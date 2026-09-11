"""
Goal: gold_agent.py가 채운 golden_dataset의 actual_output을 DeepEval FaithfulnessMetric으로
채점한다. gold_agent.py는 건드리지 않는다.

trace_id는 golden CSV에 저장돼 있지 않으므로, Langfuse에서 name="gold-agent-question" trace를
모아 input.question 문자열로 매칭한다. 같은 질문이 여러 번 재실행됐으면 가장 최근 trace를 쓴다.

retrieval_context는 trace의 도구 호출 observation(type=="TOOL", name in read_file/grep)의
output.content만 모은다. glob/ls는 경로 목록일 뿐 근거 문장이 아니라 제외한다. LangGraph의
ToolNode를 감싸는 type=="CHAIN", name=="tools" observation은 같은 내용을 중복 반환하므로 제외한다.

오염 방어: read_file/grep의 input에 "golden_dataset"이 있으면 즉시 멈춘다. gold_agent.py의
DOC_CACHE_DIR가 GOLDEN_CSV(정답 컬럼 포함)를 다시 포함하게 됐다는 뜻이라, 채점 자체가 무의미하다.

도구 호출 없이 답한 문항은 FaithfulnessMetric을 부르지 않고 자동 실패 처리한다 — 도구 호출
유무는 코드로 바로 판별되는 사실이라 유료 판사를 쓰지 않는다.
"""

import json
from csv import DictReader
from pathlib import Path

from business_lab.connections import connect_langfuse
from business_lab.models import load_judge

GOLDEN_CSV = Path(
    "outputs/gold_agent/golden_dataset_top10.csv"
)  # gold_agent.py와 같은 경로 유지
RESULTS_JSON = Path("outputs/gold_agent/faithfulness_scores.json")
TRACE_NAME = "gold-agent-question"
RETRIEVAL_TOOLS = {"read_file", "grep"}
FAITHFULNESS_THRESHOLD = 0.8
PUSH_TO_LANGFUSE = (
    False  # True면 trace마다 create_score를 호출해 Langfuse에 점수를 남긴다.
)


def load_rows() -> list[dict]:
    with GOLDEN_CSV.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in DictReader(f) if r["actual_output"].strip()]


def fetch_recent_traces(client, page_limit: int = 50, max_pages: int = 10) -> list:
    """name=TRACE_NAME인 trace를 모두 모은다."""
    collected, page = [], 1
    while page <= max_pages:
        result = client.api.trace.list(name=TRACE_NAME, limit=page_limit, page=page)
        if not result.data:
            break
        collected.extend(result.data)
        if len(result.data) < page_limit:
            break
        page += 1
    return collected


def match_trace(traces: list, question: str):
    """input.question이 정확히 일치하는 trace 중 가장 최근 것을 고른다."""
    candidates = [
        t
        for t in traces
        if isinstance(t.input, dict) and t.input.get("question") == question
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.timestamp)


def build_retrieval_context(client, trace_id: str) -> list[str]:
    page = client.api.observations.get_many(
        trace_id=trace_id,
        limit=100,
        fields="core,basic,io,metadata,trace_context",
    )
    tool_obs = [
        o.model_dump(mode="json")
        for o in page.data
        if o.type == "TOOL" and o.name in RETRIEVAL_TOOLS
    ]
    contaminated = [o for o in tool_obs if "golden_dataset" in str(o.get("input", ""))]
    if contaminated:
        raise ValueError(
            f"오염된 trace {trace_id}: observation {[o['id'] for o in contaminated]}가 "
            "golden_dataset CSV를 읽었습니다. gold_agent.py의 DOC_CACHE_DIR/GOLDEN_CSV 경로 "
            "분리부터 다시 확인하세요."
        )
    tool_obs.sort(key=lambda o: o["start_time"])
    return [
        o["output"]["content"]
        for o in tool_obs
        if o.get("output") and o["output"].get("content")
    ]


def main():
    rows = load_rows()
    if not rows:
        print(
            f"{GOLDEN_CSV}에 채점할 actual_output이 없습니다. gold_agent.py를 먼저 실행하세요."
        )
        return

    client = connect_langfuse()
    traces = fetch_recent_traces(client)
    print(
        f"Langfuse에서 {TRACE_NAME} trace {len(traces)}건 조회. "
        f"Faithfulness 판사 호출은 최대 {len(rows)}회입니다. 진행합니다."
    )

    from deepeval.metrics import FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    judge = load_judge(agent_model=None)
    metric = FaithfulnessMetric(
        threshold=FAITHFULNESS_THRESHOLD, model=judge, include_reason=True
    )

    results = []
    for row in rows:
        question = row["input"]
        trace = match_trace(traces, question)
        if trace is None:
            results.append(
                {
                    "input": question,
                    "status": "TRACE_NOT_FOUND",
                    "reason": "질문과 일치하는 Langfuse trace 없음",
                }
            )
            print(f"[NOT_FOUND] {question[:40]}...")
            continue

        retrieval_context = build_retrieval_context(client, trace.id)
        if not retrieval_context:
            results.append(
                {
                    "input": question,
                    "trace_id": trace.id,
                    "status": "AUTO_FAIL",
                    "score": 0.0,
                    "passed": False,
                    "reason": "read_file/grep 호출 없이 답변 — 판사 호출 생략",
                }
            )
            print(f"[AUTO_FAIL] {question[:40]}...")
            continue

        test_case = LLMTestCase(
            input=question,
            actual_output=row["actual_output"],
            retrieval_context=retrieval_context,
        )
        metric.measure(test_case)
        results.append(
            {
                "input": question,
                "trace_id": trace.id,
                "status": "SCORED",
                "score": metric.score,
                "passed": metric.is_successful(),
                "reason": metric.reason,
                "retrieval_context_count": len(retrieval_context),
            }
        )
        print(
            f"[{'PASS' if metric.is_successful() else 'FAIL'}] {metric.score:.2f} {question[:40]}..."
        )

        if PUSH_TO_LANGFUSE:
            client.create_score(
                trace_id=trace.id,
                name="faithfulness",
                value=metric.score,
                comment=metric.reason,
            )

    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if PUSH_TO_LANGFUSE:
        client.flush()

    scored = [r for r in results if r["status"] in ("SCORED", "AUTO_FAIL")]
    passed = sum(1 for r in scored if r.get("passed"))
    not_found = [r for r in results if r["status"] == "TRACE_NOT_FOUND"]
    print(
        f"채점 완료: {passed}/{len(scored)}건 통과 (threshold={FAITHFULNESS_THRESHOLD}). 결과: {RESULTS_JSON}"
    )
    if not_found:
        print(f"⚠ {len(not_found)}건은 Langfuse trace를 찾지 못해 채점하지 못했습니다.")


if __name__ == "__main__":
    main()
