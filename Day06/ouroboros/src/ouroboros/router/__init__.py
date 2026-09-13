"""Public API for stateless Ouroboros skill-dispatch routing.

The :mod:`ouroboros.router` package is a pure resolver shared by the Codex CLI,
Hermes, and Opencode runtimes. Given caller-owned request data, it parses the
deterministic ``ooo <skill>`` and ``/ouroboros:<skill>`` command prefixes,
loads the target skill's ``SKILL.md`` frontmatter, validates ``mcp_tool`` and
``mcp_args``, resolves the supported ``$1`` and ``$CWD`` templates, and returns
typed dispatch metadata.

The router is intentionally stateless: it keeps no mutable process state, emits
no structured logs, does not assemble :class:`AgentMessage` objects, and never
invokes MCP handlers. Adding or changing a dispatch command remains a
``SKILL.md``-only operation because runtime-neutral dispatch metadata is read
from skill frontmatter.

Preferred runtime input is :class:`ResolveRequest`, which carries the full
prompt, the runtime working directory, and an optional packaged-skill override
directory. :func:`resolve_skill_dispatch` also accepts a direct prompt-string
call.

The primary result union is :data:`ResolveResult`:

* :class:`Resolved` means the runtime should build its runtime-specific
  ``AgentMessage`` sequence and call its configured or built-in MCP handler
  described by the canonical :class:`MCPDispatchTarget`.
* :class:`NotHandled` means the prompt should continue through the normal
  runtime path because it was not a deterministic skill dispatch, or the skill
  was unavailable.
* :class:`InvalidSkill` means the command matched but the skill frontmatter was
  malformed or incomplete; the runtime owns caller-observable logging and error
  presentation.

Codex CLI, Hermes, and Opencode should call this package before starting their
subprocess flow, branch on the result variant, and keep only runtime-specific
message assembly, structured logging, and MCP handler invocation locally.
"""

from ouroboros.router.command_parser import parse_ooo_command
from ouroboros.router.dispatch import (
    DispatchTarget,
    DispatchTargetKind,
    InvalidInputReason,
    InvalidSkill,
    MCPDispatchTarget,
    McpDispatchTarget,
    NoMatchReason,
    NotHandled,
    Resolved,
    ResolveOutcome,
    ResolveRequest,
    ResolveResult,
    RouterRequest,
    SkillDispatchRouter,
    extract_first_argument,
    load_skill_frontmatter,
    normalize_mcp_frontmatter,
    resolve_dispatch_templates,
    resolve_parsed_skill_dispatch,
    resolve_skill_dispatch,
)
from ouroboros.router.registry import (
    SkillDispatchRegistry,
    SkillDispatchRegistryEntry,
    SkillDispatchTarget,
    SkillDispatchTargetResolution,
    normalize_skill_identifier,
    packaged_skill_dispatch_registry,
    resolve_skill_dispatch_target,
)
from ouroboros.router.types import (
    MCPFrontmatterArgs,
    MCPFrontmatterScalar,
    MCPFrontmatterValue,
    NormalizedMCPFrontmatter,
    NormalizedMcpFrontmatter,
    ParsedOooCommand,
)

__all__ = [
    "DispatchTarget",
    "DispatchTargetKind",
    "InvalidInputReason",
    "InvalidSkill",
    "MCPDispatchTarget",
    "MCPFrontmatterArgs",
    "MCPFrontmatterScalar",
    "MCPFrontmatterValue",
    "McpDispatchTarget",
    "NoMatchReason",
    "NormalizedMCPFrontmatter",
    "NormalizedMcpFrontmatter",
    "NotHandled",
    "ParsedOooCommand",
    "ResolveOutcome",
    "ResolveRequest",
    "ResolveResult",
    "Resolved",
    "RouterRequest",
    "SkillDispatchRegistry",
    "SkillDispatchRegistryEntry",
    "SkillDispatchRouter",
    "SkillDispatchTarget",
    "SkillDispatchTargetResolution",
    "extract_first_argument",
    "load_skill_frontmatter",
    "normalize_mcp_frontmatter",
    "normalize_skill_identifier",
    "packaged_skill_dispatch_registry",
    "parse_ooo_command",
    "resolve_dispatch_templates",
    "resolve_parsed_skill_dispatch",
    "resolve_skill_dispatch",
    "resolve_skill_dispatch_target",
]
