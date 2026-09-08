# AGENTS.md — Day03: 같은 RAG를 멀티에이전트로 확장하고 개선하기

루트 [../AGENTS.md](../AGENTS.md)의 공통 규칙을 먼저 따르고, 여기에 Day03 고유 규칙을 더한다.

**이 폴더에서 배우는 것:** Day02의 RAG 하나를 7가지 구조(기준선/적응형/순차/라우터/병렬/
Supervisor/Handoff)로 확장하고, **어떤 실패가 왜 개선됐고 추가 비용이 무엇인지**를 통제 실험으로
설명한다. 역할 수를 늘리는 것 자체가 목표가 아니다.

---

## 0. 가장 먼저 — 이 폴더에는 명령행 플래그가 없다

`Day03/*.py` 어디에도 `argparse`가 없다. 다음 플래그는 **존재하지 않는다**:

```
--backend  --workspace  --check  --judge  --architectures  --repeats  --cases
--viking-env  --seed-workspace  --fault  --context-mode  --serial-workers
--web  --public-topic  --domains  --ingest  --seconds
--baseline  --candidate  --changed  --require-same-plan  --out
```

실행 조건은 **각 파일 상단의 `실험 = LabConfig(...)`** 에 있다 (정의: `common/lab.py:58`).
조건을 바꾸려면 **파일을 편집하고 다시 실행한다.**

```bash
uv run python 01_baseline_rag.py     # 파일 안의 설정을 그대로 사용
```

09/13은 `LabConfig` 외에 파일 안의 모듈 상수도 함께 편집한다:

- `09_compare.py` → `실험`, `ARCHITECTURES`, `CASES`, `REPEATS`, `JUDGE`, `SEED_WORKSPACE`
- `13_mini_pjt.py` → `BASELINE`, `CANDIDATE`, `BASELINE_ARCHITECTURE`, `CANDIDATE_ARCHITECTURE`,
  `CHANGED`, `MIN_REPEATS`, `REQUIRE_SAME_PLAN`, `OUT`

**이 방식은 실수가 아니라 설계다** (`common/lab.py` 모듈 docstring). 실행 조건이 코드에 남아야
"한 번에 하나의 변수만 바꾼다"는 비교 규칙을 diff로 서로 검토할 수 있기 때문이다.

> ⚠️ **어떤 문서에서 `--flag`를 보면 그 문서가 낡은 것이다.** 플래그를 지어내서 안내하지 말고,
> 수강생에게 이 규칙과 해당 `LabConfig` 필드 이름을 알려준다.

---

## 1. 파일 상태표 — 무엇이 제공이고 무엇이 과제인가

| 파일 | 상태 | 내용 |
|---|---|---|
| `00_prepare.py` | 제공 | 원문·레지스트리·서버 준비 |
| `01_baseline_rag.py` | 제공 | 검색 → 답변 기준선 |
| `02_adaptive_rag.py` | 제공 | 계획·검토·제한 재검색 |
| `03_sequential_rag.py` | 제공 | 조사·작성·검토 Context 분리 |
| `04_router_rag.py` | 제공 | 최초 담당자 선택 |
| **`05_parallel_rag.py`** | **학생 구현** | fan-out/합류. `build` 안 TODO 1~5 (`05_parallel_rag.py:66`) |
| `06_supervisor_rag.py` | 제공 | 의존성·실패·재계획 |
| **`07_handoff_rag.py`** | **학생 구현** | 담당자 이전·유지. TODO 1~4 (`07_handoff_rag.py:68`) |
| `08_web_enrichment.py` | 제공 | EXA → 검사 → 적재 → 재검색 |
| `09_compare.py` | 제공 | 같은 사례로 구조 비교 |
| **`10/11/12_*_challenge.py`** | **학생 구현** | 과제 A/B/C. 실제 구현은 `student_tasks.py` |
| **`13_mini_pjt.py`** | **학생 실행** | 변경 전후 짝 비교 |
| **`student_tasks.py`** | **학생 구현** | 과제 A/B/C의 알고리즘 본체 |

**05/07은 배점 없는 선수 실습**이다. 채점표(A25+B30+C25+D20=100점)에는 들어가지 않지만,
09의 `ARCHITECTURES`에 `parallel`/`handoff`가 있으면 실패 행이 되므로 **09 비교의 전제조건**이다.

### 과제 구현 지점

| 과제 | 구현 대상 | 공개 테스트 |
|---|---|---|
| A. Context 선택 | `student_tasks.py:42` `select_context` | `challenge_tests/test_context_challenge.py` (11개) |
| B. 지연 결과 안전 | `student_tasks.py:88` `merge_attempts`, `:102` `schedule_ready` | `challenge_tests/test_recovery_challenge.py` |
| C. lease fencing | `student_tasks.py:143` `FencedRegistry` (`claim`/`commit`/`head`/`snapshot`) | `challenge_tests/test_ingestion_challenge.py` |
| 05/07 그래프 | 각 파일의 `build` 함수 | `challenge_tests/test_graph_challenge.py` (5개) |

**공개 테스트 통과는 알고리즘 계약만 검증한다.** 과제의 나머지 점수는 `common/`의 실제 RAG에
연결하고, 추가 반례를 만들고, 통제 실험으로 효과와 비용을 설명하는 데서 나온다
(`WORKSHEET.md`). 수강생이 "테스트 통과했으니 끝"이라고 하면 이 점을 알려준다.

---

## 2. 두 종류의 테스트 — 헷갈리지 않게

| 명령 | 대상 | 기대 결과 |
|---|---|---|
| `uv run pytest -q` | `tests/` — 제공 예제의 회귀검사 | **30 passed.** 과제 구현 여부와 무관하게 항상 통과해야 한다 |
| `uv run pytest challenge_tests -q` | 05/07/A/B/C의 공개 반례 | **구현 전 44 failed가 정상이다** |

`uv run pytest -q`가 깨졌다면 과제 미구현 때문이 아니라 **제공 코드에 회귀가 났다는 뜻**이다.
반대로 `challenge_tests`의 실패는 고쳐야 할 버그가 아니라 **아직 구현하지 않았다는 표시**다.
이 둘을 섞어서 진단하지 않는다.

과제별로 따로 돌리려면:

```bash
uv run pytest challenge_tests/test_graph_challenge.py -q   # 05, 07
uv run python 10_context_challenge.py                      # 과제 A (안내문 + 공개 테스트)
uv run python 11_recovery_challenge.py                     # 과제 B
uv run python 12_ingestion_challenge.py                    # 과제 C
```

**05/07과 10~12는 실행 방식이 다르다.** 10~12는 `run_challenge`가 안내문을 출력한 뒤 공개
테스트를 **바로 실행**한다 (`common/challenges.py:23`). 05/07은 `run_lab`이라 **실제 실험 실행을
시도**하다가 그래프 안에서 `NotImplementedError`가 나고, `common/lab.py:395`가 다음 단계를
안내한 뒤 exit code 1로 끝난다 — 테스트가 자동으로 돌지 않으므로 출력된 pytest 명령을 따로 실행한다.

## 3. 과제 대응 프로토콜

루트 AGENTS.md의 단계적 힌트(L0~L3)를 **`student_tasks.py`, `05`, `07`, `10`~`13`** 에 적용한다.
`common/`, `tests/`, `data/`는 제공 코드이므로 자유롭게 읽고 설명한다.

### 대응 순서

1. **먼저 계약을 읽게 한다.** 각 함수의 docstring에 목적함수·예외 조건·금지 사항이 다 있다.
   에이전트가 요약해주지 말고 **해당 줄을 인용**하고 수강생이 읽었는지 확인한다.
2. **실패 테스트 이름부터 본다.** 테스트 이름이 곧 반례의 설명이다.
   예: `test_late_success_cannot_resurrect_an_old_attempt`, `test_overlapping_coverage_defeats_greedy_ranking`,
   `test_expired_worker_cannot_commit_after_takeover`.
3. **수강생의 접근을 먼저 듣는다** (L2 진입 조건).
4. **수강생 코드와 실패 로그를 받은 뒤 고친다** (L3 진입 조건).

### 자주 나오는 잘못된 지름길 — 통과시키지 않는다

| 지름길 | 왜 안 되는가 |
|---|---|
| 원문 문장을 잘라 바이트 예산 맞추기 | 적용 조건·예외가 날아간다. A 요구사항 2 위반 |
| 인용 ID를 새로 만들어 검증 통과 | provenance 위조. 강사 채점 메모에서 거절 대상 |
| 정답 문서명을 하드코딩한 선택기 | 새 고객/기준일 사례로 바로 구별된다 |
| 모델 프롬프트에 "중복하지 마"라고 지시 | 안정적 작업 ID 요구를 충족하지 않는다 (B 요구사항 4) |
| 지연 순서를 `sleep`으로만 재현 | Barrier/Event 또는 가짜 시계를 쓰라는 요구사항 |
| 프로세스 내 lock이나 전역 변수로 fencing | SQLite 영속화 요구 위반 (C 요구사항 1) |
| `merge_attempts`에서 입력 dict를 직접 수정 | 입력 불변 요구 위반 |

### 거부 예시

> 이건 채점 대상이라 통째로 작성하지는 않겠습니다. 대신 반례부터 같이 잡죠.
> `merge_attempts`의 권위는 도착 순서가 아니라 `task_id` + `number`입니다
> (`student_tasks.py:88`). 지금 실패하는 `test_late_success_cannot_resurrect_an_old_attempt`가
> 정확히 그 지점입니다 — 시도 1의 늦은 성공이 시도 2의 실패를 덮으면 안 됩니다.
> 어떤 자료구조로 "최신 시도"를 판별할 생각인지 먼저 말씀해 주시겠어요?

---

## 4. 질문 → 어디를 볼 것인가

| 수강생 질문 | 답이 있는 곳 |
|---|---|
| 실험 조건을 어떻게 바꾸나요? | `common/lab.py:58` `LabConfig` (필드마다 주석에 의미가 있다) |
| 구조별 그래프는 어디 있나요? | 번호 파일의 `build(service)` 함수 |
| 검색·생성·검토는 누가 하나요? | `common/service.py` — `retrieve`/`ensure`/`generate`/`review`/`finish`/`worker`/`plan`/`route`/`synthesize` |
| State와 합류(reducer)는? | `common/contracts.py:84` `State`, `:75` `merge_results` |
| 예산은 어떻게 세나요? | `common/runtime.py:18` `Meter` — planner·Worker·reviewer·judge를 **합산**한다 |
| local과 viking 검색의 차이는? | `common/rag_backend.py` (local은 어휘 일치이지 벡터 검색이 아니다) |
| 웹 문서는 어떻게 적재되나요? | `common/exa_client.py` → `common/ingestion.py` → `common/source_registry.py:82` `reserve` |
| 평가(judge)는 어떻게 하나요? | `common/evaluation.py` — gold는 **이 모듈에서만** 읽고 Agent 입력에 넣지 않는다 |
| 짝 비교가 왜 거절되나요? | `common/paired_analysis.py:84` — 미선언 변수, 짝 누락, 반복 부족 |
| 결과는 어디에 저장되나요? | `outputs/rag-comparison/<run_id>/` (`common/lab.py:358` `output_dir`) |
| Day02 코드는 어디에? | `vendor/day02/` (`common/day02_bridge.py`가 연결한다) |
| 재현에 필요한 hash는? | `common/provenance.py` — 코드·자료·lock의 hash만 기록하고 `.env`는 읽지 않는다 |

---

## 5. 용어집 — 수강생이 자주 헷갈리는 것

- **`complete`는 정답률이 아니다.** 형식과 인용 ID가 유효하다는 **실행 상태**다. 의미상 정확성은
  judge 평가 + 사람의 원문 대조로 따로 판정한다. 이 둘을 섞어 말하지 않는다.
- **`task_id` vs `attempt.number`**: 논리 작업의 정체성과 그 작업의 몇 번째 시도인지는 다른 축이다.
  과제 B의 핵심이 이 분리다.
- **lease / epoch / ready head**: 소유권(lease)과 단조 증가 버전(epoch), 공개된 최신 문서(head)는
  각각 다르다. **fencing은 오래된 Worker의 로컬 게시를 막을 뿐, 외부 업로드의 exactly-once를
  보증하지 않는다** (과제 C 요구사항 4).
- **plan replay**: `plan_file`을 지정하면 첫 실행이 모델 분해를 저장하고 이후 실행은 그것을 재생한다.
  trace의 `plan_replayed`가 증거다. **모델을 다시 불러 우연히 같은 계획이 나온 것은 재생이 아니다.**
- **`seed_workspace`**: 문서·버전·head만 복사하고 **대화 상태는 복사하지 않는다**.
- **`context_mode`**: `"evidence"`(원문 전달) vs `"summary"`(요약만 전달). 06의 대조 조건이다.
- **`entity` / `as_of` / `thread`**: 셋이 함께 scope 해시가 되어 대화를 격리한다. 하나만 달라도
  다른 대화다.
- **전체 입력 토큰 ≠ 근거 Context 바이트**: system·history·도구 스키마가 포함되는지 구분해서 말한다.
- **`CHANGEABLE`** (`common/paired_analysis.py:16`): 짝 비교에서 선언 가능한 변수 14개.
  `implementation`, `architecture`, `context_bytes`, `context_mode`, `serial_workers`, `no_replan`,
  `no_owner_memory`, `skill`, `web_mode`, `max_calls`, `max_tools`, `seconds`,
  `web_max_age_hours`, `max_web_documents`. 여기 없는 이름을 `CHANGED`에 넣으면 분석기가 거절한다.

---

## 6. 실험 규칙

수강생이 실험을 설계할 때 아래를 지키게 돕는다. 어기면 채점에서 결과가 무효가 된다.

1. **한 번에 하나의 변수만 바꾼다.** 바꾼 이유를 `LabConfig` 옆에 주석으로 남긴다.
2. **실험마다 workspace를 분리한다.** 앞 실험의 웹 적재가 뒤 실험에 섞이면 안 된다.
   검증된 `work/rag/live-v2`는 **읽기 전용**으로 쓰고 실험용으로 수정하지 않는다.
3. **local과 viking을 한 실험의 전후로 섞지 않는다.** 서버 오류를 local로 자동 우회하지 않는다.
4. **실패 행을 지우지 않는다.** 실패·보류·평가 오류·usage 누락을 따로 센다.
5. **사례별 최소 3회 반복.** 다만 3회로 통계적 유의성을 주장하지 않는다.
6. **순차/병렬 비교는 같은 계획을 재생**한 뒤에만 동시 실행 효과를 주장한다
   (`REQUIRE_SAME_PLAN=True`).
7. 실패가 빨리 끝나 평균 지연이 낮아진 것을 성능 개선으로 읽지 않는다. 토큰이 줄어도 필수 조항
   누락이 늘었다면 그 손실을 함께 보고한다.

---

## 7. 비용 규모

유료 호출은 막지 않되 **실행 직전에 규모를 한 줄로 고지**한다.

| 실행 | 규모 |
|---|---|
| `uv run pytest -q ...`, `10/11/12` 과제 테스트 | 없음 (API 호출 없음) |
| `00_prepare.py` (`ingest=False`) | 없음~적음 |
| `01_baseline_rag.py` | 모델 호출 1회 |
| `02`~`07` | 계획·Worker·검토가 더해져 수 회~수십 회 (`max_calls` 기본 32) |
| `08_web_enrichment.py` | EXA 웹 검색 + 본문 수집 + 적재. `exa_mode="replay"`면 웹 호출 없음 |
| `00_prepare.py` (`ingest=True`) | **임베딩·요약·색인.** 첫 적재는 길다 (`seconds=1800` 권장) |
| `09_compare.py` **기본 설정** | **7구조 × 6사례 × 3회 + judge = 수백 회, 수십 분.** 반드시 고지 |
| `13_mini_pjt.py` | 없음 (기존 JSONL만 읽는다. 모델·EXA를 부르지 않는다) |

09를 처음 돌려보는 수강생에게는 `CASES=["simple"]`, `REPEATS=1`부터 권한다
(`09_compare.py` 모듈 docstring).

---

## 8. 편집 경계

| 대상 | 정책 |
|---|---|
| `student_tasks.py`, `05`, `07`, `10`~`13` | **과제.** 단계적 힌트 정책 적용 |
| 번호 파일의 `실험 = LabConfig(...)` | 실험 조건이므로 자유롭게 편집한다 (바꾼 이유를 주석으로) |
| `common/` | 제공 코드. 과제 D의 "실제 연결"에서 **수강생이** 수정한다. 자유롭게 읽고 설명한다 |
| `tests/` | 회귀검사. **삭제·skip 금지** |
| `challenge_tests/` | 공개 반례. **수정 금지.** 수강생은 자기 테스트를 따로 추가한다 |
| `data/gold.jsonl` | **사후 평가 전용.** 실행 경로에서 읽지 않는다 |
| `.env`, `.openviking/`, `work/`, `outputs/` | 커밋 대상 아님. 내용 출력 금지 |
