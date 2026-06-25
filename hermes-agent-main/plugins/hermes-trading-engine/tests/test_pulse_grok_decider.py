"""Grok Decision Engine — "Grok decides, bot executes" (PAPER ONLY).

Proves: decision-contract parse/validate (aliases, clamps, fail-closed on unknown); the decider
worker decides + grades direction leakage-free + is budget-gated/fail-closed; is_actionable freshness
+ confidence; and end-to-end the engine SHADOW mode decides+grades without changing trading, FOLLOW
mode follows Grok's side bypassing opinion gates (execution gate still authoritative), and FOLLOW
abstains (no trade) when Grok says no_trade / has no fresh decision. Reconciliation holds.
"""

from __future__ import annotations

import time

from engine.pulse.grok_decider import (normalize_decision, make_decider_fn, GrokDecider,
                                        AggressionController)
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


def test_decider_learns_per_context_and_recent():
    g = GrokDecider(decider_fn=lambda b: {"action": "up", "p_up": 0.7, "confidence": 0.7},
                    mode="shadow")
    g.request("a", {"b": 1}, context={"hurst_regime": "trending", "ttc_bucket": "60-120s"})
    g._process_one()
    g.grade("a", outcome_up=True)              # up + up -> correct in trending
    g.request("b", {"b": 1}, context={"hurst_regime": "trending", "ttc_bucket": "60-120s"})
    g._process_one()
    g.grade("b", outcome_up=False)             # up + down -> wrong in trending
    rep = g.report()
    acc = rep["accuracy_by_context"]["hurst_regime"]["trending"]
    assert acc["n"] == 2 and acc["accuracy"] == 0.5
    assert len(rep["recent_decisions"]) == 2 and rep["recent_decisions"][0]["view_correct"] is True
    # learning state survives a persist/restore round-trip
    g2 = GrokDecider(mode="shadow")
    g2.load_state(g.to_state())
    assert g2.report()["accuracy_by_context"]["hurst_regime"]["trending"]["n"] == 2


def test_decider_grades_view_even_on_no_trade():
    # the directional VIEW (p_up) is graded EVERY window, even when the action is no_trade — this is
    # the always-on edge data that lets Grok build a track record while abstaining.
    g = GrokDecider(decider_fn=lambda b: {"action": "no_trade", "p_up": 0.7, "confidence": 0.6},
                    mode="shadow")
    g.request("v1", {"b": 1}, context={"hurst_regime": "trending"})
    g._process_one()
    assert g.get("v1")["p_up"] == 0.7
    g.grade("v1", outcome_up=True)             # p_up 0.7 -> view up; outcome up -> correct
    rep = g.report()
    assert rep["views_graded"] == 1 and rep["view_accuracy"] == 1.0
    assert rep["graded_directional"] == 0 and rep["abstains"] == 1     # no_trade not action-graded
    assert rep["accuracy_by_context"]["hurst_regime"]["trending"]["n"] == 1


def test_view_edge_promotion_flags_real_edge_only():
    # a context with a strong, well-sampled view edge is flagged; a coin-flip one is not.
    g = GrokDecider(decider_fn=lambda b: {"action": "no_trade", "p_up": 0.7, "confidence": 0.5},
                    mode="shadow", view_promote_min_samples=20)
    # 30 graded views in 'trending', 24 correct (~0.8) -> Wilson lower > 0.5 -> edge candidate
    for i in range(30):
        g.grade_fields(action="no_trade", p_up=(0.7 if i < 24 else 0.3),
                       context={"hurst_regime": "trending"}, outcome_up=True)
    cands = g.report()["view_edge_candidates"]
    assert any(c["dimension"] == "hurst_regime" and c["bucket"] == "trending" for c in cands)
    # a 50/50 context with enough samples is NOT flagged
    g2 = GrokDecider(mode="shadow", view_promote_min_samples=20)
    for i in range(30):                            # always predicts up; outcome alternates -> ~0.5
        g2.grade_fields(action="no_trade", p_up=0.6,
                        context={"hurst_regime": "noise"}, outcome_up=(i % 2 == 0))
    assert g2.report()["view_edge_candidates"] == []


def test_normalize_decision_includes_p_up():
    assert normalize_decision({"action": "no_trade", "p_up": 0.62})["p_up"] == 0.62
    # derive p_up from action+confidence when omitted
    assert normalize_decision({"action": "up", "confidence": 0.8})["p_up"] == 0.8
    assert normalize_decision({"action": "down", "confidence": 0.7})["p_up"] == 0.3


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
    def __init__(self, mode, decision, *, follow_ok=True, policy_mode="explore"):
        self.mode = mode
        self._decision = decision
        self._follow_ok = follow_ok
        self._policy_mode = policy_mode
        self.aggr = AggressionController()             # real controller (modulates explore/size)
        self.graded = []
        self.requested = 0
        self.follow_results = []

    def context_policy(self, context, **kw):
        return ({"mode": "exploit", "size_mult": 1.5, "dimension": "hurst_regime", "bucket": "x"}
                if self._policy_mode == "exploit"
                else ({"mode": "avoid", "dimension": "hurst_regime", "bucket": "x"}
                      if self._policy_mode == "avoid" else {"mode": "explore"}))

    def request(self, decision_id, bundle, context=None):
        self.requested += 1
        self.last_bundle = bundle

    def get(self, decision_id):
        return ({**self._decision, "ts": time.time()} if self._decision else None)

    def is_actionable(self, dec, now=None):
        return bool(dec and dec.get("action") in ("up", "down")
                    and float(dec.get("confidence") or 0) >= 0.55)

    def should_follow(self, now=None):
        return (True, "ok") if self._follow_ok else (False, "breaker_test")

    def record_follow_result(self, *, won, pnl, now=None):
        self.follow_results.append((bool(won), float(pnl)))

    def grade(self, decision_id, outcome_up, pnl=None):
        self.graded.append((decision_id, bool(outcome_up)))

    def grade_fields(self, *, action, p_up, context, outcome_up, pnl=None):
        self.graded.append((action, p_up, bool(outcome_up)))

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


# ------------------------------- circuit breaker (Phase 8) -------------------------------- #
def test_breaker_consecutive_losses_then_recovers():
    g = GrokDecider(mode="follow", max_consecutive_losses=3, cooldown_s=100, daily_loss_cap_usd=999)
    now = 1000.0
    for _ in range(2):
        g.record_follow_result(won=False, pnl=-5.0, now=now)
    assert g.should_follow(now)[0] is True            # 2 losses < 3 -> still following
    g.record_follow_result(won=False, pnl=-5.0, now=now)   # 3rd -> trip
    ok, reason = g.should_follow(now)
    assert ok is False and reason == "breaker_consecutive_losses"
    assert g.should_follow(now + 50)[0] is False      # still in cooldown
    assert g.should_follow(now + 101)[0] is True       # cooldown elapsed -> follows again


def test_breaker_daily_loss_cap():
    g = GrokDecider(mode="follow", max_consecutive_losses=99, daily_loss_cap_usd=8.0, cooldown_s=100)
    now = 2000.0
    g.record_follow_result(won=False, pnl=-5.0, now=now)
    assert g.should_follow(now)[0] is True             # $5 < $8 cap
    g.record_follow_result(won=False, pnl=-5.0, now=now)   # $10 >= $8
    assert g.should_follow(now) == (False, "breaker_daily_loss_cap")


def test_breaker_latency():
    g = GrokDecider(mode="follow", max_latency_s=5.0, cooldown_s=100, daily_loss_cap_usd=999)
    for _ in range(g._recent_lat.maxlen):
        g._recent_lat.append(9.0)                      # sustained high latency
    assert g.should_follow(3000.0) == (False, "breaker_latency")


# ------------------------------- news digest (Phase 3) ----------------------------------- #
def test_news_digest_refresh_failopen_and_expiry():
    from engine.pulse.grok_decider import GrokNewsDigest
    nd = GrokNewsDigest(news_fn=lambda: {"sentiment": "bullish", "confidence": 0.7,
                                         "headlines": ["x"], "event_risk": "low"},
                        interval_s=60, max_age_s=600)
    nd.refresh()
    latest = nd.latest()
    assert latest["sentiment"] == "bullish" and "age_s" in latest and nd.report()["calls"] == 1
    nd2 = GrokNewsDigest(news_fn=lambda: None)         # fail-open
    nd2.refresh()
    assert nd2.latest() is None and nd2.report()["errors"] == 1
    nd._ts = time.time() - 10_000                       # force staleness
    assert nd.latest() is None


def _engine(tmp_path, *, mode, decision, follow_ok=True, policy_mode="explore", **over):
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
                      grok_decider_mode=mode, data_dir=str(tmp_path), **over)
    eng = PulseEngine(cfg, market_feed=_Mkt(win), price_feed=feed)
    eng.grok_decider = _FakeDecider(mode, decision, follow_ok=follow_ok,
                                    policy_mode=policy_mode)               # inject (no network)
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


def test_engine_capital_status(tmp_path):
    # on-hand capital = starting capital (default $500) + realized PnL
    eng, t0 = _engine(tmp_path, mode="shadow",
                      decision={"action": "no_trade", "confidence": 0.5, "ttl_s": 240})
    _drive(eng, t0)
    cap = eng.status()["capital"]
    assert cap["starting_capital_usd"] == 500.0 and cap["paper_only"] is True
    realized = eng.ledger.stats().get("realized_pnl_usd") or 0.0
    assert abs(cap["on_hand_capital_usd"] - (500.0 + realized)) < 0.02   # cents rounding
    assert "open_exposure_usd" in cap and "return_pct" in cap


def test_engine_bundle_is_fully_structured(tmp_path):
    # the payload sent to Grok must be complete + correctly typed (payoff, divergence, account, etc.)
    eng, t0 = _engine(tmp_path, mode="shadow",
                      decision={"action": "no_trade", "confidence": 0.5, "ttl_s": 240})
    _drive(eng, t0)
    b = getattr(eng.grok_decider, "last_bundle", None)
    assert b is not None
    for k in ("schema_version", "objective", "decision_id", "timing", "price", "digital_fair_p_up",
              "polymarket", "payoff", "account_state", "bot_learned_evidence",
              "decider_track_record"):
        assert k in b, k
    assert "breakeven_win_rate" in (b["payoff"]["up"] or {})        # binary payoff bar present
    assert "fair_minus_poly" in b["polymarket"]                     # fair-vs-market divergence present
    assert "win_rate" in b["account_state"]
    # recent resolved 5-min windows + momentum summary are present for Grok to reason over
    rw = b["recent_windows"]
    assert "windows" in rw and "up_rate" in rw and "current_streak" in rw
    assert isinstance(rw["windows"], list)


def test_recent_windows_view_summary():
    # the recent-windows momentum summary computes up-rate + current streak from resolved windows
    from engine.pulse.engine import PulseEngine
    eng = PulseEngine.__new__(PulseEngine)             # bypass __init__ for a pure-method unit test
    eng._recent_windows = [{"outcome": "down"}, {"outcome": "up"}, {"outcome": "up"},
                           {"outcome": "up"}]
    v = eng._recent_windows_view(10)
    assert v["n"] == 4 and v["up_rate"] == 0.75 and v["current_streak"] == "upx3"


def test_full_report_md_is_comprehensive():
    from engine.pulse.reporting import build_full_report_md
    light = {
        "live_trading_enabled": False, "global_reconciled": True,
        "capital": {"on_hand_capital_usd": 417.55, "starting_capital_usd": 500.0,
                    "return_pct": -16.49, "open_exposure_usd": 0.0, "open_positions": 0},
        "ledger": {"trades": 290, "settled": 290, "win_rate": 0.5276,
                   "realized_pnl_usd": -76.8, "profit_factor": 0.82},
        "reconciliation": {"global_reconciled": True, "rejected_before_execution": 15273},
        "candidate_lifecycle": {"created": 16351, "terminals": {"accepted": 139},
                                "rejected_by_stage": {"grok_decider": 6979}},
        "execution_stats": {"candidates": 245, "accepted": 245}, "reject_reasons": {},
        "calibration": {"brier": 0.23, "samples": 290},
        "pnl_by_hurst_regime": {"trending": {"n": 7, "win_rate": 0.57}},
        "pnl_by_entry_mode": {"grok_explore": {"n": 3, "win_rate": 0.33}},
        "learned_selectivity_gate": {"decision_rule": "confidently_below_breakeven", "rejected": 837,
                                     "bucket_evidence": {"buckets": [{"dimension": "direction",
                                     "bucket": "down", "n": 127, "win_rate": 0.49,
                                     "breakeven_win_rate": 0.58, "win_rate_upper_ci": 0.56,
                                     "ev_per_trade": -0.72, "confidently_losing": True}]}},
        "tradingview": {"context_gate": {"enabled": True, "blocked": 190, "block_reasons": {}},
                        "signal_learning": {"settled_with_signal": 43},
                        "rsi_trend": {"signal_direction_hit_rate": 0.47}},
        "late_window_entry": {"gate": {"enabled": False}, "edge_measurement": {"verdict": "x"}},
        "grok_decider": {"mode": "follow", "decided": 104, "view_accuracy": 0.46,
                         "aggression": {"aggression": 0.1}, "adaptive_policy_counts": {"exploit": 0},
                         "view_edge_candidates": [], "circuit_breaker": {"tripped": False}},
        "grok_signal_intel": {"budget": {}, "predictor_B": {"accuracy": 0.49}, "analyst_A": {}},
        "edge_signal": {"enabled": True}, "readiness": {"status": "not_ready"},
        "ev_before_after_costs": {"avg_ev_after_costs": 0.1},
    }
    ledger = {"positions": [{"title": "BTC Up or Down", "side": "up", "entry_price": 0.55,
                             "fair_at_entry": 0.6, "won": True, "outcome_up": True, "pnl_usd": 2.9,
                             "research": {"entry_mode": "grok_explore"}}]}
    md = build_full_report_md(light, {"ticks": 200}, ledger)
    for section in ("Performance Scorecard", "1. Trading Performance", "2. Operation",
                    "3. External Signals", "Signal impact on trading performance",
                    "Looping engine", "Recent positions"):
        assert section in md, section
    assert "417.55" in md and "grok_explore" in md and "follow" in md   # capital, positions, decider


def test_aggression_loosens_on_profit_tightens_on_loss():
    c = AggressionController(start=0.2, step_up=0.05, step_down=0.1, eval_window=12, max_aggr=1.0)
    base = c.aggression
    for _ in range(12):                            # consistent profits -> loosen
        c.record(2.0)
    assert c.aggression > base
    assert c.effective_explore_rate(0.3) > 0.3     # explores more
    assert c.size_scale() > 1.0                    # sizes up
    assert c.exploit_margin(0.0) < 0.0             # looser exploit bar
    hi = c.aggression
    for _ in range(12):                            # then losses -> tighten (faster, step_down>up)
        c.record(-5.0)
    assert c.aggression < hi
    # bounded
    for _ in range(50):
        c.record(-5.0)
    assert c.aggression >= 0.0
    # state round-trip
    c2 = AggressionController()
    c2.load_state(c.to_state())
    assert abs(c2.aggression - c.aggression) < 1e-9


def test_context_policy_exploit_avoid_explore():
    g = GrokDecider(mode="follow", adaptive_min_samples=20)
    # build a proven-edge context (trending: 30 views, 24 correct ~0.8 -> Wilson lower > 0.5)
    for i in range(30):
        g.grade_fields(action="no_trade", p_up=(0.7 if i < 24 else 0.3),
                       context={"hurst_regime": "trending"}, outcome_up=True)
    pol = g.context_policy({"hurst_regime": "trending"})
    assert pol["mode"] == "exploit" and pol["size_mult"] >= 1.0
    # build a proven-LOSING context (noise: 30 views, 6 correct ~0.2 -> Wilson upper < 0.5)
    for i in range(30):
        g.grade_fields(action="no_trade", p_up=0.7,
                       context={"hurst_regime": "noise"}, outcome_up=(i < 6))
    assert g.context_policy({"hurst_regime": "noise"})["mode"] == "avoid"
    # an unseen / under-sampled context -> explore
    assert g.context_policy({"hurst_regime": "mean_reverting"})["mode"] == "explore"


def test_engine_follow_explore_trades_view_when_grok_abstains(tmp_path):
    # Grok abstains (no_trade) but its p_up view leans up; explore_rate=1.0 -> bot trades the view
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "p_up": 0.62, "confidence": 0.4, "ttl_s": 240},
                      grok_decider_explore_rate=1.0, grok_decider_explore_size_fraction=0.5)
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    pos = list(eng.ledger.positions.values())[0]
    assert pos.side == "up" and (pos.research or {}).get("entry_mode") == "grok_explore"
    assert eng.light_report()["global_reconciled"] is True


def test_engine_follow_explore_off_still_abstains(tmp_path):
    # explore_rate=0 (default) -> abstain on no_trade as before (no trades)
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "p_up": 0.62, "confidence": 0.4, "ttl_s": 240},
                      grok_decider_explore_rate=0.0)
    _drive(eng, t0)
    assert eng.ledger.trades == 0


def test_engine_follow_explore_blocked_on_coinflip_view(tmp_path):
    # explore on a near-50/50 p_up view is blocked (no coin-flip trades)
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "p_up": 0.51, "confidence": 0.4, "ttl_s": 240},
                      grok_decider_explore_rate=1.0)
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    lc = eng.status()["decision_lifecycle"]
    assert lc["rejected_by_stage"].get("grok_decider", 0) >= 1


def test_engine_grok_follow_blocks_up_on_down_bias(tmp_path):
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "up", "confidence": 0.8, "size_fraction": 1.0, "ttl_s": 240},
                      tv_down_bias_gate_enabled=True,
                      tv_down_bias_block_up_against_confirmed_down=True)
    eng.tv_down_bias_gate.evaluate = lambda **kw: {
        "decision": "block", "reasons": ["tv_down_bias_up_against_confirmed_down"], "active": True}
    _drive(eng, t0)
    assert eng.ledger.trades == 0
    assert eng.status()["decision_lifecycle"]["rejected_by_stage"].get("down_bias_gate", 0) >= 1


def test_engine_adaptive_exploits_proven_edge_context(tmp_path):
    # Grok abstains, explore OFF; policy says EXPLOIT (proven-edge context) -> adaptive auto-trades
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "p_up": 0.7, "confidence": 0.4, "ttl_s": 240},
                      grok_decider_explore_rate=0.0, grok_decider_adaptive=True,
                      policy_mode="exploit")
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    assert any((p.research or {}).get("entry_mode") == "grok_adaptive"
               for p in eng.ledger.positions.values())
    assert eng.status()["grok_decider"]["adaptive_policy_counts"]["exploit"] >= 1
    assert eng.light_report()["global_reconciled"] is True


def test_engine_adaptive_avoids_proven_bad_context(tmp_path):
    # policy says AVOID (proven-losing context) -> no trade even though Grok gave a p_up view
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "no_trade", "p_up": 0.7, "confidence": 0.4, "ttl_s": 240},
                      grok_decider_explore_rate=1.0, grok_decider_adaptive=True, policy_mode="avoid")
    _drive(eng, t0)
    assert eng.ledger.trades == 0                       # avoid suppresses even exploration
    assert eng.status()["grok_decider"]["adaptive_policy_counts"]["avoid"] >= 1


def test_engine_follow_fraction_zero_uses_baseline(tmp_path):
    # A/B canary control arm: follow_fraction=0 -> never follows; trades by baseline logic instead
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "up", "confidence": 0.9, "size_fraction": 1.0,
                                "ttl_s": 240},
                      grok_decider_follow_fraction=0.0)
    _drive(eng, t0)
    assert eng.ledger.trades >= 1                       # still trades (baseline arm)
    assert all((p.research or {}).get("entry_mode") != "grok_follow"
               for p in eng.ledger.positions.values())


def test_engine_follow_breaker_tripped_falls_back_to_baseline(tmp_path):
    # breaker tripped -> should_follow False -> bot trades baseline (no grok_follow entries)
    eng, t0 = _engine(tmp_path, mode="follow",
                      decision={"action": "up", "confidence": 0.9, "size_fraction": 1.0,
                                "ttl_s": 240}, follow_ok=False)
    _drive(eng, t0)
    assert eng.ledger.trades >= 1
    assert all((p.research or {}).get("entry_mode") != "grok_follow"
               for p in eng.ledger.positions.values())
    assert eng.light_report()["global_reconciled"] is True
