"""Robustness + walk-forward validation primitives (pure Python, deterministic).

Quant scope — *Strategy Optimization & Robustness Testing* + *Backtesting &
Simulation*: deterministic, offline tools for validating that a paper-training
result is not an artifact of one time slice or one parameter point:

* **walk-forward windows** (rolling train/test splits),
* **rolling calibration windows** (ECE/Brier over a sliding window),
* **bootstrap confidence intervals** (seeded resampling),
* **Monte-Carlo perturbations** (seeded return shocks),
* **stress scenarios** (multiplicative shocks),
* **parameter sensitivity** (one-at-a-time grid sweep),
* **regime segmentation** (partition by a feature/threshold).

Everything is seeded + stdlib-only, so the same inputs always produce the same
output (no numpy, no network). Nothing here trades.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

logger = logging.getLogger("hte.replay.robustness")


@dataclass
class WalkForwardWindow:
    index: int
    train_start: int
    train_end: int          # exclusive
    test_start: int
    test_end: int           # exclusive

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def walk_forward_windows(n: int, *, train: int, test: int,
                         step: Optional[int] = None) -> list[WalkForwardWindow]:
    """Rolling train/test splits over ``n`` ordered observations.

    Each window's test slice strictly FOLLOWS its train slice (no look-ahead),
    test slices are non-overlapping when ``step == test`` (the default), and the
    whole thing is deterministic.
    """
    if train <= 0 or test <= 0 or n <= 0:
        return []
    step = test if step is None else int(step)
    windows: list[WalkForwardWindow] = []
    i = 0
    start = 0
    while start + train + test <= n:
        windows.append(WalkForwardWindow(
            index=i, train_start=start, train_end=start + train,
            test_start=start + train, test_end=start + train + test))
        i += 1
        start += step
    logger.debug("walk_forward_windows n=%d train=%d test=%d -> %d windows",
                 n, train, test, len(windows))
    return windows


def rolling_calibration(pairs: Sequence[tuple[float, int]], *, window: int) -> list[dict]:
    """Rolling-window calibration (ECE + Brier) over ordered ``(p, y)`` pairs."""
    from engine.calibration_models import brier, ece
    pairs = list(pairs)
    out: list[dict] = []
    if window <= 0 or len(pairs) < window:
        return out
    for start in range(0, len(pairs) - window + 1):
        w = pairs[start:start + window]
        out.append({"start": start, "n": len(w),
                    "brier": brier(w), "ece": ece(w)})
    return out


def bootstrap_ci(samples: Sequence[float], *, statistic: Callable[[list[float]], float] = None,
                 n_boot: int = 1000, alpha: float = 0.05, seed: int = 0) -> dict:
    """Seeded bootstrap confidence interval for ``statistic`` (default: mean)."""
    data = [float(x) for x in samples]
    if not data:
        return {"point": 0.0, "lo": 0.0, "hi": 0.0, "n": 0}
    stat = statistic or (lambda xs: sum(xs) / len(xs))
    point = stat(data)
    rng = random.Random(seed)
    boots: list[float] = []
    n = len(data)
    for _ in range(int(n_boot)):
        resample = [data[rng.randrange(n)] for _ in range(n)]
        boots.append(stat(resample))
    boots.sort()
    lo = boots[max(0, int((alpha / 2) * len(boots)))]
    hi = boots[min(len(boots) - 1, int((1 - alpha / 2) * len(boots)))]
    return {"point": round(point, 6), "lo": round(lo, 6), "hi": round(hi, 6), "n": n}


def monte_carlo_perturbations(returns: Sequence[float], *, n: int = 1000,
                              sigma: float = 0.01, seed: int = 0) -> dict:
    """Seeded Monte-Carlo: add Gaussian noise to each return, summarize the
    distribution of the mean (robustness of the edge to small perturbations)."""
    base = [float(x) for x in returns]
    if not base:
        return {"mean": 0.0, "p05": 0.0, "p95": 0.0, "std": 0.0, "n": 0}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(int(n)):
        perturbed = [r + rng.gauss(0.0, sigma) for r in base]
        means.append(sum(perturbed) / len(perturbed))
    means.sort()
    mean = sum(means) / len(means)
    var = sum((m - mean) ** 2 for m in means) / len(means)
    return {"mean": round(mean, 6), "p05": round(means[int(0.05 * len(means))], 6),
            "p95": round(means[min(len(means) - 1, int(0.95 * len(means)))], 6),
            "std": round(math.sqrt(var), 6), "n": len(base)}


def stress_scenarios(returns: Sequence[float], scenarios: dict) -> dict:
    """Apply multiplicative shocks to a return series. ``scenarios`` maps a name
    to a multiplier (e.g. ``{"slippage_2x": 0.5, "fee_spike": 0.8}``)."""
    base = [float(x) for x in returns]
    base_mean = (sum(base) / len(base)) if base else 0.0
    out = {"base": round(base_mean, 6)}
    for name, mult in scenarios.items():
        shocked = [r * float(mult) for r in base]
        out[name] = round((sum(shocked) / len(shocked)) if shocked else 0.0, 6)
    return out


def parameter_sensitivity(metric_fn: Callable[[dict], float], base_params: dict,
                          grid: dict) -> list[dict]:
    """One-at-a-time parameter sweep. For each ``param -> [values]`` in ``grid``,
    vary just that param (others held at ``base_params``) and record the metric."""
    rows: list[dict] = []
    for param, values in grid.items():
        for v in values:
            params = dict(base_params)
            params[param] = v
            rows.append({"param": param, "value": v,
                         "metric": round(float(metric_fn(params)), 6)})
    return rows


def regime_segmentation(observations: Sequence[dict], *, key: str,
                        thresholds: Sequence[float], labels: Optional[Sequence[str]] = None
                        ) -> dict:
    """Partition observations into regimes by a numeric ``key`` and ascending
    ``thresholds``. Returns ``regime_label -> list[observation]``."""
    edges = sorted(float(t) for t in thresholds)
    names = list(labels) if labels else (
        ["low"] + [f"r{i}" for i in range(1, len(edges))] + ["high"])
    buckets: dict[str, list] = {name: [] for name in names}
    for obs in observations:
        v = float(obs.get(key, 0.0))
        idx = 0
        while idx < len(edges) and v >= edges[idx]:
            idx += 1
        idx = min(idx, len(names) - 1)
        buckets[names[idx]].append(obs)
    return buckets
