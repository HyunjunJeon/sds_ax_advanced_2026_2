"""Source coordinates, immutable quotes and per-turn execution budgets."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from day02.errors import BudgetExceeded


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Metadata(BaseModel):
    doc_id: str
    title: str
    source: str
    domain: str = "자료"
    entity: str = "공통"
    version: str = "unknown"
    valid_from: date = date(1900, 1, 1)
    valid_until: date | None = None
    status: Literal["approved", "draft", "withdrawn"] = "approved"
    page: int | None = None
    slide: int | None = None
    extraction_method: str = "authored"
    source_sha256: str = ""
    source_start_line: int = 1
    source_end_line: int | None = None
    topics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_period(self):
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("유효 기간의 종료일은 시작일 이후여야 합니다.")
        if self.source_start_line < 1:
            raise ValueError("원본 행 번호는 1부터 시작합니다.")
        return self

    def applies(self, entity: str, as_of: date, domain: str | None = None):
        return (self.status == "approved" and self.entity in {entity, "공통"}
                and self.valid_from <= as_of
                and (self.valid_until is None or as_of < self.valid_until)
                and (not domain or self.domain == domain))


class Evidence(BaseModel):
    kind: Literal["source_text", "visual_observation"] = "source_text"
    evidence_id: str
    uri: str
    document_id: str
    title: str
    source: str
    page: int | None = None
    slide: int | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1)
    content_hash: str
    version: str
    metadata: Metadata

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_line < self.start_line or not self.quote.strip():
            raise ValueError("원문 인용과 행 범위를 확인하세요.")
        return self


def evidence_from_text(uri: str, text: str, metadata: Metadata, offset: int = 0):
    return Evidence(
        evidence_id="E-" + digest(f"{uri}:{offset}:{text}")[:12], uri=uri,
        document_id=metadata.doc_id, title=metadata.title, source=metadata.source,
        page=metadata.page, slide=metadata.slide, start_line=offset + 1,
        end_line=offset + len(text.splitlines()), quote=text, content_hash=digest(text),
        version=metadata.version, metadata=metadata,
    )


def evidence_bytes(items: list[Evidence]):
    return len(json.dumps([e.model_dump(mode="json") for e in items], ensure_ascii=False).encode())


@dataclass
class Budget:
    max_tool_calls: int = 18
    max_model_calls: int = 10
    max_searches: int = 3
    max_reads: int = 18
    max_context_bytes: int = 24000
    max_seconds: float = 180
    started: float = field(default_factory=time.monotonic)
    counts: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def remaining(self):
        remaining = self.max_seconds - (time.monotonic() - self.started)
        if remaining <= 0:
            raise BudgetExceeded("턴 실행 시간 한도를 초과했습니다.")
        return remaining

    def consume(self, kind: str, **details):
        with self.lock:
            self.remaining()
            limits = {"tool": self.max_tool_calls, "model": self.max_model_calls,
                      "search": self.max_searches, "read": self.max_reads}
            if kind in limits:
                if self.counts.get(kind, 0) >= limits[kind]:
                    raise BudgetExceeded(f"{kind} 호출 한도를 초과했습니다.")
                self.counts[kind] = self.counts.get(kind, 0) + 1
            self.events.append({"event": kind, "seconds": round(time.monotonic() - self.started, 3), **details})

    def report(self):
        return {"counts": dict(self.counts), "elapsed_seconds": round(time.monotonic() - self.started, 3),
                "max_context_bytes": self.max_context_bytes, "events": list(self.events)}
