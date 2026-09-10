"""스킬을 발견하고 범용 execute만 제공하는 실행 환경. 업무 코드는 가져오지 않는다."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents import FilesystemMiddleware, create_deep_agent
from deepagents.backends import LocalShellBackend
from deepagents.middleware.skills import SkillsMiddleware


class SelectedSkillsMiddleware(SkillsMiddleware):
    """수업 비교용 스킬 중 이번 실행에서 선택한 스킬의 메타데이터만 공개한다."""

    def __init__(self, *, skill_name: str, **kwargs):
        super().__init__(**kwargs)
        self.skill_name = skill_name

    def _select(self, update):
        if update is not None:
            update["skills_metadata"] = [
                skill
                for skill in update["skills_metadata"]
                if skill["name"] == self.skill_name
            ]
        return update

    def before_agent(self, state, runtime, config):
        return self._select(super().before_agent(state, runtime, config))

    async def abefore_agent(self, state, runtime, config):
        return self._select(await super().abefore_agent(state, runtime, config))


class RecordedShellBackend(LocalShellBackend):
    """명령 결과를 모델에 돌려주기 전에 독립된 근거 파일에 남긴다."""

    def __init__(self, *, command_dir: Path, emit, **kwargs):
        super().__init__(**kwargs)
        self.command_dir = command_dir
        self.emit = emit

    def execute(self, command: str, *, timeout: int | None = None):
        started = datetime.now(UTC).isoformat()
        result = super().execute(command, timeout=timeout)
        self.command_dir.mkdir(parents=True, exist_ok=True)
        path = self.command_dir / f"{uuid4().hex}.json"
        record = {
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "cwd": str(self.cwd),
            "command": command,
            "exit_code": result.exit_code,
            "output": result.output,
            "truncated": result.truncated,
        }
        with path.open("x", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
        self.emit("execution", path=str(path), **record)
        # 파일에는 원래 결과를 보존하고 모델에는 기록 위치도 알려 준다.
        result.output += f"\n[명령 기록] {path}"
        return result


SKILL_PROMPT = """사용 가능한 스킬:
{skills_locations}{skills_load_warnings}
{skills_list}

요청에 맞는 스킬의 SKILL.md를 execute로 읽고 사용법을 따른다.
목록에 표시된 SKILL.md의 실제 경로를 그대로 cat에 전달해 본문 전체를 읽는다.
SKILL_ROOT는 이미 선택한 스킬 디렉터리다. Python 실행 시에는 본문에 적힌 경로 변수를 사용한다.
read_file이나 업무별 도구는 제공되지 않는다. 파일 읽기도 execute를 사용한다.
스크립트는 사용법에 따라 실행한다. 정상 사용을 위해 구현 소스를 읽을 필요는 없다.
"""


def create_script_agent(model, args, emit):
    day04 = Path(__file__).resolve().parents[1]
    skill_root = args.skills_dir / args.skill_name
    backend = RecordedShellBackend(
        root_dir=day04,
        virtual_mode=False,  # 메타데이터와 셸에서 같은 실제 경로를 사용한다.
        command_dir=args.trace_path.with_suffix(".commands"),
        emit=emit,
        timeout=60,
        inherit_env=False,
        env={
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "SKILL_ROOT": str(skill_root),
            "SKILL_DB": str(args.db),
            "SKILL_EVIDENCE_DIR": str(args.evidence_dir),
        },
    )
    middleware = [
        FilesystemMiddleware(
            backend=backend,
            # SDK가 read_file을 요구한다. 모델에서는 HarnessProfile로 제외한다.
            tools=["read_file", "execute"],
            max_execute_timeout=60,
            system_prompt="파일 읽기와 프로그램 실행은 execute로 한다. Python과 경로 설정은 실행 환경에 준비돼 있다.",
        )
    ]
    if not args.no_skills:
        middleware.append(
            SelectedSkillsMiddleware(
                backend=backend,
                sources=[str(args.skills_dir)],
                skill_name=args.skill_name,
                system_prompt=SKILL_PROMPT,
            )
        )
    return create_deep_agent(
        name="requirements-script-agent",
        model=model,
        backend=backend,
        middleware=middleware,
        system_prompt=(
            "사용자의 요청을 한국어로 처리한다. 사용자가 제시한 목표·제약·완료 기준을 임의로 바꾸지 않는다. "
            "관찰한 실행 결과와 확인하지 못한 내용을 구분한다. "
            "execute는 현재 컴퓨터의 로컬 셸이며 작업 폴더는 Day04다. "
            "파일·업무 처리는 스킬에 문서화된 방법을 따른다. .env나 키·설정 비밀은 읽거나 출력하지 않는다. "
            "환경변수 전체를 출력하지 않는다. 제공된 SKILL_ROOT, SKILL_DB, SKILL_EVIDENCE_DIR만 경로로 사용한다."
        ),
    )
