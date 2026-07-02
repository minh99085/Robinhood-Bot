"""Default options watchlist — ETFs + liquid single names."""

from __future__ import annotations

DEFAULT_ETF_SYMBOLS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "IWM",
    "XLK",
    "SMH",
    "XLF",
    "XLV",
    "XLE",
    "DIA",
)

DEFAULT_STOCK_SYMBOLS: tuple[str, ...] = (
    "NVDA",
    "TSLA",
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "AVGO",
    "AMD",
    "MU",
    "INTC",
    "LLY",
    "NFLX",
    "JPM",
    "V",
    "MA",
)

DEFAULT_WATCHLIST: tuple[str, ...] = DEFAULT_ETF_SYMBOLS + DEFAULT_STOCK_SYMBOLS


def parse_watchlist(raw: str) -> list[str]:
    """Parse comma/space-separated symbols; preserve order, dedupe."""
    if not raw.strip():
        return list(DEFAULT_WATCHLIST)
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.replace(",", " ").split():
        sym = part.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out
