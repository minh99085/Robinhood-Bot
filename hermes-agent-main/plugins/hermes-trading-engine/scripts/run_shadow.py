#!/usr/bin/env python3
"""Run a shadow-mode session (Phase 7). NEVER submits live orders.

Examples:
  python scripts/run_shadow.py --dry-run-config
  python scripts/run_shadow.py --duration-minutes 60 --venues polymarket --cached-research-only
  python scripts/run_shadow.py --fixture tests/fixtures/sample_shadow_session.jsonl \
      --duration-minutes 1 --venues polymarket --cached-research-only --force-shadow

Shadow mode runs the full decision stack on read-only data and records
would-have-traded decisions. It calls no real order/cancel endpoint, no live
broker, no wallet, and no private user channel.

Quant scope — *Live Trading & Monitoring* + *Compliance*: shadow validates the
priority-3 directional research path on read-only data (Bregman arbitrage P1 and
calibrated statistical mispricing P2 are resolved in the trainer via
:mod:`engine.training.signal_resolver`). Research stays advisory-only — it never
sizes, places, or bypasses the RiskEngine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _default_db() -> str:
    try:
        from engine.config import settings
        return str(settings.db_path)
    except Exception:  # noqa: BLE001
        return os.getenv("HTE_DB_PATH", "trading.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run a shadow-mode session (no live orders)")
    ap.add_argument("--duration-minutes", type=float, default=0)
    ap.add_argument("--venues", default=None)
    ap.add_argument("--max-candidates", type=int, default=None)
    ap.add_argument("--cached-research-only", action="store_true")
    ap.add_argument("--dry-run-config", action="store_true")
    ap.add_argument("--fixture", default=None, help="JSONL of market inputs to process once")
    ap.add_argument("--force-shadow", action="store_true",
                    help="bypass SHADOW_ENABLED gate (still NEVER enables live trading)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    from engine.shadow import ShadowConfig, ShadowOrchestrator, write_report, compute_session_metrics
    from engine.shadow import LiveReadinessGate
    from engine.storage import Store

    cfg = ShadowConfig.from_env()
    if args.venues:
        cfg.venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    if args.max_candidates is not None:
        cfg.max_candidates_per_cycle = args.max_candidates
    if args.cached_research_only:
        cfg.allow_online_research = False
        cfg.use_cached_research = True
    if args.force_shadow:
        cfg.enabled = True  # bypass the enable gate ONLY; mode stays shadow_live

    if args.dry_run_config:
        ok, reason = cfg.verify_safe_to_start()
        print(json.dumps({"mode": cfg.mode, "enabled": cfg.enabled, "venues": cfg.venues,
                          "config_hash": cfg.config_hash(), "safe_to_start": ok,
                          "reason": reason, "no_live_orders": True}, indent=2))
        return 0

    store = Store(Path(args.db or _default_db()))
    orch = ShadowOrchestrator(store=store, config=cfg)
    started, sess = orch.start()
    if not started:
        print(json.dumps({"status": "not_started", "reason": str(sess)}))
        return 2
    sid = sess.shadow_session_id
    print(f"shadow session started: {sid} (mode=shadow_live, NO live orders)")

    try:
        if args.fixture:
            for i, line in enumerate(Path(args.fixture).read_text().splitlines()):
                line = line.strip()
                if not line:
                    continue
                inp = json.loads(line)
                eq = inp.pop("risk_context_equity", 100000.0)
                from engine.risk import RiskContext
                inp["risk_context"] = RiskContext(equity=float(eq))
                dec = orch.process_market(inp, cycle_id=f"c{i}")
                print(f"  [{i}] {dec.venue} {dec.market_id or dec.market_ticker} -> "
                      f"{dec.decision} ({dec.reason})")
        elif args.duration_minutes > 0:
            # No live feed in this environment; idle loop with heartbeats.
            deadline = time.time() + args.duration_minutes * 60
            while time.time() < deadline:
                orch.heartbeat()
                time.sleep(min(5.0, max(0.1, args.duration_minutes * 60 / 10)))
    except KeyboardInterrupt:
        print("interrupted — finalizing shadow session cleanly")
    finally:
        orch.stop()
        metrics = compute_session_metrics(store, sid, cfg, orch.counters)
        report = LiveReadinessGate(cfg).evaluate(metrics, orch.counters, sid)
        out = write_report(store, sid, cfg, report, metrics)
        print(f"artifacts: {out}")
        print(f"overall readiness: {report.overall_status}")
        print("NO live orders were submitted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
