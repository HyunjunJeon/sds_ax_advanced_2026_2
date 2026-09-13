"""Deterministic offline fixtures for Workflow IR conformance tests.

These helpers are intentionally local-only: they create Workflow IR specs and
lifecycle rows without network calls, model providers, plugin execution, or
runtime dispatch. They give #956 follow-up tests reusable benchmark-shaped
inputs without turning conformance into a product surface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    NodeKind,
    NodeOwner,
    SourceKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from ouroboros.orchestrator.workflow_lifecycle import (
    WorkflowLifecycleEvent,
    WorkflowLifecycleEventType,
)

START = datetime(2026, 5, 15, tzinfo=UTC)


def offline_conformance_spec() -> WorkflowSpec:
    """Return a small task -> verifier -> terminal graph."""

    return WorkflowSpec(
        spec_id="wfspec_offline_conformance",
        source=SourceKind.SYNTHETIC,
        nodes=(
            WorkflowNode(
                node_id="task",
                kind=NodeKind.TASK,
                owner=NodeOwner.AGENT,
                input_schema_ref="schema://input.agent.v1",
                evidence_schema_ref="schema://evidence.agent.v1",
            ),
            WorkflowNode(
                node_id="verify",
                kind=NodeKind.TASK,
                owner=NodeOwner.VERIFIER,
                input_schema_ref="schema://input.verifier.v1",
                evidence_schema_ref="schema://evidence.verifier.v1",
            ),
            WorkflowNode(
                node_id="done",
                kind=NodeKind.TERMINAL,
                owner=NodeOwner.HARNESS,
            ),
        ),
        edges=(
            WorkflowEdge(edge_id="edge_task_verify", source="task", target="verify"),
            WorkflowEdge(
                edge_id="edge_verify_done",
                source="verify",
                target="done",
                kind=EdgeKind.TERMINAL,
            ),
        ),
        metadata={"fixture": "offline_conformance"},
    )


def retry_success_history(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    """Return a legal history where a failed task retries and completes."""

    return (
        _event(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _event(spec, WorkflowLifecycleEventType.NODE_SCHEDULED, 1, node_id="task"),
        _event(spec, WorkflowLifecycleEventType.NODE_STARTED, 2, node_id="task", attempt=1),
        _event(spec, WorkflowLifecycleEventType.NODE_FAILED, 3, node_id="task", attempt=1),
        _event(spec, WorkflowLifecycleEventType.NODE_RETRIED, 4, node_id="task", attempt=2),
        _event(spec, WorkflowLifecycleEventType.NODE_STARTED, 5, node_id="task", attempt=2),
        _event(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 6, node_id="task", attempt=2),
        _event(spec, WorkflowLifecycleEventType.EDGE_TRAVERSED, 7, edge_id="edge_task_verify"),
        _event(spec, WorkflowLifecycleEventType.NODE_STARTED, 8, node_id="verify"),
        _event(spec, WorkflowLifecycleEventType.NODE_COMPLETED, 9, node_id="verify"),
        _event(spec, WorkflowLifecycleEventType.EDGE_TRAVERSED, 10, edge_id="edge_verify_done"),
        _event(spec, WorkflowLifecycleEventType.RUN_COMPLETED, 11),
    )


def invalid_post_terminal_history(spec: WorkflowSpec) -> tuple[WorkflowLifecycleEvent, ...]:
    """Return a history that appends work after terminal run completion."""

    return (
        _event(spec, WorkflowLifecycleEventType.RUN_CREATED, 0),
        _event(spec, WorkflowLifecycleEventType.RUN_COMPLETED, 1),
        _event(spec, WorkflowLifecycleEventType.NODE_STARTED, 2, node_id="task"),
    )


def _event(
    spec: WorkflowSpec,
    event_type: WorkflowLifecycleEventType,
    offset_seconds: int,
    *,
    node_id: str | None = None,
    edge_id: str | None = None,
    attempt: int | None = None,
) -> WorkflowLifecycleEvent:
    reason_code = None
    if event_type in {
        WorkflowLifecycleEventType.NODE_FAILED,
        WorkflowLifecycleEventType.NODE_RETRIED,
        WorkflowLifecycleEventType.RUN_FAILED,
        WorkflowLifecycleEventType.RUN_CANCELLED,
    }:
        reason_code = "offline_fixture"
    return WorkflowLifecycleEvent(
        event_type=event_type,
        workflow_id=spec.spec_id,
        node_id=node_id,
        edge_id=edge_id,
        attempt=attempt,
        reason_code=reason_code,
        timestamp=START + timedelta(seconds=offset_seconds),
    )


__all__ = [
    "invalid_post_terminal_history",
    "offline_conformance_spec",
    "retry_success_history",
]
