"""실행 설정의 원천. 경로 해결과 .env 로드, 예산 검증을 한 곳에서 한다.

가르치는 것:
- 경로는 실행 위치가 아니라 이 파일 기준으로 해결한다. 어느 디렉터리에서 실행해도
  같은 .env·동봉 자료를 읽어야 실험이 재현된다.
- 셸 환경변수는 호출자의 의도적 설정이므로 .env가 덮어쓰지 않는다(override=False).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# __file__을 기준으로 잡아 다른 작업 디렉터리에서 실행해도 같은 .env와 자료를 읽는다.
# 본편을 Day03로 옮겼으므로 common/의 부모가 프로젝트 루트다.
DAY03 = Path(__file__).resolve().parents[1]
DAY02 = DAY03 / "vendor/day02"
if not DAY02.is_dir():
    DAY02 = DAY03.parent / "Day-02"
LAB = DAY03
# 환경변수는 호출자가 의도적으로 준 실행 설정이다. .env로 덮어쓰지 않는다.
load_dotenv(DAY03 / ".env", override=False)

# 로컬 OpenViking 서버의 포트는 .env의 OPENVIKING_PORT 하나로 정한다. native_server.py의
# 서버 설정과 실습의 검색 주소가 같은 값을 보도록 여기서만 해석한다. 원격 서버를 쓸 때만
# OPENVIKING_URL을 직접 주며, 그 경우 URL이 포트보다 우선한다.
DEFAULT_VIKING_PORT = 1935


def viking_port() -> int:
    """.env의 OPENVIKING_PORT를 읽는다. 값이 없으면 기본 포트를 쓴다."""
    raw = os.getenv("OPENVIKING_PORT") or DEFAULT_VIKING_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("OPENVIKING_PORT는 정수여야 합니다.") from None
    if not 1024 <= port <= 65535:
        raise ValueError("OPENVIKING_PORT는 1024~65535 범위여야 합니다.")
    return port


def viking_url() -> str:
    """검색·적재가 붙을 서버 주소. OPENVIKING_URL이 있으면 그것이 우선한다."""
    return (os.getenv("OPENVIKING_URL") or f"http://127.0.0.1:{viking_port()}").rstrip("/")


@dataclass
class Settings:
    backend: str = "viking"
    workspace: Path = DAY03 / "work/rag/default"
    web: str = "off"  # off / transient / persist
    model: str = ""
    max_calls: int = 32
    max_tools: int = 100
    seconds: float = 240
    context_bytes: int = 20000
    exa_searches: int = 2
    max_additions: int = 3
    web_max_age_hours: int = 168
    domains: tuple[str, ...] = ("www.python-httpx.org", "docs.python.org")

    # 모든 구조가 같은 제한과 코퍼스를 사용해야 구조 자체의 효과를 비교할 수 있다.
    def __post_init__(self):
        self.workspace = Path(self.workspace).resolve()
        self.model = (
            self.model
            or os.getenv("OPENROUTER_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "google/gemini-3.8-flash"
        )
        if self.backend not in {"local", "viking"} or self.web not in {
            "off",
            "transient",
            "persist",
        }:
            raise ValueError("backend/web 설정 오류")
        if min(self.max_calls, self.max_tools, self.seconds, self.context_bytes) <= 0:
            raise ValueError("실행 예산은 양수여야 합니다.")
        self.workspace.mkdir(parents=True, exist_ok=True)


def key_presence():
    return {
        name: bool(os.getenv(name))
        for name in ("OPENROUTER_API_KEY", "EXA_API_KEY", "OPENVIKING_API_KEY")
    }
