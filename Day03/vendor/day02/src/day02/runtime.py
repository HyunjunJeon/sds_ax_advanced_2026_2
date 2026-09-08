"""환경과 HTTP 입출력만 담당합니다. 검색·발췌·예산 판단은 retrievers.py에 있습니다."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)


def make_llm(model: str | None = None, max_tokens: int = 6000) -> ChatOpenAI:
    """모델 가중치 없이 OpenRouter를 호출합니다. 키나 응답 원문은 로그로 출력하지 않습니다."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("Day-02/.env에 OPENROUTER_API_KEY를 설정하세요.")

    return ChatOpenAI(
        model=model or os.getenv("OPENROUTER_MODEL", "google/gemini-3.8-flash"),
        api_key=key,
        base_url=os.getenv("OPENROUTER_BASE_URL") or os.getenv("OPENROUTER_API_BASE") or "https://openrouter.ai/api/v1",
        temperature=0,
        max_tokens=max_tokens,
        # Gemini 3.8 Flash의 추론 토큰도 출력 한도에 포함됩니다.
        extra_body={"reasoning": {"effort": os.getenv("OPENROUTER_REASONING_EFFORT", "low")}},
        timeout=120,
        max_retries=1,
    )


def image_part(png: bytes) -> dict:
    encoded = base64.b64encode(png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


class VikingClient:
    """OpenViking HTTP 계약을 작은 메서드로 노출합니다. 검색 실패를 빈 결과로 숨기지 않습니다.

    Args:
        url: 강사 서버 또는 로컬 OpenViking 서버(Docker/Python) 주소.
        api_key: OpenViking 서버의 인증 키. OpenRouter 키와 구분합니다.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = (url or os.getenv("OPENVIKING_URL", "http://localhost:1933")).rstrip("/")
        key = api_key or os.getenv("OPENVIKING_API_KEY", "")
        self.http = httpx.Client(
            base_url=self.url,
            headers={"X-API-Key": key},
            timeout=httpx.Timeout(180, connect=10),
        )

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "error":
            raise RuntimeError(f"OpenViking {path}: {payload.get('error', 'error')}")
        return payload.get("result", payload)

    def find(self, query: str, target_uri: str, limit: int = 12) -> list[dict]:
        result = self.request(
            "POST", "/api/v1/search/find",
            json={"query": query, "target_uri": target_uri, "limit": limit, "level": 2},
        )
        return result.get("resources", [])

    def grep(self, pattern: str, uri: str, limit: int = 24) -> list[dict]:
        result = self.request(
            "POST", "/api/v1/search/grep",
            json={"pattern": pattern, "uri": uri, "node_limit": limit},
        )
        return result.get("matches", []) if isinstance(result, dict) else result

    def read(self, uri: str, offset: int = 0, limit: int = 80) -> str:
        """offset은 0부터 시작합니다. 전체 문서 대신 제한된 행을 읽습니다."""
        result = self.request(
            "GET", "/api/v1/content/read",
            params={"uri": uri, "offset": offset, "limit": limit},
        )
        return result if isinstance(result, str) else result["content"]

    def abstract(self, uri: str) -> str:
        return self.request("GET", "/api/v1/content/abstract", params={"uri": uri})

    def overview(self, uri: str) -> str:
        return self.request("GET", "/api/v1/content/overview", params={"uri": uri})

    def ls(self, uri: str) -> list[dict]:
        return self.request(
            "GET", "/api/v1/fs/ls",
            params={"uri": uri, "recursive": True, "output": "original", "node_limit": 1000},
        )

    def add_file(self, path: Path, target: str) -> dict:
        """로컬 파일을 먼저 업로드합니다. 로컬 절대 경로를 원격 서버에 전달하지 않습니다."""
        with path.open("rb") as stream:
            uploaded = self.request(
                "POST", "/api/v1/resources/temp_upload",
                files={"file": (path.name, stream, "application/octet-stream")},
            )

        return self.request(
            "POST", "/api/v1/resources",
            json={
                "temp_file_id": uploaded["temp_file_id"],
                "to": target,
                "create_parent": True,
                "wait": True,
                "timeout": 150,
            },
        )


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
