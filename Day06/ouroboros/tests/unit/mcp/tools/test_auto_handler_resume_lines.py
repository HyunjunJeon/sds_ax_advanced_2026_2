"""Resume-hint rendering for the MCP ``ouroboros_auto`` surface (#688).

These tests assert that :func:`ouroboros.mcp.tools.auto_handler._format_result`
emits the same capability-driven hint substrings as the CLI, but without Rich
markup since MCP renders plain text. They also pin the metadata contract so
that ``meta.resume_command`` only appears when the rendered text invites the
client to resume.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoResumeCapability
from ouroboros.mcp.tools.auto_handler import AutoHandler, _format_result


def _result(
    capability: AutoResumeCapability | str,
    *,
    status: str = "blocked",
    session_id: str = "auto_mcp",
) -> AutoPipelineResult:
    cap = (
        capability
        if isinstance(capability, AutoResumeCapability)
        else AutoResumeCapability(capability)
    )
    return AutoPipelineResult(
        status=status,
        auto_session_id=session_id,
        phase=status,
        resume_capability=cap,
    )


def test_format_result_resume_capability_resume_emits_resume_line() -> None:
    output = _format_result(_result("resume", status="complete"))

    assert "Resume: ooo auto --resume auto_mcp" in output
    assert "Resume (partial)" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


def test_format_result_resume_capability_partial_emits_partial_resume_line() -> None:
    output = _format_result(_result("partial_resume"))

    assert "Resume (partial): ooo auto --resume auto_mcp" in output
    assert "some progress preserved but the exact pick-up point may be approximate" in output


def test_format_result_resume_capability_retry_emits_retry_line() -> None:
    output = _format_result(_result("retry"))

    assert "Retry: ooo auto --resume auto_mcp" in output
    assert "no prior session context" in output
    assert "re-runs the failed step from scratch" in output


def test_format_result_resume_capability_none_emits_no_resume_line() -> None:
    """``_format_result`` has no ``state.goal`` so NONE prints nothing."""
    output = _format_result(_result("none", status="complete"))

    assert "Resume:" not in output
    assert "Resume (partial)" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


# --- meta gating (#688 bot follow-up) ---------------------------------------


class _StubAutoHandler(AutoHandler):
    """Test double that bypasses pipeline construction."""

    def __init__(self, fixture: AutoPipelineResult) -> None:
        super().__init__()
        self._fixture = fixture

    async def _run(self, arguments: dict[str, Any]) -> AutoPipelineResult:  # type: ignore[override]
        return self._fixture


def _invoke(handler: AutoHandler) -> dict[str, Any]:
    result = asyncio.run(handler.handle({}))
    assert result.is_ok, result.error
    return dict(result.value.meta)


def test_handle_meta_includes_resume_capability_field() -> None:
    handler = _StubAutoHandler(_result("retry"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "retry"


def test_handle_meta_resume_command_gated_on_capability_resume() -> None:
    handler = _StubAutoHandler(_result("resume"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "resume"
    assert meta["resume_command"] == "ooo auto --resume auto_mcp"


def test_handle_meta_resume_command_omitted_for_capability_none() -> None:
    """Bot blocking finding (#724): NONE-capability sessions must not advertise
    a resume_command in MCP metadata, otherwise clients will route users into
    a guaranteed-failing ``--resume`` call."""
    handler = _StubAutoHandler(_result("none", status="complete"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "none"
    assert "resume_command" not in meta


def test_handle_meta_resume_command_present_for_partial_resume() -> None:
    handler = _StubAutoHandler(_result("partial_resume"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "partial_resume"
    assert meta["resume_command"] == "ooo auto --resume auto_mcp"


# --- Ralph handoff meta surface (#773 review-4) -----------------------------


def _ralph_result(
    *,
    job_id: str | None = "job_ralph_xyz",
    lineage_id: str | None = "ralph-seed_test_001-auto_abcd",
    dispatch_mode: str | None = "job",
) -> AutoPipelineResult:
    return AutoPipelineResult(
        status="complete",
        auto_session_id="auto_ralph",
        phase="complete",
        resume_capability=AutoResumeCapability.NONE,
        ralph_job_id=job_id,
        ralph_lineage_id=lineage_id,
        ralph_dispatch_mode=dispatch_mode,
    )


def test_handle_meta_includes_ralph_handoff_handles_when_present() -> None:
    """Bot review-4 blocking finding: MCP clients must be able to monitor and
    correlate the Ralph loop they just dispatched. Without ``ralph_job_id`` /
    ``ralph_lineage_id`` / ``ralph_dispatch_mode`` on the meta surface, plugin
    delegations and mid-loop checkpoints expose no structured handle and
    clients are forced to read local state files out-of-band.
    """
    handler = _StubAutoHandler(_ralph_result())
    meta = _invoke(handler)
    assert meta["ralph_job_id"] == "job_ralph_xyz"
    assert meta["ralph_lineage_id"] == "ralph-seed_test_001-auto_abcd"
    assert meta["ralph_dispatch_mode"] == "job"


def test_handle_meta_includes_plugin_dispatch_mode() -> None:
    """Plugin-mode delegations have ``job_id=None`` but still need a tracking
    surface. ``lineage_id`` and ``dispatch_mode`` carry the correlation."""
    handler = _StubAutoHandler(_ralph_result(job_id=None, dispatch_mode="plugin"))
    meta = _invoke(handler)
    assert "ralph_job_id" not in meta
    assert meta["ralph_lineage_id"] == "ralph-seed_test_001-auto_abcd"
    assert meta["ralph_dispatch_mode"] == "plugin"


def test_handle_meta_omits_ralph_handles_for_legacy_complete_product_off() -> None:
    """``complete_product=False`` runs must keep the meta shape byte-identical
    to pre-#773 — none of the three Ralph fields appear when null."""
    handler = _StubAutoHandler(_ralph_result(job_id=None, lineage_id=None, dispatch_mode=None))
    meta = _invoke(handler)
    assert "ralph_job_id" not in meta
    assert "ralph_lineage_id" not in meta
    assert "ralph_dispatch_mode" not in meta


def test_format_result_renders_ralph_handoff_block_when_present() -> None:
    """Human-readable text mirrors the structured meta so operators tailing
    the MCP response without a JSON parser still see the Ralph handles."""
    output = _format_result(_ralph_result(dispatch_mode="plugin", job_id=None))

    assert "Ralph handoff:" in output
    assert "dispatch_mode: plugin" in output
    assert "lineage_id: ralph-seed_test_001-auto_abcd" in output
    assert "job_id:" not in output  # plugin mode has no job


def test_format_result_omits_ralph_block_when_no_handoff() -> None:
    output = _format_result(_ralph_result(job_id=None, lineage_id=None, dispatch_mode=None))

    assert "Ralph handoff:" not in output
