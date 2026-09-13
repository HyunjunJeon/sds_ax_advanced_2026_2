import { $credentialEnv } from "@gajae-code/utils";
import type {
	CredentialRankingStrategy,
	UsageFetchContext,
	UsageFetchParams,
	UsageLimit,
	UsageProvider,
	UsageReport,
} from "../usage";

interface BillingUsage {
	monthlyLimit: number;
	used: number;
	billingPeriodEnd: string;
}

interface WeeklyBillingUsage {
	creditUsagePercent: number;
	billingPeriodEnd: string;
}

const DEFAULT_GROK_BUILD_BASE_URL = "https://cli-chat-proxy.grok.com/v1";
const ALLOWED_GROK_BUILD_HOSTS = new Set(["cli-chat-proxy.grok.com"]);
const GROK_WEEKLY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

function isRecord(value: unknown): value is Record<string, unknown> {
	return !!value && typeof value === "object";
}

function finiteNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function parseValNumber(value: unknown): number | undefined {
	return isRecord(value) ? finiteNumber(value.val) : undefined;
}

export function parseGrokCliBillingUsage(payload: unknown): BillingUsage {
	if (!isRecord(payload) || !isRecord(payload.config)) {
		throw new Error("invalid Grok CLI billing payload");
	}
	const monthlyLimit = parseValNumber(payload.config.monthlyLimit);
	const used = parseValNumber(payload.config.used);
	const billingPeriodEnd = payload.config.billingPeriodEnd;
	if (
		monthlyLimit === undefined ||
		used === undefined ||
		typeof billingPeriodEnd !== "string" ||
		!Number.isFinite(new Date(billingPeriodEnd).getTime())
	) {
		throw new Error("invalid Grok CLI billing payload");
	}
	return { monthlyLimit, used, billingPeriodEnd };
}

export function parseGrokCliWeeklyBillingUsage(payload: unknown): WeeklyBillingUsage | undefined {
	if (!isRecord(payload) || !isRecord(payload.config)) return undefined;
	const currentPeriod = isRecord(payload.config.currentPeriod) ? payload.config.currentPeriod : undefined;
	if (currentPeriod?.type !== "USAGE_PERIOD_TYPE_WEEKLY") return undefined;

	const billingPeriodEnd = payload.config.billingPeriodEnd;
	if (typeof billingPeriodEnd !== "string" || !Number.isFinite(new Date(billingPeriodEnd).getTime())) {
		return undefined;
	}

	const rawCreditUsagePercent = payload.config.creditUsagePercent;
	const creditUsagePercent = rawCreditUsagePercent === undefined ? 0 : finiteNumber(rawCreditUsagePercent);
	if (creditUsagePercent === undefined) return undefined;

	return { creditUsagePercent, billingPeriodEnd };
}

function isAllowedGrokCredentialHost(baseUrl: string): boolean {
	try {
		const url = new URL(baseUrl);
		return url.protocol === "https:" && ALLOWED_GROK_BUILD_HOSTS.has(url.hostname.toLowerCase());
	} catch {
		return false;
	}
}

function normalizeGrokBaseUrl(baseUrl?: string): string {
	const normalized = (baseUrl?.trim() || DEFAULT_GROK_BUILD_BASE_URL).replace(/\/+$/, "");
	return isAllowedGrokCredentialHost(normalized) ? normalized : DEFAULT_GROK_BUILD_BASE_URL;
}
function isUnsafeGrokBaseUrlOverride(baseUrl?: string): boolean {
	const normalized = baseUrl?.trim().replace(/\/+$/, "");
	return !!normalized && !isAllowedGrokCredentialHost(normalized);
}

function resolveAccessToken(params: UsageFetchParams): string | undefined {
	// Trusted sources only for the env fallback: this token authenticates the
	// billing/usage call, so whatever can set it decides which account is queried
	// with it. `$env` merges the caller's `cwd/.env` into `process.env`, so
	// reading it there would let repository content supply the credential.
	// Stored credentials keep precedence.
	const token = params.credential.accessToken ?? params.credential.apiKey ?? $credentialEnv("GROK_CLI_OAUTH_TOKEN");
	return token?.trim() || undefined;
}

/** Test seam: the usage access token as resolved from a credential plus trusted env. */
export function resolveGrokAccessTokenForTest(params: UsageFetchParams): string | undefined {
	return resolveAccessToken(params);
}

function buildMonthlyUsageLimit(usage: BillingUsage, nowMs: number): UsageLimit {
	const usedFraction = usage.monthlyLimit > 0 ? usage.used / usage.monthlyLimit : 0;
	const percent = usedFraction * 100;
	const resetsAt = new Date(usage.billingPeriodEnd).getTime();
	return {
		id: "grok-build:7d",
		label: "SuperGrok monthly credits",
		scope: { provider: "grok-build", shared: true, windowId: "7d" },
		window: {
			id: "7d",
			label: "Monthly credits",
			resetsAt,
		},
		amount: {
			unit: "percent",
			used: percent,
			limit: 100,
			remaining: Math.max(0, 100 - percent),
			usedFraction,
			remainingFraction: Math.max(0, 1 - usedFraction),
		},
		status: percent >= 95 ? "exhausted" : percent >= 80 ? "warning" : "ok",
		notes: [
			`${usage.used}/${usage.monthlyLimit} credits used`,
			`resets in ${Math.max(0, Math.round((resetsAt - nowMs) / 3_600_000))}h`,
		],
	};
}

function buildWeeklyUsageLimit(usage: WeeklyBillingUsage): UsageLimit {
	const percent = Math.min(Math.max(usage.creditUsagePercent, 0), 100);
	const usedFraction = percent / 100;
	const resetsAt = new Date(usage.billingPeriodEnd).getTime();
	return {
		id: "grok-build:weekly",
		label: "SuperGrok weekly credits",
		scope: { provider: "grok-build", shared: true, windowId: "weekly" },
		window: {
			id: "weekly",
			label: "Weekly",
			durationMs: GROK_WEEKLY_WINDOW_MS,
			resetsAt,
		},
		amount: {
			unit: "percent",
			used: percent,
			limit: 100,
			remaining: 100 - percent,
			usedFraction,
			remainingFraction: 1 - usedFraction,
		},
		status: percent >= 95 ? "exhausted" : percent >= 80 ? "warning" : "ok",
		notes: [`${percent}% used`],
	};
}

export const grokCliUsageProvider: UsageProvider = {
	id: "grok-build",

	supports(params) {
		return params.provider === "grok-build";
	},

	async fetchUsage(params: UsageFetchParams, ctx: UsageFetchContext): Promise<UsageReport | null> {
		const accessToken = resolveAccessToken(params);
		if (!accessToken) {
			ctx.logger?.warn("Grok Build usage: no access token", { provider: params.provider });
			return null;
		}

		if (isUnsafeGrokBaseUrlOverride(params.baseUrl)) {
			ctx.logger?.warn("Grok Build usage: ignoring unsafe base URL override for credential safety", {
				provider: params.provider,
			});
		}
		const billingBaseUrl = normalizeGrokBaseUrl(params.baseUrl);
		const response = await ctx.fetch(`${billingBaseUrl}/billing`, {
			headers: {
				Authorization: `Bearer ${accessToken}`,
				"x-xai-token-auth": "xai-grok-cli",
				accept: "application/json",
			},
			signal: params.signal,
		});
		if (!response.ok) {
			ctx.logger?.warn("Grok Build billing request failed", { status: response.status, provider: params.provider });
			return null;
		}

		const payload = (await response.json()) as unknown;
		let billing: BillingUsage;
		try {
			billing = parseGrokCliBillingUsage(payload);
		} catch (error) {
			ctx.logger?.warn("Grok Build billing parse failed", { error: String(error) });
			return null;
		}

		const nowMs = Date.now();
		let weekly: WeeklyBillingUsage | undefined;
		try {
			const weeklyResponse = await ctx.fetch(`${billingBaseUrl}/billing?format=credits`, {
				headers: {
					Authorization: `Bearer ${accessToken}`,
					"x-xai-token-auth": "xai-grok-cli",
					accept: "application/json",
				},
				signal: params.signal,
			});
			if (weeklyResponse.ok) {
				weekly = parseGrokCliWeeklyBillingUsage((await weeklyResponse.json()) as unknown);
			}
		} catch (error) {
			if (params.signal?.aborted && billing.monthlyLimit <= 0) throw error;
			ctx.logger?.debug("Grok Build weekly billing request failed", { error: String(error) });
		}

		const limits: UsageLimit[] = [];
		if (billing.monthlyLimit > 0) {
			limits.push(buildMonthlyUsageLimit(billing, nowMs));
		}
		if (weekly) {
			limits.push(buildWeeklyUsageLimit(weekly));
		}
		if (limits.length === 0) {
			ctx.logger?.warn("Grok Build billing response contained no usable quota", {
				provider: params.provider,
			});
			return null;
		}

		return {
			provider: "grok-build",
			fetchedAt: nowMs,
			limits,
			metadata: {
				email: params.credential.email,
				accountId: params.credential.accountId,
				subscription: true,
			},
			raw: payload,
		};
	},
};

export const grokCliRankingStrategy: CredentialRankingStrategy = {
	findWindowLimits(report) {
		const weekly = report.limits.find(limit => limit.id === "grok-build:weekly");
		if (weekly) return { secondary: weekly };
		const monthly = report.limits.find(limit => limit.id === "grok-build:7d");
		return { secondary: monthly };
	},
	windowDefaults: { primaryMs: 5 * 60 * 60 * 1000, secondaryMs: 30 * 24 * 60 * 60 * 1000 },
};
