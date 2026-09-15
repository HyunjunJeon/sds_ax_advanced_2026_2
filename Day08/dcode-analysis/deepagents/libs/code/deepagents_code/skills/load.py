"""Skill loader for CLI commands.

This module provides filesystem-based skill discovery for CLI operations
(list, create, info, delete). It wraps the prebuilt middleware functionality from
deepagents.middleware.skills and adapts it for direct filesystem access
needed by CLI commands.

For middleware usage within agents, use
deepagents.middleware.skills.SkillsMiddleware directly.
"""

# [해설] 이 모듈의 역할: 클라이언트 측(CLI/TUI/headless)에서 로컬 파일시스템의 스킬을 발견·병합하고 SKILL.md 본문을 읽는다.
# [해설] 에이전트 그래프 안의 스킬 로딩(SDK SkillsMiddleware / plugins/adapters/skills_middleware.PluginSkillsMiddleware)과는
# [해설] 별개 경로이며, 같은 우선순위 규칙과 merge_skill(last-one-wins)을 공유해 결과가 일치하도록 한다.
# [해설] 실행 프로세스: 클라이언트. 주요 진입점 심볼: list_skills, load_skill_content, ExtendedSkillMetadata.
# [해설] 호출자: skills/invocation.discover_skills_and_roots(TUI `/skill:` 및 headless), skills/commands.py(`dcode skills ...`),
# [해설] app.py, client/non_interactive.py, skills/trust.py, tui/widgets/skill_trust.py.
# [해설] 관련 분석 문서: analysis/06-memory-skills.md / 공식 문서: docs_official/code/memory-and-skills.md
# [해설][SDK] `_list_skills`는 SDK `deepagents/middleware/skills.py`의 private 함수(1단계 디렉터리 스캔)를 직접 import한다.
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from deepagents.backends.filesystem import FilesystemBackend

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
from deepagents.middleware.skills import (
    SkillMetadata,
    _list_skills as list_skills_from_backend,  # noqa: PLC2701  # Intentional access to internal skill listing
)

from deepagents_code._version import __version__ as _cli_version
from deepagents_code.skills.merge import merge_skill

logger = logging.getLogger(__name__)


# [해설] SDK SkillMetadata(TypedDict)에 표시용 `source` 라벨을 더한 타입. `dcode skills list` 등 UI 출력에 쓰인다.
# [해설][주의] docstring은 4개 라벨만 나열하지만 실제 Literal에는 "plugin"도 포함된다.
class ExtendedSkillMetadata(SkillMetadata):
    """Extended skill metadata for CLI display, adds source tracking.

    Attributes:
        source: Origin of the skill. One of `'built-in'`, `'user'`, `'project'`,
            or `'claude (experimental)'`.
    """

    source: Literal["built-in", "plugin", "user", "project", "claude (experimental)"]


# Re-export for CLI commands
__all__ = ["SkillMetadata", "list_skills", "load_skill_content"]


# [해설] 여러 스킬 디렉터리를 우선순위 순으로 스캔해 이름 기준으로 병합한 목록을 반환한다.
# [해설] 소스는 낮은→높은 우선순위: built-in < plugin < user(.deepagents) < user(.agents) < project(.deepagents)
# [해설] < project(.agents) < ~/.claude < .claude(실험적). 같은 이름이면 뒤(높은 우선순위)가 이긴다.
def list_skills(
    *,
    built_in_skills_dir: Path | None = None,
    plugin_skill_sources: Sequence[tuple[Path, str]] = (),
    user_skills_dir: Path | None = None,
    project_skills_dir: Path | None = None,
    user_agent_skills_dir: Path | None = None,
    project_agent_skills_dir: Path | None = None,
    user_claude_skills_dir: Path | None = None,
    project_claude_skills_dir: Path | None = None,
) -> list[ExtendedSkillMetadata]:
    """List skills from built-in, user, and/or project directories.

    This is a dcode-specific wrapper around the prebuilt middleware's skill loading
    functionality. It uses `FilesystemBackend` to load skills from local directories.

    Precedence order (lowest to highest):
    0. `built_in_skills_dir` (`<package>/built_in_skills/`)
    1. `plugin_skill_sources`
    2. `user_skills_dir` (`~/.deepagents/{agent}/skills/`)
    3. `user_agent_skills_dir` (`~/.agents/skills/`)
    4. `project_skills_dir` (`.deepagents/skills/`)
    5. `project_agent_skills_dir` (`.agents/skills/`)
    6. `user_claude_skills_dir` (`~/.claude/skills/`, experimental)
    7. `project_claude_skills_dir` (`.claude/skills/`, experimental)

    Skills from higher-precedence directories override those with the same name.

    Args:
        built_in_skills_dir: Path to built-in skills shipped with the package.
        plugin_skill_sources: Plugin skill source directories with namespaces.
        user_skills_dir: Path to `~/.deepagents/{agent}/skills/`.
        project_skills_dir: Path to `.deepagents/skills/`.
        user_agent_skills_dir: Path to `~/.agents/skills/` (alias).
        project_agent_skills_dir: Path to `.agents/skills/` (alias).
        user_claude_skills_dir: Path to `~/.claude/skills/` (experimental).
        project_claude_skills_dir: Path to `.claude/skills/` (experimental).

    Returns:
        Merged list of skill metadata from all sources, with higher-precedence
            directories taking priority when names conflict.
    """
    # [해설][흐름] 1) 이름→메타데이터 누적 dict와, 충돌 로그용 이전 source 라벨 dict.
    all_skills: dict[str, ExtendedSkillMetadata] = {}
    merged_source_labels: dict[str, str | None] = {}

    # [해설][흐름] 2) (디렉터리, 라벨, 실험적 여부, 네임스페이스) 튜플을 우선순위 순서로 나열. 순서가 곧 우선순위다.
    sources: list[tuple[Path | None, str, bool, str]] = [
        (built_in_skills_dir, "built-in", False, ""),
        *[
            (path, "plugin", False, namespace)
            for path, namespace in plugin_skill_sources
        ],
        (user_skills_dir, "user", False, ""),
        (user_agent_skills_dir, "user", False, ""),
        (project_skills_dir, "project", False, ""),
        (project_agent_skills_dir, "project", False, ""),
        (user_claude_skills_dir, "claude (experimental)", True, ""),
        (project_claude_skills_dir, "claude (experimental)", True, ""),
    ]
    """Sources in precedence order (lowest to highest).

    Each tuple: `(directory, source label, is_experimental, namespace)`.

    Each source is individually try/except-guarded so a single inaccessible
    directory doesn't block the rest.
    """

    # [해설][흐름] 3) 존재하는 디렉터리만 FilesystemBackend(virtual_mode=False, 실제 경로)로 감싸 스캔.
    for skill_dir, source_label, experimental, namespace in sources:
        if not skill_dir or not skill_dir.exists():
            continue
        try:
            backend = FilesystemBackend(root_dir=str(skill_dir), virtual_mode=False)
            # [해설] 플러그인 소스는 재귀 탐색 + `plugin:sub:skill` 이름 부여(런타임 PluginSkillsMiddleware와 같은 함수 재사용).
            # [해설] 일반 소스는 SDK의 1단계 스캔(각 하위 디렉터리의 SKILL.md)만 수행한다.
            if namespace:
                # Plugin sources are walked recursively so nested skill
                # directories are namespaced as `plugin:sub:skill`, matching
                # both the runtime middleware and plugin conventions.
                from deepagents_code.plugins.adapters.skills_middleware import (
                    load_namespaced_skills,
                )

                skills = load_namespaced_skills(
                    backend, str(skill_dir.resolve()), namespace
                )
            else:
                skills = list_skills_from_backend(backend=backend, source_path=".")
            if experimental and skills:
                logger.info(
                    "Discovered %d skill(s) from experimental Claude path: %s",
                    len(skills),
                    skill_dir,
                )
            # [해설][흐름] 4) 각 스킬에 source 라벨을 붙인다. built-in이면 metadata에 설치된 dcode 버전을 주입해 출처 버전을 추적.
            for skill in skills:
                extra: dict[str, object] = {"source": source_label}
                if source_label == "built-in":
                    extra["metadata"] = {
                        **skill["metadata"],
                        "deepagents-code-version": _cli_version,
                    }
                extended = cast("ExtendedSkillMetadata", {**skill, **extra})
                # [해설] merge_skill(skills/merge.py): 이름 충돌 시 나중 것으로 교체하고 DEBUG 로그를 남긴다.
                merge_skill(
                    all_skills,
                    merged_source_labels,
                    extended,
                    source_label=source_label,
                )
        # [해설] 소스 단위 격리: 한 디렉터리의 오류가 전체 발견을 막지 않도록 경고만 남기고 다음 소스로 진행.
        except Exception:
            # Degrade gracefully — one malformed/inaccessible source must not
            # block discovery of others, so catch broadly and log instead.
            # WARNING (not ERROR) because a half-written SKILL.md from a user is
            # an expected condition, not a code defect.
            logger.warning(
                "Could not load skills from %s",
                skill_dir,
                exc_info=True,
            )

    return list(all_skills.values())


# [해설] 사용자가 스킬을 명시 호출할 때 SKILL.md 원문(frontmatter 포함)을 읽는다.
# [해설][설계] 심링크를 따라간 resolve 경로가 allowed_roots(표준 스킬 dir + extra_allowed_dirs + trust store) 안인지 검사해
# [해설] 스킬 폴더 밖 파일(예: 심링크로 ~/.ssh) 읽기를 막는다. roots는 호출자가 미리 resolve해야 한다.
def load_skill_content(
    skill_path: str,
    *,
    allowed_roots: Sequence[Path] = (),
) -> str | None:
    """Read the full raw SKILL.md content for a skill.

    Returns the complete file content including any YAML frontmatter.
    Callers are responsible for parsing or stripping frontmatter if needed.

    When `allowed_roots` is provided, the resolved path must fall within at
    least one root directory. This prevents symlink traversal from reading files
    outside known skill directories.

    Args:
        skill_path: Path to the SKILL.md file (from `SkillMetadata['path']`).
        allowed_roots: Skill root directories the resolved path must be
            contained within.

            Callers must pre-resolve these via `Path.resolve()` — the resolved
            skill path is compared directly, so un-resolved roots cause false
            containment failures.

            If empty, containment is not checked.

    Returns:
        Full text content of the SKILL.md file, or `None` on read failure.

    Raises:
        PermissionError: If the resolved path is outside all `allowed_roots`.
    """
    from pathlib import Path

    # [해설][흐름] 1) 경로를 resolve(심링크 해소)한 뒤 containment 검사.
    path = Path(skill_path).resolve()

    # [해설][주의] allowed_roots가 비어 있으면 검사를 건너뛴다 — 호출자가 반드시 roots를 넘겨야 보안 효과가 있다.
    # [해설] 거부 시 PermissionError 메시지에 해결책(EXTRA_SKILLS_DIRS env 또는 [skills].extra_allowed_dirs)을 안내한다.
    if allowed_roots and not any(path.is_relative_to(root) for root in allowed_roots):
        logger.warning(
            "Skill path %s is outside all allowed roots, refusing to read",
            skill_path,
        )
        from deepagents_code._env_vars import EXTRA_SKILLS_DIRS
        from deepagents_code._paths import PATHS

        msg = (
            f"Skill path {skill_path} resolves outside all allowed skill "
            "directories. If this is a symlink, add the target directory to "
            f"{EXTRA_SKILLS_DIRS} or [skills].extra_allowed_dirs "
            f"in {PATHS.display(PATHS.profile.config_file)}."
        )
        raise PermissionError(msg)

    # [해설][흐름] 2) 읽기 실패(권한·인코딩)는 예외 대신 None을 반환 → 호출자가 사용자 알림으로 처리.
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "Could not read skill content from %s", skill_path, exc_info=True
        )
        return None
