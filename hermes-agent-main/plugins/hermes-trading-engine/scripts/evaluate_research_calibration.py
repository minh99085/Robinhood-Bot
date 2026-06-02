#!/usr/bin/env python3
"""Evaluate research-probability calibration against realized outcomes (no network).

Joins ``probability_estimates`` with ``market_outcomes`` on
(venue, market_id, asset_id, outcome). Estimates without a realized outcome are
EXCLUDED from realized metrics and counted as ``unresolved``.

Reports Brier score, log loss, ECE, bucketed calibration, confidence calibration,
and ambiguity/evidence vs accuracy.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.storage import Store  # noqa: E402
from engine.calibration_models import (  # noqa: E402
    InstitutionalCalibrator,
    calibration_slope_intercept,
    reliability_buckets,
)

_EPS = 1e-9


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def brier_score(pairs: list[tuple]) -> float | None:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: list[tuple]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        p = min(1 - _EPS, max(_EPS, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(pairs)


def ece(pairs: list[tuple], n_buckets: int = 10) -> tuple[float | None, list[dict]]:
    if not pairs:
        return None, []
    buckets: list[list[tuple]] = [[] for _ in range(n_buckets)]
    for p, y in pairs:
        idx = min(n_buckets - 1, max(0, int(p * n_buckets)))
        buckets[idx].append((p, y))
    total = len(pairs)
    e = 0.0
    detail = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        avg_p = sum(p for p, _ in b) / len(b)
        freq = sum(y for _, y in b) / len(b)
        e += (len(b) / total) * abs(avg_p - freq)
        detail.append({"bucket": f"{i/n_buckets:.1f}-{(i+1)/n_buckets:.1f}",
                       "n": len(b), "avg_p": round(avg_p, 4), "freq": round(freq, 4)})
    return e, detail


def _bucketed_accuracy(records: list[dict], key: str, edges: list[float]) -> list[dict]:
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = [r for r in records if r.get(key) is not None and lo <= r[key] < hi
               and r.get("y") is not None]
        if not sub:
            continue
        acc = sum(1 for r in sub if round(r["p"]) == r["y"]) / len(sub)
        out.append({"range": f"{lo}-{hi}", "n": len(sub), "accuracy": round(acc, 4)})
    return out


def evaluate(store: Store) -> dict:
    outcomes = {}
    for o in store.get_market_outcomes():
        if o.get("realized_outcome") is None:
            continue
        key = (o.get("venue"), o.get("market_id"), o.get("asset_id"), o.get("outcome"))
        outcomes[key] = int(o["realized_outcome"])

    resolved_pairs: list[tuple] = []
    records: list[dict] = []
    unresolved = 0
    for e in store.get_probability_estimates(limit=1_000_000):
        p = _f(e.get("p_ensemble"))
        if p is None:
            continue
        key = (e.get("venue"), e.get("market_id"), e.get("asset_id"), e.get("outcome"))
        y = outcomes.get(key)
        if y is None:
            unresolved += 1
            records.append({"p": p, "y": None, "confidence": _f(e.get("confidence")),
                            "ambiguity": _f(e.get("ambiguity_score")),
                            "evidence": _f(e.get("evidence_score"))})
            continue
        resolved_pairs.append((p, y))
        records.append({"p": p, "y": y, "confidence": _f(e.get("confidence")),
                        "ambiguity": _f(e.get("ambiguity_score")),
                        "evidence": _f(e.get("evidence_score"))})

    e_val, e_detail = ece(resolved_pairs)
    # Baseline (raw) vs upgraded (fitted calibrator) — Strategy Optimization &
    # Robustness Testing. Deterministic, offline; conservative-shrink fallback
    # when there are too few resolved samples.
    slope, intercept = calibration_slope_intercept(resolved_pairs)
    cal = InstitutionalCalibrator(method="auto").fit(resolved_pairs)
    upgraded_pairs = [(cal.transform(p), y) for p, y in resolved_pairs]
    up_ece, _ = ece(upgraded_pairs)
    return {
        "resolved": len(resolved_pairs),
        "unresolved": unresolved,
        "brier_score": brier_score(resolved_pairs),
        "log_loss": log_loss(resolved_pairs),
        "ece": e_val,
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "calibration_buckets": e_detail,
        "reliability_buckets": reliability_buckets(resolved_pairs),
        "upgraded": {
            "method": cal.calibration_method,
            "effective_sample_size": cal.effective_sample_size,
            "brier_score": brier_score(upgraded_pairs),
            "log_loss": log_loss(upgraded_pairs),
            "ece": up_ece,
        },
        "calibration_artifact": cal.to_artifact(),
        "confidence_vs_accuracy": _bucketed_accuracy(records, "confidence", [0, 0.33, 0.66, 1.01]),
        "ambiguity_vs_accuracy": _bucketed_accuracy(records, "ambiguity", [0, 0.33, 0.66, 1.01]),
        "evidence_vs_accuracy": _bucketed_accuracy(records, "evidence", [0, 0.33, 0.66, 1.01]),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate research calibration vs realized outcomes")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    try:
        from engine.config import settings
        default_db = str(settings.db_path)
    except Exception:  # noqa: BLE001
        default_db = os.getenv("HTE_DB_PATH", "trading.db")
    store = Store(Path(args.db or default_db))
    import json
    print(json.dumps(evaluate(store), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
