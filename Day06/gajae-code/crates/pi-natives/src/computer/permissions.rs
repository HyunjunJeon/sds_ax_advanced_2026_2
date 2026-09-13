//! macOS TCC permission preflight for computer-use (macOS).
//!
//! # Overview
//! Two distinct TCC permissions gate the computer tool:
//! - **Screen & System Audio Recording** — required for `screenshot` capture
//!   (see [`super::capture`]).
//! - **Accessibility** — required for input injection (click/type/etc.). This
//!   is a *separate* grant from screen capture.
//!
//! This module performs non-prompting preflight checks and can open the correct
//! System Settings pane so the user can grant a missing permission, then retry.
//! It never injects input and never blocks; callers gate side effects on
//! [`preflight`] and surface [`PermissionError`] when a required grant is
//! missing rather than acting on a stale assumption. TCC grants are evaluated
//! for the current executable identity, so diagnostics include the actual
//! launcher path and pid instead of implying that a grant for Terminal, Bun,
//! or a previous GJC binary applies automatically.

use std::process::Command;

#[link(name = "ApplicationServices", kind = "framework")]
unsafe extern "C" {
	/// Returns whether the current process is a trusted Accessibility client
	/// (no prompt). Equivalent to `AXIsProcessTrustedWithOptions(NULL)`.
	fn AXIsProcessTrusted() -> bool;
}

#[link(name = "CoreGraphics", kind = "framework")]
unsafe extern "C" {
	/// Returns whether the current process already has Screen Recording access,
	/// without prompting.
	fn CGPreflightScreenCaptureAccess() -> bool;
}

/// A TCC permission the computer tool depends on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TccPermission {
	/// Accessibility — required for input injection.
	Accessibility,
	/// Screen & System Audio Recording — required for screen capture.
	ScreenRecording,
}

impl TccPermission {
	/// The `x-apple.systempreferences:` URL for this permission's settings pane.
	#[must_use]
	pub const fn settings_url(self) -> &'static str {
		match self {
			Self::Accessibility => {
				"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
			},
			Self::ScreenRecording => {
				"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
			},
		}
	}
}

/// Current grant state for the permissions the computer tool needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreflightStatus {
	/// Whether Accessibility (input injection) is granted.
	pub accessibility:    bool,
	/// Whether Screen & System Audio Recording (capture) is granted.
	pub screen_recording: bool,
}

/// Error returned when a required permission is missing. Carries the offending
/// permission so the caller can open the right Settings pane and ask the user
/// to grant it, then retry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PermissionError {
	/// The missing permission.
	pub missing: TccPermission,
}

impl std::fmt::Display for PermissionError {
	fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
		let (name, what) = match self.missing {
			TccPermission::Accessibility => ("Accessibility", "inject input"),
			TccPermission::ScreenRecording => {
				("Screen & System Audio Recording", "capture the screen")
			},
		};
		write!(
			f,
			"COMPUTER_PERMISSION_REQUIRED: {name} permission is required to {what}. Grant it in \
			 System Settings for the current GJC launcher, then fully quit and relaunch GJC before \
			 retrying. {}",
			current_process_diagnostic()
		)
	}
}

impl std::error::Error for PermissionError {}

/// Whether the process is a trusted Accessibility client (no prompt).
#[must_use]
pub fn accessibility_granted() -> bool {
	// SAFETY: `AXIsProcessTrusted` takes no arguments and only reads the current
	// process's TCC trust state.
	unsafe { AXIsProcessTrusted() }
}

/// Whether the process already has Screen & System Audio Recording access (no
/// prompt).
#[must_use]
pub fn screen_recording_granted() -> bool {
	// SAFETY: `CGPreflightScreenCaptureAccess` takes no arguments and only reads
	// the current process's TCC access state.
	unsafe { CGPreflightScreenCaptureAccess() }
}

/// Identify the process whose TCC grant is being queried.
///
/// macOS privacy grants are evaluated for the current executable's code
/// identity. The path is especially important in development, where GJC may
/// run as Bun, a compiled ad-hoc binary, or a launcher from another terminal.
#[must_use]
pub fn current_process_diagnostic() -> String {
	let executable = std::env::current_exe()
		.map_or_else(|_| "<unavailable>".to_string(), |path| path.display().to_string());
	format!("TCC identity: executable={executable}, pid={}", std::process::id())
}

/// Read the current grant state for both required permissions.
#[must_use]
pub fn preflight() -> PreflightStatus {
	PreflightStatus {
		accessibility:    accessibility_granted(),
		screen_recording: screen_recording_granted(),
	}
}

/// Open the System Settings pane for `permission` via `open(1)`. Best-effort;
/// returns whether the launch was spawned successfully.
pub fn open_settings(permission: TccPermission) -> bool {
	Command::new("open")
		.arg(permission.settings_url())
		.status()
		.is_ok_and(|status| status.success())
}

/// Ensure Accessibility is granted for input injection.
///
/// On failure, opens the Accessibility settings pane and returns
/// [`PermissionError`] so the caller can fail closed and prompt a
/// grant-then-retry — never proceeding to inject input.
///
/// # Errors
/// Returns [`PermissionError`] when Accessibility is not granted.
pub fn require_accessibility_for_input() -> Result<(), PermissionError> {
	if accessibility_granted() {
		return Ok(());
	}
	let _ = open_settings(TccPermission::Accessibility);
	Err(PermissionError { missing: TccPermission::Accessibility })
}

/// Ensure Screen & System Audio Recording is granted for capture.
///
/// On failure, opens the Screen Recording settings pane and returns
/// [`PermissionError`].
///
/// # Errors
/// Returns [`PermissionError`] when screen capture permission is not granted.
pub fn require_screen_recording_for_capture() -> Result<(), PermissionError> {
	if screen_recording_granted() {
		return Ok(());
	}
	let _ = open_settings(TccPermission::ScreenRecording);
	Err(PermissionError { missing: TccPermission::ScreenRecording })
}

#[cfg(test)]
mod tests {
	use super::{PermissionError, TccPermission, current_process_diagnostic, preflight};

	#[test]
	fn settings_urls_target_the_privacy_panes() {
		assert!(
			TccPermission::Accessibility
				.settings_url()
				.contains("Privacy_Accessibility")
		);
		assert!(
			TccPermission::ScreenRecording
				.settings_url()
				.contains("Privacy_ScreenCapture")
		);
	}

	#[test]
	fn diagnostic_identifies_the_process_being_checked() {
		let diagnostic = current_process_diagnostic();
		assert!(diagnostic.contains("TCC identity: executable="));
		assert!(diagnostic.contains("pid="));
	}

	#[test]
	fn permission_error_explains_launcher_identity_and_relaunch() {
		let message = PermissionError { missing: TccPermission::Accessibility }.to_string();
		assert!(message.contains("current GJC launcher"));
		assert!(message.contains("fully quit and relaunch"));
		assert!(message.contains("TCC identity:"));
	}

	/// Reports the live TCC grant state. Ignored by default (result depends on
	/// the host's granted permissions); run explicitly to learn whether input
	/// injection (Accessibility) is currently possible.
	#[test]
	#[ignore = "reports live TCC grant state; environment-dependent"]
	fn report_live_preflight() {
		let status = preflight();
		println!(
			"TCC preflight: accessibility={} screen_recording={}",
			status.accessibility, status.screen_recording
		);
	}
}
