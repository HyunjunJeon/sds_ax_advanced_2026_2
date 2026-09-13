from __future__ import annotations

from pathlib import Path

from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    NormalizedMCPFrontmatter,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    load_skill_frontmatter,
    normalize_mcp_frontmatter,
    resolve_skill_dispatch,
)


def _write_skill(skills_dir: Path, skill_name: str, frontmatter: str) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(f"---\n{frontmatter}---\n# {skill_name}\n", encoding="utf-8")
    return skill_md_path


def test_normalizer_accepts_typed_yaml_frontmatter_fields(tmp_path: Path) -> None:
    skill_md_path = _write_skill(
        tmp_path,
        "evaluate",
        """\
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  label: "artifact=$1"
  trigger_consensus: true
  acceptance_criteria:
    - AC1
    - AC2
  metadata:
    dry_run: false
    tags:
      - router
      - frontmatter
""",
    )

    frontmatter = load_skill_frontmatter(skill_md_path)
    normalized, error = normalize_mcp_frontmatter(frontmatter)

    assert error is None
    assert normalized == NormalizedMCPFrontmatter(
        mcp_tool="ouroboros_evaluate",
        mcp_args={
            "artifact": "$1",
            "label": "artifact=$1",
            "trigger_consensus": True,
            "acceptance_criteria": ["AC1", "AC2"],
            "metadata": {
                "dry_run": False,
                "tags": ["router", "frontmatter"],
            },
        },
    )
    assert isinstance(normalized.mcp_args["artifact"], str)
    assert normalized.mcp_args["trigger_consensus"] is True
    assert isinstance(normalized.mcp_args["acceptance_criteria"], list)
    assert normalized.mcp_args["metadata"]["dry_run"] is False


def test_router_preserves_typed_frontmatter_fields_while_resolving_templates(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "evaluate",
        """\
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  label: "cwd=$CWD artifact=$1"
  trigger_consensus: true
  acceptance_criteria:
    - "check $1"
    - "$CWD/reports"
  metadata:
    dry_run: false
    reviewers:
      - codex
      - hermes
""",
    )
    runtime_cwd = tmp_path / "workspace"

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt='ooo evaluate "reports/final output.md"',
            cwd=runtime_cwd,
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, Resolved)
    assert result.skill_path == skill_md_path
    assert result.outcome is ResolveOutcome.MATCH
    assert result.mcp_args == {
        "artifact": "reports/final output.md",
        "label": f"cwd={runtime_cwd} artifact=reports/final output.md",
        "trigger_consensus": True,
        "acceptance_criteria": [
            "check reports/final output.md",
            f"{runtime_cwd}/reports",
        ],
        "metadata": {
            "dry_run": False,
            "reviewers": ["codex", "hermes"],
        },
    }


def test_router_reports_invalid_typed_frontmatter_field_as_invalid_skill(
    tmp_path: Path,
) -> None:
    skills_dir = tmp_path / "skills"
    skill_md_path = _write_skill(
        skills_dir,
        "evaluate",
        """\
mcp_tool: ouroboros_evaluate
mcp_args:
  artifact: "$1"
  generated_at: 2026-04-20
""",
    )

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo evaluate report.md",
            cwd=tmp_path / "workspace",
            skills_dir=skills_dir,
        )
    )

    assert isinstance(result, InvalidSkill)
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.FRONTMATTER_INVALID
    assert result.outcome is ResolveOutcome.INVALID_INPUT
    assert result.reason == (
        "mcp_args.generated_at has unsupported type date; "
        "expected string, finite number, boolean, null, list, or mapping"
    )
