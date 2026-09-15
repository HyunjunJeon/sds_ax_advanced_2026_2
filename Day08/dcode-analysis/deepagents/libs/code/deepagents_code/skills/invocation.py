"""Helpers for loading and formatting skill invocations."""

# [해설] 이 모듈의 역할: 사용자가 `/skill:<name> [args]`로 스킬을 "명시 호출"할 때 필요한 두 단계를 제공한다.
# [해설] (1) 스킬 목록과 SKILL.md 읽기용 containment root 계산, (2) SKILL.md 본문을 감싼 프롬프트와 메타데이터 봉투 생성.
# [해설] 실행 프로세스: 클라이언트(TUI app.py, headless client/non_interactive.py). 서버는 결과 HumanMessage만 받는다.
# [해설][흐름] 즉 명시 호출은 모델의 read_file 도구를 거치지 않고, 클라이언트가 본문을 사용자 메시지로 주입한다.
# [해설] 주요 진입점 심볼: discover_skills_and_roots, build_skill_invocation_envelope, SkillInvocationEnvelope.
# [해설] 관련 분석 문서: analysis/06-memory-skills.md / 공식 문서: docs_official/code/memory-and-skills.md
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from deepagents_code._paths import (
    get_built_in_skills_dir,
    get_project_agent_skills_dir,
    get_project_claude_skills_dir,
    get_project_skills_dir,
    get_user_agent_skills_dir,
    get_user_claude_skills_dir,
    get_user_skills_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

    from deepagents_code.skills.load import ExtendedSkillMetadata


# [해설] 스킬 호출 결과 묶음. prompt는 전송할 텍스트, message_kwargs는 HumanMessage에 병합할 추가 필드
# [해설] (`additional_kwargs.__skill` → 체크포인트에 남아 재개/표시에 사용), skill_name은 트레이스 귀속용.
@dataclass(frozen=True)
class SkillInvocationEnvelope:
    """Structured prompt and checkpoint metadata for a skill invocation.

    Attributes:
        prompt: Composed prompt that wraps `SKILL.md` content with
            invocation instructions.
        message_kwargs: Extra fields merged into the initial HumanMessage.
        skill_name: Invoked skill name for trace attribution.
    """

    prompt: str
    message_kwargs: dict[str, Any]
    skill_name: str


# [해설] 스킬 발견(list_skills)과 SKILL.md 읽기 허용 루트 목록을 함께 만든다.
# [해설] 호출자: TUI 시작 시 app.py(스레드로 실행해 캐시), headless client/non_interactive.py.
# [해설] 반환된 roots는 skills/load.load_skill_content의 allowed_roots로 넘어간다.
def discover_skills_and_roots(
    assistant_id: str,
    *,
    plugin_skill_sources: tuple[tuple[Path, str], ...] = (),
    plugin_skill_roots: tuple[Path, ...] = (),
    path_base: Path | None = None,
) -> tuple[list[ExtendedSkillMetadata], list[Path]]:
    """Discover skills and build pre-resolved containment roots.

    Args:
        assistant_id: Agent identifier used to resolve user skill directories.
        plugin_skill_sources: Plugin-owned skill directories and namespaces,
            supplied by the plugin composition layer.
        plugin_skill_roots: Plugin-owned roots allowed for content loading.
        path_base: User working directory for resolving relative skill roots.
            Defaults to the process working directory.

    Returns:
        Tuple of `(skill metadata list, pre-resolved containment roots)`.

    Raises:
        RuntimeError: If the extra skill-directory option is absent from the
            manifest.
    """
    # [해설] 무거운 config/resolver import를 함수 안으로 지연해 모듈 import 비용을 줄인다.
    from pathlib import Path

    from deepagents_code.config import _use_extra_skills_path_base, credentials
    from deepagents_code.config_manifest import _emit_ranked_diagnostics, get_option
    from deepagents_code.configuration.resolver import get_config_resolver
    from deepagents_code.skills.load import list_skills
    from deepagents_code.skills.trust import load_trusted_skill_dirs

    # [해설][흐름] 1) 표준 디렉터리 8종 + 플러그인 소스로 스킬 목록 발견. project 경로는 credentials.project_root 기준.
    skills = list_skills(
        built_in_skills_dir=get_built_in_skills_dir(),
        plugin_skill_sources=plugin_skill_sources,
        user_skills_dir=get_user_skills_dir(assistant_id),
        project_skills_dir=get_project_skills_dir(credentials.project_root),
        user_agent_skills_dir=get_user_agent_skills_dir(),
        project_agent_skills_dir=get_project_agent_skills_dir(credentials.project_root),
        user_claude_skills_dir=get_user_claude_skills_dir(),
        project_claude_skills_dir=get_project_claude_skills_dir(
            credentials.project_root
        ),
    )
    # [해설][흐름] 2) containment root: 스킬을 발견한 표준 디렉터리 + 플러그인 root를 resolve해서 모은다.
    roots = [
        path.resolve()
        for path in (
            get_built_in_skills_dir(),
            *plugin_skill_roots,
            get_user_skills_dir(assistant_id),
            get_project_skills_dir(credentials.project_root),
            get_user_agent_skills_dir(),
            get_project_agent_skills_dir(credentials.project_root),
            get_user_claude_skills_dir(),
            get_project_claude_skills_dir(credentials.project_root),
        )
        if path is not None
    ]
    # [해설][흐름] 3) 매니페스트 옵션 `skills.extra_allowed_dirs`(env DEEPAGENTS_CODE_EXTRA_SKILLS_DIRS / config.toml)를
    # [해설] ConfigResolver로 해석. 상대 경로는 path_base(기본 cwd) 기준으로 풀리도록 컨텍스트를 건다.
    option = get_option("skills.extra_allowed_dirs")
    if option is None:
        msg = "skills.extra_allowed_dirs is missing from the configuration manifest"
        raise RuntimeError(msg)
    with _use_extra_skills_path_base(path_base or Path.cwd()):
        resolved = get_config_resolver().get(option)
    # [해설] 해석 과정에서 생긴 경고(잘못된 값, 가려진 계층 등)를 순위별로 출력.
    _emit_ranked_diagnostics(option, resolved)
    extra_skills_dirs = cast("list[Path] | None", resolved.value)
    roots.extend(path.resolve() for path in extra_skills_dirs or ())
    # [해설][흐름] 4) trust store(사용자가 대화형으로 승인한 디렉터리)도 root에 추가.
    # Persisted in-the-moment approvals extend the containment allowlist just
    # like the declarative `extra_allowed_dirs`, but are managed by the trust
    # store rather than hand-edited config. These entries are already the
    # canonical approved directories and are verified against post-approval
    # symlink swaps by `load_trusted_skill_dirs`, so they are added as-is
    # rather than re-resolved (re-resolving would follow an injected symlink to
    # a directory the user never approved).
    roots.extend(load_trusted_skill_dirs())
    return skills, roots


# [해설] 스킬 본문을 모델에게 보낼 사용자 프롬프트로 감싸고, 체크포인트에 남길 `__skill` 메타데이터를 만든다.
# [해설] 호출자: app.py(TUI `/skill:`), client/non_interactive.py. 결과 prompt는 _send_to_agent 등으로 전송된다.
def build_skill_invocation_envelope(
    skill: ExtendedSkillMetadata,
    content: str,
    args: str = "",
) -> SkillInvocationEnvelope:
    """Build the wrapped prompt and persisted metadata for a skill.

    Args:
        skill: Loaded skill metadata.
        content: Raw `SKILL.md` content.
        args: Optional user request appended after the skill body.

    Returns:
        A `SkillInvocationEnvelope` with the composed prompt and
            `message_kwargs` containing persisted skill metadata.
    """
    # [해설] 프롬프트 형식: 안내 문장 + `---`로 감싼 SKILL.md 원문(frontmatter 포함) + 선택적 **User request**.
    prompt = (
        f"I'm invoking the skill `{skill['name']}`. "
        "Below are the full instructions from the skill's SKILL.md file. "
        "Follow these instructions to complete the task.\n\n"
        f"---\n{content}\n---"
    )
    if args:
        prompt += f"\n\n**User request:** {args}"

    # [해설] `__skill` 키: UI가 메시지를 "스킬 호출"로 렌더링하고 재개 시 복원하는 데 쓰는 메타데이터(추정).
    message_kwargs = {
        "additional_kwargs": {
            "__skill": {
                "name": skill["name"],
                "description": str(skill.get("description", "")),
                "source": str(skill.get("source", "")),
                "args": args,
            },
        },
    }
    return SkillInvocationEnvelope(
        prompt=prompt,
        message_kwargs=message_kwargs,
        skill_name=skill["name"],
    )
