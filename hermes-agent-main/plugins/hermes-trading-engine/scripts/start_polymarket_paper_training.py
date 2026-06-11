#!/usr/bin/env python3
"""Start (or confirm) the Polymarket-only PAPER training engine.

SAFETY: refuses to start if ANY live-execution flag is detected (Micro Live,
production execution, guarded-live, or arbitrage). PAPER ONLY — it can never
place a real order, and Grok stays research-only.

Examples:
    python scripts/start_polymarket_paper_training.py --dry-run
    python scripts/start_polymarket_paper_training.py --minutes 60 --tick-seconds 30 --catalog gamma --realtime
    python scripts/start_polymarket_paper_training.py --max-ticks 5 --catalog synthetic
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env/.env.env into the process up front so the Grok key (XAI_API_KEY/
# GROK_API_KEY) + paper config apply even when the file was saved as ".env.env"
# (docker-compose only auto-loads ".env"). Live trading is force-pinned OFF by the
# loader, and the preflight below still hard-blocks any live flag.
try:
    if "pytest" not in sys.modules:         # never mutate env during the test suite
        from engine.env_loader import (load_local_env as _load_local_env,
                                        enable_grok_research_if_key_present as _grok_on,
                                        grok_key_present as _grok_key)
        _load_local_env()
        # "key in .env" => turn xAI/Grok research ON (research-only online_paper) when
        # RESEARCH_MODE was not explicitly set. NEVER enables live trading.
        _grok_mode = _grok_on()
        print(f"xAI/Grok research: key_present={_grok_key()} research_mode={_grok_mode or 'offline_cache'} "
              f"(research-only; live trading stays OFF)")
except Exception:  # noqa: E402,BLE001
    pass

# Surface INFO logs (feature-health proof: Chainlink/news/Bregman/oracle gate) in
# Docker logs. Without this, library logger.info() lines are suppressed at the
# default WARNING level and the startup proof lines never appear.
logging.basicConfig(
    level=os.getenv("HTE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s")

from engine.training import PolymarketPaperTrainer, TrainingConfig  # noqa: E402
from engine.training.polymarket_trainer import FORBIDDEN_LIVE_FLAGS, _envb  # noqa: E402
from engine.training.reports import write_reports  # noqa: E402


def preflight() -> dict:
    """Strict live-trading preflight (stricter than the trainer's runtime gate:
    also refuses if the deprecated arbitrage flag is set on)."""
    checks = {}
    for flag in FORBIDDEN_LIVE_FLAGS:
        checks[f"{flag}_off"] = not _envb(flag, False)
    checks["arbitrage_flag_off"] = not _envb("ARB_EXECUTION_ENABLED", False)
    try:
        from engine.arb.execution import ARBITRAGE_PERMANENTLY_DISABLED
        checks["arbitrage_permanently_disabled"] = bool(ARBITRAGE_PERMANENTLY_DISABLED)
    except Exception:  # noqa: BLE001
        checks["arbitrage_permanently_disabled"] = True
    checks["mode_is_paper"] = (os.getenv("HTE_MODE", "paper").lower() == "paper")
    return {"ok": all(checks.values()), "checks": checks}


def _synthetic_catalog(n: int = 60) -> list:
    now = time.time()
    out = []
    for i in range(n):
        bid, ask = 0.40, 0.42
        out.append({
            "id": f"pm{i}", "question": f"Will synthetic event {i} resolve YES?",
            "active": True, "closed": False, "archived": False,
            "enableOrderBook": True, "acceptingOrders": True,
            "clobTokenIds": [f"tok{i}a", f"tok{i}b"],
            "outcomePrices": [str((bid + ask) / 2), str(1 - (bid + ask) / 2)],
            "bestBid": bid, "bestAsk": ask, "spread": round(ask - bid, 4),
            "liquidityNum": 20000, "volume24hr": 8000, "topDepthUsd": 1000,
            "volumeNum": 40000, "endDate": "2030-01-01T00:00:00Z",
            "description": "Resolves YES per official sources by the end date. " * 6,
            "category": ["politics", "sports", "crypto-news", "econ"][i % 4],
            "bookUpdatedTs": now})
    return out


_BREGMAN_REPAIR_HINTS = {
    "non_numeric_price": "outcomePrices contained a non-numeric string; ensure "
                         "$/% formats are parsed (constraint_graph._to_float).",
    "insufficient_outcomes": "market exposed <2 outcome prices; needs a 2+ outcome "
                             "complement/MECE group.",
    "malformed_group": "same-event cluster had <2 usable outcomes after normalization.",
    "insufficient_metadata": "event cluster had no group_kind/negRisk; cannot prove "
                             "a constraint without inventing one.",
    "no_orderbook": "enableOrderBook=false; no executable book to certify.",
    "no_depth": "no top-of-book depth and no liquidity fallback.",
    "missing_quotes": "bestBid/bestAsk missing and reference quote disallowed.",
    "degenerate_price": "price outside (0,1); rejected as impossible.",
    "market_inactive": "market not active/closed/archived.",
}


def _bregman_repair_hint(reason) -> str:
    return _BREGMAN_REPAIR_HINTS.get(str(reason or ""), "inspect adapter normalization "
                                     "for this skip reason.")


def clear_stale_stop_sentinel(stop_path, keep: bool = False) -> bool:
    """Clear a STALE stop sentinel on an explicit start.

    Without this, a leftover ``polymarket_training.stop`` in the persisted data
    volume (from a previous ``stop`` or run) makes a fresh start exit at tick 0;
    under ``restart: unless-stopped`` that becomes an infinite restart loop.
    Returns True if a sentinel was cleared. PAPER ONLY — only clears a stop flag
    at startup; never enables live trading. A ``stop`` issued AFTER startup still
    writes the sentinel and the run loop honours it."""
    try:
        exists = stop_path.exists()
    except OSError:  # noqa: BLE001
        return False
    if keep or not exists:
        return False
    try:
        stop_path.unlink()
        print(f"cleared stale stop sentinel ({stop_path}) — explicit start overrides it.")
        return True
    except OSError as exc:  # noqa: BLE001 — best effort; never block startup
        print(f"warning: could not remove stale stop sentinel {stop_path}: {exc}")
        return False


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start the Polymarket PAPER training engine (PAPER ONLY).")
    ap.add_argument("--minutes", type=float, default=0.0, help="wall-clock minutes to run (0 = use --max-ticks)")
    ap.add_argument("--tick-seconds", type=float, default=30.0, help="seconds between training ticks")
    ap.add_argument("--max-ticks", type=int, default=1, help="max ticks when --minutes is 0")
    ap.add_argument("--catalog", choices=["gamma", "synthetic"], default="synthetic",
                    help="gamma = live Polymarket catalog (network); synthetic = offline deterministic")
    ap.add_argument("--from-json", default=None, help="path to a JSON catalog (offline)")
    ap.add_argument("--realtime", action="store_true", help="sleep between ticks (live loop)")
    ap.add_argument("--dry-run", action="store_true", help="preflight + 1 synthetic tick + report, then exit")
    ap.add_argument("--keep-stop-sentinel", action="store_true",
                    help="do NOT clear a pre-existing stop sentinel on start (legacy "
                         "behavior; by default an explicit start clears a stale "
                         "/data/polymarket_training.stop so it doesn't exit at tick 0)")
    ap.add_argument("--report", action="store_true", help="write a training report when finished")
    ap.add_argument("--probability-ensemble", action="store_true",
                    help="opt in to the calibrated probability ensemble + calibration "
                         "rollback guardrails (engine.models.probability_ensemble). "
                         "Sets PROBABILITY_ENSEMBLE_ENABLED=1 for the calibration layer; "
                         "default OFF preserves current probability behavior. PAPER ONLY.")
    ap.add_argument("--bregman-primary", action="store_true",
                    help="opt in to Bregman coherence arbitrage as the primary strategy "
                         "(engine.strategies.bregman): project market probs, certify a "
                         "cost/depth-aware worst-case profit, trade ONLY certified "
                         "opportunities. Sets BREGMAN_PRIMARY_STRATEGY=1; default OFF "
                         "preserves current behavior. PAPER ONLY.")
    ap.add_argument("--strategy-router", action="store_true",
                    help="opt in to the tiered strategy router (engine.strategies.router): "
                         "Tier 1 certified Bregman arbitrage > Tier 2 stale-crypto/Chainlink "
                         "BTC dislocation > Tier 3 calibrated model edge > Tier 4 "
                         "exploration-only tiny. Blocks BTC Pulse on unknown/chop regime, "
                         "negative after-cost EV, or weak fill realism; learns EV cutoffs and "
                         "separates exploration PnL from validation PnL. Sets "
                         "STRATEGY_ROUTER_ENABLED=1; default OFF preserves current behavior. "
                         "PAPER ONLY.")
    ap.add_argument("--robustness-validation", action="store_true",
                    help="opt in to institutional robustness validation for paper metrics "
                         "(engine.replay.robustness + engine.backtest: walk-forward, "
                         "combinatorial purged CV, bootstrap CIs, ablations, and "
                         "Sharpe/Sortino/Calmar significance gates). Reports exploration "
                         "separately from validation + production-readiness. Sets "
                         "ROBUSTNESS_VALIDATION_ENABLED=1; default OFF. PAPER ONLY.")
    ap.add_argument("--mode", choices=["disabled", "observe_only", "paper_train"],
                    default="paper_train", help="training mode (PAPER ONLY either way)")
    ap.add_argument("--data-dir", default=None, help="data dir for status + campaign state")
    # ---- institutional paper-training campaign (PAPER ONLY) ----
    ap.add_argument("--campaign-safe-profile", action="store_true",
                    help="apply the institutional campaign-safe profile: aggressive paper + "
                         "campaign + algorithm freeze + read-only CLOB + read-only Chainlink + "
                         "realistic fills + clean-label guard, with ALL live paths disabled "
                         "and fail-closed. PAPER ONLY.")
    ap.add_argument("--aggressive-paper", action="store_true",
                    help="use the AGGRESSIVE paper-training profile (more paper trades, "
                         "more feedback). Still PAPER ONLY; hard risk caps unchanged.")
    ap.add_argument("--campaign", action="store_true",
                    help="enable campaign mode (durable multi-run evidence collection)")
    ap.add_argument("--campaign-name", default=None, help="campaign name")
    ap.add_argument("--algorithm-freeze", action="store_true",
                    help="freeze algorithm development (no param promotion / threshold relaxation)")
    ap.add_argument("--target-days", type=int, default=None)
    ap.add_argument("--target-decisions", type=int, default=None)
    ap.add_argument("--target-trades", type=int, default=None)
    ap.add_argument("--target-resolved-labels", type=int, default=None)
    ap.add_argument("--target-bregman-candidates", type=int, default=None)
    ap.add_argument("--write-campaign-report", action="store_true",
                    help="write training_campaign.json + .md when finished")
    ap.add_argument("--continue-until-thresholds", action="store_true",
                    help="keep running until campaign thresholds pass, --max-hours is reached, "
                         "or the stop sentinel exists")
    ap.add_argument("--max-hours", type=float, default=336.0,
                    help="max wall-clock hours for --continue-until-thresholds")
    # ---- BTC 5-min Pulse PAPER-ONLY isolated experiment ----
    ap.add_argument("--btc-pulse", action="store_true",
                    help="enable the BTC 5-min Pulse PAPER-ONLY isolated training "
                         "experiment beside Polymarket. Keeps live + legacy BTC "
                         "autotrade disabled and isolated learning on. PAPER ONLY.")
    ap.add_argument("--btc-pulse-paper-only", action="store_true", default=None,
                    help="force BTC Pulse paper-only (already the default)")
    ap.add_argument("--btc-pulse-isolated-learning", action="store_true", default=None,
                    help="force BTC Pulse isolated learning (already the default)")
    ap.add_argument("--btc-pulse-tick-seconds", type=int, default=None,
                    help="BTC Pulse tick seconds (default 30)")
    ap.add_argument("--btc-pulse-round-seconds", type=int, default=None,
                    help="BTC Pulse round seconds (default 300)")
    # ---- 10x Feedback Accelerator (PAPER ONLY) ----
    ap.add_argument("--feedback-accelerator", action="store_true",
                    help="enable the PAPER-ONLY 10x feedback accelerator (more "
                         "decisions, shadow labels, no-trade labels, tiny capped "
                         "exploration). Hard safety gates never loosen.")
    ap.add_argument("--target-feedback-multiplier", type=int, default=None,
                    help="target feedback multiplier (default 10, max 20)")
    ap.add_argument("--tiny-exploration", action="store_true", default=None,
                    help="enable tiny capped exploration trades (paper only)")
    ap.add_argument("--shadow-decisions", action="store_true", default=None,
                    help="log shadow decisions for rejected candidates")
    ap.add_argument("--no-trade-labels", action="store_true", default=None,
                    help="record no-trade decisions as labeled learning samples")
    ap.add_argument("--active-learning", action="store_true", default=None,
                    help="prioritize high learning-value candidates")
    ap.add_argument("--aggressive-paper-training", action="store_true",
                    help="activate AGGRESSIVE_PAPER_TRAINING=1: PAPER-ONLY high-volume "
                         "feedback + ABCAS flagship. Forces paper-only locks and fails "
                         "closed if any real-money flag is on. Real orders stay impossible.")
    args = ap.parse_args(argv)

    # AGGRESSIVE PAPER MODE: apply the named mode + global paper-only safety lock
    # BEFORE anything else. Fail closed if a real-money flag is enabled.
    import os as _os0
    if getattr(args, "aggressive_paper_training", False) \
            or str(_os0.getenv("AGGRESSIVE_PAPER_TRAINING", "")).strip().lower() \
            in ("1", "true", "yes", "on"):
        from engine.aggressive_paper import (AggressivePaperUnsafe,
                                             apply_aggressive_paper_env)
        try:
            _agg = apply_aggressive_paper_env(_os0.environ)
        except AggressivePaperUnsafe as exc:
            print(f"\n\033[91m*** REFUSING aggressive paper mode: {exc} ***\033[0m")
            return 2
        logging.getLogger("hte.training.start").info(
            "AGGRESSIVE_PAPER_TRAINING=1: paper-only locks=%d defaults=%d "
            "real_execution_possible=False (PAPER ONLY)",
            len(_agg["locks"]), len(_agg["defaults_applied"]))
        args.aggressive_paper = True   # drive the aggressive TrainingConfig profile

    pf = preflight()
    print("=" * 64)
    print("Polymarket PAPER Training — preflight")
    for k, v in pf["checks"].items():
        print(f"  {'OK ' if v else 'XX '} {k}: {v}")
    if not pf["ok"]:
        print("\n\033[91m*** REFUSING TO START: live-trading configuration detected. ***\033[0m")
        print("This engine is PAPER ONLY. Disable the flags above and retry.")
        return 2
    print("preflight OK — PAPER ONLY, no real orders, Grok research-only.\n")

    # --aggressive-paper uses the explicit AGGRESSIVE paper profile (not from_env).
    overrides = {}
    if args.campaign:
        overrides["campaign_enabled"] = True
    if args.campaign_name:
        overrides["campaign_name"] = args.campaign_name
    if args.algorithm_freeze:
        overrides["algorithm_freeze_mode"] = True
    if args.target_days is not None:
        overrides["campaign_target_min_days"] = args.target_days
    if args.target_decisions is not None:
        overrides["campaign_target_min_decisions"] = args.target_decisions
    if args.target_trades is not None:
        overrides["campaign_target_min_paper_trades"] = args.target_trades
    if args.target_resolved_labels is not None:
        overrides["campaign_target_min_resolved_labels"] = args.target_resolved_labels
    if args.target_bregman_candidates is not None:
        overrides["campaign_target_min_bregman_candidates"] = args.target_bregman_candidates
    # BTC Pulse overrides (PAPER ONLY). --btc-pulse unfreezes the isolated
    # experiment; live + legacy BTC autotrade stay disabled regardless.
    if args.btc_pulse or _envb("BTC_PULSE_ENABLED", False):
        overrides["btc_pulse_enabled"] = True
        overrides["btc_pulse_paper_only"] = True
        overrides["btc_pulse_isolated_learning"] = True
        overrides["btc_pulse_live_enabled"] = _envb("BTC_PULSE_LIVE_ENABLED", False)
        overrides["btc_pulse_legacy_autotrade_enabled"] = _envb("BTC_AUTOTRADE_ENABLED", False)
    if args.btc_pulse_tick_seconds is not None:
        overrides["btc_pulse_tick_seconds"] = args.btc_pulse_tick_seconds
    if args.btc_pulse_round_seconds is not None:
        overrides["btc_pulse_round_seconds"] = args.btc_pulse_round_seconds
    # Feedback accelerator overrides (PAPER ONLY).
    if args.feedback_accelerator or _envb("FEEDBACK_ACCELERATOR_ENABLED", False):
        overrides["feedback_accelerator_enabled"] = True
        overrides["exploration_enabled"] = True
    if args.target_feedback_multiplier is not None:
        overrides["feedback_accelerator_target_multiplier"] = args.target_feedback_multiplier
    if args.tiny_exploration:
        overrides["exploration_tiny_size_enabled"] = True
        overrides["exploration_enabled"] = True
    if args.shadow_decisions:
        overrides["shadow_decision_logging_enabled"] = True
    if args.no_trade_labels:
        overrides["no_trade_labeling_enabled"] = True
    if args.active_learning:
        overrides["active_learning_enabled"] = True
    if args.campaign_safe_profile:
        # institutional campaign-safe profile implies aggressive paper + campaign.
        overrides["campaign_enabled"] = True
        cfg = TrainingConfig.institutional_campaign_defaults(**overrides)
    elif args.aggressive_paper:
        cfg = TrainingConfig.aggressive_paper(**overrides)
    else:
        cfg = TrainingConfig.from_env()
        for k, v in overrides.items():
            setattr(cfg, k, v)
        cfg.__post_init__()  # re-apply freeze/clamp invariants after overrides

    # Campaign-safe startup safety validation (fail-closed). Runs whenever the
    # safe profile is engaged; refuses to start if any live/unsafe flag is set.
    if args.campaign_safe_profile or cfg.campaign_safe_profile:
        from engine.training.campaign_controller import campaign_safety_check
        safety = campaign_safety_check(cfg)
        print("Campaign-safe profile — resolved safety config:")
        for k in ("campaign_safe_profile", "clob_read_only_enabled",
                  "chainlink_read_only_enabled", "realistic_fill_enabled",
                  "clean_label_guard_enabled", "live_disabled", "micro_live_disabled",
                  "guarded_live_disabled", "btc_autotrade_disabled", "risk_gates_required",
                  "startup_safety_passed"):
            print(f"  {'OK ' if safety.get(k) else 'XX '} {k}: {safety.get(k)}")
        if not safety["passed"]:
            print(f"\n\033[91m*** REFUSING TO START: campaign safety failed "
                  f"({safety['fail_closed_reason']}). ***\033[0m")
            return 2
        print("campaign-safe startup safety PASSED — PAPER ONLY, no live path.\n")

    # BTC 5-min Pulse PAPER-ONLY preflight (fail-closed). Only printed/enforced
    # when the pulse experiment is enabled (via --btc-pulse / BTC_PULSE_ENABLED).
    if bool(getattr(cfg, "btc_pulse_enabled", False)):
        from engine.training.btc_pulse import pulse_preflight
        pf_pulse = pulse_preflight(cfg)
        print("BTC 5-min Pulse — PAPER-ONLY preflight (isolated experiment):")
        for k, v in pf_pulse["resolved"].items():
            print(f"  {k}: {v}")
        print(f"  btc_pulse_status: {pf_pulse['btc_pulse_status']}")
        for k, v in pf_pulse["checks"].items():
            print(f"  {'OK ' if v else 'XX '} {k}: {v}")
        if not pf_pulse["passed"]:
            print(f"\n\033[91m*** REFUSING TO START: BTC Pulse safety failed "
                  f"({pf_pulse['fail_closed_reason']}). ***\033[0m")
            print("BTC Pulse is PAPER ONLY. Disable live/autotrade flags and retry.")
            return 2
        print("BTC Pulse preflight OK — PAPER ONLY, isolated, no live orders, "
              "no legacy autotrade.\n")
    # 10x Feedback Accelerator (PAPER ONLY): raise SOFT capacity knobs (more
    # candidates / decisions per tick) so feedback scales ~target x. Hard risk
    # caps are NEVER touched. Only soft gates relax, and only for tiny exploration.
    if bool(getattr(cfg, "feedback_accelerator_enabled", False)):
        from engine.training.feedback_accelerator import (apply_feedback_accelerator,
                                                          resolve_soft_gates)
        rep = apply_feedback_accelerator(cfg)
        sg = resolve_soft_gates(cfg)
        print("10x Feedback Accelerator — PAPER ONLY (soft gates only; hard gates locked):")
        print(f"  enabled: True · mode: {cfg.feedback_accelerator_mode} · "
              f"target_multiplier: {cfg.feedback_accelerator_target_multiplier}")
        print(f"  exploration: enabled={cfg.exploration_enabled} "
              f"tiny={cfg.exploration_tiny_size_enabled} "
              f"counts_for_readiness={cfg.exploration_counts_for_readiness}")
        print(f"  shadow_decisions={cfg.shadow_decision_logging_enabled} "
              f"no_trade_labels={cfg.no_trade_labeling_enabled} "
              f"active_learning={cfg.active_learning_enabled}")
        if rep.get("applied"):
            print(f"  capacity: decisions/tick {rep['before']['paper_decision_budget']}"
                  f"->{rep['after']['paper_decision_budget']} · candidates "
                  f"{rep['before']['trade_candidate_limit']}->{rep['after']['trade_candidate_limit']} "
                  f"· shortlist {rep['before']['shortlist_limit']}->{rep['after']['shortlist_limit']}")
        print(f"  exploit gates (UNCHANGED): edge>={sg.exploit_min_edge} "
              f"conf>={sg.exploit_min_confidence} · exploration gates (tiny only): "
              f"edge>={sg.exploration_min_edge} conf>={sg.exploration_min_confidence}")
        print("  hard gates LOCKED: no live, RiskEngine required, fresh book + valid "
              "token + realistic fill required, clean-label guard on, exploration "
              "is NOT live-readiness proof until cleanly resolved.\n")
    # Opt-in probability ensemble + calibration guardrails (default OFF; behavior
    # preserved). Exposed via env so the calibration layer can read it without a
    # structural change to the trainer. PAPER ONLY.
    import os as _os
    if getattr(args, "probability_ensemble", False):
        _os.environ["PROBABILITY_ENSEMBLE_ENABLED"] = "1"
    if getattr(args, "bregman_primary", False):
        _os.environ["BREGMAN_PRIMARY_STRATEGY"] = "1"
    if getattr(args, "strategy_router", False):
        _os.environ["STRATEGY_ROUTER_ENABLED"] = "1"
    if getattr(args, "robustness_validation", False):
        _os.environ["ROBUSTNESS_VALIDATION_ENABLED"] = "1"
        logging.getLogger("hte.training.start").info(
            "robustness validation: walk_forward+CPCV+bootstrap_CI+ablations+"
            "significance_gates(Sharpe/Sortino/Calmar); exploration reported "
            "separately from validation + production-readiness")
    logging.getLogger("hte.training.start").info(
        "algorithmic edge audit: generated reports include a mandatory decision-grade "
        "audit (strategy attribution, Bregman/BTC-Pulse/calibration/fill-realism/risk "
        "diagnostics, readiness) that fails loudly on missing/stale core fields")
    logging.getLogger("hte.training.start").info(
        "modeling config: probability_ensemble=%s bregman_primary=%s strategy_router=%s "
        "calibration=auto(Platt/isotonic/temperature/shrink) rollback_guard=available "
        "conformal_bands=available grok_news=evidence_only "
        "bregman=certify_before_trade",
        "on" if getattr(args, "probability_ensemble", False) else "off",
        "on" if getattr(args, "bregman_primary", False) else "off",
        "on" if getattr(args, "strategy_router", False) else "off")
    if getattr(args, "strategy_router", False):
        logging.getLogger("hte.training.start").info(
            "strategy router tiers: 1=certified_bregman > 2=stale_crypto/chainlink_btc_"
            "dislocation > 3=calibrated_model_edge > 4=exploration_only_tiny; "
            "btc_pulse blocked on unknown/chop|negative_after_cost_ev|weak_fill_realism; "
            "exploration_pnl separated from validation_pnl; news/grok=evidence_weight_only")
    cfg.mode = args.mode  # start-paper explicitly drives paper training
    data_dir = Path(args.data_dir) if args.data_dir else None
    # Apply persisted dashboard control overrides BEFORE constructing the trainer
    # so on/off toggles survive restarts (PAPER ONLY; never enables a live path).
    from engine import control as _control
    _ov_dir = data_dir if data_dir is not None else Path(
        _os.getenv("HTE_DATA_DIR", "."))
    _startup_overrides = _control.read_overrides(_ov_dir)
    _applied = _control.apply_to_config(cfg, _startup_overrides)
    if _applied:
        logging.getLogger("hte.training.start").info(
            "applied control overrides at startup: %s", _applied)
    trainer = PolymarketPaperTrainer(cfg, data_dir=data_dir)
    # READ-ONLY CLOB order-book hydration: fill Bregman binary YES/NO groups with REAL
    # YES+NO books (best bid/ask + side depth + book age) before certification so a
    # real NO-token ask replaces the synthetic 1-YES-bid (which stays diagnostic only).
    # ON by default whenever CLOB read-only is enabled; disable with
    # BREGMAN_CLOB_HYDRATION_ENABLED=0. PAPER ONLY: read-only GETs, never trades/sizes.
    _hyd_disabled = str(os.getenv("BREGMAN_CLOB_HYDRATION_ENABLED", "")).strip().lower() \
        in ("0", "false", "no", "off")
    _clob_ro = bool(getattr(cfg, "clob_enabled", True)) and bool(getattr(cfg, "clob_read_only", True))
    if _clob_ro and not _hyd_disabled:
        try:
            _hyd_on = trainer.enable_clob_hydration()
        except Exception as exc:  # noqa: BLE001 — never block startup on hydration wiring
            _hyd_on = False
            logging.getLogger("hte.training.start").warning("clob hydration wiring failed: %s", exc)
        logging.getLogger("hte.training.start").info(
            "bregman CLOB order-book hydration: enabled=%s (read-only /book per token; "
            "real NO ask preferred; synthetic stays diagnostic only)", _hyd_on)
    else:
        logging.getLogger("hte.training.start").info(
            "bregman CLOB order-book hydration: enabled=False (clob_read_only=%s disabled=%s)",
            _clob_ro, _hyd_disabled)
    dd = trainer.data_dir
    stop_path = dd / "polymarket_training.stop"
    # An explicit start overrides any stale stop sentinel left in the data volume
    # so `docker compose up` doesn't immediately exit at tick 0 and loop under
    # restart: unless-stopped. PAPER ONLY (see clear_stale_stop_sentinel).
    clear_stale_stop_sentinel(stop_path, keep=getattr(args, "keep_stop_sentinel", False))
    _profile_name = ("campaign_safe" if (args.campaign_safe_profile or cfg.campaign_safe_profile)
                     else ("aggressive" if args.aggressive_paper else "default"))
    print(f"mode: {cfg.mode} (PAPER ONLY)"
          + (f" · profile: {_profile_name}")
          + (" · CAMPAIGN" if cfg.campaign_enabled else "")
          + (" · ALGORITHM FROZEN" if cfg.algorithm_freeze_mode else "")
          + (" · BTC PULSE (paper, isolated)" if cfg.btc_pulse_enabled else ""))
    if cfg.btc_pulse_enabled:
        print("  BTC Pulse: enabled=true frozen=false paper_only=true "
              "isolated_learning=true live_enabled=false legacy_autotrade=false")
        logging.getLogger("hte.training.start").info(
            "btc_pulse after-cost shadow gate ON: trades only on classified regime + "
            "positive after-cost EV + explainable disagreement + fresh market + fill "
            "realism + non-degrading calibration; else shadow-only. Bregman stays Tier 1.")
    # double-check the trainer's own runtime gate agrees
    if not trainer.preflight()["ok"]:
        print("\033[91m*** REFUSING: trainer preflight failed. ***\033[0m")
        return 2

    def provider():
        if args.from_json:
            return json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        if args.catalog == "gamma":
            try:
                return trainer.scanner.fetch()
            except Exception as exc:  # noqa: BLE001
                print(f"  (gamma fetch failed: {exc}; falling back to synthetic)")
                return _synthetic_catalog()
        return _synthetic_catalog()

    if args.dry_run:
        trainer.run_tick(_synthetic_catalog())
        trainer.finalize()
        out = write_reports(trainer)
        print(f"dry-run complete · recommendation={out['recommendation']} · report={out['run_dir']}")
        return 0

    # Paper-only Bregman scan loop — the edge engine's activation path. Runs every
    # market refresh cycle, independent of BTC Pulse / Grok / news. ON by default
    # in paper mode; only disabled by explicit config (with a logged reason).
    from engine.strategies.bregman_scanner import scanner_from_env
    bregman_scanner = scanner_from_env()
    logging.getLogger("hte.training.start").info(
        "bregman paper scanner: enabled=%s reason=%s (independent of pulse/grok/news)",
        bregman_scanner.enabled, bregman_scanner.disabled_reason)
    bregman_scan_path = dd / "bregman_scan.json"

    # Resolve + create + LOG the absolute artifact directories so files land in a
    # real, writable location and the logs never print a misleading relative path.
    from engine.training.artifact_dirs import (resolve_artifact_dirs, ensure_dirs,
                                               startup_report, proof_lines,
                                               verify_durable_writes)
    art = resolve_artifact_dirs(dd)
    ensure_dirs(art)
    print(startup_report(art))
    metrics_dir = Path(art["metrics_dir"])
    reports_dir = Path(art["reports_dir"])

    def _run_bregman_scan(markets) -> None:
        try:
            tel = bregman_scanner.scan(markets or [])
            bregman_scan_path.write_text(json.dumps(tel, default=str), encoding="utf-8")
            # ABCAS metrics artifact (metrics/bregman.json) — LEGACY scanner telemetry,
            # clearly labeled as superseded by the canonical funnel + NOT controlling
            # run-readiness (TASK 6).
            metrics_dir.mkdir(parents=True, exist_ok=True)
            tel_labeled = {"source": "legacy_abcas_scanner_telemetry",
                           "controls_run_ready": False,
                           "superseded_by": "metrics/bregman_funnel.json", **tel}
            (metrics_dir / "bregman.json").write_text(json.dumps(tel_labeled, default=str),
                                                      encoding="utf-8")
            # TASK 8: every skipped Bregman group writes a durable bregman_diagnostic
            # so bregman_groups_skipped>0 implies bregman_diagnostic_events>0 (the
            # funnel is never silently zero). Capped per tick to bound IO.
            try:
                sink = trainer.closed_loop.sink
                quota = int(getattr(trainer.cfg,
                                    "active_learning_diagnostic_samples_per_tick", 50) or 50)
                for sg in (tel.get("skipped_groups", []) or [])[: max(quota, 1) * 8]:
                    sink.append_bregman_diagnostic({
                        "group_id": str(sg.get("market_id", "")),
                        "source_grouping_method": "constraint_discovery",
                        "stage": "adapter_failed",
                        "raw_market_ids": [sg.get("market_id")],
                        "skip_reason": sg.get("reason"),
                        "missing_fields": [sg.get("reason")] if sg.get("reason") else [],
                        "detail": sg.get("detail", ""),
                        "tick": getattr(trainer, "tick", 0),
                        "repair_hint": _bregman_repair_hint(sg.get("reason")),
                    })
                # NEAR-MISS groups that REACHED the certifier but were not certified:
                # durable shadow/no-trade evidence with the projected profit + Bregman
                # distance + exact reject reason (read-only; never tradeable).
                for nm in (tel.get("near_miss_certified_samples", []) or [])[: max(quota, 1)]:
                    sink.append_bregman_diagnostic({
                        "group_id": "+".join(str(x) for x in nm.get("outcome_ids", []))[:80],
                        "source_grouping_method": "constraint_discovery",
                        "stage": "certifier_reached_not_certified",
                        "raw_market_ids": list(nm.get("outcome_ids", [])),
                        "skip_reason": nm.get("reject_reason"),
                        "relation": nm.get("relation"),
                        "bregman_distance": nm.get("bregman_distance"),
                        "projected_after_fee_profit_per_set":
                            nm.get("projected_after_fee_profit_per_set"),
                        "worst_case_payoff_per_set": nm.get("worst_case_payoff_per_set"),
                        "cost_per_set": nm.get("cost_per_set"),
                        "min_leg_depth": nm.get("min_leg_depth"),
                        "certificate_status": nm.get("certificate_status"),
                        "tradeable": False, "executed": False,
                        "tick": getattr(trainer, "tick", 0),
                    })
            except Exception:  # noqa: BLE001 — diagnostics must never break the scan
                pass
        except Exception as exc:  # noqa: BLE001 — scan must never break the trainer
            logging.getLogger("hte.training.start").debug("bregman scan failed: %s", exc)

    def _write_paper_ledger(st: dict) -> None:
        """Write the canonical paper ledger snapshot so equity reconciles + entries>0.

        Aggregate snapshot (PAPER ONLY): records the trainer equity reconciliation
        entry + the latest ABCAS scan decision. Per-decision ledger writes inside
        the trainer are a follow-up; this guarantees the canonical ledger is
        non-empty and reconciles within 1% of paper-training equity."""
        try:
            from engine.ledger import CanonicalLedger
            pnl = (st or {}).get("pnl", {}) or {}
            def _n(x, d=0.0):
                try:
                    return float(x)
                except (TypeError, ValueError):
                    return d
            equity = _n(pnl.get("equity"), 500.0)
            start = _n(pnl.get("starting_balance"), 500.0)
            unreal = _n(pnl.get("unrealized_pnl"), 0.0)
            realized = equity - start - unreal
            after_cost = _n(pnl.get("after_cost_pnl"), realized)
            led = CanonicalLedger(starting_balance=start)
            led.record(ts=time.time(), market="aggregate", strategy="polymarket_trainer",
                       traded=True, signal_version="trainer-1", realized_pnl=realized,
                       unrealized_pnl=unreal, after_cost_pnl=after_cost,
                       fill_realism_status="aggregate")
            tel = bregman_scanner.last_telemetry or {}
            led.record(ts=time.time(), market="abcas_scan", strategy="abcas", traded=False,
                       kind="decision", signal_version="abcas-1",
                       gross_ev=_n(tel.get("expected_min_profit"), 0.0),
                       fill_realism_status="n/a")
            # The canonical ledger MUST record non-trade decisions, not only trades.
            # Derive the decision-ledger summary from the closed-loop event stream so
            # ledger.decisions reconciles with decision_count (never silently zero).
            cl_ledger = {}
            try:
                cl_ledger = trainer.closed_loop.ledger_summary()
            except Exception:  # noqa: BLE001
                cl_ledger = {}
            base = led.summary()
            base["entries"] = max(int(base.get("n_entries", 0) or 0),
                                  int(cl_ledger.get("entries", 0) or 0))
            base["trades"] = max(int(base.get("n_trades", 0) or 0),
                                 int(cl_ledger.get("trades", 0) or 0))
            base["decisions"] = max(int(base.get("n_decisions", 0) or 0),
                                    int(cl_ledger.get("decisions", 0) or 0))
            base["decision_ledger"] = cl_ledger
            payload = {"starting_balance": start, "equity": led.equity(),
                       "entries": [e.to_dict() for e in led.entries],
                       "summary": base, "decision_ledger": cl_ledger}
            (dd / "paper_ledger.json").write_text(json.dumps(payload, default=str),
                                                  encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — ledger write must never break a tick
            logging.getLogger("hte.training.start").debug("ledger write failed: %s", exc)

    deadline = time.time() + args.minutes * 60.0 if args.minutes > 0 else None
    max_hours_deadline = (time.time() + args.max_hours * 3600.0
                          if args.continue_until_thresholds else None)
    ticks = 0
    while True:
        # The start loop MUST honor the stop sentinel written by the stop script.
        if stop_path.exists():
            print(f"stop sentinel detected ({stop_path}) — stopping (data preserved).")
            if trainer.campaign is not None:
                try:
                    trainer.campaign.mark_stop_requested()
                except Exception:  # noqa: BLE001
                    pass
            break
        # Honor live dashboard control toggles (PAPER ONLY). Off is always applied;
        # the BTC pulse supports live freeze/unfreeze, others apply on restart.
        try:
            _control.apply_runtime(trainer, _control.read_overrides(dd))
        except Exception:  # noqa: BLE001 — control must never break a tick
            pass
        cat = provider()
        _run_bregman_scan(cat)  # paper Bregman scan every refresh cycle
        trainer.run_tick(cat)
        ticks += 1
        st = trainer.status()
        _write_paper_ledger(st)   # canonical ledger snapshot (equity reconciles)
        # Pass-2: certified Bregman execution funnel (discovery -> certify -> open).
        # Pass-3: paper execution-realism funnel (realistic vs shadow vs rejected).
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            (metrics_dir / "bregman_execution.json").write_text(
                json.dumps(trainer.bregman_summary().get("execution", {}), default=str),
                encoding="utf-8")
            (metrics_dir / "paper_realism.json").write_text(
                json.dumps(trainer.paper_realism_report(), default=str), encoding="utf-8")
            # Pass-4: strategy-priority ladder (Bregman Tier-1 reservation).
            (metrics_dir / "strategy_priority.json").write_text(
                json.dumps(trainer.strategy_priority_report(), default=str), encoding="utf-8")
            # Pass-5: profitability-first ranking funnel.
            (metrics_dir / "profitability_ranking.json").write_text(
                json.dumps(trainer.profitability_ranking_report(), default=str), encoding="utf-8")
            # Pass-6: active-learning exploration funnel.
            (metrics_dir / "active_learning.json").write_text(
                json.dumps(trainer.active_learning_report(), default=str), encoding="utf-8")
            # Pass-7: cluster/correlation risk funnel.
            (metrics_dir / "correlation_risk.json").write_text(
                json.dumps(trainer.correlation_risk_report(), default=str), encoding="utf-8")
            # Pass-8: unified inspection summary (machine + human readable) written to
            # the RESOLVED absolute metrics/reports dirs.
            _insp = trainer.write_inspection_artifacts(
                dd, metrics_dir=metrics_dir, reports_dir=reports_dir)
            from engine.training.inspection_summary import console_summary as _console
            print(_console(_insp))
            # TASK 4/5: verify the durable files actually exist on disk and print
            # ABSOLUTE paths with sizes/rows. A positive counter must NOT be claimed
            # while the file is missing/empty — if so, force run-ready false.
            _cll = (_insp.get("closed_loop_learning") or {})
            # CANONICAL Bregman source reconciliation (funnel vs legacy scanner).
            try:
                _funnel = _insp.get("bregman_funnel", {}) or {}
                _canon = int(_funnel.get("constraint_groups_scanned",
                                         _funnel.get("groups_sent_to_certifier", 0)) or 0)
                _legacy = int((bregman_scanner.last_telemetry or {}).get(
                    "constraint_groups_scanned", 0) or 0)
                _disagree = _canon > 0 and _legacy <= 0
                (metrics_dir / "bregman_source_reconciliation.json").write_text(json.dumps({
                    "canonical_source": "metrics/bregman_funnel.json",
                    "legacy_source": "metrics/bregman.json",
                    "canonical_constraint_groups_scanned": _canon,
                    "legacy_constraint_groups_scanned": _legacy,
                    "canonical_controls_run_ready": True,
                    "legacy_controls_run_ready": False,
                    "sources_disagree": _disagree,
                    "classification_impact": "warning_only" if _disagree else "none",
                    "warning": ("legacy_bregman_scanner_zero_but_canonical_funnel_active"
                                if _disagree else ""),
                }, indent=2, default=str), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            _verify = verify_durable_writes(
                art, decision_count=int(trainer.decision_count),
                pending_count=int(_cll.get("pending_labels_total", 0) or 0))
            for _ln in proof_lines(art):
                print("  " + _ln)
            if not _verify["ok"]:
                logging.getLogger("hte.training.start").error(
                    "DURABLE WRITE FAILURE: missing=%s empty=%s -> run_ready_for_hours=false",
                    _verify["missing"], _verify["empty"])
                try:
                    _rr = dict(_insp.get("run_ready") or {})
                    _rr["run_ready_for_hours"] = False
                    _rr["max_safe_runtime_minutes"] = 10
                    _rr["blocking_reasons"] = sorted(set(
                        (_rr.get("blocking_reasons") or []) + _verify["blocking_reasons"]))
                    (metrics_dir / "run_ready.json").write_text(
                        json.dumps(_rr, indent=2, default=str), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as _exc:  # noqa: BLE001 — metrics must never break a tick
            logging.getLogger("hte.training.start").error(
                "inspection summary / durable write FAILED: %s", _exc)
        print(f"tick {ticks}: scanned={st['scan_metrics']['scanned']} "
              f"open={st['pnl']['open_positions']} equity={st['pnl']['equity']} "
              f"closed={st['pnl']['trades_closed']}")
        _print_campaign_progress(trainer)
        _bt = bregman_scanner.last_telemetry or {}
        print(f"  bregman: paper_enabled={_bt.get('bregman_paper_enabled')} "
              f"scanned={_bt.get('constraint_groups_scanned')} "
              f"skipped={_bt.get('groups_skipped')} "
              f"candidates={_bt.get('candidate_arbitrages')} "
              f"certified={_bt.get('certified_arbitrages')}")
        bp = st.get("btc_pulse") or {}
        if bp.get("btc_pulse_enabled"):
            print(f"  btc_pulse: frozen={bp.get('btc_pulse_frozen')} "
                  f"ticks={bp.get('btc_pulse_ticks')} "
                  f"decisions={bp.get('btc_pulse_decisions')} "
                  f"paper_trades={bp.get('btc_pulse_paper_trades')} "
                  f"no_trades={bp.get('btc_pulse_no_trade_decisions')} "
                  f"win_rate={bp.get('btc_pulse_win_rate')} "
                  f"after_cost_pnl={bp.get('btc_pulse_after_cost_pnl')} "
                  f"blockers={bp.get('btc_pulse_blockers')}")
        fa = st.get("feedback_accelerator") or {}
        if fa.get("feedback_accelerator_enabled"):
            cap = fa.get("capacity", {})
            print(f"  feedback_accel: target x{fa.get('target_multiplier')} "
                  f"decisions/tick={cap.get('paper_decision_budget')} "
                  f"candidates={cap.get('trade_candidate_limit')} "
                  f"exploration={fa.get('exploration_enabled')} "
                  f"tiny={fa.get('exploration_tiny_size_enabled')} "
                  f"counts_for_readiness={fa.get('exploration_counts_for_readiness')}")
        if args.continue_until_thresholds:
            if trainer.campaign is not None and trainer.campaign.thresholds_met():
                print("campaign evidence thresholds met — stopping.")
                break
            if max_hours_deadline is not None and time.time() >= max_hours_deadline:
                print("max-hours reached — stopping.")
                break
        elif deadline is not None:
            if time.time() >= deadline:
                break
        elif ticks >= args.max_ticks:
            break
        if args.realtime:
            time.sleep(max(0.0, args.tick_seconds))
    trainer.finalize()
    if args.report or args.minutes > 0 or args.continue_until_thresholds:
        out = write_reports(trainer)
        print(f"report: {out['run_dir']} · recommendation={out['recommendation']}")
    if args.write_campaign_report and trainer.campaign is not None:
        from engine.training.campaign_controller import campaign_json, campaign_markdown
        rep = trainer.campaign_report() or {}
        (dd / "training_campaign.json").write_text(campaign_json(rep), encoding="utf-8")
        (dd / "training_campaign.md").write_text(campaign_markdown(rep), encoding="utf-8")
        print(f"campaign report: {dd / 'training_campaign.json'} · "
              f"verdict={rep.get('state')}")
    print(f"training run complete · ticks={ticks} · equity={trainer.equity()}")
    return 0


def _print_campaign_progress(trainer) -> None:
    """Print concise per-tick campaign progress (PAPER ONLY)."""
    if getattr(trainer, "campaign", None) is None:
        return
    try:
        rep = trainer.campaign_report() or {}
        ev = rep.get("evidence", {}) or {}
        th = rep.get("thresholds", {}) or {}
        print(
            f"  campaign[{rep.get('campaign_name')}]: verdict={rep.get('state')} "
            f"freeze={rep.get('algorithm_freeze_mode')} | "
            f"days={ev.get('elapsed_days')}/{th.get('target_min_days')} "
            f"hrs={ev.get('runtime_hours')}/{th.get('target_min_runtime_hours')} "
            f"dec={ev.get('decisions')}/{th.get('target_min_decisions')} "
            f"trades={ev.get('paper_trades')}/{th.get('target_min_paper_trades')} "
            f"resolved={ev.get('resolved_labels')}/{th.get('target_min_resolved_labels')} "
            f"clean={ev.get('clean_labels')}/{th.get('target_min_clean_labels')} "
            f"breg_cand={ev.get('bregman_candidates')}/{th.get('target_min_bregman_candidates')} "
            f"breg_fp={ev.get('bregman_false_positives')} "
            f"after_cost={ev.get('after_cost_expectancy')} "
            f"realistic={ev.get('realistic_fill_expectancy')}")
        print(f"  campaign top blockers: {', '.join(rep.get('blockers', [])[:5]) or 'none'}")
    except Exception as exc:  # noqa: BLE001 — progress printing must never break the loop
        print(f"  campaign progress unavailable: {exc}")


if __name__ == "__main__":
    raise SystemExit(run())
