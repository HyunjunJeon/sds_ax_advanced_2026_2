"""Turn-scoped retrieval; the model cannot widen the caller's entity/date/namespace."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import httpx
from langchain_core.tools import tool

from day02.errors import ValidationFailure
from day02.evidence import Budget, Evidence, Metadata, digest, evidence_bytes, evidence_from_text


class RetrievalContext:
    def __init__(self, client, manifest: dict, *, entity: str, as_of: date,
                 domain: str | None = None, budget: Budget | None = None,
                 candidate_policy=None):
        self.client = client
        self.manifest = manifest
        self.entity, self.as_of, self.domain = entity, as_of, domain
        self.budget = budget or Budget()
        self.evidence: dict[str, Evidence] = {}
        self.loaded_skills: set[str] = set()
        self.search_count = 0
        # Optional student fusion hook (WORKSHEET 07); None keeps the shipped ordering.
        self.candidate_policy = candidate_policy

    def before_http(self):
        remaining = min(60, self.budget.remaining())
        if hasattr(self.client, "http"):
            self.client.http.timeout = httpx.Timeout(remaining, connect=min(remaining, 10))

    def metadata(self, uri: str):
        record = self.manifest["documents"].get(uri)
        if record is None:
            return None
        metadata = Metadata.model_validate(record["metadata"])
        return metadata if metadata.applies(self.entity, self.as_of, self.domain) else None

    def _read(self, uri: str, offset: int = 0, limit: int = 120):
        metadata = self.metadata(uri)
        if metadata is None:
            raise ValidationFailure("요청 범위에 포함되지 않은 원문입니다.")
        self.budget.consume("read", uri=uri, offset=offset, limit=limit)
        self.before_http()
        raw = self.client.read(uri, offset=offset, limit=limit)
        self.budget.remaining()
        if not raw.strip():
            return None
        record = self.manifest["documents"][uri]
        if record.get("snapshot"):
            snapshot = Path(record["snapshot"]).read_text(encoding="utf-8")
            if digest(snapshot) != record["remote_hash"]:
                raise ValidationFailure("원문 검증용 snapshot이 변경되었습니다. 재적재하세요.")
            expected = "".join(snapshot.splitlines(keepends=True)[offset:offset + limit])
            if raw != expected:
                raise ValidationFailure("서버 원문 구간이 적재 시점과 다릅니다. 재적재하세요.")
        if offset == 0 and limit >= record["line_count"] and digest(raw) != record["remote_hash"]:
            raise ValidationFailure("서버 원문이 manifest와 다릅니다. 재적재 후 질의하세요.")
        item = evidence_from_text(uri, raw, metadata, offset)
        with self.budget.lock:
            if item.evidence_id not in self.evidence:
                prospective = [*self.evidence.values(), item]
                if evidence_bytes(prospective) > self.budget.max_context_bytes:
                    self.budget.consume("context_excluded", uri=uri, reason="context_bytes")
                    return None
                self.evidence[item.evidence_id] = item
        return item

    def search(self, query: str, limit: int = 6):
        if not query.strip() or len(query) > 1500 or not 1 <= limit <= 10:
            raise ValueError("검색어 1~1500자, limit 1~10 범위가 필요합니다.")
        self.budget.consume("search", query=query)
        self.before_http()
        hits = self.client.find(query, self.manifest["target_uri"], max(limit, 12))
        self.search_count += 1
        self.budget.remaining()
        candidates = list(dict.fromkeys(h["uri"] for h in hits if self.metadata(h.get("uri", ""))))[:limit]
        # Explicit family metadata supplies addenda/context, never an unfiltered cross-customer expansion.
        families = {self.manifest["documents"][u]["metadata"].get("family") for u in candidates} - {None, ""}
        for uri, record in self.manifest["documents"].items():
            if (families and record["metadata"].get("family") in families
                    and self.metadata(uri) and uri not in candidates):
                candidates.append(uri)
        if self.candidate_policy is not None:
            reordered = list(self.candidate_policy(list(candidates)))
            if set(reordered) - set(candidates):
                raise ValueError("candidate_policy가 입력에 없는 문서를 반환했습니다.")
            candidates = reordered
        selected = []
        for uri in candidates[:10]:
            count = self.manifest["documents"][uri]["line_count"]
            offset = 0
            if count > 120:
                keywords = re.findall(r"[가-힣A-Za-z0-9_-]{2,}", query)[:10]
                self.before_http()
                anchors = self.client.grep("|".join(map(re.escape, keywords)), uri, 3) if keywords else []
                if anchors:
                    offset = max(0, int(anchors[0].get("line", 1)) - 9)
            item = self._read(uri, offset=offset)
            if item:
                selected.append(item.model_dump(mode="json"))
        return {"status": "found" if selected else "no_evidence", "evidence": selected,
                "scope": {"entity": self.entity, "as_of": self.as_of.isoformat(), "domain": self.domain},
                "context_bytes": evidence_bytes(list(self.evidence.values())),
                "note": "원문 관련성과 충분성은 답변 전에 판단하세요. 검색 성공은 답변 가능을 뜻하지 않습니다."}

    def read_more(self, evidence_id: str, start_line: int, line_count: int = 60):
        if evidence_id not in self.evidence:
            raise ValueError("현재 턴의 검색으로 받은 evidence_id만 읽을 수 있습니다.")
        if start_line < 1 or not 1 <= line_count <= 120:
            raise ValueError("start_line >= 1, line_count 1~120 범위가 필요합니다.")
        item = self._read(self.evidence[evidence_id].uri, start_line - 1, line_count)
        return {"status": "found" if item else "empty_or_budget", "evidence": item.model_dump(mode="json") if item else None}


def make_search_tools(context: RetrievalContext):
    @tool
    def search_knowledge(query: str, limit: int = 6) -> dict:
        """OpenViking에서 업무 문서를 검색하고 실제 원문·인용 ID를 반환한다. 고객/기준일은 호출자가 정한 범위를 따른다."""
        return context.search(query, limit)

    @tool
    def read_evidence(evidence_id: str, start_line: int, line_count: int = 60) -> dict:
        """검색에서 받은 근거의 앞뒤 원문을 추가 조회한다. 행은 1부터 시작하며 표 각주/예외를 확인할 때 사용한다."""
        return context.read_more(evidence_id, start_line, line_count)

    return [search_knowledge, read_evidence]
