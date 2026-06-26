"""WS4: dependency-arb validator, paper execute, and segregated ledger."""

from __future__ import annotations

from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.dependency_arb import (
    DependencyArbLedger,
    DependencyViolation,
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


def test_execute_disabled_does_not_book():
    ledger = DependencyArbLedger(execute_enabled=False)
    trade = {"parent_window_key": "x", "close_ts": 1.0, "expected_profit_usd": 1.0}
    assert ledger.book(trade, now=0.0) is False
    assert ledger.executed == 0