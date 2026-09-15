"""주석만 추가됐는지 검증한다.

기준 커밋(baseline-1d3232c)의 파일과 작업 트리 파일을 각각 AST로 파싱해 비교한다.
`#` 주석은 AST에 나타나지 않으므로, 두 AST가 같으면 실행 의미가 바뀌지 않았다는 뜻이다.
docstring 수정·코드 변경·문법 오류는 모두 실패로 잡힌다.

사용법 (dcode-analysis/ 에서):
    python3 scripts/check_comments_only.py libs/code/deepagents_code/agent.py [...]
    python3 scripts/check_comments_only.py --changed   # baseline 대비 바뀐 .py 전부
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent / "deepagents"
BASELINE = "baseline-1d3232c"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout


def changed_files() -> list[str]:
    # 클론 시 .github/.githooks가 체크아웃되지 않아 삭제로 보이므로 dcode 소스로 범위를 한정한다.
    out = _git("diff", "--name-only", BASELINE, "--", "libs/code/deepagents_code/*.py")
    return [line for line in out.splitlines() if line]


def check(rel: str) -> str | None:
    """문제가 없으면 None, 있으면 사유 문자열을 반환한다."""
    try:
        old_src = _git("show", f"{BASELINE}:{rel}")
    except subprocess.CalledProcessError:
        return "기준 커밋에 없는 파일 (새 파일은 허용하지 않음)"
    new_path = REPO / rel
    if not new_path.exists():
        return "작업 트리에서 파일이 사라짐"
    new_src = new_path.read_text(encoding="utf-8")
    try:
        new_tree = ast.parse(new_src)
    except SyntaxError as exc:
        return f"문법 오류: {exc.msg} (line {exc.lineno})"
    old_dump = ast.dump(ast.parse(old_src))
    new_dump = ast.dump(new_tree)
    if old_dump != new_dump:
        return "AST 불일치 — 주석 외 변경(코드·docstring·문자열)이 있음"
    return None


def main(argv: list[str]) -> int:
    targets = changed_files() if argv == ["--changed"] else argv
    if not targets:
        print("검사할 파일 없음")
        return 0
    failed = 0
    for rel in targets:
        rel = rel.removeprefix("deepagents/")
        reason = check(rel)
        if reason:
            failed += 1
            print(f"FAIL {rel}: {reason}")
        else:
            print(f"OK   {rel}")
    print(f"\n{len(targets) - failed}/{len(targets)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
