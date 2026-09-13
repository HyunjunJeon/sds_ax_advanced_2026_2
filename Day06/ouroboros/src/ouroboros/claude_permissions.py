"""Shared Claude Code permission policy helpers.

Translation table between the engine-owned
:class:`~ouroboros.orchestrator.policy.SandboxClass` vocabulary and the Claude
Agent SDK's ``permission_mode`` values.  Mirrors the role of
``codex_permissions.py`` for Claude so every provider adapter reads the same
sandbox enum and resolves it through its own flat lookup table.  The module
deliberately does not make policy decisions — those live in
``orchestrator/policy.py``.
"""

from __future__ import annotations

from typing import Literal

import structlog

from ouroboros.sandbox import SandboxClass

log = structlog.get_logger(__name__)

# Claude SDK accepts a fixed vocabulary for ``permission_mode``.  Exposed as
# a type alias so downstream adapter code carries the narrow type rather than
# an opaque ``str``.
ClaudePermissionMode = Literal["default", "acceptEdits", "bypassPermissions"]

# Engine SandboxClass → Claude SDK permission_mode.
#
# The mapping happens to be identity for the legacy mode strings because
# Ouroboros previously adopted Claude's vocabulary as the shared lingua
# franca.  Introducing the enum flips the ownership: the SandboxClass is
# now authoritative and these strings are Claude-specific translations.
_SANDBOX_TO_CLAUDE_MODE: dict[SandboxClass, ClaudePermissionMode] = {
    SandboxClass.READ_ONLY: "default",
    SandboxClass.WORKSPACE_WRITE: "acceptEdits",
    SandboxClass.UNRESTRICTED: "bypassPermissions",
}


def claude_permission_mode_for_sandbox(sandbox: SandboxClass) -> ClaudePermissionMode:
    """Translate an engine sandbox class into a Claude SDK permission_mode.

    Raises ``KeyError`` if the enum grows and Claude's table was not updated —
    failing loudly beats silently defaulting to a possibly-permissive mode.
    """
    mode = _SANDBOX_TO_CLAUDE_MODE.get(sandbox)
    if mode is None:
        msg = f"No Claude SDK permission_mode registered for sandbox class {sandbox!r}"
        raise KeyError(msg)
    if sandbox is SandboxClass.UNRESTRICTED:
        log.warning("permissions.bypass_activated", sandbox=sandbox.value)
    return mode


__all__ = [
    "ClaudePermissionMode",
    "claude_permission_mode_for_sandbox",
]
