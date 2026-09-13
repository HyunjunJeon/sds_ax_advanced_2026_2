import { pythonKernelTranscriptPath } from "./session-layout";
import { writeLogJsonl } from "./state-writer";

export interface PythonTranscriptRecord {
	timestamp: string;
	code: string;
	output: string;
	exitCode: number | null;
	cancelled: boolean;
	truncated: boolean;
}

export interface PythonKernelTranscript {
	readonly dir: string;
	readonly kernelInstanceId: string;
	append(record: PythonTranscriptRecord): Promise<void>;
}

function transcriptDirectoryName(now: Date, kernelInstanceId: string): string {
	const yyyy = now.getUTCFullYear().toString().padStart(4, "0");
	const mm = (now.getUTCMonth() + 1).toString().padStart(2, "0");
	const dd = now.getUTCDate().toString().padStart(2, "0");
	const hh = now.getUTCHours().toString().padStart(2, "0");
	const min = now.getUTCMinutes().toString().padStart(2, "0");
	const ss = now.getUTCSeconds().toString().padStart(2, "0");
	return `${yyyy}${mm}${dd}T${hh}${min}${ss}Z-${kernelInstanceId}`;
}

export function openPythonKernelTranscript(input: {
	cwd: string;
	sessionId: string;
	kernelInstanceId: string;
	now?: Date;
}): PythonKernelTranscript {
	const dir = transcriptDirectoryName(input.now ?? new Date(), input.kernelInstanceId);
	const transcriptPath = pythonKernelTranscriptPath(input.cwd, input.sessionId, dir);
	let queue: Promise<void> = Promise.resolve();

	return {
		dir,
		kernelInstanceId: input.kernelInstanceId,
		append(record: PythonTranscriptRecord): Promise<void> {
			const write = queue.then(async () => {
				await writeLogJsonl(transcriptPath, record, {
					cwd: input.cwd,
					audit: {
						category: "log",
						verb: "append",
						owner: "gjc-runtime",
						sessionId: input.sessionId,
					},
				});
			});
			queue = write.catch(() => undefined);
			return write;
		},
	};
}
