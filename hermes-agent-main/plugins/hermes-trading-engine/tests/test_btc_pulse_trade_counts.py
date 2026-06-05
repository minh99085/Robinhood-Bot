"""Tests for BTC Pulse opened-vs-resolved trade accounting + report reconciliation.

The dashboard hero counts RESOLVED trades while the report counts OPENED trades;
open (unsettled) 5-min rounds explain the gap. These tests pin the new
opened/resolved/open counters and the inspection consistency check.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import inspection_metrics as metrics  # noqa: E402

from engine.training.btc_pulse import BtcPulsePaperTrainer  # noqa: E402


def _trainer(vig=0.0):
    cfg = SimpleNamespace(
        btc_pulse_enabled=True, btc_pulse_paper_only=True, btc_pulse_isolated_learning=True,
        btc_pulse_require_chainlink=False, btc_pulse_require_fast_price=False,
        btc_pulse_require_risk_gate=True, btc_pulse_require_realistic_fill=True,
        btc_pulse_require_positive_ev=True, btc_pulse_min_ev_threshold=0.0,
        pulse_vig=vig, starting_bankroll=500.0, risk_engine_enabled=True,
        btc_pulse_tick_seconds=30, btc_pulse_round_seconds=300,
    )
    return BtcPulsePaperTrainer(cfg, rng_seed=1337)


def test_resolved_never_exceeds_opened_and_open_is_difference():
    t = _trainer(vig=0.0)
    base = t._clock()
    for i in range(600):
        t.tick(now_ms=base + i * 30_000)
    assert t.paper_trades > 0
    assert t.resolved_paper_trades > 0
    assert t.resolved_paper_trades <= t.paper_trades
    st = t.status()
    assert st["btc_pulse_paper_trades"] == t.paper_trades
    assert st["btc_pulse_resolved_trades"] == t.resolved_paper_trades
    assert st["btc_pulse_open_trades"] == t.paper_trades - t.resolved_paper_trades
    assert st["btc_pulse_paper_trades_opened"] == t.paper_trades


def test_status_exposes_opened_resolved_open_fields():
    t = _trainer()
    st = t.status()
    for k in ("btc_pulse_paper_trades", "btc_pulse_resolved_trades",
              "btc_pulse_open_trades", "btc_pulse_paper_trades_opened"):
        assert k in st


def test_consistency_flags_opened_vs_resolved_gap():
    feats = {"btc_pulse_paper_trades": 21, "btc_pulse_resolved_trades": 9}
    out = metrics.detect_inconsistencies(feats, {}, {})
    rec = [c for c in out if c["check"] == "btc_pulse_trades_opened_vs_resolved"]
    assert rec and rec[0]["severity"] == "INFO"
    assert rec[0]["values"]["open"] == 12


def test_dashboard_matching_resolved_is_not_flagged_as_mismatch():
    feats = {"btc_pulse_paper_trades": 21, "btc_pulse_resolved_trades": 9}
    api = {"state": {"portfolio": {"trades": 9}}}
    out = metrics.detect_inconsistencies(feats, {}, api)
    assert not any(c["check"] == "btc_pulse_dashboard_trade_count_mismatch" for c in out)


def test_dashboard_matching_neither_is_flagged():
    feats = {"btc_pulse_paper_trades": 21, "btc_pulse_resolved_trades": 9}
    api = {"state": {"portfolio": {"trades": 5}}}
    out = metrics.detect_inconsistencies(feats, {}, api)
    mm = [c for c in out if c["check"] == "btc_pulse_dashboard_trade_count_mismatch"]
    assert mm and mm[0]["severity"] == "WARN"


def test_extract_features_surfaces_open_trades():
    status = {"btc_pulse": {"btc_pulse_paper_trades": 21, "btc_pulse_resolved_trades": 9,
                            "btc_pulse_open_trades": 12}}
    feats = metrics.extract_features(status)
    assert feats["btc_pulse_paper_trades"] == 21
    assert feats["btc_pulse_resolved_trades"] == 9
    assert feats["btc_pulse_open_trades"] == 12
