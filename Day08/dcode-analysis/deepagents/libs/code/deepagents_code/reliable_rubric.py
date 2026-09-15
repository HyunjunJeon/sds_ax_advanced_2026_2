"""CLI-specific rubric middleware customizations."""
# [해설][설계] 모듈 개요: SDK `deepagents.middleware.rubric.RubricMiddleware`를 dcode(CLI)용으로 확장한 루브릭(수락 기준) 채점 미들웨어.
# [해설] 실행 위치: LangGraph 서버 프로세스(에이전트 그래프 내부). `--acp` 모드에서는 같은 프로세스 안에서 실행된다.
# [해설] 주요 진입점: `ReliableRubricMiddleware` — `agent.py`의 에이전트 조립 단계에서 `ReliableRubricMiddleware(**rubric_kwargs)`로 생성되어 미들웨어 스택에 추가된다.
# [해설] 핵심 확장 3가지: (1) thread 상태(`_rubric_model_spec`/`_model_spec`)로 요청마다 채점 모델을 고른다,
# [해설] (2) 부모 `CLIContextSchema` 런타임 컨텍스트를 중첩 채점 에이전트에 복사해 넘긴다,
# [해설] (3) 채점기 미들웨어(재시도·예산·HITL)는 `agent.py`가 `grader_middleware`로 주입한다(검증 도구 예산은 `goal_rubric.py`의 `_ContextToolCallBudgetMiddleware` 등).
# [해설] 관련 분석 문서: `analysis/05-subagents-goals-rubrics.md`(ReliableRubricMiddleware 절), `analysis/02-agent-assembly-sdk-core.md`.
# [해설] 관련 공식 문서: `docs_official/code/goals-and-rubrics.md`.
# [해설][SDK] 부모 클래스: `libs/deepagents/deepagents/middleware/rubric.py`의 `RubricMiddleware`(채점 루프·`_ensure_grader`·`_invoke_grader`·`_handle_grader_exception`).

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Annotated, Any, Literal, NotRequired, cast

from deepagents.middleware.rubric import (
    RUBRIC_GRADER_MESSAGE_SOURCE,
    GraderResponse,
    RubricMiddleware,
    RubricState,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
)

from deepagents_code._cli_context import CLIContextSchema
from deepagents_code.resume_state import (
    INHERIT_RUBRIC_MODEL,
    coerce_model_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from deepagents.middleware.rubric import RubricEvaluation
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AnyMessage
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime


logger = logging.getLogger(__name__)


# [해설] 체크포인트에서 읽은 `_model_params`를 방어적으로 복사한다. Mapping이 아니면 빈 dict(fail closed),
# [해설] 문자열이 아닌 키는 버린다. `_grader_context`의 상속(inherit) 분기에서만 호출된다.
def _coerce_main_model_params(value: object) -> dict[str, Any]:
    """Copy checkpoint model params, or fail closed for malformed metadata.

    Args:
        value: Raw checkpoint value.

    Returns:
        A copied string-keyed mapping, or an empty mapping when malformed.
    """
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


# [해설] 두 모델 spec이 같은 모델을 가리키는지 비교한다. `provider:model` 형식과 bare 이름(`model`)이 섞여 있을 때만
# [해설] bare 이름끼리 비교한다. `_grader_context`가 "메인 모델 params를 그대로 넘겨도 되는가"를 판단할 때 쓴다.
def _model_specs_match(actual: str, requested: str) -> bool:
    """Compare canonical specs while tolerating one bare model name.

    Returns:
        Whether both values select the same model.
    """
    if (":" in actual) == (":" in requested):
        # Both canonical or both bare: only a literal match selects one model.
        return actual == requested
    # A mixed pair compares bare names. `split` mirrors `ModelSpec.parse`, so a
    # model id that itself contains a colon stays intact.
    return actual.split(":", 1)[-1] == requested.split(":", 1)[-1]


# [해설][설계] 이 미들웨어가 읽어야 하는 dcode 전용 private 채널을 state_schema에 다시 선언한 상태 타입.
# [해설] LangGraph는 미들웨어의 `state_schema`에 선언된 채널만 넘겨주므로, 선언이 빠지면 `_grader_context`가 전부 None을 읽는다.
# [해설][주의] 원본(`resume_state.GoalRubricChannels`/`resume_state.ResumeState`)과 annotation이 달라지면 안 된다. `PrivateStateAttr`가 빠지면 그래프 공개 입출력 스키마로 새어 나간다.
class ReliableRubricState(RubricState):
    """Rubric state carrying dcode's private runtime model selections.

    These three channels are declared a second time here, because this
    middleware's `state_schema` must list every channel it reads. Each
    annotation has to stay identical to its original in
    `resume_state.GoalRubricChannels` / `resume_state.ResumeState`: dropping a
    `PrivateStateAttr` marker leaks the field into the public graph input and
    output schema.
    """

    _model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    """Active main model, written by `ConfigurableModelMiddleware`."""

    _model_params: Annotated[NotRequired[dict[str, Any] | None], PrivateStateAttr]
    """Params that belong to `_model_spec`, written alongside it."""

    _rubric_model_spec: Annotated[NotRequired[str], PrivateStateAttr]
    """Thread-scoped grader selection written by the TUI. Tri-state: absent,
    `resume_state.INHERIT_RUBRIC_MODEL`, or a model spec."""


# [해설] 중첩 채점 에이전트용 상태: `rubric_grading_operation_id`로 검증 도구 호출 예산을 채점 회차 단위로 묶는다.
# [해설][주의] 같은 이름의 클래스가 `goal_rubric.py`에도 정의되어 있고, `agent.py`는 `goal_rubric.RubricGraderState`를 import해 `grader_state_schema`로 넘긴다.
# [해설] 이 파일의 사본은 이 모듈 내부에서 참조되지 않는다(중복 정의, 추정: 이관 잔재).
class RubricGraderState(AgentState[GraderResponse]):
    """Nested-grader state used to scope verification-tool budgets."""

    rubric_grading_operation_id: NotRequired[str]


# [해설][설계] dcode 채점 미들웨어 본체. SDK `RubricMiddleware`의 채점 루프(에이전트 턴 종료 후 기준 대비 채점 → 불만족 시 수정 지시 주입)는 그대로 쓰고,
# [해설] 채점 모델 선택·컨텍스트 복사·런타임 채점기 그래프 캐시만 override 한다.
# [해설] 생성: `agent.py`(루브릭 활성 시). 호출: SDK `RubricMiddleware`의 after-agent 계열 훅이 `_invoke_grader`/`_ainvoke_grader`를 부른다.
class ReliableRubricMiddleware(RubricMiddleware):
    """Run a context-aware nested grader with CLI verification middleware.

    The nested grader receives Deep Agents Code's verification middleware and
    runtime context without requiring those application-specific capabilities in
    the SDK's `RubricMiddleware`. The grader middleware stack owns model retries,
    so transient failures follow the same budget and taxonomy as every other
    dcode model call without replaying completed grader tools.

    The CLI configures the grader's `CodeModelRetryMiddleware` with hidden
    stream output. Grader messages use a nested namespace that both clients
    filter before rendering, so a dropped read or truncated body can retry the
    failed model node without duplicating visible output or replaying completed
    grader tools. Other model retry middleware instances keep the streamed-output
    guard enabled.

    The grader model is selected per request from thread state rather than
    fixed at construction. `inherit_main_model` supplies the default for a
    thread that has recorded no selection of its own.
    """

    # Widens the SDK's `RubricState` so LangGraph passes dcode's private
    # channels to this middleware. Without it `_grader_context` reads `None`
    # for every one of them and silently grades with the construction-time
    # model -- no error, no log, and the type checker stays happy.
    state_schema = ReliableRubricState

    # [해설] 생성자: SDK 인자는 그대로 부모에 넘기고, dcode 전용 3개 필드를 추가한다.
    # [해설] - `runtime_bootstrap_model`: thread별 런타임 모델 문자열로 채점할 때 중첩 그래프를 한 번 만들기 위한 부트스트랩 모델.
    # [해설] - `inherit_main_model`: thread가 채점 모델을 따로 고르지 않았을 때 메인 모델을 상속할지의 기본값.
    def __init__(  # noqa: D107
        self,
        *,
        model: str | BaseChatModel,
        system_prompt: str | None = None,
        tools: Sequence[BaseTool] | None = None,
        grader_middleware: Sequence[AgentMiddleware[Any, Any]] | None = None,
        grader_context_schema: type[Any] | None = None,
        grader_state_schema: type[AgentState[Any]] | None = None,
        prepare_messages_for_grader: Callable[[list[AnyMessage]], list[AnyMessage]]
        | None = None,
        build_grader_state: Callable[[RubricState, int], Mapping[str, Any]]
        | None = None,
        runtime_bootstrap_model: str | BaseChatModel | None = None,
        inherit_main_model: bool = False,
        max_iterations: int = 3,
        on_evaluation: Callable[[RubricEvaluation], None] | None = None,
    ) -> None:
        super().__init__(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            grader_middleware=grader_middleware,
            grader_context_schema=grader_context_schema,
            grader_state_schema=grader_state_schema,
            prepare_messages_for_grader=prepare_messages_for_grader,
            build_grader_state=build_grader_state,
            max_iterations=max_iterations,
            on_evaluation=on_evaluation,
        )
        # [해설][설계] `_runtime_grader_model`은 ContextVar다. 동시에 여러 thread의 채점이 돌아도 요청(태스크)별로 선택 모델이 격리된다.
        # [해설] 공유 미들웨어 인스턴스의 필드를 직접 바꾸지 않기 위한 장치.
        self._runtime_bootstrap_model = runtime_bootstrap_model
        self._runtime_grader: Any = None
        self._inherit_main_model = inherit_main_model
        self._runtime_grader_model: ContextVar[str | None] = ContextVar(
            "runtime_grader_model",
            default=None,
        )

    # [해설] 요청 하나의 채점 동안만 ContextVar에 선택된 채점 모델을 설정하고, 끝나면 reset 한다.
    # [해설] `_invoke_grader`/`_ainvoke_grader`/`_handle_grader_exception`에서 `with` 블록으로 사용.
    @contextmanager
    def _runtime_grader_trace(self, model: str | None) -> Iterator[None]:
        """Scope trace diagnostics to one request's selected grader model."""
        token = self._runtime_grader_model.set(model)
        try:
            yield
        finally:
            self._runtime_grader_model.reset(token)

    # [해설][SDK] 부모 `RubricMiddleware._grader_trace_metadata` override: 트레이스 메타데이터에 요청별 실제 채점 모델 라벨을 기록한다.
    # [해설] 런타임 모델 문자열이면 구조화 출력 전략(ProviderStrategy/ToolStrategy)을 생성 시점 모델로는 예측할 수 없으므로 "unknown"으로 표기한다.
    def _grader_trace_metadata(
        self,
        *,
        effective_strategy: Literal["ProviderStrategy", "ToolStrategy"] | None = None,
    ) -> dict[str, str]:
        """Build diagnostics for the request-local grader selection.

        Returns:
            The configured model label and effective structured-output strategy.
        """
        runtime_model = self._runtime_grader_model.get()
        if runtime_model is not None and effective_strategy is None:
            # A runtime string is resolved inside the nested graph, so the
            # construction-time model cannot predict its output strategy;
            # deriving one from `self._model` would only be discarded.
            return {
                "rubric_grader_configured_model": runtime_model,
                "rubric_grader_effective_strategy": "unknown",
            }
        metadata = super()._grader_trace_metadata(
            effective_strategy=effective_strategy,
        )
        if runtime_model is not None:
            metadata["rubric_grader_configured_model"] = runtime_model
        return metadata

    # [해설] 부모 런타임 컨텍스트를 `CLIContextSchema`로 정규화하고, 가변 컨테이너(dict/list)를 복사한 독립 사본을 만든다.
    # [해설][주의] 인식할 수 없는 컨텍스트면 기본값으로 대체되는데, 이 경우 `approval_mode`/`auto_approve`가 사라져 YOLO 세션도 채점기 안에서 승인 게이트가 걸린다(경고 로그).
    @staticmethod
    def _context(context: object | None) -> CLIContextSchema:
        """Copy the parent runtime context into the nested grader schema.

        Returns:
            An independent context safe to customize for one grader call.
        """
        resolved = CLIContextSchema.from_payload(context)
        if resolved is None:
            if context is not None:
                # Defaults silently drop `approval_mode`/`auto_approve`, so a
                # yolo session would become approval-gated inside the nested
                # grader. That is a wiring bug, not a normal state.
                logger.warning(
                    "Unrecognized grader context type %s; using defaults",
                    type(context).__name__,
                )
            return CLIContextSchema()
        # The parent context is shared across concurrent grader calls; copy the
        # mutable containers so one call cannot mutate another's.
        return replace(
            resolved,
            model_params=dict(resolved.model_params),
            profile_overrides=dict(resolved.profile_overrides),
            hooks_server_events=list(resolved.hooks_server_events),
        )

    # [해설][흐름] 채점 모델 선택의 핵심. `_rubric_model_spec` 3상태(없음 / `INHERIT_RUBRIC_MODEL` 센티넬 / 명시 spec)에 따라
    # [해설] 요청 전용 `CLIContextSchema`의 `model`/`model_params`를 결정한다. 이 컨텍스트는 중첩 채점 에이전트의 `ConfigurableModelMiddleware`(추정)가 읽어 실제 모델을 바꾼다.
    def _grader_context(
        self, state: ReliableRubricState, context: object | None
    ) -> CLIContextSchema:
        """Select the effective grader model without mutating shared middleware.

        `_rubric_model_spec` is a tri-state, so there are three outcomes:

        - the inheritance sentinel, or no selection while `inherit_main_model`
          is set, grades with the active main model;
        - a recorded spec grades with that dedicated model;
        - no selection while a construction-time grader model was configured
          leaves `model` unset, which selects that model.

        Returns:
            Request-local grader context carrying the selected model and params.
        """
        # [해설][흐름] 1) 부모 컨텍스트 복사 → 2) thread의 채점 모델 선택값 읽기 → 3) 상속 여부 계산(센티넬이거나, 미선택+`inherit_main_model`).
        grader_context = self._context(context)
        selected = coerce_model_spec(state.get("_rubric_model_spec"))
        inherit = selected == INHERIT_RUBRIC_MODEL or (
            selected is None and self._inherit_main_model
        )
        # [해설][흐름] 4a) 상속: 메인 모델 spec과 params를 한 단위로 복사. 요청 컨텍스트의 모델(`/model` override)과 다르면 params는 비운다(다른 모델의 params 오염 방지).
        if inherit:
            # Model and params are resolved as a unit. `_model_spec` is written
            # only after a main-model call, so on a thread's first grading pass
            # the channel is absent and the parent context still holds the live
            # `/model` override -- along with the params that belong to it.
            main_model = coerce_model_spec(state.get("_model_spec"))
            if main_model is not None:
                requested_model = coerce_model_spec(grader_context.model)
                grader_context.model = main_model
                grader_context.model_params = (
                    _coerce_main_model_params(state.get("_model_params"))
                    if requested_model is None
                    or _model_specs_match(main_model, requested_model)
                    else {}
                )
        # [해설][흐름] 4b) 비상속: 명시 spec이면 그 모델, None이면 컨텍스트 모델을 비워 생성 시점 채점 모델을 쓰게 한다.
        else:
            # `selected` is either a recorded spec or `None`; `None` means "no
            # runtime override", which selects the model the grader was built
            # with. Clearing the parent's model is deliberate.
            grader_context.model = selected
            grader_context.model_params = {}
        return grader_context

    # [해설] 해석된 기본 모델로 중첩 채점 에이전트 그래프(`create_agent`)를 만든다. `response_format=GraderResponse`로 구조화된 판정을 강제하고,
    # [해설] `name=RUBRIC_GRADER_MESSAGE_SOURCE`로 클라이언트가 채점기 메시지를 걸러낼 수 있게 한다.
    # [해설][SDK] 부모 `RubricMiddleware._ensure_grader`의 그래프 생성 로직과 동일한 인자 구성을 모델만 바꿔 재사용하려고 분리했다.
    def _build_grader(self, model: str | BaseChatModel) -> tuple[Any, BaseChatModel]:
        """Build a nested grader around a resolved base model.

        Returns:
            The grader graph and its resolved base model.
        """
        from deepagents._models import (  # noqa: PLC2701
            resolve_model,
        )
        from langchain.agents import create_agent

        resolved_model = resolve_model(model)
        grader = create_agent(
            model=resolved_model,
            system_prompt=self._system_prompt,
            tools=self._tools,
            middleware=self._grader_middleware,
            name=RUBRIC_GRADER_MESSAGE_SOURCE,
            response_format=GraderResponse,
            state_schema=self._grader_state_schema,
            context_schema=self._grader_context_schema,
        )
        return grader, resolved_model

    # [해설][SDK] 부모 `_ensure_grader` override: 채점기 그래프를 지연 생성·캐시한다.
    # [해설] 런타임 모델이 선택된 요청이면 `runtime_bootstrap_model`로 만든 별도 그래프(`_runtime_grader`)를 공유한다.
    # [해설] 실제 모델 전환은 그래프 내부에서 컨텍스트의 `model`로 이뤄지므로(추정) 그래프는 하나로 충분하다.
    def _ensure_grader(self) -> Any:  # noqa: ANN401
        if self._grader is not None:
            return self._grader
        # [해설] 분기: 런타임 모델 요청 + 부트스트랩 모델이 있을 때만 런타임 그래프 경로.
        runtime_model = self._runtime_grader_model.get()
        if runtime_model is not None and self._runtime_bootstrap_model is not None:
            if self._runtime_grader is None:
                self._runtime_grader, _ = self._build_grader(
                    self._runtime_bootstrap_model
                )
            return self._runtime_grader
        # [해설] 그 외에는 생성 시점 모델(`self._model`)로 만든 기본 그래프를 캐시한다.
        self._grader, self._resolved_model = self._build_grader(self._model)
        return self._grader

    # [해설][SDK] 부모 `_invoke_grader` override(동기): 요청 전용 컨텍스트를 계산해 `context=`로 넘기고, ContextVar 범위 안에서 부모 채점을 실행한다.
    def _invoke_grader(
        self,
        state: RubricState,
        iteration: int,
        correction: str | None = None,
        *,
        context: object | None = None,
    ) -> GraderResponse:
        """Invoke the nested grader with the state-selected model context.

        Returns:
            Parsed grader response.
        """
        reliable_state = cast("ReliableRubricState", state)
        grader_context = self._grader_context(reliable_state, context)
        with self._runtime_grader_trace(grader_context.model):
            return super()._invoke_grader(
                state,
                iteration,
                correction,
                context=grader_context,
            )

    # [해설][SDK] 비동기 버전. `_invoke_grader`와 동일한 흐름.
    async def _ainvoke_grader(
        self,
        state: RubricState,
        iteration: int,
        correction: str | None = None,
        *,
        context: object | None = None,
    ) -> GraderResponse:
        """Invoke the nested grader asynchronously with state-selected context.

        Returns:
            Parsed grader response.
        """
        reliable_state = cast("ReliableRubricState", state)
        grader_context = self._grader_context(reliable_state, context)
        with self._runtime_grader_trace(grader_context.model):
            return await super()._ainvoke_grader(
                state,
                iteration,
                correction,
                context=grader_context,
            )

    # [해설][SDK] 부모 `_handle_grader_exception` override: 채점 예외 처리(오류 판정 기록 등) 중에도 트레이스 메타데이터가 올바른 채점 모델을 가리키도록 같은 컨텍스트로 감싼다.
    def _handle_grader_exception(
        self,
        runtime: Runtime[Any],
        state: RubricState,
        grading_run_id: str,
        iteration: int,
        exc: Exception,
    ) -> dict[str, Any]:
        reliable_state = cast("ReliableRubricState", state)
        context = self._grader_context(
            reliable_state,
            getattr(runtime, "context", None),
        )
        with self._runtime_grader_trace(context.model):
            return super()._handle_grader_exception(
                runtime,
                state,
                grading_run_id,
                iteration,
                exc,
            )
