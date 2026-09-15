"""Import and initialize Python extension factories."""

# [해설] 이 모듈의 역할: extension 소스 파일(단일 .py 또는 패키지)을 import하고, 모듈의 `async def extension(api)`
# [해설] 팩토리를 실행해 레지스트리에 등록을 채운다. 실패하면 그 extension의 등록만 롤백한다(트랜잭션).
# [해설] 실행 프로세스: extension 런타임을 바인딩하는 쪽 — server_graph.py가 extensions/runtime.bind_server_extensions를
# [해설] 호출하므로 주로 서버 프로세스(추정: `--acp` in-process 경로에서도 동일 코드).
# [해설] 주요 진입점 심볼: load_extension. 호출자: extensions/runtime.py의 load_extensions(소스별 루프).
# [해설] 관련 분석 문서: analysis/07-mcp-hooks-extensions-plugins.md / 공식 문서: docs_official/code/extensions.md
# [해설][주의] extension은 신뢰된 코드로 취급되어 같은 프로세스에서 그대로 실행된다(샌드박스 없음).
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import sys
from typing import TYPE_CHECKING

from deepagents_code.extensions.api import ExtensionAPI, ExtensionMode
from deepagents_code.extensions.registry import ExtensionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from deepagents_code.extensions.registry import ExtensionRegistry, SourceInfo


# [해설] 절대 경로의 sha256 앞 16자로 고유 모듈 이름을 만든다. 같은 파일명(예: 여러 extension.py)이 충돌하지 않고,
# [해설] 같은 경로는 항상 같은 이름이 된다.
def _extension_module_name(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    return f"deepagents_code_extension_{digest}"


# [해설] 동기 import 단계(load_extension이 to_thread로 호출). 반환: (sys.modules 키, async 팩토리).
# [해설] 실패 시 sys.modules에서 제거해 반쯤 import된 모듈이 남지 않게 한다.
def _import_factory(
    source: SourceInfo,
) -> tuple[str, Callable[[ExtensionAPI], Awaitable[None]]]:
    # [해설][흐름] 1) 파일 위치로 spec 생성. 패키지면 submodule_search_locations를 줘서 상대 import가 되게 한다.
    name = _extension_module_name(source.path)
    spec = importlib.util.spec_from_file_location(
        name,
        source.path,
        submodule_search_locations=[str(source.path.parent)]
        if source.is_package
        else None,
    )
    if spec is None or spec.loader is None:
        msg = f"Could not import extension {source.path}"
        raise ExtensionError(msg)
    # [해설][흐름] 2) 모듈 실행 전에 sys.modules에 먼저 넣는다(모듈 내부의 자기 참조/순환 import 대비 표준 패턴).
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    # [해설] SystemExit까지 잡아 extension이 프로세스를 종료시키지 못하게 한다. KeyboardInterrupt만 그대로 전파.
    except (KeyboardInterrupt, SystemExit, Exception) as exc:
        sys.modules.pop(name, None)
        if isinstance(exc, KeyboardInterrupt):
            raise
        msg = (
            f"Extension import in {source.path} attempted to exit: {exc}"
            if isinstance(exc, SystemExit)
            else f"Failed to import {source.path}: {exc}"
        )
        raise ExtensionError(msg) from exc
    # [해설][흐름] 3) 모듈 최상위의 `extension` 이름이 callable이며 반드시 `async def`여야 한다.
    factory = getattr(module, "extension", None)
    if not callable(factory):
        sys.modules.pop(name, None)
        msg = f"{source.path} does not define a callable 'extension' factory"
        raise ExtensionError(msg)
    if not inspect.iscoroutinefunction(factory):
        sys.modules.pop(name, None)
        msg = f"Extension factory in {source.path} must be declared with 'async def'"
        raise ExtensionError(msg)
    return name, factory


# [해설] extension 하나를 트랜잭션으로 로드한다: 레지스트리 스냅샷 → 팩토리 실행 → 실패 시 롤백.
# [해설] 성공 시 ExtensionAPI(등록 창구)를 반환하며, 런타임이 이를 보관해 이후 종료 처리 등에 쓴다(추정).
async def load_extension(
    source: SourceInfo,
    registry: ExtensionRegistry,
    *,
    cwd: Path,
    mode: ExtensionMode,
) -> ExtensionAPI:
    """Load one extension transactionally.

    Args:
        source: Extension entry file and import shape.
        registry: Destination for registrations.
        cwd: Session working directory.
        mode: Runtime mode.

    Returns:
        The active registrar owned by the extension runtime.

    Raises:
        ExtensionError: If import or initialization fails.
        KeyboardInterrupt: If extension code interrupts the process.
        asyncio.CancelledError: If initialization is cancelled.
    """
    # [해설][흐름] 1) import는 파일 I/O·임의 최상위 코드가 있어 스레드에서 실행해 이벤트 루프를 막지 않는다.
    name, factory = await asyncio.to_thread(_import_factory, source)
    # [해설][흐름] 2) 팩토리 실행 전 레지스트리 크기 스냅샷(registry._snapshot) — 롤백 기준점.
    snapshot = registry._snapshot()
    api = ExtensionAPI(registry, source, cwd=cwd, mode=mode)
    # [해설][흐름] 3) 팩토리 실행. 취소/인터럽트는 롤백 후 원래 예외를 재발생(취소 의미 보존).
    try:
        await factory(api)
    except (KeyboardInterrupt, asyncio.CancelledError):
        registry._rollback(snapshot)
        api._deactivate()
        sys.modules.pop(name, None)
        raise
    # [해설] 그 외 예외·SystemExit은 롤백 + api 비활성화(이후 등록 호출 무효화) + 모듈 제거 후 ExtensionError로 감싼다.
    except (SystemExit, Exception) as exc:
        registry._rollback(snapshot)
        api._deactivate()
        sys.modules.pop(name, None)
        msg = (
            f"Extension factory in {source.path} attempted to exit: {exc}"
            if isinstance(exc, SystemExit)
            else f"Extension factory in {source.path} failed: {exc}"
        )
        raise ExtensionError(msg) from exc
    return api
