import json
from pathlib import Path

from engine.robinhood.watchlist import (
    DEFAULT_ETF_SYMBOLS,
    DEFAULT_STOCK_SYMBOLS,
    DEFAULT_WATCHLIST,
    parse_watchlist,
)


def test_default_watchlist_size():
    assert len(DEFAULT_ETF_SYMBOLS) == 9
    assert len(DEFAULT_STOCK_SYMBOLS) == 16
    assert len(DEFAULT_WATCHLIST) == 25
    assert "SPY" in DEFAULT_WATCHLIST
    assert "NVDA" in DEFAULT_WATCHLIST


def test_parse_watchlist_dedupes():
    raw = "spy, SPY, qqq ,QQQ"
    assert parse_watchlist(raw) == ["SPY", "QQQ"]


def test_parse_watchlist_empty_uses_default():
    assert len(parse_watchlist("")) == 25
