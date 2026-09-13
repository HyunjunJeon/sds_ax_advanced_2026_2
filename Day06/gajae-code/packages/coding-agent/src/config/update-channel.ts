/**
 * Release-channel primitives shared by the self-update command and the
 * startup update check.
 *
 * Kept in the config layer (no shell, theme, or updater imports) so the
 * startup path in main.ts can resolve the channel without pulling in the
 * updater implementation.
 */
import type { Settings } from "./settings";

export const UPDATE_CHANNELS = ["stable", "nightly"] as const;
export type UpdateChannel = (typeof UPDATE_CHANNELS)[number];

export function isUpdateChannel(value: string): value is UpdateChannel {
	return (UPDATE_CHANNELS as readonly string[]).includes(value);
}

/** npm dist-tag backing each release channel. `latest` is the stable tag; nightly publishes never move it. */
export function distTagForChannel(channel: UpdateChannel): string {
	return channel === "nightly" ? "nightly" : "latest";
}

/**
 * Resolve the machine-local update channel: read the user/global layer only (a
 * project `.gjc/config.yml` `startup.updateChannel` override must never pick
 * the global release channel) and fall back to the stable schema default when
 * the global value is unset or invalid. Used by both the `gjc update` command
 * and the startup update check so a nightly notification can always be
 * satisfied by the same default invocation.
 */
export function resolveMachineLocalUpdateChannel(settings: Settings): UpdateChannel {
	const configured = settings.getGlobal("startup.updateChannel");
	if (configured !== undefined && isUpdateChannel(configured)) return configured;
	if (configured !== undefined) {
		// A hand-edited invalid global value degrades to the schema default
		// instead of leaking into output or the registry lookup.
		process.stderr.write(
			`Ignoring invalid startup.updateChannel "${configured}". Expected one of: ${UPDATE_CHANNELS.join(", ")}; using stable.\n`,
		);
	}
	return "stable";
}
