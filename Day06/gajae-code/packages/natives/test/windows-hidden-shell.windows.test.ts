/**
 * Live Windows execution of the #4883 hidden-console fix.
 *
 * These behavioral tests run only on the windows-latest CI job
 * (`it.skipIf(!isWindows)`). They are the source of truth for the Windows
 * creation-flag contract; a source-text mirror would not exercise it.
 *
 * Strategy: spawn a bun child (hidden, so the test itself never allocates a
 * window) that calls `kernel32!FreeConsole` to become console-less — the
 * exact state of a GJC agent embedded in a console-less ACP/GUI host — and
 * self-verifies `GetConsoleWindow() == null` before proceeding, so the
 * scenario can never silently degrade into a vacuous pass. It then exercises
 * the native brush shell exactly the way the bash tool does (`executeShell`)
 * with external children (powershell / cmd / python when present) where the
 * powershell child reports its own console state via `GetConsoleWindow` +
 * `IsWindowVisible` P/Invoked in-process (the Add-Type approach proven by the
 * AVX2 probe, #4652 — works on stock PowerShell 5.1). The assertions read the
 * kernel's view of the spawned children, not a mock:
 *
 *   - console-less host → the powershell child HAS a console whose window is
 *     NOT visible (CREATE_NO_WINDOW; `DETACHED_PROCESS` would report
 *     `has-console=0`, and no fix at all would report `visible=1`);
 *   - cmd and python children run through the identical spawn path and must
 *     execute successfully (pipes/exit codes unaffected);
 *   - a console-attached parent keeps its own console (the host-side half of
 *     the contract: CREATE_NO_WINDOW must not apply to attached hosts).
 */
import { dlopen } from "bun:ffi";
import { describe, expect, it, setDefaultTimeout } from "bun:test";
import * as childProcess from "node:child_process";
import * as path from "node:path";
import * as url from "node:url";

const isWindows = process.platform === "win32";

// The child boots the native addon and runs external children; a cold
// Windows image needs real headroom.
const LIVE_TEST_TIMEOUT_MS = 60_000;
setDefaultTimeout(LIVE_TEST_TIMEOUT_MS);

const NATIVES_ENTRY_URL = url.pathToFileURL(path.resolve("packages/natives/native/index.js")).href;

// Three-way console-state probe, one P/Invoke per kernel call:
//   - handle:      GetConsoleWindow() — non-null ⇒ a console WINDOW exists
//                  (flags-0 child of a console-less host allocates a visible
//                  one; CREATE_NO_WINDOW children get none).
//   - has-console: GetConsoleCP() — non-zero ⇒ a console is attached at all.
//                  CREATE_NO_WINDOW keeps a headless console (non-zero);
//                  DETACHED_PROCESS would report zero.
//   - visible:     handle non-null and IsWindowVisible(handle).
const PROBE_PS = [
	"$w = Add-Type -Namespace GjcProbe -Name Win -PassThru -MemberDefinition @'",
	'[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();',
	'[DllImport("kernel32.dll")] public static extern uint GetConsoleCP();',
	'[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);',
	"'@",
	"$h = $w::GetConsoleWindow()",
	'Write-Output ("handle={0} has-console={1} visible={2}" -f $h, [int]($w::GetConsoleCP() -ne 0), [int]($h -ne [IntPtr]::Zero -and $w::IsWindowVisible($h)))',
].join("\n");

function runHidden(command: string, args: string[], cwd?: string, hide = true) {
	return childProcess.spawnSync(command, args, {
		encoding: "utf-8",
		windowsHide: hide,
		timeout: 50_000,
		cwd,
	});
}

/**
 * Runs one console-less bun child executing `script` and returns its output.
 * The child detaches its own console via `FreeConsole` and hard-fails unless
 * the detach actually took, so the console-less precondition is proven, not
 * assumed.
 */
function runConsolelessBun(script: string) {
	const wrapper = `
import { dlopen as childDlopen } from "bun:ffi";
import { executeShell } from ${JSON.stringify(NATIVES_ENTRY_URL)};

(async () => {
	const kernel32 = childDlopen("kernel32.dll", {
		FreeConsole: { args: [], returns: "bool" },
		GetConsoleWindow: { args: [], returns: "ptr" },
	});
	kernel32.symbols.FreeConsole();
	const consoleHandle = kernel32.symbols.GetConsoleWindow();
	if (Number(consoleHandle) !== 0) {
		console.error("host console still attached after FreeConsole: " + consoleHandle);
		process.exit(3);
	}
	console.log("host-consoleless=1");
	async function sh(command) {
		let out = "";
		const result = await executeShell({ command, timeoutMs: 45_000 }, (err, chunk) => {
			if (!err) out += chunk;
		});
		return { result, out };
	}
${script}
})().then(
	() => process.exit(0),
	(err) => { console.error((err && err.stack) || err); process.exit(1); },
);
`;
	return runHidden(process.execPath, ["-e", wrapper], "packages/natives");
}

/** This test process's own console window handle (0 when console-less). */
function hostConsoleHandle(): number {
	if (!isWindows) return 0;
	const kernel32 = dlopen("kernel32.dll", {
		GetConsoleWindow: { args: [], returns: "ptr" },
	});
	return Number(kernel32.symbols.GetConsoleWindow());
}

describe("windows hidden-console bash-tool children (#4883, live)", () => {
	it.skipIf(!isWindows)("console-less host: powershell child has a hidden console, not a visible one", () => {
		// The probe script is delivered base64-encoded: no quoting or variable
		// expansion of the brush shell can mangle it, and powershell.exe itself
		// decodes the payload.
		// `-EncodedCommand` decodes UTF-16LE, so encode that, not UTF-8.
		const encoded = Buffer.from(PROBE_PS, "utf-16le").toString("base64");
		const command = `powershell -NoProfile -NonInteractive -EncodedCommand ${encoded}`;
		const probe = runConsolelessBun(`
	const { result, out } = await sh(${JSON.stringify(command)});
	if (result.exitCode !== 0) throw new Error("powershell probe exited " + result.exitCode);
	console.log(out.trim());
`);
		expect(probe.status, `child stderr: ${probe.stderr ?? ""}`).toBe(0);
		const stdout = (probe.stdout ?? "").trim();
		// Precondition proven inside the child; regression signals below.
		expect(stdout).toContain("host-consoleless=1");
		// CREATE_NO_WINDOW contract, three-way discriminated:
		//   handle=0      — no console WINDOW allocated (a flags-0 child of a
		//                   console-less host would allocate a visible one);
		//   has-console=1 — a headless console is still attached (DETACHED_
		//                   PROCESS would report 0 and lose grandchildren);
		//   visible=0     — belt-and-braces visibility check.
		expect(stdout).toContain("handle=0");
		expect(stdout).toContain("has-console=1");
		expect(stdout).not.toContain("visible=1");
	});

	it.skipIf(!isWindows)("console-less host: cmd and python children execute through the hidden spawn path", () => {
		const cmdProbe = runConsolelessBun(`
	const { result, out } = await sh("cmd /c echo cmd-ok");
	if (result.exitCode !== 0) throw new Error("cmd exited " + result.exitCode);
	console.log(out.trim());
`);
		expect(cmdProbe.status).toBe(0);
		expect((cmdProbe.stdout ?? "").trim()).toContain("cmd-ok");

		// python, when installed, goes through the identical spawn path.
		const pythonPresent = runHidden("where", ["python"]).status === 0;
		if (pythonPresent) {
			const pyProbe = runConsolelessBun(`
	const { result, out } = await sh("python -c \\"print('py-ok')\\"");
	if (result.exitCode !== 0) throw new Error("python exited " + result.exitCode);
	console.log(out.trim());
`);
			expect(pyProbe.status).toBe(0);
			expect((pyProbe.stdout ?? "").trim()).toContain("py-ok");
		}
	});

	// Skipped when the runner itself is console-less: inheritance is only
	// observable from a host that actually owns a console.
	it.skipIf(!isWindows || hostConsoleHandle() === 0)(
		"console-attached host keeps inheriting the parent console",
		() => {
			// Spawned WITHOUT windowsHide: the child must inherit this process's
			// console and report the very same window handle. CREATE_NO_WINDOW
			// here would hand it a different hidden console — exactly the
			// interactive-session regression the fix must avoid. (The brush-path
			// equivalent is the Rust e2e test in crates/pi-shell/src/shell.rs.)
			const probe = runHidden(
				"powershell.exe",
				["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(PROBE_PS, "utf-16le").toString("base64")],
				undefined,
				false,
			);
			expect(probe.status).toBe(0);
			const out = (probe.stdout ?? "").trim();
			expect(out).toContain("has-console=1");
			const handle = out.match(/handle=(\d+)/)?.[1];
			expect(handle).toBe(String(hostConsoleHandle()));
		},
	);
});
