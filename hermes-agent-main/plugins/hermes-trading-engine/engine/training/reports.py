"""Reports for the Polymarket paper training engine.

Writes, under ``polymarket_training_reports/<run_id>/``:

  summary.json  report.md  candidates.csv  edge_diagnostics.csv  orders.csv
  fills.csv  learning.csv  no_trade_reasons.csv  calibration.csv

and computes a recommendation:
  CONTINUE_TRAINING | FIX_DATA | FIX_STRATEGY | LOWER_RISK |
  READY_FOR_SHADOW_POLYMARKET_ONLY

Quant scope — *Live Trading & Monitoring* + *Strategy Optimization & Robustness
Testing*: assembles the training report bundle (calibration, Bregman + signal +
portfolio analytics, aggressive-mode learning metrics). The final
baseline-vs-upgraded validation report is produced by
``engine.training.final_validation``. PAPER ONLY — read-only reporting.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Optional

RECOMMENDATIONS = (
    "CONTINUE_TRAINING", "FIX_DATA", "FIX_STRATEGY", "LOWER_RISK",
    "READY_FOR_SHADOW_POLYMARKET_ONLY",
)

_DATA_REASONS = {"no_fresh_clob_book", "no_executable_ask", "spread_too_wide",
                 "depth_too_thin", "fill:missing_orderbook_no_fantasy_fills"}


def recommend(status: dict) -> str:
    pnl = status.get("pnl", {})
    learning = status.get("learning", {})
    safety = status.get("safety", {})
    scan = status.get("scan_metrics", {})

    if not safety.get("ok", True) or safety.get("live_detected"):
        return "FIX_STRATEGY"
    if scan.get("scanned", 0) > 0 and scan.get("kept", 0) == 0:
        return "FIX_DATA"

    closed = pnl.get("trades_closed", 0) or 0
    no_trade = learning.get("no_trade_reasons", {}) or {}
    total_no_trade = sum(no_trade.values())
    data_share = (sum(v for k, v in no_trade.items() if k in _DATA_REASONS)
                  / total_no_trade) if total_no_trade else 0.0
    if closed == 0 and total_no_trade > 0 and data_share >= 0.5:
        return "FIX_DATA"

    total_pnl = pnl.get("total_pnl", 0.0) or 0.0
    start = pnl.get("starting_bankroll", 0.0) or 0.0
    calib_err = learning.get("calibration_error", 0.0) or 0.0
    if start and total_pnl <= -0.10 * start:
        return "LOWER_RISK"
    if total_pnl < 0 and calib_err > 0.12:
        return "FIX_STRATEGY"

    # must beat the baselines on realized PnL once it has traded enough
    res = {b.get("baseline_name"): b for b in status.get("baselines", [])}
    cur = res.get("current_strategy", {})
    if closed >= 10 and cur.get("scored_trades", 0) >= 10:
        others = max((res.get(n, {}).get("pnl", 0.0) or 0.0)
                     for n in ("do_nothing", "market_midpoint", "naive_price_extreme"))
        if (cur.get("pnl", 0.0) or 0.0) < others:
            return "FIX_STRATEGY"

    win_rate = pnl.get("win_rate") or 0.0
    if closed >= 30 and win_rate >= 0.55 and calib_err <= 0.10 and total_pnl > 0:
        return "READY_FOR_SHADOW_POLYMARKET_ONLY"
    return "CONTINUE_TRAINING"


def _write_csv(path: Path, rows: list, headers: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_reports(trainer=None, *, status: Optional[dict] = None,
                  out_root: Optional[Path] = None, run_id: Optional[str] = None) -> dict:
    status = status or (trainer.status() if trainer is not None else {})
    run_id = run_id or status.get("run_id") or f"pmtrain-{int(time.time())}"
    out_root = Path(out_root) if out_root else Path("polymarket_training_reports")
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rec = recommend(status)
    status = {**status, "recommendation": rec}

    (run_dir / "summary.json").write_text(json.dumps(status, default=str, indent=2),
                                          encoding="utf-8")

    candidates = getattr(trainer, "candidates_log", []) if trainer else []
    edges = getattr(trainer, "edge_log", []) if trainer else []
    orders = getattr(trainer, "orders_log", []) if trainer else []
    fills = getattr(trainer, "fills_log", []) if trainer else []
    positions = [p.__dict__ for p in getattr(trainer, "positions", [])] if trainer else []

    _write_csv(run_dir / "candidates.csv", candidates,
               ["tick", "market_id", "category", "p_market_mid", "p_final",
                "net_edge", "threshold", "decision"])
    _write_csv(run_dir / "edge_diagnostics.csv", edges,
               ["tick", "market_id", "executable_price", "p_final", "p_research",
                "research_source", "gross_edge", "cost_penalty", "net_edge",
                "uncertainty_band", "threshold", "should_trade", "reason"])
    _write_csv(run_dir / "orders.csv", orders,
               ["order_id", "proposal_id", "risk_decision_id", "market_id",
                "status", "reason"])
    _write_csv(run_dir / "fills.csv", fills,
               ["proposal_id", "risk_decision_id", "order_id", "fill_id",
                "market_id", "asset_id", "side", "outcome", "price", "qty",
                "notional", "tick"])
    _write_csv(run_dir / "learning.csv", positions,
               ["market_id", "category", "entry_price", "exit_price", "qty",
                "p_final", "net_edge", "realized_pnl", "close_reason", "closed"])

    learning = status.get("learning", {})
    ntr = [{"reason": k, "count": v} for k, v in
           sorted((learning.get("no_trade_reasons") or {}).items(),
                  key=lambda kv: kv[1], reverse=True)]
    _write_csv(run_dir / "no_trade_reasons.csv", ntr, ["reason", "count"])
    _write_csv(run_dir / "calibration.csv", learning.get("calibration", []),
               ["bucket", "n", "predicted", "actual", "gap"])

    # bucket_stats.csv: flatten edge buckets + calibration + category reliability
    bucket_rows = []
    for name, v in (learning.get("edge_buckets") or {}).items():
        bucket_rows.append({"bucket_type": "edge", "bucket_name": name,
                            "sample_count": v.get("n"), "trade_count": v.get("n"),
                            "pnl": v.get("pnl"), "wins": v.get("wins")})
    for row in learning.get("calibration", []):
        bucket_rows.append({"bucket_type": "calibration", "bucket_name": row.get("bucket"),
                            "sample_count": row.get("n"), "predicted": row.get("predicted"),
                            "actual": row.get("actual"), "gap": row.get("gap")})
    for cat, rel in (learning.get("category_reliability") or {}).items():
        bucket_rows.append({"bucket_type": "category", "bucket_name": cat,
                            "reliability_score": rel})
    _write_csv(run_dir / "bucket_stats.csv", bucket_rows,
               ["bucket_type", "bucket_name", "sample_count", "trade_count", "pnl",
                "wins", "predicted", "actual", "gap", "reliability_score"])

    _write_csv(run_dir / "baselines.csv", status.get("baselines", []),
               ["baseline_name", "trade_count", "scored_trades", "pnl", "win_rate",
                "drawdown"])

    (run_dir / "report.md").write_text(_markdown(status, run_id), encoding="utf-8")
    return {"run_id": run_id, "run_dir": str(run_dir), "recommendation": rec,
            "files": sorted(p.name for p in run_dir.iterdir())}


def _markdown(status: dict, run_id: str) -> str:
    pnl = status.get("pnl", {})
    scan = status.get("scan_metrics", {})
    risk = status.get("risk", {})
    learning = status.get("learning", {})
    fb = status.get("feedback", {})
    safety = status.get("safety", {})
    L = []
    a = L.append
    a(f"# Polymarket PAPER Training Report — {run_id}\n")
    a("## 1. Run summary")
    a(f"- mode: **{status.get('mode', 'paper')}** (PAPER ONLY) · polymarket_only: "
      f"{status.get('polymarket_only')}")
    a(f"- ticks: {status.get('tick')} · runtime: {status.get('runtime_seconds')}s\n")
    a("## 2. Safety status")
    a(f"- preflight ok: **{safety.get('ok')}** · live_detected: {safety.get('live_detected')}")
    a(f"- arbitrage_disabled: {safety.get('checks', {}).get('arbitrage_disabled')}\n")
    a("## 3. Scan speed")
    a(f"- scanned: {scan.get('scanned')} · kept: {scan.get('kept')} · shortlisted: "
      f"{scan.get('shortlisted')} · subscribed_assets: {scan.get('subscribed_assets')}")
    a(f"- scan_latency_ms: {scan.get('scan_latency_ms')} · candidates/s: "
      f"{scan.get('candidates_per_second')}\n")
    a("## 4. Market funnel")
    a(f"- scanned -> kept -> shortlisted: {scan.get('scanned')} -> {scan.get('kept')} -> "
      f"{scan.get('shortlisted')}\n")
    a("## 5-8. Trade results / PnL / Drawdown")
    a(f"- trades opened: {pnl.get('trades_opened')} · closed: {pnl.get('trades_closed')} · "
      f"win_rate: {pnl.get('win_rate')}")
    a(f"- realized: {pnl.get('realized_pnl')} · unrealized: {pnl.get('unrealized_pnl')} · "
      f"total: {pnl.get('total_pnl')} · equity: {pnl.get('equity')}\n")
    a("## 9. Risk approvals / rejections")
    a(f"- approvals: {risk.get('approvals')} · rejections: {risk.get('rejections')}\n")
    a("## 10. No-trade reasons")
    for k, v in sorted((learning.get("no_trade_reasons") or {}).items(),
                       key=lambda kv: kv[1], reverse=True):
        a(f"- {k}: {v}")
    a("")
    a("## 11. Calibration buckets")
    for row in learning.get("calibration", []):
        a(f"- {row['bucket']}: n={row['n']} pred={row['predicted']} actual={row['actual']} "
          f"gap={row['gap']}")
    a(f"- calibration_error: {learning.get('calibration_error')}\n")
    a("## 12. Edge bucket PnL")
    for k, v in (learning.get("edge_buckets") or {}).items():
        a(f"- {k}: n={v.get('n')} pnl={v.get('pnl')} wins={v.get('wins')}")
    a("")
    a("## 13. Markout by horizon")
    a(f"- {learning.get('markouts')}\n")
    a("## 14. Model weight changes (feedback)")
    a(f"- edge_adjustment: {fb.get('edge_adjustment')} · updates: {fb.get('updates')}\n")
    cl = status.get("chainlink", {})
    a("## 14b. Chainlink oracle layer (advisory, read-only)")
    if cl and cl.get("enabled") is not False:
        a(f"- feeds_scanned: {cl.get('feeds_scanned')} · stale_feeds: {cl.get('stale_feeds')} "
          f"· matched_markets: {cl.get('matched_markets')}")
        a(f"- avg_probability_impact: {cl.get('avg_probability_impact')} "
          f"· avg_signal_impact: {cl.get('avg_signal_impact')} "
          f"· signals_emitted: {cl.get('signals_emitted')}")
    else:
        a("- Chainlink scanner OFF (advisory feature layer; never trades on its own)")
    a("")
    cr = learning.get("category_reliability") or {}
    best = sorted(cr.items(), key=lambda kv: kv[1], reverse=True)
    a("## 15. Best categories")
    for k, v in best[:5]:
        a(f"- {k}: reliability={v}")
    a("")
    a("## 16. Worst categories")
    for k, v in sorted(cr.items(), key=lambda kv: kv[1])[:5]:
        a(f"- {k}: reliability={v}")
    a("")
    a("## 17. Baseline comparison")
    for b in status.get("baselines", []):
        a(f"- {b.get('baseline_name')}: trades={b.get('trade_count')} "
          f"scored={b.get('scored_trades')} pnl={b.get('pnl')} win_rate={b.get('win_rate')}")
    a("")
    a("## 18. Recommendation")
    a(f"- **{status.get('recommendation')}**\n")
    a("---")
    a("_PAPER ONLY. No live trading, no real orders, Micro Live disabled, "
      "production execution disabled, arbitrage disabled._")
    return "\n".join(L) + "\n"
