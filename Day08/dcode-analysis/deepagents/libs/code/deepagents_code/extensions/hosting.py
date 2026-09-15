"""Host startup and runtime extension registrations."""

# [해설] 이 모듈의 역할: Python extension이 등록한 도구·미들웨어·백엔드 라우트를 "호스트(dcode) 정책"에 맞게
# [해설] 에이전트 그래프에 연결한다. (1) 그래프 빌드 후 등록된 도구를 모델/도구 호출 시점에 동적으로 주입하고,
# [해설] (2) 백엔드 라우트가 내부 경로를 가리거나 샌드박스 경계를 깨는지 검증한다.
# [해설] 실행 프로세스: 에이전트 그래프가 조립되는 서버 프로세스(agent.py). `--acp`에서는 같은 프로세스 안.
# [해설] 주요 진입점 심볼: ExtensionRuntimeMiddleware, validate_backend_route, bind_runtime_host_policy.
# [해설] 호출자: agent.py(route 검증·정책 바인딩·미들웨어 추가). 레지스트리 자체는 extensions/registry.py.
# [해설] 관련 분석 문서: analysis/07-mcp-hooks-extensions-plugins.md, analysis/08-sandboxes-execution.md
# [해설] 관련 공식 문서: docs_official/code/extensions.md
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware.types import AgentMiddleware

from deepagents_code.extensions.registry import ExtensionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence

    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
        ToolCallRequest,
    )
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import BaseTool
    from langgraph.types import Command

    from deepagents_code.extensions.registry import ExtensionRegistry, RegisteredUnit


# [해설] 도구 이름 추출 헬퍼. OpenAI 스타일 dict 스키마({"function": {"name": ...}})와 BaseTool 객체(.name) 둘 다 지원.
def _tool_name(tool: object) -> str | None:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


# [해설] 그래프가 이미 컴파일된 뒤에 extension이 등록한 도구를 노출하기 위한 미들웨어.
# [해설] agent.py가 extension 레지스트리가 있을 때 agent_middleware 목록에 추가한다.
# [해설][설계] 도구 목록은 매 모델 호출마다 레지스트리의 최신 스냅샷으로 재구성되므로, 도구 추가는 재시작 없이 반영된다.
class ExtensionRuntimeMiddleware(AgentMiddleware):
    """Expose tools registered after the agent graph was built."""

    # [해설] 미들웨어 이름. 이중 밑줄로 내부 전용 미들웨어임을 드러낸다.
    name = "__deepagents_extension_runtime__"

    # [해설] 레지스트리 참조만 보관한다. 실제 도구 조회는 호출 시점마다 수행.
    def __init__(self, registry: ExtensionRegistry) -> None:
        """Bind dynamic model and tool dispatch to `registry`."""
        self._registry = registry

    # [해설] 기존 도구 목록에 레지스트리 도구를 병합한다. 같은 이름이면 자리를 유지한 채 교체, 없으면 뒤에 추가.
    # [해설][주의] 이름이 같으면 extension 도구가 기본 도구를 덮어쓴다(교체 우선 정책).
    def _tools(
        self, existing: Sequence[BaseTool | dict[str, Any]]
    ) -> list[BaseTool | dict[str, Any]]:
        tools = list(existing)
        indexes = {_tool_name(tool): index for index, tool in enumerate(tools)}
        for registered in self._registry.tool_units():
            index = indexes.get(registered.name)
            if index is None:
                indexes[registered.name] = len(tools)
                tools.append(registered.unit)
            else:
                tools[index] = registered.unit
        return tools

    # [해설] 동기 모델 호출 직전에 request.tools를 병합 결과로 override → 모델이 새 도구의 스키마를 보게 된다.
    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Inject the latest extension-tool snapshot into a sync model call.

        Returns:
            The wrapped model response.
        """
        return handler(request.override(tools=self._tools(request.tools)))

    # [해설] wrap_model_call의 비동기 버전.
    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Inject the latest extension-tool snapshot into an async model call.

        Returns:
            The wrapped model response.
        """
        return await handler(request.override(tools=self._tools(request.tools)))

    # [해설] 모델이 호출한 도구 이름이 레지스트리에 있으면 request.tool을 등록된 실제 객체로 교체한다.
    # [해설][설계] 그래프 ToolNode에는 없는(나중에 등록된) 도구도 LangChain의 정상 handler 경로로 실행되게 하는 장치.
    def _tool_request(self, request: ToolCallRequest) -> ToolCallRequest:
        registered = self._registry.find_tool(request.tool_call["name"])
        return request if registered is None else request.override(tool=registered.unit)

    # [해설] 동기 도구 호출 래퍼. 교체된 request로 handler를 그대로 실행(승인·HITL 등 다른 미들웨어 경로 유지).
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Execute a sync runtime tool through LangChain's normal handler.

        Returns:
            The wrapped tool result.
        """
        return handler(self._tool_request(request))

    # [해설] wrap_tool_call의 비동기 버전.
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Execute an async runtime tool through LangChain's normal handler.

        Returns:
            The wrapped tool result.
        """
        return await handler(self._tool_request(request))


# [해설] extension 백엔드 라우트 하나를 검증한다. 호출자: agent.py(그래프 조립 시 초기 라우트)와
# [해설] bind_runtime_host_policy의 apply(런타임 등록 시). 위반 시 ExtensionError.
def validate_backend_route(
    item: RegisteredUnit[Any],
    protected_routes: Collection[str],
    *,
    sandbox_active: bool,
) -> None:
    """Reject a backend route that violates host storage boundaries.

    The sandbox check is deliberately shallow. Wrapping a filesystem backend
    in another `BackendProtocol` implementation changes who owns that safety
    contract, so dcode does not inspect arbitrary backend object graphs.

    Args:
        item: Backend route registration to validate.
        protected_routes: Internal route prefixes unavailable to extensions.
        sandbox_active: Whether the default execution backend is sandboxed.

    Raises:
        ExtensionError: If the route overlaps internal storage or directly
            exposes a host filesystem backend to a sandboxed agent.
    """
    # [해설][흐름] 1) 내부 라우트(protected_routes)와 접두 관계가 "양방향"으로 겹치면 거부.
    # [해설] 예: 내부 `/memories/`가 있을 때 `/`나 `/memories/x/` 모두 거부 → 내부 저장소를 가리거나 파고드는 것 차단.
    if any(
        item.name.startswith(prefix) or prefix.startswith(item.name)
        for prefix in protected_routes
    ):
        msg = (
            f"Extension backend route {item.name!r} from {item.source.label} "
            "overlaps an internal route"
        )
        raise ExtensionError(msg)
    # [해설][흐름] 2) 샌드박스 모드에서 호스트 FilesystemBackend(하위 클래스 LocalShellBackend 포함) 마운트 거부.
    # [해설][주의] 검사는 얕은 isinstance라서 다른 BackendProtocol 구현으로 감싼 래퍼는 통과한다(docstring이 의도라고 명시).
    # [해설][SDK] FilesystemBackend는 SDK `deepagents/backends/filesystem.py`.
    if sandbox_active and isinstance(item.unit, FilesystemBackend):
        msg = (
            f"Extension backend route {item.name!r} from {item.source.label} "
            f"cannot mount {type(item.unit).__name__} in sandbox mode"
        )
        raise ExtensionError(msg)


# [해설] 그래프 빌드 이후(런타임) 들어오는 등록에 호스트 정책을 구독 방식으로 건다. 호출자: agent.py.
def bind_runtime_host_policy(
    registry: ExtensionRegistry,
    protected_routes: Collection[str],
    *,
    sandbox_active: bool = False,
) -> None:
    """Validate late routes and flag graph-bound registrations for restart."""

    # [해설] 등록 이벤트 콜백. middleware와 backend_route는 컴파일된 그래프에 묶이므로 registry.require_restart()로
    # [해설] "재시작 필요" 플래그만 세운다. backend_route는 먼저 검증해 위반이면 ExtensionError를 던진다.
    # [해설] tool 등록은 여기서 처리하지 않는다 → ExtensionRuntimeMiddleware가 다음 호출부터 자동 반영.
    def apply(kind: str, item: RegisteredUnit[Any]) -> None:
        if kind == "middleware":
            registry.require_restart()
            return
        if kind == "backend_route":
            validate_backend_route(
                item, protected_routes, sandbox_active=sandbox_active
            )
            registry.require_restart()

    # [해설] 레지스트리에 콜백 등록. 이후 모든 register_* 호출이 apply를 거친다.
    registry.subscribe_to_registrations(apply)
