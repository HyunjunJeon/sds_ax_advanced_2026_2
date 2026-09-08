"""과제 실행기는 미구현 상태를 숨기지 않고 기본 예제의 회귀검사와 분리한다.

가르치는 것:
- 테스트 주도 실습 흐름: 과제 파일을 실행하면 안내문과 함께 공개 테스트가 바로
  돌아가므로, 학생은 실패(failing) 상태에서 시작해 계약을 채우며 초록으로 만든다.
- 회귀와 과제의 분리: tests/는 제공 코드의 검사로 항상 통과해야 하고,
  challenge_tests/는 학생 구현 검사로 구현 전 실패가 정상이다. 두 묶음을 섞으면
  "예제가 깨졌는지 과제가 미완인지" 구별할 수 없다.
"""

from .config import DAY03


def run_challenge(title, description, test_file):
    print(title)
    print(description)
    print()
    print("구현 위치: student_tasks.py(알고리즘 과제) 또는 각 실습 파일(그래프 과제)")
    print("제출 기준: WORKSHEET.md 해당 과제 섹션")
    print()
    # pytest는 dev 의존성이다. 기본 uv sync에 포함되며 모델/EXA를 호출하지 않는다.
    import pytest

    print("아래에 이 과제의 테스트 결과가 출력됩니다. 미구현 실패가 정상입니다.")
    raise SystemExit(pytest.main([str(DAY03 / "challenge_tests" / test_file), "-q"]))
