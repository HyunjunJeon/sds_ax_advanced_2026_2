"""Skill을 선택해 실제 Python 파일을 수정하고 셸에서 검사하는 Deep Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, FilesystemBackend
from langchain_core.tools import tool
from llm import chat_model
from run_files import RecordedLocalShellBackend, RunFiles, new_run_id

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


async def run(args, files: RunFiles) -> None:
    model = chat_model(temperature=0, timeout=60)
    register_harness_profile(
        f"openrouter:{model.model_name}",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
        ),
    )
    # 파일 도구의 가상 경로와 실제 셸은 다르다. 이 백엔드는 OS 샌드박스가 아니다.
    shell = RecordedLocalShellBackend(
        root_dir=args.workspace,
        virtual_mode=True,
        timeout=60,
        command_dir=files.root / "commands",
        inherit_env=False,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    backend = CompositeBackend(
        default=shell,
        routes={
            "/skills/": FilesystemBackend(root_dir=args.skills_dir, virtual_mode=True),
            "/artifacts/": FilesystemBackend(
                root_dir=files.root / "artifacts", virtual_mode=True
            ),
        },
    )

    @tool
    def show_diff() -> str:
        """이번 실행 시작 시점부터의 프로젝트 변경을 unified diff로 반환한다."""
        return files.diff() or "프로젝트 파일 변경 없음."

    agent = create_deep_agent(
        name="coding-skills-agent",
        model=model,
        backend=backend,
        tools=[show_diff],
        skills=["/skills/"],
        memory=["/AGENTS.md"],
        permissions=[
            FilesystemPermission(
                operations=["write"],
                paths=["/skills/**"],
                mode="deny",
            ),
        ],
        system_prompt=(
            "사용자의 개발 요청을 한국어로 처리한다. 실제 파일과 실행 결과를 근거로 답한다. "
            "준비된 프로젝트만 작업하고 외부 폴더·환경변수·네트워크는 사용하지 않는다. "
            "파일 도구에서 /는 프로젝트, /skills/는 Skill, /artifacts/는 보고서 폴더다. "
            "execute는 프로젝트를 현재 디렉터리로 실행한다. 셸에서는 프로젝트 상대 경로를 쓰고, "
            "Skill 읽기와 보고서 저장에는 파일 도구의 가상 경로를 쓴다. "
            "일반 개념 질문에는 프로젝트 조사나 수정 없이 답한다."
        ),
    )
    files.emit(
        "connected",
        model=model.model_name,
        model_provider="openrouter",
        model_class=type(model).__name__,
    )
    async for update in agent.astream(
        {"messages": [{"role": "user", "content": args.prompt}]},
        config={"recursion_limit": 32},
        stream_mode="updates",
    ):
        for values in update.values():
            if not isinstance(values, dict):
                continue
            if "skills_metadata" in values:
                names = [skill["name"] for skill in values["skills_metadata"]]
                files.emit("skills_discovered", names=names)
                print("[발견한 Skill · 사용 기록 아님]", ", ".join(names), flush=True)
            for message in values.get("messages", []):
                text = message_text(message.content)
                calls = getattr(message, "tool_calls", [])
                files.emit(
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


async def run_with_deadline(args, files: RunFiles) -> None:
    # 외부 검사기의 강제 종료(240초) 전에 실패 기록과 파일 사본을 저장한다.
    async with asyncio.timeout(210):
        await run(args, files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--workspace", type=Path, default=DAY04 / "work" / "coding")
    parser.add_argument("--skills-dir", type=Path, default=LAB / "skills")
    parser.add_argument(
        "--run-dir", type=Path, help="새 근거 폴더 (기본: evidence/coding/<실행 ID>)"
    )
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    args.skills_dir = args.skills_dir.resolve()
    args.run_dir = (
        args.run_dir or DAY04 / "evidence" / "coding" / new_run_id()
    ).resolve()
    if (
        not args.workspace.is_relative_to(DAY04 / "work")
        or args.workspace == DAY04 / "work"
    ):
        parser.error("작업 폴더는 Day04/work 아래에 준비하세요.")
    if (
        not (args.workspace / "AGENTS.md").is_file()
        or not (args.workspace / "csv_report").is_dir()
    ):
        parser.error("coding_skills/prepare.py로 작업 사본을 먼저 준비하세요.")
    if not args.skills_dir.is_dir():
        parser.error("Skill 폴더를 확인하세요.")
    if args.run_dir.exists() or args.run_dir.is_relative_to(args.workspace):
        parser.error("프로젝트 밖의 새로운 --run-dir를 지정하세요.")
    files = RunFiles(args.run_dir, args.workspace, args.skills_dir)
    files.emit(
        "started",
        agent_pid=os.getpid(),
        prompt=args.prompt,
        workspace=str(args.workspace),
        skills_dir=str(args.skills_dir),
    )
    print(f"[근거 폴더] {files.root}", flush=True)
    print(
        "[비용] 실제 모델을 여러 번 호출합니다. 설정한 계정의 사용량이 발생합니다.",
        flush=True,
    )
    code = 0
    try:
        asyncio.run(run_with_deadline(args, files))
        files.emit("completed")
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - CLI 실패를 기록하고 비정상 종료한다.
        files.emit("failed", error_type=type(error).__name__)
        print(
            f"[실패] {type(error).__name__}: 실행 기록과 설정을 확인하세요.",
            file=sys.stderr,
        )
        code = 1
    finally:
        files.finish()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
