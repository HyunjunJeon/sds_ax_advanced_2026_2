import { randomBytes } from "node:crypto";
import * as path from "node:path";
import { readEndpointFile } from "./endpoint-authority";
import {
	type IndexedSession,
	isSessionAuthorityEligible,
	type MasterRoleAttestationV2,
	type SessionIndex,
} from "./session-index";
import type { MasterCapabilityVerifier } from "./spawn-authority";

const MASTER_CAPABILITY_VERIFY_TIMEOUT_MS = 2_000;

export type SessionEndpoint = {
	url: string;
	token: string;
};

function processIdentity(record: Pick<IndexedSession, "hostIncarnation" | "processIncarnation">): string | undefined {
	return record.hostIncarnation ?? record.processIncarnation;
}

function matchesAttestation(left: MasterRoleAttestationV2, right: MasterRoleAttestationV2): boolean {
	return (
		left.version === right.version &&
		left.ownerSessionId === right.ownerSessionId &&
		left.launchPid === right.launchPid &&
		left.launchProcessIncarnation === right.launchProcessIncarnation &&
		left.role === right.role &&
		left.attestationEpoch === right.attestationEpoch
	);
}

function isEffectiveMasterHost(
	record: IndexedSession,
	ownerSessionId: string,
	attestationEpoch: string,
): record is IndexedSession & { masterRole: MasterRoleAttestationV2 } {
	const attestation = record.masterRole;
	return (
		record.endpointGeneration > 0 &&
		record.live &&
		!record.terminal &&
		!record.terminalUncertain &&
		isSessionAuthorityEligible(record) &&
		attestation !== undefined &&
		attestation.version === 2 &&
		attestation.role === "master" &&
		attestation.ownerSessionId === ownerSessionId &&
		attestation.attestationEpoch === attestationEpoch &&
		attestation.launchPid === record.pid &&
		attestation.launchProcessIncarnation === processIdentity(record)
	);
}

function sameAttachment(left: IndexedSession, right: IndexedSession): boolean {
	return (
		left.sessionId === right.sessionId &&
		left.endpointGeneration === right.endpointGeneration &&
		left.pid === right.pid &&
		left.endpointMtimeMs === right.endpointMtimeMs &&
		left.endpointFileId === right.endpointFileId &&
		left.hostIncarnation === right.hostIncarnation &&
		left.processIncarnation === right.processIncarnation &&
		left.indexSeq === right.indexSeq &&
		left.masterRole !== undefined &&
		right.masterRole !== undefined &&
		matchesAttestation(left.masterRole, right.masterRole)
	);
}

function endpointFileId(stat: { dev: bigint; ino: bigint }): string {
	return `${stat.dev}:${stat.ino}`;
}

function adoptedDirectAttestation(
	rows: readonly IndexedSession[],
	effective: IndexedSession & { masterRole: MasterRoleAttestationV2 },
): boolean {
	return rows.some(row => {
		const direct = row.masterRole;
		return (
			row.sessionId === effective.sessionId &&
			row.endpointGeneration === 0 &&
			row.pid === effective.masterRole.launchPid &&
			processIdentity(row) === effective.masterRole.launchProcessIncarnation &&
			direct !== undefined &&
			matchesAttestation(direct, effective.masterRole)
		);
	});
}

/** Reads and validates a registered host's authenticated endpoint record. */
export async function readEndpoint(record: IndexedSession): Promise<SessionEndpoint | undefined> {
	if (record.endpointMtimeMs === undefined || record.endpointFileId === undefined) return undefined;
	try {
		const endpointPath = path.join(record.locator.stateRoot, "sdk", `${record.sessionId}.json`);
		const file = await readEndpointFile(endpointPath);
		if (!file) return undefined;
		const endpoint = JSON.parse(file.source) as Record<string, unknown>;
		if (
			endpoint.sessionId !== record.sessionId ||
			endpoint.pid !== record.pid ||
			typeof endpoint.url !== "string" ||
			typeof endpoint.token !== "string" ||
			endpoint.url.length === 0 ||
			endpoint.token.length === 0 ||
			endpointFileId(file) !== record.endpointFileId ||
			Math.abs(file.mtimeMs - record.endpointMtimeMs) > 0.001
		)
			return undefined;
		const url = new URL(endpoint.url);
		if (url.protocol !== "ws:" && url.protocol !== "wss:") return undefined;
		return { url: endpoint.url, token: endpoint.token };
	} catch {
		return undefined;
	}
}

async function verifyOverAttachment(input: {
	endpoint: SessionEndpoint;
	nonce: string;
	attestationEpoch: string;
	capability: string;
}): Promise<boolean> {
	const settled = Promise.withResolvers<boolean>();
	let complete = false;
	const settle = (allowed: boolean): void => {
		if (complete) return;
		complete = true;
		settled.resolve(allowed);
	};
	let socket: WebSocket | undefined;
	try {
		const url = new URL(input.endpoint.url);
		url.searchParams.set("token", input.endpoint.token);
		socket = new WebSocket(url);
		const onClose = (): void => {
			settle(false);
		};
		const onError = (): void => {
			settle(false);
		};
		const onMessage = (event: MessageEvent): void => {
			let frame: Record<string, unknown>;
			try {
				const parsed = JSON.parse(String(event.data));
				if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
				frame = parsed as Record<string, unknown>;
			} catch {
				return;
			}
			if (frame.type === "hello") {
				try {
					socket?.send(JSON.stringify({ type: "hello", protocolVersion: 3, capabilities: [] }));
					socket?.send(
						JSON.stringify({
							type: "master_capability_verify",
							id: input.nonce,
							nonce: input.nonce,
							attestationEpoch: input.attestationEpoch,
							capability: input.capability,
						}),
					);
				} catch {
					settle(false);
				}
				return;
			}
			if (frame.type !== "master_capability_verify_result") return;
			settle(
				frame.id === input.nonce &&
					frame.ok === true &&
					frame.nonce === input.nonce &&
					frame.attestationEpoch === input.attestationEpoch,
			);
		};
		socket.addEventListener("error", onError);
		socket.addEventListener("close", onClose);
		socket.addEventListener("message", onMessage);
		return await Promise.race([settled.promise, Bun.sleep(MASTER_CAPABILITY_VERIFY_TIMEOUT_MS).then(() => false)]);
	} catch {
		return false;
	} finally {
		try {
			socket?.close();
		} catch {
			// Closing an already failed attachment is best effort.
		}
	}
}

/** Creates the ephemeral effective-host verifier used by broker spawn admission. */
export function createMasterCapabilityVerifier(index: SessionIndex): MasterCapabilityVerifier {
	return {
		async verifyMasterCapability(
			ownerSessionId: string,
			rawCapability: string,
			attestationEpoch: string,
		): Promise<{ allowed: boolean }> {
			try {
				await index.refresh();
				const rows = index.listSessionIdentities();
				const effective = rows.filter(row => isEffectiveMasterHost(row, ownerSessionId, attestationEpoch));
				if (effective.length !== 1) return { allowed: false };
				const attachment = effective[0]!;
				if (!adoptedDirectAttestation(rows, attachment)) return { allowed: false };
				const endpoint = await readEndpoint(attachment);
				if (!endpoint) return { allowed: false };
				await index.refresh();
				const current = index.listSessionIdentities().find(row => sameAttachment(attachment, row));
				if (!current || !isEffectiveMasterHost(current, ownerSessionId, attestationEpoch))
					return { allowed: false };
				const nonce = randomBytes(32).toString("base64url");
				const allowed = await verifyOverAttachment({
					endpoint,
					nonce,
					attestationEpoch,
					capability: rawCapability,
				});
				await index.refresh();
				const after = index.listSessionIdentities().find(row => sameAttachment(attachment, row));
				return {
					allowed:
						allowed && after !== undefined && isEffectiveMasterHost(after, ownerSessionId, attestationEpoch),
				};
			} catch {
				return { allowed: false };
			}
		},
	};
}
