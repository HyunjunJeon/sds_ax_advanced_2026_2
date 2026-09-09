"""실습 원본을 Day04/work 아래 새 작업 폴더에 복사한다. 모델은 호출하지 않는다."""

import argparse
from pathlib import Path

from run_files import copy_project

LAB = Path(__file__).resolve().parent
DAY04 = LAB.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DAY04 / "work" / "coding")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_relative_to(DAY04 / "work") or workspace == DAY04 / "work":
        parser.error("--workspace는 Day04/work 아래의 새 폴더로 지정하세요.")
    if workspace.exists():
        parser.error("이미 있는 작업은 덮어쓰지 않습니다. 새 --workspace를 지정하세요.")
    copy_project(LAB / "project", workspace)
    print(f"[작업 사본] {workspace}")
    print("기존 테스트 6개 중 기간 경계 검사 1개가 실패하는 것이 초기 상태입니다.")


if __name__ == "__main__":
    main()
