"""실험의 단위를 사례·반복·턴으로 맞춘다. 성공 행만 남기는 집계를 허용하지 않는다.

가르치는 것:
- 짝 비교의 강제: 사례·반복·턴이 같은 실행끼리만 비교하고, 빠진 짝·선언하지 않은
  조건 차이·분해 계획 불일치(require_same_plan)는 예외로 거절한다. 분석가의 실수를
  분석기가 막는 구조다.
- 해석의 보호: usage·judge 누락은 0점/정답으로 채우지 않고 커버리지로 따로 남긴다.
  실패 분율, 평가된 짝의 품질, 코드 provenance를 분리해 승자 선언은 사람에게 남긴다.

13번은 정답을 새로 판정하지 않는다. 09번이 남긴 결과/평가/usage를 읽어 조건 차이와
관측 범위를 확인한다. 반복 3회도 통계적 유의성이나 일반화의 보증은 아니다.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

# 사용자 질문/고객/기준일/코퍼스/모델/backend는 통제 변수다. 바꾸는 실험이라면
# 별도의 실험 설계가 필요하다. 아래 옵션은 이번 구조/실행 정책 비교에서만 허용한다.
CHANGEABLE = {
    "implementation",
    "architecture",
    "context_bytes",
    "context_mode",
    "serial_workers",
    "no_replan",
    "no_owner_memory",
    "skill",
    "web_mode",
    "max_calls",
    "max_tools",
    "seconds",
    "web_max_age_hours",
    "max_web_documents",
}
FAILURES = {"failed", "budget_exceeded", "index_pending"}


def read_runs(path: Path) -> list[dict]:
    path = Path(path)
    if path.is_dir():
        path = path / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _index(rows, architecture):
    result = {}
    for row in rows:
        if row["architecture"] != architecture:
            continue
        key = (row["case_id"], row["repeat"], row["turn"])
        if key in result:
            raise ValueError(f"중복 실행 키: {key}. 서로 다른 실험 파일을 임의로 합치지 마세요.")
        result[key] = row
    if not result:
        raise ValueError(f"선택한 구조의 실행이 없습니다: {architecture}")
    return result


def _plan(row):
    # 모델의 설명문 reason은 제외하고 실제 실행할 역할/목적/의존성을 비교한다.
    return [
        sorted(event["plan"]["tasks"], key=lambda task: task["id"])
        for event in row.get("trace", [])
        if event.get("event") == "plan"
    ]


def _quality(row):
    evaluation = row.get("evaluation") or {}
    quality = evaluation.get("semantic_quality") or {}
    correct = quality.get("correct")
    if type(correct) is not bool or type(evaluation.get("citation_ids_valid")) is not bool:
        return None
    return correct and evaluation["citation_ids_valid"]


def analyze_pairs(
    baseline_rows,
    candidate_rows,
    *,
    baseline_architecture,
    candidate_architecture,
    changed=frozenset({"architecture"}),
    min_repeats=3,
    require_same_plan=False,
):
    if not changed <= CHANGEABLE or min_repeats < 1:
        raise ValueError("허용된 변경 변수와 양수 min_repeats를 지정하세요.")
    left = _index(baseline_rows, baseline_architecture)
    right = _index(candidate_rows, candidate_architecture)
    if set(left) != set(right):
        # 교집합만 분석하면 후보에서 실패/누락한 사례가 사라져 품질이 좋아 보인다.
        raise ValueError("두 실험의 사례·반복·턴이 다릅니다. 빠진 실행을 보존/재실행하세요.")
    repeats = {}
    for case, repeat, _ in left:
        repeats.setdefault(case, set()).add(repeat)
    if any(len(values) < min_repeats for values in repeats.values()):
        raise ValueError(f"사례마다 최소 {min_repeats}회 반복이 필요합니다.")
    actual_changes = set()
    plan_differences = 0
    deltas = {"latency_s": [], "model_calls": [], "input_tokens": []}
    final_keys = {
        (case, repeat, max(k[2] for k in left if k[:2] == (case, repeat)))
        for case, repeat, _ in left
    }
    quality_deltas = []
    for key in sorted(left):
        a, b = left[key], right[key]
        for field in ("request", "initial_corpus", "model", "backend", "mode", "exa_mode"):
            if field not in a or field not in b or a[field] != b[field]:
                raise ValueError(f"통제 조건 불일치: {key} / {field}")
        conditions_a = {
            "architecture": a["architecture"],
            "web_mode": a["web_mode"],
            **a["settings"],
        }
        conditions_b = {
            "architecture": b["architecture"],
            "web_mode": b["web_mode"],
            **b["settings"],
        }
        pa, pb = a.get("provenance"), b.get("provenance")
        if bool(pa) != bool(pb):
            raise ValueError(f"코드/자료 provenance가 한쪽에만 있습니다: {key}")
        if pa and pb:
            if pa["assets"] != pb["assets"] or pa["python"] != pb["python"]:
                raise ValueError(f"자료·의존성·Python 조건 불일치: {key}")
            conditions_a["implementation"] = pa["source_sha256"]
            conditions_b["implementation"] = pb["source_sha256"]
        differences = {
            name
            for name in conditions_a.keys() | conditions_b.keys()
            if conditions_a.get(name) != conditions_b.get(name)
        }
        if not differences <= changed:
            raise ValueError(f"선언하지 않은 변경: {key} / {sorted(differences - changed)}")
        actual_changes.update(differences)
        if _plan(a) != _plan(b):
            plan_differences += 1
            if require_same_plan:
                raise ValueError(f"분해 계획 불일치: {key}. 같은 계획을 재생한 실행이 필요합니다.")
        deltas["latency_s"].append(b["metrics"]["elapsed_s"] - a["metrics"]["elapsed_s"])
        deltas["model_calls"].append(
            b["metrics"]["calls"].get("model", 0) - a["metrics"]["calls"].get("model", 0)
        )
        ta, tb = a["metrics"].get("input_tokens"), b["metrics"].get("input_tokens")
        # 공급자가 usage를 반환하지 않으면 토큰 변화도 알 수 없다. 0으로 채우지 않는다.
        if ta is not None and tb is not None:
            deltas["input_tokens"].append(tb - ta)
        qa, qb = _quality(a), _quality(b)
        if key in final_keys and qa is not None and qb is not None:
            quality_deltas.append(int(qb) - int(qa))

    def arm(rows):
        statuses = Counter(row["result"]["status"] for row in rows.values())
        final_quality = [_quality(rows[key]) for key in final_keys]
        return {
            "statuses": dict(statuses),
            "failure_fraction_all_turns": sum(statuses[s] for s in FAILURES) / len(rows),
            "quality_evaluated_final_turns": sum(q is not None for q in final_quality),
            "verified_correct_final_turns": sum(q is True for q in final_quality),
            "final_turns": len(final_quality),
        }

    return {
        "pairs": len(left),
        "repeats_per_case": {case: len(values) for case, values in repeats.items()},
        "actual_changes": sorted(actual_changes),
        "different_plan_pairs": plan_differences,
        "pairs_without_code_provenance": sum(not row.get("provenance") for row in left.values()),
        "baseline": arm(left),
        "candidate": arm(right),
        "paired_delta_candidate_minus_baseline": {
            metric: {
                "observed_pairs": len(values),
                "mean": statistics.mean(values) if values else None,
                "median": statistics.median(values) if values else None,
            }
            for metric, values in deltas.items()
        },
        "paired_quality": {
            "both_evaluated": len(quality_deltas),
            "eligible_final_turns": len(final_keys),
            "coverage": len(quality_deltas) / len(final_keys),
            "mean_delta_on_evaluated_pairs": statistics.mean(quality_deltas)
            if quality_deltas
            else None,
        },
        "interpretation": "실패를 포함한 짝 비교입니다. complete는 정답률이 아닙니다. "
        "계획이 다르면 동시 실행만의 효과로 해석할 수 없습니다. "
        "입력 토큰은 usage가 있는 짝만 집계하며 평가/서버 비용은 별도입니다.",
    }
