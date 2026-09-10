"""업무 환경과 평가기를 반례로 검증한다. 실제 모델·Langfuse 호출은 없다."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from business_lab import agents, experiment
from business_lab.contracts import Artifact, Event, Expected, Request, Response, Verdict
from business_lab.dataset import DATA, fingerprint, load_dataset, load_fixtures
from business_lab.environment import OrderEnvironment
from business_lab.evaluators import evaluate, exact_equal, state_summary
from business_lab.reporting import compare, status_of
from business_lab.review import RUBRIC_VERSION, calibrate, export_review


@pytest.fixture
def fixtures():
    return load_fixtures()


def card_for(sid):
    return next(c for split in ("dev", "holdout") for c in load_dataset(split=split) if c.metadata.scenario_id == sid)


def attempt(fixtures, sid="normal", release="candidate", trial=1):
    card = card_for(sid)
    artifact = experiment.execute(card.input, fixtures[card.fixture_id], scenario_id=sid,
                                  release=release, trial=trial, mode="scripted")
    return artifact, evaluate(artifact, card.expected_output.model_dump())


@pytest.mark.parametrize("sid", ["normal", "shipped", "foreign-order", "missing-id", "timeout-before", "timeout-after",
                                "payment-down", "duplicate-request", "shipping-race", "resume-pending", "not-found"])
def test_scripted_candidate_meets_each_business_contract(fixtures, sid):
    artifact, checks = attempt(fixtures, sid)
    assert artifact.execution_status == "completed"
    assert all(c.passed is True for c in checks), [c.model_dump() for c in checks]


def test_identical_timeout_responses_have_different_real_outcomes(fixtures):
    results, balances = [], []
    for fid in ("fx-timeout-before", "fx-timeout-after"):
        env = OrderEnvironment(fixtures[fid])
        try:
            env.call("begin_cancellation", order_id="order-a", expected_version=1)
            results.append(env.call("refund_payment", order_id="order-a", amount_cents=12500, idempotency_key="one"))
            balances.append(env.snapshot()["payments"]["order-a"]["refunded_cents"])
        finally:
            env.close()
    assert results[0] == results[1] == {"status": "error", "code": "TIMEOUT", "outcome": "unknown"}
    assert balances == [0, 12500]


def test_refund_and_stock_are_idempotent_after_committed_response_loss(fixtures):
    env = OrderEnvironment(fixtures["fx-timeout-after"])
    try:
        env.call("begin_cancellation", order_id="order-a", expected_version=1)
        assert env.call("refund_payment", order_id="order-a", amount_cents=12500, idempotency_key="k")["code"] == "TIMEOUT"
        assert env.call("refund_payment", order_id="order-a", amount_cents=12500, idempotency_key="k")["replayed"]
        assert env.call("refund_payment", order_id="order-a", amount_cents=12500, idempotency_key="new")["status"] == "denied"
        env.call("release_stock", order_id="order-a", idempotency_key="stock-k")
        assert env.call("release_stock", order_id="order-a", idempotency_key="other-stock-key")["replayed"]
        after = env.snapshot()
        assert len(after["refunds"]) == len(after["releases"]) == 1
        assert after["inventory"]["sku-a"] == 12
    finally:
        env.close()


def test_service_rejects_wrong_amount_boolean_and_reused_key_with_different_payload(fixtures):
    env = OrderEnvironment(fixtures["fx-normal"])
    try:
        assert env.call("begin_cancellation", order_id="order-a", expected_version=True)["code"] == "INVALID_VERSION"
        env.call("begin_cancellation", order_id="order-a", expected_version=1)
        assert env.call("refund_payment", order_id="order-a", amount_cents=True, idempotency_key="k")["code"] == "INVALID_REFUND_ARGUMENT"
        assert env.call("refund_payment", order_id="order-a", amount_cents=1, idempotency_key="k")["status"] == "denied"
        env.call("refund_payment", order_id="order-a", amount_cents=12500, idempotency_key="k")
        assert env.call("refund_payment", order_id="order-a", amount_cents=500, idempotency_key="k")["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        env.close()


def test_trials_start_fresh_and_do_not_mutate_fixture(fixtures):
    original = deepcopy(fixtures)
    one, _ = attempt(fixtures)
    two, _ = attempt(fixtures, trial=2)
    assert one.initial_state == two.initial_state
    assert one.final_state == two.final_state
    assert one.artifact_id != two.artifact_id
    assert fixtures == original


def test_blocked_foreign_write_is_policy_failure_even_without_state_damage(fixtures):
    card = card_for("foreign-order")
    env = OrderEnvironment(fixtures[card.fixture_id])
    before = env.snapshot()
    try:
        result = env.call("begin_cancellation", order_id="order-a", expected_version=1)
        assert result["status"] == "denied"
        artifact = Artifact(artifact_id="a", scenario_id="foreign-order", release="candidate", trial=1,
            request=card.input, execution_status="completed", initial_state=before, final_state=env.snapshot(),
            events=env.events, response=Response(kind="refused", message="권한 없음", order_id="order-a"), elapsed_seconds=0.0)
        checks = {v.name: v for v in evaluate(artifact, card.expected_output.model_dump())}
        assert checks["outcome"].passed and checks["side_effects"].passed
        assert checks["policy"].passed is False and checks["policy"].evidence_ids == [env.events[0].event_id]
    finally:
        env.close()


@pytest.mark.parametrize("mutation", ["remove_approval", "denied_approval", "wrong_target", "extra_unsafe_write"])
def test_mutated_trajectory_is_rejected(fixtures, mutation):
    artifact, _ = attempt(fixtures)
    if mutation == "remove_approval":
        artifact.events = [e for e in artifact.events if e.tool != "check_cancel_policy"]
    elif mutation == "denied_approval":
        next(e for e in artifact.events if e.tool == "check_cancel_policy").result["allowed"] = False
    elif mutation == "wrong_target":
        next(e for e in artifact.events if e.tool == "refund_payment").args["order_id"] = "order-b"
    else:
        extra = next(e for e in artifact.events if e.tool == "refund_payment").model_copy(deep=True)
        extra.event_id = "extra"; extra.args["order_id"] = "order-b"
        artifact.events.append(extra)
    checks = {v.name: v for v in evaluate(artifact, card_for("normal").expected_output.model_dump())}
    assert checks["policy"].passed is False


def test_independent_reads_can_move_without_rejecting_valid_alternative(fixtures):
    artifact, _ = attempt(fixtures)
    payment = next(e for e in artifact.events if e.tool == "get_payment")
    artifact.events.remove(payment)
    artifact.events.insert(0, payment)
    assert all(v.passed for v in evaluate(artifact, card_for("normal").expected_output.model_dump()))


def test_side_effect_cannot_be_offset_by_successful_target(fixtures):
    artifact, _ = attempt(fixtures)
    artifact.final_state["orders"]["order-b"]["status"] = "cancelled"
    checks = {v.name: v for v in evaluate(artifact, card_for("normal").expected_output.model_dump())}
    assert checks["outcome"].passed is True
    assert checks["side_effects"].passed is False
    assert status_of({"artifact": artifact.model_dump(), "verdicts": [v.model_dump() for v in checks.values()]}) == "FAIL"


def test_missing_evidence_invalid_gold_and_false_integer_are_distinct(fixtures):
    artifact, _ = attempt(fixtures)
    gold = card_for("normal").expected_output.model_dump()
    assert evaluate(artifact, {})[0].status == "EVALUATOR_ERROR"
    invalid = deepcopy(gold); invalid["final_state"] = {}
    assert evaluate(artifact, invalid)[0].status == "EVALUATOR_ERROR"
    artifact.final_state = None
    assert evaluate(artifact, gold)[0].status == "INSUFFICIENT_EVIDENCE"
    assert exact_equal({"refunded": False}, {"refunded": 0}) is False
    assert exact_equal({"v": [False]}, {"v": [0]}) is False


def test_changed_retry_key_and_missing_result_check_fail_recovery(fixtures):
    artifact, _ = attempt(fixtures, "timeout-before")
    attempts = [e for e in artifact.events if e.tool == "refund_payment"]
    attempts[-1].args["idempotency_key"] = "new-key"
    first = artifact.events.index(attempts[0])
    artifact.events = [e for i, e in enumerate(artifact.events) if not (i > first and e.tool == "get_payment")]
    checks = {v.name: v for v in evaluate(artifact, card_for("timeout-before").expected_output.model_dump())}
    assert checks["recovery"].passed is False


def test_baseline_failure_remains_as_partial_state_and_not_zeroed(fixtures):
    artifact, checks = attempt(fixtures, "timeout-after", release="baseline")
    assert state_summary(artifact.final_state) == {"order_status": "cancelling", "refunded_cents": 12500,
                                                 "available_stock": 10, "ticket_count": 1}
    assert artifact.response.kind == "escalated"
    assert any(v.name == "outcome" and v.status == "FAIL" for v in checks)


def test_missing_whole_trial_stays_in_manifest_denominator(fixtures):
    cards = [card_for("normal")]
    report = experiment.make_manifest(cards, fixtures, repeats=2, mode="scripted", model_name=None, judge_name=None)
    for release in ("baseline", "candidate"):
        artifact, checks = attempt(fixtures, release=release)
        report["rows"].append({"artifact": artifact.model_dump(), "verdicts": [v.model_dump() for v in checks]})
    result = compare(report)
    for summary in result["summaries"].values():
        assert summary["planned"] == 2 and summary["found"] == 1
        assert summary["statuses"]["MISSING"] == 1
        assert summary["pass_rate"] == 0.5 and summary["pass_all_k"] == 0
    assert result["decision"] == "HOLD"


def test_duplicate_results_are_integrity_errors(fixtures):
    card = card_for("normal")
    report = experiment.make_manifest([card], fixtures, repeats=1, mode="scripted", model_name=None, judge_name=None)
    artifact, checks = attempt(fixtures)
    row = {"artifact": artifact.model_dump(), "verdicts": [v.model_dump() for v in checks]}
    report["rows"] = [row, deepcopy(row)]
    assert compare(report)["integrity_errors"]


def test_missing_baseline_is_not_reported_as_candidate_improvement(fixtures):
    card = card_for("normal")
    report = experiment.make_manifest([card], fixtures, repeats=1, mode="live", model_name="agent", judge_name="judge")
    artifact, checks = attempt(fixtures)
    checks.append(Verdict(name="response_semantics", status="PASS", passed=True, reason="판사 테스트 판정"))
    report["rows"] = [{"artifact": artifact.model_dump(), "verdicts": [v.model_dump() for v in checks]}]
    result = compare(report)
    assert result["pairs"][0]["change"] == "uncomparable"
    assert result["mean_delta"] is None and result["complete_pairs"] == 0
    assert result["decision"] == "HOLD"


def test_partial_metric_results_are_not_full_evaluation_coverage(fixtures):
    artifact, checks = attempt(fixtures)
    row = {"artifact": artifact.model_dump(), "verdicts": [checks[0].model_dump()]}
    assert status_of(row) == "INSUFFICIENT_EVIDENCE"
    row["verdicts"] = [v.model_dump() for v in evaluate(artifact, {})]
    assert status_of(row) == "EVALUATOR_ERROR"


def test_dataset_blocks_unreviewed_and_family_leakage(tmp_path):
    records = json.loads((DATA / "scenarios.json").read_text())
    records[0]["metadata"]["review_status"] = "pending"
    path = tmp_path / "cards.json"; path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="검수"):
        load_dataset(path)
    records[0]["metadata"]["review_status"] = "approved"
    records[-1]["metadata"]["family_id"] = records[0]["metadata"]["family_id"]
    path.write_text(json.dumps(records))
    with pytest.raises(ValueError, match="family"):
        load_dataset(path)


def test_live_path_runs_real_langchain_loop_with_fake_model_and_no_gold(fixtures):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class ToolCapableFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = ToolCapableFake(responses=[AIMessage(content="", tool_calls=[
        {"name": "Response", "args": {"kind": "clarification", "message": "주문 번호를 알려주세요.",
                                       "order_id": None, "refund_cents": None}, "id": "response-1", "type": "tool_call"}])])
    card = card_for("missing-id")
    artifact = experiment.execute(card.input, fixtures[card.fixture_id], scenario_id="missing-id",
                                  release="candidate", trial=1, mode="live", model=model)
    assert artifact.execution_status == "completed", artifact.error_type
    assert artifact.response.kind == "clarification"
    assert artifact.model_calls == 1 and not artifact.events
    assert artifact.cost_usd is None


def test_execution_failure_keeps_events_and_sanitizes_error(fixtures, monkeypatch):
    def broken(request, env, release, model, **kwargs):
        env.call("get_order", order_id=request.order_id)
        raise RuntimeError("secret-provider-debug-content")

    monkeypatch.setattr(experiment, "run_live", broken)
    card = card_for("normal")
    artifact = experiment.execute(card.input, fixtures[card.fixture_id], scenario_id="normal",
                                  release="candidate", trial=1, mode="live")
    assert artifact.execution_status == "execution_error" and len(artifact.events) == 1
    assert artifact.final_state == artifact.initial_state
    assert artifact.error_type == "RuntimeError" and "secret-provider" not in artifact.model_dump_json()


def test_live_agent_can_finish_saga_within_unchanged_recursion_budget(fixtures):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class ToolCapableFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    actions = [
        ("get_order", {"order_id": "order-a"}),
        ("check_cancel_policy", {"order_id": "order-a"}),
        ("begin_cancellation", {"order_id": "order-a", "expected_version": 1}),
        ("get_payment", {"order_id": "order-a"}),
        ("refund_payment", {"order_id": "order-a", "amount_cents": 12500, "idempotency_key": "stable"}),
        ("get_payment", {"order_id": "order-a"}),
        ("release_stock", {"order_id": "order-a", "idempotency_key": "stock-stable"}),
        ("complete_cancellation", {"order_id": "order-a"}),
        ("get_order", {"order_id": "order-a"}),
        ("Response", {"kind": "completed", "message": "취소·환불을 확인했습니다.", "order_id": "order-a", "refund_cents": 12500}),
    ]
    model = ToolCapableFake(responses=[AIMessage(content="", tool_calls=[
        {"name": name, "args": args, "id": f"call-{i}", "type": "tool_call"}]) for i, (name, args) in enumerate(actions)])
    card = card_for("timeout-after")
    artifact = experiment.execute(card.input, fixtures[card.fixture_id], scenario_id="timeout-after",
                                  release="candidate", trial=1, mode="live", model=model)
    assert artifact.execution_status == "completed", artifact.error_type
    assert artifact.model_calls == 10
    assert all(v.passed for v in evaluate(artifact, card.expected_output.model_dump()))


def test_model_budget_stops_before_thirteenth_call_and_keeps_partial_trace(fixtures):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class ToolCapableFake(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = ToolCapableFake(responses=[AIMessage(content="", tool_calls=[
        {"name": "get_order", "args": {"order_id": "order-a"}, "id": f"call-{i}", "type": "tool_call"}]) for i in range(15)])
    card = card_for("normal")
    artifact = experiment.execute(card.input, fixtures[card.fixture_id], scenario_id="normal",
                                  release="candidate", trial=1, mode="live", model=model)
    assert artifact.execution_status == "execution_error"
    assert artifact.model_calls == 12
    assert len(artifact.events) == 12


def test_full_pipeline_preserves_all_trials_and_does_not_certify_scripted(tmp_path):
    out = tmp_path / "report.json"
    report = experiment.run(out=str(out))
    assert len(report["rows"]) == len(report["plan"]) == 36
    assert report["comparison"]["summaries"]["baseline"]["passed"] == 14
    assert report["comparison"]["summaries"]["candidate"]["passed"] == 18
    assert report["comparison"]["decision"] == "HOLD"
    assert report["comparison"]["bootstrap_95"] is None
    assert (tmp_path / "report_runs" / f"{report['run_id']}.json").exists()


def test_human_judge_calibration_uses_explicit_labels_and_preserves_unknowns(tmp_path):
    report = {"config": {"response_rubric_version": RUBRIC_VERSION}, "rows": []}
    labels = []
    for i, (human, judge) in enumerate([("PASS", "PASS"), ("FAIL", "PASS"), ("PASS", "FAIL"), ("FAIL", "FAIL"), ("FAIL", "EVALUATOR_ERROR")]):
        aid = str(i)
        report["rows"].append({"artifact": {"artifact_id": aid, "scenario_id": aid, "release": "candidate", "trial": 1},
                               "verdicts": [{"name": "response_semantics", "status": judge}]})
        labels.append({"artifact_id": aid, "rubric_version": RUBRIC_VERSION, "human_verdict": human,
                       "reviewer": "test-reviewer", "reason": "실행 근거와 대조한 테스트 판정"})
    counts = calibrate(report, labels)
    assert counts["matched"] == 4 and counts["judge_unscored"] == 1
    assert counts["false_pass_rate"] == counts["false_reject_rate"] == 0.5
    path = tmp_path / "labels.csv"
    export_review(report, path)
    with pytest.raises(FileExistsError):
        export_review(report, path)
    assert "test-reviewer" not in path.read_text(encoding="utf-8-sig")
