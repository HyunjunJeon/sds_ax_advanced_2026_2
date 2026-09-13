"""Backend-neutral sandbox class vocabulary.

This module deliberately sits at the top level with zero upward dependencies
so that both engine-side code (``orchestrator.policy``) and leaf provider
translation tables (``codex_permissions``, ``claude_permissions``) can import
it without creating a circular dependency on the orchestrator package.

The enum is the single shared vocabulary at the engine/adapter boundary:
the engine decides which sandbox level a session deserves, and every
provider adapter looks the result up in its own flat translation table —
never re-derives the decision from free-form permission strings.
"""

from __future__ import annotations

from enum import StrEnum


class SandboxClass(StrEnum):
    """Backend-neutral sandbox level for an orchestrator session.

    - ``READ_ONLY``: the session may inspect state but not mutate the host.
    - ``WORKSPACE_WRITE``: the session may mutate workspace files; host
      state outside the workspace remains off-limits.
    - ``UNRESTRICTED``: approval and sandbox gates are bypassed.  Every
      provider adapter must emit a warning when this value is realized.
    """

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    UNRESTRICTED = "unrestricted"


__all__ = ["SandboxClass"]
