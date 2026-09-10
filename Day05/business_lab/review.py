"""사람 판정을 별도로 수집해 Judge의 false pass/false reject를 확인한다.

Day05에서 `uv run python -m business_lab.review`를 실행한다. 첫 실행은 빈 검수 CSV를
생성하고, 사람이 판정·담당자·근거를 채운 뒤 재실행하면 동일 객체·rubric으로 대조한다.
기존 검수 파일은 덮어쓰지 않는다. 모델 호출이나 외부 업로드는 없다.
"""

import csv
import json
from pathlib import Path

from business_lab.evaluators import RESPONSE_RUBRIC_VERSION as RUBRIC_VERSION
RESULT_FILE = Path("outputs/business_eval.json")
LABELS_CSV = Path("outputs/business_human_labels.csv")


def export_review(report: dict, path: Path):
    if report["config"].get("response_rubric_version") != RUBRIC_VERSION:
        raise ValueError("검수 도구와 실행 기록의 rubric 버전이 다릅니다.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["artifact_id", "scenario_id", "release", "trial", "rubric_version",
                                                   "human_verdict", "reviewer", "reason"])
        writer.writeheader()
        for row in report["rows"]:
            a = row["artifact"]
            writer.writerow({k: a[k] for k in ("artifact_id", "scenario_id", "release", "trial")} |
                            {"rubric_version": RUBRIC_VERSION, "human_verdict": "", "reviewer": "", "reason": ""})


def calibrate(report: dict, labels: list[dict]) -> dict:
    """미검수·판사 오류를 분모에서 구분한다. 사람이 쓴 PASS/FAIL만 정답으로 사용한다."""
    rows = {row["artifact"]["artifact_id"]: row for row in report["rows"]}
    counts = {"true_pass": 0, "false_pass": 0, "false_reject": 0, "true_fail": 0,
              "pending": 0, "judge_unscored": 0}
    seen = set()
    for label in labels:
        aid = label["artifact_id"]
        if (aid in seen or aid not in rows or label["rubric_version"] != RUBRIC_VERSION
                or report["config"].get("response_rubric_version") != RUBRIC_VERSION):
            raise ValueError("중복·다른 실행의 artifact 또는 다른 rubric입니다.")
        seen.add(aid)
        human = label["human_verdict"].strip()
        if not human:
            counts["pending"] += 1
            continue
        if human not in {"PASS", "FAIL"} or not label["reviewer"].strip() or not label["reason"].strip():
            raise ValueError("사람 판정에는 PASS/FAIL, 담당자, 판단 근거가 필요합니다.")
        judged = [v for v in rows[aid]["verdicts"] if v["name"] == "response_semantics"]
        if len(judged) != 1 or judged[0]["status"] not in {"PASS", "FAIL"}:
            counts["judge_unscored"] += 1
            continue
        pair = (human, judged[0]["status"])
        bucket = {("PASS", "PASS"): "true_pass", ("FAIL", "PASS"): "false_pass",
                  ("PASS", "FAIL"): "false_reject", ("FAIL", "FAIL"): "true_fail"}[pair]
        counts[bucket] += 1
    counts["pending"] += len(set(rows) - seen)
    positives = counts["true_pass"] + counts["false_reject"]
    negatives = counts["true_fail"] + counts["false_pass"]
    return counts | {"matched": positives + negatives,
                     "false_pass_rate": counts["false_pass"] / negatives if negatives else None,
                     "false_reject_rate": counts["false_reject"] / positives if positives else None}


def main():
    report = json.loads(RESULT_FILE.read_text())
    if not LABELS_CSV.exists():
        export_review(report, LABELS_CSV)
        print(f"{LABELS_CSV}를 만들었습니다. 결과 JSON의 요청·실제 상태·events·응답을 읽고 판정하세요.")
        return
    with LABELS_CSV.open(encoding="utf-8-sig", newline="") as file:
        result = calibrate(report, list(csv.DictReader(file)))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
