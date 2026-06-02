"""Polymarket-only mode: arbitrage + crypto/stock trading are off; live-submit
surfaces do not exist; Grok cannot place orders; training scripts refuse on any
live-trading config. PAPER ONLY.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]


def test_polymarket_only_mode_disables_arbitrage(monkeypatch):
    monkeypatch.setenv("POLYMARKET_ONLY_MODE", "1")
    import engine.config as cfgmod
    importlib.reload(cfgmod)
    s = cfgmod.Settings()
    assert s.polymarket_only_mode is True
    from engine.arb.execution import ARBITRAGE_PERMANENTLY_DISABLED
    assert ARBITRAGE_PERMANENTLY_DISABLED is True
    importlib.reload(cfgmod)  # restore


def test_polymarket_only_mode_disables_crypto_stock_trading(monkeypatch):
    # Settings exposes the disable flags ...
    monkeypatch.setenv("POLYMARKET_ONLY_MODE", "1")
    monkeypatch.setenv("DISABLE_CRYPTO_TRADING", "1")
    monkeypatch.setenv("DISABLE_STOCK_TRADING", "1")
    import engine.config as cfgmod
    importlib.reload(cfgmod)
    s = cfgmod.Settings()
    assert s.disable_crypto_trading and s.disable_stock_trading
    importlib.reload(cfgmod)
    # ... and the legacy engine gates _can_open on polymarket_only_mode
    src = (PLUGIN / "engine" / "engine.py").read_text(encoding="utf-8")
    assert "polymarket_only_mode" in src
    assert "_can_open" in src


def test_arbitrage_not_called_from_tick():
    src = (PLUGIN / "engine" / "engine.py").read_text(encoding="utf-8")
    assert "ArbExecutionEngine" not in src


def test_no_live_submit_api_route():
    src = (PLUGIN / "engine" / "app.py").read_text(encoding="utf-8")
    # the training endpoints are all READ-ONLY (GET); no order-submit POST route
    for path in ("/api/polymarket/training/status", "/api/polymarket/training/scan",
                 "/api/polymarket/training/edge", "/api/polymarket/training/learning"):
        idx = src.find(f'"{path}"')
        assert idx != -1, path
        prefix = src[max(0, idx - 60):idx]
        assert "@app.get" in prefix
    # no POST route that SUBMITS/PLACES a (live) order. Read-only GETs and the
    # paper OMS cancel-all are fine; the forbidden surface is live order submit.
    import re
    post_paths = re.findall(r'@app\.post\("([^"]+)"', src)
    for p in post_paths:
        assert not any(tok in p.lower() for tok in ("submit", "/place", "live-order")), \
            f"unexpected order-submit POST route: {p}"
    assert "/api/order/submit" not in src and "/api/orders/submit" not in src


def test_no_dashboard_live_submit_button():
    html = (PLUGIN / "web" / "index.html").read_text(encoding="utf-8")
    js = (PLUGIN / "web" / "app.js").read_text(encoding="utf-8")
    low = (html + js).lower()
    assert "submit order" not in low
    assert "place order" not in low
    assert "/api/order" not in low


def test_grok_cannot_place_orders():
    from engine.campaigns.signal_models import ResearchSignalModel
    rsm = ResearchSignalModel()
    for forbidden in ("place", "cancel", "submit", "approve", "arm", "scale", "size"):
        assert not hasattr(rsm, forbidden), f"research model must not expose {forbidden}"
    # research models only expose evaluate/status (research-only)
    assert hasattr(rsm, "evaluate") and hasattr(rsm, "status")


def _run_start(env_extra):
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PLUGIN)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(PLUGIN / "scripts" / "start_polymarket_paper_training.py"),
         "--dry-run"], cwd=str(PLUGIN), env=env, capture_output=True, text=True, timeout=120)


def test_start_training_script_refuses_if_micro_live_enabled():
    r = _run_start({"MICRO_LIVE_ENABLED": "1"})
    assert r.returncode == 2 and "REFUS" in r.stdout.upper()


def test_start_training_script_refuses_if_production_enabled():
    r = _run_start({"MICRO_LIVE_ENABLED": "0", "KALSHI_MICRO_LIVE_ENABLED": "0",
                    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION": "1"})
    assert r.returncode == 2 and "REFUS" in r.stdout.upper()


def test_start_training_script_refuses_if_arbitrage_enabled():
    r = _run_start({"MICRO_LIVE_ENABLED": "0", "KALSHI_MICRO_LIVE_ENABLED": "0",
                    "ARB_EXECUTION_ENABLED": "1"})
    assert r.returncode == 2 and "REFUS" in r.stdout.upper()
