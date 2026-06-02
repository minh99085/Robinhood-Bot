#!/usr/bin/env python3
"""Safe short smoke test of the Polymarket Training Engine v2.

Runs OFFLINE on synthetic fixture data (no network, no real orders): exercises
the scanner, ranker, subscription manager, probability stack, edge engine,
paper policy + RiskEngine + PaperBroker, baselines, learner, and report
generation. PAPER ONLY.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _catalog(n=20):
    now = time.time()
    out = []
    for i in range(n):
        bid, ask = 0.28, 0.30
        out.append({
            "id": f"smoke{i}", "question": f"Smoke market {i}?", "active": True,
            "closed": False, "archived": False, "enableOrderBook": True,
            "acceptingOrders": True, "clobTokenIds": [f"t{i}a", f"t{i}b"],
            "outcomePrices": [str((bid + ask) / 2), str(1 - (bid + ask) / 2)],
            "bestBid": bid, "bestAsk": ask, "spread": round(ask - bid, 4),
            "liquidityNum": 40000, "volume24hr": 9000, "topDepthUsd": 1500,
            "volumeNum": 50000, "endDate": "2030-01-01T00:00:00Z",
            "description": "Smoke resolution text per official sources. " * 8,
            "category": ["politics", "sports"][i % 2], "bookUpdatedTs": now})
    return out


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Polymarket training v2 OFFLINE smoke test (PAPER ONLY).")
    ap.add_argument("--ticks", type=int, default=4)
    ap.add_argument("--mode", choices=["observe_only", "paper_train"], default="paper_train")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)

    import tempfile
    os.environ.setdefault("POLYMARKET_ALLOW_OFFLINE_STUB_TRADING", "0")
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    from engine.training.reports import write_reports
    from engine.campaigns.signal_models import SignalResult

    class _Research:  # research-only cached estimate (stand-in for Grok)
        name = "research"

        def evaluate(self, rec):
            return SignalResult(0.80, 0.9, "grok_cache", "smoke")

        def status(self):
            return {"name": "research", "grok_enabled": False, "grok_source": "offline_cache"}

    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp())
    cfg = TrainingConfig(mode=args.mode, max_open_trades=5, max_hold_ticks=3)
    trainer = PolymarketPaperTrainer(cfg, data_dir=data_dir, signal_model=_Research())

    pf = trainer.preflight()
    print(f"preflight ok={pf['ok']} live_detected={pf['live_detected']}")
    cat = _catalog()
    for _ in range(args.ticks):
        r = trainer.run_tick(cat)
        print(f"  tick {r['tick']}: scanned={r.get('scanned')} candidates={r.get('candidates')} "
              f"opened={r.get('opened')} open={r.get('open_positions')}")
    trainer.finalize()
    st = trainer.status()
    print(f"opened={st['pnl']['trades_opened']} closed={st['pnl']['trades_closed']} "
          f"risk_approvals={st['risk']['approvals']} fills={st['broker']['fills']}")
    print("baselines: " + ", ".join(
        f"{b['baseline_name']}={b['trade_count']}({b['pnl']})" for b in st["baselines"]))
    out = write_reports(trainer, out_root=data_dir / "polymarket_training_reports")
    print(f"report: {out['run_dir']} · recommendation={out['recommendation']}")
    print(f"files: {', '.join(out['files'])}")
    print("SMOKE OK — PAPER ONLY, no real orders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
