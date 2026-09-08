"""B의 반례는 동일 사건의 도착 순서, 의존성, 동시성 슬롯, 합류 예산을 함께 바꾼다."""

from itertools import permutations

import pytest

from student_tasks import Attempt, Dispatch, WorkItem, merge_attempts, schedule_ready


def test_late_success_cannot_resurrect_an_old_attempt():
    late = Attempt("a", 1, "complete", "old")
    latest = Attempt("a", 2, "retryable")
    for order in permutations([late, latest, latest]):
        assert merge_attempts({}, list(order)) == {"a": latest}


def test_running_to_complete_is_order_independent_and_idempotent():
    observations = [Attempt("a", 1, "running"), Attempt("a", 1, "complete", "h")]
    for order in permutations(observations):
        result = merge_attempts({}, list(order))
        assert result == {"a": observations[1]}
        assert merge_attempts(result, list(order)) == result


@pytest.mark.parametrize(
    "other", [Attempt("a", 1, "complete", "different"), Attempt("a", 1, "terminal")]
)
def test_conflicting_final_results_are_not_last_write_wins(other):
    with pytest.raises(ValueError):
        merge_attempts({"a": Attempt("a", 1, "complete", "h")}, [other])


@pytest.mark.parametrize(
    "bad", [Attempt("", 1, "running"), Attempt("a", 0, "running"), Attempt("a", 1, "complete")]
)
def test_invalid_observations_are_rejected(bad):
    with pytest.raises(ValueError):
        merge_attempts({}, [bad])


def test_current_mapping_is_not_mutated_or_silently_miskeyed():
    before = {"a": Attempt("a", 1, "running")}
    result = merge_attempts(before, [Attempt("a", 1, "complete", "h")])
    assert before["a"].status == "running" and result["a"].status == "complete"
    with pytest.raises(ValueError):
        merge_attempts({"wrong": before["a"]}, [])


def test_ready_batch_does_not_speculatively_release_dependencies():
    tasks = [WorkItem("a"), WorkItem("b", ("a",))]
    assert schedule_ready(tasks, {}, max_inflight=2, calls_left=9) == (Dispatch("a", 1, 1),)
    done = {"a": Attempt("a", 2, "complete", "h")}
    assert schedule_ready(tasks, done, max_inflight=2, calls_left=9) == (Dispatch("b", 1, 1),)


def test_running_slots_and_final_review_budget_are_reserved():
    tasks = [WorkItem("a"), WorkItem("b", call_cost=2), WorkItem("c")]
    running = {"a": Attempt("a", 1, "running")}
    assert schedule_ready(tasks, running, max_inflight=2, calls_left=3) == (Dispatch("c", 1, 1),)
    assert schedule_ready(tasks, running, max_inflight=1, calls_left=99) == ()
    assert schedule_ready(tasks, {}, max_inflight=3, calls_left=2) == ()


def test_priority_retry_and_permanent_dependency_failure():
    tasks = [WorkItem("a"), WorkItem("b", ("a",)), WorkItem("c", priority=2)]
    failed = {"a": Attempt("a", 3, "retryable")}
    assert schedule_ready(tasks, failed, max_inflight=2, calls_left=4) == (
        Dispatch("c", 1, 1),
        Dispatch("a", 4, 1),
    )
    failed["a"] = Attempt("a", 3, "terminal")
    assert schedule_ready(tasks, failed, max_inflight=2, calls_left=9) == (Dispatch("c", 1, 1),)


@pytest.mark.parametrize(
    "tasks",
    [
        [WorkItem("a"), WorkItem("a")],
        [WorkItem("a", ("missing",))],
        [WorkItem("a", ("b",)), WorkItem("b", ("a",))],
        [WorkItem("a", call_cost=0)],
    ],
)
def test_scheduler_rejects_invalid_plan(tasks):
    with pytest.raises(ValueError):
        schedule_ready(tasks, {}, max_inflight=2, calls_left=9)


def test_scheduler_rejects_unknown_observation_and_negative_budget():
    with pytest.raises(ValueError):
        schedule_ready(
            [WorkItem("a")], {"b": Attempt("b", 1, "running")}, max_inflight=1, calls_left=4
        )
    with pytest.raises(ValueError):
        schedule_ready([WorkItem("a")], {}, max_inflight=1, calls_left=-1)
