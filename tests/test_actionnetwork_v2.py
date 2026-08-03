from __future__ import annotations

from pathlib import Path

import pytest

from src.raw_store import RawStore
from src.sources.registry import descriptor


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw"


@pytest.mark.parametrize("source", ("an_hardrock", "an_fanatics", "an_bally"))
def test_current_v2_capture_produces_real_quotes_without_rejections(source: str) -> None:
    paths = sorted(FIXTURE_DIR.glob(f"{source}__*.json"))
    assert paths
    raws = [RawStore(FIXTURE_DIR).read(path) for path in paths]
    adapter = descriptor(source).replay_instance()
    try:
        outcome = adapter.parse(raws)
    finally:
        adapter.close()
    assert outcome.quotes
    assert not outcome.rejections
    assert {quote.source for quote in outcome.quotes} == {source}
