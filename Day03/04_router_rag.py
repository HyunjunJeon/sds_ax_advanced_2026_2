"""04. 최초 질문을 한 명의 전문 담당자에게 배정한다.

가르치는 것:
- 라우팅은 추가 호출 1회로 전문화를 얻는 값싼 패턴이지만, 결과 합류·재계획이 없다는
  구조적 한계를 함께 가진다. 두 영역을 묻는 질문에서 남는 누락이 그 증거다.
- 모델의 선택은 스키마(Route)로 허용된 역할 안에 가둬야 한다. 자유 문자열 출력은
  임의 코드·도구 이름으로 이어질 수 있기 때문이다.
- Router(최초 배정)와 Handoff(07, 응답 주체 변경)의 차이를 같은 사례로 구분한다.

Router의 출력은 허용된 역할 이름과 이유다. 역할은 시스템 프롬프트를 바꾸지만
고객/기준일 필터나 인용 검증을 우회할 권한을 부여하지 않는다.
이 구조는 결과 합류와 재계획을 하지 않는다. 계약과 운영이 함께 필요한 질문이
한 담당자에게 배정될 때의 누락을 05/06과 비교한다.
07 Handoff와 비교: 여기서는 최초 선택 후 사용자의 응답 담당자를 다시 넘기지 않는다.

관찰: trace의 route 이벤트(선택된 역할과 이유)와 specialist의 worker 결과에서
질문의 나머지 절반(다른 역할 영역)이 누락되는지 확인한다.
"""

from langgraph.graph import END, START, StateGraph

from common.contracts import State, Task
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 이 질문은 operations와 security 두 영역을 묻고 있다. Router가 한쪽만 고르면
# "단일 담당자 구조의 한계"가 관찰된다. 그것이 이 실습의 포인트다.
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab04",
    question=(
        "검색 장애의 타임아웃 증가 점검 순서와 운영 접근 로그의 보관 기간·예외를 설명하세요."
    ),
)


def build(service):
    def route(state):
        # Route 스키마로 역할을 제한해 모델 문자열을 임의 코드/도구 이름으로 쓰지 않는다.
        r = service.route()
        service.meter.event("route", **r.model_dump())
        return {"owner": r.owner}

    def specialist(state):
        return {
            "result": service.worker(
                Task(id="routed", role=state["owner"], objective=state["question"])
            )
        }

    graph = StateGraph(State)
    graph.add_node("route", route)
    graph.add_node("specialist", specialist)
    graph.add_edge(START, "route")
    graph.add_edge("route", "specialist")
    graph.add_edge("specialist", END)
    return graph.compile()


if __name__ == "__main__":
    run_lab("router", 실험, build)
