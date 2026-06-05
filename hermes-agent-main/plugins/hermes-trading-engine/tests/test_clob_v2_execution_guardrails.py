"""Tests for the paper-only CLOB v2 multi-leg execution planner."""

from __future__ import annotations

from engine.execution.clob_v2 import (
    ClobV2Config,
    ClobV2ExecutionPlanner,
    ExecLeg,
)
from engine.simulation.fill_model import BookLevel, LatencyModel, OrderBook, ReplayFeeModel


def _book(ts=1000, asks=((0.40, 100),)):
    return OrderBook(ts_ms=ts, asks=[BookLevel(p, s) for p, s in asks],
                     bids=[BookLevel(0.39, 100)])


def _legs(da=100, db=100, ts_a=1000, ts_b=1000):
    return [ExecLeg(id="a", book=_book(ts_a, ((0.40, da),)), side="buy", size=10),
            ExecLeg(id="b", book=_book(ts_b, ((0.40, db),)), side="buy", size=10)]


def _planner(**kw):
    cfg = ClobV2Config(fee_model=ReplayFeeModel(taker_fee_bps=0),
                       latency=LatencyModel(latency_ms=100, max_book_age_ms=5000), **kw)
    return ClobV2ExecutionPlanner(cfg)


def test_depth_snapshot_and_ordering():
    p = _planner()
    legs = _legs(da=5, db=100)
    snap = p.snapshot_depth(legs)
    assert snap == {"a": 5.0, "b": 100.0}
    order = [l.id for l in p.order_legs(legs)]
    assert order == ["a", "b"]  # thinnest (riskiest) first


def test_single_leg_executable():
    p = _planner()
    legs = [ExecLeg(id="solo", book=_book(asks=((0.40, 100),)), side="buy", size=10)]
    plan = p.plan(legs, decision_ts_ms=1000, sets=10, worst_case_payoff_per_set=1.0)
    assert plan.executable is True
    assert plan.atomic_risk_free is True
    assert plan.reason == "executable_atomic_risk_free"


def test_multileg_certified_logged_not_executable_on_non_atomic_venue():
    p = _planner(venue_supports_atomic_multileg=False)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10, certified=True)
    # both legs fill, but a non-atomic venue cannot guarantee risk-free multi-leg
    assert all(l.status == "filled" for l in plan.legs)
    assert plan.atomic_risk_free is False
    assert plan.executable is False
    assert plan.reason == "atomicity_risk_multi_leg_non_atomic_venue"
    assert plan.certified is True   # still logged as certified


def test_multileg_executable_when_venue_atomic():
    p = _planner(venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10)
    assert plan.executable is True
    assert plan.atomic_risk_free is True


def test_fok_kills_plan_on_thin_leg():
    p = _planner(mode="FOK", venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(db=3), decision_ts_ms=1000, sets=10)
    assert plan.executable is False
    assert "unfillable_leg" in plan.reason
    assert plan.atomic_risk_free is False


def test_stale_leg_rejected():
    p = _planner(venue_supports_atomic_multileg=True,
                 )
    # b is stale: decision 10_000 + 100 latency - ts 0 = 10_100 > 5000 budget
    plan = p.plan(_legs(ts_b=0), decision_ts_ms=10_000, sets=10)
    assert plan.executable is False
    assert any(l.status == "rejected" for l in plan.legs)


def test_timeout_rejects_all_legs():
    cfg = ClobV2Config(timeout_ms=100,
                       latency=LatencyModel(latency_ms=500, max_book_age_ms=10_000),
                       venue_supports_atomic_multileg=True)
    plan = ClobV2ExecutionPlanner(cfg).plan(_legs(), decision_ts_ms=1000, sets=10)
    assert plan.executable is False
    assert plan.reason == "timeout"
    assert all(l.status == "timeout" for l in plan.legs)


def test_worst_case_slippage_cap_blocks():
    # leg b walks into a far worse level -> high slippage
    legs = [ExecLeg(id="a", book=_book(asks=((0.40, 100),)), side="buy", size=10),
            ExecLeg(id="b", book=_book(asks=((0.40, 1), (0.80, 100))), side="buy", size=10)]
    p = _planner(venue_supports_atomic_multileg=True, max_worst_case_slippage_frac=0.02)
    plan = p.plan(legs, decision_ts_ms=1000, sets=10)
    assert plan.worst_case_slippage_frac > 0.02
    assert plan.executable is False
    assert "worst_case_slippage" in plan.reason


def test_reconciliation_and_attribution_present():
    p = _planner(venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10, worst_case_payoff_per_set=1.0)
    assert plan.reconciliation["all_matched"] is True
    assert plan.reconciliation["legs"]["a"]["filled"] == 10
    assert "total_cost" in plan.attribution
    # payoff 10 - cost (10*0.4 + 10*0.4 = 8) - fees(0) = 2
    assert abs(plan.after_cost_edge - 2.0) < 1e-6


def test_uncertified_not_executable():
    p = _planner(venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10, certified=False)
    assert plan.executable is False
    assert plan.reason == "not_certified_no_execution"


def test_plan_serializes():
    p = _planner()
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10)
    d = plan.to_dict()
    assert isinstance(d["legs"], list) and "executable" in d


# --- certificate-status mapping ---------------------------------------------
def test_plan_certificate_status_executable_on_atomic_venue():
    from engine.arbitrage.certificate import CertificateStatus
    p = _planner(venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10, worst_case_payoff_per_set=1.0)
    assert plan.certificate_status == CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED
    assert plan.required_capital > 0
    assert plan.fantasy_fills_rejected == 0


def test_plan_certificate_status_theoretical_on_non_atomic_venue():
    from engine.arbitrage.certificate import CertificateStatus
    p = _planner(venue_supports_atomic_multileg=False)
    plan = p.plan(_legs(), decision_ts_ms=1000, sets=10)
    assert plan.certificate_status == CertificateStatus.CERTIFIED_THEORETICAL_NOT_EXECUTABLE
    assert plan.executable is False


def test_plan_counts_fantasy_fills_on_thin_leg():
    p = _planner(mode="IOC", venue_supports_atomic_multileg=True)
    plan = p.plan(_legs(db=3), decision_ts_ms=1000, sets=10)
    assert plan.fantasy_fills_rejected >= 1
    assert plan.executable is False
