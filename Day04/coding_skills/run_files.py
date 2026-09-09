"""실행 전후 파일, 공개 대화, 실제 셸 실행 결과를 근거로 남긴다."""

from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from deepagents.backends import LocalShellBackend

IGNORED = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def project_files(root: Path) -> dict[str, bytes]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED or part.startswith(".env") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("실습 프로젝트에는 심볼릭 링크를 사용하지 않습니다.")
        if path.is_file():
            result[relative.as_posix()] = path.read_bytes()
    return result


def diff_files(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    chunks = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        old = before.get(name, b"").decode("utf-8", errors="replace").splitlines(True)
        new = after.get(name, b"").decode("utf-8", errors="replace").splitlines(True)
        # 마지막 개행이 없는 파일도 다음 diff 헤더와 붙지 않게 표시한다.
        for line in difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{name}" if name in before else "/dev/null",
            tofile=f"b/{name}" if name in after else "/dev/null",
        ):
            chunks.append(
                line
                if line.endswith("\n")
                else line + "\n\\ No newline at end of file\n"
            )
    return "".join(chunks)


def save_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]


class RunFiles:
    def __init__(self, root: Path, workspace: Path, skills: Path):
        root.mkdir(parents=True, exist_ok=False)
        self.root = root
        self.workspace = workspace
        self.before = project_files(workspace)
        self._snapshot("before", self.before)
        # 수정한 Skill로 수행한 실험을 나중에 그대로 대조할 수 있게 보존한다.
        self._snapshot("skills", project_files(skills))
        (root / "commands").mkdir()
        (root / "artifacts").mkdir()
        self.trace = (root / "trace.jsonl").open("x", encoding="utf-8")

    def _snapshot(self, name: str, files: dict[str, bytes]) -> None:
        for relative, content in files.items():
            target = self.root / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def emit(self, kind: str, **data) -> None:
        self.trace.write(
            json.dumps(
                {"kind": kind, "at": datetime.now(UTC).isoformat(), **data},
                ensure_ascii=False,
            )
            + "\n"
        )
        self.trace.flush()

    def diff(self) -> str:
        return diff_files(self.before, project_files(self.workspace))

    def finish(self) -> None:
        try:
            after = project_files(self.workspace)
            self._snapshot("after", after)
            (self.root / "changes.patch").write_text(
                diff_files(self.before, after), encoding="utf-8"
            )
            save_json(
                self.root / "files.json",
                {
                    "workspace": str(self.workspace),
                    "before": {
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in self.before.items()
                    },
                    "after": {
                        name: hashlib.sha256(data).hexdigest()
                        for name, data in after.items()
                    },
                },
            )
        finally:
            self.trace.close()


class RecordedLocalShellBackend(LocalShellBackend):
    """기본 execute를 그대로 실행하고 반환값을 파일에 추가한다."""

    def __init__(self, *args, command_dir: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_dir = command_dir

    def execute(self, command: str, *, timeout: int | None = None):
        started = datetime.now(UTC).isoformat()
        result = super().execute(command, timeout=timeout)
        save_json(
            self.command_dir / f"{uuid4().hex}.json",
            {
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "cwd": str(self.cwd),
                "command": command,
                "exit_code": result.exit_code,
                "output": result.output,
                "truncated": result.truncated,
            },
        )
        return result


def copy_project(source: Path, target: Path) -> None:
    """이미 있는 작업을 덮어쓰지 않고 새 프로젝트 사본을 만든다."""
    if target.exists():
        raise FileExistsError(f"이미 있는 작업 폴더입니다: {target}")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED, ".env*"))
