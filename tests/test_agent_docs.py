"""AGENTS.md and CLAUDE.md must stay byte-identical.

Claude Code reads only ``CLAUDE.md``; Codex and most other coding agents read
only ``AGENTS.md``.  Neither reads both.  Keeping two copies is therefore the
only way to give both agents the same instructions, and the copies drifting is
the failure that matters: an agent working from stale instructions looks like an
agent that is simply ignoring them, which is very hard to diagnose from the
outside.

This is asserted rather than left to a note in the files because the note is
only read by an agent that already opened the file it was going to edit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"


def test_both_files_exist() -> None:
    missing = [p.name for p in (AGENTS, CLAUDE) if not p.exists()]
    assert not missing, (
        f"{', '.join(missing)} missing — an agent reading only that filename "
        "would start with no project instructions at all"
    )


def test_contents_are_identical() -> None:
    agents, claude = AGENTS.read_bytes(), CLAUDE.read_bytes()
    if agents == claude:
        return

    a_lines, c_lines = agents.decode().splitlines(), claude.decode().splitlines()
    diverged = next(
        (
            i
            for i, (x, y) in enumerate(zip(a_lines, c_lines), start=1)
            if x != y
        ),
        min(len(a_lines), len(c_lines)) + 1,
    )
    pytest.fail(
        f"AGENTS.md and CLAUDE.md diverge from line {diverged}. Edit one, then "
        "copy it over the other: cp AGENTS.md CLAUDE.md"
    )


def test_states_the_sync_rule() -> None:
    """The rule has to be in the file, not only in this test.

    An agent about to edit one file reads that file, not the test suite.
    """
    text = AGENTS.read_text()
    assert "CLAUDE.md" in text and "AGENTS.md" in text, (
        "the instructions must name both files so an agent editing one knows "
        "the other exists"
    )


def test_states_the_runtime_cost_constraint() -> None:
    """A paid API is the one mistake here that costs real money.

    Guarding the wording is deliberate: this file is the only place the
    constraint is recorded, so silently losing it in an edit is unrecoverable
    without re-reading the whole history.
    """
    text = AGENTS.read_text().lower()
    assert "no paid apis" in text
    assert "ignore development labor costs" in text
