"""한 곳에서만 Day-02 소스 패키지를 연결한다. 동봉한 최소 스냅샷을 우선 사용한다.

가르치는 것:
- 의존성의 단일 연결 지점: Day-02의 검색 알고리즘·근거 계약이 여기서만 import되므로
  하위 호환·교체·환경 오염 dotenv 정리를 한 파일에서 관리할 수 있다.
"""

import importlib.util
import os
import sys

from .config import DAY02

if "day02" not in sys.modules:
    source = DAY02 / "src/day02"
    spec = importlib.util.spec_from_file_location(
        "day02", source / "__init__.py", submodule_search_locations=[str(source)]
    )
    if not spec or not spec.loader:
        raise RuntimeError("vendor/day02/src/day02 또는 인접 Day-02/src/day02가 필요합니다.")
    package = importlib.util.module_from_spec(spec)
    sys.modules["day02"] = package
    spec.loader.exec_module(package)

_environment_before = set(os.environ)

from day02.adaptive import QueryPlan, Review
from day02.evidence import (
    ANSWER_SYSTEM,
    BusinessMetadata,
    Claim,
    Evidence,
    GroundedAnswer,
    make_evidence,
    render_context,
    select_evidence,
    terms,
    validate_citations,
)
from day02.retrievers import (
    EvidenceRetriever,
    HybridRetriever,
    KeywordRetriever,
    OpenVikingRetriever,
)
from day02.runtime import VikingClient

# Day-02 runtime의 dotenv 초기화가 Day-03 실행 설정을 추가로 채우지 않게 한다.
for _name in set(os.environ) - _environment_before:
    os.environ.pop(_name, None)

__all__ = [
    "BusinessMetadata",
    "Evidence",
    "GroundedAnswer",
    "Claim",
    "make_evidence",
    "select_evidence",
    "render_context",
    "validate_citations",
    "ANSWER_SYSTEM",
    "terms",
    "EvidenceRetriever",
    "OpenVikingRetriever",
    "HybridRetriever",
    "KeywordRetriever",
    "QueryPlan",
    "Review",
    "VikingClient",
]
