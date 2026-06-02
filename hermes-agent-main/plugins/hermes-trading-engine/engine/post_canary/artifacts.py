"""Artifact writers (Phase 10). All content is redacted before writing."""

from __future__ import annotations

import csv
import json
from pathlib import Path

try:
    from ..micro_live.secret_runtime import redact, redact_dict
except Exception:  # noqa: BLE001
    def redact(t):  # type: ignore
        return t

    def redact_dict(d):  # type: ignore
        return d


def write_json(path: Path, obj) -> None:
    safe = redact_dict(obj) if isinstance(obj, dict) else obj
    path.write_text(json.dumps(safe, indent=2, default=str))


def write_csv(path: Path, header: list, rows: list) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([redact(str(x)) if x is not None else "" for x in r])


def redacted_trace(ctx: dict) -> dict:
    plan = ctx.get("plan") or {}
    a = ctx.get("attempt") or {}
    return redact_dict({
        "shadow_session_id": plan.get("source_shadow_session_id"),
        "shadow_decision_id": plan.get("source_shadow_decision_id"),
        "readiness_report_id": plan.get("readiness_report_id"),
        "approval_batch_id": plan.get("approval_batch_id"),
        "arming_token_id": "[REDACTED]" if plan.get("arming_token_id") else None,
        "dry_run_intent_id": plan.get("source_dry_run_intent_id"),
        "canary_plan_id": plan.get("canary_plan_id") or a.get("canary_plan_id"),
        "risk_decision_id": a.get("risk_decision_id"),
        "safety_envelope_decision_id": a.get("safety_envelope_decision_id"),
        "live_order_attempt_id": a.get("live_order_attempt_id"),
        "client_order_id": a.get("client_order_id"),
        "exchange_order_id": a.get("exchange_order_id"),
    })
