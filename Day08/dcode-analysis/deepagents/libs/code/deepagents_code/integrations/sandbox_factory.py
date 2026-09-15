"""Sandbox lifecycle management with provider abstraction."""

# [해설] 이 모듈의 역할: 원격 샌드박스의 수명주기(생성/연결 → setup 스크립트 → 사용(yield) → 정리)를 관리하고,
# [해설] built-in 프로바이더 6종(LangSmith, Daytona, Modal, Runloop, AgentCore, Vercel)의 구현 클래스를 담는다.
# [해설] 실행 프로세스:
# [해설]   - create_sandbox: 서버 프로세스(server_graph.py가 워크스페이스 env를 use_environment로 바인딩한 상태에서 호출).
# [해설]   - get_default_working_dir: 그래프 조립 시 agent.py(작업 디렉터리·criteria root 결정).
# [해설]   - verify_sandbox_deps: 클라이언트(main.py)가 서버 서브프로세스를 띄우기 "전"에 호출해 의존성 누락을 조기 보고.
# [해설] 이름→클래스 해석은 integrations/sandbox_registry.SandboxRegistry, 인터페이스는 integrations/sandbox_provider.SandboxProvider.
# [해설][설계] 자격증명은 모두 resolve_env_var(접두 DEEPAGENTS_CODE_* 우선 → 원래 이름)로 "워크스페이스 env 스냅샷"에서 읽는다.
# [해설] 워크스페이스가 일부만 고정한 자격을 서버 자격으로 몰래 채우는 것(권한 치환)은 fail-closed로 거부하는 것이 반복 패턴이다.
# [해설] 관련 분석 문서: analysis/08-sandboxes-execution.md, analysis/03-config-models-credentials.md
# [해설] 관련 공식 문서: docs_official/code/remote-sandboxes.md
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import os
import shlex
import string
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from rich.markup import escape as escape_markup

from deepagents_code.config import (
    AWS_CREDENTIAL_ENV_SOURCES,
    AWS_REGION_ENV_SOURCES,
    active_environment,
    console,
    get_glyphs,
    resolve_env_kwargs,
)
from deepagents_code.integrations.sandbox_provider import (
    SandboxNotFoundError,
    SandboxProvider,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from types import ModuleType

    from deepagents.backends.protocol import SandboxBackendProtocol

    from deepagents_code.integrations.sandbox_registry import SandboxRegistry


# [해설] 사용자의 setup 스크립트를 로컬에서 읽어 env 치환 후 샌드박스 안에서 `bash -c`로 실행한다.
# [해설] 호출자: create_sandbox(프로바이더가 샌드박스를 준비한 직후). 실패 시 FileNotFoundError/RuntimeError.
# [해설][주의] string.Template는 `${VAR}`뿐 아니라 `$VAR`도 치환한다. 로컬 env에 같은 이름(예: HOME)이 있으면
# [해설] 샌드박스에서 해석되길 기대한 값이 로컬 값으로 바뀐 채 전송될 수 있다(추정: active_environment에 해당 키가 포함될 때).
# [해설][주의] 치환된 비밀값이 스크립트 본문에 평문으로 들어가 원격 명령 인자로 전달된다.
def _run_sandbox_setup(backend: SandboxBackendProtocol, setup_script_path: str) -> None:
    """Run users setup script in sandbox with env var expansion.

    Args:
        backend: Sandbox backend instance
        setup_script_path: Path to setup script file

    Raises:
        FileNotFoundError: If the setup script does not exist.
        RuntimeError: If the setup script fails to execute.
    """
    # [해설][흐름] 1) 로컬 파일 존재 확인(스크립트 경로는 서버 프로세스 기준 로컬 경로).
    script_path = Path(setup_script_path)
    if not script_path.exists():
        msg = f"Setup script not found: {setup_script_path}"
        raise FileNotFoundError(msg)

    console.print(
        f"[dim]Running setup script: {escape_markup(setup_script_path)}...[/dim]"
    )

    # Read script content
    script_content = script_path.read_text(encoding="utf-8")

    # Expand ${VAR} syntax using the workspace environment. `create_sandbox`
    # runs inside `use_environment`, so this must read the bound snapshot:
    # `os.environ` here would expand the server process's values into the
    # sandbox and hide the workspace's own `.env`.
    # [해설][흐름] 2) safe_substitute: 매핑에 없는 변수는 원문 그대로 남기고 예외를 내지 않는다.
    template = string.Template(script_content)
    expanded_script = template.safe_substitute(active_environment())

    # Execute expanded script in sandbox
    # [해설][흐름] 3) shlex.quote로 스크립트 전체를 단일 인자로 만들어 SandboxBackendProtocol.execute로 원격 실행.
    # [해설][SDK] execute는 SDK `deepagents/backends/protocol.py`의 샌드박스 백엔드 메서드(ExecuteResponse 반환).
    result = backend.execute(f"bash -c {shlex.quote(expanded_script)}")

    # [해설][흐름] 4) 비정상 종료면 출력 표시 후 RuntimeError("Setup failed - aborting").
    if result.exit_code != 0:
        console.print(f"[red]Setup script failed (exit {result.exit_code}):[/red]")
        console.print(f"[dim]{escape_markup(result.output)}[/dim]")
        msg = "Setup failed - aborting"
        raise RuntimeError(msg)

    console.print(f"[green]{get_glyphs().checkmark} Setup complete[/green]")


# [해설] 샌드박스를 만들거나 기존 것에 연결해 백엔드를 yield하는 컨텍스트 매니저(단일 통합 진입점).
# [해설] 호출자: server_graph.py(서버 그래프 초기화). 반환된 backend는 agent.py에서 에이전트의 기본(execute) 백엔드가 된다.
# [해설] 부작용: 콘솔 출력, 원격 리소스 생성, 종료 시(직접 만든 경우) 원격 삭제.
@contextmanager
def create_sandbox(
    provider: str,
    *,
    sandbox_id: str | None = None,
    snapshot_name: str | None = None,
    setup_script_path: str | None = None,
    params: dict[str, Any] | None = None,
) -> Generator[SandboxBackendProtocol, None, None]:
    """Create or connect to a sandbox of the specified provider.

    This is the unified interface for sandbox creation using the
    provider abstraction.

    Args:
        provider: Sandbox provider name. Built-ins (`'agentcore'`, `'daytona'`,
            `'langsmith'`, `'modal'`, `'runloop'`, `'vercel'`), entry-point
            providers, and config-declared providers are all resolved through
            the registry.
        sandbox_id: Optional existing sandbox ID to reuse
        snapshot_name: Optional sandbox snapshot name to use or create.
            Honored by providers whose metadata sets `supports_snapshot_name`
            (built-ins: `'langsmith'` snapshot, `'runloop'` blueprint); must be
            `None` for other providers.
        setup_script_path: Optional path to setup script to run after sandbox starts
        params: Extra keyword arguments forwarded to `provider.get_or_create()`
            (e.g. config-declared `[sandboxes.providers.<name>.params]`).

    Yields:
        `SandboxBackendProtocol` instance

    Raises:
        ValueError: If `snapshot_name` is provided for an unsupported provider,
            or combined with `sandbox_id` (snapshots only apply to fresh sandboxes).
    """
    # [해설][흐름] 1) 레지스트리를 한 번만 로드(config 재파싱·entry point 재스캔 방지)하고 snapshot 제약을 검증한다.
    registry = _get_registry()
    metadata = registry.get_metadata(provider)
    if snapshot_name is not None and (
        metadata is None or not metadata.supports_snapshot_name
    ):
        msg = (
            f"snapshot_name is not supported by provider {provider!r} "
            f"(got snapshot_name={snapshot_name!r})"
        )
        raise ValueError(msg)
    # [해설] snapshot은 "새로 만들 때만" 의미가 있으므로 sandbox_id(재연결)와 함께 쓸 수 없다.
    if snapshot_name is not None and sandbox_id is not None:
        msg = (
            "snapshot_name cannot be combined with sandbox_id; "
            "snapshots only apply when creating a fresh sandbox"
        )
        raise ValueError(msg)

    # [해설][흐름] 2) 프로바이더 인스턴스화(config class_path > entry point > built-in).
    # [해설] built-in은 생성자에서 자격증명을 검증하므로 여기서 ValueError가 날 수 있다(아직 원격 리소스는 없음).
    # Get provider instance (reuse the registry already built above so we
    # don't re-read the config file and re-scan entry points).
    provider_obj = _get_provider(provider, registry=registry)

    # [해설][흐름] 3) 정리 여부: 사용자가 sandbox_id로 기존 샌드박스를 지정했다면 소유자가 아니므로 삭제하지 않는다.
    # Determine if we should cleanup (only cleanup if we created it)
    should_cleanup = sandbox_id is None
    # [해설] kwargs 병합 우선순위: config params < 호출자 params < snapshot 인자.
    provider_kwargs: dict[str, Any] = dict(registry.get_params(provider))
    if params:
        provider_kwargs.update(params)
    if snapshot_name is not None:
        provider_kwargs["snapshot"] = snapshot_name

    # [해설][흐름] 4) 생성/연결. 각 프로바이더가 `echo ready` 폴링 등으로 준비 완료까지 블로킹한다(기본 최대 180초).
    # Create or connect to sandbox
    console.print(f"[yellow]Starting {provider} sandbox...[/yellow]")
    backend = provider_obj.get_or_create(sandbox_id=sandbox_id, **provider_kwargs)
    glyphs = get_glyphs()
    console.print(
        f"[green]{glyphs.checkmark} {provider.capitalize()} sandbox ready: "
        f"{backend.id}[/green]"
    )

    # [해설][흐름] 5) setup 스크립트 실행.
    # [해설][주의] 이 호출은 아래 try/finally "바깥"에 있다. setup이 실패(RuntimeError/FileNotFoundError)하면
    # [해설] finally의 delete가 실행되지 않아, 방금 새로 만든 샌드박스가 정리되지 않고 남는다(원격 리소스·비용 누수 위험).
    # Run setup script if provided
    if setup_script_path:
        _run_sandbox_setup(backend, setup_script_path)

    # [해설][흐름] 6) 백엔드를 호출자에게 넘기고, 컨텍스트 종료(정상/예외 모두) 시 직접 만든 샌드박스만 삭제한다.
    try:
        yield backend
    finally:
        if should_cleanup:
            try:
                console.print(
                    f"[dim]Terminating {provider} sandbox {backend.id}...[/dim]"
                )
                provider_obj.delete(sandbox_id=backend.id)
                glyphs = get_glyphs()
                console.print(
                    f"[dim]{glyphs.checkmark} {provider.capitalize()} sandbox "
                    f"{backend.id} terminated[/dim]"
                )
            # [해설] 정리 실패는 경고만 출력 — 원래 발생한 예외를 가리지 않기 위함.
            except Exception as e:  # noqa: BLE001  # Cleanup errors should not mask the original sandbox failure
                warning = get_glyphs().warning
                console.print(
                    f"[yellow]{warning} Cleanup failed for {provider} sandbox "
                    f"{backend.id}: {e}[/yellow]"
                )


# [해설] 현재 사용자 config로 SandboxRegistry를 새로 만든다(캐시 없음). 한 작업 안에서는 결과를 재사용할 것.
def _get_registry() -> SandboxRegistry:
    """Build a `SandboxRegistry` from the current user config.

    Not cached: each call re-reads the config file and re-scans entry points so
    the registry reflects the latest state. Reuse a single instance within one
    operation (see `create_sandbox`) rather than calling this repeatedly.

    Returns:
        A fresh `SandboxRegistry`.
    """
    from deepagents_code.integrations.sandbox_registry import SandboxRegistry

    return SandboxRegistry.load()


# [해설] 프로바이더의 샌드박스 내 기본 작업 디렉터리 반환. 호출자: agent.py(샌드박스 모드의 작업 경로/criteria root/grader root).
# [해설] 알 수 없는 프로바이더면 ValueError.
def get_default_working_dir(provider: str) -> str:
    """Get the default working directory for a given sandbox provider.

    Args:
        provider: Sandbox provider name. Resolved through the registry so
            built-in, entry-point, and config providers are all supported.

    Returns:
        Default working directory path as string

    Raises:
        ValueError: If provider is unknown
    """
    metadata = _get_registry().get_metadata(provider)
    if metadata is None:
        msg = f"Unknown sandbox provider: {provider}"
        raise ValueError(msg)
    return metadata.working_dir


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


# [해설] 선택 의존성(프로바이더 SDK)을 import하고, 없으면 `/install <provider>` 안내가 담긴 ImportError로 바꿔 던진다.
# [해설] 각 프로바이더 생성자/get_or_create가 필요 시점에 지연 import하는 데 사용.
def _import_provider_module(
    module_name: str,
    *,
    provider: str,
    package: str,
) -> ModuleType:
    """Import an optional provider module with a provider-specific error message.

    Args:
        module_name: Python module name to import.
        provider: Sandbox provider name (e.g. `'daytona'`).
        package: PyPI package name exposed by the package extra.

    Returns:
        The imported module object.

    Raises:
        ImportError: If the optional dependency is not installed.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"The '{provider}' sandbox provider requires the '{package}' package. "
            f"Install it with: /install {provider} (in-app) or "
            f"dcode install {provider} (CLI)"
        )
        raise ImportError(msg) from exc


# [해설] LangSmith 기본값 3종: 스냅샷 이름, 스냅샷 빌드 이미지(python:3), 파일시스템 용량 16GiB.
# [해설] _LangSmithProvider.get_or_create가 인자/env가 없을 때 사용. 파일 도구가 python3에 의존하므로 python 이미지가 기본(추정).
_LANGSMITH_DEFAULT_SNAPSHOT = "deepagents-code"
"""Default LangSmith sandbox snapshot name used when none is specified."""

_LANGSMITH_DEFAULT_IMAGE = "python:3"
"""Default Docker image for LangSmith sandbox snapshots when none is provided."""

_LANGSMITH_DEFAULT_FS_CAPACITY_BYTES = 16 * 1024**3
"""Default filesystem capacity (16 GiB) for LangSmith sandbox snapshots."""


# [해설] LangSmith 샌드박스 프로바이더. 이름으로 스냅샷을 찾고 없으면 Docker 이미지로 빌드한 뒤 그 스냅샷에서 부팅한다.
class _LangSmithProvider(SandboxProvider):
    """LangSmith sandbox provider implementation.

    Manages LangSmith sandbox lifecycle using the LangSmith SDK, booting
    sandboxes from snapshots built from a Docker image.
    """

    # [해설] API 키 우선순위: 인자 > LANGSMITH_SANDBOX_API_KEY > LANGSMITH_API_KEY > LANGCHAIN_API_KEY
    # [해설] (각각 resolve_env_var로 DEEPAGENTS_CODE_ 접두 버전이 먼저). 키가 없으면 ValueError.
    def __init__(self, api_key: str | None = None) -> None:
        """Initialize LangSmith provider.

        Args:
            api_key: LangSmith API key (defaults to `LANGSMITH_SANDBOX_API_KEY`,
                then `LANGSMITH_API_KEY` env var).

        Raises:
            ValueError: If no LangSmith API key is found.
        """
        from langsmith.sandbox import SandboxClient

        from deepagents_code.model_config import resolve_env_var

        sandbox_key = resolve_env_var("LANGSMITH_SANDBOX_API_KEY")
        if sandbox_key:
            logger.debug("Using LangSmith API key from LANGSMITH_SANDBOX_API_KEY")
        self._api_key: str | None = (
            api_key
            or sandbox_key
            or resolve_env_var("LANGSMITH_API_KEY")
            or resolve_env_var("LANGCHAIN_API_KEY")
        )
        if not self._api_key:
            msg = (
                "No LangSmith sandbox API key found. Set "
                "LANGSMITH_API_KEY, LANGCHAIN_API_KEY, or LANGSMITH_SANDBOX_API_KEY "
                "(or the DEEPAGENTS_CODE_-prefixed equivalents)."
            )
            raise ValueError(msg)
        self._client: SandboxClient = SandboxClient(api_key=self._api_key)

    # [해설] LangSmith 샌드박스 연결/생성. sandbox_id는 LangSmith에서 샌드박스 "이름"이다.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        snapshot: str | None = None,
        snapshot_image: str | None = None,
        fs_capacity_bytes: int | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get existing or create new LangSmith sandbox.

        Args:
            sandbox_id: Optional existing sandbox name to reuse.
            timeout: Timeout in seconds for sandbox startup.
            snapshot: Snapshot name to boot from.

                Resolved to a snapshot ID, creating the snapshot from
                `snapshot_image` if missing. Overrides
                `LANGSMITH_SANDBOX_SNAPSHOT_NAME`; overridden by
                `LANGSMITH_SANDBOX_SNAPSHOT_ID` (ID, wins over everything).
            snapshot_image: Docker image used when building the snapshot.
            fs_capacity_bytes: Filesystem capacity when building the snapshot.
            **kwargs: Additional LangSmith-specific parameters.

        Returns:
            `LangSmithSandbox` instance.

        Raises:
            RuntimeError: If sandbox connection or startup fails.
            TypeError: If unsupported keyword arguments are provided.
        """
        from deepagents.backends.langsmith import LangSmithSandbox

        from deepagents_code.model_config import resolve_env_var

        # [해설] 알 수 없는 kwargs는 TypeError → config params 오타가 조용히 무시되지 않는다.
        if kwargs:
            msg = f"Received unsupported arguments: {list(kwargs.keys())}"
            raise TypeError(msg)
        # [해설][흐름] 1) 재연결: 이름으로 조회만 하고 준비 폴링은 하지 않는다.
        if sandbox_id:
            # Connect to existing sandbox by name
            try:
                sandbox = self._client.get_sandbox(name=sandbox_id)
            except Exception as e:
                msg = f"Failed to connect to existing sandbox '{sandbox_id}': {e}"
                raise RuntimeError(msg) from e
            return LangSmithSandbox(sandbox)

        # [해설][흐름] 2) 스냅샷 결정 우선순위: env LANGSMITH_SANDBOX_SNAPSHOT_ID(이름 조회·빌드 생략) > 인자 snapshot
        # [해설] > env LANGSMITH_SANDBOX_SNAPSHOT_NAME > 기본 "deepagents-code". 이름 경로는 _ensure_snapshot으로 ID 확보.
        # Explicit snapshot ID wins — skip name lookup and auto-build.
        env_snapshot_id = resolve_env_var("LANGSMITH_SANDBOX_SNAPSHOT_ID")
        if env_snapshot_id:
            snapshot_id = env_snapshot_id
            snapshot_name = env_snapshot_id
        else:
            env_snapshot_name = resolve_env_var("LANGSMITH_SANDBOX_SNAPSHOT_NAME")
            snapshot_name = snapshot or env_snapshot_name or _LANGSMITH_DEFAULT_SNAPSHOT
            image = snapshot_image or _LANGSMITH_DEFAULT_IMAGE
            capacity = fs_capacity_bytes or _LANGSMITH_DEFAULT_FS_CAPACITY_BYTES
            snapshot_id = self._ensure_snapshot(snapshot_name, image, capacity)

        # [해설][흐름] 3) 스냅샷 ID로 샌드박스 생성.
        try:
            sandbox = self._client.create_sandbox(
                snapshot_id=snapshot_id, timeout=timeout
            )
        except Exception as e:
            msg = f"Failed to create sandbox from snapshot '{snapshot_name}': {e}"
            raise RuntimeError(msg) from e

        # [해설][흐름] 4) 준비 폴링: timeout//2회 반복 × 2초 sleep. 각 시도에도 5초 타임아웃이 있어 실제 대기는 timeout보다 길 수 있다.
        # Verify sandbox is ready by polling
        for _ in range(timeout // 2):
            try:
                result = sandbox.run("echo ready", timeout=5)
                if result.exit_code == 0:
                    break
            except Exception:  # noqa: S110, BLE001  # Sandbox not ready yet, continue polling
                pass
            time.sleep(2)
        # [해설] for-else: break 없이 루프가 끝나면(준비 실패) best-effort 삭제 후 RuntimeError.
        else:
            # Cleanup on failure
            with contextlib.suppress(Exception):
                self._client.delete_sandbox(sandbox.name)
            msg = f"LangSmith sandbox failed to start within {timeout} seconds"
            raise RuntimeError(msg)

        return LangSmithSandbox(sandbox)

    # [해설] 샌드박스 이름으로 삭제. create_sandbox의 finally에서 호출.
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002  # Required by SandboxFactory interface
        """Delete a LangSmith sandbox.

        Args:
            sandbox_id: Sandbox name to delete
            **kwargs: Additional parameters
        """
        self._client.delete_sandbox(sandbox_id)

    # [해설] 스냅샷 이름 → ID. ready 상태면 재사용, 같은 이름이 building/failed면 중복 빌드 대신 에러,
    # [해설] 아예 없으면 create_snapshot으로 빌드(준비될 때까지 블로킹 — 첫 실행이 오래 걸리는 이유).
    def _ensure_snapshot(
        self,
        snapshot_name: str,
        image: str,
        fs_capacity_bytes: int,
    ) -> str:
        """Resolve a snapshot by name, building it from `image` if missing.

        The LangSmith API exposes snapshots by ID, so we list and filter by
        name. Only snapshots with `status == "ready"` are returned; a
        matching-name snapshot in a non-ready state (`"building"`,
        `"failed"`, etc.) raises rather than triggering a duplicate build,
        which would mask the in-flight/failed snapshot.

        When no matching snapshot exists at all, we build one with
        `create_snapshot`, which blocks until the snapshot is ready.

        Returns:
            The snapshot ID ready to be passed to `create_sandbox`.

        Raises:
            RuntimeError: If listing or building the snapshot fails, or if
                a matching-name snapshot exists but is not ready.
        """
        try:
            snapshots = self._client.list_snapshots()
        except Exception as e:
            msg = f"Failed to list snapshots: {e}"
            raise RuntimeError(msg) from e

        # [해설] 같은 이름이 여러 개면 ready인 것이 하나라도 있으면 그 ID를 반환, 아니면 마지막 상태를 기억.
        non_ready_status: str | None = None
        for snap in snapshots:
            if snap.name != snapshot_name:
                continue
            if snap.status == "ready":
                return snap.id
            non_ready_status = snap.status

        if non_ready_status is not None:
            msg = (
                f"Snapshot '{snapshot_name}' exists but is in state "
                f"'{non_ready_status}'. Wait for it to finish building, or "
                f"delete it to rebuild."
            )
            raise RuntimeError(msg)

        # [해설] 스냅샷이 전혀 없을 때만 빌드.
        try:
            snapshot = self._client.create_snapshot(
                name=snapshot_name,
                docker_image=image,
                fs_capacity_bytes=fs_capacity_bytes,
            )
        except Exception as create_err:
            msg = f"Failed to build snapshot '{snapshot_name}': {create_err}"
            raise RuntimeError(msg) from create_err
        return snapshot.id


# [해설] Daytona 프로바이더. 생성만 지원(재연결 미지원)하고 `echo ready`로 준비를 확인한다.
class _DaytonaProvider(SandboxProvider):
    """Daytona sandbox provider — lifecycle management for Daytona sandboxes."""

    # [해설] daytona SDK 지연 import 후 DAYTONA_API_KEY(필수)/DAYTONA_API_URL(선택)로 클라이언트 생성.
    def __init__(self) -> None:
        daytona_module = _import_provider_module(
            "daytona",
            provider="daytona",
            package="langchain-daytona",
        )

        from deepagents_code.model_config import resolve_env_var

        api_key = resolve_env_var("DAYTONA_API_KEY")
        if not api_key:
            msg = (
                "No Daytona API key found. Set DAYTONA_API_KEY "
                "or DEEPAGENTS_CODE_DAYTONA_API_KEY."
            )
            raise ValueError(msg)
        self._client = daytona_module.Daytona(
            daytona_module.DaytonaConfig(
                api_key=api_key,
                api_url=resolve_env_var("DAYTONA_API_URL"),
            )
        )

    # [해설] 새 Daytona 샌드박스 생성 + 준비 폴링. kwargs(config params)는 무시된다.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,  # noqa: ARG002
    ) -> SandboxBackendProtocol:
        """Get or create a Daytona sandbox.

        Args:
            sandbox_id: Not supported yet — must be None.
            timeout: Seconds to wait for startup.
            **kwargs: Unused.

        Returns:
            `DaytonaSandbox` instance.

        Raises:
            NotImplementedError: If `sandbox_id` is provided.
            RuntimeError: If the sandbox fails to start.
        """
        daytona_backend = _import_provider_module(
            "langchain_daytona",
            provider="daytona",
            package="langchain-daytona",
        )

        # [해설][주의] sandbox_id 재연결 미지원. 그런데 sandbox_registry.BUILTIN_METADATA의 daytona 항목은
        # [해설] supports_sandbox_id를 지정하지 않아 기본값 True로 광고된다(메타데이터와 동작 불일치).
        if sandbox_id:
            msg = (
                "Connecting to existing Daytona sandbox by ID not yet supported. "
                "Create a new sandbox by omitting sandbox_id parameter."
            )
            raise NotImplementedError(msg)

        # [해설][흐름] 생성 → 폴링(마지막 예외를 보존해 실패 메시지에 포함) → 실패 시 best-effort 삭제 후 RuntimeError.
        sandbox = self._client.create()
        last_exc: Exception | None = None
        for _ in range(timeout // 2):
            try:
                result = sandbox.process.exec("echo ready", timeout=5)
                if result.exit_code == 0:
                    break
            except Exception as exc:  # noqa: BLE001  # Transient failures expected during readiness polling
                last_exc = exc
            time.sleep(2)
        else:
            with contextlib.suppress(Exception):  # Best-effort cleanup
                sandbox.delete()
            detail = f" Last error: {last_exc}" if last_exc else ""
            msg = f"Daytona sandbox failed to start within {timeout} seconds.{detail}"
            raise RuntimeError(msg)

        return daytona_backend.DaytonaSandbox(sandbox=sandbox)

    # [해설] ID로 조회한 뒤 삭제(Daytona SDK는 객체를 받아 삭제).
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Delete a Daytona sandbox by id."""
        sandbox = self._client.get(sandbox_id)
        self._client.delete(sandbox)


# [해설] Modal 프로바이더. App "deepagents-sandbox"에 샌드박스를 만들고 workdir=/workspace로 실행한다.
class _ModalProvider(SandboxProvider):
    """Modal sandbox provider — lifecycle management for Modal sandboxes."""

    # [해설] 생성자에서 인증 방식 결정과 App 조회(없으면 생성)까지 수행 → 네트워크 호출이 생성자에서 일어난다.
    def __init__(self) -> None:
        """Initialize the Modal provider.

        Raises:
            ValueError: If the resolved Modal credentials are invalid, or if
                only one half of the `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`
                pair resolves. Delegating to default Modal authentication
                there would be a silent privilege substitution.
        """
        self._modal = _import_provider_module(
            "modal",
            provider="modal",
            package="langchain-modal",
        )

        from deepagents_code.model_config import resolve_env_var

        # [해설][흐름] 토큰 쌍 3가지 경우: (a) 둘 다 있음 → 명시 자격으로 Client 생성, (b) 하나만 → fail-closed ValueError,
        # [해설] (c) 둘 다 없음 → client=None으로 Modal 기본 인증에 위임.
        token_id = resolve_env_var("MODAL_TOKEN_ID")
        token_secret = resolve_env_var("MODAL_TOKEN_SECRET")
        if token_id and token_secret:
            try:
                self._client = self._modal.Client.from_credentials(
                    token_id, token_secret
                )
            except Exception as exc:
                msg = (
                    "Failed to authenticate with Modal using "
                    "MODAL_TOKEN_ID / MODAL_TOKEN_SECRET "
                    "(or the DEEPAGENTS_CODE_-prefixed equivalents). "
                    "Verify your credentials are valid."
                )
                raise ValueError(msg) from exc
        # [해설][주의] (b) 반쪽 자격: 경고 후 기본 인증으로 넘기면 워크스페이스가 제한 토큰을 의도했어도 서버의 넓은 권한으로 실행된다.
        elif token_id or token_secret:
            # Fail closed rather than warn and delegate: a `None` client makes
            # `App.lookup` resolve credentials from the server process, so a
            # workspace that pinned a restricted token would silently run its
            # sandbox under the server's broader Modal identity.
            missing = "MODAL_TOKEN_SECRET" if token_id else "MODAL_TOKEN_ID"
            msg = (
                "The workspace Modal configuration is incomplete: "
                f"{missing} is not set. Set MODAL_TOKEN_ID and "
                "MODAL_TOKEN_SECRET together, or unset both to fall back to "
                "default Modal authentication."
            )
            raise ValueError(msg)
        # [해설] (c) Modal SDK 기본 자격 탐색(서버 프로세스 env 또는 Modal 설정 파일 — 추정)에 맡긴다.
        else:
            self._client = None

        # [해설] client가 있을 때만 kwargs에 넣는다. None을 명시 전달하지 않아 "워크스페이스 자격 적용"으로 오인되지 않게.
        lookup_kwargs: dict[str, Any] = {
            "name": "deepagents-sandbox",
            "create_if_missing": True,
        }
        if self._client is not None:
            lookup_kwargs["client"] = self._client
        self._app = self._modal.App.lookup(**lookup_kwargs)

    # [해설] Modal 샌드박스 연결(from_id) 또는 생성 + 준비 폴링.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,  # noqa: ARG002
    ) -> SandboxBackendProtocol:
        """Get or create a Modal sandbox.

        Args:
            sandbox_id: Existing sandbox ID, or None to create.
            timeout: Seconds to wait for startup.
            **kwargs: Unused.

        Returns:
            `ModalSandbox` instance.

        Raises:
            RuntimeError: If the sandbox fails to start.
        """
        modal_backend = _import_provider_module(
            "langchain_modal",
            provider="modal",
            package="langchain-modal",
        )

        client_kwargs: dict[str, Any] = {}
        if self._client is not None:
            client_kwargs["client"] = self._client

        # [해설] 재연결 경로: 폴링 없이 ID로 핸들만 얻는다.
        if sandbox_id:
            sandbox = self._modal.Sandbox.from_id(
                sandbox_id=sandbox_id,
                app=self._app,
                **client_kwargs,
            )
        else:
            # [해설] 생성 경로: workdir="/workspace"는 BUILTIN_METADATA의 modal working_dir와 일치해야 한다.
            sandbox = self._modal.Sandbox.create(
                app=self._app, workdir="/workspace", **client_kwargs
            )
            last_exc: Exception | None = None
            for _ in range(timeout // 2):
                # [해설] poll()이 None이 아니면 샌드박스가 이미 종료됨 → 더 기다리지 않고 즉시 실패.
                if sandbox.poll() is not None:
                    msg = "Modal sandbox terminated unexpectedly during startup"
                    raise RuntimeError(msg)
                try:
                    process = sandbox.exec("echo", "ready", timeout=5)
                    process.wait()
                    if process.returncode == 0:
                        break
                except Exception as exc:  # noqa: BLE001  # Transient failures expected during readiness polling
                    last_exc = exc
                time.sleep(2)
            # [해설] 준비 시간 초과 시 terminate 후 RuntimeError.
            else:
                sandbox.terminate()
                detail = f" Last error: {last_exc}" if last_exc else ""
                msg = f"Modal sandbox failed to start within {timeout} seconds.{detail}"
                raise RuntimeError(msg)

        return modal_backend.ModalSandbox(sandbox=sandbox)

    # [해설] ID로 샌드박스 핸들을 얻어 terminate. 생성자와 같은 client 규칙 적용.
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Terminate a Modal sandbox by id."""
        del_kwargs: dict[str, Any] = {"sandbox_id": sandbox_id, "app": self._app}
        if self._client is not None:
            del_kwargs["client"] = self._client
        sandbox = self._modal.Sandbox.from_id(**del_kwargs)
        sandbox.terminate()


# [해설] Runloop 프로바이더. 실제 로직은 langchain_runloop.RunloopProvider에 위임하는 얇은 어댑터.
class _RunloopProvider(SandboxProvider):
    """Runloop sandbox provider — delegates to `langchain_runloop.RunloopProvider`."""

    # [해설] RUNLOOP_API_KEY 필수. resolve_env_var 함수 자체를 넘겨 하위 패키지도 워크스페이스 env/접두 규칙을 따르게 한다.
    def __init__(self) -> None:
        runloop_module = _import_provider_module(
            "langchain_runloop",
            provider="runloop",
            package="langchain-runloop",
        )

        from deepagents_code.model_config import resolve_env_var

        api_key = resolve_env_var("RUNLOOP_API_KEY")
        if not api_key:
            msg = (
                "No Runloop API key found. Set RUNLOOP_API_KEY "
                "or DEEPAGENTS_CODE_RUNLOOP_API_KEY."
            )
            raise ValueError(msg)
        self._provider = runloop_module.RunloopProvider(
            api_key=api_key,
            resolve_env_var=resolve_env_var,
        )

    # [해설] devbox 연결/생성. snapshot(blueprint 이름)·blueprint_dockerfile 같은 kwargs는 그대로 위임.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Runloop devbox.

        Args:
            sandbox_id: Existing devbox ID, or None to create.
            timeout: Accepted for parity with other providers; currently
                forwarded but unused by the Runloop backend (the SDK manages
                its own startup wait).
            **kwargs: Runloop-specific options (`snapshot` blueprint name,
                `blueprint_dockerfile`).

        Returns:
            `RunloopSandbox` instance.

        Raises:
            SandboxNotFoundError: If `sandbox_id` does not exist. `RunloopProvider`
                translates the SDK's not-found error into a `KeyError`, which is
                mapped here.
            KeyError: If a `KeyError` is raised while no `sandbox_id` was supplied
                (re-raised unchanged rather than mislabeled as not-found).
        """
        try:
            return self._provider.get_or_create(
                sandbox_id=sandbox_id,
                timeout=timeout,
                **kwargs,
            )
        # [해설] 위임 대상이 not-found를 KeyError로 알리므로, sandbox_id를 준 경우에만 SandboxNotFoundError로 의미 변환.
        except KeyError as e:
            if sandbox_id is None:
                raise
            raise SandboxNotFoundError(sandbox_id) from e

    # [해설] devbox 종료 위임.
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002
        """Shut down a Runloop devbox by id."""
        self._provider.delete(sandbox_id=sandbox_id)


# [해설] 같은 자격증명 표를 "서버 os.environ"만으로 해석한 기준값을 만든다.
# [해설] 호출자: _AgentCoreProvider.__init__. 워크스페이스 결과와 같으면 워크스페이스가 따로 고정한 자격이 없다고 판단.
def _server_env_kwargs(table: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    """Resolve a credential table against the server's own environment.

    Workspace resolution reads `active_environment()`, which falls back to
    `os.environ`, so a plain `AWS_PROFILE` or `VERCEL_TOKEN` exported in the
    server's shell is indistinguishable from one a workspace pinned. Resolving
    the same table against `os.environ` explicitly gives the caller a baseline
    to compare against: an equal result means the workspace pinned nothing of
    its own, so there is no workspace identity to protect.

    Args:
        table: Constructor argument name mapped to candidate env var names.

    Returns:
        The arguments the server would resolve on its own.
    """
    from deepagents_code.config import _resolve_env_var_from

    return resolve_env_kwargs(
        table, lambda name: _resolve_env_var_from(os.environ, name)
    )


# [해설] 워크스페이스 env를 boto3.Session kwargs로 변환하고, 불완전한 조합을 boto3에 닿기 전에 명확한 메시지로 거부한다.
# [해설] 호출자: _AgentCoreProvider.__init__. 모델 경로와 같은 AWS_CREDENTIAL_ENV_SOURCES 표·resolve_env_var를 공유.
def _aws_session_kwargs() -> dict[str, str]:
    """Translate the bound workspace environment into boto3 session arguments.

    Resolves through `resolve_env_var` so a `DEEPAGENTS_CODE_`-prefixed
    override is honored here exactly as it is on the model path. Both paths
    share `AWS_CREDENTIAL_ENV_SOURCES`; sharing the table alone left the
    *lookup* free to drift, so a prefixed value applied to the model and was
    dropped for the sandbox.

    Validates the resolved set as a whole. A pair check alone let a session
    token with neither key half through, and botocore then declines the
    explicit credential provider and falls through to the server's own
    credentials -- so the workspace's token is silently ignored.

    Returns:
        Populated boto3 session keyword arguments.

    Raises:
        ValueError: If the resolved credentials are incomplete. Checked before
            boto3 is touched so the error names the variable at fault; boto3's
            own `PartialCredentialsError` names neither.
    """
    from deepagents_code.model_config import resolve_env_var

    # [해설][흐름] 1) 표 기반 해석(접두 우선).
    resolved = resolve_env_kwargs(AWS_CREDENTIAL_ENV_SOURCES, resolve_env_var)
    key_id = resolved.get("aws_access_key_id")
    secret = resolved.get("aws_secret_access_key")
    # [해설][흐름] 2) 키 ID/시크릿 중 하나만 있으면 거부.
    if bool(key_id) != bool(secret):
        missing = "AWS_SECRET_ACCESS_KEY" if key_id else "AWS_ACCESS_KEY_ID"
        msg = (
            f"The workspace AWS configuration is incomplete: {missing} is not "
            f"set. Set both AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in "
            f"this workspace's .env, or unset both to use AWS_PROFILE or the "
            f"server's default credentials."
        )
        raise ValueError(msg)
    # [해설][흐름] 3) 세션 토큰만 있고 키가 없으면 거부 — botocore가 명시 자격을 버리고 서버 자격으로 넘어가는 것을 방지.
    if resolved.get("aws_session_token") and not key_id:
        msg = (
            "The workspace AWS configuration is incomplete: AWS_SESSION_TOKEN "
            "is set without AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. Set "
            "all three together, or unset AWS_SESSION_TOKEN to use AWS_PROFILE "
            "or the server's default credentials."
        )
        raise ValueError(msg)
    # `AWS_PROFILE` alongside explicit keys is left alone deliberately. boto3's
    # precedence is documented and deterministic, and because
    # `active_environment()` layers the workspace over the server's own
    # environment, an exported profile plus workspace keys is an ordinary
    # combination rather than a mistake. Rejecting it would turn a working
    # setup into a startup failure.
    return resolved


# [해설] AWS Bedrock AgentCore Code Interpreter 프로바이더. 세션은 앱 종료 후 재연결할 수 없다.
class _AgentCoreProvider(SandboxProvider):
    """AgentCore Code Interpreter sandbox provider.

    Manages AgentCore session lifecycle. Sessions cannot be reconnected after
    the app exits — the `sandbox_id` parameter is not supported.
    """

    # [해설] 리전 결정과 AWS 자격 사전 검증을 수행한다. 워크스페이스 고정 자격이 잘못되면 fail-closed.
    def __init__(self, region: str | None = None) -> None:
        """Initialize AgentCore provider.

        Args:
            region: AWS region (defaults to `AWS_REGION` /
                `AWS_DEFAULT_REGION` / `us-west-2`).

        Raises:
            ValueError: If boto3 is installed and AWS credentials cannot be
                resolved, or if the workspace scoped the sandbox to specific
                AWS credentials that turn out to be invalid or incomplete.
                Falling back to the server's own credentials there would be a
                silent privilege substitution.
        """
        # [해설][흐름] 1) 리전: 인자 > AWS_REGION_ENV_SOURCES 표(워크스페이스 env) > "us-west-2".
        environment = active_environment()
        # Region resolves through the same table-driven helper as the
        # credentials, so both follow the model path's source order.
        aws_kwargs = resolve_env_kwargs(
            {**AWS_CREDENTIAL_ENV_SOURCES, "region_name": AWS_REGION_ENV_SOURCES},
            environment.get,
        )
        self._region = region or aws_kwargs.get("region_name") or "us-west-2"
        self._session: Any = None

        # [해설][흐름] 2) boto3가 설치돼 있으면 세션을 만들고 get_credentials로 조기 검증. 없으면 검증 생략.
        # Validate AWS credentials early for a clear error message.
        credential_kwargs: dict[str, str] = {}
        try:
            import boto3  # ty: ignore[unresolved-import]

            credential_kwargs = _aws_session_kwargs()
            self._session = boto3.Session(region_name=self._region, **credential_kwargs)
            credentials = self._session.get_credentials()
            if credentials is None:
                msg = (
                    "AWS credentials not found. Configure via "
                    "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN, "
                    "~/.aws/credentials, or an IAM role."
                )
                raise ValueError(msg)  # noqa: TRY301  # intentional raise for early credential validation
        except ImportError:
            logger.debug("boto3 not installed; skipping credential pre-check")
        # [해설] 우리가 만든 명확한 ValueError(자격 없음/불완전)는 그대로 전파.
        except ValueError:
            raise
        # [해설][흐름] 3) 기타 예외(프로파일 없음 등): 워크스페이스가 서버와 다른 자격을 고정했으면 거부,
        # [해설] 아니면 경고만 남기고 session=None(인터프리터가 서버 자격으로 스스로 해석).
        except Exception as exc:
            # Without a session the interpreter resolves credentials itself,
            # from the server process rather than this workspace. That is the
            # right default when the workspace scoped nothing, and a privilege
            # substitution when it did -- a workspace pinned to a restricted
            # profile would silently run under the server's broader identity.
            #
            # Compare against what the server resolves on its own: a plain
            # `AWS_PROFILE` exported in the server's shell reaches
            # `active_environment()` too, and treating that as workspace-pinned
            # turned a stale server-level profile into a startup failure whose
            # message blamed a workspace `.env` that never set it.
            server_kwargs = _server_env_kwargs(AWS_CREDENTIAL_ENV_SOURCES)
            if credential_kwargs and credential_kwargs != server_kwargs:
                msg = (
                    f"The workspace AWS configuration is invalid: {exc}. This "
                    f"workspace scoped its sandbox to specific AWS credentials "
                    f"({', '.join(sorted(credential_kwargs))}), so the server's own "
                    f"credentials are not substituted. Fix AWS_PROFILE / "
                    f"AWS_ACCESS_KEY_ID in this workspace's .env, or unset "
                    f"them to use the server's default credentials."
                )
                raise ValueError(msg) from exc
            logger.warning(
                "Could not build an AWS session from the workspace environment "
                "(region=%s). The sandbox will NOT use workspace AWS "
                "credentials and may fail to start. Check your AWS "
                "configuration.",
                self._region,
                exc_info=True,
            )
            self._session = None

        # [해설][주의] backend.id → interpreter 매핑은 이 프로세스 메모리에만 있다. 프로세스가 바뀌면 delete가 세션을 찾지 못한다.
        self._active_interpreters: dict[str, Any] = {}

    # [해설] 새 Code Interpreter 세션을 시작해 AgentCoreSandbox로 감싼다. sandbox_id는 미지원.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,  # noqa: ARG002  # required by SandboxProvider interface
    ) -> SandboxBackendProtocol:
        """Create a new AgentCore Code Interpreter session.

        Args:
            sandbox_id: Not supported — raises `NotImplementedError`
                if provided.
            **kwargs: Additional parameters (unused).

        Returns:
            `AgentCoreSandbox` instance wrapping the started interpreter.

        Raises:
            NotImplementedError: If `sandbox_id` is provided.
        """
        if sandbox_id:
            msg = (
                "AgentCore does not support reconnecting to existing sessions. "
                "Remove the --sandbox-id option."
            )
            raise NotImplementedError(msg)

        agentcore_module = _import_provider_module(
            "bedrock_agentcore.tools.code_interpreter_client",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )
        agentcore_backend = _import_provider_module(
            "langchain_agentcore_codeinterpreter",
            provider="agentcore",
            package="langchain-agentcore-codeinterpreter",
        )

        # [해설] integration_source로 호출 출처(deepagents-code)를 표시하고, 세션은 만든 경우에만 전달.
        # Pass `session` only when one was built. Handing over `None` would let
        # "no workspace session" masquerade as "workspace session applied",
        # since the SDK then falls back to its own credential resolution.
        interpreter_kwargs: dict[str, Any] = {
            "region": self._region,
            "integration_source": "deepagents-code",
        }
        if self._session is not None:
            interpreter_kwargs["session"] = self._session
        interpreter = agentcore_module.CodeInterpreter(**interpreter_kwargs)
        # [해설] start 실패 시 best-effort stop 후 원래 예외 재발생(반쯤 시작된 세션 정리).
        try:
            interpreter.start()
        except Exception:
            with contextlib.suppress(Exception):
                interpreter.stop()
            raise

        # [해설] delete에서 stop할 수 있도록 활성 목록에 등록.
        backend = agentcore_backend.AgentCoreSandbox(interpreter=interpreter)
        self._active_interpreters[backend.id] = interpreter
        return backend

    # [해설] 추적 중인 세션을 stop. 실패하면 비용 발생 가능성을 경고하지만 예외는 삼킨다.
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002  # required by SandboxProvider interface
        """Stop an AgentCore session.

        Args:
            sandbox_id: Session ID to stop.
            **kwargs: Additional parameters (unused).
        """
        interpreter = self._active_interpreters.pop(sandbox_id, None)
        if interpreter:
            try:
                interpreter.stop()
                logger.info("AgentCore session %s stopped", sandbox_id)
            except Exception:
                logger.warning(
                    "Failed to stop AgentCore session %s — the session may "
                    "still be running and incurring costs. Check the AWS "
                    "console to verify.",
                    sandbox_id,
                    exc_info=True,
                )
        else:
            logger.info(
                "AgentCore session %s not tracked (may have already expired)",
                sandbox_id,
            )


# [해설] Vercel SDK SandboxStatus를 흉내 낸 Literal 타입. SDK를 동적 import하므로 타입 검사를 위해 로컬에 정의.
_VercelStatus = Literal[
    "pending",
    "running",
    "stopping",
    "stopped",
    "failed",
    "aborted",
    "snapshotting",
]
"""Vercel sandbox lifecycle statuses (mirrors the SDK's `SandboxStatus` enum)."""

# [해설] 다시 running이 될 수 없는 종료 상태 집합. _wait_until_running이 즉시 실패 판단에 사용.
_VERCEL_TERMINAL_STATUSES: frozenset[_VercelStatus] = frozenset(
    {"aborted", "failed", "stopped"}
)
"""Vercel sandbox statuses that cannot become ready without a new sandbox.

Typing the members as `_VercelStatus` turns a typo into a type error rather than
a silently-never-matching string.
"""

# [해설] 새 Vercel 샌드박스의 런타임(python3.13)과 수명(30분).
# [해설][주의] 수명이 30분으로 고정이라, 그보다 긴 세션에서는 샌드박스가 만료될 수 있다(추정: SDK timeout 의미가 수명일 때).
_VERCEL_DEFAULT_RUNTIME = "python3.13"
"""Runtime used when creating a fresh Vercel sandbox."""

_VERCEL_SANDBOX_TIMEOUT = timedelta(minutes=30)
"""Lifetime used for freshly created Vercel sandboxes."""


# [해설] 코드가 실제 사용하는 Vercel SDK 샌드박스 표면(sandbox_id/status/wait_for_status/stop)만 정의한 Protocol.
class _VercelSandboxHandle(Protocol):
    """Minimal Vercel SDK sandbox surface used by the built-in provider."""

    sandbox_id: str
    status: str

    def wait_for_status(self, status: str, *, timeout: int) -> None:
        """Wait for the sandbox to reach the requested status."""

    def stop(self) -> None:
        """Stop the sandbox."""


# [해설] Vercel Sandbox 프로바이더. VERCEL_TOKEN/PROJECT_ID/TEAM_ID는 "전부 워크스페이스" 또는 "전부 서버(SDK 위임)"만 허용.
class _VercelProvider(SandboxProvider):
    """Vercel Sandbox provider implementation."""

    # [해설] SDK 인자명 → env 이름 표. 조회·서버 기준선·오류 메시지가 모두 이 표 하나에서 파생된다.
    _CREDENTIAL_ENV_NAMES: ClassVar[dict[str, str]] = {
        "token": "VERCEL_TOKEN",
        "project_id": "VERCEL_PROJECT_ID",
        "team_id": "VERCEL_TEAM_ID",
    }
    """SDK credential argument mapped to the env var that supplies it.

    One table drives the lookup, the server-environment baseline, and the
    incomplete-set error, so a renamed argument cannot desynchronize the error
    message from what was actually read.
    """

    # [해설] 생성 시점에 SDK 자격 kwargs를 확정(이후 get_or_create/delete가 재사용).
    def __init__(self) -> None:
        """Initialize the provider, resolving workspace Vercel credentials."""
        self._sdk_kwargs = self._resolve_sdk_kwargs()

    # [해설] 워크스페이스 Vercel 자격을 해석해 SDK에 넘길 kwargs를 결정한다. 빈 dict면 SDK 기본 인증에 위임.
    @classmethod
    def _resolve_sdk_kwargs(cls) -> dict[str, str]:
        """Resolve explicit Vercel credentials configured for this workspace.

        Each value resolves prefixed-first then canonical via `resolve_env_var`,
        against the bound workspace environment. Credentials stay SDK-managed
        when every resolved value matches the server environment the SDK reads
        for itself. Explicit credentials must come entirely from the workspace
        or entirely from the server, never a mixture of the two.

        Returns:
            Explicit SDK credential arguments, or an empty mapping to delegate
            credential resolution to the Vercel SDK.

        Raises:
            ValueError: If the workspace overrides part of the `VERCEL_TOKEN` /
                `VERCEL_PROJECT_ID` / `VERCEL_TEAM_ID` set but not all of it.
                Delegating there would use the server's identity instead of
                the credentials the workspace pinned.
        """
        from deepagents_code.model_config import resolve_env_var

        # [해설][흐름] 1) 세 값을 resolve_env_var로 해석(접두 우선, 워크스페이스 env 기준).
        values = {
            key: resolve_env_var(name)
            for key, name in cls._CREDENTIAL_ENV_NAMES.items()
        }
        # Nothing here came from the workspace: `resolve_env_var` reads
        # `active_environment()`, which falls back to `os.environ`, so the
        # server's own `VERCEL_*` resolve identically. There is no workspace
        # identity to protect and the SDK resolves exactly these values, so
        # delegate instead of demanding the full set. That covers inherited
        # token-free OIDC, and a personal-scope token, where `VERCEL_TEAM_ID`
        # is optional and demanding it turned a working setup into a startup
        # failure. A workspace override differs from the server, so it still
        # takes the fail-closed path below.
        # [해설][흐름] 2) 모든 값이 서버 os.environ의 원래 이름 값과 같으면 워크스페이스 고유 자격이 없는 것 → {} 반환(SDK 위임).
        if all(
            value == (os.environ.get(cls._CREDENTIAL_ENV_NAMES[key]) or None)
            for key, value in values.items()
        ):
            return {}
        # [해설][흐름] 3) 워크스페이스 override가 있는데 일부가 비어 있으면 fail-closed.
        missing = sorted(
            name for key, name in cls._CREDENTIAL_ENV_NAMES.items() if not values[key]
        )
        if missing:
            # Fail closed rather than warn and delegate: an empty mapping hands
            # auth back to the Vercel SDK, which resolves credentials from the
            # server process (`VERCEL_*` in its own environment, or its OIDC
            # identity). A workspace that pinned a restricted token would then
            # silently run its sandbox under the server's broader identity.
            names = list(cls._CREDENTIAL_ENV_NAMES.values())
            required = f"{', '.join(names[:-1])}, and {names[-1]}"
            msg = (
                "The workspace Vercel configuration is incomplete: "
                f"{', '.join(missing)} not set. Set {required} together, or "
                "unset all three to fall back to default Vercel authentication."
            )
            raise ValueError(msg)
        # [해설][흐름] 4) 값은 다 있지만 일부가 서버에서 상속된 혼합 구성인지 검사.
        cls._validate_credential_sources(values)
        # `missing` is empty, so every value is a non-empty string here; the
        # comprehension re-states that for the type checker.
        return {key: value for key, value in values.items() if value}

    # [해설] 실제 선택된 env 이름(resolved_env_var_name: 접두/원래 이름)으로 서버 값과 비교해 상속 필드를 찾는다.
    # [해설] 일부만 상속이면 ValueError. 접두 워크스페이스 값이 서버의 원래 이름 값과 우연히 같아도 이름이 달라 구분된다.
    @classmethod
    def _validate_credential_sources(cls, values: Mapping[str, str | None]) -> None:
        """Reject a workspace credential set completed by inherited fields.

        Compare the selected variable names as well as their values: an
        explicit prefixed workspace ID may equal the server's canonical ID.

        Raises:
            ValueError: If only part of the credential set is inherited.
        """
        from deepagents_code.model_config import resolved_env_var_name

        inherited = [
            name
            for key, name in cls._CREDENTIAL_ENV_NAMES.items()
            if values[key] == os.environ.get(resolved_env_var_name(name))
        ]
        if inherited and len(inherited) != len(values):
            msg = (
                "The Vercel configuration mixes workspace and server credentials: "
                f"{', '.join(inherited)} inherited from the server. "
                "Set VERCEL_TOKEN, VERCEL_PROJECT_ID, and VERCEL_TEAM_ID together "
                "using workspace overrides, or remove the workspace overrides "
                "to use default Vercel authentication."
            )
            raise ValueError(msg)

    # [해설] Vercel 샌드박스 연결(Sandbox.get) 또는 생성(Sandbox.create) 후 running까지 대기.
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        timeout: int = 180,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get or create a Vercel sandbox.

        Args:
            sandbox_id: Existing sandbox ID, or None to create.
            timeout: Seconds to wait for startup.
            **kwargs: Rejected; passing any keyword argument raises `TypeError`.

        Returns:
            `VercelSandbox` instance.

        Raises:
            RuntimeError: If the sandbox fails to start.
            TypeError: If unsupported keyword arguments are provided.
        """
        # [해설] kwargs 전면 거부 → config `params`를 Vercel에는 전달할 수 없다.
        if kwargs:
            msg = f"Received unsupported arguments: {list(kwargs.keys())}"
            raise TypeError(msg)

        vercel_backend = _import_provider_module(
            "langchain_vercel_sandbox",
            provider="vercel",
            package="langchain-vercel-sandbox",
        )
        vercel_sandbox = _import_provider_module(
            "vercel.sandbox",
            provider="vercel",
            package="vercel",
        )

        # [해설][흐름] 1) SDK 호출. 예외 메시지를 일반화해 자격 정보 유출을 막고, `from exc`로 원인은 트레이스백에 보존.
        # The SDK is imported dynamically, so `Sandbox.get`/`create` are typed
        # `Any`. Annotating pins the result to the Protocol so the type checker
        # verifies the `.status`/`.wait_for_status`/`.stop` calls below.
        sandbox: _VercelSandboxHandle
        try:
            if sandbox_id:
                sandbox = vercel_sandbox.Sandbox.get(
                    sandbox_id=sandbox_id,
                    **self._sdk_kwargs,
                )
            else:
                sandbox = vercel_sandbox.Sandbox.create(
                    runtime=_VERCEL_DEFAULT_RUNTIME,
                    timeout=_VERCEL_SANDBOX_TIMEOUT,
                    **self._sdk_kwargs,
                )
        except Exception as exc:  # Vercel SDK exception types vary by version
            # Keep the message generic so SDK errors cannot leak credentials,
            # but chain the original exception so tracebacks retain root cause.
            action = "connect to existing" if sandbox_id else "create"
            msg = f"Failed to {action} Vercel sandbox."
            raise RuntimeError(msg) from exc

        # [해설][흐름] 2) running 대기. 실패 시 "새로 만든" 샌드박스만 stop(기존 샌드박스는 건드리지 않음).
        try:
            self._wait_until_running(sandbox, timeout=timeout)
        except Exception:
            if sandbox_id is None:
                with contextlib.suppress(Exception):
                    sandbox.stop()
            raise

        return vercel_backend.VercelSandbox(sandbox=sandbox)

    # [해설] ID로 조회 후 stop. 실패는 일반화된 RuntimeError.
    def delete(self, *, sandbox_id: str, **kwargs: Any) -> None:  # noqa: ARG002  # **kwargs required by SandboxProvider interface
        """Stop a Vercel sandbox by id.

        Raises:
            RuntimeError: If the Vercel SDK cannot find or stop the sandbox.
        """
        vercel_sandbox = _import_provider_module(
            "vercel.sandbox",
            provider="vercel",
            package="vercel",
        )
        try:
            sandbox = vercel_sandbox.Sandbox.get(
                sandbox_id=sandbox_id,
                **self._sdk_kwargs,
            )
            sandbox.stop()
        except Exception as exc:  # Vercel SDK exception types vary by version
            # Generic message avoids leaking credentials; chain preserves cause.
            msg = "Failed to stop Vercel sandbox."
            raise RuntimeError(msg) from exc

    # [해설] 이미 running이면 즉시 반환, 종료 상태면 즉시 실패, 그 외에는 SDK wait_for_status로 timeout까지 대기.
    @staticmethod
    def _wait_until_running(
        sandbox: _VercelSandboxHandle,
        *,
        timeout: int,
    ) -> None:
        """Wait for a Vercel sandbox to become ready.

        Raises:
            RuntimeError: If the sandbox reaches a terminal state or does not
                become ready before the timeout.
        """
        status = str(sandbox.status)
        if status == "running":
            return
        if status in _VERCEL_TERMINAL_STATUSES:
            msg = f"Vercel sandbox {sandbox.sandbox_id} is in terminal state {status!r}"
            raise RuntimeError(msg)

        try:
            sandbox.wait_for_status("running", timeout=timeout)
        except TimeoutError as exc:
            status = str(sandbox.status)
            msg = (
                f"Vercel sandbox {sandbox.sandbox_id} failed to start within "
                f"{timeout} seconds; current status is {status!r}"
            )
            raise RuntimeError(msg) from exc
        except Exception as exc:  # Vercel SDK exception types vary by version
            # Generic message avoids leaking credentials; chain preserves cause.
            msg = "Failed while waiting for Vercel sandbox startup."
            raise RuntimeError(msg) from exc


# [해설] 레지스트리(주어지면 재사용)로 프로바이더 인스턴스를 만든다. 호출자: create_sandbox.
def _get_provider(
    provider_name: str,
    registry: SandboxRegistry | None = None,
) -> SandboxProvider:
    """Get a `SandboxProvider` instance for the specified provider (internal).

    Args:
        provider_name: Name of the provider. Resolved through the registry so
            built-in, entry-point, and config providers are all supported.
        registry: An already-built registry to reuse. A fresh one is loaded
            when omitted.

    Returns:
        `SandboxProvider` instance. Propagates `ValueError` from the registry
            if `provider_name` is unknown.
    """
    reg = registry if registry is not None else _get_registry()
    return reg.create_provider(provider_name)


# [해설] 클라이언트 프로세스에서 서버 스폰 전에 프로바이더 백엔드 패키지 설치 여부를 find_spec으로만 검사한다.
# [해설] 호출자: main.py(`--sandbox` 지정 시). 누락이면 설치 명령이 담긴 ImportError → main.py가 종료 처리.
def verify_sandbox_deps(provider: str) -> None:
    """Check that the required packages for a sandbox provider are installed.

    Uses `importlib.util.find_spec` for a lightweight check with no actual
    imports. Call this in the app's process *before* spawning the server
    subprocess so users get a clear, actionable error instead of an opaque
    server crash. The backend module to probe and the install hint both come
    from provider metadata.

    Args:
        provider: Sandbox provider name (e.g. `'daytona'`).

    Raises:
        ImportError: If the provider's backend package is not installed.
    """
    # [해설] 샌드박스 미사용("none" 또는 빈 값)이면 검사 없음.
    if not provider or provider == "none":
        return

    metadata = _get_registry().get_metadata(provider)
    # [해설] probe할 모듈이 없는 프로바이더(langsmith 번들, 순수 config 프로바이더, 알 수 없는 이름)는 건너뛴다.
    if metadata is None or metadata.backend_module is None:
        logger.debug(
            "No backend module to probe for provider %r; skipping pre-flight check",
            provider,
        )
        return

    # [해설] find_spec은 실제 import 없이 모듈 존재만 확인(가벼움). 잘못된 이름 등의 예외는 "없음"으로 취급.
    try:
        found = importlib.util.find_spec(metadata.backend_module) is not None
    except (ImportError, ValueError):
        found = False

    if not found:
        if metadata.install is not None:
            install_hint = (
                f"Install with: {metadata.install.command(in_app=True)} (in-app) "
                f"or {metadata.install.command(in_app=False)} (CLI)"
            )
        else:
            install_hint = "Install the provider's package."
        msg = f"Missing dependencies for '{provider}' sandbox. {install_hint}"
        raise ImportError(msg)


# [해설] 공개 API는 3개. 프로바이더 구현 클래스는 private(_ 접두)이며 sandbox_registry._create_builtin_provider만 참조한다.
__all__ = [
    "create_sandbox",
    "get_default_working_dir",
    "verify_sandbox_deps",
]
