import { describe, expect, it } from "bun:test";
import { checkTmuxSelfInjection } from "../src/tools/tmux-self-injection-guard";

const env = {
	TMUX: `/tmp/tmux-${typeof process.getuid === "function" ? process.getuid() : 0}/default,12345,0`,
	TMUX_PANE: "%47",
	TMUX_TMPDIR: "/tmp",
	PWD: "/tmp",
};

function resolver(target: string): string | undefined {
	if (target === "demo" || target === "demo:19.0" || target === ".") return "%47";
	return "%99";
}

const options = {
	env,
	cwd: "/tmp",
	resolvePaneId: async ({ target }: { target: string }) => resolver(target),
};

describe("tmux self-injection guard", () => {
	it("blocks a direct current pane id", async () => {
		await expect(checkTmuxSelfInjection("tmux send-keys -t %47 x", options)).resolves.toMatchObject({ block: true });
	});

	it("blocks a session/window/pane target resolving to the current pane", async () => {
		await expect(checkTmuxSelfInjection("tmux paste-buffer -t demo:19.0", options)).resolves.toMatchObject({
			block: true,
		});
	});

	it("blocks the current-session alias after resolution", async () => {
		await expect(checkTmuxSelfInjection("tmux send-prefix -t .", options)).resolves.toMatchObject({ block: true });
	});

	it("blocks an unqualified input verb because tmux defaults to the current pane", async () => {
		await expect(checkTmuxSelfInjection("tmux send-keys x Enter", options)).resolves.toMatchObject({ block: true });
	});

	it("allows a foreign pane and preserves cross-session orchestration", async () => {
		await expect(checkTmuxSelfInjection("tmux send-keys -t other:0.0 x Enter", options)).resolves.toEqual({
			block: false,
		});
	});

	it("fails closed when a current-server target cannot be resolved", async () => {
		const unresolvedOptions = {
			...options,
			resolvePaneId: async () => undefined,
		};
		await expect(checkTmuxSelfInjection("tmux send-keys -t unknown x", unresolvedOptions)).resolves.toMatchObject({
			block: true,
		});
	});

	it("allows a different explicit socket", async () => {
		await expect(checkTmuxSelfInjection("tmux -S /tmp/other-tmux.sock send-keys -t %47 x", options)).resolves.toEqual(
			{ block: false },
		);
	});

	it("allows a different socket selected by an inline TMUX assignment", async () => {
		await expect(
			checkTmuxSelfInjection("TMUX=/tmp/other-tmux.sock tmux send-keys -t demo x", options),
		).resolves.toEqual({
			block: false,
		});
	});

	it("blocks the current server selected with -L", async () => {
		await expect(checkTmuxSelfInjection("tmux -L default send-keys -t %47 x", options)).resolves.toMatchObject({
			block: true,
		});
	});

	it("blocks the current server selected with an explicit -S path", async () => {
		await expect(
			checkTmuxSelfInjection(`tmux -S ${env.TMUX.split(",")[0]} send-keys -t %47 x`, options),
		).resolves.toMatchObject({
			block: true,
		});
	});

	it("finds injection hidden behind a shell wrapper", async () => {
		await expect(checkTmuxSelfInjection("sh -c 'tmux send-keys -t %47 x'", options)).resolves.toMatchObject({
			block: true,
		});
	});

	it("finds injection in a shell script passed to bash", async () => {
		const scriptPath = "/tmp/gjc-5039-injection.sh";
		await Bun.write(scriptPath, "tmux send-keys -t %47 x\n");
		try {
			await expect(checkTmuxSelfInjection(`bash ${scriptPath}`, options)).resolves.toMatchObject({ block: true });
		} finally {
			await Bun.file(scriptPath).delete();
		}
	});

	it("finds injection in a shell script passed to bash", async () => {
		const scriptPath = "/tmp/gjc-5039-injection.sh";
		await Bun.write(scriptPath, "tmux send-keys -t %47 x\n");
		try {
			await expect(checkTmuxSelfInjection(`bash ${scriptPath}`, options)).resolves.toMatchObject({ block: true });
		} finally {
			await Bun.file(scriptPath).delete();
		}
	});

	it("finds injection in a command chain", async () => {
		await expect(checkTmuxSelfInjection("printf safe; tmux send-keys -t %47 x", options)).resolves.toMatchObject({
			block: true,
		});
	});

	it("does not treat read-only inspection as injection", async () => {
		await expect(checkTmuxSelfInjection("tmux list-panes -t demo", options)).resolves.toEqual({ block: false });
	});
});
