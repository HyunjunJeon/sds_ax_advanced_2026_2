"""OpenViking 0.4.16 HTTP adapter; no in-process database globals."""
from __future__ import annotations

from pathlib import Path

import httpx

from day02.errors import ServiceError
from day02.settings import Settings


class VikingClient:
    def __init__(self, url: str, api_key: str, *, timeout: float = 90, transport=None):
        self.url = url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.url, headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
            transport=transport, trust_env=False,
        )

    @classmethod
    def configured(cls, settings: Settings, **kwargs):
        return cls(*settings.connection(), **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.http.close()

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            reason = "인증/문서 권한 실패" if code in (401, 403) else "OpenViking HTTP 오류"
            raise ServiceError(f"{reason}: {method} {path} ({code})", code) from None
        except httpx.RequestError as exc:
            raise ServiceError(f"OpenViking 연결 실패: {type(exc).__name__}") from None
        try:
            payload = response.json()
        except ValueError:
            raise ServiceError(f"OpenViking JSON 응답이 아닙니다: {path}") from None
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise ServiceError(f"OpenViking 작업 실패: {path}")
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    def find(self, query: str, target_uri: str, limit: int = 12):
        result = self.request("POST", "/api/v1/search/find", json={
            "query": query, "target_uri": target_uri, "limit": limit, "level": 2,
        })
        return result.get("resources", [])

    def grep(self, pattern: str, uri: str, limit: int = 12):
        result = self.request("POST", "/api/v1/search/grep", json={
            "pattern": pattern, "uri": uri, "node_limit": limit,
        })
        return result.get("matches", []) if isinstance(result, dict) else result

    def read(self, uri: str, offset: int = 0, limit: int = 100):
        result = self.request("GET", "/api/v1/content/read", params={
            "uri": uri, "offset": offset, "limit": limit,
        })
        return result if isinstance(result, str) else result["content"]

    def abstract(self, uri: str):
        return self.request("GET", "/api/v1/content/abstract", params={"uri": uri})

    def overview(self, uri: str):
        return self.request("GET", "/api/v1/content/overview", params={"uri": uri})

    def ls(self, uri: str):
        result = self.request("GET", "/api/v1/fs/ls", params={
            "uri": uri, "recursive": True, "output": "original", "node_limit": 1000,
        })
        return result.get("entries", result.get("files", [])) if isinstance(result, dict) else result

    def add_file(self, path: Path, target: str):
        with path.open("rb") as stream:
            uploaded = self.request("POST", "/api/v1/resources/temp_upload", files={
                "file": (path.name, stream, "text/markdown"),
            })
        return self.request("POST", "/api/v1/resources", json={
            "temp_file_id": uploaded["temp_file_id"], "to": target,
            "create_parent": True, "wait": True, "timeout": 120,
        }, timeout=150)
