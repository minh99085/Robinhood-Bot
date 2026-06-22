"""Grok Decision Engine — "Grok decides, bot executes" (PAPER ONLY).

Proves: decision-contract parse/validate (aliases, clamps, fail-closed on unknown); the decider
worker decides + grades direction leakage-free + is budget-gated/fail-closed; is_actionable freshness
+ confidence; and end-to-end the engine SHADOW mode decides+grades without changing trading, FOLLOW
mode follows Grok's side bypassing opinion gates (execution gate still authoritative), and FOLLOW
abstains (no trade) when Grok says no_trade / has no fresh decision. Reconciliation holds.
"""

from __future__ import annotations

import time

from engine.pulse.grok_decider import (normalize_decision, make_decider_fn, GrokDecider)
from engine.pulse.grok_intel import GrokBudget
from engine.pulse.markets import OrderBook, PulseWindow
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol
from engine.pulse.engine import PulseEngine, PulseConfig


# ------------------------------- decision contract ---------------------------------------- #
def test_normalize_decision_aliases_clamps_failclosed():
    d = normalize_decision({"action": "BUY", "confidence": 1.7, "size_fraction": -1,
                            "max_price": 0.8, "ttl_s": 120})
    assert d["action"] == "up" and d["confidence"] == 1.0 and d["size_fraction"] == 0.0
    assert d["max_price"] == 0.8 and d["ttl_s"] == 120
    assert normalize_decision({"action": "short", "confidence": 0.6})["action"] == "down"
    nt = normalize_decision({"action": "hold", "size_fraction": 0.9})
    assert nt["action"] == "no_trade" and nt["size_fraction"] == 0.0
    assert normalize_decision({"action": "banana"}) is None     # unknown -> fail-closed
    assert normalize_decision("not a dict") is None


def test_decider_fn_failopen_on_bad_json():
    fn = make_decider_fn(chat=lambda *a, **k: "not json")
    assert fn({"x": 1}) is None
    fn2 = make_decider_fn(chat=lambda *a, **k: '{"action":"up","confidence":0.7}')
    assert fn2({"x": 1})["action"] == "up"


# ------------------------------- decider worker + grading --------------------------------- #
def test_decider_decides_and_grades_leakage_free():
    g = GrokDecider(decider_fn=lambda b: {"action": "up", "confidence": 0.8, "size_fraction": 1.0,
                                          "ttl_s": 240}, budget=None, mode="shadow")
    g.request("d1", {"any": "bundle"})
    assert g._process_one() is True
    dec = g.get("d1")
    assert dec["action"] == "up" and dec["confidence"] == 0.8 and "ts" in dec
    assert g.is_actionable(dec, now=time.time()) is True
    g.grade("d1", outcome_up=True)                  # up + realized up -> correct
    rep = g.report()
    assert rep["decided"] == 1 and rep["graded_directional"] == 1 and rep["direction_accuracy"] == 1.0
    assert rep["by_action"]["up"]["direction_accuracy"] == 1.0


def test_decider_failclosed_and_budget_skip():
    g = GrokDecider(decider_fn=lambda b: None, budget=None, mode="shadow")
    g.request("e", {})
    g._process_one()
    assert g.get("e") is None and g.report()["errors"] == 1
    spent = GrokBudget(daily_usd_cap=0.0, est_usd_per_call=0.02)
    g2 = GrokDecider(decider_fn=lambda b: {"action": "up", "confidence": 0.9}, budget=spent,
                     mode="shadow")
    g2.request("y", {})
    g2._process_one()
    assert g2.get("y") is None and g2.report()["skipped_budget"] == 1


def test_is_actionable_freshness_and_confidence():
    g = GrokDecider(min_confidence=0.55, ttl_s=240)
    now = 1000.0
    assert g.is_actionable({"action": "up", "confidence": 0.6, "ttl_s": 240, "ts": now}, now=now)
    assert not g.is_actionable({"action": "up", "confidence": 0.4, "ttl_s": 240, "ts": now}, now=now)
    assert not g.is_actionable({"action": "no_trade", "confidence": 0.9, "ts": now}, now=now)
    assert not g.is_actionable({"action": "up", "confidence": 0.9, "ttl_s": 10, "ts": now},
                               now=now + 60)             # stale


# ============================ engine end-to-end =========================================== #
class _FakeDecider:
    """Deterministic stand-in (no network): returns a fixed decision and records grades."""
    def __init__(self, mode, decision):
        self.mode = mode
        self._decision = decision
        self.graded = []
        self.requested = 0

    def request(self, decision_id, bundle):
        self.requested += 1

    def get(self, decision_id):
        return ({**self._decision, "ts": time.time()} if self._decision else None)

    def is_actionable(self, dec, now=None):
        return bool(dec and dec.get("action") in ("up", "down")
                    and float(dec.get("confidence") or 0) >= 0.55)

    def grade(self, decision_id, outcome_up, pnl=None):
        self.graded.append((decision_id, bool(outcome_up)))

    def report(self):
        return {"enabled": True, "mode": self.mode}

    def to_state(self):
        return {}


class _Mkt:
    def __init__(self, w):
        self._w = w

    def active_windows(self, now=None, **kw):
        return [self._w]

    def hydrate_books(self, w):
        w.up_book = OrderBook(best_bid=0.50, best_ask=0.55, ask_depth_usd=50000,
                              bid_depth_usd=50000, asks=[(0.55, 100000.0)], bids=[(0.50, 100000.0)])
        w.down_book = OrderBook(best_bid=0.44, best_ask=0.49, ask_depth_usd=49000,
                                bid_depth_usd=44000, asks=[(0.49, 100000.0)], bids=[(0.44, 100000.0)])
        return w

    def fetch_resolution(self, market_id):
        return True


def _engine(tmp_path, *, mode, decision):
    t0 = 9_980_000.0
    win = PulseWindow(event_id="e1", market_id="m1", slug="s", title="BTC Up or Down",
                      open_ts=t0, close_ts=t0 + 300, up_token_id="U", down_token_id="D")
    price = {"p": 64000.0}

    def fetch():
        price["p"] += 4.0
        return price["p"]
    feed = PulsePriceFeed(fetcher=fetch, source_name="rtds_chainlink",
                          vol=RollingVol(window_s=900, min_samples=8), max_open_lag_s=20.0)
    cfg = PulseConfig(tick_seconds=1.0, size_usd=10.0, min_edge=0.02, basis_buffer=0.0,
                      min_seconds_since_open=0.0, sigma_trust_floor=0.0, min_vol_samples=2,
                      settle_grace_s=0.0, exec_max_depth_consume_frac=0.9,
                      grok_decider_mode=mode, data_dir=str(tmp_path))
    eng = PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed)
    eng.grok_decider = _FakeDecider(mode, decision)        # inject (no network)
    return eng, t0


def _drive(eng, t0):
    for i in range(12):
        eng.tick(now=t0 - 12 + i)
    for k in range(6):
        eng.tick(now=t0 + 2 + k * 5)
    eng.tick(now=t0 + 305)


def test_engine_shadow_decides_and_grades_without_trading_change(tmp_path):
    # shadow: bot trades by its OWN logic (deep book -> trades), Grok is recorded + graded only
    eng, t0 = _engine(tmp_path, mode="shadow",
                      decision={"action": "down", "confidence": 0.9, "size_fraction": 1.0,
                                "ttl_s": 240})
    _drive(eng, t0)
    assert eng.ledger.trades >= 1                           # normal trading unaffected by shadow
    ext = [r.get("grok_decision") for r in eng.status()["recent_evaluations"]
           if r.get("grok_decision")]
    assert ext and ext[0]["action"] == "down"              # decision attached observe-only
    assert eng.grok_decider.graded                          # graded vs realized close
    assert eng.light_report()["global_reconciled"] is True


def test_engine_follow_follows_grok_side(tmp_path):
    # follow: Grok says UP -> bot takes the UP side (bypasses opinion gates; exec gate still applies)
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "up", "confidence": 0.8, "size_fraction": 0.5,
                                "ttl_s": 240})
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    pos = list(eng.ledger.positions.values())[0]
    assert pos.side == "up" and (pos.research or {}).get("entry_mode") == "grok_follow"
    assert eng.status()["live_trading_enabled"] is False
    assert eng.light_report()["global_reconciled"] is True


def test_engine_follow_abstains_on_no_trade(tmp_path):
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "confidence": 0.9, "ttl_s": 240})
    _drive(eng, t0)
    assert eng.ledger.trades == 0                           # fail-/abstain-closed: no trade
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("grok_decider", 0) >= 1
    assert eng.light_report()["global_reconciled"] is True


def test_engine_follow_abstains_on_no_decision(tmp_path):
    eng, t0 = _engine(tmp_path, mode="follow", decision=None)   # decider returned nothing
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    assert eng.light_report()["global_reconciled"] is True
