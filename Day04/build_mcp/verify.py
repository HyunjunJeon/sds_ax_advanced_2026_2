"""실제 stdio MCP 호출로 단계별 동작을 확인한다. 모델/API 키는 사용하지 않는다."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from client import REFERENCE, server_parameters
from mcp import Client


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def call(client: Client, tool: str, **arguments) -> dict:
    result = await client.call_tool(tool, arguments)
    check(not result.is_error, f"{tool}: MCP 도구 실행 오류 {result.content}")
    check(
        isinstance(result.structured_content, dict), f"{tool}: 객체 응답이 필요합니다."
    )
    return result.structured_content


async def verify(server: Path, stage: int) -> None:
    # 수강생의 기존 세션을 건드리지 않고 별도 DB에서 실행한다.
    with TemporaryDirectory(prefix="mcp-workflow-check-") as folder:
        evidence_dir = Path(folder) / "evidence"
        params = server_parameters(
            server, Path(folder) / "sessions.sqlite3", evidence_dir
        )
        async with Client(params) as client:
            definitions = {
                tool.name: tool for tool in (await client.list_tools()).tools
            }
            required = {"start_interview", "get_session"}
            if stage >= 2:
                required.add("record_answer")
            if stage >= 3:
                required.add("freeze_seed")
            if stage >= 5:
                required.update({"list_evidence", "read_evidence", "save_evidence"})
            check(
                required <= definitions.keys(),
                f"필요한 도구가 없습니다: {required - definitions.keys()}",
            )
            schema = definitions["start_interview"].input_schema
            check(
                "goal" in schema.get("required", []), "goal은 필수 입력이어야 합니다."
            )
            malformed = await client.call_tool("start_interview", {})
            check(
                bool(malformed.is_error), "필수 인자가 없으면 MCP 도구 오류여야 합니다."
            )

            goal = "매주 CSV 매출 자료를 요약하는 프로그램"
            started = await call(client, "start_interview", goal=goal)
            session_id = started.get("session_id")
            check(
                started.get("success") is True and bool(session_id),
                "시작 성공과 세션 ID가 필요합니다.",
            )
            check(started["status"] == "draft", "시작 상태는 draft여야 합니다.")
            check(
                started["next_question"]["field"] == "constraints",
                "첫 질문은 제약을 확인합니다.",
            )
            state = await call(client, "get_session", session_id=session_id)
            check(state["goal"] == goal, "입력한 목표가 조회되어야 합니다.")
            other = await call(client, "start_interview", goal="비품 대여 현황 조회")
            other_id = other["session_id"]
            check(other_id != session_id, "서로 다른 작업은 다른 세션 ID가 필요합니다.")
            missing = await call(client, "get_session", session_id="not-a-real-session")
            check(
                missing.get("success") is False
                and missing.get("error") == "session_not_found",
                "없는 세션을 정상 세션처럼 반환하지 마세요.",
            )
            blank = await call(client, "start_interview", goal="  ")
            check(
                blank.get("error") == "empty_goal",
                "공백만 있는 목표는 거절해야 합니다.",
            )
            print("1단계 통과: 도구 발견, 입력 형식, 세션 시작·조회, 없는 세션")
            if stage == 1:
                return

            state = await call(
                client,
                "record_answer",
                session_id=session_id,
                field="constraints",
                answer="외부 전송 금지",
            )
            check(state["status"] == "draft", "제약만 답하면 아직 draft입니다.")
            check(
                state["next_question"]["field"] == "acceptance_criteria",
                "다음 질문은 완료 기준입니다.",
            )
            blank = await call(
                client,
                "record_answer",
                session_id=session_id,
                field="acceptance_criteria",
                answer=" ",
            )
            check(blank.get("error") == "empty_answer", "빈 답변은 기록하면 안 됩니다.")
            state = await call(client, "get_session", session_id=session_id)
            check(
                state["missing_fields"] == ["acceptance_criteria"],
                "실패한 답변으로 상태가 바뀌면 안 됩니다.",
            )
            invalid_field = await client.call_tool(
                "record_answer",
                {"session_id": session_id, "field": "status", "answer": "frozen"},
            )
            check(
                bool(invalid_field.is_error),
                "field의 허용값을 입력 형식으로 제한하세요.",
            )
            constraints = "인터넷 없이 실행하고 CSV 원문을 수정하지 않는다."
            state = await call(
                client,
                "record_answer",
                session_id=session_id,
                field="constraints",
                answer=constraints,
            )
            check(
                state["answers"]["constraints"] == constraints,
                "확정 전에는 답변을 수정할 수 있어야 합니다.",
            )
            print("2단계 통과: 답변 기록·수정, 다음 질문, 잘못된 입력의 상태 보존")
            if stage == 2:
                return

            early = await call(client, "freeze_seed", session_id=session_id)
            check(
                early.get("success") is False
                and early.get("error") == "seed_incomplete",
                "완료 기준 없이 확정하면 안 됩니다.",
            )
            acceptance = "샘플 CSV의 합계와 생성된 보고서의 합계가 일치한다."
            ready = await call(
                client,
                "record_answer",
                session_id=session_id,
                field="acceptance_criteria",
                answer=acceptance,
            )
            check(
                ready["status"] == "ready" and ready["seed"] is None,
                "답변이 모인 상태와 확정 상태를 구분하세요.",
            )
            frozen = await call(client, "freeze_seed", session_id=session_id)
            check(frozen["status"] == "frozen", "확정 성공 뒤에는 frozen이어야 합니다.")
            seed = frozen["seed"]
            check(bool(seed["seed_id"]), "확정 명세에 ID가 필요합니다.")
            check(
                (seed["goal"], seed["constraints"], seed["acceptance_criteria"])
                == (goal, constraints, acceptance),
                "명세는 이 세션에 기록한 내용을 사용해야 합니다.",
            )
            duplicate = await call(client, "freeze_seed", session_id=session_id)
            check(
                duplicate["seed"] == seed, "확정 재호출은 같은 명세를 반환해야 합니다."
            )
            edit = await call(
                client,
                "record_answer",
                session_id=session_id,
                field="constraints",
                answer="제약 삭제",
            )
            check(
                edit.get("success") is False and edit.get("error") == "seed_frozen",
                "확정 명세의 변경을 서버에서 거절하세요.",
            )
            unchanged = await call(client, "get_session", session_id=session_id)
            check(unchanged["seed"] == seed, "거절된 수정이 명세를 바꾸면 안 됩니다.")
            print("3단계 통과: 확정 조건, 확정 뒤 수정 거절, 같은 명세 반환")
            if stage == 3:
                return
            check(
                unchanged["event_count"] == frozen["event_count"],
                "중복 확정과 거절된 수정은 저장 기록을 늘리면 안 됩니다.",
            )
            partial = await call(
                client,
                "record_answer",
                session_id=other_id,
                field="constraints",
                answer="사내 사용자만 조회",
            )
            duplicate_answer = await call(
                client,
                "record_answer",
                session_id=other_id,
                field="constraints",
                answer="사내 사용자만 조회",
            )
            check(
                duplicate_answer == partial,
                "같은 답변을 다시 보내도 저장 기록은 늘리지 마세요.",
            )

        # 위 Client가 끝나면 stdio 서버 프로세스도 종료된다.
        # 같은 DB로 새 프로세스를 시작해 대화 기록 없이 이어간다.
        async with Client(params) as restarted:
            restored = await call(restarted, "get_session", session_id=session_id)
            check(
                restored == frozen,
                "서버 재시작 후 확정 명세와 기록 수가 복원되어야 합니다.",
            )
            partial = await call(restarted, "get_session", session_id=other_id)
            check(
                partial["status"] == "draft",
                "다른 세션의 미완료 상태도 복원해야 합니다.",
            )
            check(
                partial["answers"] == {"constraints": "사내 사용자만 조회"},
                "다른 세션의 답변을 섞지 마세요.",
            )
            check(
                partial["next_question"]["field"] == "acceptance_criteria",
                "재시작 뒤 남은 질문부터 이어가야 합니다.",
            )
            repeated = await call(restarted, "freeze_seed", session_id=session_id)
            check(repeated == frozen, "재시작 뒤에도 중복 확정은 같은 결과여야 합니다.")
        print("4단계 통과: 실제 서버 재시작, 미완료 세션 재개, 세션 간 분리")
        if stage >= 5:
            await verify_evidence(
                server, Path(folder), params, session_id, other_id, frozen
            )


async def verify_evidence(server, folder, params, session_id, other_id, frozen):
    evidence_dir = folder / "evidence"
    session_folder = evidence_dir / session_id
    files = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in session_folder.glob("*.json")
    ]
    events = sorted(
        (record for record in files if record["kind"] == "event"),
        key=lambda record: record["content"]["sequence"],
    )
    check(
        len(events) == frozen["event_count"], "상태 변경 기록을 모두 파일로 남기세요."
    )
    check(
        events[0]["content"]["payload"]["goal"] == frozen["goal"],
        "목표 원문이 필요합니다.",
    )
    check(bool(events[0]["content"]["questions"]), "질문도 답변과 함께 보관하세요.")
    answers = [
        record["content"]["payload"]["answer"]
        for record in events
        if record["content"]["event"] == "answer_recorded"
    ]
    check(
        "외부 전송 금지" in answers and frozen["answers"]["constraints"] in answers,
        "답변 수정 전·후를 모두 보관하세요.",
    )
    calls = [record["content"] for record in files if record["kind"] == "tool_call"]
    check(
        any(
            item["result"].get("structuredContent", {}).get("error")
            == "seed_incomplete"
            for item in calls
        ),
        "업무 실패도 근거 파일에 남겨야 합니다.",
    )
    check(
        any(
            item["request"]["name"] == "record_answer" and item["result"].get("isError")
            for item in calls
        ),
        "SDK 입력 검증 실패도 기록하세요.",
    )
    unassigned = [
        json.loads(path.read_text())
        for path in (evidence_dir / "_unassigned").glob("*.json")
    ]
    check(
        any(
            record["content"]["request"].get("arguments") == {}
            and record["content"]["result"].get("isError")
            for record in unassigned
        ),
        "세션을 만들기 전 실패한 호출도 보관하세요.",
    )
    seed_id = frozen["seed_evidence_id"]
    async with Client(params) as client:
        listed = await call(client, "list_evidence", session_id=session_id)
        check(
            seed_id in {item["evidence_id"] for item in listed["evidence"]},
            "명세 파일을 목록에서 찾을 수 있어야 합니다.",
        )
        loaded = await call(
            client, "read_evidence", session_id=session_id, evidence_id=seed_id
        )
        check(
            loaded["evidence"]["content"] == frozen["seed"],
            "명세 원문을 파일에서 읽어야 합니다.",
        )
        check(
            Path(loaded["path"]).is_file(), "반환한 경로에 실제 파일이 있어야 합니다."
        )
        original = Path(loaded["path"]).read_bytes()
        artifact_args = {
            "session_id": session_id,
            "title": "명세 기반 구현 계획",
            "content": "1. CSV를 읽는다.\n2. 합계를 계산한다.\n3. 원문 합계와 보고서를 비교한다.",
            "source_evidence_id": seed_id,
        }
        artifact = await call(client, "save_evidence", **artifact_args)
        artifact_id = artifact["evidence"]["evidence_id"]
        check(
            artifact["evidence"]["source_evidence_id"] == seed_id,
            "후속 작업의 원본 근거를 연결하세요.",
        )
        duplicate = await call(client, "save_evidence", **artifact_args)
        check(
            duplicate == artifact, "같은 산출물을 다시 보내면 같은 파일을 반환하세요."
        )
        revised = await call(
            client,
            "save_evidence",
            **{**artifact_args, "content": "추가: 빈 CSV도 확인한다."},
        )
        check(
            revised["evidence"]["evidence_id"] != artifact_id,
            "수정본은 별도 파일로 남기세요.",
        )
        check(
            Path(loaded["path"]).read_bytes() == original,
            "후속 작업이 확정 명세를 덮어쓰면 안 됩니다.",
        )
        crossed = await call(
            client, "read_evidence", session_id=other_id, evidence_id=artifact_id
        )
        check(
            crossed.get("error") == "evidence_not_found",
            "다른 세션의 근거를 섞지 마세요.",
        )
        traversed = await call(
            client, "read_evidence", session_id=session_id, evidence_id="../../outside"
        )
        check(
            traversed.get("error") == "invalid_evidence_id",
            "임의 경로로 파일을 읽으면 안 됩니다.",
        )
        invalid_source = await call(
            client,
            "save_evidence",
            **{**artifact_args, "source_evidence_id": "artifact-" + "0" * 32},
        )
        check(
            invalid_source.get("error") == "evidence_not_found",
            "없는 원본 근거를 연결하면 안 됩니다.",
        )
        blank = await call(client, "save_evidence", **{**artifact_args, "content": " "})
        check(blank.get("error") == "empty_evidence", "빈 산출물을 저장하지 마세요.")

    # DB를 복사하지 않고 근거 폴더만 전달한다.
    handed = folder / "handoff" / "evidence"
    shutil.copytree(evidence_dir, handed)
    fresh_params = server_parameters(server, folder / "handoff" / "new.sqlite3", handed)
    async with Client(fresh_params) as client:
        listed = (await call(client, "list_evidence"))["sessions"]
        check(
            all(isinstance(entry, dict) for entry in listed),
            "세션 목록의 각 항목은 session_id와 goal을 담은 객체여야 합니다.",
        )
        goals = {entry.get("session_id"): entry.get("goal") for entry in listed}
        check(
            {session_id, other_id} <= set(goals),
            "세션 ID를 몰라도 파일에서 찾아야 합니다.",
        )
        check(
            goals.get(session_id) == frozen["goal"],
            "목록만으로 작업을 구분하도록 세션마다 목표를 함께 반환해야 합니다.",
        )
        artifact = await call(
            client, "read_evidence", session_id=session_id, evidence_id=artifact_id
        )
        check(
            artifact["evidence"]["content"] == artifact_args["content"],
            "DB 없이 후속 산출물 본문을 읽어야 합니다.",
        )
        loaded = await call(
            client, "read_evidence", session_id=session_id, evidence_id=seed_id
        )
        check(
            loaded["evidence"]["content"] == frozen["seed"],
            "DB 없이 확정 명세를 읽어야 합니다.",
        )
        restored = await call(client, "get_session", session_id=session_id)
        check(
            restored["seed"] == frozen["seed"]
            and restored["event_count"] == frozen["event_count"],
            "근거 파일에서 동일한 명세와 변경 기록을 복원하세요.",
        )
        continued = await call(
            client,
            "record_answer",
            session_id=other_id,
            field="acceptance_criteria",
            answer="사내 계정의 조회 성공을 확인",
        )
        check(
            continued["status"] == "ready",
            "파일만 전달받아 미완료 세션을 이어가야 합니다.",
        )
        # 다음 파일 경로를 디렉터리로 막아 실제 저장 실패를 일으킨다.
        blocked = handed / other_id / f"event-{continued['event_count'] + 1:08d}.json"
        blocked.mkdir()
        failed = await call(client, "freeze_seed", session_id=other_id)
        check(
            failed.get("error") == "evidence_publish_failed"
            and failed.get("state_saved") is True,
            "파일 저장 실패와 이미 저장된 DB 상태를 명확하게 알리세요.",
        )
        blocked.rmdir()
        recovered = await call(client, "get_session", session_id=other_id)
        check(
            recovered["status"] == "frozen",
            "저장 위치 복구 뒤 근거 파일을 다시 내보내야 합니다.",
        )
        check(
            (handed / other_id / f"{recovered['seed_evidence_id']}.json").is_file(),
            "복구된 명세 파일이 필요합니다.",
        )

    # 손상된 파일을 새 DB로 가져올 때 정상 상태로 꾸미지 않는다.
    damaged = folder / "damaged"
    shutil.copytree(evidence_dir, damaged)
    (damaged / session_id / "event-00000002.json").write_text(
        "broken JSON", encoding="utf-8"
    )
    async with Client(
        server_parameters(server, folder / "damaged.sqlite3", damaged)
    ) as client:
        corrupt = await call(client, "get_session", session_id=session_id)
        check(
            corrupt.get("error") == "evidence_corrupt",
            "손상된 근거는 복원 실패로 보고하세요.",
        )
    print(
        "5단계 통과: 근거 파일·실패 기록·산출물 연결, 파일만 전달해 재개, 저장 실패 복구"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=REFERENCE)
    parser.add_argument("--stage", type=int, choices=(1, 2, 3, 4, 5), default=5)
    args = parser.parse_args()
    if not args.server.is_file():
        parser.error("서버 파일이 없습니다.")
    asyncio.run(asyncio.wait_for(verify(args.server, args.stage), timeout=45))
    print(
        "이 검사는 서버 동작을 확인합니다. Skill 선택과 질문의 품질은 별도로 확인하세요."
    )
