"""B의 공개 반례. 실제 LLM 품질 평가가 아니라 표 근거 선택기의 결정적 계약 검사다."""
import pytest

from student_tasks import TableBlock, render_table_context, select_table_evidence


def block(key, text, *covers, requires=()):
    return TableBlock(key, text, frozenset(covers), frozenset(requires))


def choose(blocks, required, budget):
    selected = select_table_evidence(blocks, frozenset(required), budget)
    assert isinstance(selected, tuple) and selected == tuple(sorted(set(selected)))
    assert set(selected) <= {b.id for b in blocks}
    assert len(render_table_context(blocks, selected).encode("utf-8")) <= budget
    by_id = {b.id: b for b in blocks}
    assert all(by_id[key].requires <= set(selected) for key in selected)
    return selected


def test_overlapping_coverage_defeats_greedy_ranking():
    # a 한 블록은 세 조건을 덮지만, b+c만이 예산 12에서 네 조건을 전부 덮는다.
    blocks = [block("a", "xxx", "1", "2", "3"), block("b", "x", "1", "4"), block("c", "x", "2", "3")]
    assert choose(blocks, ["1", "2", "3", "4"], 12) == ("b", "c")


def test_unit_header_must_travel_with_data_rows():
    header = block("unit", "| 구간 | 비율 |\n|---|---|\n|%|")
    row = block("row", "| 99.9% 미만 | 10 |", "ratio", requires=["unit"])
    cost = len(render_table_context([header, row], ("row", "unit")).encode("utf-8"))
    assert choose([header, row], ["ratio"], cost) == ("row", "unit")
    assert choose([header, row], ["ratio"], cost - 1) == ()


def test_transitive_dependency_closure():
    blocks = [
        block("u", "x"),
        block("n", "y", requires=["u"]),
        block("b", "z", "decision", requires=["n"]),
    ]
    assert choose(blocks, ["decision"], 18) == ("b", "n", "u")
    assert choose(blocks, ["decision"], 17) == ()


def test_korean_utf8_and_rendering_overhead_are_counted():
    blocks = [block("a", "가", "target")]
    assert len(render_table_context(blocks, ("a",)).encode()) == 8
    assert choose(blocks, ["target"], 7) == ()
    assert choose(blocks, ["target"], 8) == ("a",)


def test_partial_coverage_and_ties_are_deterministic():
    blocks = [block("b", "xx", "1"), block("a", "x", "1"), block("c", "x", "2")]
    assert choose(blocks, ["1", "2", "missing"], 6) == ("a",)
    assert choose(list(reversed(blocks)), ["1", "2", "missing"], 6) == ("a",)
    assert choose(blocks, ["1", "2", "missing"], 12) == ("a", "c")


def test_missing_budget_cannot_buy_a_partial_unit_dependency():
    # 단위 헤더가 예산보다 크면 데이터 행도 남기지 않는다(조건 없는 수치 방지).
    blocks = [block("unit", "x" * 30), block("row", "| 99.9% | 10 |", "ratio", requires=["unit"])]
    assert choose(blocks, ["ratio"], 45) == ()
    assert choose(blocks, ["ratio"], 100) == ("row", "unit")


@pytest.mark.parametrize(
    "blocks",
    [
        [block("a", "x"), block("a", "y")],
        [block("a", "x", requires=["absent"])],
        [block("a", "x", requires=["b"]), block("b", "y", requires=["a"])],
        [block("a", "x", requires=["a"])],
    ],
)
def test_invalid_graph_is_rejected_before_optimization(blocks):
    with pytest.raises(ValueError):
        select_table_evidence(blocks, frozenset(), 0)


def test_empty_request_and_negative_budget():
    assert choose([], [], 0) == ()
    assert choose([block("a", "x", "unrequested")], [], 99) == ()
    with pytest.raises(ValueError):
        select_table_evidence([], frozenset(), -1)


def test_more_than_24_blocks_are_rejected():
    blocks = [block(f"b{i:02d}", "x") for i in range(25)]
    with pytest.raises(ValueError):
        select_table_evidence(blocks, frozenset(), 100)


def test_redundant_fragments_do_not_displace_a_required_pair():
    fragments = [block(f"r{i:02}", "dup", "cell") for i in range(18)]
    unit = block("unit", "x")
    table = block("table", "y", "decision", requires=["unit"])
    budget = len(render_table_context(fragments + [unit, table], ("table", "unit")).encode())
    assert choose(fragments + [unit, table], ["decision"], budget) == ("table", "unit")
