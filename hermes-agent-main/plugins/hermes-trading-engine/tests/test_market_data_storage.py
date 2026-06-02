"""Phase 2 SQLite migrations are idempotent and writes are best-effort."""

from __future__ import annotations

from engine.market_data.event_store import RawEventStore
from engine.storage import Store

_PHASE2_TABLES = {
    "raw_market_events", "orderbook_snapshots", "orderbook_deltas",
    "market_data_health", "market_events",
}


def _tables(store: Store) -> set[str]:
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def test_raw_event_store_idempotent_migration(tmp_path):
    db = tmp_path / "phase2.sqlite3"
    s1 = Store(db)
    assert _PHASE2_TABLES.issubset(_tables(s1))

    # Re-initialising the SAME database must not crash or wipe data.
    s1.append_raw_market_event(ts_ms=1, source="polymarket_clob", venue="polymarket",
                               event_type="book", market_id="m", asset_id="a",
                               payload={"k": 1})
    s2 = Store(db)  # second migration pass over an existing DB
    assert _PHASE2_TABLES.issubset(_tables(s2))
    assert len(s2.get_recent_raw_market_events(10)) >= 1


def test_raw_event_store_facade_roundtrip(tmp_path):
    store = Store(tmp_path / "facade.sqlite3")
    es = RawEventStore(store)
    es.append_raw_event("polymarket_clob", "book", "m1", "a1", {"hello": "world"})
    es.append_market_event(venue="polymarket", market_id="m1", asset_id="a1",
                           event_type="tick_size_change", payload={"new_tick_size": "0.01"})
    es.update_health(source="polymarket_clob", status="connected", last_message_ts_ms=123,
                     reconnect_count=0, parse_errors=0, subscribed_asset_count=2,
                     stale_asset_count=0)
    events = es.get_recent_events(10)
    assert any(e["event_type"] == "book" for e in events)
    assert es.get_market_event_count(event_type="tick_size_change") == 1
    health = store.get_market_data_health("polymarket_clob")
    assert health is not None and health["status"] == "connected"


def test_storage_writes_are_nonfatal_on_bad_payload(tmp_path):
    store = Store(tmp_path / "nonfatal.sqlite3")
    # Non-JSON-serializable payloads must not raise (best-effort persistence).
    store.append_raw_market_event(ts_ms=1, source="s", venue="v", event_type="e",
                                  market_id="m", asset_id="a", payload={"x": object()})
    # call still returns without raising; row may serialize via default=str
    assert isinstance(store.get_recent_raw_market_events(5), list)
