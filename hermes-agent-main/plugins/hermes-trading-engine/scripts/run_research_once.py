#!/usr/bin/env python3
"""Run ONE Grok research/probability estimate for a market (research-only).

Requires an explicit online RESEARCH_MODE (or --mode). NEVER places, sizes, or
cancels orders. Writes the estimate/evidence/run into SQLite. Secrets are never
printed.

Example:
  python scripts/run_research_once.py \
    --venue polymarket --market-id 0xabc --asset-id 123 --outcome YES \
    --mode online_paper --p-market 0.42
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

from engine.research import GrokResearchClient  # noqa: E402
from engine.research.schemas import ONLINE_MODES, ProbabilityEstimateBundle  # noqa: E402
from engine.storage import Store  # noqa: E402


def _default_db() -> str:
    try:
        from engine.config import settings
        return str(settings.db_path)
    except Exception:  # noqa: BLE001
        return os.getenv("HTE_DB_PATH", "trading.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one Grok research probability estimate (no orders)")
    ap.add_argument("--venue", default="polymarket")
    ap.add_argument("--market-id", required=True)
    ap.add_argument("--asset-id", default=None)
    ap.add_argument("--outcome", default="YES")
    ap.add_argument("--mode", default=os.getenv("RESEARCH_MODE", "offline_cache"))
    ap.add_argument("--question", default=None)
    ap.add_argument("--resolution-source", default=None)
    ap.add_argument("--p-market", type=float, default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)

    if args.mode not in ONLINE_MODES:
        print(f"refusing: --mode must be one of {sorted(ONLINE_MODES)} (got {args.mode})")
        return 2

    store = Store(Path(args.db or _default_db()))
    client = GrokResearchClient(store=store, mode=args.mode)
    ctx = {
        "venue": args.venue, "market_id": args.market_id, "asset_id": args.asset_id,
        "outcome": args.outcome, "question": args.question,
        "resolution_source": args.resolution_source, "p_market_mid": args.p_market,
    }
    result = client.research(ctx, mode=args.mode)
    if isinstance(result, ProbabilityEstimateBundle):
        print("ESTIMATE", json.dumps({
            "estimate_id": result.estimate_id, "p_ensemble": result.p_ensemble,
            "p_llm_raw": result.p_llm_raw, "confidence": result.confidence,
            "ambiguity_score": result.ambiguity_score, "evidence_score": result.evidence_score,
            "source_count": result.source_count, "no_trade_reason": result.no_trade_reason,
        }, indent=2))
    else:
        print("FAILURE", json.dumps({
            "status": result.status, "reason": result.reason,
            "retryable": result.retryable}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
