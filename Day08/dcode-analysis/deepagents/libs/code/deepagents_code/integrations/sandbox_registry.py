"""Discovery and instantiation of sandbox providers.

Merges three provider sources into one registry:

1. Built-in providers curated in this repo (installed as `deepagents-code`
   extras).
2. Entry-point providers published by third-party packages under the
   `deepagents_code.sandbox_providers` group.
3. Config-declared providers from `[sandboxes.providers]` in
   `~/.deepagents/config.toml` (escape hatch for internal/local packages).

Precedence on name collision: config > entry point > built-in, so a user can
always override discovery via their config file.
"""

# [해설] 이 모듈의 역할: 샌드박스 프로바이더 이름을 메타데이터/인스턴스로 해석하는 레지스트리.
# [해설] 세 소스(built-in 표 BUILTIN_METADATA, entry point 그룹, config.toml `[sandboxes.providers]`)를 합치고
# [해설] 이름 충돌 시 config > entry point > built-in 순으로 이긴다(docstring과 create_provider 구현이 일치).
# [해설] 실행 프로세스: 둘 다. 클라이언트(main.py에서 SandboxRegistry.load로 목록/검증, sandbox_factory.verify_sandbox_deps),
# [해설] 서버(sandbox_factory.create_sandbox → create_provider).
# [해설] 주요 진입점 심볼: SandboxRegistry(load/get_metadata/create_provider/provider_metadata), BUILTIN_METADATA.
# [해설] config 파싱은 integrations/sandbox_config.SandboxConfig가 담당한다.
# [해설] 관련 분석 문서: analysis/08-sandboxes-execution.md / 공식 문서: docs_official/code/remote-sandboxes.md
from __future__ import annotations

import importlib
import importlib.metadata
import logging
from typing import TYPE_CHECKING

from deepagents_code.integrations.sandbox_config import SandboxConfig
from deepagents_code.integrations.sandbox_provider import (
    SandboxInstallHint,
    SandboxProvider,
    SandboxProviderMetadata,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# [해설] 서드파티 패키지가 pyproject의 entry-points에 선언하는 그룹 이름. _discover_entry_points가 조회.
ENTRY_POINT_GROUP = "deepagents_code.sandbox_providers"
"""Entry-point group third-party packages publish providers under."""

# [해설] built-in 6종의 정적 메타데이터 표. 인스턴스화 없이 working_dir·설치 힌트·probe 모듈을 제공한다.
# [해설] 사용처: get_metadata(설정 override의 기본값), available_providers, _create_builtin_provider 분기.
# [해설] working_dir는 각 SDK 샌드박스 이미지의 홈/작업 경로와 맞춘 값이다(예: modal은 sandbox_factory의 workdir="/workspace").
BUILTIN_METADATA: dict[str, SandboxProviderMetadata] = {
    # [해설] agentcore: 세션 재연결 불가 → supports_sandbox_id=False(_AgentCoreProvider.get_or_create도 NotImplementedError).
    "agentcore": SandboxProviderMetadata(
        name="agentcore",
        working_dir="/tmp",  # noqa: S108  # AgentCore Code Interpreter working directory
        install=SandboxInstallHint(kind="extra", name="agentcore"),
        supports_sandbox_id=False,
        backend_module="langchain_agentcore_codeinterpreter",
    ),
    # [해설][주의] daytona는 supports_sandbox_id를 지정하지 않아 기본값 True지만, _DaytonaProvider.get_or_create는
    # [해설] sandbox_id를 주면 NotImplementedError를 던진다 → 메타데이터와 실제 동작이 어긋난다.
    "daytona": SandboxProviderMetadata(
        name="daytona",
        working_dir="/home/daytona",
        install=SandboxInstallHint(kind="extra", name="daytona"),
        backend_module="langchain_daytona",
    ),
    # [해설] langsmith: backend_module 없음 → verify_sandbox_deps의 사전 probe를 건너뛴다(langsmith[sandbox]로 기본 번들).
    # [해설] snapshot_name 지원 → snapshot_name 입력이 _LangSmithProvider의 snapshot kwarg로 전달된다.
    "langsmith": SandboxProviderMetadata(
        name="langsmith",
        working_dir="/root",  # `$HOME` in the LangSmith sandbox
        # Bundled with `deepagents-code` via `langsmith[sandbox]`; no extra.
        supports_snapshot_name=True,
    ),
    "modal": SandboxProviderMetadata(
        name="modal",
        working_dir="/workspace",
        install=SandboxInstallHint(kind="extra", name="modal"),
        backend_module="langchain_modal",
    ),
    # [해설] runloop: snapshot_name을 blueprint 이름으로 사용(supports_snapshot_name=True).
    "runloop": SandboxProviderMetadata(
        name="runloop",
        working_dir="/home/user",
        install=SandboxInstallHint(kind="extra", name="runloop"),
        supports_snapshot_name=True,
        backend_module="langchain_runloop",
    ),
    "vercel": SandboxProviderMetadata(
        name="vercel",
        working_dir="/vercel/sandbox",
        install=SandboxInstallHint(kind="extra", name="vercel"),
        supports_sandbox_id=True,
        supports_snapshot_name=False,
        backend_module="langchain_vercel_sandbox",
    ),
}
"""Metadata for curated built-in providers, keyed by provider name."""


# [해설] config의 `class_path = "module.path:ClassName"`을 import해 클래스 객체를 돌려준다.
# [해설][주의] 사용자 config에 적힌 임의 모듈을 import·실행하는 지점이다(config 파일이 신뢰 경계).
def _load_class(class_path: str) -> type:
    """Import a `module.path:ClassName` provider class.

    Args:
        class_path: Fully-qualified class path.

    Returns:
        The imported class object.

    Raises:
        ValueError: If `class_path` is malformed.
        ImportError: If the module cannot be imported or lacks the class.
    """
    if ":" not in class_path:
        msg = (
            f"Invalid class_path '{class_path}': must be in "
            "module.path:ClassName format"
        )
        raise ValueError(msg)
    module_path, class_name = class_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name, None)
    if cls is None or not isinstance(cls, type):
        msg = f"Class '{class_name}' not found in module '{module_path}'"
        raise ImportError(msg)
    return cls


# [해설] 인스턴스에서 metadata 속성을 읽고, 없거나 타입이 다르면 working_dir="/workspace" 기본 메타데이터를 합성한다.
def _provider_metadata(provider: SandboxProvider, name: str) -> SandboxProviderMetadata:
    """Extract metadata from a provider instance or class.

    Providers may expose a `metadata` attribute/property; otherwise a minimal
    default is synthesized.

    Args:
        provider: Provider instance.
        name: Provider name to use when synthesizing defaults.

    Returns:
        The provider's metadata.
    """
    meta = getattr(provider, "metadata", None)
    if isinstance(meta, SandboxProviderMetadata):
        return meta
    return SandboxProviderMetadata(name=name, working_dir="/workspace")


# [해설] 세 소스를 합친 조회 뷰. 캐시되지 않으므로 sandbox_factory._get_registry는 호출마다 새로 만든다.
class SandboxRegistry:
    """Merged view of built-in, entry-point, and config sandbox providers."""

    # [해설] config를 주입받거나(테스트) 기본 경로에서 로드. entry point 목록은 생성 시 1회 조회(EntryPoint.load는 아직 안 함).
    def __init__(
        self,
        *,
        config: SandboxConfig | None = None,
        include_entry_points: bool = True,
    ) -> None:
        """Build the registry.

        Args:
            config: Parsed sandbox config. Loaded from the default path when
                omitted.
            include_entry_points: Whether to discover entry-point providers.
                Disabled in tests that need a deterministic provider set.
        """
        self._config = config if config is not None else SandboxConfig.load()
        self._include_entry_points = include_entry_points
        self._entry_points: dict[str, importlib.metadata.EntryPoint] = (
            self._discover_entry_points() if include_entry_points else {}
        )

    # [해설] config 경로를 지정해 레지스트리를 만드는 팩토리. 호출자: main.py, sandbox_factory._get_registry.
    @classmethod
    def load(cls, config_path: Path | None = None) -> SandboxRegistry:
        """Build a registry from the config file at `config_path`.

        Args:
            config_path: Path to config file. Defaults to the user config.

        Returns:
            A new `SandboxRegistry`.
        """
        return cls(config=SandboxConfig.load(config_path))

    # [해설] importlib.metadata로 그룹의 entry point를 이름별 dict로 수집. 실패해도 경고 후 빈 dict(발견은 best-effort).
    # [해설] 같은 이름이 여러 패키지에 있으면 반복 순서상 마지막 항목이 남는다.
    @staticmethod
    def _discover_entry_points() -> dict[str, importlib.metadata.EntryPoint]:
        """Return entry-point providers keyed by name (best-effort)."""
        found: dict[str, importlib.metadata.EntryPoint] = {}
        try:
            entries = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception:
            logger.warning("Failed to discover sandbox entry points", exc_info=True)
            return found
        for entry in entries:
            found[entry.name] = entry
        return found

    # [해설] config의 기본 프로바이더(`[sandboxes] default` 추정). CLI가 `--sandbox` 미지정 시 참고(추정).
    @property
    def default(self) -> str | None:
        """The configured default provider, if any."""
        return self._config.default

    # [해설] config 파일이 있었지만 파싱에 실패한 경우의 사유. UI에서 경고 표시용.
    @property
    def config_error(self) -> str | None:
        """Why the config file failed to load, if it existed but couldn't parse.

        `None` when the config parsed cleanly or simply wasn't present.
        """
        return self._config.parse_error

    # [해설] 세 소스의 이름 합집합(정렬). 오류 메시지·선택 UI에 사용.
    def available_providers(self) -> list[str]:
        """Return all known provider names, sorted."""
        names = (
            set(BUILTIN_METADATA)
            | set(self._entry_points)
            | set(self._config.providers)
        )
        return sorted(names)

    # [해설] 이름이 어느 소스에든 있으면 True. 인스턴스화나 import는 하지 않는다.
    def is_available(self, name: str) -> bool:
        """Return whether `name` resolves to a known provider."""
        return (
            name in self._config.providers
            or name in self._entry_points
            or name in BUILTIN_METADATA
        )

    # [해설] 정적 메타데이터 조회. config 항목 → entry point(provider_metadata로 인스턴스화 probe) → built-in 순.
    def get_metadata(self, name: str) -> SandboxProviderMetadata | None:
        """Return metadata for `name`.

        Config providers may override `working_dir`, `package`, and capability
        flags. Entry-point providers are probed for advertised metadata and
        take precedence over built-ins with the same name.

        Args:
            name: Provider name.

        Returns:
            The provider's metadata, or `None` if unknown.
        """
        # [해설][흐름] 1) config 항목: 같은 이름 built-in이 있으면 그 값을 기본으로, config 키(working_dir, package,
        # [해설] supports_sandbox_id, supports_snapshot_name)로 덮어쓴다. package가 있으면 kind="package" 설치 힌트.
        config_entry = self._config.providers.get(name)
        if config_entry is not None:
            base = BUILTIN_METADATA.get(name)
            package = config_entry.get("package")
            return SandboxProviderMetadata(
                name=name,
                working_dir=config_entry.get(
                    "working_dir", base.working_dir if base else "/workspace"
                ),
                install=(
                    SandboxInstallHint(kind="package", name=package)
                    if package
                    else (base.install if base else None)
                ),
                supports_sandbox_id=config_entry.get(
                    "supports_sandbox_id",
                    base.supports_sandbox_id if base else True,
                ),
                supports_snapshot_name=config_entry.get(
                    "supports_snapshot_name",
                    base.supports_snapshot_name if base else False,
                ),
                # Carry the built-in's probe module so a config override of a
                # built-in keeps its dependency pre-flight check. A pure config
                # provider (no base) leaves this `None`; its package is resolved
                # when the provider class is imported.
                backend_module=base.backend_module if base else None,
            )
        # [해설][흐름] 2) entry point: 정적 정보가 없으므로 provider_metadata가 인스턴스를 만들어 metadata를 읽는다.
        # [해설][주의] 이 경로는 자격증명이 필요한 생성자를 실행할 수 있다(실패 시 placeholder로 폴백).
        if name in self._entry_points:
            return self.provider_metadata(name)
        # [해설][흐름] 3) built-in 정적 표.
        if name in BUILTIN_METADATA:
            return BUILTIN_METADATA[name]
        return None

    # [해설] config `params` 테이블을 반환 → create_sandbox가 get_or_create kwargs의 기본값으로 사용.
    def get_params(self, name: str) -> dict[str, object]:
        """Return config `params` forwarded to the provider's `get_or_create()`."""
        return self._config.get_params(name)

    # [해설] 이름으로 프로바이더 인스턴스를 만든다. 호출자: sandbox_factory._get_provider(create_sandbox 경로).
    # [해설] 생성자에서 자격증명 검증이 일어나므로 ValueError/ImportError가 여기서 올라올 수 있다.
    def create_provider(self, name: str) -> SandboxProvider:
        """Instantiate the provider named `name`.

        Resolution order: config `class_path` > entry point > built-in.

        Args:
            name: Provider name.

        Returns:
            A `SandboxProvider` instance. Propagates `ImportError` from
                `_load_class` / `EntryPoint.load` if a config or entry-point
                class cannot be imported.

        Raises:
            ValueError: If `name` is unknown or a config provider omits
                `class_path`.
        """
        # [해설][흐름] 1) config 우선: class_path가 필수이고, 인자 없는 생성자로 인스턴스화한다(params는 get_or_create로만 전달).
        config_entry = self._config.providers.get(name)
        if config_entry is not None:
            class_path = config_entry.get("class_path")
            if not class_path:
                msg = f"Sandbox provider '{name}' config is missing 'class_path'"
                raise ValueError(msg)
            return _load_class(class_path)()

        # [해설][흐름] 2) entry point: 클래스(또는 callable)를 load한 뒤 인자 없이 호출.
        entry = self._entry_points.get(name)
        if entry is not None:
            return entry.load()()

        # [해설][흐름] 3) built-in: sandbox_factory의 private 구현 클래스로 지연 생성.
        if name in BUILTIN_METADATA:
            return _create_builtin_provider(name)

        msg = (
            f"Unknown sandbox provider: {name}. "
            f"Available providers: {', '.join(self.available_providers())}"
        )
        raise ValueError(msg)

    # [해설] "권위 있는" 메타데이터. get_metadata와 달리 entry point를 실제 인스턴스화해 capability 플래그를 반영한다.
    def provider_metadata(self, name: str) -> SandboxProviderMetadata:
        """Return authoritative metadata for `name`.

        Config providers are described statically. Entry-point providers are
        instantiated so capability flags they expose via a `metadata` attribute
        take effect; on failure this falls back to the static placeholder.
        Built-in metadata is used only when no entry point overrides that name.

        Args:
            name: Provider name.

        Returns:
            The provider's metadata.

        Raises:
            ValueError: If `name` is unknown.
        """
        # [해설][흐름] 1) config 항목이거나, entry point가 덮어쓰지 않은 built-in이면 정적 경로로 충분.
        if name in self._config.providers or (
            name in BUILTIN_METADATA and name not in self._entry_points
        ):
            meta = self.get_metadata(name)
            if meta is not None:
                return meta
        # [해설][흐름] 2) entry point는 인스턴스화 probe. 생성 실패(자격 없음 등)는 발견을 깨지 않도록 placeholder 반환.
        if name in self._entry_points:
            try:
                provider = self.create_provider(name)
            except Exception:  # noqa: BLE001  # Metadata probe must not crash discovery
                logger.debug("Could not instantiate provider %r for metadata", name)
                return SandboxProviderMetadata(name=name, working_dir="/workspace")
            return _provider_metadata(provider, name)
        meta = self.get_metadata(name)
        if meta is None:
            msg = f"Unknown sandbox provider: {name}"
            raise ValueError(msg)
        return meta


# [해설] built-in 클래스 매핑. sandbox_factory가 이 모듈을 TYPE_CHECKING/지연 import로 참조하므로,
# [해설] 여기서도 함수 안에서 import해 순환 import를 피한다.
def _create_builtin_provider(name: str) -> SandboxProvider:
    """Instantiate a built-in provider class (lazy import avoids cycles).

    Returns:
        The built-in `SandboxProvider` instance for `name`.
    """
    from deepagents_code.integrations import sandbox_factory

    builders = {
        "agentcore": sandbox_factory._AgentCoreProvider,
        "daytona": sandbox_factory._DaytonaProvider,
        "langsmith": sandbox_factory._LangSmithProvider,
        "modal": sandbox_factory._ModalProvider,
        "runloop": sandbox_factory._RunloopProvider,
        "vercel": sandbox_factory._VercelProvider,
    }
    return builders[name]()
