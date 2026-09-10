"""04·06·08·09: 원자료 읽기, 사람의 가설·검수 연결, 회귀 초안, 최종 설명."""

from copy import deepcopy
import json
from pathlib import Path

from business_lab.authoring import write_workbook
from business_lab.contracts import Artifact
from business_lab.review import calibrate, export_review
from business_lab.storage import make_run_id, save_run_report


def write_json_new(path: Path, data) -> None:
    """사람이 편집할 파일은 기존 내용이 있으면 보존한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def inspect_recording(raw: dict, directory: Path, plan_path: Path) -> Path:
    """관찰 자료를 출력하고 빈 가설 양식을 만든다. 원인·수정안을 대신 작성하지 않는다."""
    directory.mkdir(parents=True, exist_ok=True)
    text = ["# 실행 근거", "", f"원자료 실행 ID: {raw['run_id']}", "",
            "관찰 사실을 먼저 기록하고, 원인 가설은 04_change_plan.json의 별도 칸에 작성합니다.", ""]
    for row in raw["rows"]:
        a = row["artifact"]
        text += [f"## {a['scenario_id']} / {a['release']} / 회차 {a['trial']}", "",
                 f"Artifact: `{a['artifact_id']}` / Trace: `{row['trace_id']}` / 실행: `{a['execution_status']}`", "",
                 f"요청: {a.get('initial_request', a['request'])}", "",
                 "| 단계 | 턴 | Tool | 호출 ID | 인자 | 결과 | 근거 ID |", "|---|---|---|---|---|---|---|"]
        for e in a["events"]:
            args = json.dumps(e["args"], ensure_ascii=False).replace("|", "\\|")
            result = json.dumps(e["result"], ensure_ascii=False).replace("|", "\\|")
            text.append(f"| {e['step']} | {e['turn_index']} | {e['tool']} | {e['call_id']} | {args} | {result} | {e['event_id']} |")
        text += ["", "최종 응답·실제 상태:", "", "```json",
                 json.dumps({"response": a["response"], "final_state": a["final_state"], "error_type": a["error_type"]},
                            ensure_ascii=False, indent=2), "```", ""]
    target = directory / f"evidence-{raw['run_id']}.md"
    if not target.exists():
        target.write_text("\n".join(text))
    if not plan_path.exists():
        write_json_new(plan_path, {"recording_id": raw["run_id"], "artifact_id": "", "evidence_ids": [],
            "fact": "", "hypothesis": "", "alternative": "", "change": "", "success_criteria": "", "owner": ""})
    elif json.loads(plan_path.read_text())["recording_id"] != raw["run_id"]:
        raise ValueError("기존 가설 양식이 다른 baseline을 가리킵니다. 새 WORK 경로로 분리하세요.")
    return target


def validate_plan(path: Path, raw: dict) -> dict:
    """사람이 채운 가설과 실행 근거의 연결을 확인한다. 가설의 참/거짓을 자동 확정하지 않는다."""
    plan = json.loads(path.read_text())
    if plan.get("recording_id") != raw["run_id"]:
        raise ValueError("가설의 baseline 실행 ID가 다릅니다.")
    for key in ("artifact_id", "fact", "hypothesis", "alternative", "change", "success_criteria", "owner"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise ValueError(f"04 가설 양식의 {key}를 직접 작성하세요.")
    row = next((r for r in raw["rows"] if r["artifact"]["artifact_id"] == plan["artifact_id"]), None)
    if not row:
        raise ValueError("가설의 Artifact가 baseline 원자료에 없습니다.")
    valid = {e["event_id"] for e in row["artifact"]["events"]} | {"initial_state", "final_state", "response"}
    if not plan.get("evidence_ids") or set(plan["evidence_ids"]) - valid:
        raise ValueError("근거 ID는 해당 Artifact의 이벤트 또는 initial_state/final_state/response여야 합니다.")
    return plan


def fetch_observations(client, raw: dict) -> dict:
    """모든 페이지의 Observation을 읽는다. 모델 호출과 외부 쓰기는 하지 않는다."""
    from datetime import datetime, timedelta

    collected = {}
    start = datetime.fromisoformat(raw["started_at"]) - timedelta(minutes=1)
    end = datetime.fromisoformat(raw["ended_at"]) + timedelta(minutes=1)
    for trace_id in dict.fromkeys(row["trace_id"] for row in raw["rows"] if row["trace_id"]):
        cursor, seen, rows = None, set(), []
        while True:
            page = client.api.observations.get_many(trace_id=trace_id, limit=100, cursor=cursor,
                from_start_time=start, to_start_time=end, fields="core,basic,io,metadata,trace_context")
            rows.extend(o.model_dump(mode="json") for o in page.data)
            cursor = page.meta.cursor
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Observation 페이지 cursor가 반복됩니다. 불완전한 조회로 평가하지 않습니다.")
            seen.add(cursor)
        expected = {e["observation_id"] for row in raw["rows"] if row["trace_id"] == trace_id
                    for e in row["artifact"]["events"] if e["observation_id"]}
        found = {o["id"] for o in rows}
        collected[trace_id] = {"observations": rows, "missing_service_observation_ids": sorted(expected - found)}
    return collected


def push_scores(report: dict, client) -> None:
    """저장된 실행의 Trace와 위반 Observation에 판정을 쓴다. 같은 채점 재전송은 같은 ID를 쓴다."""
    from uuid import NAMESPACE_URL, uuid5

    for row in report["rows"]:
        trace_id = row["trace_id"]
        if not trace_id:
            continue
        events = {e["event_id"]: e for e in row["artifact"]["events"]}
        for verdict in row["verdicts"]:
            common = {"trace_id": trace_id, "name": f"course.{verdict['name']}", "value": verdict["status"],
                      "data_type": "CATEGORICAL", "comment": verdict["reason"],
                      "metadata": {"evaluation_run_id": report["run_id"], "source_run_id": report["source_run_id"],
                                   "mode": report["mode"],
                                   "rubric_version": report["config"]["response_rubric_version"]}}
            client.create_score(score_id=str(uuid5(NAMESPACE_URL, f"{report['run_id']}/{row['artifact']['artifact_id']}/{verdict['name']}")), **common)
            for eid in dict.fromkeys(verdict["evidence_ids"]):
                if eid in events and events[eid]["observation_id"]:
                    client.create_score(score_id=str(uuid5(NAMESPACE_URL, f"{report['run_id']}/{eid}/{verdict['name']}")),
                                        observation_id=events[eid]["observation_id"], **common)
    client.flush()


def export_human_review(scored: dict, csv_path: Path) -> Path:
    """Judge 점수를 숨긴 원자료 묶음과 빈 검수 CSV를 만든다."""
    export_review(scored, csv_path)
    path = csv_path.with_suffix(".context.json")
    write_json_new(path, {"source_run_id": scored["source_run_id"],
                         "rubric_version": scored["config"]["response_rubric_version"],
                         "instruction": "각 턴의 안내가 당시의 실제 상태와 일치하는지 판단하세요. "
                                        "응답 기준이 있으면 마지막 답변의 필수 내용도 대조하세요. Judge 점수는 보지 마세요.",
                         "response_references": {c["metadata"]["scenario_id"]: {
                             "reference_answer": c["expected_output"].get("reference_answer", ""),
                             "required_facts": c["expected_output"].get("required_facts", [])}
                             for c in scored["snapshot"]["cards"]},
                         "artifacts": [r["artifact"] for r in scored["rows"]]})
    return path


def calibrate_partitions(scored: dict, labels: list[dict]) -> dict:
    """같은 family의 반복을 같은 분할에 둔다. 보정용과 별도 확인용 오판 비율을 구분한다."""
    card_families = {c["metadata"]["scenario_id"]: c["metadata"]["family_id"] for c in scored["snapshot"]["cards"]}
    families = sorted(set(card_families.values()))
    partitions = {f: "calibration" if i % 2 == 0 else "validation" for i, f in enumerate(families)}
    # 전체 라벨의 잘못된 ID·중복을 먼저 검사한다.
    total = calibrate(scored, labels)
    result = {"run_id": make_run_id(), "source_run_id": scored["source_run_id"],
              "evaluation_run_id": scored["run_id"], "rubric_version": scored["config"]["response_rubric_version"],
              "family_partitions": partitions, "total": total, "partitions": {}}
    for split in ("calibration", "validation"):
        rows = [r for r in scored["rows"] if partitions[card_families[r["artifact"]["scenario_id"]]] == split]
        ids = {r["artifact"]["artifact_id"] for r in rows}
        result["partitions"][split] = calibrate(scored | {"rows": rows}, [r for r in labels if r["artifact_id"] in ids])
    # 전부 PASS인 표본으로는 false pass를 검증할 수 없다. 빈 확인용 분할도 검수 완료가 아니다.
    result["coverage_gaps"] = [
        f"{split}: {metric} 분모 없음"
        for split, counts in result["partitions"].items()
        for metric in ("false_pass_rate", "false_reject_rate")
        if counts[metric] is None
    ]
    result["status"] = ("pending" if total["pending"] or total["judge_unscored"] else
                        "insufficient_coverage" if result["coverage_gaps"] else "reviewed")
    return result


def promote_failure(raw: dict, plan: dict, out: Path, *, sanitization_record: str) -> dict:
    """실패 기록을 미검수 Scenario로 승격한다. 실제 결과를 기대 상태에 복사하지 않는다."""
    if not sanitization_record.strip():
        raise ValueError("원자료에 보관할 수 없는 정보가 없는지 확인하고 비식별화 기록을 직접 작성하세요.")
    row = next(r for r in raw["rows"] if r["artifact"]["artifact_id"] == plan["artifact_id"])
    card = deepcopy(next(c for c in raw["snapshot"]["cards"] if c["metadata"]["scenario_id"] == row["artifact"]["scenario_id"]))
    card["metadata"].update(scenario_id=f"regression-{make_run_id()}", source_trace_id=row["trace_id"],
                            source_artifact_id=row["artifact"]["artifact_id"], sanitization_record=sanitization_record,
                            review_status="pending", reviewer="", review_reason="")
    card["expected_output"]["final_state"] = {}
    write_workbook([card], out)
    write_json_new(out.with_suffix(".fixture.json"), {"source_run_id": raw["run_id"],
        "fixture_id": card["fixture_id"], "fixture": raw["snapshot"]["fixtures"][card["fixture_id"]],
        "initial_state": row["artifact"]["initial_state"], "instruction": "초기 상태 재현과 정책 기준을 검수하세요. 현재 비교에 합치지 마세요."})
    return card


def write_final_report(comparison: dict, plan: dict, out: Path, *, calibration=None, holdout=None) -> None:
    """비교·사람 검수·최종 확인을 연결한다. 증거가 없으면 승인으로 만들지 않는다."""
    reasons = list(comparison["comparison"]["decision_reasons"])
    if (not calibration or calibration.get("status") != "reviewed" or
            calibration.get("evaluation_run_id") != comparison.get("evaluation_run_ids", [None])[0]):
        reasons.append("Judge 사람 검수 미완료 또는 보정·확인용 PASS/FAIL 표본 부족")
    if calibration and calibration.get("total", {}).get("false_pass", 0):
        reasons.append("Judge가 사람의 FAIL을 PASS로 판정한 사례 있음: 근거 검토 필요")
    if not holdout or holdout.get("comparison", {}).get("decision") != "READY_FOR_HUMAN_REVIEW":
        reasons.append("holdout 확인 미완료 또는 미통과")
    costs = [s["total_cost_usd"] for s in comparison["comparison"]["summaries"].values()]
    if any(cost is None for cost in costs):
        reasons.append("실제 비용 미확인: 자원 기준 별도 판단 필요")
    decision = "HOLD" if reasons else "READY_FOR_HUMAN_REVIEW"
    lines = ["# Agent 개선 검토", "", f"판정: **{decision}**", "", f"데이터 스냅샷: `{comparison['snapshot_hash']}`", "",
             f"원자료 실행: {comparison['source_runs']}", "", "## 관찰과 가설", ""]
    for field in ("fact", "hypothesis", "alternative", "change", "success_criteria", "owner"):
        lines += [f"- {field}: {plan[field]}"]
    lines += ["", "## 동일 조건 비교", "", "| 문항 | baseline | candidate | 변화 |", "|---|---|---|---|"]
    for pair in comparison["comparison"]["pairs"]:
        b, c = pair["baseline"], pair["candidate"]
        lines.append(f"| {pair['scenario_id']} | {b['passed']}/{b['planned']} | {c['passed']}/{c['planned']} | {pair['change']} |")
    lines += ["", "## 남은 판단", "", *[f"- {r}" for r in reasons], "",
              "자동 배포 승인이 아닙니다. 개선과 회귀를 같은 원자료·판정 기준으로 설명하세요."]
    out.parent.mkdir(parents=True, exist_ok=True)
    history = out.parent / f"{out.stem}_runs" / f"{make_run_id()}.md"
    history.parent.mkdir(exist_ok=True)
    history.write_text("\n".join(lines))
    out.write_text("\n".join(lines))
