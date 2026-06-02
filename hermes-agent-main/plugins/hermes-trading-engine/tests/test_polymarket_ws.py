"""Polymarket CLOB WebSocket client: parsing, subscription, robustness.

No real network — messages are fed directly to the sync handler.
"""

from __future__ import annotations

import json

from engine.market_data.polymarket_ws import PolymarketWSClient


def _client() -> PolymarketWSClient:
    return PolymarketWSClient(event_store=None, persist_raw=False)


def test_polymarket_ws_subscription_payload():
    payload = PolymarketWSClient._subscription_payload(["tok1", "tok2"])
    assert payload["assets_ids"] == ["tok1", "tok2"]
    assert payload["type"] == "market"
    assert payload["custom_feature_enabled"] is True


def test_polymarket_ws_message_parser_handles_malformed_json():
    c = _client()
    c.handle_raw_message("{not valid json")
    assert c.parse_errors == 1  # malformed JSON counted, no exception

    # a list containing a non-object event increments parse_errors but the
    # valid event in the same batch is still applied
    c.handle_raw_message(json.dumps([
        "this-is-not-an-object",
        {"event_type": "book", "asset_id": "a1", "market": "m1",
         "bids": [{"price": "0.4", "size": "100"}], "asks": [{"price": "0.42", "size": "50"}]},
    ]))
    assert c.parse_errors == 2
    ob = c.get_orderbook("a1")
    assert ob is not None and str(ob.best_bid) == "0.4"


def test_book_event_builds_orderbook_via_handler():
    c = _client()
    c.handle_raw_message(json.dumps({
        "event_type": "book", "asset_id": "a2", "market": "m2",
        "bids": [{"price": "0.30", "size": "10"}],
        "asks": [{"price": "0.33", "size": "10"}], "tick_size": "0.01",
    }))
    bbo = c.get_bbo("a2")
    assert bbo is not None and bbo.bid == 0.30 and bbo.ask == 0.33


def test_price_change_and_tick_size_via_handler():
    c = _client()
    c.handle_raw_message(json.dumps({
        "event_type": "book", "asset_id": "a3", "market": "m3",
        "bids": [{"price": "0.50", "size": "10"}], "asks": [{"price": "0.52", "size": "10"}],
    }))
    c.handle_raw_message(json.dumps({
        "event_type": "price_change", "asset_id": "a3", "market": "m3",
        "price_changes": [{"side": "BUY", "price": "0.51", "size": "5"}],
    }))
    ob = c.get_orderbook("a3")
    assert str(ob.best_bid) == "0.51"

    c.handle_raw_message(json.dumps({
        "event_type": "tick_size_change", "asset_id": "a3", "market": "m3",
        "new_tick_size": "0.001",
    }))
    fr = c.freshness_for_risk("a3")
    assert fr["tick_size_dirty"] is True


def test_market_resolved_via_handler_marks_state():
    c = _client()
    c.handle_raw_message(json.dumps({
        "event_type": "book", "asset_id": "a4", "market": "m4",
        "bids": [{"price": "0.5", "size": "1"}], "asks": [{"price": "0.6", "size": "1"}],
    }))
    c.handle_raw_message(json.dumps({
        "event_type": "market_resolved", "market": "m4", "asset_id": "a4",
    }))
    fr = c.freshness_for_risk("a4")
    assert fr["resolved"] is True


def test_freshness_missing_asset_is_unreliable_and_stale():
    c = _client()
    fr = c.freshness_for_risk("never-seen")
    assert fr["required"] is True
    assert fr["bbo_present"] is False
    assert fr["stale"] is True
    assert fr["unreliable"] is True


def test_set_desired_assets_marks_resub():
    c = _client()
    c.set_desired_assets(["t1", "t2"])
    assert c._resub_needed is True
    assert c.get_status()["subscribed_asset_count"] == 2
