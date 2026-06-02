"""Tests for the PAPER-mode dashboard / accounting / Grok-labeling fixes.

These cover five reported dashboard problems, all in PAPER / SIMULATED mode:

A. Grok / research-mode confusion (legacy GrokBrain must not call xAI when
   research is offline; status must expose grok_network_enabled / grok_source).
B. Unsafe "LIVE P&L" wording (must read PAPER / SIMULATED).
C. Duplicate positions (the positions table's UNIQUE constraint does not
   collapse NULL-keyed rows; get_positions() must dedupe per logical key).
D. Count reconciliation (accounting_summary exposes reconciled counts).
E. Risk-gate visibility (approvals + rejections + traceable decision ids).
F. Safety audit (no live/production submit route or button; no Grok live path).

Nothing here enables live trading, real orders, or a submit path.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import engine
import engine.engine as engine_mod
from engine.brain import GrokBrain
from engine.config import Settings
from engine.engine import TradingEngine
from engine.storage import Store

PLUGIN_ROOT = Path(engine.__file__).resolve().parent.parent
WEB = PLUGIN_ROOT / "web"
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
APP_PY = (PLUGIN_ROOT / "engine" / "app.py").read_text(encoding="utf-8")
BRAIN_PY = (PLUGIN_ROOT / "engine" / "brain.py").read_text(encoding="utf-8")


@pytest.fixture
def trading_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod.crypto, "get_spot", lambda *a, **k: 100.0)
    monkeypatch.setattr(engine_mod.crypto, "order_book_imbalance", lambda *a, **k: 0.0)
    monkeypatch.setattr(engine_mod.crypto, "get_klines", lambda *a, **k: [])
    monkeypatch.setenv("HTE_DATA_DIR", str(tmp_path))
    s = Settings()
    s.data_dir = tmp_path
    store = Store(tmp_path / "test.sqlite3")
    eng = TradingEngine(s, store)
    eng._last_data_ts = engine_mod.time.time()
    return eng


def _pos(venue="pulse", market_id="BTCUSDT", asset_id=None, outcome=None,
         qty="-1.0", avg="0.488775", fees="0.01", ts=None):
    return {
        "venue": venue, "market_id": market_id, "asset_id": asset_id,
        "outcome": outcome, "quantity": qty, "avg_price": avg,
        "realized_pnl": "0.0577", "unrealized_pnl": "0.0",
        "fees_paid": fees, "updated_ts_ms": ts or int(time.time() * 1000),
    }


# ---------------------------------------------------------------------------
# A. Grok / research mode
# ---------------------------------------------------------------------------

def test_default_grok_model_is_grok_4_3(monkeypatch):
    for k in ("GROK_MODEL", "HTE_GROK_MODEL"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    brain = GrokBrain(s)
    assert brain.model == "grok-4.3"
    assert brain.status()["default_model"] == "grok-4.3"


def test_legacy_grok_brain_disabled_when_research_offline(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_MODE", "offline_cache")
    monkeypatch.delenv("GROK_BRAIN_ONLINE", raising=False)
    brain = GrokBrain(Settings())
    # key present but research offline => no live network
    assert brain.grok_network_allowed is False
    assert brain.enabled is False
    assert brain.grok_source == "offline_cache"


def test_research_offline_cache_makes_no_grok_network_call(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_MODE", "offline_cache")
    monkeypatch.delenv("GROK_BRAIN_ONLINE", raising=False)

    def _boom(*a, **k):
        raise AssertionError("no network call may happen in offline_cache")

    monkeypatch.setattr(engine_mod, "httpx", type("X", (), {"Client": _boom})) \
        if hasattr(engine_mod, "httpx") else None
    import engine.brain as brain_mod
    monkeypatch.setattr(brain_mod.httpx, "Client", _boom)

    brain = GrokBrain(Settings())
    # The brain is gated off; starting it must not spin a network thread
    # (start() early-returns when not enabled), so no xAI call can happen.
    brain.start()
    assert brain.enabled is False
    assert brain._thread is None


def test_grok_status_matches_research_mode(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    # offline => research-only, no network
    monkeypatch.setenv("RESEARCH_MODE", "offline_cache")
    monkeypatch.delenv("GROK_BRAIN_ONLINE", raising=False)
    off = GrokBrain(Settings()).status()
    assert off["grok_network_enabled"] is False
    assert off["grok_source"] in ("offline_cache", "legacy_cached")

    # online_paper => network allowed
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    on = GrokBrain(Settings()).status()
    assert on["grok_network_enabled"] is True
    assert on["grok_source"] == "online_research"


def test_grok_on_off_toggle(monkeypatch):
    # with a key, the dashboard toggle turns the research layer on and off
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_MODE", "offline_cache")
    brain = GrokBrain(Settings())
    assert brain.enabled is False                 # offline default
    st_on = brain.set_active(True)                # user turns it ON
    assert st_on["enabled"] is True
    assert st_on["grok_source"] == "online_research"
    assert st_on["user_override"] is True
    st_off = brain.set_active(False)              # user turns it OFF
    assert st_off["enabled"] is False
    assert st_off["user_override"] is False
    brain.stop()


def test_grok_toggle_without_key_stays_off(monkeypatch):
    for k in ("XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    brain = GrokBrain(Settings())
    st = brain.set_active(True)                    # no key -> cannot enable
    assert st["enabled"] is False
    assert st["grok_source"] == "disabled"


def test_grok_disabled_without_key(monkeypatch):
    for k in ("GROK_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RESEARCH_MODE", "online_paper")
    st = GrokBrain(Settings()).status()
    assert st["grok_source"] == "disabled"
    assert st["grok_network_enabled"] is False


# ---------------------------------------------------------------------------
# B. PAPER wording (no unsafe LIVE P&L)
# ---------------------------------------------------------------------------

def test_dashboard_labels_paper_pnl_not_live_pnl():
    assert "LIVE P&amp;L" not in INDEX_HTML
    assert "LIVE P&L" not in INDEX_HTML
    assert "PAPER P&amp;L" in INDEX_HTML
    # Explicit simulation warning still present.
    assert "Simulated" in INDEX_HTML or "SIMULAT" in INDEX_HTML.upper()


def test_dashboard_contains_no_live_submit_button():
    blob = (INDEX_HTML + APP_JS).lower()
    for needle in ("submit order", "place order", "/api/submit", "submitorder("):
        assert needle not in blob, f"unsafe submit affordance present: {needle}"


def test_dashboard_contains_no_production_submit_button():
    # Concrete production-submit affordances must not exist. (A safety *comment*
    # such as "no production submit/cancel/scale/arm controls" is allowed.)
    blob = (INDEX_HTML + APP_JS).lower()
    for needle in ("submit_production", "/api/production/submit",
                   "submitproductionorder(", "place live order"):
        assert needle not in blob, f"production submit affordance present: {needle}"


# ---------------------------------------------------------------------------
# C. Duplicate positions
# ---------------------------------------------------------------------------

def test_positions_are_aggregated_no_duplicate_snapshots(tmp_path):
    store = Store(tmp_path / "p.sqlite3")
    for i in range(6):
        store.upsert_position(_pos(ts=1000 + i))
    positions = store.get_positions()
    assert len(positions) == 1, "NULL-keyed duplicate snapshots must collapse to one"
    assert store.position_snapshot_count() >= 6


def test_multiple_fills_one_market_create_one_position(tmp_path):
    store = Store(tmp_path / "p.sqlite3")
    store.upsert_position(_pos(qty="-1.0", ts=1000))
    store.upsert_position(_pos(qty="-2.0", ts=2000))  # later snapshot, same market
    positions = store.get_positions()
    assert len(positions) == 1
    assert positions[0]["quantity"] == "-2.0", "latest snapshot wins"


def test_position_avg_price_and_fees_aggregate_correctly(tmp_path):
    store = Store(tmp_path / "p.sqlite3")
    store.upsert_position(_pos(avg="0.40", fees="0.01", ts=1000))
    store.upsert_position(_pos(avg="0.50", fees="0.05", ts=2000))
    pos = store.get_positions()[0]
    assert pos["avg_price"] == "0.50"
    assert pos["fees_paid"] == "0.05"


def test_positions_endpoint_returns_unique_active_positions(trading_engine):
    st = trading_engine.oms.store
    for i in range(4):
        st.upsert_position(_pos(qty="-1.0", ts=1000 + i))
    st.upsert_position(_pos(market_id="ETHUSDT", qty="0", ts=5000))  # closed
    oms = trading_engine.oms_summary()
    assert oms["unique_position_count"] == 2          # BTC + ETH logical keys
    assert oms["active_position_count"] == 1          # only non-zero BTC
    assert oms["duplicate_snapshot_count"] >= 3
    # no duplicate rows in the displayed active positions
    keys = [(p["venue"], p["market_id"]) for p in oms["positions"]]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# D. Count reconciliation
# ---------------------------------------------------------------------------

def test_accounting_summary_matches_storage(trading_engine):
    acc = trading_engine.accounting_summary()
    for key in ("trade_count", "order_count", "fill_count", "active_position_count",
                "equity", "realized_pnl", "unrealized_pnl", "fees"):
        assert key in acc
    assert acc["trade_count"] == trading_engine.store.stats()["total"]
    assert acc["simulated"] is True


def test_training_pipeline_uses_correct_trade_count(trading_engine):
    snap = trading_engine.snapshot()
    total = trading_engine.store.stats()["total"]
    assert snap["training"]["trades"] == total
    assert snap["accounting"]["trade_count"] == total


def test_trade_fill_position_counts_reconcile(trading_engine):
    tid = trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="acc test")
    assert tid > 0
    acc = trading_engine.accounting_summary()
    assert acc["fill_count"] == len(trading_engine.oms.get_fills(100000))
    assert acc["order_count"] == len(trading_engine.oms.get_recent_orders(100000))
    assert acc["active_position_count"] >= 0


def test_trade_log_and_fills_have_consistent_ids(trading_engine):
    trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="ids")
    acc = trading_engine.accounting_summary()
    assert acc["fill_count"] == len(trading_engine.oms.get_fills(100000))


# ---------------------------------------------------------------------------
# E. Risk-gate visibility
# ---------------------------------------------------------------------------

def test_risk_decisions_endpoint_returns_approvals_and_rejections(trading_engine):
    # one approved open
    trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="approved")
    # one rejected via kill switch
    ks = trading_engine.s.data_dir / "KILL_SWITCH"
    trading_engine.risk.limits.kill_switch_file = ks
    ks.write_text("halt")
    trading_engine._open_trade(
        market="crypto", symbol="ETHUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", rationale="rejected")

    rd = trading_engine.risk_decisions()
    assert rd["approvals_total"] >= 1
    assert rd["rejections_total"] >= 1
    assert rd["approvals"], "approved decisions must be exposed"
    assert rd["rejections"], "rejected decisions must be exposed"


def test_risk_status_shows_approvals_and_rejections(trading_engine):
    trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="approved")
    rs = trading_engine.risk_status()
    assert "approvals_total" in rs and "rejections_total" in rs
    assert rs["approvals_total"] >= 1
    assert "recent_approvals" in rs


def test_every_fill_has_risk_approval(trading_engine):
    tid = trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="fill+approval")
    assert tid > 0
    rd = trading_engine.risk_decisions()
    assert rd["approvals_total"] >= 1
    # every approval carries a traceable risk_decision_id
    for a in rd["approvals"]:
        assert a.get("risk_decision_id")


def test_fill_traceability_proposal_risk_order_fill(trading_engine):
    trading_engine._open_trade(
        market="crypto", symbol="BTCUSDT", side="BUY", qty=0.1, price=100.0,
        stake=10.0, status="open", risk_edge=0.1, rationale="traceability")
    appr = trading_engine.risk_decisions()["approvals"][0]
    for field in ("risk_decision_id", "market", "symbol", "side"):
        assert field in appr


def test_risk_gate_ui_shows_approvals_and_rejections():
    assert "approvals:" in APP_JS
    assert "latest approved decisions" in APP_JS
    assert "no approved risk decisions yet" in APP_JS


# ---------------------------------------------------------------------------
# F. Safety audit
# ---------------------------------------------------------------------------

def test_no_live_submit_api_route():
    low = APP_PY.lower()
    for needle in ('"/api/submit"', "/api/order/submit", "def api_submit",
                   "submit_production_order", "place_live_order"):
        assert needle not in low, f"forbidden submit route present: {needle}"


def test_no_grok_live_path():
    low = BRAIN_PY.lower()
    # Grok is research-only; it must never reach an order-execution surface.
    for needle in ("submit_order", "submit_production_order", "place_order",
                   "oms.submit", "broker.submit"):
        assert needle not in low, f"Grok live-execution path present: {needle}"


def test_accounting_endpoint_registered():
    assert '"/api/accounting"' in APP_PY
    assert '"/api/risk/decisions"' in APP_PY
