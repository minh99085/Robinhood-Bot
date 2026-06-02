"""Replay metrics: drawdown, fill ratio, fee drag, empty-curve safety."""

from __future__ import annotations

from types import SimpleNamespace

from engine.replay import metrics as met


def test_metrics_handles_empty_equity_curve():
    out = met.summarize(config=SimpleNamespace(initial_cash=10000.0), equity_rows=[],
                        orders=[], fills=[], proposals=[], risk_decisions=[], positions=[])
    assert "insufficient_equity_samples" in out["warnings"]
    assert out["ending_equity"] == 10000.0
    assert out["sharpe"] == 0.0  # no crash, divide-by-zero safe


def test_metrics_calculates_drawdown():
    equities = [100.0, 120.0, 90.0, 110.0, 80.0]
    abs_dd, pct_dd = met.max_drawdown(equities)
    assert abs_dd == 40.0  # peak 120 -> trough 80
    assert round(pct_dd, 6) == round(40.0 / 120.0, 6)


def test_metrics_calculates_fill_ratio():
    orders = [{"client_order_id": "a"}, {"client_order_id": "b"}]
    fills = [{"client_order_id": "a"}]
    assert met.fill_ratio(orders, fills) == 0.5


def test_metrics_calculates_fee_drag():
    fills = [{"fee": "1.5"}, {"fee": "2.5"}, {"fee": "1.0"}]
    assert met.fee_total(fills) == 5.0
    out = met.summarize(config=SimpleNamespace(initial_cash=1000.0),
                        equity_rows=[{"equity": 1000.0}, {"equity": 995.0}],
                        orders=[{"client_order_id": "a", "status": "FILLED"}],
                        fills=fills, proposals=[], risk_decisions=[], positions=[])
    assert out["total_fees"] == 5.0
    assert out["fee_drag_pct"] == round(5.0 / 1000.0, 6)
