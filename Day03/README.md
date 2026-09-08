# Day03 — 같은 RAG를 멀티에이전트로 확장하고 개선하기

**이 폴더에서 `00` → `09`를 읽고 실행한 뒤, `10` → `13`의 고급 구현 과제를 수행합니다.** 번호 파일에 그래프와 설계 주석이 있고, 공통 검색·근거·저장은 `common/`에서 공유합니다. 필요한 Day02 코드와 업무 문서는 `vendor/day02`에 동봉했습니다. 그중 `05`(병렬)와 `07`(Handoff)은 **학생이 직접 구현하는 파일**입니다.

## 실험 방식: CLI 플래그 없이, 코드를 고쳐 실행한다

이 실습에는 명령행 플래그가 없습니다. 모든 실험 조건은 각 파일 상단의 `LabConfig`(common/lab.py)에 정의되어 있습니다.

```bash
uv run python 01_baseline_rag.py   # 파일 안의 실험 설정을 그대로 사용
```

- 바꿀 조건은 파일을 열어 `LabConfig` 값을 편집한다. **한 번에 하나의 변수만** 바꾸고, 바꾼 이유를 옆에 주석으로 남긴다.
- 실행 조건·trace·비용은 `outputs/rag-comparison/<run_id>/result.json`에 자동 저장된다.
- 회귀검사는 `uv run pytest -q`로 항상 통과해야 한다. 학생 구현 과제의 테스트는 과제 파일 실행으로 확인한다(구현 전 실패가 정상).

## 1. 준비와 첫 실행

모든 명령은 이 README와 `pyproject.toml`이 있는 **Day03 폴더**에서 실행합니다.

```bash
uv sync --locked
uv run pytest -q
uv run python 00_prepare.py   # 기본: backend="local", 환경·코퍼스 점검
uv run python 01_baseline_rag.py
```

`.env`의 `OPENROUTER_API_KEY`를 사용합니다. EXA 실습에는 `EXA_API_KEY`도 필요합니다. 셸 환경변수가 `.env`보다 우선합니다. 실제 모델·웹·색인 API 호출에는 비용이 발생합니다.

**검색 backend 두 가지:**

- `backend="local"`(기본값): 동봉 문서에 대한 어휘 검색. 키·서버 없이 모든 실습이 동작한다. 실제 모델은 호출하며, 이 검색 품질을 벡터 검색 성능으로 해석하지 않는다.
- `backend="viking"`: 실제 OpenViking 서버 검색. 실습 파일에서 `viking_env=".openviking/rag-client.env"`를 지정하고, 첫 준비에만 `ingest=True`를 지정한다(§5 참고).

**이 컴퓨터에서 이미 준비한 OpenViking을 사용하는 경우:** 서버 주소는 `127.0.0.1:1935`, 준비한 코퍼스는 `work/rag/live-v2`입니다. 실습 파일의 `LabConfig`에서 `backend="viking"`, `viking_env=".openviking/rag-client.env"`, `workspace="work/rag/live-v2"`로 지정합니다.

서버 오류를 로컬 검색으로 자동 우회하지 않습니다. local과 viking의 결과를 한 실험의 전후로 섞지 않습니다.

## 2. 번호 순서대로 따라가기

| 파일 | 형태 | 읽고 실행할 내용 | 확인할 trace·결과 |
|---|---|---|---|
| [00_prepare.py](00_prepare.py) | 제공 | 원문·레지스트리·서버 준비 | ready 문서 수, corpus snapshot |
| [01_baseline_rag.py](01_baseline_rag.py) | 제공 | 검색 → 답변 기준선 | 실제 근거와 추가 모델 호출 없는 비용 |
| [02_adaptive_rag.py](02_adaptive_rag.py) | 제공 | 계획·검토·제한 재검색 | `adaptive_retry`, 누락 항목의 변화 |
| [03_sequential_rag.py](03_sequential_rag.py) | 제공 | 조사·작성·검토 Context 분리 | 역할별 입력 크기와 모델 호출 |
| [04_router_rag.py](04_router_rag.py) | 제공 | 최초 담당자 선택 | `route`, 잘못된 배정의 영향 |
| [05_parallel_rag.py](05_parallel_rag.py) | **학생 구현** | 독립 조사 fan-out과 합류 | Worker 시작/종료 중첩, 합산 토큰 |
| [06_supervisor_rag.py](06_supervisor_rag.py) | 제공 | 의존성·실패·재계획 | 선행 성공, 완료 결과 보존 |
| [07_handoff_rag.py](07_handoff_rag.py) | **학생 구현** | 응답 담당자 이전·유지 | `handoff`, `owner_retained`, 후속 턴 |
| [08_web_enrichment.py](08_web_enrichment.py) | 제공 | EXA → 검사 → 적재 → 재검색 | `exa_results`, `ingest_ready`, 재사용 |
| [09_compare.py](09_compare.py) | 제공 | 같은 사례와 초기 코퍼스로 구조 비교 | `runs.jsonl`, 품질·호출·토큰·지연 |
| [10_context_challenge.py](10_context_challenge.py) | **학생 구현** | 과제 A: 예산·조항 의존성 기반 근거 선택 | 중복 커버·UTF-8·예외 누락 반례 |
| [11_recovery_challenge.py](11_recovery_challenge.py) | **학생 구현** | 과제 B: attempt reducer·DAG 배정 | 늦은 성공·중복 응답·잔여 예산 반례 |
| [12_ingestion_challenge.py](12_ingestion_challenge.py) | **학생 구현** | 과제 C: lease fencing·head 전환 | 오래된 Worker·버전 갱신·재시작 반례 |
| [13_mini_pjt.py](13_mini_pjt.py) | **학생 실행** | 통합 변경 전후의 짝 비교·종합 과제 | 실패 분모, 평가 커버리지, 통제 변수 |

번호 파일의 주석은 흐름과 설계 이유를 설명하고, `common/` 주석은 보장 조건과 확장할 한계를 설명합니다. 단순히 역할 수를 늘리는 것이 목표가 아닙니다. **어떤 실패가 왜 개선됐으며 추가 비용이 무엇인지 원문과 trace로 설명**해야 합니다.

05/07은 미구현 상태로 배포됩니다. 구현 전에 실행하면 실패와 함께 다음 단계 안내가 출력되고, 구현을 마치면 같은 명령이 곧 실험 실행이 됩니다. 그래프 검증은 `uv run pytest challenge_tests/test_graph_challenge.py -q`로 합니다. 두 파일을 완성하기 전에는 09의 `ARCHITECTURES`에서 `parallel`/`handoff`를 제외하고 실행합니다.

## 3. 대표 실험 — 모두 LabConfig 편집으로 실행

아래 각 실험은 "어느 파일의 어떤 값을 바꾸는가"로 읽습니다.

| 실험 | 파일과 변경 | 관찰 |
|---|---|---|
| 단순 문제 7구조 비교 | 09: `CASES=["simple"]`, `REPEATS=1` | 구조별 추가 호출 비용 |
| 실패 후 재계획 | 06: `fault="operations:timeout"` | `injected_timeout` → `supervisor_replan` → 회복 |
| 재계획 없는 대조군 | 06: `no_replan=True` | 회복하지 못한 누락과 비용 |
| 요약 전달의 손실 | 06: `context_mode="summary"` | `parent_context` 바이트, `parent_evidence_rehydrated` |
| 순차/병렬 엄밀 비교 | 05: 아래 §4 절차 | 같은 분해에서 wall-clock만 차이 |
| 담당자 유지와 전환 | 07: 같은 `workspace`·`thread`에서 question만 교체해 2회 실행 | `owner_retained`, `handoff` |
| 웹 수집과 재사용 | 08: `web` 모드별 workspace에서 실행, 같은 workspace에서 question 교체 후 재실행 | 첫 요청 `exa_search`·`ingest_ready`, 후속 요청 EXA 0회 |

`web`은 `off`(기존 코퍼스만) / `transient`(이번 응답의 임시 원문) / `persist`(저장·색인 후 재검색)이다. 다른 공개 주제는 `public_topic`과 `domains`로 지정한다. 내부 질문 전체는 EXA로 전송하지 않으며, 외부 기술 문서로 내부 계약이나 보관 규정을 확정할 수 없습니다.

## 4. 병렬 비교는 엄밀하게 — 분해 계획의 저장과 재생

병렬 비교에서 가장 흔한 오류는 "분해가 달라진 효과"를 "동시 실행의 효과"로 읽는 것이다. 모델 분해는 실행마다 달라질 수 있으므로, 05의 `plan_file`이 이 변수를 통제한다(`Service.parallel_plan`이 저장·재생·검증을 수행한다).

1. 05를 기본 설정(`serial_workers=False`, `label="parallel"`)으로 실행한다. 첫 실행이 모델 분해를 `work/plans/parallel-demo.json`에 저장한다.
2. `serial_workers=True`, `label="parallel-serial"`만 바꿔 다시 실행한다. 같은 `plan_file`을 재생하므로 **분해는 동일**하고 동시 실행 수만 1로 줄어든다. 재생 실행은 planner 모델을 호출하지 않는다(trace의 `plan_replayed`).
3. 두 실행의 result.json에서 trace의 `plan` 이벤트가 동일한지 직접 확인한다.
4. 13_mini_pjt.py에서 두 runs.jsonl을 `REQUIRE_SAME_PLAN=True`로 짝 비교한다. 분해가 다른 짝은 분석기가 거절한다.

병렬이 줄일 수 있는 것은 전체 경과 시간이며 합산 토큰은 늘어날 수 있다. 재생 실행에서 분해 단계 비용이 사라진 점도 해석에 포함한다.

## 5. OpenViking 서버 시작·재시작

기존 `.openviking/rag.conf`와 저장소가 있으면 별도 터미널에서 다음 명령을 실행하고 유지합니다(서버 프로세스 자체는 외부 프로그램이다).

```bash
uv run --isolated --no-project --python 3.13 --with openviking==0.4.16 openviking-server --config .openviking/rag.conf --host 127.0.0.1 --port 1935
```

처음 설치하거나 모델 키를 바꿨다면 `native_server.py` 상단의 `ACTION`을 단계별로 바꿔 실행한다: `"configure"`(설정 생성) → 위 서버 명령 실행 → `"init"`(문서 계정 발급). 이후 00_prepare.py의 `LabConfig`에서 `backend="viking"`, `viking_env=".openviking/rag-client.env"`, `workspace="work/rag/class-native"`, `ingest=True`, `seconds=1800`으로 첫 적재를 실행한다. 첫 적재에는 요약·임베딩·색인 대기가 포함된다. 준비 후 같은 workspace를 재사용한다. `.env`, `.openviking`, `.venv`, `work`는 수강생 배포용 소스에 포함하지 않습니다.

## 결과와 파일 배치

결과는 `outputs/rag-comparison/<run_id>/`에 있습니다. `complete`는 정답률이 아닙니다. 09의 `JUDGE=True` 자동 의미 평가, 원문 인용 검증, 사람의 적용 조건 판정을 함께 읽습니다. 키 값은 결과에 기록하지 않습니다. 질문 모델의 토큰 집계에는 서버 내부 임베딩/요약 비용이 포함되지 않습니다.

- `common/`: 같은 RAG·도구·예산·저장 계약, 비교 분석기, LabConfig 실험 실행기
- `data/`: 실행 사례와 **사후 평가 전용** 기대 사실
- `student_tasks.py`, `challenge_tests/`: 고급 구현 시작점과 공개 반례(그래프 과제 포함)
- `tests/`: 완성된 예제의 회귀검사
- `vendor/day02/`: Day02 재사용 코드와 업무 문서

[WORKSHEET.md](WORKSHEET.md)에 그래프 과제(05/07), 구현 요구, 실제 RAG 연결 지점, 추가 반례, 100점 채점표, 제출 형식을 정리했습니다. 공개 테스트만 통과하거나 옵션 실행 결과만 제출하면 고급 과제를 완료한 것이 아닙니다.
