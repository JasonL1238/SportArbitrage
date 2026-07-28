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

#: Seconds between polls in watch mode.
DEFAULT_INTERVAL_SECONDS = int(_lookup("INTERVAL_SECONDS", "300"))

#: Per-request timeout, seconds.
HTTP_TIMEOUT = float(_lookup("HTTP_TIMEOUT", "20"))


def deprecation_notice() -> str | None:
    """A line naming every deprecated variable this process honoured, or ``None``."""
    if not DEPRECATED_ENV_USED:
        return None
    pairs = ", ".join(f"{old} (use {new})" for old, new in DEPRECATED_ENV_USED)
    return f"deprecated environment variable(s) in use: {pairs}"
