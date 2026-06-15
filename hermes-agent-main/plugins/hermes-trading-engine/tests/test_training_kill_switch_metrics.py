"""Kill-switch metrics + automatic downgrade to conservative paper mode.

Quant scope — *Risk Management* + *Compliance/Security/Operational Excellence*:
proves the kill-switch fires on calibration deterioration, excessive drawdown,
bad labels, stale data, high partial-fill rate, Bregman false positives, spread
blowout, and feedback corruption — and that aggressive mode auto-downgrades to
conservative paper mode (never touching any live control). PAPER ONLY.
"""

from __future__ import annotations

import time

import pytest

from engine.campaigns.signal_models import SignalResult
from engine.training.config import TrainingConfig
from engine.training.monitoring import (KILL_SWITCH_ALERTS, KillSwitchThresholds,
                                        evaluate_kill_switch)
from engine.training.polymarket_trainer import PaperPosition, PolymarketPaperTrainer


def _healthy(**kw):
    d = dict(calibration_error=0.05, brier_trend=0.0, drawdown=-1.0, loss_streak=0,
             label_suppression_rate=0.05, ambiguous_rate=0.02,
             stale_data_rejection_rate=0.05, partial_fill_rate=0.05,
             bregman_false_positive_rate=0.0, avg_spread=0.02, learner_rollbacks=0,
             samples=50)  # enough feedback samples for statistical alerts to apply
    d.update(kw)
    return d


def test_healthy_dashboard_does_not_trigger():
    ks = evaluate_kill_switch(_healthy(), KillSwitchThresholds(), aggressive=True)
    assert ks["triggered"] == []
    assert ks["should_downgrade"] is False
    assert ks["severity"] == "OK"


@pytest.mark.parametrize("field,value,alert", [
    ("calibration_error", 0.40, "calibration_deterioration"),
    ("brier_trend", 0.20, "calibration_deterioration"),
    ("drawdown", -500.0, "excessive_drawdown"),
    ("loss_streak", 50, "excessive_drawdown"),
    ("label_suppression_rate", 0.9, "bad_labels"),
    ("ambiguous_rate", 0.9, "bad_labels"),
    ("stale_data_rejection_rate", 0.9, "stale_data"),
    ("partial_fill_rate", 0.9, "high_partial_fill_rate"),
    ("bregman_false_positive_rate", 0.8, "bregman_false_positives"),
    ("avg_spread", 0.5, "spread_blowout"),
    ("learner_rollbacks", 99, "feedback_corruption"),
])
def test_each_degraded_condition_triggers_its_alert(field, value, alert):
    ks = evaluate_kill_switch(_healthy(**{field: value}), KillSwitchThresholds(),
                             aggressive=True)
    assert alert in ks["triggered"]
    assert ks["severity"] == "CRITICAL"
    assert alert in KILL_SWITCH_ALERTS
    # MARKET-DATA-quality alerts (stale_data / spread_blowout) surface + alert but do NOT
    # auto-downgrade paper learning (the realism gates already reject those books, no risk).
    # Bot-RISK alerts still force the downgrade in aggressive mode.
    from engine.training.monitoring import MARKET_QUALITY_ALERTS
    if alert in MARKET_QUALITY_ALERTS:
        assert ks["should_downgrade"] is False                         # paper-only default
        ks_live = evaluate_kill_switch(_healthy(**{field: value}), KillSwitchThresholds(),
                                       aggressive=True, paper_only=False)
        assert ks_live["should_downgrade"] is True                     # would downgrade if not paper
    else:
        assert ks["should_downgrade"] is True


def test_alerts_reported_but_no_downgrade_when_not_aggressive():
    ks = evaluate_kill_switch(_healthy(drawdown=-999.0), KillSwitchThresholds(),
                             aggressive=False)
    assert "excessive_drawdown" in ks["triggered"]
    assert ks["should_downgrade"] is False  # conservative mode has nothing to downgrade


# --------------------------------------------------------------------------- #
# trainer auto-downgrade under degraded conditions
# --------------------------------------------------------------------------- #
class _Demo:
    name = "research"

    def evaluate(self, rec):
        return SignalResult(0.82, 0.9, "grok_cache", "e")

    def status(self):
        return {"name": "research", "research_mode": "offline_cache"}


def _losing_position(i, pnl):
    return PaperPosition(
        proposal_id=f"p{i}", risk_decision_id=f"r{i}", order_id=f"o{i}", fill_id=f"f{i}",
        market_id=f"m{i}", asset_id=f"a{i}", group_key=f"g{i}", category="crypto",
        outcome="YES", entry_price=0.5, qty=10.0, p_final=0.6, net_edge=0.05,
        ambiguity=0.0, evidence=0.8, spread=0.02, liquidity=20000.0, open_tick=0,
        yes_price_entry=0.5, executable_price_entry=0.5, p_market_entry=0.5,
        strategy="directional", strategy_variant="directional_edge",
        mark=0.5, closed=True, exit_price=0.2, realized_pnl=pnl, close_reason="stop_loss")


def test_aggressive_auto_downgrades_to_conservative_on_kill_switch(tmp_path):
    cfg = TrainingConfig.aggressive_paper(chainlink_enabled=False, ks_max_loss_streak=2)
    t = PolymarketPaperTrainer(cfg, data_dir=tmp_path, signal_model=_Demo())
    assert t._profile() == "aggressive"
    assert t.cfg.exploration_enabled is True
    # inject a losing streak that exceeds the kill-switch threshold
    for i in range(4):
        t.positions.append(_losing_position(i, -3.0))
    ks = t.run_monitoring(now=time.time())
    assert ks["should_downgrade"] is True
    assert "excessive_drawdown" in ks["triggered"]
    # aggressive features are turned OFF; profile is now conservative (PAPER still)
    assert t._downgraded is True
    assert t._profile() == "conservative"
    assert t.cfg.exploration_enabled is False
    assert t.cfg.experiments_enabled is False
    assert t.cfg.is_paper_only is True            # never touches live controls
    assert t.preflight()["live_detected"] is False


def test_kill_switch_does_not_downgrade_when_healthy(tmp_path):
    cfg = TrainingConfig.aggressive_paper(chainlink_enabled=False)
    t = PolymarketPaperTrainer(cfg, data_dir=tmp_path, signal_model=_Demo())
    ks = t.run_monitoring(now=time.time())
    assert ks["should_downgrade"] is False
    assert t._downgraded is False
    assert t._profile() == "aggressive"
