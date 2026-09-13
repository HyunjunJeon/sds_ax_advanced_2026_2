import { dlopen } from "bun:ffi";
import * as fs from "node:fs";

// `IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE)` — the kernel's
// authoritative AVX2 answer, usable in-process on every supported Windows build.
const WIN32_PF_AVX2_INSTRUCTIONS_AVAILABLE = 40;

export type Win32Avx2ProbeDiagnostic = "timeout" | "spawn_error" | "nonzero_exit" | "non_decisive_output";

const WIN32_AVX2_PROBE_TIMEOUT_MS = 4_000;
const WIN32_AVX2_PROBE_MAX_BUFFER = 4 * 1024;

function emitWin32Avx2ProbeDiagnostic(kind: Win32Avx2ProbeDiagnostic): void {
	process.emitWarning(`Windows AVX2 host probe inconclusive (${kind}); using baseline variant.`, {
		code: "GJC_WIN32_AVX2_PROBE",
	});
}

function spawnFailureDiagnostic(result: { error?: unknown; exitCode: number | null; signalCode?: string | null }): Win32Avx2ProbeDiagnostic {
	const errorCode = result.error && typeof result.error === "object" && "code" in result.error ? result.error.code : undefined;
	if (errorCode === "ETIMEDOUT" || result.signalCode === "SIGKILL") return "timeout";
	if (result.error) return "spawn_error";
	if (result.exitCode !== 0) return "nonzero_exit";
	return "non_decisive_output";
}

function runCommand(
	command: string,
	args: string[],
	report?: (diagnostic: Win32Avx2ProbeDiagnostic) => void,
): string | null {
	try {
		const result = Bun.spawnSync([command, ...args], {
			stdout: "pipe",
			stderr: "pipe",
			windowsHide: true,
			timeout: WIN32_AVX2_PROBE_TIMEOUT_MS,
			killSignal: "SIGKILL",
			maxBuffer: WIN32_AVX2_PROBE_MAX_BUFFER,
		});
		if (!result.success || result.exitCode !== 0) {
			report?.(spawnFailureDiagnostic(result));
			return null;
		}
		return result.stdout.toString("utf-8").trim();
	} catch {
		report?.("spawn_error");
		return null;
	}
}

function probeWin32Avx2InProcess(): boolean | undefined {
	try {
		const kernel32 = dlopen("kernel32.dll", {
			IsProcessorFeaturePresent: { args: ["i32"], returns: "bool" },
		});
		return Boolean(kernel32.symbols.IsProcessorFeaturePresent(WIN32_PF_AVX2_INSTRUCTIONS_AVAILABLE));
	} catch {
		return undefined;
	}
}

/**
 * Hidden PowerShell fallback for runtimes without FFI. `Add-Type` P/Invoke
 * works on both stock Windows PowerShell 5.1 (.NET Framework, which has no
 * System.Runtime.Intrinsics) and pwsh 7+. Failures fail safe to `false`.
 */
export function detectWin32Avx2Support(
	probe: () => boolean | undefined = probeWin32Avx2InProcess,
	command: (
		file: string,
		args: string[],
		report?: (diagnostic: Win32Avx2ProbeDiagnostic) => void,
	) => string | null = runCommand,
	report: (diagnostic: Win32Avx2ProbeDiagnostic) => void = emitWin32Avx2ProbeDiagnostic,
): boolean {
	const probed = probe();
	if (probed !== undefined) return probed;

	const output = command("powershell.exe", [
		"-NoProfile",
		"-NonInteractive",
		"-Command",
		"Add-Type -Namespace GjcNative -Name Cpu -MemberDefinition '[DllImport(\"kernel32.dll\")] public static extern bool IsProcessorFeaturePresent(int feature);'; " +
			`[GjcNative.Cpu]::IsProcessorFeaturePresent(${WIN32_PF_AVX2_INSTRUCTIONS_AVAILABLE})`,
	], report);
	if (output === null) return false;
	const normalized = output.toLowerCase();
	if (normalized === "true") return true;
	if (normalized === "false") return false;
	report("non_decisive_output");
	return false;
}

export function detectHostAvx2Support(): boolean {
	if (process.arch !== "x64") return false;

	if (process.platform === "linux") {
		try {
			const cpuInfo = fs.readFileSync("/proc/cpuinfo", "utf8");
			return /\bavx2\b/i.test(cpuInfo);
		} catch {
			return false;
		}
	}

	if (process.platform === "darwin") {
		const leaf7 = runCommand("sysctl", ["-n", "machdep.cpu.leaf7_features"]);
		if (leaf7 && /\bAVX2\b/i.test(leaf7)) return true;
		const features = runCommand("sysctl", ["-n", "machdep.cpu.features"]);
		return Boolean(features && /\bAVX2\b/i.test(features));
	}

	if (process.platform === "win32") {
		return detectWin32Avx2Support();
	}

	return false;
}
