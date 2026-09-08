"""07의 공개 반례. 실제 검색 품질 평가가 아니라 합류 정책의 결정적 계약 검사다."""
from datetime import date

import pytest

from student_tasks import DocumentFacts, fuse_and_rank

AS_OF = date(2026, 9, 8)


def facts(**overrides):
    base = dict(uri="doc", title="문서", entity="알파", version="1.0",
                valid_from=date(2025, 1, 1), valid_until=None, status="approved", family="")
    base.update(overrides)
    return DocumentFacts(**base)


def catalogue(*items):
    return {item.uri: item for item in items}


def test_expired_version_cannot_be_fused_back():
    v1 = facts(uri="sla/v1", title="SLA", version="1.0", valid_until=date(2026, 7, 1), family="sla")
    v2 = facts(uri="sla/v2", title="SLA", version="2.0", valid_from=date(2026, 7, 1), family="sla")
    out = fuse_and_rank([["sla/v1", "sla/v2"]], catalogue(v1, v2), entity="알파", as_of=AS_OF, limit=3)
    assert out == ["sla/v2"]


def test_overlapping_validity_is_resolved_by_version():
    # catalog 오류로 두 버전의 유효 구간이 겹쳐도 최신 version만 남는다.
    v1 = facts(uri="sla/v1", title="SLA", version="1.0", family="sla")
    v2 = facts(uri="sla/v2", title="SLA", version="2.0", valid_from=date(2025, 6, 1), family="sla")
    out = fuse_and_rank([["sla/v1", "sla/v2"]], catalogue(v1, v2), entity="알파", as_of=AS_OF, limit=3)
    assert out == ["sla/v2"]


def test_amendment_and_base_coexist_in_one_family():
    base = facts(uri="sla/v2", title="SLA", version="2.0", family="sla")
    amend = facts(uri="sla/addendum", title="SLA 추가 약정", version="1.0", family="sla")
    out = fuse_and_rank([["sla/v2", "sla/addendum"]], catalogue(base, amend), entity="알파",
                        as_of=AS_OF, limit=3)
    assert out == ["sla/v2", "sla/addendum"]


def test_duplicate_flood_cannot_starve_a_unique_family():
    runbook = [facts(uri=f"run/r{i:02d}", title=f"운영일지 {i}", family="runbook") for i in range(5)]
    amend = facts(uri="sla/addendum", title="SLA 추가 약정", family="sla")
    ranking = [item.uri for item in runbook] + [amend.uri]
    out = fuse_and_rank([ranking], catalogue(*runbook, amend), entity="알파", as_of=AS_OF,
                        limit=3, per_family=2)
    assert out == ["run/r00", "sla/addendum", "run/r01"]


def test_rrf_promotes_documents_found_by_both_retrievers():
    a, b, c = facts(uri="a", title="A"), facts(uri="b", title="B"), facts(uri="c", title="C")
    out = fuse_and_rank([["b", "c"], ["c", "a"]], catalogue(a, b, c), entity="알파", as_of=AS_OF, limit=3)
    assert out[0] == "c"


def test_unregistered_uri_from_a_reranker_is_rejected():
    a = facts(uri="a", title="A")
    with pytest.raises(ValueError):
        fuse_and_rank([["a", "ghost"]], catalogue(a), entity="알파", as_of=AS_OF, limit=3)


def test_other_entity_and_draft_are_out_of_scope():
    mine = facts(uri="mine", title="A")
    other = facts(uri="other", title="B", entity="베타")
    draft = facts(uri="draft", title="C", status="draft")
    out = fuse_and_rank([["other", "draft", "mine"]], catalogue(mine, other, draft),
                        entity="알파", as_of=AS_OF, limit=3)
    assert out == ["mine"]


def test_common_entity_documents_are_in_scope():
    shared = facts(uri="shared", title="공통", entity="공통")
    assert fuse_and_rank([["shared"]], catalogue(shared), entity="알파", as_of=AS_OF, limit=2) == ["shared"]


def test_ties_break_by_uri_and_result_is_deterministic():
    a, b = facts(uri="a", title="A"), facts(uri="b", title="B")
    first = fuse_and_rank([["b"], ["a"]], catalogue(a, b), entity="알파", as_of=AS_OF, limit=2)
    second = fuse_and_rank([["b"], ["a"]], catalogue(a, b), entity="알파", as_of=AS_OF, limit=2)
    assert first == second == ["a", "b"]


def test_validity_boundaries_are_half_open():
    doc = facts(uri="doc", title="A", valid_from=date(2026, 7, 1), valid_until=date(2026, 9, 8))
    assert fuse_and_rank([["doc"]], catalogue(doc), entity="알파", as_of=date(2026, 7, 1), limit=2) == ["doc"]
    assert fuse_and_rank([["doc"]], catalogue(doc), entity="알파", as_of=date(2026, 9, 8), limit=2) == []


def test_empty_individual_ranking_is_allowed():
    a = facts(uri="a", title="A")
    assert fuse_and_rank([[], ["a"]], catalogue(a), entity="알파", as_of=AS_OF, limit=2) == ["a"]


def test_inputs_are_not_mutated():
    a = facts(uri="a", title="A")
    rankings = [["a"]]
    catalog = catalogue(a)
    fuse_and_rank(rankings, catalog, entity="알파", as_of=AS_OF, limit=2)
    assert rankings == [["a"]] and list(catalog) == ["a"]


def test_invalid_arguments_raise():
    a = facts(uri="a", title="A")
    catalog = catalogue(a)
    base = dict(facts=catalog, entity="알파", as_of=AS_OF)
    with pytest.raises(ValueError):
        fuse_and_rank([], **base, limit=2)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=0)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=11)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=2, weights=[1.0, 2.0])
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=2, weights=[-1.0])
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=2, constant=0)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], **base, limit=2, per_family=0)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], catalogue(facts(uri="a", title="A", version="one")),
                       entity="알파", as_of=AS_OF, limit=2)
    with pytest.raises(ValueError):
        fuse_and_rank([["a"]], catalogue(facts(uri="a", title="A", valid_from=date(2026, 1, 1),
                                               valid_until=date(2026, 1, 1))),
                       entity="알파", as_of=AS_OF, limit=2)
