"""Main entry point and loop."""

# [해설] ── 모듈 개요 ──────────────────────────────────────────────────────────────
# [해설] dcode의 최상위 CLI 디스패처. 콘솔 스크립트 `dcode`/`deepagents-code`가
# [해설] `deepagents_code/__init__.py`의 지연 `__getattr__`를 거쳐 이 모듈의 `cli_main`을 호출한다.
# [해설] 실행 위치: 전부 **클라이언트 프로세스**. LangGraph 서버는 여기서 직접 띄우지 않고
# [해설] 헤드리스는 `client/non_interactive.py`의 `run_non_interactive`(→ `server_session`),
# [해설] 인터랙티브는 `app.py`의 `run_textual_app`(→ `_start_server_background`)이 띄운다.
# [해설] 예외: `--acp`는 `_run_acp_cli_async`가 서버 없이 같은 프로세스에서 그래프를 만든다.
# [해설] 주요 진입점: `cli_main`(디스패처), `parse_args`(argparse 트리), `apply_stdin_pipe`(파이프 stdin),
# [해설] `run_textual_cli_async`(TUI 준비), `_run_acp_cli_async`(ACP), `_run_startup_auto_update`(자동 업데이트),
# [해설] 신뢰 프롬프트 `_check_mcp_project_trust`/`_check_project_hooks_trust`/`_check_project_extensions_trust`.
# [해설][설계] 기동 속도를 위해 대부분의 import를 함수 내부로 미룬다(지연 import). 그래서 함수마다
# [해설] `from deepagents_code... import`가 반복된다. 자동 업데이트 후 혼합 버전 문제도 이 설계에서 나온다.
# [해설] 관련 분석: `analysis/01-boot-client-server.md`(흐름 A·B·D), `analysis/03-config-models-credentials.md`,
# [해설] `analysis/04-approval-hitl-security.md`, `analysis/09-tui-app-commands-acp.md`.
# [해설] 관련 공식 문서: `docs_official/code/cli-reference.md`, `docs_official/code/quickstart.md`.
# [해설] ─────────────────────────────────────────────────────────────────────────
# ruff: noqa: E402
# Imports placed after warning filters to suppress deprecation warnings

# Suppress deprecation warnings from langchain_core (e.g., Pydantic V1 on Python 3.14+)
import warnings

# [해설] 이후 import 과정에서 langchain_core가 내는 deprecation 경고를 막으려고 import보다 먼저 필터를 건다(E402 무시 이유).
warnings.filterwarnings("ignore", module="langchain_core._api.deprecation")

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import shutil
import signal
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

# [해설] 타입 검사 전용 import. 런타임에는 로드하지 않아 CLI 기동 비용을 줄인다(문자열 타입 주석으로 참조).
if TYPE_CHECKING:
    from deepagents import FsToolName
    from deepagents_acp.server import AgentSessionContext
    from langgraph.pregel import Pregel
    from rich.console import Console

    from deepagents_code._dep_floor_check import _FloorViolation
    from deepagents_code.app import AppResult
    from deepagents_code.approval_mode import ApprovalMode
    from deepagents_code.config import Glyphs
    from deepagents_code.configuration.resolver import ConfigResolver
    from deepagents_code.configuration.types import ProviderStatus
    from deepagents_code.hooks.trust import WorkspaceTrust
    from deepagents_code.mcp_tools import MCPServerInfo, ProjectServerSummary
    from deepagents_code.notifications import PendingNotification

# Suppress Pydantic v1 compatibility warnings from langchain on Python 3.14+
warnings.filterwarnings("ignore", message=".*Pydantic V1.*", category=UserWarning)

# [해설] 모듈 수준에서 즉시 import하는 몇 안 되는 가벼운 의존성. `_paths`는 import 시점에 프로필 경로를
# [해설] 해석하므로 `DEEPAGENTS_HOME`이 잘못되면 여기서 `DeepAgentsHomeError`가 나고 `__init__.__getattr__`가 exit 2로 바꾼다.
from deepagents_code._env_vars import LAUNCH_TERM_PROGRAM
from deepagents_code._paths import PATHS, get_deepagents_home
from deepagents_code._version import __version__
from deepagents_code.goal_state_limits import RUBRIC_CHAR_LIMIT, validate_rubric

logger = logging.getLogger(__name__)

# [해설] `--sandbox`를 값 없이 줬을 때 argparse `const`로 저장되는 표식. NUL 문자로 시작해 실제 샌드박스 이름과
# [해설] 충돌하지 않는다. `_resolve_and_validate_sandbox`가 `[sandboxes].default` 설정으로 치환한다.
_SANDBOX_DEFAULT_SENTINEL = "\x00default"
"""Marker stored by `--sandbox` with no value, resolved to `[sandboxes].default`."""

# [해설] 자동 업데이트 실패 쿨다운 마커를 state 디렉터리에 못 쓴 경우 덧붙이는 안내문. `_run_startup_auto_update`에서 사용.
_UNPERSISTED_AUTO_UPDATE_FAILURE_NOTE = (
    "\n[yellow]Note:[/yellow] this failure could not be recorded, so dcode will "
    "retry this update on the next launch until the state directory becomes writable."
)


# [해설] TUI 이전(prompt_toolkit/텍스트) 신뢰 선택기의 선택지. 프로젝트 MCP·hooks·extensions 신뢰 프롬프트와
# [해설] 편집 설치 의존성 하한 불일치 프롬프트(`prompt_for_dep_floor_mismatch`, REFRESH 사용)가 공유한다.
# [해설] `_run_trust_action_picker`/`_select_trust_action`이 이 값을 반환한다.
class _TrustAction(Enum):
    """Actions available in the shared pre-TUI decision picker.

    Project trust prompts use allow-once / remember / deny. The editable
    dependency-floor prompt also offers an explicit environment refresh.
    """

    ALLOW_ONCE = "allow_once"
    REMEMBER = "remember"
    DENY = "deny"
    REFRESH = "refresh"


# [해설] "허용/거부" 결정이 아닌 중단 결과. Ctrl+C면 INTERRUPTED(→ exit 130), Esc/빈 입력이면 CANCELLED(→ 기동 중단).
# [해설] `cli_main`이 신뢰 체크 함수의 반환값을 이 enum과 비교해 종료 경로를 고른다.
class _TrustPromptOutcome(Enum):
    """Trust-prompt results that are not decisions about the subject.

    Subject-neutral so hook and MCP prompts can report the same abort paths.
    """

    INTERRUPTED = "interrupted"
    """The user pressed Ctrl+C; the caller aborts the run (exit 130)."""

    CANCELLED = "cancelled"
    """The user backed out of the trust prompt (Esc in the action or remember
    picker, or blank/EOF in the text fallback); the caller aborts the launch."""


# [해설] 프로젝트 MCP 서버 체크박스 선택기(`_run_project_mcp_server_checkbox_picker`)에서 한 번에 보이는 행 수.
_PROJECT_MCP_PICKER_VISIBLE_ROWS = 8


# [해설] SIGHUP/SIGTERM/SIGQUIT 수신 시 `SystemExit(128+signum)`으로 스택을 풀어 finally 정리를 강제한다.
# [해설][설계] 서버(`langgraph dev`)를 `start_new_session=True`로 분리 실행하므로 터미널이 닫혀도 서버는 시그널을
# [해설] 받지 않는다. 클라이언트가 정상적으로 unwind해야 `ServerProcess.stop()`이 서버 프로세스 그룹을 회수한다.
def _handle_termination_signal(signum: int, _frame: object) -> NoReturn:
    """Unwind dcode on a terminating signal so owned resources are cleaned up.

    Args:
        signum: Received signal number.
        _frame: Interrupted stack frame, unused.

    Raises:
        SystemExit: Always, using the conventional signal-derived exit code.

    Note:
        The `SystemExit` is raised at an arbitrary point in the main thread, so
        it can interrupt server teardown mid-escalation (e.g. between SIGTERM and
        SIGKILL). This is safe because teardown is re-entrant: the process-group
        `except` clauses catch only `ProcessLookupError`/`OSError` (never a
        `BaseException` like `SystemExit`), and the app's cleanup `finally` block
        re-invokes `stop()` as the exception unwinds, resuming the teardown.
    """
    raise SystemExit(128 + signum)


# [해설] `cli_main` 초반에 한 번 호출. Windows에는 해당 시그널이 없어 POSIX에서만 설치한다.
def _install_termination_signal_handlers() -> None:
    """Install graceful terminating-signal handling on POSIX."""
    if sys.platform != "win32":
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT):
            signal.signal(signum, _handle_termination_signal)


# [해설] `dcode -v/--version` 출력 문자열 생성. `cli_main`의 fast path와 `parse_args`의 version 액션에서 쓴다.
# [해설] 네트워크 없이(오프라인) CLI/SDK 버전, 편집 설치 경로, 캐시된 업데이트 상태, 코어/선택 의존성을 모은다.
# [해설] 각 섹션은 개별 try로 감싸 한 부분이 실패해도 버전 출력 자체는 성공하게 한다.
def build_version_text() -> str:
    """Build the plain-text output for the `--version` CLI flag.

    Includes the CLI and SDK versions and any installed optional
    dependencies. For editable installs it also reports the source path and
    the resolved versions of the core LangChain-ecosystem dependencies.

    Reports the same version facts as the `/version` slash command, in the
    same section order (versions, editable path, update status, core dependencies,
    optional dependencies). Release-age suffixes stay omitted so `--version`
    remains offline; update status comes only from the fresh local cache.

    Returns:
        Multi-line version string suitable for stdout.
    """
    from deepagents_code.extras_info import (
        collect_version_report,
        format_cli_version_annotation,
        format_sdk_version_annotation,
    )

    report = collect_version_report()
    cli_annotation = format_cli_version_annotation(report.cli)
    if report.sdk.status == "resolved":
        sdk_version = report.display_sdk_version
        sdk_annotation = format_sdk_version_annotation(report)
    else:
        sdk_version = "unknown"
        sdk_annotation = ""

    text = (
        f"deepagents-code {__version__}{cli_annotation}\n"
        f"deepagents (SDK) {sdk_version}{sdk_annotation}"
    )

    # [해설][흐름] 1) 편집(editable) 설치 여부와 소스 경로.
    editable = False
    try:
        from deepagents_code.config import (
            _get_editable_install_path,
            _is_editable_install,
        )

        editable = _is_editable_install()
        if editable:
            path = _get_editable_install_path()
            text += f"\nEditable install: {path}" if path else "\nEditable install"
    except Exception:
        logger.warning("Unexpected error detecting editable install", exc_info=True)

    # [해설][흐름] 2) 로컬 캐시에 기록된 업데이트 가능 여부(편집 설치면 생략). 프리릴리스 필요 시 버전 고정 명령을 제시.
    try:
        from deepagents_code.update_check import (
            cached_release_requires_prereleases,
            get_cached_update_available,
            is_update_check_enabled,
            upgrade_command,
        )

        if not editable and is_update_check_enabled():
            available, latest = get_cached_update_available()
            if available and latest:
                needs_prereleases = cached_release_requires_prereleases(latest)
                if needs_prereleases is not None:
                    command = upgrade_command(
                        include_prereleases=True if needs_prereleases else None,
                        version=latest if needs_prereleases else None,
                    )
                    text = f"{text}\n\nUpdate available: v{latest}. Run: {command}"
                else:
                    text = f"{text}\n\nUpdate available: v{latest}."
    except Exception:
        logger.debug("Failed to read cached update status", exc_info=True)

    # Core dependencies precede optional dependencies to match the section
    # order of the `/version` slash command (see `_handle_version_command`).
    # [해설][흐름] 3) 편집 설치일 때만 코어 의존성 해석 버전을 붙인다.
    if editable:
        try:
            from deepagents_code.extras_info import format_core_dependencies_plain

            core_text = format_core_dependencies_plain()
        except Exception:
            logger.warning("Failed to collect core dependency versions", exc_info=True)
            core_text = ""
        if core_text:
            text = f"{text}\n\n{core_text}"

    # [해설][흐름] 4) 선택(optional) 의존성 설치 현황.
    try:
        from deepagents_code.extras_info import (
            format_extras_status_plain,
            get_extras_status,
        )

        extras_text = format_extras_status_plain(get_extras_status())
    except Exception:
        logger.warning("Unexpected error collecting optional deps", exc_info=True)
        extras_text = ""
    if extras_text:
        text = f"{text}\n\n{extras_text}"

    return text


# [해설] 자동 업데이트 성공 후 `os.execv`로 현재 프로세스를 새 버전으로 교체한다. 호출자: `_run_startup_auto_update`.
# [해설] 기본은 `python -m deepagents_code <원래 argv>`(→ `__main__.py`), shadow 설치가 감지되면 업그레이드된 shim 경로.
# [해설][주의] 성공하면 반환하지 않는다. `OSError`는 그대로 전파되어 호출자가 "설치됐지만 로드 안 됨"으로 처리한다.
def _restart_current_process(*, restart_path: Path | None = None) -> NoReturn:
    """Replace the current process with a fresh `deepagents_code` invocation.

    Propagates `OSError` from `os.execv` when the process cannot be replaced
    (e.g. the target is missing, not executable, or the mount is `noexec`). It
    is not listed under `Raises:` because it is not raised explicitly here, but
    callers depend on it: the startup auto-update treats both it and the
    `RuntimeError` below as "the upgrade landed but is not loaded".

    Args:
        restart_path: The upgraded console-script shim to execute. When omitted,
            re-executes the current interpreter's `deepagents_code` module.

    Raises:
        RuntimeError: If process replacement unexpectedly returns.
    """
    from deepagents_code._env_vars import INVOKED_AS
    from deepagents_code._invocation import invoked_name

    if restart_path is None:
        executable = sys.executable
        argv = [executable, "-m", "deepagents_code", *sys.argv[1:]]
    else:
        # The PATH winner can be a shim into another uv tool environment.
        # `perform_upgrade` updates uv's configured shim, not necessarily the
        # interpreter that launched this process, so restart through that shim.
        executable = str(restart_path)
        argv = [executable, *sys.argv[1:]]
    # `-m` discards argv[0], so the launch name would be lost across the exec
    # and post-update resume hints would fall back to `dcode`. Hand it to the
    # next generation explicitly.
    # [해설] env는 exec 후에도 상속되므로, 다음 세대 프로세스가 resume 힌트에 사용자가 실제 입력한 명령 이름을 쓸 수 있다.
    os.environ[INVOKED_AS] = invoked_name()
    # Re-exec the trusted current interpreter or uv-managed shim with the
    # user's own argv verbatim; the only "input" is the command the user
    # already ran, so S606's concern (untrusted/unsanitized args to a spawned
    # executable) does not apply.
    os.execv(executable, argv)  # noqa: S606
    msg = "os.execv returned unexpectedly"
    raise RuntimeError(msg)


# [해설] Rich가 현재 터미널 폭에서 `text`를 몇 줄로 감싸는지 계산. `_confirm_update_after_restart`의 줄 지우기에 사용.
def _terminal_row_count(console: "Console", text: str) -> int:
    """Return how many terminal rows Rich renders for `text`.

    Args:
        console: The Rich console whose current width determines wrapping.
        text: The string to measure, rendered with no markup.

    Returns:
        The number of visual rows Rich wraps `text` into, at least 1.
    """
    from rich.text import Text

    return max(1, len(console.render_lines(Text(text), console.options)))


# [해설] 종료 시 체크포인트 조회(LangSmith 링크·resume 힌트) 여부 판정. 현재는 thread_id 존재만 본다.
# [해설] 첫 턴이 사용량 메타데이터 기록 전에 중단돼도 체크포인트가 남을 수 있어 요청 수로 거르지 않는다.
def _should_check_teardown_thread(
    thread_id: str | None,
    *,
    request_count: int,
    resume_thread: str | None,
) -> bool:
    """Return whether teardown should query for checkpointed thread content.

    Any session that owns a thread may have persisted a checkpoint, so the only
    gate is whether a thread exists. `request_count` and `resume_thread` are
    accepted and ignored: an interrupted first turn can checkpoint before any
    usage metadata is recorded, so they are not a reliable proxy. They remain in
    the signature so callers need not change if the gate later grows selective.
    """
    del request_count, resume_thread
    return bool(thread_id)


# [해설] resume 힌트에 `TERM_PROGRAM=... dcode -r <id>` 접두어를 붙일지 결정. 설정 키 `features.resume_term_program`.
# [해설] 값은 `cli_main` 진입 시 스냅샷한 `LAUNCH_TERM_PROGRAM` env에서 읽으므로 .env로 나중에 생긴 값은 무시된다.
# [해설][주의] 제어 문자가 있으면 터미널 이스케이프 주입을 막으려고 통째로 버린다. 네이티브 Windows 셸도 제외.
def _resume_term_program() -> str | None:
    """Return the `TERM_PROGRAM` value to echo in the resume hint, if any.

    Gated on `features.resume_term_program` (off unless the user opts in, on by
    default in debug or experimental mode), so this resolves the option through
    the shared config resolver. The value comes from `LAUNCH_TERM_PROGRAM` --
    the snapshot `cli_main` takes at process entry -- so a `TERM_PROGRAM` that
    only appears later, from a project or global `.env` file, never reaches the
    hint.

    Returns:
        The printable launch-time value when the feature is enabled, else `None`.
        A value carrying control characters is dropped rather than stripped:
        stripping would both write raw escape sequences into teardown output and
        name a terminal the environment never actually contained. Native Windows
        shells also return `None` because they cannot parse the POSIX
        `VAR=value` prefix. POSIX markers (`SHELL` from git-bash/MSYS,
        `MSYSTEM`, `WSL_DISTRO_NAME`) restore the prefix there.
    """
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option
    from deepagents_code.configuration.resolver import get_config_resolver

    option = get_option("features.resume_term_program")
    if option is None:
        # Unreachable unless the manifest key is renamed without updating this
        # literal; log so that mismatch surfaces instead of silently defaulting.
        logger.warning(
            "Unknown config option %r; omitting TERM_PROGRAM from the resume hint",
            "features.resume_term_program",
        )
        return None
    resolved = get_config_resolver().get(option)
    _emit_ranked_diagnostics(option, resolved)
    if not resolved.value:
        return None

    raw = os.environ.get(LAUNCH_TERM_PROGRAM, "").strip()
    if not raw or not raw.isprintable():
        return None
    if sys.platform == "win32" and not any(
        os.environ.get(marker) for marker in ("SHELL", "MSYSTEM", "WSL_DISTRO_NAME")
    ):
        return None
    return raw


# [해설] 인터랙티브 세션 종료 후 LangSmith 스레드 링크와 `dcode -r <thread_id>` 재개 명령을 출력.
# [해설] 호출자: `cli_main`의 인터랙티브 종료 경로. `sessions.thread_exists`로 체크포인트 존재를 한 번만 조회한다.
# [해설][설계] 종료 경로 편의 출력이므로 모든 예외를 debug 로그로 삼킨다.
def _render_teardown_thread_hints(
    console: "Console",
    thread_id: str,
    *,
    return_code: int,
) -> None:
    """Print the LangSmith link and resume hint for a checkpointed thread.

    Both hints share a single `thread_exists` lookup to avoid spinning up a
    second event loop and aiosqlite connection during teardown. Every failure is
    logged at debug and swallowed: teardown convenience output must never crash
    the exit path.

    Args:
        console: Console to print the hints to.
        thread_id: Thread whose checkpoints back the hints.
        return_code: Process exit code; failed sessions add a resume safety caveat.
    """
    import shlex

    from rich.style import Style
    from rich.text import Text

    from deepagents_code._invocation import invoked_name
    from deepagents_code.config import build_langsmith_thread_url
    from deepagents_code.sessions import thread_exists

    # [해설][흐름] 1) sessions.db에서 체크포인트 존재 확인(asyncio.run으로 짧은 이벤트 루프). 없으면 힌트를 출력하지 않는다.
    try:
        thread_has_checkpoints = asyncio.run(thread_exists(thread_id))
    except Exception:
        logger.debug(
            "Could not check thread existence on teardown",
            exc_info=True,
        )
        return

    if not thread_has_checkpoints:
        return

    # [해설][흐름] 2) LangSmith 트레이싱이 설정됐으면 스레드 URL 링크 출력.
    try:
        thread_url = build_langsmith_thread_url(thread_id)
        if thread_url:
            console.print()
            ls_hint = Text("View this thread in LangSmith: ", style="dim")
            ls_hint.append(thread_url, style=Style(dim=True, link=thread_url))
            console.print(ls_hint)
    except Exception:
        logger.debug(
            "Could not display LangSmith thread URL on teardown",
            exc_info=True,
        )

    # [해설][흐름] 3) 셸에 그대로 붙여 넣을 수 있는 resume 명령(`shlex.join`) 출력.
    console.print()
    console.print("[dim]Resume this thread with:[/dim]")
    # Echo the command the user actually launched (a shim or the
    # `deepagents-code` alias), not a hardcoded `dcode` they may not have.
    resume_command = shlex.join([invoked_name(), "-r", str(thread_id)])
    # A shell alias that exports `TERM_PROGRAM` (to select a theme, say) is
    # invisible to `invoked_name`, since an alias does not change `argv[0]`, so
    # the bare command would resume without it. Carrying the launch-time value
    # as an env prefix keeps the line pasteable as-is. Guarded because this
    # reads `config.toml`: unlike the rest of this function, it can raise, and
    # an exception here would replace whatever is already unwinding.
    try:
        term_program = _resume_term_program()
    except Exception:
        logger.debug(
            "Could not resolve resume TERM_PROGRAM on teardown",
            exc_info=True,
        )
        term_program = None
    if term_program is not None:
        resume_command = f"TERM_PROGRAM={shlex.quote(term_program)} {resume_command}"
    console.print(Text(resume_command, style="cyan"))

    if return_code != 0:
        console.print(
            "[dim]Note: the session exited with a non-zero status. Attempting "
            "to resume this thread may fail.[/dim]"
        )


# [해설] re-exec된 새 프로세스에서 이전 세대가 찍은 "Updated to vX. Launching..." 줄을 커서 이동으로 지우고
# [해설] "Updated to vX."로 고쳐 쓴다. 실제 터미널일 때만(리다이렉트 출력 오염 방지). 호출자: `_run_startup_auto_update`.
def _confirm_update_after_restart(console: "Console", version: str) -> None:
    """Rewrite the pre-restart `Launching...` line as a stable update status.

    The `Updated to v{version}. Launching...` line is printed by the previous
    generation right before `os.execv`; this runs in the re-exec'd process to
    clear the transient action once the new version is actually running.

    The in-place rewrite is attempted only on a real terminal: `os.execv` does
    nothing between that print and this process's first output, so the cursor
    is parked on the line directly below it. On non-terminals the escape codes
    would corrupt redirected output, so the line is left as-is.

    The row count is recomputed against the current terminal width, so a resize
    during the upgrade/re-exec window could make the erase loop clear the wrong
    number of wrapped rows. This is a benign visual glitch (no exception), and
    is rare enough not to warrant defending against here.

    Args:
        console: The Rich console used for startup output.
        version: The version now running, used in the confirmation line.
    """
    if not console.is_terminal:
        return
    from rich.control import Control
    from rich.segment import ControlType

    launch_status = f"Updated to v{version}. Launching..."
    launch_rows = _terminal_row_count(console, launch_status)
    # Move up to the bottom row of the old status and erase each rendered row.
    # This preserves the rewrite when Rich wrapped the status in a narrow pane.
    for _ in range(launch_rows):
        console.control(
            Control(
                (ControlType.CURSOR_UP, 1),
                (ControlType.CURSOR_MOVE_TO_COLUMN, 0),
                (ControlType.ERASE_IN_LINE, 2),
            )
        )
    console.print(f"[green]Updated to v{version}.[/green]", highlight=False)


# [해설] 자동 업데이트 쿨다운 기록의 "절대 예외를 내지 않는" 래퍼. 설치 후 종료 경로에서 traceback이 안내문을 덮지 않게 한다.
def _mark_startup_auto_update_failed_safe(version: str) -> None:
    """Record the auto-update cooldown for *version*, never raising.

    Used by the paths that exit after a successful install, where the reason for
    exiting can itself be an unwritable state directory — exactly what
    `mark_startup_auto_update_failed` would trip over. A raise here would replace
    the relaunch hint with an uncaught traceback, so the cooldown is best-effort:
    it exists to break a retry loop, not to gate correctness.

    Args:
        version: Version whose upgrade should not be retried immediately.
    """
    from deepagents_code.update_check import mark_startup_auto_update_failed

    try:
        mark_startup_auto_update_failed(version)
    except Exception:
        logger.warning("Could not record auto-update cooldown", exc_info=True)


# [해설] 설치는 성공했지만 re-exec를 못 한 경우 재실행 안내 후 종료(원인 없음 → exit 0, 예외 원인 있음 → exit 1).
# [해설][설계] 이미 site-packages가 새 버전으로 바뀌어 지연 import가 새 코드를 불러오므로, 계속 실행하면
# [해설] 구버전 모듈과 신버전 모듈이 섞인 프로세스가 된다. 그래서 TUI를 띄우지 않고 멈춘다.
def _exit_after_unrestartable_update(
    console: "Console",
    version: str,
    *,
    cause: BaseException | None = None,
) -> NoReturn:
    """Report an installed-but-not-reloaded update and exit instead of launching.

    Reached only when the startup auto-update installed *successfully* and the
    re-exec that would load it did not happen — either because `os.execv` failed
    (`cause` is `None`), or because an error after the install aborted the path
    before the restart was reached (`cause` is that error). The install already
    replaced the site-packages this process imports from, so launching now would
    build the TUI out of the new code while keeping every already-imported module
    (`_version` among them) on the old release. Exiting with a relaunch hint is
    the only honest outcome.

    Exit code follows whether anything actually broke. Without a `cause` the
    install worked and the user need only run the command again, so this exits
    `0`; a wrapper script should not see that as a failure. With a `cause` an
    unexpected error was swallowed and will very likely recur next launch, so
    exiting `0` would leave the failure invisible to the user, the terminal, the
    log *and* the exit status at once — that path exits `1`.

    Args:
        console: Console to print the notice to.
        version: Version that was installed but is not loaded in this process.
        cause: Error that prevented the restart from being attempted, when the
            caller has one. Summarized in the notice, since this process is
            about to exit and the buffered traceback dies with it.

    Raises:
        SystemExit: Always, to stop the launch.
    """
    # Both are `sys.modules` cache hits by the time this runs — `_debug` from
    # the package `__init__`, `_invocation` from `cli_main` — so neither pulls
    # post-upgrade code into this mixed-version process. Anything imported here
    # must keep that property; a fresh import could load the new release and
    # raise the very `ImportError` this exit exists to avoid.
    # [해설][주의] 여기서 import하는 모듈은 이미 `sys.modules`에 캐시된 것뿐이어야 한다(새 버전 코드 로드 방지).
    from rich.markup import escape

    from deepagents_code._debug import installed_debug_log_path
    from deepagents_code._invocation import invoked_name

    if cause is None:
        detail = (
            "The automatic restart could not run, so this session would keep "
            "using the previous version."
        )
    else:
        detail = (
            "The install succeeded, but an error afterwards stopped the restart, "
            f"so this session would keep using the previous version: "
            f"{escape(str(cause) or type(cause).__name__)}"
        )
    console.print(
        f"[green]Updated to v{version}.[/green] {detail} Run "
        f"[cyan]{invoked_name()}[/cyan] again to start on v{version}.",
        highlight=False,
        markup=True,
    )
    if cause is not None:
        # The traceback went to the in-memory debug buffer, which is drained by
        # the TUI's Debug Console — and this process never starts one. Point at
        # the file log if one is attached, and at how to get one if not.
        log_path = installed_debug_log_path()
        console.print(
            f"Full error: {log_path}"
            if log_path is not None
            else "For the full error, re-run with DEEPAGENTS_CODE_DEBUG=1.",
            highlight=False,
            markup=False,
            style="dim",
        )
    raise SystemExit(0 if cause is None else 1)


# [해설] 인터랙티브 기동 직전(TUI·서버 시작 전) 자동 업데이트를 수행. 호출자: `cli_main`의 인터랙티브 분기
# [해설] (resume이면 `update_check`의 유예 정책에 따라 건너뛸 수 있음). 성공 시 `_restart_current_process`로 re-exec.
# [해설][설계] 설치 "실패"는 fail-soft(현 버전으로 계속), 설치 "성공 후 문제"는 fail-closed(종료)라는 비대칭 규칙.
# [해설] 관련 설정: `[update].auto_update`, env `DEEPAGENTS_CODE_AUTO_UPDATE`, 디버그 env `DEBUG_UPDATE`(설치 생략).
def _run_startup_auto_update(console: "Console") -> None:
    """Apply enabled auto-updates before the TUI and server start.

    On a successful upgrade the process is *always* re-exec'd so the new version
    is loaded, and the process exits rather than launching when the re-exec
    cannot happen. Continuing in-process is not fail-soft once the install has
    landed: the upgrade rewrote the site-packages this process imports from, and
    because startup deliberately defers most imports, the TUI would be loaded
    from the new code while already-imported modules (including `_version`,
    which the splash reads) stay on the old release. That mixed-version process
    reports the pre-upgrade version at best and raises `ImportError` on a
    renamed constant at worst.

    A *failed* install stays fail-soft: the installed version is launched and
    the error is surfaced, never blocking startup.

    Raises:
        SystemExit: Raised after a successful install that could not re-exec, so
            the user relaunches instead of running a mixed-version process —
            with code `0` when only the re-exec failed and `1` when an error
            after the install caused it. Also re-raised rather than suppressed
            by the fail-soft handler, so a process-exit request is never
            swallowed (the `os.execv` re-exec is simulated this way under test).
    """
    from rich.markup import escape

    from deepagents_code._env_vars import DEBUG_UPDATE, RESTARTED_AFTER_UPDATE
    from deepagents_code._version import __version__ as cli_version
    from deepagents_code.config import _is_editable_install
    from deepagents_code.update_check import (
        clear_startup_auto_update_failure,
        create_update_log_file,
        detect_shadowed_dcode_safe,
        format_log_follow_command,
        format_release_age_parenthetical,
        format_shadowed_dcode_warning,
        get_cached_update_available,
        is_auto_update_enabled,
        is_installed_version_at_least,
        is_update_check_enabled,
        mark_auto_update_default_acknowledged,
        mark_startup_auto_update_failed,
        perform_upgrade,
        release_requires_prereleases,
        should_announce_auto_update_default,
        should_skip_startup_auto_update_after_failure,
        update_install_lock,
        upgrade_command,
    )

    # Set to the target version while an upgrade attempt is in flight, and
    # cleared on success. If `perform_upgrade` *raises* instead of returning a
    # failure, the fail-soft handler below records the cooldown from this so the
    # same broken target is not retried — and re-stalled — on every launch.
    # [해설] 두 상태 변수가 예외 핸들러의 분기를 결정한다: 설치 시도 중(pending) / 설치 완료(installed).
    pending_failure_version: str | None = None
    # Set once the install has landed. From that point the on-disk code no
    # longer matches the modules this process already imported, so the fail-soft
    # handler must exit instead of launching a mixed-version TUI.
    installed_version: str | None = None
    try:
        # [해설][흐름] 1) 편집 설치이거나 업데이트 체크·자동 업데이트가 꺼져 있으면 아무것도 하지 않는다.
        if (
            _is_editable_install()
            or not is_update_check_enabled()
            or not is_auto_update_enabled()
        ):
            return
        # Consume the re-exec sentinel recorded before the previous restart.
        # [해설][흐름] 2) 직전 세대가 남긴 re-exec 표식 env를 소비. 새 버전이 실제 로드됐으면 "Launching..." 줄을 확정 표시로 바꾼다.
        restarted_for = os.environ.pop(RESTARTED_AFTER_UPDATE, None)
        if restarted_for is not None and is_installed_version_at_least(restarted_for):
            # The re-exec landed on the upgraded version, so the prior
            # "Launching..." line can be replaced with a stable completed status.
            try:
                _confirm_update_after_restart(console, restarted_for)
            except Exception:
                # The upgrade already succeeded; this rewrite is purely
                # cosmetic. Swallow rendering glitches with their own guard so
                # the outer fail-soft handler does not misreport a successful
                # upgrade as "Auto-update failed". The prior "Launching..."
                # line simply stays.
                logger.debug("Post-restart update confirmation failed", exc_info=True)
        # [해설][흐름] 3) 캐시된 최신 버전 확인(네트워크 없음). 이미 디스크 설치본이 최신이면(세션 내 `/update` 등) 종료.
        available, latest = get_cached_update_available()
        if not available or latest is None:
            return
        if is_installed_version_at_least(latest):
            # The on-disk install already satisfies `latest` (e.g. the user ran
            # `/update` in-session). `get_cached_update_available` compares the
            # baked-in `__version__`, which lags an in-session upgrade, so
            # re-running the upgrade here would be a redundant no-op and restart.
            return
        # [해설][흐름] 4) 무한 재시작 루프 방지: 방금 이 버전으로 재시작했는데 여전히 업데이트가 있다고 나오면 포기.
        if restarted_for == latest:
            # Already restarted after upgrading to this version, yet it still
            # reports as available: the install did not change the running
            # version. Bail out instead of upgrading and restarting forever
            # (this runs before the TUI, so there is no in-app way to stop it).
            update_needs_prereleases = release_requires_prereleases(latest)
            cmd = upgrade_command(
                include_prereleases=True if update_needs_prereleases else None,
                version=latest if update_needs_prereleases else None,
            )
            console.print(
                f"[bold yellow]Warning:[/bold yellow] v{latest} still reports as "
                "available after an automatic update; skipping auto-update to "
                f"avoid a restart loop. Update manually: [cyan]{cmd}[/cyan]\n"
                f"Continuing with v{cli_version}.",
                highlight=False,
            )
            return
        # [해설][흐름] 5) 최근 같은 버전 설치 실패 기록이 있으면 쿨다운 동안 건너뛴다(매 기동 지연 방지).
        if should_skip_startup_auto_update_after_failure(latest):
            # A same-version upgrade failed recently; retrying it here would
            # very likely fail again and re-stall every launch (this runs before
            # the TUI). Skip for the cooldown window and point at a manual
            # command instead.
            update_needs_prereleases = release_requires_prereleases(latest)
            cmd = upgrade_command(
                include_prereleases=True if update_needs_prereleases else None,
                version=latest if update_needs_prereleases else None,
            )
            console.print(
                f"[bold yellow]Warning:[/bold yellow] Skipping automatic update to "
                f"v{latest} after a recent failed attempt. Update manually: "
                f"[cyan]{cmd}[/cyan]\nContinuing with v{cli_version}.",
                highlight=False,
            )
            return
        # [해설][흐름] 6) 기본값으로 자동 업데이트가 켜진 첫 실행: 한 번 공지만 하고 이번 설치는 건너뛴다(opt-out 기회 제공).
        if should_announce_auto_update_default():
            # First-run consent/migration: auto-update is on only because of the
            # opt-out default, not an explicit choice. Announce it once and skip
            # this install so the user can opt out before anything runs.
            #
            # Mark *before* printing so a `console.print` failure cannot leave
            # the notice un-acknowledged and re-firing forever. The inverse risk
            # (mark succeeds, print fails, user never sees it) requires a broken
            # console and is the lesser evil versus an unbounded re-nag.
            acknowledged = mark_auto_update_default_acknowledged()
            message = (
                "[bold]dcode now updates automatically by default.[/bold] "
                f"v{latest} will be installed on the next launch.\n"
                "To opt out, set [cyan][update].auto_update = false[/cyan] in "
                "config.toml or [cyan]DEEPAGENTS_CODE_AUTO_UPDATE=0[/cyan] "
                "(or run [cyan]dcode --auto-update[/cyan] to toggle it off now).\n"
                f"Continuing with v{cli_version} for now."
            )
            if not acknowledged:
                # The acknowledgement could not be persisted (e.g. a read-only
                # state dir). Without this note the identical notice would
                # reappear every launch with no explanation.
                message += (
                    "\n[yellow]Note:[/yellow] this acknowledgement could not be "
                    "saved, so this message may appear again until you opt out "
                    "or the state directory becomes writable."
                )
            console.print(message, highlight=False)
            return
        # Everything below installs into the shared tool environment, so only
        # the process holding the cross-process lock may proceed.
        #
        # The lock is scoped to the install and released before the re-exec
        # below. `os.execv` would in fact drop it on its own — filelock opens
        # its lock file with `os.open`, and PEP 446 makes that fd
        # non-inheritable, so exec closes it — but relying on that would make
        # correctness here hinge on the fd-inheritance behavior of a dependency
        # we do not control. Releasing explicitly also covers the path where
        # `_restart_current_process` raises and this process keeps running.
        # [해설][흐름] 7) 프로세스 간 파일 락을 잡은 세션만 설치. 다른 dcode 세션이 설치 중이면 현 버전으로 계속.
        with update_install_lock() as holding_update_lock:
            if not holding_update_lock:
                console.print(
                    f"Another dcode session is updating to v{latest}; "
                    f"continuing with v{cli_version}.",
                    style="dim",
                    highlight=False,
                )
                return
            release_age = format_release_age_parenthetical(latest)
            console.print(
                f"Updating dcode from v{cli_version} to v{latest}{release_age}..."
            )
            if os.environ.get(DEBUG_UPDATE):
                console.print("Skipped update install (debug mode).", style="dim")
                return
            log_path = create_update_log_file()
            if log_path is not None:
                console.print(
                    f"Update log: {format_log_follow_command(log_path)}",
                    style="dim",
                    highlight=False,
                    markup=False,
                )
            # [해설][흐름] 8) `perform_upgrade`(uv 등으로 설치)를 동기 실행. 락은 with 블록 종료와 함께 re-exec 전에 해제된다.
            pending_failure_version = latest
            success, output, _installed = asyncio.run(
                perform_upgrade(log_path=log_path, target_version=latest)
            )
        # [해설][흐름] 9) 성공: 실패 기록 삭제 → PATH의 shadow dcode 감지 → 표식 env 기록 → re-exec. re-exec 실패 시 종료.
        if success:
            pending_failure_version = None
            installed_version = latest
            clear_startup_auto_update_failure(latest)
            # A stale PATH winner can be backed by a different uv tool
            # environment than this process. In that case restart through the
            # shim uv just upgraded, rather than this process's interpreter.
            # Warn before the restart so the notice lands above the
            # `Launching...` line that `_confirm_update_after_restart` rewrites.
            # Use the never-raises wrapper so a detector defect can't crash
            # startup after an otherwise-successful upgrade.
            shadow = detect_shadowed_dcode_safe()
            if shadow is not None:
                # The warning embeds filesystem paths from `shutil.which`,
                # which can legally contain `[` (macOS/Linux). With
                # `markup=True` those would be parsed as Rich style tags,
                # so escape the warning before interpolation; the sibling
                # auto-update failure branch at the bottom of this function
                # escapes its uv output the same way.
                warning = format_shadowed_dcode_warning(shadow)
                console.print(
                    f"[bold yellow]Warning:[/bold yellow] {escape(warning)}",
                    highlight=False,
                    markup=True,
                )
            console.print(
                f"[green]Updated to v{latest}. Launching...[/green]",
                highlight=False,
            )
            # Record the target version so the re-exec'd process can detect a
            # no-op upgrade and break the loop (see the `restarted_for` guard).
            os.environ[RESTARTED_AFTER_UPDATE] = latest
            try:
                if shadow is None:
                    _restart_current_process()
                else:
                    _restart_current_process(restart_path=shadow.upgraded_bin)
            except (OSError, RuntimeError):
                # Upgrade succeeded but the re-exec did not happen (`os.execv`
                # raised, or returned unexpectedly). Drop the sentinel and stop:
                # this process can no longer launch safely, since the install
                # replaced the code it imports from.
                os.environ.pop(RESTARTED_AFTER_UPDATE, None)
                logger.warning("Restart after update failed", exc_info=True)
                # Exiting instead of re-exec'ing means the `restarted_for`
                # sentinel never reaches a next generation, so the restart-loop
                # guard cannot fire. Record the cooldown so a *successful but
                # no-op* upgrade paired with a persistently failing `os.execv`
                # can't re-upgrade and re-exit on every launch, leaving the TUI
                # permanently unreachable.
                _mark_startup_auto_update_failed_safe(latest)
                _exit_after_unrestartable_update(console, latest)
            return
        # [해설][흐름] 10) 설치가 실패를 "반환"한 경우: 쿨다운 기록 후 수동 명령 안내, 현재 버전으로 계속 기동.
        persisted = mark_startup_auto_update_failed(latest)
        update_needs_prereleases = release_requires_prereleases(latest)
        cmd = upgrade_command(
            include_prereleases=True if update_needs_prereleases else None,
            version=latest if update_needs_prereleases else None,
        )
        detail = f": {escape(output[:200])}" if output else ""
        message = (
            f"[bold red]Auto-update failed{detail}[/bold red]\n"
            f"Run manually: [cyan]{cmd}[/cyan]\n"
            f"Continuing with v{cli_version}."
        )
        if not persisted:
            # The cooldown marker could not be saved (e.g. a read-only state
            # dir), so this same failing upgrade would otherwise be retried on
            # every launch. Surface it rather than silently re-stalling, the
            # way the consent-announce path surfaces its un-persisted state.
            message += _UNPERSISTED_AUTO_UPDATE_FAILURE_NOTE
        console.print(message, markup=True, highlight=False)
    # [해설][흐름] 예외 경로: SystemExit(re-exec/의도적 종료)는 절대 삼키지 않고, 그 외 예외는 설치 완료 여부로 분기한다.
    except SystemExit:
        # Process replacement (and test doubles that simulate it), plus the
        # deliberate post-install exit raised by
        # `_exit_after_unrestartable_update`, must not be swallowed by the
        # fail-soft handler below.
        raise
    except Exception as exc:
        logger.warning("Startup auto-update failed", exc_info=True)
        if installed_version is not None:
            # The install landed and something after it raised, so this process
            # is already mixed-version. Exit rather than describe the successful
            # install as a failure and launch anyway. Record the cooldown for
            # the same reason the re-exec failure path does: no sentinel reaches
            # a next generation, so the restart-loop guard cannot cover this.
            _mark_startup_auto_update_failed_safe(installed_version)
            _exit_after_unrestartable_update(console, installed_version, cause=exc)
        message = (
            "[bold yellow]Warning:[/bold yellow] Auto-update failed before startup; "
            "continuing with the installed version."
        )
        if pending_failure_version is not None:
            # An exception escaped the upgrade attempt itself (not a returned
            # failure), so the returned-failure branch never marked it. Record
            # the cooldown here so the same target is not retried every launch.
            persisted = mark_startup_auto_update_failed(pending_failure_version)
            if not persisted:
                message += _UNPERSISTED_AUTO_UPDATE_FAILURE_NOTE
        console.print(message, markup=True, highlight=False)


# [해설] `-a <name>`이 프로필 루트 아래 앱 소유 디렉터리 이름(`_reserved_names`)이면 진입점에서 바로 exit 2.
# [해설] 하위의 `get_agent_dir`가 거부하면 어떤 플래그 때문인지 알기 어려우므로 원인 지점에서 막는다.
def _reject_reserved_agent_arg(name: str) -> None:
    """Exit with a CLI-level message when `-a` names an app-owned directory.

    Agent profiles are siblings of directories the app owns under the profile
    root, so `get_agent_dir` rejects those names. It is called from several
    places downstream of launch, so the failure is not attributable to the flag
    that caused it. Reject the name here, at the point of entry, instead.

    Note:
        Exits the process with status 2 (argparse's usage-error status) when
        `name` is reserved.
    """
    from deepagents_code._reserved_names import (
        is_reserved_agent_dir_name,
        reserved_agent_dir_names,
    )

    reserved = reserved_agent_dir_names()
    if not is_reserved_agent_dir_name(name):
        return
    from deepagents_code.config import console

    console.print(
        f"[bold red]Error:[/bold red] Agent name {name!r} is reserved for "
        f"dcode's own state.",
        markup=True,
        highlight=False,
    )
    console.print(
        f"Reserved names: {', '.join(sorted(reserved))}.",
        markup=False,
        highlight=False,
    )
    sys.exit(2)


# [해설] 사용할 에이전트 이름 결정: `-a` > (`-r`이면 기본 에이전트, 실제 에이전트는 TUI의 `_resolve_resume_thread`가
# [해설] 스레드 메타데이터로 추론) > `[agents].default` > `[agents].recent`(유효할 때) > `DEFAULT_AGENT_NAME`.
def _resolve_agent_arg(args: argparse.Namespace) -> str:
    """Resolve the final agent identifier from parsed CLI args.

    Precedence, highest first:

    1. Explicit `-a <name>` (stored as `args.agent` by argparse).
    2. `-r <thread>` is present → use `DEFAULT_AGENT_NAME`. The real agent is
        inferred later by `_resolve_resume_thread` via thread metadata
        (`get_thread_agent`), so we must NOT pre-seed a stored agent here or
        it would suppress that inference.
    3. `[agents].default` from config — the user's intentional sticky
        default (set via Ctrl+S in the `/agents` picker).
    4. `[agents].recent` from config — the most recently switched-to agent.
    5. `DEFAULT_AGENT_NAME` as the final fallback.

    Both `default` and `recent` are gated by `_recent_agent_is_valid` so a
    stale entry pointing at a deleted or app-owned directory is ignored.

    Extracted from the `cli_main` body so it's unit-testable without
    constructing the full arg tree.

    Args:
        args: Parsed argparse namespace from `parse_args()`.

    Returns:
        The agent identifier to hand downstream.
    """
    from deepagents_code._constants import DEFAULT_AGENT_NAME

    # [해설][흐름] 1) `-a`가 명시되면 예약 이름만 거르고 그대로 채택.
    if args.agent is not None:
        _reject_reserved_agent_arg(args.agent)
        return args.agent
    # [해설][흐름] 2) `-r`이면 여기서 저장된 에이전트를 채우지 않는다. 채우면 TUI의 스레드 메타데이터 기반 추론(`get_thread_agent`)을 막게 된다.
    if getattr(args, "resume_thread", None) is not None:
        return DEFAULT_AGENT_NAME

    from deepagents_code.model_config import load_default_agent, load_recent_agent

    # [해설][흐름] 3) config.toml `[agents].default`(사용자 고정) → 4) `[agents].recent`(최근 전환) 순. 둘 다 디렉터리 유효성 검사.
    default = load_default_agent()
    if default and _recent_agent_is_valid(default):
        return default

    recent = load_recent_agent()
    if recent and _recent_agent_is_valid(recent):
        return recent
    return DEFAULT_AGENT_NAME


# [해설] `dcode threads list --cwd` 필터 정규화. 체크포인트 metadata의 `cwd`는 `str(Path.cwd())`(심볼릭 링크 미해결)로 저장되므로
# [해설] `.resolve()` 대신 어휘적 정규화(`normpath`+`absolute`)로 맞춘다. `--cwd`만 주면 빈 문자열 → 현재 디렉터리.
def _normalize_cwd_filter(cwd: str | None) -> str | None:
    """Normalize the `threads list --cwd` filter for metadata matching.

    Storage uses `str(Path.cwd())` (absolute, no symlink resolution — see
    `build_run_metadata`). We mirror that here with lexical normalization
    rather than `.resolve()` so a user invoking from a symlinked path doesn't
    get an empty result set due to symlink normalization.

    Args:
        cwd: Parsed `--cwd` value. An empty string means the flag was passed
            without a value and should use the current working directory.

    Returns:
        Absolute path string for filtering, or `None` when no filter was
        requested. Returns `None` if the current working directory cannot
        be determined (bare `--cwd` only).
    """
    if cwd is None:
        return None
    if cwd == "":  # noqa: PLC1901
        try:
            return str(Path.cwd())
        except OSError:
            logger.warning(
                "Could not determine working directory for --cwd; "
                "no cwd filter will be applied",
                exc_info=True,
            )
            return None
    return os.path.normpath(str(Path(cwd).expanduser().absolute()))


# [해설] `--interpreter-tools` 값을 PTC(프로그래매틱 도구 호출) 옵션 형태(`"safe"`/`"all"`/목록)로 파싱.
# [해설] 실제 파싱은 `configuration.provider.parse_interpreter_tools`에 위임하고, `Invalid`면 사용법 오류 exit 2.
def _parse_interpreter_tools_flag(
    raw: str | None,
) -> str | list[str] | None:
    """Parse the `--interpreter-tools` argument into the PTC option shape.

    Args:
        raw: Argparse value: `None` (flag absent), `"safe"`, `"all"`, or a
            comma-separated list of tool names.

    Returns:
        `None` when the flag is absent, the literal string `"safe"`/`"all"`,
        or a list of trimmed tool names. The list may contain `"safe"` as an
        expandable preset (e.g. `"safe,task"` → `["safe", "task"]`).

        Calls `sys.exit(2)` when the value is empty, contains only blank
        tokens, or includes `"all"` inside a list — the CLI treats those as
        usage errors.
    """
    from deepagents_code.configuration.provider import parse_interpreter_tools
    from deepagents_code.configuration.types import Invalid

    if raw is None:
        return None
    parsed = parse_interpreter_tools(raw)
    if isinstance(parsed, Invalid):
        sys.stderr.write(f"Error: --interpreter-tools {parsed.reason}.\n")
        sys.exit(2)
    return parsed


# [해설] 파일시스템 도구 이름 집합. `deepagents`(SDK) import 없이 인자 파싱 핫패스에서 쓰려고 `_constants`에 하드코딩된 사본을 쓴다.
# [해설][SDK] 원본 타입은 SDK의 `deepagents.FsToolName`(FilesystemMiddleware 도구 이름). drift는 테스트가 고정한다.
# Aliased from the dependency-free `_constants` module (see its docstring for
# why the set is hardcoded, how the drift guard pins it, and why importing it
# here keeps the arg-parsing hot path free of a `deepagents` import).
from deepagents_code._constants import FS_TOOL_NAMES as _FS_TOOL_NAMES


# [해설] `--allow-fs-tools`를 SDK `FilesystemMiddleware`의 `tools` 인자 형태로 파싱. `all`/미지정 → `None`(SDK 기본 미들웨어 유지).
# [해설][SDK] 명시 목록일 때만 대체 미들웨어가 설치된다. `read_file`은 `FilesystemMiddleware`가 요구하므로 반드시 포함해야 한다.
# [해설] 검증 실패(빈 값, all+기타 혼합, 모르는 이름, read_file 누락)는 모두 exit 2. 서버 쪽 `_server_config`에서도 fail-closed로 재검증한다.
def _parse_allow_fs_tools_flag(
    raw: str | None,
) -> "list[FsToolName] | None":
    """Parse `--allow-fs-tools` into `FilesystemMiddleware`'s `tools` shape.

    Args:
        raw: Argparse value: `None` (flag absent), `"all"`, or a
            comma-separated list of filesystem tool names.

    Returns:
        `None` when the flag is absent *or* the value is `"all"` (both mean
            "leave the SDK default filesystem middleware in place — all tools"),
            or a list of trimmed, lower-cased tool names.

            Tool names are matched case-insensitively
            (like the `"all"` sentinel), so `READ_FILE` and `read_file`
            are equivalent.

            Calls `sys.exit(2)` when the value is empty, contains only blank
            tokens, combines the `"all"` sentinel with other tool names,
            includes an unknown tool name, or is an explicit list that
            omits `"read_file"` — `FilesystemMiddleware` requires it.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        sys.stderr.write(
            "Error: --allow-fs-tools requires a value: 'all', or a "
            "comma-separated list of filesystem tool names.\n"
        )
        sys.exit(2)
    normalized = text.lower()
    if normalized == "all":
        # `"all"` collapses to `None`: both mean "all filesystem tools". `None`
        # leaves the SDK's own default `FilesystemMiddleware` untouched, which is
        # strictly safer than reinstalling a hand-built unrestricted instance
        # (that would have to re-derive descriptions/permissions and could drift
        # from the SDK default). Only an explicit sub-list installs a
        # replacement middleware.
        return None
    # Lower-case each token so tool names are case-insensitive, matching the
    # `"all"` sentinel above. SDK `FsToolName` members are all lower-case.
    names = [token.strip().lower() for token in text.split(",") if token.strip()]
    if not names:
        sys.stderr.write(
            "Error: --allow-fs-tools list must contain at least one "
            "non-empty tool name.\n"
        )
        sys.exit(2)
    if "all" in names:  # `names` are already lower-cased above.
        sys.stderr.write(
            "Error: --allow-fs-tools 'all' cannot be combined with other tool "
            "names; pass 'all' on its own.\n"
        )
        sys.exit(2)
    unknown = [name for name in names if name not in _FS_TOOL_NAMES]
    if unknown:
        sys.stderr.write(
            f"Error: --allow-fs-tools has unknown tool name(s): "
            f"{', '.join(unknown)}. Valid names: "
            f"{', '.join(sorted(_FS_TOOL_NAMES))}.\n"
        )
        sys.exit(2)
    if "read_file" not in names:
        sys.stderr.write(
            "Error: --allow-fs-tools list must include 'read_file'; it is "
            "required by FilesystemMiddleware.\n"
        )
        sys.exit(2)
    return cast("list[FsToolName]", names)


# [해설] 공유 설정 해석기(`configuration.resolver.get_config_resolver`)를 CLI 순위(CLI_RANK) 포함 상태로 반환.
# [해설] 우선순위 체인: managed → CLI → env → config.toml → 기본값(`analysis/03-config-models-credentials.md`).
# [해설][주의] `args`는 설치 후에도 변형되므로(managed 예외·stdin 병합 등) 이미 설치된 스냅샷을 우선 사용한다.
def _resolver_for_args(args: argparse.Namespace) -> "ConfigResolver":
    """Return the shared resolver after installing parsed CLI state if needed."""
    from deepagents_code.configuration.provider import CliProvider
    from deepagents_code.configuration.resolver import (
        CLI_RANK,
        get_config_resolver,
        installed_cli_provider,
    )

    resolver = get_config_resolver()
    if CLI_RANK not in resolver.provider_statuses():
        # Prefer the installed snapshot. `args` is mutated after
        # `_install_cli_provider` runs -- managed exceptions rewrite `model`
        # and `sandbox`, stdin piping sets `non_interactive_message`, the
        # headless path clears the approval flags -- so rebuilding from `args`
        # here yields a provider that compares unequal to the installed one and
        # raises `ValueError` out of `get_config_resolver`.
        resolver = get_config_resolver(
            cli_provider=installed_cli_provider() or CliProvider(args)
        )
    return resolver


# [해설] `threads list`의 정렬 기준(`threads.sort_order`)과 상대 시간 표시(`threads.relative_time`)를 설정 체인으로 해석.
def _resolve_thread_list_display_options(
    args: argparse.Namespace,
) -> tuple[str, bool]:
    """Resolve thread display flags through the ranked configuration chain.

    Returns:
        The effective sort key and relative-time state.

    Raises:
        RuntimeError: If either option is missing from the manifest.
    """
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option

    sort_option = get_option("threads.sort_order")
    relative_option = get_option("threads.relative_time")
    if sort_option is None or relative_option is None:
        msg = "thread display options are missing from the configuration manifest"
        raise RuntimeError(msg)

    resolver = _resolver_for_args(args)
    sort_order = resolver.get(sort_option)
    relative_time = resolver.get(relative_option)
    _emit_ranked_diagnostics(sort_option, sort_order)
    _emit_ranked_diagnostics(relative_option, relative_time)
    sort_by = "created" if sort_order.value == "created_at" else "updated"
    return sort_by, bool(relative_time.value)


# [해설] 파싱된 argparse 네임스페이스를 `CliProvider`로 감싸 설정 해석 체인에 "지연 설치"한다. 호출자: `cli_main`(parse_args 직후).
# [해설] 여기서 전체 해석기를 만들면 TOML 읽기 비용이 help fast path에도 붙으므로 설치만 해 둔다.
def _install_cli_provider(args: argparse.Namespace) -> None:
    """Install parsed arguments into the shared resolution chain.

    Uses the deferred install: building the full resolver here would import
    `deepagents_code.model_config` and read both TOML snapshots, which the
    help-only fast paths (`dcode help`, bare command groups) must not pay
    before they return.
    """
    from deepagents_code.configuration.provider import CliProvider
    from deepagents_code.configuration.resolver import install_cli_provider

    install_cli_provider(CliProvider(args))


# [해설] JS 인터프리터(`js_eval`) 활성 여부를 해석. managed 정책·CLI 플래그는 원격 샌드박스 기본값보다 우선하며,
# [해설] 원격 샌드박스와 동시에 켜려 하면 `strict`일 때 exit 1(`_exit_interpreter_conflicts_with_sandbox`), 아니면 경고 후 False.
# [해설] env·config.toml 수준의 활성화는 `--sandbox`에 진다(모든 원격 샌드박스 실행이 실패하는 것을 막기 위함).
# [해설][주의] manifest에 옵션이 없으면 True로 기본값을 두지 않고 RuntimeError(강제 managed 키이므로 fail-closed).
def _resolve_interpreter_enabled(
    args: argparse.Namespace, *, strict: bool = True
) -> bool:
    """Return the resolver-backed interpreter state for these CLI args.

    Managed policy and an explicit CLI flag are deliberate, invocation-scoped
    choices, so they outrank the remote-sandbox default. A remote sandbox
    cannot host the interpreter, so an enabling choice from either tier is
    unsatisfiable: under `strict` it exits `1` with an actionable message
    instead of letting a `ValueError` surface from deep inside agent
    construction, and otherwise it warns and reports the interpreter absent.

    Both tiers are honored because `interpreter.enable_interpreter` is an
    `ENFORCED_MANAGED_KEYS` member. Honoring the CLI tier alone left a policy
    hole: a managed `true` became `false` whenever `--sandbox` named a remote
    backend.

    The environment and `config.toml` tiers are ambient preferences rather than
    choices about this run, so `--sandbox` still wins over them. Letting them
    through would turn the redundant-but-harmless `enable_interpreter = true`
    into a launch failure for every remote-sandbox run.

    Managed and CLI decisions are inspected separately so conflicts with a
    remote sandbox can name the deciding tier. When neither decides, the same
    resolved value already contains the environment, user-file, and default
    tiers.

    Args:
        args: Parsed CLI arguments.
        strict: Whether an unsatisfiable choice stops the process. Read-only
            callers such as `dcode tools` pass `False` and get `False`, which
            is what the catalog would actually contain; only a launch has
            something to abort.

    Returns:
        Whether the JS interpreter is enabled for this invocation.

    Raises:
        RuntimeError: If the manifest is missing the option, which is a
            programming error rather than a runtime condition. Defaulting to
            `True` here would enable JS execution for an enforced key.
    """
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option
    from deepagents_code.configuration.resolver import CLI_RANK, MANAGED_RANK

    option = get_option("interpreter.enable_interpreter")
    if option is None:
        msg = (
            "manifest option 'interpreter.enable_interpreter' is missing; "
            "refusing to enable the interpreter without managed-policy input"
        )
        raise RuntimeError(msg)
    resolved = _resolver_for_args(args).get(option)
    _emit_ranked_diagnostics(option, resolved)
    sandbox = getattr(args, "sandbox", None)
    remote_sandbox = bool(sandbox) and sandbox != "none"
    # [해설] 결정에 기여한 순위 중 managed/CLI가 있는지 찾는다. 있으면 그 계층이 "이번 실행에 대한 의도적 선택"이다.
    deciding_rank = next(
        (rank for rank in resolved.ranks if rank in {MANAGED_RANK, CLI_RANK}), None
    )
    if deciding_rank is not None:
        enabled = bool(resolved.value)
        if enabled and remote_sandbox:
            if not strict:
                # Same explanation, no abort: a listing that silently drops the
                # interpreter reads as "policy disabled it", which is the one
                # conclusion a user debugging a missing tool must not draw.
                conflict = _interpreter_sandbox_conflict(sandbox, deciding_rank)
                sys.stderr.write(f"Warning: {conflict}\n")
                return False
            _exit_interpreter_conflicts_with_sandbox(sandbox, deciding_rank)
        return enabled
    if remote_sandbox:
        return False
    return bool(resolved.value)


# [해설] 인터프리터와 원격 샌드박스 충돌 시 오류 출력 후 exit 1. 메시지는 `_interpreter_sandbox_conflict`와 공유.
def _exit_interpreter_conflicts_with_sandbox(
    sandbox_type: str, deciding_rank: int
) -> NoReturn:
    """Abort the launch when the interpreter cannot run under a remote sandbox.

    Always exits `1`: the two settings cannot both be honored, and the user
    must drop one of them.

    Args:
        sandbox_type: The remote sandbox backend the user selected.
        deciding_rank: The provider rank that enabled the interpreter.
    """
    from rich.markup import escape

    from deepagents_code.config import console

    console.print(
        "[bold red]Error:[/bold red] "
        f"{escape(_interpreter_sandbox_conflict(sandbox_type, deciding_rank))}"
    )
    sys.exit(1)


# [해설] 충돌 설명 문구 생성. managed 정책이 원인이면 관리자에게 문의하라는 해결책을, CLI면 플래그 제거를 안내.
def _interpreter_sandbox_conflict(sandbox_type: str, deciding_rank: int) -> str:
    """Describe the interpreter/sandbox conflict and how to resolve it.

    Shared so the launch abort and the `dcode tools` warning cannot drift into
    telling the user two different things about one conflict.

    Args:
        sandbox_type: The remote sandbox backend in effect.
        deciding_rank: The provider rank that enabled the interpreter.

    Returns:
        Plain-text explanation with the remedy for the deciding tier.
    """
    from deepagents_code.configuration.resolver import MANAGED_RANK

    if deciding_rank == MANAGED_RANK:
        remedy = (
            "Managed policy requires the JS interpreter, so this sandbox "
            "cannot be used. Drop --sandbox or ask your administrator to "
            "unset interpreter.enable_interpreter."
        )
    else:
        remedy = "Drop --sandbox or drop --interpreter."
    return (
        "the JS interpreter is not supported with the "
        f"{sandbox_type} sandbox in this release. {remedy}"
    )


# [해설] 시작 승인 모드(manual/auto/yolo 등) 해석. 설정 키 `startup.mode`. 명시값이 없으면(기본 순위만) 앱이 저장한
# [해설] 최근 모드(`model_config.load_startup_mode`)를 복원한다. 호출자: `cli_main` 인터랙티브 분기. 관련: `analysis/04-approval-hitl-security.md`.
def _resolve_approval_mode(args: argparse.Namespace) -> "ApprovalMode":
    """Resolve the startup mode through the shared provider chain.

    When no explicit mode resolves, restore the app-managed recent mode through
    `load_startup_mode`. That path also applies the Auto notice gate and queues
    the explanation shown when a remembered Auto mode cannot be restored.

    Returns:
        Typed effective approval mode.
    """
    from deepagents_code.approval_mode import coerce_approval_mode
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option
    from deepagents_code.configuration.resolver import DEFAULT_RANK

    option = get_option("startup.mode")
    if option is None:
        return coerce_approval_mode("manual")
    resolved = _resolver_for_args(args).get(option)
    _emit_ranked_diagnostics(option, resolved)
    if resolved.ranks == (DEFAULT_RANK,):
        from deepagents_code.model_config import load_startup_mode

        return coerce_approval_mode(load_startup_mode())
    return coerce_approval_mode(resolved.value)


# [해설] `--recursion-limit`가 주어졌을 때만 managed/CLI/env/TOML 체인 전체를 해석해 서버 경계를 넘길 값으로 확정.
# [해설][설계] 서버 프로세스에는 부모의 메모리 내 CliProvider가 없으므로 CLI 값은 여기서 직렬화해야 한다.
# [해설] 플래그가 없으면 `None`을 넘겨 서버 쪽 일반 설정 소스가 결정하게 둔다.
def _resolved_recursion_limit(args: argparse.Namespace) -> int | None:
    """Resolve an explicit CLI limit for the server-process boundary.

    A normal TUI or headless launch builds the agent in a fresh server process,
    where the parent's in-memory CLI provider does not exist. Resolve the full
    managed/CLI/env/TOML chain here when the flag was supplied so the effective
    value survives serialization. With no flag, keep deferring to the build
    process so its ordinary configuration sources remain authoritative.

    Returns:
        The effective explicit limit, or `None` when the flag was absent or
            every configured tier was rejected. `positive_int` on the flag makes
            the latter unreachable today, but `resolve_recursion_limit` can
            return `None` with the flag present.
    """
    if getattr(args, "recursion_limit", None) is None:
        return None
    from deepagents_code.config_manifest import resolve_recursion_limit

    return resolve_recursion_limit()


# [해설] YOLO(무승인 실행) 경고와 인라인 선택기(prompt_toolkit, stderr 출력). 기본 선택은 "Use Manual".
# [해설][설계] fail-closed: TTY가 아니거나 선택기 사용 불가/오류면 False(=Manual). Ctrl+C/Ctrl+D는 KeyboardInterrupt로 기동 자체를 중단.
def _prompt_yolo_acknowledgement(console: "Console") -> bool:
    """Show an inline fail-closed selector for unrestricted execution.

    Args:
        console: Rich console used for warning and fallback text.

    Returns:
        Whether the user explicitly accepted the warning.
    """
    console.print()
    console.print(
        "[bold red]YOLO mode: the agent acts on its own, with no approval "
        "prompts.[/bold red]"
    )
    console.print(
        "It can run commands, change files, and use tools on your machine "
        "without asking you first."
    )
    console.print('[dim]Not sure? Pick "Use Manual" below.[/dim]')
    console.print()
    # [해설][주의] 비대화형 환경에서는 사용자가 확인할 수 없으므로 YOLO를 허용하지 않는다.
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.key_binding.key_processor import KeyPressEvent
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.output.defaults import create_output
        from prompt_toolkit.styles import Style

        from deepagents_code.config import get_glyphs

        choices = [(False, "Use Manual"), (True, "Acknowledge and enable YOLO")]
        selected_index = 0
        glyphs = get_glyphs()

        def rows() -> FormattedText:
            fragments: list[tuple[str, str]] = [
                (
                    "class:prompt.help",
                    (
                        f"{glyphs.arrow_up}/{glyphs.arrow_down}/Tab move "
                        f"{glyphs.separator} "
                        f"Enter select {glyphs.separator} Esc Manual "
                        f"{glyphs.separator} Ctrl+C quit\n"
                    ),
                )
            ]
            for index, (_value, label) in enumerate(choices):
                active = index == selected_index
                cursor = glyphs.cursor if active else " "
                style = "class:item.current" if active else "class:item"
                suffix = "\n" if index < len(choices) - 1 else ""
                fragments.append((style, f"{cursor} {label}{suffix}"))
            return FormattedText(fragments)

        bindings = KeyBindings()

        @bindings.add("up")
        @bindings.add("s-tab")
        def move_up(_event: KeyPressEvent) -> None:
            nonlocal selected_index
            selected_index = (selected_index - 1) % len(choices)

        @bindings.add("down")
        @bindings.add("tab")
        def move_down(_event: KeyPressEvent) -> None:
            nonlocal selected_index
            selected_index = (selected_index + 1) % len(choices)

        @bindings.add("enter")
        def choose(event: KeyPressEvent) -> None:
            event.app.exit(result=choices[selected_index][0])

        @bindings.add("escape")
        def decline(event: KeyPressEvent) -> None:
            event.app.exit(result=False)

        @bindings.add("c-c")
        @bindings.add("c-d")
        def abort(event: KeyPressEvent) -> None:
            # Quit the launch entirely rather than falling back to Manual; the
            # top-level KeyboardInterrupt handler prints "Interrupted" and exits
            # without starting the TUI.
            event.app.exit(exception=KeyboardInterrupt())

        app: Application[bool] = Application(
            layout=Layout(
                Window(
                    FormattedTextControl(rows, show_cursor=False),
                    height=len(choices) + 1,
                    dont_extend_height=True,
                )
            ),
            key_bindings=bindings,
            style=Style.from_dict(
                {"prompt.help": "ansibrightblack", "item.current": "reverse"}
            ),
            full_screen=False,
            erase_when_done=True,
            output=create_output(stdout=sys.stderr),
        )
        return bool(app.run())
    except (EOFError, OSError, RuntimeError, ImportError):
        logger.debug("YOLO acknowledgement selector unavailable", exc_info=True)
        return False
    # A KeyboardInterrupt (Ctrl+C/Ctrl+D) deliberately propagates so the launch
    # aborts instead of falling back to Manual and starting the TUI.


# [해설] YOLO 확인이 이미 저장돼 있으면 통과, 없으면 프롬프트 후 저장까지 성공해야 True. 저장 실패도 Manual로 떨어진다.
# [해설] 호출자: `cli_main`의 인터랙티브 승인 모드 해석 경로(ACP 모드에서는 저장된 확인 여부만 확인, `cli_main` 참고).
def _ensure_yolo_acknowledged(console: "Console") -> bool:
    """Ensure the current local YOLO policy has been accepted and persisted.

    Args:
        console: Console used for the acknowledgement UI.

    Returns:
        `True` only when an existing or newly persisted acknowledgement exists.
    """
    from deepagents_code.approval_mode import (
        has_yolo_acknowledgement,
        save_yolo_acknowledgement,
    )

    if has_yolo_acknowledgement():
        return True
    if not _prompt_yolo_acknowledgement(console):
        return False
    if save_yolo_acknowledgement():
        return True
    console.print(
        "[yellow]YOLO acknowledgement could not be saved; using Manual.[/yellow]"
    )
    return False


# [해설] 헤드리스(`-n`) 경로에서 원격 샌드박스 때문에 기본 활성 인터프리터가 조용히 빠졌음을 stderr로 경고.
# [해설] 판정은 `_server_config._interpreter_suppressed_by_sandbox`와 공유해 서버 쪽 실제 동작과 일치시킨다. TUI는 알림으로 표시.
def _warn_if_interpreter_disabled_by_sandbox(args: argparse.Namespace) -> None:
    """Warn that a remote sandbox suppressed the otherwise-default interpreter.

    With `js_eval` on by default in local mode, a `--sandbox` run silently drops
    it (the middleware is unsupported under a remote sandbox). This prints to
    stderr on the non-interactive (`-n`) path; the interactive TUI surfaces the
    same advisory as a startup notification (see
    `DeepAgentsApp._notify_interpreter_disabled_by_sandbox`).

    Keyed on the raw `args.interpreter` tri-state so an explicit
    `--no-interpreter` opt-out stays silent (the predicate only fires for the
    unset default).

    Raises:
        RuntimeError: If the interpreter option is absent from the manifest.
    """
    from deepagents_code._server_config import _interpreter_suppressed_by_sandbox
    from deepagents_code.config_manifest import get_option

    option = get_option("interpreter.enable_interpreter")
    if option is None:
        msg = "interpreter.enable_interpreter is missing from the config manifest"
        raise RuntimeError(msg)
    local_default = bool(_resolver_for_args(args).get(option).value)

    if not _interpreter_suppressed_by_sandbox(
        enable_interpreter=args.interpreter,
        sandbox_type=args.sandbox,
        local_default=local_default,
    ):
        return
    from rich.console import Console as _Console

    _Console(stderr=True).print(
        "[yellow]Warning:[/yellow] JS interpreter (`js_eval`) is unavailable "
        "under a remote sandbox; it runs in local mode only."
    )


# [해설] `--rubric` 값(리터럴 또는 `@파일경로`)을 문자열로 해석하고 `goal_state_limits.validate_rubric`으로 크기 검증.
# [해설] 헤드리스 전용 기능(goal/rubric은 `analysis/05-subagents-goals-rubrics.md`). 오류는 ValueError로 올려 `cli_main`이 출력.
def _resolve_rubric_text(rubric: str | None) -> str | None:
    """Resolve the rubric from `--rubric` into one string.

    `--rubric` accepts literal text, or `@path` to read a file. File paths
    may be absolute, relative to the `dcode` process working directory, or
    `~`-expanded home paths.

    Args:
        rubric: Value of `--rubric` (literal text or `@path`), or `None`.

    Returns:
        The resolved rubric text, or `None` when the flag was not supplied.

    Raises:
        ValueError: If the rubric is empty, exceeds `RUBRIC_CHAR_LIMIT`, or a
            referenced file is missing, unreadable, or empty. The size case is
            a `GoalStateSizeError`, whose message names the limit and the
            excess.
    """
    if rubric is None:
        return None

    # An `@`-prefixed value is always read as a file path. The path may be
    # absolute, relative to the `dcode` process working directory, or `~`-based.
    # There is no way to pass a literal rubric that begins with `@` (put such
    # text in a file).
    if rubric.startswith("@"):
        path = rubric[1:]
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            # `UnicodeError` (e.g. `UnicodeDecodeError`) subclasses `ValueError`,
            # not `OSError`. Catch it here so a binary/non-UTF-8 file yields the
            # framed "Could not read rubric file" message instead of a raw codec
            # error.
            msg = f"Could not read rubric file {path!r}: {exc}."
            raise ValueError(msg) from exc
        if not text.strip():
            msg = f"Rubric file {path!r} is empty."
            raise ValueError(msg)
        resolved = text.strip()
        validate_rubric(resolved)
        return resolved

    if not rubric.strip():
        msg = "--rubric must not be empty."
        raise ValueError(msg)
    resolved = rubric.strip()
    validate_rubric(resolved)
    return resolved


# The standalone `validate_rubric` above is sufficient here, unlike `/rubric next`,
# which additionally runs the combined notice check via `_next_rubric_size_error`.
# That check exists because an in-session one-shot rubric is embedded in the notice
# beside an actionable goal's objective and status note, and the pair can exceed
# `GOAL_NOTICE_TEXT_CHAR_LIMIT` even when each fits alone. `--rubric` cannot reach
# that state: it requires `-n`, and `run_non_interactive` takes no resume or
# thread-id argument, so it always starts a fresh thread with no checkpointed goal.
# `--goal` is rejected alongside `--rubric` and is interactive-only, so no goal
# objective can be set on this path either. Add the combined check here if the
# non-interactive path ever gains thread resumption.


# [해설] 헤드리스에서 인터프리터가 꺼져 있는데 `--interpreter-tools`를 준 경우 무효임을 경고.
def _warn_if_interpreter_tools_without_interpreter(
    args: argparse.Namespace, *, enable_interpreter: bool
) -> None:
    """Warn that `--interpreter-tools` is a no-op without the interpreter.

    This drives the non-interactive (`-n`) path and prints to stderr. The
    interactive TUI surfaces the same advisory as a startup notification (see
    `DeepAgentsApp._notify_interpreter_tools_without_interpreter`).

    Attributes are accessed directly (not via `getattr` defaults) so an argparse
    `dest` rename fails loudly in tests rather than silently disabling the warning.
    """
    if args.interpreter_tools is None:
        return
    if enable_interpreter:
        return
    from rich.console import Console as _Console

    _Console(stderr=True).print(
        "[yellow]Warning:[/yellow] --interpreter-tools has no effect "
        "when the interpreter is disabled."
    )


# [해설] 저장된 `[agents].default`/`recent` 이름이 현재 프로필(`DEEPAGENTS_HOME`) 아래 실제 디렉터리이며 예약 이름이 아닌지 확인.
# [해설] 오래된 값 때문에 서버 기동이 실패하지 않도록 무효면 조용히 기본 에이전트로 폴백(OSError도 무효 처리).
def _recent_agent_is_valid(name: str) -> bool:
    """Return whether `name` is a usable agent in the selected profile.

    Used to guard against a stale `[agents].recent` entry pointing at an agent
    the user has since deleted, or at a name the app owns — in either case we
    silently fall back to the hard-coded default instead of failing at server
    start.

    The path comes from the lightweight immutable launch snapshot rather than
    `settings`, which is intentionally imported *after* argparse in `cli_main`
    (per the startup-hot-path guidance there).

    `is_dir()` is wrapped in `try/except OSError` so permission errors on the
    profile root (symlink loops, EACCES) don't crash the launch — we
    treat them the same as "not valid" and fall back to the default.
    """
    from deepagents_code._reserved_names import is_reserved_agent_dir_name

    if is_reserved_agent_dir_name(name):
        # `bin/` and `plugins/` are real directories under the profile root, so
        # the `is_dir()` check below would accept them and the launch would
        # then fail in `get_agent_dir`. A stale entry must fall back, never
        # break every launch. On case-insensitive filesystems the check also
        # catches a differently cased stale entry such as `Plugins`.
        logger.warning(
            "Stored agent %r names an app-owned directory; falling back to default",
            name,
        )
        return False
    try:
        return (get_deepagents_home() / name).is_dir()
    except OSError:
        logger.warning(
            "Could not validate recent agent %r; falling back to default",
            name,
            exc_info=True,
        )
        return False


# [해설] 필수 런타임 의존성(requests, python-dotenv, tavily-python, textual) 존재 여부를 `find_spec`으로만 확인(실제 import 없음).
# [해설] 누락 시 재설치 안내 후 exit 1. 호출자: `cli_main`(`--acp`가 아닐 때, parse_args 이전).
def check_cli_dependencies() -> None:
    """Check if optional dependencies are installed."""
    missing = []

    if importlib.util.find_spec("requests") is None:
        missing.append("requests")

    if importlib.util.find_spec("dotenv") is None:
        missing.append("python-dotenv")

    if importlib.util.find_spec("tavily") is None:
        missing.append("tavily-python")

    if importlib.util.find_spec("textual") is None:
        missing.append("textual")

    if missing:
        print("\nMissing required dependencies!")  # noqa: T201  # App output for missing dependencies
        print("\nThe following packages are required to use dcode:")  # noqa: T201  # App output for missing dependencies
        for pkg in missing:
            print(f"  - {pkg}")  # noqa: T201  # CLI output for missing dependencies
        print("\nReinstall dcode with the recommended installer:")  # noqa: T201  # CLI output for missing dependencies
        print("  curl -LsSf https://langch.in/dcode | bash")  # noqa: T201  # CLI output for missing dependencies
        print("\nOr install the tool directly via uv:")  # noqa: T201  # CLI output for missing dependencies
        print("  uv tool install -U deepagents-code")  # noqa: T201  # CLI output for missing dependencies
        sys.exit(1)


# [해설] 패키지 매니저를 못 찾았을 때 안내할 ripgrep 설치 문서 URL. `_ripgrep_install_hint`와 알림 payload에서 사용.
_RIPGREP_URL = "https://github.com/BurntSushi/ripgrep#installation"
"""Fallback installation URL when no platform package manager is detected."""


# [해설] `PATHS.display`(홈 축약 등)로 표시용 경로를 만들고 Rich 마크업 이스케이프. 경로에 `[`가 있어도 안전.
def _rich_path_display(path: Path) -> str:
    """Return an effective path escaped for Rich markup."""
    from rich.markup import escape

    return escape(PATHS.display(path))


# [해설] 프로필 루트 권한 확인 안내문. 설정/state 쓰기 실패 메시지에서 재사용.
def _profile_permission_hint() -> str:
    """Return the shared "check permissions" remediation for the profile root."""
    return f"Check permissions for {_rich_path_display(PATHS.profile.root)}"


# [해설] 기본이 아닌 프로필(`DEEPAGENTS_HOME`)을 쓸 때 기동 안내문 생성. 존재/읽기 불가/새로 생성 세 상태를 구분한다.
# [해설][설계] 오타 난 DEEPAGENTS_HOME은 빈 프로필로 보여 "설정·자격 증명이 사라진 것"처럼 보이므로 원인을 명시한다.
def _configured_profile_notice() -> str | None:
    """Return a launch notice naming a non-default profile, if one is selected.

    A mistyped or stale `DEEPAGENTS_HOME` resolves to an empty directory, which
    is indistinguishable from a first run: no credentials, no MCP tokens, no
    config. Naming the profile, and saying whether it already existed,
    identifies the cause. Without it the launch looks like data loss.

    `classify_path` reports three states and each gets its own line. An
    unreadable root is a permission problem, not a wrong profile, so it must
    not be described as a new empty profile. `_reject_degenerate_root` normally
    stops that root at capture time, but permissions can change after it.

    Returns:
        A Rich-markup line, or `None` when the default profile is in use.
    """
    from deepagents_code._paths import PathState, classify_path

    if PATHS.uses_default_profile:
        return None
    root = _rich_path_display(PATHS.profile.root)
    state = classify_path(PATHS.profile.root)
    if state is PathState.EXISTS:
        return f"[dim]Using profile {root} (DEEPAGENTS_HOME)[/dim]"
    if state is PathState.UNREADABLE:
        return (
            f"[yellow]Note:[/yellow] the profile at {root} (DEEPAGENTS_HOME) "
            "exists but cannot be read. Check the permissions on it and on its "
            "parent directories."
        )
    return (
        f"[yellow]Note:[/yellow] creating a new empty profile at {root} "
        "(DEEPAGENTS_HOME). Existing settings and credentials live in a "
        "different profile."
    )


# [해설] 위 안내문을 stderr로 출력. 자체 예외 처리로 도구 경고 출력과 분리하고, Rich 실패 시 마크업 제거한 평문으로 폴백.
def _print_configured_profile_notice() -> None:
    """Print the configured-profile notice to stderr, if there is one.

    Wrapped in its own error handling rather than sharing the optional-tools
    `try`: a failure here must not be reported as "tool availability check
    skipped", and must not stop the tool warnings from printing.
    """
    try:
        notice = _configured_profile_notice()
    except Exception:
        # Nothing to fall back to: there is no notice text yet. Log and move
        # on rather than failing a launch over a diagnostic line.
        logger.warning("Could not build the profile notice", exc_info=True)
        return
    if notice is None:
        return
    try:
        from rich.console import Console as _Console

        _Console(stderr=True).print(notice)
    except Exception:
        logger.warning("Could not print the profile notice", exc_info=True)
        # The point of the notice is that a silent wrong profile looks like
        # lost settings, so fall back to plain stderr rather than dropping it.
        # Strip the markup from the notice already computed instead of writing
        # a different sentence: the "creating a new empty profile" case is the
        # one that matters most, and a fixed "Using profile" line would state
        # the opposite of it.
        with contextlib.suppress(Exception):
            from rich.markup import render

            sys.stderr.write(f"{render(notice).plain}\n")


# [해설] 비대화형 경고에 붙이는 "config.toml `[warnings].suppress`에 키 추가" 안내. `\\[`는 Rich 마크업 이스케이프.
def _suppress_hint_cli(key: str) -> str:
    """Return a configured-path suppression hint for non-interactive output.

    Args:
        key: Warning key to place in the example TOML.

    Returns:
        Rich-safe instructions for editing the effective user config.
    """
    config_path = _rich_path_display(PATHS.profile.config_file)
    return f'To suppress, edit {config_path}:\n\\[warnings]\nsuppress = \\["{key}"]'


# [해설] 플랫폼과 PATH에 있는 패키지 매니저(`shutil.which`)로 ripgrep 설치 명령을 추정. 없으면 cargo/conda, 최종은 URL.
def _ripgrep_install_hint() -> str:
    """Return a platform-specific install command for ripgrep.

    Falls back to the GitHub URL when the platform isn't recognized.
    """
    plat = sys.platform
    if plat == "darwin":
        if shutil.which("brew"):
            return "brew install ripgrep"
        if shutil.which("port"):
            return "sudo port install ripgrep"
    elif plat == "linux":
        if shutil.which("apt-get"):
            return "sudo apt-get install ripgrep"
        if shutil.which("dnf"):
            return "sudo dnf install ripgrep"
        if shutil.which("pacman"):
            return "sudo pacman -S ripgrep"
        if shutil.which("zypper"):
            return "sudo zypper install ripgrep"
        if shutil.which("apk"):
            return "sudo apk add ripgrep"
        if shutil.which("nix-env"):
            return "nix-env -iA nixpkgs.ripgrep"
    elif plat == "win32":
        if shutil.which("choco"):
            return "choco install ripgrep"
        if shutil.which("scoop"):
            return "scoop install ripgrep"
        if shutil.which("winget"):
            return "winget install BurntSushi.ripgrep"
    # Cross-platform fallbacks
    if shutil.which("cargo"):
        return "cargo install ripgrep"
    if shutil.which("conda"):
        return "conda install -c conda-forge ripgrep"
    return _RIPGREP_URL


# [해설] PATH의 `rg`가 dcode가 관리 설치한 바이너리(`managed_tools.managed_rg_path`)인지 경로 정규화 후 비교.
def _is_managed_ripgrep_path(path: str | None) -> bool:
    """Return whether `path` points at the managed `rg` binary."""
    if path is None:
        return False

    from deepagents_code.managed_tools import managed_rg_path

    managed = managed_rg_path()
    return os.path.normcase(str(Path(path).resolve())) == os.path.normcase(
        str(managed.resolve())
    )


# [해설] 시스템 `rg`가 없거나, 있어도 관리 바이너리일 때만 관리형 ripgrep 검증/설치 대상이 된다(사용자 설치 rg는 건드리지 않음).
def _should_ensure_managed_ripgrep() -> bool:
    """Return whether startup should validate or install managed ripgrep."""
    rg_path = shutil.which("rg")
    return rg_path is None or _is_managed_ripgrep_path(rg_path)


# [해설] 권장 외부 도구 누락 목록 반환: ripgrep(grep 도구 가속), tavily(웹 검색 API 키 = `credentials.has_tavily`).
# [해설] `[warnings].suppress`로 억제된 항목은 제외. 호출자: 헤드리스 경고 출력(`cli_main`)과 TUI 알림 생성.
def check_optional_tools(*, config_path: Path | None = None) -> list[str]:
    """Check for recommended external tools and return missing tool names.

    Skips tools that the user has suppressed via
    `[warnings].suppress` in `config.toml`.

    Args:
        config_path: Path to config file.

            Defaults to `~/.deepagents/config.toml`.

    Returns:
        List of missing tool names (e.g. `["ripgrep"]`).
    """
    from deepagents_code.model_config import is_warning_suppressed

    missing: list[str] = []
    if _should_ensure_managed_ripgrep() and not is_warning_suppressed(
        "ripgrep", config_path
    ):
        missing.append("ripgrep")

    from deepagents_code.config import credentials

    if not credentials.has_tavily and not is_warning_suppressed("tavily", config_path):
        missing.append("tavily")

    return missing


# [해설] 헤드리스 경로에서 관리형 ripgrep 1회 자동 설치(`managed_tools.ensure_ripgrep`). TUI의 `DeepAgentsApp._ensure_managed_ripgrep` 대응.
# [해설][주의] 다운로드 아카이브 SHA-256 불일치(`ChecksumMismatchError`)면 설치를 거부하고 오류를 크게 표시한다(공급망 방어).
# [해설] 성공 시 관리 bin 디렉터리를 PATH 앞에 붙인다 — 이후 띄울 서버 프로세스가 이 PATH를 상속한다(추정).
def _auto_install_ripgrep_cli(
    warn_console: "Console", missing_tools: list[str]
) -> list[str]:
    """Attempt the one-shot managed `rg` install for the headless CLI path.

    Mirrors the interactive `DeepAgentsApp._ensure_managed_ripgrep` flow for
    the non-interactive launch, where there is no Textual app to surface
    notices through. A checksum mismatch is reported loudly and a generic
    failure as a warning; both leave `"ripgrep"` in the returned list so the
    caller still prints the standard missing-tool notice and the slow Python
    fallback is used.

    Args:
        warn_console: `rich` console bound to stderr for user-facing notices.
        missing_tools: Tool names reported missing by `check_optional_tools`.

    Returns:
        `missing_tools` with `"ripgrep"` removed once a usable `rg` is
        resolved — the managed binary (with `BIN_DIR` prepended to `PATH`) or a
        system `rg` already on `PATH` — otherwise the list unchanged.
    """
    from deepagents_code.managed_tools import (
        ChecksumMismatchError,
        ManagedToolUnavailableError,
        ensure_ripgrep,
        managed_rg_path,
        prepend_managed_bin_to_path,
    )

    warn_console.print("Installing ripgrep...")
    try:
        installed = asyncio.run(ensure_ripgrep())
    except ChecksumMismatchError:
        logger.exception(
            "ripgrep auto-install aborted: SHA-256 mismatch on downloaded archive"
        )
        warn_console.print(
            "[bold red]Error:[/bold red] ripgrep auto-install aborted: downloaded "
            "archive failed SHA-256 verification. Refusing to install."
        )
        return missing_tools
    except ManagedToolUnavailableError as exc:
        logger.info("ripgrep auto-install unavailable: %s", exc.reason)
        warn_console.print(f"[yellow]Warning:[/yellow] {exc.message}")
        return missing_tools
    except Exception:
        logger.warning("ripgrep auto-install failed unexpectedly", exc_info=True)
        warn_console.print(
            "[yellow]Warning:[/yellow] ripgrep auto-install failed unexpectedly "
            "— see logs."
        )
        return missing_tools

    if installed is None:
        return missing_tools

    if installed == managed_rg_path():
        prepend_managed_bin_to_path()
    return [tool for tool in missing_tools if tool != "ripgrep"]


# [해설] TUI 알림 센터용 누락 도구 알림(`notifications.PendingNotification`) 생성. 설치 명령/URL을 payload에 담아
# [해설] 액션 핸들러(복사·웹 열기·API 키 입력·억제)가 플랫폼 감지를 다시 하지 않게 한다. 호출자: `app.py`(기동 시 누락 도구 알림 등록).
def build_missing_tool_notification(tool: str) -> "PendingNotification":
    """Build a `PendingNotification` for a missing optional tool.

    The returned entry carries the install hint (or URL) in a typed payload so
    the notification center action handler can copy it / open it without
    re-running platform detection.

    Args:
        tool: Name of the missing tool (e.g. `"ripgrep"`, `"tavily"`).

    Returns:
        A registry entry ready for `NotificationRegistry.add`.
    """
    # Deferred import: keeps `--version` and other hot-path commands off the
    # `deepagents_code.notifications` -> `dataclasses`/`logging` chain.
    from deepagents_code.notifications import (
        ActionId,
        MissingDepPayload,
        NotificationAction,
        PendingNotification,
    )

    suppress_action = NotificationAction(
        ActionId.SUPPRESS, "Don't show notification again"
    )
    if tool == "ripgrep":
        hint = _ripgrep_install_hint()
        if hint.startswith("http"):
            actions: tuple[NotificationAction, ...] = (
                NotificationAction(
                    ActionId.OPEN_WEBSITE, "Open installation guide", primary=True
                ),
                suppress_action,
            )
            payload = MissingDepPayload(tool="ripgrep", url=hint)
        else:
            actions = (
                NotificationAction(
                    ActionId.COPY_INSTALL, "Copy install command", primary=True
                ),
                NotificationAction(ActionId.OPEN_WEBSITE, "Open installation guide"),
                suppress_action,
            )
            payload = MissingDepPayload(
                tool="ripgrep", install_command=hint, url=_RIPGREP_URL
            )
        body = (
            "ripgrep is not installed; the grep tool will use a slower fallback.\n\n"
            f"Install: {hint}"
        )
        return PendingNotification(
            key="dep:ripgrep",
            title="ripgrep is not installed",
            body=body,
            actions=actions,
            payload=payload,
        )
    if tool == "tavily":
        return PendingNotification(
            key="dep:tavily",
            title="Web search disabled",
            body=("Add a Tavily API key to enable web search."),
            actions=(
                NotificationAction(
                    ActionId.ENTER_API_KEY, "Enter API key", primary=True
                ),
                NotificationAction(ActionId.OPEN_WEBSITE, "Open tavily.com"),
                suppress_action,
            ),
            payload=MissingDepPayload(tool="tavily", url="https://tavily.com"),
        )
    logger.warning("No install hint configured for tool %r", tool)
    return PendingNotification(
        key=f"dep:{tool}",
        title=f"{tool} is not installed",
        body=f"{tool} is not installed.",
        actions=(
            NotificationAction(
                ActionId.SUPPRESS, "Don't show notification again", primary=True
            ),
        ),
        payload=MissingDepPayload(tool=tool),
    )


# [해설] 비대화형 콘솔용 누락 도구 경고 문자열(Rich 마크업). `build_missing_tool_notification`의 CLI 버전.
def format_tool_warning_cli(tool: str) -> str:
    """Format a missing-tool warning for non-interactive console output.

    Args:
        tool: Name of the missing tool.

    Returns:
        Warning string suitable for `console.print`.
    """
    if tool == "ripgrep":
        hint = _ripgrep_install_hint()
        if hint.startswith("http"):
            hint = f"[link={hint}]{hint}[/link]"
        suppress = _suppress_hint_cli("ripgrep")
        return (
            "ripgrep is not installed; the grep tool will use a slower fallback.\n"
            f"Install: {hint}\n\n"
            f"{suppress}\n"
        )
    if tool == "tavily":
        url = "https://tavily.com"
        suppress = _suppress_hint_cli("tavily")
        return (
            "Web search is disabled \u2014 TAVILY_API_KEY is not set.\n"
            f"Get a key at [link={url}]{url}[/link]\n\n"
            f"{suppress}\n"
        )
    return f"{tool} is not installed."


# [해설] 서버 모드에서 실제 MCP 도구는 서버 프로세스가 만들지만, TUI 환영 배너와 `/mcp` 뷰어가 쓸 메타데이터를
# [해설] 클라이언트 프로세스에서 미리 로드하고 열었던 MCP 세션을 즉시 정리한다. 플러그인 MCP 설정도 합친다.
# [해설] 호출자: `app.py`(서버 기동과 병렬 사전 로드, 재시작/cwd 전환 시 재로드)와 `client/non_interactive.py`(헤드리스).
# [해설] 인자는 `run_textual_cli_async`가 만든 `mcp_preload_kwargs`. 관련: `analysis/07-mcp-hooks-extensions-plugins.md`.
async def _preload_session_mcp_server_info(
    *,
    mcp_config_path: str | None,
    no_mcp: bool,
    trust_project_mcp: bool | None,
) -> list["MCPServerInfo"] | None:
    """Load MCP metadata for the interactive TUI in server mode.

    In server mode the actual MCP tools are created inside the LangGraph server
    process, but the local Textual app still needs MCP metadata for the welcome
    banner and `/mcp` viewer. This preloads the metadata in the app process and
    immediately cleans up any temporary MCP sessions it opened.

    Args:
        mcp_config_path: Optional explicit MCP config path.
        no_mcp: Whether MCP loading is disabled.
        trust_project_mcp: Project-level MCP trust decision.

    Returns:
        MCP server metadata for the TUI, or `None` when MCP is disabled.
    """
    if no_mcp:
        return None

    from deepagents_code.mcp_tools import resolve_and_load_mcp_tools
    from deepagents_code.plugins.adapters.mcp import discover_plugin_mcp_configs
    from deepagents_code.project_utils import ProjectContext

    session_manager = None
    try:
        try:
            project_context = ProjectContext.from_user_cwd(Path.cwd())
        except OSError:
            logger.warning("Could not determine working directory for MCP preload")
            project_context = None
        project_dir = (
            project_context.project_root or project_context.user_cwd
            if project_context is not None
            else None
        )
        _tools, session_manager, server_info = await resolve_and_load_mcp_tools(
            explicit_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            project_context=project_context,
            additional_configs=discover_plugin_mcp_configs(project_dir=project_dir),
        )
        return server_info
    # [해설][주의] 메타데이터만 필요하므로 성공/실패와 무관하게 MCP 세션(stdio 서브프로세스 등)을 반드시 정리한다.
    finally:
        if session_manager is not None:
            try:
                await session_manager.cleanup()
            except Exception:
                logger.warning(
                    "MCP metadata preload cleanup failed",
                    exc_info=True,
                )


# [해설] 서브커맨드 그룹 이름 → (하위 서브커맨드 argparse dest, `deepagents_code.ui`의 도움말 함수명). `_show_bare_command_group_help`가 사용.
# [해설] 예: `dcode threads`만 치면 `threads_command`가 None이므로 `ui.show_threads_help()`를 무거운 부트스트랩 전에 출력.
_HELP_SPECS: dict[str, tuple[str | None, str]] = {
    "help": (None, "show_help"),
    "agents": ("agents_command", "show_agents_help"),
    "skills": ("skills_command", "show_skills_help"),
    "plugin": ("plugin_command", "show_plugins_help"),
    "plugins": ("plugin_command", "show_plugins_help"),
    "threads": ("threads_command", "show_threads_help"),
    "mcp": ("mcp_command", "show_mcp_help"),
    "auth": ("auth_command", "show_auth_help"),
    "tools": ("tools_command", "show_tools_help"),
}
"""Maps top-level command names to their startup-fast-path help dispatch.

Each value is `(subcommand_dest, ui_help_fn_name)`:

- `subcommand_dest` is the argparse `dest=` for the group's sub-subparsers,
    or `None` for leaf commands like `help`. When non-`None` and the parsed
    namespace has a value at that attribute, a real subcommand was given and
    the fast path declines.
- `ui_help_fn_name` is the attribute on `deepagents_code.ui` invoked to
    render the help screen.

Command groups whose bare invocation performs an action instead belong in
`_BARE_ACTION_GROUPS`. The drift test in
`tests/unit_tests/test_startup_fast_paths.py` requires every group to be in
exactly one collection.
"""

# [해설] 하위 명령 없이도 동작하는 그룹(`dcode config`는 설정 편집 동작). help fast path에서 제외된다.
_BARE_ACTION_GROUPS = frozenset({"config"})
"""Command groups that perform their primary action without a subcommand."""


# [해설] `dcode help`나 하위 명령 없는 그룹 호출이면 `console`/`settings` import 전에 도움말만 출력하고 True 반환(→ `cli_main`이 종료).
def _show_bare_command_group_help(args: argparse.Namespace) -> bool:
    """Render help for `help` and bare command groups before the heavy bootstrap.

    Short-circuits before `console`/`settings` are imported so help-only
    invocations stay snappy. Command groups in `_BARE_ACTION_GROUPS` bypass
    this path because their bare invocation has useful behavior.

    Args:
        args: Namespace from `parse_args()`. Only `command` and the per-group
            `<group>_command` attributes are read; both may be absent.

    Returns:
        `True` when help was rendered and the caller should exit; `False`
            when the command requires the full runtime path.
    """
    command = getattr(args, "command", None)
    if not isinstance(command, str) or command in _BARE_ACTION_GROUPS:
        return False
    spec = _HELP_SPECS.get(command)
    if spec is None:
        return False

    command_attr, help_fn_name = spec
    if command_attr is not None and getattr(args, command_attr, None) is not None:
        return False

    from deepagents_code import ui

    # 2-arg `getattr` is intentional: a missing/renamed `show_*_help` in
    # `ui.py` is a developer bug and should raise `AttributeError` loudly
    # rather than fall through to a silent no-op.
    getattr(ui, help_fn_name)()
    return True


# [해설] dcode 전체 argparse 트리 구성 및 파싱. 호출자: `cli_main`(모든 실행에서 1회).
# [해설] 구조: 서브커맨드(help/agents/skills/mcp/plugin/config/auth/threads/update/doctor/tools/install/uninstall)
# [해설] + 최상위 세션 플래그(-r/-a/-M/-n/-q/--sandbox/-S/--mcp-config/--acp 등). 서브커맨드 파서 일부는
# [해설] `client/commands/*`, `skills`, `plugins.commands_cli` 모듈의 `setup_*_parser`에 위임한다.
# [해설][설계] `-h`는 argparse 기본 대신 `deepagents_code.ui`의 Rich 도움말 함수로 연결(지연 import).
# [해설] 파싱 후 후처리: `--package`+uninstall 금지, extensions 플래그의 EXPERIMENTAL 게이트, 샌드박스 해석.
def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    from deepagents_code._constants import DEFAULT_AGENT_NAME
    from deepagents_code.client.commands.auth import setup_auth_parser
    from deepagents_code.client.commands.config import setup_config_parser
    from deepagents_code.client.commands.mcp import setup_mcp_parsers
    from deepagents_code.output import add_json_output_arg
    from deepagents_code.skills import setup_skills_parser

    # Factory that builds an argparse Action whose __call__ invokes the
    # supplied *help_fn* instead of argparse's default help text.  Each
    # subcommand can pass its own Rich-formatted help screen so that
    # `deepagents <subcommand> -h` shows context-specific help.
    # [해설] argparse는 custom action으로 "클래스"를 요구하므로, help_fn을 클로저로 캡처한 Action 클래스를 만들어 반환.
    # [해설] 서브커맨드 설정 함수들(`setup_skills_parser` 등)에도 인자로 넘겨 같은 방식의 -h를 쓰게 한다.
    def _make_help_action(
        help_fn: Callable[[], None],
    ) -> type[argparse.Action]:
        """Create an argparse Action that displays *help_fn* and exits.

        argparse requires a *class* (not a callable) for custom actions.
        This factory uses a closure: the returned `_ShowHelp` class captures
        *help_fn* from the enclosing scope so that each subcommand can wire `-h`
        to its own Rich help screen.

        Args:
            help_fn: Callable that prints help text to the console.

        Returns:
            An argparse Action class wired to the given help function.
        """

        class _ShowHelp(argparse.Action):
            def __init__(
                self,
                option_strings: list[str],
                dest: str = argparse.SUPPRESS,
                default: str = argparse.SUPPRESS,
                **kwargs: Any,
            ) -> None:
                super().__init__(
                    option_strings=option_strings,
                    dest=dest,
                    default=default,
                    nargs=0,
                    **kwargs,
                )

            def __call__(
                self,
                parser: argparse.ArgumentParser,
                namespace: argparse.Namespace,  # noqa: ARG002  # Required by argparse Action interface
                values: str | Sequence[Any] | None,  # noqa: ARG002  # Required by argparse Action interface
                option_string: str | None = None,  # noqa: ARG002  # Required by argparse Action interface
            ) -> None:
                with contextlib.suppress(BrokenPipeError):
                    help_fn()
                parser.exit()

        return _ShowHelp

    # Lazy wrapper: defers `ui` import until the help action fires (i.e.,
    # only when the user passes `-h`). This avoids pulling in Rich and config at
    # parse time for the common non-help path.
    # [해설] `ui` 모듈의 도움말 함수 "이름"만 받아 실제 호출 시점에 import하는 래퍼(-h를 쓰지 않는 일반 경로에서 Rich/config 로드 회피).
    def _lazy_help(fn_name: str) -> Callable[[], None]:
        def _show() -> None:
            from deepagents_code import ui

            getattr(ui, fn_name)()

        return _show

    # [해설] `-h/--help`만 가진 부모 파서를 만들어 `parents=`로 각 서브파서에 주입하는 헬퍼.
    def help_parent(help_fn: Callable[[], None]) -> list[argparse.ArgumentParser]:
        parent = argparse.ArgumentParser(add_help=False)
        parent.add_argument("-h", "--help", action=_make_help_action(help_fn))
        return [parent]

    # [해설][흐름] 1) 루트 파서와 서브커맨드 목록. 선택된 서브커맨드는 `args.command`에 저장되어 `cli_main`이 분기한다.
    parser = argparse.ArgumentParser(
        description=("Deep Agents - AI Coding Assistant"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser(
        "help",
        help="Show help information",
        add_help=False,
        parents=help_parent(_lazy_help("show_help")),
    )

    # [해설][흐름] 2) `dcode agents list|reset` — 에이전트 프로필(`DEEPAGENTS_HOME/<agent>`) 관리.
    agents_parser = subparsers.add_parser(
        "agents",
        help="Manage agents",
        add_help=False,
        parents=help_parent(_lazy_help("show_agents_help")),
    )
    add_json_output_arg(agents_parser)
    agents_sub = agents_parser.add_subparsers(dest="agents_command")

    agents_list = agents_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all agents",
        add_help=False,
        parents=help_parent(_lazy_help("show_list_help")),
    )
    add_json_output_arg(agents_list)

    agents_reset = agents_sub.add_parser(
        "reset",
        help="Reset an agent's prompt to default",
        add_help=False,
        parents=help_parent(_lazy_help("show_reset_help")),
    )
    add_json_output_arg(agents_reset)
    agents_reset.add_argument("--agent", required=True, help="Name of agent to reset")
    agents_reset.add_argument(
        "--target", dest="source_agent", help="Copy prompt from another agent"
    )
    agents_reset.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )

    # [해설][흐름] 3) skills/mcp/plugin/config/auth 서브커맨드는 각 도메인 모듈이 자기 파서를 등록한다.
    setup_skills_parser(
        subparsers,
        make_help_action=_make_help_action,
        add_output_args=add_json_output_arg,
    )

    setup_mcp_parsers(
        subparsers,
        make_help_action=_make_help_action,
    )

    from deepagents_code.plugins.commands_cli import setup_plugin_parser

    setup_plugin_parser(
        subparsers,
        make_help_action=_make_help_action,
        add_output_args=add_json_output_arg,
    )

    setup_config_parser(
        subparsers,
        make_help_action=_make_help_action,
        add_output_args=add_json_output_arg,
    )

    setup_auth_parser(
        subparsers,
        make_help_action=_make_help_action,
    )

    # [해설][흐름] 4) `dcode threads list|delete` — sessions.db(체크포인트) 기반 스레드 관리. 실행은 `sessions.list_threads_command` 등.
    threads_parser = subparsers.add_parser(
        "threads",
        help="Manage conversation threads",
        add_help=False,
        parents=help_parent(_lazy_help("show_threads_help")),
    )
    add_json_output_arg(threads_parser)
    threads_sub = threads_parser.add_subparsers(dest="threads_command")

    threads_list = threads_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List threads",
        add_help=False,
        parents=help_parent(_lazy_help("show_threads_list_help")),
    )
    add_json_output_arg(threads_list)
    threads_list.add_argument(
        "--agent", default=None, help="Filter by agent name (default: show all)"
    )
    # [해설] `-n/--limit` 기본 None → 실제 기본값 20과 env `DEEPAGENTS_CODE_RECENT_THREADS`는 `sessions` 쪽에서 해석(analysis 01 참고).
    threads_list.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Max number of threads to display (default: 20)",
    )
    threads_list.add_argument(
        "--sort",
        choices=["created", "updated"],
        default=None,
        help="Sort threads by timestamp (default: from config, or updated)",
    )
    threads_list.add_argument(
        "--branch",
        default=None,
        help="Filter by git branch name",
    )
    threads_list.add_argument(
        "--cwd",
        nargs="?",
        const="",
        default=None,
        help=(
            "Filter by working directory. With no value, uses the current "
            "directory; pass a path to filter by that directory instead."
        ),
    )
    threads_list.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show all columns (branch, created, prompt)",
    )
    threads_list.add_argument(
        "-r",
        "--relative",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show timestamps as relative time (default: from config, or absolute)",
    )
    threads_delete = threads_sub.add_parser(
        "delete",
        help="Delete a thread",
        add_help=False,
        parents=help_parent(_lazy_help("show_threads_delete_help")),
    )
    add_json_output_arg(threads_delete)
    threads_delete.add_argument("thread_id", help="Thread ID to delete")
    threads_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )

    # [해설][흐름] 5) 세션 없이 실행되는 유지보수 명령: update, doctor(오프라인 진단), tools(관리형 ripgrep), install/uninstall(extra).
    update_parser = subparsers.add_parser(
        "update",
        help="Check for and install updates",
        add_help=False,
        parents=help_parent(_lazy_help("show_update_help")),
    )
    update_parser.add_argument(
        "--prerelease",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Include alpha/beta/rc releases when checking for updates",
    )
    add_json_output_arg(update_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Print install health and diagnostics",
        add_help=False,
        parents=help_parent(_lazy_help("show_doctor_help")),
    )
    add_json_output_arg(doctor_parser)

    tools_parser = subparsers.add_parser(
        "tools",
        help="Manage managed external tools (e.g. ripgrep)",
        add_help=False,
        parents=help_parent(_lazy_help("show_tools_help")),
    )
    add_json_output_arg(tools_parser)
    tools_sub = tools_parser.add_subparsers(dest="tools_command")

    tools_install = tools_sub.add_parser(
        "install",
        help="Install or repair the managed ripgrep binary",
        add_help=False,
        parents=help_parent(_lazy_help("show_tools_install_help")),
    )
    add_json_output_arg(tools_install)

    tools_list = tools_sub.add_parser(
        "list",
        help="List the tools available to the agent",
        add_help=False,
        parents=help_parent(_lazy_help("show_tools_list_help")),
    )
    add_json_output_arg(tools_list)

    install_parser = subparsers.add_parser(
        "install",
        help="Install an optional extra (e.g. daytona, fireworks)",
        add_help=False,
        parents=help_parent(_lazy_help("show_install_help")),
    )
    install_parser.add_argument(
        "install_target",
        nargs="?",
        default=None,
        metavar="NAME",
        help="Extra name, or package name when --package is set",
    )
    install_parser.add_argument(
        "--package",
        dest="install_package",
        action="store_true",
        help=(
            "Treat NAME as a package added via `uv --with` "
            "(for a custom provider package), not a deepagents-code extra"
        ),
    )
    install_parser.add_argument(
        "--yes",
        dest="install_yes",
        action="store_true",
        help="Skip interactive confirmation prompts",
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove an installed optional extra",
        add_help=False,
        parents=help_parent(_lazy_help("show_uninstall_help")),
    )
    uninstall_parser.add_argument(
        "uninstall_target",
        nargs="?",
        default=None,
        metavar="NAME",
        help="Installed optional extra to remove",
    )

    # [해설][흐름] 6) 서브커맨드 없이 쓰는 최상위 세션 플래그들.
    # [해설] `-r`만 주면 const `"__MOST_RECENT__"`가 저장된다. 실제 스레드 ID 해석은 TUI 내부 `_resolve_resume_thread`(app.py)가 비동기로 한다.
    # Default interactive mode — argument order here determines the
    # usage line printed by argparse; keep in sync with ui.show_help().
    parser.add_argument(
        "-r",
        "--resume",
        dest="resume_thread",
        nargs="?",
        const="__MOST_RECENT__",
        default=None,
        metavar="ID",
        help="Resume thread: -r for most recent, -r <ID> for specific thread",
    )

    parser.add_argument(
        "-a",
        "--agent",
        default=None,
        metavar="NAME",
        help=(
            "Agent to use. "
            "If omitted, falls back to [agents].default, then "
            "[agents].recent, then "
            f"the '{DEFAULT_AGENT_NAME}' built-in default."
        ),
    )

    # [해설] 모델 관련 플래그(-M, --model-params, --summarization-model, --max-retries, --profile-override, --auto-classifier-model,
    # [해설] --default-model). JSON 문자열 검증은 `cli_main`에서 수행. 관련: `analysis/03-config-models-credentials.md`.
    parser.add_argument(
        "-M",
        "--model",
        metavar="MODEL",
        help="Model to use (e.g., claude-opus-4-7, gpt-5.5). "
        "Provider is auto-detected from model name.",
    )

    parser.add_argument(
        "--model-params",
        metavar="JSON",
        help="Extra kwargs to pass to the model as a JSON string "
        '(e.g., \'{"temperature": 0.7, "max_tokens": 4096}\'). '
        "These take priority, overriding config file values.",
    )

    parser.add_argument(
        "--summarization-model",
        metavar="MODEL",
        help="Model to use for context-compaction summaries. Falls back to "
        "[models].summarization_default, then the main agent model.",
    )

    from deepagents_code.ui import non_negative_int, positive_int, shell_allow_list_arg

    parser.add_argument(
        "--max-retries",
        type=non_negative_int,
        default=None,
        metavar="N",
        help=(
            "Retries after a failed model request; 0 disables them. "
            "Overrides [retries] in config.toml."
        ),
    )

    parser.add_argument(
        "--profile-override",
        metavar="JSON",
        help="Override model profile fields as a JSON string "
        "(e.g., '{\"max_input_tokens\": 4096}'). "
        "Merged on top of config file profile overrides.",
    )

    parser.add_argument(
        "--auto-classifier-model",
        dest="auto_classifier_model",
        metavar="MODEL",
        help="Model the Auto approval classifier reviews actions with "
        "(e.g. anthropic:claude-sonnet-5). Local TUI or ACP only. Defaults to "
        "DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL, then [models].auto_classifier, "
        "then a provider-specific model or the main agent model. A weaker model "
        "weakens Auto's review.",
    )

    parser.add_argument(
        "--default-model",
        metavar="MODEL",
        nargs="?",
        const="__SHOW__",
        default=None,
        help="Set the default model for future launches "
        "(e.g., anthropic:claude-opus-4-6). "
        "Use --default-model with no argument to show the current default. "
        "Use --clear-default-model to remove it.",
    )

    parser.add_argument(
        "--clear-default-model",
        action="store_true",
        help="Clear the default model, falling back to recent model "
        "or environment auto-detection.",
    )

    # [해설] 초기 입력 계열: `-m`(인터랙티브 자동 제출), `-s`(스킬 호출), `--startup-cmd`(첫 프롬프트 전 셸 명령).
    parser.add_argument(
        "-m",
        "--message",
        dest="initial_prompt",
        metavar="TEXT",
        help="Initial prompt to auto-submit when session starts",
    )

    parser.add_argument(
        "-s",
        "--skill",
        dest="initial_skill",
        metavar="NAME",
        help="Invoke a skill when the interactive session starts",
    )

    parser.add_argument(
        "--startup-cmd",
        dest="startup_cmd",
        metavar="CMD",
        help="Shell command to run at startup, before the first prompt "
        "(output shown, non-zero exit warns but does not abort)",
    )

    # [해설] `-n`: 헤드리스 1회 실행. `args.non_interactive_message`가 채워지면 `cli_main`이 `run_non_interactive`로 분기한다.
    # [해설] 파이프 stdin도 `apply_stdin_pipe`가 이 필드를 채워 헤드리스로 만든다.
    parser.add_argument(
        "-n",
        "--non-interactive",
        dest="non_interactive_message",
        metavar="TEXT",
        help="Run a single task non-interactively and exit "
        "(shell disabled unless --shell-allow-list is set)",
    )

    # [해설] 아래 -q/--no-stream/--max-turns/--timeout/--rubric*은 헤드리스 전용이며 `cli_main`이 -n 없이 쓰면 exit 2로 거부한다.
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Clean output for piping — only the agent's response "
        "goes to stdout. Requires -n or piped stdin.",
    )

    parser.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        help="Buffer the full response and write it to stdout at once "
        "instead of streaming token-by-token. Requires -n or piped stdin.",
    )

    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        default=None,
        help="Show provider-visible reasoning (off by default).",
    )

    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=positive_int,
        metavar="N",
        help="Maximum number of agentic turns before stopping (must be >= 1). "
        "Overrides the internal safety default. Useful for CI/CD pipelines "
        "to prevent runaway agents. Requires -n or piped stdin.",
    )

    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=positive_int,
        metavar="SECONDS",
        help="Hard wall-clock timeout in seconds. The agent is cancelled and "
        "the process exits with code 124 if the timeout is reached. "
        "Complements --max-turns (turn count) with a time-based limit; both "
        "use exit code 124 on expiry. Requires -n or piped stdin.",
    )

    # [해설] `--goal`은 인터랙티브 전용(수락 기준 초안 리뷰 프롬프트), `--rubric`은 헤드리스 전용. 관련: `analysis/05-subagents-goals-rubrics.md`.
    parser.add_argument(
        "--goal",
        dest="goal",
        metavar="TEXT",
        help="Goal objective to turn into acceptance criteria. Opens a review "
        "prompt on interactive launch, then runs the accepted goal as the first "
        "task.",
    )
    parser.add_argument(
        "--rubric",
        dest="rubric",
        metavar="TEXT|@PATH",
        help="Acceptance criteria the agent self-evaluates against, looping "
        "until satisfied. Accepts literal text or '@path' to read a file "
        "(relative to the current working directory; '~' supported). "
        f"Limited to {RUBRIC_CHAR_LIMIT:,} characters. "
        "Requires -n or piped stdin.",
    )
    parser.add_argument(
        "--rubric-model",
        dest="rubric_model",
        metavar="MODEL",
        help="Model the rubric grader uses (e.g. anthropic:claude-sonnet-4-6). "
        "Defaults to the main agent model.",
    )
    parser.add_argument(
        "--rubric-max-iterations",
        dest="rubric_max_iterations",
        type=positive_int,
        metavar="N",
        help="Override grader iterations per rubric attempt before stopping "
        "(must be >= 1; defaults to the SDK setting).",
    )
    # [해설] `--recursion-limit`는 `_resolved_recursion_limit`로 해석되어 서버 설정(`ServerConfig`)으로 전달된다.
    parser.add_argument(
        "--recursion-limit",
        dest="recursion_limit",
        type=positive_int,
        metavar="N",
        help="Override the main agent's LangGraph recursion_limit (graph step "
        "budget; must be >= 1). Overrides DEEPAGENTS_CODE_RECURSION_LIMIT and "
        "[runtime].recursion_limit.",
    )

    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read input from stdin explicitly (instead of auto-detection)",
    )

    add_json_output_arg(parser, default="text")

    # [해설][흐름] 7) 승인 모드: `-y`(분류기 기반 Auto)와 `--yolo`(무승인)는 상호 배타. 헤드리스에서는 경고 후 무시된다.
    # [해설] 관련: `analysis/04-approval-hitl-security.md`.
    approval_group = parser.add_mutually_exclusive_group()
    approval_group.add_argument(
        "-y",
        "--auto-approve",
        action="store_true",
        default=None,
        help=(
            "Enable classifier-backed Auto mode in the local TUI or ACP server; "
            "ignored with a warning in headless mode."
        ),
    )
    approval_group.add_argument(
        "--yolo",
        action="store_true",
        help=(
            "Run gated actions without review after the one-time local risk "
            "acknowledgement (interactive TUI or ACP mode); ignored with a "
            "warning in headless mode."
        ),
    )

    # [해설][흐름] 8) 원격 샌드박스 플래그. 값 없는 `--sandbox`는 `_SANDBOX_DEFAULT_SENTINEL`로 저장 후 `_resolve_and_validate_sandbox`가 치환.
    # [해설][주의] nargs="?"라서 `dcode --sandbox agents`처럼 뒤에 서브커맨드가 오면 서브커맨드가 값으로 먹힌다. 관련: `analysis/08-sandboxes-execution.md`.
    parser.add_argument(
        "--sandbox",
        nargs="?",
        const=_SANDBOX_DEFAULT_SENTINEL,
        default="none",
        metavar="TYPE",
        help=(
            "Remote sandbox for code execution (default: none - local only). "
            "Built-ins: agentcore, daytona, langsmith, modal, runloop, vercel. "
            "Third-party and config-declared providers are also accepted. "
            "Pass --sandbox with no value to use [sandboxes].default from "
            "config (keep the bare form last on the command line so a "
            "following subcommand isn't read as its value). langsmith is "
            "bundled; others require installing an extra or package."
        ),
    )

    parser.add_argument(
        "--sandbox-id",
        metavar="ID",
        help="Existing sandbox ID to attach to",
    )

    parser.add_argument(
        "--sandbox-snapshot-name",
        metavar="NAME",
        help="Snapshot (langsmith) or blueprint (runloop) name to use or create",
    )

    parser.add_argument(
        "--sandbox-setup",
        metavar="PATH",
        help="Path to setup script to run in sandbox after creation",
    )
    # [해설][흐름] 9) 도구·확장 신뢰 플래그: -S(shell allow-list), MCP(--mcp-config/--no-mcp/--trust-project-mcp),
    # [해설] hooks(--trust-project-hooks), extensions(-e/--trust-project-extensions, EXPERIMENTAL 필요), 인터프리터, fs 도구 허용 목록.
    parser.add_argument(
        "-S",
        "--shell-allow-list",
        type=shell_allow_list_arg,
        metavar="LIST",
        help="Comma-separated list of shell commands to auto-approve, "
        "'recommended' for safe defaults, or 'all' to allow any command. "
        "Applies to both -n and interactive modes. Managed config overrides it.",
    )
    parser.add_argument(
        "--mcp-config",
        help="Path to MCP servers JSON configuration file (Claude Desktop format). "
        "Merged on top of auto-discovered configs (highest precedence). "
        "Run `dcode mcp config` to see discovery paths.",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Disable all MCP tool loading (skip auto-discovery and explicit config)",
    )
    parser.add_argument(
        "--trust-project-mcp",
        action="store_true",
        help="Trust project-level MCP configs with stdio and remote servers "
        "(skip interactive approval prompt)",
    )
    parser.add_argument(
        "--trust-project-hooks",
        action="store_true",
        help="Trust project-level `.deepagents/hooks.json` command handlers "
        "(required for headless/CI runs that should load repository hooks)",
    )
    parser.add_argument(
        "--trust-project-extensions",
        action="store_true",
        help="Trust project-level `.deepagents/extensions/` Python extensions "
        "(requires DEEPAGENTS_CODE_EXPERIMENTAL=1)",
    )
    parser.add_argument(
        "-e",
        "--extension",
        action="append",
        default=[],
        metavar="PATH",
        help="Load an extension file or directory for this run; requires "
        "DEEPAGENTS_CODE_EXPERIMENTAL=1 (repeatable)",
    )
    parser.add_argument(
        "--interpreter",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable the JS interpreter (`js_eval`) middleware on the main agent. "
        "Enabled by default when not using a sandbox; use --no-interpreter to disable.",
    )
    parser.add_argument(
        "--interpreter-tools",
        dest="interpreter_tools",
        metavar="VALUE",
        help="PTC allowlist for `js_eval`: 'safe', 'all', or a comma-separated "
        "list of tool names (which may include the 'safe' preset, e.g. "
        "'safe,task'). Default is 'safe' (read-only file tools).",
    )
    parser.add_argument(
        "--allow-fs-tools",
        dest="allow_fs_tools",
        metavar="LIST",
        help="Allowlist of filesystem tools to expose to the agent: 'all', or "
        "a comma-separated list of tool names (ls, read_file, write_file, "
        "edit_file, delete, glob, grep, execute). 'read_file' must be "
        "included in an explicit list. Note 'execute' is the shell tool: "
        "omitting it from the list removes shell access even if shell is "
        "otherwise enabled. Default is 'all'.",
    )

    # [해설][흐름] 10) 플래그 형태의 유지보수 동작(--update/--auto-update/--install/--uninstall)과 `--acp`(stdio ACP 서버 모드).
    parser.add_argument(
        "--update",
        action="store_true",
        help="Check for and install updates, then exit",
    )
    parser.add_argument(
        "--prerelease",
        action="store_true",
        help="With --update, include alpha/beta/rc releases",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        help="Toggle automatic updates on or off, then exit",
    )
    extra_group = parser.add_mutually_exclusive_group()
    extra_group.add_argument(
        "--install",
        metavar="NAME",
        help=(
            "Alias for `install NAME`. Install an optional extra "
            "(e.g. daytona, fireworks), then exit"
        ),
    )
    extra_group.add_argument(
        "--uninstall",
        metavar="NAME",
        help=(
            "Alias for `uninstall NAME`. Remove an installed optional extra, then exit"
        ),
    )
    parser.add_argument(
        "--package",
        action="store_true",
        help=(
            "With --install or `install`, treat NAME as a package added via "
            "`uv --with` (for a custom provider package), not a "
            "deepagents-code extra"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=("Skip interactive confirmation prompts (e.g., for --install / install)"),
    )
    parser.add_argument(
        "--acp",
        action="store_true",
        help="Run as an ACP server over stdio instead of launching the Textual UI",
    )

    # [해설][흐름] 11) `-v`가 실제로 있을 때만 무거운 버전 리포트를 만든다(argparse `version=`은 값이 필수라 평소엔 자리표시자).
    # `parse_args` runs on every invocation; keep the import-heavy metadata
    # scan off the hot path unless the user explicitly asked for --version.
    if any(arg in {"-v", "--version"} for arg in sys.argv[1:]):
        version_text = build_version_text()
    else:
        # Never surfaced: argparse only emits `version=` when the flag is
        # actually passed, which takes the `build_version_text()` branch above.
        # This placeholder only exists because `version=` requires a value.
        version_text = f"deepagents-code {__version__}"
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=version_text,
    )
    parser.add_argument(
        "-h",
        "--help",
        action=_make_help_action(_lazy_help("show_help")),
    )

    # [해설][흐름] 12) 실제 파싱과 교차 검증. `parser.error`는 exit 2.
    args = parser.parse_args()
    if args.package and (args.uninstall is not None or args.command == "uninstall"):
        parser.error("--package cannot be used with uninstall")

    # [해설][주의] 프로젝트 Python 확장은 임의 코드 실행이므로 `DEEPAGENTS_CODE_EXPERIMENTAL=1`일 때만 플래그를 허용한다.
    from deepagents_code._env_vars import EXPERIMENTAL, is_env_truthy

    if (
        getattr(args, "trust_project_extensions", False)
        or getattr(args, "extension", ())
    ) and not is_env_truthy(EXPERIMENTAL):
        parser.error(
            "--extension and --trust-project-extensions require "
            "DEEPAGENTS_CODE_EXPERIMENTAL=1"
        )
    # `--auto-classifier-model ""` yields the empty string, not `None`. Keep it
    # distinct from an absent flag (just trimmed): an explicit blank is the
    # "inherit the main agent model" instruction and must override a classifier
    # configured via env var or `config.toml`, which collapsing it to `None`
    # here would silently re-enable.
    if getattr(args, "auto_classifier_model", None) is not None:
        args.auto_classifier_model = args.auto_classifier_model.strip()
    _resolve_and_validate_sandbox(args, parser)
    return args


# [해설] `--sandbox` 값을 `integrations.sandbox_registry.SandboxRegistry`(내장+서드파티+config 선언 제공자)로 검증.
# [해설] 값 없는 형태는 `[sandboxes].default`로 치환하고, 제공자 메타데이터로 `--sandbox-snapshot-name`/`--sandbox-id` 지원 여부를 확인.
# [해설] 호출자: `parse_args` 마지막. 오류는 `parser.error`(exit 2). config 파싱 오류가 있으면 오류 메시지에 단서를 붙인다.
def _resolve_and_validate_sandbox(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> None:
    """Resolve `--sandbox` against the registry and validate related flags.

    Handles the bare `--sandbox` form (resolve `[sandboxes].default`), unknown
    providers (with install/config guidance), and the `--sandbox-snapshot-name`
    / `--sandbox-id` flags whose support is driven by provider metadata. Calls
    `parser.error` (which exits) on invalid input.

    Because `--sandbox` takes an optional value (`nargs="?"`), placing it
    immediately before a subcommand (e.g. `dcode --sandbox agents`) makes
    argparse consume the subcommand as the flag's value. Pass an explicit
    provider (`--sandbox daytona`) or keep the bare form last on the command
    line.

    Args:
        args: Parsed namespace; `args.sandbox` is normalized in place.
        parser: The parser, used to emit errors.
    """
    # [해설][흐름] 1) 로컬 모드(`none`)면 샌드박스 전용 보조 플래그 사용을 금지하고 종료.
    if args.sandbox in {"none", None}:
        if args.sandbox_snapshot_name is not None:
            parser.error("--sandbox-snapshot-name requires a --sandbox provider")
        if args.sandbox_id is not None:
            parser.error("--sandbox-id requires a --sandbox provider")
        return

    from deepagents_code.integrations.sandbox_registry import SandboxRegistry

    registry = SandboxRegistry.load()
    config_display = PATHS.display(PATHS.profile.config_file)

    def _config_note() -> str:
        """Build a breadcrumb when the config file failed to parse.

        Returns:
            A note to append to an error message, or an empty string when the
            config parsed cleanly.
        """
        if registry.config_error:
            return (
                f"\n\nNote: {config_display} could not be used "
                f"({registry.config_error}); any providers or default it "
                "declares were ignored."
            )
        return ""

    # [해설][흐름] 2) 값 없는 `--sandbox` → config 기본 제공자로 치환. 3) 등록되지 않은 제공자 거부. 4) 보조 플래그 지원 여부 검증.
    if args.sandbox == _SANDBOX_DEFAULT_SENTINEL:
        default = registry.default
        if not default:
            parser.error(
                "--sandbox was given with no value but no [sandboxes].default "
                f"is configured in {config_display}. Pass a provider "
                "name explicitly or set [sandboxes].default." + _config_note()
            )
        args.sandbox = default

    if not registry.is_available(args.sandbox):
        available = ", ".join(registry.available_providers())
        parser.error(
            f"Unknown sandbox provider '{args.sandbox}'.\n"
            f"Available providers: {available}.\n\n"
            "If this is a third-party provider, install the package that "
            "publishes it and re-run:\n"
            "  /install <package-name> --package\n"
            f"or declare [sandboxes.providers.{args.sandbox}] in "
            f"{config_display}." + _config_note()
        )

    metadata = registry.get_metadata(args.sandbox)
    if args.sandbox_snapshot_name is not None and (
        metadata is None or not metadata.supports_snapshot_name
    ):
        parser.error(
            f"--sandbox-snapshot-name is not supported by provider '{args.sandbox}'"
        )
    if (
        args.sandbox_id is not None
        and metadata is not None
        and not metadata.supports_sandbox_id
    ):
        parser.error(f"--sandbox-id is not supported by provider '{args.sandbox}'")


# [해설] Auto 승인 분류기 모델이 `models.allowed` 정책에 막히면 "메인 모델 상속"(`INHERIT_CLASSIFIER_MODEL`)으로 낮춘다.
# [해설][설계] 다른 문제는 경고만 하고 넘기지만, 정책 차단 spec은 `create_cli_agent`가 예외를 내 세션이 죽으므로 대체한다.
def _classifier_model_after_policy(spec: str | None) -> str | None:
    """Downgrade a policy-blocked classifier spec to "inherit the runtime model".

    The other problems `_auto_classifier_spec_problem` detects are advisory:
    launch proceeds and the Auto middleware fails closed per-action. A blocked
    spec is different -- `create_cli_agent` raises on it -- so forwarding it
    after printing a warning would kill the session the warning was about. The
    runtime model has already been checked against the same policy, making it a
    safe fallback.

    Args:
        spec: The resolved classifier spec, the inherit sentinel, or `None`.

    Returns:
        `spec` unchanged when it is usable, otherwise `INHERIT_CLASSIFIER_MODEL`.
    """
    from deepagents_code._cli_context import INHERIT_CLASSIFIER_MODEL
    from deepagents_code.model_config import ModelConfig

    if spec is None or spec == INHERIT_CLASSIFIER_MODEL:
        return spec
    # Canonicalize for the same reason `_auto_classifier_spec_problem` does, and
    # so this decision cannot disagree with the warning the user just saw.
    if ModelConfig.load().policy_error(spec, canonicalize=True) is None:
        return spec
    return INHERIT_CLASSIFIER_MODEL


# [해설] 기동 시 Auto 분류기 spec의 저비용 사전 점검: (1) 정책 차단 (2) 제공자 식별 불가 (3) 자격 증명 누락.
# [해설][설계] `create_model`을 호출하지 않아 LangChain import를 기동 경로에서 뺀다. 나머지 오류는 미들웨어의 fail-closed(거부)에 맡긴다.
def _auto_classifier_spec_problem(spec: str) -> str | None:
    """Return why a configured Auto classifier spec looks unusable, if it does.

    `/auto model` validates by constructing the model and refuses a spec it
    cannot build. The startup surfaces — `--auto-classifier-model`, the env var,
    `[models].auto_classifier` — reached the middleware unchecked, so a typo was
    announced as the active reviewer and only surfaced as a denied tool call
    mid-turn.

    This is the cheap half of what the slash command does: check the spec
    against `models.allowed`, then parse it and check the provider's
    credentials. It deliberately does not call `create_model`, which would pull
    LangChain onto the launch path (see `AGENTS.md`), so it catches the three
    common mistakes — a policy-blocked model, an unknown provider, and a
    missing credential — and leaves the rest to the middleware's fail-closed
    path. The policy check runs first: it is the only one whose failure the
    caller must act on rather than merely report, because a blocked classifier
    would otherwise abort agent construction.

    Args:
        spec: Resolved `provider:model` specification.

    Returns:
        A one-line problem description, or `None` when the spec looks usable.
    """
    from deepagents_code.config import detect_provider
    from deepagents_code.model_config import (
        ModelConfig,
        ModelSpec,
        get_provider_auth_status,
    )

    # `canonicalize` keeps this in step with `create_model`: a bare name whose
    # provider can be inferred must not be reported as blocked here when
    # construction would infer the same provider and allow it.
    blocked = ModelConfig.load().policy_error(spec, canonicalize=True)
    if blocked is not None:
        return str(blocked)

    parsed = ModelSpec.try_parse(spec)
    provider = parsed.provider if parsed else detect_provider(spec)
    if not provider:
        return (
            f"Auto classifier model {spec!r} has no recognizable provider; "
            "Auto will deny gated actions until it is fixed. Use "
            "provider:model, e.g. anthropic:claude-haiku-4-5."
        )
    try:
        status = get_provider_auth_status(provider)
    except Exception:
        # Advisory check only — a probe failure must never block startup.
        logger.debug("Auto classifier provider auth probe failed", exc_info=True)
        return None
    if status.blocks_start:
        env_var = f" Set {status.env_var}." if status.env_var else ""
        return (
            f"Auto classifier model {spec!r} has no credentials for provider "
            f"{provider!r}; Auto will deny gated actions until it is "
            f"fixed.{env_var}"
        )
    return None


# [해설] 컨텍스트 압축(summarization)용 모델 spec 결정: `--summarization-model` > `[models].summarization_default` > None(메인 모델).
# [해설] 모델을 만들지 않으므로 잘못된 spec은 첫 압축 시점에 드러난다(`_LazySummaryModel`이 메인 모델로 강등).
def _resolve_summarization_model(spec: str | None) -> str | None:
    """Resolve the invocation override before entering a supported launch mode.

    Deliberately does not build the model: an invalid spec surfaces at the
    first compaction rather than at launch, which keeps `create_model`'s
    provider imports off the pre-first-paint path. `_LazySummaryModel` degrades
    to the main model and logs, so a bad spec cannot wedge the session.

    Args:
        spec: The `--summarization-model` value, or `None` when unset. A blank
            string overrides the configured default with "use the main model".

    Returns:
        The explicit spec, configured default, or `None` to reuse the main model.
    """
    if spec is not None:
        return spec
    from deepagents_code.model_config import ModelConfig

    return ModelConfig.load().summarization_default_model


# [해설] 인터랙티브 TUI 진입 준비(TUI 경계 직전). 호출자: `cli_main`의 인터랙티브 분기(`asyncio.run`).
# [해설] 여기서는 모델 spec 표시명만 가볍게 해석하고, 서버 기동에 필요한 `server_kwargs`·MCP 사전 로드 인자·지연 모델 생성 인자를
# [해설] 만들어 `app.run_textual_app`에 넘긴다. 실제 서버 기동(`start_server_and_get_agent`)은 TUI의 백그라운드 워커가 수행.
# [해설] 반환: `app.AppResult`(return_code, 최종 thread_id). 관련: `analysis/01-boot-client-server.md` 흐름 B, `analysis/09-tui-app-commands-acp.md`.
async def run_textual_cli_async(
    assistant_id: str,
    *,
    approval_mode: "ApprovalMode | str" = "manual",
    auto_approve: bool | None = None,
    sandbox_type: str = "none",  # str (not None) to match argparse choices
    sandbox_id: str | None = None,
    sandbox_snapshot_name: str | None = None,
    sandbox_setup: str | None = None,
    model_name: str | None = None,
    model_params: dict[str, Any] | None = None,
    summarization_model: str | None = None,
    cli_max_retries: int | None = None,
    profile_override: dict[str, Any] | None = None,
    thread_id: str | None = None,
    resume_thread: str | None = None,
    initial_prompt: str | None = None,
    initial_skill: str | None = None,
    initial_goal: str | None = None,
    startup_cmd: str | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    hook_trust: "WorkspaceTrust | None" = None,
    trust_project_extensions: bool = False,
    extension_paths: tuple[str, ...] = (),
    enable_interpreter: bool | None = None,
    interpreter_arg: bool | None = None,
    interpreter_ptc: str | list[str] | None = None,
    interpreter_ptc_acknowledge_unsafe: bool = False,
    allow_fs_tools: "list[FsToolName] | None" = None,
    auto_classifier_model: str | None = None,
    recursion_limit: int | None = None,
) -> "AppResult":
    """Run the Textual TUI interface (async version).

    Starts a LangGraph server in a subprocess and connects the TUI to it via the
    `langgraph-sdk` client.

    Args:
        assistant_id: Agent identifier for memory storage.
        approval_mode: Initial `manual`, `auto`, or `yolo` mode.
        auto_approve: Compatibility input for callers using the previous Boolean
            API. `True` maps to unrestricted `yolo`.
        sandbox_type: Type of sandbox
            ("none", "agentcore", "modal", "runloop", "daytona", "langsmith")
        sandbox_id: Optional existing sandbox ID to reuse.
        sandbox_snapshot_name: Snapshot (langsmith) or blueprint (runloop) name.
        sandbox_setup: Optional path to setup script to run in the sandbox
            after creation.
        model_name: Optional model name to use
        model_params: Extra kwargs from `--model-params` to pass to the model.

            These override config file values.
        summarization_model: Model spec used only for context-compaction
            summaries, already resolved by `_resolve_summarization_model`.

            `None` reuses the effective main agent model, as does the blank
            string a valueless `--summarization-model` produces.
        cli_max_retries: Explicit `--max-retries` value.
        profile_override: Extra profile fields from `--profile-override`.

            Merged on top of config file profile overrides.
        thread_id: Thread ID for the session.

            `None` when `resume_thread` is provided (the TUI resolves the final
            ID asynchronously).
        resume_thread: Raw resume intent from `-r` flag.

            `'__MOST_RECENT__'` for bare `-r`, a thread ID string for `-r <id>`,
            or `None` for new sessions.

            Resolved asynchronously inside the TUI.
        initial_prompt: Optional prompt to auto-submit when session starts
        initial_skill: Optional skill name to invoke when the session starts.
        initial_goal: Optional goal objective to draft criteria for when the
            session starts.
        startup_cmd: Shell command to run at startup before the first prompt.

            Output is rendered in the transcript; non-zero exits warn but
            do not abort the session.
        mcp_config_path: Optional path to MCP servers JSON configuration file.

            Merged on top of auto-discovered configs (highest precedence).
        no_mcp: Disable all MCP tool loading.
        trust_project_mcp: Controls project-level server trust (stdio and
            remote alike).

            `True` to allow, `False` to deny, `None` to fall back to the
            user's per-server scoped approvals (equivalent to `False` for the
            whole-config decision).
        hook_trust: Policy deciding which workspaces may run project-scoped hook
            commands. `None` consults only the persisted trust store.
        trust_project_extensions: Allow project-authored Python extensions for
            this session.
        extension_paths: Explicit one-run extension files or directories.
        enable_interpreter: Enable `CodeInterpreterMiddleware` (`js_eval`) on
            the main agent. `None` defers to the sandbox-aware/config default.
        interpreter_arg: The raw `--interpreter`/`--no-interpreter` tri-state,
            forwarded so the app can tell an explicit opt-out from a
            sandbox-suppressed default when surfacing the disabled-by-sandbox
            advisory.
        interpreter_ptc: Invocation-scoped PTC allowlist override for `js_eval`.
        interpreter_ptc_acknowledge_unsafe: Explicit acknowledgement for
            `interpreter_ptc="all"` outside of `auto_approve`.
        allow_fs_tools: Allowlist for `FilesystemMiddleware`'s `tools` param,
            from `--allow-fs-tools`.

            `None` leaves the SDK default (all tools).
        auto_classifier_model: Model spec the Auto approval classifier reviews
            with, from `--auto-classifier-model`.

            `None` resolves from env / `config.toml`, then to the lightweight
            default for the main model's provider. Providers without a default
            reuse the main agent model. A blank string means the flag was
            explicitly supplied with no value: it overrides any configured or
            provider default so reviews inherit the main agent model.
        recursion_limit: Explicit main-agent `recursion_limit`; `None` resolves
            from runtime configuration at agent-build time.

    Returns:
        An `AppResult` with the return code and final thread ID.
    """
    from rich.text import Text

    from deepagents_code.app import AppResult, run_textual_app
    from deepagents_code.approval_mode import ApprovalMode, coerce_approval_mode
    from deepagents_code.config import (
        _get_default_model_spec,
        default_auto_classifier_model,
        detect_provider,
        resolve_auto_classifier_model_with_problem,
        runtime_state,
    )
    from deepagents_code.model_config import (
        ModelConfigError,
        ModelSpec,
        NoCredentialsConfiguredError,
    )
    from deepagents_code.onboarding import should_run_onboarding

    # [해설][흐름] 1) 승인 모드 정규화. 구 Boolean API(`auto_approve`)는 True→YOLO, False→MANUAL로 매핑(호환용).
    resolved_approval_mode = coerce_approval_mode(approval_mode)
    if auto_approve is not None:
        resolved_approval_mode = (
            ApprovalMode.YOLO if auto_approve else ApprovalMode.MANUAL
        )

    # Resolve display-name cheaply (<1ms, no langchain) so the status
    # bar can show the model on first paint. The expensive create_model()
    # (~560ms) is deferred to a background worker.

    # [해설][흐름] 2) 모델 spec 해석(<1ms, LangChain 없음). 자격 증명이 전혀 없으면 서버 기동을 미루고(`defer_server_start`)
    # [해설] TUI 온보딩에서 키를 받게 한다(추정: `should_run_onboarding`과 연계). 설정 오류면 즉시 exit 1 결과 반환.
    defer_server_start = False
    try:
        resolved_spec = model_name or _get_default_model_spec()
    except NoCredentialsConfiguredError:
        resolved_spec = ""
        defer_server_start = True
    except ModelConfigError as e:
        from rich.markup import escape

        from deepagents_code.config import console

        console.print(f"[bold red]Error:[/bold red] {escape(str(e))}", highlight=False)
        return AppResult(return_code=1, thread_id=None)

    # [해설][흐름] 3) 상태 표시줄이 첫 화면부터 모델을 보여주도록 전역 `runtime_state`에 provider/model 기록.
    if resolved_spec:
        parsed = ModelSpec.try_parse(resolved_spec)
        if parsed:
            runtime_state.model_provider = parsed.provider
            runtime_state.model_name = parsed.model
        else:
            runtime_state.model_name = resolved_spec
            runtime_state.model_provider = detect_provider(resolved_spec) or ""
    else:
        runtime_state.model_provider = ""
        runtime_state.model_name = ""

    # Distinguish "flag absent" from "flag explicitly blank": `--auto-classifier-
    # model ""` is the "inherit the main agent model" instruction and overrides
    # any env / `config.toml` classifier, while an absent flag defers to those
    # sources. Map the explicit blank to `INHERIT_CLASSIFIER_MODEL`, the sentinel
    # the server and Auto middleware already understand as inherit, so the
    # override survives the trip to the agent server (a bare `""` would collapse
    # to `None` in `ServerConfig.from_env` and silently re-enable a configured
    # classifier there).
    from deepagents_code._cli_context import INHERIT_CLASSIFIER_MODEL

    # [해설][흐름] 4) Auto 분류기 모델 결정: 플래그 값 > 빈 플래그(상속 표식) > env/config(`resolve_auto_classifier_model_with_problem`)
    # [해설] > 제공자별 기본 경량 모델. 문제가 있으면 stderr 경고 후 정책 차단만 상속으로 강등.
    flag_supplied = auto_classifier_model is not None
    flag_classifier_model = (auto_classifier_model or "").strip() or None
    auto_classifier_problem: str | None = None
    if flag_classifier_model is not None:
        resolved_auto_classifier_model = flag_classifier_model
    elif flag_supplied:
        resolved_auto_classifier_model = INHERIT_CLASSIFIER_MODEL
    else:
        (
            resolved_auto_classifier_model,
            auto_classifier_problem,
        ) = resolve_auto_classifier_model_with_problem()
        if resolved_auto_classifier_model is None and auto_classifier_problem is None:
            resolved_auto_classifier_model = default_auto_classifier_model(
                runtime_state.model_provider
            )
    if (
        resolved_auto_classifier_model is not None
        and resolved_auto_classifier_model != INHERIT_CLASSIFIER_MODEL
    ):
        auto_classifier_problem = _auto_classifier_spec_problem(
            resolved_auto_classifier_model
        )
    if auto_classifier_problem is not None:
        from rich.console import Console as _WarnConsole
        from rich.markup import escape

        _WarnConsole(stderr=True).print(
            f"[bold yellow]Warning:[/bold yellow] {escape(auto_classifier_problem)}"
        )
        resolved_auto_classifier_model = _classifier_model_after_policy(
            resolved_auto_classifier_model
        )

    # [해설][흐름] 5) TUI 백그라운드에서 `create_model`(~560ms)을 지연 호출할 인자. 서버 기동을 미루면 None.
    model_kwargs: dict[str, Any] | None = None
    if not defer_server_start:
        model_kwargs = {
            "model_spec": model_name or resolved_spec,
            "extra_kwargs": model_params,
            "profile_overrides": profile_override,
            "cli_max_retries": cli_max_retries,
        }

    # Build kwargs for deferred server startup. Approval mode remains a live
    # per-thread Store record, so graph construction is independent of startup mode.
    # [해설][흐름] 6) `start_server_and_get_agent(**server_kwargs)`로 전달될 인자 → `ServerConfig.from_cli_args` → `DEEPAGENTS_CODE_SERVER_*` env.
    # [해설] 인터랙티브이므로 `enable_ask_user=True`, `interactive=True`. 승인 모드는 여기에 없다: 스레드별 Store 레코드로 실시간 관리되어
    # [해설] 그래프 구성이 시작 모드와 무관하기 때문이다(주석 원문 참고).
    server_kwargs: dict[str, Any] = {
        "assistant_id": assistant_id,
        "model_name": model_name or resolved_spec or None,
        "summarization_model": summarization_model,
        "model_params": model_params,
        "cli_max_retries": cli_max_retries,
        "profile_overrides": profile_override,
        "sandbox_type": sandbox_type,
        "sandbox_id": sandbox_id,
        "sandbox_snapshot_name": sandbox_snapshot_name,
        "sandbox_setup": sandbox_setup,
        "enable_ask_user": True,
        "enable_interpreter": enable_interpreter,
        "interpreter_ptc": interpreter_ptc,
        "interpreter_ptc_acknowledge_unsafe": interpreter_ptc_acknowledge_unsafe,
        "allow_fs_tools": allow_fs_tools,
        "auto_classifier_model": resolved_auto_classifier_model,
        "mcp_config_path": mcp_config_path,
        "no_mcp": no_mcp,
        "trust_project_mcp": trust_project_mcp,
        "trust_project_extensions": trust_project_extensions,
        "extension_paths": extension_paths,
        "interactive": True,
        "recursion_limit": recursion_limit,
    }

    # [해설][흐름] 7) TUI가 배너/`/mcp` 뷰어용 MCP 메타데이터를 클라이언트 쪽에서 미리 읽을 때 쓰는 인자.
    mcp_preload_kwargs: dict[str, Any] | None = None
    if not no_mcp:
        mcp_preload_kwargs = {
            "mcp_config_path": mcp_config_path,
            "no_mcp": no_mcp,
            "trust_project_mcp": trust_project_mcp,
        }

    # [해설][흐름] 8) TUI 실행(여기부터 app.py 영역). `backend=None`은 서버 모드(에이전트는 원격 서버)라는 뜻(추정).
    try:
        result = await run_textual_app(
            assistant_id=assistant_id,
            backend=None,
            approval_mode=resolved_approval_mode,
            cwd=Path.cwd(),
            thread_id=thread_id,
            resume_thread=resume_thread,
            initial_prompt=initial_prompt,
            initial_skill=initial_skill,
            initial_goal=initial_goal,
            startup_cmd=startup_cmd,
            launch_init=should_run_onboarding(),
            profile_override=profile_override,
            summarization_model=summarization_model,
            server_kwargs=server_kwargs,
            mcp_preload_kwargs=mcp_preload_kwargs,
            model_kwargs=model_kwargs,
            model_explicitly_set=model_name is not None,
            interpreter_arg=interpreter_arg,
            defer_server_start=defer_server_start,
            hook_trust=hook_trust,
        )
    # [해설][흐름] 9) TUI 크래시: 오류 출력. `TextualAppError`는 실제 활성 thread_id를 담고 있어 종료 후 resume 힌트에 쓰인다.
    except Exception as e:
        logger.debug("App error", exc_info=True)
        from deepagents_code.app import TextualAppError
        from deepagents_code.config import console

        error_text = Text("Application error: ", style="red")
        error_text.append(str(e))
        console.print(error_text)
        if logger.isEnabledFor(logging.DEBUG):
            console.print(Text(traceback.format_exc(), style="dim"))
        # The app resolves resume intent and `/threads` switches asynchronously,
        # so the crashed session's final thread ID only exists on the exception.
        # Returning its snapshot lets the caller's teardown print a resume hint
        # for the thread that was actually active when the session died.
        if isinstance(e, TextualAppError):
            return e.result
        return AppResult(return_code=1, thread_id=thread_id)

    return result


# [해설] `--acp` 모드 본체: stdio로 ACP(Agent Client Protocol) 서버를 실행한다. 호출자: `cli_main`의 `--acp` 분기.
# [해설][설계] 다른 모드와 달리 LangGraph 서버 프로세스를 띄우지 않고 **같은 프로세스 안에서** `create_cli_agent`로 그래프를 만든다.
# [해설] 따라서 `ServerConfig` env 채널·`offload_api`·`dcode_thread_workspaces` 바인딩 강제를 거치지 않는다(추정, analysis 01 "더 볼 거리").
# [해설] 체크포인터는 클라이언트 쪽 `sessions.get_checkpointer()`(AsyncSqliteSaver, sessions.db)를 명시적으로 넘긴다.
# [해설] 관련: `analysis/09-tui-app-commands-acp.md`, 외부 패키지 `deepagents_acp.server`.
async def _run_acp_cli_async(
    assistant_id: str,
    *,
    run_acp_agent: Callable[[Any], Any],
    agent_server_cls: type[Any],
    model_name: str | None = None,
    model_params: dict[str, Any] | None = None,
    summarization_model: str | None = None,
    cli_max_retries: int | None = None,
    profile_override: dict[str, Any] | None = None,
    mcp_config_path: str | None = None,
    no_mcp: bool = False,
    trust_project_mcp: bool | None = None,
    allow_fs_tools: "list[FsToolName] | None" = None,
    recursion_limit: int | None = None,
    auto: bool = False,
    yolo: bool = False,
    auto_classifier_model: str | None = None,
) -> int:
    """Run ACP server mode and return a process exit code.

    Args:
        assistant_id: Agent identifier to initialize.
        run_acp_agent: ACP server runner function.
        agent_server_cls: ACP server class constructor.
        model_name: Optional model name to use.
        model_params: Extra kwargs from `--model-params` to pass to the model.
        summarization_model: Model spec used only for context-compaction summaries.
        cli_max_retries: Explicit `--max-retries` value.
        profile_override: Extra profile fields from `--profile-override`.
        mcp_config_path: Optional path to MCP servers JSON configuration file.
        no_mcp: Disable all MCP tool loading.
        trust_project_mcp: Controls project-level server trust (stdio and
            remote alike).
        allow_fs_tools: Allowlist for `FilesystemMiddleware`'s `tools` param,
            from `--allow-fs-tools`.

            `None` leaves the SDK default (all tools).
        recursion_limit: Explicit main-agent `recursion_limit`; `None` resolves
            from runtime configuration at agent-build time.
        auto: Enable classifier-backed approval routing.
        yolo: Disable approval prompts for this ACP server.
        auto_classifier_model: Optional model for Auto approval classification.

    Returns:
        Exit code for ACP mode.
    """
    from deepagents_code.agent import create_cli_agent, load_async_subagents
    from deepagents_code.config import (
        create_model,
        credentials,
        is_memory_auto_save_enabled,
        resolve_auto_classifier_model_for_provider,
    )
    from deepagents_code.model_config import (
        ModelConfigError,
        ModelNotAllowedError,
        get_available_models,
        save_recent_model,
        touch_recent_model,
    )
    from deepagents_code.plugins.adapters.mcp import discover_plugin_mcp_configs
    from deepagents_code.project_utils import ProjectContext
    from deepagents_code.tools import fetch_url, get_current_thread_id, web_search

    # [해설][흐름] 1) 모델을 즉시 생성(`config.create_model`)해 설정 오류를 기동 시점에 잡고 전역 runtime_state에 반영.
    try:
        model_result = create_model(
            model_name,
            extra_kwargs=model_params,
            profile_overrides=profile_override,
            cli_max_retries=cli_max_retries,
        )
    except ModelConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        return 1
    model_result.apply_to_runtime_state()

    # [해설][흐름] 2) 프로젝트 컨텍스트(프로젝트 루트/사용자 cwd) 파악 — MCP 프로젝트 설정·플러그인 MCP 탐색 기준.
    try:
        project_context = ProjectContext.from_user_cwd(Path.cwd())
    except (OSError, RuntimeError):
        logger.warning("Could not determine working directory for ACP MCP loading")
        project_context = None
    project_dir = (
        project_context.project_root or project_context.user_cwd
        if project_context is not None
        else None
    )

    # [해설][흐름] 3) 확정된 모델을 `[models].recent`에 기록하고, ACP 클라이언트에 노출할 모델 선택지 목록을 만든다(중복 제거·순서 유지).
    # Persist the resolved model so [models].recent is always populated.
    resolved_spec = f"{model_result.provider}:{model_result.model_name}"
    # Best-effort persistence. `resolved_spec` came out of `create_model`, so
    # it already passed the policy gate; a refusal here means the config changed
    # mid-session, which must not take down a session that is already running.
    with contextlib.suppress(ModelNotAllowedError):
        save_recent_model(resolved_spec)
    touch_recent_model(resolved_spec)
    models = [
        {"value": spec, "name": spec}
        for spec in dict.fromkeys(
            [
                resolved_spec,
                *(
                    f"{provider}:{model}"
                    for provider, available in get_available_models().items()
                    for model in available
                ),
            ]
        )
    ]

    # [해설][흐름] 4) 기본 도구(fetch_url, get_current_thread_id) + Tavily 키가 있으면 web_search + MCP 도구를 이 프로세스에서 로드.
    tools: list[Any] = [fetch_url, get_current_thread_id]
    if credentials.has_tavily:
        tools.append(web_search)

    mcp_session_manager = None
    mcp_server_info = None
    try:
        from deepagents_code.mcp_tools import resolve_and_load_mcp_tools

        (
            mcp_tools,
            mcp_session_manager,
            mcp_server_info,
        ) = await resolve_and_load_mcp_tools(
            explicit_config_path=mcp_config_path,
            no_mcp=no_mcp,
            trust_project_mcp=trust_project_mcp,
            project_context=project_context,
            additional_configs=(
                discover_plugin_mcp_configs(project_dir=project_dir)
                if not no_mcp
                else ()
            ),
        )
        tools.extend(mcp_tools)
    except FileNotFoundError as exc:
        msg = f"Error: MCP config file not found: {exc}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        return 1
    except RuntimeError as exc:
        msg = f"Error: Failed to load MCP tools: {exc}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()
        return 1

    # [해설][흐름] 5) 비동기 서브에이전트 정의 로드(`agent.load_async_subagents`, analysis 05 참고).
    async_subagents = load_async_subagents() or None
    exit_code = 0
    try:
        # [해설][흐름] 6) sessions.db 체크포인터를 열고 스키마 준비(`setup`). Auto 모드면 승인 상태 저장용 `InMemoryStore`를 만든다(추정: Auto 분류기 상태 기록).
        from deepagents_code.sessions import get_checkpointer

        async with get_checkpointer() as checkpointer:
            await checkpointer.setup()
            from langgraph.store.memory import InMemoryStore

            store = InMemoryStore() if auto else None

            # [해설] ACP 세션마다 호출되는 그래프 팩토리. 세션이 다른 모델을 고르면 그 모델을 새로 만들고, 세션 cwd 기준 ProjectContext로
            # [해설] `agent.create_cli_agent`를 호출한다. `auto_approve=yolo`, `auto_mode_enabled=auto`로 승인 정책을 그래프에 고정.
            # [해설][SDK] `create_cli_agent`는 최종적으로 SDK `deepagents.graph.create_deep_agent`를 호출한다(analysis 02).
            def build_agent(
                context: "AgentSessionContext",
            ) -> "Pregel[Any, Any, Any, Any]":
                selected_model = context.model or resolved_spec
                session_model = (
                    model_result
                    if selected_model == resolved_spec
                    else create_model(
                        selected_model,
                        extra_kwargs=model_params,
                        profile_overrides=profile_override,
                        cli_max_retries=cli_max_retries,
                    )
                )
                session_model.apply_to_runtime_state()
                classifier_model = resolve_auto_classifier_model_for_provider(
                    session_model.provider,
                    auto_classifier_model,
                )
                agent_graph, _backend = create_cli_agent(
                    model=session_model.model,
                    assistant_id=assistant_id,
                    tools=tools,
                    mcp_server_info=mcp_server_info,
                    checkpointer=checkpointer,
                    async_subagents=async_subagents,
                    fs_tools=allow_fs_tools,
                    recursion_limit=recursion_limit,
                    auto_approve=yolo,
                    auto_mode_enabled=auto,
                    auto_classifier_model=classifier_model,
                    memory_auto_save=is_memory_auto_save_enabled(),
                    store=store,
                    cwd=context.cwd,
                    project_context=ProjectContext.from_user_cwd(Path(context.cwd)),
                    model_retries=session_model.model_retries,
                    cli_max_retries=session_model.cli_max_retries,
                    summarization_model=summarization_model,
                )
                return agent_graph

            # [해설][흐름] 7) Auto 모드면 dcode 확장 서버 `deepagents_code.acp.AgentServerACP`(store 필요), 아니면 `deepagents_acp`의 기본 서버 클래스.
            # [해설] `load_sessions=True`로 기존 세션 불러오기를 허용하고 `run_acp_agent`로 stdio 서비스 루프에 진입한다.
            if auto:
                from deepagents_code.acp import AgentServerACP

                server_cls = AgentServerACP
                server_kwargs = {"store": cast("Any", store)}
            else:
                server_cls = agent_server_cls
                server_kwargs = {}
            server = server_cls(
                build_agent,
                models=models,
                load_sessions=True,
                **server_kwargs,
            )
            await run_acp_agent(server)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        sys.stderr.write(f"Error: ACP server failed: {exc}\n")
        sys.stderr.flush()
        logger.exception("ACP server crashed")
        exit_code = 1
    # [해설][흐름] 8) 종료 시 MCP 세션(stdio 서브프로세스 등) 정리. 예외는 exit 1, KeyboardInterrupt는 정상 종료(0).
    finally:
        if mcp_session_manager is not None:
            try:
                await mcp_session_manager.cleanup()
            except Exception:
                logger.warning("MCP session cleanup failed", exc_info=True)
    return exit_code


# [해설] 파이프로 들어온 stdin을 읽어 argparse 결과에 병합하는 함수. 호출자: `cli_main`(ACP 분기 이후, 플래그 조합 검증 전).
# [해설] 헤드리스/인터랙티브 판정의 최종 결정자: stdin이 파이프이고 `-n`/`-m`/`--skill`이 없으면 `non_interactive_message`를 채워 헤드리스가 된다.
# [해설][주의] `cat x | dcode --skill foo`(자동 감지, -n 없음)는 인터랙티브 TUI로 가고 stdin이 `initial_prompt`가 된다 —
# [해설] 공식 문서의 "파이프면 자동 비대화형" 서술의 예외(analysis 01 문서↔코드 대조).
def apply_stdin_pipe(args: argparse.Namespace) -> None:
    r"""Read piped stdin and merge it into the parsed CLI arguments.

    When stdin is not a TTY (i.e. input is piped), reads all available text
    and applies it to the argument namespace. If stdin is a TTY or the piped
    input is empty/whitespace-only, the function returns without modifying
    `args`. Leading and trailing whitespace is stripped from piped input.

    - If `non_interactive_message` is already set (`-n`), prepends the
        piped text to it (the CLI still runs non-interactively):

        ```bash
        cat context.txt | dcode -n "summarize this"
        # non_interactive_message = "{contents of context.txt}\n\nsummarize this"
        ```

    - If `initial_prompt` is already set (`-m`, but not `-n`), prepends
        the piped text to it (the CLI still runs interactively):

        ```bash
        cat error.log | dcode -m "explain this"
        # initial_prompt = "{contents of error.log}\n\nexplain this"
        ```

    - If `initial_skill` is already set (`--skill`, but not `-n`/`-m`) and the
        pipe was auto-detected (no explicit `--stdin`), stores the piped text in
        `initial_prompt` so the skill receives it as the seed for the
        interactive TUI:

        ```bash
        cat diff.txt | dcode --skill code-review
        # initial_prompt = "{contents of diff.txt}"
        ```

        When `--stdin` is passed explicitly, this convenience is skipped: the
        piped text falls through to `non_interactive_message` so the skill runs
        headless (see below):

        ```bash
        cat diff.txt | dcode --skill code-review --stdin
        # non_interactive_message = "{contents of diff.txt}"
        ```

    - Otherwise, sets `non_interactive_message` to the piped text, causing
        the CLI to run non-interactively with it as the prompt:

        ```bash
        echo "fix the typo in README.md" | dcode
        # non_interactive_message = "fix the typo in README.md"
        ```

    Args:
        args: The parsed argument namespace (mutated in place).
    """
    from deepagents_code.config import console

    # [해설][흐름] 1) stdin 없음/상태 판별 불가/TTY이면 아무것도 하지 않는다. 단 `--stdin`을 명시했으면 오류 exit 1.
    explicit_stdin = args.stdin

    if sys.stdin is None:
        if explicit_stdin:
            console.print(
                "[bold red]Error:[/bold red] --stdin was passed but stdin "
                "is not available."
            )
            sys.exit(1)
        return

    try:
        is_tty = sys.stdin.isatty()
    except (ValueError, OSError):
        if explicit_stdin:
            console.print(
                "[bold red]Error:[/bold red] --stdin was passed but stdin "
                "state could not be determined."
            )
            sys.exit(1)
        return

    if is_tty:
        if explicit_stdin:
            console.print(
                "[bold red]Error:[/bold red] --stdin was passed but stdin "
                "is a terminal. Pipe input or use -n instead.\n"
                "  cat prompt.txt | dcode --stdin -q"
            )
            sys.exit(1)
        return

    # [해설][흐름] 2) 최대 10 MiB(+1 문자) 읽기. 한도 초과 여부를 판정하려고 1을 더 읽는다. 초과·디코딩 실패는 exit 1.
    # [해설][주의] `read(n)`은 텍스트 모드라 실제로는 "문자 수" 기준이다(바이트가 아님, 추정). 이름과 달리 멀티바이트 입력은 10 MiB보다 클 수 있다.
    max_stdin_bytes = 10 * 1024 * 1024  # 10 MiB

    try:
        stdin_text = sys.stdin.read(max_stdin_bytes + 1)
    except UnicodeDecodeError:
        msg = "Could not read piped input — ensure the input is valid text"
        console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)
    except (OSError, ValueError) as exc:
        from rich.markup import escape

        console.print(
            f"[bold red]Error:[/bold red] Failed to read piped input: "
            f"{escape(str(exc))}"
        )
        sys.exit(1)

    if len(stdin_text) > max_stdin_bytes:
        msg = (
            f"Piped input exceeds {max_stdin_bytes // (1024 * 1024)} MiB limit. "
            "Consider writing the content to a file and referencing it instead."
        )
        console.print(f"[bold red]Error:[/bold red] {msg}")
        sys.exit(1)

    stdin_text = stdin_text.strip()

    if not stdin_text:
        return

    # [해설][흐름] 3) 병합 우선순위 적용: `-n` 앞에 붙임 > `-m` 앞에 붙임 > `--skill`이면 initial_prompt > 그 외 헤드리스 메시지.
    # Priority: -n message > -m prompt > --skill (no -m, no explicit --stdin)
    # > fallback to -n.
    # The initial_prompt branch uses `is not None` (not truthiness) so that
    # `-m ""` is distinguished from "no -m at all", allowing stdin to land
    # in initial_prompt even when the explicit value is empty.  The --skill
    # branch only fires when -m was NOT provided; when both -m and --skill
    # are set, stdin merges with the -m value (previous branch).
    #
    # The --skill -> interactive `initial_prompt` routing applies only to
    # auto-detected pipes (no explicit `--stdin`), where seeding an interactive
    # TUI is a deliberate convenience.  When the user passes `--stdin`
    # explicitly, that signals non-interactive intent, so we skip this branch
    # and fall through to `non_interactive_message` (headless), which also
    # supports `--skill`.
    if args.non_interactive_message:
        args.non_interactive_message = f"{stdin_text}\n\n{args.non_interactive_message}"
    elif args.initial_prompt is not None:
        if args.initial_prompt:
            args.initial_prompt = f"{stdin_text}\n\n{args.initial_prompt}"
        else:
            args.initial_prompt = stdin_text
    elif getattr(args, "initial_skill", None) and not explicit_stdin:
        args.initial_prompt = stdin_text
    else:
        args.non_interactive_message = stdin_text

    # [해설][흐름] 4) stdin을 다 읽었으므로 fd 0을 `/dev/tty`로 교체(dup2). Textual 드라이버는 `sys.stdin`이 아니라 fd 0을 직접 읽기 때문.
    # Restore stdin from the real terminal so the interactive Textual app
    # (used by the -m path) can read keyboard/mouse input normally.
    # Textual's driver reads from file descriptor 0 directly (not sys.stdin),
    # so we must replace the underlying fd with /dev/tty using os.dup2.
    try:
        tty_fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        # No controlling terminal (CI, Docker, headless). Non-interactive
        # path still works; interactive -m path will fail later with a
        # clear "not a terminal" error from Textual.
        return

    try:
        os.dup2(tty_fd, 0)
        os.close(tty_fd)
        sys.stdin = open(0, encoding="utf-8", closefd=False)  # noqa: SIM115  # fd 0 requires open() for TTY restoration
    except OSError:
        console.print(
            "[yellow]Warning:[/yellow] TTY restoration failed. "
            "Interactive mode (-m) may not work correctly."
        )
        logger.warning(
            "TTY restoration failed after opening /dev/tty",
            exc_info=True,
        )
        try:
            os.close(tty_fd)
        except OSError:
            logger.warning(
                "Failed to close TTY fd %d during cleanup",
                tty_fd,
                exc_info=True,
            )


# [해설] TUI 종료 후 세션 사용량 표(토큰·시간) 출력. `[ui].show_usage_stats`로 끌 수 있다. 호출자: `cli_main` 인터랙티브 종료 경로.
# [해설] 타입 가드를 설정 조회보다 먼저 두어, 잘못된 payload면 설정 I/O 없이 경고만 남긴다.
def _print_session_stats(stats: Any, console: Any) -> None:  # noqa: ANN401
    """Print the session usage stats table on TUI exit, unless it is disabled.

    Gated by `[ui].show_usage_stats`, so this may print nothing. The payload
    type guard stays ahead of that lookup: `stats` is typed `Any`, and a caller
    passing something other than `SessionStats` should not trigger config I/O
    to decide to print nothing.

    That guard is unreachable as long as callers respect
    `AppResult.session_stats`'s declared type — a dataclass annotation, not
    runtime enforcement, which is exactly what `stats: Any` lets slip. If it
    fires, something upstream is broken rather than merely disabled, so it
    warns instead of returning silently: that keeps the two otherwise
    indistinguishable empty outputs apart.

    An exception escaping `usage_table_enabled` here would be caught by the
    top-level handler that rewrites a clean exit into `1` plus a traceback,
    which is why that call fails open.

    Args:
        stats: The cumulative session stats from the Textual app.
        console: Rich console for output.
    """
    from deepagents_code._session_stats import (
        SessionStats,
        print_usage_table,
        usage_table_enabled,
    )

    if not isinstance(stats, SessionStats):
        logger.warning(
            "Skipping session stats table: expected SessionStats, got %s",
            type(stats).__name__,
        )
        return
    if not usage_table_enabled():
        return
    print_usage_table(stats, stats.wall_time_seconds, console)


# [해설] env `DEBUG_MCP_PROJECT_TRUST`(`_env_vars`)가 켜져 있으면 프로젝트 MCP 승인 프롬프트 디버그 경로를 활성화(`_check_mcp_project_trust`에서 사용).
def _debug_mcp_project_trust_enabled() -> bool:
    """Return whether the project MCP approval prompt debug path is enabled."""
    from deepagents_code._env_vars import DEBUG_MCP_PROJECT_TRUST, is_env_truthy

    return is_env_truthy(DEBUG_MCP_PROJECT_TRUST)


# [해설] 텍스트 폴백 선택기의 `1,3` 형식 입력을 중복 없는 1-based 인덱스로 파싱. 잘못된 토큰은 조용히 무시.
def _parse_server_number_selection(raw: str, count: int) -> list[int]:
    """Parse a `1,3`-style selection into unique, in-range 1-based indices.

    Accepts comma- and/or whitespace-separated tokens. Non-integer or
    out-of-range tokens are ignored; the result preserves input order and
    drops duplicates.

    Args:
        raw: The user's raw selection input.
        count: The number of choices (valid indices are `1..count`).

    Returns:
        The selected 1-based indices.
    """
    selected: list[int] = []
    for token in raw.replace(",", " ").split():
        try:
            index = int(token)
        except ValueError:
            continue
        if 1 <= index <= count and index not in selected:
            selected.append(index)
    return selected


# [해설] 프로젝트 MCP 체크박스 선택기의 한 화면 행들(`[x] name (kind): summary`)을 prompt_toolkit 조각으로 만든다.
def _format_project_mcp_checkbox_rows(
    prompt_servers: Sequence["ProjectServerSummary"],
    selected_names: set[str],
    selected_index: int,
    glyphs: "Glyphs",
) -> list[tuple[str, str]]:
    """Format rows for the inline project MCP checkbox picker.

    Args:
        prompt_servers: The `(name, kind, summary)` rows being asked about.
        selected_names: Server names that are currently checked.
        selected_index: Zero-based cursor row.
        glyphs: Terminal-appropriate glyphs.

    Returns:
        Prompt-toolkit formatted text fragments, one per visible server row.
    """
    rows: list[tuple[str, str]] = []
    for index, (name, kind, summary) in enumerate(prompt_servers):
        active = index == selected_index
        checked = name in selected_names
        cursor = glyphs.cursor if active else " "
        box = "[x]" if checked else "[ ]"
        style = "class:item.current" if active else "class:item"
        suffix = "\n" if index < len(prompt_servers) - 1 else ""
        rows.append((style, f"{cursor} {box} {name} ({kind}): {summary}{suffix}"))
    return rows


# [해설] 인라인 선택기 사용 가능 조건: stdin·stderr가 모두 TTY(선택기 UI는 stderr로 그린다).
def _trust_picker_has_terminal() -> bool:
    """Return whether the inline trust pickers have interactive input and output."""
    return sys.stdin.isatty() and sys.stderr.isatty()


# [해설] TUI 이전에 뜨는 공용 인라인 결정 선택기(prompt_toolkit, stderr 출력, 화면 지우기). 주제 무관: 라벨만 호출자가 정한다.
# [해설] 반환: `_TrustAction` / Esc·Ctrl+D → CANCELLED / Ctrl+C → INTERRUPTED / 사용 불가 → None(텍스트 폴백 신호).
# [해설][설계] 기본 하이라이트는 위치가 아니라 "행동 정체성"으로 정한다: refresh가 있으면 REFRESH, 없으면 DENY(Enter만 누르면 거부 = fail-closed).
def _run_trust_action_picker(
    console: "Console",
    *,
    remember_label: str = "Allow for this project — until changed",
    allow_label: str = "Allow once",
    deny_label: str = "Deny",
    refresh_label: str | None = None,
    deny_first: bool = False,
) -> _TrustAction | _TrustPromptOutcome | None:
    """Show the inline decision picker shared by pre-TUI prompts.

    Subject-agnostic: callers describe the decision before calling and supply
    labels that match what each action does. Trust prompts omit `refresh_label`
    and keep their existing three choices.

    Args:
        console: Console to print fallback notices to (stderr).
        remember_label: Label for the persistent-trust option. Set this to match
            what the caller actually persists, since scope differs by subject.
        allow_label: Label for the session-scoped allow option.
        deny_label: Label for the refuse option.
        refresh_label: Label for an explicit environment refresh, when offered.
        deny_first: When `True`, list the deny option first; callers whose
            "deny" reads as a safe default (e.g. aborting a launch) put it in
            the leading position. When a refresh option is offered, the picker
            starts highlighted on it rather than on deny: fixing the
            environment is the action the prompt is steering toward, and deny
            is still one keystroke away. Without a refresh option the picker
            starts highlighted on deny in either ordering, so a bare Enter
            refuses.

    Returns:
        The chosen action, `CANCELLED` for Esc or Ctrl+D, `INTERRUPTED` for
        Ctrl+C, or `None` when the inline picker cannot run and the caller should
        use the text fallback.
    """
    if not _trust_picker_has_terminal():
        return None

    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.key_binding.key_processor import KeyPressEvent
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.output.defaults import create_output
        from prompt_toolkit.styles import Style
    except ImportError:
        logger.debug("Trust action picker unavailable", exc_info=True)
        console.print(
            "[dim]Interactive selector unavailable; falling back to text input.[/dim]",
            highlight=False,
        )
        return None

    from deepagents_code.config import get_glyphs

    glyphs = get_glyphs()
    # [해설][흐름] 1) 선택지 배열 구성: 기본 순서(allow/remember/deny), refresh 추가 형태, deny_first면 거부를 맨 앞으로.
    actions = [
        (_TrustAction.ALLOW_ONCE, allow_label),
        (_TrustAction.REMEMBER, remember_label),
        (_TrustAction.DENY, deny_label),
    ]
    if refresh_label is not None:
        actions = (
            [
                (_TrustAction.DENY, deny_label),
                (_TrustAction.REFRESH, refresh_label),
                (_TrustAction.ALLOW_ONCE, allow_label),
                (_TrustAction.REMEMBER, remember_label),
            ]
            if deny_first
            else [
                (_TrustAction.ALLOW_ONCE, allow_label),
                (_TrustAction.REMEMBER, remember_label),
                (_TrustAction.REFRESH, refresh_label),
                (_TrustAction.DENY, deny_label),
            ]
        )
    elif deny_first:
        actions.reverse()
    # Highlight the default action by identity, not position: `deny_first`
    # moves the deny option to the front, so a positional index would silently
    # default to "allow" for exactly the callers that asked for a safer
    # ordering. When a refresh option exists it is the default instead, so a
    # bare Enter repairs the environment; a deny-only list still defaults to
    # refusing.
    default_action = (
        _TrustAction.REFRESH if refresh_label is not None else _TrustAction.DENY
    )
    selected_index = next(
        index for index, (action, _) in enumerate(actions) if action is default_action
    )

    # [해설][흐름] 2) 화면 렌더러와 키 바인딩(↑↓/Tab/j/k 이동, Enter 선택, Esc/Ctrl+D 취소, Ctrl+C 중단).
    def _rows() -> FormattedText:
        rows: list[tuple[str, str]] = [
            (
                "class:prompt.help",
                (
                    f"{glyphs.arrow_up}/{glyphs.arrow_down}/Tab move "
                    f"{glyphs.separator} "
                    f"Enter select {glyphs.separator} Esc/Ctrl+D abort\n"
                ),
            ),
        ]
        for index, (_action, label) in enumerate(actions):
            active = index == selected_index
            cursor = glyphs.cursor if active else " "
            style = "class:item.current" if active else "class:item"
            suffix = "\n" if index < len(actions) - 1 else ""
            rows.append((style, f"{cursor} {label}{suffix}"))
        return FormattedText(rows)

    key_bindings = KeyBindings()

    @key_bindings.add("up")
    @key_bindings.add("s-tab")
    @key_bindings.add("k")
    def _up(_event: KeyPressEvent) -> None:
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(actions)

    @key_bindings.add("down")
    @key_bindings.add("tab")
    @key_bindings.add("j")
    def _down(_event: KeyPressEvent) -> None:
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(actions)

    @key_bindings.add("enter")
    def _confirm(event: KeyPressEvent) -> None:
        event.app.exit(result=actions[selected_index][0])

    @key_bindings.add("escape")
    @key_bindings.add("c-d")
    def _abort(event: KeyPressEvent) -> None:
        event.app.exit(result=_TrustPromptOutcome.CANCELLED)

    @key_bindings.add("c-c")
    def _interrupt(event: KeyPressEvent) -> None:
        event.app.exit(result=_TrustPromptOutcome.INTERRUPTED)

    # [해설][흐름] 3) 전체 화면이 아닌 인라인 앱으로 실행(stdout 파이프를 오염시키지 않게 stderr 출력). 실패 시 None으로 폴백.
    app: Application[_TrustAction | _TrustPromptOutcome] = Application(
        layout=Layout(
            Window(
                FormattedTextControl(_rows, show_cursor=False),
                height=len(actions) + 1,
                dont_extend_height=True,
            )
        ),
        key_bindings=key_bindings,
        style=Style.from_dict(
            {
                "prompt.help": "ansibrightblack",
                "item.current": "reverse",
            }
        ),
        full_screen=False,
        erase_when_done=True,
        output=create_output(stdout=sys.stderr),
    )
    try:
        return app.run()
    except (RuntimeError, OSError):
        logger.debug("Trust action picker failed", exc_info=True)
        console.print(
            "[dim]Interactive selector unavailable; falling back to text input.[/dim]",
            highlight=False,
        )
        return None
    except KeyboardInterrupt:
        return _TrustPromptOutcome.INTERRUPTED
    except EOFError:
        return None


# [해설] 편집(editable) 설치의 의존성 버전이 체크아웃의 하한(floor)보다 낮을 때 띄우는 차단형 선택기(refresh/계속/숨기기/중단).
# [해설] 공용 선택기를 재사용하려고 `_dep_floor_check`가 아닌 여기 둔다. `abort_on_deny=True`라 거부는 항상 CANCELLED로 보고된다.
# [해설] 호출자: `_dep_floor_check.py`의 대화형 검사(`cli_main` → `prompt_if_editable_deps_stale`)가 지연 import로 호출.
def prompt_for_dep_floor_mismatch(
    console: "Console",
    violations: "Sequence[_FloorViolation]",
) -> _TrustAction | _TrustPromptOutcome:
    """Block on a refresh / continue / mute / abort picker for stale dependencies.

    Lives here (not in `_dep_floor_check`) beside the other pre-TUI trust
    prompts so the picker implementation is shared. The prompt prints to
    stderr and runs before the Textual alternate screen mounts, so it stays
    visible.

    Args:
        console: Console printing to stderr.
        violations: The detected below-floor dependencies.

    Returns:
        `REFRESH` to update the active environment, `ALLOW_ONCE` to continue
        this session, `REMEMBER` to mute this exact mismatch for this checkout,
        `CANCELLED` to abort the launch (chosen "Abort launch", Esc, or Ctrl+D —
        `abort_on_deny` collapses all three into one outcome, so `DENY` is never
        returned), or `INTERRUPTED` on Ctrl+C.
    """
    console.print()
    console.print(
        "[bold yellow]This editable dcode install is behind the checkout's "
        "dependency floors:[/bold yellow]",
        highlight=False,
    )
    from rich.markup import escape

    from deepagents_code._dep_floor_check import refresh_command

    for v in violations:
        console.print(f"  - {escape(v.describe())}", highlight=False)
    refresh = refresh_command()
    console.print(
        f"\nRefresh the active environment:\n  {escape(refresh)}", highlight=False
    )
    console.print(
        "[yellow]\nRunning stale source against older dependencies can break "
        "behavior in hard-to-diagnose ways.[/yellow]",
        highlight=False,
    )
    console.print()
    return _select_trust_action(
        console,
        remember_label="Continue and hide until versions change",
        allow_label="Continue this session only",
        deny_label="Abort launch",
        refresh_label="Refresh environment now",
        deny_first=True,
        abort_on_deny=True,
    )


# [해설] 인라인 선택기 → 실패 시 텍스트 입력 폴백을 하나로 묶은 결정 함수. 모든 입력 경로의 결과를 같은 타입으로 정규화한다.
# [해설][설계] 텍스트 입력에서 EOF는 거부로 처리해 비대화형 기동이 멈춰 기다리지 않고 fail-closed로 끝나게 한다.
# [해설] 호출자: `_check_mcp_project_trust`, `_check_project_hooks_trust`, `_check_project_extensions_trust`, `prompt_for_dep_floor_mismatch`.
def _select_trust_action(
    console: "Console",
    *,
    remember_label: str = "Allow for this project — until changed",
    allow_label: str = "Allow once",
    deny_label: str = "Deny",
    refresh_label: str | None = None,
    deny_first: bool = False,
    abort_on_deny: bool = False,
) -> _TrustAction | _TrustPromptOutcome:
    """Choose an action for a project trust or dependency-floor prompt.

    Falls back to a text prompt when the inline picker cannot run. Every failure
    mode resolves to a decision the caller can act on without knowing which
    input path produced it: an unavailable picker degrades to text, and EOF on
    the text prompt denies rather than aborting, so a non-interactive launch
    fails closed instead of hanging.

    Args:
        console: Console used by the text fallback.
        remember_label: Label for the persistent-trust option forwarded to the
            inline picker.
        allow_label: Label for the session-scoped allow option.
        deny_label: Label for the refuse option.
        refresh_label: Label for an explicit environment refresh, when offered.
        deny_first: Forwarded to the picker to list the deny option first.
        abort_on_deny: When `True`, a deny answer is reported as `CANCELLED`
            on every input path (picker, typed answer, and EOF), so prompts
            whose refuse option aborts the launch report exactly one outcome
            and callers cannot mistake a deny for a decision to proceed.

    Returns:
        The selected trust action, `CANCELLED` when the user presses Esc or
        Ctrl+D (or denies with `abort_on_deny`), or `INTERRUPTED` when the
        user presses Ctrl+C.
    """
    from deepagents_code.config import get_glyphs

    separator = f" {get_glyphs().separator} "
    selected = _run_trust_action_picker(
        console,
        remember_label=remember_label,
        allow_label=allow_label,
        deny_label=deny_label,
        refresh_label=refresh_label,
        deny_first=deny_first,
    )
    if selected is not None:
        if selected is _TrustAction.DENY and abort_on_deny:
            return _TrustPromptOutcome.CANCELLED
        return selected

    # [해설][흐름] 텍스트 폴백: 프롬프트 형태별로 키 안내([y/r/N] 등)를 만들고 stderr에 출력한 뒤 `input()`으로 받는다. 대문자가 기본값.
    # Mirror the caller's labels: for the dep-floor prompt the choices are
    # refresh / continue / mute / abort, and a hardcoded "allow once / deny" would
    # misdescribe what the default does. Both lines go to stderr, where the
    # rest of the prompt already went; passing the question to `input()`
    # instead would split it onto stdout, so `dcode 2>log` would show a bare
    # question with all of its context redirected away.
    if refresh_label is None:
        choices = separator.join(
            (f"{allow_label} [y]", f"{remember_label} [r]", f"{deny_label} [N]")
        )
        prompt = "Choose [y/r/N]: "
    elif deny_first:
        # Deny keeps its leading position, but the refresh — not the deny —
        # is the default: the uppercase [U] mirrors the picker's initial
        # highlight so a bare Enter repairs the environment.
        choices = separator.join(
            (
                f"{deny_label} [n]",
                f"{refresh_label} [U]",
                f"{allow_label} [y]",
                f"{remember_label} [r]",
            )
        )
        prompt = "Choose [n/U/y/r]: "
    else:
        choices = separator.join(
            (
                f"{allow_label} [y]",
                f"{remember_label} [r]",
                f"{refresh_label} [u]",
                f"{deny_label} [N]",
            )
        )
        prompt = "Choose [y/r/u/N]: "
    console.print(f"[dim]{choices}[/dim]", highlight=False)
    console.print(prompt, end="", highlight=False)
    try:
        answer = input().strip().lower()
    except KeyboardInterrupt:
        return _TrustPromptOutcome.INTERRUPTED
    except EOFError:
        return _TrustPromptOutcome.CANCELLED if abort_on_deny else _TrustAction.DENY
    # [해설] 입력 해석: y=한 번 허용, r/a=기억, (refresh 형태에서) 빈 입력/u=refresh, 그 외 전부 거부.
    if answer in {"y", "yes"}:
        return _TrustAction.ALLOW_ONCE
    if answer in {"r", "remember", "a", "always"}:
        return _TrustAction.REMEMBER
    # The refresh is the default in this prompt shape, so an empty answer
    # selects it; anything else (including an explicit "n") refuses.
    if refresh_label is not None and answer in {"", "u", "update", "f", "refresh"}:
        return _TrustAction.REFRESH
    return _TrustPromptOutcome.CANCELLED if abort_on_deny else _TrustAction.DENY


# [해설] 여러 프로젝트 MCP 서버 중 "이 프로젝트에서 기억할" 서버를 고르는 체크박스 선택기. 초기 선택은 비어 있음(명시적 선택 필요).
# [해설] 반환: 선택 목록 / CANCELLED(Esc·Ctrl+D) / INTERRUPTED / None(UI 불가 → 번호 입력 폴백).
def _run_project_mcp_server_checkbox_picker(
    prompt_servers: Sequence["ProjectServerSummary"], console: "Console"
) -> list[str] | _TrustPromptOutcome | None:
    """Show an inline checkbox picker for project MCP servers to remember.

    Args:
        prompt_servers: The `(name, kind, summary)` rows being asked about.
        console: Console to print fallback notices to (stderr).

    Returns:
        Selected server names. Empty means the user confirmed no selections;
        `CANCELLED` means the user backed out (Esc or Ctrl+D) to abort the launch;
        `INTERRUPTED` means the user pressed Ctrl+C; `None` means the checkbox UI
        could not run and the caller should fall back to a simpler prompt.
    """
    if not _trust_picker_has_terminal():
        return None

    try:
        from prompt_toolkit import Application
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.key_binding.key_processor import KeyPressEvent
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.output.defaults import create_output
        from prompt_toolkit.styles import Style
    except ImportError:
        logger.debug("Project MCP checkbox picker unavailable", exc_info=True)
        console.print(
            "[dim]Checkbox picker unavailable; falling back to number selection.[/dim]",
            highlight=False,
        )
        return None

    from deepagents_code.config import get_glyphs

    names = [name for name, _kind, _summary in prompt_servers]
    selected_names: set[str] = set()
    selected_index = 0
    visible_count = min(len(names), _PROJECT_MCP_PICKER_VISIBLE_ROWS)
    glyphs = get_glyphs()

    def _selected_names() -> list[str]:
        return [name for name in names if name in selected_names]

    def _help_text() -> FormattedText:
        return FormattedText(
            [
                ("class:prompt.title", "Choose servers to remember\n"),
                (
                    "class:prompt.help",
                    (
                        "Remembered servers are trusted only for this project while "
                        "their definitions stay unchanged.\n"
                        f"{selected_index + 1} of {len(names)} {glyphs.separator} "
                        f"{len(selected_names)} selected\n"
                        f"{glyphs.arrow_up}/{glyphs.arrow_down}/Tab move "
                        f"{glyphs.separator} "
                        f"Space toggle {glyphs.separator} a select all "
                        f"{glyphs.separator} c clear {glyphs.separator} "
                        f"Enter confirm {glyphs.separator} "
                        "Esc abort\n"
                    ),
                ),
            ]
        )

    # [해설] 커서가 항상 보이도록 `_PROJECT_MCP_PICKER_VISIBLE_ROWS` 크기의 창을 스크롤해 표시할 구간을 계산.
    def _rows() -> FormattedText:
        start = min(
            max(0, selected_index - visible_count + 1),
            len(prompt_servers) - visible_count,
        )
        visible_servers = prompt_servers[start : start + visible_count]
        return FormattedText(
            _format_project_mcp_checkbox_rows(
                visible_servers,
                selected_names,
                selected_index - start,
                glyphs,
            )
        )

    key_bindings = KeyBindings()

    @key_bindings.add("up")
    @key_bindings.add("s-tab")
    @key_bindings.add("k")
    def _up(_event: KeyPressEvent) -> None:
        nonlocal selected_index
        selected_index = (selected_index - 1) % len(names)

    @key_bindings.add("down")
    @key_bindings.add("tab")
    @key_bindings.add("j")
    def _down(_event: KeyPressEvent) -> None:
        nonlocal selected_index
        selected_index = (selected_index + 1) % len(names)

    @key_bindings.add(" ")
    def _toggle(_event: KeyPressEvent) -> None:
        name = names[selected_index]
        if name in selected_names:
            selected_names.remove(name)
        else:
            selected_names.add(name)

    @key_bindings.add("a")
    def _select_all(_event: KeyPressEvent) -> None:
        selected_names.update(names)

    @key_bindings.add("c")
    def _clear(_event: KeyPressEvent) -> None:
        selected_names.clear()

    @key_bindings.add("enter")
    def _confirm(event: KeyPressEvent) -> None:
        event.app.exit(result=_selected_names())

    @key_bindings.add("escape")
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=_TrustPromptOutcome.CANCELLED)

    @key_bindings.add("c-c")
    def _interrupt(event: KeyPressEvent) -> None:
        event.app.exit(result=_TrustPromptOutcome.INTERRUPTED)

    app: Application[list[str] | _TrustPromptOutcome] = Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(_help_text, show_cursor=False),
                        height=4,
                        dont_extend_height=True,
                    ),
                    Window(
                        FormattedTextControl(_rows, show_cursor=False),
                        height=visible_count,
                        dont_extend_height=True,
                    ),
                ]
            )
        ),
        key_bindings=key_bindings,
        style=Style.from_dict(
            {
                "prompt.title": "bold",
                "prompt.help": "ansibrightblack",
                "item.current": "reverse",
            }
        ),
        full_screen=False,
        erase_when_done=True,
        output=create_output(stdout=sys.stderr),
    )
    try:
        return app.run()
    except (RuntimeError, OSError):
        logger.debug("Project MCP checkbox picker failed", exc_info=True)
        console.print(
            "[dim]Checkbox picker unavailable; falling back to number selection.[/dim]",
            highlight=False,
        )
        return None
    except KeyboardInterrupt:
        return _TrustPromptOutcome.INTERRUPTED
    except EOFError:
        # Ctrl+D backs out of the picker, same as Esc: cancel rather than
        # silently confirm an empty selection.
        return _TrustPromptOutcome.CANCELLED


# [해설] 체크박스 UI를 못 쓸 때의 번호 입력 폴백. 빈 입력/EOF는 취소(기동 중단), `all`은 전부 기억.
def _select_project_servers_with_numbers(
    prompt_servers: Sequence["ProjectServerSummary"], console: "Console"
) -> list[str] | _TrustPromptOutcome:
    """Ask which prompted project MCP servers to remember with a text fallback.

    Args:
        prompt_servers: The `(name, kind, summary)` rows being asked about.
        console: Console to print the fallback selection UI to (stderr).

    Returns:
        The chosen server names. Empty when the user makes no valid selection;
        `CANCELLED` when the user leaves the input blank or sends EOF; and
        `INTERRUPTED` when the user presses Ctrl+C.
    """
    from rich.markup import escape

    names = [name for name, _kind, _summary in prompt_servers]
    console.print()
    for index, (name, kind, summary) in enumerate(prompt_servers, start=1):
        console.print(
            f'  [bold]{index}.[/bold] "{escape(name)}" ({escape(kind)}):  '
            f"{escape(summary)}",
            highlight=False,
        )
    try:
        raw = input("Enter numbers to remember (e.g. 1,3), 'all', or blank to abort: ")
    except KeyboardInterrupt:
        return _TrustPromptOutcome.INTERRUPTED
    except EOFError:
        return _TrustPromptOutcome.CANCELLED
    if not raw.strip():
        return _TrustPromptOutcome.CANCELLED
    if raw.strip().lower() in {"a", "all"}:
        return names
    return [
        names[index - 1] for index in _parse_server_number_selection(raw, len(names))
    ]


# [해설] 기억할 프로젝트 MCP 서버 선택 진입점: 1개면 선택 없이 그대로, 여러 개면 체크박스 → 번호 입력 순으로 폴백.
def _select_project_servers_to_persist(
    prompt_servers: Sequence["ProjectServerSummary"], console: "Console"
) -> list[str] | _TrustPromptOutcome:
    """Ask which prompted project MCP servers to remember for this project.

    Multiple prompted servers use an arrow-key checkbox picker. A single
    prompted server skips the picker because there is nothing to choose between.

    Args:
        prompt_servers: The `(name, kind, summary)` rows being asked about.
        console: Console to print the fallback selection UI to (stderr).

    Returns:
        The chosen server names. Empty when the user confirms no servers or
        makes no valid fallback selection. `CANCELLED` means the user backed out
        and the caller should abort the launch. `INTERRUPTED` means the user
        pressed Ctrl+C.
    """
    names = [name for name, _kind, _summary in prompt_servers]
    if len(names) <= 1:
        return names

    selected = _run_project_mcp_server_checkbox_picker(prompt_servers, console)
    if selected is not None:
        return selected
    return _select_project_servers_with_numbers(prompt_servers, console)


# [해설] 프로젝트 수준 MCP 설정(`.mcp.json` 등)의 서버를 신뢰할지 TUI 이전에 결정. 호출자: `cli_main`(인터랙티브 기동 전 신뢰 프롬프트 단계).
# [해설][주의] stdio뿐 아니라 원격(http/sse) 서버도 승인 대상: 공격자가 만든 `.mcp.json`이 SSRF나 `headers`의 `${VAR}` 치환으로 env를 유출할 수 있다.
# [해설] 반환: None(게이트 불필요) / True(허용) / False(거부) / INTERRUPTED / CANCELLED. 결과는 `trust_project_mcp`로 서버 설정에 전달된다.
# [해설] 관련: `analysis/07-mcp-hooks-extensions-plugins.md`, `analysis/04-approval-hitl-security.md`.
def _check_mcp_project_trust(
    *, trust_flag: bool = False
) -> (
    bool
    | Literal[
        _TrustPromptOutcome.INTERRUPTED,
        _TrustPromptOutcome.CANCELLED,
    ]
    | None
):
    """Check whether project-level MCP servers should be trusted.

    Both stdio and remote (http/sse) project entries require approval —
    remote entries from an attacker-controlled `.mcp.json` can SSRF or
    exfiltrate environment variables via `${VAR}` interpolation in their
    `headers`, so they are gated identically to stdio commands.

    When the project has no servers in project-level configs, returns
    `None` (no gate needed). When `--trust-project-mcp` was passed,
    returns `True`. Otherwise it shows an inline action selector for unresolved
    servers: allow once, remember selected servers, or deny. Remembered approvals
    are scoped to this project and each exact server definition. The remember
    picker starts with nothing selected; Esc or Ctrl+D in either picker aborts
    the launch, and no server loads without an explicit allow action.

    Servers already resolved by the user's scoped approvals, the
    `DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS` env allowlist, or the
    `disabled_project_servers` list are not prompted for (approved ones load when
    the project/fingerprint still matches; env-enabled ones load by name; disabled
    ones never load).
    `None` is returned when that leaves nothing to decide. If the user's own
    allow/deny policy cannot be read, returns `False` (fail closed) rather than
    prompting under an unknown deny list.

    Args:
        trust_flag: Whether `--trust-project-mcp` was passed.

    Returns:
        `True` to allow project servers, `False` to deny (including when the
            user's trust policy could not be read), `None` when there are no
            project servers whose fate this prompt decides, `INTERRUPTED` when
            the user presses Ctrl+C, or `CANCELLED` when the user presses Esc or
            Ctrl+D to abort the launch.
    """
    # [해설][흐름] 1) 프로젝트 컨텍스트 기준으로 MCP 설정 소스를 탐색. 프로젝트 수준 설정이 없으면(디버그 env 제외) 게이트 불필요 → None.
    from deepagents_code.mcp_tools import (
        MCPConfigSources,
        ProjectServerSummary,
        discover_mcp_config_sources,
        extract_project_server_summaries,
        load_merged_mcp_configs_lenient,
    )
    from deepagents_code.project_utils import ProjectContext

    debug_prompt = _debug_mcp_project_trust_enabled()

    try:
        project_context = ProjectContext.from_user_cwd(Path.cwd())
        config_sources = discover_mcp_config_sources(project_context=project_context)
    except (OSError, RuntimeError):
        logger.debug(
            "Could not discover MCP configs for project trust check",
            exc_info=True,
        )
        return None

    project_configs = MCPConfigSources.from_sources(config_sources).project_paths
    if not project_configs and not debug_prompt:
        return None

    # [해설][흐름] 2) 사용자 홈 설정의 허용/거부 목록(`model_config.load_mcp_server_trust_lists`)을 먼저 읽고,
    # [해설] 런타임 로더와 같은 우선순위 규칙으로 병합(`load_merged_mcp_configs_lenient`)해 프롬프트 대상 서버 집합을 만든다.
    # Read the user's allow/deny policy before parsing project configs. Session
    # approval grants whole-config trust, so the prompt must be built from the
    # same server set the runtime would retain under that decision: explicitly
    # disabled entries are removed before they can invalidate a sibling.
    from deepagents_code.model_config import load_mcp_server_trust_lists

    trust_lists = load_mcp_server_trust_lists()

    # Resolve precedence before per-server validation, matching the runtime
    # loader. Otherwise one malformed lower-precedence definition can hide its
    # valid siblings from this prompt even when a higher-precedence config
    # replaces the malformed entry and runtime would activate those siblings.
    merged_config = load_merged_mcp_configs_lenient(
        project_configs, disabled_servers=trust_lists.disabled
    ) or {"mcpServers": {}}
    all_servers = extract_project_server_summaries(merged_config)
    raw_server_configs = merged_config.get("mcpServers", {})
    server_configs = raw_server_configs if isinstance(raw_server_configs, dict) else {}
    project_root = project_context.project_root or project_context.user_cwd

    # [해설] 디버그 env가 켜져 있으면 가짜 샘플 서버 행을 넣어 프롬프트 UI를 시험할 수 있게 한다(실제 저장은 하지 않음, 아래 `saved` 참고).
    if not all_servers and debug_prompt:
        all_servers = [
            ProjectServerSummary(
                "debug-project-mcp",
                "stdio",
                "uvx deepagents-debug-mcp --sample-project-server",
            )
        ]

    if not all_servers:
        return None

    # [해설][흐름] 3) `--trust-project-mcp`면 프롬프트 없이 허용.
    if trust_flag:
        return True

    # [해설][흐름] 4) 서버별 분류: disabled 목록 → 제외, 프로젝트 루트+정의 fingerprint가 일치하는 기존 승인·env 허용 → 제외, 나머지만 질문.
    # [해설][주의] 허용/거부 정책은 홈 설정에서만 읽는다(저장소 안 파일은 신뢰 경계 밖).
    # Partition by the user's own allow/deny policy (read only from home config,
    # never the repo — the same boundary the loader enforces). Scoped approvals
    # load only while the project root and server fingerprint match; disabled
    # names never load. The prompt asks only about unresolved servers.
    from rich.console import Console as _Console
    from rich.markup import escape

    prompt_console = _Console(stderr=True)
    prompt_servers: list[ProjectServerSummary] = []
    for summary_row in all_servers:
        name, _kind, _summary = summary_row
        # Disabled first: reject precedence (a name in both lists is disabled).
        if name in trust_lists.disabled:
            continue
        if trust_lists.is_enabled(
            name,
            project_root=project_root,
            server=server_configs.get(name, {}),
        ):
            continue
        prompt_servers.append(summary_row)

    # [해설][흐름] 5) 사용자 정책 파일을 못 읽으면 fail-closed(False). 모르는 거부 목록 아래에서 허용을 저장하지 않기 위함.
    if trust_lists.read_error is not None:
        # The user's allow/deny policy could not be read. Fail closed here too
        # (matching the loader, which forces the config untrusted) instead of
        # prompting and possibly persisting an allow-list entry under an unknown
        # deny list. Any env-enabled names still load — the loader re-applies the
        # lists downstream — but nothing is approved via this prompt.
        prompt_console.print(
            f"[yellow]Warning: {escape(trust_lists.read_error)}; treating "
            "project MCP servers as untrusted.[/yellow]",
            highlight=False,
            soft_wrap=True,
        )
        return False

    if not prompt_servers:
        return None

    # [해설][흐름] 6) 미해결 서버 목록 표시 → 공용 선택기. 거부=False, 한 번 허용=True(저장 0개).
    prompt_console.print()
    prompt_console.print("[bold yellow]Approve project MCP servers:[/bold yellow]")
    for name, kind, summary in prompt_servers:
        prompt_console.print(
            f'  [bold]"{escape(name)}"[/bold] ({escape(kind)}):  {escape(summary)}'
        )
    prompt_console.print()

    server_count = len(prompt_servers)
    noun = "server" if server_count == 1 else "servers"
    action = _select_trust_action(prompt_console)
    if action is _TrustPromptOutcome.INTERRUPTED:
        return _TrustPromptOutcome.INTERRUPTED
    if action is _TrustPromptOutcome.CANCELLED:
        return _TrustPromptOutcome.CANCELLED
    if action is _TrustAction.DENY:
        prompt_console.print(
            f"[dim]Denied {server_count} project MCP {noun}.[/dim]",
            highlight=False,
        )
        return False
    if action is _TrustAction.ALLOW_ONCE:
        prompt_console.print(
            f"[dim]Allowing {server_count} project MCP {noun} for this "
            "session; remembering 0.[/dim]",
            highlight=False,
        )
        return True

    # [해설][흐름] 7) "기억" 선택: 저장할 서버를 고르고 프로젝트 루트·서버 정의와 함께 홈 설정에 기록. 저장 실패해도 이번 세션은 허용.
    from deepagents_code.model_config import add_enabled_project_mcp_servers

    names = _select_project_servers_to_persist(prompt_servers, prompt_console)
    if names is _TrustPromptOutcome.INTERRUPTED:
        return _TrustPromptOutcome.INTERRUPTED
    if names is _TrustPromptOutcome.CANCELLED:
        return _TrustPromptOutcome.CANCELLED
    if not names:
        prompt_console.print(
            f"[dim]No servers selected; denied {server_count} project MCP "
            f"{noun}.[/dim]",
            highlight=False,
        )
        return False

    saved = debug_prompt or add_enabled_project_mcp_servers(
        names,
        project_root=project_root,
        server_configs=server_configs,
    )
    remembered_count = len(names) if saved else 0
    if not saved:
        prompt_console.print(
            "[yellow]Approved for this session, but the choice could not be "
            "remembered — you'll be asked again next time.[/yellow]",
            highlight=False,
        )
    prompt_console.print(
        f"[dim]Allowing {server_count} project MCP {noun} for this session; "
        f"remembering {remembered_count} for this project.[/dim]",
        highlight=False,
    )
    return True


# [해설] hooks 신뢰 프롬프트의 "영구 허용" 라벨. 테스트/다른 곳과 문구 일치를 위해 상수로 둔 것(추정).
_PROJECT_HOOKS_REMEMBER_LABEL = "Always allow hooks in this project"


# [해설] 프로젝트 `.deepagents/hooks.json`의 command 핸들러(임의 셸 명령) 실행 신뢰를 결정. 호출자: `cli_main` 인터랙티브 기동 전.
# [해설] Boolean이 아닌 `hooks.trust.WorkspaceTrust` 정책을 반환: 허가는 기동 워크스페이스에만 적용되고, 세션 중 이동한 디렉터리는
# [해설] 영구 신뢰 저장소로 다시 판정한다. hooks는 클라이언트 프로세스에서 실행된다. 관련: `analysis/07-mcp-hooks-extensions-plugins.md`.
def _check_project_hooks_trust(
    *,
    trust_flag: bool = False,
) -> "WorkspaceTrust | _TrustPromptOutcome":
    """Resolve interactive trust for project-scoped hook commands.

    Returns a policy rather than a Boolean so the decision keeps its scope: the
    grant applies to the launch workspace, and directories the session later
    moves into are re-resolved against the persisted trust store.

    Args:
        trust_flag: Whether the CLI explicitly trusted project hooks.

    Returns:
        The trust policy to run the session under, `INTERRUPTED` when the user
        presses Ctrl+C, or `CANCELLED` when the user presses Esc or Ctrl+D to
        abort startup.
    """
    from rich.console import Console

    from deepagents_code.hooks.loading import project_hooks_path, user_hooks_path
    from deepagents_code.hooks.trust import (
        WorkspaceTrust,
        is_project_hooks_trusted,
        trust_project_hooks,
    )
    from deepagents_code.project_utils import ProjectContext

    # [해설][흐름] 1) 프로젝트 hooks 파일 위치 확인. 홈 디렉터리의 사용자 hooks 파일과 같은 경로면(홈에서 실행) 프로젝트 신뢰 대상이 아니다.
    try:
        context = ProjectContext.from_user_cwd(Path.cwd())
        project_root = context.project_root or context.user_cwd
        config_path = project_hooks_path(project_root)
        if config_path.resolve(strict=False) == user_hooks_path().resolve(strict=False):
            # Running from the user config's parent makes the user hooks path
            # look project-scoped. It needs no trust decision and must not be
            # granted project trust under the wrong provenance.
            return WorkspaceTrust.none()
        if not config_path.is_file():
            return WorkspaceTrust.none()
    except OSError:
        logger.warning("Could not inspect project hooks configuration", exc_info=True)
        return WorkspaceTrust.none()

    # [해설][흐름] 2) `--trust-project-hooks` 또는 이미 저장된 신뢰가 있으면 즉시 허가. 3) 없으면 경고와 함께 선택기(거부 시 `WorkspaceTrust.none()`).
    granted = WorkspaceTrust.for_session(project_root, granted=True)
    if trust_flag or is_project_hooks_trusted(project_root):
        return granted

    from rich.markup import escape

    prompt_console = Console(stderr=True)
    prompt_console.print()
    prompt_console.print(
        "[bold yellow]Project hooks can run arbitrary shell commands on your "
        "machine.[/bold yellow]",
        highlight=False,
    )
    prompt_console.print(f"Hooks file: {escape(str(config_path))}", highlight=False)
    prompt_console.print(
        "Only trust projects you control. Allow once runs this file as it is "
        f'now; always allow trusts "{escape(str(project_root))}" for future '
        "sessions and future edits.",
        style="yellow",
        highlight=False,
    )
    action = _select_trust_action(
        prompt_console,
        remember_label=_PROJECT_HOOKS_REMEMBER_LABEL,
    )
    if action is _TrustPromptOutcome.INTERRUPTED:
        return action
    if action is _TrustPromptOutcome.CANCELLED:
        return action
    if action is _TrustAction.ALLOW_ONCE:
        prompt_console.print(
            "[dim]Allowing project hooks for this session.[/dim]",
            highlight=False,
        )
        return granted
    if action is _TrustAction.REMEMBER:
        if not trust_project_hooks(project_root):
            prompt_console.print(
                "[yellow]Project hook trust could not be remembered; "
                "allowing this session only.[/yellow]",
                highlight=False,
            )
        else:
            prompt_console.print(
                f'[dim]Hooks for "{escape(str(project_root))}" will run without '
                "asking from now on.[/dim]",
                highlight=False,
            )
        return granted
    prompt_console.print(
        "[dim]Project hooks skipped.[/dim]",
        highlight=False,
    )
    return WorkspaceTrust.none()


# [해설] 프로젝트 `.deepagents/extensions/` Python 확장(임의 Python 실행) 로드 신뢰 결정. `DEEPAGENTS_CODE_EXPERIMENTAL`이 꺼져 있으면 항상 False.
# [해설] 확장 설정의 `TrustPolicy`(NEVER/ALWAYS/프롬프트)와 저장된 프로젝트 신뢰, `--trust-project-extensions`를 차례로 본다.
# [해설] 결과는 `trust_project_extensions`로 서버 설정에 전달되고 서버 프로세스가 확장을 로드한다(analysis 01 흐름 E).
def _check_project_extensions_trust(
    *,
    trust_flag: bool = False,
) -> "bool | _TrustPromptOutcome":
    """Resolve interactive trust for project-authored Python extensions.

    Args:
        trust_flag: Whether the CLI explicitly trusted project extensions.

    Returns:
        Whether project extensions may load, `INTERRUPTED` on Ctrl+C, or
            `CANCELLED` when startup is aborted.
    """
    from deepagents_code._env_vars import EXPERIMENTAL, is_env_truthy

    if not is_env_truthy(EXPERIMENTAL):
        return False
    from rich.console import Console

    from deepagents_code.extensions.discovery import project_extensions_dir
    from deepagents_code.extensions.settings import (
        TrustPolicy,
        load_extension_settings,
    )
    from deepagents_code.extensions.trust import (
        is_project_extensions_trusted,
        trust_project_extensions,
    )
    from deepagents_code.project_utils import ProjectContext

    settings = load_extension_settings()
    if not settings.enabled or settings.trust is TrustPolicy.NEVER:
        return False

    try:
        context = ProjectContext.from_user_cwd(Path.cwd())
        project_root = context.project_root or context.user_cwd
        extensions_dir = project_extensions_dir(project_root)
        if not extensions_dir.is_dir():
            return False
    except OSError:
        logger.warning("Could not inspect project extensions", exc_info=True)
        return False

    if (
        trust_flag
        or settings.trust is TrustPolicy.ALWAYS
        or is_project_extensions_trusted(project_root)
    ):
        return True

    from rich.markup import escape

    console = Console(stderr=True)
    console.print()
    console.print(
        "[bold yellow]Project extensions run arbitrary Python in this "
        "session.[/bold yellow]",
        highlight=False,
    )
    console.print(
        f"Extensions directory: {escape(str(extensions_dir))}", highlight=False
    )
    console.print(
        "Only trust projects you control. Allow once loads these files as they "
        f'are now; always allow trusts "{escape(str(project_root))}" for future '
        "sessions and future edits.",
        style="yellow",
        highlight=False,
    )
    action = _select_trust_action(
        console,
        remember_label="Always allow extensions in this project",
    )
    if action in {
        _TrustPromptOutcome.INTERRUPTED,
        _TrustPromptOutcome.CANCELLED,
    }:
        return action
    if action is _TrustAction.ALLOW_ONCE:
        console.print(
            "[dim]Allowing project extensions for this session.[/dim]",
            highlight=False,
        )
        return True
    if action is _TrustAction.REMEMBER:
        if not trust_project_extensions(project_root):
            console.print(
                "[yellow]Extension trust could not be remembered; allowing "
                "this session only.[/yellow]",
                highlight=False,
            )
        return True
    console.print("[dim]Project extensions skipped.[/dim]", highlight=False)
    return False


# [해설] 인터프리터가 활성일 때 `langchain-quickjs` 등 의존성을 서버 서브프로세스 기동 **전에** 확인해 한 줄 오류로 exit 1.
# [해설] 서버 쪽에서 실패하면 "Server process exited with code N" 같은 불투명한 오류가 되기 때문.
def _verify_interpreter_or_exit() -> None:
    """Run the interpreter pre-flight check; print and exit on failure.

    Called before spawning the langgraph dev server subprocess so a missing
    `langchain-quickjs` dependency surfaces a one-line, actionable hint instead
    of an opaque "Server process exited with code N" downstream. Gated on the
    resolved interpreter state (`_resolve_interpreter_enabled`), not the
    `--interpreter` flag alone, since the interpreter is now on by default.
    """
    from deepagents_code.extras_info import verify_interpreter_deps

    try:
        verify_interpreter_deps()
    except ImportError as exc:
        from rich.markup import escape

        from deepagents_code.config import console

        console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        sys.exit(1)


# [해설] 설정 해석기의 계층(provider)별 상태(순위 → 건강 상태)를 반환. `_apply_managed_runtime_exceptions`의 불변식 검사에 사용.
def _config_provider_statuses() -> Mapping[int, "ProviderStatus"]:
    """Return shared provider health without resolving an option."""
    from deepagents_code.configuration.resolver import get_config_resolver

    return get_config_resolver().provider_statuses()


# [해설] `[retries]` 설정 진단을 stderr에 출력. 이후 어떤 분기(ACP 등)가 먼저 종료해도 사용자가 보도록 `cli_main` 초반에 호출.
def _print_retry_config_warnings(warnings: list[str]) -> None:
    """Print `[retries]` diagnostics to stderr, before any branch can exit."""
    if not warnings:
        return
    from rich.console import Console as _Console
    from rich.text import Text as _Text

    stderr_console = _Console(stderr=True)
    for warning in warnings:
        stderr_console.print(
            _Text.assemble(("Warning:", "bold yellow"), " ", warning),
            soft_wrap=True,
            highlight=False,
        )


# [해설] 이번 실행이 쓸 모델 제공자 이름을 LangChain import 없이 추정. 재시도 설정 경고의 범위를 좁히는 용도로만 쓰며 실패는 None.
def _startup_model_provider(args: argparse.Namespace) -> str | None:
    """Return the provider this run will build, without importing langchain.

    Used only to scope startup diagnostics, so an unresolvable spec is not an
    error here: the launch path reports it with far better context.

    Returns:
        The effective provider name, or `None` when it cannot be resolved yet.
    """
    from deepagents_code.config import _get_default_model_spec, detect_provider
    from deepagents_code.model_config import ModelSpec

    try:
        spec = getattr(args, "model", None) or _get_default_model_spec()
    except Exception:  # noqa: BLE001  # diagnostics only; the launch path reports
        return None
    if not spec:
        return None
    parsed = ModelSpec.try_parse(spec)
    return parsed.provider if parsed else detect_provider(spec)


# [해설] 관리형(managed, 관리자 배포) 정책 값을 `args`에 직접 강제하는 예외 경로. 대부분의 정책은 해석기 체인으로 읽지만,
# [해설] 네임스페이스를 직접 읽는 소비자(모델·샌드박스 등)를 위해 여기서 덮어쓴다. 호출자: `cli_main`(서브커맨드 없는 세션 실행).
# [해설][주의] `ENFORCED_MANAGED_KEYS` 강제 지점: 이 프로세스가 적용할 수 없는 관리형 값이면 exit 78(EX_CONFIG)로 기동을 중단한다.
# [해설] 관련: `analysis/03-config-models-credentials.md`.
def _apply_managed_runtime_exceptions(args: argparse.Namespace) -> None:
    """Force managed values into `args` for consumers that bypass the resolver.

    These are exceptions to the rule that policy is read through the ranked
    chain: a handful of launch-orchestration values must reach `args` itself,
    because their downstream consumer reads the namespace rather than the
    resolver.

    Also the enforcement point for `ENFORCED_MANAGED_KEYS`: a managed value
    this process cannot actually enforce stops the launch via
    `managed_policy_violations` and `sys.exit(78)`, rather than starting with
    policy silently unapplied.

    Args:
        args: Parsed CLI arguments, mutated in place.

    Raises:
        AssertionError: If managed policy is unusable or the resolver has no
            managed provider, meaning the startup health gate did not run first.
    """
    from deepagents_code.configuration.resolver import MANAGED_RANK
    from deepagents_code.configuration.service import (
        get_managed_snapshot,
        managed_policy_violations,
        resolve_managed_option,
    )
    from deepagents_code.configuration.types import Found, Invalid, Unset

    # [해설][흐름] 1) 불변식 검사: 해석기에 managed 계층이 있고 스냅샷이 사용 가능해야 한다(앞선 `_require_managed_config_or_exit` 게이트 전제).
    # [해설] 사용 불가 스냅샷은 빈 dict라 "정책 없음"으로 오인해 fail-open이 되므로 AssertionError로 막는다.
    snapshot = get_managed_snapshot()
    if MANAGED_RANK not in _config_provider_statuses():
        msg = "shared resolver has no managed provider"
        raise AssertionError(msg)
    if not snapshot.status.usable:
        # `_require_managed_config_or_exit` ran ~35 lines earlier in `cli_main`,
        # so this is unreachable. Assert it rather than inferring health from an
        # empty table: an unusable snapshot carries `{}`, so the `if not
        # managed_data: return` below would read "no policy to apply" and leave
        # every user flag in force — the exact fail-open this function prevents.
        # The ordering is a cross-module invariant with nothing else enforcing it.
        msg = (
            "managed policy is unusable when runtime policy is applied; the "
            f"startup gate must run first (health: {snapshot.status.health.value})"
        )
        raise AssertionError(msg)
    managed_data = snapshot.data
    if not managed_data:
        return

    # [해설][흐름] 2) 키별 managed 계층 해석 결과를 캐시하는 작은 헬퍼들(선언 여부 / 유효값).
    managed_results: dict[str, object] = {}

    def managed_result(key: str) -> object:
        """Return the managed tier's typed provider result for `key`."""
        if key not in managed_results:
            resolved = resolve_managed_option(
                key,
                managed_data,
                status=snapshot.status,
            )
            managed_results[key] = (
                resolved.tier_health.get(MANAGED_RANK, Unset())
                if resolved is not None
                else Unset()
            )
        return managed_results[key]

    def declared(key: str) -> bool:
        """Return whether managed policy sets `key`, valid or not."""
        return isinstance(managed_result(key), (Found, Invalid))

    def managed_value(key: str) -> tuple[bool, object]:
        """Resolve one key against managed policy alone.

        Returns:
            Whether managed policy decided the value, and the value.
        """
        result = managed_result(key)
        return (True, result.value) if isinstance(result, Found) else (False, None)

    # [해설][흐름] 3) 강제 불가 위반이 있으면 exit 78.
    violations = managed_policy_violations(managed_data, status=snapshot.status)
    if violations:
        sys.stderr.write(
            "Error: managed config rejects "
            f"{', '.join(violations)}. "
            "Ask your administrator to correct the value.\n"
        )
        sys.stderr.flush()
        sys.exit(78)

    # [해설][흐름] 4) 키별 적용: 분류기 모델·PTC는 플래그를 비워 해석기(managed 우선)가 결정하게 하고, `models.default`는 `args.model`에 대입,
    # [해설] `sandboxes.default`는 `_apply_managed_sandbox`로 처리.
    # `models.auto_classifier` is cleared rather than assigned: with the flag
    # unset, `build_server_config` falls through to
    # `resolve_auto_classifier_model_with_problem`, which resolves the managed
    # tier first. Assigning the managed spec onto the flag made the server name
    # a flag the user never passed, and `--auto-classifier-model` without
    # `--auto-approve` exits 2 in ACP mode — the same failure the
    # `startup.mode` block below avoids.
    if declared("models.auto_classifier") and hasattr(args, "auto_classifier_model"):
        args.auto_classifier_model = None

    model_found, model = managed_value("models.default")
    if model_found and hasattr(args, "model"):
        args.model = model

    if declared("interpreter.ptc") and hasattr(args, "interpreter_tools"):
        args.interpreter_tools = None

    _apply_managed_sandbox(args, managed_value("sandboxes.default"))


# [해설] 관리형 `sandboxes.default`를 적용: 이미 샌드박스를 쓰는 실행에서만 백엔드를 고정한다(키 의미는 "강제 격리"가 아니라 "기본 백엔드").
# [해설] 샌드박스 없이 실행하면 호스트에서 돈다는 Note만 출력. 관리형 값이 이 머신에서 사용 불가하면 exit 78.
def _apply_managed_sandbox(
    args: argparse.Namespace, resolved: tuple[bool, object]
) -> None:
    """Pin the sandbox backend when the launch already uses a sandbox.

    `sandboxes.default` names the backend a bare `--sandbox` selects; omitting
    the flag runs unsandboxed. Assigning it unconditionally would force every
    launch into a sandbox, which the key does not mean, so a launch that asked
    for no sandbox is left alone. A bare `--sandbox` needs no assignment here
    either: `SandboxRegistry.load` reads merged managed config, so
    `registry.default` already carries the managed value.

    The value is checked against the registry first. `parse_args` validates
    `--sandbox`, but it runs before managed policy is applied, so an
    unavailable managed name would otherwise skip `is_available` and reach the
    sandbox factory instead of producing a curated error.

    A launch that bypasses a named managed backend prints a note. The key does
    not force containment, and an administrator who read it as if it did would
    otherwise see an unsandboxed launch with no diagnostic at all.
    """
    found, value = resolved
    # `--sandbox` defaults to the string `"none"`, so an omitted flag never
    # leaves `args.sandbox` as `None`. Both spellings mean "no sandbox" to
    # `_resolve_and_validate_sandbox`, and this must match it: checking only
    # `None` forces a sandbox onto a launch that asked for none.
    if not found:
        return
    if getattr(args, "sandbox", None) in {None, "none"}:
        # Policy named a backend and this launch is not using one. That is the
        # documented meaning of the key, but an administrator who set it
        # believing it forces containment would otherwise get an unsandboxed
        # launch, exit 0, and a green `dcode doctor` row.
        if isinstance(value, str) and value not in {"", "none"}:
            sys.stderr.write(
                f"Note: managed config sets [sandboxes].default to '{value}', "
                "which names the backend for a sandboxed launch. This launch "
                "asked for no sandbox, so it runs on the host. Pass --sandbox "
                "to use the managed backend.\n"
            )
            sys.stderr.flush()
        return
    if not isinstance(value, str) or not value:
        return
    if value != "none":
        from deepagents_code.integrations.sandbox_registry import SandboxRegistry

        registry = SandboxRegistry.load()
        if not registry.is_available(value):
            available = ", ".join(registry.available_providers())
            sys.stderr.write(
                f"Error: managed config sets [sandboxes].default to '{value}', "
                "which is not available on this machine.\n"
                f"Available providers: {available}.\n"
                "Ask your administrator to correct the value.\n"
            )
            sys.stderr.flush()
            sys.exit(78)
    args.sandbox = value


# [해설] 관리형 설정 파일이 존재하지만 건강하지 않으면(파싱 실패 등) 에이전트 기동을 exit 78로 거부하는 게이트.
# [해설] 호출자: `cli_main` — `config`/`auth path`/`doctor`(진단 도구)와 `help`를 제외한 모든 명령 전에 실행.
def _require_managed_config_or_exit() -> None:
    """Fail an agent launch when present managed policy cannot be enforced."""
    from deepagents_code.configuration.service import (
        ManagedConfigError,
        require_healthy_managed_config,
    )

    try:
        require_healthy_managed_config(refresh=True)
    except ManagedConfigError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        sys.stderr.flush()
        sys.exit(78)


# [해설] ★ dcode 최상위 디스패처(콘솔 스크립트 진입점). 호출 경로: `dcode` → `deepagents_code.__getattr__("cli_main")` → 이 함수.
# [해설] 큰 흐름: 환경 준비 → `parse_args` → fast path(help/config/auth path/doctor) → managed 게이트 → tools/install/uninstall
# [해설] → 레거시 마이그레이션·자격 증명(dotenv) 부트스트랩 → JSON 플래그 검증 → `--acp` 분기 → `apply_stdin_pipe` → 플래그 조합 검증
# [해설] → update/default-model 등 세션 없는 명령 → 서브커맨드(agents/skills/plugin/mcp/threads) → 헤드리스(`run_non_interactive`) 또는 인터랙티브(TUI).
# [해설] 관련: `analysis/01-boot-client-server.md` "A. 공통 부팅"·"B. 인터랙티브"·"C. 헤드리스" 및 flowchart.
def cli_main() -> None:
    """Entry point for console script.

    Raises:
        SystemExit: On shutdown, with the session's exit code (0 on success,
            1 on error, 128+signum when a terminating signal unwound the
            process).
        KeyboardInterrupt: Re-raised out of the TUI teardown block so the
            outer handler can print the interruption notice and exit 130.
    """
    # [해설][흐름] 1) 프로세스 환경 준비: macOS gRPC fork 문제 회피 env, `TERM_PROGRAM` 스냅샷(dotenv 로드 전).
    # Fix for gRPC fork issue on macOS
    # https://github.com/grpc/grpc/issues/37642
    if sys.platform == "darwin":
        os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "0"

    # Snapshot `TERM_PROGRAM` before settings bootstrap loads any `.env` file,
    # so the resume hint echoes the variable only when the launch environment
    # (inline prefix, terminal export, or shell alias) supplied it. The app
    # itself never sets `TERM_PROGRAM`, and the update re-exec inherits this
    # sentinel, so a set value here always marks an explicit launch value.
    if "TERM_PROGRAM" in os.environ and LAUNCH_TERM_PROGRAM not in os.environ:
        os.environ[LAUNCH_TERM_PROGRAM] = os.environ["TERM_PROGRAM"]

    # Note: LANGSMITH_PROJECT override is handled lazily by config.py's
    # _ensure_bootstrap() (triggered on first access of `settings`).
    # This ensures agent traces use DEEPAGENTS_CODE_LANGSMITH_PROJECT while
    # shell commands use the user's original LANGSMITH_PROJECT.

    # [해설][흐름] 2) `dcode -v` 단독이면 argparse도 만들지 않고 즉시 버전 출력 후 종료.
    # Fast path: print version without loading heavy dependencies
    if len(sys.argv) == 2 and sys.argv[1] in {"-v", "--version"}:  # noqa: PLR2004  # argv length check for fast-path
        print(build_version_text())  # noqa: T201  # Version output
        sys.exit(0)

    # Note shim/alias launches for the Debug Console only; never prints.
    from deepagents_code._invocation import log_nonstandard_invoked_name

    log_nonstandard_invoked_name()

    # [해설][흐름] 3) 의존성 확인(ACP 제외)과 종료 시그널 핸들러 설치(분리 세션 서버의 정리 보장).
    # ACP mode does not require Textual, so skip UI dependency checks when
    # the flag is present in raw argv.
    if "--acp" not in sys.argv[1:]:
        check_cli_dependencies()

    # The app-owned server runs in a detached session so terminal job-control
    # signals do not suspend or kill it. Replace terminating signals' immediate
    # default behavior with an exception so the app/server cleanup finally
    # blocks run when dcode's process group is stopped.
    _install_termination_signal_handlers()

    # [해설][흐름] 4) 아래 거대한 try 블록 전체가 공통 예외 처리(KeyboardInterrupt → 130 등, 함수 끝부분)를 공유한다.
    try:
        # [해설] `_install_cli_provider`가 이 시점의 `vars(args)`를 스냅샷하므로 이후 `args` 변형은 해석기 결과에 반영되지 않는다.
        args = parse_args()
        _install_cli_provider(args)
        # [해설] 사용자가 실제로 입력한 승인 플래그 이름을 파싱 직후 기록(헤드리스에서 무시 경고 문구에 사용).
        explicit_approval_flag = (
            "--yolo"
            if getattr(args, "yolo", False)
            else "--auto-approve"
            if getattr(args, "auto_approve", False)
            else None
        )
        allow_fs_tools = _parse_allow_fs_tools_flag(
            getattr(args, "allow_fs_tools", None)
        )

        # [해설][흐름] 5) 도움말 fast path.
        if _show_bare_command_group_help(args):
            return

        # [해설][흐름] 6) 설정 부트스트랩 전 fast path: 서브커맨드면 편집 설치 의존성 경고 후, `config`/`auth path`/`doctor`는
        # [해설] managed 게이트보다 **먼저** 실행한다(깨진 managed 파일을 진단할 수단을 남기기 위함).
        # Keep self-contained commands that do not need global settings here, before
        # state migration and settings bootstrap. If a future command only reads
        # local files or delegates bootstrap to specific subcommands, dispatch it here
        # so lightweight diagnostic paths stay fast.
        # Use `getattr` because this fast-path block is for optional top-level
        # subcommands only. ACP/root-mode invocations may not define `command`,
        # and should fall through to the later handlers instead of raising here.
        command = getattr(args, "command", None)
        if command is not None:
            from deepagents_code._dep_floor_check import warn_if_editable_deps_stale

            warn_if_editable_deps_stale()

        if command == "config":
            from deepagents_code.client.commands.config import run_config_command

            sys.exit(run_config_command(args))

        if command == "auth" and getattr(args, "auth_command", None) == "path":
            from deepagents_code.client.commands.auth import run_auth_command

            sys.exit(run_auth_command(args))

        if command == "doctor":
            from deepagents_code.doctor import run_doctor_command

            sys.exit(run_doctor_command(args))

        # [해설][흐름] 7) managed 정책 건강 게이트(exit 78). 이후 tools/install/uninstall 명령 처리.
        # Every remaining command can read or act on managed policy, so none
        # may run while that policy is unenforceable. `config`, `doctor`, and
        # `auth path` returned above because they are the tools for diagnosing
        # a broken managed file. Gating them would leave an administrator no way
        # to see what is wrong. `help` reads no policy.
        if command != "help":
            _require_managed_config_or_exit()

        if command == "tools":
            from deepagents_code.client.commands.tools import run_tools_command

            sys.exit(run_tools_command(args))

        if command == "install":
            from deepagents_code.client.commands.extras import run_install_command

            sys.exit(run_install_command(args))

        if command == "uninstall":
            from deepagents_code.client.commands.extras import run_uninstall_command

            sys.exit(run_uninstall_command(args))

        # [해설][흐름] 8) 레거시 state 디렉터리 마이그레이션(실패해도 계속).
        # Best-effort, idempotent migration. Placed after parse_args and the
        # bare-help fast path so --help / --version / `deepagents <group>`
        # exit before any I/O. Wrapped broadly so an unexpected non-OSError
        # (e.g., RuntimeError from `Path.home()` when $HOME is unset on a CI
        # runner) cannot crash startup — state migration has zero functional
        # value vs. failing-soft.
        try:
            from deepagents_code.state_migration import migrate_legacy_state

            migrate_legacy_state()
        except Exception:
            logger.warning(
                "Legacy state migration failed unexpectedly; continuing.",
                exc_info=True,
            )

        # [해설][흐름] 9) 자격 증명 부트스트랩: `config._get_credentials()`가 `_ensure_bootstrap()`을 트리거해 .env를 로드한다.
        # [해설] 여기서 로드된 env는 이후 띄울 서버 프로세스로 상속되지만, 서버 쪽 `_build_server_env`가 dotenv 유래 값을 제거한다(analysis 01 설계 5).
        # Initialize credentials AFTER arg parsing and after the bare-help
        # fast path so neither argparse's `--help`/`-h` exit nor
        # `deepagents <group>` pays the credentials bootstrap cost. The explicit
        # accessor triggers `_ensure_bootstrap()` (dotenv loading), and
        # commands dispatched below — notably `auth status` — resolve
        # credentials from the environment expecting `.env` to be loaded.
        from deepagents_code.config import _get_credentials, console

        _get_credentials()

        # [해설][흐름] 10) 세션 실행(서브커맨드 없음)이면 managed 정책 값을 `args`에 강제(심층 방어).
        if command is None:
            # The health gate already ran above, for every command, so the
            # violation check inside cannot fire. Kept as defense in depth: it
            # is the only thing standing between a future entry point that
            # forgets the gate and a launch that silently ignores policy.
            _apply_managed_runtime_exceptions(args)

        if command == "auth":
            from deepagents_code.client.commands.auth import run_auth_command

            sys.exit(run_auth_command(args))

        # [해설][흐름] 11) `--model-params` JSON 검증(객체여야 함, 아니면 exit 1).
        model_params: dict[str, Any] | None = None
        raw_kwargs = getattr(args, "model_params", None)
        if raw_kwargs:
            try:
                model_params = json.loads(raw_kwargs)
            except json.JSONDecodeError as e:
                console.print(
                    f"[bold red]Error:[/bold red] --model-params is not valid JSON: {e}"
                )
                sys.exit(1)
            if not isinstance(model_params, dict):
                console.print(
                    "[bold red]Error:[/bold red] --model-params must be a JSON object"
                )
                sys.exit(1)

        # [해설][흐름] 12) 재시도 설정 진단. dcode는 모델 노드 미들웨어가 재시도 예산을 소유하므로 `create_model`이 제공자 자체 재시도 kwarg를
        # [해설] 비활성 값으로 강제한다. 사용자가 `--model-params`로 준 재시도 값은 무시된다는 경고를 출력.
        from deepagents_code.config import collect_retry_config_startup

        # Scoped to the provider this run will actually build: `create_model`
        # forces that provider's retry kwarg and forwards every other kwarg
        # untouched, so a registry-wide set would report an override that never
        # happens.
        retry_config_warnings, forced_retry_params = collect_retry_config_startup(
            _startup_model_provider(args),
            model_params if isinstance(model_params, dict) else None,
        )
        # Reported here rather than beside the TUI launch: every later branch
        # can exit first -- ACP does -- and `_read_retry_config` only logs into
        # the debug buffer, which the user never sees.
        _print_retry_config_warnings(retry_config_warnings)

        # dcode's model-node middleware owns the retry budget, so `create_model`
        # forces the provider's own retry kwarg to its disable value. A
        # `--model-params` retry count is therefore always overridden. Say so
        # here: the override is logged into the debug buffer, which the user
        # never sees, and a silently ignored explicit flag reads as a bug.
        if isinstance(model_params, dict):
            supplied = sorted(forced_retry_params & set(model_params))
            if supplied:
                from rich.console import Console as _Console
                from rich.text import Text as _Text

                # Assembled rather than markup: the remediation names the
                # `[retries]` table, which Rich would parse as a style tag and
                # drop -- deleting the fix the warning exists to deliver.
                _Console(stderr=True).print(
                    _Text.assemble(
                        ("Warning:", "bold yellow"),
                        f" --model-params {', '.join(supplied)} is ignored; "
                        "dcode owns the retry budget. Use --max-retries or "
                        "[retries].max_retries in config.toml instead.",
                    ),
                    soft_wrap=True,
                    highlight=False,
                )

        # [해설][흐름] 13) 요약 모델을 모든 모드에 공통으로 한 번 해석하고, `--profile-override` JSON 검증.
        max_retries = getattr(args, "max_retries", None)

        # Resolved once here rather than per launch mode, so every mode below
        # receives the same already-resolved spec.
        resolved_summarization_model = _resolve_summarization_model(
            getattr(args, "summarization_model", None)
        )

        profile_override: dict[str, Any] | None = None
        raw_profile = getattr(args, "profile_override", None)
        if raw_profile:
            try:
                profile_override = json.loads(raw_profile)
            except json.JSONDecodeError as e:
                console.print(
                    "[bold red]Error:[/bold red] "
                    f"--profile-override is not valid JSON: {e}"
                )
                sys.exit(1)
            if not isinstance(profile_override, dict):
                console.print(
                    "[bold red]Error:[/bold red] "
                    "--profile-override must be a JSON object"
                )
                sys.exit(1)

        # [해설][흐름] 14) ★ `--acp` 분기: LangGraph 서버 없이 같은 프로세스에서 ACP 서버 실행 후 exit.
        # [해설] 승인 모드는 raw 플래그가 아니라 해석된 모드로 판단(managed `startup.mode`가 플래그를 철회할 수 있음).
        # [해설][주의] YOLO는 TUI에서 한 번 확인·저장한 적이 없으면 ACP에서 거부(exit 2). ACP에는 확인 UI가 없기 때문.
        if getattr(args, "acp", False):
            # Raw flags are not authoritative here: an explicit `--yolo` or
            # `--auto-approve` outranks the lower tiers, but managed
            # `startup.mode` still revokes them, so every approval decision in
            # this branch reads the resolved mode rather than `args`.
            from deepagents_code.approval_mode import ApprovalMode

            approval_mode = _resolve_approval_mode(args)
            if approval_mode is ApprovalMode.YOLO:
                from deepagents_code.approval_mode import has_yolo_acknowledgement

                if not has_yolo_acknowledgement():
                    sys.stderr.write(
                        "Error: acknowledge YOLO in the interactive TUI before "
                        "using it in ACP mode.\n"
                    )
                    sys.exit(2)
            # Only Auto installs `AutoModeHITLMiddleware`, the sole consumer of
            # the classifier model: `_run_acp_cli_async` receives
            # `auto=approval_mode is ApprovalMode.AUTO`, and `create_cli_agent`
            # skips the middleware when `auto_mode_enabled` is false. Accepting
            # the flag in YOLO would launch with the model silently unused.
            if getattr(args, "auto_classifier_model", None) is not None and (
                approval_mode is not ApprovalMode.AUTO
            ):
                sys.stderr.write(
                    "Error: --auto-classifier-model requires Auto "
                    "mode in ACP mode (--auto-approve or "
                    '[startup].mode = "auto").\n'
                )
                sys.exit(2)
            # [해설] ACP 의존성(`acp`, `deepagents_acp`)은 선택 설치(extra)라 없으면 설치 안내 후 exit 1.
            assistant_id = _resolve_agent_arg(args)
            try:
                from acp import run_agent as run_acp_agent
                from deepagents_acp.server import AgentServerACP
            except ImportError as exc:
                msg = (
                    f"ACP dependencies not available: {exc}\n"
                    "Install with: uv tool install --reinstall -U deepagents-code "
                    "--with deepagents-acp\n"
                )
                sys.stderr.write(msg)
                sys.stderr.flush()
                sys.exit(1)

            if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
                msg = (
                    "Error: --no-mcp and --mcp-config are mutually exclusive."
                    " Use one or the other.\n"
                    "  dcode --mcp-config path/to/config.json\n"
                    "  dcode --no-mcp\n"
                )
                sys.stderr.write(msg)
                sys.stderr.flush()
                sys.exit(2)

            exit_code = asyncio.run(
                _run_acp_cli_async(
                    assistant_id=assistant_id,
                    run_acp_agent=run_acp_agent,
                    agent_server_cls=AgentServerACP,
                    model_name=getattr(args, "model", None),
                    model_params=model_params,
                    summarization_model=resolved_summarization_model,
                    cli_max_retries=max_retries,
                    profile_override=profile_override,
                    mcp_config_path=getattr(args, "mcp_config", None),
                    no_mcp=getattr(args, "no_mcp", False),
                    trust_project_mcp=getattr(args, "trust_project_mcp", False),
                    allow_fs_tools=allow_fs_tools,
                    recursion_limit=_resolved_recursion_limit(args),
                    auto=approval_mode is ApprovalMode.AUTO,
                    yolo=approval_mode is ApprovalMode.YOLO,
                    auto_classifier_model=getattr(args, "auto_classifier_model", None),
                )
            )
            sys.exit(exit_code)

        # [해설][흐름] 15) 파이프 stdin 병합. 이 호출 이후 `args.non_interactive_message`가 확정되어 헤드리스/인터랙티브가 결정된다.
        apply_stdin_pipe(args)

        # [해설][흐름] 16) 편집 설치 의존성 하한 게이트: TTY가 있는 인터랙티브면 차단형 프롬프트(`prompt_for_dep_floor_mismatch`), 아니면 경고만.
        # Gate on stale editable-install dependencies. Choose the channel from
        # the launch mode, not from `sys.stdout.isatty()`: a TTY does not imply
        # the interactive TUI. This runs after `apply_stdin_pipe` so
        # `non_interactive_message` is final. An interactive launch that can
        # actually answer a prompt gets a blocking pre-TUI continue/mute/abort
        # prompt (printed to stderr before the alternate screen mounts);
        # headless (`-n`) launches and subcommands print the full warning to
        # stderr once instead — they have no TUI to prompt in and must never
        # block. Subcommands are warned earlier, before the `config`/`update`
        # fast paths exit. (ACP exits above, before this point.)
        #
        # `_trust_picker_has_terminal()` is part of the condition because a
        # piped-stdin interactive launch still mounts the TUI: per
        # `apply_stdin_pipe`, `cat x | dcode -m ...` and `cat x | dcode
        # --skill ...` set `initial_prompt`, not `non_interactive_message`.
        # Prompting there would find no TTY for the picker, read EOF from the
        # text fallback, and abort the launch outright — with no way to answer
        # the prompt and mute it. Those launches get the warning instead.
        interactive_tui = (
            not getattr(args, "command", None) and not args.non_interactive_message
        )
        if interactive_tui and _trust_picker_has_terminal():
            from deepagents_code._dep_floor_check import prompt_if_editable_deps_stale

            dep_floor_outcome = prompt_if_editable_deps_stale()
            if dep_floor_outcome is _TrustPromptOutcome.INTERRUPTED:
                sys.exit(130)
            if dep_floor_outcome is _TrustPromptOutcome.CANCELLED:
                from rich.console import Console as _Console

                _Console(stderr=True).print(
                    "[dim]Aborted; refresh the environment and relaunch.[/dim]",
                    highlight=False,
                )
                return
        elif command is None:
            from deepagents_code._dep_floor_check import warn_if_editable_deps_stale

            warn_if_editable_deps_stale()

        # [해설][흐름] 17) 플래그 조합 검증 시작. Auto 분류기 모델은 인터랙티브+샌드박스 없음에서만 유효(exit 2).
        # Checked *before* the approval-flag warning below, so a run that
        # exits here does not first print a warning about `--auto-approve`
        # being ignored — the exit is the outcome, and the warning would only
        # add noise. Both flags are final by now: `args.sandbox` comes from
        # `parse_args` and `apply_stdin_pipe` has settled
        # `non_interactive_message`.
        #
        # Auto approval mode (and therefore its classifier) requires the
        # interactive TUI *and* no sandbox — `agent.create_cli_agent` disables
        # Auto for either. Accepting the flag in those runs would silently do
        # nothing to a setting that governs action authorization.
        if getattr(args, "auto_classifier_model", None) is not None and (
            args.non_interactive_message or args.sandbox not in {"none", None}
        ):
            from rich.console import Console as _Console

            unavailable_because = (
                "a sandbox is in use"
                if not args.non_interactive_message
                else "it runs headlessly"
            )
            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --auto-classifier-model is only "
                "supported in the interactive TUI, where Auto approval mode "
                f"runs; {unavailable_because}.\n"
                "  dcode --auto-classifier-model anthropic:claude-haiku-4-5"
            )
            sys.exit(2)

        # [해설][흐름] 18) 헤드리스에서 `--yolo`/`--auto-approve`는 오류가 아니라 **경고 후 무시**(공식 문서에 명시 없음, analysis 01).
        # [해설] 헤드리스 shell 권한은 `-S/--shell-allow-list`로만 결정된다.
        # Warned here, before any session output could bury the message:
        # `apply_stdin_pipe` has finalized `non_interactive_message` (the same
        # predicate that selects the headless branch below), so this fires on
        # both the `-n` and piped-stdin paths while leaving interactive
        # launches untouched. `explicit_approval_flag` was captured at parse
        # time, so only a flag the user actually typed warns.
        #
        # The two assignments below no longer drive anything. Headless never
        # consults an approval mode -- `run_non_interactive` takes no
        # auto/yolo parameters -- and `CliProvider` snapshotted `vars(args)`
        # at `_install_cli_provider`, so mutating the namespace cannot change
        # what the resolver reports. They are kept as a belt-and-braces guard
        # for any future consumer that reads `args` directly; the warning
        # above, not this clearing, is what the user sees.
        if explicit_approval_flag is not None and args.non_interactive_message:
            from rich.console import Console as _Console

            # `soft_wrap` keeps the message on one line. Rich otherwise hard
            # wraps at width 80 off a TTY, and the break moves with the flag
            # name — leaving no substring a CI job can grep for both spellings.
            # With the pre-existing `sys.exit(2)` gone, this text is the only
            # signal that the requested mode was dropped. Deliberately not also
            # logged: the always-on buffer handler installed on the package
            # logger in `__init__` swallows a `logger.warning` entirely, so a
            # log call would add nothing a user could see.
            _Console(stderr=True).print(
                f"[bold yellow]Warning:[/bold yellow] {explicit_approval_flag} has "
                "no effect in headless mode; ignoring it. Shell access is "
                "governed by --shell-allow-list, and MCP routing is fail-closed.",
                soft_wrap=True,
            )
            args.auto_approve = False
            args.yolo = False

        # [해설] 이하 사용법 오류는 모두 exit 2: --no-mcp+--mcp-config, --skill+(-q|--no-stream) without -n, 헤드리스 전용 플래그를 -n 없이 사용 등.
        if getattr(args, "no_mcp", False) and getattr(args, "mcp_config", None):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --no-mcp and --mcp-config "
                "are mutually exclusive. Use one or the other.\n"
                "  dcode --mcp-config path/to/config.json\n"
                "  dcode --no-mcp"
            )
            sys.exit(2)

        if (
            getattr(args, "initial_skill", None)
            and not args.non_interactive_message
            and (args.quiet or args.no_stream)
        ):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --skill requires "
                "--non-interactive (-n) when combined with --quiet or "
                "--no-stream.\n"
                "  dcode --skill code-review -m 'review this patch'\n"
                "  dcode --skill code-review -n 'review this patch'"
            )
            sys.exit(2)

        # [해설] `--max-turns`/`--timeout`은 헤드리스 전용(초과 시 exit 124는 `run_non_interactive`/바깥 `wait_for`가 담당).
        max_turns_set = getattr(args, "max_turns", None) is not None
        if max_turns_set and not args.non_interactive_message:
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --max-turns requires "
                "--non-interactive (-n) or piped stdin\n"
                "  dcode -n 'refactor auth module' --max-turns 5"
            )
            sys.exit(2)

        timeout_set = getattr(args, "timeout", None) is not None
        if timeout_set and not args.non_interactive_message:
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --timeout requires "
                "--non-interactive (-n) or piped stdin\n"
                "  dcode -n 'run the test suite' --timeout 120"
            )
            sys.exit(2)

        # [해설] goal(인터랙티브 수락 기준 생성)과 rubric(헤드리스 수락 기준 직접 제공)은 상호 배타. 모순된 "-n을 추가하라" 오류 루프를 막으려고 먼저 거부.
        # `--goal` conflicts with every rubric flag, not just `--rubric`.
        # `--rubric-model`/`--rubric-max-iterations` also require `-n` (see the
        # non-interactive guard below), so without this check `--goal
        # --rubric-model X` would slip past here and hit a contradictory "add
        # -n" error — and adding `-n` then trips the interactive-only `--goal`
        # guard. Reject the combination up front instead.
        if getattr(args, "goal", None) is not None and any(
            getattr(args, attr, None) is not None
            for attr in ("rubric", "rubric_model", "rubric_max_iterations")
        ):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --goal is mutually exclusive with "
                "--rubric/--rubric-model/--rubric-max-iterations. Use --goal to "
                "generate criteria interactively, or --rubric (with -n) to "
                "provide them directly."
            )
            sys.exit(2)

        # [해설] `--goal` 추가 제약: 빈 값 금지, 헤드리스 금지, `-m`/`--skill`과 병용 금지(모두 exit 2).
        goal_text = getattr(args, "goal", None)
        if goal_text is not None and not goal_text.strip():
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --goal must not be empty."
            )
            sys.exit(2)
        if goal_text is not None and args.non_interactive_message:
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --goal is only supported in "
                "interactive mode for now.\n"
                "  dcode --goal 'add OAuth refresh handling'"
            )
            sys.exit(2)
        if goal_text is not None and (
            getattr(args, "initial_prompt", None) is not None
            or getattr(args, "initial_skill", None)
        ):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --goal cannot be combined with "
                "-m/--message or --skill.\n"
                "  dcode --goal 'add OAuth refresh handling'"
            )
            sys.exit(2)

        # [해설] rubric 계열·-q·--no-stream은 `-n` 또는 파이프 stdin 필수. POSIX 사용법 오류 관례에 따라 stderr + exit 2.
        non_interactive_rubric_set = any(
            getattr(args, attr, None) is not None
            for attr in (
                "rubric",
                "rubric_model",
                "rubric_max_iterations",
            )
        )
        if non_interactive_rubric_set and not args.non_interactive_message:
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --rubric/--rubric-model/"
                "--rubric-max-iterations require "
                "--non-interactive (-n) or piped stdin\n"
                "  dcode -n 'implement X' --rubric 'tests pass'"
            )
            sys.exit(2)

        if (args.quiet or args.no_stream) and not args.non_interactive_message:
            # Print to stderr (not the module-level stdout console) and exit
            # with code 2 to match the POSIX convention for usage errors, as
            # argparse's parser.error() would.
            from rich.console import Console as _Console

            flags = []
            if args.quiet:
                flags.append("--quiet")
            if args.no_stream:
                flags.append("--no-stream")
            flag = " and ".join(flags)
            _Console(stderr=True).print(
                f"[bold red]Error:[/bold red] {flag} requires "
                "--non-interactive (-n) or piped stdin\n"
                "  dcode -n 'summarize README.md' --quiet"
            )
            sys.exit(2)

        if args.prerelease and not (args.update or args.command == "update"):
            from rich.console import Console as _Console

            _Console(stderr=True).print(
                "[bold red]Error:[/bold red] --prerelease requires --update "
                "or the update subcommand"
            )
            sys.exit(2)

        # [해설][흐름] 19) 세션 없는 명령 — `dcode update`/`--update`: 캐시 우회로 PyPI 최신 버전 확인 → 프로세스 간 락 → `perform_upgrade`.
        # [해설] 편집 설치면 업데이트 불가, 설치 방식이 프리릴리스를 지원하지 않으면 거부. 성공/최신이면 exit 0, 실패 exit 1.
        # [해설] 자동 업데이트(`_run_startup_auto_update`)와 달리 re-exec 없이 종료만 한다.
        # Handle --update flag or `update` subcommand (headless, no session)
        if args.update or args.command == "update":
            try:
                from rich.markup import escape

                from deepagents_code._env_vars import DEBUG_UPDATE
                from deepagents_code._version import __version__ as cli_version
                from deepagents_code.config import _is_editable_install
                from deepagents_code.update_check import (
                    _PRERELEASE_UNSUPPORTED_MESSAGE,
                    create_update_log_file,
                    format_age_suffix,
                    format_installed_age_suffix,
                    format_log_follow_command,
                    format_release_age_parenthetical,
                    is_update_available,
                    perform_upgrade,
                    prerelease_upgrade_supported,
                    release_requires_prereleases,
                    update_install_lock,
                    upgrade_command,
                )

                if _is_editable_install():
                    age_suffix = format_age_suffix(cli_version)
                    console.print(
                        "[bold yellow]Warning:[/bold yellow] "
                        "Updates are not available for editable installs. "
                        f"Currently on v{cli_version}{age_suffix}."
                    )
                    sys.exit(0)

                include_prereleases = True if args.prerelease else None

                # Refuse pre-release upgrades the install method can't honor
                # before promising an upgrade or hitting PyPI.
                if args.prerelease:
                    supported, reason = prerelease_upgrade_supported()
                    if not supported:
                        console.print(
                            "[bold red]Error:[/bold red] "
                            f"{reason or _PRERELEASE_UNSUPPORTED_MESSAGE}"
                        )
                        sys.exit(1)

                console.print("Checking for updates...", style="dim")
                available, latest = is_update_available(
                    bypass_cache=True,
                    include_prereleases=include_prereleases,
                )
                if latest is None:
                    console.print(
                        "[bold yellow]Warning:[/bold yellow] Could not "
                        "determine the latest version. Check your network "
                        "and try again."
                    )
                    sys.exit(1)
                if not available:
                    age_suffix = format_age_suffix(cli_version)
                    console.print(
                        f"Already on the latest version (v{cli_version}{age_suffix})."
                    )
                    sys.exit(0)

                upgrade_include_prereleases = include_prereleases
                pin_upgrade_version: str | None = None
                # [해설] 최신 릴리스 자체가 프리릴리스 채널에만 있으면 `--prerelease` 없이도 프리릴리스 허용 + 버전 고정으로 수동 명령을 만든다.
                if include_prereleases is None and release_requires_prereleases(latest):
                    upgrade_include_prereleases = True
                    pin_upgrade_version = latest
                if upgrade_include_prereleases is True:
                    supported, reason = prerelease_upgrade_supported()
                    if not supported:
                        console.print(
                            "[bold red]Error:[/bold red] "
                            f"{reason or _PRERELEASE_UNSUPPORTED_MESSAGE}"
                        )
                        sys.exit(1)

                # The install mutates the shared tool environment, so an
                # explicit headless update must use the same cross-process
                # guard as startup and in-session updates.
                with update_install_lock() as holding_update_lock:
                    if not holding_update_lock:
                        console.print(
                            "Another dcode session is currently updating. "
                            "Try again after it finishes.",
                            style="dim",
                        )
                        sys.exit(1)
                    release_age = format_release_age_parenthetical(latest)
                    installed_age = format_installed_age_suffix(cli_version)
                    console.print(
                        f"Update available: v{latest}{release_age}. "
                        f"Currently installed: {cli_version}{installed_age}. "
                        "Upgrading..."
                    )
                    if os.environ.get(DEBUG_UPDATE):
                        console.print(
                            "Skipped update install (debug mode).", style="dim"
                        )
                        sys.exit(0)
                    log_path = create_update_log_file()
                    if log_path is not None:
                        console.print(
                            f"Update log: {format_log_follow_command(log_path)}",
                            style="dim",
                            highlight=False,
                            markup=False,
                        )
                    success, output, _installed = asyncio.run(
                        perform_upgrade(
                            log_path=log_path,
                            include_prereleases=include_prereleases,
                            target_version=latest,
                        )
                    )
                if success:
                    console.print(f"[green]Updated to v{latest}.[/green]")
                else:
                    cmd = upgrade_command(
                        include_prereleases=upgrade_include_prereleases,
                        version=pin_upgrade_version,
                    )
                    detail = f": {escape(output[:200])}" if output else ""
                    console.print(
                        f"[bold red]Auto-update failed{detail}[/bold red]\n"
                        f"Run manually: [cyan]{cmd}[/cyan]"
                    )
                    sys.exit(1)
                sys.exit(0)
            # [해설] 예상 못한 실패 시 수동 명령 제시. `--prerelease` 의도를 유지해 안정 채널 명령으로 조용히 강등되지 않게 한다.
            except Exception:
                logger.warning("--update failed", exc_info=True)
                # Preserve the user's pre-release intent in the manual fallback:
                # a `--prerelease` request that crashes unexpectedly must not
                # suggest a stable-only command, which would silently downgrade
                # the channel. Both are module-level string constants, so this
                # import can't fail inside the last-resort handler.
                from deepagents_code.update_check import (
                    _UV_PRERELEASE_UPGRADE_COMMAND,
                    FALLBACK_UPGRADE_COMMAND,
                )

                manual_cmd = (
                    _UV_PRERELEASE_UPGRADE_COMMAND
                    if args.prerelease
                    else FALLBACK_UPGRADE_COMMAND
                )
                console.print(
                    "[bold red]Error:[/bold red] Update failed.\n"
                    f"Run manually: [cyan]{manual_cmd}[/cyan]"
                )
                sys.exit(1)

        # [해설][흐름] 20) `--uninstall`/`--install` 플래그(서브커맨드 별칭) 처리 — `client/commands/extras.py`에 위임하고 항상 종료.
        if args.uninstall is not None:
            from deepagents_code.client.commands.extras import run_uninstall_request

            sys.exit(run_uninstall_request(name=args.uninstall))

        if args.package and not args.install:
            console.print(
                "[bold red]Error:[/bold red] --package requires "
                "`dcode install <package> --package` "
                "(or the `--install <package>` alias).",
            )
            sys.exit(2)

        # Handle --install <name> [--package] flag (headless, no session).
        # Alias for `dcode install`. Always exits.
        if args.install:
            from deepagents_code.client.commands.extras import run_install_request

            sys.exit(
                run_install_request(
                    name=args.install,
                    package=bool(args.package),
                    yes=bool(args.yes),
                )
            )

        # [해설][흐름] 21) `--auto-update`: 저장된 선호를 반전. 저장 후 실효값이 다르면 더 높은 우선순위 계층(managed/env `DEEPAGENTS_CODE_AUTO_UPDATE`/기타)을 지목해 안내.
        # Handle --auto-update flag (headless toggle: reads current state
        # and inverts it, no session)
        if args.auto_update:
            try:
                from deepagents_code.config import _is_editable_install
                from deepagents_code.update_check import (
                    is_auto_update_enabled,
                    set_auto_update,
                )

                if _is_editable_install():
                    console.print(
                        "[bold yellow]Warning:[/bold yellow] "
                        "Auto-updates are not available for editable installs."
                    )
                    sys.exit(1)

                currently_enabled = is_auto_update_enabled()
                new_state = not currently_enabled
                set_auto_update(new_state)
                effective_state = is_auto_update_enabled()
                if effective_state != new_state:
                    from deepagents_code._env_vars import AUTO_UPDATE
                    from deepagents_code.configuration.service import (
                        managed_config_status,
                    )
                    from deepagents_code.update_check import _managed_update_value

                    # Managed config is only one of the layers that outrank the
                    # saved preference. Naming it for an env-var override would
                    # send the user to an administrator who set no policy. A
                    # managed file that cannot be parsed also forces the setting
                    # off, so name the parse failure rather than blaming policy
                    # the administrator may never have written.
                    managed_decides, _ = _managed_update_value("auto_update")
                    if managed_decides and not managed_config_status().usable:
                        blame = "a managed config file that could not be read"
                    elif managed_decides:
                        blame = "managed config"
                    elif os.environ.get(AUTO_UPDATE) is not None:
                        blame = AUTO_UPDATE
                    else:
                        blame = "a higher-precedence config source"
                    effective_label = "enabled" if effective_state else "disabled"
                    console.print(
                        "Preference saved, but auto-updates remain "
                        f"{effective_label} due to {blame}."
                    )
                else:
                    label = "enabled" if new_state else "disabled"
                    console.print(f"Auto-updates {label}.")
            except OSError:
                logger.warning("--auto-update failed: filesystem error", exc_info=True)
                console.print(
                    "[bold red]Error:[/bold red] Failed to toggle auto-updates. "
                    + _profile_permission_hint()
                )
                sys.exit(1)
            except Exception:
                logger.warning("--auto-update failed", exc_info=True)
                console.print(
                    "[bold red]Error:[/bold red] Failed to toggle auto-updates."
                )
                sys.exit(1)
            sys.exit(0)

        # [해설][흐름] 22) `--default-model`/`--clear-default-model`: config.toml `[models].default` 조회·설정·삭제. 제공자 없는 이름은 `detect_provider`로 보완.
        # [해설] 정책(`models.allowed`) 위반은 권한 문제와 구분해 별도 메시지로 exit 1.
        # Handle --default-model / --clear-default-model (headless, no session)
        if args.clear_default_model:
            from deepagents_code.model_config import clear_default_model

            if clear_default_model():
                console.print("Default model cleared.")
            else:
                console.print(
                    "[bold red]Error:[/bold red] Could not clear default model. "
                    + _profile_permission_hint()
                )
                sys.exit(1)
            sys.exit(0)

        if args.default_model is not None:
            from deepagents_code.model_config import (
                ModelConfig,
                save_default_model,
            )

            if args.default_model == "__SHOW__":
                config = ModelConfig.load()
                if config.default_model:
                    console.print(f"Default model: {config.default_model}")
                    # Reporting a stored value the next launch will skip is
                    # worse than reporting nothing, so say so here.
                    if not config.is_model_allowed(config.default_model):
                        console.print(
                            "[bold yellow]Warning:[/bold yellow] this value is "
                            "outside models.allowed and will be ignored."
                        )
                else:
                    console.print("No default model set.")
                sys.exit(0)

            model_spec = args.default_model
            # Auto-detect provider for bare model names
            from deepagents_code.config import detect_provider
            from deepagents_code.model_config import ModelSpec

            parsed = ModelSpec.try_parse(model_spec)
            if not parsed:
                provider = detect_provider(model_spec)
                if provider:
                    model_spec = f"{provider}:{model_spec}"

            from deepagents_code.model_config import ModelNotAllowedError

            try:
                saved = save_default_model(model_spec)
            except ModelNotAllowedError as exc:
                # A policy refusal is not an I/O problem; sending the user to
                # check directory permissions would be actively misleading.
                console.print(f"[bold red]Error:[/bold red] {exc}")
                sys.exit(1)
            if saved:
                console.print(f"Default model set to {model_spec}")
            else:
                console.print(
                    "[bold red]Error:[/bold red] Could not save default model. "
                    + _profile_permission_hint()
                )
                sys.exit(1)
            sys.exit(0)

        # [해설][흐름] 23) 서브커맨드 실행(help/agents/skills/plugin/mcp/threads). `--json` 계열 출력 형식(`output.add_json_output_arg`)을 공통 전달.
        output_format = getattr(args, "output_format", "text")

        if args.command == "help":
            from deepagents_code.ui import show_help

            show_help()
        elif args.command == "agents":
            from deepagents_code.agent import list_agents, reset_agent
            from deepagents_code.ui import show_agents_help

            # "ls" is an argparse alias for "list"
            if args.agents_command in {"list", "ls"}:
                list_agents(output_format=output_format)
            elif args.agents_command == "reset":
                reset_agent(
                    args.agent,
                    args.source_agent,
                    dry_run=args.dry_run,
                    output_format=output_format,
                )
            else:
                show_agents_help()
        elif args.command == "skills":
            from deepagents_code.skills import execute_skills_command

            execute_skills_command(args)
        elif args.command in {"plugin", "plugins"}:
            from deepagents_code.plugins.commands_cli import execute_plugin_command

            execute_plugin_command(args)
        # [해설] `dcode mcp login [server]`(OAuth 로그인)·`dcode mcp config`(탐색 경로 표시). 최상위 `--mcp-config`도 login 설정 경로로 받아 준다.
        elif args.command == "mcp":
            from deepagents_code.client.commands.mcp import (
                run_mcp_config,
                run_mcp_login,
                run_mcp_login_list,
            )
            from deepagents_code.ui import show_mcp_help

            if args.mcp_command == "login":
                config_path = args.config_path or args.mcp_config
                if config_path and not args.config_path:
                    print(  # noqa: T201
                        f"Using --mcp-config from top-level: {config_path}",
                        file=sys.stderr,
                    )
                command = (
                    run_mcp_login(
                        server=args.server,
                        config_path=config_path,
                    )
                    if args.server is not None
                    else run_mcp_login_list(config_path=config_path)
                )
                sys.exit(asyncio.run(command))
            if args.mcp_command == "config":
                sys.exit(run_mcp_config())
            show_mcp_help()
        # [해설] `dcode threads list|delete`: sessions.db의 체크포인트 metadata에 대한 조회/삭제(`sessions.list_threads_command`/`delete_thread_command`).
        # [해설] analysis 01에 따르면 delete는 `dcode_thread_workspaces` 바인딩 행을 지우지 않는다.
        elif args.command == "threads":
            from deepagents_code.sessions import (
                delete_thread_command,
                list_threads_command,
            )
            from deepagents_code.ui import show_threads_help

            # "ls" is an argparse alias for "list" — argparse stores the
            # alias as-is in the namespace, so we must match both values.
            if args.threads_command in {"list", "ls"}:
                raw_cwd = getattr(args, "cwd", None)
                cwd_filter = _normalize_cwd_filter(raw_cwd)
                sort_by, relative = _resolve_thread_list_display_options(args)
                # Warn (but still query) when the user passed an explicit
                # `--cwd <path>` that does not exist on disk — otherwise a
                # typo would silently return "No threads found" with no hint.
                # Skip the check for the bare-flag (empty string) form, where
                # the path was generated from `Path.cwd()` and is known to
                # exist.
                if (
                    raw_cwd not in {None, ""}
                    and cwd_filter is not None
                    and not Path(cwd_filter).exists()
                ):
                    print(  # noqa: T201
                        f"Warning: --cwd path {cwd_filter!r} does not exist; "
                        "filtering by stored metadata anyway.",
                        file=sys.stderr,
                    )
                asyncio.run(
                    list_threads_command(
                        agent_name=getattr(args, "agent", None),
                        limit=getattr(args, "limit", None),
                        sort_by=sort_by,
                        branch=getattr(args, "branch", None),
                        cwd=cwd_filter,
                        verbose=getattr(args, "verbose", False),
                        relative=relative,
                        output_format=output_format,
                    )
                )
            elif args.threads_command == "delete":
                asyncio.run(
                    delete_thread_command(
                        args.thread_id,
                        dry_run=args.dry_run,
                        output_format=output_format,
                    )
                )
            else:
                # No subcommand provided, show threads help screen
                show_threads_help()
        # [해설][흐름] 24) ★ 헤드리스 분기(`-n` 또는 파이프 stdin). 흐름 C: 도구 점검 → 샌드박스/인터프리터 의존성 → `run_non_interactive`.
        # [해설] 서버 기동은 `client/non_interactive.py`의 `run_non_interactive` 내부 `server_session(interactive=False)`에서 일어난다.
        elif args.non_interactive_message:
            _print_configured_profile_notice()
            # Resolve recent-agent fallback only for actual session launches.
            assistant_id = _resolve_agent_arg(args)
            # [해설][흐름] 24-1) 누락 도구 경고(stdout을 깨끗하게 유지하려고 stderr). 필요 시 관리형 ripgrep을 자동 설치한다.
            # Check for optional tools before running agent (stderr so
            # --quiet piped output stays clean)
            try:
                from rich.console import Console as _Console
            except ImportError:
                logger.warning(
                    "Could not import rich.console; skipping tool warnings",
                    exc_info=True,
                )
            else:
                warn_console = None
                try:
                    warn_console = _Console(stderr=True)
                    missing_tools = check_optional_tools()
                    if _should_ensure_managed_ripgrep():
                        missing_tools = _auto_install_ripgrep_cli(
                            warn_console, missing_tools
                        )
                    for tool in missing_tools:
                        warn_console.print(
                            f"[yellow]Warning:[/yellow] {format_tool_warning_cli(tool)}"
                        )
                except Exception:
                    logger.warning(
                        "Optional-tools check failed unexpectedly", exc_info=True
                    )
                    # A swallowed failure here must not be fully silent: surface
                    # one stderr line so a degraded grep is at least signposted.
                    if warn_console is not None:
                        with contextlib.suppress(Exception):
                            warn_console.print(
                                "[dim]Tool availability check skipped — see logs.[/dim]"
                            )
            # [해설][흐름] 24-2) 서버 서브프로세스를 띄우기 전에 샌드박스 제공자 패키지(extra) 설치 여부를 확인(`sandbox_factory.verify_sandbox_deps`).
            # Validate sandbox provider deps before spawning server subprocess
            if args.sandbox and args.sandbox != "none":
                from deepagents_code.integrations.sandbox_factory import (
                    verify_sandbox_deps,
                )

                try:
                    verify_sandbox_deps(args.sandbox)
                except ImportError as exc:
                    from rich.markup import escape

                    console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
                    sys.exit(1)

            # [해설][흐름] 24-3) 인터프리터 활성 여부 확정과 의존성 검사, PTC 플래그 파싱·경고, rubric 텍스트 해석.
            enable_interpreter = _resolve_interpreter_enabled(args)
            if enable_interpreter:
                _verify_interpreter_or_exit()

            # Non-interactive mode - execute single task and exit
            from deepagents_code.client.non_interactive import run_non_interactive
            from deepagents_code.config_manifest import load_bool_display_preference

            interpreter_ptc = _parse_interpreter_tools_flag(
                getattr(args, "interpreter_tools", None)
            )
            _warn_if_interpreter_tools_without_interpreter(
                args, enable_interpreter=enable_interpreter
            )
            _warn_if_interpreter_disabled_by_sandbox(args)

            try:
                rubric_text = _resolve_rubric_text(getattr(args, "rubric", None))
            except ValueError as exc:
                from rich.console import Console as _Console

                _Console(stderr=True).print(f"[bold red]Error:[/bold red] {exc}")
                sys.exit(2)

            # [해설][흐름] 24-4) `run_non_interactive`를 `asyncio.wait_for`로 감싸 `--timeout`(벽시계 제한)을 적용. 초과 시 exit 124.
            # [해설] 헤드리스에는 `thread_id`/resume 인자가 없어 매번 새 스레드로 실행된다. `--trust-project-hooks`는 프롬프트 없이 플래그로만 신뢰한다.
            timeout = getattr(args, "timeout", None)
            try:
                exit_code = asyncio.run(
                    asyncio.wait_for(
                        run_non_interactive(
                            message=args.non_interactive_message,
                            assistant_id=assistant_id,
                            model_name=getattr(args, "model", None),
                            model_params=model_params,
                            summarization_model=resolved_summarization_model,
                            cli_max_retries=max_retries,
                            profile_override=profile_override,
                            sandbox_type=args.sandbox,
                            sandbox_id=args.sandbox_id,
                            sandbox_snapshot_name=args.sandbox_snapshot_name,
                            sandbox_setup=getattr(args, "sandbox_setup", None),
                            initial_skill=getattr(args, "initial_skill", None),
                            startup_cmd=getattr(args, "startup_cmd", None),
                            quiet=args.quiet,
                            stream=not args.no_stream,
                            show_reasoning=load_bool_display_preference(
                                "display.show_reasoning", fallback=False
                            ),
                            mcp_config_path=getattr(args, "mcp_config", None),
                            no_mcp=getattr(args, "no_mcp", False),
                            trust_project_mcp=getattr(args, "trust_project_mcp", False),
                            trust_project_hooks=getattr(
                                args, "trust_project_hooks", False
                            ),
                            trust_project_extensions=getattr(
                                args, "trust_project_extensions", False
                            ),
                            extension_paths=tuple(getattr(args, "extension", ())),
                            enable_interpreter=enable_interpreter,
                            interpreter_ptc=interpreter_ptc,
                            allow_fs_tools=allow_fs_tools,
                            max_turns=getattr(args, "max_turns", None),
                            rubric=rubric_text,
                            rubric_model=getattr(args, "rubric_model", None),
                            rubric_max_iterations=getattr(
                                args, "rubric_max_iterations", None
                            ),
                            recursion_limit=_resolved_recursion_limit(args),
                        ),
                        timeout=timeout,
                    )
                )
            except TimeoutError:
                # `asyncio.wait_for` raises `asyncio.TimeoutError`, an alias
                # of the builtin.
                from rich.console import Console as _Console

                _Console(stderr=True).print(
                    f"[bold red]Error:[/bold red] agent timed out after "
                    f"{timeout}s. Retry with a larger --timeout, or use "
                    "--max-turns for a turn-count limit."
                )
                sys.exit(124)
            except KeyboardInterrupt:
                # `asyncio.run` re-raises `KeyboardInterrupt` past the inner
                # `run_non_interactive` handler when the signal hits during
                # `wait_for`; mirror its exit code 130 here so Ctrl-C is a
                # quiet exit instead of a traceback.
                sys.exit(130)
            sys.exit(exit_code)
        # [해설][흐름] 25) ★ 인터랙티브(TUI) 분기. 흐름 B: 자동 업데이트 → 스레드 ID → 의존성 확인 → 신뢰 프롬프트(MCP·hooks·extensions)
        # [해설] → 승인 모드 확정 → `run_textual_cli_async` → 종료 통계·resume 힌트·업데이트 배너.
        else:
            _print_configured_profile_notice()
            # [해설][흐름] 25-1) 새 세션이면 resume 유예 기록을 초기화하고 자동 업데이트 실행. resume이면 `update_check`의 유예 정책(analysis 01: 7일) 안에서만 건너뛴다.
            resume_thread = args.resume_thread  # "__MOST_RECENT__", "<id>", or None
            if resume_thread is None:
                # A normal (non-resume) launch runs the update path and resets
                # the resume grace period, so a later resume-only stretch starts
                # a fresh deferral window rather than inheriting a stale one.
                from deepagents_code.update_check import (
                    clear_resume_auto_update_deferral,
                )

                clear_resume_auto_update_deferral()
                _run_startup_auto_update(console)
            else:
                # Keep immediate resume launches uninterrupted, but do not let
                # a resume-only workflow bypass startup updates indefinitely.
                from deepagents_code.update_check import (
                    should_defer_startup_auto_update_for_resume,
                )

                if not should_defer_startup_auto_update_for_resume():
                    _run_startup_auto_update(console)
            # Resolve recent-agent fallback only for actual session launches.
            assistant_id = _resolve_agent_arg(args)
            # Interactive mode - handle thread resume
            from rich.text import Text

            from deepagents_code.sessions import generate_thread_id

            # [해설][흐름] 25-2) 새 세션은 UUID7 스레드 ID를 즉시 생성, resume이면 None을 넘기고 TUI(`_resolve_resume_thread`)가 비동기로 해석한다(첫 화면 지연 방지).
            # Instead of resolving thread_id here with synchronous asyncio.run()
            # DB calls, pass the raw resume request to the TUI and let it
            # resolve asynchronously during startup.
            thread_id = None if resume_thread else generate_thread_id()

            # Validate sandbox provider deps before spawning server subprocess
            if args.sandbox and args.sandbox != "none":
                from deepagents_code.integrations.sandbox_factory import (
                    verify_sandbox_deps,
                )

                try:
                    verify_sandbox_deps(args.sandbox)
                except ImportError as exc:
                    from rich.markup import escape

                    console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
                    sys.exit(1)

            enable_interpreter = _resolve_interpreter_enabled(args)
            if enable_interpreter:
                _verify_interpreter_or_exit()

            # [해설][흐름] 25-3) TUI 이전 신뢰 프롬프트 3종. Ctrl+C → exit 130, 취소 → 안내 후 정상 반환(기동 중단).
            # [해설] TUI의 대체 화면이 뜨기 전에 stderr로 물어야 사용자에게 보이기 때문에 여기서 처리한다.
            # Check project MCP trust before launching TUI
            mcp_trust_decision = _check_mcp_project_trust(
                trust_flag=getattr(args, "trust_project_mcp", False),
            )
            # [해설] 디버그 env가 켜져 있으면 MCP 신뢰 프롬프트만 시험하고 종료한다.
            if _debug_mcp_project_trust_enabled():
                sys.exit(0)
            if mcp_trust_decision is _TrustPromptOutcome.INTERRUPTED:
                sys.exit(130)
            if mcp_trust_decision is _TrustPromptOutcome.CANCELLED:
                from rich.console import Console as _Console

                _Console(stderr=True).print(
                    "[dim]Aborted; no project MCP servers loaded.[/dim]",
                    highlight=False,
                )
                return

            hook_trust = _check_project_hooks_trust(
                trust_flag=getattr(args, "trust_project_hooks", False),
            )
            if hook_trust is _TrustPromptOutcome.INTERRUPTED:
                sys.exit(130)
            if hook_trust is _TrustPromptOutcome.CANCELLED:
                from rich.console import Console as _Console

                _Console(stderr=True).print(
                    "[dim]Aborted; project hooks not loaded.[/dim]",
                    highlight=False,
                )
                return

            extensions_trust = _check_project_extensions_trust(
                trust_flag=getattr(args, "trust_project_extensions", False),
            )
            if extensions_trust is _TrustPromptOutcome.INTERRUPTED:
                sys.exit(130)
            if extensions_trust is _TrustPromptOutcome.CANCELLED:
                from rich.console import Console as _Console

                _Console(stderr=True).print(
                    "[dim]Aborted; project extensions not loaded.[/dim]",
                    highlight=False,
                )
                return

            # [해설][흐름] 25-4) TUI 실행 블록. `return_code`/`request_count`는 finally의 종료 힌트 출력에 쓰인다.
            # Run Textual TUI
            return_code = 0
            request_count = 0
            try:
                interpreter_ptc = _parse_interpreter_tools_flag(
                    getattr(args, "interpreter_tools", None)
                )
                # A stderr warning here would be clobbered by the alternate
                # screen the moment the TUI launches; the app surfaces the
                # advisory as a startup notification instead (see
                # `DeepAgentsApp._notify_interpreter_tools_without_interpreter`).

                # [해설] 승인 모드 확정: 샌드박스 사용 시 Auto → Manual 강등, YOLO는 1회 확인·저장(`_ensure_yolo_acknowledged`) 실패 시 Manual.
                from deepagents_code.approval_mode import ApprovalMode

                approval_mode = _resolve_approval_mode(args)
                if (
                    approval_mode is ApprovalMode.AUTO
                    and args.sandbox
                    and args.sandbox != "none"
                ):
                    console.print(
                        "[yellow]Auto is unavailable with a sandbox. "
                        "Using Manual.[/yellow]"
                    )
                    approval_mode = ApprovalMode.MANUAL
                if approval_mode is ApprovalMode.YOLO and not _ensure_yolo_acknowledged(
                    console
                ):
                    console.print(
                        "[yellow]YOLO was not enabled; using Manual.[/yellow]"
                    )
                    approval_mode = ApprovalMode.MANUAL

                # [해설][흐름] 25-5) TUI 진입(`run_textual_cli_async` → `app.run_textual_app`). 반환 후 최종 thread_id(`/threads`로 바뀌었을 수 있음)와 통계를 반영.
                result = asyncio.run(
                    run_textual_cli_async(
                        assistant_id=assistant_id,
                        approval_mode=approval_mode,
                        sandbox_type=args.sandbox,
                        sandbox_id=args.sandbox_id,
                        sandbox_snapshot_name=args.sandbox_snapshot_name,
                        sandbox_setup=getattr(args, "sandbox_setup", None),
                        model_name=getattr(args, "model", None),
                        model_params=model_params,
                        summarization_model=resolved_summarization_model,
                        cli_max_retries=max_retries,
                        profile_override=profile_override,
                        thread_id=thread_id,
                        resume_thread=resume_thread,
                        initial_prompt=getattr(args, "initial_prompt", None),
                        initial_skill=getattr(args, "initial_skill", None),
                        initial_goal=getattr(args, "goal", None),
                        startup_cmd=getattr(args, "startup_cmd", None),
                        mcp_config_path=getattr(args, "mcp_config", None),
                        no_mcp=getattr(args, "no_mcp", False),
                        trust_project_mcp=mcp_trust_decision,
                        hook_trust=hook_trust,
                        trust_project_extensions=bool(extensions_trust),
                        extension_paths=tuple(getattr(args, "extension", ())),
                        enable_interpreter=enable_interpreter,
                        interpreter_arg=args.interpreter,
                        interpreter_ptc=interpreter_ptc,
                        allow_fs_tools=allow_fs_tools,
                        auto_classifier_model=getattr(
                            args, "auto_classifier_model", None
                        ),
                        recursion_limit=_resolved_recursion_limit(args),
                    )
                )
                return_code = result.return_code
                # The user may have switched threads via /threads during the
                # session; use the final thread ID for teardown messages.
                thread_id = result.thread_id or thread_id
                request_count = result.session_stats.request_count
                _print_session_stats(result.session_stats, console)
            # [해설] 예외 경로별 return_code 기록: 일반 예외 1, Ctrl+C 130(재발생), 시그널 SystemExit(128+signum)은 코드 전달 후 재발생.
            except Exception as e:  # noqa: BLE001  # Top-level error handler for the application
                return_code = 1
                error_msg = Text("\nApplication error: ", style="red")
                error_msg.append(str(e))
                console.print(error_msg)
                console.print(Text(traceback.format_exc(), style="dim"))
                sys.exit(1)
            except KeyboardInterrupt:
                # Ctrl+C; the outer handler prints "Interrupted" and exits 130.
                # Mark non-zero so the teardown hint carries the safety caveat.
                return_code = 130
                raise
            except SystemExit as e:
                # The termination-signal handler raises SystemExit(128+signum);
                # forward non-zero codes so the teardown hint adds the caveat.
                if isinstance(e.code, int) and e.code != 0:
                    return_code = e.code
                raise
            # [해설][흐름] 25-6) 어떤 종료 경로든 체크포인트가 있는 스레드면 LangSmith 링크와 `dcode -r <id>` 힌트를 출력.
            finally:
                # Show LangSmith thread link and resume hint for threads with
                # checkpointed content. The `thread_id is not None` check narrows the
                # type to `str` for the helper; `_should_check_teardown_thread` gates
                # whether the teardown lookup runs at all.
                if thread_id is not None and _should_check_teardown_thread(
                    thread_id,
                    request_count=request_count,
                    resume_thread=args.resume_thread,
                ):
                    _render_teardown_thread_hints(
                        console, thread_id, return_code=return_code
                    )

            # [해설][흐름] 25-7) TUI가 감지한 업데이트가 있으면 종료 후 배너 출력(알림 빈도는 `should_notify_update`/`mark_update_notified`로 제한).
            # Warn about available update on exit
            try:
                if result.update_available[0]:
                    from deepagents_code._version import __version__ as cli_version
                    from deepagents_code.update_check import (
                        format_installed_age_suffix,
                        format_release_age_parenthetical,
                        is_auto_update_enabled,
                        is_installed_version_at_least,
                        mark_update_notified,
                        should_notify_update,
                        upgrade_command,
                    )

                    latest = result.update_available[1]
                    if (
                        latest
                        and not is_installed_version_at_least(latest)
                        and should_notify_update(latest)
                    ):
                        console.print()
                        release_age = format_release_age_parenthetical(latest)
                        installed_age = format_installed_age_suffix(cli_version)
                        update_msg = Text(
                            f"Update available: v{latest}", style="yellow bold"
                        )
                        update_msg.append(
                            f"{release_age}. "
                            f"Currently installed: {cli_version}{installed_age}.",
                            style="dim",
                        )
                        console.print(update_msg)
                        cmd_hint = Text("Run: ", style="dim")
                        cmd_hint.append(upgrade_command(), style="cyan")
                        console.print(cmd_hint)
                        if not is_auto_update_enabled():
                            auto_hint = Text("Enable auto-updates: ", style="dim")
                            auto_hint.append("dcode --auto-update", style="cyan")
                            console.print(auto_hint)
                        mark_update_notified(latest)
            except Exception:
                logger.warning("Failed to display exit update banner", exc_info=True)
    # [해설] 최상위 Ctrl+C 처리: traceback 없이 "Interrupted" 출력 후 exit 130. config import 전이면 `console`이 없어 NameError 폴백.
    except KeyboardInterrupt:
        # Suppress the traceback while preserving the conventional SIGINT status.
        # `console` may not be bound if Ctrl+C arrives during config import.
        try:
            console.print("\n\n[yellow]Interrupted[/yellow]")
        except NameError:
            sys.stderr.write("\n\nInterrupted\n")
        sys.exit(130)


# [해설] 직접 실행 지원. 일반 경로는 콘솔 스크립트 또는 `python -m deepagents_code`(`__main__.py`, 자동 업데이트 re-exec가 사용).
if __name__ == "__main__":
    cli_main()
