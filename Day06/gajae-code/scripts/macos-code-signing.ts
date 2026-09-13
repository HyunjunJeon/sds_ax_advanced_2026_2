import * as crypto from "node:crypto";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

/** Stable code identity used by GJC's ad-hoc macOS development binaries. */
export const GJC_MACOS_CODE_SIGNING_IDENTIFIER = "com.gajae-code.gjc";

/**
 * TCC stores the designated requirement, not only the display identifier.
 * An ad-hoc signature's default requirement is its changing CDHash, so every
 * rebuild otherwise looks like a new app to macOS privacy services.
 */
export const GJC_MACOS_DESIGNATED_REQUIREMENT =
	`designated => identifier "${GJC_MACOS_CODE_SIGNING_IDENTIFIER}"`;

export function buildMacOSAdHocCodeSignCommand(requirementPath: string, binaryPath: string): string[] {
	return [
		"codesign",
		"--force",
		"--sign",
		"-",
		"--identifier",
		GJC_MACOS_CODE_SIGNING_IDENTIFIER,
		"--requirements",
		requirementPath,
		binaryPath,
	];
}

/**
 * Sign a Darwin binary with a stable ad-hoc designated requirement.
 *
 * The requirement is passed through a temporary file because `codesign -r`
 * accepts a requirement file, not an inline requirement string. The caller
 * owns command execution so build scripts can preserve their cwd/env policy.
 */
export async function signMacOSBinary(
	binaryPath: string,
	runCommand: (command: string[]) => Promise<void>,
): Promise<void> {
	const requirementPath = path.join(os.tmpdir(), `gjc-codesign-${process.pid}-${crypto.randomUUID()}.req`);
	await Bun.write(requirementPath, `${GJC_MACOS_DESIGNATED_REQUIREMENT}\n`);
	try {
		await runCommand(buildMacOSAdHocCodeSignCommand(requirementPath, binaryPath));
	} finally {
		await fs.rm(requirementPath, { force: true });
	}
}
