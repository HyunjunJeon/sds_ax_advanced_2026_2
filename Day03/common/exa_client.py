"""EXA Search/Contents. 명시적인 live/replay와 공개 질의만 사용한다.

가르치는 것:
- 외부 호출의 경계: 허용 도메인(접미사 위장 거부), file://·자격증 포함 URL 거부로
  어디로 나가는지를 코드가 결정한다. 내부 질문은 public_topic로만 대체되어 나간다.
- 관측 가능성: 외부 응답을 snapshot으로 남겨 같은 질의·옵션의 replay가 가능하게
  한다. 웹 지연 없이 절차·비용 실험을 반복할 수 있는 기반이다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .source_registry import canonical_url, digest


def allowed_url(url, domains):
    try:
        host = urlsplit(canonical_url(url)).hostname
        # 접미사 앞의 점까지 검사한다. docs.python.org.evil.test는 허용 도메인이 아니다.
        return any(host == d or host.endswith("." + d) for d in domains)
    except ValueError:
        return False


class ExaClient:
    def __init__(self, settings, meter, cache_dir, mode="live"):
        if mode not in {"live", "replay"}:
            raise ValueError("EXA mode")
        self.settings, self.meter, self.mode = settings, meter, mode
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, public_topic):
        if not public_topic.strip():
            raise ValueError("외부 검색에는 명시적인 public_topic이 필요합니다.")
        self.meter.consume("exa_search")
        started = time.monotonic()
        payload = {
            "query": public_topic,
            "type": "auto",
            "numResults": 5,
            "includeDomains": list(self.settings.domains),
            "contents": {
                "text": {"maxCharacters": 18000},
                "maxAgeHours": self.settings.web_max_age_hours,
            },
        }
        # 공개 질의뿐 아니라 도메인/본문 길이/freshness 옵션까지 cache key에 포함한다.
        # replay는 EXA 응답만 고정한다. LLM 계획이나 응답의 재현성까지 보장하지 않는다.
        path = self.cache_dir / (digest(json.dumps(payload, sort_keys=True)) + ".json")
        if self.mode == "replay":
            if not path.exists():
                raise FileNotFoundError("EXA replay snapshot이 없습니다.")
            data = json.loads(path.read_text())
        else:
            key = os.getenv("EXA_API_KEY")
            if not key:
                raise RuntimeError("Day-03/.env에 EXA_API_KEY를 설정하세요.")
            with httpx.Client(
                base_url="https://api.exa.ai", headers={"x-api-key": key}, timeout=45
            ) as client:
                response = client.post("/search", json=payload)
                response.raise_for_status()
                data = response.json()
                missing = [
                    r["url"]
                    for r in data.get("results", [])
                    if not r.get("text") and allowed_url(r.get("url", ""), self.settings.domains)
                ]
                if missing:
                    self.meter.consume("exa_contents")
                    contents = client.post(
                        "/contents",
                        json={
                            "urls": missing[:5],
                            "text": {"maxCharacters": 18000},
                            "maxAgeHours": self.settings.web_max_age_hours,
                        },
                    )
                    contents.raise_for_status()
                    content_data = contents.json()
                    data["_contents_cost"] = content_data.get("costDollars")
                    by_url = {r["url"]: r for r in content_data.get("results", [])}
                    data["results"] = [
                        r | by_url.get(r["url"], {}) for r in data.get("results", [])
                    ]
            data["_fetched_at"] = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        self.meter.check()
        candidates = []
        for r in data.get("results", [])[:5]:
            if (
                not allowed_url(r.get("url", ""), self.settings.domains)
                or not r.get("text", "").strip()
            ):
                self.meter.event("web_rejected", reason="domain_or_empty_body")
                continue
            candidates.append(
                {
                    "url": canonical_url(r["url"]),
                    "original_url": r["url"],
                    "title": r.get("title", ""),
                    "text": r["text"],
                    "published_at": r.get("publishedDate"),
                    "fetched_at": data.get("_fetched_at"),
                    "public_topic": public_topic,
                }
            )
        self.meter.event(
            "exa_results",
            mode=self.mode,
            count=len(candidates),
            snapshot=path.name,
            elapsed_s=round(time.monotonic() - started, 3),
            estimated_search_cost=data.get("costDollars"),
            estimated_contents_cost=data.get("_contents_cost"),
        )
        return candidates
