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
    (base / "replay_report.md").write_text(_markdown(runner, summary, metrics, calib, charts),
                                           encoding="utf-8")
    return base


def _markdown(runner, summary, metrics, calib, charts) -> str:
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
        "## Top markets",
        "Winners: " + ", ".join(f"{k} ({v})" for k, v in winners) if winners else "Winners: none",
        "Losers: " + ", ".join(f"{k} ({v})" for k, v in losers) if losers else "Losers: none",
        "",
        "## Warnings / limitations",
        f"- {', '.join(metrics.get('warnings', [])) or 'none'}",
        "- Queue priority is approximated; hidden/iceberg liquidity not modeled; replay quality depends on raw-event quality; unresolved markets are excluded from realized calibration.",
    ]
    if charts:
        lines += ["", "## Charts", *[f"- {c}" for c in charts]]
    return "\n".join(lines) + "\n"
