"""Bregman divergence + simplex-projection unit tests (deterministic, offline).

Quant scope: Statistical & Probabilistic Modeling — verifies the divergence
generators (KL, generalized KL, squared Euclidean, generalized entropy), their
numerically-safe behaviour, the exact Euclidean simplex projection, and the
divergence-gap "is this off the simplex?" signal.
"""

from __future__ import annotations

import math

from engine.training.bregman import (
    divergence_gap,
    generalized_entropy_divergence,
    generalized_kl_divergence,
    is_on_simplex,
    kl_divergence,
    normalize,
    project_to_simplex,
    safe_kl_divergence,
    squared_euclidean,
)


def test_squared_euclidean_basic():
    assert squared_euclidean([0.3, 0.3, 0.3], [0.3, 0.3, 0.3]) == 0.0
    assert abs(squared_euclidean([0.5, 0.5], [0.4, 0.6]) - 0.02) < 1e-9


def test_kl_divergence_non_negative_and_zero_on_identity():
    p = [0.2, 0.3, 0.5]
    assert kl_divergence(p, p) < 1e-9
    assert kl_divergence([0.5, 0.5], [0.4, 0.6]) > 0.0


def test_kl_is_safe_with_zeros():
    # zeros must not produce inf/nan
    val = safe_kl_divergence([0.0, 1.0], [0.5, 0.5])
    assert math.isfinite(val) and val >= 0.0


def test_generalized_entropy_reduces_to_squared_euclidean_at_alpha_2():
    p, q = [0.6, 0.1, 0.3], [0.2, 0.5, 0.3]
    assert abs(generalized_entropy_divergence(p, q, alpha=2.0)
               - squared_euclidean(p, q)) < 1e-9


def test_generalized_entropy_reduces_to_generalized_kl_at_alpha_1():
    p, q = [0.6, 0.4], [0.5, 0.5]
    assert abs(generalized_entropy_divergence(p, q, alpha=1.0)
               - generalized_kl_divergence(p, q)) < 1e-9


def test_generalized_kl_detects_mass_difference():
    # normalized KL is blind to a pure scaling; generalized KL is not
    assert kl_divergence([0.45, 0.45], [0.5, 0.5]) < 1e-9       # same shape
    assert generalized_kl_divergence([0.45, 0.45], [0.5, 0.5]) > 0.0


def test_normalize_handles_all_zero():
    assert normalize([0.0, 0.0, 0.0]) == [1 / 3, 1 / 3, 1 / 3]
    assert abs(sum(normalize([0.3, 0.3, 0.3])) - 1.0) < 1e-12


def test_project_to_simplex_returns_valid_distribution():
    proj = project_to_simplex([0.3, 0.3, 0.3])
    assert abs(sum(proj) - 1.0) < 1e-12
    assert all(x >= 0.0 for x in proj)
    # symmetric input -> uniform projection adding the 0.1 deficit equally
    assert all(abs(x - 1 / 3) < 1e-9 for x in proj)


def test_project_to_simplex_idempotent_on_simplex():
    p = [0.2, 0.5, 0.3]
    proj = project_to_simplex(p)
    assert all(abs(a - b) < 1e-9 for a, b in zip(p, proj))


def test_project_to_simplex_clips_negatives():
    proj = project_to_simplex([1.2, -0.5, 0.1])
    assert abs(sum(proj) - 1.0) < 1e-12
    assert all(x >= 0.0 for x in proj)


def test_is_on_simplex():
    assert is_on_simplex([0.5, 0.5])
    assert is_on_simplex([0.2, 0.3, 0.5])
    assert not is_on_simplex([0.3, 0.3, 0.3])       # sums to 0.9
    assert not is_on_simplex([0.6, 0.6])            # sums to 1.2
    assert not is_on_simplex([1.2, -0.2])           # negative component


def test_divergence_gap_zero_on_simplex():
    for method in ("squared_euclidean", "kl", "generalized_kl", "generalized_entropy"):
        assert divergence_gap([0.4, 0.6], method=method) < 1e-9


def test_divergence_gap_positive_off_simplex():
    # squared Euclidean + generalized KL are mass-sensitive: they detect a sum != 1
    for method in ("squared_euclidean", "generalized_kl"):
        assert divergence_gap([0.3, 0.3, 0.3], method=method) > 0.0      # sum 0.9
        assert divergence_gap([0.6, 0.6], method=method) > 0.0           # sum 1.2


def test_normalized_kl_gap_detects_shape_not_pure_scaling():
    # normalized KL is blind to a uniform scaling (same shape -> 0 gap) ...
    assert divergence_gap([0.3, 0.3, 0.3], method="kl") < 1e-9
    # ... but detects a non-uniform shape that is off the simplex
    assert divergence_gap([0.6, 0.3], method="kl") > 0.0
