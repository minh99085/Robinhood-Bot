"""TradingView 1m+5m MTF gate."""

from __future__ import annotations

import json as _json

from engine.pulse.engine import PulseConfig, PulseEngine
from engine.pulse.markets import PulseWindow
from engine.pulse.price import PulsePriceFeed, RollingVol
from engine.pulse.tv_mtf_gate import TradingViewMtfConflictGate
from tests.test_pulse_context_gate import _Mkt, _drive


def test_blocks_conflict_only_when_require_off():
    g = TradingViewMtfConflictGate(enabled=True, require_confirm=False, exploration_rate=0.0)
    assert g.evaluate(tf_confirm="conflict")["decision"] == "block"
    assert g.evaluate(tf_confirm="confirmed_down")["decision"] == "pass"
    assert g.evaluate(tf_confirm="single_tf")["decision"] == "pass"
    assert g.evaluate(tf_confirm="none")["decision"] == "pass"


def test_require_confirm_blocks_incomplete_mtf():
    g = TradingViewMtfConflictGate(enabled=True, require_confirm=True,
                                   require_side_align=False, exploration_rate=0.0)
    assert g.evaluate(tf_confirm="single_tf")["decision"] == "block"
    assert g.evaluate(tf_confirm="none")["decision"] == "block"
    assert g.evaluate(tf_confirm="confirmed_down")["decision"] == "pass"


def test_require_side_align():
    g = TradingViewMtfConflictGate(enabled=True, require_confirm=True,
                                   require_side_align=True, exploration_rate=0.0)
    assert g.evaluate(tf_confirm="confirmed_down", side="down")["decision"] == "pass"
    assert g.evaluate(tf_confirm="confirmed_down", side="up")["decision"] == "block"
    assert "tv_mtf_opposes_side" in g.evaluate(tf_confirm="confirmed_up", side="down")["reasons"]


def test_disabled_passes():
    g = TradingViewMtfConflictGate(enabled=False)
    assert g.evaluate(tf_confirm="conflict")["decision"] == "pass"


def _engine(tmp_path, **over):
    t0 = 9_970_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]

    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(
        tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
        min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
        settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
        tv_context_gate_enabled=False,
        tv_mtf_conflict_gate_enabled=True,
        tv_mtf_require_confirm=True,
        tv_mtf_conflict_exploration_rate=0.0,
        tradingview_secret="s3cr3t",
        tradingview_webhook_port=0,
        tradingview_feature_symbol="BTCUSDT",
        tradingview_allowed_symbols=("BTCUSDT",),
        data_dir=str(tmp_path), **over)
    return PulseEngine(cfg, market_feed=_Mkt(win, deep=True), price_feed=feed), t0


def _ingest(eng, *, direction, tf, now):
    payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "BTCUSDT",
               "direction": direction, "timeframe": tf,
               "bar_time": now, "event_id": "BTCUSDT-%s-%d-%s" % (tf, int(now * 1000), direction)}
    eng.tradingview.ingest(_json.dumps(payload).encode(), now=now)


def test_engine_blocks_mtf_conflict(tmp_path):
    eng, t0 = _engine(tmp_path)
    _ingest(eng, direction="UP", tf="5", now=t0 - 8)
    _ingest(eng, direction="DOWN", tf="1", now=t0 - 5)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("mtf_gate", 0) >= 1
    mg = eng.status()["tradingview"]["mtf_gate"]
    assert mg["enabled"] is True and mg["blocked"] >= 1
    if eng.webhook is not None:
        eng.webhook.stop()


def test_engine_blocks_without_full_confirm(tmp_path):
    eng, t0 = _engine(tmp_path)
    _ingest(eng, direction="DOWN", tf="1", now=t0 - 5)
    _drive(eng, t0)
    mg = eng.status()["tradingview"]["mtf_gate"]
    assert mg["blocked"] >= 1
    assert mg["block_reasons"].get("tv_mtf_single_tf_only", 0) >= 1
    if eng.webhook is not None:
        eng.webhook.stop()


def test_engine_passes_mtf_confirmed(tmp_path):
    # side-align tested in unit test; here verify full 1m+5m confirm is not blocked
    eng, t0 = _engine(tmp_path, tv_mtf_require_side_align=False)
    _ingest(eng, direction="DOWN", tf="5", now=t0 - 8)
    _ingest(eng, direction="DOWN", tf="1", now=t0 - 5)
    _drive(eng, t0)
    mg = eng.status()["tradingview"]["mtf_gate"]
    lc = eng.status()["decision_lifecycle"]
    assert mg["block_reasons"].get("tv_mtf_1m_5m_conflict", 0) == 0
    assert mg["block_reasons"].get("tv_mtf_single_tf_only", 0) == 0
    assert lc["rejected_by_stage"].get("mtf_gate", 0) == 0
    if eng.webhook is not None:
        eng.webhook.stop()