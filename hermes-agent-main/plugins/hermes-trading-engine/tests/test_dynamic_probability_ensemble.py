"""Dynamic forecast ensemble + uncertainty decomposition (TDD, deterministic).

Quant scope exercised here:
* Signal Generation & Strategy Development — dynamic ensemble weighting across
  market midpoint, learned category bias, research/Grok estimate, Chainlink
  oracle features, liquidity/spread regimes, time-to-resolution, ambiguity, and
  calibration history.
* Statistical & Probabilistic Modeling — explicit uncertainty decomposition.
* Risk Management & Portfolio Optimization — the ensemble can NEVER become more
  aggressive (further from the market) as evidence weakens, the book/oracle goes
  stale, ambiguity rises, liquidity drops, calibration worsens, or time-to-
  resolution shortens.
* Compliance — Grok is research-only: it is consumed as a probability INPUT and
  the ensemble never emits an order/size/place field.

No randomness, no network, no Grok call.
"""

from __future__ import annotations

from engine.research.ensemble import (
    DynamicForecastEnsemble,
    decompose_uncertainty,
)


def _dev(result: dict, p_market: float = 0.5) -> float:
    return abs(result["p_ensemble"] - p_market)


def _combine(**over):
    base = dict(
        p_market=0.50, p_model=0.50, p_research=0.85, category_bias=0.0,
        research_confidence=0.9, research_usable=True, evidence_score=0.9,
        chainlink_adjustment=0.0, chainlink_confidence=0.0, chainlink_linked=False,
        chainlink_no_trade=False, liquidity_usd=50_000.0, spread=0.01,
        time_to_resolution_s=7 * 24 * 3600.0, ambiguity_score=0.0,
        calibration_error=0.0)
    base.update(over)
    return DynamicForecastEnsemble().combine(**base)


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def test_combine_returns_named_weights_and_uncertainty():
    r = _combine()
    assert 0.0 <= r["p_ensemble"] <= 1.0
    for k in ("market", "research", "model", "chainlink"):
        assert k in r["weights"]
    for k in ("market", "model", "research", "chainlink", "liquidity",
              "ambiguity", "stale", "total"):
        assert k in r["uncertainty"]
    assert "conservatism" in r


def test_research_only_no_execution_fields_leak():
    r = _combine()
    forbidden = ("order", "size", "place", "submit", "arm", "approve", "side")
    for key in r:
        assert not any(f in key.lower() for f in forbidden)


# --------------------------------------------------------------------------- #
# dynamic weighting
# --------------------------------------------------------------------------- #
def test_higher_research_confidence_increases_research_pull():
    lo = _combine(research_confidence=0.2)
    hi = _combine(research_confidence=0.95)
    # research sits at 0.85 (above the 0.50 market); more confidence -> closer to it
    assert hi["p_ensemble"] > lo["p_ensemble"]
    assert hi["weights"]["research"] > lo["weights"]["research"]


def test_category_bias_shifts_model_component():
    up = _combine(p_research=0.50, p_model=0.50, category_bias=0.05)
    flat = _combine(p_research=0.50, p_model=0.50, category_bias=0.0)
    assert up["p_ensemble"] > flat["p_ensemble"]


def test_fresh_chainlink_can_add_bounded_pull():
    flat = _combine(chainlink_linked=False)
    cl = _combine(chainlink_linked=True, chainlink_confidence=0.9,
                  chainlink_adjustment=0.08)
    assert cl["p_ensemble"] >= flat["p_ensemble"]
    assert cl["weights"]["chainlink"] > 0.0


# --------------------------------------------------------------------------- #
# conservatism monotonicity — never MORE aggressive under adverse conditions
# --------------------------------------------------------------------------- #
def test_wider_spread_never_more_aggressive():
    tight = _combine(spread=0.01)
    wide = _combine(spread=0.07)
    assert _dev(wide) <= _dev(tight) + 1e-12


def test_higher_ambiguity_never_more_aggressive():
    lo = _combine(ambiguity_score=0.0)
    hi = _combine(ambiguity_score=0.8)
    assert _dev(hi) <= _dev(lo) + 1e-12


def test_staler_book_never_more_aggressive():
    fresh = _combine(stale_score=0.0)
    stale = _combine(stale_score=0.9)
    assert _dev(stale) <= _dev(fresh) + 1e-12


def test_lower_liquidity_never_more_aggressive():
    deep = _combine(liquidity_usd=80_000.0)
    thin = _combine(liquidity_usd=200.0)
    assert _dev(thin) <= _dev(deep) + 1e-12


def test_weaker_evidence_never_more_aggressive():
    strong = _combine(evidence_score=0.95)
    weak = _combine(evidence_score=0.1)
    assert _dev(weak) <= _dev(strong) + 1e-12


def test_worse_calibration_never_more_aggressive():
    good = _combine(calibration_error=0.0)
    bad = _combine(calibration_error=0.4)
    assert _dev(bad) <= _dev(good) + 1e-12


def test_shorter_time_to_resolution_never_more_aggressive():
    far = _combine(time_to_resolution_s=14 * 24 * 3600.0)
    near = _combine(time_to_resolution_s=120.0)
    assert _dev(near) <= _dev(far) + 1e-12


def test_stale_chainlink_pins_to_market():
    """A stale/blocked oracle on a linked market must collapse the ensemble onto
    the market price (fully conservative) and can never be more aggressive than a
    fresh oracle."""
    fresh = _combine(chainlink_linked=True, chainlink_confidence=0.9,
                     chainlink_adjustment=0.08)
    stale = _combine(chainlink_linked=True, chainlink_no_trade=True,
                     chainlink_confidence=0.0, chainlink_adjustment=0.08)
    assert abs(stale["p_ensemble"] - 0.50) < 1e-9
    assert _dev(stale) <= _dev(fresh) + 1e-12


# --------------------------------------------------------------------------- #
# uncertainty decomposition
# --------------------------------------------------------------------------- #
def test_uncertainty_components_bounded():
    u = decompose_uncertainty(spread=0.04, calibration_error=0.1,
                              model_has_edge=True, research_usable=True,
                              confidence=0.7, evidence_score=0.6,
                              chainlink_confidence=0.0, chainlink_linked=False,
                              chainlink_no_trade=False, liquidity_usd=10_000.0,
                              ambiguity_score=0.2, stale_score=0.1)
    for k, v in u.items():
        assert 0.0 <= v <= 1.0, (k, v)


def test_uncertainty_total_increases_with_each_source():
    base = dict(spread=0.02, calibration_error=0.0, model_has_edge=True,
                research_usable=True, confidence=0.8, evidence_score=0.8,
                chainlink_confidence=0.5, chainlink_linked=True,
                chainlink_no_trade=False, liquidity_usd=50_000.0,
                ambiguity_score=0.0, stale_score=0.0)
    t0 = decompose_uncertainty(**base)["total"]
    for worse in (dict(spread=0.08), dict(ambiguity_score=0.9),
                  dict(stale_score=0.9), dict(liquidity_usd=50.0),
                  dict(evidence_score=0.05), dict(calibration_error=0.4),
                  dict(chainlink_no_trade=True)):
        cfg = {**base, **worse}
        assert decompose_uncertainty(**cfg)["total"] >= t0 - 1e-12


def test_stale_data_raises_stale_uncertainty():
    fresh = decompose_uncertainty(spread=0.02, stale_score=0.0)["stale"]
    stale = decompose_uncertainty(spread=0.02, stale_score=0.9)["stale"]
    assert stale > fresh
