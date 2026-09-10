"""01. 인터뷰·정책을 읽고 Scenario 초안을 작성한다. 모델·외부 호출 없음.

엑셀의 expected_output은 검수할 초안이다. 정책과 fixture에 근거해 수정하고 경계 사례를 추가한다.
"""

from collections import Counter

from business_lab.authoring import draft_cases, write_workbook
from course_config import DRAFT_XLSX


def main():
    cards = draft_cases()
    write_workbook(cards, DRAFT_XLSX)
    print(f"검수 초안: {DRAFT_XLSX}")
    print("원문: business_lab/data/interview.md, order_policy.md, fixtures.json")
    print("조건별 초안 수:", dict(Counter(c["metadata"]["slice"] for c in cards)))
    print("시나리오 시트에서 기준·reviewer·review_reason·review_status를 직접 검수한 뒤 02로 진행하세요.")


if __name__ == "__main__":
    main()
