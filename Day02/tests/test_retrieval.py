from datetime import date

import pytest

from day02.errors import BudgetExceeded, ValidationFailure
from day02.evidence import Budget, evidence_bytes
from day02.tools.search import make_search_tools
from day02.evidence import digest


def test_scope_filters_before_read_and_addendum_expansion(context):
    context.client.hits = ["viking://resources/test/v2.md"]
    output = context.search("신청 기한")
    assert {e["document_id"] for e in output["evidence"]} == {"v2", "addendum"}
    assert {r[0] for r in context.client.reads} == {
        "viking://resources/test/v2.md", "viking://resources/test/addendum.md",
    }


def test_effective_date_exclusive_end(context):
    context.as_of = date(2026, 6, 30)
    assert {e["document_id"] for e in context.search("신청")["evidence"]} == {"old"}


def test_no_evidence_and_empty_query_are_distinct(context):
    context.client.hits = []
    assert context.search("없는 내용")["status"] == "no_evidence"
    with pytest.raises(ValueError):
        context.search(" ")


def test_read_requires_evidence_from_current_turn(context):
    with pytest.raises(ValueError):
        context.read_more("E-from-another-turn", 1)


def test_remote_mutation_fails_closed(context):
    context.client.texts["viking://resources/test/v2.md"] = "변조된 원문\n"
    with pytest.raises(ValidationFailure):
        context.search("신청")


def test_context_budget_includes_metadata_and_quotes(context):
    context.budget.max_context_bytes = 2
    assert context.search("신청")["evidence"] == []
    assert evidence_bytes(list(context.evidence.values())) <= 2


def test_search_budget_blocks_next_call(context):
    context.budget.max_searches = 1
    context.search("신청")
    with pytest.raises(BudgetExceeded):
        context.search("다시 신청")


def test_expired_budget_blocks_before_api(context):
    context.budget = Budget(max_seconds=-1)
    with pytest.raises(BudgetExceeded):
        context.search("신청")
    assert not context.client.reads


def test_actual_tool_invocation(context):
    search, read = make_search_tools(context)
    output = search.invoke({"query": "신청"})
    reference = output["evidence"][0]["evidence_id"]
    assert read.invoke({"evidence_id": reference, "start_line": 1})["status"] == "found"


def test_partial_remote_read_checked_against_snapshot(context, tmp_path):
    uri = "viking://resources/test/v2.md"
    original = "첫 행\n" * 150
    snapshot = tmp_path / "source.md"
    snapshot.write_text(original, encoding="utf-8")
    context.manifest["documents"][uri].update(snapshot=str(snapshot), remote_hash=digest(original), line_count=150)
    context.client.texts[uri] = "변조 행\n" + "첫 행\n" * 149
    with pytest.raises(ValidationFailure, match="원문 구간"):
        context._read(uri, limit=10)
