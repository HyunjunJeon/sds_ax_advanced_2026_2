# Day02 — DeepAgents · OpenViking · Skills RAG

문서 전처리부터 원문 검색, 답변 형식 선택, 인용 검증까지 Python으로 실행하는 실습입니다.
**`00`→`06`은 읽고 실행하는 완성 예제, `07`·`08`은 직접 구현하는 학생 과제,
`09`는 통제 비교 실험, `10`→`13`은 고급 구현 과제와 미니 프로젝트입니다.**
Agent는 **DeepAgents**, 검색 저장소는 **로컬 OpenViking**, 답변 형식은 **Skills**를 사용합니다.
Jupyter와 Docker 없이 실행할 수 있습니다.

이 문서의 모든 명령은 실제로 존재하는 플래그만 적습니다. 문서의 명령과 코드의 명령이
다르면 그것을 버그로 보고 바로잡습니다([전체 검증](#전체-실행-검증) 참고).
설치·포트·키·문제 해결은 [SETUP.md](docs/SETUP.md)를 참고하세요.

## 1. 준비와 첫 실행

이 폴더는 상위 프로젝트와 분리된 **Python 3.13** 환경을 사용합니다. 생성·임베딩은 OpenRouter API를 호출합니다.
상위 `.env`의 `OPENROUTER_API_KEY`와 `OPENROUTER_BASE_URL`을 읽을 수 있으며, 상위 파일을 수정하지 않습니다.
키가 없다면 `.env.example`을 `.env`로 복사하고 API 키를 입력하세요. `.env`는 Git에 포함하지 않습니다.

```bash
cd /Users/jhj/Desktop/sds_ax_advanced_class/Day02
uv sync --locked
uv run python 00_prepare.py          # 모델 호출 없이 환경·코퍼스·Skills 점검
uv run python scripts/install_rhwp.py # HWP 바이너리 실습용(HWPX는 Python으로 직접 처리)
uv run day02 server init
uv run day02 server start
uv run day02 doctor
uv run python 02_ingest.py
uv run day02 ask "알파 SLA 서비스 크레딧 신청 기한은 언제인가요?" --as-of 2026-09-08 --format grounded-qa
```

다른 PC에서는 압축을 푼 `Day02` 폴더로 이동하면 됩니다. 첫 적재는 문서별 요약·임베딩 때문에 시간이 걸립니다.
이후 같은 원문은 서버와 비교해 재사용합니다.

정상 답변은 일반 SLA의 **15일**과 8월 추가 약정의 **20일**을 구분하고, 2026-09-08 기준의 신청 기한을 **다음 달 20일**로 설명합니다.
`data/business`는 버전·고객·예외를 비교하기 위한 가상 업무 자료입니다.

## 2. 번호 순서대로 따라가기

| 파일 | 읽고 실행할 내용 | 확인할 trace·결과 |
|---|---|---|
| 00 | 환경·코퍼스·Skills 점검 | `api_key_set`, 코퍼스 10문서(approved 9·draft 1), Skills 5개 |
| 01 | 고정/절/의미 청킹, 원문 좌표, 표 품질, 메타데이터 후보 | `outputs/examples/01_preprocess.json`의 청크 경계, 표 결측·중복 보고 |
| 02 | OpenViking 적재, manifest, 중복 적재 방지 | 재실행 시 `reused=문서 수`, `outputs/manifests/business.json`의 `complete`·주소 |
| 03 | dense/sparse/RRF/MultiQuery/표현별 가중치/LLM rerank 비교 | `03_search.json`의 순위 차이. 범위 필터가 만료 v1을 제외하는지 |
| 04 | DeepAgents 검색 Tool과 답변 Skills | `outputs/runs/<id>.json`의 trace: `search`·`validation_retry`·`support_review` |
| 05 | 후속 질문 해석, 턴별 재검색, 독립 예산, 세션 | `05_multiturn.json`: 두 턴 모두 `search >= 1`(매 턴 재검색). 같은 문서가 답이면 인용 ID는 같을 수 있음 |
| 06 | PDF·PPTX·HWPX·HWP 적재/답변, PDF 원본 이미지 확인 | evidence의 `visual_observation`이 텍스트 인용과 구분되는지 |
| **07** | **[학생 구현] 하이브리드 검색 합류 정책** | `challenge_tests/test_fusion_student.py` 전부 통과 |
| **08** | **[학생 구현] 근거 카드 인용 검증기** | `challenge_tests/test_citation_student.py` 전부 통과 |
| 09 | 청킹 전략 × 검색 변형 × 사례 × 반복 통제 비교 | `outputs/rag-comparison/<run_id>/REPORT.md`의 recall@k |
| **10** | **[고급 과제 A] 시점·버전 유효 문서 선택 — 25점** | `challenge_tests/test_version_challenge.py` + WORKSHEET A |
| **11** | **[고급 과제 B] 표 근거 보존 선택 — 25점** | `challenge_tests/test_table_challenge.py` + WORKSHEET B |
| **12** | **[고급 과제 C] 문서 갱신·세션 일관성 — 30점** | `challenge_tests/test_refresh_challenge.py` + WORKSHEET C |
| 13 | 짝 비교 미니 프로젝트(후보 − 기준선 delta) | `outputs/rag-comparison/paired-*/REPORT.md` |

01과 06은 `--live` 없이 로컬 전처리만 실행합니다. 03은 기본 모드도 OpenViking 검색을 사용하며,
`--live`가 표현별 임베딩과 LLM rerank를 추가합니다. 각 파일은 이전 파일의 변수를 공유하지 않고,
검색·답변 예제에는 02의 적재 결과가 필요합니다. 06은 필요한 입력을 직접 전처리·적재합니다.
과제 채점 기준은 [WORKSHEET.md](WORKSHEET.md)를 참고하세요. **공개 테스트만 통과하면 완성이 아닙니다.**

## 3. 대표 실험

09의 조건은 파일 안 상수(`CHUNKINGS`·`RETRIEVERS`·`CASE_TYPES`·`REPEATS`·`K`)를 편집해 바꿉니다.
CLI 플래그로 조건을 바꾸지 않습니다. 변경 이유가 코드 리뷰에 남아야 하기 때문입니다.

```bash
uv run python 09_compare.py                    # 기본값: bounded vs fixed × bm25, 모델·서버 불필요
```

dense/rrf 비교는 `02_ingest.py` 완료 뒤 `SERVER = True`, `RETRIEVERS = ["bm25", "dense", "rrf"]`로 실행합니다.
실패한 행을 결과에서 지우지 않습니다. 두 실행의 짝 비교는 13에서 합니다.

```python
# 13_mini_pjt.py 상단을 본인이 기록한 결과 경로로 바꾼다.
BASELINE = Path("outputs/rag-comparison/기준선-run-id")
CANDIDATE = Path("outputs/rag-comparison/후보-run-id")
CHANGED = frozenset({"chunking"})   # 실제로 바꾼 변수만 선언
```

분석기는 빠진 짝과 선언하지 않은 변수 변경을 거절합니다. delta는 **후보 − 기준선**입니다.

## 4. 고급 구현 과제 시작

```bash
uv run python 07_fusion_student.py    # 안내와 함께 공개 테스트가 즉시 실행된다
uv run python 10_version_challenge.py # 10/11/12 같은 형식
```

각 과제 파일을 실행하면 안내문과 공개 반례 테스트가 함께 돌아갑니다. 구현 전 실패가 정상입니다.
구현 위치는 `student_tasks.py`, 계약은 함수 docstring에, 채점 기준은 [WORKSHEET.md](WORKSHEET.md)에 있습니다.

```bash
# 항상 green이어야 하는 기본 회귀검사(과제 구현과 무관)
uv run pytest -q
# 과제별 공개 반례(구현 전에는 실패가 정상)
uv run pytest challenge_tests -q
```

## 5. 개인 OpenViking 서버 제어

```bash
uv run day02 server status
uv run day02 server logs --lines 30
uv run day02 server stop
uv run day02 server start
uv run day02 server restart
```

- 기본 주소: `http://127.0.0.1:19350`.
- OpenViking 패키지와 하위 의존성: `runtime/openviking/uv.lock`.
- 서버 설정·문서 계정: `.openviking/ov.conf`, `.openviking/client.json`.
- 영구 저장소: `.openviking/data/`.
- 로그·프로세스 정보: `.openviking/server.log`, `.openviking/process.json`.

`stop`은 이 프로젝트가 시작한 서버만 종료하며 데이터를 삭제하지 않습니다. 이미 점유된 포트의 다른 프로세스는 종료하지 않습니다.
관리용 root 키와 문서용 계정 키는 초기화 과정에서 별도로 발급합니다. Retriever는 문서용 키를 사용합니다.

`doctor`는 `/health` 확인에 그치지 않고 인증 → 문서 적재 → 실제 검색 → 원문 조회를 검증합니다.
포트·OS별 설치 조건·문제 해결은 [SETUP.md](docs/SETUP.md)를 참고하세요.

## 답변 형식 선택

```bash
uv run day02 ask "가용률 구간별 크레딧 비율을 비교해주세요." --format comparison
uv run day02 ask "서비스 크레딧 신청 절차를 알려주세요." --format procedure
uv run day02 ask "크레딧 비율의 최댓값을 table_query로 구해주세요." --format table-analysis
uv run day02 ask "2035년 화성 지사의 우편번호는?" --format insufficient-evidence
```

| Skill | 출력 |
|---|---|
| `grounded-qa` | 결론 → 설명 → 근거 |
| `comparison` | 비교 기준 → 비교표 → 차이와 적용 조건 |
| `procedure` | 적용 조건 → 절차 → 예외와 확인 사항 |
| `table-analysis` | 수치와 단위 → 계산 및 해석 → 해석의 한계 |
| `insufficient-evidence` | 확인된 사실 → 부족한 정보·상충 사항 |

`--format`을 생략하면 Agent가 선택합니다. 명시한 형식은 우선 적용하고, 충분한 근거가 없으면 근거 부족 형식으로 전환합니다.

각 `skills/<name>/SKILL.md`는 답변 절차를, `output.json`은 절 제목·순서·표/문단/순서 목록을 정의합니다.
형식을 바꾸려면 해당 파일을 편집한 뒤 아래 명령으로 확인하세요. 제목과 배치는 Python 렌더러에 하드코딩하지 않았습니다.

```bash
uv run day02 skills --validate
```

Skill을 실제로 읽었는지, 필수 절이 있는지, 인용 ID가 이번 턴의 근거인지 검사합니다.
별도 모델 호출로 주장과 근거의 지지 관계도 검토하고, 검증 실패 시 한도 내에서 재작성합니다.
Skill 지침만으로 사실성을 보장한다고 간주하지 않습니다.

## 새 문서 넣기

```bash
uv run day02 prepare data/samples/alpha-sla.hwpx
# 출력된 catalog 경로를 다음 명령에 사용합니다.
uv run day02 ingest --catalog outputs/parsed/문서-해시/catalog.json --namespace mydocs
uv run day02 ask "문서에 대해 질문" --namespace mydocs
```

PDF·PPTX의 특정 페이지/슬라이드만 선택할 수도 있습니다. CLI의 번호는 1부터 시작합니다.

```bash
uv run day02 prepare data/samples/slides-excerpt.pdf --pages 1,2
uv run day02 prepare data/samples/day02.pptx --pages 30,36,39,47
```

`prepare`는 문서와 출처 catalog를 만들고, `ingest`가 실제 저장소에 업로드합니다.
catalog의 경로는 Day02 내부 파일이어야 하며 한 catalog 내 파일명은 서로 달라야 합니다.
문서 옆 `<파일명>.metadata.json`이 있으면 해시를 확인한 뒤 관리 메타데이터를 적용합니다.
HWP/HWPX 샘플에는 수업용 관리 메타데이터를 포함했습니다. 메타데이터 후보를 뽑는 LLM 출력은 승인 상태나 유효일로 자동 승격하지 않습니다.

인용의 L 번호는 OpenViking에 적재한 Markdown의 행 번호입니다. PDF 원본 페이지와 PPTX 물리 슬라이드는 별도 좌표로 표시합니다.
HWP/HWPX에서 확인하지 않은 물리 페이지 번호를 만들어 붙이지 않습니다.

## 세션과 예산

```bash
uv run day02 ask "알파 SLA 신청 기한은?" --session demo --as-of 2026-09-08
uv run day02 ask "그 신청에 필요한 자료는?" --session demo --as-of 2026-09-08
```

세션은 `.runtime/sessions.sqlite3`에 저장합니다. 같은 세션 이름도 고객·기준일·namespace가 다르면 대화를 공유하지 않습니다.
후속 질문은 대화를 참고하지만 매 턴 다시 검색하며 이전 인용을 검증 없이 재사용하지 않습니다.

기본 한도는 검색 3회, 원문 read 18회, Agent 도구 18회, 논리 모델 호출 10회, 근거 카드 JSON 24,000 UTF-8 바이트, 턴 180초입니다.
시간은 호출 전후에 검사하고 각 HTTP/모델 호출에도 남은 시간에 따른 timeout을 설정합니다. 이미 실행 중인 외부 서비스의 작업을 원격 취소하는 기능은 아닙니다.
근거 바이트 예산과 모델의 실제 입력 토큰은 다른 값이며, 실제 사용량은 trace에 기록합니다.

## 전체 실행 검증

```bash
# API 없이 수행하는 단위·회귀 테스트
uv run pytest -q

# 과제 공개 반례(구현 전에는 실패가 정상이다)
uv run pytest challenge_tests -q
```

실습 파일 각각이 곧 검증이다. `00_prepare.py`는 환경·코퍼스를 점검하고, `day02 doctor`는
인증 → 적재 → 검색 → 원문 조회를 실제로 확인한다. 답변·검색의 실행 기록과 trace는
`outputs/runs/`, `outputs/examples/`에 남는다.

## 결과와 파일 배치

- `outputs/examples/`: 예제 실행 결과 JSON. `outputs/runs/`: 답변 실행 기록(인증 실패 기록 포함).
- `outputs/manifests/`: namespace별 적재 manifest. `outputs/rag-comparison/`: 09 비교 실행과 13 짝 비교 결과.
  이 폴더들은 실습을 실행하면 생기는 산출물이며 Git에 포함하지 않는다.
- `data/cases.jsonl`·`data/gold.jsonl`: 09의 평가 사례와 정답. gold는 판정 단계만 읽는다.

## 구현 위치

- `src/day02/agents/factory.py`: 공통 `create_deep_agent`, Skill 백엔드, 도구/모델 호출 제한.
- `src/day02/tools/`: OpenViking 검색·추가 원문 읽기·고정 표 연산·원본 PDF 시각 확인.
  검색 합류 과제(07)의 연결 지점 `RetrievalContext(candidate_policy=...)`도 여기 있다.
- `src/day02/ingestion/`: 파서·청킹·메타데이터 후보·적재.
- `src/day02/validation.py`: 형식과 인용 검사, Skill 기반 Markdown 렌더링.
- `src/day02/agents/rag.py`: 검증·제한된 재시도·최종 저장. 인용 검증 과제(08)의 연결 지점
  `ask(extra_claim_validation=...)`가 여기 있다.

Agent 생성은 공통 팩토리를 사용합니다. 메타데이터 추출과 시각 확인, 별도 근거 검토는 단발 모델 호출입니다.
Agent에 노출하는 파일 시스템은 `skills/`로 제한하며, 쓰기·셸·하위 Agent 위임은 허용 도구에 포함하지 않습니다.
초기 `src/sds_ax_advanced_class/test.py` 경로도 같은 CLI에 연결됩니다.
