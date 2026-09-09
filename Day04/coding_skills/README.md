# 코딩 Skill: 실제 파일을 고치고 검사하기

**완성 목표:** 같은 프로젝트에서 버그 수정·기능 추가·수정 없는 리뷰를 구분하는 Skill을 작성하고,
실제 파일과 실행 기록으로 결과를 설명한다. 기준 Skill 세 개를 실행한 뒤 [연습 문제](../실습.md)에서 사본을 수정한다.

먼저 [Skill의 기본 구조와 좋은 작성 기준](../SKILLS_GUIDE.md)을 읽는다.
이름·description이 있는 YAML frontmatter와 Markdown 본문을 유지하고, 보조 자료는 상대 경로로 연결한다.
형식을 유지한 다음 적용 조건·절차·판단 기준·실제 검사 방법을 개선한다.

## 원본에서 가져온 구조

[Deep Agents Code](https://github.com/langchain-ai/deepagents/tree/main/libs/code/deepagents_code)의 코드를
2026-09-09에 확인한 커밋 `fc91199`에 고정해 참고했다.

| 원본의 핵심 | 원본 위치 | 이번 실습 |
|---|---|---|
| 실제 로컬 파일·명령 실행 | [agent.py:2993](https://github.com/langchain-ai/deepagents/blob/fc91199a44b99990cca49341169aea858da222fc/libs/code/deepagents_code/agent.py#L2993) | `LocalShellBackend`로 프로젝트 수정·테스트 실행 |
| 경로에 따라 백엔드 연결 | [agent.py:3154](https://github.com/langchain-ai/deepagents/blob/fc91199a44b99990cca49341169aea858da222fc/libs/code/deepagents_code/agent.py#L3154) | 프로젝트·Skill·보고서 경로를 `CompositeBackend`로 연결 |
| Skill 발견·적용 | [agent.py:2960](https://github.com/langchain-ai/deepagents/blob/fc91199a44b99990cca49341169aea858da222fc/libs/code/deepagents_code/agent.py#L2960), [skills/load.py](https://github.com/langchain-ai/deepagents/blob/fc91199a44b99990cca49341169aea858da222fc/libs/code/deepagents_code/skills/load.py) | `create_deep_agent(skills=["/skills/"])`에 수업용 Skill 제공 |
| 개발 도구를 가진 에이전트 생성 | [agent.py:3488](https://github.com/langchain-ai/deepagents/blob/fc91199a44b99990cca49341169aea858da222fc/libs/code/deepagents_code/agent.py#L3488) | 짧은 Python CLI로 요청 하나씩 실행 |

CSV 프로그램, 세 Skill, `evidence/` 저장 방식은 이번 수업용으로 작성했다.
에이전트에 주는 개발 절차를 `SKILL.md`로 분리하고 실제 테스트 결과로 확인하는 데 집중한다.

## 1. 프로젝트 계약과 초기 실패 읽기

[Day04 준비](../README.md#준비)를 마친 뒤 모든 명령을 **Day04에서** 실행한다.

```bash
.venv/bin/python coding_skills/prepare.py
(cd work/coding && ../../.venv/bin/python -m unittest discover -s tests -v)
```

초기 결과는 **6개 실행, 기간 경계 검사 1개 실패**다. 이 명령은 모델을 호출하지 않는다.
`prepare.py`는 기존 폴더를 덮어쓰지 않는다. 다시 시작할 때는 `--workspace work/coding-02`처럼 새 이름을 쓴다.

먼저 [프로젝트 README](project/README.md)와 [기존 테스트](project/tests/test_report.py)를 대조한다.
기간의 시작일·종료일을 모두 포함하고, `paid` 행의 금액과 음수 환불을 합산한다.
이 계약이 코드와 다른 위치를 테스트의 실패 입력으로 찾는다.

## 2. 같은 에이전트에 다른 요청 보내기

각 명령은 새 대화로 시작한다. 작업 폴더의 파일은 다음 실행에도 유지된다.
아래 순서로 실행하면 리뷰에서 결함을 관찰한 뒤 수정하고, 고친 프로젝트에 기능을 추가할 수 있다.
각 에이전트 명령은 실제 모델을 호출한다.
Day04 첫 실행에서 이미 버그를 고쳤다면 새 사본을 준비하고 각 명령에 같은 `--workspace`를 지정한다.

**개념 설명:** 프로젝트 작업 없이 답하는지 확인한다.

```bash
.venv/bin/python coding_skills/agent.py --prompt 'Python의 회귀 테스트가 뭐야? 프로젝트 조사나 수정 요청은 아니야.'
```

**수정 없는 리뷰:** 실패를 확인하고 파일·줄·발생 입력을 보고하되 프로젝트는 보존한다.

```bash
.venv/bin/python coding_skills/agent.py --prompt 'CSV 매출 집계 프로그램을 README 계약과 실제 테스트로 리뷰해 줘. 코드·테스트·README는 수정하지 말고 리뷰 파일을 남겨 줘.'
```

**버그 수정:** 실패 재현, 최소 수정, 새 반례, 실제 재검사를 확인한다.

```bash
.venv/bin/python coding_skills/agent.py --prompt 'CSV 매출 집계의 기간 경계 테스트가 실패해. 실제 실패를 재현하고 원인을 고쳐 줘. 기존 tests/test_report.py는 보존하고 새 반례를 추가한 뒤 검사 결과를 보고서에 남겨 줘.'
```

**기능 추가:** 버그 수정이 끝난 작업 폴더에 실행한다.

```bash
.venv/bin/python coding_skills/agent.py --prompt 'CLI에 선택 옵션 --customer를 추가해 줘. 이름이 정확히 일치하는 고객만 집계하고, 미지정하면 기존 전체 결과를 유지해. 없는 고객은 customers={}, total="0.00"이야. 기간 양끝 포함·paid 조건·환불 합산을 유지하고, 기존 테스트는 보존해. 새 테스트 실패를 먼저 확인한 뒤 구현하고 전체 테스트와 CLI를 실행해 줘. README와 기능 보고서도 작성해 줘.'
```

| 요청 | 선택할 Skill | 필요한 보조 파일 | 결과 파일 |
|---|---|---|---|
| 일반 개념 | 없음 | 없음 | 대화 기록 |
| 버그 수정 | [debug-python](skills/debug-python/SKILL.md) | 날짜·금액·CSV 경계에 해당하면 `references/boundaries.md` | `artifacts/fix-report.md` |
| 기능 추가 | [add-python-feature](skills/add-python-feature/SKILL.md) | 프로젝트 README·소스·테스트 | `artifacts/feature-report.md` |
| 수정 없는 리뷰 | [review-python](skills/review-python/SKILL.md) | `assets/review-template.md` | `artifacts/review.md` |

## 3. 발견·읽기·실행을 따로 확인하기

출력의 `[발견한 Skill · 사용 기록 아님]`은 metadata 목록이다.
실제 적용은 `read_file`이 선택한 `SKILL.md`를 성공적으로 읽었는지부터 확인한다.
필요한 참고 파일을 읽었는지, 실제 실패를 재현했는지, 수정 뒤 테스트가 통과했는지도 이어서 본다.

```text
Skill metadata 발견
  → 필요한 SKILL.md 읽기
  → 프로젝트와 필요한 참고 파일 읽기
  → execute로 실패 재현
  → edit_file/write_file로 소스와 새 테스트 작성
  → execute로 재검사
  → show_diff → 보고서 저장
```

이는 버그 수정의 관찰 기준이다. 모델이 항상 같은 호출 순서를 보장받는 것은 아니다.
본문을 읽었어도 절차를 생략했다면 그 실행을 실패 사례로 남긴다.

## 4. 근거 파일로 결과 확인하기

실행 시작 시 출력된 `evidence/coding/<실행 ID>/`를 연다.

```text
<실행 ID>/
├── trace.jsonl         # 공개 대화, 실제 도구 요청·결과, 사용량
├── skills/             # 이 실행에 제공한 Skill과 보조 파일 사본
├── before/             # 실행 전 프로젝트
├── after/              # 실행 후 프로젝트
├── files.json          # 파일별 해시와 작업 폴더
├── changes.patch       # 실행 전후 diff
├── commands/*.json     # 실제 명령, cwd, 출력, 종료 코드, 잘림 여부
└── artifacts/          # Skill이 작성한 보고서
```

실행기는 모델의 비공개 사고 과정을 기록하지 않는다. `.env`, 가상환경, 캐시도 파일 사본에서 제외한다.
셸 출력은 기본 최대 100,000바이트이며 잘린 경우 `truncated=true`다.
명령·수정 기록은 실행기가 남기고, 원인·설계 이유는 Skill이 보고서로 작성한다.
보고서의 “통과”와 `commands/`의 종료 코드·실제 출력을 대조한다.
중간에 실패해도 그때까지의 기록과 최종 파일 상태를 남기며, 새 실행은 새 근거 폴더를 쓴다.
모델 요청의 대기 제한은 60초, 에이전트 실행 전체 제한은 210초다. 시간 초과는 실패로 기록한다.
프로세스 강제 종료나 디스크 오류에서는 최종 사본이 없을 수 있으므로 남은 기록부터 확인한다.

후속 작업에는 작업 사본과 이 근거 폴더를 함께 전달할 수 있다. 대화가 초기화되어도 사람이 변경 전후와 검사 결과를 확인할 수 있다.
이 실행기는 이전 대화를 자동 복원하지 않는다. MCP 실습에서는 [파일을 읽는 도구로 재개](../build_mcp/README.md)하는 방법을 이어서 만든다.

## 실행기에서 읽을 부분

1. [agent.py](agent.py)의 `create_deep_agent`: 모델·도구·Skill 경로·`AGENTS.md`를 한곳에 연결한다.
2. [agent.py](agent.py)의 `CompositeBackend`: 파일 도구 경로를 아래 표처럼 연결한다.
3. [run_files.py](run_files.py)의 `RecordedLocalShellBackend.execute`: 실제 셸의 반환값을 저장한다.
4. [skills/debug-python/SKILL.md](skills/debug-python/SKILL.md): 재현·수정·검사·보고 절차를 읽는다.

[llm.py](llm.py)는 이 폴더의 모델 연결 코드다. `agent.py`는 같은 폴더에서 가져오며,
환경 설정은 공통 `Day04/.env`를 읽는다.

| 도구에서 보는 경로 | 실제 위치 |
|---|---|
| `/README.md`, `/csv_report/...` | `--workspace`로 지정한 프로젝트 |
| `/skills/...` | `--skills-dir`의 Skill 파일 |
| `/artifacts/...` | 이번 근거 폴더의 `artifacts/` |
| `execute`의 현재 디렉터리 | `--workspace` 프로젝트 |

파일 도구에서는 가상 경로를, 셸에서는 프로젝트 상대 경로를 쓴다.
`python`은 실행기가 연결한 Day04 가상환경의 Python이다.
셸은 호스트에서 실행되며 `virtual_mode=True`가 셸의 파일 접근을 제한하지는 않는다.
실행기는 모델 API 키를 셸 환경에 상속하지 않고, Skill 경로의 파일 도구 쓰기를 거절한다.
프로젝트만 작업하라는 지침과 OS 수준의 격리를 같은 것으로 설명하지 않는다.

## 실제 에이전트 검사

```bash
.venv/bin/python coding_skills/verify_agent.py
```

실제 모델 약 25–45회, 약 2–5분 예상이다. 네 개의 새 에이전트 프로세스에서
개념 설명·리뷰·수정·기능 추가를 실행한다. 앞선 수정 결과를 새 폴더에 복사해 기능 추가의 입력으로 쓴다.

검사는 Skill 본문·보조 파일 읽기, 실제 실패 후 통과, 기존 테스트 보존, 새 테스트·보고서·diff를 확인한다.
별도 프로세스에서 새 날짜·고객·금액으로 수정 결과와 `--customer` CLI를 검사한다.
근거는 `evidence/coding-validation/<실행 ID>/`, 작업 사본은 `work/coding-validation/<실행 ID>/`에 남는다.
`report.json`의 실패 항목과 해당 시나리오 로그를 같이 읽는다. 이 검사는 모든 가능한 입력의 정확성을 보장하지 않는다.

## 마무리 질문

- Skill의 description과 본문은 각각 언제 필요한가?
- `AGENTS.md`에 둔 공통 규칙과 업무별 Skill의 절차는 어떻게 다른가?
- 테스트 통과·원본 테스트 보존·추가 반례가 각각 무엇을 확인하는가?
- 리뷰 Skill이 코드를 수정했다면 어느 기록으로 그 사실을 확인하는가?

다음은 [Skill을 직접 수정하는 연습](../실습.md)이다. 질문은 수업 Discord에 요청·실제 기록·수정한 경로와 함께 남긴다.
