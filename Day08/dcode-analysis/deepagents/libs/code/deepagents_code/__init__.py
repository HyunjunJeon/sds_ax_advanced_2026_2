"""Deep Agents Code - Interactive AI coding assistant."""

# [해설] 패키지 `deepagents_code`의 최상위 모듈. `import deepagents_code`나 그 하위 모듈을
# [해설] 처음 import하는 순간 실행되므로 클라이언트 프로세스(CLI/TUI/헤드리스/ACP)와
# [해설] LangGraph 서버 프로세스(`server_graph.make_graph`가 import될 때) 양쪽에서 모두 돈다.
# [해설] 역할은 두 가지: (1) 패키지 로거에 링버퍼·디버그 로깅을 설치하는 import 시점 부작용,
# [해설] (2) 콘솔 스크립트 진입점 `cli_main`을 `__getattr__`로 지연 import 해 무거운 `main.py`
# [해설] 로딩 비용을 `deepagents_code.config` 같은 하위 모듈 import에서 피하는 것.
# [해설][흐름] `dcode`/`deepagents-code` 콘솔 스크립트(pyproject의 `deepagents_code:cli_main`)
# [해설][흐름] → 이 모듈의 `__getattr__("cli_main")` → `deepagents_code.main.cli_main`.
# [해설] 관련 분석: `analysis/01-boot-client-server.md` "코드 지도"·"A. 공통 부팅".
# [해설] 관련 공식 문서: `docs_official/code/cli-reference.md`, `libs/code/DEVELOPMENT.md` "Debugging".
from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from deepagents_code._debug import configure_debug_logging
from deepagents_code._debug_buffer import install_log_buffer
from deepagents_code._home_error import DeepAgentsHomeError
from deepagents_code._version import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

# [해설][설계] 순서가 중요하다. 먼저 항상 켜진 링버퍼(`_debug_buffer.install_log_buffer`)를 붙여
# [해설] 이후 `configure_debug_logging`이 내는 경고까지 버퍼에 담는다. 그다음 디버그 로깅이
# [해설] `DEEPAGENTS_CODE_DEBUG` 등 설정에 따라 최종 레벨을 정한다(`_debug.py` 참고).
# [해설][주의] 하위 모듈이 로그를 내기 전에 패키지 로거가 구성돼야 하므로 import 시점 부작용으로 둔다.
install_log_buffer(logging.getLogger(__name__))  # noqa: RUF067  # attach the always-on tail first so warnings from configure_debug_logging are captured
configure_debug_logging(logging.getLogger(__name__))  # noqa: RUF067  # package logger must be configured before child modules emit logs; sets the final level over the buffer's INFO floor

# [해설] `cli_main`은 모듈 속성으로 실제 존재하지 않고 아래 `__getattr__`가 요청 시 해석한다(F822 무시 이유).
__all__ = [
    "__version__",
    "cli_main",  # noqa: F822  # resolved lazily by __getattr__
]


# [해설] PEP 562 모듈 수준 `__getattr__`. 콘솔 스크립트가 `deepagents_code:cli_main`을 찾을 때 호출된다.
# [해설] `cli_main`만 지연 import하고, 그 외 이름은 일반 모듈처럼 AttributeError를 낸다.
# [해설][흐름] `main.py` import → `_paths`가 import 시점에 프로필 경로를 해석 → 실패하면
# [해설][흐름] `DeepAgentsHomeError`. 이때 traceback 대신 "메시지 출력 + exit 2"를 하는 대체 `cli_main`을 반환한다.
def __getattr__(name: str) -> Callable[[], None]:
    """Lazy import for `cli_main` to avoid loading `main.py` at package import.

    `main.py` pulls in `argparse`, signal handling, and other startup machinery
    that isn't needed when submodules like `config` or `widgets` are
    imported directly.

    Returns:
        The requested callable.

    Raises:
        AttributeError: If *name* is not a lazily-provided attribute.

    Note:
        Any import error other than an unresolvable profile location
        propagates unchanged; `DeepAgentsHomeError` is reported as a message
        plus exit 2 instead of a traceback.
    """
    if name == "cli_main":
        try:
            from deepagents_code.main import cli_main
        except DeepAgentsHomeError as exc:
            # `_paths` resolves the profile at import, so a bad DEEPAGENTS_HOME
            # or an unresolvable home surfaces here. Report it as a message
            # rather than a traceback: the user has a value to fix, and every
            # module that imports `_paths` would fail the same way.
            message = str(exc)

            # [해설] 예외 객체 대신 문자열을 클로저로 캡처한다(except 블록 종료 후 `exc` 이름은 삭제되므로).
            # [해설] 콘솔 스크립트는 이 함수를 호출해 stderr에 `dcode: ...`를 찍고 종료 코드 2로 끝난다.
            def cli_main() -> None:
                print(f"dcode: {message}", file=sys.stderr)  # noqa: T201
                raise SystemExit(2)

        return cli_main
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
