"""Server lifecycle orchestration for the app.

Provides `start_server_and_get_agent` which handles the full flow of:

1. Building a `ServerConfig` from application arguments
2. Writing config to env vars via `ServerConfig.to_env()`
3. Scaffolding a workspace (langgraph.json, checkpointer, pyproject)
4. Starting the `langgraph dev` server
5. Returning a `RemoteAgent` client

Also provides `server_session`, an async context manager that wraps
server startup and guaranteed cleanup so callers don't need to
duplicate try/finally teardown.
"""
# [해설] ── 모듈 개요 ─────────────────────────────────────────────
# [해설] 역할: 클라이언트 부팅의 핵심 오케스트레이터. CLI 인자 → ServerConfig → env 기록 → 임시 작업 디렉터리 스캐폴딩
# [해설]   (langgraph.json·checkpointer.py·pyproject.toml) → 서버 기동·그래프 준비 확인 → `RemoteAgent` 반환.
# [해설] 실행 위치: **클라이언트 프로세스**.
# [해설] 주요 진입점: `start_server_and_get_agent`(TUI `app.py:_start_server_background`, cwd 전환에서 직접 호출),
# [해설]   `server_session`(헤드리스 `client/non_interactive.py:run_non_interactive`가 사용하는 async context manager).
# [해설] 협력 모듈: `_server_config.py`(스키마), `client/launch/server.py`(ServerProcess), `client/remote_client.py`(RemoteAgent),
# [해설]   서버 측 `server_graph.py`(그래프 팩토리)·`workspace.py`(바인딩).
# [해설] 관련 분석: analysis/01-boot-client-server.md (흐름 D, 설계 포인트 1·2)
# [해설] 관련 문서: libs/code/ARCHITECTURE.md "The big picture"/"Request flow", docs_official/sdk/going-to-production.md(체크포인터 영속화).

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from deepagents import FsToolName

    from deepagents_code.client.launch.server import ServerProcess
    from deepagents_code.client.remote_client import RemoteAgent
    from deepagents_code.mcp_tools import MCPSessionManager

from deepagents_code._env_vars import SERVER_ENV_PREFIX
from deepagents_code._server_config import ServerConfig
from deepagents_code.client.launch.server import (
    _EPHEMERAL_PORT,
    emit_preserved_log_notices,
)
from deepagents_code.project_utils import ProjectContext

logger = logging.getLogger(__name__)
# [해설] 생성 pyproject 의존성 스펙에 쓰는 배포 이름.
_DISTRIBUTION_NAME = "deepagents-code"


# [해설] `DEEPAGENTS_CODE_SERVER_<name>`을 설정하거나(None이면) 삭제한다. None=삭제 규칙은 `ServerConfig.to_env` 문서와 짝.
def _set_or_clear_server_env(name: str, value: str | None) -> None:
    """Set or clear a `DEEPAGENTS_CODE_SERVER_*` environment variable.

    Args:
        name: Suffix after `DEEPAGENTS_CODE_SERVER_`.
        value: String value to set, or `None` to clear the variable.
    """
    key = f"{SERVER_ENV_PREFIX}{name}"
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


# [해설] ServerConfig 전체를 현재 프로세스 os.environ에 반영한다. 이후 `_build_server_env`가 os.environ을 복사하므로 서버에 전달된다.
# [해설][주의] 클라이언트 프로세스의 전역 env를 직접 바꾸는 부작용이 있다(서버 재시작/cwd 전환 시 덮어씀).
def _apply_server_config(config: ServerConfig) -> None:
    """Write a `ServerConfig` to `DEEPAGENTS_CODE_SERVER_*` env vars.

    Uses `ServerConfig.to_env()` so that the set of variables and their
    serialization format are defined in one place (the `ServerConfig` dataclass)
    rather than maintained independently here and in the
    reader (`ServerConfig.from_env()`).

    Args:
        config: Fully resolved server configuration.
    """
    for suffix, value in config.to_env().items():
        _set_or_clear_server_env(suffix, value)


# [해설] cwd 인자가 없을 때 현재 디렉터리 기준 ProjectContext를 만든다. cwd가 삭제된 경우 등 OSError면 None.
def _capture_project_context() -> ProjectContext | None:
    """Capture the user's project context for the server subprocess.

    Returns:
        Explicit project context, or `None` when cwd cannot be determined.
    """
    try:
        return ProjectContext.from_user_cwd(Path.cwd())
    except OSError:
        logger.warning("Could not determine working directory for server")
        return None


# ------------------------------------------------------------------
# Workspace scaffolding
# ------------------------------------------------------------------


# [해설] 서버 작업 디렉터리(서버 cwd)에 부팅 파일 3종을 생성. 최초 기동과 `ServerProcess._spawn_process`의 재스캐폴딩에서 호출.
def _scaffold_workspace(work_dir: Path) -> None:
    """Prepare the server working directory with all required files.

    Generates the auxiliary files (checkpointer module, `pyproject.toml`,
    `langgraph.json`) that `langgraph dev` needs to boot. The generated
    graph reference imports the installed `deepagents_code` package directly.

    Args:
        work_dir: Temporary directory that will become the server's cwd.
    """
    from deepagents_code.client.launch.server import generate_langgraph_json

    _write_checkpointer(work_dir)
    _write_pyproject(work_dir)

    # `graph_ref` is a dotted import of the installed `deepagents_code` package,
    # but `checkpointer_path` stays cwd-relative: checkpointer.py is generated
    # fresh into work_dir (which `ServerProcess.start()` sets as the subprocess
    # cwd) and is not an importable package module. Don't "unify" these — a
    # dotted ref for the checkpointer would fail to resolve.
    generate_langgraph_json(
        work_dir,
        graph_ref="deepagents_code.server_graph:make_graph",
        checkpointer_path="./checkpointer.py:create_checkpointer",
    )


# [해설] SQLite 체크포인터 모듈 `checkpointer.py` 생성. langgraph.json `checkpointer.path`가 이 파일의 `create_checkpointer`를 가리킨다.
# [해설][설계] DB 경로를 소스에 박지 않고 `DEEPAGENTS_CODE_SERVER_DB_PATH` env로 넘긴다(경로는 `sessions.get_db_path` = 프로필 state의 sessions.db).
# [해설]   같은 env를 서버 측 `workspace._database_path`도 읽어 바인딩 테이블을 같은 DB에 둔다.
# [해설][SDK] 저장 구현은 SDK가 아니라 `langgraph-checkpoint-sqlite`의 AsyncSqliteSaver.
def _write_checkpointer(work_dir: Path) -> None:
    """Write a checkpointer module that reads its DB path from the environment.

    The generated module reads the DB path env var at runtime so the path
    is never baked into generated source. This is consistent with the
    `DEEPAGENTS_CODE_SERVER_*` env-var communication pattern used elsewhere.

    Args:
        work_dir: Server working directory.
    """
    from deepagents_code.sessions import get_db_path

    # Set the env var that the generated module will read at import time.
    os.environ[f"{SERVER_ENV_PREFIX}DB_PATH"] = str(get_db_path())

    # [해설] 아래 content는 생성될 파일의 소스 문자열(f-string)이므로 그 내부에는 주석을 달지 않는다.
    db_path_var = f"{SERVER_ENV_PREFIX}DB_PATH"
    content = f'''\
"""Persistent SQLite checkpointer for the LangGraph dev server."""

import os
from contextlib import asynccontextmanager


@asynccontextmanager
async def create_checkpointer():
    """Yield an AsyncSqliteSaver connected to the app's sessions DB.

    The database path is read from the `{db_path_var}` env var
    (set by the app before server startup) rather than hard-coded, so
    the checkpointer module works without code generation.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_path = os.environ.get("{db_path_var}")
    if not db_path:
        raise RuntimeError(
            "{db_path_var} not set. The app must set this "
            "env var before server startup."
        )
    async with AsyncSqliteSaver.from_conn_string(db_path) as saver:
        yield saver
'''
    (work_dir / "checkpointer.py").write_text(content)


# [해설] 서버 작업 디렉터리의 최소 pyproject.toml. langgraph.json의 dependencies ["."]가 이 프로젝트를 가리킨다.
def _write_pyproject(work_dir: Path) -> None:
    """Write a minimal pyproject.toml for the server working directory.

    The `langgraph dev` server needs to install the project dependencies.
    We point it at the app package which transitively pulls in the SDK.

    Args:
        work_dir: Server working directory.
    """
    content = f"""[project]
name = "deepagents-server-runtime"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
    "{_runtime_package_dependency()}",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    (work_dir / "pyproject.toml").write_text(content)


# [해설] `deepagents_code` 패키지의 상위 디렉터리(소스 체크아웃이면 프로젝트 루트, 설치본이면 site-packages)를 구한다.
def _default_package_project_root() -> Path | None:
    """Return the project root that contains the top-level package.

    Returns:
        The directory above `deepagents_code` — the editable project root in
            source checkouts and `site-packages` for installed wheels — or
            `None` when the package location cannot be determined (e.g. a frozen
            or zipimport build where `__file__` is unset). Returning `None`
            (rather than guessing `Path.cwd()`) lets the caller fall back to
            the installed distribution version instead of pointing at whatever
            unrelated project happens to sit in the launch directory.
    """
    import deepagents_code

    # `getattr` with a default: `__file__` is unset on frozen/zipimport builds
    # and namespace packages, so it is not guaranteed to exist at runtime.
    package_init = getattr(deepagents_code, "__file__", None)
    if package_init is None:
        return None
    return Path(package_init).resolve().parent.parent


# [해설] 생성 pyproject의 의존성 문자열 결정: 부모에 pyproject.toml이 있으면(editable 소스) `file://` 경로 의존성,
# [해설]   아니면 설치된 배포 버전 고정(`==버전`), 그마저 없으면 이름만.
def _runtime_package_dependency(package_root: Path | None = None) -> str:
    """Return the dependency spec for the app package in the server runtime.

    Editable source checkouts can use a local path dependency so the generated
    runtime sees the working tree. Installed wheels cannot: the package parent is
    `site-packages`, which is not an installable project. In that case, depend on
    the installed distribution version instead.

    Args:
        package_root: Optional package project root for tests.

    Returns:
        Requirement string for the generated runtime `pyproject.toml`.
    """
    root = package_root or _default_package_project_root()
    if root is not None and (root / "pyproject.toml").is_file():
        return f"{_DISTRIBUTION_NAME} @ {root.as_uri()}"

    try:
        installed_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _DISTRIBUTION_NAME
    return f"{_DISTRIBUTION_NAME}=={installed_version}"


# ------------------------------------------------------------------
# MCP pre-flight validation
# ------------------------------------------------------------------


# [해설] `--mcp-config`로 명시한 파일만 부모 프로세스에서 미리 검증한다. 서버 안에서 실패하면 잘린 로그 덤프로만 보이기 때문.
# [해설] 호출: `start_server_and_get_agent`(서버 스폰 전). 관련 분석: analysis/07-mcp-hooks-extensions-plugins.md
def _preflight_validate_mcp_config(
    *,
    mcp_config_path: str | None,
    no_mcp: bool,
) -> None:
    """Validate the explicit `--mcp-config` path before spawning the server.

    Catches the common failure mode of passing a malformed MCP config: a
    `ValueError` raised inside the server subprocess otherwise surfaces as an
    opaque truncated log dump in `wait_for_server_healthy`. Running the same
    validation in the parent process lets the TUI display a clean, actionable
    message with the offending path and reason.

    Project-level and user-level configs discovered by `resolve_and_load_mcp_tools`
    are not validated here; their errors are already handled leniently via
    `load_mcp_config_with_error` and surface as errored entries in the
    `/mcp` viewer rather than as a fatal startup failure.

    Args:
        mcp_config_path: Explicit path passed via `--mcp-config`, or `None`.
        no_mcp: When `True`, MCP is disabled and validation is skipped.

    Raises:
        MCPConfigError: If the config file is malformed or missing required
            fields. Message includes the offending path for context.
    """
    if no_mcp or not mcp_config_path:
        return

    from deepagents_code.mcp_tools import MCPConfigError, load_mcp_config

    try:
        load_mcp_config(mcp_config_path)
    except MCPConfigError:
        raise
    except FileNotFoundError as exc:
        msg = f"MCP config file not found: {mcp_config_path}"
        raise MCPConfigError(msg) from exc
    except (ValueError, TypeError) as exc:
        # `ValueError` covers `json.JSONDecodeError` (subclass) and the
        # shape/field validators in `_validate_server_config`; `TypeError`
        # covers the wrong-type branches. Bare `RuntimeError` is
        # deliberately NOT caught — it would mask unrelated bugs
        # (recursion, reentrancy, stdlib internals) as config errors.
        msg = f"Invalid MCP config at {mcp_config_path}: {exc}"
        raise MCPConfigError(msg) from exc


# ------------------------------------------------------------------
# Server startup
# ------------------------------------------------------------------


# [해설] 서버를 띄우고 연결된 RemoteAgent를 돌려주는 클라이언트 부팅 핵심 함수.
# [해설] 호출: `app.py`(TUI 백그라운드 워커·cwd 전환), `server_session`(헤드리스).
# [해설] 반환 3번째 원소(MCP 세션 매니저)는 현재 항상 None — MCP 수명은 서버 측 `server_graph._get_mcp_session_manager`가 관리.
# [해설] host 기본 127.0.0.1(루프백), port 기본 0(ephemeral).
async def start_server_and_get_agent(
    *,
    assistant_id: str,
    model_name: str | None = None,
    summarization_model: str | None = None,
    model_params: dict[str, Any] | None = None,
    cli_max_retries: int | None = None,
    profile_overrides: dict[str, Any] | None = None,
    auto_approve: bool = False,
    interrupt_shell_only: bool = False,
    shell_allow_list: list[str] | None = None,
    sandbox_type: str = "none",
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
    enable_shell: bool = True,
    enable_ask_user: bool = False,
    enable_interpreter: bool | None = None,
    interpreter_ptc: str | list[str] | None = None,
    interpreter_ptc_acknowledge_unsafe: bool = False,
    allow_fs_tools: list[FsToolName] | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    auto_classifier_model: str | None = None,
    recursion_limit: int | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    trust_project_extensions: bool = False,
    extension_paths: tuple[str, ...] = (),
    interactive: bool = True,
    host: str = "127.0.0.1",
    port: int = _EPHEMERAL_PORT,
    cwd: str | None = None,
) -> tuple[RemoteAgent, ServerProcess, MCPSessionManager | None]:
    """Start a LangGraph server and return a connected remote agent client.

    Args:
        assistant_id: Agent identifier.
        model_name: Model spec string.
        summarization_model: Model spec used only for context-compaction summaries.
        model_params: Extra model kwargs.
        cli_max_retries: Explicit `--max-retries` value.
        profile_overrides: Model profile metadata overrides.
        auto_approve: Auto-approve all tools.
        interrupt_shell_only: Validate shell commands via middleware instead of HITL.
        shell_allow_list: Restrictive shell allow-list for `ShellAllowListMiddleware`.
        sandbox_type: Sandbox type.
        sandbox_id: Existing sandbox ID to reuse.
        sandbox_snapshot_name: Snapshot (langsmith) or blueprint (runloop) name.
        sandbox_setup: Path to setup script for the sandbox.
        enable_shell: Enable shell execution tools.
        enable_ask_user: Enable ask_user tool.
        enable_interpreter: Enable the JS interpreter (`js_eval`) middleware on
            the main agent. `None` uses the sandbox-aware default.
        interpreter_ptc: Invocation-scoped PTC allowlist override.
        interpreter_ptc_acknowledge_unsafe: Explicit acknowledgement for
            `interpreter_ptc="all"` outside of `auto_approve`.
        allow_fs_tools: Allowlist for `FilesystemMiddleware`'s `tools` param.

            `None` leaves the SDK default (all tools).
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
        trust_project_extensions: Allow project extension execution.
        extension_paths: Explicit one-run extension files or directories.
        interactive: Whether the agent is interactive.
        host: Server host.
        port: Server port. Defaults to `_EPHEMERAL_PORT` (0), letting the server
            pick a free ephemeral port instead of the well-known `langgraph dev`
            port 2024.
        cwd: Explicit project workspace to bind to new threads.

    Returns:
        Tuple of `(remote_agent, server_process, mcp_session_manager)`.
            The `mcp_session_manager` is currently always `None` (MCP lifecycle
            is handled server-side).

    Raises:
        MCPConfigError: The explicit `--mcp-config` path is malformed,
            missing, or references contradictory transport fields. Raised
            from the pre-flight validator before any subprocess is spawned.
        RuntimeError: If no explicit workspace can be resolved.
    """  # noqa: DOC502 - `_preflight_validate_mcp_config()` raises indirectly
    # [해설][흐름] 1) 무거운 모듈 지연 import → ProjectContext 결정(명시 cwd 우선).
    from deepagents_code.client.launch.server import ServerProcess
    from deepagents_code.client.remote_client import RemoteAgent

    project_context = (
        ProjectContext.from_user_cwd(Path(cwd))
        if cwd is not None
        else _capture_project_context()
    )

    # [해설][흐름] 2) MCP 설정 사전 검증(부모 프로세스, MCPConfigError로 깔끔한 메시지).
    _preflight_validate_mcp_config(
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
    )

    # [해설][흐름] 3) CLI 인자 → ServerConfig(경로 절대화·인터프리터 확정·불변식 검증) → os.environ에 기록.
    config = ServerConfig.from_cli_args(
        project_context=project_context,
        model_name=model_name,
        summarization_model=summarization_model,
        model_params=model_params,
        cli_max_retries=cli_max_retries,
        profile_overrides=profile_overrides,
        assistant_id=assistant_id,
        auto_approve=auto_approve,
        interrupt_shell_only=interrupt_shell_only,
        shell_allow_list=shell_allow_list,
        sandbox_type=sandbox_type,
        sandbox_id=sandbox_id,
        sandbox_snapshot_name=sandbox_snapshot_name,
        sandbox_setup=sandbox_setup,
        enable_shell=enable_shell,
        enable_ask_user=enable_ask_user,
        enable_interpreter=enable_interpreter,
        interpreter_ptc=interpreter_ptc,
        interpreter_ptc_acknowledge_unsafe=interpreter_ptc_acknowledge_unsafe,
        allow_fs_tools=allow_fs_tools,
        rubric_model=rubric_model,
        rubric_max_iterations=rubric_max_iterations,
        auto_classifier_model=auto_classifier_model,
        recursion_limit=recursion_limit,
        mcp_config_path=mcp_config_path,
        no_mcp=no_mcp,
        trust_project_mcp=trust_project_mcp,
        interactive=interactive,
        trust_project_extensions=trust_project_extensions,
        extension_paths=extension_paths,
    )
    _apply_server_config(config)

    # [해설][흐름] 4) 임시 작업 디렉터리 생성 및 스캐폴딩. owns_config_dir=True라 stop() 때 디렉터리가 삭제된다.
    work_dir = Path(tempfile.mkdtemp(prefix="deepagents_server_"))
    _scaffold_workspace(work_dir)

    server = ServerProcess(
        host=host,
        port=port,
        config_dir=work_dir,
        owns_config_dir=True,
        scaffold=_scaffold_workspace,
    )
    # [해설][흐름] 5) 기동 → /ok 헬스 대기 → 그래프 준비 확인(지연 팩토리 강제 실행) → RemoteAgent 생성.
    started = False
    try:
        await server.start()
        await server.wait_for_graph_ready("agent")
        agent = RemoteAgent(
            url=server.url,
            graph_name="agent",
        )
        # [해설][흐름] 6) 워크스페이스 설정: cwd + 세션 claim + fingerprint를 RemoteAgent에 기억시킨다.
        # [해설]   실제 서버 바인딩은 스레드별로 `RemoteAgent.abind_workspace` → `POST /dcode/threads/{id}/workspace`에서 이뤄진다(`client/remote_client.py`).
        # [해설][주의] project_context가 None이면 서버를 이미 띄운 뒤에 실패한다 → finally에서 정리.
        if project_context is None:
            msg = "A workspace is required to start the remote agent."
            raise RuntimeError(msg)
        agent.set_workspace(
            str(project_context.user_cwd),
            config.to_session_workspace_claim(),
            config_fingerprint=config.session_workspace_fingerprint(),
        )
        started = True
        return agent, server, None
    # [해설][흐름] 7) 실패/취소 시 정리. CancelledError까지 잡으려고 except가 아니라 finally를 쓴다.
    finally:
        if not started:
            # Startup failed or was cancelled before the server was handed off
            # to the caller (which records the reference only on success). If
            # `start()` itself failed it already reaped its own subprocess, so
            # this `stop()` is then an idempotent no-op; this cleanup is the sole
            # reaper only when `start()` succeeded but `wait_for_graph_ready()`
            # (or `RemoteAgent()`) failed afterward. A `finally` rather than
            # `except Exception` is deliberate: `asyncio.CancelledError` is a
            # `BaseException`, so an `except Exception` guard would skip cleanup
            # and orphan the process. The inner guard stops a `stop()` error
            # from masking the exception already propagating.
            #
            # `stop()` only *queues* any debug-preserved log path; it is not
            # announced here. This helper is awaited by callers that still own
            # the terminal (the initial TUI startup worker and the in-session
            # cwd-switch flow), where a stderr print would be swallowed by the
            # alternate screen. The queue is process-global, so the outer
            # terminal teardown (`run_textual_app` / `server_session` finally)
            # drains this failed server's path once the terminal is restored.
            try:
                server.stop()
            except Exception:
                logger.exception(
                    "Error stopping server during startup cleanup",
                )


# ------------------------------------------------------------------
# Session context manager
# ------------------------------------------------------------------


# [해설] `start_server_and_get_agent`를 감싸 종료 정리를 보장하는 async context manager.
# [해설] 호출: `client/non_interactive.py:run_non_interactive`(헤드리스, interactive=False). 종료 시 stop + 보존 로그 알림 출력.
@asynccontextmanager
async def server_session(
    *,
    assistant_id: str,
    model_name: str | None = None,
    summarization_model: str | None = None,
    model_params: dict[str, Any] | None = None,
    cli_max_retries: int | None = None,
    profile_overrides: dict[str, Any] | None = None,
    auto_approve: bool = False,
    interrupt_shell_only: bool = False,
    shell_allow_list: list[str] | None = None,
    sandbox_type: str = "none",
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
    enable_shell: bool = True,
    enable_ask_user: bool = False,
    enable_interpreter: bool | None = None,
    interpreter_ptc: str | list[str] | None = None,
    interpreter_ptc_acknowledge_unsafe: bool = False,
    allow_fs_tools: list[FsToolName] | None = None,
    rubric_model: str | None = None,
    rubric_max_iterations: int | None = None,
    auto_classifier_model: str | None = None,
    recursion_limit: int | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    trust_project_extensions: bool = False,
    extension_paths: tuple[str, ...] = (),
    interactive: bool = True,
    host: str = "127.0.0.1",
    port: int = _EPHEMERAL_PORT,
    cwd: str | None = None,
) -> AsyncIterator[tuple[RemoteAgent, ServerProcess]]:
    """Async context manager that starts a server and guarantees cleanup.

    Wraps `start_server_and_get_agent` so callers don't need to duplicate the
    try/finally pattern for stopping the server.

    Args:
        assistant_id: Agent identifier.
        model_name: Model spec string.
        summarization_model: Model spec used only for context-compaction summaries.
        model_params: Extra model kwargs.
        cli_max_retries: Explicit `--max-retries` value.
        profile_overrides: Model profile metadata overrides.
        auto_approve: Auto-approve all tools.
        interrupt_shell_only: Validate shell commands via middleware instead of HITL.
        shell_allow_list: Restrictive shell allow-list for `ShellAllowListMiddleware`.
        sandbox_type: Sandbox type.
        sandbox_id: Existing sandbox ID to reuse.
        sandbox_snapshot_name: Snapshot (langsmith) or blueprint (runloop) name.
        sandbox_setup: Path to setup script for the sandbox.
        enable_shell: Enable shell execution tools.
        enable_ask_user: Enable ask_user tool.
        enable_interpreter: Enable the JS interpreter (`js_eval`) middleware on
            the main agent. `None` uses the sandbox-aware default.
        interpreter_ptc: Invocation-scoped PTC allowlist override.
        interpreter_ptc_acknowledge_unsafe: Explicit acknowledgement for
            `interpreter_ptc="all"` outside of `auto_approve`.
        allow_fs_tools: Allowlist for `FilesystemMiddleware`'s `tools` param.

            `None` leaves the SDK default (all tools).
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
        trust_project_extensions: Allow project extension execution.
        extension_paths: Explicit one-run extension files or directories.
        interactive: Whether the agent is interactive.
        host: Server host.
        port: Server port. Defaults to `_EPHEMERAL_PORT` (0), letting the server
            pick a free ephemeral port instead of the well-known `langgraph dev`
            port 2024.
        cwd: Explicit project workspace to bind to new threads.

    Yields:
        Tuple of `(remote_agent, server_process)`.
    """
    server_proc: ServerProcess | None = None
    mcp_session_manager: MCPSessionManager | None = None
    # [해설][흐름] 기동 → yield(호출자가 에이전트 사용) → finally: MCP 매니저 정리(현재 항상 None) → 서버 stop → 로그 알림.
    try:
        agent, server_proc, mcp_session_manager = await start_server_and_get_agent(
            assistant_id=assistant_id,
            model_name=model_name,
            summarization_model=summarization_model,
            model_params=model_params,
            cli_max_retries=cli_max_retries,
            profile_overrides=profile_overrides,
            auto_approve=auto_approve,
            interrupt_shell_only=interrupt_shell_only,
            shell_allow_list=shell_allow_list,
            sandbox_type=sandbox_type,
            sandbox_id=sandbox_id,
            sandbox_snapshot_name=sandbox_snapshot_name,
            sandbox_setup=sandbox_setup,
            enable_shell=enable_shell,
            enable_ask_user=enable_ask_user,
            enable_interpreter=enable_interpreter,
            interpreter_ptc=interpreter_ptc,
            interpreter_ptc_acknowledge_unsafe=interpreter_ptc_acknowledge_unsafe,
            allow_fs_tools=allow_fs_tools,
            rubric_model=rubric_model,
            rubric_max_iterations=rubric_max_iterations,
            auto_classifier_model=auto_classifier_model,
            recursion_limit=recursion_limit,
            mcp_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            trust_project_extensions=trust_project_extensions,
            extension_paths=extension_paths,
            interactive=interactive,
            host=host,
            port=port,
            cwd=cwd,
        )
        yield agent, server_proc
    finally:
        if mcp_session_manager is not None:
            try:
                await mcp_session_manager.cleanup()
            except Exception:
                logger.warning("MCP session cleanup failed", exc_info=True)
        if server_proc is not None:
            server_proc.stop()
        # Drain unconditionally: when startup fails inside
        # `start_server_and_get_agent`, `server_proc` is never assigned here,
        # yet the failed server may have queued a debug-preserved log path.
        # This runs with no TUI active, so the notice reaches the user.
        emit_preserved_log_notices()
