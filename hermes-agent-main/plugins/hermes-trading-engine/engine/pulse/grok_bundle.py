"""Helpers for the Grok decision bundle (v1.3 extras). Pure functions — unit-testable."""

from __future__ import annotations

from typing import Optional


def gate_funnel_top(rejected_by_stage: dict, *, top_n: int = 8) -> dict:
    """Summarize where candidate trades get blocked (highest counts first)."""
    rbs = {str(k): int(v or 0) for k, v in (rejected_by_stage or {}).items() if int(v or 0) > 0}
    ranked = sorted(rbs.items(), key=lambda x: (-x[1], x[0]))[: max(1, int(top_n))]
    return {
        "total_rejected": sum(rbs.values()),
        "top_blockers": [{"stage": stage, "count": count} for stage, count in ranked],
    }


def tv_trend_snapshot(
    *,
    mtf: Optional[dict],
    latest_by_timeframe: dict,
    feature_symbol: str = "BTCUSDT",
) -> dict:
    """All four TV chart alerts (1m/5m/10m/15m) with strength + 4-TF trend verdict."""
    mtf = mtf or {}
    feat = str(feature_symbol or "BTCUSDT").strip() or "BTCUSDT"
    dir_keys = {"1": "tf_1m_dir", "5": "tf_5m_dir", "10": "tf_10m_dir", "15": "tf_15m_dir"}
    age_keys = {"1": "tf_1m_age_s", "5": "tf_5m_age_s", "10": "tf_10m_age_s", "15": "tf_15m_age_s"}
    charts = {}
    for tf, label in (("1", "1m"), ("5", "5m"), ("10", "10m"), ("15", "15m")):
        snap = latest_by_timeframe.get("%s@%s" % (feat, tf)) or {}
        fresh_dir = mtf.get(dir_keys[tf])
        stored_dir = snap.get("direction")
        charts[label] = {
            "direction": fresh_dir or stored_dir,
            "strength": snap.get("strength"),
            "fresh": fresh_dir is not None,
            "age_s": mtf.get(age_keys[tf]),
            "stale_stored_dir": (stored_dir if fresh_dir is None and stored_dir else None),
        }
    return {
        "confirm_4tf": mtf.get("confirm_4tf"),
        "direction_4tf": mtf.get("direction_4tf"),
        "fresh_tf_count": mtf.get("trend_fresh_count"),
        "trend_by_tf": mtf.get("trend_by_tf"),
        "charts": charts,
    }