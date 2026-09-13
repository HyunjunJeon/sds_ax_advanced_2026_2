import {
	persistCoordinatorRuntimeStateFromEvent,
	prepareCoordinatorRuntimeStateRescope,
	recoverCoordinatorRuntimeStateRescope,
	relocateCoordinatorRuntimeStateForRescope,
} from "../../src/gjc-runtime/session-state-sidecar";

const stateFile = process.argv[2];
if (!stateFile) throw new Error("state file required");
if (process.argv[3] === "prepare-journal") {
	const previousCwd = process.argv[4];
	const newCwd = process.argv[5];
	if (!previousCwd || !newCwd) throw new Error("previous and new cwd required");
	await prepareCoordinatorRuntimeStateRescope({
		sessionId: "155-FinalA4",
		previousCwd,
		newCwd,
		previousSessionFile: null,
		newSessionFile: null,
	});
	process.exit(0);
}
if (process.argv[3] === "recover") {
	const newCwd = process.argv[4];
	if (!newCwd) throw new Error("new cwd required");
	await recoverCoordinatorRuntimeStateRescope({ sessionId: "155-FinalA4", cwd: newCwd, sessionFile: null });
	process.exit(0);
}
if (process.argv[3] === "relocate") {
	const previousCwd = process.argv[4];
	const newCwd = process.argv[5];
	if (!previousCwd || !newCwd) throw new Error("previous and new cwd required");
	const completed = await relocateCoordinatorRuntimeStateForRescope(
		{ sessionId: "155-FinalA4", cwd: newCwd, sessionFile: null },
		previousCwd,
	);
	if (!completed) throw new Error("relocation refused");
	process.exit(0);
}
const context = { sessionId: "155-FinalA4", cwd: process.cwd(), sessionFile: null };
await persistCoordinatorRuntimeStateFromEvent({ type: "agent_start" }, context);
await persistCoordinatorRuntimeStateFromEvent({ type: "tool_execution_start", toolCallId: "fixture-call" }, context, {
	label: "bash",
	observedAt: "2026-08-20T00:00:01.000Z",
});
await persistCoordinatorRuntimeStateFromEvent({ type: "tool_execution_end", toolCallId: "fixture-call" }, context, {
	label: "bash",
	observedAt: "2026-08-20T00:00:02.000Z",
});
await persistCoordinatorRuntimeStateFromEvent(
	{
		type: "agent_end",
		messages: [{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "fixture" }] }],
	},
	context,
);
