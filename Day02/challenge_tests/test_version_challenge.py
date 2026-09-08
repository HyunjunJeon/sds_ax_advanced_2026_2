"""A의 공개 반례. 실제 검색 품질 평가가 아니라 as_of 유효 집합의 결정적 계약 검사다."""
from datetime import date

from student_tasks import DocumentFacts, effective_documents


def facts(**overrides):
    base = dict(uri="doc", title="문서", entity="알파", version="1.0",
                valid_from=date(2025, 1, 1), valid_until=None, status="approved", family="")
    base.update(overrides)
    return DocumentFacts(**base)


def catalogue(*items):
    return {item.uri: item for item in items}


def uris(result):
    return sorted(f.uri for f in result.documents)


def reasons(result):
    return {e.uri: e.reason for e in result.excluded}


def test_boundary_day_flips_v1_to_v2():
    v1 = facts(uri="sla/v1", title="SLA", family="sla",
               valid_from=date(2025, 1, 1), valid_until=date(2026, 7, 1))
    v2 = facts(uri="sla/v2", title="SLA", family="sla", valid_from=date(2026, 7, 1))
    catalog = catalogue(v1, v2)
    june = effective_documents(catalog, entity="알파", as_of=date(2026, 6, 30))
    july = effective_documents(catalog, entity="알파", as_of=date(2026, 7, 1))
    assert uris(june) == ["sla/v1"] and reasons(june)["sla/v2"] == "not_yet"
    assert uris(july) == ["sla/v2"] and reasons(july)["sla/v1"] == "expired"


def test_draft_is_never_effective_regardless_of_window():
    # 유효일이 미래이기도 하지만, 판정 순서상 status가 먼저다.
    draft = facts(uri="sla/draft", title="SLA", family="sla", status="draft",
                  valid_from=date(2026, 8, 1))
    result = effective_documents(catalogue(draft), entity="알파", as_of=date(2026, 9, 8))
    assert uris(result) == [] and reasons(result)["sla/draft"] == "status"


def test_amendment_and_base_coexist():
    base = facts(uri="sla/v2", title="SLA", family="sla")
    amend = facts(uri="sla/addendum", title="SLA 추가 약정", family="sla",
                  valid_from=date(2026, 8, 1))
    result = effective_documents(catalogue(base, amend), entity="알파", as_of=date(2026, 9, 8))
    assert uris(result) == ["sla/addendum", "sla/v2"]
    assert result.excluded == ()


def test_overlapping_windows_resolve_to_latest_valid_from():
    old = facts(uri="sla/old", title="SLA", family="sla")
    new = facts(uri="sla/new", title="SLA", family="sla", valid_from=date(2025, 6, 1))
    result = effective_documents(catalogue(old, new), entity="알파", as_of=date(2026, 9, 8))
    assert uris(result) == ["sla/new"] and reasons(result)["sla/old"] == "superseded"


def test_other_entity_is_excluded_before_window_reasons():
    # 다른 고객 문서는 상태·시점과 무관하게 고객 이유로 먼저 제외된다.
    beta = facts(uri="beta", title="베타 SLA", entity="베타", status="draft",
                 valid_from=date(2030, 1, 1))
    result = effective_documents(catalogue(beta), entity="알파", as_of=date(2026, 9, 8))
    assert reasons(result)["beta"] == "other_entity"


def test_common_entity_is_in_scope_for_customer_query():
    shared = facts(uri="runbook", title="장애 대응", entity="공통")
    result = effective_documents(catalogue(shared), entity="알파", as_of=date(2026, 9, 8))
    assert uris(result) == ["runbook"]


def test_documents_are_sorted_by_title_then_uri():
    later_title = facts(uri="z", title="가")
    earlier_title = facts(uri="a", title="나")
    result = effective_documents(catalogue(later_title, earlier_title), entity="알파",
                                 as_of=date(2026, 9, 8))
    assert [f.uri for f in result.documents] == ["z", "a"]


def test_sla_alpha_shape_matches_the_business_corpus():
    v1 = facts(uri="sla/v1", title="알파 SLA", family="sla",
               valid_until=date(2026, 7, 1))
    v2 = facts(uri="sla/v2", title="알파 SLA", family="sla", valid_from=date(2026, 7, 1))
    draft = facts(uri="sla/draft", title="알파 SLA 개정 검토안", family="sla",
                  status="draft", valid_from=date(2026, 8, 1))
    amend = facts(uri="sla/amend", title="알파 SLA 개별 추가 약정", family="sla",
                  valid_from=date(2026, 8, 1))
    beta = facts(uri="beta", title="베타 SLA", entity="베타")
    runbook = facts(uri="runbook", title="장애 대응", entity="공통")
    result = effective_documents(catalogue(v1, v2, draft, amend, beta, runbook),
                                 entity="알파", as_of=date(2026, 9, 8))
    assert uris(result) == ["runbook", "sla/amend", "sla/v2"]
    assert reasons(result) == {"beta": "other_entity", "sla/draft": "status", "sla/v1": "expired"}


def test_empty_catalog_and_inputs_are_not_mutated():
    assert effective_documents({}, entity="알파", as_of=date(2026, 9, 8)).documents == ()
    a = facts(uri="a", title="A")
    catalog = catalogue(a)
    effective_documents(catalog, entity="알파", as_of=date(2026, 9, 8))
    assert list(catalog) == ["a"]
