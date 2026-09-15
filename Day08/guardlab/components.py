"""
번호 파일이 create_deep_agent 에 꽂는 부품이다. 어느 훅에 붙는지가 핵심이다.
  InputGuardMiddleware   before_model   새 입력 검사 → defenses 01 호출. jump_to="end" 로 모델 호출 자체를 막을 수 있다
  ActionGuardMiddleware  wrap_tool_call 부모에만 걸리는 비교용. Subagent 내부 호출은 못 본다 (02 의 반례)
  make_action_wrapper    도구 함수 안    누가 부르든 같은 검사. 02·05·06 의 실제 방어
  OutputGuardMiddleware  after_agent    최종 답변 마스킹. 이미 실행된 전달은 못 되돌린다
  make_output_wrapper    도구 함수 안    save/send 직전 본문 검사 (03)
  MemoryGuardMiddleware  before_model + wrap_tool_call   메모리 읽기·쓰기 검사 (04)
  ApprovalStore/make_approval_wrapper   도구 함수 안    승인 기록 바인딩·재사용·만료 (05)
  scope_permissions      FilesystemPermission 목록   내장 파일 도구의 컨텍스트 기반 권한 (02·06)

주요 내용:
번호 파일이 조립할 때 쓰는 middleware·래퍼·저장소. 방어 논리 자체는 `guardlab/defenses.py` 에 있다.
"""


from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from .config import DATA, POLICY_VERSION
from .context import UserContext
from . import defenses as st
from .guards import GuardSignal, InjectionGuard, PIIGuard, PolicyDecision, Span
from .trace import CURRENT_AGENT, RunLog


def scope_permissions(ctx: UserContext, ws) -> list:
    """내장 파일 도구용 권한. 사용자 컨텍스트에 없는 프로젝트 폴더는 읽기·쓰기 모두 거부하고 정책 폴더는 쓰기 거부.

    업무 도구(read_doc)는 guarded_call 이 막지만, DeepAgents 의 read_file/grep/glob 은 별도 경로다.
    라이브 실행에서 모델이 read_file 로 /projects 를 직접 읽는 것이 관찰됐다. 두 경로를 모두 닫아야 한다.
    거부 규칙이 먼저 온다 (첫 일치 규칙 적용).
    """
    from deepagents import FilesystemPermission  # noqa: PLC0415

    rules = [FilesystemPermission(operations=["read", "write"], paths=[f"/projects/{p}/**"], mode="deny")
             for p in ws.projects() if not ctx.can_read(p)]
    rules.append(FilesystemPermission(operations=["write"], paths=["/policy/**"], mode="deny"))
    return rules


def load_reviewers() -> dict:
    """신뢰된 검토자 목록. 작업 공간이 아니라 원본을 읽는다 (공격자는 작업 공간의 파일만 바꿀 수 있다)."""
    return json.loads((DATA / "base" / "policy" / "reviewers.json").read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════════════════
# 01. Input Guard middleware
# ════════════════════════════════════════════════════════════════════════════


class InputGuardMiddleware(AgentMiddleware):
    """모델 호출 전에 새로 유입된 비신뢰 입력을 검사한다.

    scope=("user",) 는 사용자 메시지만, ("user","tool_result") 는 도구 결과까지 검사한다.
    같은 인스턴스를 부모와 Subagent 스택에 함께 넣어도 되지만, 부모에만 넣으면 Subagent 내부 결과는 검사되지 않는다.
    """

    def __init__(self, guard: InjectionGuard, log: RunLog, *, scope=("user",), policy=None, agent_name: str = "main"):
        super().__init__()
        self.st = st
        self.guard = guard
        self.log = log
        self.scope = tuple(scope)
        self.policy = policy or st.InputPolicy()
        self.agent_name = agent_name
        self.scanned: set[str] = set()

    @property
    def name(self) -> str:
        return f"input_guard_{self.agent_name}"

    def before_agent(self, state, runtime):  # noqa: ARG002
        self.scanned = set()
        return None

    @hook_config(can_jump_to=["end"])   # before_model 이 {"jump_to": "end"} 를 돌려줄 수 있게 선언한다
    def before_model(self, state, runtime):  # noqa: ARG002
        """모델을 부르기 직전마다 실행된다. 순서: 새 입력 수집 → Guard 호출 → 결정 → 상태 갱신."""
        items = self.st.collect_untrusted_inputs(state["messages"], self.scanned, self.scope)
        updates: list = []
        for item in items:
            self.scanned.add(item.id)                                   # 같은 입력을 다음 턴에 다시 검사하지 않는다
            stage = "input" if item.source == "user" else "tool_result"
            signal = self.guard.scan(item.text, stage=stage)            # 모델·규칙 → 위험 신호 (권한 아님)
            self.log.guard_calls += 1
            decision = self.st.resolve_input_decision(signal, item, self.policy)   # 신호 → 결정 (코드)
            self.log.record_decision(stage, decision, tool_call_id=item.id, agent=self.agent_name,
                                     detail=f"{signal.model_id} score={signal.score} {signal.detail}".strip())
            update = self.st.apply_input_decision(decision, item, self.policy)
            if update is None:
                continue                                                # ALLOW
            if update.get("jump_to") == "end":
                return update                                           # BLOCK/REVIEW: 즉시 종료. 뒤 항목은 보지 않는다
            updates.extend(update.get("messages", []))                  # TRANSFORM: 교체할 ToolMessage 를 모은다
        return {"messages": updates} if updates else None


# ════════════════════════════════════════════════════════════════════════════
# 02. Action Guard wrapper
# ════════════════════════════════════════════════════════════════════════════


def make_action_wrapper(table, log: RunLog):
    """build_tools(ops, wrapper=...) 에 넣을 래퍼. 실제 판정·차단은 defenses.guarded_call 이 한다."""
    def wrapper(ctx: UserContext | None, tool_name: str, args: dict, impl, tool_call_id: str) -> str:
        return st.guarded_call(ctx, tool_name, args, impl, tool_call_id, table=table, log=log)

    return wrapper


class ActionGuardMiddleware(AgentMiddleware):
    """비교용: 권한 검사를 Tool Wrapper 가 아니라 부모 Agent 의 wrap_tool_call 에 둔 구성.

    부모의 wrap_tool_call 은 `task` 호출까지만 감싼다. Subagent 가 내부에서 부르는 read_doc 은
    이 middleware 를 지나지 않는다 (graph.py:730-733 — 사용자 지정 middleware 는 상속되지 않는다).
    02 에서 "middleware_parent" 구성이 실제로 어느 경로를 놓치는지 확인하는 데 쓴다.
    """

    def __init__(self, table, log: RunLog, *, agent_name: str = "main"):
        super().__init__()
        self.st = st
        self.table = table
        self.log = log
        self.agent_name = agent_name

    @property
    def name(self) -> str:
        return f"action_guard_{self.agent_name}"

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] in ("list_projects", "read_doc", "save_report", "send_report"):
            ctx = request.runtime.context if isinstance(request.runtime.context, UserContext) else None
            decision = self.table.decide(ctx, call["name"], self.st.normalize_args(call["name"], call["args"]))
            self.log.record_decision("action", decision, tool_call_id=call.get("id", ""), agent=self.agent_name)
            if decision.action != "ALLOW":
                return ToolMessage(content=f"[BLOCKED {decision.reason_code}] 이 사용자에게 허용되지 않은 호출이다.",
                                   tool_call_id=call["id"], name=call["name"])
        return handler(request)


def make_input_only_wrapper(log: RunLog):
    """비교용: 권한 검사 없이 호출만 기록하는 래퍼 (입력 분류만 적용한 구성)."""

    def wrapper(ctx, tool_name, args, impl, tool_call_id):  # noqa: ARG001
        return impl()

    return wrapper


# ════════════════════════════════════════════════════════════════════════════
# 03. Output boundary
# ════════════════════════════════════════════════════════════════════════════


def _spans_from_signal(signal: GuardSignal) -> list[Span]:
    return [Span(int(s["start"]), int(s["end"]), str(s["label"]), float(s.get("score", 1.0)), str(s.get("text", "")))
            for s in signal.evidence_spans]


class OutputGuardMiddleware(AgentMiddleware):
    """최종 답변을 사용자에게 돌려주기 전에 검사한다 (after_agent). 이미 실행된 전달은 되돌리지 못한다."""

    def __init__(self, pii: PIIGuard, log: RunLog, *, policy=None, agent_name: str = "main"):
        super().__init__()
        self.st = st
        self.pii = pii
        self.log = log
        self.policy = policy or st.DisclosurePolicy()
        self.agent_name = agent_name

    @property
    def name(self) -> str:
        return f"output_guard_{self.agent_name}"

    def after_agent(self, state, runtime):  # noqa: ARG002
        msgs = state["messages"]
        last = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
        if last is None or not isinstance(last.content, str) or not last.content:
            return None
        signal = self.pii.signal(last.content, stage="output")
        self.log.guard_calls += 1
        if signal.status == "UNKNOWN":
            decision = PolicyDecision("REVIEW", "PII_GUARD_UNKNOWN", POLICY_VERSION, detail=signal.detail)
            self.log.record_decision("output", decision, agent=self.agent_name)
            return {"messages": [AIMessage(content="[검토 필요] 출력 검사기가 응답하지 않아 답변을 보류합니다.", id=last.id)]}
        masked, removed = self.st.mask_for_audience(last.content, _spans_from_signal(signal), "user", self.policy)
        if removed:
            decision = PolicyDecision("TRANSFORM", "PII_IN_ANSWER", POLICY_VERSION, risk_labels=removed)
            self.log.record_decision("output", decision, agent=self.agent_name)
            return {"messages": [AIMessage(content=masked, id=last.id)]}
        self.log.record_decision("output", PolicyDecision("ALLOW", "ANSWER_CLEAN", POLICY_VERSION), agent=self.agent_name)
        return None


def make_output_wrapper(pii: PIIGuard, log: RunLog, *, policy=None, inner=None, check_save: bool = True):
    """send_report 직전(그리고 선택적으로 save_report)에 본문을 검사하는 래퍼. inner 가 있으면 그 뒤에 이어 붙인다."""
    policy = policy or st.DisclosurePolicy()

    def wrapper(ctx, tool_name, args, impl, tool_call_id):
        if tool_name == "send_report":
            decision = st.check_before_send(args.get("recipient", ""), args.get("subject", ""), args.get("body", ""),
                                            ctx, pii, policy)
            log.guard_calls += 1
            log.record_decision("output", decision, tool_call_id=tool_call_id)
            if decision.action == "REVIEW":
                return f"[REVIEW {decision.reason_code}] 전달 본문 검사가 보류됐다. 본문을 줄여 다시 시도하라."
            if decision.action != "ALLOW":
                return f"[BLOCKED {decision.reason_code}] 전달 본문에 공개 불가 정보가 있다: {decision.risk_labels}. 해당 정보를 빼고 다시 저장·전달하라."
        elif tool_name == "save_report" and check_save:
            signal = pii.signal(args.get("content", ""), stage="output")
            log.guard_calls += 1
            if signal.status == "UNKNOWN":
                decision = PolicyDecision("REVIEW", "PII_GUARD_UNKNOWN", POLICY_VERSION)
                log.record_decision("output", decision, tool_call_id=tool_call_id)
                return f"[REVIEW {decision.reason_code}] 초안 검사가 보류됐다."
            _, removed = st.mask_for_audience(args.get("content", ""), _spans_from_signal(signal), "draft", policy)
            if removed:
                decision = PolicyDecision("BLOCK", "PII_IN_DRAFT", POLICY_VERSION, risk_labels=removed)
                log.record_decision("output", decision, tool_call_id=tool_call_id)
                return f"[BLOCKED {decision.reason_code}] 초안에 공개 불가 정보가 있다: {removed}. 빼고 다시 저장하라."
            log.record_decision("output", PolicyDecision("ALLOW", "DRAFT_CLEAN", POLICY_VERSION), tool_call_id=tool_call_id)
        if inner is not None:
            return inner(ctx, tool_name, args, impl, tool_call_id)
        return impl()

    return wrapper


# ════════════════════════════════════════════════════════════════════════════
# 04. Memory guard (제공)
# ════════════════════════════════════════════════════════════════════════════

EXTERNAL_HINT = re.compile(r"@(?!nurisoft\.example)[a-z0-9.-]+\.[a-z]{2,}|https?://", re.IGNORECASE)
INSTRUCTION_HINT = re.compile(r"(반드시|무시|우선|전달한다|삭제|추가한 뒤|바꾼다)")


class MemoryGuardMiddleware(AgentMiddleware):
    """메모리를 읽을 때와 쓸 때 모두 검사한다 (슬라이드 17).

    읽기: MemoryMiddleware 가 state["memory_contents"] 에 적재한 뒤, 외부 주소·지시문이 든 줄을 걷어낸다.
          시스템 프롬프트에 주입되기 전에 걸러야 하므로 before_model 에서 state 를 고친다.
    쓰기: write_file/edit_file 이 /memory 아래를 향하면 내용을 검사한다. 외부 주소·지시문이면 BLOCK.
    """

    def __init__(self, log: RunLog, *, memory_paths=("/memory/AGENTS.md",), agent_name: str = "main"):
        super().__init__()
        self.log = log
        self.memory_paths = tuple(memory_paths)
        self.agent_name = agent_name
        self._cleaned = False

    @property
    def name(self) -> str:
        return f"memory_guard_{self.agent_name}"

    def before_agent(self, state, runtime):  # noqa: ARG002
        self._cleaned = False
        return None

    @staticmethod
    def suspicious_lines(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if EXTERNAL_HINT.search(ln) or INSTRUCTION_HINT.search(ln)]

    def before_model(self, state, runtime):  # noqa: ARG002
        contents = state.get("memory_contents")
        if self._cleaned or not contents:
            return None
        self._cleaned = True
        cleaned: dict[str, str] = {}
        removed_total = 0
        for path, text in contents.items():
            bad = self.suspicious_lines(text)
            removed_total += len(bad)
            cleaned[path] = "\n".join(ln for ln in text.splitlines() if ln not in bad)
        if removed_total:
            self.log.record_decision("memory", PolicyDecision("TRANSFORM", "MEMORY_LINES_REMOVED", POLICY_VERSION,
                                                              detail=f"{removed_total}줄 제거"), agent=self.agent_name)
            return {"memory_contents": cleaned}
        self.log.record_decision("memory", PolicyDecision("ALLOW", "MEMORY_CLEAN", POLICY_VERSION), agent=self.agent_name)
        return None

    def wrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] in ("write_file", "edit_file"):
            path = str(call["args"].get("file_path", ""))
            if any(path == p for p in self.memory_paths):
                new_text = str(call["args"].get("content", "") or call["args"].get("new_string", ""))
                bad = self.suspicious_lines(new_text)
                if bad:
                    decision = PolicyDecision("BLOCK", "MEMORY_WRITE_REJECTED", POLICY_VERSION, detail=bad[0][:80])
                    self.log.record_decision("memory", decision, tool_call_id=call.get("id", ""), agent=self.agent_name)
                    return ToolMessage(content=f"[BLOCKED {decision.reason_code}] 메모리에 외부 주소나 지시문을 저장할 수 없다.",
                                       tool_call_id=call["id"], name=call["name"])
                self.log.record_decision("memory", PolicyDecision("ALLOW", "MEMORY_WRITE_OK", POLICY_VERSION),
                                         tool_call_id=call.get("id", ""), agent=self.agent_name)
        return handler(request)


# ════════════════════════════════════════════════════════════════════════════
# 05. Approval binding (제공)
# ════════════════════════════════════════════════════════════════════════════


def args_hash(tool: str, args: dict) -> str:
    canon = json.dumps({"tool": tool, "args": {k: args[k] for k in sorted(args)}}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


@dataclass
class ApprovalRecord:
    id: str
    tool: str
    args_hash: str
    recipient: str
    report_version: int
    requester: str
    approver: str
    expires_at: datetime
    used: bool = False
    policy_version: str = POLICY_VERSION


@dataclass
class ApprovalStore:
    """승인은 "예/아니오"가 아니라 도구·인자 해시·자원·승인자·만료에 묶인 기록이다 (슬라이드 21·50)."""

    ttl: timedelta = timedelta(minutes=10)
    records: dict[str, ApprovalRecord] = field(default_factory=dict)

    def approve(self, tool: str, args: dict, *, requester: str, approver: str, now: datetime | None = None) -> ApprovalRecord:
        now = now or datetime.now(timezone.utc)
        rec = ApprovalRecord(id=uuid.uuid4().hex[:8], tool=tool, args_hash=args_hash(tool, args),
                             recipient=str(args.get("recipient", "")), report_version=int(args.get("report_version", 0) or 0),
                             requester=requester, approver=approver, expires_at=now + self.ttl)
        self.records[rec.id] = rec
        return rec

    def check(self, tool: str, args: dict, *, requester: str, now: datetime | None = None,
              consume: bool = True) -> PolicyDecision:
        now = now or datetime.now(timezone.utc)
        h = args_hash(tool, args)
        candidates = [r for r in self.records.values() if r.tool == tool and r.args_hash == h and r.requester == requester]
        if not candidates:
            same_tool = [r for r in self.records.values() if r.tool == tool and r.requester == requester and not r.used]
            code = "APPROVAL_ARGS_MISMATCH" if same_tool else "NO_APPROVAL"
            return PolicyDecision("BLOCK", code, POLICY_VERSION, detail=f"hash={h}")
        rec = candidates[-1]
        if rec.used:
            return PolicyDecision("BLOCK", "APPROVAL_ALREADY_USED", POLICY_VERSION, detail=rec.id)
        if now > rec.expires_at:
            return PolicyDecision("BLOCK", "APPROVAL_EXPIRED", POLICY_VERSION, detail=rec.id)
        if consume:
            rec.used = True
        return PolicyDecision("ALLOW", "APPROVED", POLICY_VERSION, detail=rec.id)


def make_approval_wrapper(store: ApprovalStore, log: RunLog, *, mode: str, table=None, inner=None):
    """send_report 실행 직전에 승인 기록을 대조한다.

    mode:
      "approve_only"     대조 없음. HITL 의 approve 만 믿는다.
      "bound"            승인 기록(도구·인자 해시·요청자)과 대조. 인자가 바뀌었거나 재사용이면 BLOCK.
      "bound_revalidate" bound + 현재 권한(PermissionTable)을 다시 검사. 승인 뒤 권한이 바뀐 경우를 잡는다.
    """
    def wrapper(ctx, tool_name, args, impl, tool_call_id):
        if tool_name == "send_report" and mode in ("bound", "bound_revalidate"):
            requester = ctx.user_id if ctx else "unknown"
            decision = store.check("send_report", args, requester=requester)
            log.record_decision("approval", decision, tool_call_id=tool_call_id)
            if decision.action != "ALLOW":
                return f"[BLOCKED {decision.reason_code}] 승인 기록과 실행 내용이 맞지 않는다 ({decision.detail}). 새 승인이 필요하다."
            if mode == "bound_revalidate" and table is not None:
                current = table.decide(ctx, tool_name, st.normalize_args(tool_name, args))
                log.record_decision("action", current, tool_call_id=tool_call_id)
                if current.action != "ALLOW":
                    return f"[BLOCKED {current.reason_code}] 승인은 있으나 현재 권한이 없다."
        if inner is not None:
            return inner(ctx, tool_name, args, impl, tool_call_id)
        return impl()

    return wrapper
