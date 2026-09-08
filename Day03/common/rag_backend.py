"""Day-02 EvidenceRetriever 재사용. local은 명시적 어휘 검색, viking은 실제 서버 검색이다.

가르치는 것:
- 근거의 위조 불가 계약: quote가 실제 원문 창의 행 범위 안에 있고 content hash가
  읽은 창과 일치하는지 좌표로 검증한다. "인용 ID가 존재한다"와 "원문이 주장을
  지지한다"는 다른 검사다.
- 저장과 검색 가능의 구분: 원문 저장에 성공해도 find에 나타나지 않으면 ready가
  아니라(IndexPending) 실패로 기록한다. 나중에 "저장했는데 왜 안 나오지"를 디버깅하는
  습관보다 상태 전이(stored→indexing→ready/failed)로 남기는 것이 옳다.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx

from .config import DAY02, viking_url
from .day02_bridge import (
    BusinessMetadata,
    EvidenceRetriever,
    HybridRetriever,
    KeywordRetriever,
    OpenVikingRetriever,
    VikingClient,
    make_evidence,
    select_evidence,
    terms,
)
from .source_registry import digest


class IndexPending(RuntimeError):
    pass


class LocalClient:
    """실제 로컬 원문 검색. 벡터 검색이나 OpenViking으로 표시하지 않는다."""

    def __init__(self, records):
        self.records = {r["uri"]: r for r in records}

    def read(self, uri, offset=0, limit=100):
        text = Path(self.records[uri]["path"]).read_text()
        return "".join(text.splitlines(keepends=True)[offset : offset + limit])

    def find(self, query, target_uri, limit=12):
        words = terms(query)
        ranked = []
        for uri, r in self.records.items():
            if not uri.startswith(target_uri):
                continue
            text = Path(r["path"]).read_text().lower()
            score = sum(1 for w in words if w in text)
            if score:
                ranked.append(
                    {"uri": uri, "abstract": r["metadata"].get("title", ""), "score": score}
                )
        return sorted(ranked, key=lambda x: -x["score"])[:limit]

    def grep(self, pattern, uri, limit=24):
        found = []
        for key, r in self.records.items():
            if key != uri and not key.startswith(uri.rstrip("/") + "/"):
                continue
            for n, line in enumerate(Path(r["path"]).read_text().splitlines(), 1):
                if re.search(pattern, line, re.I):
                    found.append({"uri": key, "line": n, "content": line})
        return found[:limit]


class RecordingClient:
    def __init__(self, client, meter):
        self.client, self.meter = client, meter
        self.windows = {}

    def find(self, *a, **kw):
        self.meter.consume("find")
        return self.client.find(*a, **kw)

    def grep(self, *a, **kw):
        self.meter.consume("grep")
        return self.client.grep(*a, **kw)

    def read(self, uri, offset=0, limit=80):
        self.meter.consume("read")
        raw = self.client.read(uri, offset=offset, limit=limit)
        self.windows[(uri, digest(raw))] = (raw, offset)
        return raw


class Backend:
    def __init__(self, settings, registry, meter):
        self.settings, self.registry, self.meter = settings, registry, meter
        self.lock = threading.RLock()
        self.evidence = {}
        self.started_wall = time.time()
        self.viking = None
        # workspace별 namespace로 실험 코퍼스를 분리한다. 기존 문서의 URI는 registry에
        # 저장되어 있으므로 seed로 가져온 문서는 재적재 없이 원래 URI에서 읽는다.
        self.root = "viking://resources/day03-rag-" + digest(str(settings.workspace))[:12]
        if settings.backend == "viking":
            self.viking = VikingClient(
                url=viking_url(),
                api_key=os.getenv("OPENVIKING_API_KEY", ""),
            )
            self.viking.http.timeout = httpx.Timeout(min(150, settings.seconds), connect=5)

    def close(self):
        if self.viking:
            self.viking.close()

    def publish(self, doc_id, path, metadata, kind):
        """결정적인 문서 경로를 재사용하고 저장 후 원문 읽기를 확인한다."""
        self.meter.check()
        if self.settings.backend == "local":
            return "local://" + kind + "/" + doc_id
        target = self.root + "/" + kind + "/" + doc_id
        expected = target + "/" + Path(path).name
        # 업로드 응답이 유실되어도 다음 실행에서 기존 본문을 확인한다.
        try:
            existing = self.viking.read(expected, limit=20000)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            existing = None
        if existing is None:
            self.viking.add_file(Path(path), target)
        raw = self.viking.read(expected, limit=20000)
        if raw.strip() != Path(path).read_text().strip():
            raise ValueError("stored_source_mismatch")
        self.meter.consume("index_probe")
        # 저장 후 원문 read 성공만으로 ready가 아니다. find에도 나타나야 재검색 가능하다.
        hits = self.viking.find(metadata.get("title") or Path(path).stem, target, 8)
        if expected not in {h["uri"] for h in hits}:
            raise IndexPending("원문 저장 후 색인 검색 준비가 확인되지 않았습니다.")
        self.meter.check()
        return expected

    def prepare(self, ingest=False):
        catalog = json.loads((DAY02 / "data/business/catalog.json").read_text())
        current = self.registry.rows("internal")
        if len(current) == len(catalog):
            for row in current:
                if digest(Path(row["path"]).read_text()) != row["hash"]:
                    raise ValueError("내부 코퍼스가 변경되었습니다. 새 workspace로 준비하세요.")
            if self.viking:
                self.viking.read(current[0]["uri"], limit=1)
            return
        previous = DAY02 / "outputs/ingestion.json"
        manifest = {}
        if self.viking and not ingest:
            if not previous.exists():
                raise FileNotFoundError(
                    "초기 적재 기록이 없습니다. 00_prepare.py의 LabConfig에서 "
                    "ingest=True로 바꿔 다시 실행하세요."
                )
            state = json.loads(previous.read_text())
            if state.get("server_url", "").rstrip("/") != self.viking.url:
                raise ValueError(
                    "Day-02 manifest 서버와 다릅니다. 00_prepare.py의 LabConfig에서 "
                    "새 workspace와 ingest=True로 바꿔 다시 실행하세요."
                )
            manifest = {m["doc_id"]: uri for uri, m in state["manifest"].items()}
        for item in catalog:
            path = DAY02 / item["path"]
            meta = item["metadata"]
            doc_id, action = self.registry.reserve(
                meta["doc_id"], path.read_text(), meta, path, kind="internal"
            )
            if action == "pending":
                raise IndexPending("다른 실행이 내부 문서를 적재 중입니다.")
            if action == "reused":
                continue
            try:
                if manifest:
                    uri = manifest[meta["doc_id"]]
                    self.viking.read(uri, limit=1)
                else:
                    uri = self.publish(doc_id, path, meta, "internal")
                self.registry.update(doc_id, "ready", uri=uri)
            except Exception as exc:
                self.registry.update(doc_id, "failed", error=type(exc).__name__)
                raise

    def _client(self, records):
        return RecordingClient(self.viking or LocalClient(records), self.meter)

    # content_sha256는 읽은 원문 창 전체의 hash이고 인용문만의 hash가 아니다.
    # 원문 창의 offset을 빼 실제 행 범위를 계산해야 다른 위치의 같은 문장 위조도 막는다.
    def remember(self, items, windows):
        for e in items:
            raw, offset = windows[(e.uri, e.content_sha256)]
            lines = raw.splitlines(keepends=True)
            start, end = e.start_line - offset - 1, e.end_line - offset
            if (
                not 0 <= start < end <= len(lines)
                or e.quote not in "".join(lines[start:end])
                or digest(raw) != e.content_sha256
            ):
                raise ValueError("evidence_coordinates_or_hash")
        with self.lock:
            self.evidence.update({e.evidence_id: e for e in items})

    def retrieve(self, question, request):
        self.meter.event("retrieve", question=question, backend=self.settings.backend)
        records = self.registry.rows()
        internal = [r for r in records if r["kind"] == "internal"]
        client = self._client(records)
        # 후보 검색 범위는 Day-02 manifest 또는 이 실습의 namespace다.
        roots = (
            sorted({r["uri"].rsplit("/", 2)[0] for r in internal})
            if self.viking
            else ["local://internal/"]
        )
        retrievers = []
        for root in roots:
            retrievers.extend(
                [
                    OpenVikingRetriever(client=client, target_uri=root, k=8),
                    KeywordRetriever(client=client, target_uri=root, k=8),
                ]
            )
        # 고객·기준일·승인 상태 필터는 역할 프롬프트 밖의 공통 검색 계약이다.
        # 웹 문서는 BusinessMetadata로 위장시키지 않고 별도 경로로 취급한다.
        manifest = {r["uri"]: BusinessMetadata.model_validate(r["metadata"]) for r in internal}
        retriever = EvidenceRetriever(
            base_retriever=HybridRetriever(retrievers=retrievers, k=10),
            client=client,
            manifest=manifest,
            entity=request.entity,
            as_of=date.fromisoformat(request.as_of),
            max_bytes=self.settings.context_bytes,
            max_documents=8,
            max_read_calls=12,
        )
        selected, report = retriever.retrieve_with_report(question)
        self.remember(selected, client.windows)
        if request.public_topic:
            selected += self.retrieve_web(question + " " + request.public_topic, request, records)
        selected, _ = select_evidence(selected, self.settings.context_bytes, [])
        self.meter.event("retrieved", evidence_ids=[e.evidence_id for e in selected], report=report)
        return selected

    def retrieve_web(self, question, request, records=None):
        # 역사적 버전 전체가 아니라 URL별 ready head만 검색한다. freshness는 수집 확인
        # 시각이며 정책 효력 발생일과 다르다. 한 요청 전체의 snapshot 고정은 과제 12의 확장점이다.
        rows = self.registry.active_web()
        rows = [
            r
            for r in rows
            if time.time() - r["checked_at"] <= self.settings.web_max_age_hours * 3600
            or r["checked_at"] >= self.started_wall
        ]
        if not rows:
            return []
        client = self._client(rows)
        roots = (
            sorted({r["uri"].rsplit("/", 2)[0] for r in rows}) if self.viking else ["local://web/"]
        )
        hits = []
        for root in roots:
            hits.extend(client.find(question, root, 8))
        by_uri = {r["uri"]: r for r in rows}
        evidence = []
        for hit in hits[:8]:
            r = by_uri.get(hit["uri"])
            if not r:
                continue
            meta = r["metadata"]
            if meta.get("published_at") and meta["published_at"][:10] > request.as_of:
                continue
            raw = client.read(r["uri"], limit=500)
            web_meta = SimpleNamespace(
                doc_id=r["id"],
                title=meta["title"],
                source=r["url"],
                page=None,
                version=r["hash"][:12],
            )
            evidence.extend(make_evidence(r["uri"], raw, web_meta, question))
        self.remember(evidence, client.windows)
        return evidence

    def resolve(self, ids):
        with self.lock:
            if any(x not in self.evidence for x in ids):
                raise ValueError("unknown_evidence_id")
            return [self.evidence[x] for x in dict.fromkeys(ids)]
