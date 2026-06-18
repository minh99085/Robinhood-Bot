"""Priority-B: universe widening toward liquidity.

fetch_catalog blends a liquidity-ordered fetch (deepest books) with the volume-ranked
fetch so the most-liquid markets (which carry real Bregman depth but often low 24h volume)
are guaranteed into the scan slice. Offline — a fake httpx client records the order params.
"""

from __future__ import annotations

from engine.markets.universe_manager import UniverseConfig, fetch_catalog


class _Resp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


class _FakeClient:
    """Returns markets keyed by the requested ``order``; records calls."""

    def __init__(self, by_order):
        self.by_order = by_order
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        order = params.get("order")
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 100))
        self.calls.append(order)
        rows = self.by_order.get(order, [])
        return _Resp(rows[offset:offset + limit])

    def close(self):
        pass


def _mkt(i, order_tag):
    return {"id": f"{order_tag}{i}", "question": f"{order_tag} {i}",
            "liquidityNum": 1000 * i, "volume24hr": 10 * i}


def test_blend_merges_liquidity_first():
    vol = [_mkt(i, "V") for i in range(10)]
    liq = [_mkt(i, "L") for i in range(10)]
    client = _FakeClient({"volume24hr": vol, "liquidityNum": liq})
    cfg = UniverseConfig(scan_limit=10, liquidity_blend_fraction=0.5)
    out = fetch_catalog(cfg, client=client)
    ids = [m["id"] for m in out]
    # liquidity-ordered markets come FIRST (guaranteed in-slice), then volume fill
    assert ids[0].startswith("L")
    assert any(i.startswith("V") for i in ids)
    assert "liquidityNum" in client.calls and "volume24hr" in client.calls
    assert len(out) == 10


def test_blend_dedupes_by_id():
    shared = [{"id": "shared", "liquidityNum": 9, "volume24hr": 9}]
    vol = shared + [_mkt(i, "V") for i in range(5)]
    liq = shared + [_mkt(i, "L") for i in range(5)]
    client = _FakeClient({"volume24hr": vol, "liquidityNum": liq})
    cfg = UniverseConfig(scan_limit=20, liquidity_blend_fraction=0.5)
    out = fetch_catalog(cfg, client=client)
    ids = [m["id"] for m in out]
    assert ids.count("shared") == 1


def test_blend_disabled_is_volume_only():
    vol = [_mkt(i, "V") for i in range(5)]
    liq = [_mkt(i, "L") for i in range(5)]
    client = _FakeClient({"volume24hr": vol, "liquidityNum": liq})
    cfg = UniverseConfig(scan_limit=5, liquidity_blend_fraction=0.0)
    out = fetch_catalog(cfg, client=client)
    assert all(m["id"].startswith("V") for m in out)
    assert "liquidityNum" not in client.calls   # no liquidity fetch when disabled


def test_respects_scan_limit():
    vol = [_mkt(i, "V") for i in range(50)]
    liq = [_mkt(i, "L") for i in range(50)]
    client = _FakeClient({"volume24hr": vol, "liquidityNum": liq})
    cfg = UniverseConfig(scan_limit=8, liquidity_blend_fraction=0.5)
    out = fetch_catalog(cfg, client=client)
    assert len(out) == 8
