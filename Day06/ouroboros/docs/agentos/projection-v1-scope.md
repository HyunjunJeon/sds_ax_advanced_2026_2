# AgentOS Projection v1 Scope Boundary

This document pins the narrow v1 scope for #946 under the #961 AgentOS SSOT.
It keeps the projection vocabulary useful without turning the first projection
slice into a catch-all AgentOS substrate.

## Source-of-truth invariant

The EventStore / journal remains authoritative. Run, stage, step, artifact, and
verdict records are rebuildable read-models over persisted events and existing
state. Projection records must not become a second state model, execution
control plane, plugin schema, or evidence-gate authority.

## Current implementation baseline

This boundary document is a scope map for the already-started #946 projection
stack, not a new roadmap surface. The current baseline includes the v1 record
schema, an EventStore projection builder, and a read-only MCP projection query
from the earlier #946 slices (#980 / #983 / #990). Follow-up PRs should use this
document to decide whether new work is additive projection coverage, a derived
consumer, or out-of-scope authority that belongs to another canonical issue.

## Existing terms mapped to projection v1

| Existing term / surface | Projection v1 term | Boundary |
| --- | --- | --- |
| Orchestrator session | `RunRecord` query anchor when no execution ID is available | A session can span retries or multiple executions; session-only projection must fail closed when ambiguous. |
| Execution aggregate | Preferred `RunRecord` source anchor | Execution-scoped events define the durable run slice for normal AgentOS projections. |
| Seed / user goal | `RunRecord.seed_id` / `RunRecord.goal` | Derived from persisted event metadata when present; caller-provided labels must expose provenance. |
| Harness phase | `StageRecord` | v1 emits the default `execute` stage; richer phase detection is additive follow-up work. |
| Tool call / shell command | `StepRecord(kind=tool_call|shell_command)` | Paired from `tool.call.started` / `tool.call.returned` by `call_id`. |
| Model call | `StepRecord(kind=model_call)` | Paired from `llm.call.requested` / `llm.call.returned` by `call_id`. |
| AC identity | `StepRecord.ac_id` / `VerdictRecord.ac_id` | AC identity is metadata on projected work, not a replacement for #956 Workflow IR nodes. |
| Typed AC evidence / TraceGuard facts | Future `ArtifactRecord(kind=evidence)` and `VerdictRecord` projection inputs | #830/#978 own the evidence schema and verifier semantics; #946 only reads and projects persisted evidence. |
| Produced file / patch / log excerpt / capsule | Future `ArtifactRecord` | Artifacts attach to steps without plugin-specific paths; v1 schema exists before broad event-family mapping. |
| AC or run judgment | Future `VerdictRecord` | Verdict projection must link evidence event IDs/artifact IDs and must not independently decide acceptance. |
| Runtime handle / resume token | Deferred projection metadata | Runtime authority remains outside #946 v1; later read views may reference handles. |
| Plugin permission/audit event | Future derived projection consumer | #939 owns plugin contract/audit substrate; #946 offers common read-model targets. |
| Workflow node graph | #956 Workflow IR, optionally projected into stages/steps later | Workflow IR remains the planning contract; projection remains an observed read model. |

## Included in v1

| Surface | v1 decision |
| --- | --- |
| `RunRecord` / `StageRecord` / `StepRecord` / `ArtifactRecord` / `VerdictRecord` | Public schema-versioned Pydantic records. |
| Step source evidence | Every projected `StepRecord` links source event IDs or marks itself `legacy_inferred=True`. |
| Projection builder | Read-only builder over existing EventStore events. |
| Machine-readable query | MCP projection query output derived on demand from EventStore rows. |
| Deterministic identity | Query/build surfaces should derive identity from persisted execution/session/event anchors where possible. |
| Minimal fixtures | Deterministic tests over tool/LLM event pairs and source-event links. |

## Explicitly deferred

| Deferred surface | Canonical home / note |
| --- | --- |
| Typed AC evidence schema | #830 / #978 evidence gate spine; #946 only projects/read-models it. |
| Workflow node graph and lifecycle | #956 Workflow IR. |
| HITL WAIT/RESUME authority contract | #960. |
| Plugin permission/audit SDK | #939. |
| StepSnapshot, session health, runtime handles, resume tokens | Later projection views; do not block v1 schema/builder/query completion. |
| OpenTelemetry/exporter sinks | Derived consumers only; disabled/out of scope for v1. |
| Context pack provider and checkpoint condensation | Later consumers of projection anchors; not a second context state model. |
| Full replay/rerun semantics | Control-plane / ledger work; projection remains read-only. |
| Projection caching | Deferred until a roadmap PR explicitly owns cache invalidation and migration semantics. |

## Review checklist for follow-up PRs

- Does the change preserve EventStore/journal as the source of truth?
- Can the same source event slice rebuild the same projection facts without writing new rows?
- Is the change additive to the v1 schema, or does it require an explicit schema-version bump and migration story?
- Is new evidence vocabulary being added here accidentally instead of under the evidence-gate spine?
- Is a folded AgentOS idea being implemented now, or should it remain a later projection view?
- Does the PR keep #946 as a read-model surface rather than a workflow, plugin, HITL, or verifier authority layer?
