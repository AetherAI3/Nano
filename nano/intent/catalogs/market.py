"""Market vocabulary, aliases, and host capability names."""

from __future__ import annotations

from ..lexicon import Catalog


def _aliases(role: str, canonical: str, *aliases: str):
    return tuple((alias, role, canonical) for alias in aliases)


MARKET_CATALOG = Catalog(
    identity="market",
    version="0.1.0",
    precedence=100,
    aliases=(
        *_aliases("topic", "chart", "chart", "charts", "price chart"),
        *_aliases("topic", "quote", "quote", "quotes"),
        *_aliases("topic", "options", "option", "options", "option chain"),
        *_aliases("topic", "earnings", "earnings", "earnings calendar"),
        *_aliases("topic", "volatility", "volatility", "vol", "implied volatility"),
        *_aliases("topic", "technicals", "technical", "technicals", "technical analysis"),
        *_aliases("topic", "unusual_flow", "unusual flow", "unusual options flow"),
        *_aliases("topic", "sector_flow", "sector flow"),
        *_aliases("topic", "calendar", "calendar", "economic calendar"),
        *_aliases("topic", "watchlist", "watchlist", "watch list"),
        *_aliases("topic", "workspace", "workspace", "work space"),
        *_aliases("security", "SPY", "spy", "$spy"),
        *_aliases("security", "QQQ", "qqq", "$qqq"),
        *_aliases("security", "ES", "/es", "es1!"),
        *_aliases("security", "NQ", "/nq", "nq1!"),
        *_aliases("security", "YM", "/ym", "ym1!"),
        *_aliases("security", "CL", "/cl", "cl1!"),
        *_aliases("security", "GC", "/gc", "gc1!"),
    ),
    capabilities=(
        ("ui.chart", "terminal.tile.chart"),
        ("ui.quote", "terminal.tile.quote"),
        ("ui.options", "terminal.tile.options"),
        ("ui.earnings", "terminal.tile.earnings"),
        ("ui.volatility", "terminal.tile.volatility"),
        ("ui.volatility.compare", "terminal.tile.volatility_comparison"),
        ("data.earnings.calendar", "market.earnings.calendar"),
        ("data.earnings.history", "market.earnings.history"),
        ("data.earnings.estimates", "market.earnings.estimates"),
        ("data.volatility.compare", "market.volatility.compare"),
        ("data.price.context", "market.price.context"),
    ),
)

__all__ = ["MARKET_CATALOG"]
