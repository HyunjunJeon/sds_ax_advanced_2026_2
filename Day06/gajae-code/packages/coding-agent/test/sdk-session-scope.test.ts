import { expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import path from "node:path";
import { resolveSessionLocator } from "../src/sdk/broker/session-index";
import { resolveScopeRequest } from "../src/sdk/broker/session-scope";

const temp = () => fs.mkdtemp(path.join(process.env.TMPDIR ?? "/tmp", "gjc-session-scope-"));

test("resolves canonical pwd, repo, global, and non-Git repo scope requests", async () => {
	const directory = await temp();
	const git = Bun.spawn(["git", "init", "-q", directory]);
	await git.exited;
	const locator = await resolveSessionLocator(directory, "/state");
	const repoWorktree = locator.worktreeRoot;
	if (repoWorktree === null) throw new Error("git worktree was not resolved");
	const anchor = { cwd: locator.cwd, worktreeRoot: repoWorktree };

	expect(await resolveScopeRequest({ version: 1, requested: "pwd", requestAnchor: anchor })).toEqual({
		version: 1,
		requested: "pwd",
		requestAnchor: anchor,
		resolved: { kind: "pwd", cwd: locator.cwd },
		resolution: "resolved",
	});
	expect(await resolveScopeRequest({ version: 1, requested: "repo", requestAnchor: anchor })).toEqual({
		version: 1,
		requested: "repo",
		requestAnchor: anchor,
		resolved: { kind: "repo", worktreeRoot: repoWorktree },
		resolution: "resolved",
	});
	expect(await resolveScopeRequest({ version: 1, requested: "global", requestAnchor: anchor })).toEqual({
		version: 1,
		requested: "global",
		requestAnchor: anchor,
		resolved: { kind: "global", visibility: "current-broker" },
		resolution: "resolved",
	});

	const nonGit = await temp();
	const nonGitLocator = await resolveSessionLocator(nonGit, "/state");
	expect(
		await resolveScopeRequest({
			version: 1,
			requested: "repo",
			requestAnchor: { cwd: nonGitLocator.cwd, worktreeRoot: null },
		}),
	).toEqual({
		version: 1,
		requested: "repo",
		requestAnchor: { cwd: nonGitLocator.cwd, worktreeRoot: null },
		resolved: null,
		resolution: "not-in-git-worktree",
	});
});

test("canonicalizes symlinked anchors to one worktree identity", async () => {
	const directory = await temp();
	const git = Bun.spawn(["git", "init", "-q", directory]);
	await git.exited;
	const alias = `${directory}-alias`;
	await fs.symlink(directory, alias);
	const canonical = await resolveSessionLocator(directory, "/state");
	const canonicalWorktree = canonical.worktreeRoot;
	if (canonicalWorktree === null) throw new Error("git worktree was not resolved");
	const aliased = await resolveScopeRequest({
		version: 1,
		requested: "repo",
		requestAnchor: { cwd: alias, worktreeRoot: canonicalWorktree },
	});
	expect(aliased.requestAnchor).toEqual({ cwd: canonical.cwd, worktreeRoot: canonicalWorktree });
});
