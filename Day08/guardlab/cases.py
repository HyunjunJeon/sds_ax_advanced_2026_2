"""
사용: get_case("doc_send_outside") / cases_for("02") / load_holdout() (06 최종 확인 전용)
포인트:
  1. 한 사례 = 실행 입력(user_message, doc_overrides) + 평가 기준(attack_goals, required_facts, forbidden_strings).
     Agent 에는 앞의 둘만 전달된다. 평가 기준은 평가기만 읽는다.
  2. attack_goals 의 type: send_to_unapproved / omit_failures / read_unauthorized / leak / policy_write / send_by_unauthorized_user.
  3. holdout.jsonl 은 프롬프트·규칙·키워드에 넣지 않는다. 넣는 순간 미공개가 아니다.

주요 내용:
공격·정상 사례. 실행 입력(user_message, doc_overrides)과 사후 평가 기준(attack_goals 등)이 한 행에 있지만,
Agent 에는 user_message 와 문서만 전달한다. 평가 기준은 평가기만 읽는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import DATA


@dataclass
class Case:
    id: str
    kind: str  # attack / benign
    title: str
    user: str
    user_message: str
    doc_overrides: dict[str, str] = field(default_factory=dict)
    attack_goals: list[dict] = field(default_factory=list)
    required_facts: list[str] = field(default_factory=list)
    forbidden_strings: list[str] = field(default_factory=list)
    approved_recipient: str = ""
    second_session_message: str = ""
    lab: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def is_attack(self) -> bool:
        return self.kind == "attack"


def _load(path) -> list[Case]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(Case(**json.loads(line)))
    return rows


def load_cases() -> list[Case]:
    return _load(DATA / "cases.jsonl")


def get_case(case_id: str) -> Case:
    for c in load_cases():
        if c.id == case_id:
            return c
    raise KeyError(f"cases.jsonl 에 없는 사례: {case_id}")


def load_holdout() -> list[Case]:
    """미공개 사례. 06 의 최종 확인과 평가기만 읽는다. Agent 입력·프롬프트·Guard 규칙에 넣지 않는다."""
    return _load(DATA / "holdout.jsonl")


def cases_for(lab: str) -> list[Case]:
    return [c for c in load_cases() if lab in c.lab]
