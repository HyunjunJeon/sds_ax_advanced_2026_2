"""01. 비교 기준선: 질문 → 근거 검색 → 한 번의 답변 생성.

가르치는 것:
- 멀티에이전트 이전의 최소 동작 단위. 구조를 추가하지 않아도 RAG는 동작하며,
  이후 모든 구조의 추가 호출·지연·토큰은 이 기준선과의 "차이"로만 의미가 있다.
- complete는 형식·인용 계약을 통과했다는 실행 상태일 뿐 의미 검토가 없음을 아는 것.
  같은 complete라도 구조에 따라 검토 범위가 다르다는 사실이 비교의 출발점이다.

이 파일에는 별도 planner/Worker/reviewer를 넣지 않는다. 검색·근거 계약은 다른 구조와
공유하므로, 이후 실습에서 늘어나는 모델 호출이 어느 실패를 해결하는지 비교할 수 있다.
web="persist"도 같은 Service.ensure를 사용한다. 웹 기능 유무를 구조 차이와 섞지 말자.
complete는 형식과 인용 ID가 유효하다는 실행 상태이며 의미상 정답의 보증은 아니다.
심화 연결: 10_context_challenge.py에서 필수 근거가 작은 Context에 살아남도록 구현한다.

관찰: result.json의 trace에서 retrieved의 근거와 제외 문서를 확인한다.
"""

from langgraph.graph import END, START, StateGraph

from common.contracts import State
from common.day02_bridge import Evidence
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 기준선은 예산을 거의 쓰지 않는다(모델 호출 1회). 다른 구조와 비교할 때 이 파일의
# question/entity/as_of/backend를 그대로 복사해야 "같은 문제, 다른 구조" 비교가 된다.
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab01",
    question="알파 서비스의 현재 가용률 목표와 서비스 크레딧 신청 기한은?",
)


def build(service):
    def retrieve(state):
        # State에는 직렬화 가능한 근거만 넣는다. HTTP client나 모델 객체는 Service에 둔다.
        refs = service.ensure(service.retrieve(state["question"]))
        return {"evidence": [e.model_dump() for e in refs]}

    def answer(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        # 기준선에는 의미 검토 호출이 없다. 같은 complete라도 03/06의 검토 범위와 다르다.
        return {"result": service.finish(service.generate(refs), refs)}

    graph = StateGraph(State)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


if __name__ == "__main__":
    run_lab("baseline", 실험, build)
