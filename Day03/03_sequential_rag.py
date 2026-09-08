"""03. 조사 → 작성 → 검토: 역할별 입력 Context의 경계를 만드는 순차 분업.

가르치는 것:
- 역할 분업의 본질은 모델을 여러 개 두는 것이 아니라 입력 Context를 분리하는 것이다.
  같은 모델도 전달되는 메시지와 도구 범위가 다르면 다른 역할을 수행한다.
- 역할 간에는 전체 대화가 아니라 계약된 결과(구조화 조사 결과 + 원문 근거)만
  넘어간다. 무엇을 넘기고 무엇을 넘기지 않는가가 곧 아키텍처의 결정이다.
- 추가 호출이 품질을 높이는지는 역할별 model_records와 검토 전후의 누락으로 판단한다.

조사자는 search/finish 도구 루프를 돌고, 작성자는 구조화 조사 결과와 원문만 받는다.
역할 이름이 다르다고 모델 자체가 달라지는 것은 아니다. 같은 모델도 전달한 메시지와
도구 범위가 다르면 별도의 역할을 수행한다. 모든 역할은 한 Meter의 비용을 공유한다.
추가 호출이 품질을 높이는지는 검토 전후의 누락·예외·근거로 판단해야 한다.

관찰: result.json의 metrics.model_records에서 역할별(general/writer/reviewer) 호출 수와
입력 바이트를 비교한다. 역할 분리만으로 비용이 어떻게 변하는지가 첫 관찰 포인트다.
심화 연결: 10번 과제의 선택기를 붙일 때 작성자와 검토자의 근거 집합을 추적한다.
"""

from langgraph.graph import END, START, StateGraph

from common.contracts import State, Task
from common.day02_bridge import Evidence, GroundedAnswer
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab03",
    question=(
        "검색 장애의 타임아웃 증가 점검 순서와 운영 접근 로그의 보관 기간·예외를 설명하세요."
    ),
)


def build(service):
    def investigate(state):
        # 조사자도 결국 worker 도구 루프다. 역할 프롬프트가 general로 시작할 뿐이다.
        result = service.worker(Task(id="research", role="general", objective=state["question"]))
        return {"results": {"research": result}, "evidence": result["evidence"]}

    # 조사자의 전체 대화나 내부 추론을 전달하지 않는다. 아래 두 필드가 역할 간 계약이다.
    def write(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        return {
            "draft": service.generate(
                refs, role="writer", prior=[state["results"]["research"]["answer"]]
            ).model_dump()
        }

    # 작성자의 확신을 재검토하는 대신, 반환한 주장과 실제 원문을 다시 대조한다.
    def verify(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        answer = GroundedAnswer.model_validate(state["draft"])
        return {"result": service.finish(answer, refs, service.review(answer, refs))}

    graph = StateGraph(State)
    graph.add_node("research_agent", investigate)
    graph.add_node("writer_agent", write)
    graph.add_node("reviewer_agent", verify)
    graph.add_edge(START, "research_agent")
    graph.add_edge("research_agent", "writer_agent")
    graph.add_edge("writer_agent", "reviewer_agent")
    graph.add_edge("reviewer_agent", END)
    return graph.compile()


if __name__ == "__main__":
    run_lab("sequential", 실험, build)
