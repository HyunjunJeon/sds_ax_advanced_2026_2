from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    NoMatchReason,
    NotHandled,
    ParsedOooCommand,
    ResolveOutcome,
    ResolveRequest,
    resolve_parsed_skill_dispatch,
    resolve_skill_dispatch,
)

_ERROR_RESULT_KEYS = ("result", "outcome", "code", "message", "skill_path")


def _write_skill(skills_dir: Path, skill_name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter}---\n# {skill_name}\n", encoding="utf-8")
    return skill_md_path


def _error_snapshot(result: object) -> dict[str, str | None]:
    if isinstance(result, NotHandled):
        return {
            "result": "not_handled",
            "outcome": result.outcome.value,
            "code": result.category.value,
            "message": result.reason,
            "skill_path": None,
        }
    if isinstance(result, InvalidSkill):
        return {
            "result": "invalid_skill",
            "outcome": result.outcome.value,
            "code": result.category.value,
            "message": result.reason,
            "skill_path": result.skill_path.as_posix(),
        }
    raise AssertionError(f"expected router error result, got {type(result).__name__}")


def _assert_repeatable_error_shape(
    resolve: Callable[[], object],
    expected: dict[str, str | None],
) -> None:
    snapshots = [_error_snapshot(resolve()) for _ in range(3)]

    assert all(tuple(snapshot) == _ERROR_RESULT_KEYS for snapshot in snapshots)
    assert snapshots == [expected, expected, expected]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        pytest.param(
            "",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="empty-prompt",
        ),
        pytest.param(
            "   \t\n",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="whitespace-only-prompt",
        ),
        pytest.param(
            "ooo",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="missing-skill-name",
        ),
        pytest.param(
            "ooo    ",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="incomplete-ooo-command",
        ),
        pytest.param(
            "/ouroboros:",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="incomplete-slash-command",
        ),
        pytest.param(
            "/ouroboros: run",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="slash-command-missing-skill-name",
        ),
        pytest.param(
            "ooo run!",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="malformed-skill-identifier",
        ),
        pytest.param(
            "ooo /run seed.yaml",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.NOT_A_SKILL_COMMAND.value,
                "message": "not a skill command",
                "skill_path": None,
            },
            id="malformed-ooo-target",
        ),
        pytest.param(
            "ooo missing seed.yaml",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.SKILL_NOT_FOUND.value,
                "message": "skill not found",
                "skill_path": None,
            },
            id="unknown-ooo-skill",
        ),
        pytest.param(
            "/ouroboros:missing seed.yaml",
            {
                "result": "not_handled",
                "outcome": ResolveOutcome.NO_MATCH.value,
                "code": NoMatchReason.SKILL_NOT_FOUND.value,
                "message": "skill not found",
                "skill_path": None,
            },
            id="unknown-slash-skill",
        ),
    ],
)
def test_malformed_or_unknown_ooo_prompts_return_repeatable_error_shape(
    tmp_path: Path,
    prompt: str,
    expected: dict[str, str | None],
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    request = ResolveRequest(
        prompt=prompt,
        cwd=tmp_path / "workspace",
        skills_dir=skills_dir,
    )

    _assert_repeatable_error_shape(lambda: resolve_skill_dispatch(request), expected)


@pytest.mark.parametrize(
    ("parsed", "expected_skill_path", "expected_message"),
    [
        pytest.param(
            ParsedOooCommand(
                skill_name=object(),  # type: ignore[arg-type]
                command_prefix="ooo run",
                remainder="seed.yaml",
            ),
            ".",
            "malformed parsed command: skill_name must be a string",
            id="invalid-skill-name-type",
        ),
        pytest.param(
            ParsedOooCommand(
                skill_name="run!",
                command_prefix="ooo run!",
                remainder="seed.yaml",
            ),
            "run!",
            "malformed parsed command: skill_name must be a valid skill identifier",
            id="invalid-skill-name",
        ),
        pytest.param(
            ParsedOooCommand(
                skill_name="run",
                command_prefix=object(),  # type: ignore[arg-type]
                remainder="seed.yaml",
            ),
            "run",
            "malformed parsed command: command_prefix must be a string",
            id="invalid-command-prefix-type",
        ),
        pytest.param(
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo execute",
                remainder="seed.yaml",
            ),
            "run",
            "malformed parsed command: command_prefix must match skill_name",
            id="prefix-skill-mismatch",
        ),
        pytest.param(
            ParsedOooCommand(
                skill_name="run",
                command_prefix="ooo run",
                remainder=object(),  # type: ignore[arg-type]
            ),
            "run",
            "malformed parsed command: remainder must be a string or null",
            id="invalid-remainder-type",
        ),
    ],
)
def test_malformed_parsed_dispatches_return_repeatable_invalid_input_shape(
    tmp_path: Path,
    parsed: ParsedOooCommand,
    expected_skill_path: str,
    expected_message: str,
) -> None:
    expected = {
        "result": "invalid_skill",
        "outcome": ResolveOutcome.INVALID_INPUT.value,
        "code": InvalidInputReason.MALFORMED_PARSED_COMMAND.value,
        "message": expected_message,
        "skill_path": expected_skill_path,
    }

    _assert_repeatable_error_shape(
        lambda: resolve_parsed_skill_dispatch(
            parsed,
            cwd=tmp_path / "workspace",
            skills_dir=tmp_path / "skills",
        ),
        expected,
    )


@pytest.mark.parametrize(
    ("frontmatter", "expected_message"),
    [
        pytest.param(
            "name: run\n",
            "missing required frontmatter key: mcp_tool",
            id="missing-mcp-tool",
        ),
        pytest.param(
            "name: run\nmcp_tool: ouroboros-execute-seed\nmcp_args: {}\n",
            "mcp_tool must contain only letters, digits, and underscores",
            id="invalid-mcp-tool-name",
        ),
        pytest.param(
            "name: run\nmcp_tool: ouroboros_execute_seed\nmcp_args: []\n",
            "mcp_args must be a mapping with string keys and YAML-safe values",
            id="invalid-mcp-args-shape",
        ),
    ],
)
def test_malformed_skill_dispatch_metadata_returns_repeatable_error_shape(
    tmp_path: Path,
    frontmatter: str,
    expected_message: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(skills_dir, "run", frontmatter)
    request = ResolveRequest(
        prompt="ooo run seed.yaml",
        cwd=tmp_path / "workspace",
        skills_dir=skills_dir,
    )
    expected = {
        "result": "invalid_skill",
        "outcome": ResolveOutcome.INVALID_INPUT.value,
        "code": InvalidInputReason.FRONTMATTER_INVALID.value,
        "message": expected_message,
        "skill_path": skill_md_path.as_posix(),
    }

    _assert_repeatable_error_shape(lambda: resolve_skill_dispatch(request), expected)
