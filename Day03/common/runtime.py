"""전체 요청 예산과 모델 호출 측정. planner·Worker·reviewer를 모두 합산한다.

가르치는 것:
- 비용 측정의 단위는 요청 전체다. 병렬 Worker가 늘어도 확인과 증가를 한 lock 안에서
  수행하지 않으면 각자 잔여 1회를 보고 상한이 깨진다.
- 누락된 usage를 0으로 채우면 비용이 낮아 보인다. 합산은 전체 기록이 있을 때만,
  아니면 null로 둔다. SDK 자동 재시도를 끊어 숨은 호출이 예산·trace 밖에서 나지
  않게 하는 것도 같은 원칙이다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter

from langchain_openai import ChatOpenAI


class BudgetExceeded(RuntimeError):
    pass


class Meter:
    def __init__(self, settings):
        self.settings = settings
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.calls = Counter()
        self.events = []
        self.model_records = []

    def check(self):
        if time.monotonic() - self.started > self.settings.seconds:
            raise BudgetExceeded("deadline")

    # 확인과 증가를 같은 lock 안에서 수행한다. 병렬 Worker가 각각 잔여 1회를 보고
    # 둘 다 호출하면 요청 전체 상한이 깨진다. 실패한 호출도 이미 소비한 횟수로 남긴다.
    def consume(self, name):
        with self.lock:
            self.check()
            limit = {
                "model": self.settings.max_calls,
                "exa_search": self.settings.exa_searches,
                "ingest": self.settings.max_additions,
            }.get(name, self.settings.max_tools)
            if self.calls[name] >= limit:
                raise BudgetExceeded(name)
            if (
                name != "model"
                and sum(v for k, v in self.calls.items() if k != "model") >= self.settings.max_tools
            ):
                raise BudgetExceeded("total_tools")
            self.calls[name] += 1

    def event(self, name, **fields):
        with self.lock:
            self.events.append(
                {"event": name, "at_s": round(time.monotonic() - self.started, 4), **fields}
            )

    def summary(self):
        records = list(self.model_records)
        # usage가 누락된 호출을 0으로 처리하면 비용이 낮아 보인다. 전체가 있어야 합산한다.
        complete = bool(records) and all(r.get("usage") for r in records)
        return {
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "calls": dict(self.calls),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in records) if complete else None,
            "output_tokens": sum(r["usage"]["output_tokens"] for r in records)
            if complete
            else None,
            "usage_complete": complete,
            "model_records": records,
        }


class Models:
    def __init__(self, settings, meter):
        self.settings, self.meter = settings, meter
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("Day-03/.env에 OPENROUTER_API_KEY를 설정하세요.")
        self.client = ChatOpenAI(
            model=settings.model,
            api_key=key,
            base_url=os.getenv("OPENROUTER_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1",
            temperature=0,
            max_tokens=6000,
            # SDK 자동 재시도를 끈다. 숨은 호출이 예산/trace 밖에서 발생하지 않게 한다.
            max_retries=0,
            timeout=min(90, settings.seconds),
            extra_body={"reasoning": {"effort": "low"}},
        )

    def ask(self, schema, system, payload, role):
        self.meter.consume("model")
        prompt = json.dumps(payload, ensure_ascii=False, default=str)
        record = {
            "role": role,
            "schema": schema.__name__,
            "input_bytes": len((system + prompt).encode()),
            "usage": None,
        }
        with self.meter.lock:
            self.meter.model_records.append(record)
        start = time.monotonic()
        self.meter.event("model_start", role=role, schema=schema.__name__)
        try:
            result = self.client.with_structured_output(
                schema, method="function_calling", include_raw=True
            ).invoke([("system", system), ("human", prompt)])
            record["usage"] = result["raw"].usage_metadata or None
            if result["parsing_error"] or result["parsed"] is None:
                raise ValueError("structured_output")
            # 응답이 도착했어도 마감 뒤의 결과는 채택하지 않는다. 이미 전송한 HTTP를
            # 강제 취소하는 기능은 아니므로, 서버 비용이 발생하지 않았다는 뜻은 아니다.
            self.meter.check()
            return schema.model_validate(result["parsed"])
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise
        finally:
            record["elapsed_s"] = round(time.monotonic() - start, 3)
            self.meter.event("model_end", role=role, error=record.get("error"))
