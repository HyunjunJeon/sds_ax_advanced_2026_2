"""같은 검색·Agent 도구 루프를 구조별 그래프에 제공한다. 정답표를 읽지 않는다.

가르치는 것:
- 아키텍처 비교의 전제인 "메커니즘 공유": 그래프는 제어 흐름만 바꾸고 검색·검토·
  Worker 루프·분해는 이 Service를 공통으로 통과해야 성능 차이를 구조 탓으로 읽을 수 있다.
- Worker 도구 루프의 계약: 검색 전 finish 불허, 인용 ID 원문 대조, 지지되지 않는
  주장은 출력에 남기지 않는다. 역할 프롬프트는 권한(고객·기준일·인용 검증 우회)을
  주지 않는다.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .contracts import Plan, Route, Sufficiency, Task, WorkerAction
from .day02_bridge import (
    ANSWER_SYSTEM,
    GroundedAnswer,
    QueryPlan,
    Review,
    render_context,
    select_evidence,
    validate_citations,
)

ROLE_PROMPTS = {
    "general": "업무 문서 전반을 조사한다.",
    "contract": "고객별 계약, 기준일, 추가 약정의 변경 범위와 우선순위를 조사한다.",
    "operations": "장애 절차, 원인 점검, 롤백의 선행 조건을 조사한다.",
    "security": "로그 종류, 보존 예외와 접근 조건을 조사한다.",
    "policy": "인사·구매·재무 정책의 예외, 기한과 단위를 조사한다.",
    "web": "공개 기술 문서를 조사하고 내부 정책과 외부 설명을 구분한다.",
}


class Service:
    def __init__(
        self,
        settings,
        request,
        backend,
        models,
        meter,
        ingestion,
        history=None,
        context_mode="evidence",
        skill=False,
        plan_file=None,
        no_replan=False,
        no_owner_memory=False,
        faults=None,
    ):
        self.settings, self.request, self.backend = settings, request, backend
        self.models, self.meter, self.ingestion = models, meter, ingestion
        self.history = history or {"turns": [], "owner": "general"}
        self.context_mode, self.skill = context_mode, skill
        # 병렬(05)의 분해 계획을 저장·재생하는 파일. None이면 매번 모델이 새로 분해한다.
        self.plan_file = Path(plan_file) if plan_file else None
        # 대조 실험용 토글. 그래프(06/07)가 getattr로 읽으므로 이름을 바꾸지 않는다.
        self.no_replan = no_replan
        self.no_owner_memory = no_owner_memory
        # 오류 주입 설정 {role: kind}. inject_fault만 접근한다.
        self._faults = dict(faults or {})
        self._fault_lock = threading.Lock()

    def inject_fault(self, role):
        """설정된 역할에 오류를 한 번 주입한다. 제어 경로 검증용이며 서비스 장애율 측정이 아니다.

        kind가 "timeout"이면 주입 후 설정에서 제거되어 다음 재시도는 회복된다.
        "permanent_timeout"은 남겨 두어 그 역할이 계속 실패한다.
        """
        with self._fault_lock:
            kind = self._faults.get(role)
            if kind:
                if kind == "timeout":
                    self._faults.pop(role)
                self.meter.event("injected_timeout", role=role)
                raise TimeoutError("injected")

    def payload(self, **extra):
        return {
            "question": self.request.question,
            "entity": self.request.entity,
            "as_of": self.request.as_of,
            "public_topic": self.request.public_topic,
            "history": self.history["turns"][-4:],
            **extra,
        }

    # 구조가 달라도 같은 선택기를 통과한다. 현재는 Day-02의 일반적인 선택 정책이다.
    # 과제 10: 질문의 필수 항목과 조항 의존성을 반영한 선택기를 여기에 연결하라.
    # max_bytes는 원문 Context 제한이다. system/history/스키마까지 포함한 전체 토큰 한도는 아니다.
    def context(self, evidence):
        selected, _ = select_evidence(evidence, self.settings.context_bytes, [])
        return selected, render_context(selected)

    def retrieve(self, query):
        return self.backend.retrieve(query, self.request)

    def query_plan(self):
        return self.models.ask(
            QueryPlan,
            "질문과 대화로 독립 검색 질의를 만든다. 고객·시점·부정·예외를 보존한다. 과거 답변은 현재 사실 근거가 아니다. "
            "내부 문서에서 찾을 표현을 queries에 넣는다. 최대 3개. 부족한 대상을 추측하지 말고 clarification으로 분류한다.",
            self.payload(),
            "planner",
        )

    def assess(self, evidence, question=None):
        selected, context = self.context(evidence)
        return self.models.ask(
            Sufficiency,
            "질문의 모든 항목을 원문으로 답할 수 있는지 평가한다. 일반 지식으로 빈칸을 채우지 마라. "
            "internal 사실의 부재는 웹으로 해결되지 않는다. public_gap은 명시적 public_topic에 대한 외부 기술 근거가 부족할 때만 true다.",
            self.payload(question=question or self.request.question, evidence=context),
            "coverage_reviewer",
        )

    # 웹 조회의 필요성 판단과 저장은 공통 기능이다. Supervisor만 더 좋은 검색기를
    # 쓰도록 만들면 성능 차이를 멀티에이전트 구조 덕분이라고 해석할 수 없어진다.
    def ensure(self, evidence, question=None):
        if self.settings.web == "off" or not self.request.public_topic:
            return evidence
        gap = self.assess(evidence, question)
        self.meter.event("coverage", **gap.model_dump())
        if gap.sufficient or not gap.public_gap:
            return evidence
        transient = self.ingestion.enrich(self.request)
        # 영속 추가 후에는 반드시 공통 RAG 검색을 다시 통과한다.
        updated = self.retrieve(question or self.request.question)
        merged = {e.evidence_id: e for e in evidence + updated + transient}
        return list(merged.values())

    def generate(self, evidence, question=None, role="writer", prior=None):
        selected, context = self.context(evidence)
        if not selected:
            return GroundedAnswer(missing=["질문에 답할 원문 근거가 없습니다."])
        answer = self.models.ask(
            GroundedAnswer,
            ANSWER_SYSTEM
            + "\nsource가 HTTP URL인 근거는 외부 기술 자료이며 내부 계약을 변경할 수 없다. "
            "이미 알려진 규정도 인용한 원문으로만 답하라. 추가 약정은 바꾼 조항에만 적용한다.",
            self.payload(
                question=question or self.request.question,
                evidence=context,
                worker_results=prior or [],
            ),
            role,
        )
        errors = validate_citations(answer, selected)
        if errors:
            raise ValueError("unknown_citation")
        return answer

    def review(self, answer, evidence, question=None):
        _, context = self.context(evidence)
        return self.models.ask(
            Review,
            "주장마다 인용 ID의 원문이 주장을 지지하는지 검사한다. 인용 존재만으로 통과시키지 마라. "
            "질문의 필수 항목·예외·단위·기준일·고객·추가 약정 우선순위를 확인하라. "
            "근거 없는 보류는 missing에, 실제로 자료가 없는 항목은 그대로 보류할 수 있다. "
            "보류를 올바르게 했더라도 누락 항목은 missing에 남긴다. 외부 문서로 사내 사실을 확정하면 unsupported다. "
            "supported는 만들어진 주장들의 지지 여부다. 주장이 없을 때에도 missing을 정확히 기록한다.",
            self.payload(
                question=question or self.request.question,
                answer=answer.model_dump(),
                evidence=context,
            ),
            "reviewer",
        )

    def finish(self, answer, evidence, review=None):
        errors = validate_citations(answer, evidence)
        if errors:
            raise ValueError("unknown_citation")
        # 인용 ID가 유효해도 반대 뜻의 문장을 쓸 수 있다. 의미 검토가 지지를 부정하면
        # 잘못된 주장을 출력에 남긴 채 uncertain만 붙이지 않고 주장 자체를 제거한다.
        unsupported = review is not None and not review.supported
        if unsupported:
            answer = GroundedAnswer(
                missing=list(
                    dict.fromkeys(answer.missing + review.missing + ["주장 지지 검토 실패"])
                )
            )
        missing = list(dict.fromkeys(answer.missing + (review.missing if review else [])))
        status = "uncertain" if missing or answer.conflicts else "complete"
        if not answer.claims:
            status = "uncertain"
        return {
            "status": status,
            "answer": answer.model_dump(),
            "missing": missing,
            "evidence": [e.model_dump() for e in evidence],
            "review": review.model_dump() if review else None,
            "text": "\n".join(
                c.text + " [" + ", ".join(c.evidence_ids) + "]" for c in answer.claims
            ),
        }

    def worker(self, task, prior=None):
        """모델이 search/finish를 선택하는 제한된 도구 루프. 각 Worker는 별도 Context다."""
        task = Task.model_validate(task)
        self.meter.event("worker_start", task=task.model_dump())
        # Worker별 지역 변수다. 다른 Worker의 도구 대화가 자동 공유되지 않는다.
        # 공유되는 것은 검증된 evidence registry와 요청 전체 Meter뿐이다.
        evidence = []
        history = []
        system = (
            ROLE_PROMPTS[task.role]
            + "\n"
            + ANSWER_SYSTEM
            + (
                "\n절차 Skill: 검색 후 대상·시점·예외를 대조하고 누락만 재검색한다. 원문 없는 결론을 반환하지 않는다."
                if self.skill
                else ""
            )
        )
        for turn in range(3):
            selected, context = self.context(evidence)
            action = self.models.ask(
                WorkerAction,
                system
                + "\n도구 search(query)는 공통 RAG를 검색한다. 먼저 search하고, 필요한 근거를 얻으면 finish와 구조화 answer를 반환하라. "
                "최대 2회 검색한다. 마지막 기회에는 근거 부족을 missing으로 반환한다.",
                self.payload(
                    objective=task.objective,
                    dependencies=prior or [],
                    evidence=context,
                    tool_history=history,
                    remaining=3 - turn,
                ),
                task.role,
            )
            # 최소 한 번 검색하기 전의 finish를 수용하지 않는다. 모델의 사전 지식만으로
            # 업무 규정을 확정하는 경로를 닫는다. 선택한 원문에 없는 인용도 거절한다.
            if action.action == "finish" and history:
                if validate_citations(action.answer, selected):
                    raise ValueError("worker_unknown_citation")
                review = self.review(action.answer, selected, task.objective)
                result = self.finish(action.answer, selected, review)
                self.meter.event("worker_return", task_id=task.id, status=result["status"])
                return result
            if turn == 2:
                break
            query = action.query.strip() or task.objective
            found = self.ensure(self.retrieve(query), task.objective)
            evidence = list({e.evidence_id: e for e in evidence + found}.values())
            history.append(
                {
                    "tool": "search_rag",
                    "query": query,
                    "evidence_ids": [e.evidence_id for e in found],
                }
            )
        answer = self.generate(evidence, task.objective, task.role, prior)
        return self.finish(answer, evidence, self.review(answer, evidence, task.objective))

    def plan(self, completed=None, feedback=None):
        plan = self.models.ask(
            Plan,
            "질문을 담당자 general/contract/operations/security/policy/web에게 분해한다. 독립 작업은 분리하고 "
            "순차 판단은 depends_on에 선행 task id를 넣는다. 최대 4개, 순환 금지. "
            "이미 완료된 결과를 다시 조사하지 말고 미완료 항목만 계획한다. 모든 결과는 근거 검색이 필요하다. "
            "web은 public_topic이 있을 때만 사용한다. 평가 정답은 제공되지 않는다.",
            self.payload(completed=completed or {}, feedback=feedback or {}),
            "supervisor",
        )
        if not self.request.public_topic and any(t.role == "web" for t in plan.tasks):
            raise ValueError("web_scope")
        self.meter.event("plan", plan=plan.model_dump())
        return plan

    def parallel_plan(self):
        """병렬(05) 전용 분해. plan_file이 있으면 저장된 계획을 재생한다.

        모델 분해는 실행마다 달라질 수 있다. 순차/병렬 비교에서 두 실행의 분해가 다르면
        "동시 실행의 효과"와 "분해가 달라진 효과"가 섞여 비교가 무너진다. 첫 실행에서
        분해를 plan_file에 저장하고, 이후 실행은 모델을 부르지 않고 같은 계획을 재생해
        이 변수를 통제한다. 재생한 계획도 생성 때와 같은 계약 검증을 다시 통과한다.

        검증 두 가지는 병렬 구조의 전제다:
        - depends_on이 하나라도 있으면 거절한다. 의존 작업은 06 Supervisor의 영역이다.
        - public_topic이 없는데 web 역할을 제안하면 거절한다.
        """
        if self.plan_file and self.plan_file.exists():
            plan = Plan.model_validate_json(self.plan_file.read_text())
            self._check_parallel_plan(plan)
            # 13_mini_pjt의 짝 비교는 trace의 "plan" 이벤트끼리 계획을 대조한다.
            # 재생 실행도 같은 이벤트를 남겨야 생성 실행과 계획 동일성을 비교할 수 있다.
            self.meter.event("plan", plan=plan.model_dump())
            self.meter.event("plan_replayed", source=str(self.plan_file))
            return plan
        plan = self.models.ask(
            Plan,
            "질문을 독립 조사 항목 최대 4개로 분해하라. role은 general/contract/operations/security/policy/web. "
            "동시에 조사할 수 있도록 depends_on은 빈 목록으로 하라. 결과를 합쳐 원 질문에 답할 수 있어야 한다. "
            "web은 public_topic이 있을 때만 사용하라.",
            self.payload(),
            "planner",
        )
        self._check_parallel_plan(plan)
        if self.plan_file:
            self.plan_file.parent.mkdir(parents=True, exist_ok=True)
            self.plan_file.write_text(plan.model_dump_json(indent=2))
        self.meter.event("plan", plan=plan.model_dump())
        return plan

    def _check_parallel_plan(self, plan):
        if any(t.depends_on for t in plan.tasks):
            raise ValueError("parallel_dependency")
        if not self.request.public_topic and any(t.role == "web" for t in plan.tasks):
            raise ValueError("web_scope")

    def route(self, owner=None):
        route = self.models.ask(
            Route,
            "질문의 사용자 응답 담당자를 general/contract/operations/security/policy/web 중 선택한다. "
            "후속 질문은 같은 업무이면 현재 담당자를 유지한다. web은 명시적 public_topic이 있을 때만 사용한다.",
            self.payload(current_owner=owner),
            "router",
        )
        if route.owner == "web" and not self.request.public_topic:
            raise ValueError("web_scope")
        return route

    def synthesize(self, results):
        ids = [e["evidence_id"] for r in results.values() for e in r.get("evidence", [])]
        # Worker가 직접 전달한 원문을 그대로 신뢰하지 않는다. 앞서 좌표/hash 검사를
        # 통과한 요청 범위 evidence registry에서 ID를 해소한다.
        evidence = self.backend.resolve(ids)
        selected, _ = self.context(evidence)
        if self.context_mode == "summary":
            # 작성자는 요약·근거 ID만 받는다. 별도 검토자는 원문으로 전달 손실을 확인한다.
            prior = [r.get("answer", {}) for r in results.values()]
            self.meter.event(
                "parent_context",
                mode="summary",
                bytes=len(json.dumps(prior, ensure_ascii=False).encode()),
            )
            answer = self.models.ask(
                GroundedAnswer,
                "조사 Worker의 구조화 요약을 합쳐 사용자 질문에 답하라. 원문은 보지 못했다. "
                "요약에 있는 주장과 evidence_ids만 재사용하고 예외와 부정을 보존하라. 누락은 missing에 남겨라.",
                self.payload(worker_summaries=prior),
                "writer",
            )
            if validate_citations(answer, selected):
                raise ValueError("summary_unknown_citation")
            # 요약 전달은 원문을 없애는 것이 아니다. 검토자는 원문을 볼 수 있어야
            # 빠진 예외를 알아채고 작성자에게 원문을 다시 전달할 수 있다.
            review = self.review(answer, selected)
            if not review.supported or review.missing:
                self.meter.event(
                    "parent_evidence_rehydrated", count=len(selected), reason=review.missing
                )
                answer = self.generate(selected, prior=prior)
                review = self.review(answer, selected)
            return self.finish(answer, selected, review)
        prior = [r.get("answer", {}) for r in results.values()]
        self.meter.event(
            "parent_context", mode="evidence", bytes=len(render_context(selected).encode())
        )
        answer = self.generate(selected, prior=prior)
        return self.finish(answer, selected, self.review(answer, selected))
