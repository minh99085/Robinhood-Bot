"""Pass-3: strict paper execution realism (PaperExecutionPolicy + trainer wiring).

A paper trade only counts as REAL executable edge if it could plausibly fill
from the LIVE book. Reference/offline-stub/missing-ask/stale/thin/wide/ambiguous
candidates are downgraded to shadow-only (logged, never realized PnL) or hard
rejected. Bregman bundles require EVERY leg to be live-executable. PAPER ONLY.
"""

from __future__ import annotations

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.paper_execution import (
    EXECUTABLE, REJECT, SHADOW,
    PaperExecutionContext, PaperExecutionPolicy, bregman_leg_reason,
    SRC_LIVE_CLOB, SRC_OFFLINE_STUB, SRC_REFERENCE,
)

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0


def _policy(**over):
    cfg = TrainingConfig(mode="paper_train", **over)
    return PaperExecutionPolicy(cfg, bregman=False)


def _ctx(**kw):
    base = dict(fill_source=SRC_LIVE_CLOB, ask=0.40, bid=0.38, spread=0.02,
                depth_usd=500.0, book_age_sec=2.0, fresh_book=True,
                ambiguity_score=0.05, resolved=False, accepting_orders=True,
                notional_usd=5.0, tick_size=0.01)
    base.update(kw)
    return PaperExecutionContext(**base)


# --- policy: valid + each failure mode --------------------------------------

def test_valid_executable_book_allows_trade():
    d = _policy().evaluate(_ctx())
    assert d.mode == EXECUTABLE
    assert d.execution_realism_status == "realistic_executable"
    assert d.fill_price and d.fill_price >= 0.40       # tick/slip never better than ask
    assert d.fill_source == SRC_LIVE_CLOB


def test_missing_ask_rejects_executable_trade():
    d = _policy().evaluate(_ctx(ask=None))
    assert not d.allow_executable_trade
    assert d.execution_realism_status == "shadow_only_missing_ask"


def test_reference_price_fallback_blocked_when_disabled():
    d = _policy().evaluate(_ctx(fill_source=SRC_REFERENCE, ask=None))
    assert d.mode == SHADOW
    assert d.execution_realism_status == "shadow_only_reference_price"
    assert d.was_reference_price_fill and d.was_fallback_fill


def test_stale_book_rejects():
    d = _policy().evaluate(_ctx(fresh_book=False, ask=0.40))
    assert not d.allow_executable_trade
    assert d.execution_realism_status == "shadow_only_stale_book"


def test_stale_by_book_age_rejects():
    d = _policy().evaluate(_ctx(book_age_sec=999.0))
    assert d.execution_realism_status == "shadow_only_stale_book"


def test_thin_depth_rejects():
    d = _policy().evaluate(_ctx(depth_usd=5.0))     # < min_depth_at_price
    assert d.execution_realism_status == "shadow_only_thin_depth"


def test_wide_spread_rejects():
    d = _policy().evaluate(_ctx(spread=0.20))       # > max_spread 0.08
    assert d.execution_realism_status == "shadow_only_wide_spread"


def test_ambiguous_settlement_rejects():
    d = _policy().evaluate(_ctx(ambiguity_score=0.99))
    assert d.execution_realism_status == "shadow_only_ambiguous_settlement"


def test_closed_or_resolved_hard_rejects():
    assert _policy().evaluate(_ctx(resolved=True)).mode == REJECT
    assert _policy().evaluate(_ctx(accepting_orders=False)).mode == REJECT


def test_offline_stub_hard_rejects():
    d = _policy().evaluate(_ctx(fill_source=SRC_OFFLINE_STUB))
    assert d.mode == REJECT
    assert d.reason == "offline_stub_fill_disallowed"


def test_negative_after_cost_rejects():
    # gross edge smaller than spread+slippage+fee+tick drag -> reject
    d = _policy().evaluate(_ctx(gross_edge=0.0001))
    assert d.mode == REJECT
    assert d.reason == "negative_after_cost"
    assert d.after_cost_edge is not None and d.after_cost_edge <= 0.0


def test_positive_after_cost_allows():
    d = _policy().evaluate(_ctx(gross_edge=0.20))
    assert d.mode == EXECUTABLE
    assert d.after_cost_edge and d.after_cost_edge > 0.0


# --- bregman per-leg mapping ------------------------------------------------

def test_bregman_leg_reason_mapping():
    assert bregman_leg_reason("missing_executable_ask") == "bregman_leg_missing_ask"
    assert bregman_leg_reason("stale_book") == "bregman_leg_stale_book"
    assert bregman_leg_reason("wide_spread") == "bregman_leg_wide_spread"
    assert bregman_leg_reason("thin_depth") == "bregman_leg_thin_depth"
    assert bregman_leg_reason("ambiguous_settlement") == "bregman_leg_ambiguous"
    assert bregman_leg_reason("reference_fill_disallowed") == "bregman_reference_fill_disallowed"


def test_bregman_policy_flags_bad_leg_not_executable():
    bp = PaperExecutionPolicy(TrainingConfig(mode="paper_train"), bregman=True)
    assert bp.evaluate(_ctx(fresh_book=False)).execution_realism_status == "shadow_only_stale_book"
    assert bp.evaluate(_ctx(ask=None)).execution_realism_status == "shadow_only_missing_ask"


# --- trainer integration ----------------------------------------------------

def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", max_open_trades=8, **cfg), data_dir=tmp_path)


def _bregman_event(asks, *, group="elect", complete=True, stale_idx=None):
    recs = []
    for i, ask in enumerate(asks):
        raw = market(i, bid=round(ask - 0.02, 4), ask=ask, liq=20_000, depth=2000,
                     category="crypto", group=group, now=_NOW)
        if complete:
            raw["negRiskComplete"] = True
        if stale_idx == i:
            raw["bookUpdatedTs"] = _NOW - 10_000.0
        recs.append(um.MarketRecord.from_raw(raw, now=_NOW))
    return recs


def test_bregman_bundle_all_executable_legs_opens(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    opened = t._run_bregman(_bregman_event([0.28, 0.30, 0.30]), _NOW)
    assert opened == 1
    breg = [p for p in t.positions if p.strategy == "bregman"]
    assert breg and all(p.execution_realism_status == "realistic_executable" for p in breg)
    assert all(p.fill_source == "live_clob" and not p.was_reference_price_fill for p in breg)


def test_bregman_bundle_one_stale_leg_rejects_whole_bundle(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    opened = t._run_bregman(_bregman_event([0.28, 0.30, 0.30], stale_idx=2), _NOW)
    assert opened == 0
    assert not any(p.strategy == "bregman" for p in t.positions)


def test_realistic_executable_trade_stamps_provenance(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t._run_bregman(_bregman_event([0.28, 0.30, 0.30]), _NOW)
    rep = t.paper_realism_report()
    assert rep["realistic_trade_count"] >= 1
    assert rep["reference_price_fills_allowed_for_exploit"] is False
    assert rep["bregman_requires_all_executable_legs"] is True
    assert rep["offline_stub_fills_count_as_real"] is False


def test_directional_shadow_only_does_not_open_or_count_pnl(tmp_path, monkeypatch):
    """A directional candidate that fails realism is logged shadow-only and never
    opens a position or contributes realized PnL."""
    from types import SimpleNamespace
    t = _trainer(tmp_path, monkeypatch)
    raw = market(0, bid=0.38, ask=0.40, liq=20_000, depth=2000, now=_NOW)
    rec = um.MarketRecord.from_raw(raw, now=_NOW)
    # estimate with a STALE book -> reference/missing-ask -> shadow only
    est = SimpleNamespace(market_id="m0", fresh_book=False, spread=0.02,
                          ambiguity_score=0.05, evidence_score=1.0, liquidity_usd=20_000.0,
                          p_market_mid=0.40, p_market_bid=0.38, bregman_group_id="",
                          confidence=0.9, research_source="research",
                          calibrated_probability=None)
    edge = SimpleNamespace(outcome="YES", executable_price=0.40, p_final=0.6, net_edge=0.08)
    diag = SimpleNamespace(diagnostics_id="diag-1")
    res = t._open(rec, est, edge, diag, exploratory=False)
    assert res.get("opened") is False
    assert res.get("shadow_only") is True
    assert not t.positions                      # nothing opened
    assert t.realism_counts["shadow_trade_count"] >= 1
    rep = t.paper_realism_report()
    assert rep["readiness_pnl"] == 0.0
    assert rep["shadow_trade_count"] >= 1


def test_readiness_pnl_excludes_shadow_and_reference(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    # open + settle a realistic bregman bundle so realistic PnL is non-trivial
    t._run_bregman(_bregman_event([0.28, 0.30, 0.30]), _NOW)
    rep = t.paper_realism_report()
    # readiness PnL only sums realistic non-exploration trades
    assert rep["readiness_pnl"] == round(
        rep["bregman_realistic_pnl"] + rep["directional_realistic_pnl"], 6)
    assert rep["reference_fill_theoretical_pnl"] == 0.0
