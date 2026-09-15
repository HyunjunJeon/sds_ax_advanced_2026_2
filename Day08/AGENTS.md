# AGENTS.md — Day08: DeepAgents Guardrail 방어 위치 비교

루트 [../AGENTS.md](../AGENTS.md)의 공통 규칙을 먼저 따르고, 여기에 Day08 고유 규칙을 더한다.

**이 폴더에서 배우는 것:** 같은 보고 Agent에 같은 공격을 넣고 방어 배치만 바꿔, 공격이 어느 경로로 들어와
어느 경계에서 막혔는지를 실행 결과로 본다. **채점 과제가 없다.** 모든 방어는 구현돼 있고, 수강생은 실행하고 읽는다.
그래서 루트 AGENTS.md 4장의 단계적 힌트 정책은 여기에 적용하지 않는다. 코드를 읽고 설명하는 요청은 자유롭게 돕는다.

## 0. 가장 먼저 — CLI 플래그가 없다

`Day08/*.py` 어디에도 `argparse`가 없다. 실행 조건은 **각 파일 상단 상수**다 (`MODEL_MODE`, `INJECTION_GUARD`,
`DETECTOR`, `CASES`, `CONFIGS`, `REPEATS`, `SCRIPT`, `REMOVE`, `HOLDOUT`). 플래그를 지어내 안내하지 않는다.

## 1. 파일 상태표

| 파일 | 내용 |
|---|---|
| `00_baseline_agent.py` | B0 정상 실행, 평가기 소개 |
| `01_input_guard.py` | Input Guard 범위 비교 (사용자 메시지만 / 도구 결과까지) |
| `02_action_guard.py` | 권한 검사 위치 비교 (분류기만 / 부모 middleware / 도구 래퍼 / 최소권한) |
| `03_output_guard.py` | 공개 경계 비교 (내장 정규식 / 최종 답변만 / 전달 직전 / 모든 경계) |
| `04_memory_permissions.py` | 파일 권한·메모리 두 세션 (프롬프트만 / 권한 / 권한+메모리 검사 / 순서 틀린 권한) |
| `05_hitl_approval.py` | 승인 바인딩 세 모드 × 시나리오 4개. 대본만 쓴다 |
| `06_compare.py` | B0·B3·B4 비교, 방어 제거, 미공개 사례 |
| `07_lfm_encoders.py` | LFM PII-Detector·Policy-Linter를 문서에 직접 적용. 로컬 CPU, 모델 호출 없음 |
| `08_guard_models.py` | 분류기 6종을 같은 17개 입력으로 비교. RunPod 2종 + OpenRouter 3종 + 키워드 |
| `scripts/runpod_guards.sh` | 강사용. Injection 분류기 Pod 생성·확인·주소 출력·삭제 |
| `guardlab/defenses.py` | 방어 논리 본체 |
| `guardlab/harness.py` | Harness Profile 로 내장 도구 제거 (02 `tool_wrapper_min`, 06 B2·B3) |
| `guardlab/` 나머지 | 데이터·도구·컨텍스트·Guard·평가기·재생·기록 |
| `tests/` | 모델 없는 검증 33개. **삭제·skip 금지** |

## 2. 수강생 질문에 답할 때

- 결과를 해석해 달라면 `outputs/<번호>/<run_id>/runs.jsonl`의 행을 함께 읽는다. 답변 문구가 아니라
  `outbox`, `drafts`, `log.tools`의 `executed`, `log.decisions`를 근거로 말한다.
- "왜 막혔나/안 막혔나"는 코드 줄로 답한다 (4장 표).
- `INJECTION_GUARD="fake"`나 `MODEL_MODE="scripted"` 결과를 탐지율·공격 성공률로 해석하지 않는다.
- 실패·오류 행을 지우거나, `guardlab/budget.py` 상한을 올리거나, `holdout.jsonl` 문구를 프롬프트·규칙에 넣는 요청은
  루트 AGENTS.md 5장대로 거절하고 대신 실패를 드러내는 방법을 제안한다.

## 3. 질문 → 어디를 볼 것인가

| 질문 | 답이 있는 곳 |
|---|---|
| 권한 규칙 순서는 왜 중요한가 | `deepagents/middleware/filesystem.py:423-433` (첫 일치, 미일치 허용). `tests/test_defenses_04.py` |
| 부모 middleware가 왜 Subagent 도구 호출을 못 보나 | `deepagents/graph.py:730-733` (fork 아니면 상속 없음). 02 `middleware_parent`의 `missed_paths` |
| general-purpose Subagent는 무엇을 받나 | `deepagents/graph.py:848-851` (부모 도구 전체). 02 `tool_wrapper_min`이 같은 이름으로 덮어쓴다 |
| 내장 read_file은 왜 따로 막나 | 업무 도구 래퍼는 `read_doc`만 본다. `guardlab/components.py` `scope_permissions`(권한)와 `guardlab/harness.py` `minimal_tools`(Harness Profile로 도구 목록에서 제거) |
| Harness Profile은 보안 경계인가 | 아니다. 문서 명시: model-facing calibration. 파일 권한·도구 래퍼와 같이 쓴다. `deepagents/profiles/harness/harness_profiles.py:613` |
| 메모리는 언제 적재되나 | `deepagents/middleware/memory.py:283,293` (thread당 한 번). 04는 두 thread_id |
| HITL은 어느 hook에서 멈추나 | `langchain/agents/middleware/human_in_the_loop.py:405` (after_model). 재검증은 도구 안 |
| UNKNOWN은 왜 ALLOW가 아닌가 | `guardlab/guards/contracts.py`, `defenses.resolve_input_decision` |
| 평가기는 무엇을 읽나 | `guardlab/evaluate.py` 모듈 docstring |
| `called`와 `executed`의 차이 | `guardlab/trace.py` 모듈 docstring |
| 재생 대본은 어떻게 쓰나 | `guardlab/replay.py`. 각 파일의 `MODEL_MODE="scripted"` |
| Guard 원격 호출 형식 | `guardlab/guards/injection.py` (SGuard: safe/unsafe logprob, Kanana: 라벨 토큰, OpenRouter Judge: VERDICT 줄) |
| 분류기가 문서형 Injection을 놓치는데 왜 괜찮나 | README "분류기 6종 × 입력 17건" 표. 놓친 행은 02·03·04의 코드 경계가 막는다 |

## 4. 용어

- **차단(BLOCK)·보류(REVIEW)·변환(TRANSFORM)·허용(ALLOW)**: `PolicyDecision.action`. UNKNOWN 신호는 결정이 아니다.
- **호출됨(called) vs 실행됨(executed)**: 도구가 불렸다는 기록과 원시 구현이 실제로 돌았다는 기록은 다르다.
- **탐지 실패 + 방어 성공**: Input Guard가 놓쳤지만 권한 검사가 막은 행. 두 사실을 따로 센다.
- **과잉 차단(overblocked)**: 정상 사례가 차단 때문에 업무를 못 마친 것. `security_training` 사례가 이걸 잡는다.
- **방어 경계 시험 vs 실제 공격 실행 시험**: 전자(scripted)는 모델이 속았다는 증거가 아니다.

## 5. 비용 규모

| 실행 | 규모 |
|---|---|
| pytest, `scripted`, 05, 07 | 없음 (07은 첫 실행에 모델 다운로드 약 2.6GB) |
| 08 | OpenRouter Guard 51회 |
| 00 | 모델 호출 10~15회 |
| 01~03 기본 상수 | 실행 6~20건 |
| 06 기본 상수 | **6실행, 60~90회.** 전체 행렬(20실행, 200~300회)은 과제. 반드시 고지 |

## 6. 편집 경계

| 대상 | 정책 |
|---|---|
| 번호 파일 상단 상수 | 자유롭게 편집 (바꾼 이유를 주석으로) |
| `guardlab/`, 번호 파일 본문 | 읽고 설명은 자유. 수강생이 실험으로 고쳐 보는 것도 막지 않되, 원본과의 diff를 남기게 한다 |
| `tests/` | **삭제·skip 금지** |
| `guardlab/data/holdout.jsonl` | 06의 최종 확인 전용. 프롬프트·규칙에 넣지 않는다 |
| `.env`, `outputs/`, `work/`, `.hf-cache/` | 커밋 대상 아님. 내용 출력 금지 |
