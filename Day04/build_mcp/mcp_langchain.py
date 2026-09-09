"""이 실습의 MCP 서버 도구를 LangChain 도구로 변환한다."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from mcp import Client
from pydantic import BaseModel, Field, create_model


def _field_type(spec: dict[str, Any]) -> type[Any]:
    """enum의 허용값은 Literal로 보존한다."""
    enum = spec.get("enum")
    if enum:
        values = tuple(str(item) for item in enum)
        return Literal[values]  # type: ignore[valid-type]
    kind = spec.get("type")
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    return str


def _args_model(name: str, schema: dict[str, Any] | None) -> type[BaseModel]:
    """JSON Schema → Pydantic. 인자 없는 도구도 빈 모델을 준다."""
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in props.items():
        default = ... if key in required else spec.get("default")
        fields[key] = (
            _field_type(spec),
            Field(default, description=str(spec.get("description") or key)),
        )
    return create_model(f"{name}Args", **fields)


def _result_text(result: Any) -> str:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    chunks: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks) or str(result)


def wrap_tools(client: Client) -> Callable[[], Awaitable[list[StructuredTool]]]:
    """연결된 Client에서 도구를 읽어 StructuredTool 리스트를 만든다."""

    async def load() -> list[StructuredTool]:
        listed = await client.list_tools()
        tools: list[StructuredTool] = []
        for spec in listed.tools:
            model = _args_model(spec.name, spec.input_schema)

            async def _run(_name: str = spec.name, **kwargs: Any) -> str:
                # 생략한 선택 인자는 서버의 기본값을 사용한다. null로 덮어쓰지 않는다.
                values = {
                    key: value for key, value in kwargs.items() if value is not None
                }
                result = await client.call_tool(_name, values or None)
                if getattr(result, "is_error", None):
                    return f"error: {_result_text(result)}"
                return _result_text(result)

            tools.append(
                StructuredTool.from_function(
                    coroutine=_run,
                    name=spec.name,
                    description=spec.description or spec.name,
                    args_schema=model,
                )
            )
        return tools

    return load
