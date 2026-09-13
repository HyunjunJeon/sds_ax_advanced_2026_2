import { RateLimitPool } from "./rate-limit-pool";
import { renderThreadedFrame, type ThreadedSend } from "./threaded-render";

/** Shared presentation engine for managed notification providers. */
export interface ManagedNotificationDaemonOptions {
	now?: () => number;
	rateLimitPool?: RateLimitPool<{ send: ThreadedSend; topicId?: string }>;
}

export abstract class ManagedNotificationDaemon {
	readonly pool: RateLimitPool<{ send: ThreadedSend; topicId?: string }>;

	protected constructor(opts: ManagedNotificationDaemonOptions = {}) {
		this.pool = opts.rateLimitPool ?? new RateLimitPool<{ send: ThreadedSend; topicId?: string }>({ now: opts.now });
	}

	protected renderFrame(frame: Record<string, unknown>): ThreadedSend | undefined {
		return renderThreadedFrame(frame);
	}
}
