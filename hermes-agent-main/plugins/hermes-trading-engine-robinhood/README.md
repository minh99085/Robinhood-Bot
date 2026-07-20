# Hermes Trading Engine — Robinhood Agentic

The repo's trading bot: connects Hermes to [Robinhood's official Trading MCP](https://agent.robinhood.com/mcp/trading) to trade **options and equities** on a Robinhood Agentic account, with local safety gates in front of every order.

## Architecture

| Service | Container | Port | Role |
|---------|-----------|------|------|
| API | `hermes-robinhood-api` | `8810` | Health + status (`/api/health`, `/api/robinhood/status`) |
| Agent loop | `hermes-robinhood-agent` | — | MCP connection manager, reconnect, status persistence |

Data volume: `rh_data` → `/data` (OAuth tokens, audit log, status JSON).

## Options loop (manual directional bias)

The agent loop (`scripts/run_robinhood_agent.py`) scans a built-in **25-symbol** universe
(9 ETFs + 16 liquid stocks) when `RH_OPTIONS_LOOP_ENABLED=1`:

**ETFs:** SPY, QQQ, IWM, XLK, SMH, XLF, XLV, XLE, DIA  
**Stocks:** NVDA, TSLA, AAPL, MSFT, AMZN, META, GOOGL, AVGO, AMD, MU, INTC, LLY, NFLX, JPM, V, MA

Set manual bias (required before any scan trades):

```bash
# Global bias for all watchlist symbols
RH_OPTIONS_BIAS=call   # or put | none

# Per-symbol overrides
RH_OPTIONS_BIAS_SPY=call
RH_OPTIONS_BIAS_NVDA=put
```

With `RH_LIVE_TRADING_ENABLED=0` (default), the loop logs **paper intents** to
`/data/options_ledger.json` without placing orders. Status: `/api/robinhood/options/status`.

Probe MCP + sample chain: `python scripts/robinhood_mcp_probe.py --symbol SPY --bias call`

**Phase 5–6 operator surfaces**

| Surface | URL / command |
|---------|----------------|
| Dashboard | `http://127.0.0.1:8810/dashboard` |
| Options status | `GET /api/robinhood/options/status` |
| Cached chain | `GET /api/robinhood/options/chain?symbol=SPY` |
| Live readiness | `GET /api/robinhood/options/readiness` or `python scripts/validate_live_readiness.py` |

Loop guards: skips symbols with **open positions**, enforces **per-symbol cooldown**
(`RH_OPTIONS_SYMBOL_COOLDOWN_S`), caps **max open positions**, runs **review_option_order**
in paper mode when `RH_OPTIONS_PAPER_REVIEW=1`.

## Quick start (local)

```bash
cd hermes-agent-main/plugins/hermes-trading-engine-robinhood
cp .env.example .env
pip install -r requirements.txt -r requirements-dev.txt

# One-time OAuth (desktop browser)
python scripts/robinhood_oauth_login.py

# Run agent + API
docker compose --profile robinhood up -d --build
curl http://127.0.0.1:8810/api/health
curl http://127.0.0.1:8810/api/robinhood/tools
```

## VPS deploy

From repo root after pushing to `main`:

```powershell
git push origin main
.\scripts\sync-vps-robinhood.ps1
.\scripts\verify-sync.ps1
```

### First-time VPS OAuth

Robinhood requires **desktop** OAuth for Agentic account onboarding:

1. SSH to VPS or run locally with `RH_DATA_DIR` pointing at the volume.
2. `python scripts/robinhood_oauth_login.py` — open the printed URL, complete auth, paste callback URL.
3. Confirm tokens at `/data/robinhood_oauth_tokens.json` inside `rh_data` volume.
4. Start containers: `docker compose --profile robinhood up -d`.

## Options + equities

Order tools are gated the same way for both asset classes:

| Place tool | Review tool |
|------------|-------------|
| `place_option_order` | `review_option_order` |
| `place_equity_order` | `review_equity_order` |

Every `place_*` call flows through `SafeRobinhoodClient` → `RobinhoodSafetyGates`; option
chains and other read tools are whatever the Robinhood MCP server exposes (logged at OAuth login).

## Monte-Carlo-Sim bridge (paper mode)

When co-hosted with [Monte-Carlo-Sim](https://github.com/minh99085/Monte-Carlo-Sim),
the optional `mc-bridge` profile watches the sim's verdict files
(`outputs/verdicts`, `outputs/paper_verdicts`) and, for each fresh TRADE:

1. maps it to `place_equity_order`-shaped args (long-only; shorts skipped —
   Robinhood can't short shares; quantity clamped to `RH_MAX_ORDER_NOTIONAL_USD`),
2. runs it through `RobinhoodSafetyGates`, and
3. appends the outcome to `/data/mc_bridge_ledger.jsonl`.

**Phase 1 makes no Robinhood API calls** — no OAuth needed, nothing can be
placed. Each verdict file is processed exactly once
(`/data/mc_bridge_state.json`), and verdicts older than 48h are skipped.

```bash
docker compose --profile robinhood --profile mc-bridge up -d
docker exec hermes-mc-bridge sh -c 'tail -n 5 /data/mc_bridge_ledger.jsonl'
```

Code: `engine/robinhood/mc_bridge.py`, loop: `scripts/run_mc_bridge.py`,
tests: `tests/test_mc_bridge.py`.

## Safety defaults

| Setting | Default | Meaning |
|---------|---------|---------|
| `RH_LIVE_TRADING_ENABLED` | `0` | Blocks `place_*` orders |
| `RH_APPROVAL_MODE` | `review_required` | Requires Robinhood `review_*` before place |
| `RH_MAX_ORDER_NOTIONAL_USD` | `100` | Hard cap per order |
| `RH_REVIEW_THRESHOLD_NOTIONAL_USD` | `50` | Calls `review_equity_order` / `review_option_order` |
| `RH_DAILY_LOSS_LIMIT_USD` | `200` | Halts new orders after daily loss |
| `RH_MAX_DAY_TRADES_5D` | `3` | PDT-style rolling limit |

Audit log: `/data/robinhood_audit.jsonl` (every tool call + safety decision).

## Tests

```bash
python -m pytest tests/ -q
```
