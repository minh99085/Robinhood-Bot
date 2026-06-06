#!/usr/bin/env python3
"""Run controlled PAPER-ONLY training experiment profiles + a comparison report.

PASS-9 orchestration: loads experiment profiles, safety-validates each (refusing
anything that could let unrealistic fills count as real edge or enable a live
path), runs each profile in-process for a bounded number of ticks over an offline
catalog (or empty), aggregates the Pass-8 inspection metrics, scores real edge by
strategy bucket, classifies bottlenecks, and recommends the next algorithmic pass.

Usage:
    python scripts/run_polymarket_experiments.py --profiles strict_full_system,bregman_only
    python scripts/run_polymarket_experiments.py --validate-only
    python scripts/run_polymarket_experiments.py --dry-run
    python scripts/run_polymarket_experiments.py --ticks 3 --catalog path/to/catalog.json
    python scripts/run_polymarket_experiments.py --compare-only metrics/experiments/<run_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.training import experiments as X  # noqa: E402

logger = logging.getLogger("hte.experiments")

_DEFAULT_PROFILE_PATH = _ROOT / "config" / "polymarket_experiment_profiles.json"


def _load_catalog(path: Optional[str]) -> list:
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data.get("markets", data) if isinstance(data, dict) else (data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not load catalog %s: %s", path, exc)
        return []


def _safety_report(profiles: dict) -> dict:
    out = {}
    for name, prof in profiles.items():
        ok, errors = X.validate_profile_safety(prof)
        out[name] = {"safe": ok, "errors": errors}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run paper-training experiment profiles (PAPER ONLY).")
    ap.add_argument("--profiles", default="", help="comma-separated profile names (default: all)")
    ap.add_argument("--ticks", type=int, default=1)
    ap.add_argument("--duration-minutes", type=float, default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--catalog", default=None, help="offline JSON raw-market catalog")
    ap.add_argument("--profile-config", default=str(_DEFAULT_PROFILE_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--compare-only", default=None, help="dir of per-profile inspection_summary.json")
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args(argv)

    all_profiles = X.load_profiles(args.profile_config)
    selected = ([p.strip() for p in args.profiles.split(",") if p.strip()]
                or list(all_profiles.keys()))
    profiles = {k: all_profiles[k] for k in selected if k in all_profiles}
    missing = [k for k in selected if k not in all_profiles]
    if missing:
        print(f"unknown profiles: {missing}", file=sys.stderr)
        return 2

    # --- compare-only: rebuild a comparison from existing per-profile summaries ---
    if args.compare_only:
        base = Path(args.compare_only)
        summaries = {}
        for sub in sorted(base.glob("*/inspection_summary.json")):
            summaries[sub.parent.name] = json.loads(sub.read_text(encoding="utf-8"))
        comparison = X.build_comparison(base.name, summaries)
        print(X.console_summary(comparison))
        return 0

    safety = _safety_report(profiles)
    unsafe = {k: v for k, v in safety.items() if not v["safe"]}

    if args.validate_only:
        print(json.dumps({"profiles": list(profiles), "safety": safety}, indent=2))
        return 1 if unsafe else 0

    if unsafe:
        print(f"REFUSING to run — unsafe profiles: {unsafe}", file=sys.stderr)
        return 3

    if args.dry_run:
        print(json.dumps({
            "dry_run": True, "profiles": list(profiles), "ticks": args.ticks,
            "safety": safety,
            "would_write": [f"metrics/experiments/<run_id>/{p}/inspection_summary.json"
                            for p in profiles],
        }, indent=2))
        return 0

    run_id = f"exp-{int(time.time())}"
    out_root = Path(args.output_dir) if args.output_dir else _ROOT
    metrics_root = out_root / "metrics" / "experiments" / run_id
    reports_root = out_root / "reports" / "experiments" / run_id
    metrics_root.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    catalog = _load_catalog(args.catalog)
    ticks = args.ticks
    if args.duration_minutes and ticks <= 1:
        ticks = max(1, int(args.duration_minutes * 4))   # ~4 ticks/min heuristic

    t0 = time.time()
    from engine.training.inspection_summary import to_markdown as _insp_md
    summaries = {}
    for name, prof in profiles.items():
        ddir = Path(tempfile.mkdtemp(prefix=f"exp-{name}-"))
        summary = X.run_profile(name, prof, catalog=catalog, ticks=ticks, data_dir=ddir)
        summaries[name] = summary
        pm = metrics_root / name
        pr = reports_root / name
        pm.mkdir(parents=True, exist_ok=True)
        pr.mkdir(parents=True, exist_ok=True)
        (pm / "inspection_summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                                    encoding="utf-8")
        (pr / "paper_training_inspection.md").write_text(_insp_md(summary), encoding="utf-8")

    comparison = X.build_comparison(run_id, summaries)
    (metrics_root / "experiment_comparison.json").write_text(
        json.dumps(comparison, indent=2, default=str), encoding="utf-8")
    (reports_root / "experiment_comparison.md").write_text(
        X.comparison_to_markdown(comparison), encoding="utf-8")
    manifest = X.run_manifest(run_id, list(profiles), comparison,
                              command=" ".join(sys.argv), duration_s=time.time() - t0,
                              safety={"all_profiles_safe": True, "per_profile": safety})
    (metrics_root / "run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                                    encoding="utf-8")
    print(X.console_summary(comparison))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
