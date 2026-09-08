"""08의 공개 반례. 모델 호출 없이 근거 카드 검증의 결정적 계약을 검사한다."""
import hashlib
from datetime import date

from student_tasks import Claim, EvidenceCard, TurnContext, validate_claims

AS_OF = date(2026, 9, 8)
QUOTE = "신청 기한은 다음 달 20일입니다."


def card(evidence_id="E-1", quote=QUOTE, **overrides):
    base = dict(uri="viking://day02/doc", content_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                kind="source_text", entity="알파", valid_from=date(2025, 1, 1),
                valid_until=None, status="approved")
    base.update(overrides)
    return EvidenceCard(evidence_id=evidence_id, quote=quote, **base)


def turn(*cards, searched=True, entity="알파", as_of=AS_OF):
    return TurnContext(evidence={c.evidence_id: c for c in cards}, entity=entity, as_of=as_of,
                       searched=searched)


def test_valid_claims_pass():
    good = card()
    claims = [Claim(text="신청 기한은 다음 달 20일이다.", evidence_ids=("E-1",))]
    assert validate_claims(claims, turn(good)) == []


def test_previous_turn_or_unknown_id_is_rejected():
    good = card()
    claims = [Claim(text="이전 턴에서 들은 기한을 재사용한다.", evidence_ids=("E-old",))]
    errors = validate_claims(claims, turn(good))
    assert any("현재 턴에 없는 인용 ID" in e and "E-old" in e for e in errors)


def test_tampered_quote_hash_is_detected():
    tampered = card(content_hash="0" * 64)
    claims = [Claim(text="인용문을 몰래 바꿨다.", evidence_ids=("E-1",))]
    errors = validate_claims(claims, turn(tampered))
    assert any("해시 불일치" in e for e in errors)


def test_out_of_scope_citation_is_rejected():
    other_customer = card(entity="베타")
    expired = card(evidence_id="E-2", valid_until=date(2026, 1, 1))
    claims = [Claim(text="베타 기한", evidence_ids=("E-1",)),
              Claim(text="지난 기한", evidence_ids=("E-2",))]
    errors = validate_claims(claims, turn(other_customer, expired))
    assert len([e for e in errors if "적용 범위를 벗어난 인용" in e]) == 2


def test_claim_without_citations_is_rejected():
    claims = [Claim(text="근거 없는 단정이다.")]
    errors = validate_claims(claims, turn(card()))
    assert any("근거가 없는 주장" in e for e in errors)


def test_numeric_claim_requires_source_text_evidence():
    visual = card(kind="visual_observation", quote="화면에 20%로 보인다.")
    numeric = Claim(text="크레딧 비율 최댓값은 20%다.", evidence_ids=("E-1",), numeric=True)
    assert any("수치 주장에는 원문 텍스트 근거가 필요" in e
               for e in validate_claims([numeric], turn(visual)))
    text = card()
    assert validate_claims([Claim(text="비율은 20%다.", evidence_ids=("E-1",), numeric=True)],
                           turn(text)) == []


def test_visual_observation_supports_qualitative_claims():
    visual = card(kind="visual_observation", quote="표에 각주 표시가 있다.")
    claim = Claim(text="원본 표에는 각주가 있다.", evidence_ids=("E-1",))
    assert validate_claims([claim], turn(visual)) == []


def test_visual_observation_hash_is_still_verified():
    visual = card(kind="visual_observation", quote="화면에 20%로 보인다.", content_hash="f" * 64)
    claims = [Claim(text="관찰 내용이 바뀌었다.", evidence_ids=("E-1",))]
    assert any("해시 불일치" in e for e in validate_claims(claims, turn(visual)))


def test_no_search_this_turn_is_rejected_first():
    claims = [Claim(text="검색 없이 답한다.", evidence_ids=("E-1",))]
    errors = validate_claims(claims, turn(card(), searched=False))
    assert errors and "검색" in errors[0]


def test_errors_are_deduplicated_and_ordered():
    claims = [Claim(text="같은 잘못된 인용을 두 번 쓴다.", evidence_ids=("E-x",)),
              Claim(text="다른 주장도 같은 인용을 쓴다.", evidence_ids=("E-x",))]
    errors = validate_claims(claims, turn(card()))
    assert len([e for e in errors if "E-x" in e]) == 1


def test_inputs_are_not_mutated():
    good = card()
    claims = [Claim(text="정상 주장", evidence_ids=("E-1",))]
    context = turn(good)
    validate_claims(claims, context)
    assert claims[0].evidence_ids == ("E-1",) and set(context.evidence) == {"E-1"}
