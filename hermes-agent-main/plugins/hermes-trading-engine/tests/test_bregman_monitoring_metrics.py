"""Bregman arbitrage monitoring metrics + false-positive kill-switch.

Quant scope — *Bregman arbitrage monitoring* + *Live Monitoring*: proves the
monitoring layer surfaces Bregman opportunities, certified profit, and the
false-positive rate, and that a high Bregman false-positive rate trips the
kill-switch. PAPER ONLY.
"""

from __future__ import annotations

import pytest

from engine.training.monitoring import (KillSwitchThresholds, bregman_monitoring,
                                        evaluate_kill_switch)


def _summary(**kw):
    base = {"enabled": True, "execution_enabled": True, "opportunity_count": 5,
            "sets_opened": 3, "rejected": 1,
            "last_scan_metrics": {"opportunity_count": 2, "certified_profit": 1.4,
                                  "false_positive_rate": 0.0, "certified_count": 2}}
    base.update(kw)
    return base


def test_bregman_monitoring_extracts_core_fields():
    m = bregman_monitoring(_summary())
    assert m["opportunities"] == 5
    assert m["certified_profit"] == pytest.approx(1.4)
    assert m["false_positive_rate"] == pytest.approx(0.0)
    assert m["sets_opened"] == 3


def test_bregman_monitoring_handles_empty_summary():
    m = bregman_monitoring({})
    assert m["opportunities"] == 0
    assert m["certified_profit"] == 0.0
    assert m["false_positive_rate"] == 0.0


def test_high_bregman_false_positive_rate_trips_kill_switch():
    summary = _summary(last_scan_metrics={"opportunity_count": 4, "certified_profit": 0.1,
                                          "false_positive_rate": 0.5})
    m = bregman_monitoring(summary)
    assert m["false_positive_rate"] == pytest.approx(0.5)
    dash = {"calibration_error": 0.05, "brier_trend": 0.0, "drawdown": -1.0,
            "loss_streak": 0, "label_suppression_rate": 0.0, "ambiguous_rate": 0.0,
            "stale_data_rejection_rate": 0.0, "partial_fill_rate": 0.0,
            "bregman_false_positive_rate": m["false_positive_rate"], "avg_spread": 0.02,
            "learner_rollbacks": 0, "samples": 50}
    ks = evaluate_kill_switch(dash, KillSwitchThresholds(), aggressive=True)
    assert "bregman_false_positives" in ks["triggered"]
    assert ks["should_downgrade"] is True


def test_stale_data_alert_does_not_downgrade_paper_learning():
    """Market-quality alert (stale_data) must SURFACE but NOT auto-downgrade paper
    active-learning: the realism gates already reject stale books and no money is at
    risk. Reproduces the VPS blocker where 79% stale-rejection disabled active learning."""
    dash = {"calibration_error": 0.05, "brier_trend": 0.0, "drawdown": -1.0,
            "loss_streak": 0, "label_suppression_rate": 0.0, "ambiguous_rate": 0.0,
            "stale_data_rejection_rate": 0.79, "partial_fill_rate": 0.0,
            "bregman_false_positive_rate": 0.0, "avg_spread": 0.02,
            "learner_rollbacks": 0, "samples": 50}
    ks = evaluate_kill_switch(dash, KillSwitchThresholds(), aggressive=True)
    assert "stale_data" in ks["triggered"]            # still visible
    assert ks["severity"] == "CRITICAL"               # still alerted
    assert ks["should_downgrade"] is False            # but does NOT disable paper learning
    # a genuine bot-RISK alert (drawdown) DOES still downgrade
    dash2 = dict(dash, drawdown=-999.0)
    ks2 = evaluate_kill_switch(dash2, KillSwitchThresholds(), aggressive=True)
    assert "excessive_drawdown" in ks2["triggered"] and ks2["should_downgrade"] is True


def test_clean_bregman_does_not_trip_kill_switch():
    m = bregman_monitoring(_summary())
    dash = {"calibration_error": 0.05, "brier_trend": 0.0, "drawdown": -1.0,
            "loss_streak": 0, "label_suppression_rate": 0.0, "ambiguous_rate": 0.0,
            "stale_data_rejection_rate": 0.0, "partial_fill_rate": 0.0,
            "bregman_false_positive_rate": m["false_positive_rate"], "avg_spread": 0.02,
            "learner_rollbacks": 0}
    ks = evaluate_kill_switch(dash, KillSwitchThresholds(), aggressive=True)
    assert "bregman_false_positives" not in ks["triggered"]
