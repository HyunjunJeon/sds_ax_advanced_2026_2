"""새 00~09 공통 실행 조건. 항상 Day05 안에서 실행한다.

MODE=live는 유료 Agent/Judge 호출, USE_LANGFUSE=True는 외부 프로젝트 기록을 뜻한다.
scripted는 수업 코드의 계약 검증용이다. LLM 성능 측정으로 보고하지 않는다.
"""

from pathlib import Path

WORK = Path("outputs/course")
MODE = "live"  # live / scripted
USE_LANGFUSE = True
REPEATS = 2
# 실행 전에 고정할 작은 개발용 부분집합. None이면 검수한 개발용 전체 문항.
SCENARIO_IDS = ["normal", "timeout-after", "clarification-episode"]

DRAFT_XLSX = WORK / "01_scenarios.xlsx"
REFINED_XLSX = WORK / "01_refined_scenarios.xlsx"
DATASET = WORK / "02_dataset.json"
BASELINE = WORK / "03_baseline.json"
CHANGE_PLAN = WORK / "04_change_plan.json"
SCORED_BASELINE = WORK / "05_baseline_scores.json"
HUMAN_LABELS = WORK / "06_human_labels.csv"
CALIBRATION = WORK / "06_calibration.json"
CANDIDATE = WORK / "07_candidate.json"
COMPARISON = WORK / "07_comparison.json"
REGRESSION_DRAFT = WORK / "08_regression.xlsx"
HOLDOUT = WORK / "08_holdout.json"
FINAL_REPORT = WORK / "09_report.md"
