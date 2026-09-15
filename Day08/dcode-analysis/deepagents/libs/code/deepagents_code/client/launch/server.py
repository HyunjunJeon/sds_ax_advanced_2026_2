"""LangGraph server lifecycle management for the app.

Handles starting/stopping a `langgraph dev` server process and generating the
required `langgraph.json` configuration file.
"""
# [해설] ── 모듈 개요 ─────────────────────────────────────────────
# [해설] 역할: `langgraph dev` 서브프로세스의 수명주기(스폰·헬스체크·그래프 준비 확인·종료·재시작)와
# [해설]   `langgraph.json` 생성, 서버 env 위생 처리를 담당한다.
# [해설] 실행 위치: **클라이언트 프로세스**(TUI/헤드리스). 서버 프로세스 안에서는 import되지 않는다.
# [해설] 주요 진입점: `ServerProcess`(start/wait_for_graph_ready/stop/restart), `generate_langgraph_json`,
# [해설]   `emit_preserved_log_notices`, `wait_for_server_healthy`.
# [해설] 호출자: `client/launch/server_manager.py`(start_server_and_get_agent/server_session), `app.py`(/restart, cwd 전환).
# [해설] 서버 쪽 짝: 생성된 langgraph.json이 `server_graph:make_graph`와 `offload_api:app`을 가리킨다.
# [해설] 관련 분석: analysis/01-boot-client-server.md (흐름 D, 설계 포인트 3·4·5)
# [해설] 관련 문서: libs/code/DEVELOPMENT.md "Debugging"(서버 로그 보존), changelog #4264(ephemeral 포트)·#4642(분리 세션)·#3833(PYTHONPATH),
# [해설]   THREAT_MODEL.md TB10(루프백 + noop 인증).

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess  # noqa: S404
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import quote

from deepagents_code._env_vars import SERVER_ENV_PREFIX
from deepagents_code._paths import (
    DEEPAGENTS_HOME_ENV,
    DEFAULT_PROFILE_MARKER_ENV,
    PATHS,
    export_profile_env,
)
from deepagents_code.config import (
    _INHERITED_PYTHONPATH_ENV,
    _USER_LANGSMITH_ENV_CARRIER,
    _encode_user_langsmith_env,
    _strip_dotenv_loaded_values,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

logger = logging.getLogger(__name__)

# [해설] 서버는 루프백에만 바인딩한다. 인증이 noop이므로 외부 노출을 막는 유일한 경계(THREAT_MODEL TB10).
_DEFAULT_HOST = "127.0.0.1"

_EPHEMERAL_PORT = 0
"""Sentinel port meaning "let `start()` pick a free ephemeral port".

The server is internal and ephemeral — callers reach it via `ServerProcess.url`,
never a typed-in address — so it deliberately avoids binding the well-known
`langgraph dev` default (2024). Leaving 2024 free lets users run their own
`langgraph dev` projects alongside `deepagents-code` without a port collision.
"""

# [해설] 기본 그래프 참조. 이 값일 때만 langgraph.json에 http 블록(offload_api 앱)이 추가된다.
_DCODE_GRAPH_REF = "deepagents_code.server_graph:make_graph"
"""Built-in graph reference. Also gates registration of the offload HTTP app:
a custom `graph_ref` gets no `http` block and does not support `/offload`."""

# [해설] 헬스 폴링 간격(초): 로컬 서버 0.1s, 원격 0.3s. `ServerProcess._start`는 local=True를 넘긴다.
_HEALTH_POLL_INTERVAL_LOCAL = 0.1

_HEALTH_POLL_INTERVAL_REMOTE = 0.3

# [해설] 헬스체크·그래프 준비 대기 기본 타임아웃 60초.
_HEALTH_TIMEOUT = 60

_SHUTDOWN_TIMEOUT = 5
"""Seconds to wait for a graceful exit before escalating to a hard kill.

The server spends up to two seconds flushing buffered LangSmith traces inside
this window. The remaining margin lets LangGraph finish its own lifespan
teardown before dcode escalates; `TestFlushBudget` guards that margin.
"""

# [해설] 종료 예산: 우아한 종료 5초(_SHUTDOWN_TIMEOUT) → SIGKILL 후 2초 대기.
_SIGKILL_TIMEOUT = 2
"""Seconds to wait for the group/process to exit after SIGKILL."""

_WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
"""Windows creation flag required for targeted console control signals.

A literal because `subprocess.CREATE_NEW_PROCESS_GROUP` exists only on
Windows. The flag also disables Ctrl+C handling for the new group, so
`CTRL_C_EVENT` is inert against it and Ctrl+Break is the only graceful
signal available.
"""

_WINDOWS_CTRL_BREAK_EVENT = 1
"""Windows Ctrl+Break event handled as a graceful SIGBREAK by Uvicorn.

A literal because `signal.CTRL_BREAK_EVENT` exists only on Windows. The
value is load-bearing: `subprocess.Popen.send_signal` dispatches on it
exactly, and 0 would mean `CTRL_C_EVENT` instead.
"""

# [해설] 프로세스 그룹 생존 확인(killpg(pgid, 0)) 폴링 간격 50ms.
_PROCESS_GROUP_POLL_INTERVAL = 0.05

# [해설] 조기 종료 시 에러 메시지에 붙일 로그 꼬리 길이(TUI 배너 `ServerStartFailed`로 노출).
_LOG_TAIL_CHARS = 3000
"""Max chars of subprocess log appended to the early-exit `RuntimeError` message.

Enough to carry a Python traceback without flooding the TUI banner when it
surfaces via `ServerStartFailed`.
"""

# [해설] `_startup_error.STARTUP_ERROR_MARKER`와 같은 문자열이어야 한다(서버가 쓰고 여기서 파싱). 값이 중복 정의돼 있음에 주의.
_STARTUP_ERROR_MARKER = "DEEPAGENTS_STARTUP_ERROR:"
"""Machine-readable prefix emitted by the server subprocess for known startup errors."""

# [해설][설계] 서버 서브프로세스 기동을 바꿀 수 있는 env(동적 링커 주입, 인터프리터 경로, askpass 등)를 제거하는 denylist.
# [해설]   신뢰하지 않은 프로젝트에서 실행해도 승인 게이트 이전에 코드가 실행되지 않게 하는 방어. 사용처: `_build_server_env`.
_SERVER_ENV_DENYLIST = frozenset(
    {
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GIT_ASKPASS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "SSH_ASKPASS",
    }
)
"""Inherited env keys that can alter subprocess startup behavior.

`PYTHONPATH` is stripped here so an inherited launch value cannot land on the
server interpreter's `sys.path` during startup, where a path inside an untrusted
project could shadow a stdlib/third-party module and run before any approval
gate. A user who launched with `PYTHONPATH` still wants it for their agent
`execute` commands, so `_build_server_env` relays the value via
`config._INHERITED_PYTHONPATH_ENV` and `agent._apply_inherited_pythonpath`
re-applies it only to the approval-gated shell backend.
"""


# [해설] 지정 포트가 사용 중인지 bind 시도로 확인. `_spawn_process`에서 명시 포트 사용 시 호출.
# [해설][주의] 확인과 실제 서버 bind 사이에 경쟁(TOCTOU)이 있을 수 있다(추정: 로컬 전용이라 실무상 드묾).
def _port_in_use(host: str, port: int) -> bool:
    """Check if a port is already in use.

    Args:
        host: Host to check.
        port: Port to check.

    Returns:
        `True` if the port is in use.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
        else:
            return False


# [해설] OS에 포트 0으로 bind해 빈 포트 번호를 받는다. 소켓을 닫은 뒤 langgraph dev가 그 포트를 쓴다(역시 TOCTOU 가능).
def _find_free_port(host: str) -> int:
    """Find a free port on the given host.

    Args:
        host: Host to bind to.

    Returns:
        An available port number.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# [해설] 서버 기본 URL. `ServerProcess.url`이 사용하며 RemoteAgent가 이 URL로 접속한다.
def get_server_url(host: str = _DEFAULT_HOST, port: int = _EPHEMERAL_PORT) -> str:
    """Build the server base URL.

    Args:
        host: Server host.
        port: Server port.

    Returns:
        Base URL string.
    """
    return f"http://{host}:{port}"


# [해설] 서버 로그를 **뒤에서부터** 훑어 마지막 `DEEPAGENTS_STARTUP_ERROR:` 줄의 요약을 뽑는다.
# [해설] 서버 쪽 출력: `server_graph._print_startup_error`, `_startup_error.emit_startup_failure`.
def _extract_startup_error_marker(output: str) -> str | None:
    """Extract a marked startup error from subprocess output.

    Args:
        output: Combined stdout/stderr captured from the server subprocess.

    Returns:
        The marked startup error message, or `None` if no marker was emitted.
    """
    for line in reversed(output.splitlines()):
        if _STARTUP_ERROR_MARKER in line:
            _, summary = line.rsplit(_STARTUP_ERROR_MARKER, 1)
            return summary.strip() or None
    return None


# [해설] `langgraph dev`용 langgraph.json 생성.
# [해설] 호출: `server_manager._scaffold_workspace`(graph_ref=server_graph:make_graph, checkpointer_path=./checkpointer.py:create_checkpointer).
# [해설] 결과 구성: dependencies ["."](생성된 pyproject), graphs.agent, http.app(offload_api), checkpointer.path.
def generate_langgraph_json(
    output_dir: str | Path,
    *,
    graph_ref: str = _DCODE_GRAPH_REF,
    env_file: str | None = None,
    checkpointer_path: str | None = None,
    auth_path: str | None = None,
) -> Path:
    """Generate a `langgraph.json` config file for `langgraph dev`.

    Registers the interactive `agent` graph and dcode's custom HTTP operations,
    which opt into LangGraph's route-auth layer (`enable_custom_route_auth`) so a
    deployment that configures auth gates them. Production runs `noop` auth and
    relies on the loopback bind, so that gate is inert there -- see
    `auth_path` below and THREAT_MODEL.md TB10. `/offload` is served by that
    backend boundary rather than exposed as another client-addressable graph.

    Args:
        output_dir: Directory to write the config file.
        graph_ref: Python "module:attribute" reference to the graph, where the
            attribute is a graph factory (e.g. `make_graph`) or a graph object.
            Custom graphs omit the built-in offload service, so `/offload` is
            unsupported when one is supplied.
        env_file: Optional path to an env file.
        checkpointer_path: Import path to an async context manager that yields a
            `BaseCheckpointSaver`. When set, the server persists checkpoint data
            to disk instead of in-memory.
        auth_path: Import path to a LangGraph `Auth` instance, emitted as the
            config's `auth.path`. Production servers run with
            `LANGGRAPH_AUTH_TYPE=noop` and no auth backend; this exists so
            tests can prove `enable_custom_route_auth` gates the operation
            routes when a deployment *does* configure one.

    Returns:
        Path to the generated config file.
    """
    # [해설] "."은 같은 디렉터리의 생성된 pyproject.toml(`server_manager._write_pyproject`)을 의존성으로 설치하게 한다.
    config: dict[str, Any] = {
        "dependencies": ["."],
        "graphs": {"agent": graph_ref},
    }
    # [해설] 내장 그래프일 때만 `/dcode/threads/*` 커스텀 HTTP 라우트(offload_api)를 마운트.
    if graph_ref == _DCODE_GRAPH_REF:
        config["http"] = {
            "app": "deepagents_code.offload_api:app",
            "enable_custom_route_auth": True,
        }
    if auth_path:
        config["auth"] = {"path": auth_path}
    if env_file:
        config["env"] = env_file
    if checkpointer_path:
        config["checkpointer"] = {"path": checkpointer_path}

    output_path = Path(output_dir) / "langgraph.json"
    output_path.write_text(json.dumps(config, indent=2))
    return output_path


# ---------------------------------------------------------------------------
# Scoped env-var management
# ---------------------------------------------------------------------------


# [해설] os.environ 오버라이드를 적용하고 **예외 시에만** 롤백하는 컨텍스트 매니저. 성공 시 값은 그대로 남는다.
# [해설] 호출: `ServerProcess.restart`(update_env로 스테이징한 값 적용).
@contextlib.contextmanager
def _scoped_env_overrides(
    overrides: dict[str, str],
) -> Iterator[None]:
    """Apply env-var overrides, rolling back only on exception.

    Separates the concern of temporary `os.environ` mutations from subprocess
    management, making both independently testable.

    On normal exit the overrides are left in place (the caller "keeps"
    them). On exception the previous values are restored so the next attempt
    starts from a known-good state.

    Args:
        overrides: Key/value pairs to set in `os.environ`.

    Yields:
        Control to the caller.
    """
    prev: dict[str, str | None] = {}
    for key, val in overrides.items():
        prev[key] = os.environ.get(key)
        os.environ[key] = val
    try:
        yield
    except Exception:
        for key, old_val in prev.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val
        raise


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------


# [해설] `/ok` 헬스 엔드포인트를 폴링. 프로세스가 먼저 죽으면 즉시 로그 요약 포함 RuntimeError(타임아웃까지 기다리지 않음).
# [해설] 호출: `ServerProcess._start`.
# [해설][주의] `/ok` 200은 "HTTP 서버가 떴다"는 뜻일 뿐, 그래프 팩토리 빌드 성공은 아니다 → `wait_for_graph_ready`가 따로 필요.
async def wait_for_server_healthy(
    url: str,
    *,
    timeout: float = _HEALTH_TIMEOUT,  # noqa: ASYNC109
    process: subprocess.Popen | None = None,
    read_log: Callable[[], str] | None = None,
    local: bool = False,
) -> None:
    """Poll a LangGraph server health endpoint until it responds.

    Args:
        url: Server base URL (health endpoint is `{url}/ok`).
        timeout: Max seconds to wait.
        process: Optional subprocess handle; if the process exits early
            we fail fast instead of waiting for the timeout.
        read_log: Optional callable returning log file contents (for
            error messages on early exit).
        local: Use a shorter poll interval for local servers.

    Raises:
        RuntimeError: If the server doesn't become healthy in time.
    """
    import httpx

    poll_interval = (
        _HEALTH_POLL_INTERVAL_LOCAL if local else _HEALTH_POLL_INTERVAL_REMOTE
    )
    health_url = f"{url}/ok"
    deadline = time.monotonic() + timeout
    last_status: int | None = None
    last_exc: Exception | None = None

    # [해설][흐름] 루프: 1) 프로세스 종료 여부 확인 → 2) GET /ok(개별 요청 타임아웃 2초) → 3) 대기 후 반복.
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                output = read_log() if read_log else ""
                msg = f"Server process exited with code {process.returncode}"
                if output:
                    summary = _extract_startup_error_marker(output)
                    if summary:
                        msg += f": {summary}"
                    msg += f"\n{output[-_LOG_TAIL_CHARS:]}"
                raise RuntimeError(msg)

            try:
                resp = await client.get(health_url, timeout=2)
                if resp.status_code == 200:  # noqa: PLR2004
                    logger.info("Server is healthy at %s", url)
                    return
                last_status = resp.status_code
                logger.debug("Health check returned status %d", resp.status_code)
            except (httpx.TransportError, OSError) as exc:
                logger.debug("Health check attempt failed: %s", exc)
                last_exc = exc

            await asyncio.sleep(poll_interval)

    # [해설] 타임아웃: 마지막 HTTP 상태 또는 마지막 연결 오류를 메시지에 붙인다.
    msg = f"Server did not become healthy within {timeout}s"
    if last_status is not None:
        msg += f" (last status: {last_status})"
    elif last_exc is not None:
        msg += f" (last error: {last_exc})"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Server command / env construction
# ---------------------------------------------------------------------------


# [해설] 서버 실행 명령. 현재 인터프리터(sys.executable)로 `python -m langgraph_cli dev`를 실행한다.
# [해설] --no-browser(Studio 창 안 띄움), --no-reload(파일 변경 감시 끔), --config로 생성된 langgraph.json 지정.
def _build_server_cmd(config_path: Path, *, host: str, port: int) -> list[str]:
    """Build the `langgraph dev` command line.

    Args:
        config_path: Path to the `langgraph.json` config file.
        host: Host to bind.
        port: Port to bind.

    Returns:
        Command argv list.
    """
    return [
        sys.executable,
        "-m",
        "langgraph_cli",
        "dev",
        "--host",
        host,
        "--port",
        str(port),
        "--no-browser",
        "--no-reload",
        "--config",
        str(config_path),
    ]


# [해설] 서버 서브프로세스 env 조립(신뢰 경계 처리).
# [해설] 호출: `_server_env_with_overrides`. DEEPAGENTS_CODE_SERVER_* 값은 os.environ에 이미 있으므로 복사로 함께 전달된다.
def _build_server_env() -> dict[str, str]:
    """Build the environment dict for the server subprocess.

    Copies `os.environ`, sets required flags, and strips variables that are not
    needed or can alter subprocess startup behavior.

    A launch-time `PYTHONPATH` is captured into `config._INHERITED_PYTHONPATH_ENV`
    before being stripped, so the value never reaches the server interpreter's
    `sys.path` but can still be re-applied to agent `execute` commands downstream.

    Returns:
        Environment dict for `subprocess.Popen`.
    """
    # [해설][흐름] 1) os.environ 복사 → 클라이언트가 dotenv에서 로드한 값 제거(서버는 워크스페이스별로 dotenv를 다시 읽음,
    # [해설]   `server_graph._make_graphs` 참고) → 프로필 env 기록 → 바이트코드 미생성 → 인증 noop 강제.
    env = os.environ.copy()
    _strip_dotenv_loaded_values(env)
    export_profile_env(env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LANGGRAPH_AUTH_TYPE"] = "noop"

    # [해설][흐름] 2) 캐리어 변수는 상속값을 신뢰하지 않고 먼저 지운 뒤, 이 프로세스의 부트스트랩 상태에서만 다시 쓴다.
    # Capture a launch-time PYTHONPATH before stripping it. Never trust inherited
    # carrier vars: overwrite them only from bootstrap state in this process.
    env.pop(_INHERITED_PYTHONPATH_ENV, None)
    env.pop(_USER_LANGSMITH_ENV_CARRIER, None)
    inherited_pythonpath = os.environ.get("PYTHONPATH")

    # [해설][흐름] 3) LangGraph 라이선스/컨트롤플레인/인증 관련 키 + denylist 제거.
    for key in (
        "LANGGRAPH_AUTH",
        "LANGGRAPH_CLOUD_LICENSE_KEY",
        "LANGSMITH_CONTROL_PLANE_API_KEY",
        "LANGSMITH_TENANT_ID",
        *_SERVER_ENV_DENYLIST,
    ):
        env.pop(key, None)

    # [해설][흐름] 4) PYTHONPATH는 캐리어로 이관(승인 게이트가 있는 shell 백엔드에만 재적용), 사용자 LangSmith env는 인코딩해 캐리어로 전달.
    if inherited_pythonpath is not None:
        env[_INHERITED_PYTHONPATH_ENV] = inherited_pythonpath
    env[_USER_LANGSMITH_ENV_CARRIER] = _encode_user_langsmith_env()
    return env


# [해설] 호출자 오버라이드(update_env/persist_env)로도 바꿀 수 없는 키. 프로필 루트·캐리어 위조를 막는다.
_IMMUTABLE_SERVER_ENV_KEYS = frozenset(
    {
        DEEPAGENTS_HOME_ENV,
        DEFAULT_PROFILE_MARKER_ENV,
        _INHERITED_PYTHONPATH_ENV,
        _USER_LANGSMITH_ENV_CARRIER,
    }
)
"""Keys `_build_server_env` owns outright, immune to caller overrides.

`persist_env` validates its keys against `SERVER_ENV_PREFIX`, but `update_env`
accepts any key, so without this an `update_env` caller could point the server
at a different profile than the client (splitting the trust root across the two
processes), forge the LangSmith carrier that authenticates approval-gated user
commands, or inject a `PYTHONPATH` into them. Dropping the keys before the
overrides apply keeps that a single list to extend rather than one re-pin per
key, which is how `_INHERITED_PYTHONPATH_ENV` was missed.
"""


# [해설] 최종 서버 env = `_build_server_env()` + persistent 오버라이드 + scoped 오버라이드(뒤가 우선) − 불변 키.
# [해설] 호출: `ServerProcess._spawn_process`.
def _server_env_with_overrides(
    persistent: Mapping[str, str], scoped: Mapping[str, str]
) -> dict[str, str]:
    """Assemble the server environment, ignoring overrides of pinned keys.

    Returns:
        The child environment for the server subprocess.
    """
    env = _build_server_env()
    overrides: dict[str, str] = {**persistent, **scoped}
    # Profile selection is immutable and is not a restart override. Say so when
    # a caller actually tried: silently pointing the server somewhere other
    # than they asked is the kind of thing that gets debugged twice.
    requested = overrides.get(DEEPAGENTS_HOME_ENV)
    if requested is not None and requested != str(PATHS.profile.root):
        logger.warning(
            "Ignoring the %s=%r override for the server subprocess. The "
            "profile is fixed at launch to %s.",
            DEEPAGENTS_HOME_ENV,
            requested,
            PATHS.profile.root,
        )
    for key in _IMMUTABLE_SERVER_ENV_KEYS:
        overrides.pop(key, None)
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Process-group teardown
# ---------------------------------------------------------------------------


# [해설] POSIX에서 서버가 자기 전용 프로세스 그룹의 리더일 때만 그 pgid를 반환(자식 트리 전체에 신호 전달용).
# [해설] dcode 자신의 그룹은 절대 반환하지 않는다(TUI 자살 방지). Windows는 None.
def _server_process_group(pid: int) -> int | None:
    """Return the server's own process group id to signal, or `None`.

    The server is spawned with `start_new_session=True` on POSIX, so it leads
    its own session and process group (its pgid equals its pid). Signaling that
    group reaches the whole `langgraph dev` process tree, so descendants receive
    the same shutdown signals as the root rather than being left running when
    only the root is signaled.

    Returns `None` on Windows (which uses a console process group instead) and
    whenever the server is not the leader of its own dedicated POSIX group. As
    a defensive check, the `pgid == os.getpgid(0)` clause also refuses to return
    dcode's own group, so the group handed back can never be the one whose
    termination would take down the TUI.

    Args:
        pid: Process id of the server subprocess.

    Returns:
        The server's dedicated process group id, or `None` to fall back to
        signaling just the root process.
    """
    if sys.platform == "win32":
        return None
    try:
        pgid = os.getpgid(pid)
        own_pgid = os.getpgid(0)
    except ProcessLookupError:
        # The process already exited; there is no group left to signal.
        return None
    except OSError:
        # Resolving the group failed unexpectedly (getpgid on an owned child
        # should not). Fall back to root-only signaling, but surface it so a
        # silently orphaned descendant tree is diagnosable rather than invisible.
        logger.warning(
            "Could not resolve process group for pid=%d; "
            "falling back to root-only signaling",
            pid,
            exc_info=True,
        )
        return None
    # [해설][주의] 보안/안전 검사: 그룹 리더가 아니거나(pgid != pid) 자기 그룹과 같으면 그룹 신호를 포기.
    if pgid != pid or pgid == own_pgid:
        return None
    return pgid


# [해설] 프로세스 그룹 전체가 사라질 때까지 폴링. 리더를 poll()로 수거해야 좀비가 그룹을 살아있게 보이지 않는다.
# [해설] 호출: `_terminate_server_process`(SIGTERM 후 5초, SIGKILL 후 2초).
def _wait_for_process_group_exit(
    process: subprocess.Popen[Any], pgid: int, timeout: float
) -> bool:
    """Wait until every process in a POSIX process group has exited.

    `Popen.wait()` only observes the group leader. Poll it on every pass so an
    exited leader does not remain a zombie and keep the group probe alive, then
    continue probing because descendants may remain after the leader exits.

    Args:
        process: The group leader, reaped as soon as it exits.
        pgid: Process group id to probe.
        timeout: Maximum seconds to wait for the whole group.

    Returns:
        `True` when the group is gone, or `False` on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        # `poll()` reaps an exited leader without blocking. Until that happens,
        # its zombie entry keeps `killpg(..., 0)` reporting the group as alive.
        process.poll()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            process.wait()
            return True
        except PermissionError:
            # The group still exists even if the probe is not permitted.
            pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_PROCESS_GROUP_POLL_INTERVAL, remaining))


# [해설] Windows 우아한 종료: Ctrl+Break(Uvicorn이 SIGBREAK로 처리). 콘솔이 없으면 terminate로 대체.
def _signal_windows_server(process: subprocess.Popen[Any]) -> None:
    """Request graceful Windows shutdown, terminating when no console exists.

    Args:
        process: The owned server process to signal.

    Raises:
        ProcessLookupError: The server exited before the signal landed.
    """
    try:
        process.send_signal(_WINDOWS_CTRL_BREAK_EVENT)
    except ProcessLookupError:
        raise
    except OSError:
        logger.warning(
            "Failed to send Ctrl+Break to server process pid=%d; "
            "falling back to terminate",
            process.pid,
        )
        process.terminate()


# [해설] 서버와 자손 종료의 핵심 루틴. 호출: `ServerProcess._stop_process_locked`(동기, 최대 약 7초 블로킹 가능).
# [해설] POSIX: killpg SIGTERM → 그룹 종료 대기 5초 → killpg SIGKILL → 2초. Windows: Ctrl+Break → terminate/kill(루트만).
def _terminate_server_process(process: subprocess.Popen[Any]) -> None:
    """Terminate the `langgraph dev` server and its descendants.

    Signals the server for a graceful exit, waits `_SHUTDOWN_TIMEOUT`, then
    escalates to a hard kill: SIGTERM then SIGKILL on POSIX, Ctrl+Break then
    `TerminateProcess` on Windows.

    On POSIX the whole detached process group is signaled via
    `os.killpg`, and teardown waits for the entire group to exit — not just the
    root — so a child that outlives the `langgraph dev` root is still escalated
    to SIGKILL rather than orphaned. On Windows, the server's console process
    group receives Ctrl+Break, which Uvicorn handles as a graceful SIGBREAK and
    runs the Starlette lifespan shutdown. If Ctrl+Break cannot be delivered,
    such as in a headless session without an attached console, the owned child
    is terminated instead. Note that the Windows escalation reaches only the
    root handle, so a descendant that outlives it is orphaned; the POSIX group
    path is the only one that escalates group-wide.

    If no dedicated group is available on POSIX, only the root process is
    signaled. `_server_process_group` guarantees dcode's own POSIX process
    group is never targeted.

    Args:
        process: The running server subprocess to terminate.
    """
    pid = process.pid
    pgid = _server_process_group(pid)
    # Windows resolves no pgid, but Ctrl+Break still reaches the whole console
    # group — while the escalation below only ever kills the root handle. The
    # two scopes are tracked separately so neither log line overstates what
    # actually happened.
    signal_scope = (
        "process group" if pgid is not None or sys.platform == "win32" else "process"
    )
    kill_scope = "process group" if pgid is not None else "process"

    # [해설][흐름] 1) 우아한 종료 신호 + 대기.
    logger.info("Stopping langgraph dev server (pid=%d)", pid)
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
            stopped = _wait_for_process_group_exit(process, pgid, _SHUTDOWN_TIMEOUT)
        else:
            if sys.platform == "win32":
                _signal_windows_server(process)
            else:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=_SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                stopped = False
            else:
                stopped = True
    # [해설] 신호 전에 이미 종료됐으면 정리할 것이 없다. 신호 자체가 실패(EPERM 등)하면 고아 가능성을 로그로 남기고 포기.
    except ProcessLookupError:
        # The server exited before the graceful signal landed; nothing to reap.
        logger.debug(
            "Server %s pid=%d already exited before the graceful signal",
            signal_scope,
            pid,
        )
        return
    except OSError:
        # The graceful signal could not be delivered (e.g. EPERM). We never
        # reach the hard-kill escalation, so the server is left running —
        # report it with the same fidelity as a failed SIGKILL rather than a
        # bare "error stopping".
        logger.exception(
            "Failed to signal server %s pid=%d; it may be orphaned",
            signal_scope,
            pid,
        )
        return

    # [해설][흐름] 2) 시간 내 종료 안 됨 → 강제 종료로 격상.
    if stopped:
        return

    logger.warning("Server did not stop gracefully, killing %s", kill_scope)
    if sys.platform == "win32":
        logger.warning(
            "Windows escalation reaches only the server root; any surviving "
            "`langgraph dev` descendant is left orphaned"
        )
    # Guard escalation explicitly: `ProcessLookupError` means the group exited
    # just before the hard kill, while any other `OSError` means it may be
    # orphaned.
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
            if not _wait_for_process_group_exit(process, pgid, _SIGKILL_TIMEOUT):
                logger.warning(
                    "Server %s pid=%d did not exit after the hard kill", kill_scope, pid
                )
        else:
            process.kill()
            try:
                process.wait(timeout=_SIGKILL_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Server %s pid=%d did not exit after the hard kill", kill_scope, pid
                )
    except ProcessLookupError:
        logger.debug(
            "Server %s pid=%d already exited before the hard kill",
            kill_scope,
            pid,
        )
    except OSError:
        logger.exception(
            "Hard kill failed for server %s pid=%d; it may be orphaned",
            kill_scope,
            pid,
        )


# ---------------------------------------------------------------------------
# Preserved server-log notices
# ---------------------------------------------------------------------------

# Debug-preserved server-log paths awaiting announcement. Recorded by
# `_stop_process_locked` and drained by `emit_preserved_log_notices` once the
# terminal is restored (a notice printed while Textual still owns the alternate
# screen is discarded on exit).
#
# Process-global rather than per-`ServerProcess` on purpose: preserved logs
# accumulate both across `/restart` (which reuses one instance) and across
# throwaway instances that never become the app's tracked process — a server
# whose startup fails, or the previous server replaced by a cwd switch. A
# per-instance slot would strand every path but the tracked instance's last
# one; a single queue lets the terminal teardown announce them all.
# [해설] DEBUG 모드에서 보존된 서버 로그 경로 대기열(프로세스 전역). 아래 락이 스레드 간 접근을 보호한다.
_PENDING_PRESERVED_LOGS: list[Path] = []
# Guards the queue independently of any instance's `_state_lock`, since
# `_stop_process_locked` can append from a fallback worker thread.
_PENDING_PRESERVED_LOGS_LOCK = threading.Lock()


# [해설] 보존된 서버 로그 경로를 stderr로 한 번씩 출력하고 비운다.
# [해설] 호출: `run_textual_app` finally, `server_manager.server_session` finally, `ServerProcess.__aexit__`.
# [해설] 관련 문서: DEVELOPMENT.md "Debugging" — DEEPAGENTS_CODE_DEBUG 시 `$TMPDIR/deepagents_server_log_*.txt` 보존.
def emit_preserved_log_notices() -> None:
    """Print every pending debug-preserved server-log path to stderr, once each.

    Deferred out of `_stop_process_locked` so the interactive TUI can surface
    the paths after Textual restores the terminal. Call this only from terminal
    stop sites that run after the terminal is restored or with no TUI active
    (`run_textual_app` finally, `server_session` finally, `ServerProcess`
    `__aexit__`) — never from the hidden in-session teardown or `/restart`,
    whose prints would be swallowed by the alternate screen.

    Draining is atomic, so the interactive path's overlapping `stop()` calls
    (and any other double teardown) still announce each path exactly once.
    """
    with _PENDING_PRESERVED_LOGS_LOCK:
        paths = list(_PENDING_PRESERVED_LOGS)
        _PENDING_PRESERVED_LOGS.clear()
    for path in paths:
        print(f"Server log preserved at: {path}", file=sys.stderr)  # noqa: T201


# ---------------------------------------------------------------------------
# ServerProcess
# ---------------------------------------------------------------------------


# [해설] `langgraph dev` 서브프로세스 관리 객체. 인스턴스 1개가 /restart를 거치며 여러 서브프로세스를 차례로 소유할 수 있다.
# [해설] 동시성 설계: `_lifecycle_lock`(asyncio, start/restart 직렬화) + `_state_lock`(threading, 동기 stop과 상태 변경 보호)
# [해설]   + `_stop_generation`(종료 세대 카운터: 재시작 중 최종 stop이 이기면 부활을 막음).
# [해설] 생성: `server_manager.start_server_and_get_agent`(config_dir=임시 디렉터리, owns_config_dir=True, scaffold=_scaffold_workspace).
class ServerProcess:
    """Manages a `langgraph dev` server subprocess.

    Focuses on subprocess lifecycle (start, stop, restart) and health checking.
    Env-var management for restarts (e.g. configuration changes requiring a full
    restart) is handled by `_scoped_env_overrides`, keeping this class focused
    on process management.
    """

    def __init__(
        self,
        *,
        host: str = _DEFAULT_HOST,
        port: int = _EPHEMERAL_PORT,
        config_dir: str | Path | None = None,
        owns_config_dir: bool = False,
        scaffold: Callable[[Path], None] | None = None,
    ) -> None:
        """Initialize server process manager.

        Args:
            host: Host to bind the server to.
            port: Initial port to bind the server to. Defaults to
                `_EPHEMERAL_PORT` (0), so `start()` picks a free port and avoids
                squatting the well-known `langgraph dev` default (2024).

                An explicit port is honored, but `start()` still falls back to a
                free port if it is already in use.
            config_dir: Directory containing `langgraph.json`.
            owns_config_dir: When `True`, the server will delete `config_dir`
                on `stop()`.
            scaffold: Optional callable that (re)generates the working
                directory's `langgraph.json` and supporting files. When the
                config is missing at `start()` (e.g. the temp dir was purged
                between the initial boot and a later `/restart`), it is invoked
                to rebuild the workspace instead of failing.
        """
        # [해설] 상태 필드: _process(Popen), _temp_dir(config_dir 미지정 시 자체 임시 디렉터리), _log_file(stdout/stderr 합친 로그),
        # [해설]   _env_overrides(다음 restart 1회용), _persistent_env_overrides(모든 기동에 적용).
        self.host = host
        self.port = port
        self.config_dir = Path(config_dir) if config_dir else None
        self._owns_config_dir = owns_config_dir
        self._scaffold = scaffold
        self._process: subprocess.Popen | None = None
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self._log_file: tempfile.NamedTemporaryFile | None = None  # ty: ignore[invalid-type-form]
        self._env_overrides: dict[str, str] = {}
        self._persistent_env_overrides: dict[str, str] = {}
        # Async lifecycle calls must be serialized by task, not by OS thread:
        # every coroutine on an event loop runs on the same thread, so an
        # RLock would let unrelated tasks enter while another task is awaiting.
        self._lifecycle_lock = asyncio.Lock()
        # Synchronous shutdown can also run from a fallback worker thread. Keep
        # its critical sections short and never hold this lock across an await.
        self._state_lock = threading.Lock()
        self._stopped = False
        self._stop_generation = 0

    @property
    def url(self) -> str:
        """Server base URL."""
        return get_server_url(self.host, self.port)

    @property
    def running(self) -> bool:
        """Whether the server process is running."""
        with self._state_lock:
            return self._running_locked()

    # [해설] `_state_lock`을 이미 잡은 호출자 전용 버전(threading.Lock은 재진입 불가이므로 분리).
    def _running_locked(self) -> bool:
        """Return whether the process is running while `_state_lock` is held."""
        return self._process is not None and self._process.poll() is None

    # [해설] 로그 임시 파일 전체를 읽는다(flush 후). 헬스/그래프 준비 실패 메시지와 마커 추출에 사용.
    def _read_log_file(self) -> str:
        """Read the server log file contents.

        Returns:
            Log file contents as a string (may be empty).
        """
        with self._state_lock:
            if self._log_file is None:
                return ""
            try:
                self._log_file.flush()
                return Path(self._log_file.name).read_text(
                    encoding="utf-8", errors="replace"
                )
            # `ValueError` covers a flush/read on a closed handle ("I/O
            # operation on closed file"): re-checking `is None` under
            # `_state_lock` makes a closed-but-non-None handle nearly
            # unreachable, so this is defensive. `read_text(errors="replace")`
            # cannot raise `ValueError` for decoding, so the catch stays narrow
            # in practice.
            except (OSError, ValueError):
                logger.warning(
                    "Failed to read server log file %s",
                    self._log_file.name,
                    exc_info=True,
                )
                return ""

    # [해설] 공개 시작 API: lifecycle 락을 잡고 `_start` 실행. `__aenter__`도 이것을 호출한다.
    async def start(
        self,
        *,
        timeout: float = _HEALTH_TIMEOUT,  # noqa: ASYNC109
    ) -> None:
        """Start the `langgraph dev` server and wait for it to be healthy.

        Args:
            timeout: Max seconds to wait for the server to become healthy.

        Raises:
            RuntimeError: If the server fails to start or become healthy.
        """  # noqa: DOC502  # `RuntimeError` propagates from `_start`
        async with self._lifecycle_lock:
            await self._start(timeout=timeout)

    # [해설] 스폰 → 헬스 대기. 헬스 대기가 실패/취소되면 finally에서 `stop()`을 워커 스레드로 돌려 서브프로세스를 수거한다.
    async def _start(
        self,
        *,
        timeout: float,  # noqa: ASYNC109
        expected_stop_generation: int | None = None,
    ) -> None:
        """Start while the caller owns `_lifecycle_lock`.

        Args:
            timeout: Max seconds to wait for the server to become healthy.
            expected_stop_generation: Generation captured before a restart's
                stop phase. If synchronous terminal shutdown ran meanwhile,
                abort instead of resurrecting the subprocess.
        """
        process = self._spawn_process(
            expected_stop_generation=expected_stop_generation,
        )
        if process is None:
            return
        started = False
        try:
            await wait_for_server_healthy(
                self.url,
                timeout=timeout,
                process=process,
                read_log=self._read_log_file,
                local=True,
            )
            started = True
        finally:
            if not started:
                # Reap the subprocess we just spawned if startup did not
                # complete — including cancellation (e.g. Ctrl+D / SIGINT before
                # the health check returns). A `finally` rather than `except
                # Exception` is deliberate: `asyncio.CancelledError` is a
                # `BaseException`, so an `except Exception` guard would skip this
                # and orphan the process. Offload `stop()` because terminating a
                # subprocess can block for several seconds; restart cancellation
                # runs this path on Textual's event loop. The inner guard stops a
                # `stop()` error from masking the exception already propagating;
                # `stop()` is effectively non-raising today, so if it does fire it
                # signals an unexpected leak — hence `error`, not `warning`.
                try:
                    await asyncio.to_thread(self.stop)
                except Exception:
                    logger.exception(
                        "Error stopping server during startup cleanup",
                    )

    # [해설] 서브프로세스 준비·스폰(동기, `_state_lock` 안). 호출: `_start`.
    def _spawn_process(
        self,
        *,
        expected_stop_generation: int | None,
    ) -> subprocess.Popen | None:
        """Synchronously prepare and spawn the subprocess under `_state_lock`.

        Args:
            expected_stop_generation: Optional shutdown generation required by
                a restart.

        Returns:
            The new process, or `None` when one is already running.

        Raises:
            asyncio.CancelledError: If terminal shutdown preempted a restart.
            RuntimeError: If the server workspace cannot be prepared.
        """
        # [해설][흐름] 1) 재시작 도중 최종 stop이 끼어들었으면(세대 불일치) 취소로 중단. 이미 실행 중이면 None.
        with self._state_lock:
            if (
                expected_stop_generation is not None
                and self._stop_generation != expected_stop_generation
            ):
                raise asyncio.CancelledError
            if self._running_locked():
                return None
            self._stopped = False

            # [해설][흐름] 2) 작업 디렉터리 결정. config가 없어졌으면(임시 디렉터리 청소 등) scaffold로 재생성 — `/restart` 복구 경로(changelog #4050).
            work_dir = self.config_dir
            if work_dir is None:
                self._temp_dir = tempfile.TemporaryDirectory(
                    prefix="deepagents_server_"
                )
                work_dir = Path(self._temp_dir.name)

            config_path = work_dir / "langgraph.json"
            if not config_path.exists() and self._scaffold is not None:
                logger.info("langgraph.json missing in %s; rescaffolding", work_dir)
                try:
                    work_dir.mkdir(parents=True, exist_ok=True)
                    self._scaffold(work_dir)
                except OSError as exc:
                    msg = f"Failed to rescaffold server workspace at {work_dir}: {exc}"
                    raise RuntimeError(msg) from exc
            if not config_path.exists():
                if self._scaffold is not None:
                    contents = sorted(p.name for p in work_dir.iterdir())
                    msg = (
                        f"Rescaffolding {work_dir} did not produce langgraph.json "
                        f"(directory contents: {contents})."
                    )
                else:
                    msg = (
                        f"langgraph.json not found in {work_dir}. "
                        "Call generate_langgraph_json() first."
                    )
                raise RuntimeError(msg)

            # [해설][흐름] 3) 포트 결정: 0이면 빈 포트, 명시 포트가 사용 중이면 조용히 다른 빈 포트로 대체.
            if self.port == _EPHEMERAL_PORT:
                self.port = _find_free_port(self.host)
                logger.info(
                    "Using ephemeral port %d for langgraph dev server", self.port
                )
            elif _port_in_use(self.host, self.port):
                self.port = _find_free_port(self.host)
                logger.info("Requested port in use, using port %d instead", self.port)

            # [해설][흐름] 4) 명령·env 구성 → 로그 임시 파일(delete=False: DEBUG 보존 가능하도록) 생성.
            cmd = _build_server_cmd(config_path, host=self.host, port=self.port)
            env = _server_env_with_overrides(
                self._persistent_env_overrides, self._env_overrides
            )

            logger.info("Starting langgraph dev server: %s", " ".join(cmd))
            self._log_file = tempfile.NamedTemporaryFile(  # noqa: SIM115
                prefix="deepagents_server_log_",
                suffix=".txt",
                delete=False,
                mode="w",
                encoding="utf-8",
            )
            # [해설][흐름] 5) Popen: cwd=작업 디렉터리(→ checkpointer.py 상대 경로 해석 기준), stdout/stderr → 로그 파일.
            # [해설]   POSIX는 start_new_session=True로 터미널 job-control 신호(Ctrl+C 등)에서 분리(changelog #4642),
            # [해설]   Windows는 새 프로세스 그룹 플래그. 대신 dcode가 종료 시 명시적으로 정리한다.
            self._process = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=str(work_dir),
                env=env,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
                start_new_session=(sys.platform != "win32"),
                creationflags=(
                    _WINDOWS_CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                ),
            )
            return self._process

    # [해설] `GET /assistants/{graph}/graph`를 한 번 호출해 LangGraph의 지연 그래프 팩토리(`server_graph.make_graph`)를 강제 실행.
    # [해설] 목적: 모델/MCP/샌드박스 설정 오류를 첫 사용자 메시지가 아니라 기동 단계에서 드러낸다.
    # [해설] 호출: `server_manager.start_server_and_get_agent`(start 직후).
    # [해설][주의] 루프 형태지만 모든 분기가 return 또는 raise라 실제로는 요청 1회(요청 타임아웃=남은 시간)만 수행한다.
    async def wait_for_graph_ready(
        self,
        graph_name: str = "agent",
        *,
        timeout: float = _HEALTH_TIMEOUT,  # noqa: ASYNC109
    ) -> None:
        """Resolve the served graph once so lazy startup failures surface early.

        Args:
            graph_name: Registered graph name from `langgraph.json`.
            timeout: Max seconds to wait for the graph readiness request.

        Raises:
            RuntimeError: If the server process exits or the graph endpoint
                does not return a successful response.
        """
        import httpx

        if self._process is None:
            msg = "Server process is not running"
            raise RuntimeError(msg)

        graph_url = f"{self.url}/assistants/{quote(graph_name, safe='')}/graph"
        deadline = time.monotonic() + timeout

        # [해설][흐름] 1) 프로세스 종료 확인 → 2) 그래프 요청 → 3) 연결 오류/타임아웃·비200·200 각각 처리.
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    msg = f"Server process exited with code {self._process.returncode}"
                    output = self._read_log_file()
                    if output:
                        summary = _extract_startup_error_marker(output)
                        if summary:
                            msg += f": {summary}"
                        msg += f"\n{output[-_LOG_TAIL_CHARS:]}"
                    raise RuntimeError(msg)

                remaining = max(0.1, deadline - time.monotonic())
                try:
                    resp = await client.get(graph_url, timeout=remaining)
                except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                    output = self._read_log_file()
                    summary = _extract_startup_error_marker(output)
                    if self._process.poll() is not None:
                        msg = (
                            f"Server process exited with code "
                            f"{self._process.returncode}"
                        )
                    else:
                        msg = (
                            f"Server graph '{graph_name}' did not initialize within "
                            f"{timeout}s"
                        )
                    if summary:
                        msg += f": {summary}"
                    if output:
                        msg += f"\n{output[-_LOG_TAIL_CHARS:]}"
                    raise RuntimeError(msg) from exc

                if resp.status_code == 200:  # noqa: PLR2004
                    logger.info("Server graph %s is ready at %s", graph_name, self.url)
                    return

                output = self._read_log_file()
                msg = (
                    f"Server graph '{graph_name}' failed readiness check "
                    f"(status: {resp.status_code})"
                )
                summary = _extract_startup_error_marker(output)
                if summary:
                    msg += f": {summary}"
                if output:
                    msg += f"\n{output[-_LOG_TAIL_CHARS:]}"
                raise RuntimeError(msg)

        msg = f"Server graph '{graph_name}' did not initialize within {timeout}s"
        raise RuntimeError(msg)

    # [해설] 서브프로세스와 로그만 정리(설정 디렉터리 유지). 호출: `restart`(워커 스레드).
    def _stop_process(self) -> None:
        """Stop only the server subprocess and its log file.

        Unlike `stop()`, this does NOT clean up the config directory or temp
        directory, so the server can be restarted with the same config.
        """
        with self._state_lock:
            self._stop_process_locked()

    # [해설] 실제 정리 본체: 살아있으면 종료 루틴 호출 → 핸들 해제 → 로그 파일 닫기 → DEBUG면 경로 대기열에, 아니면 삭제.
    def _stop_process_locked(self) -> None:
        """Stop the subprocess while `_state_lock` is held."""
        if self._process is None:
            return

        if self._process.poll() is None:
            _terminate_server_process(self._process)
            # `_terminate_server_process` is best-effort. If the process is still
            # alive here (e.g. SIGKILL failed with EPERM), then once we drop the
            # handle below we can no longer observe or reap this pid, so surface
            # the still-running process rather than clearing state as if shutdown
            # succeeded.
            if self._process.poll() is None:
                logger.warning(
                    "Dropping handle to server pid=%d that is still running; "
                    "it may be orphaned",
                    self._process.pid,
                )

        self._process = None

        if self._log_file is not None:
            log_path = Path(self._log_file.name)
            try:
                self._log_file.close()
            except OSError:
                logger.debug("Failed to close log file", exc_info=True)

            # [해설] 환경변수 DEEPAGENTS_CODE_DEBUG(`_env_vars.DEBUG`)가 참이면 서버 로그를 삭제하지 않는다.
            from deepagents_code._env_vars import DEBUG, is_env_truthy

            if is_env_truthy(DEBUG):
                # Queue the path rather than printing here: teardown can run
                # while Textual still owns the terminal, so the notice is
                # emitted later via `emit_preserved_log_notices`. Appending
                # (not overwriting) keeps every restart's log announceable.
                with _PENDING_PRESERVED_LOGS_LOCK:
                    _PENDING_PRESERVED_LOGS.append(log_path)
            else:
                try:
                    log_path.unlink()
                except OSError:
                    logger.debug("Failed to clean up log file", exc_info=True)
            self._log_file = None

    # [해설] 최종 정리(멱등): 프로세스 종료 + 임시 디렉터리 + 소유한 config 디렉터리 삭제. 세대 카운터 증가로 진행 중 restart의 부활을 차단.
    # [해설] 호출: server_session finally, start 실패 정리, `__aexit__`, TUI 종료 경로.
    # [해설][주의] 동기 함수이며 최대 수 초 블로킹한다. 이벤트 루프에서 부를 때는 to_thread로 넘기는 호출부가 있다(`_start`).
    def stop(self) -> None:
        """Stop the server process and clean up all resources.

        Idempotent and safe to call concurrently. The synchronous state lock
        prevents process teardown and resource cleanup from interleaving, while
        the generation counter prevents an in-flight async restart from
        spawning a replacement after this terminal stop wins the race.
        """
        with self._state_lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_generation += 1

            self._stop_process_locked()

            if self._temp_dir is not None:
                try:
                    self._temp_dir.cleanup()
                except OSError:
                    # Debug, not warning (unlike the config dir below): a
                    # directory under the OS temp root is eventually reclaimed
                    # by the OS temp reaper (systemd-tmpfiles / tmpwatch / the
                    # macOS periodic cleanup), so a failure here is not the
                    # unrecoverable, never-retried leak the config dir would be.
                    # (`TemporaryDirectory.cleanup()` detaches its finalizer
                    # before removal, so a failed explicit cleanup is not
                    # retried at GC — the OS reaper is what reclaims it.)
                    logger.debug("Failed to clean up temp dir", exc_info=True)
                self._temp_dir = None

            if self._owns_config_dir and self.config_dir is not None:
                import shutil

                try:
                    shutil.rmtree(self.config_dir)
                except OSError:
                    # Warning, not debug: cleanup runs exactly once and is never
                    # retried, and this is a process-owned dir that may hold
                    # session state, so a persistent failure (a real leak) must
                    # be visible to an operator.
                    logger.warning(
                        "Failed to clean up config dir %s",
                        self.config_dir,
                        exc_info=True,
                    )
                self._owns_config_dir = False

    # [해설] 다음 restart에만 적용할 env 오버라이드(키 제한 없음 → 불변 키는 `_server_env_with_overrides`에서 제거).
    def update_env(self, **overrides: str) -> None:
        """Stage env var overrides to apply on the next `restart()`.

        These are applied to `os.environ` immediately before the subprocess
        starts, keeping mutation scoped to the restart call.

        Args:
            **overrides: Key/value env var pairs
                (e.g., `DEEPAGENTS_CODE_SERVER_MODEL="anthropic:claude-sonnet-4-6"`).
        """
        self._env_overrides.update(overrides)

    # [해설] 이후 모든 기동에 적용할 오버라이드. `DEEPAGENTS_CODE_SERVER_` 접두사 키만 허용.
    def persist_env(self, **overrides: str) -> None:
        """Persist env var overrides for every future subprocess start.

        Args:
            **overrides: Key/value env var pairs that should be passed to all
                future server subprocesses.

        Raises:
            ValueError: If an override is not an app-owned server env var.
        """
        invalid = [key for key in overrides if not key.startswith(SERVER_ENV_PREFIX)]
        if invalid:
            msg = (
                "persistent server env overrides must use the "
                f"{SERVER_ENV_PREFIX!r} prefix"
            )
            raise ValueError(msg)
        self._persistent_env_overrides.update(overrides)

    # [해설] 서버 재시작(`/restart`, 모델·설정 변경 등). 같은 config 디렉터리를 재사용하며 포트는 바뀔 수 있다(0/사용 중 처리).
    # [해설] 호출: `app.py`(TUI). 관련 문서: libs/code/COMMANDS.md `/restart`.
    async def restart(self, *, timeout: float = _HEALTH_TIMEOUT) -> None:  # noqa: ASYNC109
        """Restart the server process, reusing the existing config directory.

        Stops the subprocess, then starts a new one. Any env overrides staged
        via `update_env()` are applied within a `_scoped_env_overrides` context
        manager so that failures automatically roll back the environment to the
        last known-good state.

        Args:
            timeout: Max seconds to wait for the server to become healthy.

        Raises:
            asyncio.CancelledError: Either if the restart task is cancelled
                (the blocking subprocess cleanup is awaited to completion
                first), or if a terminal `stop()` bumped the stop generation
                during cleanup — in which case `_start` aborts rather than
                resurrecting the subprocess the terminal stop just tore down.
            RuntimeError: If workspace preparation, process startup, or the
                server health check fails.
        """  # noqa: DOC502  # RuntimeError propagates from _start().
        logger.info("Restarting langgraph dev server")
        async with self._lifecycle_lock:
            # [해설][흐름] 1) 현재 종료 세대 스냅샷 → 2) 서브프로세스 종료(워커 스레드, shield로 취소돼도 끝까지 대기).
            with self._state_lock:
                stop_generation = self._stop_generation
            # Offload the synchronous subprocess shutdown (it blocks up to
            # `_SHUTDOWN_TIMEOUT` + SIGKILL grace waiting on `process.wait`) so
            # the caller's event loop — the Textual reactor for `/restart` —
            # keeps processing input instead of freezing the TUI. Shield the
            # thread and await it after cancellation so no later lifecycle call
            # can mutate process state while cleanup is still running.
            stop_task = asyncio.create_task(asyncio.to_thread(self._stop_process))
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                await stop_task
                raise

            # [해설][흐름] 3) 스테이징된 env를 os.environ에 적용한 채로 기동(실패 시 롤백) → 4) 성공 시 1회용 오버라이드 비움.
            with _scoped_env_overrides(self._env_overrides):
                await self._start(
                    timeout=timeout,
                    expected_stop_generation=stop_generation,
                )

            self._env_overrides.clear()

    # [해설] `async with ServerProcess(...)` 지원. 종료 시 stop + 보존 로그 알림.
    async def __aenter__(self) -> Self:
        """Async context manager entry.

        Returns:
            The server process instance.
        """
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        self.stop()
        emit_preserved_log_notices()
