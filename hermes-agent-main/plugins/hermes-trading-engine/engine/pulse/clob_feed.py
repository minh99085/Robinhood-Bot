"""CLOB book feed with optional WebSocket + REST fallback (Roan Part V data pipeline).

PAPER ONLY — read-only market data; measures fetch latency for ops dashboard.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("hte.pulse.clob_feed")

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class ClobBookFeed:
    """Lightweight book cache; WebSocket when enabled, else REST hydrate callback."""

    def __init__(self, *, websocket_enabled: bool = True):
        self.websocket_enabled = bool(websocket_enabled)
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._last_fetch_ms: dict[str, float] = {}
        self._errors = 0
        self._ws_running = False
        self._subscribed: set[str] = set()

    def record_fetch(self, token_id: str, elapsed_ms: float) -> None:
        with self._lock:
            self._last_fetch_ms[token_id] = round(elapsed_ms, 2)

    def latency_report(self) -> dict:
        with self._lock:
            vals = list(self._last_fetch_ms.values())
        base = {
            "samples": len(vals),
            "avg_ms": round(sum(vals) / len(vals), 2) if vals else None,
            "max_ms": round(max(vals), 2) if vals else None,
            "websocket_enabled": self.websocket_enabled,
            "ws_running": self._ws_running,
            "ws_subscribed": len(self._subscribed),
            "errors": self._errors,
        }
        return base

    def start_ws_background(self, token_ids: list[str]) -> None:
        """Best-effort WS subscriber; fails open to REST."""
        if not self.websocket_enabled or not token_ids:
            return
        new = [t for t in token_ids if t and t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)

        def _run():
            try:
                from websockets.sync.client import connect
                self._ws_running = True
                with connect(CLOB_WS, open_timeout=10) as ws:
                    sub = {"assets_ids": list(self._subscribed), "type": "market"}
                    ws.send(json.dumps(sub))
                    while self._ws_running:
                        try:
                            raw = ws.recv(timeout=5.0)
                            msg = json.loads(raw)
                            aid = msg.get("asset_id") or msg.get("market")
                            if aid:
                                with self._lock:
                                    self._cache[str(aid)] = msg
                        except Exception:
                            break
            except Exception as exc:
                logger.debug("clob ws feed stopped: %s", exc)
                self._errors += 1
            finally:
                self._ws_running = False

        threading.Thread(target=_run, daemon=True, name="clob-book-ws").start()