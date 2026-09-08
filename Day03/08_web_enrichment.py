"""08. 근거 부족을 감지하면 EXA 원문을 검사하고 RAG에 추가한다.

가르치는 것:
- 내부 지식과 외부 지식의 경계: 외부 검색 질의는 명시된 public_topic뿐이며, 외부
  기술 문서는 내부 계약의 효력을 바꿀 수 없다. 지식 출처를 섞으면 안 되는 이유다.
- 웹 활용의 두 방식: transient(이번 응답의 임시 근거)와 persist(검사 후 저장·색인
  준비 확인 뒤 공통 검색 경로로 재검색). 저장했다고 바로 인용하지 않는다.
- 지식 재사용의 지연 구조: 첫 요청의 지연에는 수집·본문 검사·서버 요약·임베딩·색인
  대기가 포함되고, 후속 요청은 웹 조회 없이 근거를 재사용한다. 어느 단계가 비용인지
  trace에서 분해해 읽는 훈련이 이 실습의 핵심이다.

질문 전체 대신 호출자가 지정한 public_topic만 외부 검색으로 보낸다. 외부 기술 문서는
내부 계약의 효력을 바꾸지 않는다. transient는 이번 응답의 근거에만 쓰고,
persist는 저장/색인 준비를 확인한 뒤 반드시 공통 검색 경로로 다시 읽는다.
같은 workspace에서 후속 질문을 실행하면 저장된 문서를 재사용할 수 있다.

첫 질문은 내부 runbook의 점검 순서와 HTTPX 공식 문서의 네 가지 timeout을 함께 묻는다.
같은 workspace에서 question을 "HTTPX에서 연결 풀 대기 timeout은?"으로 바꿔 다시
실행하면, 이번에는 calls에 exa_search/ingest가 없고 원문 인용이 유지되는지 확인한다.
첫 요청의 지연에는 수집·본문 검사·서버 요약·임베딩·색인 대기가 포함되고, 후속 요청은
웹 조회 없이 근거를 재사용한다. 이 지연 차이의 원인 목록을 result.json의 trace에서
하나씩 짚는 것이 이 실습의 관찰 포인트다.

심화 연결: 12번 과제에서 만료된 적재 Worker의 늦은 완료와 버전 갱신 경합을 다룬다.
"""

from langgraph.graph import END, START, StateGraph

from common.contracts import State
from common.day02_bridge import Evidence
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# web 모드 비교는 서로 다른 workspace에서 각각 실행한다(off/transient/persist).
#   web="off"        — 기존 RAG만 사용. HTTPX 근거는 부족으로 보류되는 기준선.
#   web="transient"  — 웹 원문을 이번 응답에만 사용. 코퍼스는 바뀌지 않는다.
#   web="persist"    — 관련성·본문 검사 후 저장하고 공통 RAG로 재검색한다.
# persist 재사용 실험: 아래 설정으로 한 번 실행한 뒤 question만 바꿔 재실행한다.
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab08-persist",
    question=(
        "내부 runbook의 타임아웃 증가 점검 순서와 HTTPX의 connect/read/write/pool "
        "timeout 종류를 근거와 함께 설명하세요."
    ),
    public_topic="HTTPX timeout connect read write pool official documentation",
    web="persist",
    max_web_documents=3,
)


def build(service):
    def retrieve(state):
        return {"evidence": [e.model_dump() for e in service.retrieve(state["question"])]}

    # persist가 반환한 웹 API 응답을 바로 인용하지 않는다. ensure 안의 재검색으로
    # 다른 구조에서도 검색 가능한지 확인한다. transient만 이번 요청의 임시 근거를 합친다.
    def enrich(state):
        refs = service.ensure([Evidence.model_validate(e) for e in state["evidence"]])
        return {"evidence": [e.model_dump() for e in refs]}

    def answer(state):
        refs = [Evidence.model_validate(e) for e in state["evidence"]]
        answer = service.generate(refs)
        return {"result": service.finish(answer, refs, service.review(answer, refs))}

    graph = StateGraph(State)
    graph.add_node("existing_rag", retrieve)
    graph.add_node("web_enrichment", enrich)
    graph.add_node("answer", answer)
    graph.add_edge(START, "existing_rag")
    graph.add_edge("existing_rag", "web_enrichment")
    graph.add_edge("web_enrichment", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


if __name__ == "__main__":
    run_lab("web", 실험, build)
