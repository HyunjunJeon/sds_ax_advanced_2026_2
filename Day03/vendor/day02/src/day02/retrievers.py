"""후보 검색과 근거 선택을 분리한 BaseRetriever 구현. 02_05에서 직접 재구현합니다."""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from day02.evidence import BusinessMetadata, Evidence, make_evidence, select_evidence, terms


class OpenVikingRetriever(BaseRetriever):
    """OpenViking L2 검색. page_content는 후보 요약이며 아직 인용 가능한 원문이 아닙니다."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    client: Any
    target_uri: str
    k: int = 12

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        results = self.client.find(query, self.target_uri, self.k)
        return [
            Document(
                page_content=hit.get("abstract", ""),
                metadata={"uri": hit["uri"], "score": hit.get("score", 0), "stage": "candidate"},
            )
            for hit in results
        ]


class KeywordRetriever(BaseRetriever):
    """서버 원문의 정확한 용어 찾기. BM25도 sparse embedding도 아닌 grep 검색입니다."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    client: Any
    target_uri: str
    k: int = 12

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        keywords = sorted(terms(query))[:8]
        if not keywords:
            return []

        matches = self.client.grep("|".join(map(re.escape, keywords)), self.target_uri, self.k)
        documents = []
        seen = set()
        for match in matches:
            uri = match.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            documents.append(Document(
                page_content=match.get("content", ""),
                metadata={"uri": uri, "score": 1.0, "stage": "candidate"},
            ))
        return documents


def reciprocal_rank_fusion(rankings: list[list[Document]], weights: list[float] | None = None,
                           constant: int = 60) -> list[Document]:
    """RRF: 서로 다른 검색 점수 대신 순위를 더합니다. 같은 목록의 중복은 한 번만 셉니다."""
    weights = weights or [1.0] * len(rankings)
    if len(weights) != len(rankings) or constant < 1 or any(w < 0 for w in weights):
        raise ValueError("검색기 수, 음수 가중치, RRF 상수를 확인하세요.")

    scores = {}
    documents = {}
    for weight, ranking in zip(weights, rankings):
        seen = set()
        for rank, document in enumerate(ranking, start=1):
            key = document.metadata["uri"]
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0) + weight / (constant + rank)
            documents[key] = document

    return [
        Document(page_content=documents[key].page_content,
                 metadata={**documents[key].metadata, "rrf_score": scores[key]})
        for key in sorted(scores, key=scores.get, reverse=True)
    ]


class HybridRetriever(BaseRetriever):
    """Dense 후보와 정확한 용어 후보를 합칩니다. 최종 Context 절약은 다음 단계가 담당합니다."""

    retrievers: list[BaseRetriever]
    weights: list[float] = Field(default_factory=list)
    k: int = 12

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        rankings = [retriever.invoke(query, config={"callbacks": run_manager.get_child()})
                    for retriever in self.retrievers]
        return reciprocal_rank_fusion(rankings, self.weights or None)[:self.k]


class EvidenceRetriever(BaseRetriever):
    """검증된 업무 범위에서 원문을 읽고 발췌하여 Document를 반환합니다.

    Args:
        base_retriever: 후보 검색기. OpenViking / Hybrid / MultiQuery를 교체해 붙일 수 있습니다.
        manifest: uri → 신뢰 가능한 관리 메타데이터. 미등록 문서는 답변 근거로 쓰지 않습니다.
        max_read_lines: 한 문서에서 읽는 행 상한. 긴 파일은 grep 행 위치 기반 확장 실습에서 다룹니다.
        facets: 필요한 답변 항목과 표현. 커버리지는 휴리스틱이며 충분성을 보증하지 않습니다.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    base_retriever: BaseRetriever
    client: Any
    manifest: dict[str, BusinessMetadata]
    entity: str
    as_of: date
    facets: dict[str, list[str]] = Field(default_factory=dict)
    domains: list[str] = Field(default_factory=list)
    max_bytes: int = 5000
    max_documents: int = 8
    max_read_lines: int = 100
    max_read_calls: int = 16
    span_selector: Any = None

    def retrieve_with_report(self, query: str) -> tuple[list[Evidence], dict]:
        return self.retrieve_queries([query])

    def retrieve_queries(self, queries: list[str]) -> tuple[list[Evidence], dict]:
        if not 1 <= len(queries) <= 3:
            raise ValueError("한 요청의 질의 수는 1~3개입니다.")
        rankings = [self.base_retriever.invoke(query) for query in queries]
        candidates = reciprocal_rank_fusion(rankings)
        query = "\n".join(queries)
        evidence = []
        reads = []
        excluded = []
        seen = set()

        for candidate in candidates:
            uri = candidate.metadata["uri"]
            if uri in seen:
                continue
            seen.add(uri)
            metadata = self.manifest.get(uri)

            if (metadata is None or not metadata.applies(self.entity, self.as_of)
                    or (self.domains and metadata.domain not in self.domains)):
                excluded.append({"uri": uri, "reason": "미등록 / 적용 대상 / 유효 기간 / 승인 상태"})
                continue
            if len({item["uri"] for item in reads}) >= self.max_documents:
                excluded.append({"uri": uri, "reason": "원문 읽기 횟수 상한"})
                continue

            # 문서 후반의 근거도 찾습니다. grep의 line은 1-based, read offset은 0-based입니다.
            keywords = sorted(terms(query))[:12]
            anchors = self.client.grep("|".join(map(re.escape, keywords)), uri, 12) if keywords else []
            offsets = sorted({max(0, int(hit["line"]) - 1 - 8) for hit in anchors})
            windows = []
            for offset in offsets or [0]:
                end = offset + min(32, self.max_read_lines)
                if windows and offset <= windows[-1][1]:
                    start, old_end = windows.pop()
                    windows.append((start, min(max(end, old_end), start + self.max_read_lines)))
                else:
                    windows.append((offset, end))

            for start, end in windows[:2]:
                if len(reads) >= self.max_read_calls:
                    excluded.append({"uri": uri, "reason": "원문 읽기 API 횟수 상한"})
                    break
                raw = self.client.read(uri, offset=start, limit=end - start)
                reads.append({"uri": uri, "offset": start, "bytes": len(raw.encode()), "line_limit": end - start})
                if self.span_selector is not None:
                    evidence.extend(self.span_selector(uri, raw, metadata, query,
                                                       base_line=start, facets=self.facets))
                else:
                    evidence.extend(make_evidence(uri, raw, metadata, query, base_line=start, facets=self.facets))

        selected, report = select_evidence(evidence, self.max_bytes, list(self.facets))
        report.update({"reads": reads, "excluded": excluded, "candidate_documents": len(candidates)})
        return selected, report

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        selected, report = self.retrieve_with_report(query)
        return [
            Document(page_content=item.quote,
                     metadata={**item.model_dump(mode="json"), "context_report": report})
            for item in selected
        ]
