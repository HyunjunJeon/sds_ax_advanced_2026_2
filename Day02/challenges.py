"""과제 실행기는 미구현 상태를 숨기지 않고 기본 회귀검사와 분리한다.

각 과제 파일(07/08/10/11/12)을 실행하면 안내문을 출력한 뒤 해당 과제의 공개 테스트를
바로 실행한다. 미구현 상태에서 실패가 나오는 것이 정상이며, 예외를 지우는 방식이
아니라 계약을 구현하는 방식으로 통과시킨다. 기본 회귀검사(tests/)는 과제 구현 여부와
무관하게 항상 통과해야 한다.
"""
from pathlib import Path


def run_challenge(title, description, test_file):
    print(title)
    print(description)
    print()
    print("구현 위치: student_tasks.py")
    print("제출 기준: WORKSHEET.md 해당 과제 섹션")
    print()
    # pytest는 dev 의존성이다. 기본 uv sync에 포함되며 모델 API를 호출하지 않는다.
    import pytest

    print("아래에 이 과제의 테스트 결과가 출력됩니다. 미구현 실패가 정상입니다.")
    raise SystemExit(pytest.main([str(Path(__file__).parent / "challenge_tests" / test_file), "-q"]))
