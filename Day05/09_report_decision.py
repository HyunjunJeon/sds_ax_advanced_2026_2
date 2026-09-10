"""09. 기준·근거·가설·변경·비교·남은 실패를 연결해 승인 또는 보류를 설명한다. 모델 호출 없음."""

import json

from business_lab.evidence import write_final_report
from course_config import CALIBRATION, COMPARISON, FINAL_REPORT, HOLDOUT


def main():
    comparison = json.loads(COMPARISON.read_text())
    calibration = json.loads(CALIBRATION.read_text()) if CALIBRATION.exists() else None
    holdout = json.loads(HOLDOUT.read_text()) if HOLDOUT.exists() else None
    if holdout and holdout.get("development_comparison_id") != comparison["run_id"]:
        raise ValueError("holdout이 다른 개발 실험을 가리킵니다. 같은 후보의 최종 확인이 필요합니다.")
    write_final_report(comparison, comparison["change_plan"], FINAL_REPORT, calibration=calibration, holdout=holdout)
    print(f"검토 보고서: {FINAL_REPORT}")
    print("관찰 사실·원인 가설·평가기의 한계·회귀·미확인을 자신의 말로 설명하세요.")


if __name__ == "__main__":
    main()
