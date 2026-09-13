//! Command execution utilities.

use std::{ffi::OsStr, os::windows::process::CommandExt as WindowsCommandExt};

use windows_sys::Win32::System::Console::GetConsoleWindow;
use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};

/// Whether this process currently has no attached console — the state a GJC
/// agent runs in when embedded in a console-less ACP/GUI host (#4883).
///
/// Deliberately not cached: `FreeConsole`/`AttachConsole` can change the
/// answer during the process lifetime, and the flag decision must reflect the
/// state at spawn time.
fn consoleless_host() -> bool {
	// SAFETY: `GetConsoleWindow` only queries this process's attached console;
	// it takes no arguments and mutates nothing.
	unsafe { GetConsoleWindow() }.is_null()
}

/// Pure creation-flags decision so the contract is testable without a live
/// console state. `std`'s `creation_flags` *replaces* the whole flag value, so
/// every flags write in this module must flow through this composition.
///
/// `CREATE_NO_WINDOW` gives the child a hidden console rather than no console.
/// That distinction is the whole point: with `DETACHED_PROCESS` the child has
/// no console at all, so any grandchild it spawns would allocate a fresh
/// *visible* console — reintroducing the flash one level deeper. A hidden
/// console is inherited by descendants, keeping the entire tree windowless.
///
/// It is only applied when the host itself is console-less: a console-attached
/// host keeps the default inherit-parent-console behavior, so ordinary
/// interactive terminal sessions are bit-for-bit unchanged.
pub fn spawn_creation_flags(group_flag: u32, host_consoleless: bool) -> u32 {
	group_flag
		| if host_consoleless {
			CREATE_NO_WINDOW
		} else {
			0
		}
}

use crate::{ShellFd, error, openfiles};

/// Extension trait for Windows command extensions.
pub trait CommandExt {
	/// Sets the zeroth argument (argv[0]) of the command.
	///
	/// # Arguments
	///
	/// * `arg` - The argument to set as argv[0].
	fn arg0<S>(&mut self, arg: S) -> &mut Self
	where
		S: AsRef<OsStr>;

	/// Sets the process group ID of the command.
	///
	/// # Arguments
	///
	/// * `pgroup` - The process group ID to set.
	fn process_group(&mut self, pgroup: i32) -> &mut Self;
}

impl CommandExt for std::process::Command {
	fn arg0<S>(&mut self, _arg: S) -> &mut Self
	where
		S: AsRef<OsStr>,
	{
		// NOTE: Windows does not support overriding argv[0] directly.
		self
	}

	fn process_group(&mut self, pgroup: i32) -> &mut Self {
		if pgroup == 0 {
			self.creation_flags(spawn_creation_flags(
				CREATE_NEW_PROCESS_GROUP,
				consoleless_host(),
			));
		}
		self
	}
}

/// Extension trait for Unix-like exit status extensions.
pub trait ExitStatusExt {
	/// Returns the signal that terminated the process, if any.
	fn signal(&self) -> Option<i32>;
}

impl ExitStatusExt for std::process::ExitStatus {
	fn signal(&self) -> Option<i32> {
		None
	}
}

/// Extension trait for injecting file descriptors into commands.
pub trait CommandFdInjectionExt {
	/// Injects the given open files as file descriptors into the command.
	///
	/// # Arguments
	///
	/// * `open_files` - A mapping of child file descriptors to open files.
	fn inject_fds(
		&mut self,
		open_files: impl Iterator<Item = (ShellFd, openfiles::OpenFile)>,
	) -> Result<(), error::Error>;
}

impl CommandFdInjectionExt for std::process::Command {
	fn inject_fds(
		&mut self,
		mut open_files: impl Iterator<Item = (ShellFd, openfiles::OpenFile)>,
	) -> Result<(), error::Error> {
		if open_files.next().is_some() {
			return Err(
				error::ErrorKind::NotSupportedOnThisPlatform(
					"fd redirections beyond stdin/stdout/stderr on Windows",
				)
				.into(),
			);
		}

		Ok(())
	}
}

/// Extension trait for arranging for commands to take the foreground.
pub trait CommandFgControlExt {
	/// Arranges for the command to take the foreground when it is executed.
	fn take_foreground(&mut self);
	/// Arranges for the command to become a session leader when it is executed.
	fn lead_session(&mut self);
}

impl CommandFgControlExt for std::process::Command {
	fn take_foreground(&mut self) {
		self.creation_flags(spawn_creation_flags(
			CREATE_NEW_PROCESS_GROUP,
			consoleless_host(),
		));
	}

	fn lead_session(&mut self) {
		self.creation_flags(spawn_creation_flags(
			CREATE_NEW_PROCESS_GROUP,
			consoleless_host(),
		));
	}
}

/// Extension trait for detaching a command from the parent's controlling terminal.
pub trait CommandSessionExt {
	/// Arranges for the command to run in a new POSIX session with no controlling
	/// terminal. On Windows this is a no-op; process-group behavior is handled
	/// by `CommandFgControlExt` via `CREATE_NEW_PROCESS_GROUP`.
	fn detach_session(&mut self);
}

impl CommandSessionExt for std::process::Command {
	fn detach_session(&mut self) {
		// NOTE: Windows has no setsid; intentionally a no-op.
	}
}

/// Extension trait for keeping child processes of a console-less host from
/// each allocating a visible console window (#4883).
///
/// Windows: adds `CREATE_NO_WINDOW` when this process has no console. The
/// method writes the full creation-flag value (std's `creation_flags`
/// replaces, not ORs), so it must be applied *before* any process-group /
/// foreground / session setup — those paths re-derive the complete flag set
/// including the no-window bit. Non-Windows platforms implement a no-op.
pub trait CommandWindowControlExt {
	/// Whether this process currently has no attached console. Windows asks
	/// the kernel (`GetConsoleWindow`); other platforms report `false`.
	fn host_is_consoleless() -> bool;

	/// Applies the host-console-aware no-window creation flag.
	fn suppress_console_window_if_host_consoleless(&mut self);
}

impl CommandWindowControlExt for std::process::Command {
	fn host_is_consoleless() -> bool {
		consoleless_host()
	}
	fn suppress_console_window_if_host_consoleless(&mut self) {
		self.creation_flags(spawn_creation_flags(0, consoleless_host()));
	}
}
