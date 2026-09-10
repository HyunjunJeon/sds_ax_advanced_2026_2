# 02 수업 진행안 — 검수한 평가 기준을 실험 데이터로 동결한다

이 문서는 현직 개발자 대상 수업에서 `02_freeze_dataset.py`를 차분히 따라가기 위한 강사 진행안이다.
01에서 초안 엑셀을 만든 뒤 약 50분을 배정하는 운영 제안이다. 실제 시간은 수강생의 이해에 맞춰 조절한다.

도메인 전문가와 협업할 때에는 [전문가 피드백 운영안](expert_feedback_workflow.md)을 먼저 적용한다.
전문가는 질문·답변·원문을 검수하고, 개발자는 그 판단을 이 실습의 Scenario와 초기 상태로 옮긴다.
현재 JSON 열이 있는 엑셀은 개발자가 평가 계약을 배우는 실습 자료로 사용한다.
도입부에서는 [전문가 검수 기본 양식](../expert_feedback_template.xlsx)의 `작성 예시`를 함께 열어
피드백이 어떤 모습으로 돌아오는지 먼저 보여 줄 수 있다. 양식의 `시작 안내`에 따라
별도 복사본에서 의견을 모으며, 이 파일을 02의 입력으로 직접 지정하지 않는다.
답변에 대한 피드백을 받았다면 개발자 역할이
`01b_refine_goldens.py`의 `REVISIONS`에 기대 답변과 필수 내용을 정리해 실행한다.
출력된 개선 초안을 이번 수업의 교차 검수에 사용하고, 02의 `USE_REFINED=True`로 동결한다.

## 1. 이번 단계에서 설명할 수 있어야 하는 것

학습 목표는 “엑셀을 JSON으로 변환했다”보다 다음 세 문장을 자기 말로 설명하는 데 둔다.

1. 이 기대 상태가 어떤 업무 원문과 초기 상태에서 나왔는가?
2. 어떤 검수가 끝나야 이번 실험의 기준으로 사용할 수 있는가?
3. 실험 도중 기준을 바꾸면 왜 데이터 버전을 구분해야 하는가?

02의 실행 흐름은 엑셀 읽기 → 검증·동결 → 결과 확인이다.
실제 검사는 별도 함수에 있으므로, 처음부터 모든 내부 코드를 순서대로 강의하지 않는다.
사례 한 건을 이해하고 실행 결과를 예측한 뒤 관련 검사로 이동한다.
근거: [02_freeze_dataset.py:9](../02_freeze_dataset.py#L9),
[business_lab/authoring.py:113](../business_lab/authoring.py#L113).

## 2. 준비

- 00의 환경 확인과 01의 초안 생성을 마친 상태에서 시작한다.
- 업무 정책, 초기 상태, 초안 엑셀을 나란히 볼 수 있게 준비한다.
- 먼저 정상 취소 한 건을 함께 읽고, 그다음 나머지 선택 문항을 교차 검수한다.
- 검수 담당자와 판단 근거는 실제 참여자가 직접 작성한다.

현재 초안 경로는 `outputs/course/01_scenarios.xlsx`, 동결 결과는 `outputs/course/02_dataset.json`이다.
기본 선택 문항은 `normal`, `timeout-after`, `clarification-episode` 세 건이다.
근거: [course_config.py:14](../course_config.py#L14).

01을 다시 실행하면 기존 검수 파일을 덮어쓰지 않고 오류로 알린다. 이어서 진행하는 수강생은
기존 엑셀을 연다. 독립 실험을 시작할 때에는 공통 설정의 `WORK`를 별도 폴더로 정한다.
근거: [business_lab/authoring.py:43](../business_lab/authoring.py#L43),
[course_config.py:9](../course_config.py#L9).

## 3. 50분 진행 순서

| 순서 | 시간 | 강사의 진행 | 수강생이 남길 것 |
|---|---:|---|---|
| 업무 완료 정의 | 5분 | 정상 취소 요청을 제시하고 성공 조건을 묻는다 | 실제로 확인할 상태 목록 |
| 초안 한 행 읽기 | 10분 | 입력·초기 상태·기대 상태를 나란히 보여준다 | 기대 상태별 원문 근거 |
| 실행 결과 예측 | 5분 | 미검수 상태에서 02 실행 결과를 예상하게 한다 | 예상 통과·실패와 이유 |
| 실패 원인 확인 | 10분 | 실행하고 오류가 발생한 검사로 이동한다 | 막힌 조건과 해당 엑셀 칸 |
| 교차 검수 | 10분 | 서로의 기대 상태와 근거를 대조하게 한다 | 실제 검수 결과와 미합의 사항 |
| 동결 결과 확인 | 10분 | 다시 실행하고 저장된 내용을 읽는다 | 이번 실험의 기준과 범위 설명 |

### ① 업무 완료 정의

강사 질문:

> Agent가 “취소했습니다”라고 답했습니다. 이 요청을 성공이라고 평가하려면 무엇을 더 확인해야 할까요?

먼저 답을 듣고 정책 원문과 대조한다. 이 실습의 완료 조건은 주문 취소, 전액 환불 기록,
주문 수량만큼 재고가 정확히 한 번 복원된 상태다. 다른 주문·결제·재고의 보호 상태도 함께 읽는다.
근거: [business_lab/data/order_policy.md:23](../business_lab/data/order_policy.md#L23).

### ② 초안 한 행 읽기

처음에는 세 칸만 설명한다. 나머지 열은 검수나 오류를 확인하는 순간에 소개한다.

| 엑셀 칸 | 수강생이 설명할 내용 |
|---|---|
| `input` | 고객이 무엇을 요청했는가? |
| `fixture_id` | 어떤 초기 상태에서 시작하는가? |
| `expected_output` | 실행 뒤 어떤 상태·응답·금지 행동을 확인할 것인가? |

이 구분은 Scenario 계약에 들어 있다.
근거: [business_lab/contracts.py:52](../business_lab/contracts.py#L52).

정상 사례에서는 “기대 재고가 왜 12인가요?”라고 묻는다. 초기 재고 10과 주문 수량 2를
찾아 완료 정책과 연결하게 한다. 기대 숫자를 먼저 외우게 하지 않는다.
이 예제의 금액 단위도 함께 확인한다. 정책에 명시된 USD 정수 센트다.
근거: [business_lab/data/fixtures.json:2](../business_lab/data/fixtures.json#L2),
[business_lab/data/scenarios.json:9](../business_lab/data/scenarios.json#L9),
[business_lab/data/order_policy.md:5](../business_lab/data/order_policy.md#L5).

### ③ 실행 결과 예측

01은 모든 초안을 `pending`으로 내보낸다. 아직 검수하지 않은 엑셀로 02를 실행하면
어디에서 막힐지 먼저 적게 한다. `approved`라는 단어를 외우는 대신, 누가 무엇을 검토해야 하는지 묻는다.
근거: [business_lab/authoring.py:32](../business_lab/authoring.py#L32),
[business_lab/authoring.py:137](../business_lab/authoring.py#L137).

Day05 폴더에서 실행한다. 02에는 모델·외부 서비스 호출이 없다.
근거: [02_freeze_dataset.py:1](../02_freeze_dataset.py#L1).

```bash
uv run python 02_freeze_dataset.py
```

### ④ 실패 원인 확인

오류가 나오면 “어느 입력의 어떤 조건이 부족했는가?”를 확인한다. 코드 검사를 제거하거나
승인 상태만 일괄 변경해서 통과시키지 않는다. 미검수 차단은 이 단계에서 확인할 정상 동작이다.
근거: [tests/test_numbered_course.py:50](../tests/test_numbered_course.py#L50).

| 읽을 코드 | 설명할 질문 |
|---|---|
| [02_freeze_dataset.py:9](../02_freeze_dataset.py#L9) | 엑셀을 읽은 값이 어디로 전달되는가? |
| [business_lab/authoring.py:99](../business_lab/authoring.py#L99) | JSON 형식 오류와 업무 검수 부족은 어떻게 다른가? |
| [business_lab/authoring.py:137](../business_lab/authoring.py#L137) | 승인 여부 외에 담당자·근거·정책·초기 상태를 어떻게 확인하는가? |

잘못된 JSON 때문에 먼저 막힌 경우에는 형식 문제를 먼저 해결한다.
검수 내용이 정책과 맞는지 판단하는 단계와 구분해서 설명한다.

### ⑤ 교차 검수

강사 안내:

> 이 기대 상태에 동의하는 근거를 적어 주세요. 원문만으로 판단할 수 없거나 서로 의견이 다르면 보류해 주세요.

정상 사례를 함께 확인한 뒤 환불 후 응답 유실과 주문 번호 보충 사례를 짝 활동으로 읽는다.
검수 상태·검수자·검수 사유뿐 아니라 기대 상태 자체가 원문과 맞는지 대조한다.
TIMEOUT 뒤 처리 원칙과 주문 번호 확인 원칙은 각각 정책 P4와 P1에서 찾는다.
근거: [business_lab/data/order_policy.md:30](../business_lab/data/order_policy.md#L30),
[business_lab/data/order_policy.md:11](../business_lab/data/order_policy.md#L11).

선택 문항 중 미합의 사례가 남으면 원인과 추가 확인 사항을 기록한다. 실행을 통과시키기 위해
사후에 어려운 문항을 빼지 않는다. 실행 범위를 바꾸려면 별도 실험으로 범위와 이유를 먼저 정한다.
근거: [../AGENTS.md](../AGENTS.md).

### ⑥ 동결 결과 확인

검수가 끝났다면 02를 다시 실행하고 생성된 JSON을 연다.

| 결과 필드 | 확인할 내용 |
|---|---|
| `snapshot.cards` | 이번 실험에 들어갈 검수 문항과 기대 상태 |
| `snapshot.fixtures` | 해당 문항을 재현할 초기 조건 |
| `snapshot.policy` | 기준으로 사용한 정책 원문 |
| `snapshot_hash` | 동결한 내용의 변경을 확인하기 위한 값 |
| `coverage` | 선택한 문항의 조건별 수; 업무 전체의 커버리지 보장은 아님 |
| `not_selected_ids` | 이번 실행 범위에 포함하지 않은 초안 |

이 필드들은 동결 단계에서 함께 저장한다.
근거: [business_lab/authoring.py:154](../business_lab/authoring.py#L154).

해시는 정답의 정확성을 인증하지 않는다. 현재 검사는 저장한 내용의 변경 여부를 확인하며,
검수 이유가 업무적으로 타당한지까지 자동 판정하지 않는다.
근거: [business_lab/authoring.py:137](../business_lab/authoring.py#L137),
[business_lab/authoring.py:163](../business_lab/authoring.py#L163).

## 4. 다음 단계로 넘어가는 기준

수강생이 다음 질문에 자기 말로 답하도록 한다.

1. 이 문항의 기대 상태는 어떤 원문과 초기 상태에서 나왔나요?
2. 미검수 문항을 실행 데이터에 넣으면 어떤 문제가 생기나요?
3. 실험 도중 기대 상태를 바꾸면 이전 결과와 그대로 비교할 수 있을까요?

03은 02의 동결 데이터를 읽어 baseline을 실행한다. 이 시간에는 해당 입력 연결만 확인하고,
실제 모델 실행은 03에서 실행 규모를 확인한 뒤 진행한다.
근거: [03_record_baseline.py:9](../03_record_baseline.py#L9).
