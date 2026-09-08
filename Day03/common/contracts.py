"""노드와 Worker 사이의 데이터 계약. 스키마가 허용 범위를, reducer가 합류를 정한다.

가르치는 것:
- 모델의 자유 출력을 스키마로 가두기: 역할 이름은 Literal, 계획은 DAG 검증,
  라우팅 결과는 허용 owner만. "JSON을 반환했다"와 "실행 가능한 계약"은 다르다.
- 동시 쓰기의 합류: 동시 Worker가 results에 쓸 때 Annotated reducer(merge_results)가
  동일 재전달은 멱등으로, 상충 결과는 거절로 처리한다.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator, model_validator

from .day02_bridge import GroundedAnswer

ROLES = ("general", "contract", "operations", "security", "policy", "web")


class Request(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    entity: str = "알파"
    as_of: str = "2026-09-08"
    thread: str = "default"
    # 공개 검색 범위는 호출자가 지정한다. 내부 질문을 그대로 EXA로 보내지 않는다.
    public_topic: str = ""

    @field_validator("as_of")
    @classmethod
    def valid_day(cls, value):
        return date.fromisoformat(value).isoformat()


class Task(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    role: Literal["general", "contract", "operations", "security", "policy", "web"]
    objective: str = Field(min_length=1, max_length=2000)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    tasks: list[Task] = Field(min_length=1, max_length=4)
    reason: str

    @model_validator(mode="after")
    # LLM이 JSON을 반환했다는 사실만으로 실행 가능한 계획이 되지 않는다.
    # 선행 작업이 모두 확정된 항목을 반복해서 제거하는 방식으로 DAG를 검증한다.
    def validate_dag(self):
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("중복 task id")
        done = set()
        for _ in self.tasks:
            done.update(t.id for t in self.tasks if set(t.depends_on) <= done)
        if done != ids:
            raise ValueError("알 수 없는 의존성 또는 cycle")
        return self


class WorkerAction(BaseModel):
    action: Literal["search", "finish"]
    query: str = ""
    reason: str
    answer: GroundedAnswer = Field(default_factory=GroundedAnswer)


class Sufficiency(BaseModel):
    sufficient: bool
    missing: list[str]
    public_gap: bool
    reason: str


class Route(BaseModel):
    owner: Literal["general", "contract", "operations", "security", "policy", "web"]
    reason: str


# LangGraph의 동시 Worker가 results에 쓰는 값을 합치는 reducer다.
# 동일 결과의 재전달은 허용하지만 같은 id의 상충 결과는 마지막 도착 값으로 덮지 않는다.
# 현재 버전은 attempt를 모른다. 11번 과제는 지연된 이전 시도까지 구별하도록 확장한다.
def merge_results(left, right):
    merged = dict(left or {})
    for task_id, result in (right or {}).items():
        if task_id in merged and merged[task_id] != result:
            raise ValueError("같은 task id의 상충 결과")
        merged[task_id] = result
    return merged


class State(TypedDict, total=False):
    question: str
    plan: list[dict]
    # Annotated의 두 번째 값은 타입 설명이 아니라 실행 시 사용되는 합류 함수다.
    results: Annotated[dict, merge_results]
    result: dict
    revision: int
    task: dict
    owner: str
    handoffs: int
    review: dict
    evidence: list[dict]
    draft: dict
