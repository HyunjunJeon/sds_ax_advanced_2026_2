import { randomBytes } from "node:crypto";
import type { Args } from "../cli/args";

export type MasterScope = "repo" | "pwd" | "global";

/**
 * Process-local master authority. Its capability accessor is deliberately the
 * only way to reach the raw capability; this context is never serializable.
 */
export interface MasterModeContext {
	readonly scope: MasterScope;
	readonly ownerSessionId: string;
	readonly attestationEpoch: string;
	getCapability(): string;
}

export function createMasterModeContext(
	scope: MasterScope,
	ownerSessionId: string,
	attestationEpoch: string,
): MasterModeContext {
	const capability = randomBytes(32).toString("base64url");
	return { scope, ownerSessionId, attestationEpoch, getCapability: () => capability };
}

/** Reject routes that cannot establish an interactive, durable master session. */
export function assertMasterLaunchArgs(parsed: Args): void {
	if (parsed.master !== true) return;
	if (
		parsed.print === true ||
		parsed.mode !== undefined ||
		parsed.export !== undefined ||
		parsed.listModels !== undefined ||
		parsed.noSession === true
	) {
		throw new Error("--master requires an interactive durable TUI launch");
	}
}

export function assertMasterLaunchDisposition(input: {
	master: boolean | undefined;
	isInteractive: boolean;
	autoPrint: boolean;
	nonInteractiveError?: string;
}): void {
	if (input.master !== true) return;
	if (!input.isInteractive || input.autoPrint || input.nonInteractiveError !== undefined) {
		throw new Error("--master requires an interactive TTY launch");
	}
}
