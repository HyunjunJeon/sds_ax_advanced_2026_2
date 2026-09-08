"""기존 의도/복잡도 → 질의 변형 → 검색 → 생성 → 평가 → 재시도 구조에 근거 Retriever를 부착합니다."""
from __future__ import annotations

from typing import Literal, TypedDict
import time
import hashlib
import json

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field, ValidationError
from langchain_core.exceptions import OutputParserException

from day02.evidence import Evidence, GroundedAnswer, answer_from_evidence, render_context, select_evidence, CitationError


class QueryPlan(BaseModel):
    intent: Literal["fact", "comparison", "procedure", "analysis", "clarification"]
    complexity: Literal["simple", "medium", "complex"]
    standalone_question: str
    queries: list[str] = Field(min_length=1, max_length=3)
    needed_facts: list[str]


class Review(BaseModel):
    supported: bool
    unsupported_claims: list[str]
    missing: list[str]
    retry_query: str = ""


class RAGState(TypedDict, total=False):
    question: str
    history: list[dict]
    plan: dict
    evidence: list[dict]
    report: dict
    answer: dict
    review: dict
    attempts: int
    trace: list[dict]
    started_at: float
    llm_calls: int
    halted_reason: str
    errors: list[dict]
    seen_keys: list[str]
    reads: list[dict]


def plan_question(llm, question: str, history: list[dict]) -> QueryPlan:
    return llm.with_structured_output(QueryPlan).invoke([
        ("system", """업무 문서 검색 계획을 작성하세요. 대화에서 생략된 지시 대상을 복원하세요.
단순 사실은 검색 1회, 비교는 대상/조건별 2~3개 쿼리로 분해하세요.
날짜, 고객명, 수치, 부정 조건은 보존하세요. 이전 답변을 새로운 사실 근거로 사용하지 마세요.
clarification은 질문이 불명확해 문서를 읽어도 대상을 정할 수 없을 때 사용하세요."""),
        ("human", f"최근 대화: {history[-4:]}\n질문: {question}"),
    ])


def review_answer(llm, question: str, answer: GroundedAnswer, evidence: list[Evidence]) -> Review:
    return llm.with_structured_output(Review).invoke([
        ("system", """각 주장과 그 주장이 인용한 ID의 원문을 대조하세요.
인용이 존재한다는 이유만으로 supported=true를 주지 마세요.
대상/시점/단위/예외가 맞는지, 질문의 필수 항목이 빠졌는지 확인하세요.
누락/비약/상충이 있으면 supported=false, missing과 재검색 쿼리를 작성하세요."""),
        ("human", f"질문: {question}\n답변: {answer.model_dump_json()}\n근거: {render_context(evidence)}"),
    ])


def build_adaptive_graph(llm, retriever, max_attempts: int = 2, checkpointer=None,
                         max_llm_calls: int = 7, max_elapsed_seconds: float = 180):
    """동일 턴의 근거를 누적하고 실패는 보류합니다. 시간은 노드 경계에서 확인하며 강제 취소가 아닙니다.
    llm_calls는 실패 포함 논리 호출 수이며 SDK/HTTP 내부 재시도 횟수는 포함하지 않습니다."""
    if not 1 <= max_attempts <= 3:
        raise ValueError("실습의 재검색 상한은 1~3회입니다.")

    if max_llm_calls < 3 or max_elapsed_seconds <= 0:
        raise ValueError("계획·생성·검토 최소 3회 호출과 양의 시간 예산이 필요합니다.")

    def budget_reason(state, reserve_calls=1):
        if state["llm_calls"] + reserve_calls > max_llm_calls:
            return "LLM 호출 예산 소진"
        if time.monotonic() - state["started_at"] >= max_elapsed_seconds:
            return "실행 시간 예산 소진 (노드 경계에서 확인)"
        return ""

    def failure(state, node, error, calls=0):
        kind = ("citation" if isinstance(error, CitationError) else
                "schema" if isinstance(error, (ValidationError, OutputParserException)) else "external")
        # 예외 본문에는 HTTP 요청/인증 정보가 있을 수 있어 클래스명만 기록합니다.
        detail = {"node": node, "kind": kind, "exception": type(error).__name__}
        if isinstance(error, CitationError):
            detail["invalid_citations"] = error.errors
        return {"halted_reason": f"{node}: {kind} 실패",
                "llm_calls": state["llm_calls"] + calls,
                "errors": state["errors"] + [detail],
                "review": {"supported": False, "missing": [f"{node}: {kind} 실패"]},
                "trace": state["trace"] + [detail]}

    def stop(state, reason):
        return {"halted_reason": reason, "review": {"supported": False, "missing": [reason]},
                "trace": state["trace"] + [{"node": "budget_stop", "reason": reason}]}

    def plan_node(state):
        fresh = {"plan": {}, "attempts": 0, "evidence": [], "report": {},
                 "started_at": time.monotonic(), "llm_calls": 0, "halted_reason": "",
                 "answer": {}, "review": {}, "trace": [], "errors": [], "seen_keys": [], "reads": []}
        try:
            plan = QueryPlan.model_validate(plan_question(llm, state["question"], state.get("history", [])))
            fresh.update(plan=plan.model_dump(), llm_calls=1,
                         trace=[{"node": "plan", **plan.model_dump()}])
        except Exception as error:
            fresh.update(failure(fresh, "plan", error, calls=1))
        return fresh

    def evidence_key(item):
        # 동일 URI·물리 페이지·행 좌표·인용 내용은 검색 호출/읽기 창이 달라도 동일합니다.
        return hashlib.sha256(json.dumps([item.uri, item.page, item.start_line, item.end_line,
            hashlib.sha256(item.quote.encode()).hexdigest()], ensure_ascii=False).encode()).hexdigest()

    def retrieve_node(state):
        reason = budget_reason(state, reserve_calls=2)
        if reason:
            return stop(state, reason)
        queries = (state["plan"]["queries"] if state["attempts"] == 0 else
                   [state["review"].get("retry_query") or state["plan"]["standalone_question"]])
        reader = retriever
        reads = state["reads"]
        if hasattr(retriever, "model_copy"):
            # 문서·read 예산은 이 사용자 턴 전체에 적용합니다. 재읽기도 비용을 씁니다.
            remaining_calls = retriever.max_read_calls - len(reads)
            remaining_docs = retriever.max_documents - len({r["uri"] for r in reads})
            if min(remaining_calls, remaining_docs) <= 0:
                return stop(state, "누적 문서/read 예산 소진")
            reader = retriever.model_copy(update={"max_read_calls": remaining_calls,
                                                  "max_documents": remaining_docs})
        try:
            incoming, report = reader.retrieve_queries(queries)
        except Exception as error:
            return {**failure(state, "retrieve", error), "attempts": state["attempts"] + 1}
        old = [Evidence.model_validate(item) for item in state["evidence"]]
        merged = {evidence_key(item): item for item in old}
        incoming_keys = set()
        for item in incoming:
            key = evidence_key(item)
            incoming_keys.add(key)
            if key not in merged:
                merged[key] = item.model_copy(update={"evidence_id": "E-" + key[:24]})
        new_keys = incoming_keys - set(state["seen_keys"])
        selected, merged_report = select_evidence(list(merged.values()),
            getattr(retriever, "max_bytes", 5000), list(getattr(retriever, "facets", {})))
        all_reads = reads + report.get("reads", [])
        merged_report.update(reads=all_reads, read_calls=len(all_reads),
            documents_read=len({r["uri"] for r in all_reads}), retrieval_report=report,
            new_evidence_count=len(new_keys))
        reason = "재검색에서 새 근거를 확보하지 못했습니다." if state["attempts"] and not new_keys else ""
        return {"evidence": [item.model_dump(mode="json") for item in selected],
                "seen_keys": sorted(set(state["seen_keys"]) | incoming_keys), "reads": all_reads,
                "report": merged_report, "attempts": state["attempts"] + 1, "halted_reason": reason,
                "trace": state["trace"] + [{"node": "retrieve", "queries": queries, **merged_report}]}

    def generate_node(state):
        reason = budget_reason(state)
        if reason:
            return stop(state, reason)
        selected = [Evidence.model_validate(item) for item in state["evidence"]]
        calls = int(bool(selected))
        try:
            answer = answer_from_evidence(llm, state["plan"]["standalone_question"], selected)
        except Exception as error:
            return failure(state, "generate", error, calls)
        return {"answer": answer.model_dump(), "llm_calls": state["llm_calls"] + calls,
                "trace": state["trace"] + [{"node": "generate", "claims": len(answer.claims)}]}

    def review_node(state):
        reason = budget_reason(state)
        if reason:
            return stop(state, reason)
        selected = [Evidence.model_validate(item) for item in state["evidence"]]
        calls = int(bool(selected))
        try:
            answer = GroundedAnswer.model_validate(state["answer"])
            review = (review_answer(llm, state["plan"]["standalone_question"], answer, selected)
                      if selected else Review(supported=False, unsupported_claims=[], missing=answer.missing))
            review = Review.model_validate(review)
        except Exception as error:
            return failure(state, "review", error, calls)
        detail = {"node": "review", **review.model_dump()}
        errors = state["errors"] + ([{**detail, "kind": "support"}] if not review.supported else [])
        return {"review": review.model_dump(), "llm_calls": state["llm_calls"] + calls,
                "errors": errors, "trace": state["trace"] + [detail]}

    def route_review(state):
        review = state["review"]
        if state.get("halted_reason") or review["supported"] or state["attempts"] >= max_attempts or not review.get("retry_query"):
            return "finalize"
        return "retrieve"

    def finalize_node(state):
        reason = budget_reason(state, reserve_calls=0)
        if reason and not state.get("halted_reason"):
            state = {**state, "halted_reason": reason, "review": {"supported": False, "missing": [reason]}}
        answer = state.get("answer", {})
        if state["plan"].get("intent") == "clarification":
            answer = GroundedAnswer(missing=["질문의 대상·시점·업무 범위를 구체화해야 합니다."]).model_dump()
        elif not state.get("review", {}).get("supported", False):
            answer = GroundedAnswer(
                missing=state.get("review", {}).get("missing", []) or ([state["halted_reason"]] if state.get("halted_reason") else ["근거 검토를 통과하지 못했습니다."]),
                conflicts=answer.get("conflicts", []),
            ).model_dump()

        history = state.get("history", []) + [{"question": state["question"], "answer": answer}]
        return {"answer": answer, "history": history[-4:], "halted_reason": state.get("halted_reason", ""),
                "trace": state["trace"] + [{"node": "finalize", "attempts": state["attempts"], "llm_calls":state["llm_calls"],
                    "elapsed_seconds":round(time.monotonic()-state["started_at"],2), "halted_reason":state.get("halted_reason","")}]}

    graph = StateGraph(RAGState)
    graph.add_node("plan", plan_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_node("review", review_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", lambda state: "finalize" if state.get("halted_reason") or state["plan"].get("intent") == "clarification" else "retrieve", {"finalize": "finalize", "retrieve": "retrieve"})
    graph.add_conditional_edges("retrieve", lambda state: "finalize" if state.get("halted_reason") else "generate", {"finalize": "finalize", "generate": "generate"})
    graph.add_conditional_edges("generate", lambda state: "finalize" if state.get("halted_reason") else "review", {"finalize": "finalize", "review": "review"})
    graph.add_conditional_edges("review", route_review, {"retrieve": "retrieve", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())
