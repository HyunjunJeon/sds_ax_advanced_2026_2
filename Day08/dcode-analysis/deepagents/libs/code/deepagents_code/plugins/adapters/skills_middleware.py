"""Code-local skills middleware adapter for plugin namespaces."""

# [해설] 이 모듈의 역할: SDK SkillsMiddleware를 상속해 "플러그인 네임스페이스" 스킬을 지원하는 dcode 전용 어댑터.
# [해설] SDK는 source 하위 1단계만 스캔하지만, 플러그인 source는 재귀로 탐색하고 이름을 `plugin:sub:skill`로 정규화한다.
# [해설] 실행 프로세스: 서버(agent.py가 그래프 조립 시 PluginSkillsMiddleware를 생성). 단 load_namespaced_skills는
# [해설] 클라이언트 경로(skills/load.list_skills, tui/modals/plugin_manager/state.py)에서도 재사용된다.
# [해설] 주요 진입점 심볼: PluginSkillsMiddleware, load_namespaced_skills/aload_namespaced_skills, discover_skill_dirs.
# [해설] 관련 분석 문서: analysis/06-memory-skills.md, analysis/07-mcp-hooks-extensions-plugins.md
# [해설] 관련 공식 문서: docs_official/code/memory-and-skills.md, docs_official/code/plugins.md
# [해설][SDK] SDK private 함수 `_skill_metadata_from_response`, `_list_skills_with_errors`, `_alist_skills_with_errors`에
# [해설] 의존한다(deepagents/middleware/skills.py). SDK 내부 변경에 취약한 결합 지점이다.
from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from deepagents.backends.protocol import FileInfo, LsResult
from deepagents.backends.utils import to_posix_path
from deepagents.middleware import skills as sdk_skills
from deepagents.middleware.skills import SkillsMiddleware

from deepagents_code.plugins.adapters.skills import (
    CodeSkillSource,
    SkillNamespace,
    namespaced_skill_name,
)
from deepagents_code.skills.merge import merge_skill

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# [해설] CodeSkillSource 튜플 길이 3이면 (path, label, namespace) → 플러그인 소스로 판정. SKILL.md는 스킬 디렉터리 표식 파일.
_PLUGIN_SKILL_SOURCE_LENGTH = 3
_SKILL_FILE = "SKILL.md"


# [해설] 백엔드 ls 결과는 버전/구현에 따라 LsResult 객체 또는 list일 수 있어 정규화한다.
def _entries(ls_result: object) -> list[FileInfo]:
    """Normalize a backend `ls` result to a list of entry dicts.

    Returns:
        The listing entries, or an empty list when the result is empty or an
        unexpected shape.
    """
    if isinstance(ls_result, LsResult):
        return list(ls_result.entries or [])
    if isinstance(ls_result, list):
        return cast("list[FileInfo]", ls_result)
    return []


# [해설] ls 항목 중 직속 하위 디렉터리만 (이름, 경로)로 추린다. 경로 비교는 POSIX 형식으로 통일.
def _child_dirs(entries: list[FileInfo], root: str) -> list[tuple[str, str]]:
    """Return `(name, path)` for each immediate subdirectory in `entries`.

    Returns:
        Name/path pairs for each immediate subdirectory, excluding `root`.
    """
    root_posix = PurePosixPath(to_posix_path(root))
    dirs: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.get("is_dir"):
            continue
        path = entry["path"]
        name = PurePosixPath(to_posix_path(path)).name
        # Skip the source dir itself if a backend echoes it back.
        if PurePosixPath(to_posix_path(path)) == root_posix:
            continue
        dirs.append((name, path))
    return dirs


# [해설] 현재 디렉터리 바로 아래에 SKILL.md가 있는지(=스킬 디렉터리인지) 판정. 더 깊은 SKILL.md는 무시.
def _has_skill_file(entries: list[FileInfo], root: str) -> bool:
    """Return whether `entries` contains a `SKILL.md` directly under `root`."""
    root_posix = PurePosixPath(to_posix_path(root))
    for entry in entries:
        path = PurePosixPath(to_posix_path(entry["path"]))
        if path.name == _SKILL_FILE and path.parent == root_posix:
            return True
    return False


# [해설] 스킬 디렉터리 경로 + "SKILL.md".
def _skill_md_path(skill_dir: str) -> str:
    """Return the `SKILL.md` path inside a skill directory."""
    return str(PurePosixPath(to_posix_path(skill_dir)) / _SKILL_FILE)


# [해설] 메타데이터 복사본의 name을 namespaced_skill_name(plugins/adapters/skills.py)으로 `ns:sub...:name` 형태로 바꾼다.
# [해설] 원본 dict는 변경하지 않는다.
def _namespace_skill(
    skill: sdk_skills.SkillMetadata,
    namespace: SkillNamespace,
    subfolders: tuple[str, ...],
) -> sdk_skills.SkillMetadata:
    """Return a copy of `skill` with a namespace-qualified name."""
    return cast(
        "sdk_skills.SkillMetadata",
        {
            **skill,
            "name": namespaced_skill_name(namespace, skill["name"], subfolders),
        },
    )


# [해설] source 아래를 DFS로 탐색해 SKILL.md를 가진 디렉터리를 찾는다. 스킬 디렉터리를 찾으면 그 아래로는 내려가지 않는다(leaf).
def discover_skill_dirs(
    backend: BackendProtocol,
    source_path: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return `(skill_dir, subfolders)` pairs found under `source_path`.

    Walks the source tree, treating any directory that directly contains a
    `SKILL.md` as a skill directory (a recursion leaf, like a plugin walker).
    `subfolders` holds the directory names between the source
    root and the skill directory, excluding the skill directory's own name.

    Returns:
        Skill directories paired with their intermediate subfolder segments.
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    # `path_segments` accumulates directory names from the source root down to
    # and including `current`. A skill directory's own name is dropped when
    # naming, since the skill's terminal identifier is its frontmatter name;
    # only the directories above it form the namespace segments.
    # [해설][흐름] 1) 탐색 루트를 resolve. visited로 심링크 순환을 막는다.
    # [해설][주의] resolve/is_relative_to는 로컬 Path 연산이다 → 플러그인 source가 호스트 로컬 경로라는 전제(추정).
    source_root = Path(source_path).resolve()
    visited: set[Path] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(str(source_root), ())]
    while stack:
        current, path_segments = stack.pop()
        try:
            resolved = Path(current).resolve()
        except (OSError, RuntimeError):
            logger.warning("Could not resolve plugin skill directory %s", current)
            continue
        # [해설][주의] resolve한 경로가 source 루트 밖(심링크 탈출)이면 건너뛴다.
        if not resolved.is_relative_to(source_root) or resolved in visited:
            continue
        visited.add(resolved)
        resolved_path = str(resolved)
        entries = _entries(backend.ls(resolved_path))
        # [해설][흐름] 2) SKILL.md가 있으면 스킬로 기록. path_segments[:-1]로 스킬 디렉터리 자신의 이름은 네임스페이스에서 제외한다
        # [해설] (최종 이름은 frontmatter의 name). 루트 자체가 스킬이면 segments는 빈 튜플.
        if _has_skill_file(entries, resolved_path):
            found.append((resolved_path, path_segments[:-1]))
            continue
        # [해설][흐름] 3) 아니면 하위 디렉터리를 스택에 넣어 계속 탐색.
        for name, path in _child_dirs(entries, resolved_path):
            stack.append((path, (*path_segments, name)))
    return found


# [해설] discover_skill_dirs의 async 버전. resolve는 to_thread, ls는 backend.als 사용. 로직은 동일.
async def adiscover_skill_dirs(
    backend: BackendProtocol,
    source_path: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Async counterpart of `discover_skill_dirs`.

    Returns:
        Skill directories paired with their intermediate subfolder segments.
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    source_root = await asyncio.to_thread(Path(source_path).resolve)
    visited: set[Path] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(str(source_root), ())]
    while stack:
        current, path_segments = stack.pop()
        try:
            resolved = await asyncio.to_thread(Path(current).resolve)
        except (OSError, RuntimeError):
            logger.warning("Could not resolve plugin skill directory %s", current)
            continue
        if not resolved.is_relative_to(source_root) or resolved in visited:
            continue
        visited.add(resolved)
        resolved_path = str(resolved)
        entries = _entries(await backend.als(resolved_path))
        if _has_skill_file(entries, resolved_path):
            found.append((resolved_path, path_segments[:-1]))
            continue
        for name, path in _child_dirs(entries, resolved_path):
            stack.append((path, (*path_segments, name)))
    return found


# [해설] 플러그인 source의 모든 스킬을 로드하고 이름에 네임스페이스를 붙인다.
# [해설] 호출자: PluginSkillsMiddleware.before_agent, skills/load.list_skills, tui/modals/plugin_manager/state.py.
def load_namespaced_skills(
    backend: BackendProtocol,
    source_path: str,
    namespace: SkillNamespace,
) -> list[sdk_skills.SkillMetadata]:
    """Load and namespace every skill found under a plugin source.

    Reads each discovered skill directory's `SKILL.md` directly, since the SDK
    loader only scans one level below a source and would not read a leaf
    directory's own `SKILL.md`. Nested directories become `:`-joined namespace
    segments (e.g. `plugin:foo:bar:review`).

    Returns:
        Namespace-qualified skill metadata for the source.
    """
    # [해설][흐름] 1) 스킬 디렉터리 발견 → 2) SKILL.md를 download_files로 일괄 다운로드 → 3) SDK 파서로 frontmatter 해석.
    skill_dirs = discover_skill_dirs(backend, source_path)
    if not skill_dirs:
        return []
    paths = [_skill_md_path(skill_dir) for skill_dir, _ in skill_dirs]
    responses = backend.download_files(paths)
    skills: list[sdk_skills.SkillMetadata] = []
    for (skill_dir, segments), path, response in zip(
        skill_dirs, paths, responses, strict=True
    ):
        # [해설][SDK] 파싱 실패(frontmatter 오류 등)는 None → 조용히 건너뛴다.
        # [해설][주의] 네임스페이스 source는 SDK 경로와 달리 skills_load_errors에 오류가 수집되지 않는다(코드상 errors 목록 없음).
        skill = sdk_skills._skill_metadata_from_response(response, skill_dir, path)
        if skill is not None:
            skills.append(_namespace_skill(skill, namespace, segments))
    return skills


# [해설] load_namespaced_skills의 async 버전(adiscover_skill_dirs + adownload_files).
async def aload_namespaced_skills(
    backend: BackendProtocol,
    source_path: str,
    namespace: SkillNamespace,
) -> list[sdk_skills.SkillMetadata]:
    """Async counterpart of `load_namespaced_skills`.

    Returns:
        Namespace-qualified skill metadata for the source.
    """
    skill_dirs = await adiscover_skill_dirs(backend, source_path)
    if not skill_dirs:
        return []
    paths = [_skill_md_path(skill_dir) for skill_dir, _ in skill_dirs]
    responses = await backend.adownload_files(paths)
    skills: list[sdk_skills.SkillMetadata] = []
    for (skill_dir, segments), path, response in zip(
        skill_dirs, paths, responses, strict=True
    ):
        skill = sdk_skills._skill_metadata_from_response(response, skill_dir, path)
        if skill is not None:
            skills.append(_namespace_skill(skill, namespace, segments))
    return skills


# [해설] agent.py가 스킬 source 목록(플러그인 포함)으로 생성하는 dcode 스킬 미들웨어.
# [해설] 네임스페이스 없는 source는 SDK와 완전히 같게, 플러그인 source는 재귀+네임스페이스로 처리한 뒤 merge_skill로 병합.
# [해설][SDK] 부모 SDK SkillsMiddleware가 시스템 프롬프트 주입(스킬 목록 안내)을 담당하고, 여기서는 before_agent 로딩만 교체한다.
class PluginSkillsMiddleware(SkillsMiddleware):
    """Load namespaced plugin skills without extending the SDK source API.

    Wraps the SDK `SkillsMiddleware`. Sources without a namespace load exactly
    as the SDK loads them. Sources carrying a plugin namespace are walked
    recursively so nested skill directories (`skills/foo/bar/review/SKILL.md`)
    are discovered, and each skill's name is qualified as
    `plugin_id:foo:bar:review` before the last-one-wins merge — matching
    the plugin skill naming convention.
    """

    # [해설] SDK에는 (path, label) 2-튜플만 넘기고, 3번째 요소(namespace)는 self._namespaces에 같은 인덱스로 따로 보관한다.
    def __init__(
        self,
        *,
        backend: BackendProtocol,
        sources: Sequence[CodeSkillSource],
        system_prompt: str | None = sdk_skills.SKILLS_SYSTEM_PROMPT,
    ) -> None:
        """Initialize the middleware with Code-local plugin source tuples.

        Args:
            backend: Backend used to load skill files.
            sources: Ordered Code skill sources, optionally including a plugin
                namespace as the third tuple item.
            system_prompt: Skills prompt template passed to the SDK middleware.
        """
        sdk_sources = [(source[0], source[1]) for source in sources]
        super().__init__(
            backend=backend,
            sources=sdk_sources,
            system_prompt=system_prompt,
        )
        self._namespaces = tuple(
            source[2] if len(source) == _PLUGIN_SKILL_SOURCE_LENGTH else None
            for source in sources
        )

    # [해설] 병합 결과를 SkillsStateUpdate로 만든다. 오류가 있으면 경고 로그 + state의 skills_load_errors에 기록.
    @staticmethod
    def _state_update(
        all_skills: dict[str, sdk_skills.SkillMetadata],
        errors: list[str],
    ) -> sdk_skills.SkillsStateUpdate:
        """Build the middleware state update, logging any load errors.

        Returns:
            The state update carrying merged skill metadata and any errors.
        """
        update = sdk_skills.SkillsStateUpdate(skills_metadata=list(all_skills.values()))
        if errors:
            logger.warning("Skills load errors: %s", errors)
            update["skills_load_errors"] = errors
        return update

    # [해설] 에이전트 실행 시작 시(동기) 스킬 메타데이터를 state에 한 번만 로드한다.
    def before_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,  # noqa: ARG002
        config: RunnableConfig,  # noqa: ARG002
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Load and namespace plugin skills before collision resolution.

        Returns:
            A state update containing collision-safe skill metadata, or `None`
            when skills are already loaded.
        """
        # [해설][흐름] 1) 이미 state에 skills_metadata가 있으면(같은 스레드 재실행) 재로딩하지 않는다 → 스레드 내 캐시 효과.
        if "skills_metadata" in state:
            return None

        backend = self._backend
        all_skills: dict[str, sdk_skills.SkillMetadata] = {}
        merged_source_labels: dict[str, str | None] = {}
        errors: list[str] = []

        # `self.sources`, `self.source_labels`, and `self._namespaces` are all
        # built from the same source sequence at the same indices (see
        # `__init__` and the SDK base), so this zip is aligned by construction.
        # `strict=True` turns future *length* drift into a loud error; it does
        # not catch a same-length reorder, which would still mispair silently.
        # [해설][흐름] 2) source를 낮은→높은 우선순위 순서로 순회. namespace 유무로 SDK 스캔/재귀 스캔을 분기.
        for source_path, source_label, namespace in zip(
            self.sources, self.source_labels, self._namespaces, strict=True
        ):
            if namespace is None:
                source_skills, source_error = sdk_skills._list_skills_with_errors(
                    backend, source_path
                )
                if source_error is not None:
                    errors.append(source_error)
            else:
                source_skills = load_namespaced_skills(backend, source_path, namespace)
            # [해설][흐름] 3) merge_skill로 last-one-wins 병합(skills/load.list_skills와 같은 규칙).
            for skill in source_skills:
                merge_skill(
                    all_skills,
                    merged_source_labels,
                    skill,
                    source_label=source_label,
                )

        return self._state_update(all_skills, errors)

    # [해설] before_agent의 비동기 버전. 서버에서 그래프를 async로 실행할 때 사용된다.
    async def abefore_agent(
        self,
        state: sdk_skills.SkillsState,
        runtime: Runtime,  # noqa: ARG002
        config: RunnableConfig,  # noqa: ARG002
    ) -> sdk_skills.SkillsStateUpdate | None:
        """Asynchronously load and namespace skills before collision resolution.

        Returns:
            A state update containing collision-safe skill metadata, or `None`
            when skills are already loaded.
        """
        if "skills_metadata" in state:
            return None

        backend = self._backend
        all_skills: dict[str, sdk_skills.SkillMetadata] = {}
        merged_source_labels: dict[str, str | None] = {}
        errors: list[str] = []

        # See `before_agent`: the three sequences are index-aligned by
        # construction, and `strict=True` guards against future length drift.
        for source_path, source_label, namespace in zip(
            self.sources, self.source_labels, self._namespaces, strict=True
        ):
            if namespace is None:
                (
                    source_skills,
                    source_error,
                ) = await sdk_skills._alist_skills_with_errors(backend, source_path)
                if source_error is not None:
                    errors.append(source_error)
            else:
                source_skills = await aload_namespaced_skills(
                    backend, source_path, namespace
                )
            for skill in source_skills:
                merge_skill(
                    all_skills,
                    merged_source_labels,
                    skill,
                    source_label=source_label,
                )

        return self._state_update(all_skills, errors)
