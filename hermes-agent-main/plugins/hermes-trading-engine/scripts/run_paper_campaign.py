#!/usr/bin/env python3
"""Run a controlled PAPER-trading campaign (paper-only; no live path).

Phase 0 runs a preflight safety check and ABORTS with a red warning if any
live-trading config is detected. The campaign uses a fully isolated paper fill
simulator + ledger; it never places real orders, enables Micro Live, or touches
production execution.

Catalog sources:
  --catalog synthetic   deterministic SIMULATED catalog (offline; default for dry runs)
  --catalog gamma       fetch live Polymarket Gamma catalog (network; opt-in)
  --catalog <file.json> read a catalog JSON array / JSON-lines file (offline)

Examples:
  # 15-minute accelerated dry run (virtual clock, completes in seconds)
  python scripts/run_paper_campaign.py --minutes 15 --catalog synthetic

  # real-time paper campaign against live Gamma data
  python scripts/run_paper_campaign.py --minutes 60 --catalog gamma --realtime
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from engine.campaigns import paper_campaign as pcamp  # noqa: E402
from engine.markets import universe_manager as um      # noqa: E402


def _data_dir() -> Path:
    # Use the SAME data dir the dashboard (engine.config.Settings) uses, so the
    # campaign status the campaign writes is exactly what the dashboard reads.
    if os.getenv("HTE_DATA_DIR"):
        return Path(os.getenv("HTE_DATA_DIR"))
    try:
        from engine.config import Settings
        return Path(Settings().data_dir)
    except Exception:  # noqa: BLE001
        return Path.home() / ".hermes" / "trading-engine"


def _catalog_provider(source: str, scan_limit: int):
    if source == "synthetic":
        return lambda: pcamp.synthetic_catalog(scan_limit, seed=7)
    if source == "gamma":
        cfg = um.UniverseConfig(scan_limit=scan_limit)
        return lambda: um.fetch_catalog(cfg)
    # else: a file path
    path = Path(source)
    data = json.loads(path.read_text(encoding="utf-8")) if path.read_text(encoding="utf-8").strip()[:1] == "[" \
        else [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lambda: data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run a controlled PAPER-trading campaign.")
    ap.add_argument("--config", default=str(_PLUGIN_ROOT / "config" / "paper_campaign.yaml"))
    ap.add_argument("--minutes", type=float, default=15.0, help="campaign duration in minutes")
    ap.add_argument("--tick-seconds", type=int, default=60, help="seconds of campaign time per cycle")
    ap.add_argument("--catalog", default="synthetic", help="synthetic | gamma | <path-to-json>")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--signal", choices=("simulated", "research"), default=None,
                    help="signal source (default: from config). 'research' = Grok research-only.")
    ap.add_argument("--realtime", action="store_true",
                    help="run in real time (default: accelerated virtual clock)")
    args = ap.parse_args(argv)

    cfg = pcamp.CampaignConfig.from_yaml(args.config) if Path(args.config).exists() \
        else pcamp.CampaignConfig()
    if args.signal:
        cfg.signal_model = args.signal
    data_dir = _data_dir()
    campaign = pcamp.PaperCampaign(
        cfg, data_dir=data_dir, seed=args.seed,
        accelerated=not args.realtime, catalog_source=args.catalog)

    # Phase 0 preflight (always shown)
    pre = campaign.preflight()
    print("== Phase 0: Preflight Safety Check ==")
    for c in pre.checks:
        mark = "PASS" if c["passed"] else "FAIL"
        extra = "" if c["passed"] else f"   -> fix config key: {c['config_key']}"
        print(f"  [{mark}] {c['name']}  {c['detail']}{extra}")
    if not pre.ok:
        if pre.red_warning:
            print(pre.red_warning)
        print("\nPreflight FAILED — campaign NOT started. Correct the keys above and retry.")
        return 2

    print("\n== Starting PAPER campaign (no live orders) ==")
    print(f"  name={cfg.campaign_name} minutes={args.minutes} catalog={args.catalog} "
          f"accelerated={not args.realtime}")
    print(f"  data dir (must match the dashboard's): {data_dir}")
    sig_status = campaign.signal_model.status()
    print(f"  signal_model={sig_status.get('name')} grok_enabled={sig_status.get('grok_enabled')} "
          f"grok_source={sig_status.get('grok_source')} research_mode={sig_status.get('research_mode')} "
          f"feedback={cfg.feedback_enabled}")
    if cfg.signal_model == "research" and not sig_status.get("grok_enabled"):
        print("  NOTE: Grok is NOT online (no key or RESEARCH_MODE!=online_*). Using cached/"
              "offline research estimates. Set XAI_API_KEY + RESEARCH_MODE=online_paper for live Grok.")
    res = campaign.run(_catalog_provider(args.catalog, cfg.market_scan_limit),
                       minutes=args.minutes, tick_seconds=args.tick_seconds)
    pf = res["pass_fail"]
    st = res["status"]
    print(f"\n== Campaign finished: {pf['decision']} ==")
    print(f"  scanned={st['total_markets_scanned']} passed={st['markets_passing_filters']} "
          f"tierA={st['tier_a_count']} tierB={st['tier_b_count']}")
    print(f"  open_trades={st['current_open_trades']}/{st['max_open_trades']} "
          f"realized={st['realized_pnl']} unrealized={st['unrealized_pnl']} "
          f"total={st['total_paper_pnl']} maxDD={st['max_drawdown_pct']}")
    failed = {k: v for k, v in pf["checks"].items() if not v}
    print(f"  failed_checks={failed or 'ALL PASS'}")
    print(f"  final report : {res['final_report']}")
    print(f"  latest report: {campaign.reports_root / 'latest_report.md'}")
    print(f"  status json  : {data_dir / ('campaign_' + cfg.campaign_name + '.json')}")
    return 0 if pf["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
