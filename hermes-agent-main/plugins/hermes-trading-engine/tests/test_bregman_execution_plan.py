"""Tests for Bregman execution planning + certificate atomicity audit."""

from __future__ import annotations

from engine.arbitrage.certificate import atomicity_risk, certify_group
from engine.arbitrage.constraint_graph import ConstraintGraph, Outcome
from engine.simulation.fill_model import BookLevel, OrderBook, ReplayFeeModel
from engine.execution.clob_v2 import ClobV2Config, ClobV2ExecutionPlanner
from engine.strategies.bregman import BregmanStrategy


def _complement_graph(depth=100):
    g = ConstraintGraph()
    g.add_outcome(Outcome(id="a", price=0.40, ask=0.40, ask_depth=depth))
    g.add_outcome(Outcome(id="b", price=0.40, ask=0.40, ask_depth=depth))
    g.add_complement("a", "b")
    return g


def _books(ts=1000, da=100, db=100):
    return {"a": OrderBook(ts_ms=ts, asks=[BookLevel(0.40, da)], bids=[BookLevel(0.39, 100)]),
            "b": OrderBook(ts_ms=ts, asks=[BookLevel(0.40, db)], bids=[BookLevel(0.39, 100)])}


def _certified_opportunity():
    g = _complement_graph()
    strat = BregmanStrategy(fee_model=None)
    res = strat.evaluate(g, now=0.0)
    opp = next(o for o in res.opportunities if o.certificate.certified)
    return strat, opp


# --- certificate atomicity audit --------------------------------------------
def test_atomicity_risk_flags_multi_leg_non_atomic():
    g = _complement_graph()
    cert = certify_group(g, g.constraints()[0])
    risk = atomicity_risk(cert, venue_supports_atomic_multileg=False)
    assert risk["multi_leg"] is True
    assert risk["atomic_risk_free_guaranteed"] is False
    assert risk["reason"] == "multi_leg_non_atomic_venue"


def test_atomicity_ok_when_venue_atomic():
    g = _complement_graph()
    cert = certify_group(g, g.constraints()[0])
    risk = atomicity_risk(cert, venue_supports_atomic_multileg=True)
    assert risk["atomic_risk_free_guaranteed"] is True


# --- bregman plan_execution -------------------------------------------------
def test_certified_multileg_logged_not_executable_by_default():
    strat, opp = _certified_opportunity()
    plan = strat.plan_execution(opp, _books(), decision_ts_ms=1000, sets=10)
    assert plan.certified is True
    assert plan.executable is False  # non-atomic venue => logged only
    assert plan.reason == "atomicity_risk_multi_leg_non_atomic_venue"


def test_certified_multileg_executable_on_atomic_venue():
    strat, opp = _certified_opportunity()
    planner = ClobV2ExecutionPlanner(ClobV2Config(
        venue_supports_atomic_multileg=True, fee_model=ReplayFeeModel(taker_fee_bps=0)))
    plan = strat.plan_execution(opp, _books(), decision_ts_ms=1000, sets=10, planner=planner)
    assert plan.executable is True
    assert plan.atomic_risk_free is True


def test_missing_book_leg_rejected():
    strat, opp = _certified_opportunity()
    planner = ClobV2ExecutionPlanner(ClobV2Config(venue_supports_atomic_multileg=True))
    plan = strat.plan_execution(opp, {"a": _books()["a"]}, decision_ts_ms=1000,
                                sets=10, planner=planner)
    assert plan.executable is False
    assert any(l.status == "rejected" for l in plan.legs)


def test_thin_book_breaks_execution():
    strat, opp = _certified_opportunity()
    planner = ClobV2ExecutionPlanner(ClobV2Config(venue_supports_atomic_multileg=True))
    plan = strat.plan_execution(opp, _books(db=2), decision_ts_ms=1000, sets=50,
                                planner=planner)
    assert plan.executable is False


def test_opportunity_certificate_status_gates_execution():
    from engine.arbitrage.certificate import CertificateStatus
    strat, opp = _certified_opportunity()
    # the certificate itself (default non-atomic venue) is theoretical-not-executable
    assert opp.certificate.status == CertificateStatus.CERTIFIED_THEORETICAL_NOT_EXECUTABLE
    assert opp.tradeable is True          # theoretical proof
    assert opp.executable is False        # but NOT after-cost executable


def test_executable_plan_reports_certificate_status():
    from engine.arbitrage.certificate import CertificateStatus
    strat, opp = _certified_opportunity()
    planner = ClobV2ExecutionPlanner(ClobV2Config(
        venue_supports_atomic_multileg=True, fee_model=ReplayFeeModel(taker_fee_bps=0)))
    plan = strat.plan_execution(opp, _books(), decision_ts_ms=1000, sets=10, planner=planner)
    assert plan.certificate_status == CertificateStatus.EXECUTABLE_AFTER_COST_CERTIFIED
