"""The dashboard's words: what each venue is, what a skip meant, what a term means.

Separated from :mod:`src.report` for the same reason :mod:`src.report_assets`
holds the CSS and JavaScript — the page's prose is the largest thing in the
module and the least related to the queries around it, and an agent reading
``build_report`` should not have to scroll past nine hundred lines of
explanation to reach the next function.

Everything here is a literal.  No imports, no computation, so anything that
needs the words — the report, the registry's completeness checks, their tests —
can read them without importing the report layer.
"""

from __future__ import annotations


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
    "polymarket_us": {
        "label": "Polymarket US",
        "host": "gateway.polymarket.us",
        "kind": "prediction market",
        "what": "The US-regulated Polymarket, and a different company from the one above "
                "with a different order book — so the two can legitimately disagree, and "
                "only this one can be traded from here. A cancelled game settles at the "
                "last fair price the exchange saw rather than being refunded.",
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
    ("ambiguous_home_away_order",
     "The event name named both sides but not which one is at home, in a sport "
     "where that decides the fixture. Skipped rather than guessed."),
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
