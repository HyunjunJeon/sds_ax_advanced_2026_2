"""Server-owned Hooks v2 lifecycle middleware.

Emits `PreCompact`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`,
`SubagentStart`, and `SubagentStop` through the LangGraph interrupt channel so the
client runtime can execute matching handlers and return typed decisions.
"""

# [해설] 모듈 개요: Hooks v2의 "서버 소유(server-owned)" 이벤트 7종(PreCompact, PreToolUse, PostToolUse, PostToolUseFailure,
# [해설] Stop, SubagentStart, SubagentStop)을 에이전트 실행 경로에서 방출하는 LangChain `AgentMiddleware`.
# [해설] 실행 프로세스: 서버(LangGraph 서버 프로세스의 에이전트 그래프 안). `--acp`는 in-process라 클라이언트와 같은 프로세스.
# [해설][설계] 핵심 아이디어 — 서버는 훅 셸 명령을 절대 직접 실행하지 않는다. 사용자의 hooks.json·셸·신뢰 판정은 클라이언트에 있으므로
# [해설] 서버는 LangGraph `interrupt()`로 그래프를 멈추고 `HookInvocationRequest`를 클라이언트에 보낸다. 클라이언트
# [해설] (`hooks/client.py`의 `fulfill_hook_invocation`)가 핸들러를 실행해 `Command(resume=...)`로 결정을 돌려주면 그래프가 재개된다.
# [해설][흐름] 왕복 요약: after_model(PreCompact/PreToolUse → state에 결과 저장) → wrap_tool_call(deny면 차단, task면 SubagentStart,
# [해설][흐름] 실행 후 소요시간을 pending에 기록) → 다음 before_model(PostToolUse/Failure, SubagentStop) → after_agent(Stop, block이면 model로 jump).
# [해설] 주요 진입점: `ServerHooksMiddleware`, `hook_decided_permission`/`hook_permission_behavior`(승인 흐름이 조회),
# [해설] `operation_hook_responses`/`HookTransportInterruptError`(그래프 밖 `/offload` 작업용 전송).
# [해설] 호출자: `agent.py`가 메인 그래프에 HITL 미들웨어 뒤로 붙이고, 서브에이전트 미들웨어에도 `emit_stop=False`로 붙인다.
# [해설] `offload_api.py`(`_execute_offload`)와 `offload_middleware.py`가 operation 모드 전송을 쓴다.
# [해설] 관련 문서: `analysis/07-mcp-hooks-extensions-plugins.md`(B절, 핵심 설계 포인트 1·2·5), `analysis/04-approval-hitl-security.md`,
# [해설] 공식 `docs_official/code/hooks.md`(server-owned events). 공식 문서 이벤트 목록에는 PostToolUseFailure가 빠져 있다(analysis/07 대조표).
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, NotRequired, TypeGuard, cast
from uuid import UUID, uuid5

from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    PrivateStateAttr,
    ResponseT,
    TracePolicy,
    hook_config,
    omit_payload,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command, interrupt
from pydantic import ValidationError
from typing_extensions import TypedDict

from deepagents_code.approval_mode import ApprovalMode, coerce_approval_mode
from deepagents_code.hooks.interrupt import (
    build_hook_interrupt_payload,
    parse_hook_resume_value,
)
from deepagents_code.hooks.models.domain import (
    AgentIdentity,
    BaseHookDecision,
    CompactTrigger,
    HookContext,
    HookDecision,
    HookDiagnostic,
    HookEvent,
    HookInvocation,
    PermissionEffect,
    PostToolUseDecision,
    PostToolUseEvent,
    PostToolUseFailureDecision,
    PostToolUseFailureEvent,
    PreCompactDecision,
    PreCompactEvent,
    PreToolUseDecision,
    PreToolUseEvent,
    StopDecision,
    StopEvent,
    SubagentStartDecision,
    SubagentStartEvent,
    SubagentStopDecision,
    SubagentStopEvent,
    ToolCallData,
)
from deepagents_code.hooks.models.transport import HookInvocationRequest
from deepagents_code.hooks.reducer import reduce_hook_results
from deepagents_code.hooks.tools import to_wire_tool_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from langchain.tools.tool_node import ToolCallRequest
    from langchain_core.messages.tool import ToolCall
    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool
    from langgraph.runtime import Runtime

    from deepagents_code.json_types import JsonObject

# [해설] 모듈 상수:
# [해설] - `_DEFAULT_DEADLINE`: 요청에 첨부되는 클라이언트 실행 마감(600초). 핸들러 기본 timeout(600초, `hooks/capabilities.py`)과 같은 값.
# [해설] - `_STOP_STATE_KEY` / `_PRE_TOOL_STATE_KEY` / `_PENDING_POST_TOOL_STATE_KEY`: `ServerHooksState`의 private 채널 이름.
# [해설] - `_TASK_TOOL_NAME`: SDK `SubAgentMiddleware`의 서브에이전트 호출 도구 이름 → SubagentStart/Stop 방출 조건.
# [해설] - `_COMPACT_TOOL_NAME`: 대화 압축 도구 → PreCompact 방출 조건.
# [해설] - `_INVOCATION_NAMESPACE`: 결정적 invocation_id(uuid5) 생성용 고정 네임스페이스. 바꾸면 진행 중 resume의 id가 어긋난다.
_DEFAULT_DEADLINE = timedelta(seconds=600)
_STOP_STATE_KEY = "_hooks_stop_continuation_count"
_PRE_TOOL_STATE_KEY = "_hooks_pre_tool_outcomes"
_PENDING_POST_TOOL_STATE_KEY = "_hooks_pending_post_tools"
_TASK_TOOL_NAME = "task"
_COMPACT_TOOL_NAME = "compact_conversation"
_INVOCATION_NAMESPACE = UUID("f2896d18-cf2a-4e7d-b11a-d5b10fc0e335")


# [해설] 그래프 밖 서버 작업(`/offload` 압축 HTTP route)에서 "클라이언트가 아직 답하지 않은 훅 요청"을 HTTP 경계까지 운반하는 제어 신호.
# [해설][흐름] `_invoke_hook`이 raise → `offload_api.py`가 잡아 `{"status": "interrupt", "request": ...}`로 클라이언트에 응답 →
# [해설] 클라이언트가 이행 후 응답 맵을 들고 재요청 → `operation_hook_responses`로 주입된 채 작업을 처음부터 재실행.
class HookTransportInterruptError(BaseException):
    """Carry a hook request across a non-graph server operation boundary.

    Derives from `BaseException`, not `Exception`, for the same reason
    `asyncio.CancelledError` does: it is a control signal that must reach the
    HTTP boundary intact. The compaction chain it crosses is lined with broad
    `except Exception` handlers, any of which would otherwise turn a resumable
    hook request into a permanent `"failed"` result.
    """

    def __init__(self, request: HookInvocationRequest) -> None:
        """Initialize the transport interrupt.

        Args:
            request: Hook invocation the client must fulfill.
        """
        super().__init__(str(request.invocation_id))
        self.request = request


logger = logging.getLogger(__name__)

# [해설] operation 모드 판별 + 응답 저장소. `None`이면 일반 그래프 모드(`interrupt()` 사용), 매핑이면 operation 모드.
# [해설] ContextVar라서 동시에 도는 다른 요청/태스크에 새지 않는다.
_HOOK_RESPONSES: ContextVar[Mapping[str, object] | None] = ContextVar(
    "deepagents_code_hook_responses",
    default=None,
)


# [해설] `offload_api.py`가 `server.offload.execute(...)`를 감싸는 컨텍스트. 지금까지 클라이언트가 답한 invocation_id→resume 값을 노출한다.
@contextmanager
def operation_hook_responses(
    responses: Mapping[str, object],
) -> Iterator[None]:
    """Serve hook responses while a server operation replays from the top.

    Args:
        responses: Resume payloads keyed by deterministic hook invocation ID.
    """
    token = _HOOK_RESPONSES.set(responses)
    try:
        yield
    finally:
        _HOOK_RESPONSES.reset(token)


# [해설] `_ask_permission_via_hitl`이 사용: operation 모드에서는 HITL interrupt를 쓸 수 없으므로 ask를 deny로 강등한다.
def _in_server_operation() -> bool:
    """Report whether hooks are running under a non-graph server operation.

    `None` means graph mode; an empty mapping means operation mode with no
    answers accumulated yet, which is why this cannot be a truthiness check.

    Returns:
        `True` when the caller is inside `operation_hook_responses`.
    """
    return _HOOK_RESPONSES.get() is not None


# [해설] PreToolUse 결과를 state에 저장할 때의 behavior 공간. "ask"는 after_model 안에서 HITL로 해소되어 allow/deny로 바뀌므로 저장되지 않는다.
type PreToolBehavior = Literal["allow", "deny", "none"]
_DEFAULT_DENY_REASON = "Blocked by PreToolUse hook"


# [해설] state(`_hooks_pre_tool_outcomes`)에 체크포인트되는 도구 호출별 판정. TypedDict라 JSON 직렬화 가능한 형태를 유지한다.
class _PreToolDenied(TypedDict):
    """Outcome for a call a hook refused. A denial always carries a reason."""

    behavior: Literal["deny"]
    reason: str
    context: list[str]


class _PreToolPassed(TypedDict):
    """Outcome for a call a hook allowed or had no opinion on."""

    behavior: Literal["allow", "none"]
    context: list[str]


type _PreToolState = _PreToolDenied | _PreToolPassed

# Maps a tool-call id to the measured execution duration while the call awaits
# its post-execution hook. The value is overloaded as a tombstone: a `None`
# value means "delete this key", not "no duration". This mirrors LangGraph's
# `RemoveMessage` sentinel -- a LangGraph reducer merges an update into the
# channel and returns the whole new value, so removal is expressed by writing
# a `None` sentinel that `_merge_pending_post_tools` pops, rather than by
# omitting the key (a plain merge can only add/overwrite, never remove).
# `_pending_post_tools` filters these tombstones out, so consumers only ever
# see real `int` durations.
type _PendingPostToolState = dict[str, int | None]


# [해설] `_hooks_pending_post_tools` 채널의 LangGraph reducer. wrap_tool_call이 `{id: ms}`를 추가하고, before_model이 `{id: None}`으로 제거한다.
# [해설] 병렬 도구 호출 여러 개가 각자 Command update를 내도 reducer가 합쳐 주므로 서로 덮어쓰지 않는다.
def _merge_pending_post_tools(
    current: _PendingPostToolState,
    update: _PendingPostToolState,
) -> _PendingPostToolState:
    """Merge pending entries, treating a `None` value as a deletion sentinel.

    LangGraph reducers return the entire new channel value, so writing
    `{call_id: None}` removes `call_id` from the merged result instead of
    storing the `None`. A merge can only add/overwrite keys, so this sentinel
    is the mechanism for removing a consumed entry.

    Args:
        current: Current channel value.
        update: Incoming update; `None` values delete their keys.

    Returns:
        The merged channel value with tombstoned keys removed.
    """
    merged = dict(current)
    for call_id, duration_ms in update.items():
        if duration_ms is None:
            merged.pop(call_id, None)
        else:
            merged[call_id] = duration_ms
    return merged


# [해설] 미들웨어가 그래프 state에 추가하는 private 채널 스키마. `ServerHooksMiddleware.state_schema`로 등록된다.
# [해설][설계] 왜 state인가: after_model → (interrupt/resume) → wrap_tool_call → before_model은 서로 다른 노드·체크포인트 경계라
# [해설] 인스턴스 변수로는 판정을 넘길 수 없다. 체크포인트된 채널에 두어야 재개·재시작 후에도 살아남는다.
# [해설][SDK] 서브에이전트 결과 병합 시 이 필드를 제거하는 일은 SDK `libs/deepagents/deepagents/middleware/subagents.py`의 `SubAgentMiddleware` 몫(docstring 기준).
class ServerHooksState(AgentState[Any]):
    """Agent state extensions for server-owned hook middleware.

    All fields are per-turn bookkeeping owned by `ServerHooksMiddleware` and
    marked `PrivateStateAttr`: they are omitted from the public graph I/O schema,
    and `SubAgentMiddleware` strips them from subagent result merges.

    `PrivateStateAttr` only omits the fields from the input and output schemas;
    the channels remain checkpointed and visible to graph nodes, so values flow
    across lifecycle boundaries and survive interrupt/resume.

    Note:
        Reducers must be placed *after* `PrivateStateAttr` in the `Annotated`
        metadata. LangGraph only inspects the last metadata entry when detecting
        reducers, so a reducer added before the marker is silently ignored.
    """

    _hooks_stop_continuation_count: NotRequired[Annotated[int, PrivateStateAttr]]
    """Stop-hook continuations in the current turn; reset to 0 when the loop ends."""

    _hooks_pre_tool_outcomes: NotRequired[
        Annotated[dict[str, _PreToolState], PrivateStateAttr]
    ]
    """Pre-execution hook verdicts keyed by tool-call id.

    A full snapshot of the *current* turn's calls, not an accumulator: every
    `_after_model` replaces the whole dict (including with `{}`) so stale ids
    cannot survive into a later turn.
    """

    _hooks_pending_post_tools: NotRequired[
        Annotated[
            _PendingPostToolState,
            PrivateStateAttr,
            _merge_pending_post_tools,
        ]
    ]
    """Executed calls awaiting post-tool hooks at a checkpointed boundary."""


# [해설] 턴 context에서 뽑은 "이 세션에서 훅을 방출해도 되는가" 게이트. `_session_gate`가 생성하며,
# [해설] snapshot_id(클라이언트 훅 스냅샷 id)와 events(핸들러가 실제로 설정된 서버 이벤트 이름 집합)를 담는다.
class _SessionHookGate(TypedDict):
    snapshot_id: str
    events: frozenset[str]


# [해설] state에 저장된 PreToolUse 판정을 wrap_tool_call이 쓰기 좋게 풀어 놓은 값. blocked가 있으면 도구를 실행하지 않고 그 메시지를 반환.
@dataclass(slots=True)
class _PreToolOutcome:
    """Pre-execution gate result for the tool-call wrapper."""

    blocked: ToolMessage | None = None
    context: tuple[str, ...] = field(default_factory=tuple)


# [해설] `task`(서브에이전트) 호출 동안 child RunnableConfig metadata에 tool_call_id를 심어, 서브에이전트 트랜스크립트를
# [해설] 부모 호출과 연결할 수 있게 한다(`hooks/transcript.py`의 `SUBAGENT_TRANSCRIPT_ID_METADATA_KEY`). 부작용은 ContextVar 한정이며 finally에서 복원.
@contextmanager
def _subagent_transcript_config(
    call: ToolCallData,
    config: RunnableConfig,
) -> Iterator[None]:
    if call.name != _TASK_TOOL_NAME:
        yield
        return

    from langchain_core.runnables.config import var_child_runnable_config

    from deepagents_code.hooks.transcript import (
        SUBAGENT_TRANSCRIPT_ID_METADATA_KEY,
    )

    metadata = config.get("metadata")
    child_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    child_metadata[SUBAGENT_TRANSCRIPT_ID_METADATA_KEY] = call.id
    child_config: RunnableConfig = {**config, "metadata": child_metadata}
    token = var_child_runnable_config.set(child_config)
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)


# [해설] 서버 소유 훅 이벤트를 interrupt 전송으로 방출하는 미들웨어 본체.
# [해설][흐름] LangChain 미들웨어 훅별 역할:
# [해설] - `after_model`/`aafter_model` → `_after_model`: 마지막 AIMessage의 tool call마다 PreCompact·PreToolUse를 방출, 판정을 state에 저장.
# [해설] - `wrap_tool_call`/`awrap_tool_call`: 저장된 deny면 실행 대신 거부 ToolMessage, task면 SubagentStart, 실행 시간 측정 후 pending 기록.
# [해설] - `before_model`/`abefore_model` → `_before_model`: 체크포인트된 도구 결과에 PostToolUse(Failure)·SubagentStop 방출 후 결과 텍스트 보강.
# [해설] - `after_agent`/`aafter_agent` → `_after_agent`: Stop 방출, 계속하라는 결정이면 `jump_to: "model"`.
# [해설][설계] 동기/비동기 훅이 같은 동기 구현을 공유한다. `interrupt()`는 동기 호출이고 실제 대기는 그래프 중단·재개로 일어나기 때문.
# [해설][주의] interrupt 재개 시 LangGraph는 해당 노드를 처음부터 재실행하고 앞서 답한 interrupt는 저장된 resume 값을 순서대로 돌려준다.
# [해설] 그래서 한 노드 안에서 여러 tool call의 훅을 순차로 부르는 코드가 안전하며, invocation_id가 결정적이어야 클라이언트 장부 중복 제거가 동작한다.
class ServerHooksMiddleware(AgentMiddleware[ServerHooksState, ContextT, ResponseT]):
    """Emit server-owned lifecycle events over the hook interrupt transport."""

    # [해설] 훅 입력(도구 인자 등)이 LangSmith 트레이스에 그대로 실리지 않도록 기본적으로 payload를 생략한다.
    trace_policy = TracePolicy(process_inputs=omit_payload)
    """Omit hook inputs from traces by default; set a `TracePolicy` to override."""

    state_schema = ServerHooksState

    # [해설] 생성자: `agent.py`가 cwd·mcp_tools를 넘겨 생성. `_mcp_servers`는 "도구 이름 → MCP 서버 이름" 사전으로,
    # [해설] after_model/before_model처럼 `BaseTool` 객체가 없는 경로에서 wire 이름(`mcp__srv__tool`) 변환에 쓴다.
    def __init__(
        self,
        *,
        cwd: Path,
        default_deadline: timedelta = _DEFAULT_DEADLINE,
        emit_stop: bool = True,
        mcp_tools: Sequence[BaseTool] = (),
    ) -> None:
        """Initialize middleware.

        Args:
            cwd: Session working directory projected into hook context.
            default_deadline: Client execution deadline attached to requests.
            emit_stop: Whether to emit the main-agent `Stop` event from
                `after_agent`. Subagent graphs set this to `False` so they still
                wrap tools without firing parent `Stop` handlers.
            mcp_tools: MCP tools whose server metadata is needed before tool
                execution for compatible hook projection.
        """
        super().__init__()
        self._cwd = cwd
        self._default_deadline = default_deadline
        self._emit_stop = emit_stop
        # [해설][주의] `_mcp_server_from_tool`은 metadata 키 `mcp_server`/`mcp_server_name`/`server_name`만 본다. 그러나 `mcp_tools.py`는
        # [해설] 도구 metadata에 `_deepagents_code_mcp_server`를 기록한다(이 파일 밖 검색 기준 위 세 키를 쓰는 곳을 찾지 못함).
        # [해설] 따라서 dcode 자체 MCP 도구에 대해서는 이 사전이 비고 wire 이름이 `mcp__srv__tool`이 아닌 `{server}_{tool}` 원형으로 나갈 수 있다 (추정, 실행 미검증).
        self._mcp_servers = {
            name: server
            for tool in mcp_tools
            if (name := getattr(tool, "name", None))
            and isinstance(name, str)
            and (server := _mcp_server_from_tool(tool)) is not None
        }

    # [해설] 모델 호출 직전 = 앞선 도구 실행 결과가 이미 체크포인트된 안전한 경계. 여기서 PostToolUse 계열을 방출한다.
    def before_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Run post-execution hooks after tool results are checkpointed.

        Returns:
            State updates for rewritten results and completed hook bookkeeping.
        """
        return self._before_model(state, runtime)

    async def abefore_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Run async post-execution hooks at the same safe boundary.

        Returns:
            State updates for rewritten results and completed hook bookkeeping.
        """
        return self._before_model(state, runtime)

    # [해설] 모델이 tool call을 낸 직후, 도구 실행·HITL 승인 전에 PreToolUse를 해결한다.
    # [해설][설계] `agent.py` 주석: HITL 미들웨어 "뒤에" append해야 PreToolUse가 승인 라우팅보다 먼저 해결된다.
    # [해설] after_model 계열 훅이 미들웨어 역순으로 실행되기 때문으로 보인다 (추정).
    def after_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any]:
        """Run pre-execution hooks before downstream HITL middleware.

        Returns:
            State update carrying per-tool hook outcomes.
        """
        return self._after_model(state, runtime)

    async def aafter_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any]:
        """Run the async graph path through the same interrupt sequence.

        Returns:
            State update carrying per-tool hook outcomes.
        """
        return self._after_model(state, runtime)

    # [해설] 개별 도구 실행을 감싸는 래퍼(ToolNode 안에서 호출).
    # [해설][흐름] 1) 게이트·호출 데이터·저장된 pre 판정 로드 → 2) deny면 핸들러 호출 없이 거부 메시지 반환 → 3) task면 SubagentStart 왕복
    # [해설][흐름] 4) 실제 도구 실행(시간 측정) → 5) PreToolUse가 준 추가 context를 결과에 덧붙임 → 6) Post 이벤트가 설정돼 있으면 pending 기록.
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Run pre-tool hooks and record synchronous results for post hooks.

        Returns:
            Tool result with checkpointed post-hook bookkeeping when needed.
        """
        gate = _session_gate(request.runtime.context)
        call = _tool_call_data(request)
        pre = _pre_tool_outcome(request.state, call)
        context = _hook_context(
            request.runtime.context, request.runtime.config, self._cwd
        )
        # [해설][흐름] 2) 차단: after_model에서 state에 저장된 deny 판정. 거부 이유 + 훅 context를 담은 error ToolMessage를 반환.
        if pre.blocked is not None:
            return _append_message_text(pre.blocked, pre.context, call.id)
        # [해설][흐름] 3) SubagentStart: 차단되면 ToolMessage, 아니면 description에 훅 context가 주입된 새 request를 받는다.
        started_or_blocked = self._maybe_subagent_start(request, call, context, gate)
        if isinstance(started_or_blocked, ToolMessage):
            return started_or_blocked
        request = started_or_blocked
        # [해설][흐름] 4) 실행 + 소요시간(ms) 측정. task면 트랜스크립트 id를 child config에 심은 채 실행.
        started = time.perf_counter()
        with _subagent_transcript_config(call, request.runtime.config):
            result = handler(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        result = _append_message_text(result, pre.context, call.id)
        # [해설][흐름] 6) PostToolUse는 여기서 바로 방출하지 않는다. 결과를 Command update로 감싸 pending 채널에 기록하고
        # [해설] 다음 before_model(체크포인트 이후)에서 방출 — 도구 결과가 확정 저장되기 전에 interrupt로 멈추면 재개 시 도구가 재실행될 수 있기 때문 (추정).
        if _post_tool_boundary_enabled(gate, call):
            return _record_pending_post_tool(result, call.id, duration_ms)
        return result

    # [해설] `wrap_tool_call`의 async 버전. 흐름은 동일하며 handler만 await한다.
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Run pre-tool hooks and record asynchronous results for post hooks.

        Returns:
            Tool result with checkpointed post-hook bookkeeping when needed.
        """
        gate = _session_gate(request.runtime.context)
        call = _tool_call_data(request)
        pre = _pre_tool_outcome(request.state, call)
        context = _hook_context(
            request.runtime.context, request.runtime.config, self._cwd
        )
        if pre.blocked is not None:
            return _append_message_text(pre.blocked, pre.context, call.id)
        started_or_blocked = self._maybe_subagent_start(request, call, context, gate)
        if isinstance(started_or_blocked, ToolMessage):
            return started_or_blocked
        request = started_or_blocked
        started = time.perf_counter()
        with _subagent_transcript_config(call, request.runtime.config):
            result = await handler(request)
        duration_ms = int((time.perf_counter() - started) * 1000)
        result = _append_message_text(result, pre.context, call.id)
        if _post_tool_boundary_enabled(gate, call):
            return _record_pending_post_tool(result, call.id, duration_ms)
        return result

    # [해설] 에이전트가 자연 종료(더 이상 tool call 없음)에 도달했을 때 Stop 방출. `can_jump_to=["model"]`로 루프 재진입을 허용한다.
    @hook_config(can_jump_to=["model"])
    def after_agent(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Emit `Stop` when the agent reaches a natural end.

        Returns:
            Optional state update that may jump back to the model.
        """
        return self._after_agent(state, runtime)

    # [해설] `after_agent`의 async 버전.
    @hook_config(can_jump_to=["model"])
    async def aafter_agent(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        """Async `Stop` emission; mirrors `after_agent`.

        Returns:
            Optional state update that may jump back to the model.
        """
        return self._after_agent(state, runtime)

    # [해설] `task` 도구 호출이고 SubagentStart가 설정됐을 때만 왕복한다. `wrap_tool_call`/`awrap_tool_call`이 호출.
    # [해설] 결과: continue_processing=False면 거부 ToolMessage(서브에이전트 미실행), context가 있으면 `_inject_subagent_start_context`로 description 앞에 삽입.
    # [해설] agent identity id는 tool_call_id라서 invocation_id가 호출마다 고유하고 재생 시에는 동일하다.
    def _maybe_subagent_start(
        self,
        request: ToolCallRequest,
        call: ToolCallData,
        context: HookContext,
        gate: _SessionHookGate | None,
    ) -> ToolCallRequest | ToolMessage:
        if call.name != _TASK_TOOL_NAME or not _event_enabled(
            gate, HookEvent.SUBAGENT_START
        ):
            return request
        agent = _task_agent_identity(call)
        decision = _invoke_hook(
            context,
            SubagentStartEvent(event=HookEvent.SUBAGENT_START, agent=agent),
            gate=gate,
            config=request.runtime.config,
            deadline=self._default_deadline,
        )
        decision = _require_decision(decision, SubagentStartDecision)
        if not decision.continue_processing:
            return _denied_tool_message(
                call,
                PermissionEffect(
                    behavior="deny",
                    reason=decision.stop_reason or "Blocked by SubagentStart hook",
                ),
            )
        return _inject_subagent_start_context(request, decision)

    # [해설] PostToolUse / PostToolUseFailure / SubagentStop 방출 구현. `before_model`/`abefore_model`이 호출.
    # [해설][흐름] 1) pending 없으면 즉시 종료(왕복 없음) → 2) 모든 pending을 제거용 tombstone으로 준비 → 3) 마지막 tool-call AIMessage 뒤의 ToolMessage 수집
    # [해설][흐름] 4) tool call 순서대로 Post 훅·SubagentStop 왕복 → 5) 보강된 ToolMessage(같은 id라 add_messages가 교체)와 tombstone을 반환.
    def _before_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        pending = _pending_post_tools(state)
        if not pending:
            return None
        # Construct a new _PendingPostToolState where `duration_ms` is None
        # for each entry. This causes the pending state to be evicted during
        # graph state reconciliation in _merge_pending_post_tools
        completed: _PendingPostToolState = dict.fromkeys(pending)
        # [해설][흐름] 3) 최신 tool-call AIMessage 이후의 ToolMessage만 이번 배치의 결과로 본다.
        messages = state.get("messages", ())
        latest_call_message = _latest_tool_call_message(messages)
        # If there is an extant _PendingPostToolState but no corresponding
        # tool message, mark the _PendingPostToolState as resolved.
        if latest_call_message is None:
            return {_PENDING_POST_TOOL_STATE_KEY: completed}
        message_index, ai_message = latest_call_message
        results = {
            message.tool_call_id: message
            for message in messages[message_index + 1 :]
            if isinstance(message, ToolMessage)
        }
        # [해설] before_model은 Runtime만 받으므로 `_runtime_hook_config`로 run_id/thread_id를 복원해 요청 run_id에 쓴다.
        gate = _session_gate(runtime.context)
        config = _runtime_hook_config(runtime)
        context = _hook_context(runtime.context, config, self._cwd)
        updates: list[ToolMessage] = []
        # [해설][흐름] 4) tool call 순서를 유지하며 순차 왕복 — 노드 재실행 시 interrupt 순서가 같아야 resume 값이 올바르게 매칭된다.
        for tool_call in ai_message.tool_calls:
            call = _tool_call_data_from_call(
                tool_call,
                mcp_server=self._mcp_servers.get(str(tool_call.get("name") or "")),
            )
            duration_ms = pending.get(call.id)
            result = results.get(call.id)
            if duration_ms is None or result is None:
                # This pending entry has already been consumed, continue
                continue
            updated = self._maybe_post_tool_use(
                call,
                context,
                gate,
                config,
                result,
                duration_ms,
            )
            updated = self._maybe_subagent_stop(
                call,
                context,
                gate,
                config,
                updated,
            )
            # [해설][주의] Post 훅은 결과를 "덧붙이기"만 해야 한다. messages 채널에 ToolMessage로 쓰기 때문에 Command로 바뀌면 불변식 위반.
            if not isinstance(updated, ToolMessage):
                msg = "Post-tool hooks must preserve committed ToolMessage results"
                raise TypeError(msg)
            updates.append(updated)
        # [해설][흐름] 5) 메시지 교체는 id가 같은 ToolMessage를 다시 넣는 방식(`model_copy`로 id 유지) — add_messages reducer가 덮어쓴다.
        state_update: dict[str, Any] = {
            _PENDING_POST_TOOL_STATE_KEY: completed,
        }
        if updates:
            state_update["messages"] = updates
        return state_update

    # [해설] PreCompact·PreToolUse 방출 구현. `after_model`/`aafter_model`이 호출.
    # [해설][흐름] 1) 두 이벤트 모두 미설정이면 빈 outcomes로 이전 턴 판정을 지우고 종료(왕복 없음) → 2) 마지막 AIMessage의 tool call 순회
    # [해설][흐름] 3) compact 도구면 PreCompact(차단 시 deny 저장 후 PreToolUse 생략) → 4) PreToolUse 결정 해석(deny/ask/allow)
    # [해설][흐름] 5) 호출별 outcome을 모아 `_hooks_pre_tool_outcomes` 전체를 교체 저장.
    # [해설] 반환 state는 `hook_decided_permission`(agent.py 승인 흐름)과 `hook_permission_behavior`(auto_mode.py)가 읽는다.
    def _after_model(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any]:
        gate = _session_gate(runtime.context)
        precompact_enabled = _event_enabled(gate, HookEvent.PRE_COMPACT)
        pretool_enabled = _event_enabled(gate, HookEvent.PRE_TOOL_USE)
        if not precompact_enabled and not pretool_enabled:
            return {_PRE_TOOL_STATE_KEY: {}}
        message = _last_ai_message(state.get("messages", ()))
        if message is None:
            return {_PRE_TOOL_STATE_KEY: {}}
        # [해설] config=None → `_run_id`가 thread_id로 대체된다(after_model에서는 run config를 넘기지 않음).
        context = _hook_context(runtime.context, None, self._cwd)
        outcomes: dict[str, _PreToolState] = {}
        for tool_call in message.tool_calls:
            call = _tool_call_data_from_call(
                tool_call,
                mcp_server=self._mcp_servers.get(str(tool_call.get("name") or "")),
            )
            behavior: PreToolBehavior = "none"
            reason: str | None = None
            hook_context: list[str] = []
            # [해설][흐름] 3) PreCompact: `compact_conversation`의 `force=True`면 MANUAL(`/compact` 사용자 요청), 아니면 AUTO로 본다.
            # [해설] PreCompact 이벤트 자체에는 도구 호출 id가 없으므로 `logical_event_id=call.id`로 invocation_id를 안정화한다.
            if precompact_enabled and call.name == _COMPACT_TOOL_NAME:
                trigger = (
                    CompactTrigger.MANUAL
                    if call.args.get("force") is True
                    else CompactTrigger.AUTO
                )
                compact = _invoke_hook(
                    context,
                    PreCompactEvent(event=HookEvent.PRE_COMPACT, trigger=trigger),
                    gate=gate,
                    config=None,
                    deadline=self._default_deadline,
                    logical_event_id=call.id,
                )
                compact = _require_decision(compact, PreCompactDecision)
                if not compact.continue_processing:
                    outcomes[call.id] = {
                        "behavior": "deny",
                        "reason": compact.stop_reason or "Blocked by PreCompact hook",
                        "context": hook_context,
                    }
                    continue
            # [해설][흐름] 4) PreToolUse: reducer가 합친 결정(deny > ask > allow, `hooks/reducer.py`)을 해석.
            # [해설] continue=false(stopReason)도 deny로 취급하며, 훅이 준 context는 behavior와 무관하게 결과 뒤에 붙도록 누적한다.
            if pretool_enabled:
                decision = _invoke_hook(
                    context,
                    PreToolUseEvent(event=HookEvent.PRE_TOOL_USE, call=call),
                    gate=gate,
                    config=None,
                    deadline=self._default_deadline,
                )
                decision = _require_decision(decision, PreToolUseDecision)
                permission = decision.permission
                hook_context.extend(decision.context)
                if not decision.continue_processing or permission.behavior == "deny":
                    behavior = "deny"
                    reason = (
                        permission.reason
                        or decision.stop_reason
                        or _DEFAULT_DENY_REASON
                    )
                # [해설] ask: 같은 노드 안에서 곧바로 두 번째 interrupt(HITL 승인 요청)를 띄워 사용자에게 묻는다. 승인하면 allow로 확정 →
                # [해설] stock 승인 미들웨어가 다시 묻지 않는다(`hook_decided_permission`).
                elif permission.behavior == "ask":
                    blocked = _ask_permission_via_hitl(call, permission)
                    if blocked is None:
                        behavior = "allow"
                    else:
                        behavior = "deny"
                        blocked_content = blocked.content
                        reason = (
                            blocked_content
                            if isinstance(blocked_content, str)
                            else str(blocked_content)
                        )
                elif permission.behavior == "allow":
                    behavior = "allow"
            # [해설][흐름] 5) outcome 저장. "none"(의견 없음)도 기록되지만 `hook_permission_behavior`는 None으로 취급해 일반 승인이 그대로 적용된다.
            if behavior == "deny":
                outcomes[call.id] = {
                    "behavior": "deny",
                    # Every deny path above resolves a reason; the guard keeps the
                    # "a denial always explains itself" invariant checkable here.
                    "reason": reason if reason is not None else _DEFAULT_DENY_REASON,
                    "context": hook_context,
                }
            else:
                outcomes[call.id] = {
                    "behavior": behavior,
                    "context": hook_context,
                }
        return {_PRE_TOOL_STATE_KEY: outcomes}

    # [해설] 실행된 도구 1건에 대해 PostToolUse 또는 PostToolUseFailure를 방출하고 결정의 feedback/context/stopReason을 결과 텍스트 뒤에 붙인다.
    # [해설] 실패 판정은 `_tool_result_error`(status=="error" 또는 execute의 비0 exit_code). 해당 이벤트가 미설정이면 왕복 없이 원본 반환.
    # [해설][주의] 도구 결과를 "대체"하지는 않는다 — `updatedToolOutput`류는 적용되지 않는다(analysis/07 문서 약속 참고).
    def _maybe_post_tool_use(
        self,
        call: ToolCallData,
        context: HookContext,
        gate: _SessionHookGate | None,
        config: Mapping[str, Any] | None,
        result: ToolMessage | Command[Any],
        duration_ms: int,
    ) -> ToolMessage | Command[Any]:
        error = _tool_result_error(result, call)
        event = (
            HookEvent.POST_TOOL_USE_FAILURE
            if error is not None
            else HookEvent.POST_TOOL_USE
        )
        if not _event_enabled(gate, event):
            return result
        if error is not None:
            hook_event = PostToolUseFailureEvent(
                event=HookEvent.POST_TOOL_USE_FAILURE,
                call=call,
                error=error,
                duration_ms=duration_ms,
            )
            decision_type = PostToolUseFailureDecision
        else:
            hook_event = PostToolUseEvent.from_tool_result(
                result,
                call=call,
                duration_ms=duration_ms,
            )
            decision_type = PostToolUseDecision
        decision = _require_decision(
            _invoke_hook(
                context,
                hook_event,
                gate=gate,
                config=config,
                deadline=self._default_deadline,
            ),
            decision_type,
        )
        return _apply_post_tool_use(result, decision, call.id)

    # [해설] task 도구 결과에 대해 SubagentStop 방출. continuation_count는 항상 0이며, block 결정은 reducer에서 context로 강등되어
    # [해설] 여기서는 context만 결과에 덧붙인다(`_apply_subagent_stop`). 즉 서브에이전트를 다시 돌리는 기능은 없다.
    def _maybe_subagent_stop(
        self,
        call: ToolCallData,
        context: HookContext,
        gate: _SessionHookGate | None,
        config: Mapping[str, Any] | None,
        result: ToolMessage | Command[Any],
    ) -> ToolMessage | Command[Any]:
        if call.name != _TASK_TOOL_NAME or not _event_enabled(
            gate, HookEvent.SUBAGENT_STOP
        ):
            return result
        agent = _task_agent_identity(call)
        decision = _invoke_hook(
            context,
            SubagentStopEvent(
                event=HookEvent.SUBAGENT_STOP,
                agent=agent,
                continuation_count=0,
                last_assistant_message=_tool_result_text(result, call.id),
            ),
            gate=gate,
            config=config,
            deadline=self._default_deadline,
        )
        decision = _require_decision(decision, SubagentStopDecision)
        return _apply_subagent_stop(result, decision, call.id)

    # [해설] Stop 방출 구현. `after_agent`/`aafter_agent`가 호출.
    # [해설][흐름] 1) 서브에이전트 인스턴스(`emit_stop=False`) 또는 미설정이면 종료 → 2) 현재 연속 횟수와 마지막 assistant 텍스트로 Stop 왕복
    # [해설][흐름] 3) 계속 안 함 → 카운터 리셋 / 계속 → feedback을 HumanMessage로 넣고 model로 점프, 카운터+1.
    # [해설] 연속 상한(`MAX_STOP_CONTINUATIONS=8`)은 여기가 아니라 `hooks/reducer.py`가 continuation_count를 보고 강제한다.
    def _after_agent(
        self,
        state: ServerHooksState,
        runtime: Runtime[ContextT],
    ) -> dict[str, Any] | None:
        if not self._emit_stop:
            return None
        gate = _session_gate(runtime.context)
        if not _event_enabled(gate, HookEvent.STOP):
            return None
        continuation = int(state.get(_STOP_STATE_KEY, 0) or 0)
        context = _hook_context(runtime.context, None, self._cwd)
        # [해설] Stop의 invocation_id는 (continuation_count, 마지막 메시지 sha256)로 정해진다 → 루프가 돌 때마다 다른 요청, 재생 시엔 같은 요청.
        decision = _invoke_hook(
            context,
            StopEvent(
                event=HookEvent.STOP,
                continuation_count=continuation,
                last_assistant_message=_last_assistant_text(state.get("messages", ())),
            ),
            gate=gate,
            config=None,
            deadline=self._default_deadline,
        )
        decision = _require_decision(decision, StopDecision)
        if not decision.continue_processing or not decision.continue_loop:
            # Reset so a later independent turn does not inherit the count.
            if continuation:
                return {_STOP_STATE_KEY: 0}
            return None
        # [해설][흐름] 3) 루프 계속: feedback이 없으면 stopReason, 그것도 없으면 기본 문구 "Continue working."을 사용자 메시지로 주입.
        feedback = "\n".join(decision.feedback).strip() or (
            decision.stop_reason or "Continue working."
        )
        return {
            "messages": [HumanMessage(content=feedback)],
            "jump_to": "model",
            _STOP_STATE_KEY: continuation + 1,
        }


# [해설] 이벤트별로 기대한 decision 타입인지 확인. 클라이언트가 다른 타입을 돌려주면 프로토콜 위반으로 TypeError.
def _require_decision[DecisionT: BaseHookDecision](
    decision: HookDecision,
    expected: type[DecisionT],
) -> DecisionT:
    if not isinstance(decision, expected):
        msg = f"Expected {expected.__name__}, got {type(decision).__name__}"
        raise TypeError(msg)
    return decision


# [해설] 턴 context에서 `hooks_snapshot_id`와 `hooks_server_events`를 읽어 게이트를 만든다.
# [해설] 두 값은 클라이언트가 턴마다 `hooks/context.py`의 `apply_hooks_context`로 채운다. 하나라도 비면 None → 모든 서버 이벤트 비활성(왕복 0회).
def _session_gate(runtime_context: object) -> _SessionHookGate | None:
    fields = _context_mapping(runtime_context)
    snapshot_id = fields.get("hooks_snapshot_id")
    events = fields.get("hooks_server_events")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return None
    if not isinstance(events, list) or not events:
        return None
    return {
        "snapshot_id": snapshot_id,
        "events": frozenset(str(item) for item in events),
    }


# [해설] "핸들러가 없으면 왕복도 없다": 게이트에 이벤트 이름이 있을 때만 interrupt를 낸다.
def _event_enabled(gate: _SessionHookGate | None, event: HookEvent) -> bool:
    return gate is not None and event.value in gate["events"]


# [해설] wrap_tool_call이 결과를 pending 채널에 기록할지 결정. Post/PostFailure가 설정됐거나, task 호출이면서 SubagentStop이 설정된 경우.
def _post_tool_boundary_enabled(
    gate: _SessionHookGate | None,
    call: ToolCallData,
) -> bool:
    return (
        _event_enabled(gate, HookEvent.POST_TOOL_USE)
        or _event_enabled(gate, HookEvent.POST_TOOL_USE_FAILURE)
        or (
            call.name == _TASK_TOOL_NAME
            and _event_enabled(gate, HookEvent.SUBAGENT_STOP)
        )
    )


# [해설] pending 채널에서 실제 duration(int)만 추린다. tombstone(None)과 bool(파이썬에서 int 하위형)은 제외.
def _pending_post_tools(state: ServerHooksState) -> dict[str, int]:
    raw = state.get(_PENDING_POST_TOOL_STATE_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(call_id): duration_ms
        for call_id, duration_ms in raw.items()
        if isinstance(duration_ms, int) and not isinstance(duration_ms, bool)
    }


# [해설] 공개 API: 훅이 allow/deny를 명시했는지. `agent.py`의 승인 흐름이 호출해 중복 승인 프롬프트를 건너뛴다.
def hook_decided_permission(state: object, tool_call_id: str) -> bool:
    """Report whether a pre-execution hook already settled permission for a call.

    Args:
        state: Agent state carrying the current turn's hook outcomes.
        tool_call_id: Tool call to look up.

    Returns:
        `True` when a hook explicitly allowed or denied the call, so stock
        approval flows must not prompt again. `False` when no hook ran, the hook
        expressed no opinion, or no outcome was recorded -- in every one of those
        cases normal approval still applies.
    """
    return hook_permission_behavior(state, tool_call_id) is not None


# [해설] 공개 API: 명시적 allow/deny 값을 반환. `auto_mode.py`가 호출해 분류기 판단보다 훅 결정을 우선시하는 데 쓴다 (추정: 호출부 세부 미확인).
def hook_permission_behavior(
    state: object, tool_call_id: str
) -> Literal["allow", "deny"] | None:
    """Return the explicit pre-execution hook permission for a call.

    Args:
        state: Agent state carrying the current turn's hook outcomes.
        tool_call_id: Tool call to look up.

    Returns:
        The hook's explicit `allow` or `deny`, or `None` when normal approval
            routing still decides permission.
    """
    outcome = _pre_tool_state(state, tool_call_id)
    if outcome is None:
        return None
    behavior = outcome.get("behavior")
    if behavior == "allow":
        return "allow"
    if behavior == "deny":
        return "deny"
    return None


# [해설] state의 `_hooks_pre_tool_outcomes`에서 tool_call_id 항목을 방어적으로 꺼낸다(state는 dict 또는 Mapping, 형태 검증 포함).
def _pre_tool_state(state: object, tool_call_id: str) -> Mapping[str, object] | None:
    if not isinstance(state, Mapping):
        return None
    raw = state.get(_PRE_TOOL_STATE_KEY)
    if not isinstance(raw, Mapping):
        return None
    outcome = raw.get(tool_call_id)
    if not isinstance(outcome, Mapping):
        return None
    return {str(key): value for key, value in outcome.items()}


# [해설] 저장된 판정을 `_PreToolOutcome`으로 변환. deny면 거부 ToolMessage를 미리 만들어 둔다. wrap_tool_call이 호출.
def _pre_tool_outcome(state: object, call: ToolCallData) -> _PreToolOutcome:
    outcome = _pre_tool_state(state, call.id)
    if outcome is None:
        return _PreToolOutcome()
    raw_context = outcome.get("context")
    context = (
        tuple(item for item in raw_context if isinstance(item, str))
        if isinstance(raw_context, Sequence) and not isinstance(raw_context, str)
        else ()
    )
    if outcome.get("behavior") != "deny":
        return _PreToolOutcome(context=context)
    raw_reason = outcome.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else None
    return _PreToolOutcome(
        blocked=_denied_tool_message(
            call,
            PermissionEffect(behavior="deny", reason=reason),
        ),
        context=context,
    )


# [해설] 모든 서버 이벤트 방출의 단일 관문 — 이 모듈의 왕복 핵심.
# [해설][흐름] 1) 게이트 필수 확인 → 2) run_id·결정적 invocation_id 계산 → 3) `HookInvocationRequest`(protocol v1, snapshot_id, deadline) 구성
# [해설][흐름] 4) 그래프 모드: `interrupt(payload)` / operation 모드: 응답 맵에서 조회, 없으면 `HookTransportInterruptError`
# [해설][흐름] 5) resume 값을 `parse_hook_resume_value`로 검증(id·snapshot 일치) → `HookDecision` 반환.
def _invoke_hook(
    context: HookContext,
    event: (
        PreToolUseEvent
        | PostToolUseEvent
        | PostToolUseFailureEvent
        | PreCompactEvent
        | StopEvent
        | SubagentStartEvent
        | SubagentStopEvent
    ),
    *,
    gate: _SessionHookGate | None,
    config: Mapping[str, Any] | None,
    deadline: timedelta,
    logical_event_id: str | None = None,
) -> HookDecision:
    if gate is None:
        msg = "hooks_snapshot_id is required to emit server-owned hook events"
        raise RuntimeError(msg)
    # [해설][흐름] 2) invocation_id = uuid5(thread, snapshot, prompt_id, event, 논리 이벤트 id). 같은 턴의 같은 논리 이벤트는 항상 같은 id.
    run_id = _run_id(config, context.thread_id)
    invocation_id = _invocation_id(
        snapshot_id=gate["snapshot_id"],
        context=context,
        event=event,
        logical_event_id=logical_event_id,
    )
    # [해설][흐름] 3) 요청 구성. deadline은 절대 시각(UTC)으로 클라이언트에 전달된다.
    request = HookInvocationRequest(
        protocol_version=1,
        invocation_id=invocation_id,
        snapshot_id=gate["snapshot_id"],
        run_id=run_id,
        invocation=HookInvocation(context=context, event=event),
        deadline=datetime.now(UTC) + deadline,
    )
    operation_responses = _HOOK_RESPONSES.get()
    # [해설][흐름] 4) 그래프 모드: 첫 실행이면 여기서 GraphInterrupt가 발생해 그래프가 멈추고 payload가 스트림으로 클라이언트에 간다.
    # [해설] 클라이언트가 `Command(resume=...)`로 재개하면 노드가 재실행되고 이번엔 같은 줄의 `interrupt()`가 resume 값을 즉시 반환한다.
    if operation_responses is None:
        raw = interrupt(build_hook_interrupt_payload(request))
    else:
        # Operation mode: `interrupt()` is unusable outside a Pregel task, so a
        # request the client has not answered yet is raised out to the HTTP
        # boundary instead. Because the operation re-executes from the top on
        # every resume round, an already-answered invocation is replayed from
        # this mapping rather than re-invoked -- that is what makes an operation
        # with several hooks terminate instead of looping forever.
        key = str(request.invocation_id)
        if key not in operation_responses:
            raise HookTransportInterruptError(request)
        raw = operation_responses[key]
    # [해설][흐름] 5) resume 검증. 형태 오류(ValidationError)만 "결정 없음"으로 강등하고, id 불일치(ValueError)는 치명적으로 전파.
    try:
        response = parse_hook_resume_value(
            raw,
            invocation_id=request.invocation_id,
            snapshot_id=request.snapshot_id,
        )
    except ValidationError:
        # Only shape errors degrade to a neutral decision. A plain `ValueError`
        # means the client answered a different request, so it stays fatal.
        #
        # Log it too: the diagnostic is only rendered by the client-side hook
        # presenter, and the offload operation reads just the pre-tool channel
        # from this update, so on that path the diagnostic is dropped and the
        # hook is silently ignored.
        logger.warning(
            "Malformed hook resume value for invocation %s; treating it as no decision",
            request.invocation_id,
            exc_info=True,
        )
        diagnostic = HookDiagnostic(
            code="invalid_resume",
            severity="warning",
            message="Malformed hook resume value; treating it as no decision",
        )
        return reduce_hook_results(request.invocation, (), diagnostics=(diagnostic,))
    return response.decision


# [해설] 훅 wire payload의 공통 context(thread_id, cwd, prompt_id, approval_mode)를 구성. approval_mode 값이 이상하면 MANUAL로 대체.
def _hook_context(
    runtime_context: object,
    config: Mapping[str, Any] | None,
    cwd: Path,
) -> HookContext:
    fields = _context_mapping(runtime_context)
    thread_id = fields.get("thread_id") or _config_thread_id(config) or "unknown"
    if not isinstance(thread_id, str):
        thread_id = "unknown"
    approval = coerce_approval_mode(fields.get("approval_mode", "manual"))
    prompt_raw = fields.get("prompt_id")
    prompt_id = UUID(prompt_raw) if isinstance(prompt_raw, str) and prompt_raw else None
    return HookContext(
        thread_id=thread_id,
        cwd=cwd,
        prompt_id=prompt_id,
        approval_mode=(
            approval if isinstance(approval, ApprovalMode) else ApprovalMode.MANUAL
        ),
    )


# [해설] in-process(dataclass `CLIContextSchema`)와 RemoteGraph(plain mapping) 두 형태의 run context를 dict로 통일.
def _context_mapping(runtime_context: object) -> dict[str, Any]:
    """Project LangGraph run context (dataclass or mapping) into a plain dict.

    In-process graphs coerce `context=` into `CLIContextSchema`; RemoteGraph
    delivers a plain mapping. Both shapes are accepted here.

    Returns:
        A shallow string-keyed dict of the hook-relevant context fields.
    """
    if runtime_context is None:
        return {}
    if isinstance(runtime_context, Mapping):
        return {str(key): value for key, value in runtime_context.items()}
    result: dict[str, Any] = {}
    for key in (
        "hooks_snapshot_id",
        "hooks_server_events",
        "thread_id",
        "approval_mode",
        "prompt_id",
    ):
        value = getattr(runtime_context, key, None)
        if value is not None:
            result[key] = value
    return result


# [해설] Runtime.execution_info에서 run_id/thread_id를 꺼내 RunnableConfig 모양으로 만든다. `_before_model`이 사용.
def _runtime_hook_config(runtime: Runtime[Any]) -> dict[str, Any] | None:
    info = runtime.execution_info
    if info is None:
        return None
    configurable = {
        key: value
        for key, value in (
            ("run_id", info.run_id),
            ("thread_id", info.thread_id),
        )
        if value
    }
    return {"configurable": configurable} if configurable else None


# [해설] 요청의 run_id 결정: configurable.run_id → thread_id → context의 thread_id 순으로 대체.
def _run_id(config: Mapping[str, Any] | None, thread_id: str) -> str:
    if isinstance(config, Mapping):
        configurable = config.get("configurable")
        if isinstance(configurable, Mapping):
            for key in ("run_id", "thread_id"):
                value = configurable.get(key)
                if isinstance(value, UUID):
                    return str(value)
                if isinstance(value, str) and value:
                    return value
    return thread_id


# [해설] 결정적 invocation_id 생성. JSON을 sort_keys로 직렬화해 dict 순서와 무관하게 같은 UUID가 나오게 한다.
# [해설][주의] `offload_middleware.py` docstring: operation 모드에서는 checkpoint_ns(operation_id 기반)가 이 id의 안정성·고유성을 보장하는 불변식의 소유자다.
def _invocation_id(
    *,
    snapshot_id: str,
    context: HookContext,
    event: (
        PreToolUseEvent
        | PostToolUseEvent
        | PostToolUseFailureEvent
        | PreCompactEvent
        | StopEvent
        | SubagentStartEvent
        | SubagentStopEvent
    ),
    logical_event_id: str | None = None,
) -> UUID:
    identity = {
        "thread_id": context.thread_id,
        "snapshot_id": snapshot_id,
        "prompt_id": str(context.prompt_id) if context.prompt_id is not None else "",
        "event": event.event.value,
        "logical_event": _logical_event_identity(
            event,
            logical_event_id=logical_event_id,
        ),
    }
    return uuid5(
        _INVOCATION_NAMESPACE,
        json.dumps(identity, sort_keys=True, separators=(",", ":")),
    )


# [해설] 이벤트 종류별 "논리적 동일성" 키:
# [해설] - Pre/Post/PostFailure: tool_call_id / PreCompact: 호출자가 준 id(없으면 ValueError)
# [해설] - SubagentStart: agent.id(=tool_call_id) / SubagentStop: `agent.id:continuation` / Stop: `continuation:sha256(마지막 메시지)`.
def _logical_event_identity(
    event: (
        PreToolUseEvent
        | PostToolUseEvent
        | PostToolUseFailureEvent
        | PreCompactEvent
        | StopEvent
        | SubagentStartEvent
        | SubagentStopEvent
    ),
    *,
    logical_event_id: str | None = None,
) -> str:
    if isinstance(
        event,
        PreToolUseEvent | PostToolUseEvent | PostToolUseFailureEvent,
    ):
        return event.call.id
    if isinstance(event, PreCompactEvent):
        if logical_event_id:
            return logical_event_id
        msg = "PreCompact requires a stable tool-call identity"
        raise ValueError(msg)
    if isinstance(event, SubagentStartEvent):
        return event.agent.id
    if isinstance(event, SubagentStopEvent):
        return f"{event.agent.id}:{event.continuation_count}"
    message_hash = hashlib.sha256(event.last_assistant_message.encode()).hexdigest()
    return f"{event.continuation_count}:{message_hash}"


# [해설] config.configurable.thread_id 추출 헬퍼(`_hook_context` fallback).
def _config_thread_id(config: Mapping[str, Any] | None) -> str | None:
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get("thread_id")
    return value if isinstance(value, str) and value else None


# [해설] wrap_tool_call 경로: `ToolCallRequest`에 BaseTool 객체가 있으므로 도구 metadata에서 MCP 서버 이름을 직접 찾는다.
def _tool_call_data(request: ToolCallRequest) -> ToolCallData:
    return _tool_call_data_from_call(
        request.tool_call,
        mcp_server=_mcp_server_from_tool(request.tool),
    )


# [해설] LangChain ToolCall dict → 도메인 `ToolCallData`. 인자가 dict가 아니면 빈 객체로 방어.
def _tool_call_data_from_call(
    tool_call: Mapping[str, object],
    *,
    mcp_server: str | None,
) -> ToolCallData:
    raw_args = tool_call.get("args")
    args: dict[str, Any]
    if isinstance(raw_args, dict):
        args = {str(key): value for key, value in raw_args.items()}
    else:
        args = {}
    return ToolCallData(
        id=str(tool_call.get("id") or ""),
        name=str(tool_call.get("name") or ""),
        args=cast("JsonObject", args),
        mcp_server=mcp_server,
    )


# [해설] 도구 metadata에서 MCP 서버 이름을 찾는다. 이 값이 있으면 `hooks/tools.py`의 `to_wire_tool_name`이 `mcp__{server}__{tool}` 형태로 매핑.
# [해설][주의] `mcp_tools.py`가 기록하는 키는 `_deepagents_code_mcp_server`로, 아래 튜플에 없다(위 `__init__` 주석 참고, 추정).
def _mcp_server_from_tool(tool: object | None) -> str | None:
    if tool is None:
        return None
    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    for key in ("mcp_server", "mcp_server_name", "server_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# [해설] 훅 거부를 표현하는 error ToolMessage. 모델은 이 텍스트를 도구 결과로 보고 다음 행동을 정한다(도구는 실행되지 않음).
def _denied_tool_message(
    call: ToolCallData,
    permission: PermissionEffect,
) -> ToolMessage:
    reason = permission.reason or "Blocked by PreToolUse hook"
    wire_name = to_wire_tool_name(call.name, mcp_server=call.mcp_server)
    return ToolMessage(
        content=f"{wire_name} blocked by hook: {reason}",
        name=call.name,
        tool_call_id=call.id,
        status="error",
    )


# [해설] PreToolUse `ask`를 기존 HITL interrupt 채널(LangChain `HITLRequest`)로 승격해 사용자 승인을 받는다.
# [해설] 클라이언트의 일반 승인 UI가 이 요청을 처리하며, approve/reject 두 가지만 허용한다.
def _ask_permission_via_hitl(
    call: ToolCallData,
    permission: PermissionEffect,
) -> ToolMessage | None:
    """Escalate PreToolUse `ask` through the existing HITL interrupt channel.

    Returns:
        A deny ToolMessage when the user rejects, otherwise `None` to proceed.
    """
    if _in_server_operation():
        # `interrupt()` is only usable inside a Pregel task: it reaches into the
        # run's scratchpad, which a server operation's fabricated config has no
        # equivalent of. Deny with an actionable reason instead of raising a
        # `KeyError` on an internal LangGraph config key. The operation
        # transport carries hook *invocations*, not HITL review requests, so
        # there is no channel to prompt the user on here.
        return _denied_tool_message(
            call,
            PermissionEffect(
                behavior="deny",
                reason=(
                    f"PreToolUse returned `ask` for {call.name}, which cannot "
                    "prompt for approval during a server-side operation such as "
                    "/offload. Return `allow` or `deny` for this tool instead."
                ),
            ),
        )
    # [해설] 그래프 모드: 훅 interrupt와 별개인 두 번째 interrupt. 재실행 시에도 같은 순서로 호출되므로 resume 매칭이 유지된다.
    description = permission.reason or "PreToolUse hook requested approval"
    response = interrupt(
        HITLRequest(
            action_requests=[
                ActionRequest(
                    name=call.name,
                    args=dict(call.args),
                    description=description,
                )
            ],
            review_configs=[
                ReviewConfig(
                    action_name=call.name,
                    allowed_decisions=["approve", "reject"],
                )
            ],
        )
    )
    decisions: Sequence[Any]
    if isinstance(response, Mapping):
        raw = response.get("decisions", ())
        decisions = raw if isinstance(raw, Sequence) else ()
    else:
        decisions = ()
    # [해설] 응답이 비었거나 approve가 아니면 deny(fail-closed). reject 메시지가 있으면 그것을 거부 이유로 쓴다.
    if not decisions:
        return _denied_tool_message(
            call,
            PermissionEffect(
                behavior="deny",
                reason="PreToolUse ask was not answered",
            ),
        )
    first = decisions[0]
    decision_type = first.get("type") if isinstance(first, Mapping) else None
    if decision_type != "approve":
        reject_message = None
        if isinstance(first, Mapping):
            raw_message = first.get("message")
            if isinstance(raw_message, str) and raw_message:
                reject_message = raw_message
        return _denied_tool_message(
            call,
            PermissionEffect(
                behavior="deny",
                reason=reject_message or description,
            ),
        )
    return None


# [해설] 도구 결과를 pending 채널 기록과 함께 `Command(update=...)`로 감싼다. 원래 Command면 기존 update에 병합.
# [해설][주의] update가 Mapping이 아닌 Command는 기록 없이 그대로 반환 → 그 호출의 Post 훅은 방출되지 않는다.
def _record_pending_post_tool(
    result: ToolMessage | Command[Any],
    call_id: str,
    duration_ms: int,
) -> Command[Any]:
    pending = {_PENDING_POST_TOOL_STATE_KEY: {call_id: duration_ms}}
    if isinstance(result, ToolMessage):
        return Command(update={"messages": [result], **pending})
    update = result.update
    if not isinstance(update, Mapping):
        return result
    return replace(result, update={**update, **pending})


# [해설] PreToolUse가 준 추가 context 문자열들을 도구 결과 텍스트 뒤에 덧붙인다.
def _append_message_text(
    result: ToolMessage | Command[Any],
    parts: Sequence[str],
    call_id: str,
) -> ToolMessage | Command[Any]:
    if not parts:
        return result
    return _append_tool_result_text(result, "\n".join(parts), call_id)


# [해설] Post(Failure) 결정의 feedback·context·(중단 시)stopReason을 결과 뒤에 덧붙인다. 결과 교체나 에이전트 중단은 하지 않는다.
def _apply_post_tool_use(
    result: ToolMessage | Command[Any],
    decision: PostToolUseDecision | PostToolUseFailureDecision,
    call_id: str,
) -> ToolMessage | Command[Any]:
    extras: list[str] = []
    if decision.feedback:
        extras.append("\n".join(decision.feedback))
    if decision.context:
        extras.append("\n".join(decision.context))
    if decision.stop_reason and not decision.continue_processing:
        extras.append(decision.stop_reason)
    if not extras:
        return result
    return _append_tool_result_text(
        result,
        "\n\n".join(part for part in extras if part),
        call_id,
    )


# [해설] SubagentStop 결정은 context만 task 결과에 덧붙인다.
def _apply_subagent_stop(
    result: ToolMessage | Command[Any],
    decision: SubagentStopDecision,
    call_id: str,
) -> ToolMessage | Command[Any]:
    if not decision.context:
        return result
    return _append_tool_result_text(result, "\n".join(decision.context), call_id)


# [해설] ToolMessage면 직접, Command면 update.messages 안에서 이 call_id의 ToolMessage만 찾아 텍스트를 덧붙인다.
def _append_tool_result_text(
    result: ToolMessage | Command[Any],
    suffix: str,
    call_id: str,
) -> ToolMessage | Command[Any]:
    if isinstance(result, ToolMessage):
        return _merge_tool_message_content(result, suffix)
    update = result.update
    if not isinstance(update, Mapping):
        return result
    changed = False
    messages: list[object] = []
    for message in _command_messages(result):
        if _is_call_result(message, call_id):
            messages.append(_merge_tool_message_content(message, suffix))
            changed = True
        else:
            messages.append(message)
    if not changed:
        return result
    return replace(result, update={**update, "messages": messages})


# [해설] 실패 판정: ToolMessage.status=="error"면 그 텍스트, `execute` 도구는 artifact.exit_code가 0이 아니면 실패로 본다
# [해설] (셸 명령은 비0 종료여도 status가 success일 수 있으므로 별도 규칙). 그 외에는 None → PostToolUse.
def _tool_result_error(
    result: ToolMessage | Command[Any],
    call: ToolCallData,
) -> str | None:
    messages = (
        [result] if isinstance(result, ToolMessage) else _command_messages(result)
    )
    for message in messages:
        if not _is_call_result(message, call.id):
            continue
        if message.status == "error":
            return _tool_result_text(result, call.id)
        artifact = message.artifact
        if call.name != "execute" or not isinstance(artifact, Mapping):
            continue
        exit_code = artifact.get("exit_code")
        if (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        ):
            return f"Command exited with non-zero status code {exit_code}"
    return None


# [해설] Command.update에서 messages 시퀀스를 안전하게 꺼낸다.
def _command_messages(result: Command[Any]) -> Sequence[object]:
    """Return the `messages` list carried by a `Command` update.

    Returns:
        The update's messages, or an empty sequence when absent or malformed.
    """
    update = result.update
    if not isinstance(update, Mapping):
        return ()
    messages = update.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, str):
        return ()
    return messages


# [해설] 병렬 호출의 Command가 여러 결과를 담을 수 있으므로 정확히 이 call_id의 ToolMessage만 대상으로 삼는다.
def _is_call_result(message: object, call_id: str) -> TypeGuard[ToolMessage]:
    """Check whether a message is the `ToolMessage` for the in-flight call.

    A `Command` update may carry results for several calls, so hook context must
    only read from and write to the one this wrapper is handling.

    Returns:
        `True` when the message answers `call_id`.
    """
    return isinstance(message, ToolMessage) and message.tool_call_id == call_id


# [해설] content가 문자열이면 두 줄 띄워 이어붙이고, 멀티모달 블록 리스트면 text 블록을 추가한다. `model_copy`로 id 등 나머지는 유지.
def _merge_tool_message_content(result: ToolMessage, suffix: str) -> ToolMessage:
    if not suffix:
        return result
    content = result.content
    if isinstance(content, str):
        merged = f"{content}\n\n{suffix}" if content else suffix
    # Preserve structured content blocks; append a text block.
    elif isinstance(content, list):
        merged = [*content, {"type": "text", "text": suffix}]
    else:
        merged = f"{content!s}\n\n{suffix}"
    return result.model_copy(update={"content": merged})


# [해설] SubagentStart 훅의 context를 task 도구의 `description` 인자 앞에 붙여 서브에이전트 프롬프트에 주입한다.
# [해설] `request.override(tool_call=...)`로 새 request를 만들며, 원본 tool_call은 변경하지 않는다.
def _inject_subagent_start_context(
    request: ToolCallRequest,
    decision: SubagentStartDecision,
) -> ToolCallRequest:
    if not decision.context:
        return request

    original = request.tool_call
    raw_args = original.get("args")
    args: dict[str, Any]
    if isinstance(raw_args, dict):
        args = {str(key): value for key, value in raw_args.items()}
    else:
        args = {}
    description = args.get("description")
    prefix = "\n".join(decision.context)
    if isinstance(description, str) and description:
        args["description"] = f"{prefix}\n\n{description}"
    else:
        args["description"] = prefix
    tool_call = cast(
        "ToolCall",
        {
            "name": str(original.get("name") or ""),
            "args": args,
            "id": original.get("id"),
            "type": "tool_call",
        },
    )
    return request.override(tool_call=tool_call)


# [해설] task 호출 인자 `subagent_type`을 에이전트 이름(매처 `agent_name`, wire `agent_type`)으로, tool_call_id를 id로 쓴다.
def _task_agent_identity(call: ToolCallData) -> AgentIdentity:
    name = call.args.get("subagent_type")
    if not isinstance(name, str) or not name:
        name = "unknown"
    return AgentIdentity(id=call.id or name, name=name)


# [해설] 결과 텍스트 추출(PostToolUseFailure의 error, SubagentStop의 last_assistant_message에 사용).
def _tool_result_text(result: ToolMessage | Command[Any], call_id: str) -> str:
    if isinstance(result, ToolMessage):
        content = result.content
        return content if isinstance(content, str) else str(content)
    return "\n".join(
        str(message.content)
        for message in _command_messages(result)
        if _is_call_result(message, call_id)
    )


# [해설] 가장 최근의 tool call이 있는 AIMessage와 그 인덱스를 뒤에서부터 찾는다(`_before_model`).
def _latest_tool_call_message(
    messages: Sequence[Any],
) -> tuple[int, AIMessage] | None:
    return next(
        (
            (index, message)
            for index, message in reversed(list(enumerate(messages)))
            if isinstance(message, AIMessage) and message.tool_calls
        ),
        None,
    )


# [해설] 마지막 AIMessage(`_after_model`의 tool call 원천, Stop의 마지막 텍스트 원천).
def _last_ai_message(messages: Sequence[Any]) -> AIMessage | None:
    return next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )


# [해설] Stop 이벤트의 last_assistant_message. 이 값이 Stop invocation_id 해시에 포함된다.
def _last_assistant_text(messages: Sequence[Any]) -> str:
    message = _last_ai_message(messages)
    if message is None:
        return ""
    content = message.content
    return content if isinstance(content, str) else str(content)
