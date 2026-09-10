"""새 Skill의 실제 Python 프로세스와 execute 연결을 검사한다. 모델/API 호출 없음."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

LAB = Path(__file__).resolve().parent
SKILL = LAB / "skills" / "requirements-interview-new"


class ScriptContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="skill-scripts-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / "sessions.sqlite3"
        self.evidence = self.root / "evidence"

    def call(self, operation, arguments=None, *, code=0, raw=None, skill=SKILL):
        command = [
            sys.executable,
            "-S",
            str(skill / "scripts" / "run.py"),
            "--db",
            str(self.db),
            "--evidence-dir",
            str(self.evidence),
            operation,
        ]
        completed = subprocess.run(
            command,
            input=raw
            if raw is not None
            else json.dumps(arguments or {}, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            cwd=self.root,
            check=False,
        )
        self.assertEqual(
            completed.returncode, code, completed.stderr + completed.stdout
        )
        self.assertEqual(completed.stderr, "")
        return json.loads(completed.stdout)

    def start(self, goal="CSV의 주간 합계를 보고서로 저장"):
        return self.call("start_interview", {"goal": goal})

    def answer(self, sid, field, answer):
        return self.call(
            "record_answer", {"session_id": sid, "field": field, "answer": answer}
        )

    def ready(self):
        sid = self.start()["session_id"]
        self.answer(sid, "constraints", "원문 수정 금지")
        return self.answer(sid, "acceptance_criteria", "보고서 합계와 원문 합계가 일치")

    def records(self, root=None):
        return [
            json.loads(path.read_text())
            for path in (root or self.evidence).glob("*/*.json")
        ]

    def test_seven_operations_and_exact_text(self):
        goal = '따옴표 "원문"과 줄바꿈\n$(문자열) `문자열` 보존'
        started = self.start(goal)
        sid = started["session_id"]
        self.assertEqual(started["goal"], goal)
        self.assertEqual(started["status"], "draft")
        constraints = self.answer(sid, "constraints", "외부 전송 금지")
        self.assertEqual(constraints["next_question"]["field"], "acceptance_criteria")
        ready = self.answer(sid, "acceptance_criteria", "샘플 합계와 일치")
        self.assertEqual(ready["status"], "ready")
        frozen = self.call("freeze_seed", {"session_id": sid})
        self.assertEqual(frozen["status"], "frozen")
        seed_id = frozen["seed_evidence_id"]
        original = self.call(
            "read_evidence", {"session_id": sid, "evidence_id": seed_id}
        )
        self.assertEqual(original["evidence"]["content"], frozen["seed"])
        source_bytes = Path(original["path"]).read_bytes()
        saved = self.call(
            "save_evidence",
            {
                "session_id": sid,
                "title": "후속 계획",
                "content": goal,
                "source_evidence_id": seed_id,
            },
        )
        self.assertEqual(saved["evidence"]["content"], goal)
        self.assertEqual(saved["evidence"]["source_evidence_id"], seed_id)
        listing = self.call("list_evidence", {"session_id": sid})
        self.assertIn(
            saved["evidence"]["evidence_id"],
            [item["evidence_id"] for item in listing["evidence"]],
        )
        self.assertEqual(self.call("get_session", {"session_id": sid}), frozen)
        self.assertEqual(Path(original["path"]).read_bytes(), source_bytes)
        self.assertEqual(
            self.call("list_evidence")["sessions"], [{"session_id": sid, "goal": goal}]
        )
        calls = [record for record in self.records() if record["kind"] == "script_call"]
        self.assertEqual(len(calls), 9)
        start_call = next(
            item
            for item in calls
            if item["content"]["request"]["name"] == "start_interview"
        )
        self.assertEqual(start_call["session_id"], sid)
        self.assertEqual(start_call["content"]["result"]["session_id"], sid)

    def test_bad_inputs_are_recorded_without_state_changes(self):
        state = self.start()
        sid = state["session_id"]
        cases = [
            ("start_interview", "{}"),
            ("start_interview", '{"goal":42}'),
            ("start_interview", '{"goal":"x","extra":"x"}'),
            ("start_interview", "[]"),
            ("start_interview", "not JSON"),
            ("unknown", "{}"),
            ("list_evidence", '{"session_id":null}'),
            (
                "record_answer",
                json.dumps({"session_id": sid, "field": "status", "answer": "frozen"}),
            ),
        ]
        for operation, raw in cases:
            with self.subTest(operation=operation, raw=raw):
                result = self.call(operation, raw=raw, code=2)
                self.assertEqual(result["error"], "invalid_arguments")
        self.assertEqual(self.call("get_session", {"session_id": sid}), state)
        failures = [
            r
            for r in self.records()
            if r["kind"] == "script_call" and r["content"]["exit_code"] == 2
        ]
        self.assertEqual(len(failures), len(cases))
        self.assertTrue(any(r["session_id"] == "_unassigned" for r in failures))

    def test_refusal_and_retries_preserve_state(self):
        state = self.start()
        sid = state["session_id"]
        early = self.call("freeze_seed", {"session_id": sid}, code=1)
        self.assertEqual(early["error"], "seed_incomplete")
        self.assertEqual(self.call("get_session", {"session_id": sid}), state)
        answered = self.answer(sid, "constraints", "원문 유지")
        self.assertEqual(self.answer(sid, "constraints", "원문 유지"), answered)
        self.answer(sid, "acceptance_criteria", "합계 비교")
        frozen = self.call("freeze_seed", {"session_id": sid})
        self.assertEqual(self.call("freeze_seed", {"session_id": sid}), frozen)
        refused = self.call(
            "record_answer",
            {"session_id": sid, "field": "constraints", "answer": "변경"},
            code=1,
        )
        self.assertEqual(refused["error"], "seed_frozen")
        self.assertEqual(self.call("get_session", {"session_id": sid}), frozen)
        artifact_args = {"session_id": sid, "title": "메모", "content": "같은 본문"}
        artifact = self.call("save_evidence", artifact_args)
        self.assertEqual(self.call("save_evidence", artifact_args), artifact)
        changed = self.call("save_evidence", {**artifact_args, "content": "수정 본문"})
        self.assertNotEqual(
            changed["evidence"]["evidence_id"], artifact["evidence"]["evidence_id"]
        )

    def test_copy_skill_and_handoff_without_original_database(self):
        ready = self.ready()
        sid = ready["session_id"]
        frozen = self.call("freeze_seed", {"session_id": sid})
        other = self.start("다른 업무")["session_id"]
        self.answer(other, "constraints", "사내 조회만")
        handed = self.root / "handoff"
        shutil.copytree(self.evidence, handed)
        self.evidence, self.db = handed, self.root / "fresh.sqlite3"
        copied_skill = self.root / "copied-skill"
        shutil.copytree(SKILL, copied_skill)
        loaded = self.call(
            "read_evidence",
            {"session_id": sid, "evidence_id": frozen["seed_evidence_id"]},
            skill=copied_skill,
        )
        self.assertEqual(loaded["evidence"]["content"], frozen["seed"])
        restored = self.call("get_session", {"session_id": sid}, skill=copied_skill)
        self.assertEqual(restored["seed"], frozen["seed"])
        self.assertEqual(restored["event_count"], frozen["event_count"])
        continued = self.answer(other, "acceptance_criteria", "사내 계정 조회 성공")
        self.assertEqual(continued["status"], "ready")
        self.assertEqual(
            self.call("get_session", {"session_id": sid})["seed"], frozen["seed"]
        )

    def test_publish_failure_then_recovery(self):
        state = self.ready()
        sid = state["session_id"]
        blocked = self.evidence / sid / f"event-{state['event_count'] + 1:08d}.json"
        blocked.mkdir()
        failed = self.call("freeze_seed", {"session_id": sid}, code=1)
        self.assertEqual(failed["error"], "evidence_publish_failed")
        self.assertIs(failed["state_saved"], True)
        blocked.rmdir()
        restored = self.call("get_session", {"session_id": sid})
        self.assertEqual(restored["status"], "frozen")
        self.assertTrue(
            (self.evidence / sid / f"{restored['seed_evidence_id']}.json").is_file()
        )

    def test_path_and_source_validation(self):
        sid = self.start()["session_id"]
        traversed = self.call(
            "read_evidence", {"session_id": sid, "evidence_id": "../../outside"}, code=1
        )
        self.assertEqual(traversed["error"], "invalid_evidence_id")
        missing = self.call(
            "save_evidence",
            {
                "session_id": sid,
                "title": "메모",
                "content": "본문",
                "source_evidence_id": "artifact-" + "0" * 32,
            },
            code=1,
        )
        self.assertEqual(missing["error"], "evidence_not_found")
        self.assertFalse(any(r["kind"] == "artifact" for r in self.records()))

    def test_call_record_failure_does_not_report_success(self):
        self.evidence.mkdir()
        # 세션에 배정되지 않은 호출의 기록 위치를 파일로 막는다.
        (self.evidence / "_unassigned").write_text("blocked")
        failed = self.call("list_evidence", code=1)
        self.assertEqual(failed["error"], "call_record_failed")
        self.assertIs(failed["success"], False)
        self.assertIs(failed["operation_result"]["success"], True)

    def test_corrupt_evidence_is_rejected_on_restore(self):
        sid = self.ready()["session_id"]
        (self.evidence / sid / "event-00000002.json").write_text("broken JSON")
        self.db = self.root / "new.sqlite3"
        failed = self.call("get_session", {"session_id": sid}, code=1)
        self.assertEqual(failed["error"], "evidence_corrupt")


class RuntimeTests(unittest.TestCase):
    def test_mcp_mode_keeps_original_skill_and_tools(self):
        import agent
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.messages import AIMessage

        class ScriptedModel(FakeMessagesListChatModel):
            model_name: str = "mcp-runtime-check"

            def _get_ls_params(self, stop=None, **kwargs):
                return {"ls_provider": "openrouter", "ls_model_name": self.model_name}

            def bind_tools(self, tools, **kwargs):
                return self

        model = ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/requirements-interview/SKILL.md"},
                            "id": "read",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "start_interview",
                            "args": {"goal": "MCP 비교 확인"},
                            "id": "start",
                        }
                    ],
                ),
                AIMessage(content="확인"),
            ]
        )
        with TemporaryDirectory(prefix="mcp-runtime-check-") as temporary:
            root = Path(temporary)
            args = Namespace(
                mode="mcp",
                skill_name="requirements-interview",
                skills_dir=SKILL.parent,
                db=root / "sessions.sqlite3",
                evidence_dir=root / "evidence",
                server=LAB / "reference" / "server.py",
                no_skills=False,
                prompt="새 업무 시작",
            )
            events = []
            with (
                patch.object(agent, "chat_model", return_value=model),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                asyncio.run(
                    agent.run(
                        args, lambda kind, **data: events.append({"kind": kind, **data})
                    )
                )
            connected = next(row for row in events if row["kind"] == "connected")
            self.assertEqual(len(connected["mcp_tools"]), 7)
            discovered = next(
                row for row in events if row["kind"] == "skills_discovered"
            )
            self.assertEqual(discovered["names"], ["requirements-interview"])
            calls = list((root / "evidence").glob("*/call-*.json"))
            self.assertEqual(len(calls), 1)
            record = json.loads(calls[0].read_text())
            self.assertEqual(record["kind"], "tool_call")
            self.assertEqual(
                record["content"]["result"]["structuredContent"]["goal"],
                "MCP 비교 확인",
            )

    def test_agent_exposes_only_execute_and_reads_instructions(self):
        import agent
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.messages import AIMessage
        from pydantic import Field

        class ScriptedModel(FakeMessagesListChatModel):
            model_name: str = "script-runtime-check"
            bindings: list[list[str]] = Field(default_factory=list)

            def _get_ls_params(self, stop=None, **kwargs):
                return {"ls_provider": "openrouter", "ls_model_name": self.model_name}

            def bind_tools(self, tools, **kwargs):
                self.bindings.append([tool.name for tool in tools])
                return self

        def tool_call(name, arguments, number):
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": arguments, "id": f"call-{number}"}],
            )

        command = """python "$SKILL_ROOT/scripts/run.py" --db "$SKILL_DB" --evidence-dir "$SKILL_EVIDENCE_DIR" start_interview <<'JSON'
{"goal":"실행기 연결 검증"}
JSON"""
        model = ScriptedModel(
            responses=[
                tool_call("read_file", {"file_path": str(SKILL / "SKILL.md")}, 1),
                tool_call("execute", {"command": 'cat "$SKILL_ROOT/SKILL.md"'}, 2),
                tool_call("execute", {"command": command}, 3),
                tool_call(
                    "execute",
                    {
                        "command": "python -c 'import os; assert \"OPENROUTER_API_KEY\" not in os.environ'"
                    },
                    4,
                ),
                AIMessage(content="실행 결과 확인"),
            ]
        )
        with TemporaryDirectory(prefix="skill-runtime-check-") as temporary:
            root = Path(temporary)
            args = Namespace(
                mode="scripts",
                skill_name=SKILL.name,
                skills_dir=SKILL.parent,
                db=root / "sessions.sqlite3",
                evidence_dir=root / "evidence",
                trace_path=root / "run.jsonl",
                no_skills=False,
                prompt="새 업무 시작",
            )
            events = []
            # mcp를 가져오려 하면 실패한다. 실제 모델 호출은 고정 응답으로 대체한다.
            with (
                patch.dict(sys.modules, {"mcp": None}),
                patch.object(agent, "chat_model", return_value=model),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                asyncio.run(
                    agent.run(
                        args,
                        lambda kind, **data: events.append({"kind": kind, **data}),
                    )
                )
            self.assertTrue(model.bindings)
            self.assertTrue(
                all(names == ["execute"] for names in model.bindings), model.bindings
            )
            discovered = [row for row in events if row["kind"] == "skills_discovered"]
            self.assertEqual(discovered[0]["names"], [SKILL.name])
            executions = [row for row in events if row["kind"] == "execution"]
            self.assertEqual(len(executions), 3)
            self.assertTrue(
                all(row["exit_code"] == 0 for row in executions), executions
            )
            messages = [row for row in events if row["kind"] == "message"]
            self.assertTrue(
                any(
                    row.get("name") == "read_file" and row.get("status") == "error"
                    for row in messages
                )
            )
            self.assertFalse(any(row["kind"] == "connected" for row in events))
            calls = list((root / "evidence").glob("*/call-*.json"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0].read_text())["kind"], "script_call")


if __name__ == "__main__":
    unittest.main(verbosity=2)
