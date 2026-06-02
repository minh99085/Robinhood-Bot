"""Chainlink feed registry (configurable, no secrets).

Quant responsibility — *Data Acquisition & Ingestion*: declares which Chainlink
price/oracle feeds the scanner knows about and the asset/category keywords used
to link them to Polymarket markets. The registry is pure metadata: pair, asset
keywords, category, decimals, heartbeat, chain, and an OPTIONAL public on-chain
aggregator address. No private keys, no RPC URLs, no secrets are stored here.

Override / extend via ``CHAINLINK_REGISTRY_PATH`` (a JSON file mapping
``feed_key -> {pair, asset_keywords, category, decimals, heartbeat_s, chain,
address, description}``) — merged on top of the built-in defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ChainlinkFeedSpec:
    """Static metadata for one Chainlink feed. ``address`` is a *public* mainnet
    aggregator address (not a secret) and may be empty when only offline /
    snapshot data is used."""

    key: str
    pair: str
    asset_keywords: tuple = ()
    category: str = "crypto"
    decimals: int = 8
    heartbeat_s: float = 3600.0
    chain: str = "ethereum"
    address: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["asset_keywords"] = list(self.asset_keywords)
        return d


def _spec(key, pair, kws, category, *, decimals=8, heartbeat_s=3600.0,
          chain="ethereum", address="", description="") -> ChainlinkFeedSpec:
    return ChainlinkFeedSpec(
        key=key, pair=pair, asset_keywords=tuple(k.lower() for k in kws),
        category=category, decimals=decimals, heartbeat_s=heartbeat_s,
        chain=chain, address=address, description=description)


# Built-in default registry. Keywords are used for keyword/slug/category linking
# to Polymarket markets. Addresses intentionally left blank (offline-safe).
DEFAULT_FEEDS: dict = {
    s.key: s for s in (
        _spec("ETH/USD", "ETH/USD", ["eth", "ethereum", "ether"], "crypto", heartbeat_s=3600),
        _spec("BTC/USD", "BTC/USD", ["btc", "bitcoin", "xbt"], "crypto", heartbeat_s=3600),
        _spec("SOL/USD", "SOL/USD", ["sol", "solana"], "crypto", heartbeat_s=3600),
        _spec("LINK/USD", "LINK/USD", ["link", "chainlink"], "crypto", heartbeat_s=3600),
        _spec("MATIC/USD", "MATIC/USD", ["matic", "polygon"], "crypto", heartbeat_s=3600),
        _spec("DOGE/USD", "DOGE/USD", ["doge", "dogecoin"], "crypto", heartbeat_s=3600),
        _spec("EUR/USD", "EUR/USD", ["eur", "euro"], "fx", heartbeat_s=3600),
        _spec("GBP/USD", "GBP/USD", ["gbp", "sterling", "pound"], "fx", heartbeat_s=3600),
        _spec("JPY/USD", "JPY/USD", ["jpy", "yen"], "fx", heartbeat_s=3600),
        _spec("XAU/USD", "XAU/USD", ["gold", "xau"], "commodity", heartbeat_s=86400),
        _spec("XAG/USD", "XAG/USD", ["silver", "xag"], "commodity", heartbeat_s=86400),
        _spec("WTI/USD", "WTI/USD", ["oil", "wti", "crude"], "commodity", heartbeat_s=86400),
        _spec("SPX/USD", "SPX/USD", ["sp500", "s&p", "spx", "s&p 500"], "index", heartbeat_s=86400),
    )
}


def load_registry(path: Optional[str] = None,
                  extra: Optional[dict] = None) -> dict:
    """Return ``feed_key -> ChainlinkFeedSpec``. Built-in defaults merged with an
    optional JSON file (``path`` or ``CHAINLINK_REGISTRY_PATH``) and an optional
    in-memory ``extra`` dict. Malformed entries are skipped (safe fallback)."""
    registry: dict = dict(DEFAULT_FEEDS)
    file_path = path or os.getenv("CHAINLINK_REGISTRY_PATH")
    if file_path and Path(file_path).exists():
        try:
            raw = json.loads(Path(file_path).read_text(encoding="utf-8"))
            registry.update(_parse_raw(raw))
        except (ValueError, OSError):
            pass
    if extra:
        registry.update(_parse_raw(extra))
    return registry


def _parse_raw(raw: dict) -> dict:
    out: dict = {}
    for key, v in (raw or {}).items():
        if not isinstance(v, dict):
            continue
        try:
            out[key] = _spec(
                key=key, pair=str(v.get("pair", key)),
                kws=list(v.get("asset_keywords", [])),
                category=str(v.get("category", "crypto")),
                decimals=int(v.get("decimals", 8)),
                heartbeat_s=float(v.get("heartbeat_s", 3600.0)),
                chain=str(v.get("chain", "ethereum")),
                address=str(v.get("address", "")),
                description=str(v.get("description", "")))
        except (TypeError, ValueError):
            continue
    return out
