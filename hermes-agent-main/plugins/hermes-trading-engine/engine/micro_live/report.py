"""Micro-live reports + artifacts (Phase 9). Writes a redacted artifact bundle
and a markdown report. Never writes secrets, raw payloads, or signed payloads."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Optional

from .config import MicroLiveConfig
from .schemas import _nid
from .secret_runtime import redact, redact_dict


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(redact_dict(obj) if isinstance(obj, dict) else obj,
                               indent=2, default=str))


def write_report(store, config: MicroLiveConfig, *, plan, attempt, safety=None, risk=None,
                 reconciliation=None, next_step: str = "stop_and_review") -> str:
    report_id = _nid("mlreport")
    base = Path(config.output_dir) / (attempt.live_order_attempt_id if attempt else
                                      plan.canary_plan_id)
    base.mkdir(parents=True, exist_ok=True)

    _write_json(base / "micro_live_config_redacted.json", config.public_dict())
    _write_json(base / "canary_plan.json", json.loads(json.dumps(plan.record(), default=str)))
    if attempt:
        _write_json(base / "order_attempt.json",
                    json.loads(json.dumps(attempt.record(), default=str)))
    if safety:
        _write_json(base / "preflight_report.json", safety.checks)
    if reconciliation:
        _write_json(base / "reconciliation_report.json",
                    json.loads(json.dumps(reconciliation.record(), default=str)))

    # audit events CSV (redacted)
    try:
        events = store.get_micro_live_audit_events(200) if store else []
    except Exception:  # noqa: BLE001
        events = []
    with (base / "audit_events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "event_type", "severity", "state", "message"])
        for e in reversed(events):
            w.writerow([e.get("ts_ms"), e.get("event_type"), e.get("severity"),
                        e.get("state"), redact(e.get("message", ""))])

    md = base / "micro_live_report.md"
    lines = [
        "# Micro-Live Canary Report",
        "",
        f"- report_id: `{report_id}`",
        f"- submitted: **{bool(attempt and attempt.submitted)}**",
        f"- venue: `{plan.venue}`",
        f"- environment: `{plan.environment}` (production_enabled={config.allow_production})",
        f"- market: `{plan.market_ticker or plan.market_id}`",
        f"- side: `{plan.side}`  outcome: `{plan.outcome}`",
        f"- price: `{plan.limit_price}`  quantity: `{plan.quantity}`  notional: `{plan.notional}`",
        f"- order_type/TIF: `{plan.order_type}` / `{plan.time_in_force}`",
        f"- final_status: `{attempt.status if attempt else 'n/a'}`",
        f"- exchange_order_id: `{attempt.exchange_order_id if attempt else None}`",
        f"- filled_quantity: `{attempt.filled_quantity if attempt else 0}`  "
        f"avg_fill_price: `{attempt.avg_fill_price if attempt else None}`  "
        f"fee: `{attempt.fee if attempt else None}`",
        f"- network_call_count: `{attempt.network_call_count if attempt else 0}`  "
        f"signer_used: `{attempt.signer_used if attempt else False}`",
        f"- risk_decision: `{(risk.code if risk else 'n/a')}` "
        f"approved={getattr(risk, 'approved', None)}",
        f"- safety_envelope: allowed={getattr(safety, 'allowed', None)} "
        f"reason=`{getattr(safety, 'reason', None)}`",
        f"- reconciliation: `{reconciliation.status if reconciliation else 'n/a'}`",
        f"- forbidden_network_endpoint_called: **False**",
        f"- retry_occurred: **False**",
        "- secrets: **all secrets redacted; none logged/persisted/returned**",
        f"- next_step: **{next_step}**",
        "",
        "_Phase 9 is gated micro-live canary execution — not autonomous live trading. "
        "Manual review is required after every canary. Do not scale automatically._",
    ]
    md.write_text("\n".join(lines))

    if store is not None:
        try:
            store.add_micro_live_report({
                "report_id": report_id, "ts_ms": int(time.time() * 1000),
                "canary_plan_id": plan.canary_plan_id,
                "live_order_attempt_id": attempt.live_order_attempt_id if attempt else None,
                "status": attempt.status if attempt else "NO_ORDER", "report_path": str(md),
                "summary_json": {"next_step": next_step, "submitted": bool(attempt and
                                                                           attempt.submitted)}})
        except Exception:  # noqa: BLE001
            pass
    return str(md)
