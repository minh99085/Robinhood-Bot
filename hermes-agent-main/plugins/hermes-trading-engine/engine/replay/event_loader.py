"""ReplayEventLoader — load normalized events from SQLite or JSONL(.gz).

Pure local I/O: it never hits the network. Events are sorted deterministically
by (ts_ms, sequence) so replays are reproducible, optionally de-duplicated, and
filterable by venue / market / asset / time / type.

Quant scope — *Data Acquisition & Ingestion* + *Backtesting & Simulation*: the
deterministic, offline event source for all replay validation (walk-forward,
robustness, Bregman + Chainlink replay analytics). Event time drives the book
reconstruction, so replay never uses future data.
"""

from __future__ import annotations

import gzip
import json
from typing import Optional

from .episode import ReplayEvent


def _open(path: str):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, "r", encoding="utf-8")


class ReplayEventLoader:
    def __init__(self, store=None):
        self.store = store

    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(obj: dict, seq: int, raw_id: Optional[int] = None) -> Optional[ReplayEvent]:
        et = obj.get("event_type") or obj.get("type")
        if not et:
            return None
        ts = obj.get("ts_ms")
        if ts is None:
            ts = obj.get("timestamp")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return None
        venue = obj.get("venue", "") or ""
        market_id = obj.get("market_id") or obj.get("market") or obj.get("condition_id")
        asset_id = obj.get("asset_id") or obj.get("asset")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else dict(obj)
        # ensure the order-book reconstructor uses EVENT time, not wall clock
        payload.setdefault("timestamp", ts)
        if asset_id is not None:
            payload.setdefault("asset_id", asset_id)
        if market_id is not None:
            payload.setdefault("market", market_id)
        payload.setdefault("event_type", et)
        s = obj.get("sequence")
        sequence = int(s) if s is not None else int(seq)  # preserve sequence==0
        return ReplayEvent(ts_ms=ts, event_type=str(et), venue=str(venue),
                           source=obj.get("source", "") or "", market_id=market_id,
                           asset_id=asset_id, payload=payload,
                           sequence=sequence, raw_event_id=raw_id)

    # ------------------------------------------------------------------ #
    def from_jsonl(self, path: str, **filters) -> list[ReplayEvent]:
        events: list[ReplayEvent] = []
        with _open(path) as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                ev = self._normalize(obj, seq=i)
                if ev is not None:
                    events.append(ev)
        return self._post(events, **filters)

    def from_sqlite(self, **filters) -> list[ReplayEvent]:
        if self.store is None:
            return []
        rows = self.store.get_recent_raw_market_events(filters.get("_db_limit", 1000000))
        events: list[ReplayEvent] = []
        for r in rows:
            payload = r.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            obj = {
                "event_type": r.get("event_type"), "ts_ms": r.get("ts_ms"),
                "venue": r.get("venue"), "market_id": r.get("market_id"),
                "asset_id": r.get("asset_id"), "source": r.get("source"),
                "payload": payload,
            }
            ev = self._normalize(obj, seq=r.get("ts_ms") or 0, raw_id=None)
            if ev is not None:
                events.append(ev)
        return self._post(events, **filters)

    # ------------------------------------------------------------------ #
    def _post(self, events: list[ReplayEvent], *, venue=None, market_id=None,
              market_ids=None, asset_id=None, asset_ids=None, start_ts_ms=None,
              end_ts_ms=None, event_type=None, max_events=None, dedup=False,
              **_ignored) -> list[ReplayEvent]:
        markets = set(market_ids or [])
        if market_id:
            markets.add(market_id)
        assets = set(asset_ids or [])
        if asset_id:
            assets.add(asset_id)

        def keep(e: ReplayEvent) -> bool:
            if venue and e.venue and e.venue != venue:
                return False
            if markets and e.market_id not in markets:
                return False
            if assets and e.asset_id not in assets:
                return False
            if start_ts_ms is not None and e.ts_ms < start_ts_ms:
                return False
            if end_ts_ms is not None and e.ts_ms > end_ts_ms:
                return False
            if event_type and e.event_type != event_type:
                return False
            return True

        filtered = [e for e in events if keep(e)]
        if dedup:
            seen = set()
            deduped = []
            for e in filtered:
                key = (e.ts_ms, e.event_type, e.market_id, e.asset_id, e.payload_hash())
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(e)
            filtered = deduped
        # deterministic stable sort
        filtered.sort(key=lambda e: (e.ts_ms, e.sequence, e.raw_event_id or 0))
        if max_events:
            filtered = filtered[: int(max_events)]
        return filtered
