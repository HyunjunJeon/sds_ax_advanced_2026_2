"""실행 후 평가 전용. gold는 이 모듈에서만 읽으며 Agent 입력에 전달하지 않는다.

가르치는 것:
- 평가의 격리: 정답표가 Agent 입력으로 새면 평가가 아니라 암기다. judge 호출에만
  정답을 주고 그 비용도 따로 기록한다.
- 검사의 층위 분리: 인용 ID 유효성(코드 검사), 자체 검토 통과(complete), 의미 정확성
  (judge)은 서로 다른 검사이며, 정답 단어가 있다는 이유만으로 통과시키지 않는다.
"""

from __future__ import annotations

import json
import re
import statistics

from pydantic import BaseModel


class Quality(BaseModel):
    correct: bool
    required_facts_met: list[str]
    missing_facts: list[str]
    unsupported_claims: list[str]
    appropriate_abstention: bool
    reason: str


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(result, gold, judge=None):
    text = result.get("text", "")
    refs = result.get("evidence", [])
    doc_ids = {e["doc_id"] for e in refs}
    expected = gold.get("required_docs", [])
    citations = [
        e for c in result.get("answer", {}).get("claims", []) for e in c.get("evidence_ids", [])
    ]
    present = {e["evidence_id"] for e in refs}
    observation = {
        "citation_ids_valid": all(e in present for e in citations),
        "required_document_recall": len(doc_ids & set(expected)) / len(expected)
        if expected
        else None,
        "expected_abstention": gold.get("abstain", False),
        "status": result.get("status"),
        "pattern_checks": {p: bool(re.search(p, text, re.I)) for p in gold.get("patterns", [])},
        "semantic_quality": None,
    }
    # ID의 존재 검사와 의미 지지 평가는 서로 다른 검사다. 원문을 인용해도 잘못된
    # 결론일 수 있고, judge 자체도 오류를 낼 수 있다. 누락된 평가를 0점/정답으로 채우지 않는다.
    if judge:
        review = judge.ask(
            Quality,
            "교재 원문에 대조해 작성한 기대 사실과 반환된 주장·인용 원문을 대조하라. "
            "정답의 단어가 있다는 이유만으로 통과시키지 말고 반대 주장, 누락, 예외, 적용 대상을 검사하라. "
            "expected_abstention이 참이면 알 수 없는 내부 사실을 추측하지 않고 보류했는지 평가하라.",
            {"expected": gold, "result": result},
            "offline_judge",
        )
        observation["semantic_quality"] = review.model_dump()
    return observation


def summarize(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["architecture"], []).append(row)
    output = []
    for architecture, runs in groups.items():
        valid = [
            r
            for r in runs
            if r["result"]["status"] not in {"failed", "budget_exceeded", "index_pending"}
        ]
        # 이 accuracy의 분모는 judge가 실제 반환한 평가뿐이다. 전체 실행의 성공률이
        # 아니다. 13번 분석기는 전체 실행 수와 평가 커버리지를 함께 출력한다.
        qualities = [r["evaluation"].get("semantic_quality") for r in runs if r.get("evaluation")]
        qualities = [q for q in qualities if q]
        tokens = [r["metrics"]["input_tokens"] for r in runs]
        output.append(
            {
                "architecture": architecture,
                "runs": len(runs),
                "completed": sum(r["result"]["status"] == "complete" for r in runs),
                "uncertain": sum(r["result"]["status"] == "uncertain" for r in runs),
                "errors": len(runs) - len(valid),
                "mean_latency_s": round(
                    statistics.mean(r["metrics"]["elapsed_s"] for r in runs), 3
                ),
                "mean_model_calls": round(
                    statistics.mean(r["metrics"]["calls"].get("model", 0) for r in runs), 2
                ),
                "mean_input_tokens": round(statistics.mean(tokens), 1)
                if tokens and all(t is not None for t in tokens)
                else None,
                "judged_accuracy": sum(q["correct"] for q in qualities) / len(qualities)
                if qualities
                else None,
            }
        )
    return output
