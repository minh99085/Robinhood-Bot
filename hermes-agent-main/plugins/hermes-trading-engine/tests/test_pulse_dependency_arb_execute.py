"""WS4: dependency-arb validator, paper execute, and segregated ledger."""

from __future__ import annotations

from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.dependency_arb import (
    DependencyArbLedger,
    DependencyViolation,
    enrich_vwap_actionable,
    realized_dependency_profit_usd,
    validate_violation,
    try_execute_nested_implication,
    scan_nested_implication,
)


def _book(ask=0.45):
    return OrderBook(
        best_bid=ask - 0.02, best_ask=ask,
        asks=[(ask, 10_000.0)], bids=[(ask - 0.02, 10_000.0)],
        ask_depth_usd=ask * 10_000, bid_depth_usd=(ask - 0.02) * 10_000,
    )


def test_validate_rejects_invalid_llm_proposal():
    v = DependencyViolation(
        constraint_type="nested_implication",
        parent_window_key="p", child_window_keys=["c"],
        description="bogus", parent_up_mid=0.55, child_up_mids=[0.50],
        violation_magnitude=0.0,
    )
    ok, reason = validate_violation(v)
    assert ok is False and reason == "no_magnitude"
    v2 = DependencyViolation(
        constraint_type="grok_guess",
        parent_window_key="p", child_window_keys=["c"],
        description="llm", violation_magnitude=0.05,
    )
    ok2, reason2 = validate_violation(v2)
    assert ok2 is False and reason2 == "unsupported_constraint"


def test_paper_execute_and_settle_dependency_ledger():
    t0 = 10_000_000.0
    parent = PulseWindow(
        event_id="p15", market_id="mp", slug="sp", title="15m",
        open_ts=t0, close_ts=t0 + 900, up_token_id="UP", down_token_id="DP",
        window_seconds=900, series_label="15m",
    )
    child = PulseWindow(
        event_id="c5", market_id="mc", slug="sc", title="5m",
        open_ts=t0 + 60, close_ts=t0 + 360, up_token_id="UC", down_token_id="DC",
        window_seconds=300, series_label="5m",
    )
    parent.up_book = _book(0.42)
    parent.down_book = _book(0.55)
    child.up_book = _book(0.57)
    child.down_book = _book(0.40)
    vios = scan_nested_implication(parent, [child], epsilon=0.02)
    assert len(vios) >= 1
    v = vios[0]
    assert validate_violation(v)[0] is True
    trade = try_execute_nested_implication(parent, child, v, max_usd=25.0, epsilon=0.02)
    assert trade is not None and trade["expected_profit_usd"] > 0
    ledger = DependencyArbLedger(execute_enabled=True)
    assert ledger.book(trade, now=t0 + 100) is True
    assert ledger.executed == 1 and ledger.has_open("p15")
    n = ledger.settle_due(t0 + 901)
    assert n == 1
    assert ledger.settled == 1
    assert ledger.realized_profit_usd > 0
    rep = ledger.report()
    assert rep["segregated_from_directional"] is True
    assert rep["strategy"] == "dependency_arbitrage"


def test_vwap_enrichment_rejection_reason():
    t0 = 10_000_000.0
    parent = PulseWindow(
        event_id="p15", market_id="mp", slug="sp", title="15m",
        open_ts=t0, close_ts=t0 + 900, up_token_id="UP", down_token_id="DP",
        window_seconds=900, series_label="15m",
    )
    child = PulseWindow(
        event_id="c5", market_id="mc", slug="sc", title="5m",
        open_ts=t0 + 60, close_ts=t0 + 360, up_token_id="UC", down_token_id="DC",
        window_seconds=300, series_label="5m",
    )
    parent.up_book = OrderBook(best_bid=0.40, best_ask=0.42, asks=[(0.42, 1.0)],
                               bids=[(0.40, 1000.0)])
    parent.down_book = _book(0.55)
    child.up_book = _book(0.57)
    child.down_book = _book(0.40)
    vios = scan_nested_implication(parent, [child], epsilon=0.02, vwap_enrich=True)
    assert len(vios) >= 1
    v = enrich_vwap_actionable(vios[0], parent, child, max_usd=25.0, epsilon=0.02)
    assert v.actionable is False
    assert v.reason in ("partial_fill", "vwap_not_executable", "below_epsilon", "zero_shares")
    ledger = DependencyArbLedger(execute_enabled=False)
    ledger.record_scan(vios)
    assert ledger.rejected_by_reason


def test_vwap_enrichment_marks_actionable():
    t0 = 10_000_000.0
    parent = PulseWindow(
        event_id="p15", market_id="mp", slug="sp", title="15m",
        open_ts=t0, close_ts=t0 + 900, up_token_id="UP", down_token_id="DP",
        window_seconds=900, series_label="15m",
    )
    child = PulseWindow(
        event_id="c5", market_id="mc", slug="sc", title="5m",
        open_ts=t0 + 60, close_ts=t0 + 360, up_token_id="UC", down_token_id="DC",
        window_seconds=300, series_label="5m",
    )
    parent.up_book = _book(0.42)
    child.up_book = _book(0.57)
    vios = scan_nested_implication(parent, [child], epsilon=0.02, vwap_enrich=True)
    assert vios and vios[0].actionable is True
    assert vios[0].reason == "vwap_executable"


def test_realized_profit_capped_below_theoretical_on_low_entry():
    trade = {
        "shares": 5000.0,
        "entry_vwap": 0.01,
        "cost_usd": 50.0,
        "violation_magnitude": 0.47,
        "implied_bound": 0.48,
        "capture_frac": 0.5,
        "expected_profit_usd": 1175.0,
    }
    booked = realized_dependency_profit_usd(trade)
    assert booked < trade["expected_profit_usd"]
    assert booked == round(50.0 * 0.47 * 0.5, 6)


def test_execute_disabled_does_not_book():
    ledger = DependencyArbLedger(execute_enabled=False)
    trade = {"parent_window_key": "x", "close_ts": 1.0, "expected_profit_usd": 1.0}
    assert ledger.book(trade, now=0.0) is False
    assert ledger.executed == 0