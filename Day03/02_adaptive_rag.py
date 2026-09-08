"""02. 단일 그래프 안에서 계획 → 검색 → 생성 → 검토 → 제한 재검색.

가르치는 것:
- 담당자를 늘리기 전에 물어야 할 질문: "이 실패는 검색 질의 교정과 자체 검토만으로
  해결되는가?" 구조 확장은 이 단일 그래프가 못 해결하는 실패가 확인된 뒤의 일이다.
- 종료 조건을 프롬프트가 아니라 그래프가 강제한다. 검토 불만족이 무한 재검색으로
  번지지 않게 상한(revision 1회)을 코드가 지킨다.
- 불확실성을 숨기지 않는다. 근거가 없으면 complete가 아니라 missing으로 보류한다.

Day-02의 QueryPlan/Review와 같은 RAG를 사용한다. 다수의 담당자를 만들기 전에
검색 질의 교정과 자체 검토만으로 해결되는 실패인지 확인하는 두 번째 기준선이다.
revision은 시도 횟수다. 검토 모델이 계속 불만족을 반환해도 재검색은 한 번만 허용한다.
불확실성을 숨기거나 근거가 없는 내부 사실을 웹에서 채우는 방식으로 완료하지 않는다.

관찰: adaptive_retry의 이유와 새 evidence_id가 실제로 누락을 해소했는지 대조한다.
심화: LabConfig.question을 "가용률 목표는?"처럼 모호한 질문으로 바꾸면
plan 노드의 clarification 분기(needs_clarification 종료)를 관찰할 수 있다.
"""

from langgraph.graph import END, START, StateGraph

from common.contracts import State
from common.day02_bridge import Evidence, GroundedAnswer
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab02",
    question="알파 서비스의 가용률 목표와 신청 기한, 그리고 8월 추가 약정이 바꾼 항목을 설명하세요.",
)


def build(service):
    def plan(state):
        p = service.query_plan()
        if p.intent == "clarification":
            # 질문 자체가 모호하면 검색을 시작하지 않는다. 잘못된 검색은 비용만 쓴다.
            return {
                "result": {
                    "status": "needs_clarification",
                    "text": "질문의 대상이나 조건을 명확히 해 주세요.",
                    "answer": {"claims": [], "missing": ["질문 대상 불명확"], "conflicts": []},
                    "evidence": [],
                    "missing": ["질문 대상 불명확"],
                },
                "plan": [],
            }
        return {"plan": [p.model_dump()], "revision": 0}

    def retrieve(state):
        p = state["plan"][0]
        queries = [state["review"]["retry_query"]] if state.get("revision") else p["queries"]
        # 첫 검색의 유효 근거는 유지하고 새 검색 결과를 evidence_id로 합친다.
        # 마지막 검색 결과로 덮어쓰면 먼저 찾은 추가 약정/예외를 잃을 수 있다.
        evidence = {e["evidence_id"]: Evidence.model_validate(e) for e in state.get("evidence", [])}
        for q in queries:
            for e in service.retrieve(q):
                evidence[e.evidence_id] = e
        refs = service.ensure(list(evidence.values()), p["standalone_question"])
        return {"evidence": [e.model_dump() for e in refs]}

    def answer(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        return {
            "draft": service.generate(refs, state["plan"][0]["standalone_question"]).model_dump()
        }

    def review(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        answer = GroundedAnswer.model_validate(state["draft"])
        r = service.review(answer, refs, state["plan"][0]["standalone_question"])
        return {"review": r.model_dump(), "result": service.finish(answer, refs, r)}

    def retry(state):
        service.meter.event("adaptive_retry", reason=state["review"]["missing"])
        return {"revision": state["revision"] + 1}

    graph = StateGraph(State)
    for name, node in [
        ("plan", plan),
        ("retrieve", retrieve),
        ("answer", answer),
        ("review", review),
        ("retry", retry),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", lambda s: "retrieve" if s["plan"] else END)
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", "review")
    # 종료 조건을 프롬프트가 아닌 그래프가 강제한다. 검토 실패가 무한 호출로 번지지 않는다.
    graph.add_conditional_edges(
        "review",
        lambda s: (
            "retry"
            if s["result"]["status"] != "complete"
            and s["revision"] < 1
            and s["review"]["retry_query"]
            else END
        ),
    )
    graph.add_edge("retry", "retrieve")
    return graph.compile()


if __name__ == "__main__":
    run_lab("adaptive", 실험, build)
