"""Canonical registry of `DEEPAGENTS_CODE_*` environment variables.

Every env var the app reads whose name starts with `DEEPAGENTS_CODE_` must
be defined here as a module-level constant.  A drift-detection test
(`tests/unit_tests/test_env_vars.py`) fails when a bare string literal
like `"DEEPAGENTS_CODE_FOO"` appears in source code instead of a constant
imported from this module.

Import the short-name constants (e.g. `AUTO_UPDATE`, `DEBUG`) and pass them
to `os.environ.get()` instead of using raw string literals. If the env var is
ever renamed, only the value here changes.

!!! note

    `resolve_env_var` also supports a dynamic prefix override for API keys
    and provider credentials: setting `DEEPAGENTS_CODE_{NAME}` takes priority
    over `{NAME}`.  For example, `DEEPAGENTS_CODE_OPENAI_API_KEY` overrides
    `OPENAI_API_KEY`. Only call sites that use `resolve_env_var` benefit from
    this -- direct `os.environ.get` lookups (like the constants below) do not.
    Dynamic overrides are not listed here because they mirror third-party
    variable names.
"""

# [해설] 이 모듈의 역할: dcode가 읽는 `DEEPAGENTS_CODE_*` 환경 변수 이름의 단일 정의처(상수 레지스트리)와 불리언 파싱 규칙.
# [해설] 값 자체는 읽지 않는다(classify_env_bool/is_env_truthy 제외). 대부분의 상수는 config_manifest.py의 옵션 선언에서
# [해설] env 키로 참조되어, configuration/resolver의 ENVIRONMENT_RANK(400) 계층(EnvProvider)으로 해석된다.
# [해설] 일부는 매니페스트 없이 직접 os.environ으로 읽힌다(예: RECENT_THREADS → sessions.py, DEBUG_DIRECTORY → _debug.py).
# [해설] 실행 프로세스: 둘 다(클라이언트·서버 모두 import). 서버로 넘기는 CLI 설정은 SERVER_ENV_PREFIX 접두 env로 직렬화된다.
# [해설] 관련 분석 문서: analysis/03-config-models-credentials.md / 관련 공식 문서: docs_official/code/configuration.md
# [해설][주의] 프로젝트 `.env`로 설정이 금지된 보안 민감 키 목록은 config.py의 _PROJECT_DOTENV_DENIED_ENV_KEYS에 있다
# [해설] (AUTO_CLASSIFIER_MODEL/TIMEOUT, DANGEROUSLY_ENABLE_/DISABLED_PROJECT_MCP_SERVERS, FORKED_SUBAGENTS 등).
# [해설] analysis/03 조사 기준 이 모듈의 약 40개 변수(예: OFFLINE, LOG_LEVEL, YOLO_SWITCHER)는 공식 문서에 나오지 않는다.
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# [해설] 아래 섹션 주석은 "이름순 정렬"을 요구하지만 FORKED_SUBAGENTS(EXPERIMENTAL 앞)·DEFAULT_DEBUG_DIRECTORY 등 일부는 순서를 벗어난다.
# ---------------------------------------------------------------------------
# Constants — import these instead of bare string literals.
# Keep alphabetically sorted by constant name.
# ---------------------------------------------------------------------------

# [해설] Auto 승인 모드 분류기 모델 override. 사용처: config_manifest.py, config.py, app.py, _server_config.py.
# [해설][주의] 보안 통제용 → 프로젝트 `.env` 금지, 해석 실패 시 메인 모델로 폴백하지 않고 거부(analysis/04).
AUTO_CLASSIFIER_MODEL = "DEEPAGENTS_CODE_AUTO_CLASSIFIER_MODEL"
"""Model spec (`provider:model`) used by the Auto approval-mode classifier.

Unset (the default) reuses the main agent model, preserving the historical
behavior. A `provider:model` value points the authorization classifier at a
separate — typically faster and cheaper — model without changing the model that
writes code. The classifier is a security control: a model that cannot be
resolved (bad spec, missing credentials, uninstalled provider package) never
falls back to the main model — reviewed actions are denied, and repeated
failures escalate to your approval. Also settable via `[models].auto_classifier`
in config.toml and `--auto-classifier-model`.

This is user-controlled process env, not a repo file: a committed *project*
`.env` cannot set it (see `config._PROJECT_DOTENV_DENIED_ENV_KEYS`), so a cloned
repository cannot point the review that authorizes its own tool calls at a weaker
model. Only the shell, the launch environment, or the global `~/.deepagents/.env`
can.
"""

# [해설] 분류기 배치 검토 제한 시간(1~300초 밖이면 무시). 사용처: config_manifest.py, config.py. 프로젝트 `.env` 금지.
AUTO_CLASSIFIER_TIMEOUT = "DEEPAGENTS_CODE_AUTO_CLASSIFIER_TIMEOUT"
"""Seconds the Auto approval-mode classifier may take to review one batch.

Raise this when reviews time out on a slow or heavily loaded classifier model:
a batch that misses the deadline is denied as `classifier_unavailable`, so the
tool call does not run and repeated misses escalate to your approval. This
covers the wait for a verdict only — the separate budget for *building* the
classifier model (cold provider import, credential bootstrap), which denies with
"could not be built within 30s", is fixed. Values outside 1-300 seconds are
ignored in favor of the next config source, so the deadline can never be
removed. Also settable via `[models].auto_classifier_timeout` in config.toml.
Resolved once per `dcode` start, so a change takes effect on the next launch.

Like `AUTO_CLASSIFIER_MODEL`, a committed *project* `.env` cannot set it (see
`config._PROJECT_DOTENV_DENIED_ENV_KEYS`).
"""

# [해설] 자동 업데이트 토글(기본 on). 사용처: config_manifest.py, main.py.
AUTO_UPDATE = "DEEPAGENTS_CODE_AUTO_UPDATE"
"""Toggle automatic app updates. Enabled by default; set to a falsy value
('0', 'false', 'no', 'off', or empty) to opt out."""

# [해설] 긴 붙여넣기 접기(기본 on). 매니페스트 옵션 `[ui].collapse_pastes`와 연결.
COLLAPSE_PASTES = "DEEPAGENTS_CODE_COLLAPSE_PASTES"
"""Collapse large chat-input pastes into `[Pasted text #N +M lines]` placeholders.

Enabled by default; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to disable auto-collapsing so pasted text is inserted verbatim. Parsed by
`classify_env_bool` (an unrecognized value falls through to the config value
rather than forcing the default). Also settable via `[ui].collapse_pastes` in
config.toml.
"""

# [해설] 입력 커서 깜빡임. empty_env_is_false라 빈 값도 false로 config.toml을 덮어쓴다.
CURSOR_BLINK = "DEEPAGENTS_CODE_CURSOR_BLINK"
"""Blink the chat input cursor.

Enabled by default; set to a falsy value (`0`, `false`, `no`, `off`, or blank)
for a steady cursor. Parsed by `classify_env_bool` (an unrecognized value falls
through to the config value rather than forcing the default). A blank value —
empty or whitespace-only — counts as `false` because the option declares
`empty_env_is_false`, so it overrides `config.toml` instead of falling through.
Also settable via `[ui].cursor_blink` in config.toml.
"""

# [해설] 커서 모양(block/underline). 잘못된 값은 다음 계층으로 폴백.
CURSOR_STYLE = "DEEPAGENTS_CODE_CURSOR_STYLE"
"""Chat input cursor style (`block` or `underline`).

Takes precedence over `[ui].cursor_style` in config.toml. Invalid values fall
through to the config file and then the default block cursor.
"""

# [해설][주의] 프로젝트 `.mcp.json` 서버를 "이름만으로" 사전 승인하는 위험한 탈출구. 명령/URL이 바뀌어도 이름이 같으면 통과.
# [해설] 사용처: config.py, model_config.py, mcp_login_service.py. 프로젝트 `.env` 금지(analysis/07).
DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS = (
    "DEEPAGENTS_CODE_DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS"
)
"""Comma-separated project MCP server names to dangerously pre-approve by name.

This is an explicit process-wide escape hatch. Servers named here load from an
otherwise-untrusted project `.mcp.json` without prompting (they are omitted from
the interactive approval prompt), while non-listed servers still require
approval (they go through the prompt, and stay dropped only on the
non-interactive or denied paths). Like
`DISABLED_PROJECT_MCP_SERVERS`, this is user-controlled process env, not a repo
file, so it does not weaken the user-level-only trust boundary (a committed
*project* `.env` cannot set it; see `config._PROJECT_DOTENV_DENIED_ENV_KEYS`).
This dangerous contract is name-based: a different project, command change, or
URL change under the same server name still matches.

This process-wide allowlist and the scoped
`[mcp].enabled_project_server_approvals` TOML approvals are independent grants.
Setting this variable, including to an empty value, does not suppress remembered
project approvals. (`DISABLED_PROJECT_MCP_SERVERS` instead *unions* with its
TOML list, so a deny is never silently emptied.)
"""

# [해설] 디버그 로깅 + 서버 서브프로세스 로그 보존. 사용처: config.py, model_config.py, mcp_auth.py 등.
DEBUG = "DEEPAGENTS_CODE_DEBUG"
"""Enable verbose debug logging and preserve the server subprocess log.

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` (case-insensitive)
as enabled, and `0`, `false`, `no`, `off`, empty string, or unset as disabled.
"""

# [해설] 아래 DEBUG_* 계열은 개발/수동 UI 테스트용 강제 트리거들이다. DEBUG_COLD_CACHE: 콜드 캐시 경고 모달 강제(app.py).
DEBUG_COLD_CACHE = "DEEPAGENTS_CODE_DEBUG_COLD_CACHE"
"""Force the cold prompt-cache warning modal on every interactive send.

Set to a truthy value when launching the interactive TUI to make
`_cold_cache_warning_for` synthesize a warning from the current model and
context, bypassing the provider-policy, token-floor, cache-window, and
cost-threshold gates as well as both session and persisted suppression. Lets
the modal be exercised without waiting out a provider cache window.

The flag is re-read on every send and nothing clears it, so the modal fires
for the life of the process, not just once.

When the active model has no documented cache policy, `debug_stand_in_policy`
supplies an Anthropic-shaped placeholder. On a non-Anthropic model the modal
will therefore cite Anthropic's retention window, and the dollar figures are
illustrative rather than real estimates.

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` (case-insensitive)
as enabled, and `0`, `false`, `no`, `off`, empty string, or unset as disabled.
"""

# [해설] 디버그 콘솔 클릭 복사(env가 설정돼 있으면 체크박스 저장값보다 우선).
DEBUG_CONSOLE_CLICK_TO_COPY = "DEEPAGENTS_CODE_DEBUG_CONSOLE_CLICK_TO_COPY"
r"""Enable click-to-copy in the `Ctrl+\` Debug Console when enabled.

Off by default; toggle the "Click to copy" checkbox in the console or set
`[ui].debug_console_click_to_copy` in config.toml. A recognized value is parsed
by `classify_env_bool`; an unrecognized value falls through to the config value.
An empty/whitespace value is ignored before parsing (rather than being treated
as falsy) and also falls through, so it never masks the saved preference.

When set, this env var takes precedence over the persisted
`[ui].debug_console_click_to_copy` config value on launch, so toggling the
checkbox will not appear to "stick" across restarts while the env var remains
set.
"""

# [해설] 가짜 의존성 하한 불일치를 합성(_dep_floor_check.py). 음소거하면 실제 dismissal이 기록된다는 부작용 주의.
DEBUG_DEP_FLOOR = "DEEPAGENTS_CODE_DEBUG_DEP_FLOOR"
"""Synthesize a stale editable-dependency floor mismatch at launch.

Set to a truthy value to short-circuit `_collect_violations` to a hard-coded
fake below-floor dependency, bypassing the editable-install gate and the real
version comparison. Both channels are then reachable without a genuinely stale
environment: the blocking pre-TUI continue/mute/abort prompt on an interactive
terminal launch, and the one-off stderr warning everywhere else.

Note that muting the synthetic mismatch writes a real dismissal for this
checkout; it re-arms on its own once the fake mismatch changes or the var is
unset.

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` as enabled.
"""

# [해설] 스레드별 디버그 로그 디렉터리(_debug.py). DEBUG_FILE은 구버전 호환(부모 디렉터리 사용).
DEBUG_DIRECTORY = "DEEPAGENTS_CODE_DEBUG_DIRECTORY"
"""Directory for per-thread debug logs (default: `DEFAULT_DEBUG_DIRECTORY`)."""

DEBUG_FILE = "DEEPAGENTS_CODE_DEBUG_FILE"
"""Deprecated debug file path; its parent is used when `DEBUG_DIRECTORY` is unset."""

# [해설] 디버그 로그 기본 경로(/tmp). 환경 변수 이름이 아니라 기본값 상수라는 점에서 다른 항목과 성격이 다르다.
DEFAULT_DEBUG_DIRECTORY = "/tmp/deepagents_debug"  # noqa: S108  # opt-in debug logs
"""Default directory for debug logs when no debug path override is set."""

# [해설] 프로젝트 MCP 신뢰 프롬프트 강제 표시 후 종료(main.py). 신뢰 결정을 저장하지 않는다.
DEBUG_MCP_PROJECT_TRUST = "DEEPAGENTS_CODE_DEBUG_MCP_PROJECT_TRUST"
"""Force the project MCP approval prompt for manual UI testing.

Set to a truthy value when launching the interactive TUI to render the
project-level MCP trust prompt without relying on an untrusted config state. If
project MCP servers are discovered, the prompt shows those real servers;
otherwise it shows a sample server. The TUI exits after the prompt response so
the debug run does not continue into TUI or server startup, and it does not
persist trust decisions.

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` as enabled.
"""

# [해설] 모델 전환 확인 모달 강제(app.py의 _confirm_and_switch_model).
DEBUG_MODEL_SWITCH = "DEEPAGENTS_CODE_DEBUG_MODEL_SWITCH"
"""Force the model-switch confirmation modal on every model change.

Set to a truthy value when launching the interactive TUI to make
`_confirm_and_switch_model` show `ModelSwitchWarningScreen` for every switch
to a different model, bypassing the `warnings.model_switch_token_threshold`
gate. Lets the modal — including the deferred path that queues behind an
active turn — be exercised without growing a thread past the token threshold.
The modal shows the real current/target models and the live context-token
count, which may be 0.

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` (case-insensitive)
as enabled, and `0`, `false`, `no`, `off`, empty string, or unset as disabled.
"""

# [해설][주의] DEBUG_NOTIFICATIONS/DEBUG_UPDATE는 is_env_truthy가 아니라 "비어 있지 않으면 on" — `0`이나 `false`도 켜진다.
DEBUG_NOTIFICATIONS = "DEEPAGENTS_CODE_DEBUG_NOTIFICATIONS"
"""Inject sample missing-dependency notifications at launch so the notification
center UI can be exercised without waiting for real conditions.

Does not auto-open the update modal (use `DEEPAGENTS_CODE_DEBUG_UPDATE` for that).

Any non-empty value enables the flag (including `"0"` or `"false"`).
"""

DEBUG_UPDATE = "DEEPAGENTS_CODE_DEBUG_UPDATE"
"""Inject a sample update-available notification and auto-open the update modal
at launch so the update-available flow can be exercised without waiting for a
real PyPI release.

Any non-empty value enables the flag (including `"0"` or `"false"`).
"""

# [해설] 프로젝트 MCP 서버 이름 거부 목록. TOML 목록과 union(거부는 누적). 승인보다 항상 우선. 프로젝트 `.env` 금지.
DISABLED_PROJECT_MCP_SERVERS = "DEEPAGENTS_CODE_DISABLED_PROJECT_MCP_SERVERS"
"""Comma-separated project MCP server names to always reject by name.

A user-level equivalent of `[mcp].disabled_project_servers`.

Rejection wins over approval: a name listed here is dropped even when it also
appears in `DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS` or in a scoped
`[mcp].enabled_project_server_approvals` entry, and even when the project config
is otherwise trusted. Unlike the enabled list, this env var *unions* with
(rather than replaces) `[mcp].disabled_project_servers` — denies accumulate
across sources, so neither can silently empty a deny set in the other. This is
process env the user controls, not a repo file, so it does not weaken the
user-level-only trust boundary: a committed *project* `.env` is blocked from
setting it (see `config._PROJECT_DOTENV_DENIED_ENV_KEYS`); only the user's
shell, launch env, or global `~/.deepagents/.env` can.
"""

# [해설] 내장 general-purpose 서브에이전트 fork 모드(기본 on). 사용처: agent.py, config.py. 프로젝트 `.env` 금지(analysis/05).
FORKED_SUBAGENTS = "DEEPAGENTS_CODE_FORKED_SUBAGENTS"
"""Whether dcode's built-in `general-purpose` subagent runs in fork mode.

On by default. Set to a falsy value to make the subagent receive only the delegated
task instead of inheriting the parent agent's conversation and state. Parsed by
`is_env_truthy`; set this only through the launching shell or global
`~/.deepagents/.env`, never a project `.env`.
"""

# [해설] 실험 기능 opt-in. 사용처: config.py, server_graph.py, agent.py(트레이스 메타데이터 표시 등).
EXPERIMENTAL = "DEEPAGENTS_CODE_EXPERIMENTAL"
"""Opt into experimental, unstable dcode behavior.

Off by default; parsed by `is_env_truthy` (see there for the accepted truthy
values). Marks experimental runs in UI/trace metadata. Behavior behind this
flag may change or be removed without notice.
"""

# [해설] Python extension 로딩 활성화 / 프로젝트 extension 신뢰 정책(ask/always/never). 매니페스트 옵션으로 해석(analysis/07).
EXTENSIONS = "DEEPAGENTS_CODE_EXTENSIONS"
"""Enable loading installed-plugin and trusted-project Python extensions."""

EXTENSIONS_TRUST = "DEEPAGENTS_CODE_EXTENSIONS_TRUST"
"""Default project extension trust policy: `ask`, `always`, or `never`."""

# [해설] 로컬 Unix 소켓 외부 이벤트 리스너(실험적) 활성화와 소켓 경로. 사용처: app.py.
EXTERNAL_EVENT_SOCKET = "DEEPAGENTS_CODE_EXTERNAL_EVENT_SOCKET"
"""Enable the local Unix-socket external event listener.

Parsed by `is_env_truthy`; off by default. Wire format and behavior are
considered experimental until the listener is documented in the README.
"""

EXTERNAL_EVENT_SOCKET_PATH = "DEEPAGENTS_CODE_EXTERNAL_EVENT_SOCKET_PATH"
"""Override the default Unix-socket path for the external event listener."""

# [해설] 스킬 containment 허용 경로 추가(os.pathsep 구분). skills/invocation.discover_skills_and_roots가 매니페스트 옵션
# [해설] `skills.extra_allowed_dirs`로 해석하고, skills/load.load_skill_content는 거부 메시지에 이름을 안내한다.
EXTRA_SKILLS_DIRS = "DEEPAGENTS_CODE_EXTRA_SKILLS_DIRS"
"""Paths added to the skill containment allowlist, split with `os.pathsep`."""

# [해설] Auto 모드에서 목표 criteria 자동 적용(기본 off). 인식 못 하는 값은 config.toml로 폴백.
GOAL_AUTO_ACCEPT_CRITERIA = "DEEPAGENTS_CODE_GOAL_AUTO_ACCEPT_CRITERIA"
"""Apply generated goal criteria automatically in Auto mode.

Disabled by default so Auto continues to show the goal review prompt unless the
user opts in. Manual always reviews criteria and YOLO always applies them.
Set to a recognized truthy or falsy value; unrecognized values are ignored and
resolution falls through to `[goals].auto_accept_criteria` in config.toml, then
the built-in default (disabled).
"""

# [해설] 아래 HIDE_* 계열은 TUI 푸터/스플래시 표시 토글(tui/widgets/status.py, welcome.py, startup_tip.py).
HIDE_CWD = "DEEPAGENTS_CODE_HIDE_CWD"
"""Hide local path displays in the TUI footer and the editable-install path in
the startup splash when enabled.

Does not control the splash working-directory row, which is gated solely by
`SPLASH_SHOW_CWD`.
"""

HIDE_GIT_BRANCH = "DEEPAGENTS_CODE_HIDE_GIT_BRANCH"
"""Hide the current git branch in the TUI footer when enabled."""

HIDE_LANGSMITH_TRACING = "DEEPAGENTS_CODE_HIDE_LANGSMITH_TRACING"
"""Hide LangSmith tracing project/thread info in the startup splash when enabled."""

HIDE_SPLASH_TIPS = "DEEPAGENTS_CODE_HIDE_SPLASH_TIPS"
"""Hide the startup tip shown above the chat input when enabled."""

HIDE_SPLASH_VERSION = "DEEPAGENTS_CODE_HIDE_SPLASH_VERSION"
"""Hide version and local-install details in the splash screen when enabled."""

# [해설] 대화 기록 오프로드 아카이브 보존 일수(0이면 정리 비활성).
HISTORY_RETENTION_DAYS = "DEEPAGENTS_CODE_HISTORY_RETENTION_DAYS"
"""Days an offloaded conversation-history archive is kept before the startup
sweep deletes it.

Archives live under `~/.deepagents/conversation_history/` and are removed by a
best-effort background sweep at startup once their age exceeds the window.
Non-negative integers only: `0` disables the sweep entirely, and an
unparseable or negative value falls through to the next config source. Also
settable via `[history].retention_days` in config.toml (managed config takes
precedence).
"""

# [해설] 내부 센티널: 자동 업데이트 re-exec 후에도 사용자가 입력한 명령 이름을 유지(_invocation.py, main.py).
INVOKED_AS = "DEEPAGENTS_CODE_INVOKED_AS"
"""Internal sentinel carrying the command name the user launched with.

Not user-facing. The launch name is normally derived from `sys.argv[0]`, but the
startup auto-update re-execs the process as `python -m deepagents_code`, which
discards it. `_restart_current_process` records the resolved name here so the
re-exec'd process still echoes the command the user actually typed in its resume
hints. Implausible values are ignored in favor of the `dcode` default; see
`_invocation.invoked_name`.
"""

# [해설] kitty 키보드 프로토콜 감지 강제(terminal_capabilities.py).
KITTY_KEYBOARD = "DEEPAGENTS_CODE_KITTY_KEYBOARD"
"""Override kitty-keyboard detection (`1` forces on, `0` forces off)."""

# [해설] LangSmith 트레이싱 관련: 프로젝트 이름 override, 비밀 redaction(기본 on), 복제 프로젝트(첫 번째만 사용). 사용처: config.py, agent.py.
LANGSMITH_PROJECT = "DEEPAGENTS_CODE_LANGSMITH_PROJECT"
"""Override LangSmith project name for agent traces."""

LANGSMITH_REDACT = "DEEPAGENTS_CODE_LANGSMITH_REDACT"
"""Toggle LangSmith secret redaction for agent traces (defaults to on)."""

LANGSMITH_REPLICA_PROJECTS = "DEEPAGENTS_CODE_LANGSMITH_REPLICA_PROJECTS"
"""Comma-separated LangSmith project names to *also* write agent traces to.

When set (and tracing is active), each agent run is dual-written to the primary
deepagents-code project *and* one extra project via LangSmith write replicas.

Only the first listed project is used: the LangGraph server mirrors a run to a
single extra project, so any additional entries are dropped (with a warning).
The value is comma-separated for forward-compatibility, not because multiple
destinations are written today.
"""

# [해설] 내부 센티널: 실행 시점 TERM_PROGRAM 스냅샷(main.py). `.env`가 나중에 넣은 값이 재개 힌트에 새지 않게.
LAUNCH_TERM_PROGRAM = "DEEPAGENTS_CODE_LAUNCH_TERM_PROGRAM"
"""Internal sentinel recording the `TERM_PROGRAM` present when `dcode` started.

Not user-facing. The resume hint echoes `TERM_PROGRAM` only when the launch
environment supplied it (an inline `TERM_PROGRAM=x dcode`, a terminal's own
export, or a shell alias), so the value set by a project or global `.env` file
*after* launch must not leak in. The app itself never sets `TERM_PROGRAM`, so
`cli_main` snapshotting the variable here at entry means a set sentinel always
marks an explicit launch value; the update re-exec inherits it unchanged,
which is correct because the relaunch runs the command the user typed.
"""

# [해설] 제거된 구 allowlist 이름. 값은 무시하고 마이그레이션 안내에만 사용(model_config.py, mcp_tools.py).
LEGACY_ENABLED_PROJECT_MCP_SERVERS = "DEEPAGENTS_CODE_ENABLED_PROJECT_MCP_SERVERS"
"""Removed project MCP allowlist env var retained for migration detection only.

The app no longer honors this value. It detects the old name so users receive a
migration notice pointing to `DANGEROUSLY_ENABLE_PROJECT_MCP_SERVERS`.
"""

# [해설] deepagents_code 로깅 최소 레벨(_debug.py, _debug_buffer.py).
LOG_LEVEL = "DEEPAGENTS_CODE_LOG_LEVEL"
"""Minimum level for `deepagents_code` runtime logging.

Accepted values are DEBUG, INFO, WARNING, ERROR, and CRITICAL.
"""

# [해설] 메모리 자동 저장 안내 프롬프트 토글. off여도 메모리 로딩과 명시적 저장은 유지(analysis/06).
MEMORY_AUTO_SAVE = "DEEPAGENTS_CODE_MEMORY_AUTO_SAVE"
"""Toggle automatic memory saving (defaults to on).

When enabled, the memory prompt tells the agent to proactively persist
learnings to the `AGENTS.md` memory files. Set to a falsy value (`0`, `false`,
`no`, `off`, or empty) to keep loading memory into context while disabling the
auto-save guidance; explicit saves (e.g. the `remember` skill) still work.
"""

# [해설] 터미널 이스케이프 출력 전면 비활성(terminal_escape.py). TERMINAL_PROGRESS보다 우선.
NO_TERMINAL_ESCAPE = "DEEPAGENTS_CODE_NO_TERMINAL_ESCAPE"
"""Disable all terminal escape/control sequence output when enabled."""

# [해설] 업데이트 확인 비활성.
NO_UPDATE_CHECK = "DEEPAGENTS_CODE_NO_UPDATE_CHECK"
"""Disable automatic update checking when set."""

# [해설] 관리 바이너리(ripgrep) 다운로드 등 네트워크 fetch 차단. 사용처: managed_tools.py, cost_tracking.py, plugins/discovery.py.
OFFLINE = "DEEPAGENTS_CODE_OFFLINE"
"""Disable network downloads of managed binaries (e.g. ripgrep).

Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` as enabled. When
truthy, `managed_tools.ensure_ripgrep` will not attempt to download a binary
and falls back to the existing missing-tool notification + slow Python regex
path."""

# [해설] Ollama 모델/프로필 탐색 probe 토글(model_config.py).
OLLAMA_DISCOVERY = "DEEPAGENTS_CODE_OLLAMA_DISCOVERY"
"""Toggle Ollama model and profile discovery probes.

Defaults to enabled. Suppress the probe when the daemon is intentionally
offline or the probe latency is undesirable. The probe is lazy and never
runs on the startup hot path. When enabled, discovery may call `/api/tags`
and `/api/show`. See `_ollama_discovery_enabled` for accepted truthy/falsy
values.
"""

# [해설] 첫 실행 온보딩 3상태 override(unset/falsy/truthy). 사용처: onboarding.py(should_run_onboarding).
ONBOARDING = "DEEPAGENTS_CODE_ONBOARDING"
"""Override whether the first-run onboarding flow opens at interactive startup.

Three-state, parsed by `classify_env_bool`:

- Unset (or an unrecognized token): keep the default first-run behavior, i.e.
  run onboarding until the completion marker exists.
- Falsy (`0`, `false`, `no`, `off`, or empty): never open onboarding, even on a
  fresh install with no completion marker.
- Truthy (`1`, `true`, `yes`, `on`): force onboarding to open on every
  interactive startup, ignoring the completion marker.

Read by `should_run_onboarding`; skipping the flow this way does not write the
completion marker, so unsetting the variable restores first-run behavior.
"""

# [해설] 온보딩의 통합 요약 화면 표시(기본 off, app.py).
ONBOARDING_INTEGRATIONS_SCREEN = "DEEPAGENTS_CODE_ONBOARDING_INTEGRATIONS_SCREEN"
"""Show the "Installed Integrations" summary screen during first-run onboarding.

Off by default: onboarding goes straight from the name prompt to the model
selector, which already surfaces (and installs) uninstalled model providers.
Set to a truthy value to bring the standalone integrations screen back into the
flow. Parsed by `is_env_truthy`: accepts `1`, `true`, `yes`, `on` as enabled.
"""

# [해설] OpenAI 계열 호출에 스레드 ID를 prompt_cache_key로 주입(기본 on). 필드를 거부하는 호환 엔드포인트면 끈다.
OPENAI_PROMPT_CACHE_KEY = "DEEPAGENTS_CODE_OPENAI_PROMPT_CACHE_KEY"
"""Toggle injecting a per-thread OpenAI `prompt_cache_key` (defaults to on).

When enabled, OpenAI-provider model calls receive the active thread ID as a
top-level `prompt_cache_key`, giving more reliable prompt-cache prefix routing
across turns. It is attempted for every model whose provider resolves to
`openai` regardless of base URL (official API, the LangSmith gateway, and other
OpenAI-compatible endpoints), because the field is optional and additive. Set to
a falsy value (`0`, `false`, `no`, `off`) to opt out for endpoints that reject
unknown request fields; an explicitly empty value also opts out because the
option declares `empty_env_is_false`. Other tokens are parsed by
`classify_env_bool`, and an unrecognized value falls through to
`[models].openai_prompt_cache_key` in config.toml, then the default. A
user-supplied key is always preserved.
"""

# [해설] 마켓플레이스 플러그인 백그라운드 업데이트 토글 / 플러그인 캐시 루트 override(plugins/store.py).
PLUGIN_AUTO_UPDATE = "DEEPAGENTS_CODE_PLUGIN_AUTO_UPDATE"
"""Toggle background updates for installed marketplace plugins.

Enabled by default; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to disable every plugin update regardless of its manifest setting.
"""

PLUGIN_CACHE_DIR = "DEEPAGENTS_CODE_PLUGIN_CACHE_DIR"
"""Override the plugin install/marketplace cache root.

When unset, plugins are stored under `DEFAULT_CONFIG_DIR / "plugins"`.
"""

# [해설] genai-prices 가격표 백그라운드 갱신 토글. OFFLINE이면 함께 억제.
PRICES_AUTO_UPDATE = "DEEPAGENTS_CODE_PRICES_AUTO_UPDATE"
"""Toggle hourly background refresh of the `genai-prices` pricing catalog.

Enabled by default; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to keep using only the pricing data bundled with the installed `genai-prices`
package. `DEEPAGENTS_CODE_OFFLINE` suppresses the refresh too, along with
every other network fetch.

Parsed by `is_env_truthy` on each pricing call until the updater starts, and
never read again after that -- so disabling it mid-process has no effect on a
running updater, while enabling it mid-process starts one on the next priced
request. Also the escape hatch for hosts embedding this package that manage
`genai_prices.UpdatePrices` themselves: genai-prices permits one updater per
process, so an embedder that starts its own would otherwise race this one.
"""

# [해설][주의] 프로젝트 `.env` 로딩 토글(기본 on). 방어적으로 끌 수 있는 스위치이며 프로젝트 `.env` 스스로는 끌 수 없다(config.py).
READ_PROJECT_DOTENV = "DEEPAGENTS_CODE_READ_PROJECT_DOTENV"
"""Toggle loading the *project* `.env` (the one found walking up from cwd).

Enabled by default, preserving the historical behavior of applying the nearest
project `.env` to the process environment (`override=False`, shell exports
win). Set to a falsy value (`0`, `false`, `no`, `off`) — or `[startup]`
`read_project_dotenv = false` in config.toml — to skip the project file
entirely, as defense-in-depth against a cloned repo whose `.env` carries
hostile values the dotenv denylist does not yet enumerate. The global
`~/.deepagents/.env` is unaffected. This is user-controlled process env, not a
repo file, so a project `.env` cannot disable itself.
"""

# [해설] 최근 스레드 표시 개수(기본 20). 매니페스트가 아니라 sessions.py가 os.environ에서 직접 읽는다.
RECENT_THREADS = "DEEPAGENTS_CODE_RECENT_THREADS"
"""Maximum number of recent threads loaded and displayed (default: 20)."""

# [해설] 메인 에이전트 LangGraph recursion_limit override. 서버로는 _server_config.py를 통해 전달된다(analysis/01).
RECURSION_LIMIT = "DEEPAGENTS_CODE_RECURSION_LIMIT"
"""Override the main agent's LangGraph `recursion_limit` (graph step budget).

Parsed as an integer by the config manifest. Values outside the accepted range
are ignored with a logged warning, falling back to `config.toml`. See
`[runtime].recursion_limit` and the `--recursion-limit` CLI flag.
"""

# [해설] 내부 센티널: 자동 업데이트 무한 재시작 루프 방지(main.py).
RESTARTED_AFTER_UPDATE = "DEEPAGENTS_CODE_RESTARTED_AFTER_UPDATE"
"""Internal sentinel recording the target version immediately before the
startup auto-update re-execs the process.

Not user-facing. The re-exec'd process consumes it and, if that same version
still reports as available (a no-op upgrade that did not change the running
version), skips auto-updating to break out of an otherwise endless
upgrade/restart loop. Set and read internally across `os.execv`.
"""

# [해설] 종료 시 재개 명령에 TERM_PROGRAM 포함 여부(실험/디버그 모드에서 기본 on).
RESUME_TERM_PROGRAM = "DEEPAGENTS_CODE_RESUME_TERM_PROGRAM"
"""Include launch-time `TERM_PROGRAM` in teardown resume commands.

Disabled by default and enabled by default in experimental or debug mode. An
explicit boolean (`1`/`true`/`yes`/`on`, or `0`/`false`/`no`/`off`) overrides
that mode-dependent default, as does an empty value, which reads as false. Also
settable as `[features].resume_term_program` in config.toml.
"""

# [해설] ripgrep 설치 방식(managed 다운로드 / system). managed_tools.py.
RIPGREP_INSTALLER = "DEEPAGENTS_CODE_RIPGREP_INSTALLER"
"""Select how ripgrep is provisioned: `managed` (default) or `system`.

`managed` downloads the pinned, SHA-256-verified upstream binary into the dcode
installation (no sudo). `system` skips that download so power users can rely on
their distro package / existing toolchain instead; the install script's
`system` mode keeps the brew/apt/cargo path. A system `rg` already on `PATH` is
reused under either setting. Unrecognized values fall back to `managed`. See
`managed_tools.ripgrep_installer`."""

# [해설] 클라이언트가 CLI 설정을 서버 서브프로세스에 넘길 때 쓰는 env 접두사(_server_config.py가 직렬화·역직렬화).
# [해설] repo 개발 문서(THREAT_MODEL.md)는 `DA_SERVER_*`라고 적지만 코드의 실제 접두사는 이 값이다(analysis/08).
SERVER_ENV_PREFIX = "DEEPAGENTS_CODE_SERVER_"
"""Environment variable prefix used to pass CLI config to the server subprocess."""

# [해설] 셸 명령 허용 목록(쉼표 구분 또는 recommended/all). 사용처: config.py, _server_config.py(서버로 전달), analysis/04.
SHELL_ALLOW_LIST = "DEEPAGENTS_CODE_SHELL_ALLOW_LIST"
"""Comma-separated shell commands to allow (or 'recommended'/'all')."""

# [해설] 아래 SHOW_* 계열은 TUI 표시 토글. env가 설정돼 있으면 슬래시 명령 토글이 재시작 후 "유지"되지 않는 것처럼 보인다.
SHOW_HEADER = "DEEPAGENTS_CODE_SHOW_HEADER"
"""Show Textual's native header bar at the top of the TUI when enabled."""

SHOW_LANGSMITH_REPLICA_TRACING = "DEEPAGENTS_CODE_SHOW_LANGSMITH_REPLICA_TRACING"
"""Show LangSmith replica project info in the startup splash when enabled.

Defaults to enabled; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to hide replica tracing details from the splash while leaving tracing active.
"""

SHOW_MESSAGE_TIMESTAMPS = "DEEPAGENTS_CODE_SHOW_MESSAGE_TIMESTAMPS"
"""Show the timestamp footer under each chat message when enabled.

Off by default; use the `/timestamps` slash command or
`[ui].show_message_timestamps` in config.toml to toggle. Parsed by
`classify_env_bool` (an unrecognized or empty value falls through to the config
value rather than forcing the default). While this env var is set it outranks
the persisted value, so a `/timestamps` toggle will not appear to "stick"
across restarts.
"""

SHOW_REASONING = "DEEPAGENTS_CODE_SHOW_REASONING"
"""Show provider-visible reasoning in local TUI and headless output.

Off by default; use `[ui].show_reasoning` in config.toml to persist it. Parsed
by `classify_env_bool` (an unrecognized value falls through to the config value
rather than forcing the default). A recognized value outranks the config value
but loses to `--show-reasoning`, which is the only way to change the setting for
a single run.
"""

SHOW_SCROLLBAR = "DEEPAGENTS_CODE_SHOW_SCROLLBAR"
"""Show the vertical scrollbar in the chat area when enabled.

Off by default; use the `/scrollbar` slash command or `[ui].show_scrollbar` in
config.toml to toggle. Parsed by `classify_env_bool` (an unrecognized or empty
value falls through to the config value rather than forcing the default).

When set, this env var takes precedence over the persisted `[ui].show_scrollbar`
config value on launch, so a `/scrollbar` toggle will not appear to "stick"
across restarts while the env var remains set.
"""

SHOW_URL_OPEN_TOAST = "DEEPAGENTS_CODE_SHOW_URL_OPEN_TOAST"
"""Show a confirmation toast after clicking a URL that opens in a browser.

Defaults to enabled; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to suppress the success toast while still opening URLs normally.
"""

SHOW_USAGE_STATS = "DEEPAGENTS_CODE_SHOW_USAGE_STATS"
"""Print the session usage-statistics table when a session ends.

Defaults to enabled; set to a falsy value (`0`, `false`, `no`, `off`, or empty)
to suppress the table. Applies to both the TUI teardown and the headless
`-x`/`--execute` run, which is why the option carries an env var at all: a CI
runner can set one, but rarely has a `~/.deepagents/config.toml` to edit.

Suppressing only the table is narrower than `--quiet`, which silences the rest
of the headless teardown output too.
"""

# [해설] 스플래시 작업 디렉터리/모델 행 표시(기본 off, tui/widgets/welcome.py).
SPLASH_SHOW_CWD = "DEEPAGENTS_CODE_SPLASH_SHOW_CWD"
"""Show the working-directory row in the startup welcome banner when enabled.

Off by default and independent of the status bar's `HIDE_CWD`.
"""

SPLASH_SHOW_MODEL = "DEEPAGENTS_CODE_SPLASH_SHOW_MODEL"
"""Show the active model row in the startup welcome banner when enabled.

Off by default; the model is always visible in the status bar, so the banner
row is opt-in to avoid duplicating it.
"""

# [해설] 접두 LangSmith 변수가 원래 이름을 덮어쓸 때의 시작 경고 억제(config.py).
SUPPRESS_ENV_OVERRIDE_WARNING = "DEEPAGENTS_CODE_SUPPRESS_ENV_OVERRIDE_WARNING"
"""Silence the startup warning emitted when a `DEEPAGENTS_CODE_`-prefixed
LangSmith variable overrides its canonical counterpart (e.g. both
`LANGSMITH_API_KEY` and `DEEPAGENTS_CODE_LANGSMITH_API_KEY` are set to
different values).

The override is intentional: the prefixed value overwrites the canonical
variable inside the Deep Agents Code process (so the LangSmith SDK, which
only reads canonical names, picks it up). The value you exported in your own
shell is unaffected, since a process cannot change its parent's environment.
Off by default; set to a truthy value (`1`, `true`, `yes`, `on`) to suppress
the warning when this coexistence is expected. Parsed by `is_env_truthy`.
"""

# [해설] OSC 9;4 작업 진행률 표시(기본 on). empty_env_is_false.
TERMINAL_PROGRESS = "DEEPAGENTS_CODE_TERMINAL_PROGRESS"
"""Report agent activity as `OSC 9;4` taskbar/dock/tab progress.

Enabled by default; set to a falsy value (`0`, `false`, `no`, `off`, or blank)
to stop emitting the sequence on terminals that render it poorly. Parsed by
`classify_env_bool` (an unrecognized value falls through to the config value
rather than forcing the default). A blank value — empty or whitespace-only —
counts as `false` because the option declares `empty_env_is_false`, so it
overrides `config.toml` instead of falling through. Also settable via
`[ui].terminal_progress` in config.toml. `NO_TERMINAL_ESCAPE` suppresses the
sequence regardless.
"""

# [해설] 테마 강제 / 문자셋 모드 / LangSmith user id 메타데이터.
THEME = "DEEPAGENTS_CODE_THEME"
"""Force the CLI to launch with this theme name when set."""

UI_CHARSET_MODE = "DEEPAGENTS_CODE_UI_CHARSET_MODE"
"""Terminal character-set mode (`auto`, `ascii`, or `unicode`)."""

USER_ID = "DEEPAGENTS_CODE_USER_ID"
"""Attach a user identifier to LangSmith trace metadata."""

# [해설] Shift+Tab 승인 모드 순환에 YOLO 포함(기본 on). 조직은 config.toml `[startup].yolo_switcher`로 끌 수 있다.
YOLO_SWITCHER = "DEEPAGENTS_CODE_YOLO_SWITCHER"
"""Include YOLO in the Shift+Tab approval-mode cycle.

Enabled by default so an interactive session can cycle Manual → Auto → YOLO
without restarting with `--yolo`. Set to a falsy value (`0`, `false`, `no`,
`off`, or empty) to leave Shift+Tab limited to Manual/Auto. Also settable via
`[startup].yolo_switcher` in config.toml so orgs can distribute the opt-out.
Parsed by `classify_env_bool` through the config resolver (unrecognized values
fall through rather than forcing the default).
"""

# [해설] 불리언 토큰 집합. 빈 문자열은 falsy로 분류된다(주의: "설정했지만 비움" = 끔).
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES = frozenset({"0", "false", "no", "off", ""})


# [해설] 원시 문자열을 True/False/None(인식 불가)으로 분류하는 단일 규칙. 사용처: is_env_truthy, config.py,
# [해설] configuration/providers.py(EnvProvider의 bool 옵션), onboarding.py. None이면 호출자가 다음 설정 계층으로 폴백한다.
def classify_env_bool(raw: str) -> bool | None:
    """Classify a raw env-var string as a truthy, falsy, or unrecognized token.

    The single source of truth for which strings count as boolean on/off
    values; `is_env_truthy` and the config resolver both build on it so they
    agree on what "recognizably boolean" means.

    Args:
        raw: The raw (unstripped) environment-variable value.

    Returns:
        `True` for `1`/`true`/`yes`/`on`, `False` for `0`/`false`/`no`/`off`/
            empty string (case-insensitive), or `None` when the value
            is neither.
    """
    lowered = raw.strip().lower()
    if lowered in _TRUTHY_VALUES:
        return True
    if lowered in _FALSY_VALUES:
        return False
    return None


# [해설] env 값을 bool로 읽는 헬퍼. unset이거나 인식 불가면 default. environ을 넘기면 워크스페이스 스냅샷 기준으로 읽는다.
def is_env_truthy(
    name: str,
    *,
    default: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether env var *name* is set to a recognizably truthy value.

    Unlike `bool(os.environ.get(name))`, this does not treat `"0"` or
    `"false"` as enabled. Use this for on/off flags where the user would
    reasonably expect `VAR=0` to mean "disabled".

    Args:
        name: Environment variable name (typically a `DEEPAGENTS_CODE_*`
            constant from this module).
        default: Value returned when the variable is unset OR set to a
            value that is neither recognizably truthy nor falsy.
        environ: Environment to read, defaulting to the process environment.
            Pass a workspace snapshot to keep the read scoped.

    Returns:
        `True` for `1`/`true`/`yes`/`on` (case-insensitive), `False` for
        `0`/`false`/`no`/`off`/empty string, or `default` otherwise.
    """
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None:
        return default
    classified = classify_env_bool(raw)
    return default if classified is None else classified
