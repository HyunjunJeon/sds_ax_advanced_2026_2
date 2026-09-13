import * as fs from "node:fs";
import { SessionManager } from "../../src/session/session-manager";

function rotate(manager: SessionManager): void {
	const file = manager.getSessionFile();
	if (!file) throw new Error("Expected resumed transcript");
	fs.copyFileSync(file, `${file}.before-rejection`);
	fs.copyFileSync(file, `${file}.test-successor`);
	fs.renameSync(`${file}.test-successor`, file);
}

// Exercise both the startup model-profile persistence catch and the live prompt
// boundary, without depending on scheduler timing between competing processes.
if (process.env.GJC_TEST_ROTATION_PHASE === "startup") {
	const append = SessionManager.prototype.appendModelChange;
	SessionManager.prototype.appendModelChange = function (...args: Parameters<SessionManager["appendModelChange"]>) {
		rotate(this);
		SessionManager.prototype.appendModelChange = append;
		return append.apply(this, args);
	};
} else {
	const append = SessionManager.prototype.appendMessage;
	SessionManager.prototype.appendMessage = function (...args: Parameters<SessionManager["appendMessage"]>) {
		if (args[0].role === "user") {
			rotate(this);
			SessionManager.prototype.appendMessage = append;
		}
		return append.apply(this, args);
	};
}
