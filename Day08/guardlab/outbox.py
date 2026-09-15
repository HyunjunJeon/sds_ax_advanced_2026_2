"""
사용: Outbox(run_dir).send(...) / .entries()  /  두 번째 세션 평가: outbox.since_now()
포인트: 외부 메일 대신 outbox.jsonl 에 기록된다. 평가기는 이 파일로 "실제 전달" 을 판정한다. Agent 의 "전달했습니다" 는 보지 않는다.

주요 내용:
로컬 전송함. 외부 메일 대신 여기에 기록되므로 상태 변경은 관찰되지만 운영 영향은 없다.
"""


from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class Outbox:
    def __init__(self, root: Path, offset: int = 0):
        self.file = Path(root) / "outbox.jsonl"
        self.offset = offset  # 두 번째 세션 평가 때 앞 세션의 전달을 제외하기 위한 시작 위치

    def since_now(self) -> "Outbox":
        return Outbox(self.file.parent, offset=len(self.all_entries()))

    def send(self, *, project: str, recipient: str, subject: str, body: str,
             report_version: int, sender: str) -> dict:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project": project,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "report_version": report_version,
            "sender": sender,
        }
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def all_entries(self) -> list[dict]:
        if not self.file.exists():
            return []
        return [json.loads(line) for line in self.file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def entries(self) -> list[dict]:
        return self.all_entries()[self.offset:]
