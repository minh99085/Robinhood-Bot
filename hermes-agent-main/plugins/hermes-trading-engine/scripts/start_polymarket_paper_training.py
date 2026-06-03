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
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    ap.add_argument("--report", action="store_true", help="write a training report when finished")
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
    cfg.mode = args.mode  # start-paper explicitly drives paper training
    data_dir = Path(args.data_dir) if args.data_dir else None
    trainer = PolymarketPaperTrainer(cfg, data_dir=data_dir)
    dd = trainer.data_dir
    stop_path = dd / "polymarket_training.stop"
    print(f"mode: {cfg.mode} (PAPER ONLY)"
          + (f" · profile: {'aggressive' if args.aggressive_paper else 'default'}")
          + (" · CAMPAIGN" if cfg.campaign_enabled else "")
          + (" · ALGORITHM FROZEN" if cfg.algorithm_freeze_mode else ""))
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
