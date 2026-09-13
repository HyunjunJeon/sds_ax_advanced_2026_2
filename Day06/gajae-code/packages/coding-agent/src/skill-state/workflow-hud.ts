import type { WorkflowHudChip, WorkflowHudSummary } from "./active-state";

interface WorkflowGateHudState {
	approvalStatus?: string;
	blockedReason?: string;
	nextAction?: string;
}

interface DeepInterviewHudState extends WorkflowGateHudState {
	phase?: string;
	ambiguity?: number;
	threshold?: number;
	roundCount?: number;
	targetComponent?: string;
	weakestDimension?: string;
	specStatus?: string;
	updatedAt?: string;
}

interface RalplanHudState extends WorkflowGateHudState {
	stage?: string;
	waiting?: string;
	iteration?: number;
	iterationFromIndex?: number;
	stages?: string;
	architectPasses?: number;
	criticPasses?: number;
	reviewPassBudget?: number;
	verdict?: string;
	latestSummary?: string;
	pendingApproval?: boolean;
	autoHandoff?: {
		configuredTarget: string;
		effectiveTarget: string;
		degradationReason: string | null;
	};
	planningStuck?: boolean;
	updatedAt?: string;
}

interface UltragoalLikeGoal {
	id: string;
	title: string;
	status: string;
}

interface UltragoalHudState extends WorkflowGateHudState {
	status: string;
	currentGoal?: UltragoalLikeGoal;
	counts: Record<string, number>;
	goals: UltragoalLikeGoal[];
	latestLedgerEvent?: { event?: string; goalId?: string; timestamp?: string; kind?: string; evidence?: string };
	updatedAt?: string;
}

function percent(value: number | undefined): string | undefined {
	if (typeof value !== "number" || !Number.isFinite(value)) return undefined;
	return `${Math.round(value * 100)}%`;
}

function chip(
	label: string,
	value: string | undefined,
	priority: number,
	severity?: WorkflowHudChip["severity"],
): WorkflowHudChip | null {
	if (!value) return null;
	return { label, value, priority, ...(severity ? { severity } : {}) };
}

function gateChips(state: WorkflowGateHudState, gatePriority: number): Array<WorkflowHudChip | null> {
	return [
		chip("gate", state.approvalStatus, gatePriority, state.approvalStatus === "approved" ? "success" : "warning"),
		chip("blocked", state.blockedReason, gatePriority + 10, "blocked"),
		chip("next", state.nextAction, gatePriority + 20),
	];
}

function compactChips(chips: Array<WorkflowHudChip | null>): WorkflowHudChip[] {
	return chips.filter((item): item is WorkflowHudChip => item !== null);
}

export function buildDeepInterviewHudSummary(state: DeepInterviewHudState): WorkflowHudSummary {
	return {
		version: 1,
		chips: compactChips([
			...gateChips(state, 5),
			chip("phase", state.phase, 10),
			chip("ambiguity", [percent(state.ambiguity), percent(state.threshold)].filter(Boolean).join("/"), 20),
			chip("round", state.roundCount === undefined ? undefined : String(state.roundCount), 30),
			chip("target", state.targetComponent, 40),
			chip("weakest", state.weakestDimension, 50),
			chip("spec", state.specStatus, 60),
		]),
		...(state.updatedAt ? { updated_at: state.updatedAt } : {}),
	};
}

export interface DeepInterviewHudDeriveOptions {
	phase?: string;
	specStatus?: string;
	updatedAt?: string;
}

function diIsPlainObject(value: unknown): value is Record<string, unknown> {
	return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function latestScoredAmbiguity(rounds: unknown): number | undefined {
	if (!Array.isArray(rounds)) return undefined;
	for (let index = rounds.length - 1; index >= 0; index--) {
		const round = rounds[index];
		if (diIsPlainObject(round) && round.lifecycle === "scored" && typeof round.ambiguity === "number") {
			return round.ambiguity;
		}
	}
	return undefined;
}

function weakestDimensionFromTopology(
	topology: Record<string, unknown>,
	targetComponent: string | undefined,
): string | undefined {
	if (!Array.isArray(topology.components)) return undefined;
	const components = topology.components.filter(diIsPlainObject);
	const dimensionOf = (component: Record<string, unknown>): string | undefined =>
		typeof component.weakest_dimension === "string" && component.weakest_dimension.trim()
			? component.weakest_dimension
			: undefined;
	if (targetComponent) {
		const targeted = components.find(component => component.id === targetComponent && dimensionOf(component));
		if (targeted) return dimensionOf(targeted);
	}
	const active = components.find(component => component.status !== "deferred" && dimensionOf(component));
	if (active) return dimensionOf(active);
	const any = components.find(component => dimensionOf(component));
	return any ? dimensionOf(any) : undefined;
}

/**
 * Single source of deep-interview HUD derivation. Reads a complete (normalized)
 * mode-state envelope so recorder, `gjc state write`, reconcile, seed, and handoff
 * all produce identical chips. Topology-aware `target`/`weakest` come from
 * `state.topology`; `legacy_missing` topology omits those chips (no synthetic values).
 */
export function deriveDeepInterviewHud(
	payload: Record<string, unknown>,
	options: DeepInterviewHudDeriveOptions = {},
): WorkflowHudSummary {
	const stateField = diIsPlainObject(payload.state) ? payload.state : {};
	const isNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
	const isArray = (value: unknown): value is unknown[] => Array.isArray(value);
	const pick = <T>(key: string, guard: (value: unknown) => value is T): T | undefined => {
		const value = stateField[key] ?? payload[key];
		return guard(value) ? value : undefined;
	};

	const phase = options.phase ?? (typeof payload.current_phase === "string" ? payload.current_phase : undefined);
	const rounds = pick("rounds", isArray);
	const ambiguity = pick("current_ambiguity", isNumber) ?? latestScoredAmbiguity(rounds);
	const threshold = pick("threshold", isNumber);
	const rawTopology = diIsPlainObject(stateField.topology)
		? stateField.topology
		: diIsPlainObject(payload.topology)
			? payload.topology
			: undefined;
	// `legacy_missing` topology was never confirmed: omit target/weakest even if stale fields linger.
	const topology = rawTopology && rawTopology.status !== "legacy_missing" ? rawTopology : undefined;
	const targetComponent =
		topology && typeof topology.last_targeted_component_id === "string"
			? topology.last_targeted_component_id
			: undefined;
	const weakestDimension = topology ? weakestDimensionFromTopology(topology, targetComponent) : undefined;
	const specStatus = options.specStatus ?? (typeof payload.spec_status === "string" ? payload.spec_status : undefined);

	return buildDeepInterviewHudSummary({
		phase,
		ambiguity,
		threshold,
		roundCount: rounds?.length,
		targetComponent,
		weakestDimension,
		specStatus,
		updatedAt: options.updatedAt ?? new Date().toISOString(),
	});
}

export function buildRalplanHudSummary(state: RalplanHudState): WorkflowHudSummary {
	const verdict = state.verdict?.toUpperCase();
	const verdictSeverity =
		verdict === "BLOCK" || verdict === "REJECT"
			? "blocked"
			: verdict === "ITERATE" || verdict === "WATCH"
				? "warning"
				: verdict === "APPROVE" || verdict === "CLEAR" || verdict === "OKAY"
					? "success"
					: undefined;
	const reviewPassChip = (
		label: "arch" | "crit",
		passes: number | undefined,
		priority: number,
	): WorkflowHudChip | null => {
		if (
			state.pendingApproval ||
			typeof passes !== "number" ||
			!Number.isFinite(passes) ||
			passes <= 0 ||
			typeof state.reviewPassBudget !== "number" ||
			!Number.isFinite(state.reviewPassBudget) ||
			state.reviewPassBudget <= 0
		) {
			return null;
		}
		return chip(label, `${passes}/${state.reviewPassBudget}`, priority);
	};
	const handoffValue = state.autoHandoff
		? `${state.autoHandoff.configuredTarget}→${state.autoHandoff.effectiveTarget}${
				state.autoHandoff.degradationReason ? `:${state.autoHandoff.degradationReason}` : ""
			}`
		: undefined;
	const handoffSeverity = state.planningStuck
		? "blocked"
		: state.autoHandoff?.degradationReason
			? "warning"
			: undefined;
	return {
		version: 1,
		summary: state.latestSummary,
		chips: compactChips([
			state.pendingApproval ? { label: "pending", value: "approval", priority: 5, severity: "warning" } : null,
			...gateChips(state, 6),
			chip("stage", state.stage, 10),
			chip("waiting", state.waiting, 20),
			chip(
				"iter",
				(state.iterationFromIndex ?? state.iteration) === undefined
					? undefined
					: String(state.iterationFromIndex ?? state.iteration),
				30,
			),
			chip("stages", state.stages, 35),
			reviewPassChip("arch", state.architectPasses, 36),
			reviewPassChip("crit", state.criticPasses, 38),
			chip("verdict", verdict, 40, verdictSeverity),
			chip("handoff", handoffValue, 45, handoffSeverity),
		]),
		...(state.updatedAt ? { updated_at: state.updatedAt } : {}),
	};
}

export interface AutoresearchHudState extends WorkflowGateHudState {
	phase: string;
	mode?: string;
	intake?: string;
	slug?: string;
	specPath?: string;
	verdict?: string;
	experimentCount?: number;
	experimentStatuses?: string[];
	updatedAt?: string;
}

export function buildAutoresearchHudSummary(state: AutoresearchHudState): WorkflowHudSummary {
	const statuses = state.experimentStatuses ?? [];
	const total = Math.max(state.experimentCount ?? statuses.length, statuses.length);
	const kept = statuses.filter(status => status === "keep").length;
	const crash = statuses.filter(status => status === "crash").length;
	const checksFailed = statuses.filter(status => status === "checks_failed").length;
	const chips: Array<WorkflowHudChip | null> = [
		...(state.phase === "verdict" ? [chip("verdict", state.verdict ?? "issued", 5)] : []),
		chip("phase", state.phase, 10),
		...(total > 0 ? [chip("exp", `${kept}/${total}`, 20)] : []),
		crash > 0 ? { label: "crash", value: String(crash), priority: 4, severity: "warning" } : null,
		checksFailed > 0
			? { label: "checks_failed", value: String(checksFailed), priority: 5, severity: "warning" }
			: null,
		chip("mode", state.mode, 30),
		chip("intake", state.intake, 40),
		chip("spec", state.specPath, 45),
		...gateChips(state, 50),
	];
	const visibleChips =
		total === 0 && state.phase !== "verdict" ? compactChips([chip("phase", state.phase, 10)]) : compactChips(chips);
	return {
		version: 1,
		...(state.slug ? { summary: `autoresearch mission ${state.slug}` } : {}),
		chips: visibleChips,
		...(state.updatedAt ? { updated_at: state.updatedAt } : {}),
	};
}

export function buildUltragoalHudSummary(state: UltragoalHudState): WorkflowHudSummary {
	const total = state.goals.length;
	const complete = state.counts.complete ?? 0;
	const blockers = (state.counts.blocked ?? 0) + (state.counts.review_blocked ?? 0) + (state.counts.failed ?? 0);
	return {
		version: 1,
		chips: compactChips([
			blockers > 0 ? { label: "blocked", value: String(blockers), priority: 5, severity: "blocked" } : null,
			chip("goals", `${complete}/${total}`, 10),
			chip("current", state.currentGoal ? `${state.currentGoal.id}:${state.currentGoal.title}` : state.status, 20),
			chip("status", state.status, 30, state.status === "complete" ? "success" : undefined),
			chip(
				"ledger",
				state.latestLedgerEvent?.event
					? [state.latestLedgerEvent.event, state.latestLedgerEvent.kind, state.latestLedgerEvent.goalId]
							.filter(Boolean)
							.join(":")
					: undefined,
				35,
			),
			...gateChips(state, 40),
		]),
		...(state.updatedAt ? { updated_at: state.updatedAt } : {}),
	};
}
