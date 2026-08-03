#!/usr/bin/env python3
"""Verify that tool-specific agent adapters share one canonical rule set."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = (
    "docs/agent-guidelines.md",
    "docs/architecture.md",
    "docs/repository-map.md",
    "docs/testing.md",
)
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "data",
        "dist",
        "node_modules",
        "vendor",
        "venv",
    }
)


def normalized_adapter(path: Path) -> str:
    """Ignore only the tool-specific nearest-instruction filename."""
    return path.read_text(encoding="utf-8").replace("AGENTS.md", "INSTRUCTIONS.md").replace(
        "CLAUDE.md", "INSTRUCTIONS.md"
    )


def discover_adapters(root: Path, filename: str) -> dict[Path, Path]:
    """Return one adapter per relative directory, pruning generated trees."""
    found: dict[Path, Path] = {}
    for directory, child_directories, filenames in os.walk(root):
        child_directories[:] = sorted(
            name for name in child_directories if name not in IGNORED_DIRECTORIES
        )
        if filename not in filenames:
            continue
        path = Path(directory) / filename
        relative = path.relative_to(root)
        found[relative.parent] = path
    return found


def main() -> int:
    errors: list[str] = []
    for relative in CANONICAL_DOCS:
        if not (ROOT / relative).is_file():
            errors.append(f"missing canonical document: {relative}")

    agents = discover_adapters(ROOT, "AGENTS.md")
    claude = discover_adapters(ROOT, "CLAUDE.md")
    directories = sorted(set(agents) | set(claude), key=lambda path: path.as_posix())
    for directory in directories:
        agent_path = agents.get(directory)
        claude_path = claude.get(directory)
        label = directory.as_posix()
        if agent_path is None:
            errors.append(f"missing adapter: {label}/AGENTS.md")
        if claude_path is None:
            errors.append(f"missing adapter: {label}/CLAUDE.md")
        if agent_path is None or claude_path is None:
            continue
        if normalized_adapter(agent_path) != normalized_adapter(claude_path):
            errors.append(
                "adapter pair drifted: "
                f"{agent_path.relative_to(ROOT)} and {claude_path.relative_to(ROOT)}"
            )
        for path in (agent_path, claude_path):
            text = path.read_text(encoding="utf-8")
            for relative in CANONICAL_DOCS:
                target = Path(relative).name
                if target not in text:
                    errors.append(
                        f"{path.relative_to(ROOT)} does not reference {target}"
                    )

    if errors:
        print("agent documentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "agent documentation check passed "
        f"({len(directories)} synchronized adapter pairs)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
