from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    ResolveOutcome,
    ResolveRequest,
    resolve_skill_dispatch,
)


def _write_skill_with_mcp_args(tmp_path: Path, mcp_args_yaml: str) -> Path:
    skill_dir = tmp_path / "run"
    skill_dir.mkdir()
    skill_md_path = skill_dir / "SKILL.md"
    skill_md_path.write_text(
        f"""---
name: run
mcp_tool: ouroboros_execute_seed
mcp_args:
{mcp_args_yaml}---
# Run
""",
        encoding="utf-8",
    )
    return skill_md_path


@pytest.mark.parametrize(
    ("mcp_args_yaml", "expected_error"),
    [
        pytest.param(
            "  created_at: 2026-04-20\n",
            (
                "mcp_args.created_at has unsupported type date; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="yaml-implicit-date-scalar",
        ),
        pytest.param(
            "  payload: !!binary |\n    YmluYXJ5\n",
            (
                "mcp_args.payload has unsupported type bytes; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="yaml-binary-scalar",
        ),
        pytest.param(
            "  checks:\n    - seed.yaml\n    - 2026-04-20\n",
            (
                "mcp_args.checks[1] has unsupported type date; "
                "expected string, finite number, boolean, null, list, or mapping"
            ),
            id="nested-list-invalid-type",
        ),
        pytest.param(
            "  metadata:\n    score: .inf\n",
            "mcp_args.metadata.score must be a finite number",
            id="nested-non-finite-float",
        ),
    ],
)
def test_router_reports_invalid_yaml_loaded_mcp_arg_types_as_invalid_skill(
    tmp_path: Path,
    mcp_args_yaml: str,
    expected_error: str,
) -> None:
    skill_md_path = _write_skill_with_mcp_args(tmp_path, mcp_args_yaml)

    result = resolve_skill_dispatch(
        ResolveRequest(
            prompt="ooo run seed.yaml",
            cwd=tmp_path / "workspace",
            skills_dir=tmp_path,
        )
    )

    assert isinstance(result, InvalidSkill)
    assert result.reason == expected_error
    assert result.skill_path == skill_md_path
    assert result.category is InvalidInputReason.FRONTMATTER_INVALID
    assert result.outcome is ResolveOutcome.INVALID_INPUT
