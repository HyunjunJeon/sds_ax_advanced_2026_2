"""OAuth login flow and token storage for MCP servers.

Note: `mcp.shared.auth.OAuthToken` is a pydantic model whose default
`repr` includes the access and refresh token strings verbatim. Never
log one via `%r`, `str()`, f-string interpolation, or
`logger.exception`/`exc_info` on an exception that wraps one — the
tokens will land in stdout, log files, and error-reporting
pipelines. Pass only structural facts ("refreshed token for
server X") rather than the token itself.
"""

# [해설] 모듈 개요: 원격(http/sse) MCP 서버의 OAuth 로그인 흐름과 토큰 저장소를 구현한다.
# [해설] 실행 프로세스: 둘 다. (1) 서버 프로세스 — `mcp_tools.py`의 연결 구성 단계가 `build_oauth_provider(interactive=False)`로
# [해설] 저장된 토큰을 붙이고 만료 시 refresh한다(대화형 프롬프트 불가 → `MCPReauthRequiredError`). (2) 클라이언트 — `/mcp login`(TUI, `app.py`)과
# [해설] `dcode mcp login`이 `login`을 호출해 브라우저 loopback / paste-back / device flow로 토큰을 받는다.
# [해설] 주요 진입점: `login`, `build_oauth_provider`, `FileTokenStorage`, `_ExpiryAwareOAuthClientProvider`, `find_reauth_required`,
# [해설] `find_oauth_challenge`, `format_login_failure`, `token_store_dir`.
# [해설] 다른 모듈 관계: 호스트별 정책은 `mcp_providers/`(`resolve_provider`: GitHub device flow, Slack 고정 loopback 포트, Generic).
# [해설] `mcp_providers/github.py`가 `_run_device_flow`를, `mcp_providers/slack.py`가 `FileTokenStorage`를 사용. UI 추상화는 `mcp_oauth_ui.OAuthInteraction`.
# [해설] ${VAR} 보간은 `mcp_config.resolve_mcp_server_env`. OAuth 프로토콜 본체는 MCP Python SDK의 `mcp.client.auth.OAuthClientProvider`(상속).
# [해설][SDK] deepagents SDK(`libs/deepagents/deepagents/`)에는 MCP·OAuth 코드가 없다 — 전부 dcode + MCP Python SDK 몫.
# [해설][주의] 모듈 docstring대로 `OAuthToken`의 repr에는 토큰 원문이 들어간다. 이 파일의 로그들이 타입명·서버명만 남기는 이유.
# [해설] 관련 문서: `analysis/07-mcp-hooks-extensions-plugins.md`(핵심 설계 포인트 11, 대조표 "OAuth refresh 파라미터"),
# [해설] 공식 `docs_official/code/mcp-tools.md`(auth: oauth, 토큰 경로 `~/.deepagents/.state/mcp-tokens/<server>-<sha256-16(url)>.json`).
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import hashlib
import html
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, TypedDict, override
from urllib.parse import parse_qs, urlparse

import httpx
from anyio import CancelScope
from filelock import FileLock, Timeout
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
)
from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from deepagents_code._env_vars import DEBUG, is_env_truthy
from deepagents_code._paths import PATHS
from deepagents_code.mcp_config import resolve_mcp_server_env

if TYPE_CHECKING:
    from _thread import LockType
    from pathlib import Path

    from mcp.client.auth.oauth2 import OAuthContext

    from deepagents_code.mcp_oauth_ui import OAuthInteraction


# [해설] RFC 8628 device authorization 응답 스키마. `_run_device_flow`가 검증에 사용. interval 누락 시 5초 기본값.
class _DeviceCodeResponse(BaseModel):
    """RFC 8628 §3.2 device-authorization response payload."""

    model_config = ConfigDict(extra="ignore")

    device_code: str
    """Opaque device code the client polls with at the token endpoint."""

    user_code: str
    """Short code the user enters in the browser to approve the device."""

    verification_uri: str
    """Provider URL the user visits to complete device authorization."""

    expires_in: int
    """Lifetime of the device code in seconds."""

    interval: int = 5
    """Recommended polling interval in seconds when the provider omits one."""


# [해설] `.mcp.json`의 `mcpServers` 항목 하나의 타입 문서. 실제 검증은 `mcp_tools.py`의 `_validate_server_config`가 한다.
class McpServerSpec(TypedDict, total=False):
    """Parsed MCP server config entry.

    All keys are optional at the type level because `mcpServers` entries
    are validated shape-first by `_validate_server_config` rather than by
    the type system. This TypedDict documents the accepted shape for
    readers and static checkers — validate the fields at use sites before
    relying on them.
    """

    auth: Literal["oauth"]
    """Authentication mode for remote MCP servers that require OAuth login."""

    type: Literal["stdio", "http", "sse"]
    """Transport type when the config uses the `type` key."""

    transport: Literal["stdio", "http", "sse"]
    """Transport type when the config uses the `transport` key."""

    url: str
    """Remote endpoint URL for HTTP or SSE MCP servers."""

    headers: dict[str, str]
    """Optional request headers sent when connecting to the remote server."""

    command: str
    """Executable for stdio MCP servers."""

    args: list[str]
    """Command-line arguments passed to the stdio server executable."""

    env: dict[str, str]
    """Environment overrides for launching a stdio MCP server."""


logger = logging.getLogger(__name__)
# [해설] 비대화형(서버) 경로에서 "예상된 재인증" SDK 로그를 숨길지 표시하는 ContextVar. `async_auth_flow`가 켜고 `_ExpectedReauthLogFilter`가 읽는다.
_SUPPRESS_EXPECTED_REAUTH_LOGS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "suppress_expected_mcp_reauth_logs",
    default=False,
)


_TOKEN_REFRESH_FAILED_PREFIX = "Token refresh failed: "  # noqa: S105  # log-message prefix, not a credential
"""Prefix of the SDK's `Token refresh failed: <status>` warning (`oauth2.py`)."""

# [해설] 400/401/403만 "refresh token 자체가 만료·폐기됨"으로 보고 조용히 재로그인 안내로 대체한다. 429/5xx는 장애이므로 로그를 남긴다.
_EXPECTED_REAUTH_REFRESH_STATUSES = frozenset({"400", "401", "403"})
"""Refresh-endpoint statuses that mean the grant was rejected (token stale).

The SDK logs `Token refresh failed: <status>` and clears tokens for *any*
non-200 on the refresh endpoint. Only these statuses indicate the refresh
token itself is expired/revoked — i.e. the expected re-auth cases our hint
replaces. Transient failures (`429`, `5xx`, gateway timeouts) must stay
visible so a provider outage isn't silently relabeled as "go re-login".
"""
_EXPECTED_REAUTH_REFRESH_STATUS_CODES = frozenset(
    int(status) for status in _EXPECTED_REAUTH_REFRESH_STATUSES
)


# [해설] MCP SDK 로거(`mcp.client.auth.oauth2`)에 붙는 필터. 억제 플래그가 켜진 동안 예상 가능한 refresh 실패/재인증 오류 로그를 버린다.
# [해설] 사용자에게는 대신 `MCPReauthRequiredError` 메시지(`/mcp login` 안내)가 보인다.
class _ExpectedReauthLogFilter(logging.Filter):
    """Drop SDK OAuth log records that are replaced by our reauth hint."""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether the SDK OAuth log record should be emitted."""
        if not _SUPPRESS_EXPECTED_REAUTH_LOGS.get():
            return True
        message = record.getMessage()
        if message.startswith(_TOKEN_REFRESH_FAILED_PREFIX):
            status = message.removeprefix(_TOKEN_REFRESH_FAILED_PREFIX)
            if status in _EXPECTED_REAUTH_REFRESH_STATUSES:
                return False
        if message == "OAuth flow error" and record.exc_info is not None:
            exc = record.exc_info[1]
            if exc is not None and find_reauth_required(exc) is not None:
                return False
        return True


# [해설] 모듈 import 시점의 전역 부작용: SDK 로거에 필터를 설치한다.
logging.getLogger("mcp.client.auth.oauth2").addFilter(_ExpectedReauthLogFilter())

# [해설][주의] 보안: 서버 이름이 토큰 파일명에 들어가므로 경로 탈출(`../`)을 막는 정규식. `mcp_tools._SERVER_NAME_RE`와 동기화 필요.
_SAFE_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Matches server names that are safe to embed in token-file basenames.

Mirrors `_SERVER_NAME_RE` in `mcp_tools` — duplicated here because
`mcp_auth` cannot import from `mcp_tools` at module top-level without
risking a circular import. Keep both regexes in sync."""

_STORAGE_VERSION = 1
"""Schema version stamped into persisted credential files; bump on incompatible
shape changes so `_load_*` can reject or migrate older payloads."""

# [해설] 만료 30초 전부터 토큰을 무효로 간주해 refresh를 앞당긴다(`_apply_stored_expiry`에서 `token_expiry_time`에 반영).
_REFRESH_SAFETY_MARGIN_SECONDS = 30.0
"""Refresh access tokens this many seconds before their advertised expiry.

Absorbs clock skew and request latency so a token deemed valid locally isn't
rejected as expired by the server — without the margin, a 401 sends the SDK
into the full re-auth (browser) flow instead of the cheaper refresh grant.
"""


# [해설] headers의 `${VAR}` 참조를 해석하는 호환용 공개 헬퍼. 실제 로직은 `mcp_config.resolve_mcp_server_env`에 위임.
def resolve_headers(
    headers: dict[str, str],
    *,
    server_name: str | None = None,
) -> dict[str, str]:
    """Resolve environment-variable references in MCP header values.

    This compatibility wrapper preserves the original public helper while
    delegating interpolation and validation to the shared MCP config resolver.

    Args:
        headers: Raw header mapping from MCP config.
        server_name: Optional server name for field-specific error messages.

    Returns:
        A new dictionary with environment-variable references resolved.

    Raises:
        TypeError: If a header value is not a string.
        RuntimeError: If interpolation fails.
    """  # noqa: DOC502 - `RuntimeError` is raised by the shared config resolver
    resolved = resolve_mcp_server_env(
        server_name or "<unknown>",
        {"headers": headers},
    )
    return resolved["headers"]


# [해설] 교차 프로세스 refresh 락 최대 대기 60초. 초과하면 refresh를 건너뛴다(`_acquire_refresh_lock`).
_REFRESH_LOCK_TIMEOUT_SECONDS = 60.0
"""Longest a provider waits for the cross-process token-refresh lock.

Bounds the wait so a live-but-stuck peer (e.g. one whose refresh network call
hangs) can't block tool calls indefinitely. A peer that outright crashes is not
the concern: the OS releases an `fcntl` lock when the holding process exits. On
timeout the provider reloads tokens from disk and avoids using any still-stale
refresh token (see `_acquire_refresh_lock`).
"""

# [해설] 프로세스 내부 스레드 락 캐시(토큰 파일 경로 → Lock). 읽기-수정-쓰기 변경 연산만 직렬화한다. 교차 프로세스 락(`FileLock`)과는 별개.
_TOKEN_FILE_LOCKS: dict[Path, LockType] = {}
_TOKEN_FILE_LOCKS_GUARD = threading.Lock()


# [해설] 토큰 파일 경로별 스레드 락을 지연 생성해 반환. `_set_*_sync`와 `discard_client_info_if_loopback_unusable`가 사용.
def _token_file_lock(path: Path) -> LockType:
    """Return the process-wide mutation lock for `path`.

    Only read-modify-write *mutations* (the `_set_*_sync` methods and the
    loopback self-heal) take this lock, to serialize their overlapping updates
    to the shared envelope. Reads deliberately do **not**: `_write` publishes
    via an atomic `tmp.replace(path)`, so a reader always sees a whole old or
    whole new file and has no modify step to lose. The reads are already
    offloaded via `asyncio.to_thread`, so guarding them would only add needless
    worker-thread contention without preventing any lost update.

    The cache is never pruned, but it is not a leak: it holds one lock per
    distinct token-file path — effectively the set of configured MCP servers —
    so it is bounded by config, not by request volume. Pruning would also race a
    thread mid-`with` on an entry, so entries are kept for the process lifetime.
    """
    with _TOKEN_FILE_LOCKS_GUARD:
        lock = _TOKEN_FILE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _TOKEN_FILE_LOCKS[path] = lock
        return lock


# [해설] 토큰 저장 디렉터리 = `model_config.DEFAULT_STATE_DIR / "mcp-tokens"`(기본 `~/.deepagents/.state/mcp-tokens`). 테스트 patch를 위해 지연 import.
def token_store_dir() -> Path:
    """Return the selected profile's MCP OAuth token-store directory.

    The deferred import lets tests redirect token storage into a temp
    directory by patching `deepagents_code.model_config.DEFAULT_STATE_DIR`.
    """
    from deepagents_code.model_config import DEFAULT_STATE_DIR

    return DEFAULT_STATE_DIR / "mcp-tokens"


# [해설] 파일명 stem = `<server>-<sha256(url)[:16]>`. 같은 서버 이름이라도 URL이 바뀌면 다른 토큰 파일을 쓰게 되어 토큰이 엉뚱한 서버로 가지 않는다.
def _token_file_stem(server_name: str, server_url: str | None) -> str:
    """Return a path-safe storage stem for this server identity.

    Safety of the stem depends on `server_name` already having passed
    `_SERVER_NAME_RE` in `_validate_server_config` — the URL is hashed
    to a hex digest, so only the server name can carry path separators.
    """
    if server_url is None:
        return server_name
    digest = hashlib.sha256(server_url.encode("utf-8")).hexdigest()[:16]
    return f"{server_name}-{digest}"


# [해설] 호출자가 취소돼도 백그라운드 task(토큰 쓰기, 락 획득/해제)를 끝까지 기다렸다가 취소를 "나중에" 돌려준다.
# [해설][설계] refresh token 회전 중 취소로 쓰기가 반쯤 끝나거나 락이 고아가 되는 것을 막기 위함. anyio `CancelScope(shield=True)`로 반복 취소도 흡수.
async def _join_task_deferring_cancellation[T](
    task: asyncio.Task[T],
) -> asyncio.CancelledError | None:
    """Join `task` without letting caller cancellation cancel the task.

    The caller must inspect `task.result()` before re-raising the returned
    cancellation so the task's failure can take precedence.

    Returns:
        The first cancellation deferred while waiting, if any.
    """
    cancellation: asyncio.CancelledError | None = None
    try:
        # Unlike awaiting the task directly, cancelling `asyncio.wait` does not
        # cancel the member task. It also sidesteps the spurious shield-failure
        # log that awaiting a shielded task emits on newer CPython.
        await asyncio.wait((task,))
    except asyncio.CancelledError as exc:
        cancellation = exc
        # Block repeated cancellation from the same AnyIO scope. Direct
        # `Task.cancel()` calls are separate edges, so keep joining after each.
        with CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.wait((task,))
                except asyncio.CancelledError:
                    continue
    return cancellation


# [해설] MCP SDK `TokenStorage` 프로토콜의 파일 구현. JSON envelope 한 파일에 version, tokens, client_info(DCR 등록), oauth_metadata, expires_at을 함께 저장.
# [해설] 생성자: `mcp_tools.py`의 연결 구성, `login`, `mcp_providers/slack.py` 등. 서버/클라이언트 프로세스 양쪽에서 같은 파일을 공유한다.
class FileTokenStorage(TokenStorage):
    """File-backed `TokenStorage` under the selected profile's state directory."""

    def __init__(self, server_name: str, *, server_url: str | None = None) -> None:
        """Bind this storage to a configured MCP server identity.

        Raises:
            ValueError: If `server_name` contains characters that would let
                it escape the MCP token-store directory when used as the
                token-file basename.
        """
        # [해설][주의] 보안 검사: 이름이 안전하지 않으면 토큰 경로가 저장 디렉터리 밖으로 나갈 수 있으므로 생성 단계에서 거부.
        if not _SAFE_SERVER_NAME_RE.fullmatch(server_name):
            tokens_dir = PATHS.display(token_store_dir())
            msg = (
                f"Invalid MCP server name {server_name!r}: token storage "
                "names must match [A-Za-z0-9_-]+ to keep the on-disk path "
                f"inside {tokens_dir}."
            )
            raise ValueError(msg)
        self._server_name = server_name
        self._server_url = server_url

    @property
    def path(self) -> Path:
        """On-disk token file path for this server."""
        stem = _token_file_stem(self._server_name, self._server_url)
        return token_store_dir() / f"{stem}.json"

    # [해설] 교차 프로세스 refresh 직렬화용 sidecar `<token>.json.lock`. 토큰 파일 자체에는 락을 걸지 않는다.
    @property
    def refresh_lock_path(self) -> Path:
        """Sibling lock file that serializes token refreshes across processes.

        A dedicated `.lock` file (never the token file itself) lets `filelock`
        coordinate refreshes between dcode processes and provider instances
        without ever holding an exclusive lock on the credential file. It holds
        no token material.
        """
        path = self.path
        return path.with_name(f"{path.name}.lock")

    # [해설] SDK가 호출하는 비동기 getter/setter들은 모두 `asyncio.to_thread`로 파일 IO를 이벤트 루프 밖에서 수행한다.
    async def get_tokens(self) -> OAuthToken | None:
        """Return the stored `OAuthToken`, or `None` if none is persisted."""
        return await asyncio.to_thread(self._get_tokens_sync)

    def _get_tokens_sync(self) -> OAuthToken | None:
        data = self._read()
        return self._tokens_from_data(data)

    @staticmethod
    def _tokens_from_data(data: dict[str, Any] | None) -> OAuthToken | None:
        if data is None:
            return None
        raw = data.get("tokens")
        if raw is None:
            return None
        return OAuthToken.model_validate(raw)

    # [해설] 토큰과 만료를 "같은 파일 스냅샷"에서 읽는다. `_apply_stored_expiry`가 사용 — 다른 프로세스의 회전과 섞이지 않게.
    async def get_tokens_with_expiry(
        self,
    ) -> tuple[OAuthToken | None, float | None]:
        """Return tokens and their absolute expiry from one file snapshot.

        Reading both fields together prevents a concurrent token rotation from
        pairing one token generation with another generation's expiry.
        """
        return await asyncio.to_thread(self._get_tokens_with_expiry_sync)

    def _get_tokens_with_expiry_sync(
        self,
    ) -> tuple[OAuthToken | None, float | None]:
        data = self._read()
        return self._tokens_from_data(data), self._expires_at_from_data(data)

    # [해설] 토큰 저장 + 절대 만료시각(expires_at) sidecar 기록. refresh 성공 시 SDK가 호출한다.
    # [해설][흐름] 1) expires_in으로 절대 시각 계산 → 2) 스레드에서 쓰기 task 시작 → 3) 취소를 미루며 완료 대기 → 4) 쓰기 오류 우선, 그다음 지연된 취소 전파.
    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist `tokens` to disk, preserving any stored client info.

        Also records the absolute Unix-epoch expiry as a sidecar so a
        cold-started provider can detect a stale access token and trigger
        the SDK's `refresh_token` grant instead of a full browser re-auth.
        Cleared when `expires_in` is absent so the sidecar can't go stale.

        Once persistence starts, cancellation is delayed until the write is
        terminal. If persistence fails while cancellation is pending, the
        persistence error takes precedence so it is not silently discarded.
        """
        expires_at = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        write_task = asyncio.create_task(
            asyncio.to_thread(self._set_tokens_sync, tokens, expires_at)
        )
        cancellation = await _join_task_deferring_cancellation(write_task)

        # A refresh may rotate the refresh token. Observe persistence before
        # propagating cancellation so the refresh lock cannot be released while
        # a new token is still queued, and so a write failure is not discarded.
        try:
            write_task.result()
        except Exception:
            # The write failure takes precedence and is re-raised, but log that
            # it supersedes a deferred cancellation so the dropped edge is not
            # silent (parity with `_refresh_lock_guard`).
            if cancellation is not None:
                logger.warning(
                    "MCP token write for %s failed; a deferred cancellation is "
                    "superseded by the write error.",
                    self._server_name,
                )
            raise
        if cancellation is not None:
            raise cancellation

    # [해설] 스레드 락 안에서 read-modify-write. 다른 필드(client_info 등)는 보존하고 tokens/expires_at만 교체.
    def _set_tokens_sync(self, tokens: OAuthToken, expires_at: float | None) -> None:
        with _token_file_lock(self.path):
            data = self._read() or {}
            data["version"] = _STORAGE_VERSION
            data["tokens"] = json.loads(tokens.model_dump_json(exclude_none=True))
            if expires_at is not None:
                data["expires_at"] = expires_at
            else:
                data.pop("expires_at", None)
            self._write(data)

    # [해설] DCR(Dynamic Client Registration)로 받은 client 등록 정보 getter/setter.
    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Return the stored client registration, or `None` if none is persisted."""
        return await asyncio.to_thread(self._get_client_info_sync)

    def _get_client_info_sync(self) -> OAuthClientInformationFull | None:
        data = self._read()
        if data is None:
            return None
        raw = data.get("client_info")
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate(raw)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist `client_info` to disk, preserving any stored tokens."""
        await asyncio.to_thread(self._set_client_info_sync, client_info)

    def _set_client_info_sync(self, client_info: OAuthClientInformationFull) -> None:
        with _token_file_lock(self.path):
            data = self._read() or {}
            data["version"] = _STORAGE_VERSION
            data["client_info"] = json.loads(
                client_info.model_dump_json(exclude_none=True)
            )
            self._write(data)

    # [해설] 발견된 인가 서버 메타데이터(토큰 엔드포인트 등) 캐시. refresh 때 `/token`을 추측하지 않고 광고된 엔드포인트를 쓰기 위함.
    async def get_oauth_metadata(self) -> OAuthMetadata | None:
        """Return stored public OAuth authorization metadata, if available."""
        return await asyncio.to_thread(self._get_oauth_metadata_sync)

    def _get_oauth_metadata_sync(self) -> OAuthMetadata | None:
        data = self._read()
        if data is None:
            return None
        raw = data.get("oauth_metadata")
        if raw is None:
            return None
        return OAuthMetadata.model_validate(raw)

    async def set_oauth_metadata(self, metadata: OAuthMetadata) -> None:
        """Persist public OAuth authorization metadata beside the token state."""
        await asyncio.to_thread(self._set_oauth_metadata_sync, metadata)

    def _set_oauth_metadata_sync(self, metadata: OAuthMetadata) -> None:
        with _token_file_lock(self.path):
            data = self._read() or {}
            data["version"] = _STORAGE_VERSION
            data["oauth_metadata"] = json.loads(
                metadata.model_dump_json(exclude_none=True)
            )
            self._write(data)

    # [해설] 토큰+client_info를 한 번에 원자적으로 기록(한쪽만 저장된 고아 상태 방지). 호출자는 이 파일 밖(추정: `mcp_providers`의 사전 등록 흐름).
    async def set_tokens_and_client_info(
        self,
        tokens: OAuthToken,
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Persist tokens and client info in a single atomic write.

        Prevents the state where one call succeeds and the other fails,
        leaving an orphan on disk.
        """
        expires_at = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        await asyncio.to_thread(
            self._set_tokens_and_client_info_sync,
            tokens,
            client_info,
            expires_at,
        )

    def _set_tokens_and_client_info_sync(
        self,
        tokens: OAuthToken,
        client_info: OAuthClientInformationFull,
        expires_at: float | None,
    ) -> None:
        with _token_file_lock(self.path):
            data = self._read() or {}
            data["version"] = _STORAGE_VERSION
            data["tokens"] = json.loads(tokens.model_dump_json(exclude_none=True))
            data["client_info"] = json.loads(
                client_info.model_dump_json(exclude_none=True)
            )
            if expires_at is not None:
                data["expires_at"] = expires_at
            else:
                data.pop("expires_at", None)
            self._write(data)

    # [해설] 절대 만료 시각만 읽는 getter. None은 "모름" — 정책 결정은 호출자(`_apply_stored_expiry`)가 한다.
    async def get_expires_at(self) -> float | None:
        """Return the stored absolute token expiry (Unix epoch), or `None`.

        Returns `None` for token files written before this field existed,
        for tokens whose provider omitted `expires_in`, or when the sidecar
        value fails to coerce to `float`. Callers should treat `None` as
        "expiry unknown" and decide policy (skip, assume-expired, etc.).
        """
        return await asyncio.to_thread(self._get_expires_at_sync)

    def _get_expires_at_sync(self) -> float | None:
        data = self._read()
        return self._expires_at_from_data(data)

    # [해설] expires_at을 float으로 강제 변환. 실패 시 값이 아닌 타입만 로그(토큰 원문이 섞였을 가능성 대비).
    def _expires_at_from_data(self, data: dict[str, Any] | None) -> float | None:
        if data is None:
            return None
        raw = data.get("expires_at")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            # Log the value's type, never the value itself — the sidecar lives
            # next to the bearer token in the same JSON envelope, so a
            # misplaced token string would land here.
            logger.warning(
                "MCP token sidecar 'expires_at' for %s is not numeric (%s); "
                "treating as unknown. The next request will trigger a refresh "
                "or browser re-auth.",
                self._server_name,
                type(raw).__name__,
            )
            return None

    # [해설] DCR로 등록된 redirect_uri의 포트를 재사용하기 위한 조회. `build_oauth_provider`가 loopback 포트 결정에 사용.
    # [해설][설계] 매 실행마다 새 랜덤 포트를 쓰면 등록된 redirect_uri와 불일치해 인가 서버가 거부하므로, 저장된 포트를 재사용한다.
    def stored_loopback_port(self) -> int | None:
        """Return the stored loopback redirect URI port, if one is reusable.

        DCR registers `client_id` against a specific `redirect_uri`. If the
        callback server binds a fresh random port on a later launch, the
        authorize request will carry a `redirect_uri` that no longer matches
        the one registered with the persisted `client_id`, and the
        authorization server will reject it ("invalid or missing redirect_uri").
        Reusing the persisted port keeps the registration valid across runs.

        Returns:
            The integer port parsed from a stored
                `http://localhost:<port>/callback` redirect URI, or `None` if
                no usable port is on disk.
        """
        try:
            data = self._read()
        except RuntimeError as exc:
            logger.warning(
                "MCP token file for %s is unreadable during loopback port "
                "lookup; falling back to a fresh random port. Delete the file "
                "and log in again if OAuth authorization fails: %s",
                self.path,
                exc,
            )
            return None
        if data is None:
            return None
        client_info = data.get("client_info") or {}
        redirect_uris = client_info.get("redirect_uris") or []
        if not redirect_uris:
            return None
        uri = str(redirect_uris[0])
        port = self._loopback_callback_port(uri)
        if port is None:
            logger.warning(
                "Stored MCP OAuth redirect URI for %s is not a reusable "
                "loopback callback URI; falling back to a fresh random port. "
                "OAuth authorization may fail if the server requires the "
                "persisted client registration redirect URI: %s",
                self.path,
                uri,
            )
            return None
        return port

    # [해설] `http://localhost:<port>/callback` 형태일 때만 포트를 돌려준다(명시 포트 필수).
    @staticmethod
    def _loopback_callback_port(uri: str) -> int | None:
        """Return `uri`'s port if it is a reusable loopback callback URI.

        A reusable URI is `http://localhost:<port>/callback` with an explicit
        port. Anything else (a different scheme/host/path, or a portless
        `http://localhost/callback`) returns `None` because it cannot be paired
        with the loopback callback server a CLI login binds.
        """
        parsed = urlparse(uri)
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "http"
            or parsed.hostname != _LOOPBACK_URI_HOST
            or parsed.path != _LOOPBACK_CALLBACK_PATH
            or port is None
        ):
            return None
        return port

    # [해설] 자가 치유: 저장된 client 등록이 loopback으로 쓸 수 없는 redirect_uri를 가졌고 토큰이 전혀 없을 때만 client_info를 지워 새 DCR을 유도.
    # [해설] 토큰이 하나라도 있으면 지우지 않는다(불필요한 재인증 방지, 보수적).
    def discard_client_info_if_loopback_unusable(self) -> bool:
        """Drop a persisted client registration that can't serve loopback login.

        `stored_loopback_port` explains why the authorize request's
        `redirect_uri` must match the one registered against the persisted
        `client_id`. When that registered URI is not a reusable loopback
        callback (e.g. a portless `http://localhost/callback` left by an earlier
        non-loopback login), no port can be reused, so a CLI login binds a fresh
        random port and the server rejects the mismatched `redirect_uri`
        ("invalid or missing redirect_uri"). Removing the stale `client_info`
        makes the SDK perform a fresh DCR with the loopback redirect URI it will
        actually use.

        Only discards when no token is persisted at all, so a session with a
        usable (or refreshable) token is never downgraded to a full re-auth.
        The presence check is deliberately conservative: it errs toward keeping
        the registration, never toward deleting one that might still be needed.

        Returns:
            `True` if a stale client registration was removed. `False` covers
                both "nothing to discard" and "could not discard" (an
                unreadable or unwritable token file, logged where it happens).
        """
        with _token_file_lock(self.path):
            return self._discard_client_info_if_loopback_unusable_sync()

    def _discard_client_info_if_loopback_unusable_sync(self) -> bool:
        try:
            data = self._read()
        except RuntimeError as exc:
            # Mirror `stored_loopback_port`: a corrupt or unsupported-version
            # token file carries actionable "delete the file" guidance in the
            # `RuntimeError` message, so surface it rather than dropping it.
            logger.warning(
                "MCP token file for %s is unreadable while checking for a "
                "stale client registration; skipping self-heal. Delete the "
                "file and log in again if OAuth authorization fails: %s",
                self.path,
                exc,
            )
            return False
        if data is None or "client_info" not in data:
            return False
        # A persisted access/refresh token can still authenticate (or refresh)
        # without re-running the authorization-code grant, so leave the
        # registration intact rather than forcing an avoidable re-auth.
        if data.get("tokens") is not None:
            return False
        redirect_uris = (data.get("client_info") or {}).get("redirect_uris") or []
        if (
            redirect_uris
            and self._loopback_callback_port(str(redirect_uris[0])) is not None
        ):
            return False
        del data["client_info"]
        try:
            self._write(data)
        except OSError as exc:
            # Surface the failure but don't crash login: the stale registration
            # simply remains, and the login attempt fails the same way it would
            # have without this self-heal.
            logger.warning(
                "Could not remove stale MCP client registration in %s: %s",
                self.path,
                exc,
            )
            return False
        return True

    # [해설] 토큰 파일 읽기. 파일 없음 → None, 손상/비객체/버전 불일치 → 삭제 후 `/mcp login` 하라는 안내가 담긴 RuntimeError.
    # [해설][주의] 오류 메시지에 파일 "내용"은 넣지 않는다(버전 필드도 타입명만) — 토큰 유출 방지.
    def _read(self) -> dict | None:
        path = self.path
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it needs
        # its own entry — otherwise an undecodable file escapes without the
        # remedy text that every other corruption mode gets.
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = (
                f"Failed to read MCP token file {path}: {exc}. "
                f"Delete the file and run `/mcp login {self._server_name}` "
                f"in the TUI (or `dcode mcp login {self._server_name}`)."
            )
            raise RuntimeError(msg) from exc
        # `json.loads` yields a `dict` only for object literals; `null`, a list,
        # or a bare scalar would make the `.get` below raise `AttributeError`,
        # which callers do not catch. Fail as a normal corrupt-file error.
        if not isinstance(data, dict):
            msg = (
                f"MCP token file {path} is not a JSON object (found "
                f"{type(data).__name__}). Delete it and run "
                f"`/mcp login {self._server_name}` in the TUI (or "
                f"`dcode mcp login {self._server_name}`)."
            )
            # Not `TypeError` (TRY004): this is a corrupt-file report, not a
            # caller type error, and callers catch the same `RuntimeError` the
            # other corruption modes raise. `TypeError` would escape them.
            raise RuntimeError(msg)  # noqa: TRY004
        if data.get("version") != _STORAGE_VERSION:
            # Render only the value's type, never the value itself: callers
            # print this message verbatim (e.g. `mcp login` list on stderr),
            # and the version field is attacker-controlled file content that
            # could carry credential material planted by a malformed write.
            msg = (
                f"MCP token file {path} has unsupported version "
                f"({type(data.get('version')).__name__}; expected "
                f"{_STORAGE_VERSION!r}). Delete it and run "
                f"`/mcp login {self._server_name}` in the "
                f"TUI (or `dcode mcp login {self._server_name}`)."
            )
            raise RuntimeError(msg)
        return data

    # [해설] 원자적·권한 제한 쓰기.
    # [해설][흐름] 1) 디렉터리 생성 후 0700 → 2) `.tmp`를 O_EXCL + 0600으로 생성해 기록(기본 umask로 노출되는 순간이 없음)
    # [해설][흐름] 3) `tmp.replace(path)` 원자적 교체(읽는 쪽은 항상 완전한 옛/새 파일만 봄) → 4) 0600 재확인(Windows 등 mode 무시 FS 대비).
    # [해설] chmod 실패는 치명적이지 않고 경고만 남긴다.
    def _write(self, data: dict) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "chmod"):
            try:
                path.parent.chmod(stat.S_IRWXU)
            except OSError as exc:
                # A failing chmod on the parent dir leaves the tokens
                # directory at the default umask. Warn so operators on
                # shared hosts notice.
                logger.warning(
                    "Could not lock down MCP tokens dir %s (mode 0700): %s. "
                    "Tokens may be readable by other local users.",
                    path.parent,
                    exc,
                )
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
        # O_EXCL + mode 0600 means the token file is never visible at the
        # default umask between open() and chmod(). On Windows, os.open()
        # ignores the mode bits, so the explicit chmod below is the
        # cross-platform guarantee.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(str(tmp), flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        try:
            tmp.replace(path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        if hasattr(os, "chmod"):
            # Already 0600 from os.open on POSIX; a second chmod covers
            # filesystems that ignore the create-mode argument.
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                logger.warning(
                    "Could not set mode 0600 on MCP token file %s: %s. "
                    "Stored refresh/access tokens may be world-readable.",
                    path,
                    exc,
                )


# [해설] 명시적 재로그인 전용 저장소: 토큰 getter만 None을 돌려 SDK가 반드시 전체 인가 흐름을 돌게 한다. `login`에서만 사용.
# [해설] 실패/중단 시 기존 토큰 파일은 그대로 남고, 성공 시 `set_tokens`가 덮어쓴다. client_info·메타데이터는 디스크 값을 재사용.
class _FreshLoginTokenStorage(FileTokenStorage):
    """`FileTokenStorage` that hides stored tokens to force a full re-auth.

    An explicit login must re-run the authorization flow even when the
    persisted access token is still valid. Without this, the SDK loads that
    token, the handshake succeeds without ever prompting, and the user is
    told they logged in again when nothing happened.

    Tokens are hidden rather than deleted so an aborted or failed flow leaves
    the working credential on disk untouched. Only the token getters are
    overridden: client registration, OAuth metadata, and the loopback port
    still come from disk, so the handshake reuses the existing registration
    instead of re-running DCR against a new redirect URI. A successful flow
    writes through `set_tokens`, replacing the hidden token.
    """

    @override
    async def get_tokens(self) -> OAuthToken | None:
        """Return `None` so the provider runs a full authorization flow."""
        return None

    @override
    async def get_tokens_with_expiry(
        self,
    ) -> tuple[OAuthToken | None, float | None]:
        """Return no token and no expiry, matching `get_tokens`."""
        return None, None

    @override
    async def get_expires_at(self) -> float | None:
        """Return `None` so no hidden token looks refreshable."""
        return None


# [해설] OAuth 콜백 핸들러 타입과 loopback 상수. 바인드는 127.0.0.1, URI 호스트는 localhost, 경로 `/callback`, 브라우저 콜백 대기 300초.
# [해설] `_LOOPBACK_URI_HOST`/`_LOOPBACK_CALLBACK_PATH`는 위쪽 `FileTokenStorage._loopback_callback_port`에서도 참조(런타임 참조라 선언 순서 무관).
RedirectHandler = Callable[[str], Awaitable[None]]
CallbackHandler = Callable[[], Awaitable[tuple[str, str | None]]]
_LOOPBACK_BIND_HOST = "127.0.0.1"
_LOOPBACK_URI_HOST = "localhost"
_LOOPBACK_CALLBACK_PATH = "/callback"
_LOOPBACK_CALLBACK_TIMEOUT = 300.0


# [해설] 브라우저가 제한 시간 내 콜백을 주지 않음 → `_make_loopback_handlers`의 callback이 paste-back으로 폴백.
class _LoopbackCallbackTimeoutError(RuntimeError):
    """Raised when the browser never reaches the local callback server."""


# [해설] 콜백 서버를 시작 못 함(브라우저 없음·포트 bind 실패) → 역시 paste-back 폴백.
class _LoopbackCallbackUnavailableError(RuntimeError):
    """Raised when the local callback server cannot be started."""


# [해설] 동적 포트 범위(49152–65535)에서 무작위 포트 선택. 소켓은 열지 않는다(redirect_uri를 먼저 확정해야 하므로).
def _choose_loopback_port() -> int:
    """Return a high local TCP port candidate without opening a socket.

    The OAuth redirect URI must be known before the provider starts the
    handshake, but the actual callback server should not keep a socket open
    unless a browser redirect is needed.

    Returns:
        A port number from the dynamic/private port range.
    """
    return 49152 + secrets.randbelow(65535 - 49152 + 1)


# [해설] 1회용 로컬 HTTP 콜백 서버. 브라우저가 `?code=&state=`로 리다이렉트되면 future에 결과를 넣는다.
# [해설][설계] 포트 선택(생성자)과 소켓 bind(`start`)를 분리 — redirect_uri는 DCR·authorize 전에 필요하지만 소켓은 실제 브라우저 리다이렉트 때만 연다.
# [해설] HTTP 처리는 백그라운드 데몬 스레드, 결과 전달은 `concurrent.futures.Future` → `asyncio.wrap_future`로 이벤트 루프에 연결.
class _LoopbackOAuthCallbackServer:
    """Single-use loopback HTTP server for CLI OAuth callbacks.

    Port selection and socket binding are intentionally separated: the
    redirect URI is fixed at construction time so it can be registered with
    the OAuth provider, while the socket is not opened until `start()` is
    called from the redirect handler.
    """

    def __init__(self, *, port: int) -> None:
        """Prepare a callback server for a previously selected loopback port.

        Args:
            port: TCP port to bind when `start()` is called.
        """
        self._port = port
        self.redirect_uri = (
            f"http://{_LOOPBACK_URI_HOST}:{port}{_LOOPBACK_CALLBACK_PATH}"
        )
        self._future: concurrent.futures.Future[tuple[str, str | None]] = (
            concurrent.futures.Future()
        )
        self._server: object | None = None
        self._started = False
        self._closed = False
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Loopback TCP port this server will bind on `start()`."""
        return self._port

    # [해설] 127.0.0.1에 `ThreadingHTTPServer`를 bind하고 데몬 스레드에서 serve. 요청 로그는 `log_message` 오버라이드로 억제.
    def start(self) -> None:
        """Bind and start serving callback requests in a background thread."""
        if self._started:
            return
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parent._handle_get(self)

            def log_message(  # noqa: PLR6301  # stdlib override
                self,
                format: str,  # noqa: A002
                *args: object,
            ) -> None:
                del format, args

        self._server = ThreadingHTTPServer((_LOOPBACK_BIND_HOST, self._port), Handler)
        self._started = True
        self._thread = threading.Thread(
            target=self._serve_forever,
            name="deepagents-mcp-oauth-callback",
            daemon=True,
        )
        self._thread.start()

    def _serve_forever(self) -> None:
        from http.server import ThreadingHTTPServer

        if isinstance(self._server, ThreadingHTTPServer):
            self._server.serve_forever()

    # [해설] 콜백을 최대 `_LOOPBACK_CALLBACK_TIMEOUT`(300초)까지 기다린다.
    async def wait(self) -> tuple[str, str | None]:
        """Wait for the authorization callback and return `(code, state)`.

        Returns:
            The OAuth authorization code and optional state.

        Raises:
            _LoopbackCallbackTimeoutError: If no callback arrives before the timeout.
            _LoopbackCallbackUnavailableError: If the server could not be started.
            RuntimeError: If the provider returned an OAuth error or the callback
                URL lacked a `code` parameter.
        """  # noqa: DOC502 - _LoopbackCallbackUnavailableError/RuntimeError set on future
        import asyncio

        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(self._future),
                timeout=_LOOPBACK_CALLBACK_TIMEOUT,
            )
        except TimeoutError as exc:
            msg = "Browser callback was not received before the timeout."
            raise _LoopbackCallbackTimeoutError(msg) from exc

    # [해설] future에 예외를 넣어 `wait()`를 즉시 깨운다(브라우저 없음 등 사전 실패 알림).
    def fail(self, exc: Exception) -> None:
        """Poison the future so `wait()` raises `exc` immediately.

        Args:
            exc: Exception to surface to the awaiting coroutine.
        """
        if not self._future.done():
            self._future.set_exception(exc)

    def close(self) -> None:
        """Stop the local callback server and release its socket."""
        if self._closed:
            return
        self._closed = True
        from http.server import ThreadingHTTPServer

        if isinstance(self._server, ThreadingHTTPServer):
            if self._started:
                self._server.shutdown()
            self._server.server_close()

    # [해설] 콜백 GET 처리.
    # [해설][흐름] 1) 이미 완료면 중복 요청(재시도·prefetch·favicon)에 결과 페이지만 응답 → 2) `/callback` 외 경로 404
    # [해설][흐름] 3) `error=` 파라미터면 거부 예외 → 4) `code` 누락이면 예외 → 5) (code, state)를 future에 설정하고 성공 페이지.
    # [해설][주의] state 검증은 여기서 하지 않고 반환만 한다 — 검증은 MCP SDK `OAuthClientProvider` 쪽 몫 (추정).
    def _handle_get(self, request: object) -> None:
        from http.server import BaseHTTPRequestHandler

        handler = request
        if not isinstance(handler, BaseHTTPRequestHandler):
            return

        if self._future.done():
            # Duplicate browser request (retry, prefetch, favicon) after the
            # flow already completed. Respond and return without touching the
            # future — avoids InvalidStateError from a concurrent set_result.
            # Branch on the future's terminal state: a previous error must
            # not be papered over with a success page.
            if self._future.exception() is None:
                self._send_html(
                    handler,
                    200,
                    _oauth_success_html(
                        "MCP authorization complete. "
                        "You can close this tab and return to your terminal.",
                    ),
                )
            else:
                self._send_html(
                    handler,
                    400,
                    _oauth_error_html(
                        "Authorization did not complete. "
                        "Return to your terminal for details.",
                    ),
                )
            return

        parsed = urlparse(handler.path)
        if parsed.path != _LOOPBACK_CALLBACK_PATH:
            self._send_html(
                handler,
                404,
                _oauth_error_html("Callback route not found."),
            )
            return

        params = parse_qs(parsed.query)
        if "error" in params:
            err_code = params["error"][0]
            err_desc = (params.get("error_description") or [""])[0]
            detail = f": {err_desc}" if err_desc else ""
            msg = f"Authorization denied by provider: {err_code}{detail}"
            self._future.set_exception(RuntimeError(msg))
            self._send_html(handler, 400, _oauth_error_html(msg))
            return

        if "code" not in params or not params["code"]:
            msg = "Callback URL is missing the 'code' parameter."
            self._future.set_exception(RuntimeError(msg))
            self._send_html(handler, 400, _oauth_error_html(msg))
            return

        self._future.set_result((params["code"][0], (params.get("state") or [None])[0]))
        self._send_html(
            handler,
            200,
            _oauth_success_html(
                "MCP authorization complete. "
                "You can close this tab and return to your terminal.",
            ),
        )

    @staticmethod
    def _send_html(handler: object, status: int, body: str) -> None:
        from http.server import BaseHTTPRequestHandler

        if not isinstance(handler, BaseHTTPRequestHandler):
            return
        payload = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


# [해설] 브라우저 탭에 보여줄 성공/실패 HTML 페이지 생성 헬퍼들. 메시지는 `html.escape`로 이스케이프(XSS 방지).
def _oauth_success_html(message: str) -> str:
    return _oauth_result_html(
        title="Authorization complete",
        heading="You're signed in",
        message=message,
        status="success",
    )


def _oauth_error_html(message: str) -> str:
    return _oauth_result_html(
        title="Authorization failed",
        heading="Authorization failed",
        message=message,
        status="error",
    )


def _oauth_result_html(
    *,
    title: str,
    heading: str,
    message: str,
    status: Literal["success", "error"],
) -> str:
    accent = "#137333" if status == "success" else "#b3261e"
    background = "#eef7f0" if status == "success" else "#fceeee"
    mark = "✓" if status == "success" else "!"
    escaped_title = html.escape(title)
    escaped_heading = html.escape(heading)
    escaped = html.escape(message)
    # `window.close()` is only honored for tabs the script itself opened
    # (browser policy). The loopback flow launches the browser via
    # `webbrowser.open`, so the callback tab was usually opened by the OS and
    # the browser refuses to close it. Attempt the close for the rare
    # script-opened case; the static message already reads correctly whether
    # or not the tab closes, so nothing is rewritten and the text never shifts.
    auto_close = (
        "<script>setTimeout(function(){window.close();},1000);</script>"
        if status == "success"
        else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escaped_title}</title>"
        "<style>"
        "body{margin:0;min-height:100vh;display:grid;place-items:center;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "background:#f8faf9;color:#1f2328}"
        ".panel{width:min(480px,calc(100vw - 40px));box-sizing:border-box;"
        "padding:32px;border:1px solid #d8dee4;border-radius:8px;"
        "background:#fff;box-shadow:0 18px 45px rgba(31,35,40,.08)}"
        ".mark{width:44px;height:44px;border-radius:50%;display:grid;"
        "place-items:center;margin-bottom:20px;font-weight:700}"
        "h1{font-size:24px;line-height:1.2;margin:0 0 10px}"
        "p{font-size:15px;line-height:1.5;margin:0;color:#57606a}"
        "</style></head><body>"
        '<main class="panel">'
        f'<div class="mark" style="background:{background};color:{accent}">{mark}</div>'
        f"<h1>{escaped_heading}</h1><p>{escaped}</p>"
        "</main>"
        f"{auto_close}"
        "</body></html>"
    )


# [해설] 서버 프로세스(비대화형)에서 토큰이 없거나 refresh 불가할 때 발생. 사용자에게 `/mcp login` / `dcode mcp login` 안내.
# [해설] `mcp_tools.py`가 `find_reauth_required`로 찾아내 서버 상태를 `unauthenticated`로 표시한다.
class MCPReauthRequiredError(RuntimeError):
    """Raised when an MCP server needs interactive re-authentication."""

    def __init__(self, server_name: str) -> None:
        """Build with `server_name` so the message tells the user what to fix."""
        self.server_name = server_name
        super().__init__(
            f"MCP server {server_name!r} needs re-authentication. "
            f"Run `/mcp login {server_name}` in the TUI, or "
            f"`dcode mcp login {server_name}` from the shell.",
        )


# [해설] 비대화형 모드용 핸들러: 인가 URL을 보여주거나 입력을 기다리는 대신 즉시 `MCPReauthRequiredError`를 던진다(서버가 input()에 걸려 멈추는 것 방지).
def _make_reauth_required_handlers(
    server_name: str,
) -> tuple[RedirectHandler, CallbackHandler]:
    """Return OAuth handlers that refuse to prompt and raise instead.

    Used in non-interactive server mode so that a missing or expired token
    surfaces as `MCPReauthRequiredError` rather than hanging on `input()`.
    """

    async def redirect(_auth_url: str) -> None:  # noqa: RUF029
        raise MCPReauthRequiredError(server_name)

    async def callback() -> tuple[str, str | None]:  # noqa: RUF029
        raise MCPReauthRequiredError(server_name)

    return redirect, callback


# [해설] paste-back 흐름: 인가 URL을 표시하고 사용자가 브라우저 주소창의 콜백 URL을 붙여넣게 한다. 브라우저 없는 SSH 환경이나 loopback 미지원 provider용.
def _make_paste_back_handlers(
    *,
    extra_auth_params: dict[str, str] | None = None,
    ui: OAuthInteraction | None = None,
) -> tuple[RedirectHandler, CallbackHandler]:
    """Create paste-back redirect and callback handlers for OAuth.

    Args:
        extra_auth_params: Extra query params to append to the auth URL.
        ui: Interaction surface for the auth URL display and the
            pasted-back callback URL prompt.

    Returns:
        A tuple of `(redirect_handler, callback_handler)`.
    """
    extras = dict(extra_auth_params or {})
    interaction = ui if ui is not None else _default_ui()

    async def redirect(auth_url: str) -> None:
        final_url = _append_query_params(auth_url, extras) if extras else auth_url
        await interaction.show_authorize_url(final_url, opened_in_browser=False)

    async def callback() -> tuple[str, str | None]:
        url = await interaction.request_callback_url()
        return _parse_callback_url(url)

    return redirect, callback


# [해설] 붙여넣은 콜백 URL에서 code/state 추출. error 파라미터가 있거나 code가 없으면 RuntimeError.
def _parse_callback_url(url: str) -> tuple[str, str | None]:
    """Parse a provider callback URL into `(code, state)`.

    Args:
        url: Raw callback URL pasted by the user.

    Returns:
        The `code` and optional `state` query parameters.

    Raises:
        RuntimeError: If the URL contains `error=` or lacks `code`.
    """
    params = parse_qs(urlparse(url).query)
    if "error" in params:
        err_code = params["error"][0]
        err_desc = (params.get("error_description") or [""])[0]
        detail = f": {err_desc}" if err_desc else ""
        msg = f"Authorization denied by provider: {err_code}{detail}"
        raise RuntimeError(msg)
    if "code" not in params or not params["code"]:
        msg = "Callback URL is missing the 'code' parameter."
        raise RuntimeError(msg)
    return params["code"][0], (params.get("state") or [None])[0]


# [해설] UI 미지정 시 기본 CLI stdio 구현(`mcp_oauth_ui.CliOAuthInteraction`).
def _default_ui() -> OAuthInteraction:
    """Return the default `OAuthInteraction` implementation (CLI stdio)."""
    from deepagents_code.mcp_oauth_ui import CliOAuthInteraction

    return CliOAuthInteraction()


# [해설] 브라우저 loopback 흐름 핸들러 쌍을 만든다. 실패 시 paste-back으로 자연스럽게 폴백하도록 paste 핸들러를 미리 만들어 둔다.
def _make_loopback_handlers(
    *,
    callback_server: _LoopbackOAuthCallbackServer,
    extra_auth_params: dict[str, str] | None = None,
    ui: OAuthInteraction | None = None,
) -> tuple[RedirectHandler, CallbackHandler]:
    """Create browser loopback redirect and callback handlers for OAuth.

    Args:
        callback_server: Prepared local callback server for this login attempt.
            The socket is bound when the returned redirect handler is first called.
        extra_auth_params: Extra query params to append to the auth URL.
        ui: Interaction surface for the browser-opened or fallback prompts.

    Returns:
        A tuple of `(redirect_handler, callback_handler)`.
    """
    extras = dict(extra_auth_params or {})
    interaction = ui if ui is not None else _default_ui()
    last_authorize_url: str | None = None
    _paste_redirect, paste_callback = _make_paste_back_handlers(
        extra_auth_params=extra_auth_params,
        ui=interaction,
    )

    # [해설] redirect: SDK가 인가 URL을 만들면 호출된다.
    # [해설][흐름] 1) 추가 쿼리 파라미터 병합 → 2) `webbrowser.get()`으로 실제 브라우저 존재 확인(헤드리스에서 300초 낭비 방지)
    # [해설][흐름] 3) 열기 실패 → 서버 future를 실패시키고 URL만 표시 → 4) 콜백 서버 start 실패 → 안내 후 URL 표시 → 5) 성공 → "브라우저에서 열림" 표시.
    async def redirect(auth_url: str) -> None:
        import asyncio
        import webbrowser

        nonlocal last_authorize_url
        final_url = _append_query_params(auth_url, extras) if extras else auth_url
        last_authorize_url = final_url

        # Resolve a browser explicitly before opening so headless / SSH
        # environments fall through to paste-back without burning the
        # 300s loopback timeout. `webbrowser.open` can return `True` in
        # those environments even when nothing launches.
        try:
            await asyncio.to_thread(webbrowser.get)
            has_browser = True
        except webbrowser.Error:
            has_browser = False

        if has_browser:
            opened = await asyncio.to_thread(webbrowser.open, final_url)
        else:
            opened = False
        if not opened:
            callback_server.fail(
                _LoopbackCallbackUnavailableError(
                    "No browser is available to complete the OAuth flow.",
                ),
            )
            await interaction.show_authorize_url(final_url, opened_in_browser=False)
            return
        try:
            callback_server.start()
        except OSError as exc:
            logger.warning(
                "Could not start loopback OAuth callback server on port %s: %s",
                callback_server.port,
                exc,
            )
            msg = "Local OAuth callback server could not be started."
            callback_server.fail(_LoopbackCallbackUnavailableError(msg))
            await interaction.show_notice(
                "Could not start the local OAuth callback server.",
            )
            await interaction.show_authorize_url(final_url, opened_in_browser=False)
            return
        await interaction.show_authorize_url(final_url, opened_in_browser=True)

    # [해설] callback: loopback 결과를 기다리고, timeout/unavailable이면 URL을 다시 보여주고 paste-back으로 폴백. 어느 경우든 서버 소켓은 finally에서 닫힌다.
    async def callback() -> tuple[str, str | None]:
        try:
            return await callback_server.wait()
        except (
            _LoopbackCallbackTimeoutError,
            _LoopbackCallbackUnavailableError,
        ) as exc:
            if last_authorize_url is not None:
                await interaction.show_authorize_url(
                    last_authorize_url,
                    opened_in_browser=False,
                )
            await interaction.show_notice(
                f"{exc}\nPaste the full callback URL instead.",
            )
            return await paste_callback()
        finally:
            callback_server.close()

    return redirect, callback


# [해설] 인가 URL의 쿼리에 provider별 추가 파라미터(예: Slack team ID)를 덮어써 넣는다.
def _append_query_params(url: str, params: dict[str, str]) -> str:
    """Return `url` with `params` replacing any same-named query keys."""
    from urllib.parse import urlencode, urlunparse

    parsed = urlparse(url)
    existing = dict(parse_qs(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        existing[key] = [value]
    return urlunparse(parsed._replace(query=urlencode(existing, doseq=True)))


# [해설] SDK 호환성 패치: client_secret_basic 인증일 때 토큰 요청 본문의 중복 client_id를 제거한다(일부 인가 서버가 중복을 거부).
# [해설] `context.prepare_token_auth`를 래핑하는 방식이며 `_ExpiryAwareOAuthClientProvider.__init__`에서 적용.
def _strip_duplicate_client_id_under_basic_auth(context: OAuthContext) -> None:
    """Drop the redundant body `client_id` when token auth uses HTTP Basic.

    The MCP SDK copies `client_id` into the token-request body (on both the
    authorization-code exchange and refresh paths) and, for
    `token_endpoint_auth_method == "client_secret_basic"`, *also* sends it in the
    `Authorization: Basic` header. RFC 6749 §2.3.1 carries the client identity in
    the header for Basic auth, so the body copy is redundant; some authorization
    servers (e.g. Pylon) reject the duplicate identity with an `OAuthTokenError`.
    Wrapping `prepare_token_auth` strips the body `client_id` only when a Basic
    header is present, leaving `client_secret_post`/`none` flows untouched.
    """
    original = context.prepare_token_auth

    def prepare_token_auth(
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        data, headers = original(data, headers)
        # RFC 7617 makes the auth-scheme token case-insensitive, so match
        # `basic` regardless of casing rather than coupling to the SDK's exact
        # `Basic ` literal.
        if headers.get("Authorization", "").lower().startswith("basic "):
            data = {k: v for k, v in data.items() if k != "client_id"}
        return data, headers

    context.prepare_token_auth = prepare_token_auth  # ty: ignore[invalid-assignment]


# [해설] dcode가 MCP SDK `OAuthClientProvider`를 확장한 provider. `build_oauth_provider`가 생성해 langchain_mcp_adapters 연결의 `auth`로 들어간다.
# [해설][설계] 추가 기능 4가지: (1) 저장된 만료 시각 복원 + 30초 margin, (2) refresh 전 디스크 재읽기, (3) 교차 프로세스 FileLock으로 refresh 직렬화,
# [해설] (4) 401 이전에 메타데이터를 선제 발견해 올바른 토큰 엔드포인트 사용. 대상 위협: refresh token 회전 서버(LangSmith 등)의 재사용 감지 → 토큰 계열 전체 폐기.
# [해설][주의] `_initialize`, `_refresh_token`, `_handle_refresh_response` 등 SDK의 private 메서드에 의존하므로 SDK 업그레이드에 취약하다.
class _ExpiryAwareOAuthClientProvider(OAuthClientProvider):
    """`OAuthClientProvider` that restores `token_expiry_time` from storage.

    Upstream `_initialize` loads stored tokens but leaves
    `context.token_expiry_time` at `None`, which makes `is_token_valid`
    report any stored access token — even one that expired hours ago —
    as valid. The SDK then sends a stale `Bearer`, gets a 401, and falls
    into a full re-auth (browser) instead of the `refresh_token` grant.

    Restoring the persisted absolute expiry to the context after load
    lets the SDK's refresh-when-invalid-and-refreshable branch fire on
    the first request after a cold start. When the sidecar is absent
    (older token files written before this field existed), assume the
    token is expired so the refresh path still gets a chance before
    falling back to 401.
    """

    # [해설] `suppress_expected_reauth_logs`는 dcode 전용 kwarg라 SDK 생성자에 넘기기 전에 pop한다(비대화형일 때 True).
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._suppress_expected_reauth_logs = bool(
            kwargs.pop("suppress_expected_reauth_logs", False)
        )
        super().__init__(*args, **kwargs)
        _strip_duplicate_client_id_under_basic_auth(self.context)

    async def _initialize(self) -> None:
        # Overrides a leading-underscore SDK method; behavior depends on
        # `super()._initialize()` populating `context.current_tokens` from
        # storage. If an upstream rename or refactor breaks that contract,
        # the test suite's TestExpiryAwareOAuthClientProvider cases will
        # fail loudly rather than silently regress to the 401-on-restart
        # bug this class exists to prevent.
        await super()._initialize()
        await self._apply_stored_expiry()

    # [해설] 저장된 expires_at을 SDK context에 주입.
    # [해설][흐름] 1) 메타데이터 미캐시면 저장소에서 로드 → 2) 토큰+만료를 한 스냅샷에서 읽음(구현 없으면 만료만) → 3) 만료 모름: refresh token 있으면 1.0(=만료됨)으로 설정
    # [해설][흐름] 4) 만료 앎: `expires_at - 30초`를 token_expiry_time으로 설정 → 첫 요청에서 SDK의 refresh 분기가 발동.
    async def _apply_stored_expiry(self) -> None:
        """Seed `context.token_expiry_time` from the persisted sidecar.

        Upstream `_initialize` loads stored tokens but leaves the expiry unset,
        so a token whose access portion expired long ago still reports as valid
        and is sent stale. Restoring the absolute expiry recorded beside the
        token lets `is_token_valid` return `False` in time for the cheaper
        refresh grant to fire. Also caches persisted OAuth metadata so the
        refresh uses the advertised token endpoint. Safe to call repeatedly, so
        it doubles as the post-reload expiry refresh.
        """
        if self.context.oauth_metadata is None:
            get_oauth_metadata = getattr(
                self.context.storage,
                "get_oauth_metadata",
                None,
            )
            if get_oauth_metadata is not None:
                self.context.oauth_metadata = await get_oauth_metadata()
        get_tokens_with_expiry = getattr(
            self.context.storage,
            "get_tokens_with_expiry",
            None,
        )
        if get_tokens_with_expiry is not None:
            tokens, expires_at = await get_tokens_with_expiry()
            # Keep the token and expiry paired from the same file snapshot. A
            # peer may rotate both while the upstream initializer is yielding.
            self.context.current_tokens = tokens
        else:
            get_expires_at = getattr(self.context.storage, "get_expires_at", None)
            if get_expires_at is None:
                return
            expires_at = await get_expires_at()
            tokens = self.context.current_tokens
        if expires_at is None:
            # Use 1.0 (one second after the Unix epoch) rather than 0.0 so the
            # SDK's `not self.token_expiry_time` falsy-zero check doesn't treat
            # the sentinel as "no expiry known" and mark the token valid again.
            if tokens is not None and tokens.refresh_token:
                self.context.token_expiry_time = 1.0
            elif tokens is not None and tokens.access_token:
                # Legacy file with no refresh_token: nothing we can do to
                # pre-empt expiry. Surface a structural breadcrumb so the
                # 401-then-browser-reauth flow isn't completely silent.
                logger.info(
                    "Legacy MCP token file for %s has no refresh_token; "
                    "cannot pre-empt expiry. The next 401 will trigger "
                    "browser re-auth.",
                    self.context.server_url,
                )
            return
        if expires_at - time.time() < _REFRESH_SAFETY_MARGIN_SECONDS:
            # Token already inside its safety margin (or past it) — likely a
            # cold start after a long pause, or a misconfigured server issuing
            # sub-margin lifetimes. Log only the duration, never any token
            # material.
            logger.debug(
                "MCP token for %s is within %.0fs of expiry on load; "
                "scheduling refresh on next request.",
                self.context.server_url,
                _REFRESH_SAFETY_MARGIN_SECONDS,
            )
        self.context.token_expiry_time = expires_at - _REFRESH_SAFETY_MARGIN_SECONDS

    # [해설] 다른 프로세스가 회전시킨 토큰을 반영하기 위해 tokens·client_info·만료를 디스크에서 다시 읽는다. refresh 락을 잡은 직후 호출.
    async def _reload_tokens_from_storage(self) -> None:
        """Re-read persisted tokens so a peer's refresh is observed.

        Another dcode process (or a separate provider instance in this process)
        may have rotated the refresh token on disk while this provider held a
        now-stale copy in memory. Re-reading before deciding to refresh keeps
        this provider from replaying an already-rotated refresh token, which
        the LangSmith OAuth server treats as reuse and punishes by revoking the
        whole identity+client token family.
        """
        self.context.current_tokens = await self.context.storage.get_tokens()
        client_info = await self.context.storage.get_client_info()
        if client_info is not None:
            self.context.client_info = client_info
        await self._apply_stored_expiry()

    # [해설] FileLock 획득을 스레드에서 최대 60초 대기. timeout 또는 OSError(읽기 전용 디렉터리 등)면 False → 호출자는 refresh를 하지 않는다(fail-safe).
    # [해설] 취소가 겹치면 획득 결과를 확인한 뒤 취소를 우선 전파한다.
    async def _acquire_refresh_lock(self, lock: FileLock) -> bool:
        """Wait for the cross-process refresh lock off the event loop.

        `lock.acquire` blocks for up to `_REFRESH_LOCK_TIMEOUT_SECONDS` while a
        peer finishes its refresh, so it runs in a worker thread to avoid
        stalling the event loop for that long.

        Args:
            lock: The `filelock.FileLock` serializing refreshes for this server
                (backed by the sidecar `.lock` file, not the token file).

        Returns:
            `True` when the lock was acquired; `False` when the wait timed out
            or the lock could not be created, signalling the caller to avoid
            using the possibly in-flight refresh token after reloading.
        """
        acquire_task = asyncio.create_task(
            asyncio.to_thread(
                lock.acquire,
                timeout=_REFRESH_LOCK_TIMEOUT_SECONDS,
            )
        )
        cancellation = await _join_task_deferring_cancellation(acquire_task)
        try:
            acquire_task.result()
        except Timeout:
            if cancellation is not None:
                raise cancellation from None
            # A timeout means a peer may still be mid-refresh with this same
            # token. Do not refresh unlocked: rotating-token servers can treat
            # the second grant as reuse and revoke the whole token family.
            logger.warning(
                "Timed out after %.0fs waiting for the MCP token refresh lock "
                "for %s; skipping refresh to avoid refresh-token reuse.",
                _REFRESH_LOCK_TIMEOUT_SECONDS,
                self.context.server_url,
            )
            return False
        except OSError as exc:
            if cancellation is not None:
                raise cancellation from None
            # Creating/locking the sidecar can fail (read-only or missing
            # tokens dir, permission denial on a hardened host). Avoid an
            # unlocked refresh so we do not replay a rotating refresh token if a
            # peer did manage to take the lock.
            logger.warning(
                "Could not acquire the MCP token refresh lock for %s (%s); "
                "skipping refresh to avoid refresh-token reuse.",
                self.context.server_url,
                type(exc).__name__,
            )
            return False
        if cancellation is not None:
            raise cancellation
        return True

    # [해설] refresh 임계 구역 동안 락을 보유하는 async 컨텍스트. 해제 조건은 획득 결과가 아니라 `lock.is_locked` —
    # [해설] 획득 직후 취소가 들어와도 락이 고아가 되지 않게. 보호 대상 예외와 해제 실패·지연 취소 중 무엇을 우선할지 세밀히 정리한다.
    @contextlib.asynccontextmanager
    async def _refresh_lock_guard(self, lock_path: Path) -> AsyncIterator[bool]:
        """Hold the cross-process refresh lock across the serialized refresh.

        Acquires the lock (waiting up to `_REFRESH_LOCK_TIMEOUT_SECONDS`; on
        timeout it yields `False` so the caller can avoid the refresh grant).
        Release is gated on `lock.is_locked` rather than the acquire result, so
        a cancellation that lands *after* the worker thread took the lock still
        frees it instead of orphaning it, while a timed-out/failed acquisition
        skips the release. Acquisition and release are each joined before
        cancellation escapes, so neither worker can acquire or retain the lock
        after the guard has returned.

        Args:
            lock_path: Sibling `.lock` path from `FileTokenStorage`.

        Yields:
            Whether the refresh lock was acquired.
        """
        # `thread_local=False` because acquire and release run in different
        # `asyncio.to_thread` worker threads; the default would refuse the
        # cross-thread release and leak the OS lock until process exit.
        lock = FileLock(str(lock_path), thread_local=False)
        pending_exception: BaseException | None = None
        try:
            yield await self._acquire_refresh_lock(lock)
        except BaseException as exc:
            # Preserve the guarded operation's exception while joining cleanup.
            pending_exception = exc
            raise
        finally:
            if lock.is_locked:
                release_task = asyncio.create_task(asyncio.to_thread(lock.release))
                cancellation = await _join_task_deferring_cancellation(release_task)
                try:
                    release_task.result()
                except Exception as exc:
                    if pending_exception is None:
                        # No guarded error to preserve, so the release failure
                        # is the primary error to surface. It supersedes any
                        # deferred cancellation; log that loss so it is not
                        # silent.
                        if cancellation is not None:
                            logger.warning(
                                "MCP token refresh lock release for %s failed; "
                                "a deferred cancellation is superseded by the "
                                "release error.",
                                self.context.server_url,
                            )
                        raise
                    # Preserve the guarded operation's exception — including a
                    # `CancelledError`, whose propagation structured
                    # cancellation depends on — and record the release failure
                    # as a note rather than masking the original with it.
                    pending_exception.add_note(
                        "MCP refresh lock release also failed with "
                        f"{type(exc).__name__}."
                    )
                    logger.warning(
                        "Failed to release the MCP token refresh lock for %s "
                        "while propagating %s",
                        self.context.server_url,
                        type(pending_exception).__name__,
                        exc_info=True,
                    )
                else:
                    # Release succeeded. Re-raise a deferred cancellation unless
                    # a guarded error is already propagating, in which case the
                    # guarded error wins; log the superseded cancellation so the
                    # dropped edge is not silent (parity with the failure path).
                    if cancellation is not None:
                        if pending_exception is None:
                            raise cancellation
                        logger.warning(
                            "MCP token refresh lock for %s released cleanly, but "
                            "a deferred cancellation is superseded by the "
                            "in-flight %s.",
                            self.context.server_url,
                            type(pending_exception).__name__,
                        )

    # [해설] 발견한 OAuth 메타데이터를 저장소에 기록(저장소가 지원할 때만).
    async def _persist_oauth_metadata(self) -> None:
        """Persist discovered public OAuth metadata when storage supports it."""
        if self.context.oauth_metadata is None:
            return
        set_oauth_metadata = getattr(self.context.storage, "set_oauth_metadata", None)
        if set_oauth_metadata is not None:
            await set_oauth_metadata(self.context.oauth_metadata)

    # [해설] 전체 로그인(authorization code 교환) 응답 처리 후 메타데이터도 함께 저장하도록 SDK 훅을 확장.
    async def _handle_token_response(self, response: httpx.Response) -> None:
        """Persist tokens and any metadata discovered during full OAuth login."""
        await super()._handle_token_response(response)
        await self._persist_oauth_metadata()

    # [해설] 락을 잡은 refresh의 응답 처리. 400/401/403 실패면 토큰을 메모리에서 지우고 `_initialized=False`로 되돌려 SDK 재인증 흐름에 넘긴다.
    # [해설] 그 외 상태코드의 예외는 그대로 전파(장애를 재인증으로 오인하지 않음).
    async def _handle_locked_refresh_response(self, response: httpx.Response) -> bool:
        """Handle a serialized refresh without bypassing SDK re-auth fallback.

        Args:
            response: Refresh endpoint response returned through the auth generator.

        Returns:
            `True` when refresh succeeded, otherwise `False` so the caller can
            continue into the delegated SDK flow.
        """
        try:
            return bool(await self._handle_refresh_response(response))
        except Exception:
            if response.status_code not in _EXPECTED_REAUTH_REFRESH_STATUS_CODES:
                raise
            logger.debug(
                "Locked MCP token refresh for %s failed with %s; "
                "deferring to the SDK re-auth flow.",
                self.context.server_url,
                response.status_code,
            )
            self.context.clear_tokens()
            self._initialized = False
            return False

    # [해설] httpx `Auth` 흐름(비동기 제너레이터). 요청마다 호출되며 yield로 추가 HTTP 요청(메타데이터·refresh)을 보내고 응답을 받는다.
    # [해설][흐름] 1) 초기화(저장 토큰+만료 복원) → 2) 토큰 무효·refresh 가능·메타데이터 없음이면 PRM(RFC 9728)→인가 서버 메타데이터 선제 발견
    # [해설][흐름] 3) 여전히 무효·refresh 가능하면 FileLock 안에서 디스크 재읽기 → 락 획득 시에만 refresh, 실패 시 메모리 토큰 삭제
    # [해설][흐름] 4) 나머지는 SDK 원래 `async_auth_flow`에 수동 위임(asend로 응답 전달).
    async def async_auth_flow(
        self,
        request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Discover and cache OAuth metadata before the SDK refresh branch.

        Yields:
            HTTP requests for OAuth metadata discovery and the delegated SDK auth flow.
        """
        async with self.context.lock:
            if not self._initialized:
                await self._initialize()
            self.context.protocol_version = request.headers.get(MCP_PROTOCOL_VERSION)
            if (
                not self.context.is_token_valid()
                and self.context.can_refresh_token()
                and self.context.oauth_metadata is None
            ):
                # Pre-empt the SDK's 401-path discovery so its refresh branch
                # finds populated `oauth_metadata` and uses the advertised token
                # endpoint instead of guessing `/token`. The resource-metadata
                # URL is `None`: no 401 yet, so no `WWW-Authenticate` to read.
                try:
                    prm_urls = build_protected_resource_metadata_discovery_urls(
                        None,
                        self.context.server_url,
                    )
                    for url in prm_urls:
                        # ASYNC119: yielding the request to receive its response is
                        # this auth generator's handshake protocol, not a value
                        # escaping a context manager.
                        response = yield create_oauth_metadata_request(url)  # noqa: ASYNC119
                        prm = await handle_protected_resource_response(response)
                        if prm is None:
                            logger.debug(
                                "Protected resource metadata discovery failed: %s",
                                url,
                            )
                            continue
                        self.context.protected_resource_metadata = prm
                        self.context.auth_server_url = str(prm.authorization_servers[0])
                        break

                    asm_urls = build_oauth_authorization_server_metadata_discovery_urls(
                        self.context.auth_server_url,
                        self.context.server_url,
                    )
                    for url in asm_urls:
                        # ASYNC119: yielding the request to receive its response is
                        # this auth generator's handshake protocol, not a value
                        # escaping a context manager.
                        response = yield create_oauth_metadata_request(url)  # noqa: ASYNC119
                        ok, metadata = await handle_auth_metadata_response(response)
                        if not ok:
                            break
                        if metadata is None:
                            logger.debug("OAuth metadata discovery failed: %s", url)
                            continue
                        self.context.oauth_metadata = metadata
                        await self._persist_oauth_metadata()
                        break
                except httpx.HTTPError as exc:
                    # Log only the exception type, never its payload — discovery
                    # responses travel the same channel as bearer tokens.
                    logger.debug(
                        "Pre-emptive OAuth metadata discovery for %s raised %s; "
                        "deferring to the SDK auth flow.",
                        self.context.server_url,
                        type(exc).__name__,
                    )

            # [해설][흐름] 3) 교차 프로세스 refresh 직렬화. `self.context.lock`은 이 provider 인스턴스만 보호하므로 FileLock이 추가로 필요하다.
            if (
                not self.context.is_token_valid()
                and self.context.can_refresh_token()
                and isinstance(self.context.storage, FileTokenStorage)
            ):
                # Serialize the refresh across processes and provider instances.
                # Without this, two holders of the same token file can both
                # replay the same refresh token; the LangSmith OAuth server
                # rotates refresh tokens and revokes the entire token family on
                # reuse, which surfaces as requests hanging until a full
                # re-auth. `self.context.lock` only guards this one provider,
                # so a file lock is required for the cross-process case.
                async with self._refresh_lock_guard(
                    self.context.storage.refresh_lock_path
                ) as refresh_lock_acquired:
                    # A peer may have rotated the token while we waited for the
                    # lock; reload so a now-valid token skips the refresh.
                    await self._reload_tokens_from_storage()
                    if (
                        not self.context.is_token_valid()
                        and self.context.can_refresh_token()
                    ):
                        if refresh_lock_acquired:
                            # ASYNC119: the refresh lock must stay held across this
                            # yield — the request/response round-trip is the
                            # critical section being serialized. Release is safe
                            # because httpx deterministically drives and
                            # `aclose()`s this generator (see the delegation note
                            # below), so the guard's `finally` runs rather than
                            # deferring cleanup to GC.
                            refresh_response = yield await self._refresh_token()  # noqa: ASYNC119
                            await self._handle_locked_refresh_response(refresh_response)
                        else:
                            # The delegated SDK flow has its own refresh branch;
                            # clear only in-memory tokens so this request falls
                            # through to re-auth instead of replaying the refresh
                            # token while another process may still be using it.
                            self.context.clear_tokens()

        # [해설][흐름] 4) SDK 흐름 위임. 억제 플래그를 ContextVar로 켠 채 내부 제너레이터를 `anext`→`asend` 루프로 구동하고 끝나면 aclose.
        # Delegate to the SDK flow by manually pumping the inner generator so
        # the HTTP responses httpx feeds back via `auth_flow.asend(response)`
        # are forwarded into it. A plain `async for` would advance the inner
        # generator with `__anext__()` (i.e. `asend(None)`), discarding every
        # response — the SDK's `response = yield request` and refresh-path
        # `yield refresh_request` would then see `None` and raise
        # `AttributeError: 'NoneType' object has no attribute 'status_code'`,
        # surfacing as the `ExceptionGroup` users hit on MCP OAuth login.
        # httpx primes the flow with `__anext__()`, then drives it with
        # `asend`/`aclose` (never `athrow`), so forwarding sent values and
        # closing the inner generator on `GeneratorExit` is sufficient — no
        # `athrow` forwarding needed.
        token: contextvars.Token[bool] | None = None
        if self._suppress_expected_reauth_logs:
            token = _SUPPRESS_EXPECTED_REAUTH_LOGS.set(True)
        inner = super().async_auth_flow(request)
        try:
            # Prime with `anext()` (no response to send yet); thereafter every
            # resume carries httpx's response back in via `asend`.
            flow_request = await anext(inner)
            while True:
                response = yield flow_request
                flow_request = await inner.asend(response)
        except StopAsyncIteration:
            return
        finally:
            await inner.aclose()
            if token is not None:
                _SUPPRESS_EXPECTED_REAUTH_LOGS.reset(token)


# [해설] MCP 연결용 OAuth provider 팩토리. 호출자: `mcp_tools.py`(서버 프로세스, 연결 구성 시), `login`(클라이언트, 명시적 로그인).
# [해설][흐름] 1) URL 호스트로 provider 정책 선택 → 2) 대화형이면 loopback(고정 포트 또는 저장 포트 재사용/랜덤) 또는 paste-back 핸들러
# [해설][흐름] 3) 비대화형이면 재인증 필요 예외 핸들러 → 4) 정책의 client_metadata(redirect_uri 포함) → 5) `_ExpiryAwareOAuthClientProvider` 생성.
def build_oauth_provider(
    *,
    server_name: str,
    server_url: str,
    storage: TokenStorage,
    extra_auth_params: dict[str, str] | None = None,
    interactive: bool = True,
    ui: OAuthInteraction | None = None,
) -> OAuthClientProvider:
    """Construct an `OAuthClientProvider` for an MCP server.

    Args:
        server_name: MCP server name used in re-auth messages.
        server_url: Remote MCP server URL.
        storage: Token storage implementation for this server.
        extra_auth_params: Optional query params for the interactive auth URL.
        interactive: Whether the provider may prompt on stdin.
        ui: Interaction surface used for URL display and paste-back
            input in interactive mode.

    Returns:
        A configured `OAuthClientProvider`.
    """
    from deepagents_code.mcp_providers import resolve_provider

    policy = resolve_provider(server_url)
    redirect_uri: str | None = None

    if interactive:
            # [해설] 정책이 고정 포트를 요구하면 그것을 쓴다(예: Slack은 사전 등록된 공개 client라 redirect 포트가 고정 — analysis/07 기준 3118).
        if policy.supports_loopback_callback():
            fixed = policy.loopback_port()
            if fixed is not None:
                port = fixed
            else:
                # Reuse the port from a prior DCR registration when available,
                # so the authorize request's redirect_uri matches what was
                # registered against the persisted client_id. A fresh random
                # port on every launch would otherwise invalidate the URI on
                # the second run and force the server to reject the request.
                stored = (
                    storage.stored_loopback_port()
                    if isinstance(storage, FileTokenStorage)
                    else None
                )
                # No reusable port means any persisted registration can't be
                # paired with the random loopback port we're about to bind. Drop
                # a stale registration so the handshake re-runs DCR with a
                # matching redirect URI instead of failing with "invalid or
                # missing redirect_uri".
                if (
                    stored is None
                    and isinstance(storage, FileTokenStorage)
                    and storage.discard_client_info_if_loopback_unusable()
                ):
                    logger.info(
                        "Discarded a stale MCP client registration for %s "
                        "whose redirect URI can't serve loopback login; the "
                        "handshake will register a fresh client.",
                        server_name,
                    )
                port = stored if stored is not None else _choose_loopback_port()
            callback_server = _LoopbackOAuthCallbackServer(port=port)
            redirect_uri = callback_server.redirect_uri
            redirect, callback = _make_loopback_handlers(
                callback_server=callback_server,
                extra_auth_params=extra_auth_params,
                ui=ui,
            )
        else:
            redirect, callback = _make_paste_back_handlers(
                extra_auth_params=extra_auth_params,
                ui=ui,
            )
    else:
        # [해설] 비대화형(서버 프로세스): 인가 URL을 띄울 수 없으므로 토큰이 없거나 refresh 실패 시 곧바로 `MCPReauthRequiredError`.
        redirect, callback = _make_reauth_required_handlers(server_name=server_name)

    metadata = (
        policy.client_metadata(redirect_uri=redirect_uri)
        if redirect_uri is not None
        else policy.client_metadata()
    )

    return _ExpiryAwareOAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect,
        callback_handler=callback,
        suppress_expected_reauth_logs=not interactive,
    )


# [해설] RFC 8628 Device Authorization Grant. `mcp_providers/github.py`(GitHub Copilot MCP)가 사용.
# [해설][흐름] 1) device_code 요청 → 2) 사용자 코드·검증 URL 표시 → 3) interval마다 토큰 엔드포인트 폴링(expires_in까지)
# [해설][흐름] 4) authorization_pending은 계속, slow_down은 interval +5초, 기타 error는 실패 → 5) 성공 응답을 `OAuthToken`으로 검증해 반환.
# [해설] 토큰 저장은 이 함수가 아니라 호출자(provider) 몫 (추정).
async def _run_device_flow(
    *,
    device_code_url: str,
    token_url: str,
    client_id: str,
    scope: str | None = None,
    ui: OAuthInteraction | None = None,
) -> OAuthToken:
    """Run OAuth 2.0 Device Authorization Grant and return the token.

    Args:
        device_code_url: Provider endpoint that issues a device + user code.
        token_url: Provider endpoint to poll for the access token.
        client_id: Registered OAuth client ID.
        scope: Optional space-delimited scope string.
        ui: Interaction surface used to display the device code.

    Returns:
        The issued OAuth access token payload.

    Raises:
        RuntimeError: If the device flow fails, times out, or the provider
            returns an unexpected HTTP status on the device-code request.
    """
    import asyncio

    import httpx

    interaction = ui if ui is not None else _default_ui()

    init_data = {"client_id": client_id}
    if scope is not None:
        init_data["scope"] = scope

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            device_code_url,
            data=init_data,
            headers={"Accept": "application/json"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            msg = (
                f"Device code request failed: HTTP {response.status_code} "
                f"from {device_code_url}."
            )
            raise RuntimeError(msg) from exc
        try:
            device = _DeviceCodeResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            msg = (
                f"Device code response from {device_code_url} is missing "
                f"required fields: {exc}"
            )
            raise RuntimeError(msg) from exc

        await interaction.show_device_code(
            verification_uri=device.verification_uri,
            user_code=device.user_code,
            expires_in=device.expires_in,
        )

        interval = max(device.interval, 1)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + device.expires_in
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            token_response = await client.post(
                token_url,
                data={
                    "client_id": client_id,
                    "device_code": device.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            # RFC 8628 §3.5 lets providers return `authorization_pending` /
            # `slow_down` with either a 200 or 400 response. Check the body
            # before raise_for_status so 400-returning providers work.
            try:
                body = token_response.json()
            except ValueError as exc:
                # Malformed JSON would otherwise cascade into a confusing
                # OAuthToken.model_validate({}) error below; log the cause
                # explicitly so debugging is possible.
                logger.warning(
                    "Token endpoint %s returned non-JSON body: %s",
                    token_url,
                    exc,
                )
                body = {}
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err:
                msg = f"Device flow failed: {err}: {body.get('error_description', '')}"
                raise RuntimeError(msg)
            try:
                token_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                msg = (
                    f"Token request failed: HTTP {token_response.status_code} "
                    f"from {token_url}."
                )
                raise RuntimeError(msg) from exc
            try:
                return OAuthToken.model_validate(body)
            except ValidationError as exc:
                msg = (
                    f"Token response from {token_url} is not a valid "
                    f"OAuth token payload: {exc}"
                )
                raise RuntimeError(msg) from exc

    msg = "Device flow timed out. Try logging in again."
    raise RuntimeError(msg)


# [해설] 로그인 실패 예외를 토큰 안전한 한 줄 요약으로 변환. `app.py`의 `/mcp login` 처리와 `mcp_tools.py`의 로드 실패 로그가 사용.
# [해설] 우선순위: 중첩 `MCPReauthRequiredError` 메시지 → `MCPConfigError` 메시지 → loopback 예외 메시지 → 그 외는 예외 클래스명 체인만.
def format_login_failure(exc: BaseException) -> str:
    """Return a token-safe single-line summary of an OAuth-login exception.

    OAuth handshakes commonly surface as `ExceptionGroup` (anyio task
    groups) or as MCP-SDK errors whose `args`/`repr` may include an
    `OAuthToken`. Never call `str()`/`repr()` on the raw exception for
    display or logging — instead, prefer a known-safe nested
    `MCPReauthRequiredError` message, fall back to the messages of our
    own loopback-related exception types, and degrade to a class-name
    chain for anything else.

    Args:
        exc: Root exception caught from the login worker.

    Returns:
        A user-displayable string that is safe to log and to render.
    """
    reauth = find_reauth_required(exc)
    if reauth is not None:
        return str(reauth)

    from deepagents_code.mcp_tools import MCPConfigError

    if isinstance(exc, MCPConfigError):
        # Config-interpolation errors are our own and are raised before the
        # OAuth handshake, so they carry no token material and their
        # field-scoped messages are safe (and useful) to render verbatim.
        return str(exc)

    safe_types = (
        _LoopbackCallbackTimeoutError,
        _LoopbackCallbackUnavailableError,
    )
    if isinstance(exc, safe_types):
        return f"{type(exc).__name__}: {exc}"

    parts: list[str] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        parts.append(type(current).__name__)
        if isinstance(current, BaseExceptionGroup):
            parts.append(
                "[" + ", ".join(type(e).__name__ for e in current.exceptions[:5]) + "]"
            )
            break
        current = current.__cause__ or current.__context__
    return " -> ".join(parts) if parts else type(exc).__name__


# [해설] ExceptionGroup·__cause__·__context__를 순회(순환 방지)해 `MCPReauthRequiredError`를 찾는다. anyio 태스크 그룹이 예외를 감싸기 때문.
def find_reauth_required(exc: BaseException) -> MCPReauthRequiredError | None:
    """Find an `MCPReauthRequiredError` anywhere inside `exc`'s tree.

    Walks `exceptions` (for `ExceptionGroup`), then `__cause__` and
    `__context__`, tracking visited nodes to terminate on cyclic chains.

    Args:
        exc: Root exception to inspect.

    Returns:
        The nested `MCPReauthRequiredError`, or `None` if not present.
    """
    visited: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, MCPReauthRequiredError):
            return current
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            stack.append(cause)
    return None


# [해설] WWW-Authenticate 헤더에서 Bearer 챌린지와 RFC 9728 `resource_metadata` URL을 찾는 정규식들.
_BEARER_SCHEME_RE = re.compile(r"(?:^|,)\s*bearer\b", re.IGNORECASE)
"""Match a `Bearer` auth scheme at the start of a challenge or after a comma.

A `WWW-Authenticate` line may list several schemes (RFC 7235); anchoring to
the start or a preceding comma finds `Bearer` even when it isn't listed first.
"""

_RESOURCE_METADATA_RE = re.compile(
    r'(?:^|[\s,])resource_metadata\s*=\s*"?([^",\s]+)',
    re.IGNORECASE,
)
"""Capture the RFC 9728 `resource_metadata` URL from a Bearer challenge."""


# [해설] 여러 WWW-Authenticate 값·여러 챌린지 중 Bearer + resource_metadata를 가진 것을 찾는다.
def _oauth_resource_challenge(headers: httpx.Headers) -> str | None:
    """Return the RFC 9728 `resource_metadata` URL from a Bearer challenge.

    A single `WWW-Authenticate` header line may carry several comma-separated
    challenges (RFC 7235), and a response may repeat the header. Scan every
    value for a `Bearer` scheme — anywhere in the line, not only first — that
    advertises a `resource_metadata` parameter.

    Args:
        headers: Response headers to inspect.

    Returns:
        The `resource_metadata` URL when a Bearer challenge carries one,
            else `None`.
    """
    for value in headers.get_list("www-authenticate"):
        if _BEARER_SCHEME_RE.search(value) is None:
            continue
        match = _RESOURCE_METADATA_RE.search(value)
        if match is not None:
            return match.group(1)
    return None


# [해설] 예외 트리에서 "401 + Bearer resource_metadata 챌린지"를 찾는다. `mcp_tools.py`가 `auth: oauth` 미설정 서버도
# [해설] OAuth가 필요한 서버로 자동 감지해 `unauthenticated` 상태로 표시하는 데 사용한다.
def find_oauth_challenge(exc: BaseException) -> str | None:
    """Return the `resource_metadata` URL of a 401 OAuth challenge in `exc`.

    Per the MCP authorization spec (RFC 9728), a server requiring OAuth
    answers an unauthenticated request with HTTP 401 plus a Bearer
    `WWW-Authenticate` challenge pointing at its protected-resource metadata.
    The MCP client surfaces that as an `httpx.HTTPStatusError`. Walks
    `exceptions` (for `ExceptionGroup`), then `__cause__`/`__context__`,
    tracking visited nodes to terminate on cyclic chains.

    Args:
        exc: Root exception to inspect.

    Returns:
        The `resource_metadata` URL when a 401 response carrying a Bearer
            challenge is found, else `None`.
    """
    visited: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            response = current.response
            if (
                response is not None and response.status_code == 401  # noqa: PLR2004  # HTTP Unauthorized
            ):
                challenge = _oauth_resource_challenge(response.headers)
                if challenge is not None:
                    return challenge
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            stack.append(cause)
    return None


# [해설] 1회용 MCP 세션을 열었다 닫아 OAuth 핸드셰이크(=SDK auth 흐름)를 강제로 일으킨다. 도구 목록은 가져오지 않는다.
async def _drive_handshake(connections: dict) -> None:
    """Open a one-shot MCP session for `connections` to trigger OAuth handshake."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(connections=connections)
    server_name = next(iter(connections))
    async with client.session(server_name):
        pass


# [해설] 명시적 로그인 진입점. 클라이언트 측 `/mcp login`(app.py)과 `dcode mcp login`에서 호출된다.
# [해설][흐름] 1) 전송이 http/sse인지 확인(auth: oauth 선언 여부와 무관 — RFC 9728 발견 기반) → 2) `${VAR}` 보간(실패는 MCPConfigError)
# [해설][흐름] 3) provider 정책의 `run_login`(GitHub device flow 등 자체 흐름) → 완료면 종료
# [해설][흐름] 4) 아니면 `_FreshLoginTokenStorage`로 provider를 만들어 1회용 세션 핸드셰이크 → 토큰 저장 → 성공 메시지.
# [해설][주의] 로그인 후 서버 프로세스의 MCP 도구가 즉시 생기지는 않는다 — 서버 재시작/재연결이 필요(`awaiting_reconnect` 상태, `mcp_tools.MCPServerStatus`).
async def login(
    *,
    server_name: str,
    server_config: McpServerSpec,
    ui: OAuthInteraction,
) -> None:
    """Drive OAuth login for `server_name`, persisting tokens on success.

    Args:
        server_name: Name of the configured MCP server.
        server_config: Parsed server config for that entry.
        ui: Interaction surface for all user prompts and progress messages
            during the flow.

    Raises:
        ValueError: If `server_config` isn't an http/sse server.
        MCPConfigError: If config env-var interpolation fails or a
            supported field has the wrong type (a non-string value, or
            args/env/headers with the wrong container type).
        RuntimeError: If the device flow fails or times out, or the
            OAuth handshake aborts.
    """  # noqa: DOC502 - `RuntimeError` surfaces via the device flow / handshake
    from langchain_mcp_adapters.sessions import (
        SSEConnection,
        StreamableHttpConnection,
    )

    from deepagents_code.mcp_tools import MCPConfigError, _resolve_server_type

    # OAuth login is discovery-based (RFC 9728), so it works for any remote
    # http/sse server — whether the config opted in with `auth: oauth` or the
    # server was auto-detected as needing auth via a 401 challenge. Only the
    # transport needs gating; stdio servers can't speak OAuth.
    transport = _resolve_server_type(server_config)
    if transport not in {"http", "sse"}:
        msg = (
            f"Server '{server_name}' uses {transport!r} transport; "
            "OAuth login is only valid for http/sse."
        )
        raise ValueError(msg)
    try:
        resolved_config = resolve_mcp_server_env(server_name, server_config)
    except (RuntimeError, TypeError) as exc:
        # Re-raise as MCPConfigError (a ValueError) so callers' existing
        # config-error handling catches it, and `format_login_failure`
        # preserves the actionable, field-scoped message instead of
        # collapsing it to a bare "RuntimeError"/"TypeError".
        raise MCPConfigError(str(exc)) from exc

    from deepagents_code.mcp_providers import resolve_provider

    # [해설][흐름] 3) 토큰 파일은 URL 해시로 식별되므로 보간된 URL 기준으로 저장소를 만든다.
    storage = FileTokenStorage(server_name, server_url=resolved_config["url"])
    policy = resolve_provider(resolved_config["url"])
    result = await policy.run_login(
        server_name=server_name,
        server_url=resolved_config["url"],
        storage=storage,
        ui=ui,
    )

    success_message = f"Logged in to MCP server '{server_name}'."
    if is_env_truthy(DEBUG):
        success_message += f" Tokens saved to {storage.path}."

    if result.completed:
        await ui.show_success(success_message)
        return

    # Hide any still-valid stored token so this login actually re-authorizes
    # instead of silently succeeding on the existing credential. `run_login`
    # above keeps the real storage — providers preseed client info and tokens
    # through it.
    # [해설][흐름] 4) 기존 토큰을 숨긴 저장소로 전체 인가를 강제. 정책이 준 extra_auth_params(예: team)도 인가 URL에 반영.
    provider = build_oauth_provider(
        server_name=server_name,
        server_url=resolved_config["url"],
        storage=_FreshLoginTokenStorage(server_name, server_url=resolved_config["url"]),
        extra_auth_params=result.extra_auth_params or None,
        ui=ui,
    )
    conn: StreamableHttpConnection | SSEConnection
    if transport == "http":
        conn = StreamableHttpConnection(
            transport="streamable_http",
            url=resolved_config["url"],
            auth=provider,
        )
    else:
        conn = SSEConnection(
            transport="sse",
            url=resolved_config["url"],
            auth=provider,
        )

    if "headers" in resolved_config:
        conn["headers"] = resolved_config["headers"]

    await _drive_handshake({server_name: conn})
    await ui.show_success(success_message)
