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

## Safety (real money — NEVER relax without explicit operator ask)

- `RH_LIVE_TRADING_ENABLED=0` by default — all `place_*` orders are blocked until the operator
  turns it on in `.env`.
- All `place_equity_order` / `place_option_order` calls go through `SafeRobinhoodClient` →
  `RobinhoodSafetyGates`. Orders ≥ `RH_REVIEW_THRESHOLD_NOTIONAL_USD` must pass Robinhood
  `review_*` first.
- PDT, daily-loss, concentration, and max-notional gates are enforced locally.
- See `.grok/rules/real-money-discipline.md` and `.grok/rules/destructive-change-guard.md`.

## VPS deploy (OPERATOR MEMORY — ALWAYS follow)

**Every completed change:** push to `main` → sync VPS → `down --remove-orphans` → `build` →
`up -d --remove-orphans`. Execute yourself; never push and stop.

1. `git push origin main`
2. `.\scripts\sync-vps-robinhood.ps1`
3. `.\scripts\verify-sync.ps1` — VPS HEAD must equal `origin/main`

Manual on VPS (`/opt/Robinhood-Bot/hermes-agent-main/plugins/hermes-trading-engine-robinhood`):

```bash
docker compose --profile robinhood down --remove-orphans
docker compose --profile robinhood build
docker compose --profile robinhood up -d --force-recreate --remove-orphans
```

## Tests

From the plugin dir:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```
