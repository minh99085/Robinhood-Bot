"""ARB_SIMULATE_OPPS is a float probability in [0.0, 1.0], not a boolean."""

from __future__ import annotations

import pytest

from engine.arb.detector import ArbitrageDetector, _parse_sim_prob


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.2", 0.2),
        ("0", 0.0),
        ("", 0.0),
        ("1", 1.0),
        ("true", 1.0),
        ("on", 1.0),
        ("false", 0.0),
        ("off", 0.0),
        ("abc", 0.0),    # unparseable -> safe off
        ("1.5", 1.0),    # clamped to 1.0
        ("-0.4", 0.0),   # clamped to 0.0
        (None, 0.0),
    ],
)
def test_parse_sim_prob(raw, expected):
    assert _parse_sim_prob(raw) == pytest.approx(expected)


def test_detector_reads_probability_from_env(monkeypatch):
    monkeypatch.setenv("ARB_SIMULATE_OPPS", "0.2")
    det = ArbitrageDetector(feeds=None, mapper=None, universe=None)
    assert det.simulate_prob == pytest.approx(0.2)
    assert det.simulate is True


def test_detector_zero_probability_disables_simulation(monkeypatch):
    monkeypatch.setenv("ARB_SIMULATE_OPPS", "0")
    det = ArbitrageDetector(feeds=None, mapper=None, universe=None)
    assert det.simulate_prob == 0.0
    assert det.simulate is False
