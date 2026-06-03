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

    # concise monitoring + kill-switch JSON artifacts (Live Monitoring)
    (run_dir / "monitoring.json").write_text(
        json.dumps(status.get("monitoring", {}), default=str, indent=2), encoding="utf-8")
    (run_dir / "kill_switch.json").write_text(
        json.dumps(status.get("kill_switch", {}), default=str, indent=2), encoding="utf-8")
    # live-readiness verdict + capital-preservation plan (PAPER ONLY — verdict only)
    (run_dir / "live_readiness.json").write_text(
        json.dumps(status.get("live_readiness", {}), default=str, indent=2), encoding="utf-8")
    # BTC 5-min Pulse PAPER-ONLY isolated experiment artifact (when present)
    btc_pulse = status.get("btc_pulse") or {}
    if btc_pulse.get("btc_pulse_enabled"):
        (run_dir / "btc_pulse.json").write_text(
            json.dumps(btc_pulse, default=str, indent=2), encoding="utf-8")
    # institutional paper-training campaign artifacts (PAPER ONLY) — when present
    campaign = status.get("training_campaign") or {}
    if campaign and campaign.get("enabled") is not False:
        (run_dir / "training_campaign.json").write_text(
            json.dumps(campaign, default=str, indent=2), encoding="utf-8")
        try:
            from .campaign_controller import campaign_markdown
            (run_dir / "training_campaign.md").write_text(
                campaign_markdown(campaign), encoding="utf-8")
        except Exception:  # noqa: BLE001 — campaign report must never break the run report
            pass

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

    # controlled strategy-variant experiment attribution (champion/challenger)
    from .metrics import variant_attribution_table
    exp_rows = variant_attribution_table(status.get("experiments", {}))
    _write_csv(run_dir / "experiments.csv", exp_rows,
               ["strategy_variant", "role", "trade_count", "feedback_count",
                "sharpe", "sortino", "calmar", "max_drawdown", "brier", "log_loss",
                "ece", "realized_edge", "fill_quality"])

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
    bp = status.get("btc_pulse") or {}
    a("## 17e. BTC 5-min Pulse (PAPER ONLY, isolated experiment)")
    if bp.get("btc_pulse_enabled"):
        a(f"- frozen: {bp.get('btc_pulse_frozen')} · paper_only: {bp.get('paper_only')} "
          f"· isolated_learning: {bp.get('isolated_learning')} · live_enabled: "
          f"{bp.get('live_enabled')} · legacy_autotrade: {bp.get('legacy_autotrade_enabled')}")
        a(f"- ticks: {bp.get('btc_pulse_ticks')} · rounds: {bp.get('btc_pulse_rounds_seen')} "
          f"· decisions: {bp.get('btc_pulse_decisions')} · paper_trades: "
          f"{bp.get('btc_pulse_paper_trades')} · no_trades: "
          f"{bp.get('btc_pulse_no_trade_decisions')} · rejected: {bp.get('btc_pulse_rejected_trades')}")
        a(f"- win_rate: {bp.get('btc_pulse_win_rate')} · sharpe: {bp.get('btc_pulse_sharpe')} "
          f"· sortino: {bp.get('btc_pulse_sortino')} · calmar: {bp.get('btc_pulse_calmar')} "
          f"· max_dd: {bp.get('btc_pulse_max_drawdown')}")
        a(f"- brier: {bp.get('btc_pulse_brier')} · log_loss: {bp.get('btc_pulse_log_loss')} "
          f"· ece: {bp.get('btc_pulse_ece')} · after_cost_pnl: {bp.get('btc_pulse_after_cost_pnl')}")
        a(f"- rejection_reasons: {bp.get('btc_pulse_rejection_reasons')} · "
          f"transfer_gate: {bp.get('btc_pulse_transfer_gate_status')} · "
          f"blockers: {bp.get('btc_pulse_blockers')}")
    else:
        a("- BTC Pulse OFF (isolated paper experiment; never trades on its own)")
    a("")
    fa = status.get("feedback_accelerator") or {}
    a("## 17f. 10x Feedback Accelerator (PAPER ONLY)")
    if fa.get("feedback_accelerator_enabled"):
        cap = fa.get("capacity", {})
        sg = fa.get("soft_gates", {})
        a(f"- target x{fa.get('target_multiplier')} · mode: {fa.get('mode')} · "
          f"exploration: {fa.get('exploration_enabled')} (tiny={fa.get('exploration_tiny_size_enabled')})")
        a(f"- capacity: decisions/tick={cap.get('paper_decision_budget')} "
          f"candidates={cap.get('trade_candidate_limit')} shortlist={cap.get('shortlist_limit')}")
        a(f"- exploit gates UNCHANGED (edge>={sg.get('exploit_min_edge')}, "
          f"conf>={sg.get('exploit_min_confidence')}); exploration gates (tiny only): "
          f"edge>={sg.get('exploration_min_edge')}, conf>={sg.get('exploration_min_confidence')}")
        a(f"- shadow_decisions={fa.get('shadow_decision_logging_enabled')} · "
          f"no_trade_labels={fa.get('no_trade_labeling_enabled')} · "
          f"counts_for_readiness={fa.get('exploration_counts_for_readiness')}")
        a("- HARD gates locked: no live, RiskEngine required, fresh book + valid "
          "token + realistic fill required; exploration is NOT readiness proof "
          "until cleanly resolved + validated.")
    else:
        a("- Feedback Accelerator OFF (conservative default; turn on with "
          "--feedback-accelerator). PAPER ONLY.")
    a("")
    exp = status.get("experiments", {}) or {}
    if exp.get("enabled"):
        from .metrics import variant_attribution_table
        cc = exp.get("champion_challenger", {}) or {}
        a("## 17b. Strategy-variant experiments (PAPER ONLY)")
        a(f"- experiment_id: `{exp.get('experiment_id')}` · champion: "
          f"**{cc.get('champion')}** · challengers: {', '.join(cc.get('challengers', [])) or 'none'}")
        a("- per-variant (trades · feedback · sharpe · brier · ece · realized_edge · fill_q):")
        for r in variant_attribution_table(exp):
            a(f"  - {r['strategy_variant']} [{r['role']}]: {r['trade_count']} · "
              f"{r['feedback_count']} · {r['sharpe']} · {r['brier']} · {r['ece']} · "
              f"{r['realized_edge']} · {r['fill_quality']}")
        a("")
    mon = status.get("monitoring", {}) or {}
    ks = status.get("kill_switch", {}) or {}
    if mon:
        from .monitoring import kill_switch_markdown
        for ln in kill_switch_markdown(mon, ks):
            a(ln)
        if status.get("downgraded"):
            a(f"- **AUTO-DOWNGRADED to conservative paper mode** (kill-switch: "
              f"{', '.join(ks.get('triggered', []))})")
        a("")
    prof = status.get("profitability", {}) or {}
    if prof:
        truth = prof.get("truth", {}) or {}
        a("## 17c. Profitability truth (gross edge vs after-cost reality)")
        a(f"- net expectancy: {prof.get('net_expectancy')} · profit_factor: "
          f"{prof.get('profit_factor')} · edge_survival: {prof.get('edge_survival')}")
        if truth:
            a(f"- gross={truth.get('gross_edge')} − fees={truth.get('fees')} "
              f"spread={truth.get('spread')} slippage={truth.get('slippage')} "
              f"fill_fail={truth.get('fill_failure')} adverse={truth.get('adverse_selection')} "
              f"ambiguity={truth.get('label_ambiguity')} timing={truth.get('timing_decay')} "
              f"-> NET={truth.get('net_edge')}")
        a(f"- rejected bad markets: {prof.get('rejected_bad_markets', 0)} "
          f"(graylist {prof.get('rejected_graylisted', 0)} / blacklist "
          f"{prof.get('rejected_blacklisted', 0)})")
        a("")
    lr = status.get("live_readiness", {}) or {}
    if lr:
        from .live_readiness import readiness_markdown
        for ln in readiness_markdown(lr.get("verdict", {}), lr.get("capital_preservation", {})):
            a(ln)
        a("")
    cap = status.get("capital_allocation", {}) or {}
    if cap:
        gov = cap.get("drawdown_governor", {}) or {}
        a("## 17b. Adaptive capital allocation (PAPER ONLY)")
        a(f"- total_allocated: {cap.get('total_allocated')} · expected_return: "
          f"{cap.get('expected_return')} · capital_efficiency: {cap.get('capital_efficiency')}")
        a(f"- CVaR/expected_shortfall: {cap.get('cvar')} · max_drawdown: "
          f"{cap.get('max_drawdown')} · concentration: {cap.get('concentration')}")
        a(f"- feedback_per_risk_unit: {cap.get('feedback_per_risk_unit')}")
        for b, v in (cap.get("bucket_allocations") or {}).items():
            a(f"  - {b}: {v}")
        a(f"- drawdown_governor: action={gov.get('action')} "
          f"size_multiplier={gov.get('size_multiplier')} reasons={gov.get('reasons')}")
        rej = cap.get("rejected_sizing_reasons") or {}
        if rej:
            a(f"- rejected_sizing_reasons: {rej}")
        a("")
    canary = status.get("canary", {}) or {}
    if canary:
        a("## 17c. Micro-live canary (DISABLED by default)")
        a(f"- enabled: {canary.get('enabled')} · dry_run: {canary.get('dry_run')} · "
          f"manual_enable: {canary.get('manual_enable')} · rolled_back: "
          f"{canary.get('rolled_back')}")
        a(f"- require_certificate: {canary.get('require_certificate')} · "
          f"allowed_strategies: {canary.get('allowed_strategies')}")
        caps = canary.get("caps", {}) or {}
        if caps:
            a(f"- caps: {caps}")
        cmp = canary.get("fill_vs_paper", {}) or {}
        if cmp:
            a(f"- live-vs-paper: fills={cmp.get('fills_compared')} "
              f"within_tolerance_rate={cmp.get('within_tolerance_rate')} "
              f"mean_slippage_forecast_error_bps={cmp.get('mean_slippage_forecast_error_bps')}")
        lr = canary.get("last_rollback")
        if lr:
            a(f"- last_rollback: target={lr.get('target_mode')} reasons={lr.get('reasons')}")
        a("")
    camp = status.get("institutional_campaign", {}) or {}
    if camp:
        a("## 17d. Institutional validation campaign (PAPER ONLY)")
        a(f"- decision: **{'READY' if camp.get('overall_ready') else 'NOT READY'}** · "
          f"readiness_state: {camp.get('readiness_state')} · certificate: "
          f"{'ISSUED' if camp.get('certificate') else 'NOT ISSUED'}")
        crit = camp.get("criteria", {}) or {}
        for name, cr in crit.items():
            a(f"  - {'PASS' if cr.get('passed') else 'FAIL'} {name}")
        if camp.get("blockers"):
            a(f"- blockers: {camp.get('blockers')}")
        a("")
    a("## 18. Recommendation")
    a(f"- **{status.get('recommendation')}**\n")
    a("---")
    a("_PAPER ONLY. No live trading, no real orders, Micro Live disabled, "
      "production execution disabled, arbitrage disabled._")
    return "\n".join(L) + "\n"
