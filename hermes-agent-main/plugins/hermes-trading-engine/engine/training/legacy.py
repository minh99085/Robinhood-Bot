"""Paper-trading training pipeline: phases, metrics, and auto-reporting.

Phases gate how much Grok drives decisions, so the agent earns trust before it
ever reaches LIVE readiness (Guard 1):

  PHASE_1 OBSERVATION  (< 100 trades)   Markov only; Grok observes (builds memory)
  PHASE_2 GROK_ASSIST  (100-299)        Grok recommends; Markov has veto
  PHASE_3 GROK_PRIMARY (300-499, or 500+ if not yet ready)  Grok primary; Markov confirms
  PHASE_4 LIVE_READY   (Guard 1 passed) training gate unlocks (still paper until
                                        the user arms LIVE via Guard 2)

Metrics (recomputed at most every 60s by the engine) include Sortino, average
hold time, per-market / per-symbol breakdowns, and a Grok-accuracy score.
Reports are written to data/reports/.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

PHASES = [
    (1, "OBSERVATION", "Markov only; Grok observes"),
    (2, "GROK_ASSIST", "Grok recommends; Markov veto"),
    (3, "GROK_PRIMARY", "Grok primary; Markov confirms"),
    (4, "LIVE_READY", "Readiness met; arm LIVE via Guard 2"),
]
PHASE_1_MAX = 100
PHASE_2_MAX = 300
PHASE_3_MAX = 500  # used only for the "trades to next phase" progress bar


def phase_for(trades: int, guard1_ready: bool) -> tuple[int, str, str]:
    if guard1_ready:
        return PHASES[3]            # LIVE_READY
    if trades >= PHASE_2_MAX:
        return PHASES[2]            # 300+ (incl. 500+ not-yet-ready) -> GROK_PRIMARY
    if trades >= PHASE_1_MAX:
        return PHASES[1]            # 100-299 -> GROK_ASSIST
    return PHASES[0]                # <100 -> OBSERVATION


def next_phase_at(trades: int) -> int | None:
    for b in (PHASE_1_MAX, PHASE_2_MAX, PHASE_3_MAX):
        if trades < b:
            return b
    return None


# --------------------------------------------------------------------------
def _sortino(curve: list[dict]) -> float:
    eq = np.array([c.get("equity", 0.0) for c in curve], dtype=float)
    if eq.size < 3:
        return 0.0
    rets = np.diff(eq) / eq[:-1]
    downside = rets[rets < 0]
    dd = downside.std() if downside.size else 0.0
    if dd == 0:
        return 0.0
    return round(float(rets.mean() / dd * np.sqrt(len(rets))), 2)


def compute_metrics(trades: list[dict], curve: list[dict]) -> dict:
    settled = [t for t in trades if t["status"] in ("won", "lost", "closed")]
    wins = sum(1 for t in settled if (t["pnl"] or 0) > 0)
    total = len(settled)

    holds, per_market, per_symbol = [], {}, {}
    grok_aligned = grok_correct = 0
    for t in settled:
        try:
            meta = json.loads(t.get("meta") or "{}")
        except (ValueError, TypeError):
            meta = {}
        if meta.get("closed_ts") and t.get("ts"):
            holds.append(max(0.0, meta["closed_ts"] - t["ts"]))
        mk, sym, pnl = t["market"], t["symbol"], (t["pnl"] or 0)
        for d, k in ((per_market, mk), (per_symbol, sym)):
            e = d.setdefault(k, {"trades": 0, "pnl": 0.0, "wins": 0})
            e["trades"] += 1
            e["pnl"] = round(e["pnl"] + pnl, 2)
            e["wins"] += 1 if pnl > 0 else 0
        gd = meta.get("grok_dir")
        if gd in ("up", "down"):
            grok_aligned += 1
            if pnl > 0:
                grok_correct += 1

    return {
        "total_trades": total,
        "win_rate": round(wins / total, 4) if total else 0.0,
        "sortino": _sortino(curve),
        "avg_hold_seconds": round(sum(holds) / len(holds), 1) if holds else None,
        "per_market": per_market,
        "per_symbol": dict(sorted(per_symbol.items(), key=lambda kv: -abs(kv[1]["pnl"]))[:8]),
        "grok_accuracy": round(grok_correct / grok_aligned, 4) if grok_aligned else None,
        "grok_signals": grok_aligned,
    }


# --------------------------------------------------------------------------
class Reporter:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.reports_dir = self.data_dir / "reports"
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @staticmethod
    def _atomic_write(path: Path, obj) -> None:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def persist_ledger(self, ledger: dict) -> None:
        self._atomic_write(self.data_dir / "paper_ledger.json", ledger)

    def write_daily(self, stats: dict) -> None:
        date = time.strftime("%Y-%m-%d", time.gmtime())
        self._atomic_write(self.reports_dir / f"daily_{date}.json",
                           {"date": date, "generated_ts": time.time(), **stats})

    def write_phase_summary(self, phase_n: int, stats: dict) -> None:
        self._atomic_write(self.reports_dir / f"phase_{phase_n}_summary.json",
                           {"phase": phase_n, "generated_ts": time.time(), **stats})
