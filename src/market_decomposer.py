"""Market text decomposition for prediction markets and exchange-style markets.

The goal is not perfect natural-language understanding. The goal is to turn
market text into a structured, confidence-scored hypothesis that downstream
matching can use to compare sources safely and reject ambiguous composites.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from src.canonical import infer_sport_from_text, normalize_name

_SIDE_RE = re.compile(r"^(yes|no)\s+", re.IGNORECASE)
_TRIM_RE = re.compile(r"^[\s\W]+|[\s\W]+$")
_NUM_RE = re.compile(r"(?P<line>\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class MarketLeg:
    text: str
    side: str | None
    market_kind: str
    subject: str | None
    counterparty: str | None
    line: float | None
    period: str | None
    confidence: float


@dataclass(frozen=True)
class MarketDecomposition:
    source: str
    raw_text: str
    sport: str
    market_kind: str
    event_name: str
    participant: str | None
    counterparty: str | None
    line: float | None
    side: str | None
    confidence: float
    is_composite: bool
    legs: tuple[MarketLeg, ...] = ()
    notes: str | None = None

    @property
    def is_reliable(self) -> bool:
        return not self.is_composite and self.confidence >= 0.7


def decompose_market_text(
    text: str,
    *,
    source: str,
    sport_hint: str | None = None,
) -> MarketDecomposition:
    raw = _clean_text(text)
    raw = _strip_question_prefix(raw)
    clauses = [c for c in _split_clauses(raw) if c]
    if len(clauses) > 1:
        legs = tuple(_classify_clause(clause, source=source, sport_hint=sport_hint) for clause in clauses)
        best = max(legs, key=lambda leg: leg.confidence)
        return MarketDecomposition(
            source=source,
            raw_text=raw,
            sport=_infer_sport(raw, sport_hint=sport_hint),
            market_kind="composite",
            event_name=raw,
            participant=None,
            counterparty=None,
            line=None,
            side=None,
            confidence=max(0.1, round(best.confidence * 0.55, 3)),
            is_composite=True,
            legs=legs,
            notes=f"composite:{len(legs)}",
        )

    leg = _classify_clause(raw, source=source, sport_hint=sport_hint)
    event_name = _build_event_name(leg, raw)
    return MarketDecomposition(
        source=source,
        raw_text=raw,
        sport=_infer_sport(raw, sport_hint=sport_hint),
        market_kind=leg.market_kind,
        event_name=event_name,
        participant=leg.subject,
        counterparty=leg.counterparty,
        line=leg.line,
        side=leg.side,
        confidence=leg.confidence,
        is_composite=False,
        legs=(leg,),
        notes=None if leg.confidence >= 0.7 else "low-confidence",
    )


def decompose_polymarket_market(
    event: dict[str, Any],
    market: dict[str, Any],
) -> MarketDecomposition:
    question = str(market.get("question") or market.get("market_slug") or event.get("title") or event.get("slug") or "")
    sport_hint = _infer_sport(
        " ".join(
            str(v)
            for v in [
                event.get("title", ""),
                event.get("slug", ""),
                market.get("question", ""),
                market.get("market_slug", ""),
                " ".join(_stringify_tags(event.get("tags", []))),
                " ".join(_stringify_tags(market.get("tags", []))),
            ]
        ),
        sport_hint=None,
    )
    return decompose_market_text(question, source="polymarket", sport_hint=sport_hint)


def decompose_kalshi_market(market: dict[str, Any]) -> MarketDecomposition:
    text = str(
        market.get("title")
        or market.get("yes_sub_title")
        or market.get("no_sub_title")
        or market.get("ticker")
        or ""
    )
    sport_hint = _infer_sport(
        " ".join(
            str(v)
            for v in [
                market.get("ticker", ""),
                market.get("event_ticker", ""),
                market.get("title", ""),
                market.get("yes_sub_title", ""),
                market.get("no_sub_title", ""),
            ]
        )
    )
    return decompose_market_text(text, source="kalshi", sport_hint=sport_hint)


def _classify_clause(clause: str, *, source: str, sport_hint: str | None) -> MarketLeg:
    side, core = _strip_side_prefix(clause)
    normalized = _clean_text(core)
    if not normalized:
        return MarketLeg(text=clause, side=side, market_kind="unknown", subject=None, counterparty=None, line=None, period=None, confidence=0.0)

    for matcher in (
        _match_total,
        _match_spread,
        _match_player_prop,
        _match_advances,
        _match_beat,
        _match_win,
        _match_goal_diff,
        _match_btts,
        _match_team_only,
    ):
        leg = matcher(normalized, side=side, source=source, sport_hint=sport_hint)
        if leg is not None:
            return leg

    return MarketLeg(
        text=clause,
        side=side,
        market_kind="unknown",
        subject=normalized,
        counterparty=None,
        line=None,
        period=None,
        confidence=0.15 if side else 0.05,
    )


def _match_total(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(
        r"(?:(?P<period>reg time|1h|2h|first half|second half|game|full game)\s*:\s*)?(?P<direction>over|under)\s*(?P<line>\d+(?:\.\d+)?)\s*(?P<unit>[\w\s]+?)?(?:\s+scored)?$",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    unit = _clean_text(m.group("unit") or "")
    market_kind = "total"
    if "corner" in unit:
        market_kind = "corners"
    elif "shot" in unit:
        market_kind = "shots"
    elif "run" in unit:
        market_kind = "runs"
    elif "goal" in unit:
        market_kind = "goals"
    elif "point" in unit:
        market_kind = "points"
    return MarketLeg(
        text=text,
        side=side,
        market_kind=market_kind,
        subject=None,
        counterparty=None,
        line=_to_float(m.group("line")),
        period=_clean_text(m.group("period") or "") or None,
        confidence=0.92,
    )


def _match_spread(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(
        r"(?:(?P<period>reg time|1h|2h|first half|second half|game|full game)\s*:\s*)?(?P<subject>.+?)\s+wins?\s+by\s+(?:(?P<direction>over)\s+)?(?P<line>\d+(?:\.\d+)?)\s*(?P<unit>[\w\s]+?)?$",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    unit = _clean_text(m.group("unit") or "")
    market_kind = "spread"
    if "goal" in unit:
        market_kind = "goal_diff"
    elif "run" in unit:
        market_kind = "run_diff"
    elif "point" in unit:
        market_kind = "point_diff"
    return MarketLeg(
        text=text,
        side=side,
        market_kind=market_kind,
        subject=subject or None,
        counterparty=None,
        line=_to_float(m.group("line")),
        period=_clean_text(m.group("period") or "") or None,
        confidence=0.9 if subject else 0.7,
    )


def _match_player_prop(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(r"(?P<subject>.+?):\s*(?P<line>\d+(?:\.\d+)?)\+$", text, re.IGNORECASE)
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    return MarketLeg(
        text=text,
        side=side,
        market_kind="player_prop",
        subject=subject or None,
        counterparty=None,
        line=_to_float(m.group("line")),
        period=None,
        confidence=0.93 if subject else 0.75,
    )


def _match_advances(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(r"(?P<subject>.+?)\s+advances$", text, re.IGNORECASE)
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    return MarketLeg(
        text=text,
        side=side,
        market_kind="advancement",
        subject=subject or None,
        counterparty=None,
        line=None,
        period=None,
        confidence=0.88 if subject else 0.65,
    )


def _match_beat(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(r"(?P<subject>.+?)\s+beat(?:s)?\s+(?P<counterparty>.+)$", text, re.IGNORECASE)
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    counterparty = _clean_text(m.group("counterparty") or "")
    return MarketLeg(
        text=text,
        side=side,
        market_kind="moneyline",
        subject=subject or None,
        counterparty=counterparty or None,
        line=None,
        period=None,
        confidence=0.9 if subject and counterparty else 0.7,
    )


def _match_win(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(r"(?P<subject>.+?)\s+win(?:s)?(?:\s+(?P<event>.+))?$", text, re.IGNORECASE)
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    event = _clean_text(m.group("event") or "")
    return MarketLeg(
        text=text,
        side=side,
        market_kind="futures",
        subject=subject or None,
        counterparty=event or None,
        line=None,
        period=None,
        confidence=0.91 if subject and event else 0.72,
    )


def _match_goal_diff(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    m = re.match(
        r"(?:(?P<period>reg time)\s*:\s*)?goal diff(?:\s+reg time)?:\s*(?P<subject>.+?)\s+wins?\s+by\s+more\s+than\s+(?P<line>\d+(?:\.\d+)?)\s*(?P<unit>[\w\s]+?)?$",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    subject = _clean_text(m.group("subject") or "")
    return MarketLeg(
        text=text,
        side=side,
        market_kind="goal_diff",
        subject=subject or None,
        counterparty=None,
        line=_to_float(m.group("line")),
        period=_clean_text(m.group("period") or "") or "reg_time",
        confidence=0.94 if subject else 0.7,
    )


def _match_btts(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    if "both teams to score" not in text.lower():
        return None
    return MarketLeg(
        text=text,
        side=side,
        market_kind="btts",
        subject=None,
        counterparty=None,
        line=None,
        period=None,
        confidence=0.98,
    )


def _match_team_only(text: str, *, side: str | None, source: str, sport_hint: str | None) -> MarketLeg | None:
    words = text.split()
    if len(words) > 5:
        return None
    if any(ch.isdigit() for ch in text):
        return None
    if not re.fullmatch(r"[\w .'\-]+", text):
        return None
    return MarketLeg(
        text=text,
        side=side,
        market_kind="futures" if side else "selection",
        subject=_clean_text(text) or None,
        counterparty=None,
        line=None,
        period=None,
        confidence=0.45 if side else 0.25,
    )


def _build_event_name(leg: MarketLeg, raw_text: str) -> str:
    if leg.subject and leg.counterparty and leg.market_kind == "moneyline":
        return f"{leg.subject} vs {leg.counterparty}"
    if leg.subject and leg.market_kind in {"futures", "advancement", "player_prop", "goal_diff", "spread", "run_diff", "point_diff"}:
        return leg.counterparty or leg.subject
    if leg.subject:
        return leg.subject
    return raw_text


def _clean_text(text: str) -> str:
    return _TRIM_RE.sub("", re.sub(r"\s+", " ", str(text).strip()))


def _strip_question_prefix(text: str) -> str:
    return re.sub(
        r"^(?:will|who will|what will|which team will|which player will|can|could|would|is|are|do|does|did|should)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _split_clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*,\s*", text) if part.strip()]


def _strip_side_prefix(text: str) -> tuple[str | None, str]:
    m = _SIDE_RE.match(text)
    if not m:
        return None, text
    return m.group(1).upper(), text[m.end():].strip()


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _infer_sport(text: str, sport_hint: str | None = None) -> str:
    if sport_hint:
        return sport_hint
    inferred = infer_sport_from_text(text)
    if inferred:
        return inferred
    haystack = normalize_name(text)
    for token, sport in [
        ("nba", "basketball_nba"),
        ("wnba", "basketball_wnba"),
        ("nfl", "americanfootball_nfl"),
        ("mlb", "baseball_mlb"),
        ("nhl", "icehockey_nhl"),
        ("soccer", "soccer"),
        ("football", "americanfootball_nfl"),
        ("baseball", "baseball_mlb"),
        ("hockey", "icehockey_nhl"),
        ("world cup", "soccer"),
        ("epl", "soccer"),
        ("corners", "soccer"),
    ]:
        if token in haystack:
            return sport
    return "prediction_market"


def _stringify_tags(tags: Any) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        if isinstance(tag, dict):
            out.extend([str(tag.get("label", "")), str(tag.get("slug", ""))])
        else:
            out.append(str(tag))
    return out
