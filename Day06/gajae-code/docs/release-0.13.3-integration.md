# Release 0.13.3 integration record

## Authority and immutable base

- Integration branch: `release/0.13.3`
- Authorized repository: `Yeachan-Heo/gajae-code`
- Exact base: `origin/main` at `5666472818b71a1c37615408d9b4d3b5a77b7fa3`
- Delivery mode: coherent cherry-pick groups pushed directly to the existing release branch
- Explicitly not authorized here: version bump, `main` mutation, PR creation, tag, publish, or release execution

## Included scope and dependency decisions

### Original hotfixes

| Candidate | Decision | Dependency boundary |
| --- | --- | --- |
| `0bd4898d` / #4437 | Include | Isolated Rust text wrapping termination fix and regression tests. No provider, auth, SDK lifecycle, or settings dependency. |
| `38f3b407` / supplied #4509 anchor (commit subject references #4481) | Include | Bounded TUI overlay geometry and dedicated capture regressions. Changes stay in TUI rendering plus diagnostic scripts. |
| `b5ef23f5` / #4446 | Include | Print-mode process termination and postmortem draining. Embedding and ACP ownership are explicitly preserved by the change. |

### Bounded utilities

| Candidate | Decision | Dependency boundary |
| --- | --- | --- |
| `6080b983` / #4453 | Include | Narrow `todo_write` positional-handle diagnostics and tests. No routing or model-contract change. |
| `8eb8127e` / #4424 | Include | Narrow terminal image scrollback preservation. Shares `tui.ts` with the overlay hotfix, so it is integrated before the geometry hardening and tested together. |
| `c06f4f5` / #4451 | Skip | Candidate directly modifies `src/autoresearch/dashboard.ts`; autoresearch is explicitly excluded. Its render-cache work is not cherry-picked partially because the commit couples the cache contract to excluded autoresearch and several controller/component migrations. |

### Session, storage, retry, and crash resilience

| Candidate | Decision | Dependency boundary |
| --- | --- | --- |
| `c81614fc` / #4396 | Include | Managed-output publication/reaping plus the minimum native path-identity binding needed by that storage implementation. No SDK notification/lifecycle architecture change. |
| `8d6784cb` / #4411 | Include | Managed transcript size proactive/reactive recovery and compaction signal. Changes are limited to agent compaction and coding-agent session persistence. |
| `515f1c7c` / #4421 | Include | Missing managed transcript recovery in session manager. |
| `ffa07d6c` / #4450 | Include | Missing predecessor recovery in managed session storage. Applied after #4396 because both touch the same storage implementation and #4450 is the later semantic correction. |
| `0a736b17` / #4470 | Include | Crash-index compaction/read compatibility fix. Applied before #4495 so the later broader recovery work retains this invariant. |
| `7a6b0d13` / #4495 | Include | Crash journal/index recovery hardening. Scoped to crash persistence and record loading. |

### Pet features

| Candidate | Decision | Dependency boundary |
| --- | --- | --- |
| `d2f6e8a4` / #4468 | Include with strict pet-only settings allowance | Ouroboros pet/theme requires a bounded settings schema enum, pet/theme selectors, theme registration, and UI verification updates. These are accepted only as the minimum cohesive dependency of the pet feature; no general settings migration, MCP, customization migration, provider, model, or auth contract is admitted. |
| `dfb1081c` / #4499 | Include | iTerm2 pet rendering is applied after #4468 because both update the pet widget and TUI pet renderer. It does not alter auth/provider/model or SDK lifecycle contracts. |

## Owner-authorized expansion after the original audit

The owner subsequently authorized a bounded provider/preset and stability expansion on the existing release branch. The following exact changes were added without importing the broader `dev` architecture:

### Stability and security

- #4385 Synthetic login validation: use the provider models endpoint instead of a retired hard-coded Kimi probe (`6de4096025`, changelog `e14189ec82`).
- #4302 image-generation provider error hardening: redact credentials and bound response bodies (`08e3e4de22`, `b3ce4bb584`).
- #4452 status-line scratch-root resolution at render time (`d27175a1c3`).
- #4373 Windows replacement-receipt reconciliation over the release branch's existing managed-session storage fixes (`bc9417f4eb`).

### Providers and presets

- #4304 native TypeScript Kiro OAuth/CodeWhisperer provider (`7913ce221a`).
- #4294 Muse Spark 1.2 open-weight catalog, profile, reasoning, cache-normalization, and routing closure (`6ff17fe082` through `efd850df86`).
- #4377 direct xAI Grok 4.6 catalog and role profiles (`cb3eba7a93`, `8c7170a212`).
- #4462 bundled Grok CLI 4.6 catalog and bounded effort parsing (`0ce9f10436`, `5a931cc529`, `4b15d708bf`).

CHANGELOG conflicts were resolved by retaining the released 0.13.2 history and existing release entries while adding only the new Unreleased provider/preset records. Generated model data was merged with its source generators and must pass reproducibility tests before the release cut.

## Explicit exclusions

- General settings, MCP, or customization migrations beyond the minimum cohesive pet dependencies listed above
- SDK lifecycle or notification architecture
- GJC master/supervisor work
- Autorouting
- Autoresearch
- macOS TCC/signing change #4275 pending dedicated release-build and permission-identity validation
- Version bump, release metadata cut, tag, publish, release execution, PR creation, or `main` mutation

## Integration order

1. Record this scope and boundary decision.
2. Original hotfixes and bounded TUI utilities: #4437, #4424, supplied #4509 anchor/#4481 commit, #4446, #4453.
3. Session/storage resilience: #4396, #4411, #4421, #4450.
4. Crash recovery resilience: #4470, #4495.
5. Pet: #4468, then #4499.
6. Run focused tests after each coherent group, followed by branch-wide checks, build, test, and install/package smoke.

## Validation evidence

This section is updated as groups are integrated. Exact commands, outcomes, skips, and environmental blockers are recorded before final delivery.
