# Research: evaluating the unlazy depth-tree method for Gajae-Code

- **Issue:** #4844
- **Source request:** Discord playground-ko message 1540980632572788817
- **Verdict receipt:** `f76e2db6-fbf3-4e74-a17e-a9b2f2fa95b8` (autoresearch ledger, this session)
- **Scope:** research only. No product code, dependency, package manifest, or benchmark binary was changed. The research evidence manifest is part of this docs bundle; nothing from the external repository was executed or copied into product code. Verbatim upstream snapshots are retained for review with attribution under the upstream MIT license, whose text is pinned in `snapshots/unlazy-LICENSE.txt.snapshot`, and whose local integrity is covered by `MANIFEST.sha256`.
- **External project state when inspected:** `Leonxlnx/unlazy`, pinned to main commit `754d9a68109e39b836cc72a39fb9a823f9d6b613` and v1 commit `baf39ef9b6e71077fa6056bcf8715e09fe6d7462`, both inspected read-only via the GitHub API. The upstream `LICENSE` blob is `48a2f6640b81ff8eca9ce7f6a96337692713ef5b`; its text is retained in the license snapshot.

## Executive summary

**Reject the unlazy depth-tree method. Propose two narrow ideas for separate product decisions and record one low-priority future consideration.**

The Discord request asks about "the depth-tree method" that "splits a task N layers deep and gives every leaf the full time budget of the whole task, so effort multiplies with depth." That claim is the **v1 (2026-08-09) version of unlazy**, and the project's own current documentation **explicitly retracts it**:

> "Do not treat depth as an arithmetic promise about effort or tokens. The original v1 method claimed that each binary split multiplied effort. A small maintainer-run comparison later suggested that agents treated depth as a thoroughness cue rather than following that arithmetic. The repository does not contain the raw artifacts needed to reproduce those historical figures, so treat them as design history, not benchmark evidence."
> — `references/method.md` (v2, current `main`) — see `snapshots/unlazy-method.md.snapshot:5`

The GitHub repository **description** still advertises the retracted arithmetic ("…so effort multiplies with depth"), which is presumably what the Discord message saw. The `v1` branch README goes further: "`tree 3` is 4 units of work, `tree 5` is 16, `tree 7` is 64… Effort multiplies with depth. It never divides." (`snapshots/unlazy-README-v1.md.snapshot:9,111`). Both the multiplication claim and the "every leaf gets the full budget T" rule are gone from v2.

What v2 actually is: a **completion-discipline system** — acceptance-gate ledgers written before work, runnable checks with exit-code+marker evidence, parent re-verification, branch integration gates, ownership leases, and an optional Claude Code Stop hook that blocks stop while gates are unmet, with a bounded no-progress release. In GJC terms, this is not a foreign paradigm; it is a peer implementation of machinery GJC already has. Comparing the two:

| unlazy v2 concept | Gajae-Code equivalent today | Delta |
|---|---|---|
| Depth Tree decomposition (layer 1 = task; leaves = coherent deliverables; contracts before fan-out) | Ultragoal `create-goals` brief→stories, ralplan consensus planning, per-slice coordination contracts | overlap — GJC adds planner/architect/critic consensus to unlazy's one-shot contract checklist |
| Gates-before-work (`GATES.md` with `CHECK:`/`EXPECT:`) | Ultragoal quality gates (`--quality-gate-json`), `gjc ultragoal quality-gate validate` | none in concept; GJC gates verify after work, unlazy gates are authored *before* work |
| Leaf/branch/root completion hierarchy (leaf self-check → parent reverify → branch integration → root remeasure) | Ultragoal boundary cohort: cleaner→architect→QA lanes on a frozen `sourceHash`, terminal critic gate, validation batches | overlap — GJC adds frozen-source and joined-lane enforcement beyond the retained unlazy hierarchy |
| Runnable-gate success contract (exit 0 **and** `EXPECT:` marker) | quality-gate surface evidence (cli-replay invariants, live-surface artifacts) | similar intent; GJC's is surface-typed, unlazy's is one marker contract |
| Ownership leases (`OWNS:` claim/release) | per-slice coordination contracts (target files, conflict-escalation rule) + `task` isolation worktrees | none — GJC additionally has real worktree isolation, unlazy leases are explicitly "coordination, not isolation" |
| Stop hook blocking stop while gates unmet | native GJC Stop hook (`hooks/skill-state.ts`) blocking on active workflow state, ultragoal durable completion, stale mode-state, uncrystallized deep-interview | **one real delta:** unlazy's block is bounded (6 consecutive no-progress blocks → release); GJC's block path has no equivalent no-progress release |
| Bounded ceilings (ralplan `maxIterations=5`, ultragoal `nudgeBudget=10`, critic ceiling 5, review-blocker cap 3) | GJC has these per-workflow | GJC's ceilings bound *work*, not *stop attempts* |
| Abandonment (`ABANDON:` with non-empty reason, surfaced in report) | ultragoal `record-review-blockers` / `classify-blocker` + terminal critic | partial — GJC keeps review blockers active and uses `classify-blocker` for human-only pause decisions rather than an identical abandonment handoff |
| "Final report audit: re-measure every number before reporting" | autoresearch verdict contract (status/evidence/caveats/evaluator) + ultragoal receipts | none in concept |

So the honest answer to "compare unlazy with Gajae-Code and identify reusable ideas" is: **the method's headline claim is dead upstream, and many v2 mechanisms overlap with what GJC's four workflow skills already enforce — with one mechanical safety idea and a few gate-authoring rules worth considering.**

## Findings

### F1. The time-budget multiplication claim is retracted by its own author (P0 — dispositive)

- v1 (`SKILL-v1.md:24`, `README-v1.md:111`): "every leaf gets the FULL budget T… Depth therefore multiplies total effort by 2 to the power of N minus 1. That multiplication is the entire point of the method."
- v2 (`references/method.md:5`): "Do not treat depth as an arithmetic promise about effort or tokens… treat them as design history, not benchmark evidence."
- `references/token-economy.md:39`: "Earlier unlazy documentation gave exact token and effort ratios from a six-run exploratory comparison. The raw prompts, traces, outputs, and scoring records are not present in this repository, so those numbers are not reproducible here. Do not use them as product guarantees."
- `research/validation-protocol.md:81`: the deterministic test suite "validate[s] implementation behavior; they do not validate broad claims about model psychology or task productivity."
- The current GitHub repo **description** nonetheless still says "so effort multiplies with depth" (`snapshots/unlazy-repo-description.txt.snapshot`). The Discord request cites the retracted framing.

Independent grounds for rejection, beyond the upstream retraction:

1. **Arithmetic cannot compel effort.** A prompt rule "every leaf gets the full T" does not create compute. If a model completes a leaf in 0.2T, nothing forces it to spend the remaining 0.8T on non-work; the observed v1-era behavior (per upstream's own comparison) was that depth acts as a *thoroughness cue*, i.e., an ordinary prompt-level effect wearing arithmetic clothing.
2. **It optimizes the wrong objective.** Even if it worked, multiplying effort by 2^N−1 multiplies cost identically. GJC's engineering principles ("avoid speculative work", "smallest version that works end to end") and the overthinking literature unlazy itself cites (`arXiv 2604.10739`, `2508.13141`) both point the other way: effort should be risk-proportional, which is exactly what GJC's `docs/workflow-recovery-and-risk-proportional-validation.md` and ultragoal's boundary-vs-deferred gate split already implement.
3. **GJC's product surface is deliberately small.** AGENTS.md fixes the public workflow surface at exactly four default skills and four role agents. An "unlazy" fifth default skill is not a research question; it violates the surface contract.

### F2. Stop/verification mechanisms and runaway-work risk (the actual engineering content)

unlazy v2's enforcement stack:

1. **Gates before work.** `GATES.md` authored from a template before implementation; one observable outcome per gate; runnable gates carry indented `CHECK:` (shell) and `EXPECT:` (success-only marker); manual gates allowed only when no command can decide the outcome. Parser rejects zero-gate ledgers, duplicate ids, incomplete runnable gates, abandonment without reason.
2. **Two-step check execution.** `--status` parses without executing. A normal run on an unapproved oracle prints the resolved command/cwd/shell/`PATH` and leaves it unexecuted. `--approve` records consent, keyed to a content hash over the *exact* `CHECK`/`EXPECT`/resolved CWD/resolved shell/timeout/output+regex limits/platform/full inherited `PATH` (`gate-check.mjs:312-394`). Approval storage must live outside the repository root (fail-closed guard at `gate-check.mjs:337-338`). This is a genuinely careful design for inherited-ledger hostile-repo scenarios, and their `SECURITY.md` is explicit that approval "is consent, not a sandbox."
3. **Success = exit 0 AND marker match**, with regex expectations executed in a Worker with a 250 ms timeout (`gate-check.mjs:406-423`) to bound catastrophic-backtracking checks. Evidence records resolved shell, cwd, exit status, a `PATH` fingerprint, and decisive (not full-log) output.
4. **Leaf → branch → root verification hierarchy.** Leaf self-check is explicitly "self-certification"; parent must `--reverify` (re-execute, never trust prior evidence — "old evidence is not re-execution"); branch gates prove integration; root remeasures final claims.
5. **Stop hook with bounded release.** While the resolved pipeline has unmet gates, the hook returns Claude Code's `decision: "block"`. The anti-trap guard: session-keyed state, and after **6 consecutive blocks without ledger progress** (content hash of outstanding items unchanged) the hook *releases* with an explanatory message (`stop-hook.mjs:11,93-131`). This bounds the runaway case where an agent cannot make progress and the block loop would otherwise burn unbounded continuation budget.

**Runaway-work comparison.** Both projects treat "agent stops early" and "agent never stops" as dual failure modes. unlazy bounds stop-blocks by counting no-progress blocks; GJC bounds *work* by per-workflow ceilings (ralplan iteration cap 5 with `PLANNING-STUCK` terminal; ultragoal nudge budget 10; terminal-critic run-level ceiling 5; review-blocker recursion cap 3) and gates stop itself through `skill-state.ts` (active workflow state, ultragoal durable completion, stale mode-state coherence, uncrystallized deep-interview). What GJC does **not** have is the third leg: a bounded release for its own stop-block when the blocked agent makes no progress. Today, a wedged-but-active workflow skill (e.g., ultragoal with an unwinnable quality gate and an operator absent) can block stop indefinitely; every exit requires either real progress or an explicit operator action (`record-critic-gate-override`, `gjc state clear`). That is a defensible fail-closed choice, but it has a known cost.

### F3. Interaction with Gajae-Code's existing workflows

- **Ultragoal.** unlazy's orchestrated mode is a structurally simpler peer of Ultragoal: `PLAN.md` + leaf ledgers + rolling dispatch ≈ `goals.json` + `ledger.jsonl` + leader-owned slices; unlazy's "finish one line of attack"/four-pass leaf loop overlaps with ultragoal's cohort generation loop with delta-only re-review. Two unlazy details have no Ultragoal counterpart: (a) gates authored *before* implementation (Ultragoal gates are constructed at checkpoint time from evidence that already exists); (b) the bounded stop-block release (F2.5).
- **Ralplan.** unlazy has no planning/consensus phase — its contract checklist is the primitive ralplan replaces. No interaction. One borrowed-authoring rule (below) would land in ralplan-adjacent gate guidance if adopted.
- **Deep Interview.** No overlap. unlazy assumes a given task; deep-interview exists to decide what the task is. (unlazy's own "do not create gates for a trivial edit or factual reply" is a weak cousin of deep-interview's do-not-use-when list.)
- **Autoresearch.** unlazy's "audit the final report: re-measure every number and completion claim immediately before reporting" is the same discipline as the autoresearch verdict contract (status/evidence/caveats/evaluator, self-issued with optional distinct critic receipt). No gap.
- **Hooks surface.** unlazy's Stop hook is Claude-Code-shaped; GJC already normalizes `Stop`/`agent_end` across conventions (`hooks/events.ts`, `codex-native-hooks-config.ts`), so a GJC-native equivalent of the bounded release would not need any new event plumbing.

## Reusable ideas (explicitly separated from any product change)

Research conclusions only — each item below names a *candidate*, not an approved change. Any actual adoption is a separate, explicitly proposed product decision (see "Proposal boundary").

### R1 — Candidate for separate decision: bounded no-progress release for the GJC skill stop-hook block

Add an unlazy-style escape hatch to `buildSkillStopOutput` (`packages/coding-agent/src/hooks/skill-state.ts`): when the same active-workflow stop block fires N consecutive times with no durable-state progress (ledger append, checkpoint, state-write), release the block with an operator-visible message instead of blocking forever. Upstream evidence: `stop-hook.mjs` `MAX_BLOCKS = 6`, session-keyed, content-hash progress detection, release message that still names the outstanding items. This is small, mechanical, fail-open-at-the-edge, and addresses a real wedged-session mode GJC currently resolves only through operator intervention. Prerequisite: define "progress" against GJC's durable artifacts (`ledger.jsonl` appends, mode-state mtime/content) rather than unlazy's ledger text hash.

### R2 — Candidate for separate decision (guidance, not machinery): gate-authoring rules

Two authoring rules from `references/gates.md:95-100` are cheap to fold into existing GJC gate/verification guidance (ultragoal quality-gate docs, `docs/` verification guidance) without any runtime change:

1. **Negative controls**: before trusting an absence/negative assertion (e.g., "no regressions", "no slop left"), run the same check against a known positive fixture and confirm it fails. GJC gates currently encode positive evidence well; absence claims are the weak spot.
2. **Supplied-number independence**: never let a number copied from the brief become its own `EXPECT:`; the check must compute the figure from source data. GJC's "never hand-compute a hash" rule is the same instinct; extending it to all measured figures in gate evidence is a one-line documentation change.

### R3 — Consider (low priority): pre-authored acceptance gates

unlazy's "write gates before real work" ordering (gates authored at plan time, then executed) versus GJC's gates-assembled-at-checkpoint-time. Pre-authoring makes gates a plan artifact reviewers can attack during ralplan consensus, and prevents post-hoc gate shaping to match whatever was built. Cost: gates written before implementation are often wrong and need revision, which reintroduces the exact drift problem. Verdict: **interesting, not urgent**; if desired, the natural seam is ralplan final artifacts carrying an acceptance-gate section that Ultragoal checkpoints must satisfy or explicitly amend (an amendment ledger entry, never silent weakening).

### Rejected, with reasons

- **R — Depth-tree effort multiplication / full-budget-per-leaf** (F1): retracted upstream; not reproducible; optimizes spend over correctness; contradicts risk-proportional validation.
- **R — "tree N" depth semantics or an `unlazy` default skill**: violates the four-skill public-surface contract (AGENTS.md); adds a fifth workflow with no capability the existing four lack.
- **R — unlazy's `gate-check.mjs`/`stop-hook.mjs` as code or dependency**: vendor surface overlapping GJC-native machinery; GJC already has quality-gate validation, cohort sourceHash freezing, and surface-typed evidence, so copying would duplicate mechanisms. Also violates "do not copy code from the untrusted reference."
- **R — `GATES.md` markdown ledger format**: GJC's structured `--quality-gate-json` + `ledger.jsonl` receipts are machine-checkable and freshness-scoped; a second prose ledger format would be regression, not addition.
- **R — Ownership leases as a new mechanism**: GJC already has per-slice coordination contracts plus real worktree isolation; unlazy's leases are explicitly not isolation.
- **R — `--jobs N` parallel gate execution**: GJC already has parallel cohort lanes on frozen snapshots, which is a safer parallelism primitive (immutable source vs. concurrent command execution).

## Proposal boundary (not implemented here)

Any change above requires a separate maintainer-approved product decision with its own issue/plan; this lane is research-only. If R1 is pursued, the natural shape is: a counter on consecutive stop-blocks per `(session, skill)` in hook state, progress defined as durable-state change, release threshold ~6 with an operator-visible message mirroring upstream's phrasing, and tests in `packages/coding-agent/test/` covering (a) release after N no-progress blocks, (b) counter reset on progress, (c) cross-session isolation, (d) no release for handoff-required phases where an explicit user-facing step is the correct exit. R2 needs no runtime change; R3 would be a ralplan/Ultragoal artifact-schema discussion.

## Prerequisites and product risks (for any future adoption lane)

- **R1 risk**: a no-progress release weakens the fail-closed guarantee that active workflows never vanish silently. Mitigation: release must be loud (system message naming outstanding work), and handoff-required phases (deep-interview interviewing, ultragoal handoff) should stay unreleaseable — their correct exit is a user-facing step, not a timeout.
- **R1 prerequisite**: a durable "progress" signal that cannot be advanced by noise (unlazy hashes outstanding-gate content; GJC would hash ledger/state content similarly).
- **R2 risk**: none identified (documentation-only).
- **General**: nothing in unlazy is a dependency candidate; all ideas above are re-implementable natively in GJC terms.

## Method and evidence integrity

- All external-repository evidence was captured read-only via the GitHub REST API on 2026-08-23 into `snapshots/` (18 files, including both `main` and `v1` variants of SKILL/README, the security policy cited in F2, and the upstream MIT license notice). No unlazy script was executed at any point. The bundle intentionally omits the upstream changelog because it is not evidence for a report claim; the remaining snapshots cover every quoted or behavior-critical source.
- The prior autoresearch session receipt reports a claim-verification harness (`autoresearch.sh`) covering 10 claims (5 external, 5 GJC-side) with deterministic exit-0 output. That retired harness is not part of this PR or the current checkout, so this review treats the receipt as provenance rather than independently reproducible evidence.
- GJC-side citations are to `packages/coding-agent/src/hooks/skill-state.ts`, `packages/coding-agent/src/hooks/events.ts`, `packages/coding-agent/src/gjc-runtime/autoresearch-runtime.ts`, `packages/coding-agent/src/defaults/gjc/skills/{ultragoal,ralplan,deep-interview,autoresearch}/SKILL.md`, and `AGENTS.md` at base `d06a42e53a9d6363d152a88c8168b5d6b2ab345e`.
- **Limitations:** no live benchmark of the depth-tree method against GJC workloads was run — the multiplication claim is rejected on upstream's own retraction plus internal-consistency grounds, not new measurements, which matches the request's research-only boundary. unlazy has no tagged releases, so findings are pinned to the inspected commit SHAs rather than a version.
