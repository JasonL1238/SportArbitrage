"""Replay fixture data for the ESPN odds adapter.

These mirror the structure of ESPN's public odds JSON so the parser
can be tested without network access.
"""

ESPN_NBA_ODDS_SNAPSHOT: list[dict] = [
    {
        "id": "401584901",
        "date": "2025-07-10T00:00:00Z",
        "shortName": "LAL vs BOS",
        "league": {"slug": "nba"},
        "competitions": [
            {
                "id": "401584901",
                "date": "2025-07-10T00:00:00Z",
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"displayName": "Los Angeles Lakers", "name": "Lakers"},
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": "Boston Celtics", "name": "Celtics"},
                    },
                ],
                "odds": [
                    {
                        "provider": {"name": "DraftKings"},
                        "homeTeamOdds": {
                            "moneyLine": -150,
                            "spreadOdds": -110,
                            "spread": "-3.5",
                        },
                        "awayTeamOdds": {
                            "moneyLine": 130,
                            "spreadOdds": -110,
                            "spread": "+3.5",
                        },
                        "overUnder": 215.5,
                        "overOdds": -110,
                        "underOdds": -110,
                    },
                ],
            },
        ],
    },
]

ESPN_MLB_ODDS_SNAPSHOT: list[dict] = [
    {
        "id": "401590001",
        "date": "2025-07-10T19:00:00Z",
        "shortName": "NYY vs BOS",
        "league": {"slug": "mlb"},
        "competitions": [
            {
                "id": "401590001",
                "date": "2025-07-10T19:00:00Z",
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"displayName": "New York Yankees", "name": "Yankees"},
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": "Boston Red Sox", "name": "Red Sox"},
                    },
                ],
                "odds": [
                    {
                        "provider": {"name": "ESPN BET"},
                        "homeTeamOdds": {
                            "moneyLine": -180,
                            "spreadOdds": -115,
                            "spread": "-1.5",
                        },
                        "awayTeamOdds": {
                            "moneyLine": 160,
                            "spreadOdds": -105,
                            "spread": "+1.5",
                        },
                        "overUnder": 9.0,
                        "overOdds": -105,
                        "underOdds": -115,
                    },
                ],
            },
        ],
    },
]

ESPN_NO_ODDS_SNAPSHOT: list[dict] = [
    {
        "id": "401590099",
        "date": "2025-07-11T00:00:00Z",
        "shortName": "MIL vs PHX",
        "league": {"slug": "nba"},
        "competitions": [
            {
                "id": "401590099",
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Milwaukee Bucks"}},
                    {"homeAway": "away", "team": {"displayName": "Phoenix Suns"}},
                ],
                "odds": [],
            },
        ],
    },
]

ESPN_MULTI_PROVIDER_SNAPSHOT: list[dict] = [
    {
        "id": "401584902",
        "date": "2025-07-10T02:00:00Z",
        "shortName": "GSW vs DEN",
        "league": {"slug": "nba"},
        "competitions": [
            {
                "id": "401584902",
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Golden State Warriors"}},
                    {"homeAway": "away", "team": {"displayName": "Denver Nuggets"}},
                ],
                "odds": [
                    {
                        "provider": {"name": "DraftKings"},
                        "homeTeamOdds": {"moneyLine": 120, "spreadOdds": -110, "spread": "+2.5"},
                        "awayTeamOdds": {"moneyLine": -140, "spreadOdds": -110, "spread": "-2.5"},
                        "overUnder": 225.5,
                        "overOdds": -108,
                        "underOdds": -112,
                    },
                    {
                        "provider": {"name": "ESPN BET"},
                        "homeTeamOdds": {"moneyLine": 125, "spreadOdds": -105, "spread": "+2.5"},
                        "awayTeamOdds": {"moneyLine": -145, "spreadOdds": -115, "spread": "-2.5"},
                        "overUnder": 225.5,
                        "overOdds": -110,
                        "underOdds": -110,
                    },
                ],
            },
        ],
    },
]
