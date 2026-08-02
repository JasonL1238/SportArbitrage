#!/usr/bin/env python3
"""Verify that tool-specific agent adapters share one canonical rule set."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = (
    "docs/agent-guidelines.md",
    "docs/architecture.md",
    "docs/repository-map.md",
    "docs/testing.md",
)
ADAPTER_PAIRS = (
    ("AGENTS.md", "CLAUDE.md"),
    ("src/sources/AGENTS.md", "src/sources/CLAUDE.md"),
    ("src/promos/AGENTS.md", "src/promos/CLAUDE.md"),
)


def normalized_adapter(path: Path) -> str:
    """Ignore only the tool-specific nearest-instruction filename."""
    return path.read_text(encoding="utf-8").replace("AGENTS.md", "INSTRUCTIONS.md").replace(
        "CLAUDE.md", "INSTRUCTIONS.md"
    )


def main() -> int:
    errors: list[str] = []
    for relative in CANONICAL_DOCS:
        if not (ROOT / relative).is_file():
            errors.append(f"missing canonical document: {relative}")

    for agent_relative, claude_relative in ADAPTER_PAIRS:
        agent_path = ROOT / agent_relative
        claude_path = ROOT / claude_relative
        for path in (agent_path, claude_path):
            if not path.is_file():
                errors.append(f"missing adapter: {path.relative_to(ROOT)}")
        if not agent_path.is_file() or not claude_path.is_file():
            continue
        if normalized_adapter(agent_path) != normalized_adapter(claude_path):
            errors.append(
                f"adapter pair drifted: {agent_relative} and {claude_relative}"
            )
        text = agent_path.read_text(encoding="utf-8")
        for relative in CANONICAL_DOCS:
            target = Path(relative).name
            if target not in text:
                errors.append(f"{agent_relative} does not reference {target}")

    if errors:
        print("agent documentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("agent documentation check passed (3 synchronized adapter pairs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
