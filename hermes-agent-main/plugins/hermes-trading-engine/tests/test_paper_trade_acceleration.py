"""100X paper profit-discovery profile + per-lane paper-trade acceleration (PAPER ONLY).

Proves:
* the aggressive-paper proof block is surfaced in trainer status,
* the three tiny paper-learning lanes are accounted for (active-learning tiny
  directional, relaxed Bregman, BTC Pulse),
* a tiny directional exploration trade opens through ALL hard paper gates and is
  excluded from readiness PnL,
* tiny exploration HARD gates (live / stale / missing-ask / fake-fill / synthetic /
  failed-RiskEngine) still block, and
* a zero-trade run reports LANE-SPECIFIC blockers (never a single generic message).
No live trading is ever reachable.
"""

from __future__ import annotations

from pathlib import Path

from engine.markets import universe_manager as um
from engine.training import PolymarketPaperTrainer, TrainingConfig
from engine.training.feedback_accelerator import (SoftGates, TINY_EXPLORATION_TRADE,
                                                  tiny_exploration_gate)

from tests._pmtrain_helpers import clean_live_env, market

_NOW = 1_000_000.0
_PLUGIN = Path(__file__).resolve().parents[1]
_SG = SoftGates(0.03, 0.8, 0.03, -0.02, 0.5, -0.02)


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    cfg.setdefault("max_open_trades", 8)
    return PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", **cfg), data_dir=tmp_path)


def _rec(mid="m0", *, depth=4000, spread=0.02, ask=0.40, fresh=True):
    raw = market(0, bid=round(ask - spread, 4), ask=ask, depth=depth, now=_NOW)
    raw["id"] = mid
    rec = um.MarketRecord.from_raw(raw, now=_NOW)
    rec.market_id = mid
    rec.group_key = "event:" + mid
    if not fresh:
        rec.book_age_s = 9999.0
    return rec


# --------------------------------------------------------------------------- #
# 1) proof block surfaced in status
# --------------------------------------------------------------------------- #
def test_status_includes_aggressive_proof_and_acceleration(tmp_path, monkeypatch):
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    monkeypatch.setenv("FEEDBACK_ACCELERATOR_ENABLED", "1")
    monkeypatch.setenv("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", "100")
    t = _trainer(tmp_path, monkeypatch)
    st = t.status()
    assert st["aggressive_paper"]["aggressive_paper_training_enabled"] is True
    assert st["aggressive_paper"]["feedback_accelerator_target_multiplier"] == 100
    assert st["aggressive_paper"]["real_execution_possible"] is False
    acc = st["paper_trade_acceleration"]
    for k in ("aggressive_paper_training_enabled", "feedback_accelerator_enabled",
              "feedback_accelerator_target_multiplier",
              "paper_profit_discovery_profile_enabled", "real_execution_possible",
              "live_flags_forced_off", "active_learning_tiny_trades_selected",
              "active_learning_tiny_trades_opened",
              "active_learning_tiny_trades_blocked_by_reason",
              "relaxed_bregman_trades_opened", "btc_pulse_paper_trades_opened",
              "exploration_pnl", "readiness_pnl_excludes_exploration",
              "bregman_blocker", "relaxed_bregman_blocker", "tiny_directional_blocker",
              "btc_pulse_blocker", "paper_trade_acceleration_blocker_if_any"):
        assert k in acc, k


# --------------------------------------------------------------------------- #
# 2) lane-specific blockers when zero trades (never one generic message)
# --------------------------------------------------------------------------- #
def test_zero_trade_lane_specific_blockers(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    acc = t.paper_trade_acceleration_report()
    assert acc["active_learning_tiny_trades_opened"] == 0
    assert acc["relaxed_bregman_trades_opened"] == 0
    assert acc["btc_pulse_paper_trades_opened"] == 0
    # every lane gets its OWN blocker
    assert acc["tiny_directional_blocker"]
    assert acc["btc_pulse_blocker"] == "btc_pulse_disabled"
    blk = acc["paper_trade_acceleration_blocker_if_any"]
    assert blk and "bregman=" in blk and "relaxed_bregman=" in blk \
        and "tiny_directional=" in blk and "btc_pulse=" in blk
    # NOT a single generic no-positive-after-cost message
    assert blk != "no positive after-cost Bregman"
    assert acc["readiness_pnl_excludes_exploration"] is True


# --------------------------------------------------------------------------- #
# 3) tiny directional exploration trade opens through ALL hard gates
# --------------------------------------------------------------------------- #
def test_tiny_directional_trade_opens_and_excluded_from_readiness(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch, exploration_enabled=True,
                 active_learning_enabled=True, min_net_edge=0.9,
                 exploration_notional_usd=1.0)
    t._begin_directional_phase(dir_slots_before=0, bregman_opened=0)
    t._begin_exploration_phase()
    # The ActiveLearningSelector decision is unit-tested elsewhere; here we force an
    # "explore" admit so the REAL hard-gate open path (realism + RiskEngine +
    # PaperBroker) is exercised on a clean fresh book.
    monkeypatch.setattr(t, "_active_learning_admit",
                        lambda *a, **k: {"decision": "explore",
                                         "learning_bucket": "active_learning",
                                         "reason": "edge_too_low"})
    res = t._consider(_rec(), _NOW)
    assert res.get("opened") is True, res
    assert t._tiny_directional_selected == 1
    assert t._tiny_directional_opened == 1
    # the opened position is tagged exploration and excluded from readiness PnL
    expl = [p for p in t.positions if p.exploration]
    assert expl and expl[0].is_realistic            # realistic fill, but exploration
    acc = t.paper_trade_acceleration_report()
    assert acc["active_learning_tiny_trades_opened"] >= 1
    assert acc["tiny_directional_blocker"] == ""    # lane opened -> no blocker
    pr = t.paper_realism_report()
    assert pr["readiness_pnl"] == 0.0               # exploration never feeds readiness


# --------------------------------------------------------------------------- #
# 4) tiny exploration HARD gates still block (never loosened)
# --------------------------------------------------------------------------- #
def test_tiny_exploration_hard_gates_block():
    base = dict(fresh_book=True, valid_token=True, has_price=True, risk_ok=True,
                realistic_fill_ok=True, exploration_daily_loss_ok=True, edge=0.0,
                confidence=0.6, after_cost_ev=0.0, exposure_ok=True, soft_gates=_SG)
    cases = {
        "live_blocked": {"live_blocked": True},                  # live mode
        "no_fresh_book": {"fresh_book": False},                  # stale book
        "missing_price": {"has_price": False},                   # missing ask
        "realistic_fill_rejected": {"realistic_fill_ok": False},  # fake/reference fill
        "risk_rejected": {"risk_ok": False},                     # failed RiskEngine
        "settlement_ambiguous": {"ambiguity_score": 0.9},        # synthetic/ambiguous NO
    }
    for expected, over in cases.items():
        res = tiny_exploration_gate(**{**base, **over})
        assert res["allowed"] is False
        assert res["reason"] == expected
        assert res["hard_gate_block"] is True


def test_tiny_exploration_allows_when_all_gates_pass():
    res = tiny_exploration_gate(
        fresh_book=True, valid_token=True, has_price=True, risk_ok=True,
        realistic_fill_ok=True, exploration_daily_loss_ok=True, edge=0.0,
        confidence=0.6, after_cost_ev=0.0, exposure_ok=True, soft_gates=_SG)
    assert res["allowed"] is True
    assert res["decision_class"] == TINY_EXPLORATION_TRADE


# --------------------------------------------------------------------------- #
# 5) docker-compose boots the 100X profile by default for hermes-training
# --------------------------------------------------------------------------- #
def test_compose_defaults_aggressive_and_100x():
    txt = (_PLUGIN / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'AGGRESSIVE_PAPER_TRAINING: "${AGGRESSIVE_PAPER_TRAINING:-1}"' in txt
    assert 'FEEDBACK_ACCELERATOR_ENABLED: "${FEEDBACK_ACCELERATOR_ENABLED:-1}"' in txt
    # hard-pinned to 100 (NOT ${...:-100}, so a project .env=10 can't win interpolation)
    assert 'FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER: "100"' in txt


# --------------------------------------------------------------------------- #
# 6) VPS aggressive paper profile actually turns the 100X learning posture ON
# --------------------------------------------------------------------------- #
def test_vps_aggressive_profile_enables_learning():
    cfg = TrainingConfig.aggressive_paper()
    assert cfg.active_learning_enabled is True
    assert cfg.exploration_enabled is True
    assert cfg.feedback_accelerator_enabled is True
    assert cfg.exploration_tiny_size_enabled is True
    assert cfg.mode == "paper_train"
    # high discovery throughput is set explicitly by the profile (no accel override
    # that would clobber an explicit scan_limit)
    assert cfg.scan_limit >= 2000 and cfg.shortlist_limit >= 200


def test_acceleration_report_proves_100x_active_under_vps_env(tmp_path, monkeypatch):
    # VPS env seeded by the hermes-training entrypoint before config init.
    for k, v in (("AGGRESSIVE_PAPER_TRAINING", "1"), ("FEEDBACK_ACCELERATOR_ENABLED", "1"),
                 ("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", "100"),
                 ("HERMES_ACCELERATED_DISCOVERY", "1"),
                 ("POLYMARKET_ACTIVE_LEARNING_ENABLED", "1"),
                 ("POLYMARKET_EXPLORATION_ENABLED", "1"),
                 ("EXPLORATION_TINY_SIZE_ENABLED", "1"),
                 ("PAPER_PROFIT_DISCOVERY_PROFILE", "1")):
        monkeypatch.setenv(k, v)
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    t = PolymarketPaperTrainer(TrainingConfig.aggressive_paper(), data_dir=tmp_path)
    acc = t.paper_trade_acceleration_report()
    assert acc["aggressive_paper_training_enabled"] is True
    assert acc["feedback_accelerator_enabled"] is True
    assert acc["feedback_accelerator_target_multiplier"] == 100   # 100X (env-sourced proof)
    assert acc["paper_profit_discovery_profile_enabled"] is True
    assert acc["active_learning_enabled"] is True                 # config posture reached
    assert acc["accelerated_discovery_enabled"] is True           # HERMES env resolved
    assert acc["real_execution_possible"] is False
    assert acc["live_flags_forced_off"] is True
    # requested 100X target + SEPARATE effective capacity cap are both surfaced (honest)
    assert acc["feedback_accelerator_requested_multiplier"] == 100
    assert acc["feedback_accelerator_effective_capacity_cap"] == 100
    assert acc["feedback_accelerator_effective_capacity_multiplier"] == 100


def test_vps_100x_profile_resolves_effective_runtime_config(monkeypatch):
    """The 100X VPS profile must RESOLVE the effective runtime values the report checks:
    accelerated discovery + active learning ON, multiplier 100, and the SOFT tiny-
    exploration selection gates loosened — while the hard order-notional cap stays <= $2."""
    for k, v in (("HERMES_ACCELERATED_DISCOVERY", "1"),
                 ("POLYMARKET_EXPLORATION_RATE", "1.0"),
                 ("POLYMARKET_EXPLORATION_MIN_EDGE", "-0.15"),
                 ("POLYMARKET_ACTIVE_LEARNING_TINY_TRADES_PER_TICK", "5"),
                 ("POLYMARKET_EXPLORATION_MAX_TRADES_PER_TICK", "5"),
                 ("POLYMARKET_EXPLORATION_MAX_EXPECTED_LOSS_USD", "0.50"),
                 ("POLYMARKET_EXPLORATION_NOTIONAL_USD", "1"),
                 ("PAPER_MAX_ORDER_NOTIONAL_USD", "2")):
        monkeypatch.setenv(k, v)
    cfg = TrainingConfig.aggressive_paper()
    # the six required effective flags
    assert cfg.active_learning_enabled is True
    assert cfg.accelerated_discovery_enabled is True
    assert cfg.feedback_accelerator_enabled is True
    assert cfg.exploration_enabled is True
    # SOFT tiny-exploration selection gates (loosened, env-tunable)
    assert cfg.exploration_rate == 1.0
    assert cfg.exploration_min_edge == -0.15
    assert cfg.active_learning_tiny_trades_per_tick == 5
    assert cfg.exploration_max_trades_per_tick == 5
    assert cfg.exploration_max_expected_loss_usd == 0.50
    assert cfg.exploration_notional_usd == 1.0
    # HARD tiny cap stays small (NEVER loosened)
    assert cfg.max_order_notional_usd <= 2.0


def test_100x_profile_does_not_loosen_hard_caps_or_bregman(monkeypatch):
    """Loosening SOFT selection gates must not raise the hard order-notional cap above
    the tiny ceiling, and must not touch Bregman after-cost positivity / paper-realism."""
    monkeypatch.setenv("PAPER_MAX_ORDER_NOTIONAL_USD", "999")     # attempt to over-loosen
    monkeypatch.setenv("POLYMARKET_EXPLORATION_NOTIONAL_USD", "999")
    cfg = TrainingConfig.aggressive_paper()
    assert cfg.max_order_notional_usd <= 2.0                     # clamped small, never 999
    assert cfg.exploration_notional_usd <= cfg.max_order_notional_usd
    # hard paper-realism invariants remain reasserted by __post_init__
    assert cfg.exploration_can_bypass_hard_gate is False
    assert cfg.exploration_requires_realistic_fill is True
    assert cfg.exploration_requires_risk_gate is True
    assert cfg.exploration_min_book_freshness_required is True


def test_aggressive_profile_without_hermes_env_keeps_accel_off_and_scan_limit(monkeypatch):
    # A caller that passes an explicit small scan_limit (and no HERMES env) must NOT be
    # auto-bumped — accelerated discovery stays an env opt-in.
    monkeypatch.delenv("HERMES_ACCELERATED_DISCOVERY", raising=False)
    cfg = TrainingConfig.aggressive_paper(scan_limit=25)
    assert cfg.accelerated_discovery_enabled is False
    assert cfg.scan_limit == 25


def test_startup_fails_fast_if_multiplier_not_100_under_profile(tmp_path, monkeypatch):
    """fix #1: if the paper profit-discovery (100X) profile is active but the final
    FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER resolves to something other than 100 (e.g. a
    stale .env=10 that bypassed the force), startup REFUSES with a clear error (rc 2)."""
    import os
    import importlib.util
    import engine.aggressive_paper as ap
    snap = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "start_pp_failfast",
            Path(__file__).resolve().parents[1] / "scripts" / "start_polymarket_paper_training.py")
        starter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(starter)
        # simulate the bug: apply_aggressive_paper_env does NOT force the multiplier,
        # leaving a stale =10 in the container env.
        monkeypatch.setattr(ap, "apply_aggressive_paper_env",
                            lambda env=None: {"locks": [], "defaults_applied": [],
                                              "forced": [], "forbidden_clear": True,
                                              "real_execution_possible": False})
        monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
        monkeypatch.setenv("PAPER_PROFIT_DISCOVERY_PROFILE", "1")
        monkeypatch.setenv("FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER", "10")  # stale, not 100
        monkeypatch.setenv("HTE_MODE", "paper")
        rc = starter.run(["--catalog", "synthetic", "--max-ticks", "1",
                          "--data-dir", str(tmp_path)])
        assert rc == 2                       # refused startup
    finally:
        os.environ.clear()
        os.environ.update(snap)


def test_active_learning_enabled_true_under_env(tmp_path, monkeypatch):
    """When POLYMARKET_ACTIVE_LEARNING_ENABLED=1 (and the aggressive profile is used),
    BOTH the active-learning report and the acceleration report agree active learning is
    enabled — no more active_learning_enabled=false while candidates are selected."""
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    monkeypatch.setenv("POLYMARKET_ACTIVE_LEARNING_ENABLED", "1")
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    t = PolymarketPaperTrainer(TrainingConfig.aggressive_paper(), data_dir=tmp_path)
    al = t.active_learning_report()
    assert al["active_learning_enabled"] is True
    assert al["active_learning_runtime_enabled"] is True
    assert al["active_learning_config_source"] == "aggressive_paper_profile"
    # explicit tiny-lane metrics present (the 6 required keys)
    for k in ("active_learning_tiny_evaluator_called", "active_learning_tiny_trades_selected",
              "active_learning_tiny_trades_opened", "active_learning_tiny_blocked_by_reason"):
        assert k in al
    acc = t.paper_trade_acceleration_report()
    assert acc["active_learning_runtime_enabled"] is True
    assert "active_learning_tiny_evaluator_called" in acc
    # truth-chain: aggressive profile is self-consistent (no mismatch) + reconciliation
    assert al["active_learning_config_mismatch"] is False
    assert al["active_learning_config_mismatch_reason"] == ""
    assert al["active_learning_tiny_candidates_evaluated"] == \
        al["active_learning_tiny_evaluator_called"]
    assert al["active_learning_selected_but_not_evaluated_count"] == 0


def test_active_learning_config_mismatch_when_declared_but_disabled(tmp_path, monkeypatch):
    """STALE-container truth-chain guard: the aggressive_paper profile ALWAYS enables
    active learning, so config_source=aggressive_paper_profile with an effective
    active_learning_enabled=False is a config_mismatch (the running container is stale)."""
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    clean_live_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")          # declared aggressive...
    # ...but the effective config has active learning OFF (the stale-image symptom)
    t = PolymarketPaperTrainer(
        TrainingConfig(mode="paper_train", active_learning_enabled=False), data_dir=tmp_path)
    al = t.active_learning_report()
    assert al["active_learning_config_source"] == "aggressive_paper_profile"
    assert al["active_learning_enabled"] is False
    assert al["active_learning_config_mismatch"] is True
    assert "STALE" in al["active_learning_config_mismatch_reason"]
    acc = t.paper_trade_acceleration_report()
    assert acc["active_learning_config_mismatch"] is True


def test_selected_tiny_candidate_reaches_evaluator_and_blocks_exactly(tmp_path, monkeypatch):
    """A selected active-learning candidate enters the tiny evaluator; if it can't open it
    records an EXACT canonical blocker (never generic 'open_rejected')."""
    t = _trainer(tmp_path, monkeypatch, exploration_enabled=True, active_learning_enabled=True,
                 min_net_edge=0.9, exploration_notional_usd=1.0)
    t._begin_directional_phase(dir_slots_before=0, bregman_opened=0)
    t._begin_exploration_phase()
    monkeypatch.setattr(t, "_active_learning_admit",
                        lambda *a, **k: {"decision": "explore", "learning_bucket": "al",
                                         "reason": "edge_too_low"})
    # force the open to be REJECTED by the RiskEngine so we exercise the blocker path
    monkeypatch.setattr(t.risk, "evaluate",
                        lambda *a, **k: __import__("types").SimpleNamespace(
                            approved=False, risk_decision_id="r1", proposal_id=""))
    res = t._consider(_rec(), _NOW)
    assert res.get("opened") is not True
    assert t._tiny_directional_evaluator_called == 1
    assert t._tiny_directional_opened == 0
    blocked = t._tiny_directional_blocked
    assert "open_rejected" not in blocked                  # never the vague reason
    assert blocked.get("risk_rejected", 0) == 1            # exact canonical reason


def test_canonical_tiny_block_reason_units():
    from engine.training.polymarket_trainer import _canonical_tiny_block_reason as f
    assert f({"reason": "risk_rejected"}) == "risk_rejected"
    assert f({"reason": "paperbroker_rejected"}) == "broker_error"
    assert f({"reason": "shadow_stale_book", "execution_realism_status": "shadow_stale"}) == "stale_book"
    assert f({"reason": "shadow_missing_executable_ask"}) == "missing_ask"
    assert f({"shadow_only": True, "reason": "shadow_theoretical_only"}) == "fill_not_realistic"
    assert f({"reason": "variant_budget_exhausted"}) == "budget_blocked"


def test_relaxed_bregman_open_reject_is_exact_not_generic(tmp_path, monkeypatch):
    """For a tradable relaxed candidate that fails to open, the reject reason is the EXACT
    _open_bregman reason (e.g. bregman_leg_stale_book), not generic 'open_rejected'."""
    import engine.training.relaxed_candidates as rc
    t = _trainer(tmp_path, monkeypatch, paper_micro_exploration_enabled=True,
                 paper_relaxed_exploration_enabled=True)

    # one tradable relaxed candidate, but _open_bregman rejects with an exact reason
    fake = {"group_id": "g1", "group_type": "binary_yes_no", "gate_result": rc.GATE_TRADABLE,
            "after_cost_edge": 0.02, "is_real_book": True}
    monkeypatch.setattr(rc, "evaluate_relaxed_group", lambda g, **k: dict(fake))
    monkeypatch.setattr(rc, "summarize", lambda recs: {
        "pipeline_scanned": 1, "real_book_candidates_seen": 1,
        "positive_real_book_candidates_seen": 1, "tradable_candidates": 1,
        "blocked_by_reason": {}, "source_counts": {"binary_yes_no": 1},
        "best_real_book_candidate": dict(fake), "best_reject_example": {},
        "best_after_cost_edge": 0.02})

    def _fake_open(opp, rec_by_id, now, *, exploration=False, notional_cap=None):
        t._breg_reason("bregman_leg_stale_book")           # exact reason recorded
        return False
    monkeypatch.setattr(t, "_open_bregman", _fake_open)
    monkeypatch.setattr(t, "_relaxed_opportunity_from_group",
                        lambda g: __import__("types").SimpleNamespace(has_synthetic_leg=False))
    t._bregman_hydrated_groups = [__import__("types").SimpleNamespace(group_id="g1")]
    t._run_micro_exploration([], _NOW)
    reasons = t._micro_exploration_reject_reasons
    assert "open_rejected" not in reasons                  # not the generic bucket
    assert reasons.get("bregman_leg_stale_book", 0) >= 1   # exact reason


def test_grok_news_not_disabled_when_env_enabled(tmp_path, monkeypatch):
    """NEWS_SCANNER_ENABLED=1 must NOT report grok blocker 'news_scanner_disabled'."""
    monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
    monkeypatch.setenv("NEWS_SCANNER_ENABLED", "1")
    monkeypatch.setenv("NEWS_PROVIDER_MODE", "live_read_only")
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    monkeypatch.setenv("XAI_API_KEY", "x" * 84)
    clean_live_env(monkeypatch, tmp_path)
    for k, v in (("AGGRESSIVE_PAPER_TRAINING", "1"), ("NEWS_SCANNER_ENABLED", "1"),
                 ("NEWS_PROVIDER_MODE", "live_read_only"), ("RESEARCH_MODE", "online_paper"),
                 ("XAI_API_KEY", "x" * 84)):
        monkeypatch.setenv(k, v)
    t = PolymarketPaperTrainer(TrainingConfig.aggressive_paper(), data_dir=tmp_path)
    rs = t.research_status()
    assert rs.get("grok_zero_call_reason") != "news_scanner_disabled"
    assert rs.get("grok_brain_blocker") != "news_scanner_disabled"
    # precise reason from the allowed set, and the key is never leaked
    assert rs.get("grok_zero_call_reason") in (
        None, "scanner_not_started", "no_news_packet_available", "not_due_yet",
        "proof_call_disabled_by_config")
    assert "x" * 84 not in str(rs)


def test_aggressive_profile_env_fails_closed_on_live_flag():
    # The VPS entrypoint seeds the 100X profile then applies the paper-only lock; a
    # live/real-money flag must make activation FAIL CLOSED (never start).
    import pytest
    from engine.aggressive_paper import AggressivePaperUnsafe, apply_aggressive_paper_env
    env = {"AGGRESSIVE_PAPER_TRAINING": "1", "FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER": "100",
           "BTC_AUTOTRADE_ENABLED": "1"}     # a live flag is on
    with pytest.raises(AggressivePaperUnsafe):
        apply_aggressive_paper_env(env)


def test_startup_applies_aggressive_env_before_config_build(tmp_path, monkeypatch):
    """fix #1: with AGGRESSIVE_PAPER_TRAINING=1, the hermes-training startup runs
    apply_aggressive_paper_env() (which sets HERMES_ACCELERATED_DISCOVERY + locks)
    BEFORE the TrainingConfig is built."""
    import os
    import importlib.util
    import pytest
    import engine.aggressive_paper as ap
    from engine.training import TrainingConfig
    snap = dict(os.environ)                          # restore env (run() writes os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "start_pp_test",
            Path(__file__).resolve().parents[1] / "scripts" / "start_polymarket_paper_training.py")
        starter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(starter)
        order: list = []
        orig_apply = ap.apply_aggressive_paper_env

        def _spy_apply(env=None):
            order.append("apply")
            return orig_apply(env)
        monkeypatch.setattr(ap, "apply_aggressive_paper_env", _spy_apply)

        class _StopBuild(RuntimeError):
            pass

        def _spy_cfg(cls, **ov):
            order.append("config")
            raise _StopBuild()
        monkeypatch.setattr(TrainingConfig, "aggressive_paper", classmethod(_spy_cfg))
        monkeypatch.setenv("AGGRESSIVE_PAPER_TRAINING", "1")
        monkeypatch.setenv("HTE_MODE", "paper")
        with pytest.raises(_StopBuild):
            starter.run(["--catalog", "synthetic", "--max-ticks", "1",
                         "--data-dir", str(tmp_path)])
        assert order[0] == "apply" and "config" in order
        assert order.index("apply") < order.index("config")
        # apply_aggressive_paper_env set the accelerated-discovery env before config
        assert os.environ.get("HERMES_ACCELERATED_DISCOVERY") == "1"
    finally:
        os.environ.clear()
        os.environ.update(snap)
