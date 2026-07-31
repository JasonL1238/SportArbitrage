"""Sportsbook promotions / free-EV scrapers.

Parallel to :mod:`src.sources`: same books, different product.  Never emits
:class:`~src.schema.Quote` rows and never enters the arbitrage engine.
"""
from __future__ import annotations

from src.promos.registry import PROMO_SOURCES, keys
from src.promos.schema import PromoKind, PromoOffer

__all__ = ["PROMO_SOURCES", "PromoKind", "PromoOffer", "keys"]
