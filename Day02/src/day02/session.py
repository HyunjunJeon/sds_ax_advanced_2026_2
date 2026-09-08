"""SQLite stores only completed, validated turns; invocation state and evidence stay turn-local."""
import json
import re
import sqlite3


class Sessions:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS turns (id INTEGER PRIMARY KEY, session TEXT, scope TEXT, question TEXT, answer TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def validate_id(session):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
            raise ValueError("session은 영문/숫자/-/_ 1~64자여야 합니다.")

    def history(self, session, scope):
        self.validate_id(session)
        key = json.dumps(scope, sort_keys=True, ensure_ascii=False)
        with self.connect() as db:
            rows = db.execute("SELECT question,answer FROM turns WHERE session=? AND scope=? ORDER BY id DESC LIMIT 4",
                              (session, key)).fetchall()
        return [message for q, a in reversed(rows) for message in [
            {"role": "user", "content": q}, {"role": "assistant", "content": a},
        ]]

    def append(self, session, scope, question, answer):
        self.validate_id(session)
        with self.connect() as db:
            db.execute("INSERT INTO turns(session,scope,question,answer) VALUES(?,?,?,?)",
                       (session, json.dumps(scope, sort_keys=True, ensure_ascii=False), question, answer))
