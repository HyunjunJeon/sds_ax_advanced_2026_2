# dcode (Deep Agents Code) 소스 분석

LangChain의 DeepAgents SDK 기반 코딩 에이전트 `dcode`를 공식 문서와 대조하며 분석한 자료.

## 분석 기준

| 항목 | 값 |
|---|---|
| 저장소 | https://github.com/langchain-ai/deepagents (모노레포) |
| 커밋 | `1d3232c0852c47af09119edea10eeec887e4f0da` (2026-09-14, `release(deepagents-code): 0.1.69`) |
| dcode | `libs/code/` — `deepagents-code` 0.1.69, 진입점 `dcode = deepagents_code:cli_main` |
| SDK | `libs/deepagents/` — `deepagents` 0.7.14 (dcode가 `==0.7.14`로 고정) |
| 공식 문서 수집일 | 2026-09-15 (`docs.langchain.com/llms.txt` 기준 `.md` 원문) |

## 폴더 구성

```text
dcode-analysis/
├── AGENTS.md            ← 이 폴더로 기능을 파악하는 방법 (작업 가이드)
├── README.md            ← 이 파일 (인덱스)
├── scripts/             ← check_comments_only.py (주석만 바뀌었는지 AST 검증)
├── deepagents/          ← git clone, 브랜치 annotated-ko (핵심 모듈에 # [해설] 주석)
│                          원본 기준은 태그 baseline-1d3232c
│                          .gitnexus/ = GitNexus 코드 그래프 (레지스트리 이름 deepagents-dcode)
├── docs_official/
│   ├── code/            ← dcode 공식 문서 17개
│   └── sdk/             ← DeepAgents SDK 공식 문서 39개
└── analysis/            ← 영역별 분석 문서
```

## 분석 문서 목록

| # | 문서 | 영역 | 주 대조 문서 |
|---|---|---|---|
| 00 | [00-overview.md](analysis/00-overview.md) | 전체 아키텍처 종합 | — |
| 01 | [01-boot-client-server.md](analysis/01-boot-client-server.md) | 진입점·부팅·클라이언트/서버·세션 | overview, quickstart, cli-reference |
| 02 | [02-agent-assembly-sdk-core.md](analysis/02-agent-assembly-sdk-core.md) | 에이전트 조립·미들웨어 스택·백엔드·컨텍스트 관리 | SDK customization, backends, context-engineering |
| 03 | [03-config-models-credentials.md](analysis/03-config-models-credentials.md) | 설정 계층·모델/프로바이더·자격증명·비용 | configuration, config-file, credentials, providers |
| 04 | [04-approval-hitl-security.md](analysis/04-approval-hitl-security.md) | 승인 모드·HITL·권한·위협 모델 | approval-modes, THREAT_MODEL |
| 05 | [05-subagents-goals-rubrics.md](analysis/05-subagents-goals-rubrics.md) | 서브에이전트·목표·루브릭 | subagents, goals-and-rubrics |
| 06 | [06-memory-skills.md](analysis/06-memory-skills.md) | 메모리·스킬 | memory-and-skills |
| 07 | [07-mcp-hooks-extensions-plugins.md](analysis/07-mcp-hooks-extensions-plugins.md) | MCP·훅·확장·플러그인 | mcp-tools, hooks, extensions, plugins |
| 08 | [08-sandboxes-execution.md](analysis/08-sandboxes-execution.md) | 원격 샌드박스·실행 백엔드 | remote-sandboxes, SDK sandboxes |
| 09 | [09-tui-app-commands-acp.md](analysis/09-tui-app-commands-acp.md) | TUI·app.py·슬래시 커맨드·ACP | cli-reference, COMMANDS.md |

각 문서는 같은 틀을 따른다: 문서가 약속하는 것 → 코드 지도 → 동작 흐름 → 핵심 설계 포인트 → 문서↔코드 대조 → dcode↔SDK 경계 → 더 볼 거리.
코드 참조는 저장소 루트 기준 `path:line` 형식이다 (예: `libs/code/deepagents_code/agent.py:120`).

## 규모 (참고)

| 범위 | Python 줄 수 |
|---|---|
| `libs/code/deepagents_code/` 전체 | 약 20만 |
| └ `app.py` + `tui/` | 약 7.8만 |
| └ `hooks/`, `client/` | 각 약 9.4천 |
| `libs/deepagents/deepagents/` (SDK) | 약 2.8만 |
