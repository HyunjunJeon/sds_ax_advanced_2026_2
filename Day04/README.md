# Day04 · 두 Deep Agent로 배우는 Skills와 MCP

Day04는 **코딩 에이전트**와 **질문 강화형 에이전트** 두 개를 만들며 Skills와 MCP를 배운다.
두 에이전트 모두 Deep Agents로 실행한다. 첫 실습에서 업무 절차를 Skill로 분리하고,
두 번째 실습에서 그 절차를 실제 MCP 도구·상태 저장·근거 파일과 연결한다.

| 순서 | 에이전트 | 핵심 학습 | 실습 자료 |
|---|---|---|---|
| 1. Skills | 코딩 에이전트 | Skill 선택·본문·참고 자료 → 실제 실패 재현·수정·테스트·리뷰 | [실행과 코드 읽기](coding_skills/README.md), [연습 문제 6개](실습.md) |
| 2. MCP | 질문 강화형 에이전트 | 부족한 요구사항 질문 → 답변 기록·명세 확정 → 근거 파일로 후속 작업 | [Deep Agent와 MCP 서버 만들기](build_mcp/README.md) |

여기서 **질문 강화**는 사용자의 요청을 목표·제약·완료 기준으로 구체화하는 과정이다.
Skill은 무엇을 묻고 어떤 순서로 일할지 안내한다. 도구는 실제 작업을 수행하며,
MCP는 에이전트가 별도 서버의 도구 목록을 읽고 호출·응답을 주고받게 한다.
두 실습 모두 최종 답변을 실제 파일과 도구 실행 근거로 확인한다.

공통 읽기 자료는 [Skill의 기본 구조와 좋은 작성 기준](SKILLS_GUIDE.md),
[MCP 서버·Stateless·업무 상태 설명](build_mcp/MCP_GUIDE.md)이다.

첫 실습은 [Deep Agents Code](https://github.com/langchain-ai/deepagents/tree/main/libs/code/deepagents_code)의
프로젝트 파일·로컬 셸·Skill 연결 방식을 작은 수업용 실행기에 적용했다.
원본 코드와 이번 실습의 대응 관계는 [코딩 실습 안내](coding_skills/README.md#원본에서-가져온-구조)에 있다.

## 준비

아래 명령은 저장소의 **Day04 폴더에서** 실행한다. Day02·Day03의 가상환경과 섞지 않는다.

```bash
cd Day04
uv venv --python 3.13 .venv
uv pip install -p .venv/bin/python -r requirements.txt
cp -n .env.example .env
```

기존 `.venv`가 있으면 생성은 건너뛴다. `.env`에 `OPENROUTER_API_KEY`를 직접 입력한다.
모델 설정은 [.env.example](.env.example)을 확인한다.
각 실습 폴더의 [코딩 모델 연결](coding_skills/llm.py)과 [MCP 모델 연결](build_mcp/llm.py)이
공통 `Day04/.env`를 읽는다. 파이썬 실행 코드는 각 실습 폴더 안에 두고 가상환경·설정은 Day04에서 함께 사용한다.
모델은 `langchain-openrouter` 패키지와 `init_chat_model`로 불러온다.
`OPENROUTER_API_KEY`를 설정하고, 모델을 바꿀 때는 `OPENROUTER_MODEL`에 모델 ID를 적는다.
기본값은 `openai/gpt-5.6-luna`다. 이전 설정 파일의 `OPENAI_MODEL`은 `OPENROUTER_MODEL`로 옮긴다.
`OPENAI_BASE_URL`과 `OPENAI_API_KEY`는 이 실습의 OpenRouter 연결에 사용하지 않는다.
에이전트 실행은 실제 모델을 여러 번 호출하며 해당 계정의 크레딧을 사용한다.
원본 테스트와 MCP 서버의 직접 검사는 모델을 호출하지 않는다.

코딩 실행기의 `execute`는 현재 Mac의 로컬 셸에서 실행된다. 파일 도구의 가상 경로는 OS 샌드박스가 아니다.
이 실습에서는 준비 명령으로 만든 프로젝트 사본을 사용한다. 경로 대응은 [실행기 구조](coding_skills/README.md#실행기에서-읽을-부분)를 확인한다.

## 첫 실행: 코딩 에이전트

각 실습의 `llm.py`가 공통 환경 설정을 읽고 아래 방식으로 모델을 생성한다.

```python
from langchain.chat_models import init_chat_model

model = init_chat_model(
    model="openai/gpt-5.6-luna",
    model_provider="openrouter",
    temperature=0,
    timeout=60000,  # langchain-openrouter의 단위: 밀리초
)
```

`model_provider`는 연결 패키지, `model`은 OpenRouter에서 호출할 모델을 지정한다.
이 객체를 `create_deep_agent(model=model, ...)`에 전달한다.
`llm.py`의 `chat_model(timeout=60)`은 초 단위를 받아 60,000밀리초로 변환한다.
[LangChain 모델 가이드](https://docs.langchain.com/oss/python/langchain/models#initialize-a-model),
[OpenRouter 연동 가이드](https://docs.langchain.com/oss/python/integrations/chat/openrouter)

```bash
.venv/bin/python coding_skills/prepare.py
.venv/bin/python coding_skills/agent.py --prompt 'CSV 매출 집계의 기간 경계 테스트가 실패해. 원인을 재현하고 고쳐 줘. 기존 테스트는 보존하고 새 반례를 추가한 뒤 실제 검사 결과를 보고서에 남겨 줘.'
```

준비 명령은 `work/coding/`에 새 작업 사본을 만든다. 초기 테스트는 6개 중 1개가 실패한다.
실행기는 `evidence/coding/<실행 ID>/`에 공개 대화·도구 호출, 실행 명령·출력·종료 코드,
수정 전후 파일, diff, 보고서를 저장한다. 수정한 Skill도 함께 보존한다.
에이전트의 최종 답변과 실제 테스트 결과가 일치하는지 확인한다.

## 이어서 실행: 질문 강화형 에이전트

코딩 Skill 실습과 연습 문제를 마친 뒤 [MCP 실습의 1–6단계](build_mcp/README.md)를 진행한다.
아래는 기준 구현을 확인하는 명령이다. 첫 명령은 모델 없이 서버 계약을 검사하고,
두 번째는 실제 모델을 호출해 부족한 요구사항을 질문한다.

```bash
.venv/bin/python build_mcp/verify.py
.venv/bin/python build_mcp/agent.py --prompt '주간 CSV 매출 요약 도구를 만들고 싶어. 부족한 요구사항을 질문으로 구체화해 줘.'
```

에이전트가 알려준 세션 ID로 답변을 이어가고, 정리한 명세와 후속 계획을 근거 파일로 남긴다.
명세 확정과 프로그램 구현 완료는 구별한다. 이 동작을 기준으로 학생이 같은 계약의 MCP 서버와 Skill을 직접 만든다.

## 필요한 파일

| 파일·폴더 | 역할 |
|---|---|
| [coding_skills/skills/](coding_skills/skills/) | 버그 수정·기능 추가·코드 리뷰 Skill과 참고 자료 |
| [coding_skills/project/](coding_skills/project/) | 실패가 포함된 CSV 집계 프로그램과 기존 계약 검사 |
| [coding_skills/agent.py](coding_skills/agent.py) | Deep Agent, Skill 발견, 파일 도구, 실제 셸 연결 |
| [coding_skills/run_files.py](coding_skills/run_files.py) | 수정 전후 파일과 셸 실행 근거 저장 |
| [coding_skills/verify_agent.py](coding_skills/verify_agent.py) | 실제 모델로 네 시나리오 실행·독립 검사 |
| [build_mcp/agent.py](build_mcp/agent.py), [질문 Skill](build_mcp/skills/requirements-interview/SKILL.md) | 질문으로 요구사항을 구체화하고 MCP 도구를 사용하는 에이전트 |
| [build_mcp/reference/](build_mcp/reference/), [구현 안내](build_mcp/README.md) | MCP 서버·명세 상태·근거 파일 저장을 직접 만드는 실습 |
| [coding_skills/llm.py](coding_skills/llm.py), [build_mcp/llm.py](build_mcp/llm.py) | 각 실습의 모델 연결·공통 환경 설정 읽기 |
| [build_mcp/mcp_langchain.py](build_mcp/mcp_langchain.py) | MCP 도구의 LangChain 도구 변환 |
| `work/`, `evidence/` | 학생 작업 사본과 실행 근거. Git 제출 대상에서 제외 |

## 실습을 마칠 때

다섯 줄을 남긴다: **요청 / 관찰한 호출·결과 / 수정 위치와 이유 / 재실행 결과 / 아직 확인하지 못한 것**.
코드가 통과한 것과 Skill의 절차를 따른 것은 각각 확인한다. 실패한 실행도 보존한다.
질문은 수업 Discord에 현재 단계·기대한 결과·실제 결과·관련 근거 경로를 함께 남긴다.

Skill 논문은 선택 자료인 [advanced_study/](advanced_study/)에서 읽는다.
