
# [공격 - 방어] 시뮬레이션 (RedTeaming)

- 공격자: 강사의 Claude Code
  > 공격 유형: 입력 교란
  > Input Data 를 여러 버전으로 넣도록 지시.

1) 기본 형태의 DeepAgents 를 만들어주세요.
   - 처음에는 Input Guard 를 만들지 마세요.

2) 제공한 데이터를 실행하여 공격과 방어가 되는지 검증하세요.
   > 데이터 경로: guardlab/data/attacks/redteam.jsonl
3) 2번의 공격이 방어되는지 확인하세요.


- 방어자: 여러분의 에이전트(by DeepAgents) 만들기
  > Model 기반 Guard 채택
   - 모델 정보는 .env.example 을 참고.
  
  > 만약, 모델이 가드를 잘 못한다면 어떻게 해야할까요?
