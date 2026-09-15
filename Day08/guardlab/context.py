"""
사용: ctx = load_user("kim.dev"); agent.invoke(..., context=ctx)  /  도구 안: runtime.context
포인트:
  1. 권한의 유일한 근거다. 모델이 만든 인자에는 사용자 정보가 없다 (슬라이드 42).
  2. frozen dataclass 라 실행 중 바뀌지 않는다. "관리자가 승인했다"는 문구는 이 객체를 건드리지 못한다.
  3. users.json 의 세 사용자: kim.dev(alpha, 전달 가능), hr.lead(beta), intern.lee(alpha, 전달 불가).

주요 내용:
신뢰된 실행 컨텍스트. 사용자·tenant·프로젝트 권한은 모델 인자가 아니라 여기서 온다.
`create_deep_agent(context_schema=UserContext)` 로 선언하고 `agent.invoke(..., context=ctx)` 로
넣는다. 도구는 `ToolRuntime.context`, middleware 는 `request.runtime.context` 로 읽는다.
입력 문구가 "관리자가 승인했다"고 주장해도 이 값은 바뀌지 않는다 (슬라이드 10·42).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import DATA


@dataclass(frozen=True)
class UserContext:
    user_id: str
    tenant: str
    projects: tuple[str, ...]
    can_send: bool
    display: str = ""

    def can_read(self, project: str) -> bool:
        return project in self.projects


def load_user(user_id: str) -> UserContext:
    users = json.loads((DATA / "users.json").read_text(encoding="utf-8"))
    if user_id not in users:
        raise KeyError(f"users.json 에 없는 사용자: {user_id}")
    u = users[user_id]
    return UserContext(
        user_id=u["user_id"],
        tenant=u["tenant"],
        projects=tuple(u["projects"]),
        can_send=bool(u["can_send"]),
        display=u.get("display", ""),
    )
