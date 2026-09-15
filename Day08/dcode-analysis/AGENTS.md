# AGENTS.md — dcode 기능 파악 가이드

이 폴더는 LangChain **dcode(Deep Agents Code)** 와 그 기반인 **DeepAgents SDK**의 기능을 파악하기 위한 분석 작업공간이다.
이 문서는 이 폴더에서 일하는 에이전트(와 사람)가 **어떤 순서로, 무엇을 믿고, 어떻게 확인하며** 기능을 파악할지 안내한다.

답변과 새로 쓰는 문서는 **한국어**로 작성한다. 코드 식별자·경로·명령은 원문 그대로 둔다.

---

## 1. 폴더 구성과 각 자료의 성격

```text
dcode-analysis/
├── AGENTS.md          ← 이 파일. 작업 방법
├── README.md          ← 기준 커밋·규모·문서 목록 (인덱스)
├── analysis/          ← 영역별 분석 (가장 먼저 볼 곳)
│   ├── 00-overview.md     종합: 큰 그림, 설계 패턴, 문서↔코드 불일치, 버그 후보
│   └── 01~09-*.md         영역별 상세
├── docs_official/     ← 공식 문서 .md 원문 (2026-09-15 수집)
│   ├── code/              dcode 문서 17개
│   └── sdk/               SDK 문서 39개
├── scripts/
│   └── check_comments_only.py  ← 소스 변경이 "주석만"인지 AST로 검증
└── deepagents/        ← git clone, 브랜치 annotated-ko. 원본 소스 + 한국어 해설 주석 (§9)
    ├── libs/code/         dcode (deepagents_code/, tests/, ARCHITECTURE.md, THREAT_MODEL.md …)
    └── libs/deepagents/   SDK (deepagents/graph.py, middleware/, backends/, profiles/)
```

| 자료 | 무엇을 알려주나 | 한계 |
|---|---|---|
| `analysis/` | 기능 위치·흐름·설계 이유·문서와의 차이를 **요약** | 사람이 아닌 에이전트가 정적 분석으로 작성. `(추정)` 표시 부분은 미검증. 줄 번호는 기준 커밋 기준 |
| `docs_official/` | 제품이 **의도한** 동작, 사용법, 설정 키 | 코드와 어긋나는 곳이 확인됨 (`00-overview.md` §5) |
| `libs/code/*.md` (저장소 개발 문서) | 개발자 관점 설계 원칙, 위협 모델 | 일부 구식 (THREAT_MODEL 도구명 등) |
| `deepagents/` 소스 | **실제** 동작 | 20만 줄 이상. 전체를 읽지 말고 찾아 들어간다 |
| `libs/code/tests/unit_tests/` (147개 항목) | 동작이 **의도된 것인지** 여부 | 테스트가 없는 동작도 많다 |

### 신뢰 순서

> **소스 코드 > 테스트 > `analysis/` > 저장소 개발 문서 > 공식 문서**

- 공식 문서와 코드가 다르면 **코드가 실제 동작**이다. 다만 "누가 틀렸나"(문서 오류 vs 코드 버그)는 docstring·테스트·CHANGELOG로 판단한다.
  - 예: API 키 우선순위는 코드 docstring이 의도를 명시하므로 문서 쪽 오류일 가능성이 높다.
- `analysis/`의 주장을 답변의 결론으로 쓰기 전에, 핵심 한두 개는 인용된 `path:line`을 **직접 열어 확인**한다.

---

## 2. 기본 작업 흐름: "기능 X는 어떻게 동작하나?"

```mermaid
flowchart LR
    Q["질문: 기능 X"] --> IDX["§3 색인에서<br/>영역 번호 찾기"]
    IDX --> AN["analysis/NN 읽기<br/>요약·흐름·대조표"]
    AN --> DOC["docs_official<br/>의도 확인"]
    DOC --> SRC["deepagents/ 소스<br/>path:line 검증"]
    SRC --> TEST["tests/ 로<br/>의도 여부 확인"]
    TEST --> ANS["답변<br/>근거·검증수준 명시"]
```

1. **영역 찾기** — §3 색인에서 키워드로 영역 번호를 찾는다. 여러 영역에 걸치면 `00-overview.md` §4(설계 패턴)·§7(경계표)부터 본다.
2. **분석 문서 읽기** — `analysis/NN-*.md`는 모두 같은 틀이다. 목적에 맞는 섹션으로 바로 간다.

   | 알고 싶은 것 | 볼 섹션 |
   |---|---|
   | 이 기능이 뭘 약속하나 | `## 문서가 약속하는 것` |
   | 코드가 어디 있나 | `## 코드 지도` |
   | 실행 순서 | `## 동작 흐름` (mermaid 포함) |
   | 왜 이렇게 만들었나 | `## 핵심 설계 포인트` |
   | 문서와 다른 점 | `## 문서 ↔ 코드 대조` |
   | dcode가 만든 것 vs SDK가 준 것 | `## dcode ↔ SDK 경계` |
   | 아직 모르는 것 | `## 더 볼 거리` |

3. **공식 문서로 의도 확인** — 분석 문서가 인용한 `docs_official/...` 파일의 해당 절을 읽는다.
4. **소스로 검증** — 분석 문서의 `path:line`을 연다. 경로는 **저장소 루트(`deepagents/`) 기준**이다.
5. **테스트로 의도 확인** — `libs/code/tests/unit_tests/test_<모듈명>.py`를 먼저 찾는다 (예: `auto_mode.py` → `test_auto_mode.py`).
6. **답변** — §5 형식을 따른다.

`analysis/`에 없는 기능이라면 §4의 탐색 요령으로 소스에서 직접 찾고, 결과가 크면 해당 영역 문서의 `## 더 볼 거리`에 추가할지 사용자에게 묻는다.

---

## 3. 기능 색인

키워드 → 분석 문서 → 공식 문서 → 코드 진입점. 경로 접두어: `C/` = `deepagents/libs/code/deepagents_code/`, `S/` = `deepagents/libs/deepagents/deepagents/`.

| 키워드 | 분석 | 공식 문서 (`docs_official/`) | 코드 진입점 |
|---|---|---|---|
| 실행·CLI 플래그·헤드리스(`-n`)·resume·세션·스레드 | 01 | `code/quickstart.md`, `code/cli-reference.md` | `C/main.py` (`cli_main`), `C/client/non_interactive.py` |
| 클라이언트/서버 분리·langgraph 서버 기동 | 01 | `code/overview.md`, 저장소 `ARCHITECTURE.md` | `C/client/launch/server_manager.py`, `C/client/launch/server.py`, `C/server_graph.py`, `C/_server_config.py` |
| 체크포인트·`sessions.db`·워크스페이스 바인딩 | 01 | (문서 없음) | `C/workspace.py`, `C/offload_api.py` |
| 에이전트 조립·미들웨어 순서 | 02 | `sdk/customization.md`, `sdk/overview.md` | `C/agent.py` (`create_cli_agent`), `S/graph.py` (`create_deep_agent`) |
| 파일 도구·백엔드·`virtual_mode` | 02 | `sdk/backends.md`, `sdk/tools.md` | `S/middleware/filesystem.py`, `S/backends/` |
| 컨텍스트 압축·요약·`/offload`·`compact_conversation` | 02 | `sdk/context-engineering.md`, `code/configuration.md` | `C/offload_middleware.py`, `S/middleware/summarization.py` |
| 설정 계층·`config.toml`·`.env`·`/reload` | 03 | `code/configuration.md`, `code/config-file.md` | `C/configuration/resolver.py`, `C/config.py`, `C/config_manifest.py` |
| 모델·프로바이더·`/model`·재시도·비용 | 03 | `code/providers.md`, `sdk/models.md`, `sdk/profiles.md` | `C/model_config.py`, `C/configurable_model.py`, `C/model_retry.py`, `C/cost_tracking.py`, `S/profiles/` |
| API 키·`/auth`·자격증명 | 03 | `code/credentials.md` | `C/model_config.py` (`resolve_provider_credential`) |
| 승인 모드(Manual/Auto/YOLO)·분류기·HITL | 04 | `code/approval-modes.md`, `sdk/human-in-the-loop.md` | `C/auto_mode.py`, `C/agent.py` |
| 권한·보안 경계·위협 모델 | 04 | `sdk/permissions.md`, 저장소 `libs/code/THREAT_MODEL.md` | `C/tools.py` (SSRF 가드), `S/middleware/_fs_interrupt.py` |
| 서브에이전트(`task`)·fork·비동기 서브에이전트 | 05 | `code/subagents.md`, `sdk/subagents.md`, `sdk/async-subagents.md` | `S/middleware/subagents.py`, `S/middleware/async_subagents.py`, `C/agent.py` |
| 목표(`/goal`)·루브릭·grader | 05 | `code/goals-and-rubrics.md`, `sdk/rubric.md` | `C/goal_rubric.py`, `C/goal_tools.py`, `C/reliable_rubric.py`, `S/middleware/rubric.py` |
| 메모리·`AGENTS.md`·`/remember` | 06 | `code/memory-and-skills.md`, `sdk/memory.md` | `S/middleware/memory.py`, `C/agent.py` |
| 스킬·`SKILL.md`·`/skill:`·built-in 스킬 | 06 | `code/memory-and-skills.md`, `sdk/skills.md` | `S/middleware/skills.py`, `C/skills/`, `C/built_in_skills/` |
| MCP 서버·OAuth | 07 | `code/mcp-tools.md`, `sdk/mcp.md` | `C/mcp_tools.py`, `C/mcp_auth.py`, `C/mcp_providers/` |
| 훅(`hooks.json`)·이벤트 | 07 | `code/hooks.md`, 저장소 `HOOKS.md` | `C/hooks/` (`server_middleware.py`, `client.py`) |
| Python 확장·플러그인 | 07 | `code/extensions.md`, `code/plugins.md`, 저장소 `EXTENSIONS.md` | `C/extensions/` (`hosting.py`), `C/plugins/` |
| 원격 샌드박스·setup 스크립트·`execute` | 08 | `code/remote-sandboxes.md`, `sdk/sandboxes.md` | `C/integrations/` (`sandbox_factory.py`, `sandbox_registry.py`), `S/backends/sandbox.py` |
| JS 인터프리터(`js_eval`) | 08 | `sdk/interpreters.md` | `grep -rn quickjs C/` |
| TUI 화면·입력·단축키 | 09 | `code/cli-reference.md` | `C/app.py`, `C/tui/` |
| 슬래시 커맨드 목록·동작 | 09 | 저장소 `COMMANDS.md` | `C/command_registry.py` (선언), `C/app.py` (실행 분기) |
| 스트림 이벤트 → 화면 | 09 | `sdk/streaming.md`, `sdk/event-streaming.md` | `C/tui/textual_adapter.py` (`execute_task_textual`) |
| ACP(에디터 연동) | 09 | `sdk/acp.md` | `C/acp.py`, `deepagents/libs/acp/` |

---

## 4. 소스 탐색 요령

코드가 크다 (`app.py` 3만 줄, `tui/` 4.8만 줄). 파일을 처음부터 읽지 않는다.

- **파일 구조부터**: `grep -n '^class \|^def \|^async def ' <파일>`로 목차를 만든 뒤 필요한 범위만 읽는다.
- **설정 키·환경변수**: `C/_env_vars.py`(env 이름 상수)와 `C/config_manifest.py`(키 정의·기본값)를 먼저 grep한다.
- **커맨드 추적**: `C/command_registry.py`에서 이름을 찾고 → `C/app.py`에서 같은 문자열로 실행 분기를 찾는다.
- **미들웨어 추적**: `C/agent.py`에서 클래스명을 찾고 → 병합 규칙은 `S/graph.py`에서 확인한다. dcode는 **미들웨어 `name`을 SDK와 같게 만들어 슬롯을 교체**하는 수법을 쓰므로, 클래스명보다 `name` 속성을 본다.
- **서버/클라이언트 어느 쪽인가**: 표시·입력은 클라이언트(`app.py`, `tui/`, `client/`), 모델·도구·메모리는 서버(`server_graph.py`, `agent.py`, 미들웨어). 예외: `--acp`는 인프로세스, 훅 실행은 항상 클라이언트.
- **SDK 쪽인지 dcode 쪽인지**: `00-overview.md` §7 경계표를 먼저 본다.
- **변경 이력**: `git -C deepagents log --oneline -- <경로>`, 그리고 `libs/code/CHANGELOG.md`·`docs_official/code/changelog.md`.

---

## 5. 답변 형식

기능을 설명할 때는 다음을 포함한다.

1. **한 줄 요약** — 이 기능이 무엇이고 어디서 동작하는지 (클라이언트/서버, dcode/SDK).
2. **동작 흐름** — 단계별, 각 단계에 `path:line`. 복잡하면 mermaid.
3. **문서와의 차이** — 있으면 반드시 언급하고, `00-overview.md` §5에 이미 있는 항목이면 번호(S1, F3 …)를 붙인다.
4. **검증 수준** — 다음 중 하나를 명시한다.
   - `소스 확인`: 이번에 직접 코드를 열어 확인함
   - `분석 문서 인용`: `analysis/` 내용을 재확인 없이 옮김
   - `추정`: 코드 읽기만으로 추론, 실행 검증 필요

`path:line`은 **저장소 루트 기준**으로 쓴다 (예: `libs/code/deepagents_code/agent.py:2869`).

---

## 6. 규칙

### 해야 할 것

- `deepagents/`는 브랜치 **`annotated-ko`** 에서 dcode 소스에 한국어 해설 주석(`# [해설]`)을 직접 달아 둔 상태다. 그래서 **작업 트리의 줄 번호는 `analysis/`의 `path:line`과 다르다.**
  - `analysis/`의 줄 번호는 기준 태그 **`baseline-1d3232c`** 기준이다. 인용 줄을 정확히 보려면:
    `git -C deepagents show baseline-1d3232c:libs/code/deepagents_code/agent.py | sed -n '2860,2875p'`
  - 주석과 함께 읽을 때는 작업 트리 파일에서 **심볼명으로** 찾는다.
  - 주석만 보고 싶으면: `git -C deepagents diff baseline-1d3232c -- libs/code`
- `(추정)` 표시가 붙은 주장은 결론으로 단정하지 않는다.
- `(추정)` 표시가 붙은 주장은 결론으로 단정하지 않는다.
- 새로 확인한 사실이 분석 문서와 다르면, 답변에서 차이를 밝히고 **분석 문서를 고칠지 사용자에게 묻는다**.

### 하지 말 것

- `deepagents/` 안의 **코드**를 수정하지 않는다. 허용되는 변경은 §9 규칙을 따른 `# [해설]` 주석 추가뿐이며, 수정 후 반드시 `scripts/check_comments_only.py`로 검증한다.
- `git pull`·`checkout` 등으로 기준 커밋을 옮기지 않는다. 업스트림 비교는 `git fetch` 후 `git log 1d3232c..origin/main -- libs/code libs/deepagents`로만 본다.
- dcode를 실행해 버그 후보를 검증하는 일은 사용자 요청이 있을 때만 한다. 원격 샌드박스 검증은 **과금**이 발생할 수 있다.
- `docs_official/`을 손으로 고치지 않는다. 원문 스냅샷이다. 갱신은 다시 내려받는다.
- API 키·`.env` 값을 출력하거나 분석 문서에 적지 않는다.

---

## 7. 분석 문서를 갱신·추가할 때

- 새 영역은 `analysis/10-<주제>.md`로 만들고, 기존 문서와 **같은 8개 섹션 틀**을 따른다 (`# 제목` + 요약, 그 뒤 §2의 `##` 섹션 7개).
- 추가·수정 후 `README.md`의 문서 목록과 `00-overview.md`(색인·불일치 표·경계표)를 함께 갱신한다. 이 파일 §3 색인에도 키워드를 추가한다.
- 문서↔코드 불일치를 새로 찾으면 `00-overview.md` §5 표에 번호를 이어 붙이고 **검증** 열을 채운다.
- 기준 커밋을 옮겨 재분석했다면 `README.md`의 기준 표와 이 파일 §6의 커밋 해시를 함께 바꾼다.

---

## 8. 처음 온 사람을 위한 추천 경로

| 목표 | 경로 | 예상 분량 |
|---|---|---|
| 30분 안에 전체 감 잡기 | `00-overview.md` §1·§3·§7 → `libs/code/ARCHITECTURE.md` | 문서 2개 |
| 에이전트 루프 구조 이해 | 01 → 02 → `S/graph.py`의 `create_deep_agent` 직접 읽기 | 반나절 |
| 확장 기능을 만들고 싶다 | 07 → 06 → `libs/code/examples/` | 반나절 |
| 보안·운영 검토 | 04 → 08 → `00-overview.md` §5.1·§6 → `THREAT_MODEL.md` | 하루 |
| SDK만 쓰려는 경우 dcode에서 배울 점 | `00-overview.md` §4 설계 패턴 → 02·05·06의 `## dcode ↔ SDK 경계` | 반나절 |

---

## 9. 소스 속 해설 주석 (`# [해설]`)

`deepagents/`의 dcode 핵심 모듈에는 한국어 해설 주석이 직접 달려 있다. 분석 문서가 "지도"라면 해설 주석은 "현장 안내판"이다. 코드를 열었을 때 바로 옆에서 역할·흐름·설계 이유를 읽을 수 있다.

### 태그

| 태그 | 의미 |
|---|---|
| `# [해설]` | 일반 설명. 모듈 머리말에는 역할·실행 프로세스·진입점·관련 `analysis/` 문서가 있다 |
| `# [해설][흐름] 1) …` | 긴 함수 안의 단계 표시 |
| `# [해설][설계]` | 왜 이렇게 만들었는지 |
| `# [해설][SDK]` | SDK 쪽 대응 심볼 연결 |
| `# [해설][문서 불일치]` | 공식 문서와 다른 동작 (`00-overview.md` §5와 대응) |
| `# [해설][주의]` | 함정·위험·버그 후보 |
| `(추정)` | 코드 읽기만으로 추론한 내용 |

### 활용법

```bash
# 한 모듈의 머리말과 구조 설명만 훑기
grep -n '\[해설\]' deepagents/libs/code/deepagents_code/agent.py | head -40
# 문서 불일치·주의 지점만 모아 보기
grep -rn '\[해설\]\[문서 불일치\]\|\[해설\]\[주의\]' deepagents/libs/code/deepagents_code
# 한 함수의 실행 단계만 보기
grep -n '\[해설\]\[흐름\]' deepagents/libs/code/deepagents_code/main.py
# 주석을 뺀 원본과 비교
git -C deepagents diff baseline-1d3232c -- libs/code/deepagents_code/agent.py
```

### 주석이 달린 범위 (핵심 흐름 우선)

| 영역 | 파일 (`libs/code/deepagents_code/` 기준) |
|---|---|
| 01 부팅·서버 | `__init__.py`, `main.py`, `server_graph.py`, `_server_config.py`, `workspace.py`, `client/launch/server.py`, `client/launch/server_manager.py`, `client/non_interactive.py` |
| 02 조립·컨텍스트 | `agent.py`, `tools.py`, `offload_middleware.py`, `local_context.py` |
| 03 설정·모델 | `config.py`, `model_config.py`, `configurable_model.py`, `model_retry.py`, `configuration/resolver.py`, `_env_vars.py` |
| 04·05 승인·목표 | `auto_mode.py`, `_ask_user_types.py`, `goal_rubric.py`, `goal_tools.py`, `reliable_rubric.py` |
| 06 스킬 | `skills/load.py`, `skills/invocation.py`, `plugins/adapters/skills_middleware.py` |
| 07 MCP·훅·확장 | `mcp_tools.py`, `mcp_auth.py`, `hooks/server_middleware.py`, `hooks/client.py`, `extensions/hosting.py`, `extensions/loader.py` |
| 08 샌드박스 | `integrations/sandbox_factory.py`, `integrations/sandbox_registry.py`, `integrations/sandbox_provider.py` |

주석이 없는 파일(`app.py`, `tui/`, 나머지 `hooks/`·`plugins/`·`client/commands/` 등)은 `analysis/` 문서와 §4 탐색 요령으로 읽는다.

### 주석을 추가할 때의 규칙

1. **`#` 주석 줄만 추가**한다. 코드·docstring·문자열·기존 주석·빈 줄은 건드리지 않는다.
2. 모든 줄은 `# [해설]`로 시작하고 들여쓰기를 주변 코드에 맞춘다. 여러 줄 문자열 안에는 넣지 않는다.
3. 다른 코드는 **줄 번호가 아니라 심볼명·파일 경로**로 가리킨다 (주석이 늘면 줄 번호가 밀린다).
4. 밀도: 모듈 머리말(5~15줄) + 모든 클래스·함수 위 1~5줄 + 긴 함수 안 `[흐름]` 단계 표시.
5. 추가 후 검증 (실패하면 주석이 문자열 안에 들어간 경우가 대부분):
   ```bash
   python3 scripts/check_comments_only.py libs/code/deepagents_code/<파일>.py
   python3 scripts/check_comments_only.py --changed   # 바뀐 dcode 파일 전체
   ```
6. 새로 주석을 단 파일은 위 범위 표에 추가한다.

---

## 10. GitNexus 코드 그래프

`deepagents/` 저장소 전체가 GitNexus로 인덱싱되어 있다. grep이 "어디에 문자열이 있나"를 알려준다면, GitNexus는 **"누가 누구를 호출하나, 어떤 실행 흐름에 속하나"** 를 알려준다. `app.py`(3만 줄)처럼 grep만으로 흐름을 잡기 어려운 곳에서 특히 쓸모 있다.

| 항목 | 값 |
|---|---|
| 레지스트리 이름 | **`deepagents-dcode`** (MCP 도구의 `repo` 인자에 넣는다) |
| 인덱스 위치 | `deepagents/.gitnexus/` (`.git/info/exclude`로 git에서 제외) |
| 인덱싱 대상 | 작업 트리 = 브랜치 `annotated-ko` (해설 주석 포함, 줄 번호도 작업 트리 기준) |
| 옵션 | `--index-only` (저장소의 upstream `AGENTS.md`·`.claude/` 미변경), `--max-file-size 4096` (1.3MB인 `app.py` 포함), 임베딩·PDG 없음 |
| 실행 파일 | `/Users/jhj/.nvm/versions/node/v24.18.0/bin/gitnexus` (Claude Code MCP 서버와 같은 바이너리) |

### 어떤 질문에 무엇을 쓰나

| 질문 | 도구 (MCP) | 예시 |
|---|---|---|
| "X 기능의 실행 흐름은?" | `query` | `search_query="auto approval classifier review"` |
| "이 함수를 누가 부르고, 무엇을 부르나?" | `context` | `name="create_cli_agent"`, `file_path="libs/code/deepagents_code/agent.py"` |
| "이 심볼을 바꾸면 어디가 영향받나?" | `impact` | `create_model`의 upstream 호출자 확인 |
| "특정 흐름 전체 추적" | 리소스 `gitnexus://repo/deepagents-dcode/process/{name}` | `query` 결과의 프로세스 이름 사용 |
| 구조 질의 (파일 간 호출 집계 등) | `cypher` | 모듈 간 CALLS 엣지 수 |

CLI로도 같은 인덱스를 쓸 수 있다: `gitnexus status` (저장소 루트에서), `gitnexus list`.

### 기존 자료와 함께 쓰는 순서

1. §3 색인·`analysis/`로 **영역과 진입 심볼**을 찾는다.
2. GitNexus `context`로 그 심볼의 **호출자·피호출자**를 넓힌다. 분석 문서가 놓친 호출 경로를 찾는 데 쓴다.
3. 소스에서 `# [해설]` 주석과 함께 읽는다.
4. GitNexus 결과는 정적 해석이다. 동적 import·이름 기반 미들웨어 교체·LangGraph 문자열 노드명처럼 **런타임에 결정되는 연결은 빠질 수 있으므로**, 결론 전에 소스로 확인한다.
   - **확인된 사례**: `context(name="create_cli_agent")`의 호출자에 `main.py:build_agent`(ACP)와 테스트만 나오고, 실제 주 경로인 `server_graph.py`는 **빠진다**. `server_graph.py`가 함수 본문 안에서 `from deepagents_code.agent import create_cli_agent`로 지연 import하기 때문이다. dcode는 기동 속도를 위해 이런 **함수 내부 import를 광범위하게 쓰므로**, "호출자 없음/적음" 결과는 반드시 `grep -rn '<심볼명>' libs/code/deepagents_code`로 교차 확인한다.

### 인덱스 갱신

소스에 주석을 더 달거나 기준 커밋을 옮기면 저장소 루트에서 다시 인덱싱한다.

```bash
cd deepagents
export PATH=/Users/jhj/.nvm/versions/node/v24.18.0/bin:$PATH
gitnexus analyze --index-only --max-file-size 4096 --name deepagents-dcode .
```

- `--index-only`를 빼면 upstream `AGENTS.md`에 GitNexus 섹션이 주입되고 `.claude/skills/`가 생긴다. 넣지 않는다.
- 제어/데이터 의존(taint·"무엇이 이 문장을 가드하나")이 필요하면 `--pdg`를 추가한다 (시간이 더 걸린다).
- 재인덱싱 후 MCP가 옛 인덱스를 보면 Claude Code를 재시작한다.
