# Robinhood-Bot — project rules

## Mandate (ALWAYS follow)

This repo is a **Robinhood Agentic options + equities trading bot**. It connects to
Robinhood's official Trading MCP (`https://agent.robinhood.com/mcp/trading`) and places
orders through a local safety layer. There is **no Polymarket engine** — it was removed.

Operate as a **quant research + engineer + trader** team: read live behavior, hypothesize,
implement the smallest safe change, and verify. Every order path must stay behind the
safety gates.

## Project layout

- **Trading bot:** `hermes-agent-main/plugins/hermes-trading-engine-robinhood/` — options loop
  scans 25-symbol ETF+stock watchlist on manual call/put bias (`RH_OPTIONS_BIAS*`)
- **Framework:** `hermes-agent-main/` is the vendored Hermes agent framework the plugin ships inside.
- **Deploy scripts:** `scripts/sync-vps-robinhood.ps1`, `scripts/verify-sync.ps1`
- **Rules:** `.grok/rules/`

## Repository scope (ALWAYS follow)

- **Canonical repo:** `https://github.com/minh99085/Robinhood-Bot` — the only GitHub repo for
  code, commits, pushes, and deploys.
- **Do not** clone, commit, or push to any other repo unless the operator explicitly overrides
  this in the current message.
- **Default branch:** `main`.

## Git workflow (OPERATOR MEMORY — set 2026-07-03)

**Merge all feature branches into `main` and always push to `main`.** Do not leave completed
work stranded on `cursor/*` branches. After every completed change:

1. Merge the working branch into `main` (fast-forward or merge commit).
2. `git push origin main`
3. Deploy VPS per the deploy mandate below.

`origin/main` is the single source of truth; VPS HEAD must match it.

## Safety (real money — NEVER relax without explicit operator ask)

- `RH_LIVE_TRADING_ENABLED=0` by default — all `place_*` orders are blocked until the operator
  turns it on in `.env`.
- All `place_equity_order` / `place_option_order` calls go through `SafeRobinhoodClient` →
  `RobinhoodSafetyGates`. Orders ≥ `RH_REVIEW_THRESHOLD_NOTIONAL_USD` must pass Robinhood
  `review_*` first.
- PDT, daily-loss, concentration, and max-notional gates are enforced locally.
- See `.grok/rules/real-money-discipline.md` and `.grok/rules/destructive-change-guard.md`.

## VPS deploy (OPERATOR MEMORY — ALWAYS follow)

**Always push to `main` and VPS, then remove orphans and rebuild the container.** Execute
yourself; never push and stop.

1. `git push origin main`
2. `.\scripts\sync-vps-robinhood.ps1`
3. `.\scripts\verify-sync.ps1` — VPS HEAD must equal `origin/main`

Manual on VPS (`/opt/Robinhood-Bot/hermes-agent-main/plugins/hermes-trading-engine-robinhood`):

```bash
docker compose --profile robinhood down --remove-orphans
docker compose --profile robinhood build
docker compose --profile robinhood up -d --force-recreate --remove-orphans
```

Full detail: `.grok/rules/vps-deploy-mandate.md` and `.grok/rules/repo-scope.md`.

## Tests

From the plugin dir:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

## Cursor Cloud specific instructions

Cloud-agent dev setup targets the **Robinhood Agentic plugin** at
`hermes-agent-main/plugins/hermes-trading-engine-robinhood/`. The VM update script creates a
`.venv` there and installs `requirements.txt` + `requirements-dev.txt`. All commands below run
from that plugin directory with the venv active (`source .venv/bin/activate`).

- **Tests:** `python -m pytest tests/ -q` (config in `pytest.ini`, `asyncio_mode=auto`). No secrets or network needed — MCP is mocked.
- **Run the API:** `RH_DATA_DIR=<writable dir> uvicorn engine.app:app --host 127.0.0.1 --port 8810`. In the VM, `/data` (the container default for `RH_DATA_DIR`) is not writable, so set `RH_DATA_DIR` to something like `/tmp/rh_data`. Use tmux for this long-running process. Verify with `curl http://127.0.0.1:8810/api/health` (200). See `README.md` for endpoint list.
- **Status endpoints reflect a status file, not live MCP.** `engine/app.py` only reads `$RH_DATA_DIR/robinhood_status.json`. `/api/robinhood/status` returns **503** until that file exists; the file is normally written by the separate agent loop (`scripts/run_robinhood_agent.py`), which needs OAuth + the live MCP endpoint. To exercise the API read path without OAuth, write a `robinhood_status.json` into `RH_DATA_DIR` manually.
- **Live trading is OFF by default** (`RH_LIVE_TRADING_ENABLED=0`) and OAuth/live MCP are not available in the VM, so the agent loop and any `place_*`/`review_*` flows cannot be exercised here — API + tests are the in-VM surface.
- **Docker is not installed in the VM.** The plugin's `docker-compose.yml` (`--profile robinhood`) is for the VPS; run the app directly with `uvicorn` locally instead.
- **System dep:** `python3.12-venv` is required for `.venv` creation and is baked into the VM snapshot (not in the update script).
