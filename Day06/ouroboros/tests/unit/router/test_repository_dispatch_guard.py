from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src" / "ouroboros"

RUNTIME_DISPATCH_FILES = (
    Path("src/ouroboros/orchestrator/codex_cli_runtime.py"),
    Path("src/ouroboros/orchestrator/hermes_runtime.py"),
    Path("src/ouroboros/orchestrator/opencode_runtime.py"),
)

REMOVED_RUNTIME_DISPATCH_HELPERS = frozenset(
    {
        "SkillInterceptRequest",
        "_extract_first_argument",
        "_load_skill_frontmatter",
        "_normalize_mcp_frontmatter",
        "_resolve_dispatch_templates",
        "_resolve_skill_dispatch",
        "_resolve_skill_intercept",
    }
)

CANONICAL_ROUTER_DEFINITIONS = {
    "parse_ooo_command": {Path("src/ouroboros/router/command_parser.py")},
    "extract_first_argument": {Path("src/ouroboros/router/dispatch.py")},
    "load_skill_frontmatter": {Path("src/ouroboros/router/dispatch.py")},
    "normalize_mcp_frontmatter": {Path("src/ouroboros/router/dispatch.py")},
    "resolve_dispatch_templates": {Path("src/ouroboros/router/dispatch.py")},
    "resolve_parsed_skill_dispatch": {Path("src/ouroboros/router/dispatch.py")},
}


def _python_source_files(root: Path) -> Iterable[Path]:
    return sorted(root.rglob("*.py"))


def _relative(path: Path) -> Path:
    return path.relative_to(REPO_ROOT)


def _parse_source(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(_relative(path)))


def _defined_names(path: Path) -> set[str]:
    tree = _parse_source(path)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_runtime_modules_do_not_reintroduce_local_ooo_dispatch_helpers() -> None:
    """Runtimes must delegate deterministic ooo parsing to the shared router."""
    offenders = {
        runtime_file: sorted(
            _defined_names(REPO_ROOT / runtime_file) & REMOVED_RUNTIME_DISPATCH_HELPERS
        )
        for runtime_file in RUNTIME_DISPATCH_FILES
    }
    offenders = {path: names for path, names in offenders.items() if names}

    assert offenders == {}


def test_runtime_modules_do_not_reference_removed_ooo_dispatch_helpers() -> None:
    """Removed helper calls should not survive as dead references in runtimes."""
    offender_tokens: dict[Path, list[str]] = {}
    reference_tokens = tuple(f"{name}(" for name in REMOVED_RUNTIME_DISPATCH_HELPERS) + (
        "SkillInterceptRequest",
    )

    for runtime_file in RUNTIME_DISPATCH_FILES:
        source = (REPO_ROOT / runtime_file).read_text(encoding="utf-8")
        matches = sorted(token for token in reference_tokens if token in source)
        if matches:
            offender_tokens[runtime_file] = matches

    assert offender_tokens == {}


def test_ooo_dispatch_parser_and_helpers_are_defined_only_in_shared_router() -> None:
    """Adding dispatch commands stays SKILL.md-only; runtimes cannot add parsers."""
    definitions: dict[str, set[Path]] = {name: set() for name in CANONICAL_ROUTER_DEFINITIONS}

    for source_file in _python_source_files(SOURCE_ROOT):
        relative_source_file = _relative(source_file)
        for defined_name in _defined_names(source_file):
            if defined_name in definitions:
                definitions[defined_name].add(relative_source_file)

    assert definitions == CANONICAL_ROUTER_DEFINITIONS
