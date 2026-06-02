"""Polymarket training API tests — read-only GETs + paper-only start/stop +
no live-submit route. PAPER ONLY."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._pmtrain_helpers import FORBIDDEN

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_mod(monkeypatch, tmp_path):
    for k in FORBIDDEN:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYMARKET_CLOB_ENABLED", "1")
    try:
        import engine.app as app_module
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"app import unavailable: {exc}")
    return app_module


def _body(resp):
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp


def test_api_training_start_is_paper_only(app_mod, monkeypatch):
    for k in FORBIDDEN:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYMARKET_CLOB_ENABLED", "1")
    resp = app_mod.api_training_start_paper()
    body = _body(resp)
    assert body["started"] is True
    assert body["execution"] == "paper"


def test_api_training_start_refuses_if_micro_live(app_mod, monkeypatch):
    monkeypatch.setenv("MICRO_LIVE_ENABLED", "1")
    resp = app_mod.api_training_start_paper()
    assert getattr(resp, "status_code", 200) == 409
    body = _body(resp)
    assert body["started"] is False and "MICRO_LIVE_ENABLED" in body["refused"]


def test_api_training_start_refuses_if_clob_disabled(app_mod, monkeypatch):
    for k in FORBIDDEN:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POLYMARKET_CLOB_ENABLED", "0")
    resp = app_mod.api_training_start_paper()
    assert getattr(resp, "status_code", 200) == 409
    assert "polymarket_clob_disabled" in _body(resp)["refused"]


def test_api_training_stop_is_paper(app_mod):
    body = app_mod.api_training_stop_paper()
    assert body["stopped"] is True and body["execution"] == "paper"


def test_api_training_status_endpoint(app_mod):
    out = app_mod.api_training_status()
    assert "mode" in out or out.get("available") is False


def test_api_training_baselines_endpoint(app_mod):
    out = app_mod.api_training_baselines()
    assert "baselines" in out


def test_api_has_no_live_submit_route():
    import re
    src = (PLUGIN / "engine" / "app.py").read_text(encoding="utf-8")
    post_paths = re.findall(r'@app\.post\("([^"]+)"', src)
    for p in post_paths:
        assert not any(tok in p.lower() for tok in ("submit", "/place", "live-order"))
    # training start/stop are paper-only and named accordingly
    assert "/api/polymarket/training/start-paper" in src
    assert "/api/polymarket/training/stop-paper" in src
