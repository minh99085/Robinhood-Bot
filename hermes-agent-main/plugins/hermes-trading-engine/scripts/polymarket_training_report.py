#!/usr/bin/env python3
"""Write a Polymarket PAPER training report bundle.

If a persisted training status exists, report from it. Otherwise run a short
OFFLINE synthetic training session so the report is real (not a stub). PAPER
ONLY — no network, no real orders.

Outputs under polymarket_training_reports/<run_id>/:
  summary.json report.md candidates.csv edge_diagnostics.csv orders.csv
  fills.csv learning.csv no_trade_reasons.csv calibration.csv
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.training.reports import write_reports  # noqa: E402


def _data_dir() -> Path:
    try:
        from engine.config import Settings
        return Path(Settings().data_dir)
    except Exception:  # noqa: BLE001
        return Path(os.getenv("HTE_DATA_DIR") or ".")


def _offline_demo_trainer():
    """Run a short offline synthetic training session (allows stub trading so the
    report has trades) — purely to demonstrate the report shape with no data."""
    os.environ.setdefault("POLYMARKET_ALLOW_OFFLINE_STUB_TRADING", "1")
    from engine.training import PolymarketPaperTrainer, TrainingConfig
    now = time.time()

    def mkt(i):
        bid, ask = 0.40, 0.42
        return {"id": f"d{i}", "question": f"Demo market {i}?", "active": True,
                "closed": False, "archived": False, "enableOrderBook": True,
                "acceptingOrders": True, "clobTokenIds": [f"t{i}a", f"t{i}b"],
                "outcomePrices": [str((bid + ask) / 2), str(1 - (bid + ask) / 2)],
                "bestBid": bid, "bestAsk": ask, "spread": round(ask - bid, 4),
                "liquidityNum": 20000, "volume24hr": 8000, "topDepthUsd": 1000,
                "volumeNum": 40000, "endDate": "2030-01-01T00:00:00Z",
                "description": "Demo resolution text per official sources. " * 6,
                "category": ["politics", "sports"][i % 2], "bookUpdatedTs": now}

    cfg = TrainingConfig.from_env()
    t = PolymarketPaperTrainer(cfg)
    cat = [mkt(i) for i in range(20)]
    for _ in range(4):
        t.run_tick(cat)
    t.finalize()
    return t


class _DemoResearch:
    """Deterministic cached-research signal for the OFFLINE final-validation demo
    (research-only; it cannot place/size/approve an order)."""

    name = "research"

    def __init__(self, fair: float = 0.80, conf: float = 0.9):
        self._fair, self._conf = fair, conf

    def evaluate(self, rec):
        from engine.campaigns.signal_models import SignalResult
        return SignalResult(self._fair, self._conf, "grok_cache", "demo-est")

    def status(self) -> dict:
        return {"name": "research", "grok_enabled": False,
                "grok_source": "offline_cache", "research_mode": "offline_cache"}


def _run_demo(*, aggressive: bool, ticks: int = 4):
    """Deterministic OFFLINE demo trainer (PAPER ONLY) for the final validation."""
    from engine.training import (AggressivePaperTrainingConfig, PolymarketPaperTrainer,
                                 TrainingConfig)
    cats = ["politics", "sports", "crypto", "econ", "tech"]

    def mkt(i):
        bid, ask = 0.28, 0.30
        return {"id": f"d{i}", "question": f"Demo market {i}?", "active": True,
                "closed": False, "archived": False, "enableOrderBook": True,
                "acceptingOrders": True, "clobTokenIds": [f"t{i}a", f"t{i}b"],
                "outcomePrices": [str((bid + ask) / 2), str(1 - (bid + ask) / 2)],
                "bestBid": bid, "bestAsk": ask, "spread": round(ask - bid, 4),
                "liquidityNum": 20000, "volume24hr": 8000, "topDepthUsd": 2000,
                "volumeNum": 40000, "endDate": "2030-01-01T00:00:00Z",
                "description": "Demo resolution text per official sources. " * 6,
                "category": cats[i % len(cats)], "bookUpdatedTs": time.time()}

    cfg = (AggressivePaperTrainingConfig(max_hold_ticks=2) if aggressive
           else TrainingConfig(mode="paper_train", max_hold_ticks=2))
    t = PolymarketPaperTrainer(cfg, signal_model=_DemoResearch())
    cat = [mkt(i) for i in range(25)]
    for _ in range(ticks):
        t.run_tick(cat)
    t.finalize()
    return t


def _system_block(t) -> dict:
    """Build the metric block for the final validation report from a trainer."""
    from engine.replay import metrics as _m
    closed = [p for p in t.positions if p.closed]
    eqs = [float(t.cfg.starting_bankroll)]
    for p in closed:
        eqs.append(eqs[-1] + p.realized_pnl)
    trades = [{"realized_pnl": p.realized_pnl, "cost": p.cost, "net_edge": p.net_edge,
               "category": p.category} for p in closed]
    preds = [p.p_final for p in closed]
    outs = [1.0 if p.realized_pnl > 0 else 0.0 for p in closed]
    inst = _m.institutional_metrics(equities=eqs, trades=trades, decisions=t.decision_count,
                                    rejections=t.rejection_count, predictions=preds,
                                    outcomes=outs, notional_traded=sum(p.cost for p in t.positions))
    breg = t.bregman_summary().get("last_scan_metrics", {})
    pnl = t.pnl_summary()
    cl = t.chainlink.metrics() if t.chainlink else {}
    return {
        "trade_count": pnl["trades_opened"],
        "unique_markets": len({p.market_id for p in t.positions}),
        "feedback_samples": int(t.learner.closed),
        "sharpe": inst["sharpe"], "sortino": inst["sortino"], "calmar": inst["calmar"],
        "omega": inst["omega"], "max_drawdown": inst["max_drawdown"],
        "expectancy": inst["expectancy"], "brier": inst["brier_score"],
        "log_loss": inst["log_loss"], "ece": inst["ece"],
        "realized_edge": inst["realized_edge"],
        "fill_quality": round(t.broker.fills / max(1, t.broker.orders), 6),
        "chainlink_impact": float(cl.get("avg_probability_impact", 0.0)),
        "bregman_certified_profit": float(breg.get("certified_profit", 0.0)),
        "false_positive_rate": float(breg.get("false_positive_rate", 0.0)),
        "paper_only": bool(t.cfg.is_paper_only), "live_orders": 0,
    }


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write a Polymarket PAPER training report.")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--out-root", default="polymarket_training_reports")
    ap.add_argument("--demo", action="store_true", help="force an offline synthetic demo run")
    ap.add_argument("--baseline-report", action="store_true",
                    help="print the algorithm inventory + institutional metrics baseline")
    ap.add_argument("--final-validation", action="store_true",
                    help="run conservative vs Chainlink+Bregman-first aggressive offline demos "
                         "and print the final baseline-vs-upgraded validation report (PAPER ONLY)")
    args = ap.parse_args(argv)

    if args.final_validation:
        import json as _json
        from engine.training.final_validation import final_validation_report
        cons = _system_block(_run_demo(aggressive=False))
        upg = _system_block(_run_demo(aggressive=True))
        rep = final_validation_report(cons, upg)
        print("=" * 64)
        print("FINAL QUANTITATIVE VALIDATION — conservative directional-only vs")
        print("Chainlink + Bregman-first AGGRESSIVE paper training (PAPER ONLY)")
        print("=" * 64)
        print(_json.dumps(rep, indent=2, default=str))
        print(f"\nproduction_ready: {rep['production_ready']}  "
              f"no_regression: {rep['no_regression_ok']}  paper_only: {rep['paper_only']}")
        return 0

    if args.baseline_report:
        from engine.training.algorithm_inventory import algorithm_inventory
        inv = algorithm_inventory()
        print("=" * 60)
        print("Polymarket Training — Algorithm Inventory Baseline")
        print(f"  active   : {', '.join(inv['active'])}")
        print(f"  disabled : {', '.join(inv['disabled'])}")
        print(f"  absent   : {', '.join(inv['absent'])}")
        print(f"  chainlink_present : {inv['chainlink_present']}")
        print(f"  bregman_present   : {inv['bregman_present']} "
              f"(flagship Polymarket Bregman arbitrage: {'active' if inv['bregman_present'] else 'absent'})")
        print("  signal_priority   : 1=bregman_arbitrage > 2=statistical_mispricing "
              "> 3=directional")
        print(f"  legacy_arb_disabled: {inv['legacy_arb_disabled']}")
        print(f"  gaps     : {', '.join(inv['gaps']) or 'none'}")
        return 0

    dd = Path(args.data_dir) if args.data_dir else _data_dir()
    status_path = dd / "polymarket_training.json"

    if status_path.exists() and not args.demo:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        out = write_reports(status=status, out_root=Path(args.out_root))
        print(f"report from persisted status → {out['run_dir']}")
    else:
        if not args.demo:
            print("no persisted training status; running a short OFFLINE synthetic demo...")
        trainer = _offline_demo_trainer()
        out = write_reports(trainer, out_root=Path(args.out_root))
        print(f"offline demo report → {out['run_dir']}")
    print(f"recommendation: {out['recommendation']}")
    print("files: " + ", ".join(out["files"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
