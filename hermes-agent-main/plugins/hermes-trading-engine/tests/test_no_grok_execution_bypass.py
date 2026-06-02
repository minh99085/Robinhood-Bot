"""Grok stays research-only: it cannot bypass risk, sizing, or execution (TDD).

Quant scope: Compliance/Security/Operational Excellence. The resolver is an
advisory SELECTION layer — it carries no order size / notional / placement
surface, research can never override the deterministic edge gate or escalate a
signal above its earned priority, and the trade side comes from market
microstructure (the executable book), never from the research view alone.
"""

from __future__ import annotations

from pathlib import Path

import engine  # noqa: F401  (conftest puts plugin root on sys.path)
from engine.training.signal_resolver import SignalResolver

from tests.test_signal_priority_bregman_first import _bregman_opp, _edge, _est

PLUGIN_ROOT = Path(engine.__file__).resolve().parent.parent


def test_resolved_signal_has_no_order_or_size_surface():
    r = SignalResolver()
    d = r.resolve(est=_est(p_research=0.9, p_final=0.7), edge=_edge()).to_dict()
    forbidden = ("notional", "size", "order", "place", "submit", "arm", "qty", "approve")
    for key in d:
        assert not any(f in key.lower() for f in forbidden), key
    # advisory-only marker is always set
    assert r.resolve(est=_est(), edge=_edge()).grok_advisory_only is True


def test_research_cannot_override_failed_edge_gate():
    # an extremely confident research view still yields NO trade when the
    # deterministic edge gate rejects it
    r = SignalResolver()
    est = _est(mid=0.50, p_research=0.99, p_final=0.70, confidence=1.0, evidence=1.0)
    sig = r.resolve(est=est, edge=_edge(should_trade=False, reason="uncertainty_too_high"))
    assert sig.should_trade is False
    assert sig.no_trade_reason == "uncertainty_too_high"


def test_research_cannot_escalate_priority_to_bregman():
    r = SignalResolver()
    sig = r.resolve(est=_est(p_research=0.99, p_final=0.70, confidence=1.0), edge=_edge())
    assert sig.priority >= 2          # never priority 1 without a certified bundle
    assert sig.strategy != "bregman_arbitrage"


def test_trade_side_comes_from_microstructure_not_research():
    r = SignalResolver()
    # research leans down hard, but the executable edge/side is BUY -> side stays BUY
    sig = r.resolve(est=_est(mid=0.50, p_research=0.20, p_final=0.45),
                    edge=_edge(side="BUY", should_trade=True))
    assert sig.side == "BUY"


def test_resolver_module_has_no_execution_surface():
    src = (PLUGIN_ROOT / "engine" / "training" / "signal_resolver.py").read_text(
        encoding="utf-8").lower()
    for needle in ("submit_order", "place_order", "cancel_order", "oms.submit",
                   "broker.submit", "broker.place"):
        assert needle not in src, f"signal_resolver exposes order surface: {needle}"
