"""01b. 전문가 피드백을 개발자가 정리해 Golden Dataset 초안을 개선한다.

01 → 이 파일의 REVISIONS 작성·실행 → 수정본 검수 → 02(USE_REFINED=True).
전문가용 엑셀은 읽지 않는다. 01이 만든 Scenario 초안에 아래 수정 사항만 반영한다.
모델·외부 서비스 호출 없음. 기존 초안과 동결 데이터는 덮어쓰지 않는다.
"""

import json
from copy import deepcopy

from business_lab.authoring import read_workbook, write_workbook
from business_lab.contracts import Scenario
from business_lab.dataset import fingerprint
from course_config import DRAFT_XLSX, REFINED_XLSX

SOURCE_XLSX = DRAFT_XLSX
OUTPUT_XLSX = REFINED_XLSX

# 전문가가 확인한 내용을 직접 옮긴다. 아래는 작성 형태만 보여 주는 가상 예시다.
# reference_answer는 마지막 턴의 기대 답변, required_facts는 답변에 필요한 사실이다.
# 실제 도구 호출 순서·환불 횟수 등의 업무 규칙은 기존 expected_output과 Code 평가기로 검증한다.
REVISIONS = [
    # {
    #     "scenario_id": "normal",  # 01_scenarios.xlsx에서 대응하는 ID를 확인한다.
    #     "feedback_id": "Q-001",
    #     "source": "outputs/실제_검수파일.xlsx#전문가 검수!Q-001",
    #     "feedback": "원장에 기록된 환불과 카드사 정산을 구분해 안내해야 합니다.",
    #     "reference_answer": "주문 취소와 환불 처리가 완료됐습니다. 결제 원장에 환불이 기록됐으며 카드사 정산 완료를 뜻하지는 않습니다.",
    #     "required_facts": ["원장에 기록된 환불과 카드사 정산 완료를 구분한다."],
    # },
]


def refine_goldens(
    cards: list[dict], revisions: list[dict]
) -> tuple[list[dict], list[dict]]:
    """답변 기준만 수정하고 변경 전후·의견을 반환한다. 수정 문항은 재검수가 필요하다."""
    if not revisions:
        raise ValueError("REVISIONS에 확인한 기대 답변과 피드백을 직접 작성하세요.")
    updated = deepcopy(cards)
    by_id = {c["metadata"]["scenario_id"]: c for c in updated}
    if len(by_id) != len(updated):
        raise ValueError("초안에 중복 scenario_id가 있습니다.")
    changes, seen = [], set()
    text_fields = {
        "scenario_id",
        "feedback_id",
        "source",
        "feedback",
        "reference_answer",
    }
    for revision in revisions:
        if set(revision) != text_fields | {"required_facts"}:
            raise ValueError(
                "수정 항목은 예시의 여섯 필드를 모두 사용하세요. 알 수 없는 필드는 허용하지 않습니다."
            )
        if any(
            not isinstance(revision[k], str) or not revision[k].strip()
            for k in text_fields
        ):
            raise ValueError(
                "문항 ID·피드백 ID·출처·피드백·기대 답변은 비울 수 없습니다."
            )
        facts = revision["required_facts"]
        if not isinstance(facts, list) or any(
            not isinstance(f, str) or not f.strip() for f in facts
        ):
            raise ValueError(
                "required_facts는 비어 있지 않은 문자열의 목록이어야 합니다. 추가 조건이 없으면 []로 둡니다."
            )
        sid = revision["scenario_id"]
        if sid not in by_id or sid in seen:
            raise ValueError(f"알 수 없거나 중복된 scenario_id: {sid}")
        seen.add(sid)
        card = by_id[sid]
        if card["metadata"]["split"] != "dev":
            raise ValueError(
                "이 단계는 개발용 문항만 수정합니다. holdout은 최종 확인에 남겨 두세요."
            )
        before = deepcopy(card)
        card["expected_output"].update(
            reference_answer=revision["reference_answer"].strip(),
            required_facts=[f.strip() for f in facts],
        )
        # 기존 승인은 수정한 정답에 대한 승인이 아니다. 실제 답변·입력·업무 상태는 바꾸지 않는다.
        card["metadata"].update(review_status="pending", reviewer="", review_reason="")
        card["metadata"]["source"] += (
            f"\n피드백 {revision['feedback_id']}: {revision['source']}"
        )
        Scenario.model_validate(card)
        changes.append(
            {
                "scenario_id": sid,
                "revision": deepcopy(revision),
                "before": before,
                "after": deepcopy(card),
            }
        )
    return updated, changes


def main():
    if not REVISIONS:
        print(
            "반영할 내용이 없습니다. REVISIONS에 전문가가 확인한 기대 답변·피드백을 직접 작성하세요."
        )
        return
    changes_path = OUTPUT_XLSX.with_suffix(".changes.json")
    for path in (OUTPUT_XLSX, changes_path):
        if path.exists():
            raise FileExistsError(
                f"기존 결과를 보존합니다. OUTPUT_XLSX를 새 이름으로 지정하세요: {path}"
            )
    cards = read_workbook(SOURCE_XLSX)
    updated, changes = refine_goldens(cards, REVISIONS)
    report = {
        "kind": "golden_refinement",
        "source": str(SOURCE_XLSX),
        "source_hash": fingerprint(cards),
        "output": str(OUTPUT_XLSX),
        "draft_hash": fingerprint(updated),
        "changes": changes,
    }
    write_workbook(updated, OUTPUT_XLSX)
    with changes_path.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"개선 초안: {OUTPUT_XLSX}\n변경 전후·피드백: {changes_path}")
    print("수정 문항:", ", ".join(c["scenario_id"] for c in changes))
    print(
        "수정본의 expected_output을 재검수하고 reviewer·review_reason·review_status를 직접 작성하세요."
    )
    print(
        "02_freeze_dataset.py의 USE_REFINED=True로 수정본을 동결합니다. 새 버전에서는 03부터 다시 실행하세요."
    )


if __name__ == "__main__":
    main()
