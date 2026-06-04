"""Test the dashboard "what's running" status endpoint (read-only, PAPER)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import engine.app as app_mod  # noqa: E402

_EXPECTED_KEYS = {
    "polymarket", "btc_pulse", "news", "chainlink", "btc_fast_price",
    "grok", "feedback_accelerator", "clob",
}
_VALID_STATES = {"on", "warn", "off"}


def _client() -> TestClient:
    return TestClient(app_mod.app)


def test_running_status_returns_all_subsystems():
    resp = _client().get("/api/running-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "paper"
    keys = {s["key"] for s in data["systems"]}
    assert _EXPECTED_KEYS <= keys


def test_running_status_states_are_valid_and_counts_consistent():
    data = _client().get("/api/running-status").json()
    for s in data["systems"]:
        assert s["state"] in _VALID_STATES, s
        assert s["label"] and isinstance(s["label"], str)
        assert "detail" in s
    assert data["total"] == len(data["systems"])
    assert data["running_count"] == sum(1 for s in data["systems"] if s["state"] == "on")


def test_running_status_is_read_only_paper():
    # The endpoint must never report live execution as "on".
    data = _client().get("/api/running-status").json()
    assert data["mode"] == "paper"
    # No system key implies live trading; it's a status view only.
    assert all(s["key"] in _EXPECTED_KEYS for s in data["systems"])
