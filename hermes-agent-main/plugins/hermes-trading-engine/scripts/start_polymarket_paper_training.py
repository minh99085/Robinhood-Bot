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

    cfg = TrainingConfig.from_env()
    cfg.mode = args.mode  # start-paper explicitly drives paper training
    trainer = PolymarketPaperTrainer(cfg)
    print(f"mode: {cfg.mode} (PAPER ONLY)")
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
    ticks = 0
    while True:
        trainer.run_tick(provider())
        ticks += 1
        st = trainer.status()
        print(f"tick {ticks}: scanned={st['scan_metrics']['scanned']} "
              f"open={st['pnl']['open_positions']} equity={st['pnl']['equity']} "
              f"closed={st['pnl']['trades_closed']}")
        if deadline is not None:
            if time.time() >= deadline:
                break
        elif ticks >= args.max_ticks:
            break
        if args.realtime:
            time.sleep(max(0.0, args.tick_seconds))
    trainer.finalize()
    if args.report or args.minutes > 0:
        out = write_reports(trainer)
        print(f"report: {out['run_dir']} · recommendation={out['recommendation']}")
    print(f"training run complete · ticks={ticks} · equity={trainer.equity()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
