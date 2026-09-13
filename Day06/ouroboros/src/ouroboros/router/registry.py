"""Canonical SKILL.md-backed registry for deterministic skill dispatch.

The registry maps every supported command identifier for a packaged skill to a
single runtime-neutral target. Direct skill directory names, frontmatter
``name``, and supported alias fields all normalize to the target skill
directory. The module is intentionally pure: no logging, no runtime message
assembly, and no MCP invocation.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import yaml

from ouroboros.router.types import NoMatchReason, NotHandled, ParsedOooCommand
from ouroboros.skills.artifacts import (
    SKILL_ENTRYPOINT,
    collect_skill_bundle_dirs,
    resolve_packaged_skills_dir,
)

_COMMAND_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMMAND_PREFIX_PATTERN = re.compile(
    r"^(?:(?:ooo)\s+|/ouroboros:)(?P<identifier>[a-z0-9][a-z0-9_-]*)(?:\s+.*)?$",
    re.IGNORECASE,
)
_ALIAS_FRONTMATTER_FIELDS = (
    "alias",
    "aliases",
    "command_aliases",
    "skill_aliases",
    "commands",
)


@dataclass(frozen=True, slots=True)
class SkillDispatchTarget:
    """Stable skill-level target resolved from a command identifier."""

    skill_name: str
    skill_path: Path
    identifiers: tuple[str, ...]

    @property
    def canonical_identifier(self) -> str:
        """Return the stable identifier runtimes use for skill lookup."""
        return self.skill_name

    @property
    def command_identifier(self) -> str:
        """Alias for callers that name the canonical target as a command."""
        return self.skill_name


SkillDispatchRegistryEntry = SkillDispatchTarget
type SkillDispatchTargetResolution = SkillDispatchTarget | NotHandled


class SkillDispatchRegistry:
    """Immutable mapping from command identifiers to canonical skill targets."""

    def __init__(self, targets: tuple[SkillDispatchTarget, ...]) -> None:
        self._targets = targets
        self._mapping = MappingProxyType(_build_identifier_mapping(targets))

    @classmethod
    def from_skills_dir(cls, skills_dir: str | Path) -> SkillDispatchRegistry:
        """Build a registry from a directory containing skill bundle folders."""
        root = Path(skills_dir).expanduser()
        targets = tuple(
            _target_from_skill_dir(skill_dir) for skill_dir in collect_skill_bundle_dirs(root)
        )
        return cls(targets)

    @property
    def targets(self) -> tuple[SkillDispatchTarget, ...]:
        """Return all canonical targets in deterministic order."""
        return self._targets

    @property
    def mapping(self) -> Mapping[str, SkillDispatchTarget]:
        """Return the identifier-to-target mapping."""
        return self._mapping

    def resolve(self, identifier: str | ParsedOooCommand) -> SkillDispatchTargetResolution:
        """Resolve one parsed command identifier to its canonical target."""
        normalized_identifier = normalize_skill_identifier(_command_identifier(identifier))
        if normalized_identifier is None:
            return _skill_not_found()
        target = self._mapping.get(normalized_identifier)
        if target is None:
            return _skill_not_found()
        return target


def normalize_skill_identifier(raw_identifier: str) -> str | None:
    """Normalize a parsed command identifier or alias to registry form."""
    candidate = raw_identifier.strip().lower()
    if not candidate:
        return None

    prefixed = _COMMAND_PREFIX_PATTERN.match(candidate)
    if prefixed is not None:
        candidate = prefixed.group("identifier")

    if _COMMAND_IDENTIFIER_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate


@contextmanager
def packaged_skill_dispatch_registry(
    *,
    skills_dir: str | Path | None = None,
) -> Iterator[SkillDispatchRegistry]:
    """Build a registry from the resolved packaged skills directory."""
    with resolve_packaged_skills_dir(
        skills_dir=skills_dir,
        anchor_file=__file__,
    ) as resolved_skills_dir:
        yield SkillDispatchRegistry.from_skills_dir(resolved_skills_dir)


@contextmanager
def resolve_skill_dispatch_target(
    identifier: str | ParsedOooCommand,
    *,
    skills_dir: str | Path | None = None,
) -> Iterator[SkillDispatchTargetResolution]:
    """Resolve an identifier while keeping packaged resource paths alive.

    The yielded target may reference package resources materialized through
    ``importlib.resources.as_file``. Callers that need to inspect the path must
    do so inside the ``with`` block.
    """
    try:
        with packaged_skill_dispatch_registry(skills_dir=skills_dir) as registry:
            yield registry.resolve(identifier)
    except FileNotFoundError:
        yield _skill_not_found()


def _command_identifier(identifier: str | ParsedOooCommand) -> str:
    """Return the command identifier used for canonical skill target lookup."""
    if isinstance(identifier, ParsedOooCommand):
        return identifier.skill_name
    return identifier


def _skill_not_found() -> NotHandled:
    """Build the typed missing-skill outcome used by shared resolution."""
    return NotHandled(reason="skill not found", category=NoMatchReason.SKILL_NOT_FOUND)


def _build_identifier_mapping(
    targets: tuple[SkillDispatchTarget, ...],
) -> dict[str, SkillDispatchTarget]:
    """Build a deterministic identifier map while preserving direct-name priority."""
    mapping: dict[str, SkillDispatchTarget] = {}

    for target in targets:
        mapping[target.skill_name] = target

    for target in targets:
        for identifier in target.identifiers:
            mapping.setdefault(identifier, target)

    return mapping


def _target_from_skill_dir(skill_dir: Path) -> SkillDispatchTarget:
    """Create one canonical dispatch target from a skill bundle directory."""
    skill_name = normalize_skill_identifier(skill_dir.name) or skill_dir.name.strip().lower()
    skill_path = skill_dir / SKILL_ENTRYPOINT
    frontmatter = _load_registry_frontmatter(skill_path)
    identifiers = _collect_target_identifiers(skill_name, frontmatter)
    return SkillDispatchTarget(
        skill_name=skill_name,
        skill_path=skill_path,
        identifiers=identifiers,
    )


def _collect_target_identifiers(
    skill_name: str,
    frontmatter: Mapping[str, Any],
) -> tuple[str, ...]:
    """Collect the canonical identifier plus frontmatter-defined aliases."""
    identifiers: list[str] = [skill_name]

    frontmatter_name = frontmatter.get("name")
    normalized_name = (
        normalize_skill_identifier(str(frontmatter_name)) if frontmatter_name else None
    )
    if normalized_name is not None:
        identifiers.append(normalized_name)

    for field in _ALIAS_FRONTMATTER_FIELDS:
        identifiers.extend(_normalize_alias_values(frontmatter.get(field)))

    return tuple(dict.fromkeys(identifiers))


def _normalize_alias_values(raw_aliases: Any) -> tuple[str, ...]:
    """Normalize string, CSV, and sequence alias metadata to command identifiers."""
    if raw_aliases is None:
        return ()

    if isinstance(raw_aliases, str):
        candidates = (raw_aliases,)
    elif isinstance(raw_aliases, Mapping):
        candidates = ()
    else:
        try:
            candidates = tuple(raw_aliases)
        except TypeError:
            candidates = (raw_aliases,)

    aliases: list[str] = []
    for candidate in candidates:
        parts = candidate.split(",") if isinstance(candidate, str) else (str(candidate),)
        for part in parts:
            normalized = normalize_skill_identifier(part)
            if normalized is not None:
                aliases.append(normalized)

    return tuple(dict.fromkeys(aliases))


def _load_registry_frontmatter(skill_md_path: Path) -> dict[str, Any]:
    """Load best-effort frontmatter needed for registry alias extraction."""
    try:
        content = skill_md_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        return {}

    frontmatter_text = "\n".join(lines[1:closing_index]).strip()
    if not frontmatter_text:
        return {}

    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return {}

    if not isinstance(parsed, Mapping):
        return {}

    return {str(key).lower(): value for key, value in parsed.items()}


__all__ = [
    "SkillDispatchRegistry",
    "SkillDispatchRegistryEntry",
    "SkillDispatchTargetResolution",
    "SkillDispatchTarget",
    "normalize_skill_identifier",
    "packaged_skill_dispatch_registry",
    "resolve_skill_dispatch_target",
]
