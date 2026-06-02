"""Load + validate the latest shadow readiness report for guarded-live prechecks."""

from __future__ import annotations

import time
from typing import Optional


def load_latest_readiness(store, report_id: Optional[str] = None) -> Optional[dict]:
    if store is None:
        return None
    if report_id:
        return store.get_readiness_report(report_id)
    reports = store.get_readiness_reports(None, 1)
    return reports[0] if reports else None


def validate_readiness(store, config, *, report_id: Optional[str] = None,
                       now_ms: Optional[int] = None) -> tuple[bool, str, Optional[str]]:
    """Returns (ok, reason, readiness_report_id)."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    rep = load_latest_readiness(store, report_id)
    if rep is None:
        return False, "no_shadow_readiness_report", None
    rid = rep.get("report_id")
    status = rep.get("overall_status")
    if status != config.required_shadow_status:
        return False, f"shadow_status_{status}_not_{config.required_shadow_status}", rid
    gen = rep.get("generated_ts_ms") or 0
    age_h = (now - int(gen)) / 3_600_000.0 if gen else 1e9
    if age_h > config.max_shadow_report_age_hours:
        return False, f"shadow_report_too_old_{round(age_h, 1)}h", rid
    return True, "ok", rid
