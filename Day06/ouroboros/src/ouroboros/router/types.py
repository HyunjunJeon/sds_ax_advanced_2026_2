"""Shared types for stateless ``ooo`` skill-dispatch routing."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

type MCPFrontmatterScalar = str | int | float | bool | None
type MCPFrontmatterValue = (
    MCPFrontmatterScalar | list[MCPFrontmatterValue] | dict[str, MCPFrontmatterValue]
)
type MCPFrontmatterArgs = dict[str, MCPFrontmatterValue]


class DispatchTargetKind(StrEnum):
    """Canonical runtime-neutral dispatch target kinds."""

    MCP_TOOL = "mcp_tool"


class ResolveOutcome(StrEnum):
    """Structured resolver outcome categories."""

    MATCH = "match"
    NO_MATCH = "no_match"
    INVALID_INPUT = "invalid_input"


class NoMatchReason(StrEnum):
    """Structured reasons a prompt was not claimed by the router."""

    NOT_A_SKILL_COMMAND = "not_a_skill_command"
    SKILL_NOT_FOUND = "skill_not_found"


class InvalidInputReason(StrEnum):
    """Structured reasons a parsed dispatch input could not be dispatched."""

    MALFORMED_PARSED_COMMAND = "malformed_parsed_command"
    FRONTMATTER_LOAD_ERROR = "frontmatter_load_error"
    FRONTMATTER_INVALID = "frontmatter_invalid"
    TEMPLATE_RESOLUTION_ERROR = "template_resolution_error"


@dataclass(frozen=True, slots=True)
class ParsedOooCommand:
    """Canonical parsed ``ooo`` skill command data.

    Attributes:
        skill_name: Lowercase skill identifier resolved from the command prefix.
        command_prefix: Canonical command prefix that matched the prompt.
        remainder: Remaining command text after the canonical command prefix.
    """

    skill_name: str
    command_prefix: str
    remainder: str | None

    @property
    def remaining_text(self) -> str | None:
        """Alias for the remaining command text after the command prefix."""
        return self.remainder


@dataclass(frozen=True, slots=True)
class MCPDispatchTarget:
    """Canonical runtime-neutral MCP dispatch target."""

    mcp_tool: str
    mcp_args: dict[str, Any]
    kind: DispatchTargetKind = DispatchTargetKind.MCP_TOOL


DispatchTarget = MCPDispatchTarget
McpDispatchTarget = MCPDispatchTarget


@dataclass(frozen=True, slots=True)
class NormalizedMCPFrontmatter:
    """Canonical normalized MCP dispatch metadata from a skill's frontmatter."""

    mcp_tool: str
    mcp_args: MCPFrontmatterArgs

    @property
    def target(self) -> MCPDispatchTarget:
        """Return this normalized frontmatter as a dispatch target."""
        return MCPDispatchTarget(mcp_tool=self.mcp_tool, mcp_args=self.mcp_args)

    def __iter__(self) -> Iterator[Any]:
        """Keep tuple-unpacking compatibility for existing router call sites."""
        yield self.mcp_tool
        yield self.mcp_args


NormalizedMcpFrontmatter = NormalizedMCPFrontmatter


@dataclass(frozen=True, slots=True)
class Resolved:
    """Resolved skill dispatch metadata for a runtime-specific MCP call.

    Runtimes consume this variant by assembling their own runtime messages and
    invoking the configured or built-in MCP handler named by ``mcp_tool`` with
    ``mcp_args``. The values are detached from loaded frontmatter and contain
    only runtime-neutral metadata.
    """

    skill_name: str
    command_prefix: str
    prompt: str
    skill_path: Path
    mcp_tool: str
    mcp_args: MCPFrontmatterArgs
    first_argument: str | None = field(default=None, compare=False)

    @property
    def outcome(self) -> ResolveOutcome:
        """Structured outcome category for successful resolution."""
        return ResolveOutcome.MATCH

    @property
    def target(self) -> DispatchTarget:
        """Canonical MCP dispatch target for runtime-specific invocation."""
        return MCPDispatchTarget(mcp_tool=self.mcp_tool, mcp_args=self.mcp_args)

    @property
    def dispatch_target(self) -> DispatchTarget:
        """Alias for callers that prefer explicit dispatch terminology."""
        return self.target

    @property
    def dispatch_metadata(self) -> NormalizedMCPFrontmatter:
        """Canonical normalized dispatch metadata for runtime-specific invocation."""
        return NormalizedMCPFrontmatter(mcp_tool=self.mcp_tool, mcp_args=self.mcp_args)


@dataclass(frozen=True, slots=True)
class NotHandled:
    """Prompt is not a deterministic skill dispatch."""

    reason: str
    category: NoMatchReason = NoMatchReason.NOT_A_SKILL_COMMAND

    @property
    def outcome(self) -> ResolveOutcome:
        """Structured outcome category for non-matched prompts."""
        return ResolveOutcome.NO_MATCH


@dataclass(frozen=True, slots=True)
class InvalidSkill:
    """A parsed dispatch input had invalid skill or dispatch metadata."""

    reason: str
    skill_path: Path
    category: InvalidInputReason = InvalidInputReason.FRONTMATTER_INVALID

    @property
    def outcome(self) -> ResolveOutcome:
        """Structured outcome category for invalid matched input."""
        return ResolveOutcome.INVALID_INPUT


type ResolveResult = Resolved | NotHandled | InvalidSkill


__all__ = [
    "DispatchTarget",
    "DispatchTargetKind",
    "InvalidSkill",
    "InvalidInputReason",
    "MCPDispatchTarget",
    "MCPFrontmatterArgs",
    "MCPFrontmatterScalar",
    "MCPFrontmatterValue",
    "McpDispatchTarget",
    "NoMatchReason",
    "NotHandled",
    "NormalizedMCPFrontmatter",
    "NormalizedMcpFrontmatter",
    "ParsedOooCommand",
    "ResolveResult",
    "ResolveOutcome",
    "Resolved",
]
