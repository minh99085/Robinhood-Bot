"""Walk-forward + robustness validation (TDD, deterministic, offline).

Quant scope: Strategy Optimization & Robustness Testing. Walk-forward windows,
rolling calibration, seeded bootstrap CIs, seeded Monte-Carlo perturbations,
stress scenarios, parameter sensitivity, and regime segmentation.
"""

from __future__ import annotations

from engine.replay.robustness import (
    bootstrap_ci,
    monte_carlo_perturbations,
    parameter_sensitivity,
    regime_segmentation,
    rolling_calibration,
    stress_scenarios,
    walk_forward_windows,
)


def test_walk_forward_windows_no_lookahead_and_nonoverlapping():
    ws = walk_forward_windows(10, train=4, test=2)
    assert len(ws) == 3
    for w in ws:
        assert w.train_end == w.test_start          # test follows train (no look-ahead)
        assert w.train_start < w.train_end <= w.test_start < w.test_end
    # default step == test -> non-overlapping test slices
    assert [w.test_start for w in ws] == [4, 6, 8]


def test_walk_forward_windows_empty_when_insufficient_data():
    assert walk_forward_windows(3, train=4, test=2) == []


def test_rolling_calibration_windows():
    pairs = [(0.9, 1), (0.9, 0), (0.2, 0), (0.8, 1), (0.3, 0), (0.7, 1)]
    rows = rolling_calibration(pairs, window=3)
    assert len(rows) == 4
    for r in rows:
        assert r["n"] == 3 and "brier" in r and "ece" in r


def test_bootstrap_ci_is_seeded_and_brackets_point():
    data = [0.1, 0.2, 0.15, 0.3, -0.05, 0.25, 0.0, 0.4]
    a = bootstrap_ci(data, seed=7, n_boot=500)
    b = bootstrap_ci(data, seed=7, n_boot=500)
    assert a == b                                    # deterministic
    assert a["lo"] <= a["point"] <= a["hi"]


def test_monte_carlo_perturbations_seeded_and_ordered():
    rets = [0.02, -0.01, 0.03, 0.0, 0.01]
    a = monte_carlo_perturbations(rets, n=300, sigma=0.005, seed=3)
    b = monte_carlo_perturbations(rets, n=300, sigma=0.005, seed=3)
    assert a == b
    assert a["p05"] <= a["mean"] <= a["p95"]


def test_stress_scenarios_apply_multiplicative_shocks():
    rets = [0.04, 0.02, 0.06]
    out = stress_scenarios(rets, {"halve": 0.5, "wipe": 0.0})
    assert out["base"] > out["halve"] > out["wipe"]
    assert out["wipe"] == 0.0


def test_parameter_sensitivity_sweeps_one_at_a_time():
    rows = parameter_sensitivity(lambda p: p["a"] * 2 + p["b"],
                                 {"a": 1.0, "b": 0.0}, {"a": [1.0, 2.0], "b": [0.0, 5.0]})
    by = {(r["param"], r["value"]): r["metric"] for r in rows}
    assert by[("a", 2.0)] == 4.0
    assert by[("b", 5.0)] == 7.0


def test_regime_segmentation_partitions_by_threshold():
    obs = [{"vol": 0.01}, {"vol": 0.05}, {"vol": 0.20}]
    buckets = regime_segmentation(obs, key="vol", thresholds=[0.02, 0.10],
                                  labels=["calm", "normal", "stressed"])
    assert len(buckets["calm"]) == 1
    assert len(buckets["normal"]) == 1
    assert len(buckets["stressed"]) == 1
