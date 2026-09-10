"""같은 업무를 MCP 도구 또는 Skill의 Python 스크립트로 실행한다.

기본 mcp 모드는 기존 수업용 서버, --mode scripts는 execute 하나를 사용한다.
매 실행은 새 대화다. 이전 업무는 세션 ID와 근거 파일로 이어간다.
모델 호출에는 설정한 계정의 사용량이 발생한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents import (
    FilesystemMiddleware,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import FilesystemBackend
from llm import chat_model
from script_runtime import SelectedSkillsMiddleware, create_script_agent

DAY04 = Path(__file__).resolve().parents[1]
LAB = Path(__file__).resolve().parent


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


async def run(args, emit) -> None:
    model = chat_model(temperature=0, timeout=60)
    # scripts 모드는 모델에 execute만 공개한다. 기본 파일·계획 도구도 제외한다.
    register_harness_profile(
        f"openrouter:{model.model_name}",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=(
                frozenset({"read_file", "write_todos"})
                if args.mode == "scripts"
                else frozenset()
            ),
        ),
    )
    if args.mode == "scripts":
        agent = create_script_agent(model, args, emit)
        emit(
            "ready",
            mode="scripts",
            model=model.model_name,
            model_provider="openrouter",
            tools=["execute"],
        )
        print("[Python 실행] execute · Skill의 scripts/ 사용", flush=True)
        await stream_agent(agent, args, emit)
        return

    # 스크립트 모드에서는 MCP SDK와 변환기를 가져오지 않는다.
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from mcp_langchain import wrap_tools

    backend = FilesystemBackend(root_dir=args.skills_dir, virtual_mode=True)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            str(args.server),
            "--db",
            str(args.db),
            "--evidence-dir",
            str(args.evidence_dir),
        ],
        cwd=str(DAY04),
    )
    # Client가 MCP 서버를 자식 프로세스로 시작하고 블록 종료 시 정리한다.
    async with Client(params) as client:
        tools = await wrap_tools(client)()
        names = [tool.name for tool in tools]
        emit(
            "connected",
            model=model.model_name,
            model_provider="openrouter",
            model_class=type(model).__name__,
            protocol_version=client.protocol_version,
            mcp_tools=names,
        )
        print("[MCP 연결]", ", ".join(names), flush=True)
        middleware = [
            FilesystemMiddleware(
                backend=backend, tools=["read_file", "ls", "glob", "grep"]
            )
        ]
        if not args.no_skills:
            middleware.append(
                SelectedSkillsMiddleware(
                    backend=backend,
                    sources=["/"],
                    skill_name=args.skill_name,
                )
            )
        agent = create_deep_agent(
            name="requirements-evidence-agent",
            model=model,
            tools=tools,
            backend=backend,
            middleware=middleware,
            system_prompt=(
                "사용자의 요청을 한국어로 처리한다. 관찰한 결과와 확인하지 못한 내용을 구분한다. "
                "사용자가 제시한 목표·제약·완료 기준을 임의로 추가하거나 바꾸지 않는다."
            ),
        )
        await stream_agent(agent, args, emit)


async def stream_agent(agent, args, emit) -> None:
    # checkpointer와 이전 messages를 주입하지 않는다. 재개는 근거 파일로 한다.
    async for update in agent.astream(
        {"messages": [{"role": "user", "content": args.prompt}]},
        config={"recursion_limit": 24},
        stream_mode="updates",
    ):
        for values in update.values():
            if not isinstance(values, dict):
                continue
            if "skills_metadata" in values:
                names = [skill["name"] for skill in values["skills_metadata"]]
                emit("skills_discovered", names=names)
                print("[발견한 Skill · 사용 기록 아님]", ", ".join(names), flush=True)
            for message in values.get("messages", []):
                text = message_text(message.content)
                calls = getattr(message, "tool_calls", [])
                # 공개된 대화·도구 결과만 기록한다. 키·환경변수·공급자 응답 메타데이터는 제외한다.
                emit(
                    "message",
                    role=message.type,
                    content=text,
                    tool_calls=calls,
                    name=getattr(message, "name", None),
                    tool_call_id=getattr(message, "tool_call_id", None),
                    status=getattr(message, "status", None),
                    usage=getattr(message, "usage_metadata", None),
                )
                for call in calls:
                    print(
                        f"[호출] {call['name']} {json.dumps(call['args'], ensure_ascii=False)}",
                        flush=True,
                    )
                if message.type == "tool":
                    print(f"[결과] {message.name}: {text}", flush=True)
                elif message.type == "ai" and text and not calls:
                    print(f"\n[답변]\n{text}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--mode", choices=("mcp", "scripts"), default="mcp")
    parser.add_argument(
        "--skill-name", help="발견할 스킬 이름 (기본: 실행 모드에 맞는 수업 스킬)"
    )
    parser.add_argument("--server", type=Path, default=LAB / "reference" / "server.py")
    parser.add_argument("--skills-dir", type=Path, default=LAB / "skills")
    parser.add_argument(
        "--db", type=Path, default=LAB / "reference" / "work" / "sessions.sqlite3"
    )
    parser.add_argument("--evidence-dir", type=Path, default=DAY04 / "evidence")
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="Skill 자동 탐색을 끈다 (SKILL.md를 create_deep_agent에 등록하지 않음).",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        help="대화·호출 기록 JSONL (기본: evidence/_runs/<실행 ID>.jsonl)",
    )
    args = parser.parse_args()
    args.skill_name = args.skill_name or (
        "requirements-interview-new"
        if args.mode == "scripts"
        else "requirements-interview"
    )
    if Path(args.skill_name).name != args.skill_name or args.skill_name in (".", ".."):
        parser.error("--skill-name에는 스킬 디렉터리 이름을 지정하세요.")
    for field in ("server", "skills_dir", "db", "evidence_dir"):
        setattr(args, field, getattr(args, field).resolve())
    if args.mode == "mcp" and not args.server.is_file():
        parser.error("서버 파일을 확인하세요.")
    if not args.skills_dir.is_dir():
        parser.error("Skill 폴더를 확인하세요.")
    if (
        not args.no_skills
        and not (args.skills_dir / args.skill_name / "SKILL.md").is_file()
    ):
        parser.error("선택한 스킬의 SKILL.md를 확인하세요.")
    run_id = uuid4().hex
    trace_path = (
        args.trace or args.evidence_dir / "_runs" / f"{run_id}.jsonl"
    ).resolve()
    args.trace_path = trace_path
    if trace_path.exists():
        parser.error(
            "이미 있는 실행 기록은 덮어쓰지 않습니다. 새 --trace 경로를 지정하세요."
        )
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("x", encoding="utf-8") as trace:

        def emit(kind, **data):
            record = {"kind": kind, "at": datetime.now(UTC).isoformat(), **data}
            trace.write(json.dumps(record, ensure_ascii=False) + "\n")
            trace.flush()  # 실패해도 그때까지 받은 기록은 남긴다.

        emit(
            "started",
            run_id=run_id,
            agent_pid=os.getpid(),
            prompt=args.prompt,
            mode=args.mode,
            skill_name=args.skill_name,
            server=str(args.server) if args.mode == "mcp" else None,
            skills_dir=str(args.skills_dir),
            db=str(args.db),
            evidence_dir=str(args.evidence_dir),
        )
        print(f"[실행 기록] {trace_path}", flush=True)
        print(
            "[비용] 실제 모델을 여러 번 호출합니다. 설정한 계정의 사용량이 발생합니다.",
            flush=True,
        )
        try:
            asyncio.run(run(args, emit))
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI는 실패 기록과 종료 코드 1을 남긴다.
            # 공급자가 오류 메시지에 키 일부를 돌려줄 수 있어 원문 예외는 저장하지 않는다.
            emit("failed", error_type=type(error).__name__)
            print(
                f"[실패] {type(error).__name__}: 실행 기록과 모델·실행 모드 설정을 확인하세요.",
                file=sys.stderr,
            )
            return 1
        emit("completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
