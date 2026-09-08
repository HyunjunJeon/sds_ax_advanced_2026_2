"""07. [학생 구현] 현재 담당자가 다음 응답 담당자에게 Command로 제어권을 넘긴다.

가르치는 것:
- 제어권 이전과 결과 반환의 차이: Subagent(03/05/06)는 부모에게 결과를 돌려주지만
  Handoff는 현재 담당자가 최종 답한다. Command(update=..., goto=...) 하나가 상태
  변경과 이동을 원자적으로 표현한다.
- 담당자 기억의 손익: 후속 질문에서 담당자를 유지하면 대화가 이어지지만, 잘못
  유지하면 다른 영역의 누락이 생긴다. 왕복 상한(2회)은 무한 전환을 끊는 안전장치다.
- 과거 대화와 현재 근거의 구분: 담당자를 유지해도 현재 주장은 다시 검색한 원문으로
  만들어야 한다. 대화 기억은 사실 근거가 아니다.

이 파일의 그래프도 직접 구현한다. TODO 1~4를 채우고 challenge_tests/
test_graph_challenge.py의 handoff 검사가 통과하면 완성이다.

Subagent(03/05/06)가 부모에게 결과를 돌려주는 구조와 달리, 여기서는 현재 owner가
최종 답한다. LangGraph에서 제어권 이전은 노드가 다음에 실행할 노드를 지정하는
Command(update=..., goto=...)로 표현한다. update와 goto를 한 Command에 담아야
다음 노드가 바뀐 상태(owner, handoffs)를 본다.

만들어야 할 구조:
    START → resume_owner → (역할 노드 6종: general/contract/operations/
                            security/policy/web, ROLES 순서로 등록)

- HandoffState: contracts.State를 확장해 handoffs(전환 횟수) 필드를 추가한다.
- resume_owner 노드: 다음 턴을 이어갈 담당자를 정한다.
  service.history.get("owner", "general")이 이전 담당자다. service.no_owner_memory가
  True면 general에서 다시 시작한다(대조 실험). 알 수 없는 역할이면 ValueError.
  Command(update={"owner": 담당자, "handoffs": 0}, goto=담당자)로 시작한다.
- owner_node(role): 역할별 respond 함수를 만드는 팩토리.
  1) r = service.route(role)로 현재 담당자가 응답할지 넘길지 판단한다.
  2) r.owner != role이면 담당자 전환이다.
     - state["handoffs"] >= 2면 왕복 상한이다. needs_clarification 결과로 END.
     - 아니면 meter.event("handoff", previous_owner=role, owner=r.owner, reason=r.reason)을
       남기고 Command(update={owner 갱신, handoffs + 1}, goto=r.owner)로 넘긴다.
  3) r.owner == role이면 응답할 차례다. meter.event("owner_retained", owner=role) 후
     service.worker(Task(id="owner-" + role, role=role, objective=state["question"]))을
     호출하고 결과에 result["owner"] = role을 붙여 Command(update=..., goto=END).

실패 상황(이 구조가 지켜야 할 것):
- 전환이 왕복하면 끝나지 않는다. handoffs 상한(2)으로 needs_clarification 종료.
- 과거 대화는 질문 해석의 단서일 뿐이다. 담당자를 유지했다고 현재 주장을 과거
  답변으로 대체하면 안 되고, 현재 원문을 다시 검색해야 한다(service.worker가 처리).
- 담당자 유지 비용과, 잘못 유지된 담당자로 인한 누락을 함께 측정한다.

구현 완료 후의 비교 실험(README §3 대표 실험 "담당자 유지와 전환"):
아래 실험 설정으로 두 질문을 같은 workspace·thread에서 순서대로 실행한다
(question을 바꿔 두 번 실행). 이어 "운영 접근 로그의 보관 예외는?"을 물으면
contract에서 operations로 담당자가 넘어가는지 관찰한다. trace의 owner_retained와
handoff(previous_owner/owner)로 설명한다. no_owner_memory=True 실행과 비교하면
담당자 기억의 효과가 드러난다.
"""

from langgraph.graph import START, StateGraph

from common.contracts import ROLES, State
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 후속 질문 실험을 위해 workspace/thread는 첫 질문 실행과 같은 값을 유지한다.
# 두 번째 실행에서 question만 아래로 바꾼다:
#   "그 약정이 가용률 목표도 바꾸나요?"
# 대조군: no_owner_memory=True로 바꾸고 같은 두 질문을 반복한다.
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab07-handoff",
    thread="demo",
    question="알파 서비스의 신청 기한과 추가 약정을 설명하세요.",
)


class HandoffState(State):
    handoffs: int


def build(service):
    def resume_owner(state):
        # TODO 1: 이전 담당자(service.history.get("owner", "general"), no_owner_memory면
        #         "general")을 검증하고 Command(update={"owner": ..., "handoffs": 0},
        #         goto=담당자)를 반환한다.
        raise NotImplementedError("TODO 1: resume_owner 노드를 구현하세요")

    def owner_node(role):
        def respond(state):
            # TODO 2~4: 위 모듈 docstring의 owner_node 규격을 따라 구현한다.
            #   TODO 2: service.route(role) 결과가 다르면 handoff(상한 2) 또는 종료.
            #   TODO 3: 같으면 service.worker(...) 호출 후 result["owner"] = role.
            #   TODO 4: 모든 분기의 반환은 Command(update=..., goto=...)다.
            raise NotImplementedError("TODO 2~4: respond를 구현하세요")

        return respond

    graph = StateGraph(HandoffState)
    graph.add_node("resume_owner", resume_owner)
    graph.add_edge(START, "resume_owner")
    for role in ROLES:
        graph.add_node(role, owner_node(role))
    # 역할 노드들은 Command(goto=...)로만 이동한다. START→resume_owner 외에
    # 정적 엣지를 추가하지 않는다.
    return graph.compile()


if __name__ == "__main__":
    run_lab("handoff", 실험, build)
