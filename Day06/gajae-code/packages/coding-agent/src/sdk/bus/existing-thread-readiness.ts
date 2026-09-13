/**
 * The prepare → bind → activate lifecycle for adopting an existing chat root.
 *
 * Stock startup publishes a session's readiness immediately, so the running
 * chat daemon surfaces the session and creates its own root before an operator
 * could ever name an existing one. A session that opts in is *prepared*
 * instead: readiness stays withheld while the provider applies its one root
 * claim. The SessionRouter owns endpoint authority and activation; this module
 * only proves that the provider mapping authorizes readiness publication.
 *
 * Nothing here mutates a mapping. The gate only reads the daemon-owned store to
 * prove a binding exists for the exact session and endpoint generation.
 */

import type { SessionActivationGate } from "../host";
import type { ConversationStore } from "./conversation-store";
import type { SlackConversation } from "./slack-conversation";

/** Explicit, session-scoped opt-in for withholding readiness until a root is bound. */
export const EXISTING_THREAD_BIND_ENV = "GJC_NOTIFY_BIND_EXISTING_THREAD";

/**
 * Existing-thread binding is opt-in, and only the exact value `1` opts in.
 *
 * Every other value — absent, empty, `0`, or anything truthy-looking — keeps
 * the stock immediate-readiness contract, so no session silently loses its
 * root publication because of an ambiguous environment.
 */
export function isExistingThreadBindingRequested(env: NodeJS.ProcessEnv = process.env): boolean {
	return env[EXISTING_THREAD_BIND_ENV] === "1";
}

export interface SlackBindingActivationInput {
	store: ConversationStore<SlackConversation>;
	teamId: string;
	channelId: string;
}

/**
 * Authorize activation only against an applied binding for this exact session.
 *
 * The proof is the daemon-owned mapping itself: an active record for this
 * session, in the configured workspace and channel, carrying a root and the
 * exact endpoint generation being activated. A missing, foreign, or
 * stale-generation mapping is a refusal, and an unreadable store raises so the
 * caller reports unavailable authority instead of an authorization.
 */
export function createSlackBindingActivationGate(input: SlackBindingActivationInput): SessionActivationGate {
	return async ({ sessionId, generation }) => {
		const document = await input.store.load();
		return Object.values(document.conversations).some(
			record =>
				record.state === "active" &&
				record.sessionId === sessionId &&
				record.teamId === input.teamId &&
				record.channelId === input.channelId &&
				typeof record.rootTs === "string" &&
				record.rootTs.length > 0 &&
				record.endpointGeneration === generation,
		);
	};
}
