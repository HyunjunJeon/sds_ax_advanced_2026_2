# WikiSkill 요약

Liyan Tang, Cyrus Rashtchian, Chun-Sung Ferng, Andrew Tomkins, Da-Cheng Juan, Tu Vu.
Google Research · Virginia Tech. arXiv:2608.27454v1, 2026-08-27.
원문: [WikiSkill_2608.27454.pdf](WikiSkill_2608.27454.pdf)

Agent Skill은 모델 가중치를 바꾸지 않고, 디렉터리에 담은 지침·스크립트·자원으로 에이전트 능력을 확장한다. 최근 연구는 과제를 실행한 뒤 성공·실패 궤적을 보고 Skill을 자동으로 고친다. 다만 그 과정에서 얻은 통찰은 최적화 이력에 흩어져, 다음 반복이 체계적으로 재사용하기 어렵다.

WikiSkill은 실행 가능한 Skill과 별도로, 경험을 컴파일하는 지속 지식 베이스(wiki)를 같이 진화시킨다. 원문 실행 경험, 축적된 지식, 실행 절차를 나누고, 경험은 위키로 모은 뒤 이후 Skill 수정이 그 위키 위에 쌓이게 한다.

## 방법

워크스페이스는 세 층이다 (Figure 2, §3.1).

| 층 | 디렉터리 | 내용 |
|---|---|---|
| Raw | `raw/` | 추론·도구 호출·결과·최종 답을 담은 실행 궤적. 불변 |
| Wiki | `wiki/` | 실패·성공 패턴, 진화 로그, Skill 제안의 수락/거절 기록. 반복마다 쌓이며 되돌리지 않음 |
| Skill | `skills/` | Inference Agent가 읽는 절차. 검증 점수가 떨어지면 되돌림 |

위키는 `patterns/` 페이지, 목록 `index.md`, Maintainer가 쓰는 `logs.md`, 바깥 루프가 검증 후 쓰는 `skill-impact.md`(제안 diff·점수·Accepted/Rejected)로 구성된다. 각 Skill 디렉터리에는 실행 지침 `SKILL.md`와, 그 Skill을 만들게 한 위키 패턴을 가리키는 `PURPOSE.md`가 있다.

상태 `(S_k, W_k)`는 빈 Skill·빈 위키에서 시작한다. 한 반복은 네 단계다 (Algorithm 1).

1. **Inference Agent**가 현재 Skill로 훈련 과제를 실행한다. 활성 Skill 전문을 시스템 프롬프트에 넣는다. 실행 중에는 위키를 읽지 않는다.
2. **Wiki Maintainer**가 표본 궤적(반복당 최대 8개: 실패 ≤5, 성공 ≤3)을 보고 패턴을 만들거나 고친다.
3. **Skill Proposer**가 ReAct로 위키와 원문 궤적을 골라 읽은 뒤, Skill 하나를 만들거나 패치한다.
4. **Gating**이 후보를 검증 분할에 평가한다. 점수가 역대 최고보다 엄격히 높을 때만 Skill을 남긴다. 위키는 거절이어도 남긴다. 검증이 1.0이면 루프를 끝낸다.

비교 대상인 Trace2Skill, EvoSkill, SkillOpt도 「실행 → 궤적 분석 → Skill 수정 → 검증」은 같다. WikiSkill의 차이는 배운 내용을 Skill 문서 안이 아니라 별도 위키로 유지한다는 점이다.

## 실험

벤치마크 5개: LiveMath(수학), SealQA(웹 검색), SpreadSheet(표 조작), OfficeQA(장문 QA), ALFWorld(대화형 환경).
모델 5개: Qwen-3.5-4B/9B, Qwen-3.6-27B, Gemma-4-31B, Gemini-3.5-Flash.
진화 파이프라인을 3회 독립 실행한 테스트 평균이다 (Table 1).

| 모델 | No skill | 기존 최고 | WikiSkill |
|---|---|---|---|
| Qwen-3.5-4B | 26.2 | 35.2 (SkillOpt) | 38.5 |
| Qwen-3.5-9B | 29.9 | 42.3 (EvoSkill) | 47.4 |
| Qwen-3.6-27B | 39.4 | 53.3 (EvoSkill) | 63.3 |
| Gemma-4-31B | 41.3 | 49.1 (SkillOpt) | 54.9 |
| Gemini-3.5-Flash | 49.5 | 56.1 (EvoSkill) | 68.1 |

논문이 강조하는 결과:

- **스케일과 보완적이다.** Qwen에서 No skill 대비 이득이 4B +12.3, 9B +17.5, 27B +23.9 포인트다. 동시에 Qwen-3.5-9B + WikiSkill(47.4)이 Skill 없는 Qwen-3.6-27B(39.4)를 넘는다.
- **전이가 된다.** ALFWorld에서 Qwen-3.5-9B는 자기 Skill 63.4, Qwen-3.6-27B가 만든 Skill 70.2다 (Table 2). Skill 발견과 Skill 실행은 다른 능력으로 본다.
- **전이가 항상 이롭지는 않다.** Qwen-3.5-4B Skill을 Gemini-3.5-Flash SpreadSheet에 넣으면 50.5 → 18.1이다. 작은 모델용 우회가 큰 모델의 절차를 제약한다.
- **과제마다 이득이 다르다.** LiveMath·SpreadSheet·ALFWorld는 크고, OfficeQA에서 4B는 30.2 → 28.5로 약간 떨어진다. 긴 검색 절차를 끝까지 못 따르고 기본 읽기로 돌아간다. 같은 4B Skill을 27B가 쓰면 42.1 → 52.9로 오른다.

위키 접근 ablation (Table 3, Gemini-3.5-Flash, ALFWorld 제외 평균):

| Inference Agent | Skill Proposer | 평균 |
|---|---|---|
| — (No skill) | — | 40.4 |
| 아니오 | 아니오 | 48.7 |
| 예 | 예 | 60.9 |
| 아니오 (기본값) | 예 | 63.7 |

Proposer에게 위키를 주면 48.7 → 63.7이다. 그 상태에서 실행 에이전트에게도 위키를 주면 63.7 → 60.9로 떨어진다. 실행 중 위키에서 답을 얻으면, 그 궤적이 Skill만으로 푼 기록이 아니어서 다음 수정에 정보가 적어진다는 가설이다 (§5.1).

사례 (Figure 3, Qwen-3.6-27B × ALFWorld): 반복 0의 추상 Skill `goal-directed-action`은 거절되고, 그 기록이 위키에 남는다. 반복 1의 구체 규칙 `break-repetition-loop`(물건을 원래 자리에 되돌리지 마라)는 수락된다. 반복 4에 새 루프 패턴이 쌓인 뒤 「한 물건에 같은 조작은 한 번만」이 패치된다.

## 한계

- Skill 검색·트리거를 평가하지 않았다. 활성 Skill을 프롬프트에 넣어 품질만 본다.
- 게이트가 동점을 버린다. 당장은 점수가 그대로여도 이후 반복을 열 수정이 탈락한다.
- 위키를 줄이는 장치가 없다.
- 수백 스텝·수 시간 실행 도중에 Skill을 고치는 설정은 다루지 않는다.
