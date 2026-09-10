# Day05 — 업무 기준으로 Agent를 평가하고 개선을 검증한다

주문 취소·결제 환불·예약 재고 복원을 하나의 사례로 사용한다. 수강생이 만든 평가 기준을
실제 실행에 연결하고, 실패 근거를 읽은 뒤 한 조건을 바꿔 개선과 회귀를 설명하는 수업이다.
기준 강의는 [Day5 Agent Evaluation](../Lecture_Note/Day5_Agent_Evaluation_Golden_Dataset_Langfuse.pptx)이다.

**루트의 `00~09`가 새 주 실습이다.** 기존 번호 파일·전용 도우미·자료·테스트는
[`legacy/`](legacy/README.md)에 보존했다. Day05의 `.venv`, `.env`, `uv.lock`을 함께 사용한다.

## 수업 흐름

| 순서 | 실행 파일 | 수강생의 판단 | 다음 단계의 입력 |
|---|---|---|---|
| 00 | [00_check_env.py](00_check_env.py) | 응답·업무 상태·정책 준수는 어떻게 다른가? | 환경 확인 |
| 01 | [01_draft_scenarios.py](01_draft_scenarios.py) | 정책을 어떤 기대 상태·금지 행동으로 바꿀 것인가? | `01_scenarios.xlsx` |
| 01b (피드백이 있을 때) | [01b_refine_goldens.py](01b_refine_goldens.py) | 전문가 의견을 어떤 기대 답변·필수 내용으로 반영할 것인가? | `01_refined_scenarios.xlsx`, 변경 기록 |
| 02 | [02_freeze_dataset.py](02_freeze_dataset.py) | 초기 상태로 재현되고, 근거가 검수됐는가? | `02_dataset.json` |
| 03 | [03_record_baseline.py](03_record_baseline.py) | Agent가 실제로 무엇을 했는가? | `03_baseline.json`, Langfuse Trace |
| 04 | [04_inspect_traces.py](04_inspect_traces.py) | 관찰 사실·원인 가설·대안 가설은 무엇인가? | 근거 문서, `04_change_plan.json` |
| 05 | [05_evaluate_runs.py](05_evaluate_runs.py) | 평가기가 중요한 오류를 잡는가? | `05_baseline_scores.json` |
| 06 | [06_review_judge.py](06_review_judge.py) | Judge가 사람의 실패 판정을 통과시키는가? | 사람 검수 CSV, 보정/확인용 결과 |
| 07 | [07_compare_candidate.py](07_compare_candidate.py) | 한 조건의 변경으로 무엇이 좋아지고 나빠졌는가? | `07_comparison.json` |
| 08 | [08_promote_regression.py](08_promote_regression.py) | 실패를 어떻게 재현·검수하고 최종 확인할 것인가? | 회귀 초안, holdout 결과 |
| 09 | [09_report_decision.py](09_report_decision.py) | 이번 후보를 승인·보류할 근거는 무엇인가? | `09_report.md` |

순수 수업 6시간을 가정한 [재구성 계획](docs/restructure_plan.md)을 바탕으로 구현했다.
모든 번호를 한 번에 자동 실행하지 않는다. 검수·가설·사람 판정은 중간에 직접 작성한다.

강사용 [02 수업 진행안](docs/02_teaching_guide.md)에는 사례 읽기 → 결과 예측 → 미검수 오류 확인 →
교차 검수 → 동결 결과 설명의 50분 운영 예시를 정리했다.
[도메인 전문가 피드백 운영안](docs/expert_feedback_workflow.md)에는 문서 기반 질문 생성, Agent 답변 검수,
피드백 반영·재확인 방법과 전문가에게 보낼 요청 문안을 담았다.
바로 열어 볼 수 있는 [전문가 검수용 엑셀 기본 양식](expert_feedback_template.xlsx)도 제공한다.
작성 안내·가상 예시 4건·빈 검수 10건·피드백 반영·원문 근거를 포함하며, 원본을 복사해 쓴다.

## 준비

항상 Day05 안에서 실행한다.

```bash
cd Day05
uv sync
uv run python 00_check_env.py
uv run pytest -q
```

실행 조건은 [course_config.py](course_config.py)와 각 번호 파일 상단 상수에 있다. CLI 플래그는 없다.
기본 주 실습은 `MODE="live"`, `USE_LANGFUSE=True`다. 모델과 Judge를 실제 호출하고,
Langfuse 프로젝트에 교육용 Dataset·실행 기록·점수를 쓴다. `.env.example`을 보고 키를 직접 설정한다.
Agent와 Judge 모델은 서로 다르게 둔다. 00의 기본 점검은 외부 호출 없이 설정 유무·fixture를 확인한다.

외부 연결 없는 코드 계약 확인은 `MODE="scripted"`, `USE_LANGFUSE=False`로 선택한다.
scripted의 두 드라이버는 강사 제공 예시다. 이 결과로 실제 Agent의 성능·프롬프트 개선을 주장하지 않는다.
`python -m business_lab`은 이제 번호 순서 안내를 출력한다. 이전 통합 실행은 legacy에 있다.

## 01~02: 업무 기준을 작성하고 검수한다

현재 01의 엑셀은 개발자가 Scenario 계약을 배우는 실습 자료다. 도메인 전문가에게는
질문·답변·원문 근거를 보여 주고 판단과 수정 의견을 받은 뒤, 개발자가 기대 상태와 평가 규칙으로 옮긴다.
DeepEval로 실제 전달 문서에서 질문을 합성하는 전문가 검수 흐름은
[추가 구현할 연결](docs/expert_feedback_workflow.md#8-현재-코드에서-가능한-것과-추가-구현할-것)로 구분했다.
전문가용 기본 양식은 의견 회수용이며, 아래 02가 읽는 Scenario 엑셀과는 형식이 다르다.
개발자가 확인된 답변·의견을 `01b_refine_goldens.py`의 `REVISIONS`에 직접 옮기면 개선 초안을 만든다.
전문가용 엑셀의 열·수식을 파싱하거나 자유 의견을 모델로 자동 해석하는 단계는 없다.

[가상 인터뷰](business_lab/data/interview.md), [업무 정책](business_lab/data/order_policy.md),
[초기 상태](business_lab/data/fixtures.json)를 읽고 01을 실행한다.

```bash
uv run python 01_draft_scenarios.py
```

`outputs/course/01_scenarios.xlsx`의 `작성안내`와 `시나리오` 시트를 연다.
`input`, `expected_output`, `followups` 열은 JSON이다. 기대 상태는 **검수할 강사 제공 초안**이며,
정답이 맞다는 보증이 아니다. 문서와 금액·상태·금지 행동을 직접 대조한다.

- `fixture_id`: 실행 환경이 복원할 초기 조건. 모델에는 장애 설정을 전달하지 않는다.
- `expected_output`: 최종 상태, 금지 도구, 응답 유형, 환불 시도 한도, 선택적 필수 이정표.
  `reference_answer`와 `required_facts`는 01b에서 반영하는 마지막 턴의 기대 답변·답변 필수 내용이다.
- `followups`: 추가 질문 뒤 제공할 사용자 입력. 평가 정답을 넣지 않는다.
- `source`, `policy_version`: 기준의 근거. `family_id`는 같은 사건의 변형을 묶는다.
- `review_status`, `reviewer`, `review_reason`: 사람이 직접 입력한다. 미합의 문항은 보류한다.

기본 선택 문항은 정상 취소, 환불 후 응답 유실, 주문 번호 확인 후 처리의 세 건이다.
다른 조건도 추가하려면 **실행 전에** `SCENARIO_IDS`를 정하고 해당 문항을 검수한다.
`None`은 개발용 전체 초안을 선택한다. 기존 예시의 holdout은 08에서 별도로 사용한다.

전문가 피드백이 있다면 02 전에 다음 단계를 진행한다.

1. `01b_refine_goldens.py`의 `REVISIONS`에 대응하는 `scenario_id`, 피드백 ID·출처·의견,
   `reference_answer`, `required_facts`를 직접 적는다. 주석 예시는 가상 자료이며 기본 목록은 비어 있다.
2. 아래 명령으로 `01_refined_scenarios.xlsx`를 만든다. 원래 초안은 보존하며,
   `.changes.json`에는 수정 전후와 의견을 함께 기록한다.
3. 수정본의 `expected_output`을 확인하고 검수 정보 세 칸을 직접 작성한다.
   수정 문항은 `pending`으로 돌아가므로 이전 승인이 그대로 적용되지 않는다.
4. `02_freeze_dataset.py`의 `USE_REFINED=True`로 선택하고 02를 실행한다.
   피드백 없이 원래 초안을 쓰는 경우에는 기본값 `False`를 유지한다.

```bash
uv run python 01b_refine_goldens.py
```

01b는 기존 개발용 문항의 답변 기준을 개선한다. 질문·초기 상태·도구 규칙까지 바꿀 필요가 있으면
수정본의 해당 Scenario와 Code 평가기를 별도로 검토한다. `required_facts`에 도구 순서를 적는 것만으로
실제 도구 순서를 검증할 수는 없다. 여러 턴에서는 기대 답변을 마지막 안내 기준으로 작성한다.
반복 개선은 `SOURCE_XLSX`를 직전 검수본, `OUTPUT_XLSX`를 새 파일명으로 정하고,
02가 사용할 `course_config.py`의 `REFINED_XLSX`도 그 출력 경로와 맞춘다.

```bash
uv run python 02_freeze_dataset.py
```

02는 선택 문항의 검수, 빈 정답, 중복, 정책 버전, family 분할을 확인한다.
미검수 오류가 나면 엑셀의 기준·담당자·근거를 채운다. 코드에서 승인을 강제하지 않는다.
동결 데이터에는 승인 Scenario와 정책·fixture의 내용 해시를 함께 저장한다.
초안 추가·정답 변경은 새 데이터 버전이며, 이전 실행과 바로 비교할 수 없다.
이미 03 이후를 실행했다면 결과 폴더를 새로 정하고 두 Agent 버전을 새 데이터로 다시 실행한다.
05·07의 Judge에는 동결한 기대 답변·필수 내용을 `LLMTestCase.expected_output`으로 전달하고,
06의 사람 검수 자료에도 같은 기준을 제공한다. Agent 실행 입력에는 평가 정답을 넣지 않는다.

## 03~04: 실행 근거와 가설을 분리한다

```bash
uv run python 03_record_baseline.py
uv run python 04_inspect_traces.py
```

03은 채점하지 않는다. 실제 상태·모든 서비스 호출·최종 응답·부분 완료·오류를 원자료에 남긴다.
Langfuse에서는 같은 승인 Dataset의 Item을 반복별 Run으로 실행한다. 한 Episode의 Trace 안에
턴별 Observation을 두고, `session_id`, `episode_id`, `turn_index`로 연결한다.
각 Tool의 `call_id`와 서비스 Observation의 부모 ID도 기록한다.

여러 턴 사례는 “주문 취소 요청 → 주문 번호 질문 → 번호 제공 → 처리”다.
첫 턴의 질문 적절성과 마지막 업무 완료를 별도로 검사한다. 같은 Episode 안에서는 대화·상태를
유지하고, 새 반복에서는 초기화한다. 모델 12회·서비스 도구 18회의 한도는 Episode 전체에 적용한다.

04는 근거를 Markdown으로 정리하고, Trace ID가 있으면 모든 페이지의 원격 Observation을 읽는다.
서비스 근거 ID가 원격 조회에서 누락됐는지도 표시한다. 화면의 중복 계측 스팬을 Tool 호출 두 번으로 세지 않는다.
현재 업무 정책 검사는 서비스 실행 순서를 사용한다. 임의의 병렬 인과관계를 검증하는 범용 DAG 평가기는 아니다.

`04_change_plan.json`에는 직접 다음을 작성한다.

- `artifact_id`, `evidence_ids`: 조사한 실행과 근거. 이벤트 ID 또는 `initial_state`, `final_state`, `response`를 사용한다.
- `fact`: 실제 관찰한 사실.
- `hypothesis`, `alternative`: 원인 가설과 대안 가설.
- `change`, `success_criteria`, `owner`: 바꿀 한 조건, 사전에 정한 확인 기준, 작성자.

07은 빈 가설이나 다른 baseline을 가리키는 근거를 받아 실행하지 않는다.

## 05~06: 평가 코드와 Judge를 검증한다

[learner_evaluator.py](learner_evaluator.py)는 “주문 번호가 없는 턴에 쓰기 시도가 있었는가”를 검사하는 예시다.
서비스가 차단한 시도도 FAIL이며, 턴 기록이 없으면 `INSUFFICIENT_EVIDENCE`다.
자신의 평가 항목을 보강하기 전에 입력·판정 계약과 정상·실패 반례를 먼저 정한다.
[counterexamples.py](business_lab/counterexamples.py)와 [테스트](tests/test_numbered_course.py)를 함께 읽는다.

```bash
uv run python 05_evaluate_runs.py
uv run python 06_review_judge.py
```

05는 저장된 원자료를 Code/Judge로 채점한다. 실제 상태, 보호 상태, 대상·정책·순서, 복구,
필수 이정표, 응답, Episode 종료를 나눠 본다. 새 기준으로 재채점해도 Agent는 다시 실행하지 않는다.
판정에는 이유와 가능한 근거 ID를 붙인다. 정책 실패를 다른 점수의 평균으로 상쇄하지 않는다.

06 첫 실행은 `06_human_labels.csv`와 Judge 점수가 없는 `06_human_labels.context.json`을 만든다.
자료의 각 턴 당시 상태와 답변을 대조해 CSV의 `human_verdict`, `reviewer`, `reason`을 채운다.
기준은 **응답이 사실을 정확하게 알렸는가**이며, 전체 업무 완료 여부와 구분한다.
다시 실행하면 family 단위로 나눈 보정용·별도 확인용의 false pass/false reject를 계산한다.
Judge 오류·미검수·다른 rubric은 별도로 드러낸다. 사람이 쓴 판정을 코드가 대신 채우지 않는다.
각 분할에 사람의 PASS/FAIL이 모두 없으면 오판 비율을 확인할 수 없으므로 `insufficient_coverage`다.
작은 기본 문항만으로 두 종류의 표본이 확보된다고 보장하지 않는다. 이를 검수 완료로 처리하지 않는다.

## 07~09: 한 조건 수정, 회귀 확인, 결정 설명

04의 가설을 근거로 [candidate_agent.py](candidate_agent.py)의 `PROMPT`를 수정한다.
초기값은 baseline과 같으며, 바뀐 것이 없으면 07은 실행 전에 안내한다.
이 수업의 기본 개선 실험은 Prompt 한 조건 변경이다. 도구 코드·모델·예산이 바뀌면
두 Agent 버전을 같은 실행 기반에서 다시 측정해야 한다.

```bash
uv run python 07_compare_candidate.py
```

07은 두 버전의 원자료를 같은 평가기·Judge로 재채점한다. 기존 사람 라벨은 보존하고
새 Judge 판정에 대한 오판 비율만 다시 계산한다. `RUN_CANDIDATE=False`면 이전 candidate 원자료를 재채점한다.
문항별 차이, 모든 반복 통과 여부의 변화, 조건별 통과율, 예정 분모, `pass@k`·`pass^k`,
시나리오 단위 bootstrap, 호출 수·p50/p95·확인 가능한 비용은 비교 JSON에 들어 있다.
작은 부분집합 결과를 전체 업무 품질로 해석하지 않는다. 누락된 반복도 분모에 남긴다.

08에는 두 작업이 있다. 파일 상단 `ACTION`으로 선택한다.

- `promote`: 04에서 조사한 기록을 새 회귀 초안으로 만든다. `SANITIZATION_RECORD`를 직접 작성해야 한다.
  출처와 초기 상태를 보존하고 기대 상태는 비워 둔다. 검수한 뒤 새 데이터 버전에서 사용한다.
  ID만 바꾼 동일 사례는 02에서 중복으로 차단한다.
- `holdout`: 07에서 비교한 두 Prompt를 강사 제공 최종 확인용 사례에 실행한다.
  모델·코드·예산이 개발 실험과 다르면 시작 전에 멈춘다. 결과로 Prompt를 다시 고쳤다면 새로운 실험이다.

```bash
uv run python 08_promote_regression.py
uv run python 09_report_decision.py
```

09는 비교·가설·사람 검수·같은 후보의 holdout을 연결해 보고서를 만든다.
오류·회귀·정책 위반·검수 미완료·최종 확인 부족은 `HOLD` 사유다. `READY_FOR_HUMAN_REVIEW`는 사람 검토 진입이다.
Agent 비용은 모든 모델 응답에 공급자가 제공한 비용이 있을 때만 합산한다. 누락·호출 오류가 있으면
`null`이며 0원으로 간주하지 않는다. 이 값은 Agent 실행 비용이고, 별도 Judge 비용은 아직 합산하지 않는다.
최종 보고에는 평가 기준, 원인 가설, 한 조건의 변경, 개선과 회귀, 남은 미확인을 포함한다.

## 비용과 결과 보관

기본 세 문항 × 두 번이면 버전당 6시도다.

| 단계 | 예상 외부 모델 호출 |
|---|---|
| 00 | 기본 0회. 연결 점검을 선택하면 Judge 약 1회 |
| 01·02·04·06·09 | 0회. 04의 Langfuse 조회는 가능 |
| 03 | Agent 최대 약 72회, Judge 0회 |
| 05 | Agent 0회, Judge 약 6~18회 |
| 07 | 후보 Agent 최대 약 72회, 두 버전 Judge 약 12~36회 |
| 08 promote | 0회 |
| 08 holdout | 2문항 × 2반복 × 2버전: Agent 최대 약 96회, Judge 약 8~24회 |

공급자 내부 재시도에 따라 증가할 수 있다. 각 실행 직전에 범위를 출력한다.
결과는 `outputs/course/`와 각 파일의 `_runs/`에 보관한다. 새 독립 실험은 `course_config.WORK`를
새 폴더로 바꿔 진행한다. 기존 엑셀·가설·사람 검수 파일은 덮어쓰지 않는다.
키와 `.env`는 출력·커밋하지 않는다. `outputs/`도 커밋하지 않는다.

## 구현 근거와 범위

- [Scenario·Artifact·Verdict 계약](business_lab/contracts.py), [작성·검수·동결](business_lab/authoring.py)
- [업무 서비스](business_lab/environment.py), [여러 턴 실행](business_lab/episodes.py), [실행·채점 분리](business_lab/lesson_runs.py)
- [업무 평가기](business_lab/evaluators.py), [반복·회귀 집계](business_lab/reporting.py), [근거·사람 검수](business_lab/evidence.py)
- [LangChain Agents 공식 문서](https://docs.langchain.com/oss/python/langchain/agents)
- [Langfuse SDK Experiments 공식 문서](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)

업무 서비스는 SQLite의 격리 환경과 장애 주입으로 재현한다. 실제 결제·출고 시스템이나
실서비스의 분산 트랜잭션·부하 전체를 재현하지 않는다. 외부 서비스 연결과 평가 정확도는 별도 검증 대상이다.
구조 변경의 테스트 결과와 실제 연결의 확인 범위는 [검증 기록](docs/verification.md)에 남겼다.
