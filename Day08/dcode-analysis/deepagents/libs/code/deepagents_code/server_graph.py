"""Server-side graph entry point for `langgraph dev`.

This module is referenced by the generated `langgraph.json` and exposes a graph
factory that the LangGraph server can load and serve.

The graph is created by `make_graph()`, which reads configuration from
`ServerConfig.from_env()` — the same dataclass the CLI uses to *write* the
configuration via `ServerConfig.to_env()`. This shared schema ensures the two
sides stay in sync.
"""
# [해설] ── 모듈 개요 ─────────────────────────────────────────────
# [해설] 역할: `langgraph dev` 서버가 로드하는 그래프 팩토리(`make_graph`)와
# [해설]   서버 프로세스 전체가 공유하는 런타임(그래프·백엔드·offload·MCP 메타데이터) 캐시를 제공한다.
# [해설] 실행 위치: **서버 프로세스 전용**. 클라이언트는 이 모듈을 import하지 않고,
# [해설]   `client/launch/server.py`의 `generate_langgraph_json`이 `deepagents_code.server_graph:make_graph`를 참조로 적는다.
# [해설] 주요 진입점: `make_graph`(langgraph.json graphs.agent), `_workspace_runtime`(offload_api가 별칭 import해 공유), `get_server_runtime`.
# [해설] 설정 입력: 클라이언트가 심어 둔 `DEEPAGENTS_CODE_SERVER_*` env → `ServerConfig.from_env()` (`_server_config.py`).
# [해설] 흐름 요약: make_graph → (run 요청이면) require_thread_workspace → _workspace_runtime → _make_graphs
# [해설]   → _make_graphs_in_environment → create_cli_agent(`agent.py`) → SDK `create_deep_agent`.
# [해설] 관련 분석: analysis/01-boot-client-server.md (흐름 E, 설계 포인트 6·7·8), analysis/02-agent-assembly-sdk-core.md
# [해설] 관련 문서: libs/code/ARCHITECTURE.md "Request flow", libs/code/DEVELOPMENT.md "Startup crash"/"LangSmith tracing projects"
# [해설][주의] langgraph-runtime-inmem의 blockbuster가 이벤트 루프 위 블로킹 I/O를 예외로 막으므로,
# [해설]   파일/경로/모델 생성은 모두 `asyncio.to_thread`로 넘기는 패턴이 반복된다.

from __future__ import annotations

import asyncio
import atexit
import logging
import sys
from collections import OrderedDict
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

# [해설][설계] 아래 import는 런타임에 필요하다(TYPE_CHECKING 금지). make_graph의 `runtime` 파라미터 타입을
# [해설]   langgraph 서버가 get_type_hints로 해석해 "runtime을 받는 팩토리"인지 판별하기 때문이다.
# Imported at runtime rather than under TYPE_CHECKING: the LangGraph server
# classifies `make_graph` by resolving its annotations with
# `typing.get_type_hints` at graph-load time. A name that only type checkers
# can see fails to resolve, and the server then refuses to load the graph.
from langgraph_sdk.runtime import ServerRuntime as LangGraphServerRuntime  # noqa: TC002

from deepagents_code._cli_context import CLIContextSchema
from deepagents_code._server_config import ServerConfig
from deepagents_code._startup_error import (
    STARTUP_ERROR_MARKER as _STARTUP_ERROR_MARKER,
    emit_startup_failure,
)
from deepagents_code.configuration.interpreter import InterpreterConfig
from deepagents_code.configuration.resolver import get_config_resolver
from deepagents_code.project_utils import ProjectContext, get_server_project_context
from deepagents_code.workspace import (
    PROJECT_POLICY_DRIFT_REASON,
    SERVER_CONFIG_DRIFT_REASON,
    WorkspaceConflictError,
    drifted_project_fields,
    resolve_workspace,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from contextlib import AbstractContextManager

    from deepagents.backends.composite import CompositeBackend

    EnvironmentContext = Callable[
        [Mapping[str, str] | None], AbstractContextManager[None]
    ]

    from deepagents_code.config import CredentialsSnapshot
    from deepagents_code.extensions.registry import ExtensionRegistry
    from deepagents_code.mcp_tools import MCPServerInfo
    from deepagents_code.offload_middleware import OffloadOperation
    from deepagents_code.workspace import WorkspaceBinding

logger = logging.getLogger(__name__)

# [해설] 프로세스 수명 동안 유지되는 전역 상태들.
# [해설] _sandbox_cm/_sandbox_backend: 샌드박스 컨텍스트 매니저와 그 백엔드. 프로세스당 1개(아래 `_claim_sandbox_workspace` 참고).
# [해설] _mcp_session_manager: MCP 세션 매니저 싱글턴(`_get_mcp_session_manager`).
# [해설] _server_tracing_settings/_initialized: 첫 워크스페이스가 고정한 LangSmith 트레이싱 설정과 초기화 여부.
_sandbox_cm: Any = None
_sandbox_backend: Any = None
_mcp_session_manager: Any = None
_server_tracing_settings: tuple[dict[str, str | None], bool] | None = None
_server_tracing_initialized = False


# [해설] 샌드박스 컨텍스트 매니저를 동기적으로 닫는다. atexit 핸들러와 취소 정리 경로에서 호출된다.
def _close_sandbox(context: AbstractContextManager[Any]) -> None:
    context.__exit__(None, None, None)


# [해설] 샌드박스 컨텍스트(`create_sandbox`가 반환)를 워커 스레드에서 `__enter__`한다.
# [해설][설계] asyncio.shield로 감싸 호출자가 취소돼도 진입 작업은 끝까지 진행시키고,
# [해설]   진입이 성공했다면 즉시 닫아 원격 샌드박스 세션이 새지 않게 한 뒤 CancelledError를 다시 올린다.
async def _open_sandbox(
    create: Callable[[], AbstractContextManager[Any]],
) -> tuple[AbstractContextManager[Any], Any]:
    def _enter() -> tuple[AbstractContextManager[Any], Any]:
        context = create()
        return context, context.__enter__()  # noqa: PLC2801

    task = asyncio.create_task(asyncio.to_thread(_enter))
    try:
        return await asyncio.shield(task)
    # [해설][흐름] 취소 경로: 진입 완료를 기다렸다가(성공 시) 컨텍스트를 닫고 취소를 전파한다.
    except asyncio.CancelledError:
        try:
            context, _ = await asyncio.shield(task)
        except BaseException:  # Preserve the caller's cancellation
            logger.debug(
                "Sandbox startup did not complete after cancellation", exc_info=True
            )
        else:
            await asyncio.to_thread(_close_sandbox, context)
        raise


# [해설] 워크스페이스 environ 기준 LangSmith 트레이싱 설정을 프로세스 전체에 "한 번" 고정한다.
# [해설] 호출: `_make_graphs`(런타임 빌드 직전). 두 번째 이후 워크스페이스가 다른 설정이면 WorkspaceConflictError.
# [해설] 관련 문서: DEVELOPMENT.md "LangSmith tracing projects" — 서버 하나는 첫 워크스페이스 트레이싱을 유지.
def _configure_server_tracing(environ: Mapping[str, str], *, redact: bool) -> None:
    """Pin tracing for the server lifetime before any runtime can execute.

    LangSmith uses process-wide env caches and a default client. Replacing
    them, even under a build lock, reroutes cached and concurrently executing
    runtimes. Only workspaces with matching tracing settings can share this
    process. Keep the reservation across failed builds and cache eviction.

    Called on the server loop with no suspension between claim and setup.
    """
    from deepagents_code.config import (
        _tracing_environment_values,
        configure_langsmith_secret_redaction,
        reconcile_tracing_environment,
    )

    global _server_tracing_settings, _server_tracing_initialized  # noqa: PLW0603  # process-lifetime policy
    # [해설] 비교 키 = (트레이싱 관련 env 값들, 비밀값 redaction 여부). 이미 예약된 값과 다르면 거부.
    settings = (_tracing_environment_values(environ), redact)
    if _server_tracing_settings is not None and settings != _server_tracing_settings:
        reason = (
            "its LangSmith tracing settings differ from this server's; "
            "start a separate server for this workspace"
        )
        conflict = WorkspaceConflictError.from_reason(reason)
        raise conflict
    # [해설][주의] 실패한 빌드나 LRU 축출 이후에도 예약은 해제하지 않는다(docstring의 "Keep the reservation").
    _server_tracing_settings = settings
    if not _server_tracing_initialized:
        reconcile_tracing_environment(environ)
        # Keep redaction on the server task: its fail-closed disable must
        # reach this task's LangSmith ContextVar, not a worker's copied context.
        configure_langsmith_secret_redaction()
        _server_tracing_initialized = True


# [해설] 사람이 읽을 메시지와 `DEEPAGENTS_STARTUP_ERROR:` 마커 줄을 stderr에 함께 쓴다.
# [해설] 부모(클라이언트)의 `client/launch/server.py`가 서버 로그를 뒤에서부터 훑어 이 마커를 에러 요약으로 쓴다.
# [해설] 샌드박스 생성 실패처럼 sys.exit(1) 직전 경로에서 사용된다.
def _print_startup_error(message: str) -> None:
    """Print a startup error for both humans and the parent app process.

    Args:
        message: Concise startup failure to surface in the parent process.
    """
    print(message, file=sys.stderr)  # noqa: T201  # stderr fallback for logs
    print(  # noqa: T201  # machine-readable marker consumed by server.py
        f"{_STARTUP_ERROR_MARKER}{message}",
        file=sys.stderr,
    )


# [해설] MCP 세션 매니저(`mcp_tools.MCPSessionManager`)를 지연 생성하는 프로세스 싱글턴 접근자.
# [해설] `_build_tools`가 `resolve_and_load_mcp_tools(session_manager=...)`로 넘긴다.
def _get_mcp_session_manager() -> Any:  # noqa: ANN401
    """Return the process-wide MCP session manager singleton.

    Sessions are bound to the langgraph dev server's event loop. Cleanup
    therefore belongs to that loop's normal shutdown path, not `atexit` —
    an atexit handler runs after the loop is already closed and cannot
    await `AsyncExitStack.aclose()` safely. Subprocess handles held by
    stdio transports are released when the Python process exits.
    """
    global _mcp_session_manager  # noqa: PLW0603

    if _mcp_session_manager is None:
        from deepagents_code.mcp_tools import MCPSessionManager

        _mcp_session_manager = MCPSessionManager()

    return _mcp_session_manager


# [해설] 서버 쪽 도구 목록 조립: 기본 도구(fetch_url, get_current_thread_id, 선택적 web_search) + MCP 도구.
# [해설] 호출: `_make_graphs_in_environment`. 반환 4-튜플은 create_cli_agent 인자와 rubric/goal 도구 선별에 쓰인다.
# [해설] 관련 분석: analysis/07-mcp-hooks-extensions-plugins.md
async def _build_tools(
    config: ServerConfig,
    project_context: ProjectContext | None,
    *,
    tavily_api_key: str | None,
) -> tuple[list[Any], list[Any] | None, list[Any], list[Any]]:
    """Assemble the tool list based on server config.

    Loads built-in tools (conditionally including web search when Tavily is
    available) and MCP tools when enabled.

    MCP discovery is awaited on the server's event loop: LangGraph invokes this
    async factory on its running loop, so discovery must use `await` rather than
    `asyncio.run` (which raises inside a running loop). `stateless=True` ensures
    discovery only uses throwaway sessions, while the shared runtime session
    manager binds real sessions lazily inside the server loop on first tool
    invocation. MCP adapter imports are warmed in a worker thread inside
    `_load_tools_from_config` (only when active servers exist) because first
    import can perform blocking package-resource scans.

    Args:
        config: Deserialized server configuration.
        project_context: Resolved project context for MCP discovery.
        tavily_api_key: Workspace Tavily key, or `None` when the workspace
            configures none. An empty string still binds the tool, which then
            reports the key as unconfigured.

    Returns:
        Tuple of `(tools, mcp_server_info, mcp_tools, read_only_builtins)`. The
        last element is the exact built-in tool objects that are safe to expose
        to criteria drafting and rubric grading; read-only-ness is known here,
        at construction, so no consumer has to re-derive it.

    Raises:
        FileNotFoundError: If the MCP config file is not found.
        RuntimeError: If MCP tool loading fails.
    """
    from deepagents_code.tools import (
        create_web_search_tool,
        fetch_url,
        get_current_thread_id,
    )

    # [해설][흐름] 1) 내장 도구. read_only_builtins에는 부작용 없는 도구만 담는다(rubric 채점·criteria 작성용).
    tools: list[Any] = [fetch_url, get_current_thread_id]
    read_only_builtins: list[Any] = [fetch_url]
    # [해설] Tavily 키가 None이면 검색 도구를 아예 바인딩하지 않는다. 빈 문자열이면 바인딩은 하되 "미설정"을 보고.
    if tavily_api_key is not None:
        search_tool = create_web_search_tool(tavily_api_key)
        tools.append(search_tool)
        read_only_builtins.append(search_tool)

    # [해설][흐름] 2) MCP 도구. `--no-mcp`면 건너뛴다.
    mcp_server_info: list[Any] | None = None
    mcp_tools: list[Any] = []
    if not config.no_mcp:
        from deepagents_code.mcp_tools import resolve_and_load_mcp_tools
        from deepagents_code.plugins.adapters.mcp import discover_plugin_mcp_configs

        # [해설] 플러그인 MCP 설정 탐색 기준 디렉터리: 프로젝트 루트가 있으면 루트, 없으면 사용자 cwd.
        project_dir = (
            project_context.project_root or project_context.user_cwd
            if project_context is not None
            else None
        )
        # Offload plugin discovery: it does blocking disk IO (`os.mkdir` for
        # per-plugin data dirs, plus state/manifest reads) that `blockbuster`
        # rejects on the server event loop.
        plugin_mcp_configs = await asyncio.to_thread(
            discover_plugin_mcp_configs, project_dir=project_dir
        )
        # [해설][흐름] 3) 명시 설정(--mcp-config) + 프로젝트 MCP(신뢰된 경우) + 플러그인 설정을 합쳐 로드.
        # [해설] stateless=True: 탐색은 일회용 세션으로만 하고, 실제 세션은 첫 도구 호출 시 서버 루프에서 바인딩된다.
        try:
            mcp_tools, _, mcp_server_info = await resolve_and_load_mcp_tools(
                explicit_config_path=config.mcp_config_path,
                no_mcp=config.no_mcp,
                trust_project_mcp=config.trust_project_mcp,
                project_context=project_context,
                additional_configs=plugin_mcp_configs,
                stateless=True,
                session_manager=_get_mcp_session_manager(),
            )
        except FileNotFoundError:
            logger.exception("MCP config file not found: %s", config.mcp_config_path)
            raise
        except RuntimeError:
            logger.exception(
                "Failed to load MCP tools (config: %s)", config.mcp_config_path
            )
            raise

        tools.extend(mcp_tools)
        if mcp_tools:
            logger.info("Loaded %d MCP tool(s)", len(mcp_tools))

    return tools, mcp_server_info, mcp_tools, read_only_builtins


# [해설] 전체 도구 중 "읽기 전용"으로 확인된 것만 골라 goal criteria 작성·rubric 채점 서브 호출에 제공한다.
# [해설] 객체 identity(id)로 비교해 이름 충돌 도구가 섞이지 않게 하며, 원래 실행 순서를 유지한다.
# [해설] 관련 분석: analysis/05-subagents-goals-rubrics.md
def _criteria_context_tools(
    tools: list[Any],
    mcp_tools: list[Any],
    read_only_builtins: list[Any],
) -> list[Any]:
    """Select read-only external tools for criteria drafting and rubric grading.

    Args:
        tools: Main agent tools in execution order.
        mcp_tools: Exact tool objects returned by MCP discovery.
        read_only_builtins: Built-in tool objects `_build_tools` created and
            marked read-only.

    Returns:
        External context tools available to criteria generation and grading.
        MCP tools are included only when their protocol annotations explicitly
        declare them read-only.
    """
    allowed_ids = {id(tool) for tool in read_only_builtins}
    allowed_ids.update(
        id(tool) for tool in mcp_tools if _mcp_tool_is_explicitly_read_only(tool)
    )
    return [tool for tool in tools if id(tool) in allowed_ids]


# [해설] MCP 도구의 readOnlyHint가 명시적 True이고 destructive 힌트와 모순이 없을 때만 True(fail-closed).
# [해설] 실제 판정은 `auto_mode.mcp_tool_is_coherently_read_only`에 위임한다(auto 모드 분류기와 기준 공유).
def _mcp_tool_is_explicitly_read_only(tool: Any) -> bool:  # noqa: ANN401
    """Return whether a wrapped MCP tool is unambiguously read-only.

    MCP `ToolAnnotations.readOnlyHint` is serialized by the installed adapter
    into the LangChain tool's metadata as the camel-case `readOnlyHint` key.
    Require the literal boolean `True` and reject a contradictory destructive
    hint so absent, malformed, or ambiguous annotations fail closed.

    Returns:
        `True` only for an explicitly and consistently read-only MCP tool.
    """
    from deepagents_code.auto_mode import mcp_tool_is_coherently_read_only

    return mcp_tool_is_coherently_read_only(tool)


# [해설] 서버 런타임 번들. make_graph(그래프)와 offload_api(`/dcode/threads/{id}/offload`)가 같은 인스턴스를 공유해야
# [해설]   서버 측 아카이브를 에이전트가 읽을 수 있다. NamedTuple 필드 이름으로 위치 뒤바뀜을 방지.
class ServerRuntime(NamedTuple):
    """The one-per-process result with named slots to prevent transposition."""

    agent: Any
    """Compiled LangGraph agent graph served as `agent`."""

    backend: CompositeBackend
    """Composite backend the agent and its operations were built with."""

    offload: OffloadOperation
    """Server-owned thread offload operation bound to `backend`."""

    mcp_server_info: list[MCPServerInfo] | None = None
    """Workspace-scoped MCP metadata for the interactive client."""


# [해설] 런타임 1개를 빌드하는 최상위 빌더. 워크스페이스 environ 스냅샷을 만들고 트레이싱을 고정한 뒤
# [해설]   `_make_graphs_in_environment`를 그 environ 컨텍스트 안에서 실행한다.
# [해설] 호출: `_build_runtime_factory.get_runtime`(launch 워크스페이스), `_workspace_runtime`(바인딩별 오버라이드).
async def _make_graphs(
    *,
    config_override: ServerConfig | None = None,
    project_context_override: ProjectContext | None = None,
) -> ServerRuntime:
    """Create the agent graph and the backend carrying its shared resources.

    Reads `DEEPAGENTS_CODE_SERVER_*` env vars via `ServerConfig.from_env()`
    (the inverse of `ServerConfig.to_env()` used by the app process), resolves a
    model, assembles tools, and compiles the agent graph.

    Returns:
        The agent graph, its configured composite backend, and the server-owned
            offload operation bound to that backend.
    """
    # [해설][흐름] 1) 설정 결정: 오버라이드가 없으면 env(`DEEPAGENTS_CODE_SERVER_*`)에서 역직렬화.
    # [해설]   워크스페이스 경로는 project_context_override.user_cwd > config.cwd > None 순.
    config = config_override or ServerConfig.from_env()
    workspace_path = (
        project_context_override.user_cwd
        if project_context_override is not None
        else Path(config.cwd)
        if config.cwd is not None
        else None
    )

    # Offload the workspace environment snapshot off the event loop. Dotenv
    # discovery walks parent directories (`Path.resolve()`, `is_file()`) and
    # reads up to three files, and `snapshot_from_environment` adds
    # `find_project_root()` -> `Path.cwd()` — all of which `blockbuster`
    # rejects when invoked directly from the server loop (see issue #5043),
    # for the same reason as the offload in `_make_graphs_in_environment`.
    # [해설][흐름] 2) 워커 스레드에서 dotenv 미리보기 → 불변 environ(MappingProxyType) + 자격 증명 스냅샷 생성.
    # [해설][설계] os.environ을 직접 바꾸지 않고 워크스페이스별 environ을 만들어, 여러 워크스페이스 런타임이
    # [해설]   한 프로세스에서 서로의 .env 값을 오염시키지 않게 한다. 관련 분석: analysis/03-config-models-credentials.md
    def _resolve_workspace_environment() -> tuple[
        Mapping[str, str], CredentialsSnapshot, EnvironmentContext, bool
    ]:
        from deepagents_code.config import (
            Credentials,
            _ensure_bootstrap,
            _preview_dotenv_environ,
            is_langsmith_redaction_enabled,
            use_environment,
        )

        # Finish the one-time global credential publication before pinning
        # tracing. A later lazy import of `agent` must not overwrite the pin.
        _ensure_bootstrap()
        environ = MappingProxyType(_preview_dotenv_environ(start_path=workspace_path))
        with use_environment(environ):
            redact = is_langsmith_redaction_enabled()
        return (
            environ,
            Credentials.snapshot_from_environment(
                start_path=workspace_path,
                environ=environ,
            ),
            use_environment,
            redact,
        )

    (
        workspace_env,
        workspace_credentials,
        use_environment,
        redact,
    ) = await asyncio.to_thread(_resolve_workspace_environment)

    # [해설][흐름] 3) 워크스페이스 environ 활성화 → 트레이싱 고정(루프 위, suspend 없음) → 실제 빌드.
    with use_environment(workspace_env):
        _configure_server_tracing(workspace_env, redact=redact)
        return await _make_graphs_in_environment(
            config=config,
            project_context_override=project_context_override,
            workspace_env=workspace_env,
            workspace_credentials=workspace_credentials,
        )


# [해설] 워크스페이스 environ이 활성화된 상태에서 모델·도구·샌드박스·확장을 준비하고 `create_cli_agent`로 그래프를 컴파일한다.
# [해설] 반환: ServerRuntime(agent, backend, offload, mcp_server_info).
# [해설][SDK] 그래프 조립은 `agent.py:create_cli_agent` → SDK `deepagents/graph.py:create_deep_agent`.
# [해설][주의] checkpointer는 여기서 넘기지 않는다. 서버 모드에서는 langgraph.json의 checkpointer.path가 주입(추정: langgraph-api가 부착).
async def _make_graphs_in_environment(
    *,
    config: ServerConfig,
    project_context_override: ProjectContext | None,
    workspace_env: Mapping[str, str],
    workspace_credentials: CredentialsSnapshot,
) -> ServerRuntime:
    """Build one runtime while its immutable workspace environment is active.

    Returns:
        Agent graph and its workspace-bound resources.
    """

    # Offload cwd/path resolution and the lazy settings bootstrap off the event
    # loop. On Windows, `Path.resolve()` / `Path.cwd()` call `os.getcwd()`, which
    # `blockbuster` rejects when invoked directly from the server loop (see
    # issue #5043). Importing `deepagents_code.agent` / first `settings` access
    # can also trigger `find_project_root()` -> `Path.cwd()`.
    # [해설][흐름] 1) 프로젝트 컨텍스트 해석 + 무거운 모듈(agent/config) 지연 import를 워커 스레드에서 수행.
    def _resolve_project_context_and_settings() -> tuple[
        ProjectContext | None,
        Any,
        Any,
        Any,
        Any,
        Any,
    ]:
        project_context = project_context_override or get_server_project_context()

        from deepagents_code.agent import create_cli_agent, load_async_subagents
        from deepagents_code.config import (
            create_model,
            is_memory_auto_save_enabled,
            resolve_auto_classifier_model_for_provider,
        )

        return (
            project_context,
            create_cli_agent,
            load_async_subagents,
            create_model,
            is_memory_auto_save_enabled,
            resolve_auto_classifier_model_for_provider,
        )

    (
        project_context,
        create_cli_agent,
        load_async_subagents,
        create_model,
        is_memory_auto_save_enabled,
        resolve_auto_classifier_model_for_provider,
    ) = await asyncio.to_thread(_resolve_project_context_and_settings)
    # [해설][흐름] 2) 모델 생성(워커 스레드). apply_to_runtime_state로 선택된 모델 정보를 런타임 전역 상태에 반영.
    # Offload to a worker thread: `create_model` does blocking disk IO for some
    # providers (e.g. the `openai_codex` token store currently acquires a file
    # lock via `langchain-openai` that calls `os.mkdir`), which `blockbuster`
    # rejects on the server event loop.
    result = await asyncio.to_thread(
        create_model,
        config.model,
        extra_kwargs=config.model_params,
        profile_overrides=config.profile_overrides,
        cli_max_retries=config.cli_max_retries,
    )
    result.apply_to_runtime_state()

    # [해설][흐름] 3) 도구 조립 + 읽기 전용 컨텍스트 도구 선별.
    tools, mcp_server_info, mcp_tools, read_only_builtins = await _build_tools(
        config,
        project_context,
        tavily_api_key=workspace_credentials.tavily_api_key,
    )
    read_only_context_tools = _criteria_context_tools(
        tools, mcp_tools, read_only_builtins
    )

    # [해설][흐름] 4) 샌드박스(선택). config.sandbox_type이 있을 때만. 관련 분석: analysis/08-sandboxes-execution.md
    # Create sandbox backend if a sandbox provider is configured.
    # The context manager is created here in the factory, but its reference is
    # stored in a module-level global (and cleaned up via atexit) so the sandbox
    # lives for the entire server process lifetime. `make_graph` caches the built
    # graph, so this runs once per process despite LangGraph's per-run factory
    # invocation.
    global _sandbox_cm, _sandbox_backend  # noqa: PLW0603
    sandbox_backend = None
    if sandbox_type := config.sandbox_type:
        from deepagents_code.integrations.sandbox_factory import create_sandbox

        try:
            context, backend = await _open_sandbox(
                lambda: create_sandbox(
                    sandbox_type,
                    sandbox_id=config.sandbox_id,
                    snapshot_name=config.sandbox_snapshot_name,
                    setup_script_path=config.sandbox_setup,
                )
            )
            _sandbox_cm = context
            _sandbox_backend = backend
            sandbox_backend = backend
            atexit.register(_close_sandbox, context)
        # [해설][주의] 샌드박스 실패는 예외 전파가 아니라 마커 출력 후 즉시 프로세스 종료(sys.exit(1)).
        # [해설]   부모의 health/graph-ready 대기가 로그 마커로 원인을 표시한다.
        except ImportError:
            logger.exception(
                "Sandbox provider '%s' is not installed", config.sandbox_type
            )
            _print_startup_error(
                f"Sandbox provider '{config.sandbox_type}' is not installed"
            )
            sys.exit(1)
        except NotImplementedError:
            logger.exception("Sandbox type '%s' is not supported", config.sandbox_type)
            _print_startup_error(
                f"Sandbox type '{config.sandbox_type}' is not supported"
            )
            sys.exit(1)
        except ValueError as exc:
            logger.exception(
                "Invalid sandbox configuration for '%s'", config.sandbox_type
            )
            _print_startup_error(f"Invalid sandbox configuration: {exc}")
            sys.exit(1)
        except Exception as exc:
            logger.exception("Sandbox creation failed for '%s'", config.sandbox_type)
            _print_startup_error(
                f"Sandbox creation failed for '{config.sandbox_type}': {exc}"
            )
            sys.exit(1)

    # [해설] 확장 레지스트리는 아래 EXPERIMENTAL 분기에서 채워지고, 클로저 `_create_cli_graphs_sync`가 늦게 읽는다.
    extension_registry: ExtensionRegistry | None = None

    # [해설][흐름] 5) 그래프 컴파일 본체(동기, 워커 스레드에서 실행). create_cli_agent + offload 연산 추출.
    def _create_cli_graphs_sync() -> ServerRuntime:
        async_subagents = load_async_subagents() or None
        # [해설] auto 모드(분류기 기반 승인)는 인터랙티브이면서 샌드박스가 없을 때만 활성화된다.
        auto_mode_enabled = config.interactive and sandbox_backend is None

        # [해설] 인터프리터(PTC 포함) 설정은 enable_interpreter일 때만 config resolver에서 읽는다.
        interpreter_config = (
            InterpreterConfig.from_resolver(
                get_config_resolver(),
                ptc=config.interpreter_ptc,
                ptc_acknowledge_unsafe=config.interpreter_ptc_acknowledge_unsafe,
            )
            if config.enable_interpreter
            else None
        )

        agent, composite_backend = create_cli_agent(
            model=result.model,
            assistant_id=config.assistant_id,
            tools=tools,
            mcp_tools=mcp_tools,
            sandbox=sandbox_backend,
            sandbox_type=config.sandbox_type,
            system_prompt=config.system_prompt,
            interactive=config.interactive,
            auto_approve=config.auto_approve,
            auto_mode_enabled=auto_mode_enabled,
            interrupt_shell_only=config.interrupt_shell_only,
            shell_allow_list=config.shell_allow_list,
            fs_tools=config.allow_fs_tools,
            enable_ask_user=config.enable_ask_user,
            enable_memory=config.enable_memory,
            memory_auto_save=is_memory_auto_save_enabled(),
            enable_skills=config.enable_skills,
            enable_shell=config.enable_shell,
            enable_interpreter=config.enable_interpreter,
            interpreter_config=interpreter_config,
            rubric_model=config.rubric_model,
            rubric_max_iterations=config.rubric_max_iterations,
            # [해설] 자동 승인 분류기 모델은 메인 모델 provider에 맞춰 해석된다(관련: analysis/04-approval-hitl-security.md).
            auto_classifier_model=resolve_auto_classifier_model_for_provider(
                result.provider,
                config.auto_classifier_model,
            ),
            recursion_limit=config.recursion_limit,
            mcp_server_info=mcp_server_info,
            cwd=project_context.user_cwd if project_context is not None else config.cwd,
            project_context=project_context,
            async_subagents=async_subagents,
            goal_criteria_tools=read_only_context_tools,
            rubric_grader_tools=read_only_context_tools,
            model_retries=result.model_retries,
            cli_max_retries=result.cli_max_retries,
            summarization_model=config.summarization_model,
            extension_registry=extension_registry,
            environ=workspace_env,
            credentials_snapshot=workspace_credentials,
            model_result=result,
        )
        # [해설] offload 연산은 백엔드가 "게시"한 것을 꺼내 쓴다. 없으면 /offload HTTP 라우트가 동작할 수 없어 빌드 실패로 처리.
        from deepagents_code.offload_middleware import offload_operation_from

        offload = offload_operation_from(composite_backend)
        if offload is None:
            msg = (
                "Agent backend did not publish its offload operation; "
                "/offload has no server implementation."
            )
            raise RuntimeError(msg)
        return ServerRuntime(
            agent=agent,
            backend=composite_backend,
            offload=offload,
            mcp_server_info=mcp_server_info,
        )

    # [해설][흐름] 6) DEEPAGENTS_CODE_EXPERIMENTAL이 켜진(워크스페이스 environ 기준) 경우에만 확장 로드.
    # [해설]   프로젝트 확장은 trust_project_extensions가 참일 때만, CLI 경로(--extension)는 extension_paths로 전달.
    from deepagents_code._env_vars import EXPERIMENTAL, is_env_truthy

    if is_env_truthy(EXPERIMENTAL, environ=workspace_env):
        from deepagents_code.extensions import ExtensionMode, load_extensions
        from deepagents_code.extensions.runtime import bind_server_extensions

        extension_result = await load_extensions(
            cwd=(
                project_context.user_cwd
                if project_context is not None
                else Path(config.cwd)
                if config.cwd is not None
                else None
            ),
            mode=(
                ExtensionMode.INTERACTIVE
                if config.interactive
                else ExtensionMode.HEADLESS
            ),
            project_root=(
                project_context.project_root or project_context.user_cwd
                if project_context is not None
                else None
            ),
            project_trust_granted=config.trust_project_extensions,
            cli_paths=tuple(Path(path) for path in config.extension_paths),
        )
        for message in extension_result.errors:
            logger.warning("Extension not loaded: %s", message)
        if extension_result.active:
            extension_registry = extension_result.registry
            bind_server_extensions(extension_result)
    # [해설][흐름] 7) 컴파일 실행. 실패(취소 포함 BaseException) 시 이미 바인딩된 서버 확장을 정리하고 재전파.
    try:
        return await asyncio.to_thread(_create_cli_graphs_sync)
    except BaseException:
        if extension_registry is not None:
            from deepagents_code.extensions.runtime import shutdown_server_extensions

            await shutdown_server_extensions()
        raise


# [해설] 프로세스당 한 번만 런타임을 빌드하는 캐시 팩토리(더블 체크 락). 모듈 하단 `_get_runtime`이 유일한 운영 인스턴스.
# [해설][설계] 캐시는 정확성의 전제: MCP 재탐색·샌드박스 누수·atexit 중복을 막는다.
# [해설][주의] 빌드 예외는 `emit_startup_failure`(마커 출력) 후 sys.exit(1) — "startup barrier".
def _build_runtime_factory(
    builder: Callable[[], Awaitable[ServerRuntime]] | None = None,
) -> Callable[[], Awaitable[ServerRuntime]]:
    """Build the cached factory for all server-owned runtime resources.

    The cache is load-bearing, not an optimization: MCP discovery, sandbox
    creation, and `atexit` registration each must happen exactly once. Building
    per request would re-discover MCP servers, leak sandbox sessions, and stack
    duplicate `atexit` handlers. Two consumers now share this cache -- the
    interactive graph and the offload HTTP route -- so both must resolve the
    *same* agent, backend, and compaction policy for a server-side archive to be
    readable by the agent.

    The cache and its lock live in this closure rather than in module-level
    globals, so importing this module introduces no shared mutable state; the
    single process-wide instance is created explicitly at the bottom of the
    module.

    Args:
        builder: Optional alternate builder used by unit tests.

    Returns:
        Async runtime factory shared by the graph and custom operation API.
    """
    runtime: ServerRuntime | None = None
    lock = asyncio.Lock()

    async def get_runtime() -> ServerRuntime:
        """Return the cached interactive graph and operation resources."""
        nonlocal runtime
        if runtime is None:
            async with lock:
                if runtime is None:
                    # [해설] managed config(조직 정책 파일)가 손상됐으면 빌드 전에 실패시킨다(refresh=True로 최신 상태 재확인).
                    try:
                        from deepagents_code.configuration.service import (
                            require_healthy_managed_config,
                        )

                        await asyncio.to_thread(
                            require_healthy_managed_config,
                            refresh=True,
                        )
                        runtime = await (builder or _make_graphs)()
                    except Exception as exc:  # noqa: BLE001  # startup barrier
                        emit_startup_failure(exc)
                        sys.exit(1)
        return runtime

    return get_runtime


# [해설] 테스트 전용 그래프 팩토리 빌더. 운영 경로는 모듈 레벨 `make_graph`를 사용한다.
def _build_graph_factory(
    builder: Callable[[], Awaitable[ServerRuntime]] | None = None,
) -> Callable[[], Awaitable[Any]]:
    """Build a cached graph factory, for tests.

    `langgraph.json` references the module-level `make_graph`, which delegates to
    `get_server_runtime`; nothing in production calls this. It survives so unit
    tests can inject a builder.

    Args:
        builder: Optional alternate runtime builder used by unit tests.

    Returns:
        Async graph factory for the interactive `agent` graph.
    """
    get_runtime = _build_runtime_factory(builder)

    async def make_graph() -> Any:  # noqa: ANN401
        """Create or return the cached agent graph for `langgraph dev`.

        Returns:
            Compiled LangGraph agent graph.
        """
        return (await get_runtime()).agent

    return make_graph


# [해설] 운영용 전역 캐시들.
# [해설] _get_runtime: launch 워크스페이스 기본 런타임(1개). _MAX_WORKSPACE_RUNTIMES=32: 바인딩별 런타임 LRU 상한.
# [해설] _workspace_runtimes: resource_key → ServerRuntime (OrderedDict로 LRU 구현).
# [해설] _sandbox_workspace_id: 프로세스 단일 샌드박스를 소유한 workspace_id.
# [해설][주의] LRU에서 축출돼도 해당 런타임의 MCP/샌드박스 자원을 명시적으로 닫는 코드는 여기 없다(추정: 프로세스 종료 시 정리).
_get_runtime = _build_runtime_factory()
_MAX_WORKSPACE_RUNTIMES = 32
_workspace_runtimes: OrderedDict[str, ServerRuntime] = OrderedDict()
_workspace_runtime_lock = asyncio.Lock()
_sandbox_workspace_id: str | None = None


# [해설] resource_key로 캐시 조회, 적중 시 LRU 최신으로 이동.
def _cached_workspace_runtime(binding: WorkspaceBinding) -> ServerRuntime | None:
    """Return and refresh a cached runtime for one workspace binding."""
    cached = _workspace_runtimes.get(binding.resource_key)
    if cached is None:
        return None
    _workspace_runtimes.move_to_end(binding.resource_key)
    return cached


# [해설] 샌드박스가 설정된 경우, 처음 요청한 워크스페이스에 샌드박스 소유권을 부여한다.
# [해설] 다른 워크스페이스가 요청하면 WorkspaceConflictError(offload_api 라우트에서는 HTTP 409로 매핑).
def _claim_sandbox_workspace(
    sandbox_type: str | None,
    binding: WorkspaceBinding,
) -> None:
    """Reserve the process-wide sandbox for the first requesting workspace."""
    global _sandbox_workspace_id  # noqa: PLW0603  # process-lifetime ownership
    if not sandbox_type:
        return
    if _sandbox_workspace_id is None:
        _sandbox_workspace_id = binding.workspace_id
        return
    if _sandbox_workspace_id == binding.workspace_id:
        return
    reason = (
        "a runtime for another workspace already exists and the configured "
        "sandbox is process-wide"
    )
    # Built into a local first: `raise X.from_reason(...)` reads as a
    # `from_reason` raise to ruff's DOC501.
    conflict = WorkspaceConflictError.from_reason(reason)
    raise conflict


# [해설] 런타임을 LRU에 넣고 상한(32) 초과 시 가장 오래된 항목을 제거한다.
def _remember_workspace_runtime(
    binding: WorkspaceBinding,
    runtime: ServerRuntime,
) -> None:
    """Cache one workspace runtime and enforce the bounded LRU size."""
    _workspace_runtimes[binding.resource_key] = runtime
    if len(_workspace_runtimes) > _MAX_WORKSPACE_RUNTIMES:
        _workspace_runtimes.popitem(last=False)


# [해설] 서버 기동 시 설정된 cwd(=launch 워크스페이스)로부터 정규화된 WorkspaceBinding을 계산한다(DB 기록 없음).
# [해설] 호출: `get_server_runtime`. 결과의 resource_key로 기본 런타임을 LRU에 등록한다.
async def _default_workspace_binding(config: ServerConfig) -> WorkspaceBinding | None:
    """Resolve the launch workspace represented by the server configuration.

    Returns:
        The canonical launch binding, or `None` without a configured workspace.
    """
    if config.cwd is None:
        return None

    def _bind() -> WorkspaceBinding:
        # First pass resolves identity only (cwd plus project root); its
        # fingerprints are digests of an empty policy and are discarded.
        identity = resolve_workspace(config.cwd)
        # The shared policy resolver honors the explicit launch root while
        # keeping the durable identity consistent with workspace validation.
        resolved = config.resolve_workspace(identity.cwd, identity.project_root)
        return resolve_workspace(
            identity.cwd,
            resolved.to_workspace_payload(),
            config_fingerprint=resolved.workspace_fingerprint(),
        )

    return await asyncio.to_thread(_bind)


# [해설] 매 run 요청마다 현재 정책을 다시 계산해 바인딩 시점 정책과 비교(drift 검사)한다.
# [해설] 1) 프로젝트 정책 필드(MCP 설정·sandbox setup·확장 신뢰 등) 변화 → 필드명을 담아 거부
# [해설] 2) 그 외 서버 설정 fingerprint 변화 → SERVER_CONFIG_DRIFT_REASON으로 거부.
# [해설] 호출: `_workspace_runtime`(워커 스레드). 관련: `workspace.py:drifted_project_fields`.
def _resolve_bound_workspace_config(binding: WorkspaceBinding) -> ServerConfig:
    """Resolve current workspace policy and reject drift from its binding.

    Refusals name the fields that drifted. This runs on every request, and it
    reads the extension trust store each time, so a transient read failure
    reports as a policy change; without the field names that refusal is not
    diagnosable. The values are paths and booleans, never secrets.

    Returns:
        The current server configuration resolved for the workspace.
    """
    config = ServerConfig.from_env()
    current_config = config.resolve_workspace(binding.cwd, binding.project_root)
    bound_policy = binding.workspace_config()
    # [해설] 확장 신뢰는 "추가는 새 스레드에만, 철회는 즉시" 규칙을 `preserve_bound_extension_trust`가 적용한다.
    current_config = current_config.preserve_bound_extension_trust(bound_policy)
    drifted = drifted_project_fields(
        bound_policy, current_config.to_project_workspace_policy()
    )
    if drifted:
        fields = ", ".join(drifted)
        logger.warning(
            "Workspace %s project policy drifted since binding: %s",
            binding.cwd,
            fields,
        )
        conflict = WorkspaceConflictError.from_reason(
            f"{PROJECT_POLICY_DRIFT_REASON} ({fields})"
        )
        raise conflict
    if current_config.workspace_fingerprint() != binding.config_fingerprint:
        logger.warning(
            "Workspace %s server config fingerprint changed since binding",
            binding.cwd,
        )
        conflict = WorkspaceConflictError.from_reason(SERVER_CONFIG_DRIFT_REASON)
        raise conflict
    return current_config


# [해설] 바인딩(스레드↔워크스페이스)에 맞는 런타임을 LRU에서 찾거나 새로 빌드한다.
# [해설] 호출: `make_graph`(execution 경로), 그리고 `offload_api.py`가 이 함수를 `get_server_runtime`이라는 별칭으로 import해
# [해설]   워크스페이스 바인딩/offload 라우트에서 사용한다(WorkspaceConflictError → HTTP 409).
async def _workspace_runtime(binding: WorkspaceBinding) -> ServerRuntime:
    """Build or reuse a runtime from the persisted workspace resource policy.

    Returns:
        The runtime selected by the binding's immutable resource key.
    """
    # [해설][흐름] 1) drift 검사(캐시 적중이어도 매번 수행 — 정책 변경을 즉시 거부하기 위해).
    current_config = await asyncio.to_thread(_resolve_bound_workspace_config, binding)
    cached = _cached_workspace_runtime(binding)
    if cached is not None:
        return cached
    # [해설][흐름] 2) 락 안에서 재확인 → 샌드박스 소유권 확인 → 바인딩 경로로 ProjectContext 구성 → 빌드 → LRU 등록.
    async with _workspace_runtime_lock:
        cached = _cached_workspace_runtime(binding)
        if cached is not None:
            return cached
        _claim_sandbox_workspace(current_config.sandbox_type, binding)
        project_context = ProjectContext(
            user_cwd=Path(binding.cwd),
            project_root=(
                Path(current_config.project_root)
                if current_config.project_root
                else None
            ),
        )
        runtime = await _make_graphs(
            config_override=current_config,
            project_context_override=project_context,
        )
        _remember_workspace_runtime(binding, runtime)
        return runtime


# [해설] launch 워크스페이스의 런타임을 반환한다. graph-ready 확인(`GET /assistants/agent/graph`)처럼
# [해설]   execution runtime이 없는 호출에서 사용된다.
# [해설][주의] 이름이 같지만 `offload_api.py`의 `get_server_runtime`은 이 함수가 아니라 `_workspace_runtime`의 별칭이다
# [해설]   (docstring의 offload_api 언급은 현재 import와 어긋남).
async def get_server_runtime() -> ServerRuntime:
    """Return resources shared by the graph and dcode operation routes.

    Builds once and caches. A construction failure is converted into a
    startup-error marker (scraped by the parent app process) before
    `sys.exit(1)`, which is right for the `langgraph.json` graph factory at
    startup. Callers in request scope must contain that exit -- `SystemExit` is a
    `BaseException` -- as `offload_api._execute_offload` does, mapping it to a 503
    rather than killing the server mid-request.

    Returns:
        The cached server runtime.
    """
    # Resolving the launch binding touches the filesystem and can raise, and
    # claiming the sandbox can refuse. Both run before `_get_runtime`, so they
    # sit outside its startup barrier and would exit without the marker the
    # parent app process scrapes. Emit it here instead.
    try:
        config = ServerConfig.from_env()
        binding = await _default_workspace_binding(config)
    except Exception as exc:  # noqa: BLE001  # startup barrier
        emit_startup_failure(exc)
        sys.exit(1)
    # [해설] cwd 미설정이면 바인딩 없이 기본 런타임만. 있으면 같은 런타임을 launch 바인딩의 resource_key로도 캐시해
    # [해설]   이후 같은 워크스페이스의 run 요청(`_workspace_runtime`)이 재빌드 없이 공유하게 한다.
    # [해설][주의] `_get_runtime`의 내부 lock과 `_workspace_runtime_lock`이 중첩되지만 획득 순서가 항상 같아 교착은 없다(추정).
    async with _workspace_runtime_lock:
        if binding is None:
            return await _get_runtime()
        cached = _cached_workspace_runtime(binding)
        if cached is not None:
            return cached
        _claim_sandbox_workspace(config.sandbox_type, binding)
        runtime = await _get_runtime()
        _remember_workspace_runtime(binding, runtime)
        return runtime


# [해설] langgraph.json `graphs.agent`가 가리키는 그래프 팩토리. LangGraph 서버가 요청마다 호출한다.
# [해설] - execution runtime이 있으면(실제 run): context.workspace 페이로드 + thread_id를 요구하고,
# [해설]   `workspace.require_thread_workspace`로 DB 바인딩과 대조한 뒤 해당 워크스페이스 런타임의 그래프를 반환.
# [해설] - 없으면(그래프 조회/스키마 요청 등): launch 워크스페이스 런타임 그래프를 반환.
# [해설] 클라이언트 쪽 페이로드 주입: `client/remote_client.py:RemoteAgent`(astream의 context={"workspace": ...}).
async def make_graph(
    config: dict[str, Any] | None = None,
    runtime: LangGraphServerRuntime[CLIContextSchema] | None = None,
) -> Any:  # noqa: ANN401
    """Return the graph after validating execution workspace context.

    Raises:
        ValueError: If execution context is missing or malformed.
    """
    execution = runtime.execution_runtime if runtime is not None else None
    if execution is not None:
        # [해설] CLIContextSchema.from_payload는 형식이 틀리면 None → 아래에서 ValueError로 거절.
        context = CLIContextSchema.from_payload(execution.context)
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        if context is None or not isinstance(thread_id, str) or not thread_id:
            msg = "A thread id and workspace context are required for execution."
            raise ValueError(msg)
        from deepagents_code.workspace import require_thread_workspace

        binding = await require_thread_workspace(thread_id, context.workspace)
        return (await _workspace_runtime(binding)).agent
    return (await get_server_runtime()).agent
