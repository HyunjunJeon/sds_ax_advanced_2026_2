import concurrent.futures
from types import SimpleNamespace

import pytest

from common.config import Settings
from common.contracts import Plan, Request, Task, merge_results
from common.exa_client import allowed_url
from common.ingestion import CandidateReview, Ingestion
from common.lab import load_build
from common.rag_backend import Backend
from common.runtime import BudgetExceeded, Meter
from common.source_registry import Registry, canonical_url, digest


@pytest.fixture
def local(tmp_path):
    settings = Settings(backend="local", workspace=tmp_path, web="persist", max_tools=300)
    registry = Registry(tmp_path / "registry.sqlite")
    meter = Meter(settings)
    backend = Backend(settings, registry, meter)
    backend.prepare()
    yield settings, registry, meter, backend
    backend.close()


def test_day02_scope_and_coordinates(local):
    _, registry, _, backend = local
    refs = backend.retrieve(
        "알파 가용률 신청 서비스 크레딧", Request(question="알파 가용률", entity="알파")
    )
    ids = {e.doc_id for e in refs}
    assert "sla-a-v2" in ids and "sla-a-amendment" in ids
    assert not ids & {"sla-b", "sla-a-v1", "sla-a-draft"}
    for e in refs:
        assert backend.resolve([e.evidence_id])[0] == e
    old = backend.retrieve("알파 가용률 신청", Request(question="가용률", as_of="2026-06-15"))
    assert "sla-a-v1" in {e.doc_id for e in old}
    assert "sla-a-v2" not in {e.doc_id for e in old}


def test_hash_and_coordinates_reject_forgery(local):
    _, _, _, backend = local
    refs = backend.retrieve("가용률", Request(question="가용률"))
    e = refs[0]
    with pytest.raises(ValueError):
        backend.remember([e], {(e.uri, e.content_sha256): ("원문 위조", 0)})
    with pytest.raises(ValueError):
        backend.resolve(["invented"])


def test_internal_only_does_not_use_web(local):
    s, r, m, b = local
    path = s.workspace / "fake.md"
    text = "애플리케이션 오류 로그는 7일 보관한다.\n"
    path.write_text(text)
    key, _ = r.reserve(
        "https://docs.python.org/fake", text, {"title": "로그", "published_at": None}, path
    )
    r.update(key, "ready", uri="local://web/" + key)
    refs = b.retrieve("애플리케이션 오류 로그 보관", Request(question="오류 로그 보관"))
    assert all(e.doc_id != key for e in refs)


def test_registry_versions_aliases_and_restart(tmp_path):
    r = Registry(tmp_path / "r.sqlite")
    a, _ = r.reserve("https://example.org/a", "one", {}, tmp_path / "a")
    r.update(a, "ready", uri="local://web/a")
    b, action = r.reserve("https://example.org/mirror", "one", {}, tmp_path / "b")
    assert (b, action) == (a, "reused")
    c, action = r.reserve("https://example.org/a", "two", {}, tmp_path / "c")
    assert c != a and action == "new"
    r.update(c, "ready", uri="local://web/c")
    assert len(Registry(r.path).rows()) == 2
    assert Registry(r.path).get(a)["hash"] == digest("one")


def test_parallel_reservation_has_one_owner(tmp_path):
    r = Registry(tmp_path / "r.sqlite")

    def reserve(i):
        return r.reserve("https://example.org/" + str(i), "same", {}, tmp_path / str(i))

    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert len({i for i, _ in results}) == 1
    assert sum(s == "new" for _, s in results) == 1
    assert sum(s == "pending" for _, s in results) == 3


def test_failed_ingestion_resumes_without_duplicate(tmp_path):
    r = Registry(tmp_path / "r.sqlite")
    a, _ = r.reserve("https://example.org/a", "one", {}, tmp_path / "a")
    r.update(a, "failed", error="TimeoutError")
    b, state = Registry(r.path).reserve("https://example.org/a", "one", {}, tmp_path / "a")
    assert a == b and state == "resumed"


def test_budget_atomic_across_workers(tmp_path):
    m = Meter(Settings(backend="local", workspace=tmp_path, max_tools=5))

    def run(_):
        try:
            m.consume("read")
            return True
        except BudgetExceeded:
            return False

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        counts = list(pool.map(run, range(20)))
    assert sum(counts) == 5
    m.started -= 1000
    with pytest.raises(BudgetExceeded):
        m.check()


def test_url_canonicalization_and_domain_boundary():
    assert (
        canonical_url("https://EXAMPLE.org/a?utm_source=x&v=2#top") == "https://example.org/a?v=2"
    )
    assert allowed_url("https://docs.python.org/3/", ("docs.python.org",))
    assert not allowed_url("https://docs.python.org.evil.test/", ("docs.python.org",))
    assert not allowed_url("file:///tmp/a", ("docs.python.org",))
    assert not allowed_url("https://user:pass@docs.python.org/", ("docs.python.org",))


def test_dag_and_conflicting_join():
    with pytest.raises(ValueError):
        Plan(tasks=[Task(id="a", role="general", objective="a", depends_on=["b"])], reason="bad")
    with pytest.raises(ValueError):
        merge_results({"a": {"value": 1}}, {"a": {"value": 2}})
    assert merge_results({"a": {"value": 1}}, {"a": {"value": 1}}) == {"a": {"value": 1}}


class AcceptModels:
    def ask(self, *args):
        return CandidateReview(relevant=True, usable_body=True, reason="offline fixture")


class FakeExa:
    def __init__(self):
        self.calls = 0

    def search(self, topic):
        self.calls += 1
        return [
            {
                "url": "https://www.python-httpx.org/advanced/timeouts/",
                "title": "HTTPX timeouts",
                "text": ("HTTPX pool timeout waits for a connection from the pool.\n" * 5),
                "published_at": None,
                "public_topic": topic,
            }
        ]


def test_ingest_retrieve_reuse_and_transient(local):
    s, r, m, b = local
    exa = FakeExa()
    ing = Ingestion(s, r, b, m, AcceptModels(), exa)
    request = Request(question="HTTPX pool timeout", public_topic="HTTPX timeout")
    before = r.snapshot()
    ing.enrich(request)
    assert before != r.snapshot() and len(r.rows("web")) == 1
    found = b.retrieve(request.question, request)
    assert any(e.source.startswith("https://www.python-httpx.org") for e in found)
    ing.enrich(request)
    assert exa.calls == 1
    ing2 = Ingestion(s, r, b, m, AcceptModels(), exa)
    ing2.enrich(request)
    assert len(r.rows("web")) == 1
    s.web = "transient"
    snapshot = r.snapshot()
    transient = Ingestion(s, r, b, m, AcceptModels(), exa).enrich(request)
    assert transient and r.snapshot() == snapshot


def test_history_customer_scope_and_restart(tmp_path):
    r = Registry(tmp_path / "r.sqlite")
    r.save_history("alpha", {"owner": "contract", "turns": [{"question": "A"}]})
    assert Registry(r.path).history("alpha")["owner"] == "contract"
    assert Registry(r.path).history("beta")["turns"] == []


class GraphService:
    def __init__(self):
        self.request = SimpleNamespace(question="운영과 보안", public_topic="")
        self.history = {"owner": "operations", "turns": []}
        self.meter = SimpleNamespace(event=lambda *a, **k: None)
        self.models = SimpleNamespace(
            ask=lambda *a: Plan(
                tasks=[
                    Task(id="a", role="operations", objective="운영"),
                    Task(id="b", role="security", objective="보안"),
                ],
                reason="fixture",
            )
        )
        self.calls = []
        self.fault = True
        self.plans = 0

    def payload(self):
        return {}

    def route(self, owner=None):
        return SimpleNamespace(
            owner=owner or "operations",
            reason="fixture",
            model_dump=lambda: {"owner": "operations", "reason": "fixture"},
        )

    def plan(self, completed, feedback):
        self.plans += 1
        tasks = [Task(id="a", role="operations", objective="운영")]
        if not completed:
            tasks.append(Task(id="b", role="security", objective="보안"))
        return Plan(tasks=tasks, reason="fixture")

    def inject_fault(self, role):
        if role == "operations" and self.fault:
            self.fault = False
            raise TimeoutError("fixture")

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
        return {
            "status": "complete" if {"operations", "security"} <= roles else "uncertain",
            "missing": [] if {"operations", "security"} <= roles else ["운영"],
        }


def test_supervisor_preserves_success_on_retry():
    service = GraphService()
    result = load_build("supervisor")(service).invoke(
        {"question": "운영과 보안", "results": {}, "revision": 0}
    )
    assert result["result"]["status"] == "complete"
    assert service.calls.count("security") == 1
    assert service.calls.count("operations") == 1
    assert service.plans == 2


def test_parallel_plan_saves_then_replays_identically(tmp_path):
    # 병렬 비교의 엄밀화: plan_file이 있으면 첫 실행이 분해를 저장하고, 이후 실행은
    # 모델을 부르지 않고(ask 호출 수 1) 같은 계획을 재생한다. 두 실행의 trace에 모두
    # "plan" 이벤트가 남아야 13_mini_pjt의 계획 동일성 비교가 성립한다.
    from common.service import Service

    settings = Settings(backend="local", workspace=tmp_path)
    meter = Meter(settings)
    asked = []

    class PlanModels:
        def ask(self, *args, **kwargs):
            asked.append(1)
            return Plan(
                tasks=[Task(id="a", role="operations", objective="운영")], reason="fixture"
            )

    request = Request(question="운영")

    def make_service():
        return Service(
            settings, request, None, PlanModels(), meter, None, plan_file=tmp_path / "plan.json"
        )

    first = make_service().parallel_plan()
    second = make_service().parallel_plan()
    assert first == second
    assert len(asked) == 1  # 재생 실행은 모델을 부르지 않았다.
    events = [e["event"] for e in meter.events]
    assert events.count("plan") == 2 and "plan_replayed" in events

    # 의존성 있는 분해는 병렬 구조에서 거절한다(선행 작업 보장이 없는 fan-out이므로).

    class DependentModels:
        def ask(self, *args, **kwargs):
            return Plan(
                tasks=[
                    Task(id="a", role="operations", objective="운영", depends_on=["b"]),
                    Task(id="b", role="security", objective="보안"),
                ],
                reason="fixture",
            )

    svc = Service(settings, request, None, DependentModels(), meter, None)
    with pytest.raises(ValueError):
        svc.parallel_plan()


def test_version_reversion_moves_active_head(tmp_path):
    r = Registry(tmp_path / "r.sqlite")
    a, _ = r.reserve("https://example.org/a", "one", {}, tmp_path / "a")
    r.update(a, "ready", uri="local://web/a")
    b, _ = r.reserve("https://example.org/a", "two", {}, tmp_path / "b")
    r.update(b, "ready", uri="local://web/b")
    before = r.snapshot()
    assert [x["id"] for x in r.active_web()] == [b]
    r.reserve("https://example.org/a", "one", {}, tmp_path / "a")
    assert [x["id"] for x in r.active_web()] == [a]
    assert before != r.snapshot()
    assert len(r.rows()) == 2


def test_seed_isolates_heads_and_history(tmp_path):
    source = Registry(tmp_path / "source.sqlite")
    a, _ = source.reserve("https://example.org/a", "one", {}, tmp_path / "a")
    source.update(a, "ready", uri="local://web/a")
    source.save_history("t", {"owner": "web", "turns": [{"question": "old"}]})
    target = Registry(tmp_path / "target.sqlite")
    target.seed_from(source)
    assert target.snapshot() == source.snapshot()
    assert not target.history("t")["turns"]
    b, _ = target.reserve("https://example.org/a", "two", {}, tmp_path / "b")
    target.update(b, "ready", uri="local://web/b")
    assert target.snapshot() != source.snapshot()
    assert [x["id"] for x in source.active_web()] == [a]


def test_stored_but_not_searchable_is_not_ready(local):
    from common.rag_backend import IndexPending

    s, r, m, b = local
    item = FakeExa().search("HTTPX")[0]
    text = item["text"]
    s.backend = "viking"
    b.viking = SimpleNamespace(
        read=lambda *a, **k: text, find=lambda *a, **k: [], close=lambda: None
    )
    with pytest.raises(IndexPending):
        Ingestion(s, r, b, m, AcceptModels(), FakeExa()).store(item)
    assert not r.rows("web")
    assert r.rows("web", ready=False)[0]["state"] == "failed"


def test_unsupported_claim_is_not_rendered(local):
    from common.day02_bridge import Claim, GroundedAnswer, Review
    from common.service import Service

    s, r, m, b = local
    evidence = b.retrieve("가용률", Request(question="가용률"))
    answer = GroundedAnswer(
        claims=[Claim(text="틀린 결론", evidence_ids=[evidence[0].evidence_id])]
    )
    service = Service(s, Request(question="가용률"), b, None, m, None)
    result = service.finish(
        answer,
        evidence,
        Review(supported=False, unsupported_claims=["틀린 결론"], missing=[], retry_query=""),
    )
    assert result["status"] == "uncertain"
    assert "틀린 결론" not in result["text"]
    assert not result["answer"]["claims"]
