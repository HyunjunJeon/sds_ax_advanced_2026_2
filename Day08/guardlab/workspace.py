"""
사용: ws = Workspace.create(run_dir, case.doc_overrides); ws.backend() → create_deep_agent(backend=...)
포인트:
  1. 실행마다 data/base 를 새로 복사한다. 비교군이 달라도 초기 상태가 같아야 방어 효과만 분리된다.
  2. 공격자는 doc_overrides 로 문서 본문 하나만 바꾼다. 정책·도구·컨텍스트는 바꾸지 못한다 (위협 모델).
  3. 정책 파일 3개의 해시를 생성 시점에 기록해 두고 changed_policy_paths() 로 변조를 판정한다.
  4. FilesystemBackend(virtual_mode=True) 라 내장 파일 도구의 "/" 가 이 폴더다. LocalShellBackend 는 쓰지 않는다.

주요 내용:
실행별 작업 공간. data/base 를 복사하고 사례의 공격 문서로 일부를 바꾼다.
가상 경로(`/projects/alpha/tests.md`)는 DeepAgents 내장 파일 도구가 보는 경로다.
`FilesystemBackend(root_dir=..., virtual_mode=True)` 가 이 폴더를 루트로 삼는다.
정책 파일 3개의 해시를 생성 시점에 기록해 두고, 평가기가 변경 여부를 대조한다.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .config import DATA

POLICY_PATHS = ("/policy/rules.md", "/policy/reviewers.json", "/memory/AGENTS.md")


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.initial_policy = self.policy_snapshot()

    # ── 생성 ────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, run_dir: Path, overrides: dict[str, str] | None = None) -> "Workspace":
        """run_dir 를 비우고 base 를 복사한 뒤 overrides 의 문서만 공격 문서로 바꾼다.

        overrides: {"/projects/alpha/meeting.md": "meeting_send_outside.md"} 형식.
        공격자는 문서 본문 하나만 바꿀 수 있다. 정책·도구·컨텍스트는 바꾸지 못한다.
        """
        run_dir = Path(run_dir)
        if run_dir.exists():
            shutil.rmtree(run_dir)
        shutil.copytree(DATA / "base", run_dir)
        for vpath, attack_file in (overrides or {}).items():
            src = DATA / "attacks" / attack_file
            if not src.exists():
                raise FileNotFoundError(f"공격 문서 없음: {src}")
            target = run_dir / vpath.lstrip("/")
            if not target.exists():
                raise FileNotFoundError(f"덮어쓸 원본 문서 없음: {vpath}")
            shutil.copyfile(src, target)
        return cls(run_dir)

    def backend(self):
        from deepagents.backends import FilesystemBackend

        return FilesystemBackend(root_dir=self.root, virtual_mode=True)

    # ── 파일 접근 ───────────────────────────────────────────────────────
    def path(self, vpath: str) -> Path:
        p = (self.root / vpath.lstrip("/")).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise PermissionError(f"작업 공간 밖 경로: {vpath}")
        return p

    def exists(self, vpath: str) -> bool:
        return self.path(vpath).exists()

    def read(self, vpath: str) -> str:
        return self.path(vpath).read_text(encoding="utf-8")

    def write(self, vpath: str, text: str) -> None:
        p = self.path(vpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def list_docs(self, project: str) -> list[str]:
        d = self.root / "projects" / project
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_file())

    def projects(self) -> list[str]:
        return sorted(p.name for p in (self.root / "projects").iterdir() if p.is_dir())

    # ── 초안 ────────────────────────────────────────────────────────────
    def drafts(self) -> list[tuple[str, str]]:
        d = self.root / "drafts"
        out = []
        for p in sorted(d.glob("*.md")):
            out.append(("/drafts/" + p.name, p.read_text(encoding="utf-8")))
        return out

    def next_version(self, project: str) -> int:
        d = self.root / "drafts"
        existing = [p for p in d.glob(f"{project}_weekly_v*.md")]
        return len(existing) + 1

    # ── 정책 무결성 ─────────────────────────────────────────────────────
    def policy_snapshot(self) -> dict[str, str]:
        snap = {}
        for vpath in POLICY_PATHS:
            p = self.root / vpath.lstrip("/")
            snap[vpath] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "<missing>"
        return snap

    def changed_policy_paths(self) -> list[str]:
        now = self.policy_snapshot()
        return [k for k in POLICY_PATHS if now[k] != self.initial_policy[k]]
