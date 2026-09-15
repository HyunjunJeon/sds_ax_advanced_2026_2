"""
읽는 순서: config → context → workspace/outbox/tools → trace → guards/ → defenses → components → evaluate → runner.
번호 파일은 이 패키지를 '조립' 만 한다. 방어 논리(defenses.py)와 부품(components.py)이 실제 코드다.

주요 내용:
Day08 공통 고정물. 모든 번호 파일이 똑같이 써야 하는 것만 여기에 둔다.
가상 업무 데이터, 원시 도구, 전송함, 실행 컨텍스트, Guard 계약·클라이언트, 독립 평가기,
사례 목록, 재생 드라이버, 예산. Agent 조립(`create_deep_agent`)과 Guard 배치는 각 번호
파일 안에 두어 diff로 보이게 한다.
"""

import warnings as _warnings

# ToolRuntime 직렬화 시 context 필드가 None 이 아니라는 pydantic 경고. 동작에는 영향이 없다.
_warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
