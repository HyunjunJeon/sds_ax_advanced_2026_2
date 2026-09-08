# Day03 고급 구현 과제

**목표:** 동일한 Day02 RAG를 유지하면서 Context 전달, 작업 제어, 지식 갱신의 실패를 코드로 해결하고, 개선 효과와 비용을 통제 실험으로 설명한다.

00~04, 06, 08~09는 읽고 실행할 완성 예제다. **05(병렬)·07(Handoff) 그래프와 10~12 알고리즘을 직접 구현한다.** 그래프 과제는 각 실습 파일의 TODO 주석을 따라 작성하고, 알고리즘 과제는 `student_tasks.py`의 `NotImplementedError`를 채운다. 함수의 공개 계약은 코드 docstring에 정의되어 있다. 최종 제출은 **A/B/C를 실제 RAG에 연결한 통합 변경**이다. 모델 변경, 정답 하드코딩, 테스트 삭제, 무제한 예산으로 실패를 숨기는 방식은 인정하지 않는다.

알고리즘 과제(10~12)는 실행 자체가 테스트다. `uv run python 10_context_challenge.py`를 실행하면 안내문과 함께 해당 과제의 공개 테스트가 돌아간다. 그래프 과제(05/07)는 실행하면 실험이 돌아가므로 검증은 `uv run pytest challenge_tests/test_graph_challenge.py -q`로 한다. 어느 쪽이든 구현 전 실패가 정상이다.

## 0. 그래프 과제(선행) — 05 병렬, 07 Handoff

**05 [병렬 fan-out]:** `05_parallel_rag.py`에서 `Send` 기반 dispatch, 작업별 reducer 합류, join 종합을 구현한다. 검증:

```bash
uv run pytest challenge_tests/test_graph_challenge.py -q   # 구현 전 실패가 정상
uv run python 05_parallel_rag.py                            # 구현 후 같은 명령이 실험 실행이 된다
```

완성 후 README §4의 절차로 순차/병렬 엄밀 비교를 수행한다. 이 그래프는 과제 B의 연결 대상이기도 하다.

**07 [Handoff 제어권 이전]:** `07_handoff_rag.py`에서 `Command(update=..., goto=...)` 기반 담당자 이전·유지·왕복 상한을 구현한다. 같은 검증 파일로 확인한다.

두 그래프를 완성하기 전에는 09_compare의 `ARCHITECTURES`에서 `parallel`/`handoff`를 제외한다.

## 먼저 남길 기준선

```bash
uv run pytest -q
uv run python 09_compare.py
```

09의 `ARCHITECTURES`/`CASES`/`REPEATS`는 파일 안에서 편집한다. 기본값(7구조 × 6유형 × 3회)을 그대로 두거나, 처음에는 `CASES=["simple"]`, `REPEATS=1`로 좁혀 시작한다. 출력된 결과 폴더를 `before`로 기록한다. 변경 후 같은 조건의 결과를 별도 `after`로 남긴다. 실패한 행을 지우지 않는다. 검증된 `work/rag/live-v2`를 실험용으로 수정하지 않고 새 workspace를 사용한다.

실제 OpenViking으로 비교할 때는 09의 `실험`에서 `backend="viking"`, `viking_env=".openviking/rag-client.env"`, `workspace="work/rag/assignment-before"`를 지정하고 `SEED_WORKSPACE="work/rag/live-v2"`로 준비된 원문 snapshot을 읽기 전용으로 가져온다. seed는 문서/버전/head만 복사하고 대화 상태는 복사하지 않는다. 같은 코퍼스라도 local과 viking의 결과를 한 실험의 전후로 섞지 않는다.

## A. 조건을 잃지 않는 Context 선택 — 25점

**실패 상황:** 작은 Context에서 비슷한 SLA 설명 여러 개가 공간을 차지하고, 신청 기한을 바꾸는 추가 약정 또는 그 약정의 적용 범위가 빠진다.

진입 파일: [10_context_challenge.py](10_context_challenge.py). 구현 함수: `student_tasks.select_context`. 연결 위치: `common/service.py:Service.context`, 필요 시 `common/rag_backend.py`의 최종 선택 단계.

요구사항:

1. `required` 항목의 커버 수 최대화 → 실제 렌더링 바이트 최소화 → ID 사전순의 우선순위로 선택한다. 단순 관련도 정렬의 반례를 먼저 설명한다.
2. 추가 약정과 기본 조항처럼 함께 읽어야 하는 단위의 전이적 의존성을 지킨다. 일부 문장만 잘라 바이트 예산을 우회하지 않는다.
3. 한글 UTF-8, ID/개행의 비용까지 포함한다. 완전한 커버가 불가능하면 예산 내 최선의 부분해를 반환하고 실제 답변에는 부족한 항목이 남아야 한다.
4. 실제 Evidence와 연결한다. `facets`는 질의 계획과 현재 원문에서 구성하고 gold를 읽지 않는다. `requires` 관계의 추출 근거를 trace에 남긴다. 이 관계의 의미상 정확성을 공개 알고리즘 테스트가 보증하지 않는다는 점을 설명한다.
5. 기존 고객/시점 필터와 URI·문서 버전·행·읽기 창 hash 검증을 유지한다. **이미 초기 검색에서 제외된 문서는 후단 선택기로 복원할 수 없다.** 후보 수집과 Context 축소 중 어디에서 근거가 사라졌는지 분리해서 진단한다.

필수 검증:

```bash
uv run python 10_context_challenge.py
```

추가로 학생이 최소 세 반례를 작성한다: 필수 조항보다 점수가 높은 중복 문서, 예산이 필수 묶음보다 1바이트 작은 경우, 관련 있어 보이지만 고객/기준일이 다른 문서. 기존 선택기와 새 선택기를 같은 질문·초기 코퍼스·예산에서 비교하고, 별도로 세 가지 예산 크기의 민감도를 분석한다. system/history/도구 스키마를 포함한 **전체 입력 토큰**과 **근거 Context 바이트**를 구분한다.

배점: 선택 계약·공개 반례 10, 실제 RAG 연결과 provenance 보존 8, 추가 반례·비용/누락 분석 7.

## B. 지연 결과가 섞여도 안전한 Supervisor — 30점

**실패 상황:** 시도 1이 timeout된 뒤 시도 2가 시작됐다. 시도 1의 성공이 늦게 도착해 최신 상태를 덮거나, 재계획이 이미 끝난 작업을 다른 이름으로 다시 실행한다.

진입 파일: [11_recovery_challenge.py](11_recovery_challenge.py). 구현 함수: `merge_attempts`, `schedule_ready`. 연결 위치: 자신이 구현한 `05_parallel_rag.py`와 제공된 `06_supervisor_rag.py`, `common/contracts.py`, `common/runtime.py`.

요구사항:

1. 논리 작업 ID와 시도 번호를 분리한다. 최신 시도만 유효하고, 동일 완료의 중복 전달은 멱등이어야 한다. 같은 시도의 상충 결과는 도착 순서로 해결하지 않는다.
2. 결과 순서를 섞어도 유효한 사건 집합의 합류 결과가 같아야 한다. 입력 딕셔너리를 직접 변경하지 않는다. 이전 성공이 최신 실패를 덮어 후속 작업을 열면 안 된다.
3. 최신 complete인 선행 작업만 의존성을 충족한다. running 슬롯, task별 호출 예약, 최종 합류/검토 여유를 동시에 계산한다. permanent 실패의 후속 작업은 실행하지 않는다.
4. 실제 그래프에 연결할 때 배정과 running 기록 사이의 경합을 방지한다. 현재의 revision 접두사는 ID 충돌을 피하지만 논리적으로 같은 업무의 재실행을 막는 충분조건이 아니다. 재계획에서 작업 정체성을 유지하는 방식을 설계하고 검증한다.
5. 병렬 비교의 계획 고정은 `Service.parallel_plan`과 `LabConfig.plan_file`(저장·재생·검증 제공)으로 이미 가능하다. 이 경로를 자신의 스케줄러 연결에도 적용해, 순차/병렬 비교뿐 아니라 재시도 실험에서도 같은 계획을 재생한다. 계획의 역할·목적·의존성도 검증한다. 모델을 다시 호출해 우연히 같은 계획을 얻는 것은 계획 재생이 아니다(trace의 `plan_replayed`로 구별한다).

필수 검증:

```bash
uv run python 11_recovery_challenge.py
```

추가 테스트는 지연 순서를 `sleep`에만 의존하지 않고 Barrier/Event 또는 가짜 시계로 제어한다. 최소 세 시나리오를 포함한다: 이전 시도의 늦은 성공, 상충하는 중복 완료, 완료 업무를 다른 ID로 다시 제안하는 계획. 성공한 작업이 몇 번 시작됐는지 실제 그래프 trace로 확인한다. 모의 API 장애와 실제 API 장애율 측정을 구분한다.

배점: reducer/스케줄러 계약 10, 그래프 연결·안정적 작업 ID·계획 재생 12, 추가 반례·예산/실패 분석 8.

## C. 웹 적재의 lease fencing과 snapshot — 25점

**실패 상황:** Worker A가 업로드 중 멈춰 lease가 만료됐다. B가 권한을 넘겨받거나 더 최근 내용을 게시한 뒤 A의 완료가 늦게 도착한다. 내용 hash의 중복 검사만으로는 최신 head를 보호하지 못한다.

진입 파일: [12_ingestion_challenge.py](12_ingestion_challenge.py). 구현 대상: `student_tasks.FencedRegistry`. 연결 위치: `common/source_registry.py`, `common/ingestion.py`, `common/rag_backend.py`.

요구사항:

1. SQLite에 URL별 단조 증가 epoch, lease, ready head를 영속화한다. Python 전역 변수나 프로세스 내 lock만으로 해결하지 않는다.
2. 예약은 짧은 트랜잭션으로 수행한다. 외부 업로드/색인 중 DB 쓰기 잠금을 잡지 않는다. commit은 입력 Lease가 아니라 DB의 현재 소유권과 만료 시각을 검증한다.
3. 새 버전이 준비되기 전에는 이전 ready head를 유지한다. 이전 시도, 만료된 미완료 시도, 조작한 lease의 완료는 head를 바꾸지 못한다. 같은 완료 재전달과 다른 URI의 상충 완료를 구분한다.
4. 실제 `Ingestion.store`에 연결하고 publish 이후 응답 유실을 주입한다. 외부 저장이 성공한 후 클라이언트가 실패해도 재개 시 동일 URI/원문 확인으로 중복을 줄인다. **fencing은 오래된 Worker의 로컬 게시를 막으며 외부 업로드 자체의 exactly-once를 보증하지 않는다.**
5. 요청 시작의 ready snapshot을 고정해 여러 Worker가 읽는 문서 버전을 일관되게 한다. 같은 요청이 직접 추가한 새 문서는 어떻게 공개할지 규칙을 정하고, snapshot 해제·갱신 시점을 trace로 설명한다.

필수 검증:

```bash
uv run python 12_ingestion_challenge.py
```

공개 테스트에는 실제 SQLite 경합과 다른 Python 프로세스에서의 재읽기가 포함된다. 추가 테스트에는 process 종료 후 lease 회복, A→B→A 내용 복귀, 응답 중간 갱신으로 인한 혼합 버전을 포함한다. ready 판정에는 여전히 원문 read와 검색 index 확인이 모두 필요하다.

배점: 영속 계약·공개 반례 8, 실제 적재 연결·응답 snapshot 12, 프로세스/부분 실패 증거와 한계 설명 5.

## D. 통합 변경의 효과를 입증하기 — 20점

A/B/C를 실제 RAG에 연결한 뒤, 한 번에 한 가지 변경의 효과부터 분석한다. 여러 기능을 동시에 켠 최종 결과만으로 각 기능의 효과를 주장하지 않는다.

- 대표 여섯 유형을 유형별 최소 3회 반복한다. `followup`은 마지막 턴의 평가와 전체 대화 비용을 모두 남긴다. 문서가 없는 `unknown`에서는 적절한 보류가 목표다.
- 학습 중 반복해서 본 문제 외에 최소 4개의 새 검증 사례를 작성한다. 다른 고객, 기준일 경계, 예외 조항, 답 불가를 포함하고 기대 사실을 원문 행에 연결한다. 전후 실험에서는 같은 사례와 같은 gold를 사용한다.
- 동일 모델/backend/질문/고객/시점/초기 코퍼스를 통제한다. 예산이나 Context 모드를 변경했다면 변수로 명시한다. 순차/병렬 효과를 비교할 때는 같은 계획을 재생한다(05의 `plan_file`).
- 실패, 보류, 평가 오류, usage 누락을 별도로 센다. 코드 snapshot과 결과를 함께 제출한다.
- 최소 6개의 답변을 직접 원문에 대조한다. 추가 약정·예외·부정·적용 대상을 확인하고, 모델 judge와 다르게 판정한 사례를 설명한다. 불일치가 없다면 대조한 근거를 남긴다.

`13_mini_pjt.py` 상단의 상수를 본인이 기록한 결과 경로로 바꾼다. 아래 두 가지 비교가 기본이다.

```python
# 1) 같은 Supervisor의 코드 개선 효과. 코드가 바뀌었으면 CHANGED에 "implementation"을
#    선언한다(설정이 같아도 구현 diff가 유일한 변경 변수라는 뜻).
BASELINE_ARCHITECTURE = "supervisor"
CANDIDATE_ARCHITECTURE = "supervisor"
CHANGED = frozenset({"implementation"})

# 2) 같은 분해 계획으로 순차 실행과 병렬 실행을 비교한다(05의 plan_file 재생).
BASELINE_ARCHITECTURE = "parallel"
CANDIDATE_ARCHITECTURE = "parallel-serial"
CHANGED = frozenset({"architecture", "serial_workers"})
REQUIRE_SAME_PLAN = True
```

분석기는 빠진 짝이나 선언하지 않은 변수 변경을 거절한다. 출력의 delta는 **후보 − 기준선**이다. 실패가 빨리 끝나 평균 지연이 낮아졌다면 성능 개선으로 해석하지 않는다. 토큰이 줄어도 필수 조항 누락이 늘었다면 그 손실을 함께 보고한다. 3회 반복만으로 통계적 유의성을 주장하지 않는다.

배점: 실험 통제·반복/새 사례 5, 실패/평가/usage 커버리지 5, 원문 기반 품질 판정 5, 개선·악화 원인과 한계 설명 5.

## 제출물

1. 실제 실행 코드 변경(05/07 그래프 포함)과 `student_tasks.py` 구현. 미사용 함수만 완성해 제출하지 않는다.
2. 기본 회귀 결과, 세 과제 공개 테스트 결과, 학생이 추가한 반례와 실제 그래프/저장 계층 통합 테스트.
3. 수정 전후 실행 JSONL, 짝 비교 결과, 사용한 문제/정답표, 계획 재생 자료, 코드 snapshot.
4. 아래 형식의 분석표와 핵심 실패 하나의 시간순 trace. 전체 API 비용을 측정하지 않았다면 그 범위를 명시한다.

| 실패 반례 | 변경한 코드·한 가지 변수 | 원문으로 확인한 품질 | 호출·입력 토큰·지연 | 남은 실패/추가 비용 |
|---|---|---|---|---|
| Context의 약정 누락 | | | | |
| 늦은 시도 응답 | | | | |
| 오래된 적재 완료 | | | | |
| 개선되지 않은 사례 | | | | |

공개 함수 테스트에만 해당하는 점수는 A 10 + B 10 + C 8 = **28점**이다. 나머지는 실제 연결과 추가 반례·분석으로 평가한다. 성능이 항상 개선되어야 만점인 것은 아니다. 개선되지 않은 결과라도 통제된 실험과 정확한 실패 원인 분석이 있으면 해당 분석 점수를 받을 수 있다.

## 강사용 채점 메모

- 그래프 과제(05/07): Send/Command 없이 순차 호출로 우회했는지, reducer 키를 작업 id로 썼는지, 왕복 상한을 그래프가 강제하는지를 본다. `test_graph_challenge.py`의 barrier 테스트가 동시성을 실제로 검증한다.
- A: `requires`가 선택 비용과 커버 모두에 반영되는지 본다. ID를 임의로 만들어 인용 검사를 통과시키거나, 예외 문장을 잘라낸 해법은 거절한다. 정답 문서명을 hardcode한 선택기는 새 고객/시점 사례로 구별한다.
- B: 최신 attempt를 판별하는 reducer와 배정의 원자성은 다른 문제다. 계획상의 stable task key, attempt, 결과 hash의 역할을 구두로 설명하게 한다. 모델에게 "중복하지 마"라고만 지시한 구현은 안정적 ID 요구를 충족하지 않는다. 계획 재생은 trace의 `plan_replayed` 이벤트로 확인한다.
- C: lease 만료 직후 이전 Worker가 commit하는 반례를 확인한다. 외부 publish와 SQLite commit을 하나의 트랜잭션으로 묶었다고 주장하면 실패/응답 유실 경계를 질문한다. 요청 중 snapshot 갱신 규칙이 일관적인지도 본다.
- D: 가장 좋은 반복 하나만 고르지 않았는지, 실패를 제거하지 않았는지, gold/코퍼스를 후보에게 유리하게 바꾸지 않았는지 확인한다. 자동 judge 점수와 실행 상태를 혼용하면 분석을 다시 요구한다.

공개 테스트는 계약 예시이며 생산 환경의 분산 실행, 외부 API 장애율, 대규모 코퍼스 품질을 인증하지 않는다. 학생이 추가한 통합 반례와 사람의 원문 판정이 고급 과제 평가의 중심이다.
