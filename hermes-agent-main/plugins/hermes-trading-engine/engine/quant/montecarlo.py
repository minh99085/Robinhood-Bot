"""Monte Carlo price forecast via Geometric Brownian Motion.

Simulates N paths over a short horizon from the recent drift/volatility, then
returns a quantile fan, terminal distribution, and P(up) — the inputs the
dashboard's "Monte Carlo - 500 paths" panel renders.
"""

from __future__ import annotations

import numpy as np


def simulate(closes: list[float], horizon_steps: int = 60, paths: int = 500,
             seed: int | None = None) -> dict:
    arr = np.asarray(closes[-500:], dtype=float)
    if arr.size < 10:
        spot = float(arr[-1]) if arr.size else 0.0
        return {
            "spot": spot,
            "horizon_steps": horizon_steps,
            "paths": paths,
            "p_up": 0.5,
            "expected": spot,
            "quantiles": {"p05": [], "p25": [], "p50": [], "p75": [], "p95": []},
            "terminal_hist": {"bins": [], "counts": []},
            "converged": False,
        }

    rng = np.random.default_rng(seed)
    rets = np.diff(np.log(arr))
    mu = float(np.mean(rets))
    sigma = float(np.std(rets)) or 1e-6
    spot = float(arr[-1])

    # GBM step: S_{t+1} = S_t * exp((mu - 0.5 sigma^2) + sigma * Z)
    drift = mu - 0.5 * sigma ** 2
    shocks = rng.standard_normal((paths, horizon_steps))
    log_paths = np.cumsum(drift + sigma * shocks, axis=1)
    price_paths = spot * np.exp(log_paths)

    qs = np.percentile(price_paths, [5, 25, 50, 75, 95], axis=0)
    terminal = price_paths[:, -1]
    p_up = float(np.mean(terminal > spot))
    expected = float(np.mean(terminal))

    counts, edges = np.histogram(terminal, bins=24)
    centers = ((edges[:-1] + edges[1:]) / 2.0)

    return {
        "spot": round(spot, 2),
        "horizon_steps": horizon_steps,
        "paths": paths,
        "p_up": round(p_up, 3),
        "expected": round(expected, 2),
        "quantiles": {
            "p05": np.round(qs[0], 2).tolist(),
            "p25": np.round(qs[1], 2).tolist(),
            "p50": np.round(qs[2], 2).tolist(),
            "p75": np.round(qs[3], 2).tolist(),
            "p95": np.round(qs[4], 2).tolist(),
        },
        "terminal_hist": {
            "bins": np.round(centers, 1).tolist(),
            "counts": counts.tolist(),
        },
        "converged": True,
    }
