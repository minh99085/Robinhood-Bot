"""Helpers for the Grok decision bundle (v1.4). Pure functions — unit-testable."""

from __future__ import annotations

import json
from typing import Optional

from engine.pulse.tradingview import (
    DEFAULT_MTF_TIMEFRAMES,
    tf_age_key,
    tf_dir_key,
    tf_label,
)

# Fields emitted first so a hard char-cap truncates history, not live edge context.
_BUNDLE_PRIORITY_KEYS = (
    "schema_version",
    "grok_task",
    "market",
    "series_label",
    "series_slug",
    "window_seconds",
    "decision_id",
    "timing",
    "tradingview_trend",
    "tradingview_signal",
    "tv_signal_learning",
    "cex_lead_mispricing",
    "polymarket",
    "price",
    "payoff",
    "digital_fair_p_up",
    "edge_signal",
    "grok_per_signal_p_up",
    "research",
    "news",
    "by_market_series",
    "gate_funnel",
    "model_vs_market",
    "edge_model_p_up",
    "decider_track_record",
    "bot_learned_evidence",
    "recent_windows",
    "trade_decision_history",
    "lessons",
    "active_markets",
    "cex_prices",
    "account_state",
    "note",
)


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
    feature_symbol: str = "BTCUSD",
) -> dict:
    """Configured TV chart alerts (default 2/3/4m) with direction, strength, signal_level."""
    mtf = mtf or {}
    feat = str(feature_symbol or "BTCUSD").strip() or "BTCUSD"
    tfs = tuple(mtf.get("mtf_timeframes") or DEFAULT_MTF_TIMEFRAMES)
    n = int(mtf.get("mtf_count") or len(tfs))
    charts = {}
    for tf in tfs:
        label = tf_label(tf)
        snap = latest_by_timeframe.get("%s@%s" % (feat, tf)) or {}
        fresh_dir = mtf.get(tf_dir_key(tf))
        stored_dir = snap.get("direction")
        charts[label] = {
            "timeframe": tf,
            "direction": fresh_dir or stored_dir,
            "signal_level": snap.get("signal_level"),
            "strength": snap.get("strength"),
            "fresh": fresh_dir is not None,
            "age_s": mtf.get(tf_age_key(tf)),
            "stale_stored_dir": (stored_dir if fresh_dir is None and stored_dir else None),
        }
    confirm_ntf = mtf.get("confirm_%dtf" % n) if n else None
    direction_ntf = mtf.get("direction_%dtf" % n) if n else None
    return {
        "mtf_timeframes": list(tfs),
        "mtf_count": n,
        "fast_pair": mtf.get("fast_pair"),
        "fast_pair_confirm": mtf.get("confirm"),
        "fast_pair_direction": mtf.get("direction"),
        "confirm_mtf": mtf.get("confirm_mtf"),
        "direction_mtf": mtf.get("direction_mtf"),
        "confirm_%dtf" % n: confirm_ntf,
        "direction_%dtf" % n: direction_ntf,
        "fresh_tf_count": mtf.get("trend_fresh_count"),
        "trend_by_tf": mtf.get("trend_by_tf"),
        "charts": charts,
    }


def compact_tv_learning(signal_learning: Optional[dict]) -> dict:
    """Tiny TV learning slice for Grok — best/worst levels + top buckets only."""
    sl = signal_learning or {}
    return {
        "settled_with_signal": sl.get("settled_with_signal"),
        "best_signal_levels": (sl.get("best_signal_levels") or [])[:3],
        "worst_signal_levels": (sl.get("worst_signal_levels") or [])[:3],
        "best_buckets": (sl.get("best_buckets") or [])[:4],
        "worst_buckets": (sl.get("worst_buckets") or [])[:4],
        "by_signal_level": {
            k: v for k, v in list((sl.get("by_signal_level") or {}).items())[:6]
        },
    }


def grok_task_for_window(*, series_label: str, window_seconds: int, ttc_s: Optional[float]) -> dict:
    """Series-specific instructions so Grok calibrates horizon + entry band."""
    ws = int(window_seconds or 300)
    label = str(series_label or ("15m" if ws >= 900 else "5m"))
    ttc = float(ttc_s) if ttc_s is not None else None
    if ws >= 900:
        # Baseline 15m fast-lane band (scaled): ~480–660s to close.
        in_entry_band = ttc is not None and 480.0 <= ttc <= 660.0
        return {
            "horizon": "15m_chainlink_window",
            "primary_series": label,
            "entry_band_ttc_s": [480, 660],
            "in_entry_band": in_entry_band,
            "tv_role": ("MTF trend confirmation for 15m settlement; "
                        "re-check tradingview_trend when in_entry_band is true"),
            "decision_priority": [
                "1_cex_lead_mispricing",
                "2_tradingview_trend_confirm_mtf",
                "3_polymarket_payoff_vs_p_up",
                "4_decider_track_record_context",
            ],
        }
    return {
        "horizon": "5m_chainlink_window",
        "primary_series": label,
        "entry_band_ttc_s": None,
        "in_entry_band": True,
        "tv_role": "MTF trend + latest signal_level for short-horizon confirmation",
        "decision_priority": [
            "1_cex_lead_mispricing",
            "2_tradingview_trend_confirm_mtf",
            "3_polymarket_payoff_vs_p_up",
        ],
    }


def order_bundle_for_grok(bundle: dict) -> dict:
    """Reorder keys so truncation keeps live edge fields, not tail history."""
    out: dict = {}
    for key in _BUNDLE_PRIORITY_KEYS:
        if key in bundle:
            out[key] = bundle[key]
    for key, val in bundle.items():
        if key not in out:
            out[key] = val
    return out


def serialize_bundle_for_grok(bundle: dict, *, max_chars: int = 14000) -> str:
    """JSON serialize with priority ordering and a generous cap (was blind 12k slice)."""
    ordered = order_bundle_for_grok(bundle)
    cap = 3500 if str(bundle.get("grok_compute_tier") or "").lower() == "light" else max_chars
    raw = json.dumps(ordered, default=str, separators=(",", ":"))
    if len(raw) <= cap:
        return raw
    return raw[:cap]


def classify_grok_compute_tier(
    bundle: dict,
    *,
    refresh_token: Optional[str] = None,
    tiered_enabled: bool = True,
    full_divergence_min: float = 0.025,
    deep_divergence_min: float = 0.04,
) -> str:
    """light = p_up calibration only; full = v2 decision; deep = full + optional live search."""
    if not tiered_enabled:
        return "full"
    cex = bundle.get("cex_lead_mispricing") or {}
    try:
        div = abs(float(cex.get("divergence") or 0.0))
    except (TypeError, ValueError):
        div = 0.0
    tv_confirms = bool(cex.get("tv_confirms"))
    cex_confirmed = bool(cex.get("confirmed"))
    task = bundle.get("grok_task") or {}
    in_entry_band = bool(task.get("in_entry_band"))
    news = bundle.get("news") or {}
    event_high = str(news.get("event_risk") or "").lower() == "high"
    tv_trend = bundle.get("tradingview_trend") or {}
    confirm_mtf = str(tv_trend.get("confirm_mtf") or "")
    mtf_aligned = confirm_mtf.startswith("confirmed_")
    fresh_tf = int(tv_trend.get("fresh_tf_count") or 0)

    if in_entry_band and (div >= deep_divergence_min or (tv_confirms and mtf_aligned)):
        return "deep"
    if event_high and div >= full_divergence_min and (tv_confirms or mtf_aligned):
        return "deep"
    if refresh_token and ("entry15m" in str(refresh_token) or str(refresh_token).startswith("tv:")):
        if in_entry_band and div >= full_divergence_min:
            return "deep"
        return "full"
    if div >= full_divergence_min or tv_confirms or cex_confirmed or mtf_aligned or fresh_tf >= 2:
        return "full"
    return "light"


def compact_bundle_for_light_tier(bundle: dict) -> dict:
    """Strip history tails — light tier only needs live state for p_up calibration."""
    price = bundle.get("price") or {}
    poly = bundle.get("polymarket") or {}
    tv = bundle.get("tradingview_trend") or {}
    charts = {}
    for label, row in (tv.get("charts") or {}).items():
        if isinstance(row, dict):
            charts[label] = {k: row.get(k) for k in
                             ("direction", "signal_level", "strength", "fresh", "age_s")}
    return {
        "schema_version": bundle.get("schema_version"),
        "grok_compute_tier": "light",
        "grok_task": bundle.get("grok_task"),
        "decision_id": bundle.get("decision_id"),
        "series_label": bundle.get("series_label"),
        "window_seconds": bundle.get("window_seconds"),
        "timing": bundle.get("timing"),
        "price": {k: price.get(k) for k in ("btc_now", "btc_open", "move_from_open", "sigma_per_sec")},
        "digital_fair_p_up": bundle.get("digital_fair_p_up"),
        "polymarket": {k: poly.get(k) for k in ("yes_mid", "spread", "fair_minus_poly")},
        "cex_lead_mispricing": bundle.get("cex_lead_mispricing"),
        "tradingview_trend": {
            "confirm_mtf": tv.get("confirm_mtf"),
            "fresh_tf_count": tv.get("fresh_tf_count"),
            "charts": charts,
        },
    }