# Hermes Robinhood Agentic — operator guide

## Scope

This plugin is the repo's trading bot: it trades **options and equities** on a Robinhood
Agentic account through Robinhood's official Trading MCP.

- **MCP endpoint:** `https://agent.robinhood.com/mcp/trading`
- **Plugin path:** `hermes-agent-main/plugins/hermes-trading-engine-robinhood/`
- **VPS plugin path:** `/opt/Robinhood-Bot/hermes-agent-main/plugins/hermes-trading-engine-robinhood`

## Deploy to VPS (OPERATOR MEMORY — ALWAYS follow)

**Every change:** push to `main` → sync VPS → `down --remove-orphans` → `build` → `up -d --remove-orphans`.

```powershell
git push origin main
.\scripts\sync-vps-robinhood.ps1
.\scripts\verify-sync.ps1
```

Manual on VPS:

```bash
cd /opt/Robinhood-Bot/hermes-agent-main/plugins/hermes-trading-engine-robinhood
cp .env.example .env   # first time only
docker compose --profile robinhood down --remove-orphans
docker compose --profile robinhood build
docker compose --profile robinhood up -d --force-recreate --remove-orphans
```

Verify:

```bash
curl -s http://127.0.0.1:8810/api/health
curl -s http://127.0.0.1:8810/api/robinhood/status
docker ps | grep hermes-robinhood
```

## Options loop (manual bias)

Enabled by default (`RH_OPTIONS_LOOP_ENABLED=1`). Set `RH_OPTIONS_BIAS=call` or `put`
(or per-symbol `RH_OPTIONS_BIAS_SPY=call`) before expecting scan activity.

- Built-in watchlist: 9 ETFs + 16 stocks (override with `RH_OPTIONS_WATCHLIST`)
- Paper mode: intents logged to `/data/options_ledger.json` until live trading is on
- API: `GET /api/robinhood/options/status`, `/ledger`, `/chain?symbol=SPY`, `/readiness`, `/mcp/catalog`
- Dashboard: `GET /dashboard`
- Live checklist: `python scripts/validate_live_readiness.py`

## OAuth (desktop flow)

1. Run `python scripts/robinhood_oauth_login.py` (inside container or on operator machine).
2. Open the printed URL in a **desktop** browser (Robinhood requirement).
3. Paste the callback URL when prompted.
4. Tokens persist to `RH_DATA_DIR/robinhood_oauth_tokens.json`.

## Enabling live trading (operator only)

Default: `RH_LIVE_TRADING_ENABLED=0` — all `place_*` calls blocked.

To enable after OAuth + Agentic account onboarding:

1. Set `RH_LIVE_TRADING_ENABLED=1` in `.env`
2. Set `RH_AGENTIC_ACCOUNT_ID` if filtering to Agentic account
3. Rebuild: `docker compose --profile robinhood up -d --force-recreate`
4. Monitor `/data/robinhood_audit.jsonl`

## Safety invariants

- All `place_equity_order` / `place_option_order` calls go through `SafeRobinhoodClient`
- Orders ≥ `RH_REVIEW_THRESHOLD_NOTIONAL_USD` must pass Robinhood `review_*` first
- PDT, daily loss, concentration, and max-notional gates are enforced locally
- Connectivity + safety layer (Phase 1). Add trading strategies deliberately, always behind the gates.

## Stop the bot

```bash
docker compose --profile robinhood down --remove-orphans
```
