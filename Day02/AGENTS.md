# AGENTS.md — Day02: DeepAgents · OpenViking · Skills RAG

루트 [../AGENTS.md](../AGENTS.md)의 공통 규칙을 먼저 따르고, 여기에 Day02 고유 규칙을 더한다.

**이 폴더에서 배우는 것:** 문서 전처리 → 적재 → 검색 → 답변 형식 선택 → 인용 검증까지,
RAG 한 개를 Python으로 끝까지 만든다. Agent는 DeepAgents, 저장소는 로컬 OpenViking,
답변 형식은 Skills다. Jupyter와 Docker를 쓰지 않는다.

## 0. 파일 상태표 — 무엇이 제공이고 무엇이 과제인가

실습은 루트의 번호 파일 `00`~`13`로 진행한다. **Day03과 달리 00~06 예제는 CLI 플래그를
가진다**(`--live`, `--format` 등). 실험 도구 09/13은 Day03과 같은 방식으로 **파일 안
상수를 편집해** 조건을 바꾼다.

| 파일 | 상태 | 내용 |
|---|---|---|
| `00_prepare.py` ~ `06_multimodal.py` | 제공 | 환경 점검·청킹·적재·검색 비교·Skills 답변·세션·멀티모달 |
| **`07_fusion_student.py`** | **학생 구현** | 하이브리드 검색 합류 정책 (`student_tasks.fuse_and_rank`) |
| **`08_citation_student.py`** | **학생 구현** | 근거 카드 인용 검증기 (`student_tasks.validate_claims`) |
| `09_compare.py` | 제공 | 청킹 × 검색 × 사례 × 반복 통제 비교 (조건은 파일 상수) |
| **`10/11/12_*_challenge.py`** | **학생 구현** | 과제 A(시점·버전)·B(표 근거)·C(갱신·세션) |
| `13_mini_pjt.py` | **학생 실행** | 09의 두 실행을 짝 비교 (BASELINE/CANDIDATE/CHANGED 상수) |
| **`student_tasks.py`** | **학생 구현** | 07/08/A/B/C의 알고리즘 본체. 계약은 각 함수 docstring |
| `src/day02/`, `skills/`, `docs/SETUP.md`, `scripts/install_rhwp.py` | 제공 | 프로덕션 패키지, 운영 안내, HWP 설치 |

07/08은 채점표의 선행 과제, 10~12는 배점 과제(A 25 + B 25 + C 30 + D 20 = 100점)다.
기준은 [WORKSHEET.md](WORKSHEET.md). 07/08을 완성하기 전에 09 비교에서 자신의 합류
정책을 기준선으로 삼지 않는다.

### 과제 구현 지점과 연결 위치

| 과제 | 구현 대상 | 실제 연결 지점 | 공개 테스트 |
|---|---|---|---|
| 07 검색 합류 | `student_tasks.fuse_and_rank` | `src/day02/tools/search.py` `RetrievalContext(candidate_policy=...)` | `challenge_tests/test_fusion_student.py` |
| 08 인용 검증 | `student_tasks.validate_claims` | `src/day02/agents/rag.py` `ask(extra_claim_validation=...)` | `challenge_tests/test_citation_student.py` |
| A 시점·버전 | `student_tasks.effective_documents` | 검색 전 유효 문서 확정, `day02 ask --as-of` 대조 | `challenge_tests/test_version_challenge.py` |
| B 표 근거 | `student_tasks.select_table_evidence` | `src/day02/tools/tables.py` 파싱·근거 바이트 예산 | `challenge_tests/test_table_challenge.py` |
| C 갱신·세션 | `student_tasks.SessionLedger` | `05_multiturn.py` 실행 전후, manifest의 remote_hash | `challenge_tests/test_refresh_challenge.py` |

두 연결 지점은 기본값이 꺼진 선택 인자라 제공 예제의 동작이 그대로다. 연결 과제에서
수강생이 켠다. **공개 테스트 통과는 계약만 검증한다.** 나머지 점수는 실제 연결·추가
반례·분석에서 나온다.

## 1. 검증된 실행 표면

모든 명령은 **`Day02/` 폴더 안에서** 실행한다. 진입점은 `day02` CLI 하나이고, 파서 정의는
`src/day02/cli.py:41`에 있다. 여기에 없는 서브커맨드나 옵션은 존재하지 않는다.

```bash
uv sync --locked
uv run python scripts/install_rhwp.py   # HWP 바이너리 실습용. HWPX는 Python으로 직접 처리한다.
uv run day02 server init                # 설정·키 발급 (.openviking/)
uv run day02 server start               # 개인 서버 기동 (기본 127.0.0.1:19350)
uv run day02 doctor                     # 인증 → 적재 → 검색 → 원문 조회까지 실제 검증
uv run python 02_ingest.py              # 업무 문서 적재
uv run day02 ask "..." --as-of 2026-09-08 --format grounded-qa
```

**순서에 의존한다.** `ask`/`search` 예제는 `02_ingest.py`의 적재 결과를 필요로 한다.
적재 없이 검색이 비면 그것은 버그가 아니라 선행 단계 누락이다.

| 서브커맨드 | 주요 인자 |
|---|---|
| `server` | `init` / `start` / `status` / `logs --lines N` / `stop` / `restart` |
| `doctor` | 없음 |
| `prepare` | `path`, `--entity`, `--pages`(PDF/PPTX, 1부터 시작) |
| `ingest` | `--catalog`, `--namespace` |
| `search` / `ask` | `question`, `--entity`, `--as-of`, `--namespace` |
| `ask` 추가 | `--format`(auto/grounded-qa/comparison/procedure/table-analysis/insufficient-evidence), `--session`, `--json`, `--timeout` |
| `skills` | `--validate` |

`server stop`은 **이 프로젝트가 시작한 서버만** 종료하며 데이터를 지우지 않는다. 포트를 점유한
다른 프로세스를 죽이지 않는다. 수강생이 "포트가 막혔다"고 하면 `docs/SETUP.md`의 포트 충돌 절로 보낸다.

## 2. 두 종류의 테스트 — 헷갈리지 않게

| 명령 | 대상 | 기대 결과 |
|---|---|---|
| `uv run pytest -q` | `tests/` — 제공 코드의 회귀검사 | **40 passed.** 과제 구현 여부와 무관하게 항상 통과해야 한다 |
| `uv run pytest challenge_tests -q` | 07/08/A/B/C의 공개 반례 | **구현 전 54 failed가 정상이다** |

`uv run pytest -q`가 깨졌다면 과제 미구현 때문이 아니라 **제공 코드에 회귀가 났다는 뜻**이다.
반대로 `challenge_tests`의 실패는 고칠 버그가 아니라 **아직 구현하지 않았다는 표시**다.
과제 파일 실행(`uv run python 07_fusion_student.py` 등)은 안내문과 함께 해당 과제의
공개 테스트를 바로 돌린다(`challenges.py`).

과제 대응은 루트 AGENTS.md의 단계적 힌트(L0~L3)를 `student_tasks.py`, `07`, `08`,
`10`~`13`에 적용한다. 실패 테스트 이름이 곧 반례의 설명이다. 예:
`test_expired_version_cannot_be_fused_back`, `test_tampered_quote_hash_is_detected`,
`test_missing_uri_is_conservatively_stale`.

### 자주 나오는 잘못된 지름길 — 통과시키지 않는다

| 지름길 | 왜 안 되는가 |
|---|---|
| 범위 필터를 건너뛰고 RRF만 구현 | 만료 버전이 답변 근거로 돌아온다 (07 요구사항 2) |
| 인용을 카드에 맞춰 quote를 바꿔 통과 | 역방향 검증. 근거 무결성 위조 (08) |
| `numeric` 플래그를 꺼서 수치 주장 우회 | 시각 관찰만으로 수치를 단정하게 된다 (08) |
| 날짜→문서 대응표 하드코딩 | 겹침·미래 반례에서 즉시 드러난다 (A) |
| `requires`를 비우고 관련도 정렬로 해결 | 단위 행이 잘린 조각이 근거로 남는다 (B) |
| 메모리 dict로 턴 장부 제출 | 재시작 후 유지가 안 된다. SQLite 요구 위반 (C) |
| 세션을 매 턴 새로 만들어 갱신 회피 | 스코프 격리 요청을 우회한다 (C) |
| 예산·`--timeout`을 올려 실패 감추기 | 루트 AGENTS.md 5절 금지 사항 |

## 3. 질문 → 어디를 볼 것인가

수강생 질문에 답할 때 **여기 있는 파일을 실제로 읽고 인용**한다. 기억으로 답하지 않는다.

| 수강생 질문 | 답이 있는 곳 |
|---|---|
| 청킹을 어떻게 나누나요? 고정/절/의미 차이는? | `01_preprocess.py`, `src/day02/ingestion/chunking.py` |
| HWPX·PDF·PPTX는 어떻게 읽나요? | `src/day02/ingestion/parsers.py`, `06_multimodal.py` |
| 메타데이터는 누가 정하나요? | `src/day02/ingestion/metadata.py` (LLM 후보는 승인 상태로 자동 승격되지 않음) |
| 적재가 왜 중복되지 않나요? manifest가 뭔가요? | `src/day02/ingestion/pipeline.py`, `02_ingest.py` |
| 검색 순위는 어떻게 정해지나요? RRF는? | `src/day02/ranking.py:9`(`rrf`), `:72`(`multi_query`), `03_search.py` |
| 고객·기준일 필터는 어디서 걸리나요? | `src/day02/evidence.py:47` (`Metadata.applies`) |
| Skill이 뭔가요? 답변 형식을 바꾸려면? | `skills/<이름>/SKILL.md` + `output.json` → `uv run day02 skills --validate` |
| Agent가 어떤 도구를 쓸 수 있나요? | `src/day02/agents/factory.py:37` (`build_agent`), `src/day02/tools/` |
| 인용 검증이 왜 실패하나요? | `src/day02/validation.py:57` (`validate_answer`) |
| 검증 실패 후 재작성은 어떻게 되나요? | `src/day02/agents/rag.py` |
| 예산 한도(도구 18회 등)는 어디 있나요? | `src/day02/evidence.py:92` (`Budget`) |
| 후속 질문에서 대화를 어떻게 기억하나요? | `src/day02/session.py:22`, `05_multiturn.py` |
| 과제 계약을 다시 보고 싶어요 | `student_tasks.py` 함수 docstring + [WORKSHEET.md](WORKSHEET.md) |
| 비교 실험 조건은 어디서 바꾸나요? | `09_compare.py` 상수(`CHUNKINGS`/`RETRIEVERS`/`CASE_TYPES`/`REPEATS`/`K`), `13_mini_pjt.py` 상수 |
| 짝 비교가 왜 거절되나요? | `13_mini_pjt.py` — 미선언 변수, 짝 누락, provenance 불일치 |

## 4. 용어집 — 수강생이 자주 헷갈리는 것

- **namespace**: 적재 대상 구획. `ask --namespace`가 적재할 때의 값과 다르면 검색 결과가 빈다.
- **`--as-of` (기준일)**: 그 날짜에 유효한 문서 버전만 검색한다. 답이 이상하면 기준일부터 확인한다.
  대표 예제의 정답은 기준일 `2026-09-08`에서 일반 SLA **15일**과 8월 추가 약정 **20일**을 구분한다.
- **catalog / manifest**: `prepare`가 만드는 출처 목록이 catalog, `ingest`가 남기는 적재 기록이
  manifest다. 둘이 어긋나면 `docs/SETUP.md`의 "원문·manifest 불일치" 절로 보낸다.
- **인용의 L 번호**: OpenViking에 적재한 **Markdown의 행 번호**다. PDF 원본 페이지나 PPTX 물리
  슬라이드와 **같은 좌표가 아니다.** HWP/HWPX에서 확인하지 않은 물리 페이지 번호를 지어내지 않는다.
- **근거 카드 예산 24,000바이트**: 모델의 실제 입력 토큰과 **다른 값**이다. 실사용량은 trace에 남는다.
- **`visual_observation`**: 시각 모델의 관찰 근거. 행 좌표 검증이 없어 별도 종류로 기록되며
  수치 주장의 유일한 근거가 되어서는 안 된다(과제 08).
- **family**: 추가 약정·기본 조항처럼 함께 읽어야 하는 문서 집합 표시(`catalog.json`).
  합류 정책(07)의 다양성 단위이기도 하다.
- **recall@k**: 문서 수준 기대와의 대조 결과일 뿐 답변 문장의 사실성 평가가 아니다(09/13).
- **`doctor`**: `/health` 확인이 아니라 인증 → 적재 → 실제 검색 → 원문 조회까지 검증한다.
- **관리용 root 키 ≠ 문서용 계정 키**: Retriever는 문서용 키를 쓴다. 401/403은 대개 키 종류 혼동이다.

## 5. 오류가 났을 때 — 문서로 라우팅

내용을 여기에 복사하지 않는다. **아래 문서를 열어 읽고 인용**한다. 그래야 낡지 않는다.

| 증상 | 문서 |
|---|---|
| 포트 충돌 / 401·403 / manifest 불일치 / 모델 지연 / HWP / OS별 범위 | `docs/SETUP.md` |
| 공식 참고 링크(OpenViking 문서·CLI, DeepAgents Skills, rhwp) | `docs/SETUP.md` 말미의 공식 참고 목록 |

## 6. 편집 경계

| 대상 | 정책 |
|---|---|
| `student_tasks.py`, `07`, `08`, `10`~`13` | **과제.** 루트 AGENTS.md의 단계적 힌트 정책 적용 |
| `skills/*/SKILL.md`, `skills/*/output.json` | **수업 중 편집 대상.** 형식을 바꿔보고 `day02 skills --validate`로 확인한다. 절 제목·순서는 Python에 하드코딩되어 있지 않다. |
| `09_compare.py` / `13_mini_pjt.py` 상수 | 실험 조건이므로 자유롭게 편집한다 (바꾼 이유를 주석으로) |
| `data/samples/`, `data/business/` | 수업용 가상 자료. 정답을 유리하게 바꾸지 않는다. |
| `data/gold.jsonl` | **사후 판정 전용.** 실행 경로에서 읽지 않는다 |
| `src/day02/` | 주로 **읽고 설명하는** 대상. 과제 연결에서 수강생이 수정한다. 수정 시 `uv run pytest -q`로 회귀 확인 |
| `tests/` | 회귀검사. **삭제·skip 금지** |
| `challenge_tests/` | 공개 반례. **수정 금지.** 수강생은 자기 테스트를 따로 추가한다 |
| `.env`, `.openviking/`, `outputs/`, `.runtime/` | 커밋 대상 아님. 내용 출력 금지. |

Agent에 노출하는 파일 시스템은 `skills/`로 제한되어 있고, 쓰기·셸·하위 Agent 위임은 허용 도구에
없다(`src/day02/agents/factory.py`). 수강생이 "Agent가 파일을 못 읽는다"고 하면 이 제약을 설명한다.

## 7. 검증과 비용

```bash
uv run pytest -q     # 오프라인 단위·회귀 테스트. 현재 40 passed. 자유롭게 실행해도 된다.
```

이 값이 깨지면 수강생 환경 문제가 아니라 **변경이 회귀를 냈다는 뜻**이다.

| 실행 | 비용 규모 |
|---|---|
| `uv run pytest -q`, `challenge_tests`, 과제 파일 실행 | 없음 (API 호출 없음) |
| `00_prepare.py` | 없음 (모델 미호출) |
| `day02 ask "..."` | 모델 호출 최대 10회, 최대 180초 (`Budget` 기본값) |
| `02_ingest.py` (첫 적재) | 문서별 요약·임베딩. 시간이 걸린다. 같은 원문은 이후 재사용된다. |
| `01_preprocess.py`, `06_multimodal.py` (`--live` 없이) | 로컬 전처리만. 무료 |
| `03_search.py` 기본 | OpenViking 검색(서버 필요). `--live`가 실제 모델 API 추가 |
| `09_compare.py` 기본값 | 없음 (로컬 bm25). `SERVER=True`/`LIVE=True`가 서버·모델 비용을 추가 |
| `13_mini_pjt.py` | 없음 (기존 JSONL만 읽는다) |

## 8. Day03과의 관계

Day03은 이 폴더의 코드와 업무 문서를 `Day03/vendor/day02/`에 **스냅샷으로 동봉**한다.
Day03 질문에 답할 때는 이 폴더가 아니라 그쪽을 봐야 할 수 있다. 반대로 여기서의 변경이 Day03에
자동 반영되지는 않는다. Day03은 CLI 플래그 없이 LabConfig 편집 방식을 쓴다 — 이 폴더의
00~06 예제 CLI와 혼동하지 않게 수강생에게 알려준다.
