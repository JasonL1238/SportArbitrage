"""Local runtime paths and knobs.

Everything the collector writes stays inside one directory so a run leaves no
trace elsewhere and can be inspected or deleted wholesale.  There are no
credentials, keys, or hosted services to configure — every source is a public
endpoint of the sportsbook's own website.

Configuration is read from ``ODDS_*`` environment variables.  The names used to
be ``MLB_*``, which stopped being true the moment the pipeline collected a
second sport: an operator reading ``MLB_DB_PATH`` on a database full of tennis
prices learns something false.  The old names still work — they are looked up as
deprecated aliases and each one that is actually used is recorded in
:data:`DEPRECATED_ENV_USED` so the CLI can say so out loud rather than honouring
them in silence.  A misleading name that still works is a smaller problem than a
run that silently writes to the default path because the variable it was told to
read was renamed underneath it.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

#: Prefix for the current variable names.
ENV_PREFIX = "ODDS"

#: Prefix kept working for compatibility.  Reading one of these is not an error;
#: it is reported.
DEPRECATED_ENV_PREFIX = "MLB"

#: ``(deprecated_name, current_name)`` for every alias actually found in the
#: environment of this process, in lookup order.  The CLI prints these; nothing
#: depends on the list being empty.
DEPRECATED_ENV_USED: list[tuple[str, str]] = []

#: Every setting this module reads, as ``suffix -> (current, deprecated)``.
ENV_NAMES: dict[str, tuple[str, str]] = {}


def _lookup(suffix: str, default: str) -> str:
    """Read one setting, preferring the current name over the deprecated alias."""
    current = f"{ENV_PREFIX}_{suffix}"
    deprecated = f"{DEPRECATED_ENV_PREFIX}_{suffix}"
    ENV_NAMES[suffix] = (current, deprecated)
    value = os.environ.get(current)
    if value is not None:
        return value
    value = os.environ.get(deprecated)
    if value is not None:
        DEPRECATED_ENV_USED.append((deprecated, current))
        return value
    return default


#: Root for all local artifacts.  Gitignored.
DATA_DIR = Path(_lookup("DATA_DIR", "data")).expanduser()

#: Captured raw HTTP responses, partitioned by source and date.
RAW_DIR = Path(_lookup("RAW_DIR", str(DATA_DIR / "raw"))).expanduser()

#: SQLite database holding runs, quotes, health, rejections, and findings.
DB_PATH = Path(_lookup("DB_PATH", str(DATA_DIR / "collector.sqlite3"))).expanduser()


class BadSetting(RuntimeError):
    """An environment variable holds a value this pipeline cannot honour."""


#: Every ``ODDS_*`` value that could not be honoured, as ready-to-print lines.
#:
#: Recorded rather than raised.  This module is imported at the top of every
#: other one, so raising here aborts ``--help`` and every read command with an
#: import-time traceback — and a failed import is evicted from ``sys.modules``,
#: so even a wrapper that tries to catch it re-triggers the failure inside its
#: own handler.  :func:`refuse_bad_settings` is what turns these into a clean
#: refusal, called at the top of each entry point.
BAD_SETTINGS: list[str] = []


def _number(suffix: str, default: str, *, whole: bool, minimum: float):
    """Read a numeric setting, refusing what cannot be honoured — by name.

    argparse validators never see these: they validate a *flag*, and the flag's
    default comes from here.  So the work ``--interval`` does with
    ``_positive`` was undone by ``ODDS_INTERVAL_SECONDS``, reintroducing
    verbatim the failure that type exists to remove — ``-9`` reached
    ``time.sleep`` and killed an unattended watch loop with a traceback *after*
    its first successful pass, and ``0`` polled ten public endpoints
    continuously, which this pipeline treats as a design property rather than a
    courtesy.

    Unparseable values used to abort every command, ``--help`` included, with a
    bare ``ValueError: could not convert string to float: 'abc'`` from an import
    that never named the variable.
    """
    current, _ = ENV_NAMES.get(suffix, (f"{ENV_PREFIX}_{suffix}", ""))
    text = _lookup(suffix, default)

    def _refuse(why: str):
        BAD_SETTINGS.append(f"{current}={text!r} {why}")
        # Fall back to the shipped default so the process stays importable; the
        # entry point refuses before the value can be acted on.
        return int(default) if whole else float(default)

    try:
        value = int(text) if whole else float(text)
    except ValueError:
        return _refuse(f"is not a {'whole number' if whole else 'number'}")
    if not math.isfinite(value):
        return _refuse("must be a finite number")
    if value < minimum:
        return _refuse(
            f"must be at least {minimum:g}"
            + (
                " — a shorter gap polls the sources continuously, which this "
                "pipeline treats as a design property rather than a courtesy"
                if suffix == "INTERVAL_SECONDS"
                else ""
            )
        )
    return value


#: Seconds between polls in watch mode.  One second is the floor: below it the
#: loop is a hot poll of ten public endpoints.
DEFAULT_INTERVAL_SECONDS = _number("INTERVAL_SECONDS", "300", whole=True, minimum=1)

#: Per-request timeout, seconds.
HTTP_TIMEOUT = _number("HTTP_TIMEOUT", "20", whole=False, minimum=0.001)


def refuse_bad_settings() -> int | None:
    """Print every unhonourable ``ODDS_*`` value and return an exit code, or
    ``None`` when the environment is fine.  Called first by each entry point."""
    if not BAD_SETTINGS:
        return None
    import sys

    for line in BAD_SETTINGS:
        print(f"error: {line}", file=sys.stderr)
    return 2


def deprecation_notice() -> str | None:
    """A line naming every deprecated variable this process honoured, or ``None``."""
    if not DEPRECATED_ENV_USED:
        return None
    pairs = ", ".join(f"{old} (use {new})" for old, new in DEPRECATED_ENV_USED)
    return f"deprecated environment variable(s) in use: {pairs}"
