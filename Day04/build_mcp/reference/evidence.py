"""MCP가 받은 입력·반환한 결과·후속 산출물을 독립된 JSON 파일로 보관한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4


class EvidenceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.result = {"success": False, "error": code, "message": message}


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _folder(self, session_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}|_unassigned", session_id):
            raise EvidenceError("session_not_found", "세션 ID를 확인하세요.")
        folder = self.root / session_id
        if folder.is_symlink() or folder.resolve().parent != self.root:
            raise EvidenceError("invalid_path", "근거 폴더 밖으로 접근할 수 없습니다.")
        return folder

    def _path(self, session_id: str, evidence_id: str) -> Path:
        if not re.fullmatch(
            r"(?:event-\d{8}|(?:seed|call|artifact)-[0-9a-f]{32})", evidence_id
        ):
            raise EvidenceError(
                "invalid_evidence_id", "목록에 있는 근거 ID를 사용하세요."
            )
        folder = self._folder(session_id)
        path = folder / f"{evidence_id}.json"
        if path.is_symlink() or path.resolve().parent != folder:
            raise EvidenceError("invalid_path", "근거 폴더 밖으로 접근할 수 없습니다.")
        return path

    def has_session(self, session_id: object) -> bool:
        if not isinstance(session_id, str):
            return False
        try:
            return (self._folder(session_id) / "event-00000001.json").is_file()
        except EvidenceError:
            return False

    def put(
        self,
        session_id: str,
        evidence_id: str,
        kind: str,
        title: str,
        content: object,
        source_evidence_id: str = "",
    ) -> dict:
        """완성된 파일만 공개한다. 같은 ID의 기존 내용은 덮어쓰지 않는다."""
        path = self._path(session_id, evidence_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "evidence_id": evidence_id,
            "session_id": session_id,
            "kind": kind,
            "title": title,
            "source_evidence_id": source_evidence_id,
            # 과거 이벤트의 발생 시간이 아니라 파일로 내보낸 시간이다.
            "exported_at": datetime.now(UTC).isoformat(),
            "content": content,
        }
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".pending-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            try:
                json.dump(record, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        try:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                existing = self.read(session_id, evidence_id)["evidence"]
                if any(
                    existing.get(key) != value
                    for key, value in record.items()
                    if key != "exported_at"
                ):
                    raise EvidenceError(
                        "evidence_conflict",
                        f"기존 근거와 내용이 다릅니다: {evidence_id}",
                    )
        finally:
            temporary_path.unlink(missing_ok=True)
        return self.read(session_id, evidence_id)

    def read(self, session_id: str, evidence_id: str) -> dict:
        path = self._path(session_id, evidence_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EvidenceError(
                "evidence_not_found", "근거 ID와 세션 ID를 확인하세요."
            ) from None
        except (ValueError, UnicodeError):
            raise EvidenceError(
                "evidence_corrupt", f"읽을 수 없는 근거 파일: {evidence_id}"
            ) from None
        if (
            not isinstance(record, dict)
            or record.get("evidence_id") != evidence_id
            or record.get("session_id") != session_id
            or not {"kind", "title", "exported_at", "source_evidence_id", "content"}
            <= record.keys()
        ):
            raise EvidenceError(
                "evidence_corrupt", f"근거 파일의 형식이 다릅니다: {evidence_id}"
            )
        return {
            "success": True,
            "session_id": session_id,
            "path": str(path),
            "evidence": record,
        }

    def _goal(self, session_id: str) -> str | None:
        """목록에서 작업을 구분할 수 있도록 시작 기록의 목표만 읽는다."""
        try:
            content = self.read(session_id, "event-00000001")["evidence"]["content"]
        except EvidenceError:
            # 시작 기록이 없거나 손상된 세션도 목록에서 빠뜨리지 않는다.
            return None
        payload = content.get("payload") if isinstance(content, dict) else None
        goal = payload.get("goal") if isinstance(payload, dict) else None
        return goal if isinstance(goal, str) else None

    def list(self, session_id: str = "") -> dict:
        """세션 ID를 생략하면 세션 목록을 목표와 함께 반환한다. DB는 읽지 않는다."""
        if not session_id:
            sessions = [
                {"session_id": name, "goal": self._goal(name)}
                for name in sorted(
                    path.name
                    for path in self.root.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and re.fullmatch(r"[0-9a-f]{32}|_unassigned", path.name)
                )
            ]
            return {
                "success": True,
                "evidence_dir": str(self.root),
                "sessions": sessions,
            }
        folder = self._folder(session_id)
        if not folder.is_dir():
            raise EvidenceError("session_not_found", "세션 ID를 확인하세요.")
        records = []
        for path in sorted(folder.glob("*.json")):
            loaded = self.read(session_id, path.stem)
            records.append(
                {
                    **{
                        key: value
                        for key, value in loaded["evidence"].items()
                        if key != "content"
                    },
                    "path": loaded["path"],
                }
            )
        return {"success": True, "session_id": session_id, "evidence": records}

    def save(
        self, session_id: str, title: str, content: str, source_evidence_id: str = ""
    ) -> dict:
        if not self.has_session(session_id):
            raise EvidenceError(
                "session_not_found", "먼저 세션을 시작하거나 근거 폴더를 가져오세요."
            )
        if not title.strip() or not content.strip():
            raise EvidenceError(
                "empty_evidence", "제목과 산출물 본문을 함께 입력하세요."
            )
        if source_evidence_id:
            self.read(session_id, source_evidence_id)
        # 같은 산출물 재전송은 같은 ID. 수정본은 별도 파일로 남는다.
        identity = json.dumps([title, content, source_evidence_id], ensure_ascii=False)
        evidence_id = "artifact-" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        return self.put(
            session_id, evidence_id, "artifact", title, content, source_evidence_id
        )

    def publish_events(
        self, session_id: str, events: list[tuple[str, str]], questions: dict
    ) -> None:
        for number, (kind, raw_payload) in enumerate(events, start=1):
            payload = json.loads(raw_payload)
            content = {"sequence": number, "event": kind, "payload": payload}
            if kind == "interview_started":
                content["questions"] = questions
            elif kind == "answer_recorded":
                content["question"] = questions[payload["field"]]
            event_id = f"event-{number:08d}"
            self.put(session_id, event_id, "event", kind, content)
            if kind == "seed_frozen":
                self.put(
                    session_id,
                    "seed-" + payload["seed_id"],
                    "seed",
                    "확정 명세",
                    payload,
                    event_id,
                )

    def load_events(self, session_id: str) -> list[tuple[str, str]]:
        """파일만 전달받은 새 DB에서도 입력 순서대로 세션을 복원할 수 있다."""
        events = []
        paths = (
            path
            for path in self._folder(session_id).glob("event-*.json")
            if not path.is_dir()
        )
        for number, path in enumerate(sorted(paths), start=1):
            record = self.read(session_id, path.stem)["evidence"]
            content = record["content"]
            if (
                path.stem != f"event-{number:08d}"
                or record["kind"] != "event"
                or not isinstance(content, dict)
                or content.get("sequence") != number
                or not isinstance(content.get("payload"), dict)
                or content.get("event")
                not in {"interview_started", "answer_recorded", "seed_frozen"}
            ):
                raise EvidenceError(
                    "evidence_corrupt", "이벤트 순서 또는 형식을 확인하세요."
                )
            events.append(
                (content["event"], json.dumps(content["payload"], ensure_ascii=False))
            )
        return events

    def record_call(self, params: dict, result: object) -> None:
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        structured = (
            result.get("structuredContent") if isinstance(result, dict) else None
        )
        structured = structured if isinstance(structured, dict) else {}
        candidate = structured.get("session_id") or arguments.get("session_id")
        session_id = candidate if self.has_session(candidate) else "_unassigned"
        self.put(
            session_id,
            "call-" + uuid4().hex,
            "tool_call",
            str(params.get("name", "알 수 없는 도구")),
            {"server_pid": os.getpid(), "request": params, "result": result},
        )
