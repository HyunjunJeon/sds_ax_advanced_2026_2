"""Durable, server-authoritative thread workspace bindings."""
# [해설] ── 모듈 개요 ─────────────────────────────────────────────
# [해설] 역할: 스레드(thread_id) ↔ 워크스페이스(cwd·project_root·자원 정책)의 **영구·서버 권위** 바인딩.
# [해설]   `/restart`, cwd 전환, 여러 워크스페이스 전환 중에도 한 스레드의 실행 디렉터리와 정책이 바뀌지 않게 강제한다.
# [해설] 저장소: 체크포인트와 같은 `sessions.db`(SQLite)의 `dcode_thread_workspaces` 테이블.
# [해설] 실행 위치: **서버 프로세스**(langgraph dev 안의 `offload_api.py`, `server_graph.py`). DB 경로는
# [해설]   클라이언트가 넘긴 `DEEPAGENTS_CODE_SERVER_DB_PATH` env(없으면 `sessions.get_db_path`).
# [해설] 주요 진입점: `bind_thread_workspace`(바인딩 생성/검증, offload_api.workspace 라우트),
# [해설]   `require_thread_workspace`(매 run의 context 검증, server_graph.make_graph), `resolve_workspace`(정규화·fingerprint).
# [해설] 관련 분석: analysis/01-boot-client-server.md (흐름 E-6, 설계 포인트 6), analysis/04-approval-hitl-security.md
# [해설] 관련 문서: 공식 문서에는 이 테이블 설명이 없다(코드에만 존재). `sessions.delete_thread`는 이 테이블 행을 지우지 않는다(analysis/01).

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, TypedDict, cast

from deepagents_code._env_vars import SERVER_ENV_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

# [해설] 바인딩 스키마 세대. v3 미만 행은 `_bind`에서 마이그레이션되고, `require_thread_workspace`는 v3만 허용한다.
_SCHEMA_VERSION = 3
"""Binding schema generation.

Version 3 resolves project policy per workspace. Version 2 used the launch
config's fingerprint for every directory, so its project policy values may
differ from the newly resolved ones. After checking workspace identity and
session policy, `_bind` migrates old rows instead of rejecting that mismatch
as configuration drift.
"""
# [해설] 입력 크기 상한: 경로 4096자, 정규 JSON 정책 64,000자. 클라이언트가 보낸 값으로 DB가 비대해지는 것을 막는다.
_MAX_PATH_LENGTH = 4096
_MAX_CONFIG_LENGTH = 64_000


# [해설] LangGraph runtime context(`context={"workspace": ...}`)에 실리는 공개 표현. 정책 JSON 본문은 제외된다.
# [해설] 클라이언트는 `POST /dcode/threads/{id}/workspace` 응답으로 받아 이후 run마다 그대로 되돌려 보낸다(`client/remote_client.py`).
class WorkspacePayload(TypedDict):
    """JSON-safe workspace descriptor carried in LangGraph runtime context."""

    schema_version: int
    workspace_id: str
    cwd: str
    project_root: str | None
    generation: int
    resource_key: str
    config_fingerprint: str


# [해설] DB 한 행에 대응하는 불변 바인딩.
# [해설] workspace_id = hash(cwd, project_root) — 디렉터리 정체성.
# [해설] resource_key = hash(workspace_id, config_fingerprint) — 런타임 캐시 키(`server_graph._workspace_runtimes`).
# [해설] generation은 현재 항상 1로 생성된다(추정: 향후 재바인딩 세대용 예약).
@dataclass(frozen=True)
class WorkspaceBinding:
    """Server-authoritative workspace and resource policy for one thread."""

    schema_version: int
    workspace_id: str
    cwd: str
    project_root: str | None
    generation: int
    resource_key: str
    config_fingerprint: str
    workspace_config_json: str

    def to_payload(self) -> WorkspacePayload:
        """Return the public runtime-context representation."""
        payload = asdict(self)
        payload.pop("workspace_config_json")
        return cast("WorkspacePayload", payload)

    def workspace_config(self) -> dict[str, Any]:
        """Return the persisted, server-authoritative resource policy."""
        return cast("dict[str, Any]", json.loads(self.workspace_config_json))


# [해설] 워크스페이스 claim/런타임이 서버 정책과 충돌할 때의 예외. offload_api에서 HTTP 409로 매핑된다.
class WorkspaceConflictError(RuntimeError):
    """A workspace claim or runtime conflicts with server resource policy."""

    @classmethod
    def from_reason(cls, reason: str) -> WorkspaceConflictError:
        """Build a workspace-hosting refusal with a stated reason.

        Returns:
            A conflict with the standard workspace-hosting message.
        """
        msg = f"Cannot host this workspace because {reason}."
        return cls(msg)


# [해설] 바인딩 DB 경로: 서버 env `DEEPAGENTS_CODE_SERVER_DB_PATH` 우선(클라이언트 server_manager가 설정),
# [해설]   없으면 프로필 state 디렉터리의 sessions.db.
def _database_path() -> Path:
    value = os.environ.get(f"{SERVER_ENV_PREFIX}DB_PATH")
    if value:
        return Path(value)
    from deepagents_code.sessions import get_db_path

    return get_db_path()


# [해설] 신뢰할 수 없는 경로 문자열을 검증·정규화한다(보안 검사).
# [해설] 1) 비어있지 않은 str·길이 상한 2) 절대 경로 + `..` 금지 3) (POSIX) SDK `validate_path`
# [해설] 4) strict resolve(존재해야 함, 심볼릭 링크 해소) 5) 디렉터리 확인 6) 해소된 경로로 다시 `validate_path`.
# [해설][SDK] `deepagents/backends/utils.py:validate_path` — 백엔드 경로 검증 규칙을 재사용.
def _canonical_directory(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_LENGTH:
        msg = f"workspace.{field} must be a non-empty absolute path"
        raise ValueError(msg)
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in PurePath(value).parts:
        msg = f"workspace.{field} must be an absolute path without traversal"
        raise ValueError(msg)
    if os.name != "nt":
        from deepagents.backends.utils import validate_path

        validate_path(value)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        msg = f"workspace.{field} is unavailable: {value}"
        raise ValueError(msg) from exc
    if not resolved.is_dir():
        msg = f"workspace.{field} is not a directory: {value}"
        raise ValueError(msg)
    if os.name != "nt":
        from deepagents.backends.utils import validate_path

        validate_path(str(resolved))
    return resolved


# [해설] 정책 딕셔너리를 정규 JSON(키 정렬·공백 없음)과 SHA-256으로 변환. None은 {}로 취급.
def canonical_workspace_config(value: object | None) -> tuple[str, str]:
    """Return bounded canonical JSON and its SHA-256 fingerprint.

    Raises:
        TypeError: If the configuration is not an object.
        ValueError: If it cannot be serialized or exceeds the size limit.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        msg = "workspace_config must be an object"
        raise TypeError(msg)
    try:
        serialized = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        msg = "workspace configuration must be JSON serializable"
        raise ValueError(msg) from exc
    if len(serialized) > _MAX_CONFIG_LENGTH:
        msg = "workspace configuration is too large"
        raise ValueError(msg)
    return serialized, hashlib.sha256(serialized.encode()).hexdigest()


# [해설] 모든 fingerprint 계산이 공유하는 정규 직렬화. 클라이언트/서버가 같은 해시를 얻으려면 이 규칙이 동일해야 한다.
def _canonical_json(value: object) -> str:
    """Return JSON with consistent key ordering and spacing for fingerprinting."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# [해설] 정규 JSON의 SHA-256. `_server_config.ServerConfig.session_workspace_fingerprint`/`workspace_fingerprint`도 이 함수를 쓴다.
def canonical_fingerprint(value: object) -> str:
    """Fingerprint `value` with the canonical workspace serialization.

    Returns:
        The SHA-256 hex digest of the canonical JSON encoding.
    """
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


# [해설] cwd(+정책)를 정규 WorkspaceBinding으로 계산한다(DB 접근 없음, 순수 계산 + 파일시스템 조회).
# [해설] 호출: `bind_thread_workspace`, `require_thread_workspace`(재검증), `server_graph._default_workspace_binding`, `offload_api.workspace`.
# [해설][주의] project_root는 인자로 받지 않고 `project_utils.find_project_root`로 다시 탐지한다. 그래서 나중에
# [해설]   `git init` 등으로 루트가 바뀌면 workspace_id가 달라져 기존 스레드가 "identity changed"로 거부될 수 있다(추정).
def resolve_workspace(
    cwd: object,
    workspace_config: object | None = None,
    *,
    config_fingerprint: str | None = None,
) -> WorkspaceBinding:
    """Resolve a client-supplied cwd into a canonical workspace binding.

    `cwd` is untrusted and is validated here. `workspace_config` is not: every
    caller passes server-resolved policy. A client claim is verified against
    server policy in `offload_api.workspace` and never reaches this function.

    Returns:
        A canonical, fingerprinted binding including the resource policy.
    """
    # [해설][흐름] 1) 정책 정규화 → 명시 fingerprint가 없으면 정책 자체의 해시 사용.
    config_json, policy_fingerprint = canonical_workspace_config(workspace_config)
    config_fingerprint = config_fingerprint or policy_fingerprint
    # [해설][흐름] 2) cwd 검증·정규화 → 프로젝트 루트 탐지·검증.
    canonical_cwd = _canonical_directory(cwd, field="cwd")
    from deepagents_code.project_utils import find_project_root

    project_root = find_project_root(canonical_cwd)
    if project_root is not None:
        project_root = _canonical_directory(str(project_root), field="project_root")
    # [해설][흐름] 3) 정체성 해시(workspace_id)와 자원 키(resource_key) 계산.
    workspace_id = canonical_fingerprint(
        {
            "cwd": str(canonical_cwd),
            "project_root": str(project_root) if project_root else None,
        }
    )
    resource_key = canonical_fingerprint(
        {"workspace_id": workspace_id, "config_fingerprint": config_fingerprint}
    )
    return WorkspaceBinding(
        schema_version=_SCHEMA_VERSION,
        workspace_id=workspace_id,
        cwd=str(canonical_cwd),
        project_root=str(project_root) if project_root else None,
        generation=1,
        resource_key=resource_key,
        config_fingerprint=config_fingerprint,
        workspace_config_json=config_json,
    )


# [해설] 테이블 생성 + 구 스키마 컬럼 보강(ALTER TABLE). 모든 DB 접근 함수가 트랜잭션 시작 직후 호출한다.
# [해설] 오래된 행은 config_fingerprint=''(→ `_is_migratable` True)로 채워진다.
def _initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dcode_thread_workspaces (
            thread_id TEXT PRIMARY KEY NOT NULL,
            schema_version INTEGER NOT NULL,
            workspace_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            project_root TEXT,
            generation INTEGER NOT NULL,
            resource_key TEXT NOT NULL,
            config_fingerprint TEXT NOT NULL,
            workspace_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(dcode_thread_workspaces)").fetchall()
    }
    if "config_fingerprint" not in columns:
        conn.execute(
            "ALTER TABLE dcode_thread_workspaces "
            "ADD COLUMN config_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "workspace_config_json" not in columns:
        conn.execute(
            "ALTER TABLE dcode_thread_workspaces "
            "ADD COLUMN workspace_config_json TEXT NOT NULL DEFAULT '{}'"
        )


# [해설] sqlite3.Row → WorkspaceBinding 변환.
def _row_binding(row: sqlite3.Row) -> WorkspaceBinding:
    return WorkspaceBinding(
        schema_version=row["schema_version"],
        workspace_id=row["workspace_id"],
        cwd=row["cwd"],
        project_root=row["project_root"],
        generation=row["generation"],
        resource_key=row["resource_key"],
        config_fingerprint=row["config_fingerprint"],
        workspace_config_json=row["workspace_config_json"],
    )


# [해설] fingerprint가 없거나(v1 추정) 스키마가 오래된 행인지. 이런 행은 거부 대신 새 값으로 업데이트될 수 있다.
def _is_migratable(existing: WorkspaceBinding) -> bool:
    """Whether a row's recorded policy predates the current schema.

    Older rows may contain launch-project policy instead of workspace policy.
    `_binding_differs` checks workspace identity and recorded session policy
    before allowing migration.

    Returns:
        `True` when the row has no fingerprint yet, or an older schema.
    """
    return not existing.config_fingerprint or existing.schema_version < _SCHEMA_VERSION


# [해설] 기존 행과 제안 바인딩이 "충돌"하는지 판정.
# [해설] - 디렉터리 정체성이 다르면 항상 충돌.
# [해설] - 최신 스키마 행: fingerprint가 다르면 충돌.
# [해설] - 구 스키마 행: 세션 정책 필드만 비교(프로젝트 정책은 v3에서 계산법이 바뀌었으므로 비교하지 않고 마이그레이션).
def _binding_differs(existing: WorkspaceBinding, proposed: WorkspaceBinding) -> bool:
    if existing.workspace_id != proposed.workspace_id:
        return True
    if not _is_migratable(existing):
        return existing.config_fingerprint != proposed.config_fingerprint
    if not existing.config_fingerprint:
        # Pre-fingerprint rows have no recorded policy to preserve.
        return False
    from deepagents_code._server_config import SESSION_WORKSPACE_FIELDS

    bound_policy = existing.workspace_config()
    proposed_policy = proposed.workspace_config()
    return any(
        bound_policy.get(key) != proposed_policy.get(key)
        for key in SESSION_WORKSPACE_FIELDS
    )


# [해설] drift 거절 사유 문자열. `server_graph._resolve_bound_workspace_config`와 `_binding_conflict`가 공유한다.
PROJECT_POLICY_DRIFT_REASON = (
    "the project's resolved policy differs from the policy recorded "
    "when this workspace was bound"
)
SERVER_CONFIG_DRIFT_REASON = (
    "the server configuration changed after this workspace was bound"
)


# [해설] 바인딩 정책과 현재 정책 사이에서 달라진 PROJECT_WORKSPACE_FIELDS 이름 목록(정렬). 로그/오류 메시지 진단용.
def drifted_project_fields(
    bound_config: Mapping[str, Any],
    current_config: Mapping[str, Any],
) -> list[str]:
    """Name the project-scoped fields that drifted from their binding.

    The refusal these feed is safe either way, but it is not diagnosable
    without the field names: the resolution reads the extension trust store on
    every call, so a transient read failure reports as a policy change. These
    values are paths and booleans, never secrets, so naming them is safe.

    Returns:
        The drifted field names, sorted; empty when the policy is unchanged.
    """
    from deepagents_code._server_config import PROJECT_WORKSPACE_FIELDS

    return sorted(
        key
        for key in PROJECT_WORKSPACE_FIELDS
        if bound_config.get(key) != current_config.get(key)
    )


# [해설] 충돌 종류에 맞는 WorkspaceConflictError를 만든다: 다른 디렉터리 / 서버 설정 변경 / 프로젝트 정책 변경.
def _binding_conflict(
    thread_id: str,
    existing: WorkspaceBinding,
    proposed: WorkspaceBinding,
) -> WorkspaceConflictError:
    if existing.workspace_id != proposed.workspace_id:
        return WorkspaceConflictError(
            f"thread {thread_id} is already bound to a different workspace"
        )
    if _is_migratable(existing):
        return WorkspaceConflictError.from_reason(SERVER_CONFIG_DRIFT_REASON)
    drifted = drifted_project_fields(
        existing.workspace_config(),
        proposed.workspace_config(),
    )
    reason = PROJECT_POLICY_DRIFT_REASON if drifted else SERVER_CONFIG_DRIFT_REASON
    return WorkspaceConflictError.from_reason(reason)


# [해설] 바인딩 생성 또는 검증(동기, 워커 스레드에서 실행). "처음 쓴 쪽이 이긴다"는 원자적 INSERT OR IGNORE 패턴.
# [해설][흐름] 1) BEGIN IMMEDIATE로 쓰기 잠금 선점(동시 바인딩 경합 방지, busy timeout 5초)
# [해설][흐름] 2) INSERT OR IGNORE → 3) 실제 저장된 행을 다시 읽음 → 4) 충돌 검사 → 5) 구 스키마면 조건부 UPDATE로 마이그레이션.
# [해설] `with closing(...) as conn, conn:` — 바깥은 연결 닫기, 안쪽 `conn` 컨텍스트는 성공 시 commit / 예외 시 rollback.
def _bind(thread_id: str, proposed: WorkspaceBinding) -> WorkspaceBinding:
    with closing(sqlite3.connect(_database_path(), timeout=5)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _initialize(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO dcode_thread_workspaces (
                thread_id, schema_version, workspace_id, cwd, project_root,
                generation, resource_key, config_fingerprint, workspace_config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                proposed.schema_version,
                proposed.workspace_id,
                proposed.cwd,
                proposed.project_root,
                proposed.generation,
                proposed.resource_key,
                proposed.config_fingerprint,
                proposed.workspace_config_json,
            ),
        )
        row = conn.execute(
            "SELECT * FROM dcode_thread_workspaces WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            msg = f"workspace binding was not persisted for thread {thread_id}"
            raise RuntimeError(msg)
        existing = _row_binding(row)
        if _binding_differs(existing, proposed):
            raise _binding_conflict(thread_id, existing, proposed)
        if _is_migratable(existing):
            # Guard on the fingerprint this transaction actually read, so a
            # concurrent migration cannot be overwritten after the fact.
            conn.execute(
                """
                UPDATE dcode_thread_workspaces
                SET schema_version = ?, resource_key = ?, config_fingerprint = ?,
                    workspace_config_json = ?
                WHERE thread_id = ? AND config_fingerprint = ?
                """,
                (
                    proposed.schema_version,
                    proposed.resource_key,
                    proposed.config_fingerprint,
                    proposed.workspace_config_json,
                    thread_id,
                    existing.config_fingerprint,
                ),
            )
            return proposed
        return existing


# [해설] 바인딩 조회. 읽기지만 `_initialize`(DDL)가 필요할 수 있어 BEGIN IMMEDIATE로 연다.
def _read(thread_id: str) -> WorkspaceBinding | None:
    with closing(sqlite3.connect(_database_path(), timeout=5)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _initialize(conn)
        row = conn.execute(
            "SELECT * FROM dcode_thread_workspaces WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return _row_binding(row) if row is not None else None


# [해설] 비동기 공개 API: 바인딩을 원자적으로 만들거나 기존 것과 일치하는지 검증한다.
# [해설] 호출: `offload_api.workspace`(`POST /dcode/threads/{id}/workspace`, validate_only가 아닐 때).
# [해설] workspace_config는 서버가 해석한 신뢰 정책이어야 한다(클라이언트 claim 검증은 offload_api에서 선행).
async def bind_thread_workspace(
    thread_id: str,
    cwd: object,
    workspace_config: object | None = None,
    *,
    config_fingerprint: str | None = None,
) -> WorkspaceBinding:
    """Atomically create or verify a thread workspace binding.

    Returns:
        The immutable binding for the thread.

    Raises:
        ValueError: If the thread is invalid.
    """
    if not isinstance(thread_id, str) or not thread_id:
        msg = "thread_id must be non-empty"
        raise ValueError(msg)
    proposed = await asyncio.to_thread(
        resolve_workspace,
        cwd,
        workspace_config,
        config_fingerprint=config_fingerprint,
    )
    return await asyncio.to_thread(_bind, thread_id, proposed)


# [해설] 바인딩 조회(없으면 None). 호출: `offload_api.workspace`(확장 신뢰 보존 판단), offload 경로 등.
async def get_thread_workspace(thread_id: str) -> WorkspaceBinding | None:
    """Read a thread's durable workspace binding.

    Returns:
        The binding, or `None` when the thread is unbound.
    """
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return await asyncio.to_thread(_read, thread_id)


# [해설] 매 run 요청에서 클라이언트가 보낸 workspace context가 DB 바인딩과 정확히 일치하는지 검증한다.
# [해설] 호출: `server_graph.make_graph`(execution runtime이 있는 경우). 반환된 바인딩으로 `_workspace_runtime`을 고른다.
async def require_thread_workspace(
    thread_id: str,
    payload: object,
    workspace_config: object | None = None,
    *,
    config_fingerprint: str | None = None,
) -> WorkspaceBinding:
    """Validate run context against the durable workspace binding.

    Returns:
        The server-authoritative binding and persisted resource policy.

    Raises:
        TypeError: If workspace context is not an object.
        WorkspaceConflictError: If the context, policy, or workspace has changed.
    """
    # [해설][흐름] 1) payload 형식 검사, (선택) claim 정책의 fingerprint 계산.
    if not isinstance(payload, dict) or not payload:
        msg = "workspace context is required"
        raise TypeError(msg)
    data = cast("dict[str, Any]", payload)
    claimed_fingerprint = config_fingerprint
    if workspace_config is not None:
        _, claimed_fingerprint = canonical_workspace_config(workspace_config)

    # [해설][흐름] 2) DB 트랜잭션 안에서: 행 존재 → payload의 7개 공개 필드 모두 일치 → (선택) fingerprint 일치.
    # [해설][주의] 바인딩이 없는 스레드는 거부된다. 즉 클라이언트는 run 전에 반드시 workspace 라우트로 바인딩해야 한다.
    def _require() -> WorkspaceBinding:
        with closing(sqlite3.connect(_database_path(), timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            _initialize(conn)
            row = conn.execute(
                "SELECT * FROM dcode_thread_workspaces WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                msg = f"thread {thread_id} has no workspace binding"
                raise WorkspaceConflictError(msg)
            existing = _row_binding(row)
            expected = existing.to_payload()
            # [해설] payload에 추가 키가 있어도 무시하고, 바인딩 쪽 키만 비교한다.
            if any(data.get(key) != value for key, value in expected.items()):
                msg = f"workspace context does not match thread {thread_id}"
                raise WorkspaceConflictError(msg)
            if (
                claimed_fingerprint is not None
                and claimed_fingerprint != existing.config_fingerprint
            ):
                msg = f"workspace configuration does not match thread {thread_id}"
                raise WorkspaceConflictError(msg)
            return existing

    # [해설][흐름] 3) 스키마 버전 확인(v3만) → 4) 저장된 cwd를 지금 다시 정규화해 디렉터리 정체성이 그대로인지 확인
    # [해설]   (디렉터리 삭제·교체·프로젝트 루트 변화 등을 run 시점에 잡아냄).
    existing = await asyncio.to_thread(_require)
    if existing.schema_version != _SCHEMA_VERSION:
        msg = f"workspace binding schema is unsupported for thread {thread_id}"
        raise WorkspaceConflictError(msg)
    resolved = await asyncio.to_thread(
        resolve_workspace,
        existing.cwd,
        existing.workspace_config(),
        config_fingerprint=existing.config_fingerprint,
    )
    if resolved.workspace_id != existing.workspace_id:
        msg = f"workspace identity changed for thread {thread_id}"
        raise WorkspaceConflictError(msg)
    return existing
