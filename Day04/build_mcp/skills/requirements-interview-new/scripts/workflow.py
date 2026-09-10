"""질문·답변 기록에서 현재 상태를 다시 만든다.

reference/workflow.py의 수업 비교용 사본이다. MCP나 모델에 의존하지 않는다.
수강생은 네 공개 메서드와
replay부터 읽는다. SQLite 연결과 저장 코드는 상태를 남기는 데 사용한다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from evidence import EvidenceError, EvidenceStore

QUESTIONS = {
    "constraints": "반드시 지켜야 할 제약은 무엇인가요? 없다면 '없음'이라고 답하세요.",
    "acceptance_criteria": "어떤 결과를 확인하면 작업이 끝났다고 판단할 수 있나요?",
}


class WorkflowError(Exception):
    """사용자가 입력이나 호출 순서를 바꾸어 해결할 수 있는 업무 오류."""

    def __init__(self, code: str, message: str, **details: object):
        super().__init__(message)
        self.result = {"success": False, "error": code, "message": message, **details}


def replay(session_id: str, events: list[tuple[str, str]]) -> dict:
    """저장 순서대로 변경 기록을 읽어 답변과 확정 명세를 복원한다."""
    if not events:
        raise WorkflowError("session_not_found", "세션 ID를 확인하세요.")
    goal, answers, seed = "", {}, None
    for kind, raw_payload in events:
        payload = json.loads(raw_payload)
        if kind == "interview_started":
            if (
                goal
                or not isinstance(payload.get("goal"), str)
                or not payload["goal"].strip()
            ):
                raise EvidenceError(
                    "evidence_corrupt", "시작 기록의 순서와 목표를 확인하세요."
                )
            goal = payload["goal"]
        elif kind == "answer_recorded":
            if (
                not goal
                or seed is not None
                or payload.get("field") not in QUESTIONS
                or not isinstance(payload.get("answer"), str)
                or not payload["answer"].strip()
            ):
                raise EvidenceError(
                    "evidence_corrupt", "답변 기록의 순서와 내용을 확인하세요."
                )
            answers[payload["field"]] = payload["answer"]
        elif kind == "seed_frozen":
            expected = {"goal": goal, **answers}
            if (
                not goal
                or seed is not None
                or set(answers) != set(QUESTIONS)
                or not isinstance(payload.get("seed_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", payload["seed_id"])
                or any(payload.get(key) != value for key, value in expected.items())
            ):
                raise EvidenceError(
                    "evidence_corrupt", "명세와 원래 답변이 일치하지 않습니다."
                )
            seed = payload
        else:
            raise ValueError(f"알 수 없는 저장 이벤트: {kind}")

    missing = [field for field in QUESTIONS if field not in answers]
    status = "frozen" if seed is not None else "draft" if missing else "ready"
    question = {"field": missing[0], "text": QUESTIONS[missing[0]]} if missing else None
    return {
        "success": True,
        "session_id": session_id,
        "status": status,
        "goal": goal,
        "answers": answers,
        "missing_fields": missing,
        "next_question": question,
        "seed": seed,
        "event_count": len(events),
    }


class Workflow:
    def __init__(self, db_path: Path, evidence: EvidenceStore):
        self.db_path = db_path
        self.evidence = evidence
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                )"""
            )

    @contextmanager
    def _transaction(self):
        # 상태 검사와 기록 추가를 한 트랜잭션으로 묶는다.
        # 확정 요청이 동시에 와도 한 세션에 명세 두 개를 만들지 않는다.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _append(db: sqlite3.Connection, session_id: str, kind: str, payload: dict):
        db.execute(
            "INSERT INTO events(session_id, kind, payload) VALUES (?, ?, ?)",
            (session_id, kind, json.dumps(payload, ensure_ascii=False)),
        )

    @staticmethod
    def _events(db: sqlite3.Connection, session_id: str) -> list[tuple[str, str]]:
        return db.execute(
            "SELECT kind, payload FROM events WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()

    def _load(self, db: sqlite3.Connection, session_id: str) -> dict:
        events = self._events(db, session_id)
        files = self.evidence.load_events(session_id)
        # DB가 없거나 과거 사본이면 파일의 나머지 기록을 가져온다.
        # 공통 부분이 다르면 어느 쪽도 덮어쓰지 않고 충돌을 알린다.
        for (kind, payload), (file_kind, file_payload) in zip(events, files):
            if kind != file_kind or json.loads(payload) != json.loads(file_payload):
                raise EvidenceError(
                    "evidence_conflict", "DB와 근거 파일의 기록이 다릅니다."
                )
        if len(files) > len(events):
            replay(session_id, files)  # 손상된 기록은 DB에 가져오지 않는다.
            for kind, payload in files[len(events) :]:
                self._append(db, session_id, kind, json.loads(payload))
            events = files
        return replay(session_id, events)

    def _publish(self, session_id: str) -> dict:
        # DB 커밋 뒤 파일을 저장한다. 실패하면 get_session으로 다시 내보낼 수 있다.
        with closing(sqlite3.connect(self.db_path)) as db:
            events = self._events(db, session_id)
        state = replay(session_id, events)
        try:
            self.evidence.publish_events(session_id, events, QUESTIONS)
        except (OSError, EvidenceError) as error:
            raise WorkflowError(
                "evidence_publish_failed",
                f"DB는 저장됐지만 근거 파일 저장에 실패했습니다: {error}. 저장 위치를 확인하고 get_session을 호출하세요.",
                session_id=session_id,
                state_saved=True,
            ) from error
        state["evidence_dir"] = str(self.evidence.root / session_id)
        state["seed_evidence_id"] = (
            "seed-" + state["seed"]["seed_id"] if state["seed"] else None
        )
        return state

    def start_interview(self, goal: str) -> dict:
        """목표를 저장하고 세션 ID와 첫 질문을 반환한다."""
        if not goal.strip():
            raise WorkflowError("empty_goal", "만들려는 결과를 한 문장으로 입력하세요.")
        session_id = uuid4().hex
        with self._transaction() as db:
            self._append(db, session_id, "interview_started", {"goal": goal.strip()})
        return self._publish(session_id)

    def record_answer(self, session_id: str, field: str, answer: str) -> dict:
        """확정 전 답변을 기록한다. 수정 전 답변도 저장 기록에 남는다."""
        if field not in QUESTIONS:
            raise WorkflowError(
                "unknown_field", "응답의 next_question.field를 확인하세요."
            )
        if not answer.strip():
            raise WorkflowError("empty_answer", "빈 답변은 기록할 수 없습니다.")
        with self._transaction() as db:
            state = self._load(db, session_id)
            if state["status"] == "frozen":
                raise WorkflowError(
                    "seed_frozen",
                    "확정된 명세는 바꿀 수 없습니다. 새 세션을 시작하세요.",
                )
            # 같은 답변의 재전송은 기록을 중복해서 늘리지 않는다.
            if state["answers"].get(field) != answer.strip():
                self._append(
                    db,
                    session_id,
                    "answer_recorded",
                    {"field": field, "answer": answer.strip()},
                )
        return self._publish(session_id)

    def get_session(self, session_id: str) -> dict:
        """세션을 조회하고 근거 파일을 복구한다. DB가 없으면 파일에서 가져온다."""
        with self._transaction() as db:
            self._load(db, session_id)
        return self._publish(session_id)

    def freeze_seed(self, session_id: str) -> dict:
        """필수 답변이 있으면 명세를 확정한다. 재호출은 같은 명세를 반환한다."""
        with self._transaction() as db:
            state = self._load(db, session_id)
            if state["missing_fields"]:
                raise WorkflowError(
                    "seed_incomplete",
                    "필수 질문에 답한 뒤 명세를 확정하세요.",
                    missing_fields=state["missing_fields"],
                )
            if state["status"] != "frozen":
                seed = {
                    "seed_id": uuid4().hex,
                    "goal": state["goal"],
                    "constraints": state["answers"]["constraints"],
                    "acceptance_criteria": state["answers"]["acceptance_criteria"],
                }
                self._append(db, session_id, "seed_frozen", seed)
        return self._publish(session_id)
