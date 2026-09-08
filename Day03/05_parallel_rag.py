"""05. [학생 구현] 독립 조사 항목을 Send로 동시에 실행하고 근거를 합친다.

가르치는 것:
- 동시 실행의 실체: LangGraph의 Send fan-out은 같은 worker 노드를 서로 다른 입력으로
  병렬 인스턴스화하고, 동시 쓰기는 reducer가 합류한다. 직접 구현해 봐야 "동시"와
  "빠른 순차 호출"의 차이가 보인다.
- 병렬의 손익: 줄어드는 것은 전체 경과 시간뿐이고 합산 토큰·호출은 늘어난다.
  또한 Worker마다 별도 Context라 서로의 도구 대화가 보이지 않는다는 대가가 있다.
- 엄밀한 비교 방법: 분해가 매번 달라지면 동시 실행의 효과와 분해의 효과가 섞인다.
  plan_file 저장·재생으로 "같은 분해"를 고정하고 나서야 병렬/순차를 비교할 수 있다.

이 파일의 그래프는 직접 구현한다. 아래 가이드를 따라 build 안의 TODO 1~4를 채우고
challenge_tests/test_graph_challenge.py가 전부 통과하면 완성이다. 구현 전에는
NotImplementedError가 나는 것이 정상이다.

만들어야 할 구조(노드 4개):
    START → plan → (dispatch가 Send 목록 반환) → worker × N (동시 실행) → join → END

- plan 노드: service.parallel_plan()이 질문을 독립 조사 항목으로 분해한다.
  LabConfig.plan_file을 지정하면 첫 실행에서 분해를 파일로 남기고, 이후 실행은
  모델을 부르지 않고 그 계획을 그대로 재생한다. 순차(serial_workers) 실행과의
  엄밀한 비교는 "같은 분해"에서만 성립한다. 분해·검증·저장은 Service가 이미 하므로
  이 노드에서 할 일은 반환된 Plan을 State.plan에 dict로 옮기는 것뿐이다.
- dispatch 노드(조건부 엣지): 항목마다 Send("worker", {"task": t})를 반환해 fan-out을
  만든다. LangGraph의 Send는 같은 worker 노드를 서로 다른 입력으로 병렬 인스턴스화한다.
  Send 목록을 반환하는 노드는 자기 자신으로 흐르는 일반 엣지를 갖지 않는다.
- worker 노드: Task.model_validate(state["task"])로 입력을 복원하고 service.worker(t)로
  조사한 뒤 {"results": {t.id: 결과}}를 반환한다. 동시 실행 중 여러 worker가 같은
  State.results에 쓰므로 common/contracts.py의 merge_results reducer가 합류한다.
  작업 id를 키로 쓰지 않고 하나의 result 필드에 덮어쓰면 합류 충돌(ValueError)이다.
- join 노드: service.synthesize(state["results"])가 Worker가 넘긴 근거 ID를 원문
  레지스트리에서 해소해 종합/검토한다. Worker 요약을 그대로 정답으로 쓰지 않는다.

실패 상황(이 구조가 지켜야 할 것):
- Send 없이 for 문으로 service.worker를 부르면 동시 실행이 아니라 순차 호출이다.
  trace의 worker_start/worker_return 시각이 겹치는지로 확인할 수 있다.
- 의존성 있는 작업을 병렬로 돌리면 선행 결과 없이 조사가 시작된다. parallel_plan은
  depends_on이 하나라도 있으면 거절한다. 의존 작업은 06 Supervisor의 영역이다.
- Worker마다 별도 Context다. 다른 Worker의 도구 대화가 자동으로 공유되지 않는다.
  공유되는 것은 검증된 evidence registry와 요청 전체의 Meter뿐이다.

구현 완료 후의 비교 실험(엄밀한 병렬 vs 순차):
1) 아래 실험 설정 그대로(serial_workers=False) 실행한다. 첫 실행이 분해를
   work/plans/parallel-demo.json에 저장한다.
2) serial_workers=True와 label="parallel-serial"만 바꿔 다시 실행한다. 같은
   plan_file을 재생하므로 분해는 동일하고 동시 실행 수만 1이 된다.
3) 두 실행의 result.json에서 trace의 plan 이벤트가 같은지, worker_start/worker_return
   시각의 중첩, 전체 elapsed_s, 합산 모델 호출을 대조한다.
병렬이 줄일 수 있는 것은 전체 경과 시간이며 합산 토큰은 늘어날 수 있다.
13_mini_pjt.py에서 이 두 runs 파일을 require_same_plan=True로 짝 비교하면 된다.
"""

from langgraph.graph import START, StateGraph

from common.contracts import State
from common.lab import LabConfig, run_lab

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 병렬 vs 순차 비교: 아래 serial_workers와 label만 바꿔 가며 두 번 실행한다.
# plan_file은 두 실행에서 반드시 같은 경로여야 한다(같은 분해 재생).
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab05-parallel",
    question=(
        "검색 장애의 타임아웃 증가 점검 순서와 운영 접근 로그의 보관 기간·예외를 설명하세요."
    ),
    serial_workers=False,
    label="parallel",
    plan_file="work/plans/parallel-demo.json",
)


def build(service):
    def plan(state):
        # TODO 1: p = service.parallel_plan() 호출 후 {"plan": [t.model_dump() for t in p.tasks]}
        #         를 반환한다. 분해 검증(의존성 금지·web 범위)과 저장/재생은 Service가 한다.
        raise NotImplementedError("TODO 1: plan 노드를 구현하세요")

    def dispatch(state):
        # TODO 2: state["plan"]의 각 작업 dict에 대해 Send("worker", {"task": t})를 만들어
        #         리스트로 반환한다. 이 함수는 graph.add_conditional_edges의 라우터로 쓴다.
        raise NotImplementedError("TODO 2: dispatch(Send fan-out)를 구현하세요")

    def worker(state):
        # TODO 3: t = Task.model_validate(state["task"]) 후 service.worker(t)를 호출하고
        #         {"results": {t.id: 결과}}를 반환한다. 키가 작업 id여야 reducer가 합류한다.
        raise NotImplementedError("TODO 3: worker 노드를 구현하세요")

    def join(state):
        # TODO 4: {"result": service.synthesize(state["results"])}를 반환한다.
        raise NotImplementedError("TODO 4: join 노드를 구현하세요")

    graph = StateGraph(State)
    graph.add_node("plan", plan)
    graph.add_node("worker", worker)
    graph.add_node("join", join)
    graph.add_edge(START, "plan")
    # TODO 5: graph.add_conditional_edges("plan", dispatch)로 fan-out을 만들고,
    #         worker→join, join→END 엣지를 추가한 뒤 graph.compile() 결과를 반환한다.
    raise NotImplementedError("TODO 5: 그래프 엣지를 완성하고 compile 결과를 반환하세요")


if __name__ == "__main__":
    run_lab("parallel", 실험, build)
