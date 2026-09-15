"""Goal tools exposed to the agent for persisted TUI goals."""
# [해설][설계] 모듈 개요: `/goal`로 설정된 목표를 에이전트(모델)에게 노출하는 미들웨어와 `update_goal` 도구.
# [해설] 실행 위치: LangGraph 서버 프로세스(에이전트 그래프 내부). `--acp`는 in-process.
# [해설] 주요 진입점: `GoalToolsMiddleware` — `agent.py`에서 `[ResumeStateMiddleware(), CostTrackingMiddleware(), GoalToolsMiddleware()]` 형태로 스택에 추가된다.
# [해설] 설계 핵심: 모델에게 목표를 "읽는" 도구는 없다. 대신 goal-state notice(HumanMessage)를 대화 이력에 영속화(`before_model`)하고,
# [해설] 요약(summarization)으로 창 밖에 밀리면 요청에만 일시적으로 다시 붙인다(`wrap_model_call`).
# [해설] 모델이 쓸 수 있는 유일한 쓰기 도구는 `update_goal(complete|blocked, note)`이며, `complete`는 즉시 커밋되지 않고 staging만 된다.
# [해설] 목표 생성/수락은 클라이언트(TUI `app.py`)가 체크포인트에 기록하고, 기준 초안은 `goal_rubric.GoalCriteriaMiddleware`가 만든다.
# [해설] 관련 분석 문서: `analysis/05-subagents-goals-rubrics.md`. 관련 공식 문서: `docs_official/code/goals-and-rubrics.md`.
# [해설] notice 생성/판별 헬퍼는 `deepagents_code/goal_state_notice.py`, 크기 한도는 `deepagents_code/goal_state_limits.py`.

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    NotRequired,
    cast,
    override,
)

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    TracePolicy,
    omit_payload,
)
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import Field

from deepagents_code.goal_state_limits import (
    GOAL_STATUS_NOTE_CHAR_LIMIT,
    GoalStateSizeError,
    validate_goal_status_note,
)
from deepagents_code.goal_state_notice import (
    build_goal_state_notice,
    goal_notice_size_error,
    goal_state_fingerprint,
    has_goal_or_rubric_state,
    is_oversized_goal_state_message,
    latest_goal_state_message_index,
    latest_goal_state_notice,
    latest_human_is_unsaved_goal_continuation,
    log_malformed_summarization_event,
    superseded_goal_state_placeholder,
    validated_summarization_cutoff,
)

# Runtime (not TYPE_CHECKING) import. `GoalRubricChannels` looks type-only but is
# a base class of `GoalToolState`, supplying the shared `PrivateStateAttr`-marked
# goal/rubric channels so the markers are declared once (see that class).
from deepagents_code.resume_state import (
    GoalRubricChannels,
    coerce_goal_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


# [해설][설계] 목표 도구 이름 집합. 도구가 없어야 하는 상황(behavioral absence gate)과 미들웨어 계약 테스트에서 참조된다.
GOAL_TOOL_NAMES = frozenset({"update_goal"})
"""Tool names used by behavioral absence gates and middleware contract tests."""


# [해설] 이력에 "현재 상태와 일치하고, 모델에게 보이는" notice가 없으면 새 notice를 만든다.
# [해설] 호출자: `GoalToolsMiddleware._notice_update`(영속 경로), `GoalToolsMiddleware._request_with_goal_notice`(일시 re-pin 경로).
def _goal_state_notice_for(
    state: dict[str, Any],
    messages: Sequence[object],
    *,
    cutoff: int,
) -> HumanMessage | None:
    """Build a notice when effective history lacks current goal/rubric state.

    Args:
        state: Authoritative middleware state.
        messages: Messages visible at the next model boundary.
        cutoff: Summarization cutoff index that `messages` is measured against.
            Required rather than defaulted: both callers pass the full persisted
            list, where a notice below the cutoff is present but invisible to the
            model, and this middleware wraps the summarizer so a trimmed window
            never reaches here. A default of `0` would silently treat such a
            notice as visible.

    Returns:
        Current notice to append, or `None` when history is already authoritative.
    """
    # [해설][흐름] 1) 마지막 사람 메시지가 저장 실패한 목표의 continuation 메시지면 그 메시지가 목표를 인용하므로 notice 불필요.
    if latest_human_is_unsaved_goal_continuation(messages):
        return None
    # [해설][흐름] 2) 최신 notice가 최신 후보 인덱스와 같고, fingerprint가 현재 상태와 같고, 요약 cutoff 이상(모델에게 보임)이면 그대로 둔다.
    latest = latest_goal_state_notice(messages)
    latest_candidate = latest_goal_state_message_index(messages)
    fingerprint = goal_state_fingerprint(state)
    if (
        latest is not None
        and latest[0] == latest_candidate
        and latest[1]["state_fingerprint"] == fingerprint
        and latest[0] >= cutoff
    ):
        return None
    # [해설][흐름] 3) notice 이력도 없고 목표/루브릭 상태도 없으면 알릴 것이 없다. 그 외에는 현재 상태로 새 notice 생성.
    if latest_candidate is None and not has_goal_or_rubric_state(state):
        return None
    return build_goal_state_notice(state)


# [해설][설계] 목표 도구가 쓰는 상태 스키마. private 채널(`_goal_*`, `_sticky_rubric`)은 `resume_state.GoalRubricChannels`에서 상속해
# [해설] `ResumeState`와 선언이 어긋나지 않게 하고, 공개 입력 `rubric`(SDK `RubricMiddleware`의 입력)만 추가한다.
class GoalToolState(GoalRubricChannels):
    """State fields used by goal tools.

    Inherits the shared `_goal_*`/`_sticky_rubric` channels (with their
    `PrivateStateAttr` markers) from `GoalRubricChannels`, so the goal tools and
    `ResumeState` cannot drift apart. Adds only the public `rubric` graph input,
    which is intentionally non-private — it is the `RubricMiddleware` input.
    """

    rubric: NotRequired[str | None]
    """Public `RubricMiddleware` graph input (intentionally non-private).

    Distinct from the TUI-owned `_sticky_rubric`: this is the per-invocation
    rubric passed in via the graph schema, not checkpointed TUI state.
    """


# [해설] `update_goal` 도구의 실제 로직. 전제조건을 순서대로 검사하고, 통과 못 하면 상태 변경 없이 설명 ToolMessage만 반환한다.
# [해설] 호출자: `GoalToolsMiddleware.__init__` 안의 `@tool update_goal`. 반환: LangGraph `Command(update=...)`.
def _update_goal_command(
    *,
    status: Literal["complete", "blocked"],
    note: str,
    tool_call_id: str,
    state: dict[str, Any],
) -> Command[Any]:
    """Build the constrained `update_goal` command.

    Args:
        status: Goal status the model is reporting (`complete` or `blocked`).
        note: Evidence the goal is complete, or the specific blocker. Required;
            the status is not committed without it.
        tool_call_id: Tool call ID for the returned `ToolMessage`.
        state: Current graph state injected by LangGraph.

    Returns:
        Command updating goal metadata and returning a tool response.
            A `complete` request stages `_pending_goal_completion_note` for
            the TUI to resolve once the rubric verdict lands, rather than
            committing the status directly; `blocked` commits immediately.

            Nothing is committed in five cases, and the `ToolMessage` explains
            what the model must do instead: no goal is set, saved state is too
            large to render as a notice, the goal is paused or already complete,
            `note` is empty, or `note` exceeds `GOAL_STATUS_NOTE_CHAR_LIMIT`.
            The `note` size is also gated by the tool schema's `max_length`, so
            the runtime check catches only calls that bypass it — and measures
            the stripped note.
    """
    # Enforced preconditions here are: an objective exists, its state fits the
    # notice budget, its status is neither paused nor complete, and `note` is
    # non-empty and fits `GOAL_STATUS_NOTE_CHAR_LIMIT`. Note the objective check alone
    # does not imply actionability — a paused goal has an objective too, so the
    # status check is separate. Completion is staged because `RubricMiddleware`
    # records its final verdict after the model stops making tool calls; the TUI
    # resolves the staged request during post-turn checkpoint sync.
    # [해설][흐름] 1) 목표(objective)가 없으면 거부.
    objective = state.get("_goal_objective")
    if not isinstance(objective, str) or not objective:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="No active goal is set.",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    # [해설][흐름] 2) 저장된 목표/루브릭 상태가 notice 예산을 넘으면 거부(다른 어떤 조건보다 우선). 복구는 사용자 `/goal clear`로만 가능.
    # Oversized state takes precedence over every other precondition: the notice
    # has already told the model the goal is unavailable and not to work toward
    # it or grade against it. Since the read tools were removed, `update_goal` is
    # the only goal surface the model has, so refusing here is what keeps that
    # instruction from resting on prose alone. Project exactly as the renderer
    # does, or the check passes against text the notice does not carry, which is
    # why this goes through the shared helper rather than projecting again here.
    #
    # The refusal covers an oversized `_goal_status_note` too, which the model
    # itself wrote on an earlier turn. It cannot replace that note with a shorter
    # one, because this is the call that would do it. Recovery is deliberately
    # user-only (`/goal clear`): the alternative is letting the model rewrite
    # state the notice has already told it is unavailable.
    exc = goal_notice_size_error(state)
    if exc is not None:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"Saved goal/rubric state is too large to use, so its "
                            f"status cannot be updated. Ask the user to clear and "
                            f"recreate the goal. Validation detail: {exc}"
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    # [해설][흐름] 3) paused/complete 상태면 거부. 상태가 없거나 이상하면 `coerce_goal_status` 결과 None → "active"로 간주.
    goal_status = coerce_goal_status(state.get("_goal_status")) or "active"
    if goal_status in {"paused", "complete"}:
        if goal_status == "paused":
            message = (
                "The goal is paused. The user must run `/goal resume` before its "
                "status can be updated."
            )
        else:
            message = "The goal is already complete and cannot be updated."
        return Command(
            update={
                "messages": [ToolMessage(content=message, tool_call_id=tool_call_id)]
            }
        )
    # [해설][흐름] 4) note 공백 제거 후 비어 있으면 거부(근거 없는 상태 변경 금지).
    clean_note = note.strip()
    if not clean_note:
        # Evidence is required: refuse to commit a status with no justification
        # rather than silently storing an empty note.
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=(
                            f"Provide a note with evidence before marking the "
                            f"goal {status}."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    # [해설][흐름] 5) note 길이 한도(`GOAL_STATUS_NOTE_CHAR_LIMIT`) 재검사. 스키마 `max_length`를 우회한 호출 대비.
    try:
        validate_goal_status_note(clean_note)
    except GoalStateSizeError as exc:
        return Command(
            update={
                "messages": [ToolMessage(content=str(exc), tool_call_id=tool_call_id)]
            }
        )
    # [해설][흐름] 6a) complete: `_pending_goal_completion_note`에만 staging. 실제 완료 확정은 클라이언트(TUI)가 같은 턴의 루브릭 `satisfied` 판정과 상관시켜 수행한다.
    # [해설][설계] 모델의 자기 완료 선언을 채점기 판정 없이 받아들이지 않기 위한 구조.
    if status == "complete":
        return Command(
            update={
                "_pending_goal_completion_note": clean_note,
                "messages": [
                    ToolMessage(
                        content=(
                            "Goal completion requested. It will be recorded if "
                            "the accepted rubric is satisfied."
                        ),
                        tool_call_id=tool_call_id,
                    )
                ],
            }
        )
    # [해설][흐름] 6b) blocked: 즉시 `_goal_status`/`_goal_status_note`에 커밋하고, 대기 중인 완료 note는 지운다.
    update = {
        "_goal_status": status,
        "_goal_status_note": clean_note,
        "_pending_goal_completion_note": None,
    }
    return Command(
        update={
            **update,
            "messages": [
                ToolMessage(
                    content=f"Goal marked {status}. {clean_note}",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


# [해설][설계] 목표 미들웨어 본체. `update_goal` 도구 등록 + goal-state notice 유지(영속 `before_model` / 일시 `wrap_model_call`).
# [해설][주의] 이 미들웨어는 요약(summarization) 미들웨어를 "감싸는" 위치에 있어서 `request.messages`가 잘리지 않은 전체 이력이다. 그래서 cutoff를 직접 반영해야 한다.
class GoalToolsMiddleware(AgentMiddleware[GoalToolState, ContextT]):
    """Expose the constrained `update_goal` tool and maintain the goal-state notice.

    The model reads goal awareness from the injected goal-state notice rather
    than a read tool: `before_model` persists a fresh notice into checkpointed
    history when the latest one no longer matches authoritative state (or has
    scrolled below the summarization cutoff), and `wrap_model_call` re-pins the
    notice into the request when the persisted one is out of view. This
    middleware wraps the summarizer rather than running after it, so the re-pin
    sees untrimmed history and discounts it against the same cutoff
    `before_model` uses. The notice carries the objective and status note for an
    actionable goal and the acceptance criteria for an active rubric, so no
    separate read tool is needed. Only the write-side `update_goal` tool is
    registered.
    """

    # [해설] 트레이스에 훅 입력(상태 전체)을 기록하지 않는다(목표 텍스트 등 페이로드 노출 최소화).
    trace_policy = TracePolicy(process_inputs=omit_payload)
    """Omit hook inputs from traces by default; set a `TracePolicy` to override."""

    state_schema = GoalToolState

    # [해설] 도구를 클로저로 정의해 `self.tools`에 등록한다. `InjectedToolCallId`/`InjectedState`는 LangGraph가 주입하므로 모델 스키마에는 `status`/`note`만 보인다.
    def __init__(self) -> None:
        """Initialize goal tools."""
        super().__init__()

        @tool
        def update_goal(
            status: Annotated[
                Literal["complete", "blocked"],
                Field(
                    description=(
                        "`complete` to attach completion evidence, or `blocked` "
                        "when you are stuck and need the user."
                    )
                ),
            ],
            note: Annotated[
                str,
                Field(
                    max_length=GOAL_STATUS_NOTE_CHAR_LIMIT,
                    description=(
                        "Evidence the criteria are satisfied, or the specific "
                        "blocker. Required when calling this tool."
                    ),
                ),
            ],
            tool_call_id: Annotated[str, InjectedToolCallId],
            state: Annotated[dict[str, Any], InjectedState],
        ) -> Command[Any]:
            """Update a goal only when the latest state notice says it is actionable.

            Read the current objective and any acceptance criteria from the latest
            goal/rubric state notice in context, or — right after a goal whose save
            failed — from the objective and criteria quoted in the accompanying
            goal continuation message. There is no read tool for them. Use
            `blocked` when you cannot proceed without user input. Goals complete
            automatically after a satisfied goal-backed grading turn, so
            `complete` is optional and only stages its evidence for that result.
            Do not create, pause, resume, clear, or replace goals — those are
            user-controlled.

            Returns:
                Command that updates goal status and returns a tool message.
            """
            return _update_goal_command(
                status=status,
                note=note,
                tool_call_id=tool_call_id,
                state=state,
            )

        self.tools = [update_goal]

    @staticmethod
    # [해설] `before_model` 경계에서 체크포인트에 기록할 상태 업데이트를 계산한다(동기/비동기 공용).
    # [해설] 세 부분이 독립적으로 붙는다: 새 notice(`messages`), 잘못된 `_summarization_event` 리셋, 크기 초과 시 공개 `rubric` 비우기.
    def _notice_update(state: AgentState[Any]) -> dict[str, Any] | None:
        """Compute the checkpointed notice update for a `before_model` boundary.

        Returns:
            A state update with any of three independent parts, or `None` when
            none apply: a `messages` entry carrying a fresh notice; a
            `_summarization_event` reset discarding a malformed event; and a
            `rubric` entry set to `None` when saved goal/rubric state exceeds
            the notice budget, which clears the public per-invocation rubric so
            grading cannot re-inject oversized text. The oversized case can
            return a `rubric` clear with no `messages` key at all.
        """
        # [해설][흐름] 1) 메시지 목록과 요약 이벤트의 cutoff를 검증. 잘못된 이벤트(`cutoff is None`)면 malformed로 표시.
        values = cast("dict[str, Any]", state)
        raw_messages = values.get("messages", [])
        messages = list(raw_messages) if isinstance(raw_messages, list) else []
        # `state["messages"]` is the full persisted list, so the cutoff rule
        # applies — see `validated_summarization_cutoff`. Discounting matters here
        # because it is what makes the durable write happen, instead of leaving
        # the transient re-pin in `wrap_model_call` to carry the objective on every
        # subsequent turn. For a valid event this matches the client-side
        # predicate in `app.py`. A malformed event diverges deliberately: `app.py`
        # collapses it to `0`, while here it forces a fresh notice and clears the
        # event.
        event = values.get("_summarization_event")
        cutoff = validated_summarization_cutoff(
            event,
            message_count=len(messages),
        )
        malformed_event = event is not None and cutoff is None
        # [해설][흐름] 2) notice 필요 여부 판단. malformed면 cutoff를 메시지 길이로 두어 기존 notice를 "안 보임"으로 취급 → 새 notice 강제.
        notice = _goal_state_notice_for(
            values,
            messages,
            # Force a fresh notice when discarding an event so a summarization
            # regenerated on this boundary retains the canonical goal state.
            cutoff=len(messages) if malformed_event else (cutoff or 0),
        )
        # [해설][흐름] 3) 업데이트 조립: malformed 이벤트는 로그 후 None으로 초기화, notice가 있으면 추가.
        update: dict[str, Any] = {}
        if malformed_event:
            log_malformed_summarization_event(event, len(messages))
            update["_summarization_event"] = None
        if notice is not None:
            update["messages"] = [notice]
        # [해설][흐름] 4) 크기 초과 시 저장 상태는 보존(복구 가능)하되 이번 턴 채점이 거대한 텍스트를 다시 주입하지 않도록 `rubric`만 None.
        exc = goal_notice_size_error(values)
        # Keep authoritative saved state recoverable, but clear the public
        # per-invocation rubric so grading cannot re-inject oversized text.
        if exc is not None and values.get("rubric") is not None:
            logger.warning(
                "Goal/rubric state exceeds the notice budget; clearing the "
                "per-invocation rubric so this turn is not graded: %s",
                exc,
            )
            update["rubric"] = None
        return update or None

    # [해설] 영속 notice 쓰기(동기). 반환 dict가 LangGraph 상태 업데이트로 체크포인트에 반영된다.
    @override
    def before_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice into checkpointed history.

        This is the durable half of the notice mechanism; the transient
        counterpart in `wrap_model_call` re-pins the notice into a request whose
        persisted notice has scrolled out of the model-visible window.

        Returns:
            Message update containing a current notice, or `None` when unchanged.
        """
        del runtime
        return self._notice_update(state)

    # [해설] 영속 notice 쓰기(비동기). 로직은 `_notice_update` 공유.
    @override
    async def abefore_model(
        self,
        state: AgentState[Any],
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Persist a current goal-state notice at an async model boundary.

        Async twin of `before_model`; see it for the persisted-vs-transient split.

        Returns:
            Message update containing a current notice, or `None` when unchanged.
        """
        del runtime
        return self._notice_update(state)

    # [해설][흐름] 요청 단위 일시 re-pin. 체크포인트는 건드리지 않고 `request.override`로 모델 요청 메시지만 바꾼다.
    @staticmethod
    def _request_with_goal_notice(
        request: ModelRequest[ContextT],
    ) -> ModelRequest[ContextT]:
        """Ensure a current goal-state notice is visible in a model request.

        When checkpointed history no longer surfaces a current notice, a
        transient goal-state notice is appended to the request messages only
        (not persisted; `before_model` owns the durable write). Earlier bounded
        notices remain byte-stable so changing goal state does not invalidate the
        cacheable conversation prefix. Oversized legacy notices are replaced by
        bounded same-index stand-ins so they cannot exhaust the model context.
        Replacement keeps the list length and every index stable, which the inner
        summarizer's persisted absolute cutoff depends on — see
        `superseded_goal_state_placeholder`. The current notice explicitly
        supersedes them, and the system prompt is left unchanged.

        This middleware wraps the summarizer, so `request.messages` is the full
        persisted list rather than a trimmed window. The summarization cutoff is
        therefore passed through to `_goal_state_notice_for`, matching
        `before_model`: without it a notice sitting below the cutoff counts as
        visible here, the re-pin declines, and the inner summarizer then drops
        the only copy the model had.

        Returns:
            The request unchanged apart from any malformed-event state override
            when no notice or oversized-message replacement is needed. Otherwise,
            a request with oversized legacy notices replaced in place, any current
            goal-state notice appended, and — for a malformed
            `_summarization_event` — a state override nulling that event.
        """
        # [해설][흐름] 1) 요약 이벤트 cutoff 검증. malformed면 내부(요약 미들웨어)로 넘기는 state에서 이벤트를 None으로 덮어써 유일한 notice 사본이 잘려 나가는 것을 막는다.
        values = cast("dict[str, Any]", request.state)
        event = values.get("_summarization_event")
        cutoff = validated_summarization_cutoff(
            event,
            message_count=len(request.messages),
        )
        malformed_event = event is not None and cutoff is None
        if malformed_event:
            # Disable an invalid restored event in the request passed inward so
            # its Python slice cannot remove the only model-visible copy of the
            # goal state.
            log_malformed_summarization_event(event, len(request.messages))
            values = {**values, "_summarization_event": None}
            request = request.override(state=cast("AgentState[Any]", values))
        # [해설][흐름] 2) 현재 notice 필요 여부(영속 경로와 같은 cutoff 규칙).
        notice = _goal_state_notice_for(
            values,
            request.messages,
            # Force a fresh notice when discarding an event, matching
            # `_notice_update`, so a summarization regenerated on this boundary
            # retains the canonical goal state.
            cutoff=len(request.messages) if malformed_event else (cutoff or 0),
        )
        # [해설][흐름] 3) 과거의 크기 초과 notice는 같은 인덱스의 짧은 placeholder로 교체(리스트 길이·인덱스 보존 → 요약기의 절대 cutoff가 깨지지 않음).
        # [해설] 새 notice가 없을 때는 최신 notice 하나는 보존한다.
        latest = latest_goal_state_notice(request.messages)
        preserved_index = latest[0] if notice is None and latest is not None else None
        messages = [
            (
                superseded_goal_state_placeholder(message)
                if index != preserved_index and is_oversized_goal_state_message(message)
                else message
            )
            for index, message in enumerate(request.messages)
        ]
        # [해설][흐름] 4) 새 notice는 끝에 append(앞쪽 prefix를 바이트 동일하게 유지해 프롬프트 캐시 보존). 아무것도 안 바뀌면 원래 request를 그대로 반환.
        if notice is not None:
            messages.append(notice)
        if notice is None and all(
            projected is original
            for projected, original in zip(messages, request.messages, strict=True)
        ):
            return request
        return request.override(messages=messages)

    # [해설][SDK] LangChain `AgentMiddleware.wrap_model_call` 훅: 모델 호출 직전에 notice가 보이도록 요청을 보정한 뒤 handler(다음 미들웨어/모델)를 호출.
    @override
    def wrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each model request when needed.

        Returns:
            Model response from the wrapped handler.
        """
        return handler(self._request_with_goal_notice(request))

    # [해설] 비동기 버전.
    @override
    async def awrap_model_call[ResponseT](
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT]:
        """Re-pin the goal-state notice into each async model request when needed.

        Returns:
            Model response from the wrapped handler.
        """
        return await handler(self._request_with_goal_notice(request))
