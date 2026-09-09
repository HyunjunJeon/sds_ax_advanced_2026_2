# Agent Skills for Large Language Models 요약

Renjun Xu (ReDiscovery), Yang Yan (Westlake University).
arXiv:2602.12430v4, 2026-06-02. AgentSkills ’26 (ACM CAIS 2026 Workshop).
원문: [Agent_Skills_LLM_2602.12430.pdf](Agent_Skills_LLM_2602.12430.pdf)

프롬프트는 일시적이고, 도구는 한 번의 호출이다. RAG가 가져온 문장은 다단계 절차를 규정하거나 실행 코드를 묶거나 런타임 권한을 바꾸지 못한다. Agent Skill은 디렉터리 패키지다. `SKILL.md`, 선택적 스크립트·참고 문서·자산을 담고, 에이전트가 필요할 때 읽어 절차 지식을 넣는다. 도구는 실행하고 결과를 반환한다. Skill은 답을 내기 **전에** 무엇을 알고 할 수 있는지를 바꾼다.

Anthropic이 2025-10에 제품에 넣고, 2025-12에 공개 표준으로 냈다. 이 서베이는 Skill 층을 네 축으로 정리한다. 아키텍처, 습득, 배포, 보안.

## 아키텍처

Progressive disclosure는 세 단계다 (Figure 1).

| 단계 | 언제 올라가는가 | 내용 |
|---|---|---|
| Level 1 메타데이터 | 항상 | YAML `name`, `description`. Skill당 대략 수십 토큰 |
| Level 2 지침 | 트리거 시 | `SKILL.md` 본문. 대략 200–2k 토큰 |
| Level 3 자원 | 본문이 요청할 때 | `scripts/`, 참고 문서, 템플릿. 상한 없음 |

요청이 설명과 맞으면 지침이 숨은 메시지로 들어가고, 미리 승인한 도구·권한이 열린다. Skill은 출력이 아니라 **준비**를 바꾼다.

Skill과 MCP는 경쟁이 아니라 층이 다르다 (Table 1). Skill은 「무엇을 할지」(절차), MCP는 「어디에 연결할지」(도구·데이터). Skill이 특정 MCP 서버를 쓰라고 하고, 실패 시 우회를 적을 수 있다.

## 습득

네 갈래다 (Table 2).

- **사람이 씀.** 가장 바로 쓰이는 경로. `skill-creator`가 디렉터리와 `SKILL.md`를 골조로 만들 수 있다.
- **Skill 라이브러리 + RL (SAGE).** Sequential rollout으로 앞 과제의 Skill을 뒤에 재사용한다. AppWorld에서 GRPO 대비 Scenario Goal Completion +8.9%, 생성 토큰 −59%.
- **자율 탐색 (SEAgent).** OSWorld 신규 소프트웨어 5개에서 성공률 11.3% → 34.5%.
- **구조화·합성.** CUA-Skill은 실행 그래프. Agentic Proposing은 Skill을 조합한다. Li (2026)는 멀티에이전트를 단일 에이전트 Skill 라이브러리로 「컴파일」할 수 있다고 보고하고, 라이브러리가 커지면 선택 정확도가 급히 떨어지는 상전이도 적는다.

## 보안

Skill은 자연어 지침과 실행 코드를 한 패키지에 넣고, 로드되면 권위 있는 맥락으로 취급된다.

- Schmotz et al.: `SKILL.md`와 참조 스크립트에 넣은 지시로 프롬프트 인젝션이 「아주 단순」해진다. 「다시 묻지 않음」승인이 해로운 행동으로 넘어갈 수 있다.
- Liu et al.: 마켓플레이스 Skill 42,447개 중 31,132개를 분석. **26.1%**가 취약점을 하나 이상 갖는다. 데이터 유출 13.3%, 권한 상승 11.8%. 스크립트를 묶은 Skill은 지침만 있는 Skill보다 취약할 오즈가 2.12배. 5.2%는 악의로 볼 수 있는 고심각 패턴이다.

저자들은 Skill Trust and Lifecycle Governance Framework를 제안한다 (Figure 3). 검증 게이트 G1–G4(정적 분석, 의도 분류, 샌드박스, 권한 매니페스트)와 신뢰 티어 T1–T4(지침만 / 읽기 전용 / 선언된 도구 / 전체). T1·T2에는 스크립트 실행을 주지 않는다. 런타임 이상이면 강등, 이력이 깨끗하면 승격한다. 경험적으로 검증된 시스템이 아니라, 신뢰 가정을 명시하는 제안이다.

## 열린 문제

1. 플랫폼 간 이식 (Claude 전제에 묶인 Skill)
2. 라이브러리가 커질 때 선택
3. 여러 Skill의 조합·충돌·실패 복구
4. 능력 기반 권한 (로드되면 모든 도구를 쓰게 하는 암묵 신뢰)
5. Skill 전용 검증·테스트
6. 새 Skill을 들이면서 기존 능력을 잃지 않기
7. 과제 성공이 아니라 Skill 품질(재사용·조합·유지)을 재는 평가
