"""실제 모델 → Deep Agents 프로세스 → MCP 서버 → 근거 파일을 검증한다.

실제 모델 호출 약 20~35회, 보통 2~5분. 모든 시도·실패를 새 근거 폴더에 남긴다.
서버 계약만 빠르게 확인하려면 모델을 쓰지 않는 verify.py를 먼저 실행한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DAY04 = Path(__file__).resolve().parents[1]
LAB = Path(__file__).resolve().parent
GOAL = "비품 대여 현황을 요약하는 도구"
CONSTRAINTS = "외부 전송 없이 로컬에서 처리한다."
ACCEPTANCE = "입력한 대여 건수와 요약의 대여 건수가 일치한다."
MCP_TOOLS = {
    "start_interview",
    "record_answer",
    "get_session",
    "freeze_seed",
    "list_evidence",
    "read_evidence",
    "save_evidence",
}


def records(root: Path) -> list[dict]:
    return sorted(
        (
            json.loads(path.read_text(encoding="utf-8"))
            for path in root.glob("*/*.json")
            if re.fullmatch(r"[0-9a-f]{32}|_unassigned", path.parent.name)
        ),
        key=lambda record: record["exported_at"],
    )


def body(call: dict) -> dict:
    return call["content"]["result"].get("structuredContent") or {}


def request(call: dict) -> dict:
    return call["content"]["request"]


def final_answer(trace: list[dict]) -> str:
    """도구 호출 없이 사용자에게 보여 준 답변만 모은다."""
    return "\n".join(
        row["content"]
        for row in trace
        if row.get("role") == "ai" and row.get("content") and not row.get("tool_calls")
    )


def read_skill(trace: list[dict]) -> bool:
    calls = {
        call["id"]
        for row in trace
        for call in row.get("tool_calls", [])
        if call["name"] == "read_file"
        and str(call["args"].get("file_path", "")).endswith(
            "/requirements-interview/SKILL.md"
        )
    }
    return any(
        row.get("role") == "tool"
        and row.get("tool_call_id") in calls
        and row.get("status") != "error"
        and "requirements-interview" in row.get("content", "")
        for row in trace
    )


def run_case(args, output, name, prompt, db, evidence) -> dict:
    trace_path = output / "runs" / f"{name}.jsonl"
    log_path = output / "runs" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    before = {record["evidence_id"] for record in records(evidence)}
    command = [
        sys.executable,
        str(LAB / "agent.py"),
        "--server",
        str(args.server),
        "--skills-dir",
        str(args.skills_dir),
        "--db",
        str(db),
        "--evidence-dir",
        str(evidence),
        "--trace",
        str(trace_path),
        "--prompt",
        prompt,
    ]
    print(f"[실행] {name}: 새 Deep Agents·MCP 프로세스, 실제 모델 호출", flush=True)
    started = time.monotonic()
    timeout = False
    with log_path.open("x", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=DAY04,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            timeout = True
            # 시간 초과 시 MCP 자식 프로세스도 함께 종료하고 부분 기록을 보존한다.
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        except KeyboardInterrupt:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    trace = (
        [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
        ]
        if trace_path.exists()
        else []
    )
    new = [
        record for record in records(evidence) if record["evidence_id"] not in before
    ]
    calls = [record for record in new if record["kind"] == "tool_call"]
    case = {
        "name": name,
        "command": command,
        "agent_pid": process.pid,
        "server_pids": sorted(
            {
                call["content"].get("server_pid")
                for call in calls
                if call["content"].get("server_pid")
            }
        ),
        "exit_code": process.returncode,
        "timed_out": timeout,
        "seconds": round(time.monotonic() - started, 2),
        "trace_path": str(trace_path),
        "log_path": str(log_path),
        "skill_read": read_skill(trace),
        "observed_model_responses": sum(row.get("role") == "ai" for row in trace),
        "tool_calls": [request(call)["name"] for call in calls],
        "trace": trace,
        "calls": calls,
    }
    print(
        f"[종료] {name}: exit={case['exit_code']}, 서버 PID={case['server_pids']}",
        flush=True,
    )
    return case


def verify(args, output: Path, checks: list, cases: list) -> None:
    evidence = output / "mcp"
    work = DAY04 / "work" / "agent-validation" / output.name
    work.mkdir(parents=True, exist_ok=False)
    db = work / "sessions.sqlite3"

    def expect(condition, name, **details):
        checks.append({"name": name, "passed": bool(condition), **details})
        print(f"[{'통과' if condition else '실패'}] {name}", flush=True)

    def execute(name, prompt, database=db, root=evidence):
        case = run_case(args, output, name, prompt, database, root)
        cases.append(case)
        expect(
            case["exit_code"] == 0 and not case["timed_out"],
            f"{name}: 프로세스 정상 종료",
        )
        expect(
            any(row["kind"] == "completed" for row in case["trace"]),
            f"{name}: 에이전트 실행 완료",
        )
        expect(
            any(
                row["kind"] == "connected" and MCP_TOOLS <= set(row["mcp_tools"])
                for row in case["trace"]
            ),
            f"{name}: 실제 MCP 도구 일곱 개 발견",
        )
        expect(case["skill_read"], f"{name}: Skill 본문 읽기 성공")
        expect(
            len(case["server_pids"]) == 1
            and case["agent_pid"] not in case["server_pids"],
            f"{name}: 별도 MCP 서버 프로세스에서 처리",
        )
        return case

    missing = execute(
        "01_incomplete",
        (
            f"새 업무의 요구사항 정리를 시작해 줘. 목표는 다음 문장 그대로야: 「{GOAL}」. "
            "제약과 완료 기준은 아직 정하지 않았어. 서버 계약을 검증하려고 하니, "
            "세션을 시작한 뒤 답변을 기록하지 않은 상태에서 freeze_seed를 한 번 호출해 "
            "실제 거절 결과를 확인해 줘. 임의의 답변을 넣지 말고 결과 요약을 근거로 남겨 줘."
        ),
    )
    starts = [
        record
        for record in records(evidence)
        if record["kind"] == "event"
        and record["content"]["event"] == "interview_started"
    ]
    expect(len(starts) == 1, "목표로 세션 하나 생성")
    if not starts:
        expect(False, "후속 세 사례 실행 불가: 시작 세션이 생성되지 않음")
        return
    session_id = starts[0]["session_id"]
    expect(starts[0]["content"]["payload"]["goal"] == GOAL, "목표 원문 유지")
    expect(
        session_id in final_answer(missing["trace"]),
        "최종 답변에 세션 ID를 남겨 다음 실행으로 이어갈 수 있음",
    )
    expect(
        any(
            request(call)["name"] == "freeze_seed"
            and body(call).get("error") == "seed_incomplete"
            for call in missing["calls"]
        ),
        "필수 답변 없는 확정을 서버가 실제 거절",
    )
    expect(
        not any(
            record["kind"] == "seed"
            or (
                record["kind"] == "event"
                and record["content"]["event"] == "answer_recorded"
            )
            for record in records(evidence)
        ),
        "답변을 지어내거나 불완전한 명세를 확정하지 않음",
    )

    complete = execute(
        "02_resume",
        (
            f"세션 {session_id}의 요구사항 정리를 이어가 줘. 저장된 목표를 유지해 줘. "
            f"제약은 「{CONSTRAINTS}」, 완료 기준은 「{ACCEPTANCE}」야. "
            "두 답변은 각각 따옴표 안의 원문 그대로 기록해 줘. 명세를 확정한 뒤 "
            "명세 파일을 읽고 최종 요약을 근거 파일로 저장해 줘."
        ),
    )
    expect("get_session" in complete["tool_calls"], "새 대화에서 저장된 세션 조회")
    seeds = [
        record
        for record in records(evidence)
        if record["kind"] == "seed" and record["session_id"] == session_id
    ]
    expect(len(seeds) == 1, "같은 세션에 확정 명세 파일 하나 생성")
    if not seeds:
        expect(False, "후속 두 사례 실행 불가: 확정 명세 파일이 생성되지 않음")
        return
    seed = seeds[0]
    seed_id = seed["evidence_id"]
    expect(
        {
            key: seed["content"].get(key)
            for key in ("goal", "constraints", "acceptance_criteria")
        }
        == {
            "goal": GOAL,
            "constraints": CONSTRAINTS,
            "acceptance_criteria": ACCEPTANCE,
        },
        "목표·제약·완료 기준이 파일에 그대로 보존됨",
    )
    expect(
        any(
            request(call)["name"] == "save_evidence"
            and body(call).get("success")
            and body(call)["evidence"]["source_evidence_id"] == seed_id
            for call in complete["calls"]
        ),
        "명세에 연결된 최종 요약 저장",
    )
    seed_path = evidence / session_id / f"{seed_id}.json"
    frozen_bytes = seed_path.read_bytes()
    event_count = len(
        [
            record
            for record in records(evidence)
            if record["kind"] == "event" and record["session_id"] == session_id
        ]
    )

    rejected = execute(
        "03_frozen_edit",
        (
            f"서버 계약 점검이야. 세션 {session_id}에 record_answer를 딱 한 번 호출해서 "
            "field=constraints, answer='제약 없음으로 변경'을 보내 줘. 확정 명세 변경을 "
            "서버가 거절하는지 실제 결과를 확인하려는 요청이야. 새 세션은 만들지 말고 "
            "거절되면 재시도하지 말고 점검 결과를 근거 파일로 남겨 줘."
        ),
    )
    expect(
        any(
            request(call)["name"] == "record_answer"
            and body(call).get("error") == "seed_frozen"
            for call in rejected["calls"]
        ),
        "확정 후 수정 요청을 서버가 실제 거절",
    )
    expect(seed_path.read_bytes() == frozen_bytes, "거절 후 명세 파일 원문 보존")
    expect(
        rejected["tool_calls"].count("record_answer") == 1,
        "거절된 수정을 반복 호출하지 않음",
    )
    expect(
        len(
            [
                record
                for record in records(evidence)
                if record["kind"] == "event" and record["session_id"] == session_id
            ]
        )
        == event_count,
        "거절 후 상태 변경 기록 수 유지",
    )

    handed = output / "handoff" / "evidence"
    shutil.copytree(evidence, handed)
    fresh_db = work / "handoff.sqlite3"
    expect(not fresh_db.exists(), "인계할 때 기존 DB와 대화 기록을 전달하지 않음")
    plan = execute(
        "04_files_only",
        (
            f"세션 {session_id}의 작업을 이어가 줘. 근거 폴더만 전달받았으니 파일에서 "
            "현재 세션 상태와 확정 명세를 확인한 뒤 구현 계획을 작성해 줘. 계획 전체를 "
            "근거 파일로 저장하고 원본 명세의 근거 ID를 연결해 줘. 실제 구현이나 테스트는 실행하지 마."
        ),
        database=fresh_db,
        root=handed,
    )
    read_indexes = [
        index
        for index, call in enumerate(plan["calls"])
        if request(call)["name"] == "read_evidence"
        and body(call).get("success")
        and request(call)["arguments"].get("evidence_id") == seed_id
    ]
    saves = [
        (index, body(call)["evidence"])
        for index, call in enumerate(plan["calls"])
        if request(call)["name"] == "save_evidence" and body(call).get("success")
    ]
    expect(bool(read_indexes), "새 프로세스가 전달받은 명세 파일 본문을 읽음")
    expect(
        any(
            read_index < save_index
            and artifact["source_evidence_id"] == seed_id
            and len(artifact["content"]) > 100
            for read_index in read_indexes
            for save_index, artifact in saves
        ),
        "파일을 읽은 뒤 원본 ID를 연결한 계획 본문 저장",
    )
    expect(
        any(
            request(call)["name"] == "get_session"
            and body(call).get("status") == "frozen"
            for call in plan["calls"]
        ),
        "근거 파일로 세션 상태 복원",
    )
    if fresh_db.exists():
        with closing(sqlite3.connect(fresh_db)) as connection:
            count = connection.execute(
                "SELECT count(*) FROM events WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
        expect(count == event_count, "새 DB에 파일의 변경 기록 복원")
    else:
        expect(False, "새 DB에 파일의 변경 기록 복원")
    expect(
        (handed / session_id / f"{seed_id}.json").read_bytes() == frozen_bytes,
        "후속 계획이 원본 명세를 덮어쓰지 않음",
    )
    server_pids = [pid for case in cases for pid in case["server_pids"]]
    expect(
        MCP_TOOLS <= {tool for case in cases for tool in case["tool_calls"]},
        "일곱 MCP 도구를 실제로 사용",
    )
    expect(
        len(set(server_pids)) == len(cases),
        "네 사례에서 서로 다른 MCP 서버 프로세스 실행",
    )
    expect(
        len({case["agent_pid"] for case in cases}) == len(cases),
        "네 사례에서 서로 다른 Deep Agents 프로세스 실행",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=LAB / "reference" / "server.py")
    parser.add_argument("--skills-dir", type=Path, default=LAB / "skills")
    parser.add_argument("--output-dir", type=Path, help="검사 근거를 저장할 새 폴더")
    args = parser.parse_args()
    args.server, args.skills_dir = args.server.resolve(), args.skills_dir.resolve()
    if not args.server.is_file() or not args.skills_dir.is_dir():
        parser.error("서버 파일과 Skill 폴더를 확인하세요.")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    output = (args.output_dir or DAY04 / "evidence" / "validation" / run_id).resolve()
    if output.exists():
        parser.error(
            "검사 기록은 덮어쓰지 않습니다. 새 --output-dir 경로를 사용하세요."
        )
    output.mkdir(parents=True)
    checks, cases = [], []
    print(f"[검사 근거] {output}", flush=True)
    print("[비용] 실제 모델 호출 약 20~35회, 2~5분 예상입니다. 실행합니다.", flush=True)
    try:
        verify(args, output, checks, cases)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - 실패도 보고서에 보존하고 종료 코드 1을 반환한다.
        checks.append(
            {
                "name": "검사 실행 중 오류",
                "passed": False,
                "error_type": type(error).__name__,
            }
        )
    report = {
        "passed": bool(checks) and all(check["passed"] for check in checks),
        "cases": [
            {key: value for key, value in case.items() if key not in {"trace", "calls"}}
            for case in cases
        ],
        "checks": checks,
    }
    report_path = output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[{'전체 통과' if report['passed'] else '실패 있음'}] {report_path}",
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
