"""06. 같은 응답을 사람이 판단하고 Judge의 false pass/false reject를 확인한다. 모델 호출 없음."""

import csv
import json

from business_lab.evidence import calibrate_partitions, export_human_review
from business_lab.storage import save_run_report
from course_config import CALIBRATION, HUMAN_LABELS, SCORED_BASELINE


def main():
    scored = json.loads(SCORED_BASELINE.read_text())
    if not HUMAN_LABELS.exists():
        context = export_human_review(scored, HUMAN_LABELS)
        print(f"검수 자료: {context}\n직접 입력할 CSV: {HUMAN_LABELS}")
        print("human_verdict, reviewer, reason을 입력한 뒤 06을 다시 실행하세요.")
        return
    with HUMAN_LABELS.open(encoding="utf-8-sig", newline="") as f:
        result = calibrate_partitions(scored, list(csv.DictReader(f)))
    save_run_report(result, CALIBRATION)
    print(json.dumps(result["partitions"], ensure_ascii=False, indent=2))
    print(f"검수 상태: {result['status']}. 보정용 결과로 rubric을 바꾸면 별도 확인용에서 다시 검증하세요.")


if __name__ == "__main__":
    main()
