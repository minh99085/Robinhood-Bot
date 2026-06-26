"""Cross-timeframe (1m + 5m) TradingView confirmation — the bot must USE BOTH signals together,
not let the latest overwrite the other. OBSERVE-ONLY. Proves: same-symbol 1m+5m agreement yields
confirmed_up/down; disagreement -> conflict; only-one-fresh -> single_tf; stale 5m -> not confirmed;
and the confirmation flows into the feature + is graded as its own bucket dimension."""

from __future__ import annotations

import json

from engine.pulse.tradingview import TradingViewIntake, TradingViewEdge


def _intake(tmp_path):
    return TradingViewIntake(secret="s3cr3t", bot_name="hermes",
                             allowed_symbols=("BTCUSD",), data_dir=str(tmp_path))


def _send(intake, *, direction, tf, now, bar_time=None):
    payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "BTCUSD",
               "direction": direction, "timeframe": tf,
               "bar_time": bar_time if bar_time is not None else now,
               "event_id": "BTCUSD-%s-%s-%s" % (tf, int(now * 1000), direction)}
    return intake.ingest(json.dumps(payload).encode(), now=now)


def test_1m_5m_confirmation_states(tmp_path):
    ik = _intake(tmp_path)
    t = 1_000_000.0
    # both 1m and 5m DOWN within the window -> confirmed_down
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="1", now=t + 30)
    c = ik.mtf_confirmation(symbol="BTCUSD", now=t + 31)
    assert c["confirm"] == "confirmed_down" and c["direction"] == "DOWN"
    assert c["tf_1m_dir"] == "DOWN" and c["tf_5m_dir"] == "DOWN"
    # disagreement -> conflict
    _send(ik, direction="UP", tf="1", now=t + 60)
    c2 = ik.mtf_confirmation(symbol="BTCUSD", now=t + 61)
    assert c2["confirm"] == "conflict" and c2["direction"] is None
    # fresh 1m but the 5m (from t) is now older than confirm_window_s -> single_tf (5m drops out)
    _send(ik, direction="UP", tf="1", now=t + ik.confirm_window_s + 40)
    c3 = ik.mtf_confirmation(symbol="BTCUSD", now=t + ik.confirm_window_s + 41)
    assert c3["confirm"] == "single_tf" and c3["tf_5m_dir"] is None and c3["tf_1m_dir"] == "UP"


def test_10m_stored_separately_not_overriding_other_tfs(tmp_path):
    ik = _intake(tmp_path)
    t = 6_000_000.0
    _send(ik, direction="DOWN", tf="1", now=t)
    _send(ik, direction="UP", tf="5", now=t + 5)
    _send(ik, direction="UP", tf="10", now=t + 10)
    _send(ik, direction="DOWN", tf="15", now=t + 15)
    rep = ik.report()
    by_tf = rep["tradingview_latest_by_timeframe"]
    assert by_tf["BTCUSD@1"]["direction"] == "DOWN"
    assert by_tf["BTCUSD@5"]["direction"] == "UP"
    assert by_tf["BTCUSD@10"]["direction"] == "UP"
    assert by_tf["BTCUSD@15"]["direction"] == "DOWN"
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 20)
    assert mtf["tf_1m_dir"] == "DOWN"
    assert mtf["tf_5m_dir"] == "UP"
    assert mtf["tf_10m_dir"] == "UP"
    assert mtf["tf_15m_dir"] == "DOWN"


def test_10m_timeframe_normalized_from_suffix(tmp_path):
    ik = _intake(tmp_path)
    t = 6_100_000.0
    payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "BTCUSD",
               "direction": "UP", "timeframe": "10m", "bar_time": t,
               "event_id": "BTCUSD-10m-%d" % int(t * 1000)}
    code, body = ik.ingest(json.dumps(payload).encode(), now=t)
    assert code == 200 and body.get("accepted")
    assert "BTCUSD@10" in ik.report()["tradingview_latest_by_timeframe"]


def test_4tf_trend_alignment(tmp_path):
    ik = _intake(tmp_path)
    t = 6_200_000.0
    for tf in ("1", "5", "10", "15"):
        _send(ik, direction="UP", tf=tf, now=t + int(tf))
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 20)
    assert mtf["confirm_4tf"] == "confirmed_up_4tf"
    assert mtf["direction_4tf"] == "UP"
    assert mtf["trend_fresh_count"] == 4
    feat = ik.latest_feature(now=t + 20, symbol="BTCUSD")
    assert feat["tf_confirm_4tf"] == "confirmed_up_4tf"
    assert feat["trend_by_tf"] == {"1": "UP", "5": "UP", "10": "UP", "15": "UP"}


def test_15m_aligns_with_1m_5m(tmp_path):
    ik = _intake(tmp_path)
    t = 5_000_000.0
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="1", now=t + 10)
    _send(ik, direction="DOWN", tf="15", now=t + 20)
    mtf = ik.mtf_confirmation(symbol="BTCUSD", now=t + 30)
    assert mtf["tf_15m_dir"] == "DOWN"
    assert mtf["confirm_3tf"] == "confirmed_down_3tf"
    feat = ik.latest_feature(now=t + 30, symbol="BTCUSD")
    assert feat["tf_15m_dir"] == "DOWN"
    assert feat["tf_confirm_3tf"] == "confirmed_down_3tf"


def test_confirmation_flows_into_feature_and_grades(tmp_path):
    ik = _intake(tmp_path)
    t = 2_000_000.0
    _send(ik, direction="DOWN", tf="5", now=t)
    _send(ik, direction="DOWN", tf="1", now=t + 10)
    feat = ik.latest_feature(now=t + 11, symbol="BTCUSD")
    assert feat["tf_confirm"] == "confirmed_down" and feat["tf_confirm_direction"] == "DOWN"
    # report exposes both timeframes' latest (confirm in report uses wall-clock, so not asserted here)
    rep = ik.report()
    assert "tradingview_mtf_confirmation" in rep
    assert "BTCUSD@1" in rep["tradingview_latest_by_timeframe"]
    assert "BTCUSD@5" in rep["tradingview_latest_by_timeframe"]
    # the edge learner grades tf_confirm as its own dimension
    edge = TradingViewEdge()
    edge.record(tv=feat, traded_side="down", outcome_up=False, won=True, pnl=4.0)
    er = edge.report()
    assert "by_tf_confirm" in er and "confirmed_down" in er["by_tf_confirm"]
    assert er["by_tf_confirm"]["confirmed_down"]["n"] == 1


def test_index_mtf_via_feature_symbol(tmp_path):
    """Operator feeds INDEX:BTCUSD on 1m+5m charts — MTF resolves under BTCUSD."""
    ik = TradingViewIntake(secret="s3cr3t", bot_name="hermes",
                           allowed_symbols=("BTCUSD", "INDEX:BTCUSD"), data_dir=str(tmp_path),
                           feature_symbol="BTCUSD")
    t = 4_000_000.0
    for tf, ts in (("5", t), ("1", t + 10)):
        payload = {"secret": "s3cr3t", "bot_name": "hermes", "symbol": "INDEX:BTCUSD",
                   "direction": "UP", "timeframe": tf,
                   "bar_time": ts, "event_id": "BTCUSD-%s-%d-UP" % (tf, int(ts * 1000))}
        ik.ingest(json.dumps(payload).encode(), now=ts)
    c = ik.mtf_confirmation(symbol="btc/usd", now=t + 11)
    assert c["confirm"] == "confirmed_up" and c["symbol"] == "BTCUSD"
    feat = ik.latest_feature(now=t + 11, symbol="btc/usd")
    assert feat["tf_confirm"] == "confirmed_up"


def test_confirmation_survives_restart(tmp_path):
    ik = _intake(tmp_path)
    t = 3_000_000.0
    _send(ik, direction="UP", tf="5", now=t)
    _send(ik, direction="UP", tf="1", now=t + 5)
    ik2 = _intake(tmp_path)                       # reload from disk
    c = ik2.mtf_confirmation(symbol="BTCUSD", now=t + 6)
    assert c["confirm"] == "confirmed_up" and c["direction"] == "UP"
