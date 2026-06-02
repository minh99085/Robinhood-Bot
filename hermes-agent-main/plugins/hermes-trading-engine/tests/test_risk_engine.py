"""Deterministic RiskEngine gate tests."""

from __future__ import annotations

from pathlib import Path

from engine.risk import RiskCode, RiskContext, RiskEngine, RiskLimits
from engine.schemas import TradeProposal


def _proposal(**kw) -> TradeProposal:
    base = dict(strategy="t", market="crypto", symbol="BTCUSDT", side="BUY",
                notional=10.0, price=100.0, edge_after_costs=0.05, spread=0.0,
                data_age_s=0.0, ambiguity_score=0.0)
    base.update(kw)
    return TradeProposal(**base)


def _ctx(**kw) -> RiskContext:
    base = dict(equity=100_000.0, total_exposure=0.0, market_exposure=0.0,
                has_open_same_market_side=False, open_orders=0, day_pnl=0.0)
    base.update(kw)
    return RiskContext(**base)


def test_baseline_proposal_is_approved():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_proposal(), _ctx())
    assert d.approved is True
    assert d.code == RiskCode.OK


def test_risk_engine_rejects_oversize_order():
    eng = RiskEngine(RiskLimits(max_order_notional_abs=100.0))
    d = eng.evaluate(_proposal(notional=500.0), _ctx())
    assert d.approved is False
    assert d.code == RiskCode.OVERSIZE_ORDER

    # Fractional cap also bites even without an absolute cap.
    eng2 = RiskEngine(RiskLimits(max_order_notional_frac=0.001))  # 0.1% of equity = $100
    d2 = eng2.evaluate(_proposal(notional=500.0), _ctx())
    assert d2.approved is False
    assert d2.code == RiskCode.OVERSIZE_ORDER


def test_risk_engine_rejects_stale_data():
    eng = RiskEngine(RiskLimits(max_data_age_s=5.0))
    d = eng.evaluate(_proposal(data_age_s=120.0), _ctx())
    assert d.approved is False
    assert d.code == RiskCode.STALE_DATA


def test_risk_engine_rejects_low_edge():
    eng = RiskEngine(RiskLimits(min_edge_after_costs=0.05))
    d = eng.evaluate(_proposal(edge_after_costs=0.0), _ctx())
    assert d.approved is False
    assert d.code == RiskCode.LOW_EDGE


def test_kill_switch_blocks_orders(tmp_path: Path):
    ks = tmp_path / "KILL_SWITCH"
    eng = RiskEngine(RiskLimits(kill_switch_file=ks))
    # Absent -> approved.
    assert eng.evaluate(_proposal(), _ctx()).approved is True
    # Present -> every order blocked, even an otherwise-perfect one.
    ks.write_text("halt")
    d = eng.evaluate(_proposal(), _ctx())
    assert d.approved is False
    assert d.code == RiskCode.KILL_SWITCH


def test_rejects_duplicate_market_side_unless_allowed():
    eng = RiskEngine(RiskLimits())
    d = eng.evaluate(_proposal(), _ctx(has_open_same_market_side=True))
    assert d.approved is False and d.code == RiskCode.DUPLICATE_EXPOSURE
    d2 = eng.evaluate(_proposal(allow_duplicate=True), _ctx(has_open_same_market_side=True))
    assert d2.approved is True


def test_rejects_total_and_market_exposure_and_daily_loss():
    eng = RiskEngine(RiskLimits())
    # Total exposure cap (default 0.60 of 100k = 60k).
    d = eng.evaluate(_proposal(notional=5_000.0), _ctx(total_exposure=58_000.0))
    assert d.approved is False and d.code == RiskCode.TOTAL_EXPOSURE
    # Daily loss cap (default 0.10 of 100k = 10k).
    d2 = eng.evaluate(_proposal(), _ctx(day_pnl=-12_000.0))
    assert d2.approved is False and d2.code == RiskCode.DAILY_LOSS


def test_limits_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HTE_RISK_MAX_ORDER_NOTIONAL_FRAC", "0.05")
    monkeypatch.setenv("HTE_RISK_MAX_DATA_AGE_S", "3")
    lim = RiskLimits.from_env(tmp_path)
    assert lim.max_order_notional_frac == 0.05
    assert lim.max_data_age_s == 3.0
    assert lim.kill_switch_file == tmp_path / "KILL_SWITCH"
