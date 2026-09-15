"""
사용: PIIGuard("regex"|"lfm").scan(text) → list[Span] / .signal(text) → GuardSignal
포인트:
  1. 두 모드가 같은 Span 계약을 쓴다. 그래서 03·07 에서 나란히 비교할 수 있다.
  2. lfm 은 trust_remote_code 모델이다. GUARD_PII_REVISION 으로 커밋을 고정한다 (공급망 위험, 슬라이드 12).
  3. lfm 의 구간은 토큰 단위로 쪼개져 온다. 병합은 defenses.merge_spans 가 한다. 모델은 구간만, 코드가 처리를.
  4. vLLM 이 지원하지 않는 아키텍처라 transformers 로 돈다. device 를 주면 GPU 도 된다.

주요 내용:
개인정보 구간 탐지기.
mode:
  "regex"  이메일·한국 휴대전화·주민등록번호·카드번호 정규식. 결정적이며 모델이 아니다.
  "lfm"    LiquidAI/LFM2.5-Encoder-350M-PII-Detector (token classification, 40개 유형, 한국어 포함).
           trust_remote_code 모델이므로 GUARD_PII_REVISION 으로 커밋을 고정한다.

둘 다 구간(Span)만 돌려준다. 마스킹·삭제·공개 여부 판단은 후속 코드(03 학생 구현)가 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import env
from .contracts import GuardSignal, unknown_signal


@dataclass
class Span:
    start: int
    end: int
    label: str
    score: float
    text: str

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "label": self.label, "score": self.score, "text": self.text}


REGEX_RULES = [
    ("contact.email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("contact.phone", re.compile(r"01[016789]-?\d{3,4}-?\d{4}")),
    ("identity.national_id", re.compile(r"\d{6}-[1-4]\d{6}")),
    ("financial.card", re.compile(r"(?:\d{4}-){3}\d{4}")),
]


class PIIGuard:
    def __init__(self, mode: str = "regex", *, model: str | None = None, revision: str | None = None):
        self.mode = mode
        self.model = model or env("GUARD_PII_MODEL", "LiquidAI/LFM2.5-Encoder-350M-PII-Detector")
        self.revision = revision if revision is not None else env("GUARD_PII_REVISION")
        self._pipe = None
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "regex-kr" if self.mode == "regex" else self.model

    def scan(self, text: str) -> list[Span]:
        self.calls += 1
        if self.mode == "regex":
            return self._scan_regex(text)
        if self.mode == "lfm":
            return self._scan_lfm(text)
        raise ValueError(f"알 수 없는 mode: {self.mode}")

    def signal(self, text: str, stage: str = "output") -> GuardSignal:
        try:
            spans = self.scan(text)
        except RuntimeError as e:
            return unknown_signal(stage, self.model_id, str(e))
        return GuardSignal(stage=stage, status="OK", risk_labels=sorted({s.label for s in spans}),
                           evidence_spans=[s.as_dict() for s in spans], score_type="span_count",
                           score=float(len(spans)), model_id=self.model_id, revision=self.revision)

    def _scan_regex(self, text: str) -> list[Span]:
        out: list[Span] = []
        for label, rx in REGEX_RULES:
            for m in rx.finditer(text):
                out.append(Span(m.start(), m.end(), label, 1.0, m.group(0)))
        return sorted(out, key=lambda s: (s.start, -s.end))

    def _load(self):
        if self._pipe is not None:
            return self._pipe
        try:
            from transformers import pipeline
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("transformers 가 없습니다. `uv sync` 를 다시 실행하세요.") from e
        kwargs = {"model": self.model, "trust_remote_code": True, "aggregation_strategy": "simple"}
        if self.revision:
            kwargs["revision"] = self.revision
        try:
            self._pipe = pipeline("token-classification", **kwargs)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"LFM PII 모델 로드 실패 ({type(e).__name__}). 네트워크·HF_HOME·revision 을 확인하세요.") from e
        return self._pipe

    def _scan_lfm(self, text: str) -> list[Span]:
        pipe = self._load()
        out: list[Span] = []
        for ent in pipe(text):
            label = ent.get("entity_group") or ent.get("entity") or "pii"
            out.append(Span(int(ent["start"]), int(ent["end"]), str(label), float(ent["score"]),
                            text[int(ent["start"]):int(ent["end"])]))
        return sorted(out, key=lambda s: (s.start, -s.end))
