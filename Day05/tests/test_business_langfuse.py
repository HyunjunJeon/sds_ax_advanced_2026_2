"""Langfuse 연결 계약을 가짜 클라이언트로 확인한다. 외부 데이터셋을 만들지 않는다."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from langfuse.api.commons.errors.not_found_error import NotFoundError

from business_lab.dataset import load_dataset, load_fixtures
from business_lab.experiment import assess, make_manifest
from business_lab.langfuse_adapter import prepare_dataset, run_hosted


class FakeDataset:
    def __init__(self, client):
        self.client = client
        self.items = []
        self.version = "frozen-version"

    def run_experiment(self, **kwargs):
        assert kwargs["max_concurrency"] == 1
        self.client.runs.append(kwargs["run_name"])
        for item in self.items:
            output = kwargs["task"](item=item)
            self.client.outputs.append(output)
            values = kwargs["evaluators"][0](input=item.input, output=output, expected_output=item.expected_output)
            assert {v.name for v in values} >= {"order.outcome", "order.policy", "order.side_effects"}
        return NS(dataset_run_url="https://example.invalid/test-run")


class FakeClient:
    def __init__(self):
        self.dataset = None
        self.creates = 0
        self.runs, self.outputs, self.spans = [], [], []

    def get_dataset(self, *args, **kwargs):
        assert kwargs.get("version") is not None
        if self.dataset is None:
            raise NotFoundError(body={})
        return self.dataset

    def create_dataset(self, **kwargs):
        self.dataset = FakeDataset(self)
        self.creates += 1

    def create_dataset_item(self, **kwargs):
        self.dataset.items.append(NS(input=kwargs["input"], expected_output=kwargs["expected_output"], metadata=kwargs["metadata"]))

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        span = NS(id=f"obs-{len(self.spans)}", update=lambda **kw: None)
        self.spans.append(kwargs)
        yield span

    def get_current_trace_id(self):
        return "trace-from-sdk"

    def create_score(self, **kwargs):
        assert kwargs["trace_id"] and kwargs["observation_id"]

    def flush(self):
        pass


def setup_report():
    cards = [load_dataset()[0]]
    fixtures = load_fixtures()
    report = make_manifest(cards, fixtures, repeats=2, mode="scripted", model_name=None, judge_name=None)
    return cards, fixtures, report


def test_existing_hosted_dataset_is_checked_without_overwriting_reviewed_edits():
    cards, fixtures, report = setup_report()
    client = FakeClient()
    prepare_dataset(client, report, cards)
    client.dataset.items[0].expected_output = deepcopy(client.dataset.items[0].expected_output)
    client.dataset.items[0].expected_output["final_state"]["order_status"] = "shipped"
    with pytest.raises(ValueError, match="동결"):
        prepare_dataset(client, report, cards)
    assert client.creates == 1
    assert client.dataset.items[0].expected_output["final_state"]["order_status"] == "shipped"


def test_hosted_runs_use_same_dataset_all_trials_and_keep_event_links(monkeypatch):
    import langfuse

    @contextmanager
    def no_network_attributes(**kwargs):
        yield

    monkeypatch.setattr(langfuse, "propagate_attributes", no_network_attributes)
    cards, fixtures, report = setup_report()
    client = FakeClient()
    saved = []

    def persist(artifact, trace_id):
        saved.append((artifact.artifact_id, trace_id))

    def record(artifact, expected, trace_id):
        assert (artifact.artifact_id, trace_id) in saved  # 평가 전에 이미 원자료가 보관된다
        return assess(artifact, expected)

    run_hosted(report, cards, fixtures, model=None, record=record, persist=persist, client=client)
    assert len(client.runs) == len(saved) == 4
    assert len(set(client.runs)) == 4
    assert client.creates == 1
    for output in client.outputs:
        artifact = output["artifact"]
        assert all(e["observation_id"] for e in artifact["events"])
        assert set(artifact["request"]) == {"message", "order_id", "request_id"}
        assert "expected_output" not in artifact["request"] and "fault" not in artifact["request"]
