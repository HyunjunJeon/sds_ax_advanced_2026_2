/** Process-wide emergency stop; it cannot be enabled by project dotenv files. */
export const TELEMETRY_KILL_SWITCH_ENV = "GJC_DISABLE_TELEMETRY" as const;

function isTruthy(value: string | undefined): boolean {
	return value === "1" || value === "true" || value === "yes" || value === "on";
}

export interface TelemetryStatus {
	enabled: boolean;
	disabledByKillSwitch: boolean;
	reason: "enabled" | "setting_disabled" | "kill_switch";
}

export interface TelemetrySettingsReader {
	get(path: "telemetry.enabled"): boolean;
}

export function getTelemetryStatus(settings: TelemetrySettingsReader): TelemetryStatus {
	if (isTruthy(process.env[TELEMETRY_KILL_SWITCH_ENV])) {
		return { enabled: false, disabledByKillSwitch: true, reason: "kill_switch" };
	}
	if (settings.get("telemetry.enabled") !== true) {
		return { enabled: false, disabledByKillSwitch: false, reason: "setting_disabled" };
	}
	return { enabled: true, disabledByKillSwitch: false, reason: "enabled" };
}
