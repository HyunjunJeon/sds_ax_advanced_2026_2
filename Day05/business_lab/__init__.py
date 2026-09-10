"""주문 취소·결제·재고를 대상으로 하는 Agent Evaluation 실습.

실행 환경은 평가 정답을 읽지 않는다. 새 수업은 루트의 00~09 순서로 진행하며,
실제 모델·Langfuse 실행 조건은 course_config.py에서 선택한다.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
