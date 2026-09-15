"""
사용: with minimal_tools(model): agent = create_deep_agent(model=model, ...)
포인트:
  1. HarnessProfile.excluded_tools 로 DeepAgents 내장 도구(ls/read_file/write_file/edit_file/glob/grep/delete/execute)를
     모델의 도구 목록에서 뺀다. _ToolExclusionMiddleware 가 스택 맨 끝에서 걸러내고, 호출 경계에서도 거절한다.
  2. general_purpose_subagent=enabled False 로 부모 도구 전체를 물려받는 기본 Subagent 도 만들지 않는다.
  3. 프로필은 프로세스 전역 레지스트리(provider 키)에 등록되고 create_deep_agent 시점에 해석된다. 0.7.13 에는
     해제 API 가 없어서 with 블록 안에서만 등록하고 나올 때 원래 값으로 되돌린다. 같은 프로세스에서 구성을 바꿔 가며
     비교하는 02·06 때문이다.
  4. 문서가 명시하듯 "model-facing calibration 이지 security surface 가 아니다". 파일 권한·도구 래퍼를 대체하지 않는다.
     수업에서는 '최소 도구'(슬라이드 12·52)의 구현이다.

주요 내용:
내장 도구를 쓰지 않는 Agent 에는 그 도구를 아예 보여주지 않는다. 라이브 실행에서 모델이 read_doc 대신 내장 read_file 로
문서를 읽는 경로가 관찰됐다(02). 업무 도구 래퍼는 그 경로를 보지 못하므로, 파일 권한(scope_permissions)과 함께
이 프로필로 경로 자체를 닫는다.
"""

from __future__ import annotations

from contextlib import contextmanager

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile
from deepagents._models import get_model_provider
from deepagents.profiles.harness import harness_profiles as _hp

BUILTIN_FS_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "delete", "execute"})


def minimal_profile(*, keep_task: bool = True, extra_excluded: frozenset[str] = frozenset()) -> HarnessProfile:
    """내장 파일 도구·execute 를 빼고 general-purpose Subagent 를 끈 프로필. keep_task=False 면 위임(task)도 뺀다."""
    excluded = set(BUILTIN_FS_TOOLS) | set(extra_excluded)
    if not keep_task:
        excluded.add("task")
    return HarnessProfile(excluded_tools=frozenset(excluded),
                          general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))


def profile_key(model) -> str:
    """프로필 레지스트리 키. ChatOpenRouter 는 "openrouter", 대본 모델은 클래스 이름에서 파생된다."""
    key = get_model_provider(model)
    if not key:
        raise RuntimeError(f"모델의 provider 를 알 수 없어 프로필을 붙일 수 없습니다: {type(model).__name__}")
    return key


@contextmanager
def minimal_tools(model, *, keep_task: bool = True, extra_excluded: frozenset[str] = frozenset()):
    """with 블록 안에서 만든 create_deep_agent 에만 프로필을 적용한다. 블록을 나가면 레지스트리를 원래대로 되돌린다."""
    key = profile_key(model)
    _hp._ensure_harness_profiles_loaded()
    registry = _hp._HARNESS_PROFILES
    previous = registry.get(key)
    registry[key] = minimal_profile(keep_task=keep_task, extra_excluded=extra_excluded)
    try:
        yield registry[key]
    finally:
        if previous is None:
            registry.pop(key, None)
        else:
            registry[key] = previous
