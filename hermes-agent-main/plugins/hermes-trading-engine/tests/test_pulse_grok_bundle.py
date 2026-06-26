"""Grok decision bundle v1.3 helpers — per-market stats, gate funnel, 4-TF TV trend."""

from __future__ import annotations

from engine.pulse.grok_bundle import gate_funnel_top, tv_trend_snapshot


def test_gate_funnel_top_sorted():
    funnel = gate_funnel_top({
        "context_gate": 100,
        "down_bias_gate": 200,
        "grok_decider": 5,
        "execution_gate": 50,
    }, top_n=3)
    assert funnel["total_rejected"] == 355
    assert funnel["top_blockers"][0] == {"stage": "down_bias_gate", "count": 200}
    assert funnel["top_blockers"][1]["stage"] == "context_gate"


def test_tv_trend_snapshot_all_four_charts():
    mtf = {
        "tf_1m_dir": "DOWN",
        "tf_5m_dir": "UP",
        "tf_10m_dir": "UP",
        "tf_15m_dir": "UP",
        "tf_1m_age_s": 45.0,
        "tf_5m_age_s": 120.0,
        "tf_10m_age_s": 200.0,
        "tf_15m_age_s": 300.0,
        "confirm_4tf": "partial_up_4tf",
        "direction_4tf": "UP",
        "trend_fresh_count": 4,
        "trend_by_tf": {"1": "DOWN", "5": "UP", "10": "UP", "15": "UP"},
    }
    by_tf = {
        "BTCUSD@1": {"direction": "DOWN", "strength": 0.61},
        "BTCUSD@5": {"direction": "UP", "strength": 0.75},
        "BTCUSD@10": {"direction": "UP", "strength": 0.79},
        "BTCUSD@15": {"direction": "UP", "strength": 0.82},
    }
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf, feature_symbol="BTCUSD")
    assert snap["confirm_4tf"] == "partial_up_4tf"
    assert snap["direction_4tf"] == "UP"
    assert snap["charts"]["10m"]["direction"] == "UP"
    assert snap["charts"]["10m"]["strength"] == 0.79
    assert snap["charts"]["10m"]["fresh"] is True
    assert snap["charts"]["1m"]["age_s"] == 45.0


def test_tv_trend_stale_fallback():
    mtf = {"tf_5m_dir": None, "tf_10m_dir": "UP", "tf_10m_age_s": 90.0,
           "confirm_4tf": "single_tf", "direction_4tf": "UP", "trend_fresh_count": 1}
    by_tf = {"BTCUSD@5": {"direction": "DOWN", "strength": 0.55}}
    snap = tv_trend_snapshot(mtf=mtf, latest_by_timeframe=by_tf)
    assert snap["charts"]["5m"]["direction"] == "DOWN"
    assert snap["charts"]["5m"]["fresh"] is False
    assert snap["charts"]["5m"]["stale_stored_dir"] == "DOWN"