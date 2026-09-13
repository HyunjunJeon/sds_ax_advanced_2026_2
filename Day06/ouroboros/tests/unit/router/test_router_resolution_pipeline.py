from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    MCPDispatchTarget,
    NoMatchReason,
    NotHandled,
    ParsedOooCommand,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    ResolveResult,
    resolve_parsed_skill_dispatch,
    resolve_skill_dispatch,
)
import ouroboros.router.dispatch as dispatch_module


def _write_skill(skills_dir: Path, skill_name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter}---\n# {skill_name}\n", encoding="utf-8")
    return skill_md_path


def _write_dispatch_skill(
    skills_dir: Path,
    skill_name: str,
    *,
    mcp_tool: str,
    argument_key: str,
) -> Path:
    return _write_skill(
        skills_dir,
        skill_name,
        f"""\
name: {skill_name}
mcp_tool: {mcp_tool}
mcp_args:
  {argument_key}: "$1"
  cwd: "$CWD"
""",
    )


def _resolved(result: ResolveResult) -> Resolved:
    assert isinstance(result, Resolved)
    return result


def _resolution_snapshot(result: Resolved) -> dict[str, Any]:
    return {
        "skill_name": result.skill_name,
        "command_prefix": result.command_prefix,
        "prompt": result.prompt,
        "skill_path": result.skill_path,
        "mcp_tool": result.mcp_tool,
        "mcp_args": result.mcp_args,
        "first_argument": result.first_argument,
        "target": result.target,
        "outcome": result.outcome,
    }


def test_router_resolves_dispatchable_skill_to_runtime_neutral_mcp_target(
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
  label: "cwd=$CWD seed=$1"
  nested:
    values:
      - "$1"
      - "$CWD"
      - true
""",
    )
    runtime_cwd = tmp_path / "workspace"
    prompt = 'ooo run "seed file.yaml" --max-iterations 2'

    result = _resolved(
        resolve_skill_dispatch(
            ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
        )
    )

    assert result.skill_name == "run"
    assert result.command_prefix == "ooo run"
    assert result.prompt == prompt
    assert result.skill_path == skill_md_path
    expected_argument = "seed file.yaml --max-iterations 2"
    assert result.first_argument == expected_argument
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {
        "seed_path": expected_argument,
        "cwd": str(runtime_cwd),
        "label": f"cwd={runtime_cwd} seed={expected_argument}",
        "nested": {"values": [expected_argument, str(runtime_cwd), True]},
    }
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=result.mcp_args,
    )
    assert result.outcome is ResolveOutcome.MATCH


@pytest.mark.parametrize(
    (
        "prompt",
        "skill_name",
        "mcp_tool",
        "argument_key",
        "first_argument",
    ),
    [
        pytest.param(
            "ooo run seeds/alpha.yaml",
            "run",
            "ouroboros_execute_seed",
            "seed_path",
            "seeds/alpha.yaml",
            id="run-seed",
        ),
        pytest.param(
            "ooo evaluate reports/final.md",
            "evaluate",
            "ouroboros_evaluate",
            "artifact",
            "reports/final.md",
            id="evaluate-artifact",
        ),
        pytest.param(
            "ooo status orch_123",
            "status",
            "ouroboros_session_status",
            "session_id",
            "orch_123",
            id="status-session",
        ),
    ],
)
def test_router_resolves_canonical_ooo_dispatch_inputs_to_expected_targets(
    tmp_path: Path,
    prompt: str,
    skill_name: str,
    mcp_tool: str,
    argument_key: str,
    first_argument: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_dispatch_skill(
        skills_dir,
        skill_name,
        mcp_tool=mcp_tool,
        argument_key=argument_key,
    )
    runtime_cwd = tmp_path / "workspace"

    result = _resolved(
        resolve_skill_dispatch(
            ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
        )
    )

    expected_args = {
        argument_key: first_argument,
        "cwd": str(runtime_cwd),
    }
    assert result.skill_name == skill_name
    assert result.command_prefix == f"ooo {skill_name}"
    assert result.prompt == prompt
    assert result.skill_path == skill_md_path
    assert result.first_argument == first_argument
    assert result.mcp_tool == mcp_tool
    assert result.mcp_args == expected_args
    assert result.target == MCPDispatchTarget(
        mcp_tool=mcp_tool,
        mcp_args=expected_args,
    )
    assert result.outcome is ResolveOutcome.MATCH


def test_router_replaces_channel_seed_path_routing_with_skill_dispatch(
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
    runtime_cwd = tmp_path / "workspace"

    result = _resolved(
        resolve_skill_dispatch(
            ResolveRequest(
                prompt="ooo run seed.yaml",
                cwd=runtime_cwd,
                skills_dir=skills_dir,
            )
        )
    )

    assert result.skill_name == "run"
    assert result.command_prefix == "ooo run"
    assert result.skill_path == skill_md_path
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {
        "seed_path": "seed.yaml",
        "cwd": str(runtime_cwd),
    }
    assert "seed_content" not in result.mcp_args
    assert "channel_id" not in result.mcp_args
    assert "guild_id" not in result.mcp_args
    assert "user_id" not in result.mcp_args


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param("ooo run seed.yaml", "ooo run", id="canonical-directory-name"),
        pytest.param("OoO Start seed.yaml", "ooo start", id="frontmatter-alias"),
        pytest.param("/OUROBOROS:Launch seed.yaml", "/ouroboros:launch", id="slash-alias"),
    ],
)
def test_router_normalizes_aliases_to_canonical_skill_target(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
aliases:
  - start
command_aliases:
  - /ouroboros:launch
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
""",
    )

    result = _resolved(
        resolve_skill_dispatch(ResolveRequest(prompt=prompt, cwd=tmp_path, skills_dir=skills_dir))
    )

    assert result.skill_name == "run"
    assert result.command_prefix == expected_prefix
    assert result.skill_path == skill_md_path
    assert result.first_argument == "seed.yaml"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args={"seed_path": "seed.yaml", "cwd": str(tmp_path)},
    )


def test_router_loads_frontmatter_from_resolved_target_path_for_noncanonical_directory(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "Quick-Run",
        """\
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
""",
    )

    result = _resolved(
        resolve_skill_dispatch(
            ResolveRequest(
                prompt="ooo quick-run seed.yaml",
                cwd=tmp_path,
                skills_dir=skills_dir,
            )
        )
    )

    assert result.skill_name == "quick-run"
    assert result.skill_path == skill_md_path
    assert result.mcp_tool == "ouroboros_execute_seed"
    assert result.mcp_args == {"seed_path": "seed.yaml"}


@pytest.mark.parametrize(
    ("prompt", "expected_prefix"),
    [
        pytest.param(
            'ooo run "seed file.yaml"',
            "ooo run",
            id="directory-name",
        ),
        pytest.param(
            ' \tOOO   Execute\t"seed file.yaml"',
            "ooo execute",
            id="frontmatter-name-with-casing-and-whitespace",
        ),
        pytest.param(
            'OoO Quick-Run "seed file.yaml"',
            "ooo quick-run",
            id="single-alias-field",
        ),
        pytest.param(
            'ooo start "seed file.yaml"',
            "ooo start",
            id="aliases-sequence",
        ),
        pytest.param(
            ' \t/OUROBOROS:Launch   "seed file.yaml"',
            "/ouroboros:launch",
            id="command-alias-slash-prefix",
        ),
        pytest.param(
            'OOO Ship-It "seed file.yaml"',
            "ooo ship-it",
            id="skill-alias-prefixed-value",
        ),
        pytest.param(
            '/ouroboros:go "seed file.yaml"',
            "/ouroboros:go",
            id="commands-sequence",
        ),
    ],
)
def test_router_resolves_equivalent_alias_and_normalized_inputs_to_same_dispatch(
    tmp_path: Path,
    prompt: str,
    expected_prefix: str,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: execute
alias: Quick-Run
aliases:
  - start
command_aliases: "ooo begin, /ouroboros:launch"
skill_aliases:
  - OOO Ship-It
commands:
  - /ouroboros:go
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
  label: "cwd=$CWD seed=$1"
""",
    )
    runtime_cwd = tmp_path / "workspace"

    result = _resolved(
        resolve_skill_dispatch(
            ResolveRequest(prompt=prompt, cwd=runtime_cwd, skills_dir=skills_dir)
        )
    )

    expected_args = {
        "seed_path": "seed file.yaml",
        "cwd": str(runtime_cwd),
        "label": f"cwd={runtime_cwd} seed=seed file.yaml",
    }
    assert result.skill_name == "run"
    assert result.command_prefix == expected_prefix
    assert result.skill_path == skill_md_path
    assert result.first_argument == "seed file.yaml"
    assert result.target == MCPDispatchTarget(
        mcp_tool="ouroboros_execute_seed",
        mcp_args=expected_args,
    )


@pytest.mark.parametrize(
    ("prompt", "expected_reason", "expected_category"),
    [
        pytest.param(
            "please run seed.yaml",
            "not a skill command",
            NoMatchReason.NOT_A_SKILL_COMMAND,
            id="non-command-prompt",
        ),
        pytest.param(
            "ooo missing seed.yaml",
            "skill not found",
            NoMatchReason.SKILL_NOT_FOUND,
            id="unknown-skill",
        ),
    ],
)
def test_router_returns_no_match_outcomes_without_claiming_prompt(
    tmp_path: Path,
    prompt: str,
    expected_reason: str,
    expected_category: NoMatchReason,
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    result = resolve_skill_dispatch(
        ResolveRequest(prompt=prompt, cwd=tmp_path, skills_dir=skills_dir)
    )

    assert isinstance(result, NotHandled)
    assert result.reason == expected_reason
    assert result.category is expected_category
    assert result.outcome is ResolveOutcome.NO_MATCH


@pytest.mark.parametrize(
    "prompt",
    [
        pytest.param("", id="empty"),
        pytest.param("   \t", id="whitespace-only"),
        pytest.param("please run seed.yaml", id="plain-language"),
        pytest.param("run ooo seed.yaml", id="ooo-not-at-start"),
        pytest.param("ooo", id="missing-skill-name"),
        pytest.param("ooo-run seed.yaml", id="hyphenated-prefix"),
    ],
)
def test_router_returns_documented_no_dispatch_result_for_non_ooo_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prompt: str,
) -> None:
    def _raise_if_downstream_resolution_is_invoked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("non-ooo input must not invoke downstream dispatch resolution")

    monkeypatch.setattr(
        dispatch_module,
        "resolve_parsed_skill_dispatch",
        _raise_if_downstream_resolution_is_invoked,
    )

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=prompt,
            cwd=tmp_path / "workspace",
            skills_dir=tmp_path / "skills-that-must-not-be-read",
        )
    )

    assert isinstance(result, NotHandled)
    assert result.reason == "not a skill command"
    assert result.category is NoMatchReason.NOT_A_SKILL_COMMAND
    assert result.outcome is ResolveOutcome.NO_MATCH


def test_router_does_not_autoroute_seed_like_yaml_without_skill_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise_if_downstream_resolution_is_invoked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("seed-like non-command input must not reach skill resolution")

    monkeypatch.setattr(
        dispatch_module,
        "resolve_parsed_skill_dispatch",
        _raise_if_downstream_resolution_is_invoked,
    )
    seed_like_prompt = "goal: Demo\nacceptance_criteria:\n- one\nconstraints:\n- two\n"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt=seed_like_prompt,
            cwd=tmp_path / "workspace",
            skills_dir=tmp_path / "skills-that-must-not-be-read",
        )
    )

    assert isinstance(result, NotHandled)
    assert result.reason == "not a skill command"
    assert result.category is NoMatchReason.NOT_A_SKILL_COMMAND
    assert result.outcome is ResolveOutcome.NO_MATCH


def test_router_reports_invalid_frontmatter_as_invalid_input(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "run",
        """\
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
  created_at: 2026-04-20
""",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo run seed.yaml", cwd=tmp_path, skills_dir=skills_dir)
    )

    assert isinstance(result, InvalidSkill)
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.FRONTMATTER_INVALID
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert result.reason == (
        "mcp_args.created_at has unsupported type date; "
        "expected string, finite number, boolean, null, list, or mapping"
    )


def test_router_reports_alias_matched_invalid_frontmatter_with_skill_path(
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
  - "$1"
""",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(prompt="ooo start seed.yaml", cwd=tmp_path, skills_dir=skills_dir)
    )

    assert isinstance(result, InvalidSkill)
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.FRONTMATTER_INVALID
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert result.reason == "mcp_args must be a mapping with string keys and YAML-safe values"


def test_router_reports_malformed_parsed_command_as_invalid_input(tmp_path: Path) -> None:
    parsed = ParsedOooCommand(
        skill_name="run",
        command_prefix="ooo execute",
        remainder="seed.yaml",
    )

    result = resolve_parsed_skill_dispatch(parsed, cwd=tmp_path, skills_dir=tmp_path)

    assert isinstance(result, InvalidSkill)
    assert result.skill_path == Path("run")
    assert result.category is InvalidInputReason.MALFORMED_PARSED_COMMAND
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert result.reason == "malformed parsed command: command_prefix must match skill_name"


def test_router_repeated_calls_are_deterministic_and_do_not_share_result_payloads(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "evaluate",
        """\
name: evaluate
aliases:
  - eval
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  cwd: "$CWD"
  payload:
    files:
      - "$1"
      - "$CWD/$1"
""",
    )
    request = ResolveRequest(
        prompt='ooo eval "reports/final output.md"',
        cwd=tmp_path / "workspace",
        skills_dir=skills_dir,
    )

    first = _resolved(resolve_skill_dispatch(request))
    second = _resolved(resolve_skill_dispatch(request))

    assert first is not second
    assert first.mcp_args is not second.mcp_args
    assert _resolution_snapshot(first) == _resolution_snapshot(second)

    first.mcp_args["artifact"] = "mutated.md"
    third = _resolved(resolve_skill_dispatch(request))

    assert third.mcp_args["artifact"] == "reports/final output.md"
    assert _resolution_snapshot(second) == _resolution_snapshot(third)
