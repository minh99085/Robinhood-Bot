"""ArbLedger — append-only arb-trade ledger (separate from directional trades).

Writes data/arb_trades.jsonl, tracks currently-open symbols (duplicate
suppression), and computes panel metrics (total P&L, win rate, avg net%, avg
latency, best single arb).
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path


class ArbLedger:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "arb_trades.jsonl"
        self._open: set[str] = set()
        self._recent: deque = deque(maxlen=200)
        self._load_recent()

    def _load_recent(self) -> None:
        try:
            for ln in self.path.read_text(encoding="utf-8").splitlines()[-200:]:
                try:
                    self._recent.append(json.loads(ln))
                except ValueError:
                    continue
        except OSError:
            pass

    def is_open_trade(self, symbol: str) -> bool:
        return symbol.upper() in self._open

    def mark_open(self, symbol: str) -> None:
        self._open.add(symbol.upper())

    def mark_closed(self, symbol: str) -> None:
        self._open.discard(symbol.upper())

    def record(self, rec: dict) -> None:
        rec.setdefault("timestamp", time.time())
        self._recent.append(rec)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def recent(self, n: int = 25) -> list[dict]:
        return list(self._recent)[-n:][::-1]

    def metrics(self) -> dict:
        rows = [r for r in self._recent if r.get("outcome") in ("profit", "loss", "breakeven")]
        n = len(rows)
        if not n:
            return {"trades": 0, "total_profit": 0.0, "win_rate": None,
                    "avg_net_pct": None, "avg_latency_ms": None, "best": 0.0,
                    "incidents": sum(1 for r in self._recent if r.get("outcome") == "incident")}
        profits = [r.get("profitUSD_actual", 0.0) for r in rows]
        nets = [r.get("netPct_actual", 0.0) for r in rows]
        lats = [r.get("executionLatency_ms", 0.0) for r in rows]
        wins = sum(1 for p in profits if p > 0)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        today_n = sum(1 for r in rows if time.strftime("%Y-%m-%d", time.gmtime(r.get("timestamp", 0))) == today)
        return {
            "trades": n, "trades_today": today_n,
            "total_profit": round(sum(profits), 2),
            "win_rate": round(wins / n, 4),
            "avg_net_pct": round(sum(nets) / n, 4),
            "avg_latency_ms": round(sum(lats) / n, 1),
            "best": round(max(profits), 2),
            "incidents": sum(1 for r in self._recent if r.get("outcome") == "incident"),
        }
