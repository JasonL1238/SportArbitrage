"""``python -m src`` — the collector's command line.

A thin alias for :func:`src.collector.main`, so the package has one obvious entry
point.  Every subcommand takes ``--sport`` and ``--league``:

    python -m src collect --sport hockey
    python -m src show --league NHL --limit 20
    python -m src runs
    python -m src migrate --db data/collector.sqlite3

The dashboard is a separate entry point, because it never fetches anything:

    python -m src.report
"""
import sys

from src.collector import main

sys.exit(main())
