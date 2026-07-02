import pytest

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import OPTIONS_TOOLS
from engine.robinhood.options_loop import run_options_tick


class _FakeClient:
    def __init__(self, tools: set[str]):
        self._tools = tools
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[str]:
        return sorted(self._tools)

    async def call_tool(self, name: str, arguments: dict | None = None):
        self.calls.append((name, arguments or {}))
        raise RuntimeError("not implemented in fake")


@pytest.mark.asyncio
async def test_options_tick_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RH_OPTIONS_BIAS", "call")
    cfg = RobinhoodConfig.from_env()
    client = _FakeClient({"get_portfolio"})
    out = await run_options_tick(client, cfg)
    assert out["available"] is False
    assert "missing_tools" in out


@pytest.mark.asyncio
async def test_options_tick_no_active_bias(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RH_OPTIONS_BIAS", "none")
    cfg = RobinhoodConfig.from_env()
    tools = set(OPTIONS_TOOLS) | {"get_equity_quotes", "get_portfolio"}
    client = _FakeClient(tools)
    out = await run_options_tick(client, cfg)
    assert out["reason"] == "no_active_bias"
