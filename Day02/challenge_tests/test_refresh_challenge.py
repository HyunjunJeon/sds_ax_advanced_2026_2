"""C의 공개 반례. 실제 서버 재적재가 아니라 턴 장부의 지속·격리·갱신 감지 계약 검사다."""
import pytest

from student_tasks import SessionLedger

SCOPE = {"entity": "알파", "as_of": "2026-09-08", "namespace": "business"}
OTHER_SCOPE = {"entity": "베타", "as_of": "2026-09-08", "namespace": "business"}


def test_reopening_preserves_turns_and_numbering(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    first = SessionLedger(path)
    assert first.record_turn("s", SCOPE, question="Q1", evidence={"E1": ("u1", "h1")}) == 1
    assert first.record_turn("s", SCOPE, question="Q2", evidence={}) == 2
    second = SessionLedger(path)
    assert second.record_turn("s", SCOPE, question="Q3", evidence={}) == 3
    assert [h["turn"] for h in second.history("s", SCOPE)] == [1, 2, 3]


def test_scope_isolation_ignores_other_customer_turns(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    ledger.record_turn("s", SCOPE, question="알파 질문", evidence={"E1": ("u1", "h1")})
    ledger.record_turn("s", OTHER_SCOPE, question="베타 질문", evidence={"E9": ("u9", "h9")})
    assert ledger.stale_evidence("s", SCOPE, current={}) == (("E1", 1),)
    assert [h["question"] for h in ledger.history("s", SCOPE)] == ["알파 질문"]


def test_updated_document_marks_previous_citations_stale(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    ledger.record_turn("s", SCOPE, question="Q1", evidence={"E1": ("u1", "h1")})
    assert ledger.stale_evidence("s", SCOPE, current={"u1": "h2"}) == (("E1", 1),)
    assert ledger.stale_evidence("s", SCOPE, current={"u1": "h1"}) == ()


def test_missing_uri_is_conservatively_stale(tmp_path):
    # 원문을 더 이상 확인할 수 없으면 그 인용은 재검색 대상이다.
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    ledger.record_turn("s", SCOPE, question="Q1", evidence={"E1": ("u1", "h1")})
    assert ledger.stale_evidence("s", SCOPE, current={"u2": "h2"}) == (("E1", 1),)


def test_stale_ordering_is_deterministic(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    ledger.record_turn("s", SCOPE, question="Q1", evidence={"z": ("u2", "h1"), "a": ("u1", "h1")})
    ledger.record_turn("s", SCOPE, question="Q2", evidence={"b": ("u1", "h1")})
    assert ledger.stale_evidence("s", SCOPE, current={}) == (("a", 1), ("z", 1), ("b", 2))


def test_history_returns_last_four_turns_with_evidence(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    for turn in range(1, 7):
        ledger.record_turn("s", SCOPE, question=f"Q{turn}", evidence={f"E{turn}": ("u1", "h1")})
    history = ledger.history("s", SCOPE)
    assert [h["turn"] for h in history] == [3, 4, 5, 6]
    assert history[0] == {"turn": 3, "question": "Q3", "evidence": ["E3"]}


def test_invalid_session_ids_and_limits_are_rejected(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError):
        ledger.record_turn("한글세션", SCOPE, question="Q", evidence={})
    with pytest.raises(ValueError):
        ledger.record_turn("", SCOPE, question="Q", evidence={})
    with pytest.raises(ValueError):
        ledger.history("s", SCOPE, limit=0)


def test_record_turn_validates_payload(tmp_path):
    ledger = SessionLedger(tmp_path / "ledger.sqlite3")
    with pytest.raises(ValueError):
        ledger.record_turn("s", SCOPE, question="", evidence={})
    with pytest.raises(ValueError):
        ledger.record_turn("s", SCOPE, question="Q" * 4001, evidence={})
    with pytest.raises(ValueError):
        ledger.record_turn("s", SCOPE, question="Q", evidence={"E1": ("u1", "")})
