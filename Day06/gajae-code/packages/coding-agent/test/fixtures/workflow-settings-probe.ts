/**
 * Prints the resolved workflow setting for the current working directory.
 * Directory/env resolution happens at module load, so this must be a child
 * process when HOME/GJC_CONFIG_DIR/GJC_CODING_AGENT_DIR need isolation.
 * Pass `--strict` to resolve with the throw invalid policy (ralplan contract).
 */
import { resolveWorkflowSetting, type WorkflowSettingKey } from "../../src/gjc-runtime/workflow-settings";

const cwd = process.cwd();
const key = process.argv[2] as WorkflowSettingKey;
const strict = process.argv.includes("--strict");
const agentDirIndex = process.argv.indexOf("--agent-dir");
const agentDir = agentDirIndex >= 0 ? process.argv[agentDirIndex + 1] : undefined;
try {
	const result = await resolveWorkflowSetting(cwd, key, {
		defaultValue: "default",
		parse: (value: unknown) => ({ kind: "valid" as const, value }),
		...(strict ? { invalidPolicy: "throw" as const } : {}),
		...(agentDir ? { agentDir } : {}),
	});
	process.stdout.write(
		`${JSON.stringify({ value: result.value, source: result.source, diagnostics: result.diagnostics })}
`,
	);
} catch (error) {
	process.stdout.write(
		`${JSON.stringify({
			value: null,
			threw: true,
			message: error instanceof Error ? error.message : String(error),
		})}
`,
	);
}
