"""Multi-leg Bregman bundle execution realism.

A certified "buy the complete set" hedge is only risk-free if EVERY leg fills.
The bundle simulator sequences legs with realistic fills, timeout + cancel, and
reports failure modes. A hedge that breaks under partial fills must NOT be
treated as guaranteed profit. PAPER ONLY; deterministic.
"""

from __future__ import annotations

from engine.training.bregman_execution import (BregmanArbitrageEngine,
                                                BregmanBundleExecutionSimulator)
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg


def _leg(mid, outcome, ask, *, depth=5000.0, fresh=True, bid=None):
    return SimplexLeg(market_id=mid, outcome=outcome, token_id=f"{mid}:{outcome}",
                      ask=ask, bid=bid if bid is not None else ask - 0.01,
                      depth_usd=depth, fresh_book=fresh, stale=not fresh)


def _exhaustive_group(gid="g1", asks=(0.45, 0.45), depths=(5000.0, 5000.0)):
    legs = [_leg(f"{gid}_{i}", f"O{i}", a, depth=d) for i, (a, d) in enumerate(zip(asks, depths))]
    return SimplexGroup(group_id=gid, group_type="exhaustive_event", legs=legs,
                        mutually_exclusive=True, exhaustive=True, payout=1.0)


def _certify(group, **kw):
    eng = BregmanArbitrageEngine(min_depth_usd=50.0, max_spread=0.2,
                                 target_capital_usd=100.0, slippage_bps=0.0,
                                 taker_fee_bps=0.0, **kw)
    return eng, eng.certify(group)


def test_full_bundle_fills_realizes_certified_profit():
    group = _exhaustive_group(asks=(0.45, 0.45))           # cost 0.90 < 1 -> arb
    eng, opp = _certify(group)
    assert opp.is_opportunity and opp.profit_lower_bound > 0
    sim = BregmanBundleExecutionSimulator()
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 1.0])  # both legs fully fill
    assert res.fully_hedged is True
    assert res.hedge_complete is True
    assert res.failure_mode == ""
    assert res.realized_pnl > 0
    assert abs(res.realized_pnl - opp.profit_lower_bound) < 1e-6


def test_partial_fill_breaks_hedge_is_not_risk_free():
    group = _exhaustive_group(asks=(0.45, 0.45))
    eng, opp = _certify(group)
    assert opp.is_opportunity
    sim = BregmanBundleExecutionSimulator(cancel_on_leg_failure=True)
    # second leg only half fills -> the complete-set hedge is broken
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 0.5])
    assert res.fully_hedged is False
    assert res.hedge_complete is False
    assert res.failure_mode == "partial_fill_breaks_hedge"
    # realized PnL is NOT the certified profit (the hedge did not complete)
    assert res.realized_pnl < opp.profit_lower_bound


def test_leg_timeout_cancels_bundle():
    group = _exhaustive_group(asks=(0.45, 0.45))
    eng, opp = _certify(group)
    sim = BregmanBundleExecutionSimulator(timeout_ms=10)
    # leg latencies exceed the timeout before all legs complete
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 1.0], leg_latencies_ms=[8, 50])
    assert res.timed_out is True
    assert res.fully_hedged is False
    assert res.failure_mode in ("timeout", "partial_fill_breaks_hedge")


def test_certify_with_fill_risk_refuses_risk_free_when_hedge_can_break():
    # A thin leg gives a low all-leg fill probability; with a fill-risk model the
    # opportunity must NOT be certified risk-free. FAIL if it still claims risk_free.
    group = _exhaustive_group(asks=(0.45, 0.45), depths=(5000.0, 60.0))
    eng = BregmanArbitrageEngine(min_depth_usd=50.0, max_spread=0.2,
                                 target_capital_usd=5000.0, slippage_bps=0.0,
                                 taker_fee_bps=0.0)
    fm = BregmanBundleExecutionSimulator()  # carries the fill model
    opp = eng.certify(group, fill_model=fm.fill_model, min_all_leg_fill_prob=0.95)
    assert opp.risk_free is False
    assert "partial_fill_breaks_hedge" in opp.failure_modes


def test_bundle_failure_modes_reported():
    group = _exhaustive_group(asks=(0.45, 0.45))
    eng, opp = _certify(group)
    sim = BregmanBundleExecutionSimulator()
    res = sim.simulate(opp, leg_fill_fractions=[1.0, 0.0])  # second leg does not fill
    d = res.to_dict()
    for k in ("group_id", "fully_hedged", "hedge_complete", "failure_mode",
              "filled_legs", "total_legs", "partial_fill_rate", "realized_pnl",
              "leg_results"):
        assert k in d
    assert d["filled_legs"] < d["total_legs"]
