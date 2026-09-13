import { Command } from "@gajae-code/utils/cli";
import { ensureWorkflowSettingsMigrated } from "../config/settings";
import {
	assertExplicitTargetGjcNotSymlinked,
	type RalplanCommandResult,
	resolveRalplanTargetRoot,
	runNativeRalplanCommand,
} from "../gjc-runtime/ralplan-runtime";
import { CommandError } from "../gjc-runtime/workflow-cli-common";

/** Public CLI path: validate --worktree-root before any settings migration, then dispatch. */
export async function runRalplanCliCommand(args: string[], cwd: string): Promise<RalplanCommandResult> {
	try {
		const target = await resolveRalplanTargetRoot(args, cwd);
		if (target.explicit) await assertExplicitTargetGjcNotSymlinked(target.root);
		await ensureWorkflowSettingsMigrated(target.root);
	} catch (error) {
		if (error instanceof CommandError) return { status: error.exitStatus, stderr: `${error.message}\n` };
		throw error;
	}
	return await runNativeRalplanCommand(args, cwd);
}

export default class Ralplan extends Command {
	static description = "Run native GJC RALPLAN consensus planning workflow";
	static strict = false;
	static examples = [
		'$ gjc ralplan "<task description>"',
		'$ gjc ralplan --interactive --deliberate "<task description>"',
		'$ gjc ralplan --write --stage planner --stage_n 1 --artifact "<markdown or path>"',
		"$ gjc ralplan --write --stage critic --stage_n 1 --artifact-env GJC_RALPLAN_ARTIFACT",
		'$ gjc ralplan --worktree-root /abs/path/to/target-worktree "<task description>"',
		"$ gjc ralplan --write --worktree-root /abs/path/to/target-worktree --session-id <owner> --run-id <run> --stage critic --stage_n 1 --artifact-env GJC_RALPLAN_ARTIFACT",
	];

	async run(): Promise<void> {
		const result = await runRalplanCliCommand(this.argv, process.cwd());
		if (result.stdout) process.stdout.write(result.stdout);
		if (result.stderr) process.stderr.write(result.stderr);
		process.exitCode = result.status;
	}
}
