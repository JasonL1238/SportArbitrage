"""Realistic The Odds API v4 JSON snapshots for parser/pipeline backtests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

_NOW = datetime.now(UTC)
_FRESH = _NOW.isoformat().replace("+00:00", "Z")
_STALE = (_NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")


NBA_TWO_WAY_ARB: list[dict] = [
    {
        "id": "abc123nba",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": (_NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 2.20},
                            {"name": "Boston Celtics", "price": 1.70},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.91, "point": -3.5},
                            {"name": "Boston Celtics", "price": 1.91, "point": 3.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.80},
                            {"name": "Boston Celtics", "price": 2.30},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Over", "price": 1.90, "point": 220.5},
                            {"name": "Under", "price": 1.90, "point": 220.5},
                        ],
                    },
                ],
            },
        ],
    }
]

MULTI_EVENT_SNAPSHOT: list[dict] = [
    {
        "id": "evt_nba_1",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": (_NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        "home_team": "Team Alpha",
        "away_team": "Team Beta",
        "bookmakers": [
            {
                "key": "book_a",
                "title": "Book A",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Team Alpha", "price": 2.10},
                            {"name": "Team Beta", "price": 1.75},
                        ],
                    }
                ],
            },
            {
                "key": "book_b",
                "title": "Book B",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Team Alpha", "price": 1.80},
                            {"name": "Team Beta", "price": 2.20},
                        ],
                    }
                ],
            },
        ],
    },
    {
        "id": "evt_nba_2",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": (_NOW + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "home_team": "Team Gamma",
        "away_team": "Team Delta",
        "bookmakers": [
            {
                "key": "book_a",
                "title": "Book A",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Team Gamma", "price": 1.90},
                            {"name": "Team Delta", "price": 1.90},
                        ],
                    }
                ],
            },
        ],
    },
]

STALE_ODDS_SNAPSHOT: list[dict] = [
    {
        "id": "evt_stale",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": (_NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "home_team": "Stale Home",
        "away_team": "Stale Away",
        "bookmakers": [
            {
                "key": "fresh_book",
                "title": "Fresh Book",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Stale Home", "price": 2.20},
                            {"name": "Stale Away", "price": 1.70},
                        ],
                    }
                ],
            },
            {
                "key": "stale_book",
                "title": "Stale Book",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _STALE,
                        "outcomes": [
                            {"name": "Stale Home", "price": 1.50},
                            {"name": "Stale Away", "price": 3.00},
                        ],
                    }
                ],
            },
        ],
    }
]

SOCCER_THREE_WAY: list[dict] = [
    {
        "id": "evt_epl_1",
        "sport_key": "soccer_epl",
        "sport_title": "EPL",
        "commence_time": (_NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "home_team": "Liverpool",
        "away_team": "Arsenal",
        "bookmakers": [
            {
                "key": "book_a",
                "title": "Book A",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Liverpool", "price": 3.50},
                            {"name": "Draw", "price": 3.20},
                            {"name": "Arsenal", "price": 2.40},
                        ],
                    }
                ],
            },
            {
                "key": "book_b",
                "title": "Book B",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Liverpool", "price": 3.00},
                            {"name": "Draw", "price": 4.00},
                            {"name": "Arsenal", "price": 2.60},
                        ],
                    }
                ],
            },
            {
                "key": "book_c",
                "title": "Book C",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _FRESH,
                        "outcomes": [
                            {"name": "Liverpool", "price": 3.80},
                            {"name": "Draw", "price": 3.40},
                            {"name": "Arsenal", "price": 2.80},
                        ],
                    }
                ],
            },
        ],
    }
]

EMPTY_BOOKMAKERS: list[dict] = [
    {
        "id": "evt_empty",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "commence_time": (_NOW + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        "home_team": "No Odds Home",
        "away_team": "No Odds Away",
        "bookmakers": [],
    }
]
