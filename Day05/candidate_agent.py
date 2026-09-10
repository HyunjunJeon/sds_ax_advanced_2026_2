"""07에서 수정할 Agent 설정. 04에서 근거·가설·한 가지 변경을 먼저 기록한다.

초기값은 baseline과 같다. PROMPT를 수정하면 새 버전으로 기록된다.
정책·fixture·평가 정답·호출 예산을 바꿔 개선을 만들지 않는다.
"""

from business_lab.agents import COMMON_PROMPT

PROMPT = COMMON_PROMPT
