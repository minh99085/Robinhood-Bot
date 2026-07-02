import pytest

from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.constants import OPTIONS_TOOLS
from engine.robinhood.options_loop import run_options_tick


class _FakeClient:
    def __init__(self, tools: set[str], positions: list | None = None):
        self._tools = tools
        self._positions = positions or []

    async def list_tools(self) -> list[str]:
        return sorted(self._tools)

    async def call_tool(self, name: str, arguments: dict | None = None):
        if name == "get_option_positions":
            return self._positions
        raise RuntimeError(f"unexpected tool {name}")

    async def review_option_order(self, arguments: dict):
        return {"warnings": [], "can_place": True}


@pytest.mark.asyncio
async def test_options_tick_skips_open_position(tmp_path, monkeypatch):
    monkeypatch.setenv("RH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RH_OPTIONS_BIAS", "call")
    monkeypatch.setenv("RH_OPTIONS_BIAS_SPY", "call")
    monkeypatch.setenv("RH_OPTIONS_WATCHLIST", "SPY")
    cfg = RobinhoodConfig.from_env()
    tools = set(OPTIONS_TOOLS) | {"get_equity_quotes"}
    positions = [{"chain_symbol": "SPY", "type": "call", "quantity": 1}]
    client = _FakeClient(tools, positions=positions)
    out = await run_options_tick(client, cfg, agent_status={"connected": True})
    assert out["funnel"].get("already_open", 0) >= 1
    assert out["results"][0]["stage"] == "already_open"
