from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.router import (
    DispatchTargetKind,
    InvalidInputReason,
    InvalidSkill,
    MCPDispatchTarget,
    NoMatchReason,
    NormalizedMCPFrontmatter,
    NotHandled,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    resolve_skill_dispatch,
)
import ouroboros.router.dispatch as dispatch_module


def _write_skill(skills_dir: Path, skill_name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter}---\n# {skill_name}\n", encoding="utf-8")
    return skill_md_path


def _not_handled_shape(result: NotHandled) -> dict[str, str | None]:
    return {
        "result": "not_handled",
        "outcome": result.outcome.value,
        "code": result.category.value,
        "message": result.reason,
        "skill_path": None,
        "target": None,
        "dispatch_metadata": None,
    }


def _invalid_skill_shape(result: InvalidSkill) -> dict[str, str | None]:
    return {
        "result": "invalid_skill",
        "outcome": result.outcome.value,
        "code": result.category.value,
        "message": result.reason,
        "skill_path": result.skill_path.as_posix(),
        "target": None,
        "dispatch_metadata": None,
    }


def test_successful_dispatch_result_shape_exposes_runtime_payload_fields(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
aliases:
  - start
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
  summary: "cwd=$CWD seed=$1"
  nested:
    values:
      - "$1"
      - "$CWD"
      - true
  static_mode: deterministic
""",
    )
    runtime_cwd = tmp_path / "workspace"
    prompt = 'OOO start "seeds/alpha seed.yaml" --max-iterations 2'

    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
    )

    expected_argument = "seeds/alpha seed.yaml --max-iterations 2"
    expected_args = {
        "seed_path": expected_argument,
        "cwd": str(runtime_cwd),
        "summary": f"cwd={runtime_cwd} seed={expected_argument}",
        "nested": {"values": [expected_argument, str(runtime_cwd), True]},
        "static_mode": "deterministic",
    }
    expected_target = MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )

    assert type(result) is Resolved
    assert result.skill_name == "run"
    assert result.command_prefix == "ooo start"
    assert result.prompt == prompt
    assert result.skill_path == skill_md_path
    assert result.first_argument == expected_argument
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == expected_args
    assert result.target == expected_target
    assert result.dispatch_target == expected_target
    assert result.dispatch_metadata == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )
    assert result.dispatch_metadata.target == expected_target
    assert result.target.kind is DispatchTargetKind.MCP_TOOL
    assert result.outcome is ResolveOutcome.MATCH


def test_successful_dispatch_result_shape_selects_matching_skill_template(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "run",
        """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
""",
    )
    skill_md_path = _write_skill(
        skills_dir,
        "evaluate",
        """\
name: evaluate
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  cwd: "$CWD"
  label: "artifact=$1 cwd=$CWD"
  trigger_consensus: true
""",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo evaluate reports/final.md",
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    expected_args = {
        "artifact": "reports/final.md",
        "cwd": str(runtime_cwd),
        "label": f"artifact=reports/final.md cwd={runtime_cwd}",
        "trigger_consensus": True,
    }

    assert type(result) is Resolved
    assert result.skill_name == "evaluate"
    assert result.command_prefix == "ooo evaluate"
    assert result.skill_path == skill_md_path
    assert result.first_argument == "reports/final.md"
    assert result.mcp_tool == "ouroboros_evaluate"
    assert result.mcp_args == expected_args
    assert "seed_path" not in result.mcp_args
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_evaluate",
        mcp_args=expected_args,
    )
    assert result.outcome is ResolveOutcome.MATCH


def test_unknown_skill_lookup_returns_not_handled_result_shape(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo missing seed.yaml",
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert type(result) is NotHandled
    assert result.reason == "skill not found"
    assert result.category is NoMatchReason.SKILL_NOT_FOUND
    assert result.outcome is ResolveOutcome.NO_MATCH
    assert _not_handled_shape(result) == {
        "result": "not_handled",
        "outcome": ResolveOutcome.NO_MATCH.value,
        "code": NoMatchReason.SKILL_NOT_FOUND.value,
        "message": "skill not found",
        "skill_path": None,
        "target": None,
        "dispatch_metadata": None,
    }
    assert not hasattr(result, "skill_path")
    assert not hasattr(result, "target")
    assert not hasattr(result, "dispatch_metadata")


@pytest.mark.parametrize(
    ("prompt", "expected_reason", "expected_category"),
    [
        pytest.param(
            "please run seed.yaml",
            "not a skill command",
            NoMatchReason.NOT_A_SKILL_COMMAND,
            id="non-ooo-input",
        ),
        pytest.param(
            "",
            "not a skill command",
            NoMatchReason.NOT_A_SKILL_COMMAND,
            id="empty-input",
        ),
        pytest.param(
            "ooo",
            "not a skill command",
            NoMatchReason.NOT_A_SKILL_COMMAND,
            id="empty-ooo-dispatch",
        ),
        pytest.param(
            "/ouroboros:",
            "not a skill command",
            NoMatchReason.NOT_A_SKILL_COMMAND,
            id="empty-slash-dispatch",
        ),
        pytest.param(
            "ooo unsupported seed.yaml",
            "skill not found",
            NoMatchReason.SKILL_NOT_FOUND,
            id="unsupported-dispatch-input",
        ),
    ],
)
def test_no_op_dispatch_result_shapes_for_non_ooo_empty_and_unsupported_inputs(
    tmp_path: Path,
    prompt: str,
    expected_reason: str,
    expected_category: NoMatchReason,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert type(result) is NotHandled
    assert result.reason == expected_reason
    assert result.category is expected_category
    assert result.outcome is ResolveOutcome.NO_MATCH
    assert _not_handled_shape(result) == {
        "result": "not_handled",
        "outcome": ResolveOutcome.NO_MATCH.value,
        "code": expected_category.value,
        "message": expected_reason,
        "skill_path": None,
        "target": None,
        "dispatch_metadata": None,
    }
    assert not hasattr(result, "skill_path")
    assert not hasattr(result, "target")
    assert not hasattr(result, "dispatch_metadata")


@pytest.mark.parametrize(
    ("frontmatter", "expected_reason"),
    [
        pytest.param(
            """\
name: run
mcp_tool: ouroboros_execute_seed
""",
            "missing required frontmatter key: mcp_args",
            id="missing-required-mcp-args",
        ),
        pytest.param(
            """\
name: run
mcp_tool: ""
mcp_args: {}
""",
            "mcp_tool must be a non-empty string",
            id="missing-required-mcp-tool-value",
        ),
        pytest.param(
            """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args: []
""",
            "mcp_args must be a mapping with string keys and YAML-safe values",
            id="malformed-mcp-args-container",
        ),
        pytest.param(
            """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  metadata:
    1: numeric-key
""",
            "mcp_args.metadata keys must be non-empty strings",
            id="malformed-nested-mcp-args-key",
        ),
        pytest.param(
            """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  created_at: 2026-04-20
""",
            (
                "mcp_args.created_at has unsupported type date; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="malformed-mcp-args-value",
        ),
    ],
)
def test_validation_error_result_shapes_for_missing_values_and_malformed_arguments(
    tmp_path: Path,
    frontmatter: str,
    expected_reason: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(skills_dir, "run", frontmatter)

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seed.yaml",
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert type(result) is InvalidSkill
    assert result.reason == expected_reason
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.FRONTMATTER_INVALID
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert _invalid_skill_shape(result) == {
        "result": "invalid_skill",
        "outcome": ResolveOutcome.INVALID_INPUT.value,
        "code": InvalidInputReason.FRONTMATTER_INVALID.value,
        "message": expected_reason,
        "skill_path": skill_md_path.as_posix(),
        "target": None,
        "dispatch_metadata": None,
    }
    assert not hasattr(result, "target")
    assert not hasattr(result, "dispatch_metadata")


def test_template_resolution_failure_returns_invalid_skill_result_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
""",
    )

    def _raise_template_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("unsupported template token")

    monkeypatch.setattr(
        dispatch_module,
        "resolve_dispatch_templates",
        _raise_template_failure,
    )

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seed.yaml",
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert type(result) is InvalidSkill
    assert result.reason == "template resolution failed: unsupported template token"
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.TEMPLATE_RESOLUTION_ERROR
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert _invalid_skill_shape(result) == {
        "result": "invalid_skill",
        "outcome": ResolveOutcome.INVALID_INPUT.value,
        "code": InvalidInputReason.TEMPLATE_RESOLUTION_ERROR.value,
        "message": "template resolution failed: unsupported template token",
        "skill_path": skill_md_path.as_posix(),
        "target": None,
        "dispatch_metadata": None,
    }
    assert not hasattr(result, "target")
    assert not hasattr(result, "dispatch_metadata")


def test_template_render_failure_returns_invalid_skill_result_shape(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  cwd: "$CWD"
""",
    )

    class UnrenderableCwd:
        def __str__(self) -> str:
            raise RuntimeError("cwd cannot be rendered")

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seed.yaml",
            cwd=UnrenderableCwd(),  # type: ignore[arg-type]
            skills_dir=skills_dir,
        )
    )

    assert type(result) is InvalidSkill
    assert result.reason == "template resolution failed: cwd cannot be rendered"
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.TEMPLATE_RESOLUTION_ERROR
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert _invalid_skill_shape(result) == {
        "result": "invalid_skill",
        "outcome": ResolveOutcome.INVALID_INPUT.value,
        "code": InvalidInputReason.TEMPLATE_RESOLUTION_ERROR.value,
        "message": "template resolution failed: cwd cannot be rendered",
        "skill_path": skill_md_path.as_posix(),
        "target": None,
        "dispatch_metadata": None,
    }
    assert not hasattr(result, "target")
    assert not hasattr(result, "dispatch_metadata")
