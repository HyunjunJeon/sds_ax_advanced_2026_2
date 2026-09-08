"""SQLite 원장: 문서의 준비 상태와 URL 별칭, 버전, 멱등 적재 키를 보존한다.

가르치는 것:
- 외부 세계의 동일성 판정: URL 정규화(utm 제거·대소문자·정렬)로 별칭을 묶고
  내용 hash로 버전을 구별한다. "같은 문서다"의 정의를 코드로 내리는 작업이다.
- 프로세스를 넘는 상태: 메모리 dict가 아니라 SQLite 트랜잭션이 병렬 reserve에서
  소유자를 하나로 만들고, 재시작 후에도 실패 지점에서 재개된다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("공개 HTTP(S) URL만 허용합니다.")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(sorted(query)), "")
    )


class Registry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents(
              id TEXT PRIMARY KEY, url TEXT, hash TEXT, kind TEXT, state TEXT,
              uri TEXT, path TEXT, metadata TEXT, error TEXT, lease REAL DEFAULT 0,
              updated REAL, UNIQUE(url,hash));
            CREATE TABLE IF NOT EXISTS aliases(url TEXT, id TEXT, PRIMARY KEY(url,id));
            CREATE TABLE IF NOT EXISTS heads(url TEXT PRIMARY KEY, id TEXT, checked_at REAL);
            CREATE TABLE IF NOT EXISTS conversations(thread TEXT PRIMARY KEY, payload TEXT);
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def rows(self, kind=None, ready=True):
        with self.connect() as c:
            clauses, params = [], []
            if ready:
                clauses.append("state='ready'")
            if kind:
                clauses.append("kind=?")
                params.append(kind)
            rows = c.execute(
                "SELECT * FROM documents"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY updated",
                params,
            ).fetchall()
            return [dict(r) | {"metadata": json.loads(r["metadata"])} for r in rows]

    def get(self, doc_id):
        with self.connect() as c:
            r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            return dict(r) | {"metadata": json.loads(r["metadata"])} if r else None

    def reserve(self, url, text, metadata, path, kind="web"):
        h = digest(text)
        identity = digest(url + "\n" + h)[:24]
        with self.connect() as c:
            # 검사→예약 사이에 다른 쓰기 트랜잭션이 끼어들지 않게 먼저 쓰기 권한을 얻는다.
            # 같은 프로세스의 Python lock만으로는 여러 프로세스의 경합을 막을 수 없다.
            c.execute("BEGIN IMMEDIATE")
            # 같은 내용의 다른 URL은 별칭으로 기록한다. 내부 문서는 관리 ID를 보존한다.
            duplicate = (
                c.execute(
                    "SELECT * FROM documents WHERE hash=? AND kind=? ORDER BY updated DESC LIMIT 1",
                    (h, kind),
                ).fetchone()
                if kind == "web"
                else None
            )
            # 내용이 같으면 다른 URL도 같은 문서의 별칭으로 본다. URL이 같아도 내용이
            # 바뀌면 새 버전이다. URL의 수와 적재된 원문 버전의 수를 혼동하지 않는다.
            if duplicate:
                c.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (url, duplicate["id"]))
                if duplicate["state"] == "ready":
                    c.execute(
                        "INSERT OR REPLACE INTO heads VALUES (?,?,?)",
                        (url, duplicate["id"], time.time()),
                    )
                    return duplicate["id"], "reused"
                if duplicate["lease"] > time.time():
                    return duplicate["id"], "pending"
                c.execute(
                    "UPDATE documents SET lease=?, error=NULL WHERE id=?",
                    (time.time() + 300, duplicate["id"]),
                )
                return duplicate["id"], "resumed"
            r = c.execute("SELECT * FROM documents WHERE id=?", (identity,)).fetchone()
            if r and r["state"] == "ready":
                return identity, "reused"
            if r and r["lease"] > time.time():
                return identity, "pending"
            if r:
                c.execute(
                    "UPDATE documents SET lease=?, error=NULL WHERE id=?",
                    (time.time() + 300, identity),
                )
                return identity, "resumed"
            c.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    url,
                    h,
                    kind,
                    "validated",
                    None,
                    str(path),
                    json.dumps(metadata, ensure_ascii=False),
                    None,
                    time.time() + 300,
                    time.time(),
                ),
            )
            c.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (url, identity))
            return identity, "new"

    # 현재 실습의 갱신 단위는 doc_id다. lease를 되찾은 새 Worker와 이전 Worker를
    # 구별하는 fencing token은 아직 없다. 12번 과제에서 API/스키마를 함께 확장한다.
    def update(self, doc_id, state, uri=None, error=None):
        with self.connect() as c:
            c.execute(
                "UPDATE documents SET state=?, uri=COALESCE(?,uri), error=?, updated=?, lease=? WHERE id=?",
                (
                    state,
                    uri,
                    error,
                    time.time(),
                    0 if state in {"ready", "failed"} else time.time() + 300,
                    doc_id,
                ),
            )
            if state == "ready":
                row = c.execute("SELECT url,kind FROM documents WHERE id=?", (doc_id,)).fetchone()
                if row["kind"] == "web":
                    c.execute(
                        "INSERT OR REPLACE INTO heads VALUES (?,?,?)",
                        (row["url"], doc_id, time.time()),
                    )

    def active_web(self):
        with self.connect() as c:
            rows = c.execute(
                "SELECT d.*,MAX(h.checked_at) checked_at FROM documents d JOIN heads h ON d.id=h.id WHERE d.state='ready' GROUP BY d.id"
            ).fetchall()
            return [dict(r) | {"metadata": json.loads(r["metadata"])} for r in rows]

    def snapshot(self):
        with self.connect() as c:
            heads = [tuple(r) for r in c.execute("SELECT url,id FROM heads ORDER BY url")]
        return digest(
            json.dumps(
                {
                    "documents": sorted((r["url"], r["hash"], r["kind"]) for r in self.rows()),
                    "active_web": heads,
                },
                ensure_ascii=False,
            )
        )

    def history(self, thread):
        with self.connect() as c:
            r = c.execute("SELECT payload FROM conversations WHERE thread=?", (thread,)).fetchone()
            return json.loads(r[0]) if r else {"owner": "general", "turns": []}

    def seed_from(self, other):
        """문서 snapshot만 복사한다. 턴 상태는 복사하지 않고 원문은 읽기 전용으로 참조한다."""
        if self.rows(ready=False):
            raise ValueError("seed 대상 작업 영역이 비어 있지 않습니다.")
        with other.connect() as source, self.connect() as target:
            # source의 문서/별칭/head를 하나의 읽기 트랜잭션으로 관측한다. 복사 중 갱신된
            # head와 이전 documents를 섞지 않는다. 원문 파일은 읽기 전용으로 참조한다.
            source.execute("BEGIN")
            for row in source.execute("SELECT * FROM documents WHERE state='ready'"):
                target.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(row))
            for table in ("aliases", "heads"):
                for row in source.execute("SELECT * FROM " + table):
                    target.execute(
                        "INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")",
                        tuple(row),
                    )

    def save_history(self, thread, value):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?,?)",
                (thread, json.dumps(value, ensure_ascii=False)),
            )
