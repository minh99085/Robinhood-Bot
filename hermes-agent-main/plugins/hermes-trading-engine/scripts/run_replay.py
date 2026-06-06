#!/usr/bin/env python3
"""Deterministic, offline replay/backtest CLI.

Replays saved raw market events (from a JSONL file or the SQLite raw-event
store) through RiskEngine + OMS + PaperBroker against the *replayed* book, then
writes reproducible metrics + artifacts. NO network, NO Grok, NO live orders.

Examples:
  python scripts/run_replay.py --from-jsonl tests/fixtures/sample_polymarket_replay.jsonl \\
      --policy noop --initial-cash 10000 --seed 42
  python scripts/run_replay.py --venue polymarket --asset-id <id> \\
      --start-ts-ms 1700000000000 --end-ts-ms 1700003600000 --policy existing
  python scripts/run_replay.py --from-jsonl data/sample_events.jsonl \\
      --policy simple_edge --fair-probability 0.6 --min-edge 0.05 --out replay_artifacts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.replay import ReplayConfig, ReplayEventLoader, ReplayRunner, write_report  # noqa: E402
from engine.storage import Store  # noqa: E402


def _build_config(args) -> ReplayConfig:
    params: dict = {}
    if args.fair_probability is not None:
        params["fair_probability"] = args.fair_probability
    if args.min_edge is not None:
        params["min_edge"] = args.min_edge
    if args.quantity is not None:
        params["quantity"] = args.quantity
    return ReplayConfig(
        venue=args.venue, market_ids=args.market_id or [], asset_ids=args.asset_id or [],
        start_ts_ms=args.start_ts_ms, end_ts_ms=args.end_ts_ms, max_events=args.max_events,
        policy_name=args.policy, policy_params=params, strategy_tick_ms=args.strategy_tick_ms,
        initial_cash=args.initial_cash, seed=args.seed, from_jsonl=args.from_jsonl,
        output_dir=args.out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic replay/backtest (offline, no network)")
    ap.add_argument("--from-jsonl", default=None)
    ap.add_argument("--venue", default=None)
    ap.add_argument("--market-id", action="append", default=None)
    ap.add_argument("--asset-id", action="append", default=None)
    ap.add_argument("--start-ts-ms", type=int, default=None)
    ap.add_argument("--end-ts-ms", type=int, default=None)
    ap.add_argument("--max-events", type=int, default=None)
    ap.add_argument("--policy", default="noop")
    ap.add_argument("--initial-cash", type=float, default=10000.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--strategy-tick-ms", type=int, default=1000)
    ap.add_argument("--fair-probability", type=float, default=None)
    ap.add_argument("--min-edge", type=float, default=None)
    ap.add_argument("--quantity", type=float, default=None)
    ap.add_argument("--out", default=os.getenv("REPLAY_OUTPUT_DIR", "replay_artifacts"))
    ap.add_argument("--db", default=None, help="operational sqlite path for replay_* tables")
    ap.add_argument("--dry-run-config", action="store_true")
    ap.add_argument("--baseline-report", action="store_true",
                    help="print the deterministic algorithm inventory baseline and exit "
                         "(no replay run; does not change default behavior)")
    ap.add_argument("--campaign-profile", default=None,
                    help="tag this replay's validation evidence with a campaign profile id "
                         "and write it to <out>/campaign_evidence.json (offline, no live)")
    ap.add_argument("--robustness", action="store_true",
                    help="after the replay, print an institutional robustness block: "
                         "risk-adjusted ratios (Sharpe/Sortino/Calmar), a seeded bootstrap "
                         "CI on per-step returns, and an exploration/validation/"
                         "production-readiness split (offline, read-only).")
    args = ap.parse_args(argv)

    if args.baseline_report:
        from engine.training.algorithm_inventory import algorithm_inventory
        inv = algorithm_inventory()
        print(json.dumps({
            "algorithm_inventory": inv,
            "chainlink_scanner_present": inv["chainlink_present"],
            "bregman_arbitrage_present": inv["bregman_present"],
            "bregman_arbitrage_status": "active" if inv["bregman_present"] else "absent",
            "legacy_cross_exchange_arbitrage_disabled": inv["legacy_arb_disabled"],
        }, indent=2))
        return 0

    try:
        config = _build_config(args)
    except Exception as exc:  # noqa: BLE001
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    if args.dry_run_config:
        print(json.dumps({"config": json.loads(config.model_dump_json()),
                          "config_hash": config.config_hash()}, indent=2))
        return 0

    # operational store for replay_* tables (isolated by replay_run_id)
    db_path = args.db or os.path.join(os.getenv("HTE_DATA_DIR", "."), "trading_engine.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(Path(db_path))

    loader = ReplayEventLoader(store=store)
    if args.from_jsonl:
        events = loader.from_jsonl(args.from_jsonl, venue=args.venue,
                                   market_ids=args.market_id, asset_ids=args.asset_id,
                                   start_ts_ms=args.start_ts_ms, end_ts_ms=args.end_ts_ms,
                                   max_events=args.max_events, dedup=config.dedup_raw_events)
    else:
        events = loader.from_sqlite(venue=args.venue, market_ids=args.market_id,
                                    asset_ids=args.asset_id, start_ts_ms=args.start_ts_ms,
                                    end_ts_ms=args.end_ts_ms, max_events=args.max_events,
                                    dedup=config.dedup_raw_events)

    if not events:
        print("FAIL: no events found for the given source/filters (fail-closed).", file=sys.stderr)
        return 3

    runner = ReplayRunner(config, store, events)
    report = runner.run()
    out_dir = write_report(runner, config.output_dir)

    # Institutional validation campaign evidence (one profile) — offline, no live.
    if args.campaign_profile:
        try:
            evidence = runner.validation_evidence()
            payload = {"campaign_profile": args.campaign_profile, "evidence": evidence}
            (Path(out_dir) / "campaign_evidence.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8")
            print(f"  campaign      : wrote validation evidence for profile "
                  f"'{args.campaign_profile}'")
        except Exception as exc:  # noqa: BLE001
            print(f"  campaign      : evidence export failed: {exc}", file=sys.stderr)

    m = report.get("metrics", {})
    c = report.get("calibration", {})
    print("== Replay summary (offline; no live orders) ==")
    print(f"  run_id        : {report['replay_run_id']}")
    print(f"  status        : {report['status']}")
    print(f"  policy        : {config.policy_name}  seed={config.seed}  hash={report['config_hash'][:12]}")
    print(f"  events        : {report['event_count']}")
    print(f"  orders/fills  : {report['counts']['orders']}/{report['counts']['fills']}")
    print(f"  ending equity : {m.get('ending_equity')}  total_pnl={m.get('total_pnl')}")
    print(f"  max drawdown  : {m.get('max_drawdown')}  sharpe={m.get('sharpe')}")
    print(f"  fill ratio    : {m.get('fill_ratio')}  fees={m.get('total_fees')}")
    print(f"  calibration   : brier={c.get('brier_score')} ece={c.get('expected_calibration_error')} "
          f"resolved={c.get('resolved_count')} unresolved={c.get('unresolved_count')}")
    ov = report.get("overfit", {}) or {}
    if ov:
        iv, ov_oos = ov.get("in_sample", {}), ov.get("out_of_sample", {})
        print(f"  overfit       : {'OVERFIT' if ov.get('overfit') else 'OK'} "
              f"score={ov.get('overfit_score')} "
              f"sharpe IS/OOS={iv.get('sharpe')}/{ov_oos.get('sharpe')} "
              f"brier IS/OOS={iv.get('brier')}/{ov_oos.get('brier')}")
    if args.robustness:
        _print_robustness(report)
    print(f"  artifacts     : {out_dir}")
    return 0 if report["status"] == "finished" else 4


def _step_returns(report: dict) -> list:
    """Best-effort per-step return series from the replay report (read-only)."""
    m = report.get("metrics", {}) or {}
    for key in ("step_returns", "returns", "period_returns"):
        seq = m.get(key) or report.get(key)
        if isinstance(seq, list) and seq:
            return [float(x) for x in seq]
    curve = m.get("equity_curve") or report.get("equity_curve")
    if isinstance(curve, list) and len(curve) > 1:
        out = []
        for a, b in zip(curve, curve[1:]):
            if a:
                out.append((float(b) - float(a)) / float(a))
        return out
    return []


def _print_robustness(report: dict) -> None:
    """Print the institutional robustness block (offline, read-only, deterministic)."""
    from engine.backtest import calmar_ratio, sharpe_ratio, sortino_ratio
    from engine.replay.robustness import bootstrap_ci
    from engine.strategies.strategy_attribution import production_readiness

    rets = _step_returns(report)
    counts = report.get("counts", {}) or {}
    n_fills = int(counts.get("fills", 0) or 0)
    ratios = {"sharpe": sharpe_ratio(rets), "sortino": sortino_ratio(rets),
              "calmar": calmar_ratio(rets)}
    ci = bootstrap_ci(rets, n_boot=1000, alpha=0.05, seed=42) if rets else {
        "point": 0.0, "lo": 0.0, "hi": 0.0, "n": 0}
    readiness = production_readiness(
        validation={"n_validation": n_fills, "validation_pnl": report.get("metrics", {}).get("total_pnl")},
        exploration={"note": "exploration excluded from readiness verdict"},
        significance=None, ablations=None, overfit=bool((report.get("overfit") or {}).get("overfit")))
    print("== Robustness (offline; exploration excluded from readiness) ==")
    print(f"  returns       : n={len(rets)} "
          f"sharpe={ratios['sharpe']} sortino={ratios['sortino']} calmar={ratios['calmar']}")
    print(f"  bootstrap mean: point={ci['point']} ci95=[{ci['lo']}, {ci['hi']}] n={ci['n']}")
    print(f"  readiness     : production_ready={readiness['production_ready']} "
          f"reasons={readiness['blocking_reasons']}")
    # Institutional walk-forward + statistically-credible after-cost expectancy.
    from engine.validation_contract import (credible_positive_expectancy,
                                            walk_forward_report)
    wf = walk_forward_report(rets, train=max(2, len(rets) // 3), test=max(1, len(rets) // 6))
    exp = credible_positive_expectancy(rets)
    print(f"  walk-forward  : windows={wf['windows']} test_means={wf['walk_forward_test_means']}")
    print(f"  expectancy    : point={exp['point']} ci=[{exp['lo']}, {exp['hi']}] "
          f"credible_positive={exp['credible_positive']} ({exp['reason']})")


if __name__ == "__main__":
    raise SystemExit(main())
