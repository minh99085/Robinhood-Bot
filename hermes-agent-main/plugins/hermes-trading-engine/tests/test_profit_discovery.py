"""Profit-discovery: durable Bregman shadow labels + queue + bandit (PAPER ONLY).

Proves the critical fix (shadow_records_written=0 / empty shadow_labels.jsonl):
* a Bregman near-miss with shadow_label_candidate=True writes a DURABLE shadow label;
* shadow labels are LEARNING-ONLY (never paper trades, never readiness PnL, never
  bypass a gate);
* incomplete / thin-depth groups become shadow labels (or diagnostics), never trades;
* the profit-discovery queue is populated + prioritized from near-misses;
* the bandit router records action/reward metrics and CANNOT execute or size;
* if shadow candidates exist but zero labels are written, the report shows the blocker.
"""

import tempfile

from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.bregman_grouping import SimplexGroup, SimplexLeg
from engine.training.profit_discovery import (build_profit_discovery_queue,
                                              ProfitDiscoveryBandit, classify_priority)
from engine.training.inspection_summary import build_bregman_funnel


def _leg(mid, o, tok, ask, depth, fresh=True):
    return SimplexLeg(mid, o, tok, ask=ask, bid=ask - 0.01, depth_usd=depth,
                      fresh_book=fresh, stale=not fresh)


def _trainer(tmp_path, monkeypatch):
    from tests._pmtrain_helpers import clean_live_env
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train"), data_dir=tmp_path)


def _depth_only(): return SimplexGroup("dep", "binary_yes_no",
    [_leg("m", "YES", "mY", 0.4, 5), _leg("m", "NO", "mN", 0.4, 5)], exhaustive=True)


def _not_exhaustive(): return SimplexGroup("ne", "mutually_exclusive",
    [_leg("a", "YES", "aY", 0.3, 500), _leg("b", "YES", "bY", 0.3, 500)], exhaustive=False)


# --- durable shadow-label persistence -------------------------------------- #
def test_shadow_candidate_writes_durable_shadow_label(tmp_path, monkeypatch):
    import engine.training.polymarket_trainer as P
    t = _trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [_depth_only(), _not_exhaustive()])
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "m"}], now=1000.0)
    bx = t.bregman_exec_metrics
    assert bx["bregman_shadow_label_candidates"] >= 2
    assert bx["bregman_shadow_labels_written"] >= 2
    # durable file is actually non-empty (the bug was 0 rows)
    assert t.closed_loop.sink.file_line_counts()["shadow_labels"] >= 2
    assert t.closed_loop.counts["shadow_records_written"] >= 2


def test_shadow_label_is_learning_only_not_a_paper_trade(tmp_path, monkeypatch):
    import json
    import engine.training.polymarket_trainer as P
    t = _trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [_depth_only()])
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "m"}], now=1000.0)
    rows = [json.loads(x) for x in
            (t.closed_loop.sink.dir / "shadow_labels.jsonl").read_text().splitlines() if x.strip()]
    assert rows
    for r in rows:
        assert r["advisory_only"] is True
        assert r["executed"] is False
        assert r["trade_gate_bypassed"] is False
        assert r["counts_for_readiness"] is False
        assert r["is_paper_trade"] is False
        assert r["strategy_source"] == "bregman"
        assert r["event_type"] == "shadow_label"
    # no paper trade opened by the shadow path
    if hasattr(t, "open_positions"):
        assert all(getattr(p, "strategy", "") != "bregman" for p in t.open_positions())


def test_thin_and_incomplete_groups_become_shadow_not_trade(tmp_path, monkeypatch):
    import engine.training.polymarket_trainer as P
    t = _trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [_depth_only(), _not_exhaustive()])
    t.closed_loop.begin_tick()
    certs = t.scan_bregman([{"market_id": "m"}], now=1000.0)
    # nothing certified (gates intact), but shadow labels were written
    assert sum(1 for c in certs if c.is_opportunity) == 0
    assert t.bregman_exec_metrics["bregman_shadow_labels_written"] >= 2


def test_shadow_writes_are_rate_limited_and_deduped(tmp_path, monkeypatch):
    import engine.training.polymarket_trainer as P
    t = _trainer(tmp_path, monkeypatch)
    t._shadow_labels_per_tick = 1                      # force rate-limit
    monkeypatch.setattr(P, "group_markets", lambda recs, **kw: [_depth_only(), _not_exhaustive()])
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "m"}], now=1000.0)
    assert t.bregman_exec_metrics["bregman_shadow_labels_written_this_tick"] == 1
    assert "rate_limited" in t.bregman_exec_metrics["shadow_label_write_rejection_reasons"]
    # second tick: same groups -> deduped (already_written), not rewritten
    t._shadow_labels_per_tick = 25
    t.closed_loop.begin_tick()
    t.scan_bregman([{"market_id": "m"}], now=2000.0)
    assert "already_written" in t.bregman_exec_metrics["shadow_label_write_rejection_reasons"]


# --- profit-discovery queue ------------------------------------------------ #
def test_queue_priorities_from_near_misses():
    nms = [
        {"reject_reason": "not_exhaustive", "after_cost_lower_bound": 0.4,
         "depth_quality": {"thin_legs": 0}, "freshness": {"stale_legs": 0},
         "group_type": "mutually_exclusive", "group_key": "p1"},
        {"reject_reason": "depth_too_thin", "one_fix_away": True, "primary_fix": "depth",
         "after_cost_lower_bound": 0.2, "depth_quality": {"thin_legs": 2},
         "group_type": "binary_yes_no", "group_key": "p2"},
        {"reject_reason": "stale_book", "after_cost_lower_bound": None,
         "depth_quality": {"thin_legs": 1}, "group_type": "binary_yes_no", "group_key": "p5"},
    ]
    assert classify_priority(nms[0]) == 1
    assert classify_priority(nms[1]) == 2
    assert classify_priority(nms[2]) == 5
    q = build_profit_discovery_queue(nms)
    assert q["profit_discovery_queue_items"] == 3
    assert q["profit_discovery_queue_by_priority"]["1"] == 1
    assert q["profit_discovery_queue_sample"][0]["priority"] == 1   # sorted best-first


# --- bandit router --------------------------------------------------------- #
def test_bandit_records_actions_and_cannot_execute():
    b = ProfitDiscoveryBandit(enabled=True)
    for _ in range(6):
        a = b.select()
        b.update(a, b.reward_for(a, near_misses=[], shadow_written=0, grok_analyzed=0))
    st = b.status()
    assert st["bandit_router_enabled"] is True
    assert sum(st["bandit_action_counts"].values()) == 6
    assert st["bandit_no_gate_override"] is True
    assert st["bandit_can_execute"] is False and st["bandit_can_size"] is False
    # the router has no execute/size surface at all
    assert b.can_execute is False and b.can_size is False and b.can_override_gates is False


def test_bandit_reward_schedule():
    b = ProfitDiscoveryBandit()
    pos_nm = [{"depth_quality": {"thin_legs": 0}, "after_cost_lower_bound": 0.3}]
    assert b.reward_for("bregman_depth_watchlist", near_misses=pos_nm,
                        shadow_written=0, grok_analyzed=0) == 3.0
    assert b.reward_for("active_learning_shadow", near_misses=[],
                        shadow_written=2, grok_analyzed=0) == 2.0
    assert b.reward_for("grok_news_linked_near_miss", near_misses=[],
                        shadow_written=0, grok_analyzed=1) == 1.0


# --- report blocker when candidates exist but nothing persisted ------------ #
def test_funnel_reports_writer_blocker_when_zero_written():
    funnel = build_bregman_funnel({
        "bregman_shadow_label_candidates": 344,
        "bregman_shadow_labels_written": 0,
        "opened_bregman_bundles": 0})
    assert funnel["shadow_label_writer_blocker"] == "shadow_label_writer_not_persisting_candidates"
    assert funnel["profit_learning_status"] == "shadow_writer_not_persisting_candidates"


def test_funnel_profit_learning_status_shadow_data_only():
    funnel = build_bregman_funnel({
        "bregman_shadow_label_candidates": 10,
        "bregman_shadow_labels_written": 10,
        "opened_bregman_bundles": 0})
    assert funnel["profit_learning_status"] == "shadow_data_only"
    assert funnel["shadow_label_writer_blocker"] is None
