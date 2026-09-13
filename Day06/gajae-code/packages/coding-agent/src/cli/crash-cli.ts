/**
 * Terminal adapter for `gjc crash`.
 *
 * Keeps all I/O concerns (readline, TTY detection, stdout) out of the flow in
 * `crash/report.ts`, which is the part that carries the consent contract and is
 * therefore tested against stubbed boundaries.
 */

import * as path from "node:path";
import { createInterface } from "node:readline/promises";
import { getCrashIndexPath } from "@gajae-code/utils";
import type { Settings } from "../config/settings";
import { compactCrashIndex, listCrashSignatures, resolveCrashStatePaths } from "../crash/index-store";
import { type CrashReportIo, type CrashReportOutcome, runCrashReportFlow } from "../crash/report";
import { type CrashRelayOutcome, readTrustedRelayConfig, relayAllSignatures } from "../crash/upstream/relay";
import { runGhDefault } from "../utils/gh";

function createIo(): CrashReportIo {
	const write = (text: string) => {
		process.stdout.write(text);
	};
	const ask = async (question: string): Promise<string> => {
		const rl = createInterface({ input: process.stdin, output: process.stdout, terminal: true });
		try {
			return (await rl.question(question)).trim();
		} finally {
			rl.close();
		}
	};
	return {
		print: write,
		input: prompt => ask(`${prompt}: `),
		confirm: async prompt => {
			const answer = (await ask(`${prompt} [y/N] `)).toLowerCase();
			return answer === "y" || answer === "yes";
		},
		select: async (prompt, options) => {
			write(`\n${prompt}\n`);
			for (const [index, option] of options.entries()) write(`  ${index + 1}) ${option}\n`);
			const answer = await ask(`Choice [1-${options.length}] `);
			const choice = Number.parseInt(answer, 10);
			if (!Number.isInteger(choice) || choice < 1 || choice > options.length) return undefined;
			return choice - 1;
		},
	};
}

export async function runCrashReportCommand(): Promise<CrashReportOutcome> {
	const paths = resolveCrashStatePaths();
	return runCrashReportFlow({
		io: createIo(),
		paths,
		snapshotDir: path.dirname(getCrashIndexPath()),
		runGh: runGhDefault,
		interactive: process.stdin.isTTY === true && process.stdout.isTTY === true,
	});
}

export async function runCrashListCommand(json: boolean): Promise<void> {
	const paths = resolveCrashStatePaths();
	const index = await compactCrashIndex({ paths });
	const signatures = listCrashSignatures(index);
	if (json) {
		process.stdout.write(`${JSON.stringify({ overflow: index.overflow, signatures }, null, 2)}\n`);
		return;
	}
	if (signatures.length === 0) {
		process.stdout.write("No crash signatures recorded.\n");
		return;
	}
	if (index.overflow) process.stdout.write("warning: crash index is full; new signatures are not being recorded.\n");
	for (const signature of signatures) {
		const state = signature.reportedAt !== undefined ? "reported" : "unreported";
		process.stdout.write(
			`${signature.fingerprint}  fpv:${signature.fpv}  ${signature.lifetimeCount}x  ` +
				`${new Date(signature.firstSeen).toISOString().slice(0, 10)}→${new Date(signature.lastSeen).toISOString().slice(0, 10)}  ` +
				`${state}\n    ${signature.errorName}: ${signature.messageClass.slice(0, 120)}\n`,
		);
	}
}

/**
 * Batch-relay every due signature to the configured upstream.
 *
 * The startup relay is silent and best-effort; this is the loud form, so a
 * refusal is reported as a refusal instead of being swallowed. It shares the
 * same gate: with `crashReport.upstream` off, it explains that and exits
 * non-zero rather than quietly doing nothing.
 */
export function crashRelayExitCode(outcome: CrashRelayOutcome): number {
	if (outcome.status === "skipped") return outcome.reason === "nothing-to-relay" ? 0 : 1;
	return outcome.failed > 0 || outcome.refused > 0 ? 1 : 0;
}

export async function runCrashRelayCommand(settings: Settings): Promise<void> {
	const outcome = await relayAllSignatures({ config: readTrustedRelayConfig(settings) });
	if (outcome.status === "skipped") {
		const explain: Record<string, string> = {
			disabled: "crashReport.upstream is off; nothing was transmitted.",
			"no-dsn": "No upstream DSN configured (crashReport.upstreamDsn or GJC_CRASH_SENTRY_DSN).",
			"invalid-dsn": "The configured upstream DSN could not be parsed; refusing to send.",
			"nothing-to-relay": "No crash signatures are due for relay.",
		};
		process.stdout.write(`${explain[outcome.reason] ?? outcome.reason}\n`);
		process.exitCode = crashRelayExitCode(outcome);
		return;
	}
	process.stdout.write(`relayed ${outcome.sent}, refused ${outcome.refused}, failed ${outcome.failed}\n`);
	if (outcome.refused > 0)
		process.stdout.write("refused signatures failed the outbound sanitizer and were not sent.\n");
	// A partially refused batch is not a success. Automation that only reads the
	// exit code must not conclude the whole set was delivered.
	process.exitCode = crashRelayExitCode(outcome);
}
