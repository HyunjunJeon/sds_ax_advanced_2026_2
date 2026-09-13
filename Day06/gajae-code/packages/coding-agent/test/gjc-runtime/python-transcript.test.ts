import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { openPythonKernelTranscript, type PythonTranscriptRecord } from "../../src/gjc-runtime/python-transcript";
import { auditPath, pythonKernelTranscriptPath, sessionRoot } from "../../src/gjc-runtime/session-layout";

const TEST_SESSION_ID = "python-transcript-session";
const tempRoots: string[] = [];

async function tempDir(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "gjc-python-transcript-"));
	tempRoots.push(dir);
	return dir;
}

async function readJsonLines<T>(filePath: string): Promise<T[]> {
	const text = await fs.readFile(filePath, "utf-8");
	return text
		.split(/\r?\n/)
		.filter(Boolean)
		.map(line => JSON.parse(line) as T);
}

function record(code: string): PythonTranscriptRecord {
	return {
		timestamp: "2026-08-19T12:34:56.000Z",
		code,
		output: `output:${code}`,
		exitCode: 0,
		cancelled: false,
		truncated: false,
	};
}

afterEach(async () => {
	await Promise.all(tempRoots.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

describe("python kernel transcript", () => {
	it("names each kernel directory with a UTC second stamp and the kernel instance id", () => {
		const transcript = openPythonKernelTranscript({
			cwd: "/project",
			sessionId: TEST_SESSION_ID,
			kernelInstanceId: "kernel-instance-42",
			now: new Date("2026-08-19T03:04:05.987Z"),
		});

		expect(transcript.dir).toBe("20260819T030405Z-kernel-instance-42");
	});

	it("serializes interleaved appends in invocation order", async () => {
		const cwd = await tempDir();
		const transcript = openPythonKernelTranscript({
			cwd,
			sessionId: TEST_SESSION_ID,
			kernelInstanceId: "kernel-ordering",
			now: new Date("2026-08-19T03:04:05.000Z"),
		});

		const first = transcript.append(record("first"));
		const second = transcript.append(record("second"));
		const third = transcript.append(record("third"));
		await Promise.all([first, second, third]);

		const transcriptPath = pythonKernelTranscriptPath(cwd, TEST_SESSION_ID, transcript.dir);
		const records = await readJsonLines<PythonTranscriptRecord>(transcriptPath);
		expect(records.map(entry => entry.code)).toEqual(["first", "second", "third"]);
	});

	it("writes transcript and audit records under the session-scoped state root through the state writer", async () => {
		const cwd = await tempDir();
		const transcript = openPythonKernelTranscript({
			cwd,
			sessionId: TEST_SESSION_ID,
			kernelInstanceId: "kernel-audit",
			now: new Date("2026-08-19T03:04:05.000Z"),
		});
		await transcript.append(record("audit-me"));

		const transcriptPath = pythonKernelTranscriptPath(cwd, TEST_SESSION_ID, transcript.dir);
		expect(transcriptPath).toBe(
			path.join(
				sessionRoot(cwd, TEST_SESSION_ID),
				"ipykernels",
				"20260819T030405Z-kernel-audit",
				"transcript.jsonl",
			),
		);
		expect(await readJsonLines<PythonTranscriptRecord>(transcriptPath)).toEqual([record("audit-me")]);

		const auditEntries = await readJsonLines<{
			category: string;
			verb: string;
			owner: string;
			paths: string[];
		}>(auditPath(cwd, TEST_SESSION_ID));
		expect(auditEntries).toContainEqual(
			expect.objectContaining({
				category: "log",
				verb: "append",
				owner: "gjc-runtime",
				paths: [transcriptPath],
			}),
		);
	});

	it("rejects transcript directory components that contain separators or traversal", () => {
		for (const unsafe of ["../escape", "..", "a/b", "a\\b", "", "  "]) {
			expect(() => pythonKernelTranscriptPath("/tmp/x", TEST_SESSION_ID, unsafe)).toThrow();
		}
		// A well-formed {datetime}-{uuid} component stays inside ipykernels/.
		const ok = pythonKernelTranscriptPath("/tmp/x", TEST_SESSION_ID, "20260819T000000Z-abc123");
		expect(ok).toContain(path.join("ipykernels", "20260819T000000Z-abc123", "transcript.jsonl"));
	});
});
