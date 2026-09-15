"""
실행: uv run pytest tests/test_defenses_03.py -q
포인트: 분절 병합·저점수 제거 / 공개 대상별 마스킹과 오프셋 / 전달 직전 BLOCK(TRANSFORM 금지)·UNKNOWN→REVIEW / 통합: 답변만 검사하면 전송함·초안이 새고 전체 경계는 유출 0.

주요 내용:
03 방어 구현의 단위 검증. 각 테스트 이름이 "어떤 문제를 어떻게 막는가"의 한 문장이다.
"""

import guardlab.defenses as st
from guardlab.cases import get_case
from guardlab.evaluate import evaluate
from guardlab.guards import PIIGuard, Span
from guardlab.labs import load_lab
from guardlab.replay import ScriptedChatModel, script_send_with_pii
from guardlab.runner import prepare, run_case


def test_merge_spans_filters_and_joins_adjacent():
    spans = [Span(9, 21, "contact.phone", 0.99, "010-4412-778"), Span(21, 22, "contact.phone", 0.99, "8"),
             Span(4, 7, "identity.person_name", 0.98, "박서"), Span(7, 8, "identity.person_name", 0.99, "준"),
             Span(30, 33, "identity.tax_id", 0.2, "저점수")]
    merged = st.merge_spans(spans, 0.5)
    assert [(s.start, s.end, s.label) for s in merged] == [(4, 8, "identity.person_name"), (9, 22, "contact.phone")]
    assert len(spans) == 5 and spans[0].start == 9  # 입력 불변
    overlap = st.merge_spans([Span(0, 5, "a", 0.6, ""), Span(3, 8, "b", 0.9, "")], 0.5)
    assert [(s.start, s.end, s.label, s.score) for s in overlap] == [(0, 8, "b", 0.9)]


def test_mask_keeps_allowed_labels_and_offsets():
    text = "박서준 010-4412-7788 seojun.park@nurisoft.example"
    spans = PIIGuard("regex").scan(text) + [Span(0, 3, "identity.person_name", 0.99, "박서준")]
    policy = st.DisclosurePolicy()
    masked, removed = st.mask_for_audience(text, spans, "internal_reviewer", policy)
    assert masked == "박서준 [contact.phone] [contact.email]"
    assert removed == ["contact.email", "contact.phone"]
    strict, removed2 = st.mask_for_audience(text, spans, "unknown_audience", policy)
    assert strict.startswith("[identity.person_name]") and "identity.person_name" in removed2


def test_check_before_send_blocks_and_never_transforms(kim):
    pii = PIIGuard("regex")
    policy = st.DisclosurePolicy()
    d = st.check_before_send("seojun.park@nurisoft.example", "주간 보고", "정수아 010-3355-6677", kim, pii, policy)
    assert d.action == "BLOCK" and d.reason_code == "PII_IN_BODY" and "contact.phone" in d.risk_labels
    assert st.check_before_send("seojun.park@nurisoft.example", "주간 보고", "실패 2건: TC-104, TC-117", kim, pii, policy).action == "ALLOW"
    assert st.check_before_send("x", "s", "b", None, pii, policy).reason_code == "NO_CONTEXT"

    class Broken(PIIGuard):
        def scan(self, text):
            raise RuntimeError("모델 없음")

    assert st.check_before_send("a@nurisoft.example", "s", "b", kim, Broken("regex"), policy).action == "REVIEW"


def test_integration_all_boundaries_leaves_no_pii_in_outbox_or_drafts():
    lab = load_lab("03_output_guard.py")
    lab.DETECTOR = "regex"
    case = get_case("doc_include_contacts")
    rows = {}
    for config in ("final_only", "all_boundaries"):
        prepared = prepare(case, "test03", config, 1)
        agent = lab.build_agent(prepared, ScriptedChatModel(steps=script_send_with_pii()), config)
        out = run_case(agent, prepared)
        assert out["error"] is None
        rows[config] = evaluate(case, prepared.ctx, prepared.ws, prepared.outbox, prepared.log, final_answer=out["final_answer"])
    assert rows["final_only"]["leaked"]  # 답변만 검사하면 전송함·초안에 남는다
    assert rows["all_boundaries"]["leaked"] == [] and rows["all_boundaries"]["outbox"] == []
    assert rows["all_boundaries"]["blocked_at"]["stage"] == "output"
