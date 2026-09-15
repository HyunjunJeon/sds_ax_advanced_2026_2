"""
포인트:
  1. GuardSignal = 모델의 의견. PolicyDecision = 코드의 결정. 둘을 섞지 않는 것이 슬라이드 19·36 의 요지다.
  2. status="UNKNOWN" 은 결정이 아니다. 어떤 호출자도 UNKNOWN 을 ALLOW 로 바꾸지 않는다 (defenses.resolve_input_decision 참고).
  3. score_type 으로 점수의 종류(token_prob / verdict / keyword / span_count)를 보존한다. 서로 다른 점수를 직접 비교하지 않는다.

주요 내용:
내부 계약 두 가지 (슬라이드 22·36).
GuardSignal    모델·규칙이 낸 위험 신호. 실행 권한이 아니다.
PolicyDecision 정책 코드가 내린 결정. ALLOW 가 아니면 실제 함수는 호출되지 않는다.

Guard 시간 초과·파싱 실패·누락 판정은 status="UNKNOWN" 으로 넘긴다. UNKNOWN 을 ALLOW 로
해석하는 코드는 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Action = Literal["ALLOW", "BLOCK", "REVIEW", "TRANSFORM"]
Stage = Literal["input", "tool_result", "action", "output", "permissions", "approval", "memory"]


@dataclass
class GuardSignal:
    stage: str
    status: Literal["OK", "UNKNOWN"]
    risk_labels: list[str] = field(default_factory=list)
    evidence_spans: list[dict] = field(default_factory=list)
    score_type: str = ""  # "token_prob" / "keyword" / "span_count" / "fixed"
    score: float | None = None
    model_id: str = ""
    revision: str = ""
    detail: str = ""

    @property
    def risky(self) -> bool:
        return bool(self.risk_labels)


@dataclass
class PolicyDecision:
    action: Action
    reason_code: str
    policy_version: str = "rules-v3"
    risk_labels: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "ALLOW"


def unknown_signal(stage: str, model_id: str, detail: str) -> GuardSignal:
    return GuardSignal(stage=stage, status="UNKNOWN", model_id=model_id, score_type="none", detail=detail)
