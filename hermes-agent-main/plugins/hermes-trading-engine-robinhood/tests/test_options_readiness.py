import json
import time

import pytest

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.options_positions import parse_option_positions
from engine.robinhood.options_readiness import evaluate_readiness
from engine.robinhood.options_state import record_symbol_action, symbol_in_cooldown


def test_parse_option_positions():
    payload = [
        {
            "chain_symbol": "SPY",
            "type": "call",
            "quantity": 2,
            "strike_price": 505,
            "instrument_id": "opt-1",
        }
    ]
    pos = parse_option_positions(payload)
    assert len(pos) == 1
    assert pos[0].symbol == "SPY"
    assert pos[0].quantity == 2


def test_symbol_cooldown(tmp_path):
    record_symbol_action(tmp_path, "SPY", "paper_intent")
    assert symbol_in_cooldown(tmp_path, "SPY", 3600)
    assert not symbol_in_cooldown(tmp_path, "SPY", 0)


def test_readiness_requires_bias_and_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RH_OPTIONS_BIAS", "none")
    cfg = RobinhoodConfig.from_env()
    report = evaluate_readiness(cfg, status={"connected": True}, options_status={"available": True})
    assert not report.ready
    assert "manual_bias" in report.blockers

    (tmp_path / "robinhood_oauth_tokens.json").write_text(
        json.dumps({"tokens": {"access_token": "x", "token_type": "bearer"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RH_OPTIONS_BIAS", "call")
    cfg = RobinhoodConfig.from_env()

    catalog = {
        "tools": [{"name": n} for n in [
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
            "get_option_positions",
            "get_option_orders",
            "get_option_level_upgrade_info",
            "review_option_order",
            "place_option_order",
            "cancel_option_order",
        ]],
    }
    (tmp_path / "mcp_tool_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    ledger = {
        "events": [{"type": "scan_complete", "ts": time.time()}],
    }
    (tmp_path / "options_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    report = evaluate_readiness(
        cfg,
        status={"connected": True},
        options_status={"available": True},
        min_paper_scans=1,
    )
    assert report.ready
