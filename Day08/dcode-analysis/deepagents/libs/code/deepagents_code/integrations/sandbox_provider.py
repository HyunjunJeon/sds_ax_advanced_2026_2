"""Sandbox provider interface used by Deep Agents Code."""

# [해설] 이 모듈의 역할: 샌드박스 프로바이더의 "계약"(ABC)과 정적 메타데이터 타입을 정의한다.
# [해설] 실제 구현(LangSmith/Daytona/Modal/Runloop/AgentCore/Vercel)은 integrations/sandbox_factory.py에 있고,
# [해설] 이름→구현 매핑과 우선순위(config > entry point > built-in)는 integrations/sandbox_registry.py가 담당한다.
# [해설] 실행 프로세스: 둘 다. 메타데이터는 클라이언트(main.py의 verify_sandbox_deps 경로)에서도 읽고,
# [해설] get_or_create/delete는 서버 프로세스(server_graph.py → sandbox_factory.create_sandbox)에서 호출된다.
# [해설] 주요 진입점 심볼: SandboxProvider, SandboxProviderMetadata, SandboxInstallHint, SandboxNotFoundError.
# [해설] 서드파티 패키지는 SandboxProvider를 상속해 entry point `deepagents_code.sandbox_providers`로 배포한다.
# [해설] 관련 분석 문서: analysis/08-sandboxes-execution.md / 공식 문서: docs_official/code/remote-sandboxes.md
# [해설][SDK] 반환 타입 SandboxBackendProtocol은 SDK `deepagents/backends/protocol.py`의 프로토콜이다.
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from deepagents.backends.protocol import SandboxBackendProtocol


# [해설] 누락된 의존성 안내 메시지에 쓸 설치 명령 힌트. built-in은 extra(`/install daytona`),
# [해설] 서드파티는 임의 패키지(`--package`)로 구분한다. sandbox_factory.verify_sandbox_deps가 command()를 호출한다.
@dataclass(frozen=True)
class SandboxInstallHint:
    """How to install the package that provides a sandbox backend.

    Built-in providers ship as `deepagents-code` extras (`kind="extra"`);
    third-party providers install as arbitrary packages (`kind="package"`).
    The distinction lets error messages emit the correct install command
    (`/install daytona` vs. `/install acme-dcode-sandbox --package`).
    """

    kind: Literal["extra", "package"]
    name: str

    # [해설] in_app=True면 TUI 슬래시 명령(`/install`), False면 CLI(`dcode install`) 형식 문자열을 만든다.
    def command(self, *, in_app: bool) -> str:
        """Render the install command for this hint.

        Args:
            in_app: Whether to render the in-app slash command (`/install`)
                rather than the CLI command (`dcode install`).

        Returns:
            The install command string.
        """
        prefix = "/install" if in_app else "dcode install"
        suffix = " --package" if self.kind == "package" else ""
        return f"{prefix} {self.name}{suffix}"


# [해설] 프로바이더를 "인스턴스화하지 않고" 설명하기 위한 정적 정보.
# [해설] 인스턴스화는 자격증명·선택 의존성이 필요해 실패할 수 있으므로, CLI 검증·작업 디렉터리 조회는 이 값만 쓴다.
@dataclass(frozen=True)
class SandboxProviderMetadata:
    """Static description of a sandbox provider used by the registry.

    Lets the CLI and registry describe built-in and config providers without
    instantiating them (which may require credentials or optional
    dependencies). Entry-point providers expose their own instance via the
    `SandboxProvider.metadata` property, which the registry reads only when it
    already needs to construct the provider.
    """

    # [해설] name: 레지스트리 키. working_dir: 샌드박스 안 기본 작업 경로(sandbox_factory.get_default_working_dir → agent.py가 사용).
    # [해설] install: 설치 힌트. supports_sandbox_id: `--sandbox-id` 재연결 지원 여부.
    # [해설] supports_snapshot_name: create_sandbox의 snapshot_name 허용 여부(아니면 ValueError).
    # [해설] backend_module: verify_sandbox_deps가 find_spec으로 존재만 검사할 모듈 이름.
    name: str
    working_dir: str
    install: SandboxInstallHint | None = None
    supports_sandbox_id: bool = True
    supports_snapshot_name: bool = False
    backend_module: str | None = None
    """Importable backend module checked by the pre-flight dependency probe.

    `None` skips the probe (e.g. bundled providers, or third-party providers
    whose package is only resolved when the provider is constructed).
    """


# [해설] 프로바이더 작업 오류의 공통 기반 클래스. original_exc는 `raise ... from e`로 연결된 원인을 노출한다.
class SandboxError(Exception):
    """Base error for sandbox provider operations."""

    @property
    def original_exc(self) -> BaseException | None:
        """Original exception that caused this error, if any."""
        return self.__cause__


# [해설] 요청한 sandbox_id가 존재하지 않을 때. sandbox_factory._RunloopProvider.get_or_create가
# [해설] RunloopProvider의 KeyError를 이 예외로 변환한다.
class SandboxNotFoundError(SandboxError):
    """Raised when the requested sandbox cannot be found."""


# [해설] 모든 샌드박스 프로바이더가 구현하는 추상 인터페이스.
# [해설] 동기 get_or_create/delete가 필수이고, 비동기 버전은 스레드로 감싼 기본 구현을 제공한다.
class SandboxProvider(ABC):
    """Interface for creating and deleting sandbox backends."""

    # [해설] 선택적 메타데이터 훅. entry point 프로바이더가 override하면 SandboxRegistry.provider_metadata가
    # [해설] 인스턴스를 만든 뒤 이 값을 읽어 working_dir·capability 플래그를 반영한다. None이면 레지스트리가 기본값을 합성.
    @property
    def metadata(self) -> SandboxProviderMetadata | None:
        """Static metadata describing this provider.

        Third-party providers published under the
        `deepagents_code.sandbox_providers` entry-point group override this so
        the registry can surface their working directory and capability flags
        (snapshot/sandbox-id support) instead of falling back to a generic
        placeholder. Returns `None` by default; the registry then synthesizes a
        minimal default.
        """
        return None

    # [해설] sandbox_id가 있으면 기존 샌드박스에 연결, 없으면 새로 생성해 준비 완료까지 대기한 뒤 백엔드를 반환한다.
    # [해설] kwargs에는 config `[sandboxes.providers.<name>.params]`와 snapshot 등이 병합되어 들어온다(create_sandbox 참고).
    @abstractmethod
    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Get an existing sandbox, or create one if needed."""
        raise NotImplementedError

    # [해설] 샌드박스 삭제. create_sandbox의 finally 블록에서 "직접 생성한 경우에만" 호출된다.
    @abstractmethod
    def delete(
        self,
        *,
        sandbox_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a sandbox by id."""
        raise NotImplementedError

    # [해설][설계] 동기 SDK 호출(폴링 sleep 포함)이 이벤트 루프를 막지 않도록 asyncio.to_thread로 위임한다.
    async def aget_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        **kwargs: Any,
    ) -> SandboxBackendProtocol:
        """Async wrapper around get_or_create.

        Returns:
            The created or existing sandbox backend.
        """
        return await asyncio.to_thread(
            self.get_or_create, sandbox_id=sandbox_id, **kwargs
        )

    # [해설] delete의 비동기 래퍼(to_thread).
    async def adelete(
        self,
        *,
        sandbox_id: str,
        **kwargs: Any,
    ) -> None:
        """Async wrapper around delete."""
        await asyncio.to_thread(self.delete, sandbox_id=sandbox_id, **kwargs)
