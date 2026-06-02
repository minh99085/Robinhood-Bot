"""Replay report + artifact bundle generation.

Quant scope — *Backtesting & Simulation* + *Compliance/Security/Operational
Excellence*: the deterministic, offline replay artifact bundle. It always
asserts "No live orders were submitted" and surfaces calibration + Bregman +
Chainlink replay analytics so a reviewer can validate the upgraded system
end-to-end from fixtures alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import artifacts


def write_report(runner, output_dir: str | Path) -> Path:
    base = Path(output_dir) / runner.run_id
    base.mkdir(parents=True, exist_ok=True)
    metrics = getattr(runner, "metrics", {}) or {}
    calib = getattr(runner, "_calibration", {}) or {}

    artifacts.write_json(base / "config.json", json.loads(runner.config.model_dump_json()))
    artifacts.write_json(base / "metrics.json", metrics)
    summary = {
        "replay_run_id": runner.run_id, "status": runner.status, "error": runner.error,
        "config_hash": runner.config_hash, "seed": runner.seed,
        "episode": runner.episode().record(), "policy": runner.policy.name,
        "event_count": len(runner.events),
        "ending_equity": metrics.get("ending_equity"), "total_pnl": metrics.get("total_pnl"),
        "max_drawdown": metrics.get("max_drawdown"), "sharpe": metrics.get("sharpe"),
        "fill_ratio": metrics.get("fill_ratio"), "total_fees": metrics.get("total_fees"),
        "calibration": {k: calib.get(k) for k in
                        ("brier_score", "log_loss", "expected_calibration_error",
                         "resolved_count", "unresolved_count")},
        "no_live_orders": True,
    }
    artifacts.write_json(base / "summary.json", summary)

    overfit = {}
    try:
        overfit = runner._overfit_report()
    except Exception:  # noqa: BLE001
        overfit = {}
    if overfit:
        artifacts.write_json(base / "overfit_report.json", overfit)

    artifacts.write_csv(base / "equity_curve.csv", runner.equity_rows)
    artifacts.write_csv(base / "orders.csv", runner.orders)
    artifacts.write_csv(base / "fills.csv", runner.fills)
    artifacts.write_csv(base / "positions.csv", runner.positions)
    artifacts.write_csv(base / "proposals.csv", runner.proposals)
    artifacts.write_csv(base / "risk_decisions.csv", runner.risk_decisions)
    artifacts.write_csv(base / "calibration.csv", calib.get("calibration_by_probability_bucket", []))
    rej = metrics.get("rejection_reasons", {}) or {}
    artifacts.write_csv(base / "rejection_reasons.csv",
                        [{"reason": k, "count": v} for k, v in rej.items()])
    pnlm = metrics.get("pnl_by_market", {}) or {}
    artifacts.write_csv(base / "pnl_by_market.csv",
                        [{"market_id": k, "pnl": v} for k, v in pnlm.items()])

    charts = artifacts.maybe_charts(base, runner.equity_rows, calib)
    (base / "replay_report.md").write_text(
        _markdown(runner, summary, metrics, calib, charts, overfit), encoding="utf-8")
    return base


def _markdown(runner, summary, metrics, calib, charts, overfit=None) -> str:
    ep = summary["episode"]
    pnlm = metrics.get("pnl_by_market", {}) or {}
    winners = sorted(pnlm.items(), key=lambda kv: kv[1], reverse=True)[:5]
    losers = sorted(pnlm.items(), key=lambda kv: kv[1])[:5]
    lines = [
        f"# Replay report — {runner.run_id}",
        "",
        "> Offline/simulated replay. **No live orders were submitted.**",
        "",
        f"- Status: **{runner.status}**" + (f" (error: {runner.error})" if runner.error else ""),
        f"- Policy: `{runner.policy.name}`  ·  Seed: `{runner.seed}`  ·  Config hash: `{runner.config_hash[:16]}`",
        f"- Source: `{ep.get('source')}`  ·  Events: {ep.get('event_count')}  "
        f"·  Range: {ep.get('start_ts_ms')} → {ep.get('end_ts_ms')}",
        "",
        "## Performance (after fees / slippage / rejects)",
        f"- Starting cash: {metrics.get('starting_cash')}",
        f"- Ending equity: {metrics.get('ending_equity')}",
        f"- Total PnL: {metrics.get('total_pnl')}  ({metrics.get('total_return')} return)",
        f"- Max drawdown: {metrics.get('max_drawdown')} ({metrics.get('max_drawdown_pct')} pct)",
        f"- Sharpe: {metrics.get('sharpe')}  ·  Sortino: {metrics.get('sortino')}",
        f"- Fill ratio: {metrics.get('fill_ratio')}  ·  Partial-fill ratio: {metrics.get('partial_fill_ratio')}",
        f"- Total fees: {metrics.get('total_fees')}  ·  Fee drag: {metrics.get('fee_drag_pct')}",
        f"- Orders: {metrics.get('order_count')}  ·  Fills: {metrics.get('fill_count')}  "
        f"·  Rejects: {metrics.get('reject_count')}",
        "",
        "## Risk",
        f"- Approval rate: {metrics.get('risk_approval_rate')}  ·  Rejection rate: {metrics.get('risk_rejection_rate')}",
        f"- Rejection reasons: {json.dumps(metrics.get('rejection_reasons', {}))}",
        "",
        "## Calibration",
        f"- Resolved: {calib.get('resolved_count')}  ·  Unresolved (excluded): {calib.get('unresolved_count')}",
        f"- Brier: {calib.get('brier_score')}  ·  Log loss: {calib.get('log_loss')}  "
        f"·  ECE: {calib.get('expected_calibration_error')}",
        "",
        *(_overfit_lines(overfit) if overfit else []),
        "## Top markets",
        "Winners: " + ", ".join(f"{k} ({v})" for k, v in winners) if winners else "Winners: none",
        "Losers: " + ", ".join(f"{k} ({v})" for k, v in losers) if losers else "Losers: none",
        "",
        "## Warnings / limitations",
        f"- {', '.join(metrics.get('warnings', [])) or 'none'}",
        "- Queue priority is approximated; hidden/iceberg liquidity not modeled; replay quality depends on raw-event quality; unresolved markets are excluded from realized calibration.",
    ]
    prof = metrics.get("profitability_truth") or {}
    uplift = metrics.get("market_quality_uplift") or {}
    if prof or uplift:
        lines += ["", "## Profitability truth (after-cost net edge)",
                  f"- gross={prof.get('gross_edge')} total_cost={prof.get('total_cost')} "
                  f"net={prof.get('net_edge')} edge_survival={prof.get('edge_survival')}",
                  f"- market-quality uplift (selected vs all): {uplift.get('uplift')} "
                  f"(rejected {uplift.get('rejected_count')})"]
    lr = metrics.get("live_readiness") or {}
    if lr:
        v = lr.get("verdict", lr) or {}
        cap = lr.get("capital_preservation", {}) or {}
        lines += ["", "## Live-readiness gate (PAPER ONLY — verdict never enables live)",
                  f"- state: **{v.get('state')}**  ·  live-escalation allowed: "
                  f"{v.get('allows_live_escalation')}  ·  hard blockers: "
                  f"{', '.join(v.get('blockers', [])) or 'none'}",
                  f"- max initial live notional: {cap.get('max_initial_live_notional')}  ·  "
                  f"max daily loss: {cap.get('max_daily_loss')}"]
    ks = metrics.get("kill_switch") or {}
    mon = metrics.get("monitoring") or {}
    if ks or mon:
        lines += ["", "## Aggressive kill-switch (PAPER ONLY)",
                  f"- severity: **{ks.get('severity', 'OK')}**  ·  triggered: "
                  f"{', '.join(ks.get('triggered', [])) or 'none'}  ·  downgraded: "
                  f"{ks.get('downgraded', False)}"]
        if mon:
            lines.append(f"- paper trades/hr: {mon.get('paper_trades_per_hour')}  ·  "
                         f"useful feedback/hr: {mon.get('useful_feedback_per_hour')}  ·  "
                         f"loss streak: {mon.get('loss_streak')}")
    if charts:
        lines += ["", "## Charts", *[f"- {c}" for c in charts]]
    return "\n".join(lines) + "\n"


def _overfit_lines(overfit: dict) -> list:
    """In-sample vs out-of-sample anti-overfit section (Strategy Optimization)."""
    is_v = overfit.get("in_sample", {})
    oos_v = overfit.get("out_of_sample", {})
    keys = [k for k in ("sharpe", "brier", "log_loss", "ece", "max_drawdown",
                        "realized_edge") if k in is_v or k in oos_v]
    rows = [f"  - {k}: IS={is_v.get(k)}  OOS={oos_v.get(k)}  Δ={overfit.get('delta', {}).get(k)}"
            for k in keys]
    verdict = "OVERFIT ⚠" if overfit.get("overfit") else "OK"
    return ["## Overfitting (in-sample vs out-of-sample)",
            f"- Verdict: **{verdict}**  ·  score: {overfit.get('overfit_score')}",
            f"- Reasons: {', '.join(overfit.get('reasons', [])) or 'none'}",
            *rows, ""]
