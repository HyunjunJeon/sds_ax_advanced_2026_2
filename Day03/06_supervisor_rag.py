"""06. 의존성 있는 계획 → 준비된 작업 실행 → 합류 → 제한된 재계획.

가르치는 것:
- 작업 의존성(DAG) 관리: 선행 작업이 complete일 때만 후속을 실행하고, 실패한
  선행의 후속은 실행하지 않는다. 존재가 아니라 완료가 의존성을 충족한다.
- 실패의 분류와 대응: timeout/429/5xx는 회복 후보, 예산 초과는 즉시 중단, permanent
  실패는 재계획하지 않고 종료. 무엇을 재시도하고 무엇을 전파할지 정책으로 코드화한다.
- 재계획의 비용 절약: 성공한 작업은 results에 보존되어 재실행되지 않는다.
  오류 주입(fault)으로 이 제어 경로를 실제로 확인하는 방법도 함께 배운다.

Plan은 DAG를 검증하고, 선행 작업이 complete인 작업만 dispatch한다. 실패한 선행
작업의 후속 단계는 실행하지 않고 합류 검토로 넘어간다. 성공 결과는 상태에 남긴다.
revision 접두사는 재계획 전후 task id 충돌을 피한다. 그러나 이름을 바꿔 재제안한
동일 업무까지 코드가 판별하지는 못한다. 현재는 모델에게 완료 업무를 다시 주지 말라고
요청한다. 이 한계는 11번 과제에서 안정적인 작업 ID와 attempt/fencing으로 확장한다.
Timeout/429/5xx는 회복 후보, 예산 초과는 즉시 중단이다. 재계획은 최대 한 번이다.

이 파일은 구조를 통째로 제공하는 참고 구현이다. 05/07을 직접 구현한 뒤 이 파일과
비교하면 "결정적 dispatch 루프"와 "Send fan-out"의 차이를 읽을 수 있다.

관찰(오류 주입 실험):
- fault="operations:timeout" → operations Worker가 한 번 실패하고 재계획 후 회복된다.
  trace의 injected_timeout → supervisor_replan → worker_start 순서와, 성공한 다른
  작업이 재실행되지 않는지(results 보존)를 확인한다.
- 같은 실행을 no_replan=True로 돌리면 재계획 없이 누락으로 끝난다. 회복의 손익을
  실패 대조군과 비교할 수 있다.
- context_mode="summary"는 부모 작성자에게 원문 대신 Worker 요약만 전달한다.
  parent_context 이벤트의 바이트와 parent_evidence_rehydrated(검토 실패 시 원문
  복원)를 관찰한다. README §3 대표 실험.
"""

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from openai import APIConnectionError, APIStatusError

from common.contracts import State, Task
from common.lab import LabConfig, run_lab
from common.runtime import BudgetExceeded

# ── 실험 조건 ────────────────────────────────────────────────────────────────
# 한 번에 하나만 바꿔 실행한다. 기본은 오류 주입 없음.
#   fault="operations:timeout"       — 일시 실패 후 재계획 관찰
#   no_replan=True                   — 재계획 없이 실패를 노출하는 대조군
#   context_mode="summary"           — 요약 전달 모드(README §3 대표 실험)
실험 = LabConfig(
    backend="local",
    workspace="work/rag/lab06",
    question=(
        "검색 장애의 타임아웃 증가 점검 순서와 운영 접근 로그의 보관 기간·예외를 설명하세요."
    ),
    fault="",
    no_replan=False,
    context_mode="evidence",
)


def build(service):
    def plan(state):
        revision = state.get("revision", 0)
        completed = {
            k: v for k, v in state.get("results", {}).items() if v.get("status") == "complete"
        }
        p = service.plan(completed, state.get("result"))
        prefix = "r" + str(revision) + "-"
        tasks = [
            t.model_copy(
                update={"id": prefix + t.id, "depends_on": [prefix + x for x in t.depends_on]}
            ).model_dump()
            for t in p.tasks
        ]
        return {"plan": tasks, "revision": revision}

    def dispatch(state):
        done = state.get("results", {})
        # done에는 failed도 들어 있다. 의존성은 존재 여부가 아니라 complete로 충족된다.
        ready = [
            t
            for t in state["plan"]
            if t["id"] not in done
            and all(done.get(dep, {}).get("status") == "complete" for dep in t["depends_on"])
        ]
        if ready:
            return [
                Send("worker", {"task": t, "results": {k: done[k] for k in t["depends_on"]}})
                for t in ready
            ]
        return "join"

    def worker(state):
        t = Task.model_validate(state["task"])
        try:
            service.inject_fault(t.role)
            result = service.worker(
                t, prior=[v.get("answer", {}) for v in state.get("results", {}).values()]
            )
        # 공유 예산 소진은 특정 Worker의 일시 장애가 아니다. 다른 Worker의 재시도에도
        # 같은 예산이 적용되므로 상위 실행기로 전파해 요청 전체를 종료한다.
        except BudgetExceeded:
            raise
        except (TimeoutError, httpx.TimeoutException, APIConnectionError) as exc:
            result = {
                "status": "failed",
                "error": type(exc).__name__,
                "retryable": True,
                "evidence": [],
                "missing": [t.objective],
            }
        except APIStatusError as exc:
            result = {
                "status": "failed",
                "error": "HTTP_" + str(exc.status_code),
                "retryable": exc.status_code in {408, 429} or exc.status_code >= 500,
                "evidence": [],
                "missing": [t.objective],
            }
        result["task"] = t.model_dump()
        return {"results": {t.id: result}}

    def join(state):
        result = service.synthesize(state["results"])
        return {"result": result}

    def next_step(state):
        if (
            state["result"]["status"] == "complete"
            or state["revision"] >= 1
            or getattr(service, "no_replan", False)
        ):
            return END
        if any(
            v.get("status") == "failed" and not v.get("retryable")
            for v in state["results"].values()
        ):
            return END
        return "replan"

    # results를 비우지 않는 부분이 핵심이다. 성공한 조사 결과와 실패 이유를 함께 남긴다.
    def replan(state):
        service.meter.event(
            "supervisor_replan",
            missing=state["result"]["missing"],
            completed=[k for k, v in state["results"].items() if v["status"] == "complete"],
        )
        return {"revision": state["revision"] + 1}

    graph = StateGraph(State)
    for name, node in [
        ("plan", plan),
        ("worker", worker),
        ("join", join),
        ("replan", replan),
        ("dispatch", lambda s: {}),
    ]:
        graph.add_node(name, node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "dispatch")
    graph.add_conditional_edges("dispatch", dispatch)
    graph.add_edge("worker", "dispatch")
    graph.add_conditional_edges("join", next_step)
    graph.add_edge("replan", "plan")
    return graph.compile()


if __name__ == "__main__":
    run_lab("supervisor", 실험, build)
