"""SymbolMapper — unified symbol -> each exchange's exact pair format.

Persisted to data/symbol_map.json; refresh() rebuilds from the configured
symbol set. We map to the three public, no-key venues we can read live
(Coinbase, Kraken, Bitstamp). Binance US is geo-blocked from many regions, so it
is excluded by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

EXCHANGES = ["coinbase", "kraken", "bitstamp"]

# default sub-$120 liquid universe (BTC/ETH are excluded by the price filter)
DEFAULT_SYMBOLS = [
    "SOL", "AVAX", "LINK", "DOT", "ADA", "MATIC", "ATOM", "NEAR", "APT", "OP",
    "ARB", "INJ", "LTC", "XRP", "DOGE", "ALGO", "FIL", "SUI", "SEI", "TIA",
]


def pair_for(symbol: str, exchange: str) -> str:
    s = symbol.upper()
    if exchange == "coinbase":
        return f"{s}-USD"
    if exchange == "kraken":
        return f"{s}USD"
    if exchange == "bitstamp":
        return f"{s.lower()}usd"
    return s


class SymbolMapper:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "symbol_map.json"
        syms = os.getenv("ARB_SYMBOLS")
        self.symbols = [x.strip().upper() for x in syms.split(",")] if syms else list(DEFAULT_SYMBOLS)
        self._map = self._load() or self.refresh()

    def _load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def refresh(self) -> dict:
        m = {s: {ex: pair_for(s, ex) for ex in EXCHANGES} for s in self.symbols}
        try:
            self.path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        except OSError:
            pass
        self._map = m
        return m

    def pair(self, symbol: str, exchange: str) -> str:
        return self._map.get(symbol.upper(), {}).get(exchange) or pair_for(symbol, exchange)

    def all_symbols(self) -> list[str]:
        return list(self._map.keys())
