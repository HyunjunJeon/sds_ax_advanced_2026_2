"""
사용: log = RunLog(); 각 Agent 스택에 TraceMiddleware(log, "<agent 이름>") 하나씩.
포인트:
  1. called(도구가 불림) 와 executed(원시 구현이 실제로 돎) 를 구분한다. 차단 문자열만 돌려주고 impl 이 돌면 executed 가 남는다.
  2. 어느 Agent 의 호출인지는 wrap_tool_call 안에서 contextvar 로 표시한다. before_agent 에서 설정하면 스레드가 달라 새는 경우가 있었다.
  3. record_decision() 이 모든 Guard 의 결정을 한 곳에 모은다. 평가기의 blocked_at·missed_paths 가 여기서 나온다.

주요 내용:
실행 기록. 평가기는 Agent의 답변이 아니라 이 기록과 저장소 상태를 읽는다.
RunLog           도구 호출·정책 결정·모델 호출 수. 부모와 Subagent 가 같은 객체를 공유한다.
TraceMiddleware  각 Agent 스택에 하나씩 넣는다. 모델 호출을 세고, 모든 도구 호출(내장 파일 도구 포함)을
                 어느 Agent 가 불렀는지와 함께 남긴다. 부모에만 넣으면 Subagent 내부 호출은 보이지 않는다.

"호출됨(called)"과 "실제 함수가 실행됨(executed)"은 다르다. 업무 도구의 원시 구현은 실행될 때
executed 를 따로 남기므로, 차단 로그가 있어도 executed 가 없으면 실제 피해가 없었다는 뜻이다.
"""

from __future__ import annotations

import contextvars
from dataclasses import asdict, dataclass, field

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from .guards.contracts import PolicyDecision

CURRENT_AGENT: contextvars.ContextVar[str] = contextvars.ContextVar("guardlab_agent", default="main")
CURRENT_TOOL_CALL: contextvars.ContextVar[str] = contextvars.ContextVar("guardlab_tool_call", default="")


@dataclass
class ToolEvent:
    agent: str
    name: str
    args: dict
    tool_call_id: str
    outcome: str  # called / executed / blocked / review / error / denied
    detail: str = ""


@dataclass
class DecisionEvent:
    stage: str
    action: str
    reason_code: str
    agent: str
    tool_call_id: str = ""
    risk_labels: list[str] = field(default_factory=list)
    detail: str = ""


class RunLog:
    def __init__(self):
        self.tools: list[ToolEvent] = []
        self.decisions: list[DecisionEvent] = []
        self.model_calls: int = 0
        self.guard_calls: int = 0
        self.notes: list[str] = []

    def record_tool(self, name: str, args: dict, outcome: str, *, tool_call_id: str = "", detail: str = "",
                    agent: str | None = None) -> None:
        self.tools.append(ToolEvent(agent or CURRENT_AGENT.get(), name, dict(args), tool_call_id or CURRENT_TOOL_CALL.get(),
                                    outcome, detail))

    def record_decision(self, stage: str, decision: PolicyDecision, *, tool_call_id: str = "", detail: str = "",
                        agent: str | None = None) -> None:
        self.decisions.append(DecisionEvent(stage, decision.action, decision.reason_code, agent or CURRENT_AGENT.get(),
                                            tool_call_id or CURRENT_TOOL_CALL.get(), list(decision.risk_labels),
                                            detail or decision.detail))

    def first_stop(self) -> DecisionEvent | None:
        for d in self.decisions:
            if d.action in ("BLOCK", "REVIEW"):
                return d
        return None

    def executed(self, name: str | None = None) -> list[ToolEvent]:
        return [t for t in self.tools if t.outcome == "executed" and (name is None or t.name == name)]

    def to_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "guard_calls": self.guard_calls,
            "tools": [asdict(t) for t in self.tools],
            "decisions": [asdict(d) for d in self.decisions],
            "notes": list(self.notes),
        }


class TraceMiddleware(AgentMiddleware):
    """모델 호출 수와 모든 도구 호출을 기록한다. Agent 마다 하나씩 넣는다.

    도구 실행은 wrap_tool_call 과 같은 스레드에서 일어나므로, 그 안에서 contextvar 로 "지금 어느 Agent 의
    어느 tool_call 인가"를 알린다. 원시 도구 구현(RawOps)이 남기는 executed 기록이 이 값을 읽는다.
    LangGraph 노드는 서로 다른 스레드에서 돌 수 있어 before_agent 에서 설정한 contextvar 는 믿을 수 없다.
    """

    def __init__(self, log: RunLog, agent_name: str = "main"):
        super().__init__()
        self.log = log
        self.agent_name = agent_name

    @property
    def name(self) -> str:
        return f"trace_{self.agent_name}"

    def wrap_model_call(self, request, handler):
        self.log.model_calls += 1
        return handler(request)

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        t_agent = CURRENT_AGENT.set(self.agent_name)
        t_call = CURRENT_TOOL_CALL.set(call.get("id") or "")
        try:
            result = handler(request)
        except Exception as e:
            self.log.record_tool(call["name"], call.get("args", {}), "error", tool_call_id=call.get("id") or "",
                                 detail=type(e).__name__, agent=self.agent_name)
            raise
        finally:
            CURRENT_TOOL_CALL.reset(t_call)
            CURRENT_AGENT.reset(t_agent)
        outcome = "called"
        detail = ""
        if isinstance(result, ToolMessage):
            text = result.content if isinstance(result.content, str) else str(result.content)
            low = text.lower()
            if "permission denied" in low:
                outcome, detail = "denied", text[:160]  # 내장 파일 도구의 권한 거부
            elif text.startswith("[BLOCKED"):
                outcome, detail = "blocked", text[:160]
            elif text.startswith("[REVIEW"):
                outcome, detail = "review", text[:160]
            elif result.status == "error":
                outcome, detail = "error", text[:160]
        self.log.record_tool(call["name"], call.get("args", {}), outcome, tool_call_id=call.get("id") or "", detail=detail,
                             agent=self.agent_name)
        return result
