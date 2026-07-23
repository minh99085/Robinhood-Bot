"""The chart-analyze endpoint must feed live Robinhood data into the pipeline.

The API process holds no persistent MCP session, but the OAuth token lives
in the shared /data volume. The endpoint builds a short-lived client from
that token, passes it to the pipeline (so image prices get fact-checked),
and disconnects after. With no token it degrades cleanly to image-only.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import engine.app as appmod
from engine.chart_vision.models import AnalyzeChartResponse
from engine.robinhood.audit_log import AuditLog


def test_endpoint_passes_live_client_and_disconnects(monkeypatch):
    class FakeAdapter:
        def __init__(self):
            self.disconnected = False
        async def disconnect(self):
            self.disconnected = True

    fake = FakeAdapter()
    captured = {}

    async def fake_live(audit):
        return fake

    async def fake_pipeline(**kwargs):
        captured["mcp_client"] = kwargs.get("mcp_client")
        return AnalyzeChartResponse(ok=True)

    monkeypatch.setattr(appmod, "_live_mcp_client", fake_live)
    monkeypatch.setattr(
        "engine.chart_vision.pipeline.run_full_pipeline", fake_pipeline)

    r = TestClient(appmod.app).post("/api/chart/analyze",
                                    json={"image_base64": "QUJD"})
    assert r.status_code == 200
    assert captured["mcp_client"] is fake       # live client reached the pipeline
    assert fake.disconnected is True            # and was torn down after


def test_endpoint_degrades_to_image_only_without_token(monkeypatch):
    captured = {}

    async def no_client(audit):
        return None

    async def fake_pipeline(**kwargs):
        captured["mcp_client"] = kwargs.get("mcp_client")
        return AnalyzeChartResponse(ok=True)

    monkeypatch.setattr(appmod, "_live_mcp_client", no_client)
    monkeypatch.setattr(
        "engine.chart_vision.pipeline.run_full_pipeline", fake_pipeline)

    r = TestClient(appmod.app).post("/api/chart/analyze",
                                    json={"image_base64": "QUJD"})
    assert r.status_code == 200
    assert captured["mcp_client"] is None       # no crash, just unverified


def test_live_mcp_client_none_when_no_tokens(monkeypatch, tmp_path):
    class FakeStorage:
        def has_tokens(self):
            return False

    class FakeAdapter:
        def __init__(self, cfg, audit=None):
            self.storage = FakeStorage()

    monkeypatch.setattr(
        "engine.robinhood.robinhood_mcp_adapter.RobinhoodMCPAdapter",
        FakeAdapter)
    res = asyncio.run(appmod._live_mcp_client(AuditLog(str(tmp_path))))
    assert res is None
