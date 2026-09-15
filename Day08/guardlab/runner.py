"""
사용: prepared = prepare(case, "02", config, r); out = run_case(agent, prepared); resume(agent, prepared, decisions, thread_id=...)
포인트:
  1. prepare() 가 실행마다 새 작업 공간·전송함·기록을 만든다. 같은 사례를 다른 구성으로 돌려도 초기 상태가 같다.
  2. run_case() 는 예외를 삼켜 out["error"] 로 돌려준다. 실패 행도 결과에 남기기 위해서다. OpenRouter 오류 본문은 _describe_error 가 붙인다.
  3. announce() 는 유료 호출 직전에 규모를 한 줄 고지한다 (루트 AGENTS.md 6장).
  4. interrupt 가 걸리면 out["interrupted"]=True, out["interrupt"] 에 HITL 요청이 담긴다 (05).

주요 내용:
번호 파일이 공통으로 쓰는 실행 절차. 실행 조건은 각 파일 상단 상수이며 CLI 플래그는 없다.
run_case  사례 하나를 Agent 에 넣고 최종 답변·오류·중단(interrupt) 여부를 돌려준다.
announce  유료 호출 직전에 규모를 한 줄로 알린다 (루트 AGENTS.md 6장).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from .cases import Case
from .config import OUTPUTS, WORK, model_id
from .context import UserContext, load_user
from .outbox import Outbox
from .trace import RunLog
from .workspace import Workspace


@dataclass
class Prepared:
    case: Case
    ctx: UserContext
    ws: Workspace
    outbox: Outbox
    log: RunLog
    run_dir: Path


def prepare(case: Case, lab: str, config_name: str, repeat: int = 1) -> Prepared:
    """사례마다 새 작업 공간을 만든다. 비교군이 달라도 초기 상태는 같다."""
    run_dir = WORK / lab / f"{case.id}__{config_name}__r{repeat}"
    ws = Workspace.create(run_dir, case.doc_overrides)
    return Prepared(case=case, ctx=load_user(case.user), ws=ws, outbox=Outbox(run_dir), log=RunLog(), run_dir=run_dir)


def run_id(lab: str) -> str:
    return f"{lab}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def output_dir(lab: str, rid: str | None = None) -> Path:
    return OUTPUTS / lab / (rid or run_id(lab))


def announce(calls_estimate: str, note: str = "") -> None:
    print(f"[비용 고지] 모델 {model_id()} 로 {calls_estimate}. {note}".rstrip())


def final_text(result: dict) -> str:
    for m in reversed(result.get("messages", [])):
        if isinstance(m, AIMessage):
            if isinstance(m.content, str):
                return m.content
            return " ".join(b.get("text", "") for b in m.content if isinstance(b, dict))
    return ""


def run_case(agent, prepared: Prepared, *, message: str | None = None, thread_id: str | None = None,
             recursion_limit: int = 150) -> dict:
    """Agent 를 한 번 실행한다. 예외는 행에 남기고 삼킨다. interrupt 가 걸리면 그 상태로 돌려준다."""
    msg = message or prepared.case.user_message
    config = {"recursion_limit": recursion_limit, "configurable": {"thread_id": thread_id or f"{prepared.case.id}-1"}}
    t0 = time.perf_counter()
    out: dict = {"final_answer": "", "error": None, "interrupted": False, "interrupt": None, "result": None}
    try:
        result = agent.invoke({"messages": [HumanMessage(msg)]}, config=config, context=prepared.ctx)
        out["result"] = result
        out["final_answer"] = final_text(result)
        if "__interrupt__" in result:
            out["interrupted"] = True
            out["interrupt"] = result["__interrupt__"]
    except Exception as e:  # noqa: BLE001 — 실패도 행으로 남긴다
        out["error"] = _describe_error(e)
    out["elapsed_s"] = time.perf_counter() - t0
    return out


def _describe_error(e: Exception) -> str:
    """예외 문자열이 짧을 때(OpenRouter 'Provider returned error') SDK 가 예외 인자에 담아 둔 응답 본문까지 붙인다.

    openrouter SDK 는 `BadRequestResponseError(response_data, http_res, http_res_text)` 형태로 던진다.
    응답 객체는 스트리밍 상태라 .text 접근이 실패할 수 있으므로 문자열 인자만 쓴다.
    """
    parts = [f"{type(e).__name__}: {str(e)[:200]}"]
    for a in e.args[1:] if e.args else ():
        if isinstance(a, (str, bytes)):
            text = a.decode("utf-8", "replace") if isinstance(a, bytes) else a
            if text and text not in parts[0]:
                parts.append(f"body={text[:500]}")
        elif isinstance(a, dict):
            parts.append(f"data={str(a)[:500]}")
    return " | ".join(parts)


def resume(agent, prepared: Prepared, decisions: list[dict], *, thread_id: str, recursion_limit: int = 150) -> dict:
    """HITL 중단을 사람 결정으로 재개한다. decisions 는 [{"type":"approve"}] 같은 목록."""
    from langgraph.types import Command

    config = {"recursion_limit": recursion_limit, "configurable": {"thread_id": thread_id}}
    t0 = time.perf_counter()
    out: dict = {"final_answer": "", "error": None, "interrupted": False, "interrupt": None, "result": None}
    try:
        result = agent.invoke(Command(resume={"decisions": decisions}), config=config, context=prepared.ctx)
        out["result"] = result
        out["final_answer"] = final_text(result)
        if "__interrupt__" in result:
            out["interrupted"] = True
            out["interrupt"] = result["__interrupt__"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["elapsed_s"] = time.perf_counter() - t0
    return out


def print_row(row: dict) -> None:
    b = row["blocked_at"]
    print(f"- {row['case']} [{row['config'].get('name', '')}] 공격달성={row['attack_goal_achieved']} "
          f"차단={f'{b['stage']}@{b['agent']}' if b else '-'} 업무완료={row['task_completed']} "
          f"유출={row['leaked']} 무단열람={row['unauthorized_reads']} 정책변경={row['policy_changed']} "
          f"모델호출={row['ops']['model_calls']} 오류={row['error'] or '-'}")
    if row["achieved_goals"]:
        for g in row["achieved_goals"]:
            print(f"    · 달성된 목표: {g['type']} ← {g['evidence']}")
    if row["missed_paths"]:
        print(f"    · 검사 없이 실행된 경로: {row['missed_paths']}")
