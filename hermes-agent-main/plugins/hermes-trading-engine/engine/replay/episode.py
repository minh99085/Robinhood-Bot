"""Replay episode / event / config models (Phase 4).

Deterministic, offline backtest primitives. Nothing here touches the network or
submits live orders. ``ReplayConfig`` is the reproducibility contract: the same
config (same ``config_hash``) + same saved raw events + same seed yields the
same metrics.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default) not in ("0", "false", "False", "")


# Event types the replay loader / runner understand.
REPLAY_EVENT_TYPES = frozenset({
    "book", "price_change", "tick_size_change", "last_trade_price", "best_bid_ask",
    "new_market", "market_resolved", "orderbook_snapshot", "orderbook_delta",
    "synthetic_timer", "synthetic_resolution", "strategy_tick", "equity_snapshot",
})

# Market-data events that mutate the reconstructed order book.
MARKET_DATA_EVENT_TYPES = frozenset({
    "book", "price_change", "tick_size_change", "last_trade_price", "best_bid_ask",
    "new_market", "market_resolved", "orderbook_snapshot", "orderbook_delta",
})


@dataclass
class ReplayEvent:
    ts_ms: int
    event_type: str
    venue: str = ""
    source: str = ""
    market_id: Optional[str] = None
    asset_id: Optional[str] = None
    payload: dict = field(default_factory=dict)
    sequence: int = 0
    raw_event_id: Optional[int] = None

    def payload_hash(self) -> str:
        try:
            return hashlib.sha256(
                json.dumps(self.payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
        except Exception:  # noqa: BLE001
            return ""


@dataclass
class ReplayEpisode:
    episode_id: str
    venue: str = ""
    market_ids: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    start_ts_ms: Optional[int] = None
    end_ts_ms: Optional[int] = None
    event_count: int = 0
    source: str = ""
    config_hash: str = ""
    seed: int = 42
    notes: str = ""

    def record(self) -> dict:
        return {
            "episode_id": self.episode_id, "venue": self.venue,
            "market_ids": self.market_ids, "asset_ids": self.asset_ids,
            "start_ts_ms": self.start_ts_ms, "end_ts_ms": self.end_ts_ms,
            "event_count": self.event_count, "source": self.source,
            "config_hash": self.config_hash, "seed": self.seed, "notes": self.notes,
        }


class ReplayConfig(BaseModel):
    """Frozen, hashable replay configuration."""

    model_config = ConfigDict(extra="ignore")

    replay_run_id: Optional[str] = None
    episode_id: Optional[str] = None
    seed: int = Field(default_factory=lambda: _env_int("REPLAY_DEFAULT_SEED", 42))
    venue: Optional[str] = None
    market_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    start_ts_ms: Optional[int] = None
    end_ts_ms: Optional[int] = None
    max_events: Optional[int] = None
    policy_name: str = "noop"
    policy_params: dict[str, Any] = Field(default_factory=dict)
    strategy_tick_ms: int = Field(default_factory=lambda: _env_int("REPLAY_STRATEGY_TICK_MS", 1000))
    equity_snapshot_ms: int = Field(default_factory=lambda: _env_int("REPLAY_EQUITY_SNAPSHOT_MS", 1000))
    initial_cash: float = Field(default_factory=lambda: float(_env_int("REPLAY_DEFAULT_INITIAL_CASH", 10000)))
    allow_grok_network: bool = Field(default_factory=lambda: _env_flag("REPLAY_ALLOW_GROK_NETWORK", "0"))
    use_cached_grok_estimates: bool = Field(default_factory=lambda: _env_flag("REPLAY_USE_CACHED_GROK", "1"))
    allow_reference_price_fills: bool = False  # prediction markets: CLOB-only by default
    end_open_order_policy: str = Field(default_factory=lambda: os.getenv("REPLAY_END_OPEN_ORDER_POLICY", "cancel"))
    mark_to_market: bool = Field(default_factory=lambda: _env_flag("REPLAY_MARK_TO_MARKET", "1"))
    require_resolution_for_realized_pnl: bool = False
    deterministic_shuffle_same_timestamp: bool = False
    persist_processed_events: bool = True
    report_format: str = "json"
    output_dir: str = Field(default_factory=lambda: os.getenv("REPLAY_OUTPUT_DIR", "replay_artifacts"))
    # source: SQLite store (default) or a JSONL/JSONL.GZ file
    from_jsonl: Optional[str] = None
    dedup_raw_events: bool = True
    stale_ms: int = Field(default_factory=lambda: _env_int("POLYMARKET_CLOB_STALE_MS", 3000))

    def frozen_dict(self) -> dict:
        d = self.model_dump()
        d.pop("replay_run_id", None)  # volatile: excluded from the hash
        return d

    def config_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.frozen_dict(), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
