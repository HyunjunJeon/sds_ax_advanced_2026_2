"""새 번호 수업의 진입점을 안내한다. 모델·외부 호출 없음."""

from pathlib import Path


def main():
    print("Day05 폴더에서 아래 번호 순서로 진행하세요. 실행 조건은 course_config.py에 있습니다.")
    for path in sorted(Path(__file__).resolve().parents[1].glob("[0-9][0-9]_*.py")):
        print(f"uv run python {path.name}")
    print("검수·가설·사람 판정은 각 단계에서 직접 작성합니다. README.md를 함께 읽으세요.")


if __name__ == "__main__":
    main()
