interface HookApi {
	on(event: "tool_call", handler: (event: { toolName: string; input: { command: string; cwd?: string } }) => Promise<{ block: true; reason: string } | undefined>): void;
}

/**
 * Repository-local opt-in: copy this file to .gjc/hooks/pre/bash.ts in this checkout.
 * Do not install it under ~/.gjc/agent because the enforced PR contract belongs to this repository.
 */
export default function registerPrPreflight(api: HookApi): void {
	api.on("tool_call", async event => {
		if (event.toolName !== "bash") return;
		const command = event.input.command;
		if (!/(?:^|\s)gh\s+pr\s+create(?:\s|$)/u.test(command)) return;

		const invocationCwd = event.input.cwd ?? process.cwd();
		const rootProbe = Bun.spawn(["git", "rev-parse", "--show-toplevel"], { cwd: invocationCwd, stdout: "pipe", stderr: "pipe" });
		const [repoRoot, rootExitCode] = await Promise.all([new Response(rootProbe.stdout).text(), rootProbe.exited]);
		if (rootExitCode !== 0) return { block: true, reason: "PR preflight requires a Git repository checkout." };
		const repositoryRoot = repoRoot.trim();
		const check = Bun.spawn(["bun", "scripts/verify-pr-verdict.ts", "--preflight-command", command, "--repo", repositoryRoot, "--invocation-cwd", invocationCwd], {
			cwd: repositoryRoot,
			stdout: "pipe",
			stderr: "pipe",
		});
		const [stdout, stderr, exitCode] = await Promise.all([
			new Response(check.stdout).text(),
			new Response(check.stderr).text(),
			check.exited,
		]);
		if (exitCode === 0) return;
		return { block: true, reason: `${stderr}${stdout}`.trim() || "PR preflight failed closed without diagnostics." };
	});
}
