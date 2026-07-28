"""Local runtime paths.

Everything the collector writes stays inside one directory so a run leaves no
trace elsewhere and can be inspected or deleted wholesale.  There are no
credentials, keys, or hosted services to configure — every source is a public
endpoint of the sportsbook's own website.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Root for all local artifacts.  Gitignored.
DATA_DIR = Path(os.environ.get("MLB_DATA_DIR", "data")).expanduser()

#: Captured raw HTTP responses, partitioned by source and date.
RAW_DIR = Path(os.environ.get("MLB_RAW_DIR", str(DATA_DIR / "raw"))).expanduser()

#: SQLite database holding runs, quotes, health, rejections, and findings.
DB_PATH = Path(os.environ.get("MLB_DB_PATH", str(DATA_DIR / "collector.sqlite3"))).expanduser()

#: Seconds between polls in watch mode.
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("MLB_INTERVAL_SECONDS", "300"))

#: Per-request timeout, seconds.
HTTP_TIMEOUT = float(os.environ.get("MLB_HTTP_TIMEOUT", "20"))
