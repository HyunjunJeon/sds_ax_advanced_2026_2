"""모델 없이 MCP 도구 목록을 읽거나 지정한 도구를 한 번 호출한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

REFERENCE = Path(__file__).resolve().parent / "reference" / "server.py"


def server_parameters(
    server: Path, db: Path | None = None, evidence_dir: Path | None = None
) -> StdioServerParameters:
    args = [str(server.resolve())]
    if db is not None:
        args += ["--db", str(db.resolve())]
    if evidence_dir is not None:
        args += ["--evidence-dir", str(evidence_dir.resolve())]
    return StdioServerParameters(command=sys.executable, args=args)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=REFERENCE)
    parser.add_argument("--db", type=Path, help="생략하면 서버의 기본 저장 위치를 사용")
    parser.add_argument("--evidence-dir", type=Path, help="근거 파일을 모을 폴더")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="도구 이름·설명·입력 형식을 확인")
    call = commands.add_parser("call", help="도구를 한 번 호출")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON 객체")
    args = parser.parse_args()
    if not args.server.is_file():
        parser.error("서버 파일이 없습니다. --server 경로를 확인하세요.")
    arguments = {}
    if args.command == "call":
        try:
            arguments = json.loads(args.arguments)
        except json.JSONDecodeError as error:
            parser.error(f"인자가 JSON 형식이 아닙니다: {error.msg}")
        if not isinstance(arguments, dict):
            parser.error("도구 인자는 JSON 객체여야 합니다.")

    async with Client(
        server_parameters(args.server, args.db, args.evidence_dir)
    ) as client:
        if args.command == "list":
            result = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in (await client.list_tools()).tools
            ]
            failed = False
        else:
            response = await client.call_tool(args.tool, arguments)
            result = response.structured_content
            if result is None:
                result = {
                    "is_error": response.is_error,
                    "content": [getattr(item, "text", "") for item in response.content],
                }
            failed = response.is_error or result.get("success") is False
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
