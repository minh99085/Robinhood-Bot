"""3-state Markov regime model (BULL / BEAR / SIDE).

We classify each return into one of three states, estimate the transition
matrix from recent history, compute the stationary distribution, and derive a
forward probability that the next move is UP. Pure numpy, deterministic.
"""

from __future__ import annotations

import numpy as np

STATES = ["BULL", "BEAR", "SIDE"]


def _classify(returns: np.ndarray, side_band: float) -> np.ndarray:
    """Map returns to states: 0=BULL, 1=BEAR, 2=SIDE."""
    out = np.full(returns.shape, 2, dtype=int)  # default SIDE
    out[returns > side_band] = 0  # BULL
    out[returns < -side_band] = 1  # BEAR
    return out


def fit(closes: list[float], lookback: int = 400) -> dict:
    arr = np.asarray(closes[-(lookback + 1):], dtype=float)
    if arr.size < 20:
        # Not enough data: return a neutral, uninformative model.
        eye = np.full((3, 3), 1 / 3)
        return {
            "matrix": eye.tolist(),
            "stationary": [1 / 3, 1 / 3, 1 / 3],
            "current_state": "SIDE",
            "p_up": 0.5,
            "regime_strength": 0.0,
            "labels": STATES,
        }

    rets = np.diff(np.log(arr))
    # band ~ half a standard deviation defines the "sideways" zone
    side_band = 0.5 * float(np.std(rets)) if np.std(rets) > 0 else 1e-9
    states = _classify(rets, side_band)

    # transition counts with Laplace smoothing
    counts = np.ones((3, 3))
    for a, b in zip(states[:-1], states[1:]):
        counts[a, b] += 1
    matrix = counts / counts.sum(axis=1, keepdims=True)

    # stationary distribution = left eigenvector of matrix for eigenvalue 1
    vals, vecs = np.linalg.eig(matrix.T)
    idx = int(np.argmin(np.abs(vals - 1.0)))
    stat = np.real(vecs[:, idx])
    stat = np.abs(stat)
    stat = stat / stat.sum() if stat.sum() else np.full(3, 1 / 3)

    current = int(states[-1])
    next_dist = matrix[current]
    # P(up next) attributes BULL fully, SIDE half, BEAR none.
    p_up = float(next_dist[0] + 0.5 * next_dist[2])
    p_up = min(max(p_up, 0.01), 0.99)

    # regime strength = how dominant the current state's self-persistence is
    regime_strength = float(matrix[current, current])

    return {
        "matrix": np.round(matrix, 2).tolist(),
        "stationary": np.round(stat, 2).tolist(),
        "current_state": STATES[current],
        "p_up": round(p_up, 3),
        "regime_strength": round(regime_strength, 2),
        "labels": STATES,
    }
