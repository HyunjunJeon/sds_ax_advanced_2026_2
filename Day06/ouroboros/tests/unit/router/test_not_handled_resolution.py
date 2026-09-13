from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.router import (
    NoMatchReason,
    NotHandled,
    ResolveOutcome,
    ResolveRequest,
    ResolveResult,
    resolve_skill_dispatch,
)


def _resolve_without_exception(request: ResolveRequest) -> ResolveResult:
    try:
        return resolve_skill_dispatch(request)
    except Exception as exc:  # pragma: no cover - failure path should be explicit.
        pytest.fail(f"router raised instead of returning NotHandled: {exc!r}")


def test_non_dispatch_input_returns_not_handled_without_exception(tmp_path: Path) -> None:
    result = _resolve_without_exception(
        ResolveRequest(
            prompt="please run the seed file",
            cwd=tmp_path / "workspace",
            skills_dir=tmp_path / "skills",
        )
    )

    assert isinstance(result, NotHandled)
    assert result.reason == "not a skill command"
    assert result.category is NoMatchReason.NOT_A_SKILL_COMMAND
    assert result.outcome is ResolveOutcome.NO_MATCH


def test_missing_skill_resolution_returns_not_handled_without_exception(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    result = _resolve_without_exception(
        ResolveRequest(
            prompt="ooo missing seed.yaml",
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, NotHandled)
    assert result.reason == "skill not found"
    assert result.category is NoMatchReason.SKILL_NOT_FOUND
    assert result.outcome is ResolveOutcome.NO_MATCH
