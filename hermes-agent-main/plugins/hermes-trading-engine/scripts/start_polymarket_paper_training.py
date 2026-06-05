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
    args = ap.parse_args(argv)

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
    trainer = PolymarketPaperTrainer(cfg, data_dir=data_dir)
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
        trainer.run_tick(provider())
        ticks += 1
        st = trainer.status()
        print(f"tick {ticks}: scanned={st['scan_metrics']['scanned']} "
              f"open={st['pnl']['open_positions']} equity={st['pnl']['equity']} "
              f"closed={st['pnl']['trades_closed']}")
        _print_campaign_progress(trainer)
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
