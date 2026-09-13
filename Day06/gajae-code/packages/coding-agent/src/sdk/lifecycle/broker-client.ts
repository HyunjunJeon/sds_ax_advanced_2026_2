import { logger } from "@gajae-code/utils";
import { ensureBroker } from "../broker/ensure";
import { SdkClient } from "../client/client";
import { readSdkBrokerDiscovery } from "../client/discovery";
import {
	type SessionLifecycleClient,
	type SessionLifecycleClientRequestOptions,
	type SessionLifecycleOperation,
	SessionLifecycleService,
} from "./service";

/** SDK-core Broker client that keeps Broker credentials inside the lifecycle boundary. */
export class AgentDirSessionLifecycleClient implements SessionLifecycleClient {
	readonly #agentDir: string;

	constructor(agentDir: string) {
		this.#agentDir = agentDir;
	}

	async global(
		operation: SessionLifecycleOperation,
		input: Record<string, unknown>,
		options: SessionLifecycleClientRequestOptions,
	): Promise<unknown> {
		await ensureBroker({ agentDir: this.#agentDir });
		const discovery = await readSdkBrokerDiscovery(this.#agentDir);
		if (!discovery) throw new Error("SDK broker discovery is unavailable.");
		const client = await SdkClient.connect(discovery.url, discovery.token, {
			...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
		});
		let result: unknown;
		try {
			result = await client.global(operation, input, {
				...(options.idempotencyKey === undefined ? {} : { idempotencyKey: options.idempotencyKey }),
				...(options.timeoutMs === undefined ? {} : { timeoutMs: options.timeoutMs }),
			});
		} finally {
			try {
				await client.close();
			} catch (error) {
				logger.warn("SDK lifecycle client cleanup failed", {
					operation,
					error: error instanceof Error ? error.message : String(error),
				});
			}
		}
		return result;
	}
}

export function createBrokerSessionLifecycleService(agentDir: string): SessionLifecycleService {
	return new SessionLifecycleService(new AgentDirSessionLifecycleClient(agentDir));
}

/**
 * Dedicated dispatch for the local master `session.spawn` surface. It shares
 * the lifecycle client's credential boundary but bypasses the generic mutation
 * outcome types: spawn responses are validated by the caller's allowlist.
 */
export async function dispatchSpawnGlobal(
	agentDir: string,
	input: Record<string, unknown>,
	idempotencyKey: string,
	timeoutMs: number,
): Promise<unknown> {
	await ensureBroker({ agentDir });
	const discovery = await readSdkBrokerDiscovery(agentDir);
	if (!discovery) throw new Error("SDK broker discovery is unavailable.");
	const client = await SdkClient.connect(discovery.url, discovery.token, { timeoutMs });
	try {
		return await client.global("session.spawn", input, { idempotencyKey, timeoutMs });
	} finally {
		try {
			await client.close();
		} catch (error) {
			logger.warn("SDK spawn client cleanup failed", {
				error: error instanceof Error ? error.message : String(error),
			});
		}
	}
}
