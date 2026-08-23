"""Sportsbook promotions / free-EV scrapers.

Parallel to :mod:`src.sources`: same books, different product.  Never emits
:class:`~src.schema.Quote` rows and never enters the arbitrage engine.  Import
the submodule you need (``src.promos.registry``, ``src.promos.store``, …); this
package re-exports nothing.
"""
