"""A의 공개 반례. 실제 LLM 품질 평가가 아니라 선택기의 결정적 계약 검사다."""

import pytest

from student_tasks import ContextItem, render_selection, select_context


def item(key, text, *facets, requires=()):
    return ContextItem(key, text, frozenset(facets), frozenset(requires))


def choose(items, required, budget):
    selected = select_context(items, frozenset(required), budget)
    assert isinstance(selected, tuple) and selected == tuple(sorted(set(selected)))
    assert set(selected) <= {i.id for i in items}
    assert len(render_selection(items, selected).encode("utf-8")) <= budget
    by_id = {i.id: i for i in items}
    assert all(by_id[key].requires <= set(selected) for key in selected)
    return selected


def test_overlapping_coverage_defeats_greedy_ranking():
    # a 한 개는 세 항목을 덮지만, b+c만이 예산 12에서 네 항목을 전부 덮는다.
    items = [item("a", "xxx", "1", "2", "3"), item("b", "x", "1", "4"), item("c", "x", "2", "3")]
    assert choose(items, ["1", "2", "3", "4"], 12) == ("b", "c")


def test_amendment_requires_its_base_even_if_base_has_no_requested_facet():
    items = [item("a", "base"), item("b", "amend", "deadline", requires=["a"])]
    cost = len(render_selection(items, ("a", "b")).encode())
    assert choose(items, ["deadline"], cost) == ("a", "b")
    assert choose(items, ["deadline"], cost - 1) == ()


def test_transitive_dependency_closure():
    items = [
        item("a", "x"),
        item("b", "y", requires=["a"]),
        item("c", "z", "decision", requires=["b"]),
    ]
    assert choose(items, ["decision"], 18) == ("a", "b", "c")
    assert choose(items, ["decision"], 17) == ()


def test_korean_utf8_and_rendering_overhead_are_counted():
    items = [item("a", "가", "target")]
    assert len(render_selection(items, ("a",)).encode()) == 8
    assert choose(items, ["target"], 7) == ()
    assert choose(items, ["target"], 8) == ("a",)


def test_partial_coverage_and_ties_are_deterministic():
    items = [item("b", "xx", "1"), item("a", "x", "1"), item("c", "x", "2")]
    assert choose(items, ["1", "2", "missing"], 6) == ("a",)
    assert choose(list(reversed(items)), ["1", "2", "missing"], 6) == ("a",)
    assert choose(items, ["1", "2", "missing"], 12) == ("a", "c")


@pytest.mark.parametrize(
    "items",
    [
        [item("a", "x"), item("a", "y")],
        [item("a", "x", requires=["absent"])],
        [item("a", "x", requires=["b"]), item("b", "y", requires=["a"])],
        [item("a", "x", requires=["a"])],
    ],
)
def test_invalid_graph_is_rejected_before_optimization(items):
    with pytest.raises(ValueError):
        select_context(items, frozenset(), 0)


def test_empty_request_and_negative_budget():
    assert choose([], [], 0) == ()
    assert choose([item("a", "x", "unrequested")], [], 99) == ()
    with pytest.raises(ValueError):
        select_context([], frozenset(), -1)


def test_redundant_candidates_do_not_displace_a_required_pair():
    items = [item(f"r{i:02}", "redundant", "availability") for i in range(18)]
    items += [item("base", "x", "availability"), item("amend", "x", "deadline", requires=["base"])]
    budget = len(render_selection(items, ("amend", "base")).encode())
    assert choose(items, ["availability", "deadline"], budget) == ("amend", "base")
