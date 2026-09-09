# SkillOpt 요약

Yifan Yang, Ziyang Gong, Weiquan Huang, Qihao Yang, Ziwei Zhou, Zisu Huang, Yan Li, Xuemei Gao, Qi Dai, Bei Liu, Kai Qiu, Yuqing Yang, Dongdong Chen, Xue Yang, Chong Luo.
Microsoft · Shanghai Jiao Tong University · Tongji University · Fudan University.
arXiv:2605.23904v2, 2026-05-25.
원문: [SkillOpt_2605.23904.pdf](SkillOpt_2605.23904.pdf)

Agent Skill은 지금 손으로 쓰거나, 한 번에 생성하거나, 느슨한 자기 수정으로 바뀐다. 어느 쪽도 딥러닝 옵티마이저처럼 동작하지 않고, 피드백 아래에서 시작점보다 안정적으로 나아간다고 보기 어렵다. SkillOpt는 Skill 문서를 동결된 에이전트의 **외부 상태**로 두고, 가중치 최적화와 같은 규율로 학습시킨다.

별도의 옵티마이저 모델이 점수 붙은 롤아웃을 보고, 하나의 Skill 문서에 add/delete/replace 편집을 제안한다. 검증 분할 점수가 엄격히 오를 때만 수락한다. 텍스트 학습률(편집 개수 한도), 거절된 편집 버퍼, 에포크 단위 slow/meta 업데이트가 학습을 안정화한다. 배포 시점에는 추가 모델 호출이 없다.

## 방법

대상 모델 \(M\)은 고정이다. 하네스 \(h\)가 과제 \(x\)와 Skill \(s\)로 궤적과 점수 \(r(s)\in[0,1]\)를 만든다. 훈련 분할은 경험, 선택(검증) 분할은 게이트, 테스트 분할은 최종 보고에만 쓴다. 내보내는 산출물은 `best_skill.md` 하나다 (대략 300–2,000 토큰).

한 스텝 (Figure 2):

1. **롤아웃.** 현재 Skill로 훈련 배치를 실행한다. 메시지, 도구 호출, 관측, 최종 답, 검증기 피드백을 남긴다.
2. **미니배치 반성.** 실패와 성공을 나누고, 각각을 미니배치로 본다. 실패 배치는 빠진 규칙을, 성공 배치는 유지할 행동을 제안한다. 편집은 add/delete/replace다.
3. **유계 갱신.** 편집 예산 \(L_t\)가 학습률이다. 병합·순위 후 상위 \(L_t\)개만 적용한다. 기본은 cosine 스케줄(크게 시작해서 줄어듦). 기본값 \(L_t=4\).
4. **검증 게이트.** 선택 분할 점수가 현재보다 엄격히 높을 때만 남긴다. 동점은 거절한다. 거절된 편집과 점수 하락은 에포크 안 버퍼에 남아, 다음 반성이 같은 수정을 반복하지 않게 한다.
5. **Slow/meta.** 에포크가 끝나면 같은 과제를 이전 Skill과 현재 Skill로 다시 돌려, 개선·퇴행·지속 실패·안정 성공을 본다. 오래 가는 지침은 Skill의 보호 구역에 쓰고, 이 후보도 게이트를 통과해야 한다. 옵티마이저 전용 meta Skill은 「어떤 편집이 통했고 실패했는지」를 다음 옵티마이저 프롬프트에만 붙이며, 배포 산출물에는 넣지 않는다.

하네스는 어댑터로 바꾼다. Direct chat, Codex CLI, Claude Code CLI가 같은 `best_skill.md`를 읽는다.

## 실험

벤치마크 6개: SearchQA, SpreadsheetBench, OfficeQA, DocVQA, LiveMath, ALFWorld.
대상 모델 7개: GPT-5.5/5.4/5.4-mini/5.4-nano/5.2, Qwen3.5-4B, Qwen3.6-35B-A3B.
하네스 3개: direct chat, Codex, Claude Code.
비교: No skill, Human skill, 한 번 생성한 LLM skill, Trace2Skill, TextGrad, GEPA, (하네스에서) EvoSkill.

Table 1에서 측정한 52개 (모델, 벤치마크, 하네스) 칸 모두 SkillOpt가 최고이거나 동률이다.

GPT-5.5 direct chat, No skill → SkillOpt:

| 벤치마크 | No skill | SkillOpt |
|---|---|---|
| SearchQA | 77.7 | 87.3 |
| SpreadsheetBench | 41.8 | 80.7 |
| OfficeQA | 33.1 | 72.1 |
| DocVQA | 78.8 | 91.2 |
| LiveMath | 37.6 | 66.9 |
| ALFWorld | 83.6 | 95.5 |
| 평균 | 58.8 | 82.3 (+23.5) |

칸마다 가장 센 경쟁 방법을 고른 오라클보다 평균 +5.4 포인트다. Codex에서 GPT-5.5는 No skill 대비 +24.8, Claude Code에서 +19.1이다. Direct chat 7모델 평균 이득은 약 +17.6이다. 작은 모델의 상대 이득이 더 크다 (예: GPT-5.4-nano ALFWorld 34.3 → 69.4).

전이 (Table 4): GPT-5.4 Spreadsheet Skill을 mini/nano에 옮기면 No skill보다 높다. Codex에서 학습한 Spreadsheet Skill을 Claude Code에 넣으면 22.1 → 81.8 (+59.7). OlympiadBench Skill을 Omni-MATH에 옮기면 세 스케일 모두 양의 이득이다.

Ablation: 편집 한도를 없애면 점수가 떨어진다. 거절 버퍼를 빼면 Spreadsheet −4.6. meta와 slow를 둘 다 빼면 Spreadsheet 77.5 → 55.0 (−22.5). 롤아웃 배치·미니배치·스케줄은 상대적으로 둔감하다.

## 한계

- 자동 점수(검증기, exact-match, 실행 검사)가 있는 과제에 가장 잘 맞는다. 주관적 성공은 게이트가 약해진다.
- 배포 산출물은 짧지만, 학습에는 롤아웃과 옵티마이저 호출이 든다. 한 번만 쓰는 과제에는 비용이 클 수 있다.
- 고의로 Skill 하나를 최적화한다. 서로 다른 절차가 많은 영역에는 부족할 수 있다.
- 학습 분포의 휴리스틱이 들어갈 수 있어, 다른 모델·하네스·과제로 옮기기 전에 검증이 필요하다.
