"""Typed configuration for the app-to-server subprocess communication channel.

The app spawns a `langgraph dev` subprocess and passes configuration via
environment variables prefixed with `DEEPAGENTS_CODE_SERVER_`. This module
provides a single
`ServerConfig` dataclass that both sides share so that the set of variables,
their serialization format, and their default values are defined in one place.
The app writes config with `to_env()` and the server graph reads it back
with `from_env()`.
"""
# [해설] ── 모듈 개요 ─────────────────────────────────────────────
# [해설] 역할: 클라이언트 → 서버 서브프로세스 설정 채널의 **단일 스키마**(`ServerConfig`)와 env 직렬화 규칙.
# [해설] 실행 위치: **양쪽 모두**.
# [해설]   - 클라이언트: `client/launch/server_manager.py`가 `ServerConfig.from_cli_args` → `to_env` → os.environ 기록.
# [해설]   - 서버: `server_graph.py`, `offload_api.py`가 `ServerConfig.from_env`로 복원하고 `resolve_workspace`로 워크스페이스별 정책 계산.
# [해설] 핵심 개념: 워크스페이스 정책 필드는 두 부류로 나뉜다.
# [해설]   SESSION_WORKSPACE_FIELDS(클라이언트가 "주장" 가능) / PROJECT_WORKSPACE_FIELDS(서버가 디렉터리별로만 결정).
# [해설] 관련 모듈: `workspace.py`(바인딩·fingerprint), `offload_api.py:workspace`(주장 검증 → 409).
# [해설] 관련 분석: analysis/01-boot-client-server.md (설계 포인트 1·6), analysis/03-config-models-credentials.md
# [해설] 관련 문서: 공식 문서에는 `DEEPAGENTS_CODE_SERVER_*` 설명이 없다(내부 채널). CLI 플래그 의미는 docs_official/code/cli-reference.md.
# [해설][설계] 서버 쪽에서 env는 변조 가능하다고 가정한다 → 보안 제어값(ALLOW_FS_TOOLS 등)은 fail-closed로 재검증.

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from deepagents_code._constants import DEFAULT_AGENT_NAME as DEFAULT_ASSISTANT_ID
from deepagents_code._env_vars import SERVER_ENV_PREFIX

if TYPE_CHECKING:
    from collections.abc import Mapping

    from deepagents import FsToolName

    from deepagents_code.project_utils import ProjectContext

logger = logging.getLogger(__name__)


# [해설] 세션 범위 정책 필드: 클라이언트 자신의 CLI 플래그에서 온 값이라, 클라이언트가 claim으로 보내면
# [해설]   서버는 "양쪽이 같은 값을 알고 있는지"만 확인한다. `to_session_workspace_claim`/`workspace._binding_differs`에서 사용.
SESSION_WORKSPACE_FIELDS = frozenset(
    {
        "allow_fs_tools",
        "assistant_id",
        "auto_approve",
        "enable_ask_user",
        "enable_interpreter",
        "enable_memory",
        "enable_shell",
        "enable_skills",
        "interactive",
        "interpreter_ptc",
        "interpreter_ptc_acknowledge_unsafe",
        "interrupt_shell_only",
        "no_mcp",
        "recursion_limit",
        "sandbox_id",
        "sandbox_snapshot_name",
        "sandbox_type",
        "shell_allow_list",
    }
)
"""Policy a managed client may claim for its own command invocation.

These come from the client's own CLI flags, so the client already knows them
and claiming them proves only that both sides agree. Together with
`PROJECT_WORKSPACE_FIELDS` this must partition `to_workspace_payload()`
exactly: a payload field in neither set is never verified against a client
claim and never checked for project drift.
`test_workspace_claim_partitions_every_policy_field` pins that.
"""
# [해설] 프로젝트 범위 정책 필드: MCP 서버·sandbox setup 스크립트·Python 확장 등 "코드 실행 권한"과 직결.
# [해설]   클라이언트가 이 키를 claim하면 `offload_api.workspace`가 409로 거절하고, 서버가 디렉터리별로 직접 해석한다.
# [해설]   `server_graph._resolve_bound_workspace_config`/`workspace.drifted_project_fields`의 drift 비교 대상.
PROJECT_WORKSPACE_FIELDS = frozenset(
    {
        "extension_paths",
        "mcp_config_path",
        "sandbox_setup",
        "trust_project_extensions",
        "trust_project_mcp",
    }
)
"""Policy the server must resolve per project directory, never accept.

Each of these grants code execution scoped to a checkout -- MCP servers,
sandbox setup commands, Python extensions. A client that could claim them could
execute one directory's configuration against another directory's trust
decision.
"""


# [해설] 두 경로가 같은 프로젝트 디렉터리인지(device+inode 비교). 판단 불가/미설정이면 "다름"(fail-closed).
# [해설] 호출: `ServerConfig.resolve_workspace`. "다름"이면 프로젝트 정책을 버리는 쪽으로 간다.
def _same_workspace_project(first: str | None, second: str) -> bool:
    """Whether two paths name the same project directory.

    Fails closed: an unset launch root, a missing path, or an undecidable
    comparison counts as *different*, so the caller drops project policy rather
    than carrying it across an unverified boundary. `_same_directory` compares
    by device and inode, so a symlinked or differently cased spelling of one
    directory still compares equal.

    Returns:
        `True` only when both paths name the same directory.
    """
    if first is None:
        return False
    from deepagents_code._paths import DeepAgentsHomeError, _same_directory

    try:
        return _same_directory(Path(first), Path(second))
    except DeepAgentsHomeError:
        logger.warning(
            "Could not compare project directories %s and %s; treating as "
            "separate projects, so project-scoped policy will not apply",
            first,
            second,
            exc_info=True,
        )
        return False


# [해설] 아래 `_read_env_*` 헬퍼들은 서버 프로세스에서 `from_env`가 사용한다. 접두사는 `_env_vars.SERVER_ENV_PREFIX`.
# [해설] bool: 'true'(대소문자 무시)만 True, 그 외 모든 값은 False. 변수 없음이면 default.
def _read_env_bool(suffix: str, *, default: bool = False) -> bool:
    """Read a `DEEPAGENTS_CODE_SERVER_*` boolean from the environment.

    Boolean env vars use the `'true'` / `'false'` convention (case insensitive).
    Missing variables fall back to *default*.

    Args:
        suffix: Variable name suffix after the `DEEPAGENTS_CODE_SERVER_` prefix.
        default: Value when the variable is absent.

    Returns:
        Parsed boolean.
    """
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    return raw.lower() == "true"


# [해설] JSON 변수 읽기. 형식이 깨졌으면 조용히 넘어가지 않고 ValueError(값 앞 200자만 메시지에 포함).
def _read_env_json(suffix: str) -> Any:  # noqa: ANN401
    """Read a JSON-encoded `DEEPAGENTS_CODE_SERVER_*` variable.

    Args:
        suffix: Variable name suffix after the `DEEPAGENTS_CODE_SERVER_` prefix.

    Returns:
        Parsed JSON value, or `None` if the variable is absent.

    Raises:
        ValueError: If the variable is present but not valid JSON.
    """
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = (
            f"Failed to parse {SERVER_ENV_PREFIX}{suffix} as JSON: {exc}. "
            f"Value was: {raw[:200]!r}"
        )
        raise ValueError(msg) from exc


# [해설] JSON 문자열 리스트(예: EXTENSION_PATHS). 없음 → 빈 튜플, 모양이 틀리면 ValueError.
def _read_env_str_list(suffix: str) -> tuple[str, ...]:
    raw = _read_env_json(suffix)
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return tuple(raw)
    msg = f"Invalid {SERVER_ENV_PREFIX}{suffix}: expected a JSON string list"
    raise ValueError(msg)


# [해설] 파일시스템 도구 allowlist(`--allow-fs-tools`)를 서버에서 읽는다. 보안 제어이므로 fail-closed.
# [해설] 없음 → None(무제한), 비어있지 않은 유효 FsToolName 리스트 → 그대로, 그 외 → ValueError.
# [해설][SDK] 유효 이름 집합은 SDK `deepagents.FsToolName` Literal에서 `get_args`로 뽑는다.
def _read_env_allow_fs_tools() -> list[FsToolName] | None:
    """Read and shape-validate the `ALLOW_FS_TOOLS` filesystem allowlist.

    The parent writes only an absent variable (unrestricted — `None`, which is
    also what `--allow-fs-tools all` collapses to) or a non-empty JSON list of
    tool names (`main._parse_allow_fs_tools_flag`). This runs in the server
    subprocess, where the variable could be tampered with, so — because the
    value is a security control — any unrecognized shape must fail closed
    (raise) rather than fall through to an unrestricted filesystem.
    (`_read_env_json` already fails closed on malformed JSON.)

    `[]` and unknown tool names are rejected here, not deferred downstream, so
    the returned list genuinely satisfies `list[FsToolName]` and the `cast`
    asserts membership that was actually checked. Importing `deepagents` here is
    fine: the subprocess already imports the SDK to build the agent (this is not
    the arg-parsing hot path guarded in `main`). The `"read_file"` requirement
    is not checked here: `ServerConfig.__post_init__` enforces it when the
    returned value is placed on the config (with `FilesystemMiddleware` as a
    final backstop), so a tampered list without `read_file` still fails closed
    at construction.

    Returns:
        `None` when the variable is absent, or a non-empty list of filesystem
            tool-name strings, each a valid `FsToolName`.

    Raises:
        ValueError: If the present variable parses to anything other than a
            non-empty list of strings, or if any list element is not a
            recognized filesystem tool name.
    """
    env_name = f"{SERVER_ENV_PREFIX}ALLOW_FS_TOOLS"
    if env_name not in os.environ:
        return None

    raw = _read_env_json("ALLOW_FS_TOOLS")
    if isinstance(raw, list) and raw and all(isinstance(name, str) for name in raw):
        from typing import get_args

        from deepagents import FsToolName

        valid_names = frozenset(get_args(FsToolName))
        unknown = [name for name in raw if name not in valid_names]
        if unknown:
            msg = (
                f"Invalid {SERVER_ENV_PREFIX}ALLOW_FS_TOOLS value: unknown "
                f"filesystem tool name(s) {unknown!r}; valid names are "
                f"{sorted(valid_names)}."
            )
            raise ValueError(msg)
        return cast("list[FsToolName]", raw)
    msg = (
        f"Invalid {SERVER_ENV_PREFIX}ALLOW_FS_TOOLS value: {raw!r}; expected "
        "a non-empty list of filesystem tool names."
    )
    raise ValueError(msg)


# [해설] 선택 문자열 변수. 빈 문자열도 그대로 반환하므로 호출부에서 `or None`으로 정규화하는 필드가 있다.
def _read_env_str(suffix: str) -> str | None:
    """Read an optional `DEEPAGENTS_CODE_SERVER_*` string variable.

    Args:
        suffix: Variable name suffix after the `DEEPAGENTS_CODE_SERVER_` prefix.

    Returns:
        The string value, or `None` if absent.
    """
    return os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")


# [해설][주의] 정수 파싱 실패 시 예외 없이 default로 떨어진다. RECURSION_LIMIT/MAX_RETRIES 등이 잘못돼도 경고가 없다
# [해설]   (ALLOW_FS_TOOLS의 fail-closed와 대조적. 클라이언트는 항상 정수 문자열을 쓰므로 실사용에서는 드묾, 추정).
def _read_env_int(suffix: str, *, default: int | None) -> int | None:
    """Read a `DEEPAGENTS_CODE_SERVER_*` integer from the environment.

    Args:
        suffix: Variable name suffix after the `DEEPAGENTS_CODE_SERVER_` prefix.
        default: Value when the variable is absent or malformed.

    Returns:
        Parsed integer, or the default when absent or parsing fails.
    """
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# [해설] 삼상태 bool(True/False/None). TRUST_PROJECT_MCP처럼 None이 "미지정 → 기본 로직/프롬프트"를 뜻할 때 사용.
def _read_env_optional_bool(suffix: str) -> bool | None:
    """Read a tri-state `DEEPAGENTS_CODE_SERVER_*` boolean (`True` / `False` / `None`).

    Used for settings where `None` carries a distinct meaning (e.g. "not
    specified, use default logic").

    Args:
        suffix: Variable name suffix after the `DEEPAGENTS_CODE_SERVER_` prefix.

    Returns:
        `True`, `False`, or `None` when the variable is absent.
    """
    raw = os.environ.get(f"{SERVER_ENV_PREFIX}{suffix}")
    if raw is None:
        return None
    return raw.lower() == "true"


# [해설] 인터프리터(`js_eval`) 사용 여부 삼상태를 확정한다: 명시값 > 원격 샌드박스면 False > config resolver의
# [해설]   `interpreter.enable_interpreter` 기본값. 호출: `from_cli_args`(클라이언트 프로세스).
def _resolve_enable_interpreter(
    enable_interpreter: bool | None, sandbox_type: str | None
) -> bool:
    """Resolve the interpreter's tri-state caller option to a concrete boolean.

    Args:
        enable_interpreter: Explicit caller preference, or `None` to use the
            sandbox-aware default.
        sandbox_type: Sandbox backend identifier. Any falsy value (`None`, `""`)
            or `"none"` is treated as local execution.

    Returns:
        The explicit `enable_interpreter` value when not `None`; `False` for
            remote-sandbox defaults; otherwise the configured local default
            from `interpreter.enable_interpreter`.

    Raises:
        RuntimeError: If the interpreter option is absent from the manifest.
    """
    if enable_interpreter is not None:
        return enable_interpreter
    if sandbox_type and sandbox_type != "none":
        return False

    # [해설] 로컬 기본값은 config manifest 옵션을 resolver로 해석(관리형 정책·env·config.toml 순위 적용, analysis/03 참고).
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option
    from deepagents_code.configuration.resolver import get_config_resolver

    option = get_option("interpreter.enable_interpreter")
    if option is None:
        msg = "interpreter.enable_interpreter is missing from the config manifest"
        raise RuntimeError(msg)
    resolved = get_config_resolver().get(option)
    _emit_ranked_diagnostics(option, resolved)
    return bool(resolved.value)


# [해설] "사용자가 선택하지 않았는데 원격 샌드박스 때문에 인터프리터가 꺼졌는가"를 판정해 안내 문구 표시 여부를 결정.
# [해설] 호출: `main.py`, `app.py`(클라이언트 UI 안내).
def _interpreter_suppressed_by_sandbox(
    *, enable_interpreter: bool | None, sandbox_type: str | None, local_default: bool
) -> bool:
    """Whether a remote sandbox suppressed the otherwise-default interpreter.

    Used to decide whether to surface an advisory: returns `True` only when the
    user made no explicit choice, a remote sandbox is active, and the local
    default would have enabled it — i.e. the sandbox (not an explicit
    `--no-interpreter` opt-out, nor a disabled `[interpreter]` config) is why
    `js_eval` is unavailable.

    Takes the *raw* tri-state caller intent rather than the resolved boolean: a
    sandbox-suppressed default and an explicit `--no-interpreter` both resolve to
    `False`, so the resolved value cannot distinguish them. Any explicit choice
    (`not None`) is the user's own decision and is left unannounced.

    Args:
        enable_interpreter: The raw tri-state caller intent (`--interpreter` →
            `True`, `--no-interpreter` → `False`, unset → `None`).
        sandbox_type: Sandbox backend identifier. Any falsy value (`None`, `""`)
            or `"none"` is treated as local execution.
        local_default: The resolver-backed local-mode default;
            gating on it keeps the advisory quiet for users who disabled the
            interpreter in config.

    Returns:
        `True` when the advisory should be shown, otherwise `False`.
    """
    if enable_interpreter is not None:
        return False
    if not (sandbox_type and sandbox_type != "none"):
        return False
    return local_default


# [해설] 클라이언트 → 서버 전체 설정 페이로드(frozen dataclass). 필드 하나가 env 변수 하나(`to_env`의 키)에 대응한다.
# [해설] 생성 경로: 클라이언트 `from_cli_args`, 서버 `from_env`. 워크스페이스별 변형은 `resolve_workspace`(replace 사용).
# [해설][설계] frozen이므로 변형은 항상 `dataclasses.replace`로 새 인스턴스를 만든다.
@dataclass(frozen=True)
class ServerConfig:
    """Full configuration payload passed from the app to the server subprocess.

    Serialized to/from `DEEPAGENTS_CODE_SERVER_*` environment variables so
    that the server graph (which runs in a separate Python interpreter)
    can reconstruct the app's intent without sharing memory.
    """

    # [해설] ── 모델 관련 필드 ──
    model: str | None = None
    """Model spec string (e.g. `'anthropic:claude-opus-4-7'`); `None` lets the
    server pick its default."""

    summarization_model: str | None = None
    """Model spec used only for context-compaction summaries.

    `None` reuses the main agent model.
    """

    model_params: dict[str, Any] | None = None
    """Extra kwargs forwarded to the chat model constructor (temperature,
    max_tokens, etc.)."""

    cli_max_retries: int | None = None
    """Explicit `--max-retries` value, separate from provider model kwargs."""

    profile_overrides: dict[str, Any] | None = None
    """Model profile metadata overrides resolved by the client."""

    # [해설] ── 에이전트/승인 정책 필드 (대부분 SESSION_WORKSPACE_FIELDS) ──
    # [해설][주의] system_prompt는 env 채널(SYSTEM_PROMPT)에는 있지만 `from_cli_args`에 인자가 없어 CLI 경로로는 설정되지 않는다.
    assistant_id: str = DEFAULT_ASSISTANT_ID
    """Identifier of the agent graph to invoke on the server."""

    system_prompt: str | None = None
    """Override for the agent's system prompt; `None` uses the agent's default."""

    auto_approve: bool = False
    """Auto-approve every tool call without human-in-the-loop interrupts."""

    interrupt_shell_only: bool = False
    """Route only shell tool calls through HITL; validate others via middleware."""

    shell_allow_list: list[str] | None = None
    """Restrictive allow-list of shell commands; `None` disables the allow-list.

    Must be non-empty when set.
    """

    interactive: bool = True
    """Whether the agent runs in an interactive session (vs.
    one-shot/non-interactive)."""

    enable_shell: bool = True
    """Enable the shell execution tool on the server."""

    enable_ask_user: bool = False
    """Enable the `ask_user` tool that lets the agent prompt the user mid-run."""

    enable_memory: bool = True
    """Enable the long-term memory subsystem."""

    enable_skills: bool = True
    """Enable the skills subsystem (SKILL.md loading and skill tools)."""

    enable_interpreter: bool = False
    """Enable `CodeInterpreterMiddleware` (`js_eval` REPL) on the main agent.

    Always the resolved concrete value: `from_cli_args` collapses the tri-state
    caller option via `_resolve_enable_interpreter` before constructing the
    config, so the `bool | None` "defer to default" sentinel never reaches this
    field. The `False` default here is only the bare-constructor/`from_env`
    fallback; the user-facing default (on in local mode) is resolver-backed.

    Local-mode only; the server graph raises if a sandbox is configured and
    this flag is `True`.
    """

    interpreter_ptc: str | list[str] | None = None
    """Invocation-scoped override for `interpreter.ptc`.

    `None` means "fall through to whatever `interpreter.ptc` resolves
    to from `~/.deepagents/config.toml`". A string is one of `"safe"`/`"all"`;
    a list is an explicit allowlist of tool names that may also include the
    `"safe"` preset (expanded at agent-build time); `"all"` is rejected inside
    a list.
    """

    interpreter_ptc_acknowledge_unsafe: bool = False
    """Override for `interpreter.ptc_acknowledge_unsafe` — required when
    `interpreter_ptc="all"` is paired with non-`auto_approve` mode.
    """

    allow_fs_tools: list[FsToolName] | None = None
    """Allowlist for `FilesystemMiddleware`'s `tools` param, from
    `--allow-fs-tools`.

    `None` means "all filesystem tools" and is also what `--allow-fs-tools all`
    parses to: it leaves the SDK's own default `FilesystemMiddleware` in place
    (no replacement). A list is an explicit allowlist of filesystem tool names,
    must include `"read_file"`, and installs a restricted replacement (see
    `create_cli_agent`).
    """

    rubric_model: str | None = None
    """Grader model spec for `RubricMiddleware` (e.g. `'anthropic:...'`).

    `None` reuses the main agent model.
    """

    rubric_max_iterations: int | None = None
    """Explicit grader iterations per rubric attempt; `None` uses the SDK default."""

    auto_classifier_model: str | None = None
    """Classifier model spec for Auto mode (e.g. `'anthropic:claude-haiku-4-5'`).

    `None` falls through to `DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL`, then
    `[models].auto_classifier`, and then to the main agent model. An empty value
    round-trips to `None`, so it means "inherit", never "empty spec".
    """

    recursion_limit: int | None = None
    """Explicit main-agent LangGraph `recursion_limit` (graph step budget).

    `None` resolves from runtime configuration. An explicit value from
    `--recursion-limit` wins over the env var and `config.toml`, but managed
    config outranks the flag. Must be a positive integer when set.
    """

    # [해설] ── 샌드박스 필드 (sandbox_setup만 PROJECT 정책) ──
    sandbox_type: str | None = None
    """Sandbox backend identifier (e.g. `'daytona'`); `None` runs tools on the
    host. `'none'` is normalized to `None` in `__post_init__`."""

    sandbox_id: str | None = None
    """Existing sandbox ID to attach to; `None` creates a fresh sandbox."""

    sandbox_snapshot_name: str | None = None
    """Sandbox snapshot (langsmith) or blueprint (runloop) name; must be `None`
    when `sandbox_id` is set."""

    sandbox_setup: str | None = None
    """Absolute path to a setup script executed inside the sandbox on first attach."""

    # [해설] ── 워크스페이스 식별/프로젝트 정책 필드 ──
    cwd: str | None = None
    """User's original working directory, serialized as an absolute path."""

    project_root: str | None = None
    """Detected project root (e.g. nearest git/uv/npm boundary), or `None` when
    outside a project."""

    mcp_config_path: str | None = None
    """Absolute path to the MCP server config file; `None` disables
    MCP-from-config."""

    no_mcp: bool = False
    """Disable all MCP server connections regardless of other config."""

    trust_project_mcp: bool | None = None
    """Tri-state trust flag for project-scoped MCP servers: `True`/`False`/`None`
    (prompt user)."""

    trust_project_extensions: bool = False
    """Whether the project's Python extensions may execute for this run."""

    extension_paths: tuple[str, ...] = ()
    """Absolute one-run extension files or directories from repeatable CLI flags."""

    # [해설] 워크스페이스 바인딩에 영구 저장할 "비밀이 아닌" 자원 정책 딕셔너리.
    # [해설] 모델·모델 파라미터·cwd 등은 포함하지 않는다(모델 변경은 fingerprint(`workspace_fingerprint`) 쪽에서 감지).
    # [해설][주의] 두 FIELDS 집합이 이 딕셔너리 키를 정확히 분할해야 한다(테스트가 고정). 새 필드를 추가하면 둘 중 하나에 넣어야 한다.
    def to_workspace_payload(self) -> dict[str, Any]:
        """Return non-secret resource policy for a durable workspace binding."""
        return {
            "assistant_id": self.assistant_id,
            "auto_approve": self.auto_approve,
            "interrupt_shell_only": self.interrupt_shell_only,
            "shell_allow_list": self.shell_allow_list,
            "interactive": self.interactive,
            "enable_shell": self.enable_shell,
            "enable_ask_user": self.enable_ask_user,
            "enable_memory": self.enable_memory,
            "enable_skills": self.enable_skills,
            "enable_interpreter": self.enable_interpreter,
            "interpreter_ptc": self.interpreter_ptc,
            "interpreter_ptc_acknowledge_unsafe": (
                self.interpreter_ptc_acknowledge_unsafe
            ),
            "allow_fs_tools": self.allow_fs_tools,
            "recursion_limit": self.recursion_limit,
            "sandbox_type": self.sandbox_type,
            "sandbox_id": self.sandbox_id,
            "sandbox_snapshot_name": self.sandbox_snapshot_name,
            "sandbox_setup": self.sandbox_setup,
            "mcp_config_path": self.mcp_config_path,
            "no_mcp": self.no_mcp,
            "trust_project_mcp": self.trust_project_mcp,
            "trust_project_extensions": self.trust_project_extensions,
            "extension_paths": list(self.extension_paths),
        }

    # [해설] 클라이언트가 서버에 보내는 claim(세션 필드만). 호출: `server_manager.start_server_and_get_agent`, `app.py`
    # [해설]   → RemoteAgent.set_workspace → `POST /dcode/threads/{id}/workspace` 본문의 workspace_config.
    def to_session_workspace_claim(self) -> dict[str, Any]:
        """Return the command-scoped policy a managed client may claim.

        Returns:
            The session-scoped subset of the workspace policy.
        """
        return {
            key: value
            for key, value in self.to_workspace_payload().items()
            if key in SESSION_WORKSPACE_FIELDS
        }

    # [해설] 프로젝트 정책만 추출. `server_graph._resolve_bound_workspace_config`가 drift 비교에 사용.
    def to_project_workspace_policy(self) -> dict[str, Any]:
        """Return policy that must be resolved for each project directory.

        Returns:
            The project-scoped subset of the workspace policy.
        """
        return {
            key: value
            for key, value in self.to_workspace_payload().items()
            if key in PROJECT_WORKSPACE_FIELDS
        }

    # [해설] 세션 claim의 정규 SHA-256. 클라이언트가 config_fingerprint로 보내고 `offload_api.workspace`가 서버 계산값과 대조.
    def session_workspace_fingerprint(self) -> str:
        """Fingerprint the exact client-claimable session policy.

        Returns:
            The canonical SHA-256 fingerprint.
        """
        from deepagents_code.workspace import canonical_fingerprint

        return canonical_fingerprint(self.to_session_workspace_claim())

    # [해설] 서버가 특정 디렉터리(cwd, project_root)에 대해 신뢰 가능한 정책을 계산한다.
    # [해설] 호출: `server_graph._default_workspace_binding`, `_resolve_bound_workspace_config`, `offload_api.workspace`.
    # [해설] 규칙: launch 프로젝트와 같으면 정책 유지, 다르면 MCP·sandbox setup·CLI 확장 경로를 버리고 확장 신뢰는 신뢰 저장소에서 다시 읽음.
    def resolve_workspace(
        self,
        cwd: str,
        project_root: str | None,
    ) -> ServerConfig:
        """Resolve directory-bound policy for one server workspace.

        Project-scoped policy (`PROJECT_WORKSPACE_FIELDS`) is valid only for
        the directory it was resolved against: it came from the launch-time CLI
        and that project's trust decisions. Reusing it for another directory
        would apply one project's MCP servers, sandbox setup, and extensions to
        a different, possibly untrusted, checkout.

        So the launch project keeps its policy verbatim, and any other project
        starts from nothing: MCP and sandbox setup are *dropped* rather than
        rediscovered, and extension trust is re-read from the trust store for
        that project. `_same_workspace_project` fails closed, so an
        unresolvable path also takes the drop branch.

        Args:
            cwd: Absolute, canonical working directory for the workspace.
            project_root: Canonical project root, or `None` when the workspace
                has none. The launch cwd uses the server's explicit root when
                configured. Otherwise, extension trust is keyed on `cwd` when
                no root exists.

        Returns:
            A config whose session policy is unchanged and whose project policy
            is either the launch project's or empty.
        """
        # [해설][흐름] 1) launch cwd와 같은 디렉터리면, 서버에 명시된 project_root(정규화)를 우선 사용.
        if self.project_root is not None and _same_workspace_project(self.cwd, cwd):
            project_root = str(Path(self.project_root).expanduser().resolve())
        # [해설][흐름] 2) 비교 기준: 프로젝트 루트가 있으면 루트, 없으면 cwd.
        launch_root = self.project_root or self.cwd
        target_root = project_root or cwd
        if _same_workspace_project(launch_root, target_root):
            return replace(self, cwd=cwd, project_root=project_root)
        # [해설][흐름] 3) 다른 프로젝트 → 프로젝트 정책 초기화. trust_project_mcp=None은 "신뢰 결정 없음"(프롬프트 필요 상태).
        from deepagents_code.extensions.trust import is_project_extensions_trusted

        return replace(
            self,
            cwd=cwd,
            project_root=project_root,
            sandbox_setup=None,
            mcp_config_path=None,
            trust_project_mcp=None,
            trust_project_extensions=is_project_extensions_trusted(target_root),
            extension_paths=(),
        )

    # [해설] 확장 신뢰의 비대칭 규칙: 바인딩 당시 False였는데 지금 True면 False로 유지(새 신뢰는 새 스레드부터).
    # [해설]   반대로 True → False(철회)는 그대로 둬서 drift로 즉시 거부되게 한다. 호출: server_graph, offload_api.
    def preserve_bound_extension_trust(
        self, bound_policy: Mapping[str, object]
    ) -> ServerConfig:
        """Keep an existing thread's extension trust when a new grant appears.

        Args:
            bound_policy: Server policy persisted when the thread was bound.

        Returns:
            A config that defers new grants to new threads. Revocations remain
            visible so binding and runtime validation can reject them.
        """
        if bound_policy.get("trust_project_extensions") is False and (
            self.trust_project_extensions is True
        ):
            return replace(self, trust_project_extensions=False)
        return self

    # [해설] 워크스페이스 식별(CWD, PROJECT_ROOT)을 뺀 전체 env 직렬화의 fingerprint.
    # [해설] 모델·시스템 프롬프트 등 모든 설정 변화가 반영되므로 `server_graph._resolve_bound_workspace_config`가
    # [해설]   "서버 설정이 바인딩 후 바뀜(SERVER_CONFIG_DRIFT_REASON)"을 감지하는 기준이 된다.
    def workspace_fingerprint(self) -> str:
        """Fingerprint the resolved runtime config except workspace identity.

        Returns:
            The canonical SHA-256 fingerprint.
        """
        values = self.to_env()
        values.pop("CWD")
        values.pop("PROJECT_ROOT")
        from deepagents_code.workspace import canonical_fingerprint

        return canonical_fingerprint(values)

    # [해설] 불변식 검증 단일 지점: 직접 생성과 from_env 왕복 모두 여기를 지난다.
    # [해설] 'none' 샌드박스 정규화, 빈 shell allow-list 금지, allow_fs_tools 규칙, 정수 필드의 bool 금지(bool은 int 하위형이라 별도 검사).
    def __post_init__(self) -> None:
        """Normalize fields and validate invariants.

        Raises:
            TypeError: If `rubric_max_iterations` or `recursion_limit` is a
                boolean.
            ValueError: If `shell_allow_list` is an empty list,
                `allow_fs_tools` is an empty list or omits `"read_file"`, or
                `rubric_max_iterations` / `recursion_limit` is non-positive.
        """
        if self.sandbox_type == "none":
            object.__setattr__(self, "sandbox_type", None)
        if self.shell_allow_list is not None and len(self.shell_allow_list) == 0:
            msg = "shell_allow_list must be None or non-empty"
            raise ValueError(msg)
        # `allow_fs_tools` is a security control: `None` means unrestricted, but
        # an explicit list must be a usable allowlist. Own the non-empty +
        # `read_file`-required invariant here (the single authoritative point
        # for both the env round-trip via `from_env` and direct construction)
        # rather than deferring to `FilesystemMiddleware`, which would only
        # surface the violation a process boundary away. `_parse_allow_fs_tools_flag`
        # still enforces the same rule at the CLI for a friendlier error.
        if self.allow_fs_tools is not None:
            if len(self.allow_fs_tools) == 0:
                msg = "allow_fs_tools must be None or a non-empty list"
                raise ValueError(msg)
            if "read_file" not in self.allow_fs_tools:
                msg = "allow_fs_tools must include 'read_file'"
                raise ValueError(msg)
        if isinstance(self.rubric_max_iterations, bool):
            msg = "rubric_max_iterations must be None or a positive integer"
            raise TypeError(msg)
        if self.rubric_max_iterations is not None and self.rubric_max_iterations <= 0:
            msg = "rubric_max_iterations must be None or a positive integer"
            raise ValueError(msg)
        if isinstance(self.cli_max_retries, bool):
            msg = "cli_max_retries must be None or a non-negative integer"
            raise TypeError(msg)
        if self.cli_max_retries is not None and self.cli_max_retries < 0:
            msg = "cli_max_retries must be None or a non-negative integer"
            raise ValueError(msg)
        if isinstance(self.recursion_limit, bool):
            msg = "recursion_limit must be None or a positive integer"
            raise TypeError(msg)
        if self.recursion_limit is not None and self.recursion_limit <= 0:
            msg = "recursion_limit must be None or a positive integer"
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    # [해설] env 직렬화. 규칙: bool → 'true'/'false', dict/list → JSON, shell_allow_list → 콤마 결합, None → 변수 삭제.
    # [해설] 호출: `server_manager._apply_server_config`(클라이언트), `workspace_fingerprint`.
    # [해설][주의] SHELL_ALLOW_LIST는 JSON이 아닌 콤마 결합이라 콤마를 포함한 명령 항목은 왕복 시 쪼개진다.
    def to_env(self) -> dict[str, str | None]:
        """Serialize this config to a `DEEPAGENTS_CODE_SERVER_*` env-var mapping.

        `None` values signal that the variable should be *cleared* from the
        environment (rather than set to an empty string), so callers can
        iterate and set or clear each variable in `os.environ`.

        Returns:
            Dict mapping env-var suffixes (without the prefix) to their
                string values or `None`.
        """
        return {
            "MODEL": self.model,
            "SUMMARIZATION_MODEL": self.summarization_model,
            "MODEL_PARAMS": (
                json.dumps(self.model_params) if self.model_params is not None else None
            ),
            "MAX_RETRIES": (
                str(self.cli_max_retries) if self.cli_max_retries is not None else None
            ),
            "PROFILE_OVERRIDES": (
                json.dumps(self.profile_overrides)
                if self.profile_overrides is not None
                else None
            ),
            "ASSISTANT_ID": self.assistant_id,
            "SYSTEM_PROMPT": self.system_prompt,
            "AUTO_APPROVE": str(self.auto_approve).lower(),
            "INTERRUPT_SHELL_ONLY": str(self.interrupt_shell_only).lower(),
            "SHELL_ALLOW_LIST": (
                ",".join(self.shell_allow_list)
                if self.shell_allow_list is not None
                else None
            ),
            "INTERACTIVE": str(self.interactive).lower(),
            "ENABLE_SHELL": str(self.enable_shell).lower(),
            "ENABLE_ASK_USER": str(self.enable_ask_user).lower(),
            "ENABLE_MEMORY": str(self.enable_memory).lower(),
            "ENABLE_SKILLS": str(self.enable_skills).lower(),
            "ENABLE_INTERPRETER": str(self.enable_interpreter).lower(),
            "INTERPRETER_PTC": (
                json.dumps(self.interpreter_ptc)
                if self.interpreter_ptc is not None
                else None
            ),
            "INTERPRETER_PTC_ACKNOWLEDGE_UNSAFE": str(
                self.interpreter_ptc_acknowledge_unsafe
            ).lower(),
            "ALLOW_FS_TOOLS": (
                json.dumps(self.allow_fs_tools)
                if self.allow_fs_tools is not None
                else None
            ),
            "RUBRIC_MODEL": self.rubric_model,
            "AUTO_CLASSIFIER_MODEL": self.auto_classifier_model,
            "RUBRIC_MAX_ITERATIONS": (
                str(self.rubric_max_iterations)
                if self.rubric_max_iterations is not None
                else None
            ),
            "RECURSION_LIMIT": (
                str(self.recursion_limit) if self.recursion_limit is not None else None
            ),
            "SANDBOX_TYPE": self.sandbox_type,
            "SANDBOX_ID": self.sandbox_id,
            "SANDBOX_SNAPSHOT_NAME": self.sandbox_snapshot_name,
            "SANDBOX_SETUP": self.sandbox_setup,
            "CWD": self.cwd,
            "PROJECT_ROOT": self.project_root,
            "MCP_CONFIG_PATH": self.mcp_config_path,
            "NO_MCP": str(self.no_mcp).lower(),
            "TRUST_PROJECT_MCP": (
                str(self.trust_project_mcp).lower()
                if self.trust_project_mcp is not None
                else None
            ),
            "TRUST_PROJECT_EXTENSIONS": str(self.trust_project_extensions).lower(),
            # [해설] 확장 경로는 빈 튜플이면 변수 자체를 지운다(None).
            "EXTENSION_PATHS": (
                json.dumps(self.extension_paths) if self.extension_paths else None
            ),
        }

    # [해설] env 역직렬화(서버 프로세스). `to_env`와 키가 1:1 대응해야 한다 — 한쪽만 추가하면 설정이 조용히 누락된다.
    # [해설] 호출: `server_graph._make_graphs`/`get_server_runtime`/`_resolve_bound_workspace_config`, `offload_api.workspace`.
    @classmethod
    def from_env(cls) -> ServerConfig:
        """Reconstruct a `ServerConfig` from `DEEPAGENTS_CODE_SERVER_*` env vars.

        This is the inverse of `to_env()` and is called inside the server
        subprocess to recover the app's configuration.

        Returns:
            A `ServerConfig` populated from the environment.
        """
        return cls(
            model=_read_env_str("MODEL"),
            # [해설] `or None`: 빈 문자열을 "미지정"으로 정규화하는 필드(summarization/rubric/auto_classifier/snapshot).
            summarization_model=_read_env_str("SUMMARIZATION_MODEL") or None,
            model_params=_read_env_json("MODEL_PARAMS"),
            cli_max_retries=_read_env_int("MAX_RETRIES", default=None),
            profile_overrides=_read_env_json("PROFILE_OVERRIDES"),
            assistant_id=_read_env_str("ASSISTANT_ID") or DEFAULT_ASSISTANT_ID,
            system_prompt=_read_env_str("SYSTEM_PROMPT"),
            auto_approve=_read_env_bool("AUTO_APPROVE"),
            interrupt_shell_only=_read_env_bool("INTERRUPT_SHELL_ONLY"),
            # [해설] 콤마 분리 후 공백 제거, 결과가 비면 None(→ __post_init__의 빈 리스트 금지 규칙을 피함).
            shell_allow_list=(
                [cmd.strip() for cmd in raw.split(",") if cmd.strip()]
                if (raw := _read_env_str("SHELL_ALLOW_LIST"))
                else None
            )
            or None,
            interactive=_read_env_bool("INTERACTIVE", default=True),
            enable_shell=_read_env_bool("ENABLE_SHELL", default=True),
            enable_ask_user=_read_env_bool("ENABLE_ASK_USER"),
            enable_memory=_read_env_bool("ENABLE_MEMORY", default=True),
            enable_skills=_read_env_bool("ENABLE_SKILLS", default=True),
            enable_interpreter=_read_env_bool("ENABLE_INTERPRETER"),
            interpreter_ptc=_read_env_json("INTERPRETER_PTC"),
            interpreter_ptc_acknowledge_unsafe=_read_env_bool(
                "INTERPRETER_PTC_ACKNOWLEDGE_UNSAFE"
            ),
            # [해설] 보안 제어: 모양 검사 실패 시 ValueError로 서버 빌드가 실패한다(fail-closed).
            allow_fs_tools=_read_env_allow_fs_tools(),
            rubric_model=_read_env_str("RUBRIC_MODEL") or None,
            auto_classifier_model=_read_env_str("AUTO_CLASSIFIER_MODEL") or None,
            rubric_max_iterations=_read_env_int("RUBRIC_MAX_ITERATIONS", default=None),
            recursion_limit=_read_env_int("RECURSION_LIMIT", default=None),
            sandbox_type=_read_env_str("SANDBOX_TYPE"),
            sandbox_id=_read_env_str("SANDBOX_ID"),
            sandbox_snapshot_name=_read_env_str("SANDBOX_SNAPSHOT_NAME") or None,
            sandbox_setup=_read_env_str("SANDBOX_SETUP"),
            cwd=_read_env_str("CWD"),
            project_root=_read_env_str("PROJECT_ROOT"),
            mcp_config_path=_read_env_str("MCP_CONFIG_PATH"),
            no_mcp=_read_env_bool("NO_MCP"),
            trust_project_mcp=_read_env_optional_bool("TRUST_PROJECT_MCP"),
            trust_project_extensions=_read_env_bool("TRUST_PROJECT_EXTENSIONS"),
            extension_paths=_read_env_str_list("EXTENSION_PATHS"),
        )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    # [해설] CLI 인자 → ServerConfig (클라이언트 프로세스). 호출: `client/launch/server_manager.start_server_and_get_agent`.
    # [해설] 상대 경로(MCP config, sandbox setup, extension)를 사용자 cwd 기준 절대 경로로 바꾸고, 인터프리터 삼상태를 확정한다.
    # [해설] 서버 서브프로세스는 다른 cwd(`ServerProcess._spawn_process`가 지정하는 임시 작업 디렉터리)에서 돌기 때문에 상대 경로를 그대로 넘기면 안 된다.
    @classmethod
    def from_cli_args(
        cls,
        *,
        project_context: ProjectContext | None,
        model_name: str | None,
        summarization_model: str | None = None,
        model_params: dict[str, Any] | None,
        cli_max_retries: int | None = None,
        profile_overrides: dict[str, Any] | None = None,
        assistant_id: str,
        auto_approve: bool,
        interrupt_shell_only: bool = False,
        shell_allow_list: list[str] | None = None,
        sandbox_type: str = "none",
        sandbox_id: str | None,
        sandbox_snapshot_name: str | None,
        sandbox_setup: str | None,
        enable_shell: bool,
        enable_ask_user: bool,
        enable_interpreter: bool | None = None,
        interpreter_ptc: str | list[str] | None = None,
        interpreter_ptc_acknowledge_unsafe: bool = False,
        allow_fs_tools: list[FsToolName] | None = None,
        rubric_model: str | None = None,
        rubric_max_iterations: int | None = None,
        auto_classifier_model: str | None = None,
        recursion_limit: int | None = None,
        mcp_config_path: str | None,
        no_mcp: bool,
        trust_project_mcp: bool | None,
        interactive: bool,
        trust_project_extensions: bool = False,
        extension_paths: tuple[str, ...] = (),
    ) -> ServerConfig:
        """Build a `ServerConfig` from parsed CLI arguments.

        Handles path normalization (e.g. resolving relative MCP config paths
        against the user's working directory) so that the raw serialized values
        are always absolute and unambiguous.

        Args:
            project_context: Explicit user/project path context.
            model_name: Model spec string.
            summarization_model: Model spec used only for context-compaction
                summaries; `None` reuses the main model.
            model_params: Extra model kwargs.
            cli_max_retries: Explicit `--max-retries` value.
            profile_overrides: Model profile metadata overrides.
            assistant_id: Agent identifier.
            auto_approve: Auto-approve all tools.
            interrupt_shell_only: Validate shell commands via middleware instead
                of HITL.
            shell_allow_list: Restrictive shell allow-list to forward to the
                server subprocess for `ShellAllowListMiddleware`.
            sandbox_type: Sandbox type.
            sandbox_id: Existing sandbox ID to reuse.
            sandbox_snapshot_name: Snapshot (langsmith) or blueprint (runloop)
                name to use or create.
            sandbox_setup: Path to setup script for the sandbox.
            enable_shell: Enable shell execution tools.
            enable_ask_user: Enable ask_user tool.
            enable_interpreter: Enable `CodeInterpreterMiddleware` on the main
                agent. `None` uses the sandbox-aware default.
            interpreter_ptc: Invocation-scoped PTC allowlist override.
            interpreter_ptc_acknowledge_unsafe: Explicit acknowledgement for
                an invocation-scoped `interpreter_ptc="all"`.
            allow_fs_tools: Allowlist for `FilesystemMiddleware`'s `tools`
                param to forward to the server subprocess. `None` leaves the
                SDK default (all tools).
            rubric_model: Grader model spec; `None` reuses the main model.
            rubric_max_iterations: Explicit grader iterations per rubric attempt;
                `None` uses the SDK default.
            auto_classifier_model: Auto classifier model spec; `None` resolves from
                env / `config.toml` and then reuses the main model.
            recursion_limit: Explicit main-agent `recursion_limit`; `None` resolves
                from runtime configuration at agent-build time.
            mcp_config_path: Path to MCP config.
            no_mcp: Disable MCP.
            trust_project_mcp: Trust project MCP servers.
            interactive: Whether the agent is interactive.
            trust_project_extensions: Allow project extension execution.
            extension_paths: Explicit one-run extension files or directories.

        Returns:
            A fully resolved `ServerConfig`.
        """
        # [해설][흐름] 1) 경로 정규화 (실패 시 ValueError — 서버 기동 전에 사용자에게 보고).
        normalized_mcp = _normalize_path(mcp_config_path, project_context, "MCP config")

        # [해설][흐름] 2) 인터프리터 활성 여부 확정(config resolver 조회가 필요하므로 클라이언트에서 한 번만).
        resolved_enable_interpreter = _resolve_enable_interpreter(
            enable_interpreter, sandbox_type
        )

        # [해설][흐름] 3) 생성 → __post_init__ 검증. cwd/project_root는 ProjectContext에서 문자열로.
        return cls(
            model=model_name,
            summarization_model=summarization_model,
            model_params=model_params,
            cli_max_retries=cli_max_retries,
            profile_overrides=profile_overrides,
            assistant_id=assistant_id,
            auto_approve=auto_approve,
            interrupt_shell_only=interrupt_shell_only,
            shell_allow_list=shell_allow_list,
            interactive=interactive,
            enable_shell=enable_shell,
            enable_ask_user=enable_ask_user,
            enable_interpreter=resolved_enable_interpreter,
            interpreter_ptc=interpreter_ptc,
            interpreter_ptc_acknowledge_unsafe=interpreter_ptc_acknowledge_unsafe,
            allow_fs_tools=allow_fs_tools,
            rubric_model=rubric_model,
            rubric_max_iterations=rubric_max_iterations,
            auto_classifier_model=auto_classifier_model,
            recursion_limit=recursion_limit,
            sandbox_type=sandbox_type,
            sandbox_id=sandbox_id,
            sandbox_snapshot_name=sandbox_snapshot_name,
            sandbox_setup=_normalize_path(
                sandbox_setup, project_context, "sandbox setup"
            ),
            cwd=(
                str(project_context.user_cwd) if project_context is not None else None
            ),
            project_root=(
                str(project_context.project_root)
                if project_context is not None
                and project_context.project_root is not None
                else None
            ),
            mcp_config_path=normalized_mcp,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            trust_project_extensions=trust_project_extensions,
            # [해설] 빈 문자열 경로는 _normalize_path가 None을 돌려주므로 walrus 조건으로 걸러진다.
            extension_paths=tuple(
                path
                for raw in extension_paths
                if (path := _normalize_path(raw, project_context, "extension"))
            ),
        )


# [해설] 상대 경로를 절대 경로로 해석. ProjectContext가 있으면 사용자 cwd 기준(`resolve_user_path`), 없으면 프로세스 cwd 기준.
# [해설] OSError는 사람이 읽을 ValueError로 감싼다.
def _normalize_path(
    raw_path: str | None,
    project_context: ProjectContext | None,
    label: str,
) -> str | None:
    """Resolve a possibly-relative path to absolute.

    The server subprocess runs in a different working directory, so relative
    paths must be resolved against the user's original cwd before serialization.

    Args:
        raw_path: Path from CLI arguments (may be relative).
        project_context: User/project context for path resolution.
        label: Human-readable label for error messages (e.g. "MCP config").

    Returns:
        Absolute path string, or `None` when *raw_path* is `None` or empty.

    Raises:
        ValueError: If the path cannot be resolved.
    """
    if not raw_path:
        return None
    try:
        if project_context is not None:
            return str(project_context.resolve_user_path(raw_path))
        return str(Path(raw_path).expanduser().resolve())
    except OSError as exc:
        msg = (
            f"Could not resolve {label} path {raw_path!r}: {exc}. "
            "Ensure the path exists and is accessible."
        )
        raise ValueError(msg) from exc
