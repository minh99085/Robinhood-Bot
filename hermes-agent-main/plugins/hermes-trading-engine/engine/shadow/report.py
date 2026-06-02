"""Shadow report writer (Phase 7). Emits JSON/CSV/MD artifacts. The Markdown
report ALWAYS contains an explicit 'No live orders were submitted' statement.

Quant scope — *Live Trading & Monitoring* + *Compliance/Security/Operational
Excellence*: read-only shadow reporting; the no-live-orders attestation is
preserved."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .artifacts import write_csv, write_json
from .config import ShadowConfig
from .schemas import LiveReadinessReport

NO_LIVE_STATEMENT = (
    "No live orders were submitted. Phase 7 shadow mode adds NO real order "
    "submission, NO real cancellation, NO live broker adapter, NO Polymarket "
    "wallet/private-key signing, NO Kalshi order endpoints, and NO private "
    "user-channel subscriptions. All fills are simulated by the PaperBroker.")


def _rows(store, table: str, session_id: str) -> list[dict]:
    try:
        return store.get_shadow_rows(table, session_id)
    except Exception:  # noqa: BLE001
        return []


def _markdown(session_id: str, config: ShadowConfig, report: LiveReadinessReport,
              metrics: dict) -> str:
    lines = [
        f"# Shadow Live-Readiness Report — `{session_id}`",
        "",
        f"**{NO_LIVE_STATEMENT}**",
        "",
        f"- mode: `shadow_live`",
        f"- venues: {', '.join(config.venues)}",
        f"- overall status: **{report.overall_status}**",
        f"- recommended next step: `{report.recommended_next_step}`",
        "",
        "## Summary metrics",
    ]
    for k in ("decision_count", "approved_shadow_order_count", "shadow_order_count",
              "shadow_fill_count", "fill_ratio", "risk_rejection_rate", "reject_rate",
              "total_fees"):
        lines.append(f"- {k}: {metrics.get(k)}")
    lines += ["", "## Readiness gates", "", "| gate | status | observed | threshold |",
              "|---|---|---|---|"]
    for g in report.gate_results:
        lines.append(f"| {g.gate_name} | {g.status} | {g.observed_value} | {g.threshold} |")
    fails = [g.gate_name for g in report.gate_results if g.status == "FAIL"]
    lines += ["", "## Hard FAIL gates", ""]
    lines.append(", ".join(fails) if fails else "_none_")
    lines += [
        "", "## Known limitations",
        "- Shadow fills are still simulated; queue priority and market impact are approximate.",
        "- Hidden liquidity is unknown; short runs do not prove profitability.",
        "- Unresolved markets limit calibration.",
        "- Live trading requires a separate guarded-live adapter design and manual review.",
        "",
        "## Recommended next step",
        f"`{report.recommended_next_step}` — this is NOT an instruction to enable live "
        "trading. Live enablement is never automatic.",
    ]
    return "\n".join(lines)


def write_report(store, session_id: str, config: ShadowConfig,
                 report: LiveReadinessReport, metrics: dict,
                 base_dir: Optional[str] = None) -> Path:
    out = Path(base_dir or config.output_dir) / session_id
    out.mkdir(parents=True, exist_ok=True)

    write_json(out / "shadow_config.json", config.public_dict())
    write_json(out / "shadow_summary.json", {
        "shadow_session_id": session_id, "mode": "shadow_live", "venues": config.venues,
        "overall_status": report.overall_status, "no_live_orders": True})
    write_json(out / "shadow_metrics.json", metrics)
    write_json(out / "shadow_readiness_report.json", report.model_dump(mode="json"))
    (out / "shadow_readiness_report.md").write_text(
        _markdown(session_id, config, report, metrics), encoding="utf-8")

    write_csv(out / "candidates.csv", _rows(store, "shadow_candidates", session_id))
    write_csv(out / "decisions.csv", _rows(store, "shadow_decisions", session_id))
    write_csv(out / "orders.csv", _rows(store, "shadow_orders", session_id))
    write_csv(out / "fills.csv", _rows(store, "shadow_fills", session_id))
    write_csv(out / "positions.csv", _rows(store, "shadow_positions", session_id))
    write_csv(out / "equity_curve.csv", _rows(store, "shadow_equity", session_id))
    write_csv(out / "observations.csv", _rows(store, "shadow_observations", session_id))
    write_csv(out / "alerts.csv", _rows(store, "shadow_alerts", session_id))

    rej = metrics.get("rejection_reasons") or {}
    write_csv(out / "rejection_reasons.csv",
              [{"reason": k, "count": v} for k, v in rej.items()])
    write_csv(out / "readiness_gates.csv", [{
        "gate_name": g.gate_name, "status": g.status, "observed_value": g.observed_value,
        "threshold": g.threshold, "reason": g.reason} for g in report.gate_results])

    if store is not None:
        try:
            store.add_readiness_report({
                "report_id": report.report_id, "shadow_session_id": session_id,
                "generated_ts_ms": report.generated_ts_ms,
                "overall_status": report.overall_status,
                "summary_json": report.metrics_summary, "report_path": str(out)})
            for g in report.gate_results:
                store.add_readiness_gate_result({
                    "report_id": report.report_id, "shadow_session_id": session_id,
                    "gate_name": g.gate_name, "status": g.status,
                    "score": None if g.score is None else str(g.score),
                    "threshold": None if g.threshold is None else str(g.threshold),
                    "observed_value": None if g.observed_value is None else str(g.observed_value),
                    "reason": g.reason, "details_json": g.details})
        except Exception:  # noqa: BLE001
            pass
    return out
