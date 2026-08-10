"""Build a local dashboard from what the collector has already stored.

This reads the SQLite store and writes one self-contained HTML file.  It is a
*view*, never a second source of truth: every number on the page comes from a
query in this module, and the module never fetches anything.  That keeps the
report honest — if the page shows 1,656 rows it is because 1,656 rows are in the
database, and generating the report cannot alter what was collected.

The page opens from ``file://`` with no network access: no CDN, no webfont, no
remote image, no telemetry.

Now that several sports are collected, the page has to answer one more question
before any of its prices mean anything: **which sports are actually usable?**  A
sport priced by a single book cannot be compared against anything, so a large row
count for it is not progress.  The coverage grid states the per-sport, per-book
counts and marks every sport against the two-book bar, and the sport filter lets
the rest of the page be read one sport at a time.

    python -m src.report                 # write data/dashboard.html
    python -m src.report --open          # ... and open it
    python -m src.report --serve 8765 --open   # local UI with a Scrape button
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from src import settings
from src.arb import (
    DEFAULT_TOTAL_STAKE,
    Opportunity,
    counterparty_groups,
    find_opportunities,
    merge_counterparty_groups,
)
from src.betlinks import link_payload
from src.betlog import BetLog, BetLogError, empty_payload as empty_bet_payload
from src.betlog import slip_from_payload
from src.commission import commission_for, net_decimal_odds
from src.coverage import LocalityMarking, locality_marking
from src.egress import is_recent, load_detection
from src.events import reconcile_event_keys
from src.jurisdictions import JURISDICTIONS, route_warnings, source_host
from src.leagues import is_known
from src.leagues import league as get_league
from src.report_assets import BODY, CSS, JS
from src.schema import (
    PERIOD_RULES,
    Market,
    Period,
    QuoteStatus,
    Selection,
    Side,
    Sport,
    period_rules,
    scoring_unit,
)
from src.settlement import SettlementRegime, regime_for
from src.store import IncompatibleDatabase, Store

#: How many recent runs to embed quote rows for.  Bounded because the page holds
#: every row in memory; the run list itself always covers more runs than this.
DEFAULT_QUOTE_RUNS = 6
DEFAULT_RUN_LIMIT = 40

#: Hard ceiling on the rows embedded in the page, whatever ``--quote-runs`` asks
#: for.  ``--quote-runs`` bounds the number of *collections*, which is the wrong
#: unit once the source list grows: six runs of three books is 200,000 rows and
#: six runs of thirty books is two million.  Measured behaviour of the page at
#: various sizes — 34,000 rows embeds as roughly 20 MB of JSON, 340,000 as 200 MB
#: with the builder's memory in gigabytes, and 3.4 million exceeds V8's maximum
#: string length and renders blank.  Interactivity goes first, somewhere around
#: 50-100k rows, because each keystroke in the search box re-filters every row.
#:
#: 150,000 sits above a realistic multi-run slate and well below where the page
#: stops working.  Exceeding it truncates to the newest rows and says so on the
#: page, rather than producing a file nobody can open.
DEFAULT_MAX_QUOTE_ROWS = 150_000

#: Two books is the bar for a sport being comparable at all.  Mirrors
#: ``src.collector.MIN_HEALTHY_SOURCES``; kept as its own constant rather than
#: imported, because the report must stay usable when the adapters are not.
MIN_BOOKS_FOR_COMPARISON = 2

#: What each source is, in the reader's terms rather than the parser's.
SOURCE_NOTES: dict[str, dict[str, str]] = {
    "fanduel": {
        "label": "FanDuel",
        "host": "sbapi.il.sportsbook.fanduel.com",
        "kind": "sportsbook",
        "what": "Read from the feeds behind FanDuel's own sport pages. Whole-game bets only "
                "— who wins, the handicap, and the combined total — but it does publish "
                "extra numbers beyond its main line.",
    },
    "pinnacle": {
        "label": "Pinnacle",
        "host": "guest.api.arcadia.pinnacle.com",
        "kind": "sportsbook",
        "what": "The most detailed of the sportsbooks: parts of a game as well as the "
                "whole, and the one that publishes the largest bet it will accept.",
    },
    "betrivers_kambi": {
        "label": "BetRivers",
        "host": "eu-offering-api.kambicdn.com",
        "kind": "sportsbook",
        "what": "Lists the most fixtures, and says in words which part of the game each bet "
                "covers — including whether overtime counts. That is what let Pinnacle's "
                "unlabelled period numbering be verified.",
    },
    "leovegas_kambi": {
        "label": "LeoVegas",
        "host": "eu-offering-api.kambicdn.com",
        "kind": "sportsbook",
        "what": "A second book on the same platform as BetRivers, run by a different "
                "company. Whether that makes it a second opinion or the same one twice is "
                "measured on every run rather than assumed — see the sources page.",
    },
    "bovada": {
        "label": "Bovada",
        "host": "www.bovada.lv",
        "kind": "sportsbook",
        "what": "Reads the coupon feed behind its own sport pages, and is the only source "
                "that states outright which side it counts as home — which is how the "
                "others' orderings were checked.",
    },
    "betmgm": {
        "label": "BetMGM",
        "host": "www.il.betmgm.com",
        "kind": "sportsbook",
        "what": "US retail book via Entain's public CDS fixtures feed. Illinois answers "
                "an honest User-Agent with a public access id, so whole-game moneylines, "
                "spreads and totals can be collected without a browser session.",
    },
    "draftkings": {
        "label": "DraftKings",
        "host": "sportsbook-nash.draftkings.com",
        "kind": "sportsbook",
        "what": "First-party feed behind DraftKings's own state door — the US-ST-SB "
                "site path, ST being the state code. Publishes both a display price "
                "and a true price per selection; the true one is what gets recorded.",
    },
    "hardrock": {
        "label": "Hard Rock Bet",
        "host": "api.hardrocksportsbook.com",
        "kind": "sportsbook",
        "what": "First-party GraphQL feed behind Hard Rock's own app. Needs a "
                "licensed-state egress to answer with a slate at all.",
    },
    "caesars": {
        "label": "Caesars",
        "host": "api.americanwagering.com",
        "kind": "sportsbook",
        "what": "First-party feed behind Caesars's own per-state path. Its CDN refuses "
                "some egresses outright, which is recorded as the wall it is rather "
                "than worked around.",
    },
    "cloudbet": {
        "label": "Cloudbet",
        "host": "www.cloudbet.com",
        "kind": "sportsbook",
        "what": "Public sports-api list plus per-event detail: moneyline, handicap and "
                "totals (including baseball first-five), with home/away stated on the event.",
    },
    "onexbet": {
        "label": "1xBet",
        "host": "1xbet.com",
        "kind": "sportsbook",
        "what": "LineFeed Get1x2_VZip championship slates — short-key moneyline, handicap "
                "and total selections filtered by the league label string.",
    },
    "an_draftkings": {
        "label": "DraftKings (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "DraftKings prices as Action Network publishes them on its public "
                "scoreboard — a secondary path when a book's own edge is unreachable.",
    },
    "an_caesars": {
        "label": "Caesars (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Caesars prices from Action Network's public scoreboard.",
    },
    "an_bet365": {
        "label": "Bet365 (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Bet365 prices from Action Network's public scoreboard.",
    },
    "vi_draftkings": {
        "label": "DraftKings (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "DraftKings's named column on VegasInsider's public comparison table. "
                "A redundant observation path, never a separate counterparty.",
    },
    "vi_caesars": {
        "label": "Caesars (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "Caesars's named column on VegasInsider's public comparison table. "
                "A redundant observation path, never a separate counterparty.",
    },
    "vi_hardrock": {
        "label": "Hard Rock Bet (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "Hard Rock Bet's named VegasInsider column, retained as fallback and "
                "cross-check data for the same sportsbook.",
    },
    "vi_fanatics": {
        "label": "Fanatics (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "Fanatics's named VegasInsider column, retained as fallback and "
                "cross-check data for the same sportsbook.",
    },
    "vi_bet365": {
        "label": "bet365 (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "bet365's named VegasInsider column, retained as fallback and "
                "cross-check data for the same sportsbook.",
    },
    "vi_betmgm": {
        "label": "BetMGM (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "BetMGM's named column on VegasInsider's public comparison table. "
                "A redundant observation path, never a separate counterparty.",
    },
    "vi_fanduel": {
        "label": "FanDuel (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "FanDuel's named column on VegasInsider's public comparison table. "
                "A redundant observation path, never a separate counterparty.",
    },
    "vi_betrivers": {
        "label": "BetRivers (VegasInsider)",
        "host": "www.vegasinsider.com",
        "kind": "sportsbook",
        "what": "BetRivers's named column on VegasInsider's public comparison table. "
                "A redundant observation path, never a separate counterparty.",
    },
    "vsin_circa": {
        "label": "Circa (VSiN)",
        "host": "data.vsin.com",
        "kind": "sportsbook",
        "what": "Circa's named column on VSiN's public Las Vegas line tracker. "
                "Circa lists VSiN as an odds aggregator; this is fallback and "
                "cross-check data, never a separate counterparty.",
    },
    "an_open": {
        "label": "Open (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Action Network's Open column — opening / consensus lines for "
                "context on the odds board. Not a book you can bet; excluded from "
                "arbitrage and best-price highlighting.",
    },
    "an_fanduel": {
        "label": "FanDuel (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "FanDuel prices as Action Network publishes them — a redundant secondary "
                "feed beside the primary FanDuel adapter, kept for durability.",
    },
    "an_betrivers": {
        "label": "BetRivers (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "BetRivers prices as Action Network publishes them — a redundant secondary "
                "feed beside the Kambi adapter, kept for durability.",
    },
    "an_betmgm": {
        "label": "BetMGM (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "BetMGM prices as Action Network publishes them — a redundant secondary "
                "feed beside the Entain CDS adapter, kept for durability.",
    },
    "an_bovada": {
        "label": "Bovada (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Bovada prices as Action Network publishes them — a redundant secondary "
                "feed beside the primary Bovada adapter, kept for durability.",
    },
    "an_onexbet": {
        "label": "1xBet (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "1xBet prices as Action Network publishes them — a redundant secondary "
                "feed beside the primary 1xBet adapter, kept for durability.",
    },
    "an_parx": {
        "label": "betPARX (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "betPARX prices from Action Network's public scoreboard — the only "
                "observation path into this book until a first-party route exists.",
    },
    "an_unibet": {
        "label": "Unibet (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Unibet prices from Action Network's public scoreboard. Watched for "
                "Mohegan Pennsylvania, whose online skin ran on Unibet's licence — a "
                "mapping the venue's own records would have to confirm.",
    },
    "an_thescore": {
        "label": "theScore Bet (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "theScore Bet prices from Action Network's public scoreboard — the only "
                "observation path into this book until a first-party route exists.",
    },
    "an_fanatics": {
        "label": "Fanatics (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Fanatics prices from Action Network's public scoreboard.",
    },
    "an_hardrock": {
        "label": "Hard Rock (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Hard Rock prices from Action Network's public scoreboard — a secondary "
                "feed beside the first-party Hard Rock adapter.",
    },
    "an_bally": {
        "label": "Bally Bet (Action Network)",
        "host": "api.actionnetwork.com",
        "kind": "sportsbook",
        "what": "Bally Bet prices from Action Network's public scoreboard; New Jersey "
                "is the only licence its id table covers.",
    },
    "matchbook": {
        "label": "Matchbook",
        "host": "www.matchbook.com",
        "kind": "exchange",
        "what": "Not a bookmaker: you are matched against another customer. Only prices "
                "somebody is actually offering are collected, each with the amount behind "
                "it, and Matchbook takes a share of your winnings on top.",
    },
    "smarkets": {
        "label": "Smarkets",
        "host": "api.smarkets.com",
        "kind": "exchange",
        "what": "A second exchange. Its rate limit is tight enough that only the "
                "who-wins market is collected — asking for handicaps and totals as well "
                "costs more requests than it will answer.",
    },
    "sxbet": {
        "label": "SX Bet",
        "host": "api.sx.bet",
        "kind": "exchange",
        "what": "An exchange whose order book is public. Prices are what a taker can hit, "
                "converted from the maker's side, and its own fee is charged on winnings.",
    },
    "kalshi": {
        "label": "Kalshi",
        "host": "api.elections.kalshi.com",
        "kind": "prediction market",
        "what": "A regulated exchange in contracts rather than bets: each side is its own "
                "order book, there is a fee per contract, and a cancelled game is not "
                "refunded the way a book refunds it.",
    },
    "polymarket": {
        "label": "Polymarket",
        "host": "gamma-api.polymarket.com",
        "kind": "prediction market",
        "what": "Contracts again, priced between 0 and 1. A cancelled game resolves every "
                "contract at 0.50 whatever you paid, which is not what a book does with "
                "the same fixture.",
    },
}

#: What each kind of venue is, in the reader's terms.  The distinction is not
#: decoration: it decides whether the number on the screen is the number you are
#: paid, whether there is a stated amount behind it, and what happens to your
#: stake if the game is called off.
VENUE_KINDS: dict[str, str] = {
    "sportsbook": "Takes the other side of your bet itself. Its margin is already inside "
                  "the price shown, and it refunds a cancelled game.",
    "exchange": "Matches you against another customer. The price is somebody's actual "
                "offer, with an amount behind it, and the venue charges a commission on "
                "your winnings — so the price shown is not the price you are paid.",
    "prediction market": "Trades contracts that pay 1 if the thing happens. There is a "
                         "fee per contract, each side is its own order book, and a "
                         "cancelled game is resolved rather than refunded.",
}

#: Why an offer the books do publish is deliberately not collected.  Matched by
#: prefix, longest first, so a specific reason beats the family it belongs to.
SKIP_NOTES: list[tuple[str, str]] = [
    # ── not a single fixture ─────────────────────────────────────────────────
    ("market_on_non_game_event",
     "A market attached to a season-long container — a futures or specials page — "
     "rather than to a fixture."),
    ("non_game_event",
     "A season-long or tournament-long bet: division winners, awards, outrights. Not a "
     "single fixture."),
    ("market_on_out_of_scope_matchup",
     "A market belonging to something already ruled out as a fixture, so the market "
     "goes with it rather than being kept as an orphan."),
    ("participant_priced_market",
     "Priced per competitor with no home/away side to it — the shape a futures market "
     "has. Reading it as a fixture would invent a game."),
    ("matchup_type:special",
     "Bets on individual players, or on the exact final score. Not a bet on the "
     "fixture itself, so out of scope."),
    ("child_matchup",
     "A duplicate of a fixture the book also lists in a different scoring unit — games "
     "won rather than sets, corners rather than goals. Treating it as its own fixture "
     "would fabricate a second game between the same two competitors."),
    ("alternate_scoring_unit",
     "The same fixture priced in a different unit (games, corners). A handicap in games "
     "is not a handicap in sets, so it is not filed as one."),
    ("corners_market",
     "Corners rather than goals. A different thing being counted."),
    ("tennis_doubles", "A doubles match: four competitors, not two."),
    ("market_on_doubles_event", "Belongs to a doubles match, which is out of scope."),

    ("incomplete_event_header",
     "A comparison page listed a game whose header was missing an id, a start time or "
     "a team name, so the row could not be tied to a fixture. Reading it anyway would "
     "attach somebody's prices to the wrong game."),

    # ── already started, or already collected ────────────────────────────────
    ("event_already_started",
     "The fixture is under way. Only pre-match prices are collected, because an in-play "
     "price and a pre-match price are not the same market."),
    ("orders_response_truncated",
     "The venue returned a full page of orders and this market was not in it, so whether "
     "anyone is offering this side is unknown rather than no."),
    ("orders_page_cap_reached",
     "One market has more than a page of resting orders, so the best price shown may not "
     "be the best price offered."),
    ("whole_number_strike",
     "A contract whose line is a whole number. It settles on a strict inequality, so an "
     "exact landing is a loss for one side rather than the push a whole line means "
     "everywhere else — a different contract, not a version of this one."),
    # ── the venues added after this table was first written ─────────────────
    #
    # Every one of these rendered as "Not one of the four kinds of game bet
    # collected here", because the test guarding the table was a hand-written
    # list of three sources.  2,540 of 8,479 skipped records on the captured
    # slate carried that answer, and several contradicted it outright.
    ("market_in_scope_but_not_fetched",
     "One of the four kinds collected here, which this venue does publish and this "
     "collector does not ask it for — its published rate limit does not leave room for "
     "the whole ladder. A coverage choice, not a fact about the market."),
    ("market_type_out_of_scope",
     "A market this venue offers that is not one of the four kinds collected here — "
     "the venue's own type code is on the end of the reason."),
    ("market_on_out_of_scope_event",
     "Belongs to a fixture that was not collected, so there is nothing to attach it to."),
    ("market_on_out_of_scope_game",
     "Belongs to a fixture that was not collected, so there is nothing to attach it to."),
    ("game_already_started",
     "The fixture is under way. Only pre-match prices are collected, because an in-play "
     "price and a pre-match price are not the same market."),
    ("event_in_running",
     "The venue flags this fixture as in-running, so its prices are live ones."),
    ("event_live",
     "The venue flags this fixture as live, so its prices are in-play ones."),
    ("draw_not_priced_for_sport",
     "A draw price on a sport or window where a level score is not a settlement "
     "outcome."),
    ("no_resting_order_for_outcome",
     "Nobody is offering this side on the exchange. Ordinary on a thin order book, and "
     "the reason an exchange row is not the same thing as a book's posted price."),
    ("runner_without_a_back_price",
     "Nobody is offering this side on the exchange. Ordinary on a thin order book, and "
     "the reason an exchange row is not the same thing as a book's posted price."),
    ("contract_without_a_takeable_offer",
     "The contract exists but nothing is currently offered on it at any price."),
    ("league_unmapped",
     "A competition this venue prices that the league registry does not carry, so the "
     "rows cannot be filed under a competition and are captured rather than guessed at."),
    ("competition_unmapped",
     "A competition this venue prices that the league registry does not carry."),
    ("event_beyond_the_leagues_schedule_horizon",
     "Further ahead than this competition schedules, so it is a placeholder rather than "
     "a fixture with a real date."),
    ("duplicate_event",
     "The same fixture appeared twice in one response; kept once and counted here."),
    ("navigation_response",
     "A menu listing rather than prices — it maps competition ids to names and carries "
     "no odds."),

    ("market_on_started_event",
     "Belongs to a fixture already under way, so it is not a pre-match price."),
    ("market_in_play",
     "The book itself flags this market as in-play. Read from the feed rather than "
     "inferred from the kick-off time, which admits a market that has gone live in the "
     "minute before it."),
    ("already_collected_from_sport_page",
     "The same market was already read from another of this book's pages. Kept once, "
     "counted here, so the duplicate is visible rather than silently dropped."),

    # ── a different contract from the one it resembles ───────────────────────
    ("out_of_scope_market:different_settlement_window",
     "Settles on a stretch of the game this tool does not track, and no second book "
     "labels it the same way, so there is no way to confirm what it settles on."),
    ("out_of_scope_market:voids_on_draw",
     "Your stake comes back if the scores are level, which makes it a different bet "
     "from picking a winner."),
    ("out_of_scope_market:bonus_settlement_rule",
     "Carries an extra payout rule, so it is not the plain market it looks like."),
    ("out_of_scope_market:combined_outcome",
     "One bet on two things happening together. Not one of the four kinds collected."),
    ("out_of_scope_market:margin_prop",
     "A bet on the exact winning margin rather than on a handicap."),
    ("out_of_scope_market:goal_prop", "A bet about a specific goal, not about the result."),
    ("out_of_scope_market:period_out_of_scope",
     "A part of the game whose settlement rules are not recorded, so the pipeline "
     "refuses to guess whether a tie voids the bet."),
    ("out_of_scope_market:prop", "A bet on a player or an occurrence within the game."),
    ("out_of_scope_market", "A kind of market outside the four collected here."),
    ("period_out_of_scope",
     "A part of the game whose settlement rules are not recorded. Collecting it would "
     "mean guessing whether a level score voids the bet, and that guess is worth the "
     "whole stake."),
    ("market_out_of_scope",
     "A market this sport does not have in a comparable form here — a tennis handicap "
     "in games, for instance."),
    ("not_a_full_period_result",
     "Settles on something other than the result of a whole scoring window."),
    ("price_outside_the_plausible_band",
     "A price so far outside the normal range that publishing it would be a units "
     "error waiting to happen. On an order-driven venue it is usually a real resting "
     "order at an extreme — somebody offering a hundred to one — which is genuine and "
     "still not a comparable price. Counted rather than faulted, because the venue is "
     "working normally."),
    ("market_prices_one_side_twice",
     "One market published two prices for the same side of the fixture, which a "
     "two-outcome market cannot do. The payload named one competitor on both "
     "outcomes, and the pair would otherwise read as a hedge while both bets sat on "
     "the same team."),
    ("market_prices_the_book_to_lose",
     "A line tracker's column showed a complete market whose prices sum at or "
     "below fair — a combination no book hangs, since a book's pairing always "
     "carries margin; it is produced when the page's cells update one at a time "
     "and the read landed mid-move. Both legs are dropped, because either one "
     "paired with another book's genuine other side reads as an arbitrage that "
     "does not exist."),
    ("event_not_open_pregame",
     "The venue no longer lists this fixture as open for pre-match trading — it has "
     "started, been settled, or been pulled — so its prices are not the pre-match "
     "prices this pipeline compares."),
    ("not_a_two_outcome_market",
     "An order-driven market with more or fewer than two outcomes. Where only bids "
     "rest on the book, one side's price is derived from the other side's best bid, "
     "and that derivation means nothing when there is no single other side."),
    ("half_only_market",
     "Settles at half time only, and is collected only where the book labels the period "
     "unambiguously."),
    ("draw_voids_market",
     "A two-way price on a fixture that can end level: a draw refunds the stake. That "
     "is a different contract from a three-way result market."),
    ("soccer_3way_handicap_prices_a_draw",
     "A handicap with three results, including the draw. Not the same product as the "
     "two-way handicap, so it is not filed as one."),
    ("asian_quarter_line_splits_the_stake",
     "A quarter line splits your stake across two numbers. Merging it with the ordinary "
     "total would misstate what settles."),
    ("odd_even_market",
     "Whether the total is an odd or even number. No over/under and no number, so it is "
     "not a total."),
    ("yes_no_market", "A yes/no proposition rather than a result, handicap or total."),
    ("combined_outcome_market",
     "One bet on two outcomes together — a different contract from either alone."),
    ("correct_score_market", "The exact final score, not the result."),
    ("player_prop", "A bet on one player's performance rather than on the fixture."),
    ("pitcher_conditional_market",
     "Cancelled unless a named starter actually plays. That condition makes it a "
     "different bet from the plain one."),
    ("opta_derived_market",
     "Priced off a third-party statistics feed rather than being one of the book's own "
     "game markets."),
    ("unmapped_criterion",
     "The book's own name for this market is not one of the labels this tool recognises. "
     "Guessing which of the four kinds it is would put a false price in the data, so it "
     "is counted and left alone."),
    ("criterion:",
     "The book's own label for a market outside the four kinds collected here."),
    ("extra_time",
     "A cup tie's extra time, or 'to qualify'. A different contract from the 90 minutes "
     "the ordinary market settles on."),
    ("market_type:OUTRIGHT",
     "A season-long or tournament-long bet rather than a single fixture."),
    ("futures", "A season-long or tournament-long bet rather than a single fixture."),
    ("outright", "A season-long or tournament-long bet rather than a single fixture."),
    ("unknown_league",
     "A competition this tool has no recorded rules for. Counted rather than guessed at."),
    ("competition_out_of_scope",
     "A competition this collector was not asked for, or one outside the leagues it "
     "knows how to settle."),
    ("doubles_or_team_pairing",
     "A doubles or team pairing rather than a two-competitor fixture."),
    ("first_to_score_market",
     "Who scores first — a different contract from the result, handicap or total."),
    ("combined_or_prop_market",
     "A combined outcome or player prop, not one of the four game markets collected."),
    ("promo_market",
     "A promotional or boosted market with settlement rules that are not the plain "
     "game market it resembles."),
    ("set_only_market",
     "Settles on a single set rather than the match, so it is not the full-game "
     "contract collected here."),
    ("outcome_not_visible",
     "A selection the book is not currently showing a price for."),
    ("virtual_event",
     "A simulated or virtual fixture, not a real scheduled contest."),
    ("league_not_requested",
     "A competition outside the leagues this scrape was asked to collect."),
    ("league_out_of_scope",
     "A competition outside the leagues this scrape was asked to collect."),
    ("event_state:",
     "The book flags this fixture in a state that is not a pre-match price "
     "(started, live, or finished)."),
    ("statistic_not_a_fixture",
     "A statistics or props container, not a two-competitor fixture."),
    ("competition_missing",
     "The book listed a fixture without naming its competition."),
    ("unknown_competition",
     "A competition this tool has no recorded rules for."),
    ("market_not_visible",
     "A market the book is not currently offering."),
    ("duplicate_market_id",
     "The same market id appeared twice; kept once and counted here."),
    ("option_market_not_an_object",
     "A market entry the book returned in a shape that is not an object."),
    ("game_market_not_an_object",
     "A game-market entry the book returned in a shape that is not an object."),
    ("option_not_an_object",
     "A selection the book returned in a shape that is not an object."),
    ("outcome_not_an_object",
     "A selection the book returned in a shape that is not an object."),
    ("fixture_not_an_object",
     "A fixture entry the book returned in a shape that is not an object."),
    ("outcome_without_price",
     "A selection the book listed without a price. There is nothing to record."),
    ("implausible_price",
     "A price outside the range a real game market can have — refused rather than stored."),
    ("period_only_market",
     "Settles on a single period rather than the full game collected here."),
    ("outright_or_series",
     "A series, series-winner, or to-qualify market — not a single fixture result."),
    ("unmapped_market:",
     "The book's own name for this market is not one of the labels this tool "
     "recognises, so it is counted and left alone rather than guessed at."),

    # ── the row itself was unusable ──────────────────────────────────────────
    ("runner_without_price",
     "A selection the book listed without a price. There is nothing to record."),
    ("outcome_without_odds",
     "A selection the book listed without a price. There is nothing to record."),

    # ── new multi-source adapters (prefix-matched) ───────────────────────────
    ("odds_for_other_book",
     "An Action Network scoreboard row belonging to a different book_id than this "
     "registered source."),
    ("event_status:",
     "The fixture is not in a pregame state the adapter collects (complete, live, "
     "delayed, …)."),
    ("period_out_of_scope:",
     "A partial period (half, quarter, …) outside the windows this pipeline prices."),
    ("market_type_out_of_scope:",
     "A market type this adapter does not map — player props, races, odd/even, …"),
    ("market_in_scope_but_not_fetched:",
     "A market the source can price but this request tier or view did not return."),
    ("market_out_of_scope:",
     "A market shape this source deliberately does not collect for that sport."),
    ("unmapped_criterion:",
     "A bet-offer criterion label that has no mapping in this adapter."),
    ("league_unmapped:",
     "A competition the adapter saw but has no canonical league key for."),
    ("alternate_scoring_unit_games",
     "Priced in games rather than the sport's primary scoring unit."),
    ("child_matchup:",
     "A child matchup in an alternate scoring unit of a parent fixture."),
    ("already_collected_from_sport_page:",
     "Already collected from another endpoint of the same source."),
    ("empty_linefeed",
     "The LineFeed response body was empty."),
    ("soccer_total_out_of_scale",
     "A soccer total whose line is far above game goals — usually corners."),
    ("line_out_of_scale",
     "A handicap/total line far outside the sport's plausible magnitude."),
    ("mirror_spread_framing",
     "The opposite home/away framing of the same handicap (already collected)."),
    ("total_out_of_band",
     "A total whose line sits outside a plausible range for that sport."),
    ("unknown_selection_type:",
     "A 1xBet selection-type code this adapter does not map."),
    ("sport_market_out_of_scope:",
     "A market type this adapter does not collect for that sport."),
    ("draw_not_priced",
     "A draw row on a sport/period where regulation draw is not a settlement."),
    ("regulation_three_way_unpriced",
     "A regulation 1X2 moneyline on a sport that settles in OT/SO, so the draw is not a priced settlement."),
    ("markets_missing",
     "An event detail payload had no markets object."),
    ("event_not_an_object",
     "An event entry that was not a JSON object."),
    ("selection_not_an_object",
     "A selection entry that was not a JSON object."),
    ("unreadable_odds",
     "Odds that could not be read as a number."),
    ("price_below_plausible_minimum",
     "A price below the lowest plausible decimal odds."),
    ("selection_disabled",
     "The venue marked this selection as not currently bettable."),
    ("unmapped_outcome_label",
     "An outcome label that could not be matched to home or away."),
    ("offer_suspended",
     "The bet offer is suspended."),
    ("odds_not_an_object",
     "An odds entry that was not a JSON object."),
    ("game_not_an_object",
     "A game entry that was not a JSON object."),
    ("missing_home_away",
     "The fixture did not state both participants."),
    ("bad_price_row",
     "A price row missing a usable type or coefficient."),
    ("bad_spread_line",
     "A spread line that could not be read as a number."),
    ("bad_total_line",
     "A total line that could not be read as a number."),
    ("unreadable_american_odds",
     "American odds that could not be parsed as an integer."),
    ("implausible_odds",
     "Odds outside the plausible decimal range."),
    ("missing_line",
     "A line market without a numeric line."),
    ("missing_price",
     "A selection without a price."),
    ("missing_event_id",
     "An event with no id."),
    ("outcome:",
     "An outcome name this adapter does not map."),
    ("market:",
     "A market label this adapter does not collect."),
    ("status:",
     "A venue status value outside the collectable set."),
    ("state:",
     "A venue state value outside the collectable set."),
]

#: Field-by-field reference, shown so "one consistent schema" is inspectable.
#: Every field of :class:`~src.schema.Quote` must appear — a field with no
#: explanation would render as a blank row in the page's own answer to "what is
#: one row?".
SCHEMA_FIELDS: list[tuple[str, str, bool, str]] = [
    ("source", "str", True, "Which sportsbook this price came from."),
    ("observed_at", "datetime", True, "When this collector fetched the payload. Never a book's own clock."),
    ("raw_ref", "str", True,
     "Points at the stored raw response the price was parsed from. Single-valued, so "
     "it can be checked against the response's own fetch time."),
    ("identity_raw_ref", "str", False,
     "Points at the response that supplied the fixture's identity, when that came from "
     "a different call than the price — Pinnacle needs two. Kept separate so a reader "
     "never has to split one field on a grammar nobody declared."),
    ("sport", "enum", True,
     "Which sport. Part of every fixture's identity, so a baseball handicap and a "
     "soccer handicap can never be joined even though both are 'spread'."),
    ("league", "str", True,
     "Canonical competition key — MLB, WNBA, EPL, ATP. Recorded for coverage "
     "reporting, and deliberately not part of the fixture key: the books disagree "
     "about classification constantly, and that must not break a join."),
    ("event_key", "str", True,
     "Shared fixture identity: AWAY@HOME:date, built from participant keys on the "
     "league's own scheduling date."),
    ("source_event_id", "str", True, "The book's own id for the fixture."),
    ("home_participant", "str", True,
     "Resolved identity of the home side (MLB-MIA, SOCCER-arsenal). This is what "
     "everything joins on."),
    ("away_participant", "str", True,
     "Resolved identity of the away side. In tennis there is no home player, so the "
     "two are ordered deterministically instead of trusting the book's ordering."),
    ("home_team", "str", True, "Home side's canonical display name, for humans."),
    ("away_team", "str", True, "Away side's canonical display name, for humans."),
    ("commence_time", "datetime", True, "Scheduled start, in UTC."),
    ("market", "enum", True,
     "One of four: who wins, winner with a handicap, combined total, one side's total."),
    ("period", "enum", True,
     "The scoring window the bet settles on: the whole game including overtime, "
     "regulation only, a half, or an opening stretch of innings."),
    ("selection", "enum", True, "home, away, draw, over or under."),
    ("side", "enum", False, "Which participant a team total refers to. Set only for team totals."),
    ("line", "float", False, "The handicap or total, from this selection's perspective. Set only where meaningful."),
    ("is_alternate", "bool", True, "True for an alternate-line market. Books really do price the same number twice."),
    ("decimal_odds", "float", True, "Price as a decimal multiplier. Must be above 1.0."),
    ("american_odds", "int", True, "The same price in American format."),
    ("implied_probability", "float", True, "1 / decimal odds, as the book implies it."),
    ("source_market_id", "str", False, "The book's own market id — what defines one market group."),
    ("source_selection_id", "str", False, "The book's own id for this selection."),
    ("limit_amount", "float", False, "Maximum stake, where the book publishes one."),
    ("status", "enum", True, "active or suspended."),
    ("last_change_at", "datetime", False, "When the book says the price last moved, where it says so."),
]

#: Plain-language glossary.  Betting notation is compact but opaque, and this page is
#: meant to be readable by someone who has never placed a bet, so every term the
#: tables use is spelled out here alongside the word a sportsbook would use.
GLOSSARY: list[dict[str, str]] = [
    {
        "term": "Sportsbook",
        "plain": "A company that takes bets, and takes the other side of yours. This "
                 "tool reads five: FanDuel, Pinnacle, BetRivers, LeoVegas and Bovada. "
                 "It also reads five venues that are not sportsbooks — see "
                 "\u201cExchange\u201d and \u201cPrediction market\u201d.",
    },
    {
        "term": "Exchange",
        "plain": "Not a bookmaker. It matches you against another customer, shows the "
                 "amount actually behind each price, and takes a commission out of your "
                 "winnings — so the price on the screen is better than the price you are "
                 "paid. Matchbook, Smarkets and SX Bet.",
    },
    {
        "term": "Prediction market",
        "plain": "Trades contracts that pay 1 if the thing happens, at a price between 0 "
                 "and 1, with a fee per contract. Each side is a separate order book, so "
                 "the two need not add up the way a book's do. Kalshi and Polymarket.",
    },
    {
        "term": "Commission",
        "plain": "What an exchange or prediction market charges. An exchange takes a "
                 "share of a winning bet; a prediction market charges a fee per contract "
                 "when you enter, whether or not it settles your way — so on a hedge the "
                 "losing leg is not free. It is "
                 "not in the quoted price, so every comparison on this page uses the "
                 "price after commission — otherwise the venues that charge would look "
                 "like the best ones on every fixture.",
    },
    {
        "term": "A price (decimal)",
        "plain": "What a winning bet pays back in total, per unit staked. 2.30 means a "
                 "winning $100 bet returns $230 — your $100 back plus $130 profit.",
    },
    {
        "term": "American odds",
        "plain": "The same price written the way US books show it. +130 means a $100 bet "
                 "profits $130. −150 means you must stake $150 to profit $100. A minus "
                 "sign marks the favourite.",
    },
    {
        "term": "The book's implied chance",
        "plain": "Turn the price back into a percentage: 1 ÷ 2.30 = 43%. That is roughly "
                 "how likely the book is treating that outcome — plus a bit extra, which "
                 "is its cut.",
    },
    {
        "term": "The book's margin",
        "plain": "Add up the implied chances for every side of one bet and it comes to "
                 "more than 100%. The excess is the book's built-in profit. Around 4–7% "
                 "is normal. Under 100% would be impossible, and would mean this tool had "
                 "paired the wrong prices together.",
    },
    {
        "term": "Two or more books",
        "plain": "The bar for a sport being usable at all. A single book's price cannot "
                 "be compared with anything, so a sport only one book covers is listed "
                 "as not comparable however many prices it has.",
    },
    {
        "term": "Who wins",
        "plain": "The simplest bet: pick the winner, no adjustments. Books call it the "
                 "moneyline. Where the window being priced can end level — soccer, and "
                 "any part-game window such as hockey regulation time, a baseball "
                 "half-inning stretch or a first half — a draw is a third thing you can "
                 "back. The Settlement panel lists exactly which windows those are; this "
                 "sentence used to name two of them and the slate held more.",
    },
    {
        "term": "Winner with a handicap",
        "plain": "One side is given a head start, to even up a mismatch. \"Reds −1.5\" "
                 "wins only if the Reds win by 2 or more. Books call it the run line in "
                 "baseball, the puck line in hockey, the point spread in football and "
                 "basketball and the Asian handicap in soccer — the same bet each time.",
    },
    {
        "term": "Combined total",
        "plain": "Ignore who wins: bet on whether both sides together score more (over) "
                 "or fewer (under) than a set number. What gets counted depends on the "
                 "sport — runs, goals, points or games.",
    },
    {
        "term": "One side's total",
        "plain": "The same over/under idea applied to a single team's score. Books call "
                 "it a team total.",
    },
    {
        "term": "Part of the game",
        "plain": "Most bets settle on the whole game as the book settles it — overtime, "
                 "extra innings and shootouts included. Some settle on regulation time "
                 "only, or on a half, or on the first innings. Those are different bets: "
                 "a hockey game level after 60 minutes is a draw for one and not for the "
                 "other, so this tool never compares them with each other.",
    },
    {
        "term": "The number (the line)",
        "plain": "The figure a bet hinges on — the handicap, or the over/under total. "
                 "Half numbers like 8.5 exist so there is no tie.",
    },
    {
        "term": "Alternate line",
        "plain": "Besides its main number, a book offers the same bet at other numbers at "
                 "different prices. Both are real bets, so both are collected and marked.",
    },
    {
        "term": "Max bet",
        "plain": "The largest stake available on that selection, where the venue "
                 "publishes one. Pinnacle states a limit; the exchanges and prediction "
                 "markets state the amount actually behind the price, which is a harder "
                 "cap; the other books mostly state nothing, which means unknown rather "
                 "than unlimited.",
    },
    {
        "term": "Suspended",
        "plain": "The book is showing the price but not currently accepting bets on it — "
                 "usually because it is about to move.",
    },
    {
        "term": "Fixture ID",
        "plain": "This tool's own name for a fixture, like MLB-CLE@MLB-CIN:2026-07-28 — "
                 "the away side, the home side, and the date. Each book uses its own "
                 "private ID, so this is what lets their prices be compared.",
    },
    {
        "term": "Competition (league)",
        "plain": "Which league or tour a fixture belongs to. It is recorded, but it is "
                 "deliberately not part of a fixture's identity: the books disagree about "
                 "classification constantly, and that disagreement must not be able to "
                 "stop their prices being compared.",
    },
    {
        "term": "Saved response, fingerprint",
        "plain": "Every page fetched is saved to disk exactly as it arrived, with a "
                 "fingerprint of its contents. Two fetches with the same fingerprint are "
                 "byte-for-byte identical, which is how a stuck feed becomes visible.",
    },
    {
        "term": "Re-check from saved data",
        "plain": "The saved pages are read again from disk and re-interpreted from "
                 "scratch. If that produces anything different from what was stored, "
                 "something is wrong. Its verdict is on the Checks page.",
    },
]

VOCAB_NOTE = (
    "These lists are closed. A source value that cannot be mapped onto one of them is "
    "rejected upstream and counted, never passed through as free text — a free-text "
    "fallback is what makes unnormalized data look normalized."
)


class _Interner:
    """String table, so a value repeated on a thousand rows is stored once."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._index: dict[str, int] = {}

    def __call__(self, value: object) -> int | None:
        if value is None:
            return None
        text = str(value)
        found = self._index.get(text)
        if found is None:
            found = len(self.values)
            self._index[text] = found
            self.values.append(text)
        return found


#: Columns read straight out of the ``quote`` table.
STORED_QUOTE_COLUMNS = [
    "run_id", "source", "sport", "league", "event_key", "source_event_id",
    "home_participant", "away_participant", "home_team", "away_team", "commence_time",
    "market", "period", "selection", "side", "line", "is_alternate",
    "decimal_odds", "american_odds", "implied_probability", "source_market_id",
    "limit_amount", "status", "last_change_at",
]

#: What the venue actually pays after its commission, computed here rather than
#: in the page.
#:
#: An exchange or prediction market quotes a price and then takes a cut, so the
#: stored number is not the number you receive — and the gap is the same size as
#: the edge this whole tool looks for.  Every comparison in :mod:`src.arb` is
#: made net; a page that showed gross would disagree with the detector on exactly
#: the rows the commission model exists for, which is how an operator concludes
#: the detector is broken.
#:
#: Computed in Python, from the same table :mod:`src.arb` prices with, rather
#: than reimplemented in JavaScript: two implementations of a fee schedule drift,
#: and the one on the page is the one nobody tests.
NET_ODDS_COLUMN = "net_decimal_odds"

QUOTE_COLUMNS = [*STORED_QUOTE_COLUMNS, NET_ODDS_COLUMN]

_INTERNED = frozenset({
    "source", "sport", "league", "event_key", "source_event_id", "home_participant",
    "away_participant", "home_team", "away_team", "commence_time", "market", "period",
    "selection", "side", "source_market_id", "status", "last_change_at",
})


def build_report(
    store: Store,
    *,
    run_limit: int = DEFAULT_RUN_LIMIT,
    quote_runs: int = DEFAULT_QUOTE_RUNS,
    max_quote_rows: int = DEFAULT_MAX_QUOTE_ROWS,
    replay_note: str = "not checked",
    replay_run_id: int | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble everything the page shows, as plain JSON-ready data."""
    generated_at = generated_at or datetime.now(UTC)

    run_rows = store.query(
        """SELECT r.id, r.started_at, r.finished_at, r.ok, r.quote_count, r.event_count,
                  r.error_count, r.warning_count, r.note, r.excluded_count,
                  r.jurisdiction, r.batch_id, r.route_scope,
                  (SELECT COUNT(*) FROM raw_response w WHERE w.run_id = r.id) AS raw_count
             FROM collection_run r
            WHERE r.finished_at IS NOT NULL
            ORDER BY r.id DESC LIMIT ?""",
        (run_limit,),
    )
    # Unfinished runs are excluded, as every other read command excludes them —
    # ``runs`` says in words that "arb, lines, report and mirrors skip it", and
    # this was the one that did not.  An interrupted pass has health rows and no
    # quotes, so it became "the latest run" for the page alone and the headline
    # reported a total coverage collapse: six sports to zero, on a database whose
    # newest *finished* run was healthy.
    if not run_rows:
        raise LookupError(f"no finished collection runs recorded in {store.path}")

    runs: list[dict[str, Any]] = []
    for row in run_rows:
        # What each source *published* and what actually reached the table are
        # two numbers, and they differ exactly when that source's insert failed.
        # The page labelled the published one "prices stored" and rendered it
        # beside a panel reading "This book stored no prices in this collection"
        # — two contradicting numbers under one heading, on the surface most
        # likely to be read.
        #
        # ``stored_count`` is only attached to sources the run's own
        # ``quotes_not_persisted`` finding names — the same gate the ``runs``
        # listing uses — because "produced more than it stored" is *not* on its
        # own a storage fault.  Health is written before the ``--sport``/
        # ``--league`` filter drops what is out of scope, so a scoped collection
        # legitimately stores fewer rows than every source produced, and reading
        # the difference as a lost write had the page saying "the rest were not
        # stored — see Checks" about rows the operator asked it to drop, above a
        # Checks panel with no storage finding in it.
        stored_by_source = store.stored_quote_counts(row["id"])
        lost_sources = store.sources_that_failed_to_persist(row["id"])
        health = [
            {
                "key": h["source_key"],
                "ok": bool(h["ok"]),
                "quote_count": h["quote_count"],
                **(
                    {"stored_count": stored_by_source.get(h["source_key"], 0)}
                    if lost_sources is None or h["source_key"] in lost_sources
                    else {}
                ),
                "event_count": h["event_count"],
                "request_count": h["request_count"],
                "raw_bytes": h["raw_bytes"],
                "latency_ms": h["latency_ms"],
                "rejection_count": h["rejection_count"],
                "skipped_count": h["skipped_count"],
                "unchanged_payloads": h["unchanged_payloads"],
                # What the source was asked for and what it refused.  The
                # columns were added to the store and read by the CLI summary
                # and nothing else, so the dashboard's per-source card showed
                # ``ok: true`` for a book that had lost most of its leagues —
                # which is the state the whole instrumentation exists to make
                # visible, hidden again by the surface most likely to be read.
                "repaired_count": h["repaired_count"],
                "scopes_requested": h["scopes_requested"],
                # Distinct scopes refused — the numerator the dashboard prints
                # as "refused N of M".  Without this field the page falls back
                # to ``scopes_refused.length``, and one scope that produced two
                # messages (SX Bet's metadata half and its order-book half)
                # read as "refused 2 of the 1 scope(s)".
                #
                # Additive migration backfills ``0`` onto older rows that still
                # carry refusal messages.  Re-deriving the distinct count here
                # means a pre-upgrade run does not render as a clean one.
                "scopes_failed": (
                    h["scopes_failed"]
                    if h["scopes_failed"]
                    or not (h["scopes_refused"] or "").strip()
                    else len({
                        # ``"{scope}: {error}"`` — split on the first colon-space,
                        # not the first colon.  Pinnacle's fallback scopes are
                        # ``MLB:246`` / ``MLB:247``, and splitting on ``:`` alone
                        # collapsed every refused league under one sport.
                        entry.split(": ", 1)[0]
                        for entry in (h["scopes_refused"] or "").split("\n")
                        if entry
                    })
                ),
                "scopes_truncated": [
                    entry for entry in (h["scopes_truncated"] or "").split("\n") if entry
                ],
                "scopes_refused": [
                    entry for entry in (h["scopes_refused"] or "").split("\n") if entry
                ],
                "error_kind": h["error_kind"],
                "error_message": h["error_message"],
            }
            for h in store.health_for_run(row["id"])
        ]
        sports, gaps = _coverage_for_run(store, row["id"])
        runs.append(
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "duration_ms": _duration_ms(row["started_at"], row["finished_at"]),
                "ok": bool(row["ok"]),
                "quote_count": row["quote_count"],
                "event_count": row["event_count"],
                "error_count": row["error_count"],
                "warning_count": row["warning_count"],
                "raw_count": row["raw_count"],
                "note": row["note"],
                "jurisdiction": row["jurisdiction"] or "UNKNOWN",
                "batch_id": row["batch_id"] or "",
                # Passed through, not defaulted. ``or "state"`` claimed exact-state
                # collection for a run that recorded no scope at all, which is the
                # one thing the badge exists to distinguish; the page already renders
                # a missing value as ``legacy``, the same word the store backfills.
                "route_scope": row["route_scope"] or "",
                # Rows the --sport/--league filter dropped on purpose.  The flow
                # strip rendered "read 5,429 -> checked 5,429 -> stored 2,476"
                # for a scoped run: 2,953 rows vanish between adjacent boxes,
                # "checked 5,429" is false (validation runs after the filter),
                # and the sentence explaining it — the run's note — was in the
                # payload with nothing rendering it.
                "excluded_count": row["excluded_count"],
                "total_latency_ms": sum(h["latency_ms"] or 0 for h in health),
                "sources": sorted(health, key=lambda h: h["key"]),
                "sports": sports,
                "league_gaps": gaps,
            }
        )

    quote_run_ids = [run["id"] for run in runs[:quote_runs]]
    quotes = _quote_payload(store, quote_run_ids, max_rows=max_quote_rows)
    strings: list[str] = quotes.pop("_strings")
    rows_available: int = quotes.pop("_available")
    latest_run_id = runs[0]["id"]
    latest_jurisdiction = runs[0]["jurisdiction"]
    jurisdiction_warnings = (
        list(route_warnings(latest_jurisdiction))
        if latest_jurisdiction in JURISDICTIONS
        else (
            ["This is the state-neutral global-source run for its collection batch."]
            if latest_jurisdiction == "GLOBAL"
            else ["This run predates jurisdiction-aware collection."]
        )
    )
    detected = load_detection(settings.EGRESS_STATE_PATH)
    if (
        detected is not None
        and is_recent(detected)
        and latest_jurisdiction in JURISDICTIONS
        and detected.state != latest_jurisdiction
    ):
        jurisdiction_warnings.append(
            f"Stored run jurisdiction {latest_jurisdiction} differs from recent "
            f"detected egress {detected.state}."
        )

    detail_ids = tuple(quote_run_ids)
    placeholders = ",".join("?" * len(detail_ids))

    return {
        "meta": {
            "generated_at": generated_at.isoformat(),
            "db_path": str(store.path),
            "db_name": store.path.name,
            "latest_run_id": latest_run_id,
            "jurisdiction": latest_jurisdiction,
            "jurisdiction_live_validated": (
                JURISDICTIONS[latest_jurisdiction].live_validated
                if latest_jurisdiction in JURISDICTIONS
                else False
            ),
            "jurisdiction_warnings": jurisdiction_warnings,
            "detected_state": detected.state if detected is not None else None,
            "lede": _lede(runs[0]),
            "slate_dates": _slate_dates(quotes, strings),
            "replay_note": replay_note,
            # The run whose captures the note describes.  The verdict was
            # rendered inside the run-scoped Checks strip, so selecting an older
            # run showed the newest run's PASS as if this run's bytes had been
            # re-read — for a run whose captures were pruned and whose own
            # ``replay --run`` says FAIL.
            "replay_run_id": replay_run_id,
            "vocab_note": VOCAB_NOTE,
            "runs_recorded": len(runs),
            # Runs whose rows actually made the page, not runs *asked* for —
            # ``len(quote_run_ids)`` read 2 on a database whose second run
            # stored nothing.
            "runs_with_rows": len({row[0] for row in quotes["rows"]}) if quotes["rows"] else 0,
            "quote_rows_embedded": len(quotes["rows"]),
            "quote_rows_available": rows_available,
            "quote_rows_capped": rows_available > len(quotes["rows"]),
            "max_quote_rows": max_quote_rows,
            "min_books": MIN_BOOKS_FOR_COMPARISON,
        },
        # Every venue whose prices are on the page, not only those with a health
        # row in the listed runs.  Health is what used to decide membership, so a
        # source whose quotes were embedded but whose health row sat outside
        # ``--runs`` was absent from ``SOURCE_INFO``: the page fell back to the
        # raw key, treated it as a sportsbook that charges nothing, and printed
        # the *gross* American odds beside the *net* return — the same row
        # disagreeing with itself.  Union with the quoted sources makes the
        # page describe every venue whose prices it shows.
        "sources": [
            _source_entry(key, state=latest_jurisdiction)
            for key in sorted(_venues_on_the_page(runs, quotes, strings))
        ],
        # ``""`` is a real key: legacy runs carry it as their jurisdiction, and
        # without an entry the page's ``sourceInfo`` fell back to the top-level
        # ``sources`` array — built for *latest_jurisdiction*, not the run
        # being viewed — so a legacy run's badges were graded by whatever run
        # happened to be newest.
        "sources_by_jurisdiction": {
            state: [
                _source_entry(key, state=state)
                for key in sorted(_venues_on_the_page(runs, quotes, strings))
            ]
            for state in (*JURISDICTIONS, "GLOBAL", "")
        },
        "venue_kinds": VENUE_KINDS,
        "runs": runs,
        "quotes": quotes,
        "participants": _participants(quotes, strings),
        "sport_facts": _sport_facts(),
        "period_labels": _period_labels(),
        "league_names": _league_names(store),
        "league_tolerances": _fixture_tolerances(store),
        "skipped": [
            {"run_id": r["run_id"], "source": r["source"], "reason": r["reason"], "count": r["count"]}
            for r in store.query(
                f"SELECT run_id, source, reason, count FROM skipped WHERE run_id IN ({placeholders})",
                detail_ids,
            )
        ],
        "skip_notes": [list(pair) for pair in SKIP_NOTES],
        # Rows that were **published** after a field was reconstructed.  Shipped
        # beside the skips and rendered apart from them: the counter existed,
        # was persisted, and reached no view at all, so the 118 Kambi rows it
        # describes appeared on this page *less* than before it was introduced —
        # they had at least been in the skipped breakdown, mislabelled.
        "repaired": [
            {"run_id": r["run_id"], "source": r["source"], "reason": r["reason"],
             "count": r["count"]}
            for r in store.query(
                f"SELECT run_id, source, reason, count FROM repaired "
                f"WHERE run_id IN ({placeholders})",
                detail_ids,
            )
        ],
        "rejections": [
            {"run_id": r["run_id"], "source": r["source"], "reason": r["reason"], "detail": r["detail"]}
            for r in store.query(
                # Newest run first.  Ordering by ``id`` alone meant the *oldest*
                # runs filled the cap and the newest lost — the run the page
                # opens on, and the only one anybody is reading.
                f"SELECT run_id, source, reason, detail FROM rejection "
                f"WHERE run_id IN ({placeholders}) ORDER BY run_id DESC, id LIMIT 500",
                detail_ids,
            )
        ],
        "findings": [
            {
                "run_id": r["run_id"], "severity": r["severity"], "code": r["code"],
                "message": r["message"], "source": r["source"], "event_key": r["event_key"],
            }
            for r in store.query(
                # Newest run first, then errors before warnings.  Ordered by
                # severity and ``id`` alone, ten runs of 60 warnings filled the
                # 500 from the oldest seven and left the newest run with none —
                # and the page then said "Nothing was flagged in this collection"
                # while its own stat strip pointed at Checks.
                f"SELECT run_id, severity, code, message, source, event_key FROM finding "
                f"WHERE run_id IN ({placeholders}) "
                f"ORDER BY run_id DESC, CASE severity WHEN 'error' THEN 0 ELSE 1 END, "
                f"id LIMIT 500",
                detail_ids,
            )
        ],
        "raws": [
            {
                "run_id": r["run_id"], "source": r["source"], "endpoint": r["endpoint"],
                "url": r["url"], "status_code": r["status_code"], "fetched_at": r["fetched_at"],
                "byte_size": r["byte_size"], "sha256": r["sha256"], "unchanged": bool(r["unchanged"]),
            }
            for r in store.query(
                f"SELECT * FROM raw_response WHERE run_id IN ({placeholders}) ORDER BY run_id DESC, id",
                detail_ids,
            )
        ],
        # Which runs the detail tables above were queried for at all.  Every run
        # in the picker is selectable, but skips, rejections, raw responses and
        # findings are only fetched for the embedded few — so for the others the
        # page was rendering "This collection saved nothing" and "Everything this
        # collection saw was in scope" about tables it had never loaded, with the
        # true numbers printed a few lines up in the same flow diagram.
        "detail_runs": list(detail_ids),
        # Arb opportunities per embedded run — same detector the CLI uses, so the
        # page and ``collector arb`` cannot disagree about what is takeable.
        "arbs": _arb_payload(store, detail_ids, as_of=generated_at),
        "glossary": GLOSSARY,
        "schema_fields": [
            {"name": name, "type": kind, "required": required, "note": note}
            for name, kind, required, note in SCHEMA_FIELDS
        ],
        "vocabularies": [
            {"name": "sport", "values": [s.value for s in Sport]},
            {"name": "market", "values": [m.value for m in Market]},
            {"name": "period", "values": [p.value for p in Period]},
            {"name": "selection", "values": [s.value for s in Selection]},
            {"name": "side", "values": [s.value for s in Side]},
            {"name": "status", "values": [s.value for s in QuoteStatus]},
        ],
        "strings": strings,
        # Sibling DB — signup bonuses / boosts / free bets.  Never mixed into quotes.
        "promos": _promo_payload(store, quote_run_ids=detail_ids, as_of=generated_at),
        # Sibling DB — what the operator says they actually placed.  Embedded so a
        # ``file://`` copy of the page still shows the ledger; only a served page
        # can change it.
        "bets": _bets_payload(),
    }


#: How many logged positions the page embeds.  A ledger is small and read whole,
#: unlike quotes; the cap only exists so a pathological file cannot make the
#: dashboard unopenable.
BET_EMBED_LIMIT = 1000


def _bets_payload(limit: int = BET_EMBED_LIMIT) -> dict[str, Any]:
    """The placed-bet ledger, or an empty one with the reason it is empty.

    An unreadable or absent ledger must never take the dashboard down: the
    ledger is a sibling of the odds database, not part of it, and a page that
    refuses to render prices because a hand-kept bet file is corrupt has the
    dependency backwards.
    """
    if not settings.BET_DB_PATH.exists():
        return empty_bet_payload()
    log: BetLog | None = None
    try:
        log = BetLog(settings.BET_DB_PATH)
        return log.payload(limit=limit)
    except Exception as exc:  # noqa: BLE001
        payload = empty_bet_payload()
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload
    finally:
        if log is not None:
            try:
                log.close()
            except Exception:  # noqa: BLE001
                pass


def _bet_id(body: Mapping[str, Any], field: str) -> int:
    """Read a row id out of a request body, refusing anything else by name."""
    value = body.get(field)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BetLogError(f"{field} must be a row id, got {value!r}") from None


def _bet_changes(body: Mapping[str, Any], allowed: Sequence[str]) -> dict[str, Any]:
    """The subset of an edit body the ledger is willing to apply.

    Only keys actually present are forwarded, because ``update_leg`` treats
    presence as intent: sending every field with ``None`` for the untouched ones
    would blank a note and unsettle a leg the operator only meant to re-price.
    """
    return {name: body[name] for name in allowed if name in body}


def _log_bet(log: BetLog, body: Mapping[str, Any]) -> dict[str, Any]:
    slip_id = log.record(slip_from_payload(body))
    return {"slip_id": slip_id}


def _edit_leg(log: BetLog, body: Mapping[str, Any]) -> dict[str, Any]:
    changes = _bet_changes(body, (
        "book", "selection", "line", "note", "link_url",
        "american_odds", "decimal_odds", "stake", "status", "returned",
        "settled_at",
    ))
    return {"slip": log.update_leg(_bet_id(body, "leg_id"), changes)}


def _edit_slip(log: BetLog, body: Mapping[str, Any]) -> dict[str, Any]:
    changes = _bet_changes(body, (
        "note", "placed_at", "home_team", "away_team", "league", "sport",
        "market", "period", "side",
    ))
    return {"slip": log.update_slip(_bet_id(body, "slip_id"), changes)}


def _settle_bet(log: BetLog, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "slip": log.settle_slip(
            _bet_id(body, "slip_id"), str(body.get("status") or "")
        )
    }


def _delete_bet(log: BetLog, body: Mapping[str, Any]) -> dict[str, Any]:
    slip_id = _bet_id(body, "slip_id")
    if not log.delete_slip(slip_id):
        raise BetLogError(f"no bet with id {slip_id}")
    return {"deleted": slip_id}


#: ``POST`` routes that change the ledger.  A table rather than a chain of
#: ``if``s so the set of writable endpoints is one readable list.
_BET_WRITERS: dict[str, Any] = {
    "/api/bets/log": _log_bet,
    "/api/bets/leg": _edit_leg,
    "/api/bets/slip": _edit_slip,
    "/api/bets/settle": _settle_bet,
    "/api/bets/delete": _delete_bet,
}


def _promo_payload(
    odds_store: Store | None = None,
    *,
    quote_run_ids: Sequence[int] = (),
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Latest promo-collection snapshot for the dashboard Promos panel.

    Reads the separate promo SQLite file.  A missing or empty store is a normal
    state (operator has not scraped bonuses yet), not an error.

    Given an odds store and at least one embedded quote run, each offer also
    gets a concrete usage plan built from that run's games — the same quotes,
    the same event reconciliation, and the same counterparty gate the arb panel
    uses, so the promo panel cannot name a hedge the arb panel would refuse.
    """
    empty: dict[str, Any] = {
        "run": None, "offers": [], "health": [], "kinds": [],
        "plans": {}, "plan_meta": None,
    }
    try:
        from src.promos.schema import PromoKind
        from src.promos.store import PromoStore
    except Exception:  # noqa: BLE001 — page must still render without promos package
        return empty

    empty["kinds"] = [kind.value for kind in PromoKind]
    try:
        store = PromoStore(settings.PROMO_DB_PATH)
    except Exception:  # noqa: BLE001
        return empty
    try:
        run_id = store.latest_run_id()
        if run_id is None:
            return empty
        runs = store.list_runs(limit=1)
        run = runs[0] if runs else None
        if run is not None:
            run = {
                "id": run["id"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "ok": bool(run["ok"]),
                "offer_count": run["offer_count"],
                "source_count": run["source_count"],
                "jurisdiction": run.get("jurisdiction") or "UNKNOWN",
            }
        offers = []
        for row in store.offers_for_run(run_id):
            offers.append(
                {
                    "source": row["source"],
                    "offer_id": row["offer_id"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "description": row["description"] or "",
                    "terms": row.get("terms") or "",
                    "url": row["url"],
                    "starts_at": row.get("starts_at"),
                    "ends_at": row["ends_at"],
                    "product": row["product"] or "",
                    "requires_login": bool(row["requires_login"]),
                    "raw_kind": row["raw_kind"] or "",
                    "summary": row.get("summary") or "",
                    "eligible_regions": row.get("eligible_regions") or [],
                    "ineligible_regions": row.get("ineligible_regions") or [],
                    "eligibility_notes": row.get("eligibility_notes") or "",
                    "bonus_amount": row.get("bonus_amount"),
                    "min_deposit": row.get("min_deposit"),
                    "min_odds": row.get("min_odds"),
                    "wagering_requirement": row.get("wagering_requirement"),
                    "reward_type": row.get("reward_type") or "",
                    "usage_guidance": row.get("usage_guidance") or "",
                    "is_specific": bool(row.get("is_specific")),
                    "metadata": row.get("metadata") or {},
                }
            )
        health = store.health_for_run(run_id)
        promo_state = str((run or {}).get("jurisdiction") or "").upper()
        if promo_state == "UNKNOWN":
            promo_state = ""
        def _run_jurisdiction(candidate: int) -> str | None:
            # ``(row or {})["jurisdiction"]`` raised KeyError for an absent
            # run: a sqlite Row indexes by name, an empty dict does not.
            # Unreachable today (candidates come from this store's own
            # listing), which is exactly when a crash-on-absence survives
            # unnoticed until the listing and the lookup drift apart.
            if odds_store is None:
                return None
            row = odds_store.run_row(candidate)
            return row["jurisdiction"] if row is not None else None

        matching_quote_runs = (
            tuple(quote_run_ids)
            if not promo_state
            else tuple(
                candidate
                for candidate in quote_run_ids
                if _run_jurisdiction(candidate) == promo_state
            )
        )
        if not matching_quote_runs and odds_store is not None and promo_state:
            latest_matching = odds_store.latest_run_id(jurisdiction=promo_state)
            if latest_matching is not None:
                matching_quote_runs = (latest_matching,)
        plans, plan_meta = _promo_plans(
            odds_store, offers, matching_quote_runs, as_of, state=promo_state or None
        )
        return {
            "run": run,
            "offers": offers,
            "health": health,
            "kinds": empty["kinds"],
            "plans": plans,
            "plan_meta": plan_meta,
        }
    finally:
        store.close()


def _promo_payload_for_serve() -> dict[str, Any]:
    """Promo panel payload for a ``--serve`` response, plans included.

    The page builds its own payload against the runs it embedded; a serve
    response has no such context, so it opens the odds store and prices against
    the newest stored run.

    Every odds-side failure gets its **own** reason.  Answering all of them with
    the plan-less payload made each one say ``no_odds_run`` — the panel
    claiming there is no odds run while an odds database sits there with a real,
    actionable error (a schema too old for this build, say) that the operator
    never sees.  That is the exact sentence the plans here were added to stop
    printing, and routing errors through the same fallback reinstated it.

    Today the ``reload`` that follows a successful scrape replaces the page, so
    these plans are usually discarded — but the response is what the panel would
    render if it ever stopped reloading, and a payload that is discarded should
    still be true.
    """
    def _failed(exc: BaseException) -> dict[str, Any]:
        payload = _promo_payload()
        if payload.get("plan_meta") is not None or payload.get("offers"):
            payload["plan_meta"] = {
                "reason": f"odds_store_unreadable: {type(exc).__name__}: {exc}"
            }
        return payload

    try:
        odds_store = Store(settings.DB_PATH)
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)
    try:
        promo = _promo_payload()
        promo_state = str((promo.get("run") or {}).get("jurisdiction") or "")
        if promo_state == "UNKNOWN":
            promo_state = ""
        run_id = odds_store.latest_run_id(jurisdiction=promo_state or None)
        if run_id is None:
            # The honest ``no_odds_run``: a readable store with nothing in it.
            return _promo_payload()
        return _promo_payload(
            odds_store, quote_run_ids=(run_id,), as_of=datetime.now(UTC)
        )
    except Exception as exc:  # noqa: BLE001
        return _failed(exc)
    finally:
        # The page's own build closes its store; this one opened its own.
        try:
            odds_store.close()
        except Exception:  # noqa: BLE001
            pass


def _promo_plans(
    odds_store: Store | None,
    offers: Sequence[Mapping[str, Any]],
    quote_run_ids: Sequence[int],
    as_of: datetime | None,
    *,
    state: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Concrete usage plans for the promo panel, or an empty map with a reason.

    Failure to plan must never take the page down — the promos panel predates
    the planner and stays useful without it — but it must also never be silent:
    ``plan_meta`` carries either the run the plans were built from or the
    reason there are none.
    """
    if not offers:
        return {}, None
    if odds_store is None or not quote_run_ids or as_of is None:
        return {}, {"reason": "no_odds_run"}
    try:
        from src.promos.planner import build_promo_plans
        from src.sources.registry import view_only_for_run

        # The newest embedded run — the same slate the arb panel prices.
        run_id = max(quote_run_ids)
        quotes = odds_store.load_quotes(run_id)
        if not quotes:
            return {}, {"reason": "empty_odds_run", "odds_run_id": run_id}
        everything, _ = reconcile_event_keys(quotes)
        recorded = odds_store.recorded_counterparty_groups(run_id)
        # The **odds** run's jurisdiction, not the promo run's.  The *state*
        # parameter is derived from the promo run, and every real promo run
        # predates jurisdiction-aware scraping (jurisdiction ``''``) — so a
        # governed odds run was being planned under the ungoverned fallback,
        # and the panel named a hedge at a book the run's own state declares
        # view-only while the promos CLI on the same stores refused it.  When
        # both states are known they already match (the caller filters odds
        # runs to the promo state), so preferring the odds run's is never a
        # contradiction, only a recovery of the fact the promo run lost.
        odds_row = odds_store.run_row(run_id)
        odds_state = ((odds_row["jurisdiction"] if odds_row else "") or "").strip().upper()
        plan_state = odds_state or (state or "")
        built = build_promo_plans(
            offers,
            everything,
            as_of=as_of,
            one_counterparty=merge_counterparty_groups(
                {}
                if recorded
                else counterparty_groups(
                    everything, view_only=view_only_for_run(plan_state)
                ),
                recorded,
            ),
            state=plan_state or None,
        )
        meta = dict(built["meta"])
        meta["odds_run_id"] = run_id
        return built["plans"], meta
    except Exception as exc:  # noqa: BLE001 — the panel degrades, the page survives
        return {}, {"reason": f"planner_failed: {type(exc).__name__}: {exc}"}


def _blank_sport(sport: str) -> dict[str, Any]:
    return {
        "sport": sport,
        "per_source": {},
        "leagues": {},
        "quote_count": 0,
        "event_count": 0,
    }


def _arb_payload(
    store: Store,
    run_ids: Sequence[int],
    *,
    as_of: datetime,
    total_stake: float = DEFAULT_TOTAL_STAKE,
) -> dict[str, Any]:
    """Run the arb detector for each embedded scrape, keyed by run id.

    Mirrors ``collector arb``: whole-run quotes, recorded counterparties unioned
    with a re-measure, and *as_of* so already-started fixtures are not shown as
    takeable.  Empty runs still get an entry so the UI can say "none" rather than
    "not loaded".

    Each run carries **two** bundles.  The default one is the US-only view, which
    treats :data:`US_UNAVAILABLE_SOURCE_KEYS` as unstakeable; ``with_offshore``
    holds the same detector run with those venues allowed as legs.  Detection is
    Python and the page is a static file, so a reader who wants to see what the
    offshore books would have added cannot recompute it client-side — the answer
    has to be precomputed here or it cannot exist.  The offshore bundle is the
    superset and is stored second, so a run with no offshore edge costs almost
    nothing beyond the US-only one.
    """
    from src.sources.registry import (
        US_UNAVAILABLE_SOURCE_KEYS,
        VIEW_ONLY_SOURCES,
        view_only_for_run,
    )

    out: dict[str, Any] = {}
    for run_id in run_ids:
        quotes = store.load_quotes(run_id)
        if not quotes:
            out[str(run_id)] = _blank_arb_bundle(total_stake)
            continue
        everything, _ = reconcile_event_keys(quotes)
        # Same rule as ``collector arb``: the recorded full-slate gate is the
        # strong answer; re-measure only when a pre-column run left nothing.
        recorded = store.recorded_counterparty_groups(run_id)

        run = store.run_row(run_id)
        state = run["jurisdiction"] if run is not None else ""
        # ``None`` no longer appears here: it meant "the detector's own default",
        # which is the ambient ``VIEW_ONLY_SOURCES``, and that is the leak this
        # helper closes.  ``view_only_for_run`` returns the same fallback set
        # explicitly for a run with no recognisable jurisdiction.  Stored keys
        # the registry has since forgotten join the set (ghost keys are never
        # counterparties), which is why the rows' own sources ride along.
        view_only = view_only_for_run(state, {q.source for q in everything})
        # The gate re-measures with the same set the legs are formed under —
        # the comment above closes the leak for legs, and closing it there
        # while measuring pairs with the ambient set reopened it one line down.
        one_counterparty = merge_counterparty_groups(
            {} if recorded else counterparty_groups(everything, view_only=view_only),
            recorded,
        )
        # ``JURISDICTIONS[state].routes`` was the wrong table: it holds every
        # route the registry knows for the state *including the ``UNAVAILABLE``
        # ones*, so Hard Rock — a book with no Pennsylvania licence at all —
        # counted as the local leg that made a position takeable from PA.
        #
        # And the ``route_scope == "state"`` test that used to guard it was the
        # wrong question twice over: it made this surface disagree with ``arb`` about
        # a run recorded ``jurisdiction="PA", route_scope="legacy"``, and then, once
        # they agreed, it made them agree on *admitting* one — so a historical row,
        # which is what ``legacy`` marks, offered a position with no reachable leg
        # and nothing saying so.  Both the predicate and the decision now live in
        # ``coverage`` (``locality_marking`` / ``locality_applies``), so there is no
        # condition here for the four surfaces to spell differently.
        marking = locality_marking(
            state, route_scope=(run["route_scope"] if run is not None else "") or ""
        )

        def bundle(excluded: frozenset[str] | None) -> dict[str, Any]:
            report = find_opportunities(
                everything,
                total_stake=total_stake,
                as_of=as_of,
                one_counterparty=one_counterparty,
                # ``None`` means "the detector's own default", which is
                # ``VIEW_ONLY_SOURCES``. Unioning an exclusion onto ``None``
                # would resolve to the exclusion *alone* and silently re-admit
                # every republished mirror as a leg, so the default is named
                # explicitly before anything is added to it.
                view_only_sources=(
                    view_only if not excluded
                    else (VIEW_ONLY_SOURCES if view_only is None else frozenset(view_only))
                    | excluded
                ),
            )
            rejected: dict[str, int] = {}
            for diagnostic in report.diagnostics:
                rejected[diagnostic.code] = rejected.get(diagnostic.code, 0) + 1
            return {
                "opportunities": [
                    _opportunity_entry(opp, marking) for opp in report.opportunities
                ],
                "diagnostics": [
                    {"code": code, "count": count}
                    for code, count in sorted(rejected.items(), key=lambda item: -item[1])
                ],
                "group_count": report.group_count,
                "comparable_group_count": report.comparable_group_count,
                "stake": total_stake,
                # Rendered by ``renderArb`` in :mod:`src.report_assets`, which is
                # the whole point: a reader comparing this against ``arb`` on the
                # same run must not find two position counts and no explanation.
                # Shipped without being rendered for one round, which bought the
                # labels and none of the account of them.
                "non_local_flagged": marking.count_without_local_leg(
                    report.opportunities
                ),
            }

        entry = bundle(US_UNAVAILABLE_SOURCE_KEYS)
        entry["with_offshore"] = bundle(None)
        out[str(run_id)] = entry
    return out


def _blank_arb_bundle(total_stake: float) -> dict[str, Any]:
    """A run whose prices are not embedded, in both views."""
    empty: dict[str, Any] = {
        "opportunities": [],
        "diagnostics": [],
        "group_count": 0,
        "comparable_group_count": 0,
        "stake": total_stake,
        # Same keys as a real bundle: a consumer that reads this one must not have
        # to branch on which shape it got.
        "non_local_flagged": 0,
    }
    return {**empty, "with_offshore": dict(empty)}


def _opportunity_entry(
    opportunity: Opportunity, marking: LocalityMarking
) -> dict[str, Any]:
    """JSON shape for one position on the dashboard, locality labels included.

    The label text is composed here rather than in the JS so the phrase is the
    same one ``arb``, ``lines`` and the SMS print — one vocabulary per fact.
    """
    league = opportunity.legs[0].quote.league if opportunity.legs else ""
    return {
        "no_local_leg": not marking.has_local_leg(opportunity),
        "event_key": opportunity.event_key,
        "sport": opportunity.sport.value,
        "league": league,
        "home_team": opportunity.home_team,
        "away_team": opportunity.away_team,
        "home_participant": opportunity.legs[0].quote.home_participant if opportunity.legs else "",
        "away_participant": opportunity.legs[0].quote.away_participant if opportunity.legs else "",
        "commence_time": opportunity.commence_time.isoformat(),
        "market": opportunity.market.value,
        "period": opportunity.period.value,
        "side": opportunity.side.value if opportunity.side else None,
        "line": opportunity.line,
        "margin_pct": round(opportunity.margin * 100.0, 3),
        "roi_pct": round(opportunity.roi * 100.0, 3),
        "guaranteed_profit": round(opportunity.guaranteed_profit, 2),
        "total_stake": round(opportunity.total_stake, 2),
        "max_total_stake": (
            None if opportunity.max_total_stake is None
            else round(opportunity.max_total_stake, 2)
        ),
        "sum_implied": round(opportunity.sum_implied, 6),
        "is_risk_free": opportunity.is_risk_free,
        "can_push": opportunity.can_push,
        "can_half_push": opportunity.can_half_push,
        "notes": list(opportunity.notes),
        "legs": [
            {
                "source": leg.source,
                "non_local_label": (
                    "" if marking.leg_is_local(leg.source) else marking.label()
                ),
                "selection": leg.selection.value,
                "line": leg.quote.line,
                "american_odds": leg.quote.american_odds,
                "decimal_odds": round(leg.decimal_odds, 4),
                "net_decimal_odds": round(leg.net_odds, 4),
                "stake": round(leg.stake, 2),
                "payout": round(leg.payout, 2),
                # Where to actually place it.  None only for a consensus feed,
                # which names no venue that would take the bet.  The governed
                # state selects a state-partitioned book's own front door.
                "link": link_payload(
                    leg.quote,
                    state=(marking.state or None) if marking is not None else None,
                ),
            }
            for leg in opportunity.legs
        ],
        "outcome_profits": [
            {"label": label, "profit": round(profit, 2)}
            for label, profit in opportunity.outcome_profits
        ],
    }


def _coverage_for_run(
    store: Store, run_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Per-sport coverage for one run, and the configured leagues that came back empty.

    Both come out of the database: the per-sport, per-book counts from the stored
    rows, and the gaps from what each source recorded it had been *asked* for.
    Without that second query a league nobody managed to collect is
    indistinguishable from a league nobody wanted.
    """
    per_sport: dict[str, dict[str, Any]] = {}
    for row in store.sport_source_matrix(run_id):
        sport = per_sport.setdefault(row["sport"], _blank_sport(row["sport"]))
        sport["per_source"][row["source"]] = (
            sport["per_source"].get(row["source"], 0) + row["quote_count"]
        )
        league = sport["leagues"].setdefault(
            row["league"],
            {"league": row["league"], "per_source": {}, "quote_count": 0, "event_count": 0},
        )
        league["per_source"][row["source"]] = row["quote_count"]
        league["quote_count"] += row["quote_count"]
        sport["quote_count"] += row["quote_count"]

    # Fixture counts are queried separately: summing the per-source counts would
    # count one fixture once per book that priced it.
    for row in store.query(
        """SELECT sport, league, COUNT(DISTINCT event_key) AS event_count
             FROM quote WHERE run_id = ? GROUP BY sport, league""",
        (run_id,),
    ):
        sport = per_sport.get(row["sport"])
        if sport is not None and row["league"] in sport["leagues"]:
            sport["leagues"][row["league"]]["event_count"] = row["event_count"]
    for row in store.sport_coverage(run_id):
        if row["sport"] in per_sport:
            per_sport[row["sport"]]["event_count"] = row["event_count"]

    # Fixtures more than one book priced.  A sport can have two books and no
    # overlap at all — one book's NHL openers against another's friendly — and in
    # that case there is still nothing on the page that can be compared, so the
    # count is shown next to the book count rather than instead of it.
    from src.sources.registry import view_only_for_run

    run_state = ""
    row = store.run_row(run_id)
    if row is not None:
        run_state = row["jurisdiction"] or ""
    cross_book = store.cross_book_event_counts(
        run_id,
        min_books=MIN_BOOKS_FOR_COMPARISON,
        view_only=view_only_for_run(run_state),
    )

    # The run's own scope, honoured before anything is blamed.  League coverage
    # is recorded before the ``--sport``/``--league`` filter — correct for "what
    # was asked for" — but a sport the operator *excluded* is not a coverage
    # failure, and blank-filling it here had the lede of a ``--sport tennis``
    # run claiming "Baseball did not clear that bar — one book only, or no
    # fixture that two of them both priced … listed rather than hidden": three
    # false claims (both books priced it, the operator dropped it, and none of
    # it is listed), contradicted by the run's own note in the same payload.
    scope_sports, scope_leagues, _ = store.run_scope(run_id)

    def _in_run_scope(sport: str, league: str) -> bool:
        if scope_sports and sport not in scope_sports:
            return False
        if scope_leagues and league not in scope_leagues:
            return False
        return True

    gaps: list[dict[str, str]] = []
    for row in store.league_coverage(run_id):
        if not _in_run_scope(row["sport"], row["league"]):
            continue  # excluded by the operator's own flag, not missing
        # A sport that was configured and returned nothing at all still gets a
        # row, reported as priced by zero books.  Leaving it out would make the
        # page silent about exactly the case it exists to surface.
        per_sport.setdefault(row["sport"], _blank_sport(row["sport"]))
        if row["quote_count"] == 0:
            gaps.append(
                {"source": row["source_key"], "league": row["league"], "sport": row["sport"]}
            )

    from src.sources.registry import view_only_for_run

    run = store.run_row(run_id)
    state = run["jurisdiction"] if run is not None else ""
    # ``view_only_for_run`` and never the ambient constant: for a legacy or
    # GLOBAL run the old ``else VIEW_ONLY_SOURCES`` arm graded ``books`` /
    # ``meets_bar`` / ``comparable`` by the *reader's* ODDS_STATE — a legacy
    # run holding hardrock+fanduel read ``comparable: True`` from an IL box
    # and ``comparable: False`` from a PA box, beside a ``cross_book_events``
    # ten lines up that this same function already resolved from the run.
    view_only = view_only_for_run(state)

    result: list[dict[str, Any]] = []
    for sport in sorted(per_sport):
        entry = per_sport[sport]
        # View-only feeds stay in ``per_source`` for the coverage grid, but do
        # not count toward the comparability bar — Open + one book is not two books.
        books = sorted(
            key
            for key, count in entry["per_source"].items()
            if count and key not in view_only
        )
        result.append(
            {
                "sport": sport,
                "quote_count": entry["quote_count"],
                "event_count": entry["event_count"],
                "cross_book_events": cross_book.get(sport, 0),
                "books": books,
                "per_source": entry["per_source"],
                "meets_bar": len(books) >= MIN_BOOKS_FOR_COMPARISON,
                "comparable": (
                    len(books) >= MIN_BOOKS_FOR_COMPARISON and cross_book.get(sport, 0) > 0
                ),
                "leagues": [
                    {
                        "league": league["league"],
                        "quote_count": league["quote_count"],
                        "event_count": league["event_count"],
                        "books": sorted(k for k, v in league["per_source"].items() if v),
                        "per_source": league["per_source"],
                    }
                    for league in sorted(
                        entry["leagues"].values(), key=lambda item: -item["quote_count"]
                    )
                ],
            }
        )
    return result, sorted(gaps, key=lambda gap: (gap["sport"], gap["league"], gap["source"]))


def _quote_payload(
    store: Store, run_ids: Sequence[int], *, max_rows: int = DEFAULT_MAX_QUOTE_ROWS
) -> dict[str, Any]:
    """Quote rows for the given runs, columnar, string-interned, and **bounded**.

    The bound is the point.  This page embeds its rows as JSON inside a single
    HTML file, and the only lever on that used to be ``--quote-runs``: at 34,000
    rows per run the payload is already ~20 MB, at 340,000 it is ~200 MB and the
    builder's memory runs to gigabytes, and at 3.4 million it exceeds V8's
    maximum string length and the page renders blank.  A source list ten times
    longer walks straight into that.

    Truncation keeps the newest run's **soonest fixtures**, across every source.
    Both halves of that are deliberate.

    Newest run, because the dashboard is a report on the most recent collections
    and dropping those to keep older ones would invert its purpose.

    Soonest fixtures rather than "newest rows", because within one run every row
    shares an instant: ordering by insert order therefore orders by *source*
    (rows are inserted one source at a time, alphabetically), and a cap applied
    to that dropped whole sportsbooks — a 10-source run capped at 300 embedded
    only ``polymarket``, ``smarkets`` and ``sxbet``, and the other seven vanished
    from the price table while the coverage grid, which queries the run directly,
    still showed all ten with their real counts.  Ordering by kickoff keeps every
    source's imminent fixtures, which is both balanced and the part anyone
    actually looks at.

    What was left out is returned alongside, so the page can say so.
    """
    intern = _Interner()
    rows: list[list[Any]] = []
    available = 0
    if run_ids:
        placeholders = ",".join("?" * len(run_ids))
        available = int(
            store.query(
                f"SELECT COUNT(*) AS n FROM quote WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            )[0]["n"]
        )
        selected = ", ".join(STORED_QUOTE_COLUMNS)
        # Selected newest-run-first and soonest-kickoff-first so the cap drops
        # the most distant fixtures rather than the last few sportsbooks in the
        # alphabet; re-sorted afterwards into the (run, insert) order every
        # consumer of this payload assumes.
        picked = store.query(
            f"SELECT id AS _row_id, {selected} FROM quote "
            f"WHERE run_id IN ({placeholders}) "
            "ORDER BY run_id DESC, commence_time ASC, id DESC LIMIT ?",
            (*run_ids, max(max_rows, 0)),
        )
        for row in sorted(picked, key=lambda entry: (entry["run_id"], entry["_row_id"])):
            values = [
                intern(row[name]) if name in _INTERNED else row[name]
                for name in STORED_QUOTE_COLUMNS
            ]
            values.append(_net_odds(row["source"], row["decimal_odds"]))
            rows.append(values)
    return {
        "columns": QUOTE_COLUMNS,
        "rows": rows,
        "_strings": intern.values,
        "_available": available,
    }


def _net_odds(source: str, decimal_odds: Any) -> float | None:
    """Decimal odds after the venue's charge, or ``None`` if unpriceable.

    A stored price at or below 1.0 is not something the commission models accept
    — it pays nothing — and this is a display path, so it reports "unknown"
    rather than raising and taking the whole report down.
    """
    try:
        return round(net_decimal_odds(source, float(decimal_odds)), 6)
    except (TypeError, ValueError):
        return None


def _participants(quotes: dict[str, Any], strings: Sequence[str]) -> dict[str, dict[str, str]]:
    """Map each participant key onto a display name and a short label.

    "Reds win by 2 or more" is a sentence; "home -1.5" is a notation.  The label
    is derived from the identity the pipeline already resolved and the display
    name stored beside it — never from a fresh guess at the book's spelling — so
    the page cannot name a competitor the validator would have rejected.
    """
    names: dict[str, dict[str, str]] = {}
    columns = quotes["columns"]
    pairs = (
        (columns.index("home_participant"), columns.index("home_team")),
        (columns.index("away_participant"), columns.index("away_team")),
    )
    for row in quotes["rows"]:
        for key_index, name_index in pairs:
            key = row[key_index]
            if key is None:
                continue
            participant = strings[key]
            if participant in names:
                continue
            display = strings[row[name_index]] if row[name_index] is not None else participant
            # Keys are namespaced — "MLB-CIN", "TENNIS-humbertugo" — and the part
            # after the namespace is the shortest honest label available.
            _, _, short = participant.partition("-")
            names[participant] = {
                "name": display,
                "abbr": short or participant,
                "short": _short_name(display, short or participant),
            }
    return names


def _short_name(display: str, fallback: str) -> str:
    """The last word of a display name: "Reds", "Marlins", "Arsenal", "Humbert"."""
    words = display.split()
    if not words:
        return fallback
    return words[-1]


def _sport_facts() -> dict[str, dict[str, Any]]:
    """What each sport's totals count, and where a draw is a real outcome.

    Read straight out of :mod:`src.vocab`, so the page's wording cannot drift from
    the settlement rules the pipeline actually applies.  This is what lets one
    describe-the-bet routine say "9 or more runs" for baseball and "goals" for
    hockey without either being hardcoded beside the other.
    """
    facts: dict[str, dict[str, Any]] = {}
    for sport in Sport:
        facts[sport.value] = {
            "label": sport.value.capitalize(),
            "unit": scoring_unit(sport),
            "periods": {
                period.value: {
                    "draw_is_priced": period_rules(sport, period).draw_is_priced,
                    "tie_possible": period_rules(sport, period).tie_possible,
                    "unit": period_rules(sport, period).scoring_unit,
                }
                for period in Period
                if (sport, period) in PERIOD_RULES
            },
        }
    return facts


def _period_labels() -> dict[str, dict[str, str]]:
    """Plain wording for each scoring window, and the book's own shorthand.

    ``full_game`` and ``regulation`` are spelled out because the difference
    between them is the whole reason they are separate rows: one includes
    overtime and cannot be tied, the other excludes it and can.
    """
    return {
        "full_game": {"plain": "Whole game", "term": "full game, overtime included"},
        "regulation": {"plain": "Regulation only", "term": "overtime excluded"},
        "first_half": {"plain": "First half", "term": "1st half"},
        "first_5_innings": {"plain": "First 5 innings", "term": "F5"},
        "first_1_inning": {"plain": "First inning", "term": "1st inning"},
    }


def _league_names(store: Store) -> dict[str, str]:
    """Human names for every league key present in the database."""
    names: dict[str, str] = {}
    for table in ("quote", "source_league"):
        for row in store.query(f"SELECT DISTINCT league FROM {table}"):
            key = row["league"]
            names.setdefault(key, get_league(key).name if is_known(key) else key)
    return names


def _fixture_tolerances(store: Store) -> dict[str, int]:
    """How far apart two venues' clocks may be and still mean one fixture, in
    seconds, per league key present in the database.

    The dashboard needs this to tell one fixture's prices from another's across
    collections, and it has to be the *same* number the pipeline clustered with.
    A flat two-hour window looked safe and was not: tennis clusters at 14 hours
    and soccer at 12, so three cross-source tennis fixtures on the captured slate
    — FanDuel against Matchbook, 3½ to 6½ hours apart — had one of their two
    venues dropped from the panel, which then reported "1 venue offering it —
    nothing to compare" for a bet the detector does compare.

    Safe as a window precisely where it is widest: the sports with a wide
    tolerance are the ones where two competitors never meet twice in a day, so
    there is no second fixture for it to reach.
    """
    seconds: dict[str, int] = {}
    for table in ("quote", "source_league"):
        for row in store.query(f"SELECT DISTINCT league FROM {table}"):
            key = row["league"]
            if key not in seconds and is_known(key):
                seconds[key] = int(get_league(key).same_event_tolerance.total_seconds())
    return seconds


def _unknown_source(key: str) -> dict[str, str]:
    return {
        "label": key,
        "host": "—",
        "kind": "sportsbook",
        "what": "No description recorded for this source.",
    }


def _venues_on_the_page(
    runs: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Any],
    strings: Sequence[str],
) -> set[str]:
    """Every venue the page has a reason to name.

    Health rows are the venues that answered this collection; quote rows are the
    venues whose prices are actually embedded.  They are not the same set when
    ``--runs`` is larger than ``--quote-runs``, or when a source produced rows
    that were stored under a different identity from its health key — and the
    page must describe every venue whose prices it shows, or the commission-
    aware paths silently treat a charging venue as free.
    """
    keys = {h["key"] for run in runs for h in run["sources"]}
    columns: Sequence[str] = quotes.get("columns") or ()
    if "source" not in columns:
        return keys
    source_col = columns.index("source")
    for row in quotes.get("rows") or ():
        index = row[source_col]
        if isinstance(index, int) and 0 <= index < len(strings):
            keys.add(strings[index])
    return keys


def _source_entry(key: str, *, state: str | None = None) -> dict[str, Any]:
    """One venue, as the page describes it.

    Carries what it charges and how it settles a game that is not played, and not
    only what it is called.  Both change the number a reader should act on: an
    exchange's quoted price is better than the price it pays, and a prediction
    market does not refund a cancelled fixture the way a book does — which is the
    difference between a hedge and a one-sided bet.  Reading it out of the same
    tables the arbitrage engine uses means the page cannot describe a venue in
    terms the pipeline does not price it in.
    """
    from src.sources.registry import (
        BY_BASE_KEY,
        BY_KEY,
        REPUBLISHED_SOURCE_KEYS,
        RETAIL_SOURCE_KEYS,
        STATE_LICENSED_REPUBLISHER_KEYS,
        US_UNAVAILABLE_SOURCE_KEYS,
        view_only_for_run,
        view_only_for_state,
    )

    entry = dict(key=key, **SOURCE_NOTES.get(key, _unknown_source(key)))
    if state in JURISDICTIONS:
        configured = JURISDICTIONS[state]
        route = configured.routes.get(key)
        host = source_host(state, key)
        if host:
            entry["host"] = host
        if route is not None:
            entry["route_status"] = route.status.value
            entry["routed_state"] = route.routed_state
            if route.warning:
                entry["route_warning"] = route.warning
    charge = commission_for(key)
    entry["commission"] = "" if charge.is_free else charge.describe()
    entry["settles"] = _SETTLEMENT_WORDS[regime_for(key)]
    # The last arm is ``view_only_for_run``, never the ambient ``is_view_only``:
    # for a legacy run the ambient read graded ``hardrock``'s badge by the
    # reader's ODDS_STATE — " · context only, not a book" on a PA-built page,
    # a counterparty on an IL-built one — while the same page's arb payload,
    # resolved from the run, kept showing the hardrock-legged position beside
    # the badge that disowned it.  Two reviewers found this independently.
    # A key the registry no longer knows is view-only under EVERY state:
    # rows outlive registrations, and a ghost key has no commission schedule,
    # no settlement rule and no bet link, so it cannot be a counterparty.
    entry["view_only"] = key not in BY_BASE_KEY or (
        key in REPUBLISHED_SOURCE_KEYS
        if state == "GLOBAL"
        else key in view_only_for_state(state)
        if state in JURISDICTIONS
        else key in view_only_for_run(state or "")
    )
    # A state-licensed republisher is not a global route.  ``an_fanduel`` is asked
    # for Pennsylvania's own book id (255) on a PA run, so the page must not badge
    # it GLOBAL beside a Las Vegas column that genuinely is one — that erases the
    # locality distinction rules (a) and (c) exist to make visible, on the one
    # panel whose job is to say where each feed's number comes from.
    entry["route_scope"] = (
        "state"
        if key in RETAIL_SOURCE_KEYS or key in STATE_LICENSED_REPUBLISHER_KEYS
        else "global"
    )
    entry["diagnostic_only"] = key in REPUBLISHED_SOURCE_KEYS
    # Independent of ``view_only``: a republished mirror is unstakeable because it
    # is somebody else's board, this is unstakeable because the venue will not
    # take a US customer. The page toggles on it rather than hiding it outright,
    # so a reader can still ask what the sharp offshore line was.
    entry["us_unavailable"] = key in US_UNAVAILABLE_SOURCE_KEYS
    # Whether a row exists only because somebody offered liquidity. A sportsbook
    # quotes both sides itself and will not price itself to lose, so its two sides
    # summing below 1.0 is evidence about the parser — ``validation.MIN_OVERROUND``
    # says exactly that. On an exchange the two sides are separate order books that
    # can legitimately cross by a little. The page needs the distinction to know when
    # a negative cut is a mispairing rather than a price, and it is read off the
    # registry here for the same reason ``collector._order_book_sources`` reads it
    # there: it is a fact about the venue, not something to infer from the data.
    registry_entry = BY_KEY.get(key)
    entry["order_driven"] = bool(
        registry_entry is not None and registry_entry.kind.has_stated_liquidity
    )
    return entry


#: How each settlement regime reads on the page.
_SETTLEMENT_WORDS: dict[SettlementRegime, str] = {
    SettlementRegime.VOID_AND_REFUND: "Refunds a cancelled game.",
    SettlementRegime.SETTLE_MAKE_UP_GAME: (
        "Does not refund a cancelled game: it stays open through a postponement and "
        "settles from the rescheduled one, or resolves at a price the venue chooses."
    ),
    SettlementRegime.RESOLVE_FIFTY_FIFTY: (
        "Does not refund a cancelled game: every contract resolves at 0.50, whatever "
        "you paid."
    ),
}


def _duration_ms(started_at: str, finished_at: str | None) -> float | None:
    if not finished_at:
        return None
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(finished_at)
    return (end - start).total_seconds() * 1000


def _column(quotes: dict[str, Any], name: str) -> list[Any]:
    index = quotes["columns"].index(name)
    return [row[index] for row in quotes["rows"]]


def _slate_dates(quotes: dict[str, Any], strings: Sequence[str]) -> str:
    """The scheduling dates covered, read straight off the event keys."""
    keys = {i for i in _column(quotes, "event_key") if i is not None}
    # ``~`` as well as ``#``: an event key can carry a same-time split suffix
    # (``…:2026-07-28~bookx2``) when one source publishes two fixtures at one
    # nominal time, and reading the date off the tail without stripping it put
    # "2026-07-28~bookx2" in the page's own slate-date header.
    dates = sorted(
        {strings[i].split(":")[-1].split("#")[0].split("~")[0] for i in keys}
    )
    if not dates:
        return "no fixtures stored"
    return dates[0] if len(dates) == 1 else f"{dates[0]} … {dates[-1]}"


def _lede(latest: dict[str, Any]) -> str:
    # Labels come from ``SOURCE_NOTES``, which names every registered source.  A private
    # three-entry dict here predated the expansion and printed raw registry keys
    # for everything added since: "…from BetRivers, FanDuel, leovegas_kambi and
    # Pinnacle."
    books = [
        SOURCE_NOTES.get(h["key"], {}).get("label", h["key"])
        for h in latest["sources"]
        if h["quote_count"] > 0
    ]
    listed = (
        " and ".join(books) if len(books) < 3 else f"{', '.join(books[:-1])} and {books[-1]}"
    )
    usable = [entry["sport"] for entry in latest["sports"] if entry["comparable"]]
    single = [entry["sport"] for entry in latest["sports"] if not entry["comparable"]]
    verdict = (
        f"Two or more books priced the same fixtures in {_join(usable)}, so those can "
        f"be compared."
        if usable
        else "No sport was priced by two books on this pass, so nothing can be compared."
    )
    if single:
        verdict += (
            f" {_join(single).capitalize()} did not clear that bar — one book only, or no "
            "fixture that two of them both priced — so those prices cannot be compared "
            "with anything. They are listed rather than hidden."
        )
    count = len(latest["sports"])
    return (
        f"This page compares what {listed or 'the books'} are offering for the same games. "
        f"Latest scrape: {latest['quote_count']:,} prices across {latest['event_count']} "
        f"games in {count} sport{'' if count == 1 else 's'}. {verdict} "
        f"Nothing here updates by itself — hit Scrape now (or rebuild the page) for a "
        f"fresh snapshot."
    )


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if not items:
        return "nothing"
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


# ── rendering ────────────────────────────────────────────────────────────────


def _embed(data: dict[str, Any]) -> str:
    """JSON for a ``<script>`` block.

    ``<`` only ever appears inside a JSON string, so escaping it as ``\\u003c``
    keeps the payload valid JSON while making ``</script>`` unrepresentable.
    """
    dumped = json.dumps(data, separators=(",", ":"), allow_nan=False)
    return dumped.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_fragment(data: dict[str, Any]) -> str:
    """Body-only markup, for hosts that supply their own document shell."""
    return (
        f"<style>{CSS}</style>\n"
        f"{BODY}\n"
        f'<script type="application/json" id="report-data">{_embed(data)}</script>\n'
        f"<script>{JS}</script>\n"
    )


def render_page(data: dict[str, Any]) -> str:
    """A complete standalone document."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>Sportsbook prices — easy compare</title>\n"
        "</head>\n<body>\n"
        f"{render_fragment(data)}"
        "</body>\n</html>\n"
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _replay_note(store: Store, run_id: int) -> str:
    """Re-parse the run's stored bytes so the page can state replay's verdict.

    Imported lazily: the report itself must stay usable without the adapters.
    """
    try:
        from src.collector import MIGRATED_EVOLUTION_NOTE, replay_run
        from src.raw_store import RawStore

        ok, problems = replay_run(run_id, store=store, raw_store=RawStore(settings.RAW_DIR))
        if ok:
            return "PASS"
        # A migrated run's differences can be parser evolution rather than
        # corruption, and ``replay_run`` says so in an explanatory first line —
        # which this count was including, so the masthead read "FAIL (2)" with
        # one real difference and the explanation reached no surface at all.
        # The phrase is IMPORTED, not retyped: this check matched a literal
        # string while round 6 moved that string from the benign migrated
        # wording into the non-vouching one, and the masthead silently
        # inverted — DIFFERS for a migrated run with a rotted archive, hard
        # FAIL for benign migrated evolution.  The constant lives in the
        # wording itself, so the two can no longer drift apart.
        #
        # And the text match alone is NOT the gate: on a run with no
        # migration stamp, no preamble is inserted, so ``problems[0]`` is the
        # first appended problem — whose message can interpolate
        # file-controlled text (a tampered ``envelope_version`` rides into
        # the unreadable-bytes ValueError verbatim, before any hash check).
        # An adversarial round bought this DIFFERS verdict, with its false
        # "predates the current parser" provenance, on a NATIVE run that
        # way.  The stamp is read from the run row, structurally: when it is
        # set and problems exist, ``_judged`` has ALWAYS inserted a
        # code-authored preamble at index 0, so matching the constant there
        # distinguishes exactly the benign wording from the non-vouching
        # one; when it is absent, DIFFERS is unreachable no matter what any
        # problem says.
        run_row = store.run_row(run_id)
        migrated = run_row["migrated_from"] if run_row is not None else None
        if problems and migrated is not None and MIGRATED_EVOLUTION_NOTE in problems[0]:
            return (
                f"DIFFERS ({len(problems) - 1}) — run predates the current "
                "parser; can be evolution rather than corruption"
            )
        return f"FAIL ({len(problems)})"
    except Exception as exc:  # noqa: BLE001 - a view must never be the thing that breaks
        return f"unavailable: {type(exc).__name__}"


def _a_count(text: str) -> int:
    """An argparse type for a strictly positive whole number.

    These flags were bare ``int``.  ``--runs 0`` produced "no finished
    collection runs recorded" on a database holding four, ``--serve 0`` was
    falsy so the flag silently did not serve, and a negative reached SQLite as
    ``LIMIT -1`` — which means *unlimited*, the opposite of what a negative
    could possibly have meant.

    argparse already prefixes the failing flag's own name onto the message, so
    this takes the value and nothing else.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    refusal = settings.refuse_bad_settings()
    if refusal is not None:
        return refusal
    parser = argparse.ArgumentParser(
        prog="python -m src.report",
        description="Write a self-contained dashboard for what the collector has stored.",
    )
    parser.add_argument("--out", type=Path, default=settings.DATA_DIR / "dashboard.html")
    parser.add_argument("--runs", type=_a_count, default=DEFAULT_RUN_LIMIT,
                        help="how many runs to list")
    parser.add_argument("--quote-runs", type=_a_count, default=DEFAULT_QUOTE_RUNS,
                        help="how many recent runs to embed price rows for")
    parser.add_argument("--max-quote-rows", type=_a_count, default=DEFAULT_MAX_QUOTE_ROWS,
                        help=(
                            "ceiling on embedded price rows, whatever --quote-runs asks "
                            "for; the newest rows are kept and the page says how many "
                            "were left out"
                        ))
    parser.add_argument("--fragment", action="store_true",
                        help="write body-only markup instead of a whole document")
    parser.add_argument("--no-replay-check", action="store_true",
                        help="skip re-parsing the latest run's stored bytes")
    parser.add_argument("--open", action="store_true", help="open the file when it is written")
    parser.add_argument("--serve", type=_a_count, metavar="PORT",
                        help="serve the report on localhost instead of only writing it")
    args = parser.parse_args(argv)

    try:
        store = Store(settings.DB_PATH)
    except IncompatibleDatabase as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"cannot open {settings.DB_PATH}: {exc}", file=sys.stderr)
        return 1

    with store:
        latest = store.latest_run_id()
        note = "not checked"
        if latest is not None and not args.no_replay_check:
            note = _replay_note(store, latest)
        try:
            data = build_report(
                store,
                run_limit=args.runs,
                quote_runs=args.quote_runs,
                max_quote_rows=args.max_quote_rows,
                replay_note=note,
                replay_run_id=latest,
            )
        except LookupError as exc:
            # A wiped DB has no runs yet.  Ordinary ``--out`` still refuses so
            # callers do not open an empty board and think collection failed —
            # but ``--serve`` must still bring up the Scrape button that fills it.
            if not args.serve:
                print(f"{exc}\nrun `python -m src.collector collect` first", file=sys.stderr)
                return 1
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(_empty_scrape_shell(), encoding="utf-8")
            print(
                f"wrote empty scrape shell to {args.out} — no finished runs yet; "
                "use Scrape now in the browser",
                file=sys.stderr,
            )
            return _serve(
                args.out,
                args.serve,
                open_browser=args.open,
                run_limit=args.runs,
                quote_runs=args.quote_runs,
                max_quote_rows=args.max_quote_rows,
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    markup = render_fragment(data) if args.fragment else render_page(data)
    args.out.write_text(markup, encoding="utf-8")

    rows = len(data["quotes"]["rows"])
    latest_sports = data["runs"][0]["sports"]
    usable = [entry["sport"] for entry in latest_sports if entry["comparable"]]
    single = [entry["sport"] for entry in latest_sports if not entry["comparable"]]
    print(
        f"wrote {args.out} ({len(markup) / 1024:.0f} KB): "
        f"{data['meta']['runs_recorded']} runs listed, "
        f"{rows:,} price rows from {data['meta']['runs_with_rows']} run(s), "
        f"replay {data['meta']['replay_note']}"
    )
    print(
        f"  latest run: {len(usable)} comparable sport(s) — {MIN_BOOKS_FOR_COMPARISON}+ books "
        f"on a shared fixture ({', '.join(usable) or 'none'})"
        + (f"; not comparable: {', '.join(single)}" if single else "")
    )

    if args.serve:
        return _serve(
            args.out,
            args.serve,
            open_browser=args.open,
            run_limit=args.runs,
            quote_runs=args.quote_runs,
            max_quote_rows=args.max_quote_rows,
        )
    if args.open:
        webbrowser.open(args.out.resolve().as_uri())
    return 0


def _empty_scrape_shell() -> str:
    """Minimal localhost page so a wiped DB can still scrape from the UI."""
    return """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Line shop — scrape to begin</title>
<style>
  :root { color-scheme: dark; --ground:#141414; --surface:#1a1a1a; --ink:#ecece8;
    --muted:#8a8a82; --line:#333; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
    background:var(--ground); color:var(--ink);
    font:400 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .card { width:min(400px, 92vw); padding:22px; border:1px solid var(--line);
    border-radius:4px; background:var(--surface); }
  h1 { margin:0 0 6px; font:600 18px/1.25 system-ui; }
  p { margin:0 0 14px; color:var(--muted); font-size:13px; }
  select, button { width:100%; padding:8px 10px; border-radius:3px; border:1px solid var(--line);
    font:500 13px/1.2 system-ui; margin-top:6px; background:var(--surface); color:var(--ink); }
  button { background:#222; cursor:pointer; }
  button:hover { border-color:var(--muted); }
  button:disabled { opacity:0.55; cursor:wait; }
  #status { margin-top:10px; font:400 12px/1.4 ui-monospace, monospace; color:var(--muted); }
  .bar { margin-top:10px; height:4px; border-radius:2px; background:#2a2a2a; overflow:hidden; display:none; }
  .bar.on { display:block; }
  .bar > i { display:block; height:100%; width:0%; background:#7aa3c9; transition:width .25s ease; }
</style></head><body>
<div class="card">
  <h1>Line shop</h1>
  <p>No scrapes yet. Pull live prices, then this page reloads as the odds board.</p>
  <label for="scope" style="font-size:12px;color:var(--muted)">Scope</label>
  <select id="scope">
    <option value="league:MLB" selected>MLB baseball (fast)</option>
    <option value="sport:baseball">All baseball</option>
    <option value="all">Everything (slower)</option>
  </select>
  <button type="button" id="go">Scrape now</button>
  <div class="bar" id="bar"><i id="fill"></i></div>
  <div id="status">Ready.</div>
</div>
<script>
const btn = document.getElementById('go');
const status = document.getElementById('status');
const scope = document.getElementById('scope');
const bar = document.getElementById('bar');
const fill = document.getElementById('fill');
let poll = null;
function payload() {
  const v = scope.value || 'league:MLB';
  if (v === 'all') return { tier: 'core' };
  if (v.startsWith('sport:')) return { tier: 'core', sport: v.slice(6) };
  if (v.startsWith('league:')) return { tier: 'core', league: v.slice(7) };
  return { tier: 'core', league: 'MLB' };
}
function paint(p) {
  if (!p) return;
  bar.classList.add('on');
  status.textContent = p.message || 'Scraping…';
  const done = Number(p.done) || 0, total = Number(p.total) || 0;
  if (total > 0) fill.style.width = Math.max(4, Math.round(done / total * 100)) + '%';
}
function startPoll() {
  if (poll) clearInterval(poll);
  poll = setInterval(async () => {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      const b = await r.json();
      if (b.progress) paint(b.progress);
    } catch (_) {}
  }, 500);
}
btn.addEventListener('click', async () => {
  btn.disabled = true;
  status.textContent = 'Scraping…';
  bar.classList.add('on');
  startPoll();
  try {
    const res = await fetch('/api/collect', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload()),
    });
    const body = await res.json().catch(() => ({}));
    if (poll) clearInterval(poll);
    if (!res.ok || !body.ok) {
      status.textContent = 'Failed: ' + (body.error || res.statusText || res.status);
      btn.disabled = false;
      return;
    }
    status.textContent = 'Got ' + ((body.collect && body.collect.quote_count) || 0) + ' prices — reloading…';
    fill.style.width = '100%';
    location.reload();
  } catch (err) {
    if (poll) clearInterval(poll);
    status.textContent = 'Failed: ' + (err && err.message ? err.message : err);
    btn.disabled = false;
  }
});
</script></body></html>
"""


def _rebuild_dashboard(
    out: Path,
    *,
    run_limit: int,
    quote_runs: int,
    max_quote_rows: int,
) -> dict[str, Any]:
    """Rewrite *out* from the current database and return a small status dict.

    Replay is skipped here on purpose: a scrape-from-UI path should land on a
    fresh page quickly, and the next ordinary ``python -m src.report`` still
    runs the replay check.
    """
    with Store(settings.DB_PATH) as store:
        latest = store.latest_run_id()
        if latest is None:
            raise LookupError(f"no finished collection runs recorded in {store.path}")
        data = build_report(
            store,
            run_limit=run_limit,
            quote_runs=quote_runs,
            max_quote_rows=max_quote_rows,
            replay_note="not checked (rebuilt after scrape)",
            replay_run_id=latest,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    markup = render_page(data)
    out.write_text(markup, encoding="utf-8")
    run = data["runs"][0]
    return {
        "run_id": run["id"],
        "started_at": run["started_at"],
        "quote_count": run["quote_count"],
        "ok": run["ok"],
        "bytes": len(markup),
    }


def _run_collect_from_ui(
    *,
    tier: str,
    sport: str | None,
    league: str | None,
    states: Sequence[str] | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """One collection pass, started from the dashboard's Scrape button."""
    from src.collector import collect_batch_once
    from src.raw_store import RawStore
    from src.sources._common import Tier
    from src.state_selection import detect_and_select

    try:
        chosen_tier = Tier(tier)
    except ValueError as exc:
        raise ValueError(f"unknown tier {tier!r}; use 'core' or 'full'") from exc

    sports = (sport,) if sport else None
    leagues = (league,) if league else None
    if sports:
        unknown = [s for s in sports if s not in {member.value for member in Sport}]
        if unknown:
            raise ValueError(f"unknown sport(s): {unknown}")
    try:
        selection = detect_and_select(states)
    except SystemExit as exc:
        raise ValueError(str(exc) or "could not start collect") from None
    store = Store(settings.DB_PATH)
    raw_store = RawStore(settings.RAW_DIR)
    try:
        batch = collect_batch_once(
            selection.states,
            detected_state=selection.detected.state,
            raw_store=raw_store,
            store=store,
            sports=sports,
            leagues=leagues,
            tier=chosen_tier,
            on_progress=on_progress,
        )
    finally:
        store.close()
    quote_count = sum(len(result.quotes) for _, result in batch.runs)
    return {
        "batch_id": batch.batch_id,
        "detected_state": batch.detected_state,
        "states": [state for state, _ in batch.runs if state != "GLOBAL"],
        "runs": {state: result.run_id for state, result in batch.runs},
        "ok": batch.ok,
        "quote_count": quote_count,
        "error_count": sum(len(result.report.errors) for _, result in batch.runs),
        "warning_count": sum(len(result.report.warnings) for _, result in batch.runs),
        "tier": chosen_tier.value,
        "sports": list(sports or ()),
        "leagues": list(leagues or ()),
    }


def _run_promos_from_ui(
    *,
    sources: Sequence[str] | None = None,
    states: Sequence[str] | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """One promo/bonus collection pass from the dashboard Promos scrape button."""
    from src.promos.collector import collect_promos_batch_once
    from src.state_selection import detect_and_select

    selection = detect_and_select(states)
    batch = collect_promos_batch_once(
        selection.states,
        detected_state=selection.detected.state,
        sources=sources,
        store=True,
        persist_raw=True,
        on_progress=on_progress,
    )
    by_source: dict[str, int] = {}
    for _, result in batch.runs:
        for offer in result.offers:
            by_source[offer.source] = by_source.get(offer.source, 0) + 1
    return {
        "batch_id": batch.batch_id,
        "detected_state": batch.detected_state,
        "states": [state for state, _ in batch.runs],
        "runs": {state: result.run_id for state, result in batch.runs},
        "ok": batch.ok,
        "offer_count": sum(len(result.offers) for _, result in batch.runs),
        "source_ok": sum(sum(1 for h in result.health if h.ok) for _, result in batch.runs),
        "source_count": sum(len(result.health) for _, result in batch.runs),
        "by_source": by_source,
        "started_at": min(result.started_at for _, result in batch.runs).isoformat(),
        "finished_at": max(result.finished_at for _, result in batch.runs).isoformat(),
    }


def _serve(
    path: Path,
    port: int,
    *,
    open_browser: bool,
    run_limit: int,
    quote_runs: int,
    max_quote_rows: int,
) -> int:
    """Serve the dashboard on localhost, with Scrape endpoints for the UI buttons.

    Static ``file://`` pages stay view-only.  The Scrape buttons only appear when
    the page is loaded from this server, which is what can run a collect and
    rewrite the HTML without violating that rule.
    """
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    root = path.parent.resolve()
    out = path.resolve()
    lock = threading.Lock()
    state: dict[str, Any] = {
        "busy": False,
        "busy_kind": None,
        "last_error": None,
        "progress": None,
        "started_at": None,
    }
    state_lock = threading.Lock()
    # The ledger is written by whichever request thread arrives.  Each one opens
    # its own connection (SQLite objects are not shareable across threads) and
    # this serializes the read-modify-write pairs — settling a leg reads the row,
    # derives a return from it and writes it back, which two concurrent clicks
    # would otherwise interleave.
    bet_lock = threading.Lock()

    def set_progress(payload: Mapping[str, Any] | None) -> None:
        with state_lock:
            state["progress"] = dict(payload) if payload is not None else None

    def status_payload() -> dict[str, Any]:
        detected = load_detection(settings.EGRESS_STATE_PATH)
        with state_lock:
            progress = dict(state["progress"]) if state["progress"] else None
            return {
                "ok": True,
                "busy": state["busy"],
                "busy_kind": state["busy_kind"],
                "control": True,
                "dashboard": out.name,
                "last_error": state["last_error"],
                "started_at": state["started_at"],
                "progress": progress,
                "detected_state": detected.state if detected is not None else None,
                "supported_states": list(JURISDICTIONS),
            }

    def _begin(kind: str, starting: Mapping[str, Any]) -> bool:
        if not lock.acquire(blocking=False):
            return False
        with state_lock:
            state["busy"] = True
            state["busy_kind"] = kind
            state["last_error"] = None
            state["started_at"] = datetime.now(UTC).isoformat()
            state["progress"] = dict(starting)
        return True

    def _end() -> None:
        with state_lock:
            state["busy"] = False
            state["busy_kind"] = None
        lock.release()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            # Keep scrape progress visible; silence routine GETs of the page.
            if self.path.startswith("/api/"):
                super().log_message(fmt, *args)

        def _json(self, code: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route in ("/api/status", "/api/promos/status"):
                self._json(200, status_payload())
                return
            if route == "/api/bets":
                # The ledger itself is the answer; ``_handle_bets`` always
                # attaches it, so the action has nothing of its own to report.
                self._handle_bets(lambda _log, _body: None)
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            if route == "/api/collect":
                self._handle_odds_collect()
                return
            if route == "/api/promos/collect":
                self._handle_promos_collect()
                return
            handler = _BET_WRITERS.get(route)
            if handler is not None:
                self._handle_bets(handler)
                return
            self.send_error(404, "unknown endpoint")

        def _handle_bets(self, action) -> None:
            """Run one ledger operation and answer with the whole ledger.

            Every bet endpoint returns the full payload rather than a delta, so
            the page never has to merge — it replaces ``DATA.bets`` and
            re-renders.  A ledger is a few hundred rows; the correctness of "what
            is on screen is what is in the file" is worth more than the bytes.
            """
            body: dict[str, Any] = {}
            if self.command == "POST":
                parsed, err = self._read_json_body()
                if parsed is None:
                    self._json(400, {"ok": False, "error": err})
                    return
                body = parsed
            log: BetLog | None = None
            try:
                with bet_lock:
                    log = BetLog(settings.BET_DB_PATH)
                    result = action(log, body)
                    payload = log.payload(limit=BET_EMBED_LIMIT)
            except BetLogError as exc:
                # The operator typed something the ledger will not accept.  That
                # is a 400 with the reason, not a 500 with a traceback.
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return
            finally:
                if log is not None:
                    try:
                        log.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._json(200, {"ok": True, "bets": payload, "result": result})

        def _read_json_body(self) -> tuple[dict[str, Any] | None, str | None]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return None, "body must be JSON"
            if not isinstance(body, dict):
                return None, "body must be a JSON object"
            return body, None

        def _handle_odds_collect(self) -> None:
            body, err = self._read_json_body()
            if body is None:
                self._json(400, {"ok": False, "error": err})
                return

            tier = str(body.get("tier") or "core")
            sport = body.get("sport") or None
            league = body.get("league") or None
            states = body.get("states") or []
            if not isinstance(states, list) or not all(
                isinstance(item, str) and item.upper() in JURISDICTIONS
                for item in states
            ):
                self._json(400, {
                    "ok": False,
                    "error": "states must be a list containing only IL, PA, NJ, or DC",
                })
                return
            if sport is not None:
                sport = str(sport)
            if league is not None:
                league = str(league)

            if not _begin("odds", {
                "phase": "starting",
                "message": "Starting scrape…",
                "done": 0,
                "total": 0,
                "quote_count": 0,
                "kind": "odds",
            }):
                self._json(409, {
                    "ok": False,
                    "error": "a scrape is already running; wait for it to finish",
                    "busy": True,
                    "busy_kind": status_payload().get("busy_kind"),
                    "progress": status_payload().get("progress"),
                })
                return
            try:
                collected = _run_collect_from_ui(
                    tier=tier,
                    sport=sport,
                    league=league,
                    states=states,
                    on_progress=set_progress,
                )
                set_progress({
                    "phase": "rebuilding",
                    "message": "Rebuilding dashboard…",
                    "done": 1,
                    "total": 1,
                    "quote_count": collected.get("quote_count") or 0,
                    "kind": "odds",
                })
                rebuilt = _rebuild_dashboard(
                    out,
                    run_limit=run_limit,
                    quote_runs=quote_runs,
                    max_quote_rows=max_quote_rows,
                )
                with state_lock:
                    state["last_error"] = None
                    state["progress"] = {
                        "phase": "done",
                        "message": (
                            f"Got {collected.get('quote_count', 0):,} prices — reloading…"
                        ),
                        "quote_count": collected.get("quote_count") or 0,
                        "kind": "odds",
                    }
                self._json(200, {
                    "ok": True,
                    "collect": collected,
                    "dashboard": rebuilt,
                    "reload": True,
                })
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                with state_lock:
                    state["last_error"] = err
                    state["progress"] = {
                        "phase": "error",
                        "message": err,
                        "kind": "odds",
                    }
                self._json(500, {
                    "ok": False,
                    "error": err,
                    "busy": False,
                })
            finally:
                _end()

        def _handle_promos_collect(self) -> None:
            body, err = self._read_json_body()
            if body is None:
                self._json(400, {"ok": False, "error": err})
                return

            sources = body.get("sources")
            states = body.get("states") or []
            if not isinstance(states, list) or not all(
                isinstance(item, str) and item.upper() in JURISDICTIONS
                for item in states
            ):
                self._json(400, {
                    "ok": False,
                    "error": "states must be a list containing only IL, PA, NJ, or DC",
                })
                return
            if sources is not None:
                if not isinstance(sources, list) or not all(
                    isinstance(item, str) for item in sources
                ):
                    self._json(400, {
                        "ok": False,
                        "error": "sources must be a list of promo source keys",
                    })
                    return

            if not _begin("promos", {
                "phase": "starting",
                "message": "Starting promo scrape…",
                "done": 0,
                "total": 0,
                "offer_count": 0,
                "kind": "promos",
            }):
                self._json(409, {
                    "ok": False,
                    "error": "a scrape is already running; wait for it to finish",
                    "busy": True,
                    "busy_kind": status_payload().get("busy_kind"),
                    "progress": status_payload().get("progress"),
                })
                return
            try:
                collected = _run_promos_from_ui(
                    sources=sources,
                    states=states,
                    on_progress=set_progress,
                )
                set_progress({
                    "phase": "rebuilding",
                    "message": "Rebuilding dashboard…",
                    "done": 1,
                    "total": 1,
                    "offer_count": collected.get("offer_count") or 0,
                    "kind": "promos",
                })
                try:
                    rebuilt = _rebuild_dashboard(
                        out,
                        run_limit=run_limit,
                        quote_runs=quote_runs,
                        max_quote_rows=max_quote_rows,
                    )
                    reload = True
                except LookupError:
                    # Odds DB still empty — promo rows are stored; board comes later.
                    rebuilt = None
                    reload = False
                with state_lock:
                    state["last_error"] = None
                    state["progress"] = {
                        "phase": "done",
                        "message": (
                            f"Got {collected.get('offer_count', 0):,} promo offer(s)"
                            + (" — reloading…" if reload else "")
                        ),
                        "offer_count": collected.get("offer_count") or 0,
                        "kind": "promos",
                    }
                self._json(200, {
                    "ok": True,
                    "collect": collected,
                    "dashboard": rebuilt,
                    "reload": reload,
                    # Priced against the newest stored odds run, like the page
                    # itself.  Calling this with no store hardcoded
                    # ``plans: {}`` / ``reason: no_odds_run`` into the one serve
                    # path that does not reload afterwards, so the panel would
                    # have claimed there was no odds run while one sat in the
                    # database.
                    "promos": _promo_payload_for_serve(),
                })
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                with state_lock:
                    state["last_error"] = err
                    state["progress"] = {
                        "phase": "error",
                        "message": err,
                        "kind": "promos",
                    }
                self._json(500, {
                    "ok": False,
                    "error": err,
                    "busy": False,
                })
            finally:
                _end()

    url = f"http://127.0.0.1:{port}/{path.name}"
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"serving {url} — ctrl-c to stop")
        print("  Scrape controls and the editable bet ledger are live here (file:// is view-only)")
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
