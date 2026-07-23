"""Paper book (2%-risk sizing, holding time, P&L → loss gate) and scout."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import engine.app as appmod
from engine.robinhood.paper_book import PaperBook
from engine.robinhood.scout import rank_candidates


# ---------------------------------------------------------------------------
# PaperBook core
# ---------------------------------------------------------------------------


def test_sizing_2pct_risk_and_caps(tmp_path):
    book = PaperBook(tmp_path)  # $10k
    # risk = $200; stop 5% of $100 → $5/share → 40 shares = $4000 notional,
    # but 10% position cap = $1000 → 10 shares.
    plan = book.size_buy(100.0, 0.05)
    assert plan["risk_usd"] == 200.0
    assert plan["qty"] == 10
    assert "position cap" in plan["capped_by"][0]


def test_open_close_pnl_and_cash(tmp_path):
    book = PaperBook(tmp_path)
    res = book.open_position("NVDA", 100.0, stop_pct=0.05, horizon_days=5)
    assert res["ok"] and res["position"]["qty"] == 10
    assert book.state["cash"] == 9000.0
    # can't double-open
    assert not book.open_position("NVDA", 101.0)["ok"]

    out = book.close_position("NVDA", 110.0, reason="target")
    assert out["ok"]
    assert out["trade"]["pnl_usd"] == 100.0        # 10 sh × $10
    assert book.state["cash"] == 10_100.0
    assert book.state["realized_pnl"] == 100.0
    # trade log has both rows
    lines = (tmp_path / "paper_trades.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_holding_time_flags_review_due(tmp_path):
    book = PaperBook(tmp_path)
    book.open_position("XLE", 50.0, stop_pct=0.05, horizon_days=5)
    # backdate open by 10 calendar days ≈ 7 trading days > 5
    pos = book.position_for("XLE")
    pos["opened_at"] = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat()
    book.save()
    snap = PaperBook(tmp_path).snapshot()
    assert snap["review_due"] == ["XLE"]
    assert snap["positions"][0]["review_due"] is True


def test_persistence_across_instances(tmp_path):
    PaperBook(tmp_path).open_position("SPY", 500.0, stop_pct=0.04)
    again = PaperBook(tmp_path)
    assert again.position_for("SPY") is not None
    assert again.state["cash"] < 10_000.0


# ---------------------------------------------------------------------------
# Scout ranking (pure)
# ---------------------------------------------------------------------------


def test_scout_ranks_aligned_bullish_first():
    up = [100.0 * (1.01 ** i) for i in range(60)]       # strong aligned up
    down = [100.0 * (0.99 ** i) for i in range(60)]     # strong aligned down
    flat = [100.0 + (i % 2) * 0.1 for i in range(60)]   # noise
    out = rank_candidates({"UPP": up, "DWN": down, "FLT": flat}, top_n=5)
    assert [r["symbol"] for r in out["suggest"]] == ["UPP"]
    assert [r["symbol"] for r in out["avoid"]] == ["DWN"]
    assert out["scanned"] == 3
    assert "21d" in out["suggest"][0]["why"]


def test_scout_skips_short_history():
    out = rank_candidates({"NEW": [10.0] * 5})
    assert out["usable"] == 0 and out["suggest"] == []


# ---------------------------------------------------------------------------
# Endpoints (fake live prices; no MCP)
# ---------------------------------------------------------------------------


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))

    async def fake_prices(symbols):
        return {s.upper(): 100.0 for s in symbols}

    monkeypatch.setattr(appmod, "_fetch_live_prices", fake_prices)
    return TestClient(appmod.app)


def test_paper_endpoints_open_book_close(paper_env, tmp_path):
    c = paper_env
    r = c.post("/api/paper/open",
               json={"symbol": "nvda", "stop_pct": 0.05,
                     "horizon_days": 5, "thesis": "battery BUY"})
    assert r.status_code == 200 and r.json()["ok"]

    r = c.get("/api/paper/book")
    body = r.json()
    assert body["n_open"] == 1
    assert body["positions"][0]["symbol"] == "NVDA"
    assert body["positions"][0]["mark_price"] == 100.0

    r = c.post("/api/paper/close", json={"symbol": "NVDA",
                                         "reason": "review: SELL"})
    assert r.status_code == 200 and r.json()["ok"]
    # realized paper P&L reached the daily-loss accumulator's state file
    state = json.loads((tmp_path / "safety_state.json").read_text())
    assert "daily_pnl" in state


def test_paper_open_requires_live_price(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))

    async def no_prices(symbols):
        return {}

    monkeypatch.setattr(appmod, "_fetch_live_prices", no_prices)
    c = TestClient(appmod.app)
    r = c.post("/api/paper/open", json={"symbol": "NVDA"})
    assert r.status_code == 502
    assert "live" in r.json()["error"]
