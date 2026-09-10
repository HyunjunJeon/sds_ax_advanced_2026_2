"""예정 실행 분모, 문항별 개선·회귀, 반복 신뢰성을 집계한다. 모델 호출은 없다."""

from collections import Counter
import random
import statistics


def key(row):
    return row["release"], row["scenario_id"], row["trial"]


def status_of(row: dict | None) -> str:
    if row is None:
        return "MISSING"
    artifact, checks = row["artifact"], row["verdicts"]
    if artifact["execution_status"] != "completed":
        return artifact["execution_status"].upper()
    statuses = {c["status"] for c in checks}
    if "EVALUATOR_ERROR" in statuses:
        return "EVALUATOR_ERROR"
    required = {"outcome", "side_effects", "policy", "recovery", "response_contract"}
    if not required <= {check["name"] for check in checks}:
        return "INSUFFICIENT_EVIDENCE"
    if "INSUFFICIENT_EVIDENCE" in statuses:
        return "INSUFFICIENT_EVIDENCE"
    return "FAIL" if "FAIL" in statuses else "PASS"


def quantile(values: list[float], p: float):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


def compare(report: dict) -> dict:
    """같은 시나리오·회차를 짝짓는다. 누락된 회차를 조회된 회차 수로 역산하지 않는다.

    bootstrap은 시나리오별 A/B와 모든 반복을 한 묶음으로 재표집한다. 작은 교육용 표본의
    구간을 운영 품질 보증으로 해석하지 않는다. scripted에서는 불확실성 추정을 하지 않는다.
    """
    rows, duplicates = {}, []
    planned_keys = {key(p) for p in report["plan"]}
    for row in report["rows"]:
        k = key(row["artifact"])
        if k in rows or k not in planned_keys:
            duplicates.append(k)
        rows[k] = row
    result = {"summaries": {}, "pairs": [], "integrity_errors": duplicates}

    def planned_status(p):
        row = rows.get(key(p))
        status = status_of(row)
        if row and status in {"PASS", "FAIL"} and report["mode"] == "live":
            if "response_semantics" not in {v["name"] for v in row["verdicts"]}:
                return "INSUFFICIENT_EVIDENCE"
        return status

    for release in ("baseline", "candidate"):
        plan = [p for p in report["plan"] if p["release"] == release]
        statuses = Counter(planned_status(p) for p in plan)
        by_case, case_valid, slices = {}, {}, {}
        for p in plan:
            passed = planned_status(p) == "PASS"
            by_case.setdefault(p["scenario_id"], []).append(passed)
            case_valid.setdefault(p["scenario_id"], []).append(planned_status(p) in {"PASS", "FAIL"})
            slices.setdefault(p["slice"], []).append(passed)
        artifacts = [rows[key(p)]["artifact"] for p in plan if key(p) in rows]
        passed = statuses["PASS"]
        valid = passed + statuses["FAIL"]
        costs = [a["cost_usd"] for a in artifacts]
        # 모든 예정 시도의 비용을 알 때만 총비용/성공당 비용을 산출한다.
        total_cost = sum(costs) if len(artifacts) == len(plan) and all(c is not None for c in costs) else None
        result["summaries"][release] = {
            "planned": len(plan), "found": len(artifacts), "statuses": dict(statuses),
            "passed": passed, "valid": valid, "pass_rate": passed / len(plan) if plan else None,
            "valid_pass_rate": passed / valid if valid else None,
            "evaluation_coverage": valid / len(plan) if plan else None,
            "pass_at_k": sum(any(v) for v in by_case.values()) / len(by_case) if by_case else None,
            "pass_all_k": sum(all(v) for v in by_case.values()) / len(by_case) if by_case else None,
            "cases": {k: {"passed": sum(v), "planned": len(v), "valid": sum(case_valid[k]), "all_pass": all(v)} for k, v in by_case.items()},
            "slices": {k: {"passed": sum(v), "planned": len(v)} for k, v in slices.items()},
            "tool_calls": sum(len(a["events"]) for a in artifacts),
            "model_calls": sum(a["model_calls"] for a in artifacts),
            "total_cost_usd": total_cost, "cost_per_pass_usd": total_cost / passed if total_cost is not None and passed else None,
            "latency_p50_seconds": quantile([a["elapsed_seconds"] for a in artifacts], 0.5),
            "latency_p95_seconds": quantile([a["elapsed_seconds"] for a in artifacts], 0.95),
        }
    base, candidate = (result["summaries"][r]["cases"] for r in ("baseline", "candidate"))
    differences = []
    for sid in base:
        b, c = base[sid], candidate[sid]
        comparable = b["valid"] == b["planned"] and c["valid"] == c["planned"]
        delta = c["passed"] / c["planned"] - b["passed"] / b["planned"] if comparable else None
        if comparable:
            differences.append(delta)
        result["pairs"].append({"scenario_id": sid, "baseline": b, "candidate": c, "delta": delta,
                                "change": "uncomparable" if not comparable else "regression" if delta < 0 else "improved" if delta > 0 else "unchanged"})
    result["complete_pairs"] = len(differences)
    result["mean_delta"] = statistics.mean(differences) if differences else None
    result["bootstrap_95"] = None
    if report["mode"] == "live" and len(differences) > 1:
        rng = random.Random(19)
        draws = [statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000)]
        result["bootstrap_95"] = [quantile(draws, 0.025), quantile(draws, 0.975)]
    candidate_rows = [row for row in report["rows"] if row["artifact"]["release"] == "candidate"]
    gate_failures = [v for row in candidate_rows for v in row["verdicts"]
                     if v["name"] in {"policy", "side_effects"} and v["status"] == "FAIL"]
    summary = result["summaries"]["candidate"]
    reasons = []
    if duplicates:
        reasons.append("중복 또는 계획에 없는 실행")
    if any(s["valid"] != s["planned"] for s in result["summaries"].values()):
        reasons.append("실행/평가 누락 또는 오류")
    if gate_failures:
        reasons.append("정책·보호 상태 위반")
    if any(pair["change"] == "regression" for pair in result["pairs"]):
        reasons.append("시나리오 회귀")
    if summary["passed"] != summary["planned"]:
        reasons.append("후보의 미통과 시나리오")
    if report["mode"] == "scripted":
        reasons.append("scripted 계약 검증: LLM 성능 미측정")
    result["decision"] = "HOLD" if reasons else "READY_FOR_HUMAN_REVIEW"
    result["decision_reasons"] = reasons
    return result


def print_report(report):
    print("\n=== 주문 취소 평가 ===")
    print(f"실행 {report['run_id']} / {report['mode']} / snapshot {report['snapshot_hash'][:12]}")
    for release, summary in report["comparison"]["summaries"].items():
        print(f"{release}: 통과 {summary['passed']}/{summary['planned']}, "
              f"유효 평가 {summary['valid']}/{summary['planned']}, 상태 {summary['statuses']}")
    for pair in report["comparison"]["pairs"]:
        print(f"  {pair['scenario_id']}: {pair['baseline']['passed']}/{pair['baseline']['planned']} → "
              f"{pair['candidate']['passed']}/{pair['candidate']['planned']}  {pair['change']}")
    print(f"판정: {report['comparison']['decision']} {report['comparison']['decision_reasons']}")
    print("실패 행의 artifact.events와 verdicts.evidence_ids에서 최초 위반 근거를 확인하세요.")
