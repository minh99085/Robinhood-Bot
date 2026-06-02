"""Chainlink oracle readings + data sources.

Quant responsibility — *Data Acquisition & Ingestion*: pulls the latest Chainlink
round (answer, decimals, updatedAt, roundId) and computes freshness/staleness.

Sources:
* ``StaticChainlinkSource`` — deterministic, snapshot-backed (tests + replay).
* ``ReplayChainlinkSource`` — cursor-bound; only returns readings observed at or
  before the replay cursor so backtests NEVER see future oracle data.
* ``RpcChainlinkSource`` — OPTIONAL live read via a public JSON-RPC ``eth_call``
  to ``latestRoundData()`` (off unless ``CHAINLINK_RPC_URL`` is set). Lazy httpx
  import; any failure returns ``None`` (fail-closed, never raises).

Compliance/Security: no private keys, no signing, no secret material. The RPC
source only performs read-only ``eth_call``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ChainlinkReading:
    """One Chainlink round. ``updated_at`` is the on-chain ``updatedAt`` (unix s);
    ``observed_ts`` is when *we* recorded it (used for replay timestamp-safety)."""

    feed_key: str
    answer_raw: int
    decimals: int
    updated_at: float
    round_id: int
    observed_ts: float
    chain: str = "ethereum"

    @property
    def value(self) -> float:
        return self.answer_raw / (10 ** self.decimals) if self.decimals >= 0 else float(self.answer_raw)

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.updated_at)

    def is_stale(self, now: Optional[float] = None, heartbeat_s: float = 3600.0,
                 grace: float = 1.5) -> bool:
        return self.age_s(now) > heartbeat_s * grace

    def to_dict(self) -> dict:
        return {"feed_key": self.feed_key, "value": round(self.value, 8),
                "answer_raw": self.answer_raw, "decimals": self.decimals,
                "updated_at": self.updated_at, "round_id": self.round_id,
                "observed_ts": self.observed_ts, "chain": self.chain}


class ChainlinkSource:
    """Abstract source of Chainlink readings."""

    def read(self, spec, now: Optional[float] = None) -> Optional[ChainlinkReading]:
        raise NotImplementedError

    def history(self, feed_key: str, now: Optional[float] = None,
                limit: int = 50) -> list:
        raise NotImplementedError


class StaticChainlinkSource(ChainlinkSource):
    """In-memory source backed by per-feed reading lists. Timestamp-safe: only
    returns readings with ``observed_ts <= now`` (when ``now`` is given)."""

    def __init__(self, readings: Optional[dict] = None):
        # feed_key -> list[ChainlinkReading]
        self._readings: dict = {}
        for k, v in (readings or {}).items():
            self._readings[k] = sorted(v, key=lambda r: r.observed_ts)

    def add(self, reading: ChainlinkReading) -> None:
        self._readings.setdefault(reading.feed_key, []).append(reading)
        self._readings[reading.feed_key].sort(key=lambda r: r.observed_ts)

    def _visible(self, feed_key: str, now: Optional[float]) -> list:
        rs = self._readings.get(feed_key, [])
        if now is None:
            return list(rs)
        return [r for r in rs if r.observed_ts <= now]

    def history(self, feed_key: str, now: Optional[float] = None, limit: int = 50) -> list:
        return self._visible(feed_key, now)[-limit:]

    def read(self, spec, now: Optional[float] = None) -> Optional[ChainlinkReading]:
        key = spec.key if hasattr(spec, "key") else str(spec)
        vis = self._visible(key, now)
        return vis[-1] if vis else None


class ReplayChainlinkSource(StaticChainlinkSource):
    """Cursor-bound replay source: build from a flat list of readings, then set
    ``cursor`` (unix s). Reads ignore any reading observed after the cursor so a
    backtest at time T can never use oracle data published after T."""

    def __init__(self, readings: Optional[list] = None, cursor: Optional[float] = None):
        grouped: dict = {}
        for r in (readings or []):
            grouped.setdefault(r.feed_key, []).append(r)
        super().__init__(grouped)
        self.cursor = cursor

    @classmethod
    def from_jsonl(cls, path, cursor: Optional[float] = None) -> "ReplayChainlinkSource":
        """Build a replay source from a JSONL snapshot file (one reading per
        line). Replay-safe: with ``cursor`` set, reads never return a reading
        observed after the cursor, so a backtest at time T cannot use future
        oracle data. No network, deterministic."""
        import json
        from pathlib import Path
        readings: list = []
        text = Path(path).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            try:
                readings.append(ChainlinkReading(
                    feed_key=str(d["feed_key"]), answer_raw=int(d["answer_raw"]),
                    decimals=int(d.get("decimals", 8)), updated_at=float(d["updated_at"]),
                    round_id=int(d.get("round_id", 0)),
                    observed_ts=float(d.get("observed_ts", d["updated_at"])),
                    chain=str(d.get("chain", "ethereum"))))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(readings, cursor=cursor)

    def _effective(self, now: Optional[float]) -> Optional[float]:
        if self.cursor is None:
            return now
        return self.cursor if now is None else min(now, self.cursor)

    def history(self, feed_key: str, now: Optional[float] = None, limit: int = 50) -> list:
        return super().history(feed_key, self._effective(now), limit)

    def read(self, spec, now: Optional[float] = None) -> Optional[ChainlinkReading]:
        return super().read(spec, self._effective(now))


class RpcChainlinkSource(ChainlinkSource):
    """OPTIONAL live source via read-only JSON-RPC ``eth_call``. Disabled unless
    a ``rpc_url`` (or ``CHAINLINK_RPC_URL``) is provided. Never raises."""

    _LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData()
    _DECIMALS = "0x313ce567"           # decimals()

    def __init__(self, rpc_url: Optional[str] = None, timeout_s: float = 6.0):
        self.rpc_url = rpc_url or os.getenv("CHAINLINK_RPC_URL") or ""
        self.timeout_s = timeout_s
        self.enabled = bool(self.rpc_url)

    def history(self, feed_key: str, now: Optional[float] = None, limit: int = 50) -> list:
        r = None
        return [r] if (r := self.read_by_key(feed_key, now)) else []

    def read(self, spec, now: Optional[float] = None) -> Optional[ChainlinkReading]:
        if not self.enabled or not getattr(spec, "address", ""):
            return None
        return self._read(spec.key, spec.address, getattr(spec, "decimals", 8), now)

    def read_by_key(self, feed_key, now=None) -> Optional[ChainlinkReading]:  # pragma: no cover
        return None

    def _read(self, key, address, decimals, now) -> Optional[ChainlinkReading]:  # pragma: no cover
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return None
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(self.rpc_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": address, "data": self._LATEST_ROUND_DATA}, "latest"]})
                hexdata = resp.json().get("result", "")
            if not hexdata or len(hexdata) < 2 + 64 * 5:
                return None
            words = hexdata[2:]
            round_id = int(words[0:64], 16)
            answer = int(words[64:128], 16)
            if answer >= 2 ** 255:                # int256 two's-complement
                answer -= 2 ** 256
            updated_at = int(words[192:256], 16)
            return ChainlinkReading(
                feed_key=key, answer_raw=answer, decimals=int(decimals),
                updated_at=float(updated_at), round_id=round_id,
                observed_ts=(now if now is not None else time.time()))
        except Exception:  # noqa: BLE001 — fail-closed, never raise
            return None
