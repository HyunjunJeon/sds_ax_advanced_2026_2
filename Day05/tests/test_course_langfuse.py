"""새 수업의 Dataset 실행과 후속 Score 연결. 외부 서비스는 가짜 클라이언트로 검사한다."""

from contextlib import contextmanager
from types import SimpleNamespace as NS

from langfuse.api.commons.errors.not_found_error import NotFoundError
import pytest

from business_lab.authoring import draft_cases, freeze
from business_lab.dataset import load_fixtures
from business_lab.evidence import fetch_observations, push_scores
from business_lab.lesson_runs import grade_recording, record_release


class Dataset:
    def __init__(self, client):
        self.client, self.items, self.version = client, [], "frozen-test-version"

    def run_experiment(self, **kwargs):
        assert kwargs["evaluators"] == []  # 03은 실행만 하고 채점은 나중에 한다.
        assert kwargs["max_concurrency"] == 1
        self.client.runs.append(kwargs["run_name"])
        for item in self.items:
            self.client.trace_id = f"trace-{len(self.client.outputs)}"
            self.client.outputs.append(kwargs["task"](item=item))
        return NS(dataset_run_url="https://example.invalid/recording")


class Client:
    def __init__(self):
        self.dataset, self.trace_id = None, None
        self.runs, self.outputs, self.stack, self.spans = [], [], [], []
        self.scores = {}

    def get_dataset(self, *args, **kwargs):
        if self.dataset is None:
            raise NotFoundError(body={})
        return self.dataset

    def create_dataset(self, **kwargs):
        self.dataset = Dataset(self)

    def create_dataset_item(self, **kwargs):
        self.dataset.items.append(NS(**kwargs))

    def get_current_trace_id(self):
        return self.trace_id

    def get_current_observation_id(self):
        return self.stack[-1] if self.stack else None

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        oid = f"observation-{len(self.spans)}"
        self.spans.append(kwargs | {"id": oid, "parent": self.get_current_observation_id()})
        self.stack.append(oid)
        try:
            yield NS(id=oid, update=lambda **kw: None)
        finally:
            self.stack.pop()

    def create_score(self, **kwargs):
        self.scores[kwargs["score_id"]] = kwargs

    def flush(self):
        pass


def test_recording_and_scoring_share_dataset_trace_and_service_evidence(tmp_path, monkeypatch):
    import langfuse

    @contextmanager
    def attrs(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "propagate_attributes", attrs)
    cards = draft_cases()
    for c in cards:
        c["metadata"].update(review_status="approved", reviewer="test-only", review_reason="가짜 검수 자료")
    frozen = freeze(cards, load_fixtures(), tmp_path / "dataset.json", scenario_ids=["clarification-episode"])
    client = Client()
    raw = record_release(frozen, release="baseline", prompt="test", repeats=2, mode="scripted",
                         use_langfuse=True, out=tmp_path / "raw.json", client=client)
    assert len(client.runs) == len(raw["rows"]) == 2 and not client.scores
    assert client.dataset.items[0].input["max_turns"] == 2 and client.dataset.items[0].input["followups"]
    for row in raw["rows"]:
        assert row["trace_id"]
        assert len(row["artifact"]["turns"]) == 2
        assert all(e["parent_observation_id"] and e["observation_id"] for e in row["artifact"]["events"])
    scored = grade_recording(raw)
    # 차단된 쓰기의 근거를 붙이는 코드 경로도 검증한다.
    event = scored["rows"][0]["artifact"]["events"][0]
    scored["rows"][0]["verdicts"].append({"name": "test_policy", "status": "FAIL", "passed": False,
        "reason": "테스트용 위반", "evidence_ids": [event["event_id"]]})
    push_scores(scored, client)
    count = len(client.scores)
    push_scores(scored, client)
    assert len(client.scores) == count  # 같은 채점의 재전송은 같은 Score ID.
    assert any(s.get("observation_id") == event["observation_id"] for s in client.scores.values())
    assert all(s["metadata"]["source_run_id"] == raw["run_id"] and s["metadata"]["mode"] == "scripted" for s in client.scores.values())


def test_observation_pages_are_fully_read_and_missing_ids_reported():
    calls = []

    def get_many(**kwargs):
        calls.append(kwargs)
        ids, cursor = (["one"], "next") if kwargs["cursor"] is None else (["two"], None)
        return NS(data=[NS(model_dump=lambda mode, oid=oid: {"id": oid}) for oid in ids], meta=NS(cursor=cursor))

    client = NS(api=NS(observations=NS(get_many=get_many)))
    raw = {"started_at": "2026-09-10T00:00:00+00:00", "ended_at": "2026-09-10T00:01:00+00:00",
           "rows": [{"trace_id": "trace", "artifact": {"events": [{"observation_id": "one"}, {"observation_id": "absent"}]}}]}
    result = fetch_observations(client, raw)
    assert len(calls) == 2
    assert [o["id"] for o in result["trace"]["observations"]] == ["one", "two"]
    assert result["trace"]["missing_service_observation_ids"] == ["absent"]


def test_repeated_page_cursor_is_an_error():
    client = NS(api=NS(observations=NS(get_many=lambda **kw: NS(data=[], meta=NS(cursor="same")))))
    raw = {"started_at": "2026-09-10T00:00:00+00:00", "ended_at": "2026-09-10T00:01:00+00:00",
           "rows": [{"trace_id": "trace", "artifact": {"events": []}}]}
    with pytest.raises(ValueError, match="cursor"):
        fetch_observations(client, raw)
