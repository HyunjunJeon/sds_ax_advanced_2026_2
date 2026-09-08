"""05(병렬)·07(Handoff) 학생 구현 그래프의 공개 검사. 모델·EXA를 호출하지 않는다.

가짜 Service는 그래프가 지켜야 할 계약(parallel_plan/route/worker/synthesize)만
제공한다. 검증 대상은 그래프의 제어 흐름뿐이다:
- 05: Send fan-out으로 worker가 정말 동시에 시작하는지, 작업마다 정확히 한 번씩
  조사하고 reducer 합류가 충돌 없이 되는지.
- 07: 이전 담당자에서 시작하는지, 전환이 일어나는지, 왕복 상한이 지켜지는지.

미구현 상태에서 실패가 나오는 것이 정상이다. 예외를 지우지 말고 그래프를 구현한다.
"""

import threading
from types import SimpleNamespace

from common.lab import load_build


class FakeGraphService:
    """05/07 그래프가 호출하는 Service 계약만 구현한 오프라인 fixture."""

    def __init__(self):
        self.request = SimpleNamespace(question="운영과 보안", public_topic="")
        self.history = {"owner": "operations", "turns": []}
        self.meter = SimpleNamespace(event=lambda *a, **k: None)
        self.calls = []
        from common.contracts import Plan, Task

        self._plan = Plan(
            tasks=[
                Task(id="a", role="operations", objective="운영"),
                Task(id="b", role="security", objective="보안"),
            ],
            reason="fixture",
        )

    def payload(self):
        return {}

    def parallel_plan(self):
        # 실제 Service.parallel_plan과 같은 형태(Plan 객체 반환)를 제공한다.
        return self._plan

    def route(self, owner=None):
        from common.contracts import Route

        return Route(owner=owner or "operations", reason="fixture")

    def worker(self, task, prior=None):
        self.calls.append(task.role)
        return {
            "status": "complete",
            "answer": {"claims": []},
            "evidence": [],
            "missing": [],
            "role": task.role,
        }

    def synthesize(self, results):
        roles = {r.get("role") for r in results.values() if r["status"] == "complete"}
        ok = {"operations", "security"} <= roles
        return {"status": "complete" if ok else "uncertain", "missing": [] if ok else ["운영"]}


def test_parallel_runs_every_task_and_joins():
    # 분해된 작업 2개가 모두 worker로 가고(누락 없음), 합류 결과가 완성된다.
    service = FakeGraphService()
    result = load_build("parallel")(service).invoke(
        {"question": "운영과 보안", "results": {}}, config={"max_concurrency": 4}
    )
    assert sorted(service.calls) == ["operations", "security"]
    assert result["result"]["status"] == "complete"


def test_parallel_workers_really_overlap():
    # barrier에 도달한 worker가 서로를 기다린다 = 실제 동시 실행. 순차 실행이면
    # barrier.wait가 timeout으로 실패한다. 동시성을 주는 것은 Send fan-out뿐이다.
    service = FakeGraphService()
    barrier = threading.Barrier(2)
    original = service.worker

    def work(t, prior=None):
        barrier.wait(timeout=5)
        return original(t, prior)

    service.worker = work
    result = load_build("parallel")(service).invoke(
        {"question": "운영과 보안", "results": {}}, config={"max_concurrency": 4}
    )
    assert result["result"]["status"] == "complete"


def test_handoff_returns_from_current_owner():
    # 이전 턴의 담당자(operations)에서 시작해 유지되면 그 담당자가 최종 답한다.
    service = FakeGraphService()
    result = load_build("handoff")(service).invoke({"question": "그 조건은?", "results": {}})
    assert result["owner"] == "operations"
    assert result["result"]["owner"] == "operations"


def test_current_owner_hands_off_before_answering():
    # 현재 담당자가 다른 담당자를 지목하면 응답 전에 제어권이 넘어간다.
    from common.contracts import Route

    service = FakeGraphService()
    service.route = lambda owner: Route(
        owner="security" if owner == "operations" else owner, reason="업무 전환"
    )
    result = load_build("handoff")(service).invoke({"question": "로그 보관은?", "results": {}})
    assert result["owner"] == "security"
    # operations는 응답하지 않았다. 답한 것은 넘겨받은 security뿐이다.
    assert service.calls == ["security"]


def test_handoff_cycle_is_bounded():
    # 담당자가 서로를 계속 넘기면 왕복 상한(2회)에서 needs_clarification로 종료한다.
    from common.contracts import Route

    service = FakeGraphService()
    service.route = lambda owner: Route(
        owner="security" if owner == "operations" else "operations", reason="왕복 fixture"
    )
    result = load_build("handoff")(service).invoke({"question": "애매한 요청", "results": {}})
    assert result["result"]["status"] == "needs_clarification"
    assert result["handoffs"] == 2
    # 아무도 응답하지 못했다. 무한 전환 대신 질문을 되묻는 것이 올바른 종료다.
    assert not service.calls
