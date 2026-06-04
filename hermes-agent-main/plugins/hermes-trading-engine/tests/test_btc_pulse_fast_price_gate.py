"""BTC Pulse fast-price + Chainlink-anchor gating (PAPER ONLY)."""

from __future__ import annotations

import types

from engine.feeds.btc_fast_price import BtcFastPriceFeed
from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.chainlink_oracle import ChainlinkBtcUsdOracle
from engine.training.config import TrainingConfig

_NOW_MS = 1_700_000_000_000
_NOW_S = _NOW_MS / 1000.0


class _AnchorSource:
    def __init__(self, value, updated_at):
        self._r = types.SimpleNamespace(value=value, updated_at=updated_at)

    def read(self, spec, now=None):
        return self._r

    def history(self, feed_key, now=None, limit=50):
        return [self._r]


def _anchor(value=65000.0, age_s=100.0):
    # age 100s < 7200s max => anchor is "older but acceptable" (the headline fix)
    src = _AnchorSource(value, _NOW_S - age_s)
    return ChainlinkBtcUsdOracle(src, registry={"BTC/USD": object()}, max_age_seconds=7200)


def _pulse(oracle, fast, **cfgkw):
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_require_chainlink=True,
                         btc_pulse_require_fast_price=True,
                         btc_pulse_chainlink_max_age_seconds=7200,
                         btc_pulse_max_oracle_disagreement_bps=50.0,
                         btc_pulse_min_ev_threshold=-1.0, **cfgkw)
    return BtcPulsePaperTrainer(cfg, clock=lambda: _NOW_MS, oracle=oracle,
                                fast_price=fast, rng_seed=3)


def test_anchor_older_but_acceptable_is_not_stale():
    # 100s-old Chainlink anchor with a 7200s window must NOT be "stale".
    o = _anchor(age_s=100.0)
    st = o.read(now=_NOW_S)
    assert st.valid is True
    assert st.stale is False


def test_fresh_anchor_and_fast_price_allows_decision_and_uses_fast_price():
    o = _anchor(value=65000.0)
    fast = BtcFastPriceFeed(fetch=lambda: 65010.0, max_age_seconds=10)
    t = _pulse(o, fast)
    t.tick(now_ms=_NOW_MS)
    assert t.fast_counters["oracle_anchor_fresh_decisions"] >= 1
    assert t.fast_counters["fast_price_fresh_decisions"] >= 1
    assert t._price == 65010.0                        # uses the FAST price
    assert "chainlink_stale" not in t.rejection_reasons


def test_stale_fast_price_blocks_when_required():
    o = _anchor(value=65000.0)
    fast = BtcFastPriceFeed(fetch=lambda: None, max_age_seconds=10)   # never fresh
    t = _pulse(o, fast)
    out = t.tick(now_ms=_NOW_MS)
    assert out["event"] == "oracle_blocked"
    assert out["reason"] == "fast_price_stale"
    assert t.paper_trades == 0
    assert t.fast_counters["fast_price_stale_skips"] >= 1
    assert t.regime == "stale_fast_price"


def test_oracle_disagreement_blocks_trade():
    o = _anchor(value=65000.0)
    fast = BtcFastPriceFeed(fetch=lambda: 70000.0, max_age_seconds=10)   # ~769 bps off
    t = _pulse(o, fast)
    out = t.tick(now_ms=_NOW_MS)
    assert out["event"] == "oracle_blocked"
    assert out["reason"] == "oracle_disagreement"
    assert t.fast_counters["oracle_disagreement_skips"] >= 1
    assert t.regime == "oracle_disagreement"


def test_status_exposes_fast_price_fields():
    o = _anchor(value=65000.0)
    fast = BtcFastPriceFeed(fetch=lambda: 65010.0, max_age_seconds=10)
    t = _pulse(o, fast)
    t.tick(now_ms=_NOW_MS)
    st = t.status()
    assert st["btc_pulse_fast_price_required"] is True
    assert st["btc_pulse_fast_btc_price"] == 65010.0
    assert st["btc_pulse_chainlink_anchor_price"] == 65000.0
    assert st["btc_pulse_oracle_disagreement_bps"] is not None
    assert "btc_pulse_regime" in st
