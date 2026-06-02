# Hermes Trading Engine (Paper Trading) ☤📈

A self-contained **autonomous paper-trading agent + live dashboard** for
**crypto, stocks, and Polymarket**, styled after the "Hermes Trading Engine"
look. It watches real, live market prices and trades with **pretend money** so
you can see how it behaves with **zero financial risk**. It can optionally use
**Grok (xAI)** as its decision "brain."

> ⚠️ **IMPORTANT — THIS IS A SIMULATION.**
> It uses **fake money only**. It **never** connects to your real exchange,
> broker, or wallet, and it **never** places a real order. There is no place to
> enter real trading keys. Think of it as a flight simulator for trading.

---

## Phase 1: safety architecture (paper-only, risk-gated)

This engine is being grown into a serious prediction-market trading system. The
non-negotiable foundation, shipped now:

### No real orders exist yet
There is **no real-money order placement** anywhere in this codebase — no
exchange/broker signing, no wallet, no private keys. "LIVE" is *armed
simulation* (the safeguards are real and testable, but no order is ever sent).
Real order submission is intentionally **not** part of this pass.

### Every simulated order passes a deterministic RiskEngine
No code path — BTC pulse, crypto, stocks, Polymarket paper bets, **or**
cross-exchange arbitrage — can open even a *simulated* trade without an
approved decision from `engine/risk.py::RiskEngine`. The RiskEngine is pure and
deterministic (no randomness, no LLM): same proposal + same portfolio state +
same kill-switch state → same verdict. Rejections are logged with a reason code
to `<data_dir>/risk_rejections.jsonl` and surfaced on the dashboard and at
`GET /api/risk`.

### Grok is research-only
Grok (xAI) may research, classify regimes, and *propose* — it can **never**
execute, and it **never sets order size** (`suggestedSizePct` is advisory
metadata). Invalid / unparseable Grok output collapses to a **WAIT** action
(via the Pydantic `GrokAction` model), never a best-effort trade. In the arb
path Grok can only *veto*; the deterministic RiskEngine always has the final say.

### Trading modes (current + roadmap)
| Mode | Status | What it means |
|------|--------|---------------|
| **paper** | ✅ now (default) | Fake money on live data. Always the boot mode. |
| **replay** | 🔜 roadmap | Deterministic backtest/replay over recorded market data. |
| **shadow** | 🔜 roadmap | Mirror real decisions against live data with zero order submission, to compare intended vs. actual. |
| **guarded-live** | 🔜 future | A *vetted* execution adapter behind the same RiskEngine + circuit breaker + readiness gate. Not in this pass. |

### Risk configuration

All limits are config-driven via environment variables (safe defaults shown).
Fractions are of current paper **equity**.

| Env var | Default | Meaning |
|---------|---------|---------|
| `HTE_RISK_MAX_ORDER_NOTIONAL_FRAC` | `0.10` | Max single order as a fraction of equity. |
| `HTE_RISK_MAX_ORDER_NOTIONAL_USD` | `0` (off) | Absolute per-order USD cap (0 disables). |
| `HTE_RISK_MAX_MARKET_EXPOSURE_FRAC` | `0.30` | Max open exposure per market (crypto/stock/…). |
| `HTE_RISK_MAX_TOTAL_EXPOSURE_FRAC` | `0.60` | Max open exposure across all markets. |
| `HTE_RISK_MAX_OPEN_ORDERS` | `50` | Max concurrent open positions. |
| `HTE_RISK_MAX_DAILY_LOSS_FRAC` | `0.10` | Block new opens once day P&L ≤ −this×equity. |
| `HTE_RISK_MAX_SPREAD` | `0.10` | Reject when quoted spread exceeds this fraction. |
| `HTE_RISK_MIN_EDGE_AFTER_COSTS` | `0.0` | Require edge-after-costs ≥ this. |
| `HTE_RISK_MAX_DATA_AGE_S` | `60` | Reject on stale market data older than this. |
| `HTE_RISK_MAX_AMBIGUITY` | `1.0` | Reject markets with ambiguity score above this. |
| `HTE_RISK_ALLOW_DUPLICATE` | `0` | Allow duplicate same-market-side exposure. |
| `HTE_KILL_SWITCH_FILE` | `<data_dir>/KILL_SWITCH` | Create this file to block **all** orders instantly. |

**Kill switch:** `docker compose exec hermes-trading-engine touch /data/KILL_SWITCH`
halts every order path immediately; delete the file to resume.

### Safe runtime defaults
The engine boots with the safest possible configuration:

```
HTE_MODE=paper            # always boots paper; no live adapter exists
HTE_AUTOTRADE=0           # bot opens no simulated trades until you enable it
HTE_AGGRESSIVENESS=cautious
ARB_EXECUTION_ENABLED=false
ARB_SIMULATE_OPPS=0       # probability in [0,1]; 0 = no synthetic arb opps
```

Enable paper autotrading from the dashboard **AUTOTRADE** button, or set
`HTE_AUTOTRADE=1` in `docker-compose.yml`.

---

## Phase 2: Polymarket CLOB market data (read-only)

Phase 2 adds a real **read-only** market-data layer for the Polymarket CLOB: a
WebSocket client, normalized order-book state, raw-event persistence, stale-data
controls, and dashboard/API visibility. **It is strictly read-only — it never
authenticates, signs, or submits anything. No live order placement exists.** The
engine still always boots in PAPER, and every paper trade still goes through the
deterministic RiskEngine.

### Enabling it
Off by default. Turn it on in `docker-compose.yml` (or env):

```
POLYMARKET_CLOB_ENABLED=1
```

then restart:

```
docker compose down
```
```
docker compose up --build
```

When enabled, the engine subscribes (read-only) to the CLOB **token (asset) ids**
of the same trending markets it paper-trades, capped at `POLYMARKET_CLOB_MAX_ASSETS`.
The subscription payload is exactly:

```json
{"assets_ids": ["<token_id>", ...], "type": "market", "custom_feature_enabled": true}
```

### Env vars
| Env var | Default | Meaning |
|---------|---------|---------|
| `POLYMARKET_CLOB_ENABLED` | `0` | Master switch for the read-only CLOB feed. |
| `POLYMARKET_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Market WebSocket endpoint. |
| `POLYMARKET_CLOB_MAX_ASSETS` | `20` | Max token ids subscribed at once. |
| `POLYMARKET_CLOB_STALE_MS` | `3000` | Order book older than this is "stale" → risk-blocking. |
| `POLYMARKET_CLOB_PERSIST_RAW` | `1` | Persist every raw inbound message to SQLite. |
| `POLYMARKET_CLOB_SUBSCRIBE_TRENDING` | `1` | Auto-subscribe to trending-market token ids. |

### Adaptive Polymarket Market Universe Manager (selection only)

`engine/markets/universe_manager.py` scans a large market catalog, filters out
untradable markets, scores the rest with a `MarketQualityScore`, and produces
tiers. **It never places, cancels, or sizes an order** — the RiskEngine + paper
OMS remain the only execution path, and live order-book subscription stays gated
by `POLYMARKET_CLOB_ENABLED`.

Pipeline: **scan 1000 → shortlist 100 → live-watch 80 → trade from top 20 → hold ≤ 3**.

- **Tier A** (top 20): trade candidates (still subject to model edge, depth, RiskEngine, dedup, max-open).
- **Tier B** (next 80): live WebSocket watchlist.
- **Tier C** (next ≤200): periodic refresh, not live-subscribed.
- **Tier D**: ignored until the next full refresh.

Score = `0.25·liquidity + 0.20·vol24h + 0.15·velocity + 0.15·spread + 0.10·depth +
0.10·time_to_resolution + 0.05·category_diversity − penalties` (wide spread, low
depth, stale book, missing token ids, low volume, extreme price, ending too soon,
unclear resolution, duplicate/correlated event).

Run a scan (offline from a saved catalog, or online from Gamma):

```bash
python scripts/scan_polymarket_universe.py --from-json catalog.json --out universe.json   # offline
python scripts/scan_polymarket_universe.py --limit 1000                                    # online (Gamma)
```

The dashboard reads the cached status at `GET /api/markets/universe` (it never
triggers a network scan on a web request).

| Env var | Default | Hard max | Meaning |
|---------|---------|----------|---------|
| `MARKET_SCAN_LIMIT` | `1000` | `2000` | Full catalog scan size. |
| `MARKET_SHORTLIST_LIMIT` | `100` | `200` | Ranked shortlist (Tier A+B). |
| `MARKET_LIVE_WATCHLIST_LIMIT` | `80` | `120` | Tier B size (live watchlist). |
| `MARKET_TRADE_CANDIDATE_LIMIT` | `20` | `25` | Tier A size (trade candidates). |
| `MAX_OPEN_POLYMARKET_TRADES` | `3` | `8` | Max open PM positions (paper clamps to 5). |
| `MAX_OPEN_TRADES_HARD_CAP` | `8` | `8` | Absolute open-trade ceiling. |
| `MIN_MARKET_LIQUIDITY_USD` | `1000` | — | Soft liquidity floor (penalty below). |
| `MIN_MARKET_VOLUME_24H_USD` | `500` | — | Soft 24h-volume floor (penalty below). |
| `MAX_ALLOWED_SPREAD` | `0.04` | — | Spreads above this are penalised. |
| `MIN_TOP_OF_BOOK_DEPTH_USD` | `100` | — | Depth below this is penalised. |
| `CATALOG_REFRESH_SECONDS` | `600` | — | Full catalog refresh cadence (paper). |
| `SCORE_REFRESH_SECONDS` | `60` | — | Score refresh cadence. |

### Events handled
`book` (full snapshot replace), `price_change` (per-level deltas; `size:"0"`
removes a level), `tick_size_change` (risk-critical), `last_trade_price`,
`best_bid_ask`, `new_market`, `market_resolved`. `best_bid_ask`, `new_market`,
and `market_resolved` require `custom_feature_enabled: true`.

### How market-data freshness affects the RiskEngine
For a paper trade on a **tracked** Polymarket market, the RiskEngine rejects the
proposal (in addition to all Phase 1 checks) when any of these hold, with the
explicit reason code shown:

| Condition | RiskDecision code |
|-----------|-------------------|
| Feed disconnected / connecting / reconnecting / degraded | `market_data_degraded` |
| No BBO for the required asset | `missing_bbo` |
| Order book older than `POLYMARKET_CLOB_STALE_MS` | `stale_market_data` |
| Live spread exceeds `HTE_RISK_MAX_SPREAD` | `excessive_spread` |
| Market resolved | `resolved_market` |
| Tick size changed and not yet refreshed by a new `book` | `tick_size_changed_requires_refresh` |
| Delta stream applied with no base snapshot (unreliable) | `market_data_degraded` |

If the CLOB feed is **disabled**, or a market is **not tracked**, `market_data`
is `None` and these checks are skipped — Phase 1 behavior is preserved exactly.

**Why tick-size changes block trading:** a tick-size change can silently
invalidate the cached book (levels no longer align to the grid). The asset is
marked `tick_size_dirty` and stays risk-blocked until a fresh `book` snapshot
re-establishes a trustworthy state.

### Dashboard & API
A **Polymarket CLOB market data** panel appears on the dashboard (connection
status, subscribed/tracked/stale asset counts, last-message age, parse errors,
reconnects, and per-asset BBO/freshness). New read-only endpoints:

- `GET /api/market-data/status`
- `GET /api/market-data/recent-events`
- `GET /api/market-data/orderbook/{asset_id}`
- `GET /api/market-data/bbo` (optional `?asset_id=`)

### Smoke test (read-only, no wallet)
From `hermes-agent-main/plugins/hermes-trading-engine/`:

```
python scripts/polymarket_clob_smoke.py --seconds 30 --max-assets 10
```

It fetches trending markets, subscribes read-only, prints BBO/health for ~30s,
optionally writes raw events to SQLite, and exits cleanly. It never needs a
wallet or private key.

### How to verify the feed is read-only
- The client only ever `send`s the subscription payload above — grep
  `engine/market_data/polymarket_ws.py` for `ws.send`; the only call sends the
  `assets_ids` subscription. There is no auth header, no signing, no order RPC.
- There is no order-submission code path anywhere in the plugin (Phase 1 + 2).
- The `MarketDataAdapter` interface exposes only `start/stop/subscribe/
  unsubscribe/get_status/get_bbo/get_orderbook` — no place/cancel methods.

**Current mode remains paper-only. No live order placement exists yet.**

---

## Phase 3: Order Management System + PaperBroker (paper-only)

Phase 3 replaces "insert a simulated trade row" with a real internal **Order
Management System (OMS)** and a realistic **PaperBroker** that models limit
orders, spread, depth, partial fills, IOC/FOK/GTC, cancels, replaces, slippage,
fees, stale-data rejection, and reconciliation. **It is still paper-only — no
real order submission, no wallet/private-key signing, no live broker adapter,
and no dependency requiring exchange credentials.**

### Flow
```
strategy/proposal -> RiskEngine.approve() -> OMS.submit() -> PaperBroker
   -> fills + order status persisted -> positions rebuilt -> legacy trade view projected
```
Every simulated open (pulse / crypto / stock / Polymarket) now goes through
`engine/execution/`. Nothing fantasy-fills: the broker decides the fill from
local CLOB state when available, otherwise a conservative reference-price
simulation. Every fill is auditable back to its order **and** risk decision
(`orders.proposal_id` + `orders.risk_decision_json`, `order_events`, `fills`).

### Order lifecycle
`CREATED → (RISK_REJECTED | ACCEPTED) → broker → (OPEN | PARTIALLY_FILLED |
FILLED | CANCELLED | REJECTED)`. Idempotent on `client_order_id`. Order
persistence **fails closed**: if the order row can't be written, the order is
rejected (`broker_unavailable`) rather than executed.

### Fill model
- **CLOB-backed** (venue/asset has a local book): marketable-limit crossing,
  per-level depth consumed with a **queue-position haircut**
  (`PAPER_MAX_FILL_DEPTH_FRACTION`), partial fills, IOC cancels the remainder,
  FOK fully-fills-or-rejects, GTC/DAY rest OPEN and fill later on a crossing
  book update. Stale or resolved books are rejected, never filled.
- **Reference-price fallback** (legacy crypto/stock/pulse with no book): a
  conservative full fill at the reference price worsened by slippage + fees,
  flagged **SIMULATED**. **Prediction-market reference fills default OFF**
  (`PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS=0`) — Polymarket uses real CLOB state
  when `POLYMARKET_CLOB_ENABLED=1`, and otherwise its orders are rejected
  (`missing_orderbook`) rather than fantasy-filled. Set
  `PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS=1` to restore legacy PM reference fills.

### Fee / slippage / broker env vars
| Env var | Default | Meaning |
|---|---|---|
| `PAPER_MAX_FILL_DEPTH_FRACTION` | `0.35` | Max fraction of a level's size you can take (queue haircut). |
| `PAPER_TAKER_FEE_BPS` | `30` | Taker fee (bps). |
| `PAPER_MAKER_FEE_BPS` | `10` | Maker fee (bps). |
| `PAPER_MIN_FEE_USD` | `0` | Fixed minimum fee per fill. |
| `PAPER_SLIPPAGE_BPS` | `25` | Slippage (bps); only ever worsens the price. |
| `PAPER_LATENCY_MS` | `250` | Simulated latency (recorded, non-blocking). |
| `PAPER_ALLOW_REFERENCE_PRICE_FILLS` | `1` | Allow reference fills for legacy venues. |
| `PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS` | `0` | Allow reference fills for prediction markets. |
| `PAPER_RESTING_ORDER_FILL_ON_CROSS` | `1` | Resting orders fill on a crossing book update. |
| `PAPER_REJECT_ON_STALE_BOOK` | `1` | Reject (don't fill) against a stale book. |
| `PAPER_DEFAULT_TIME_IN_FORCE` | `IOC` | TIF used by auto-strategy marketable opens. |

### Cancel / replace
`OMS.cancel_order`, `cancel_all_for_market`, `cancel_all`, and
`replace_order` (cancels the original and creates a new linked
`client_order_id`). FILLED orders can't be cancelled; cancelling an already-
cancelled order returns `already_cancelled`. Every action is persisted to
`order_events`. No real cancel API calls.

### Reconciliation
Runs in `tick()` (~30s): rebuilds positions from fills, updates unrealized PnL
from BBO/midpoint/reference, and detects overfills, status mismatches, and
orphan fills. Findings are written to `reconciliation_events`. A **high-severity**
finding (e.g. an order filled beyond its quantity) flags the system **degraded**,
which blocks new orders until cleared. Status: `GET /api/reconciliation/status`
and `/api/health` (`oms_degraded`).

### API endpoints (read-only + cancel)
`GET /api/orders`, `/api/orders/open`, `/api/orders/recent`,
`/api/orders/{client_order_id}`, `/api/fills`, `/api/positions`,
`POST /api/orders/{client_order_id}/cancel`, `POST /api/orders/cancel-all`,
`GET /api/reconciliation/status`, `/api/reconciliation/events`. The dashboard
gains an **Orders & fills** panel (open orders, recent fills, positions,
reconciliation severity).

### Storage (idempotent migrations)
New tables: `orders`, `fills`, `positions`, `order_events`,
`reconciliation_events`. Existing `trades` / `equity` tables are untouched and
still drive the dashboard P&L; OMS fills are projected into the legacy trade
view so the existing equity/settlement logic keeps working.

### Run / test
Paper mode (safe defaults; bot opens nothing until you enable autotrade):
```
docker compose down
```
```
docker compose up --build
```
Tests (from `hermes-agent-main/plugins/hermes-trading-engine/`):
```
python -m compileall -q engine __init__.py
pytest -q
```

### Verifying it is paper-only
- No order-submission, signing, wallet, or exchange-credential code exists in
  `engine/execution/` (or anywhere in the plugin). The PaperBroker has no
  network client; its only "execution" is in-memory math over a local book.
- Every order requires a `RiskEngine`-approved decision before the OMS accepts
  it; the OMS re-checks `risk_decision.approved` as defense in depth.

**No real order submission, private-key signing, or live broker adapter was added.**

> Known limitation: cross-exchange **arbitrage** still uses its dedicated
> Phase-1 paper executor (already RiskEngine-gated); it has not yet been
> migrated onto the OMS in this phase.

---

## Phase 4: deterministic replay / backtest (offline)

Phase 4 adds an **offline, deterministic, event-driven replay/backtest** framework
(`engine/replay/`). It replays saved raw market events, reconstructs the order
book, runs a policy's proposals through the **same** RiskEngine + OMS +
PaperBroker used in paper trading (against the *replayed* book), and produces
reproducible metrics, calibration, and report artifacts — so you can judge
whether the agent has real edge **after costs, slippage, rejects, partial fills,
and stale-data blocks**.

**It is offline and simulated. No network calls, no Grok/xAI calls, and no live
orders during replay.** Determinism: same config (`config_hash`) + same saved
events + same seed → identical metrics.

### Live paper vs. shadow vs. replay
- **Paper live** — trades simulated orders on *live* market data in real time.
- **Shadow live** (roadmap) — mirrors decisions against live data, submitting nothing.
- **Replay** (this phase) — *offline* re-run over *recorded* events; deterministic,
  reproducible, no clocks/network. The decision/execution stack is identical;
  only the data source and clock differ.

### Why replay doesn't call Grok
Replay must be reproducible from saved data alone. Grok is non-deterministic and
network-bound, so by default replay uses **cached** probability estimates only
(`REPLAY_USE_CACHED_GROK=1`, `REPLAY_ALLOW_GROK_NETWORK=0`).

### Collect raw events (Phase 2 capture)
Run the engine with `POLYMARKET_CLOB_ENABLED=1`; the Phase 2 market-data layer
persists every raw message to `raw_market_events` (and `market_events`). Replay
reads those, or a JSONL(.gz) export.

### Run it
Sample fixture (committed, tiny):
```
python scripts/run_replay.py --from-jsonl tests/fixtures/sample_polymarket_replay.jsonl \
    --policy noop --initial-cash 10000 --seed 42
python scripts/run_replay.py --from-jsonl tests/fixtures/sample_polymarket_replay.jsonl \
    --policy simple_edge --fair-probability 0.7 --min-edge 0.05 --quantity 50 --seed 42
```
Against the SQLite raw-event store:
```
python scripts/run_replay.py --venue polymarket --asset-id <token_id> \
    --start-ts-ms 1700000000000 --end-ts-ms 1700003600000 --policy existing --max-events 50000
```
Validate config only: add `--dry-run-config`. The CLI **fails closed** (nonzero
exit) if no events match, never hits the network, and writes artifacts to
`replay_artifacts/<replay_run_id>/`.

Import realized outcomes for calibration:
```
python scripts/import_replay_outcomes.py outcomes.csv
```
(CSV: `venue,market_id,asset_id,outcome,resolved_ts_ms,realized_outcome,payout_price`.)

### Policies
`noop` (baseline, never trades), `simple_edge` (deterministic: BUY when
cached/fair probability exceeds the ask by `min_edge`), `cached_grok` (cached
Grok estimates only), `existing` (offline EV adapter), `random` (seed-controlled,
test baseline). Policies only **emit `TradeProposal`s** — the runner routes them
through RiskEngine → OMS → PaperBroker.

### Replay env vars
`REPLAY_DEFAULT_INITIAL_CASH=10000`, `REPLAY_DEFAULT_SEED=42`,
`REPLAY_STRATEGY_TICK_MS=1000`, `REPLAY_EQUITY_SNAPSHOT_MS=1000`,
`REPLAY_OUTPUT_DIR=replay_artifacts`, `REPLAY_ALLOW_GROK_NETWORK=0`,
`REPLAY_USE_CACHED_GROK=1`, `REPLAY_END_OPEN_ORDER_POLICY=cancel`,
`REPLAY_MARK_TO_MARKET=1`.

### Storage isolation
Replay writes only to `replay_runs`, `replay_events_processed`, `replay_proposals`,
`replay_risk_decisions`, `replay_orders`, `replay_fills`, `replay_positions`,
`replay_equity`, `replay_metrics`, `replay_calibration` (+ `market_outcomes`),
keyed by `replay_run_id`. The OMS runs against a **separate in-memory store**, so
operational `orders`/`fills`/`positions` are never touched. Migrations are
idempotent; existing DBs are not wiped.

### Interpreting the report
- **PnL / total return** — change in equity after fees + slippage.
- **Max drawdown** — worst peak-to-trough equity drop.
- **Sharpe / Sortino** — risk-adjusted return per equity-snapshot returns (small
  samples are flagged in `warnings`).
- **Fill ratio** — fraction of orders that got any fill; **partial-fill ratio** —
  orders that only partially filled (depth-limited).
- **Fee drag** — total fees ÷ starting cash; **average slippage** — adverse
  execution vs. quote.
- **Brier score** — mean `(p−y)²` (lower better); **log loss** — clamped to
  `[1e-6, 1−1e-6]` (no inf); **ECE** — weighted bucket gap between predicted and
  realized frequency.
- **Edge capture** — realized PnL vs. predicted edge.
- Unresolved markets are reported separately and **excluded** from realized
  calibration.

### API / dashboard
`GET /api/replay/runs`, `/api/replay/runs/{id}`, `/metrics`, `/equity`, `/orders`,
`/fills`, `/calibration`, `/report`, plus a guarded `POST /api/replay/run` for
small synchronous fixtures. The dashboard shows a **Replay / backtest** panel
(recent runs, equity, PnL, drawdown, fill ratio, Brier).

### Known limitations
- Queue priority is approximated (a depth haircut); hidden/iceberg liquidity is
  not modeled; historical market metadata may be incomplete; unresolved markets
  are excluded from realized calibration; **replay quality depends entirely on
  raw-event quality**.

**Replay is offline/simulated and submits no live orders.**

---

## Phase 5: Grok research / probability engine (research-only)

Phase 5 adds a controlled **Grok 4.3 research/probability engine** (`engine/research/`).
It turns a market question into an **audited, cached, source-backed probability
estimate**. Grok is a *research analyst* here — it may estimate probability and
supply evidence, but it **cannot execute, size, cancel, or bypass the RiskEngine**.
Strategy code may consume calibrated estimates; every order still flows through
**RiskEngine + OMS** exactly as in Phases 1–4.

### Architecture
- `grok_client.py` — `GrokResearchClient`: strict structured output, budget/rate
  limits (fail-closed), timeouts + retries, evidence persistence, secret redaction.
- `validators.py` — strict-schema validation; strips/flags any execution or size
  field; `redact()` removes API keys from every log/error string.
- `schemas.py` — `GrokProbabilityOutput`, `ProbabilityEstimateBundle`,
  `ResearchFailure`, `EvidenceItem`, `MarketRuleSummary` (Pydantic v2).
- `budget.py` — `ResearchBudget`: per-minute/hour/day + per-market caps, daily USD
  cap, env kill switch; **fails closed** when exceeded (no network call).
- `source_cache.py` / `evidence_store.py` — dedup sources by normalized URL /
  content hash; store **short excerpts only** (never full articles), linked to
  `research_run_id` + `estimate_id`.
- `market_rules.py` / `ambiguity.py` — extract resolution rules and score
  settlement ambiguity (0..1) across categories (unclear source, vague threshold,
  subjective judgment, oracle/dispute risk, …).
- `probability.py` / `ensemble.py` / `calibration_adapter.py` — deterministic,
  conservative combination of probabilities (below).
- `replay_cache.py` — `ReplayResearchCache`: returns the latest cached estimate at
  or before a replay timestamp; **never calls the network**.

### Grok / xAI integration
- Uses `XAI_API_KEY` (or `GROK_API_KEY`), default model **`grok-4.3`**,
  OpenAI-compatible xAI endpoint, strict JSON output.
- Optional server-side `web_search` / `x_search` (both **off by default**) and
  read-only client tools only.
- **Invalid schema → `ResearchFailure(VALIDATION_FAILED)`**; missing evidence →
  `NO_EVIDENCE`; any output containing order/size keys is stripped and a
  validation event is recorded. Secrets are never logged, persisted, or displayed.

### Probabilities
- `p_market_mid` — market-implied (orderbook/BBO).
- `p_llm_raw` — Grok's raw fair probability.
- `p_calibrated` — `p_llm_raw` shrunk toward 0.5 (LLMs are over-confident).
- `p_model` — optional existing feature/regime estimate.
- `p_ensemble` — conservative weighted blend. The LLM weight is reduced by low
  confidence, weak evidence, and high ambiguity; extreme blends are clamped to
  `[RESEARCH_EXTREME_PROB_CLAMP_LOW, …_HIGH]` unless evidence is exceptionally
  strong. **No size is ever produced.**

### RiskEngine integration
When a proposal carries a *required* research snapshot, the RiskEngine adds
**blocking-only** checks (additive — they never relax Phase 1–4 checks):
`research_missing`, `research_invalid_estimate`, `research_mode_not_allowed`,
`research_no_trade`, `research_estimate_stale`, `research_low_evidence`,
`research_insufficient_sources`, `research_high_ambiguity`,
`research_probability_conflict`.

### Strategy + replay
- Research enters strategy **only** when `RESEARCH_USE_IN_STRATEGY=1` (default `0`,
  so current behavior is unchanged). Even then, proposals go through RiskEngine + OMS.
- Replay consumes **cached** estimates only (`ReplayResearchCache`); it never calls
  Grok. `REPLAY_ALLOW_GROK_NETWORK` defaults to `0`.

### Run it
```bash
# One research estimate (requires an online mode; never places orders):
python scripts/run_research_once.py \
  --venue polymarket --market-id <market_id> --asset-id <asset_id> \
  --outcome YES --mode online_paper

# Export the research dataset to CSV (offline):
python scripts/export_research_dataset.py --out-dir research_export

# Evaluate calibration vs realized outcomes (offline):
python scripts/evaluate_research_calibration.py
```

### Storage
New idempotent tables: `research_runs`, `research_sources`, `research_evidence`,
`market_rule_summaries`, `probability_estimates`, `research_budget_events`,
`research_validation_events`. API keys and full prompts are never stored (only a
`prompt_hash`, unless `RESEARCH_STORE_PROMPTS=1`, and even then secrets are redacted).

### Env vars
`RESEARCH_MODE` (`disabled|offline_cache|online_paper|online_shadow|guarded_live_readonly`,
default `offline_cache`), `GROK_MODEL` (`grok-4.3`), `GROK_BASE_URL`,
`GROK_ENABLE_WEB_SEARCH`, `GROK_ENABLE_X_SEARCH`, `GROK_TIMEOUT_SECONDS`,
`GROK_MAX_RETRIES`, `RESEARCH_MAX_DAILY_COST_USD`, `RESEARCH_MAX_REQUESTS_PER_MINUTE`,
`RESEARCH_CACHE_TTL_SECONDS`, `RESEARCH_ESTIMATE_STALE_SECONDS`,
`RESEARCH_MIN_SOURCE_COUNT`, `RESEARCH_MIN_EVIDENCE_SCORE`,
`RESEARCH_MAX_AMBIGUITY_SCORE`, `RESEARCH_USE_IN_STRATEGY`,
`RESEARCH_ALLOW_TRADE_PROPOSALS`, `REPLAY_ALLOW_GROK_NETWORK`,
`REPLAY_USE_CACHED_RESEARCH`. API: `GET /api/research/{status,runs,estimates,evidence,
market-rules,budget}` and `POST /api/research/estimate` (online modes only; never
places an order).

### Known limitations
- Sources may be wrong or stale; social signals may be noisy/manipulated; stored
  snippets are not full articles; market resolution may still surprise the model;
  cached estimates can become stale quickly; calibration requires resolved outcomes.

**No real order submission, private-key/wallet signing, live broker adapter, or
Grok-driven execution exists in this phase. Grok is research-only.**

---

## Phase 6: venue-neutral layer + Kalshi (read-only market data)

Phase 6 adds a **venue-neutral prediction-market layer** (`engine/venues/`) and a
**read-only Kalshi adapter** alongside the existing Polymarket feed. Market
metadata, lifecycle, resolution rules, and binary YES/NO orderbooks are
normalized into common schemas that Research, RiskEngine, OMS, PaperBroker, and
Replay consume.

### Read-only, by construction
- **No Kalshi order placement, no cancellation, no live broker, and no private
  user channels** (`fill`, `user_orders`, `market_positions`, `order_group_updates`
  are never subscribed). The REST client (`engine/venues/kalshi/rest.py`) has
  **no** order/cancel/portfolio methods (`READ_ONLY = True`).
- Read-only auth (`engine/venues/kalshi/auth.py`) signs **only** GET requests and
  the WS handshake (RSA-PSS over `timestamp + METHOD + PATH`; the WS handshake
  signs `timestamp + "GET" + "/trade-api/ws/v2"`). There is intentionally **no**
  generic trading signer. Key material is never logged, persisted, displayed,
  sent to Grok, or written to replay artifacts (`redact()` + `__repr__` masking).
- **Missing credentials degrade gracefully**: Kalshi reports
  `disabled_missing_credentials` and Polymarket keeps working.

### Kalshi YES/NO orderbook normalization
Kalshi quotes YES bids and NO bids. The opposite-side ask is derived by binary
complement and Decimal math:
- `yes_ask = 1 − best_no_bid`  (size carried from the NO bid)
- `no_ask  = 1 − best_yes_bid` (size carried from the YES bid)

Each market keeps separate normalized YES and NO books. We track `seq` per
`market_ticker`; a gap (`seq != prev+1`) sets `gap_detected`/`needs_snapshot`,
requests a fresh snapshot via `update_subscription`/`get_snapshot`, and **the
RiskEngine rejects proposals until a snapshot restores the book**. Crossed or
out-of-`[0,1]` books are marked invalid.

### RiskEngine venue gates (blocking-only, additive)
When a proposal carries a required venue snapshot, the RiskEngine adds:
`venue_disabled`, `venue_degraded`, `market_metadata_missing`,
`market_not_tradable`, `market_closed`, `market_settled`, `orderbook_missing`,
`bbo_missing`, `stale_orderbook`, `sequence_gap_requires_snapshot`,
`invalid_orderbook_state`, `invalid_price_level`, `resolution_rules_missing`,
`settlement_ambiguity_high`, `unsupported_venue_mapping`. Existing Phase 1–5
checks are unchanged.

### PaperBroker (Kalshi binary)
`PaperBroker` consumes a normalized binary book via `KalshiBookView`: BUY YES
fills derived YES asks (from NO bids), SELL YES fills YES bids, BUY NO fills
derived NO asks (from YES bids), SELL NO fills NO bids — all in `[0,1]` with
Decimal precision. Reference-price fallback for Kalshi is **off by default**
(`PAPER_ALLOW_KALSHI_REFERENCE_PRICE_FILLS=0`): a missing book → reject, not a
fantasy fill. There is no live Kalshi submit/cancel path.

### Research + replay
Kalshi `settlement_sources`, `contract_url`/`contract_terms_url`, and
`rules_primary/secondary` feed `build_resolution_ruleset()` → the Phase 5
`AmbiguityScorer`; missing rules / non-open status → no-trade or high ambiguity.
Research cache keys include **venue** (estimates for the same `market_id` on
different venues never collide). Replay reconstructs Kalshi YES/NO books
deterministically from saved events (`engine/venues/kalshi/replay.py`), detects
sequence gaps, and never calls the network. See
`tests/fixtures/sample_kalshi_replay.jsonl`.

### API + dashboard
`GET /api/venues`, `/api/venues/status`, `/api/venues/{venue}/status|markets|
markets/{ref}|orderbook/{ref}|bbo/{ref}|lifecycle|resolution-rules/{ref}`,
`POST /api/venues/{venue}/sync-metadata`, `POST /api/venues/kalshi/smoke-readonly`
(none place orders; status shows credential *presence* only, never values). A
small **Venue Health** panel is added to the dashboard.

### Run it
```bash
python scripts/kalshi_readonly_smoke.py --env demo --max-markets 5 --seconds 30
python scripts/sync_prediction_markets.py --venue kalshi --status open --max-markets 25
python scripts/export_venue_dataset.py --venue kalshi
python scripts/run_replay.py --from-jsonl tests/fixtures/sample_kalshi_replay.jsonl --policy noop --initial-cash 10000 --seed 42
```

### How to create read-only Kalshi credentials safely
Generate an API key + RSA key pair in the Kalshi account portal, store the
private key as a file, and point `KALSHI_PRIVATE_KEY_PATH` at it (prefer a file
over inlining `KALSHI_PRIVATE_KEY_PEM`). The engine only ever signs market-data
GETs and the WS handshake — read-only auth is isolated from any execution path.

### Storage
New idempotent tables: `venue_markets`, `venue_series`, `resolution_rules`,
`venue_lifecycle_events`, `kalshi_orderbook_snapshots`, `kalshi_orderbook_deltas`,
`venue_market_data_health`. Raw Kalshi WS events reuse Phase 2 `raw_market_events`
with `source=kalshi_ws`. No secrets are stored.

### Env vars
`VENUES_ENABLED`, `VENUE_REQUIRE_RESOLUTION_RULES`, `VENUE_MAX_SETTLEMENT_AMBIGUITY`,
`KALSHI_ENABLED`, `KALSHI_ENV`, `KALSHI_REST_BASE_URL`, `KALSHI_WS_URL`,
`KALSHI_ACCESS_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_PRIVATE_KEY_PEM`,
`KALSHI_PRIVATE_KEY_PASSWORD`, `KALSHI_SYNC_*`, `KALSHI_MAX_MARKETS`,
`KALSHI_WS_*`, `PAPER_ALLOW_KALSHI_REFERENCE_PRICE_FILLS`. See `.env.example`.

### Known limitations
- Kalshi's WebSocket requires an authenticated connection even for market data.
- Sequence-gap recovery depends on a successful snapshot refresh.
- Derived-ask logic assumes binary complement pricing.
- Settlement metadata quality varies by market/series.
- No live order execution, no user portfolio/fill monitoring, no private channels.

**No real order submission, Kalshi order placement, Kalshi cancellation, live
broker adapter, or private user-channel subscription was added. Kalshi is
read-only market data + metadata.**

---

## Phase 7: shadow-mode orchestration (no live orders)

Phase 7 adds **shadow mode** (`engine/shadow/`): it runs the *full live decision
stack* on live read-only data **without submitting orders**. It answers: *"If this
exact system had been allowed to trade live, what would it have proposed, what
would RiskEngine have approved/rejected, what would simulated execution have done,
and what did the market do afterward?"*

### Modes
- **paper** — operational simulated trading (existing).
- **replay** — offline deterministic backtest (Phase 4).
- **shadow_live** — *this phase*: live read-only data, would-have-traded
  decisions, simulated fills, outcome tracking, readiness report. **No orders.**
- **guarded_live_design_only** — a future, human-designed phase. Not implemented.
  `guarded_live` / `live` / `real_money` execution are **not** added here.

### Flow (every cycle, per market)
candidate selection → (cached/online) research → deterministic decision → **RiskEngine**
→ if approved **and** allowed → **ShadowOMS → PaperBroker** (simulated) → schedule
outcome observations. Every step is persisted to isolated `shadow_*` tables.

- **Research** is research-only and cached by default
  (`SHADOW_ALLOW_ONLINE_RESEARCH=0`). Grok can influence `p_ensemble` but can
  never set size, side, or bypass risk.
- **Sizing** is fixed from config (`SHADOW_DEFAULT_NOTIONAL_USD`), never from Grok.
- **Fills** are simulated by the Phase 3 PaperBroker against the live local book;
  Kalshi reference fills stay off (`PAPER_ALLOW_KALSHI_REFERENCE_PRICE_FILLS=0`).
- **Distinct from paper:** shadow writes only `shadow_*` tables, tagged
  `mode=shadow_live` + `shadow_session_id` + `decision_id` + `proposal_id`.

### Fails closed
Shadow blocks new orders on: kill switch, CRITICAL alert, reconciliation failure,
degraded venue, stale data, sequence gap, invalid book, high ambiguity, low
evidence, budget exhaustion, or RiskEngine rejection. One failing cycle degrades
the session but never crashes the process.

### Outcome tracking / markout
For each decision the `ShadowOutcomeTracker` captures BBO/midpoint/spread/last
trade (and resolution if available) at configured horizons
(`SHADOW_OUTCOME_HORIZONS_MS`, default `0,5s,30s,60s,5m,15m,1h`) and computes
**markout** (`midpoint_after − fill_price`, signed by side), adverse selection,
and edge capture. Missing observations are tolerated (recorded as null).

### Live-readiness gates (never auto-live)
`LiveReadinessGate` produces PASS/FAIL/NOT_ENOUGH_DATA per gate and an overall
status: `NOT_READY`, `NOT_ENOUGH_DATA`, `SHADOW_STABLE_BUT_NOT_APPROVED`, or
`READY_FOR_MANUAL_REVIEW`. **There is no auto-live status.** Hard FAIL gates
include `risk_bypass_count==0`, `live_order_endpoint_calls==0`,
`reconciliation_clean`, `no_real_broker_configured`, plus data-quality
(stale/parse/sequence-gap/uptime), execution (fill ratio, edge capture, reject
rate), performance (drawdown, PnL), and calibration (Brier/log-loss/ECE) gates.
`READY_FOR_MANUAL_REVIEW` means *a human may begin designing a guarded-live phase*
— it never enables live trading.

### Run it
```bash
python scripts/run_shadow.py --dry-run-config
python scripts/run_shadow.py --duration-minutes 60 --venues polymarket --cached-research-only
python scripts/run_shadow.py --fixture tests/fixtures/sample_shadow_session.jsonl \
    --venues polymarket --cached-research-only --force-shadow
python scripts/summarize_shadow.py --latest
python scripts/generate_shadow_report.py --latest
python scripts/check_live_readiness.py --latest --fail-on-not-ready   # nonzero unless READY_FOR_MANUAL_REVIEW
```

### Interpreting
- **fill ratio** — filled/total simulated orders (low ⇒ unmarketable prices/thin book).
- **edge capture** — realized PnL ÷ predicted edge (is the edge real after costs?).
- **markout / adverse selection** — did the market move against the decision shortly after?
- **stale book / sequence gap rate** — live data reliability (must be tiny for readiness).
- **risk rejection rate** — explainable rejections, not pathological.
- **Brier / log-loss / ECE** — probability calibration on *resolved* markets only.

### API + dashboard
`GET /api/shadow/status`, `POST /api/shadow/{start,stop,pause,resume}`,
`GET /api/shadow/sessions[...]/{candidates,decisions,orders,fills,positions,equity,
observations,metrics,alerts}`, `GET /api/shadow/readiness`,
`POST /api/shadow/sessions/{id}/readiness/report`, `GET /api/shadow/readiness/reports[...]`.
`start` fails unless `SHADOW_ENABLED=1`, verifies `shadow_live` mode + no live
broker, and never submits orders. A small **Shadow Mode** dashboard panel shows
status, counts, and readiness. Secrets are never returned.

### Env vars (see `.env.example`)
`SHADOW_ENABLED`, `SHADOW_VENUES`, `SHADOW_START_ON_BOOT`, `SHADOW_DECISION_INTERVAL_MS`,
`SHADOW_MAX_CANDIDATES_PER_CYCLE`, `SHADOW_DEFAULT_NOTIONAL_USD`,
`SHADOW_MIN_EDGE_AFTER_COSTS`, `SHADOW_MAX_STALE_MS`, `SHADOW_USE_RESEARCH`,
`SHADOW_ALLOW_ONLINE_RESEARCH`, `SHADOW_USE_CACHED_RESEARCH`,
`SHADOW_MIN_DECISIONS_FOR_READINESS`, `SHADOW_MIN_RUNTIME_HOURS_FOR_READINESS`,
`SHADOW_REQUIRED_VENUE_UPTIME_PCT`, `SHADOW_MAX_DRAWDOWN_PCT`, `SHADOW_OUTPUT_DIR`,
`SHADOW_KILL_SWITCH_PATH`.

### Known limitations
- Shadow fills are still simulated; queue priority and market impact are approximate.
- Hidden liquidity is unknown; short shadow runs do not prove profitability.
- Unresolved markets limit calibration.
- Live trading requires a separate guarded-live adapter design and manual review.

**Phase 7 does not add real order submission, real cancellation, live broker
adapters, Polymarket wallet/private-key signing, Kalshi order endpoints, or
private user-channel subscriptions.**

---

## Phase 8: guarded-live design skeleton (DRY-RUN ONLY — door stays shut)

Phase 8 adds `engine/guarded_live/`: a **design skeleton** that specifies exactly
how real execution *would* be integrated later, while keeping every execution
method **hard-disabled**. The principle: *design the door, install the locks, do
not open the door.* **Live execution remains impossible.**

### Modes (recap + new)
`paper` · `replay` · `shadow_live` · **`guarded_live_design_only`** ·
**`guarded_live_dry_run_only`** · *future* `guarded_live` execution (NOT
implemented). There is no `live` / `real_money` / `production_execution` path.

### What's locked
- Every `submit_order` / `cancel_order` / `replace_order` / `post_order` /
  `create_order` / `create_and_post_order` / `create_market_order` raises
  **`LiveExecutionDisabled`** (base interface + `DisabledLiveBroker`, the default).
- No live broker, no Polymarket EIP-712 signing, no Kalshi order placement, no
  private user channels, no wallet/private key loaded.
- `DryRunLiveBroker` only ever produces an **UNSIGNED / UNSENT** `DryRunOrderIntent`
  (`signer_used=False`, `network_called=False`) and requires both a RiskEngine
  decision id and a SafetyEnvelope decision id.

### State machine (no live state)
`DISABLED → DESIGN_ONLY → PRECHECK_PASSED → AWAITING_APPROVAL →
APPROVED_DRY_RUN_ONLY → ARMED_DRY_RUN_ONLY → DRY_RUN_ACTIVE` (+ `PAUSED`,
`KILL_SWITCHED`, `EXPIRED`, `STOPPED`, `FAILED`). Any attempt to enter a live
state (`LIVE_ACTIVE`, `REAL_MONEY_ACTIVE`, `PRODUCTION_EXECUTION`, `AUTO_LIVE`)
raises `GuardedLiveStateError`.

### Gates before any future live path
A future guarded-live path requires ALL of: a shadow `READY_FOR_MANUAL_REVIEW`
report (fresh), passing **prechecks**, **two-person manual approval** (typed
confirmation; no automated/Grok actor), a short-lived **dry-run-only arming
token** (hashed in storage, shown once, constant-time verify), a kill-switch
check, a **SafetyEnvelope** pass, a **conformance** pass, and a **secret-policy**
pass. Approvals expire and are invalidated by config changes.

### SafetyEnvelope
Validates every would-be (dry-run) order against ~28 checks: mode/state,
live-execution-disabled, kill switch, readiness/approvals/arming, notional +
market/venue/total exposure, daily loss, edge, freshness, spread, orderbook
validity, venue status, sequence gap / tick dirty, market status, ambiguity,
evidence/source count, close time, no private channel, no forbidden endpoint,
secret policy, reconciliation. Fails closed.

### Conformance harness
Proves — with no real network/order/signing — that execution methods are
disabled, dry-run intents are unsigned/unsent and require Risk + Safety, the
state machine has no live state, secrets are redacted, and forbidden env/endpoints
are detected. Test traps make it FAIL if a network/order/signing call ever fires.

### Secret policy
Forbids execution secrets in guarded-live mode (`POLYMARKET_PRIVATE_KEY`,
`KALSHI_TRADING_PRIVATE_KEY`, `LIVE_BROKER_ENABLED`, `ENABLE_REAL_ORDERS`,
`REAL_MONEY`, `PRODUCTION_EXECUTION`, …). Redacts secret-looking strings; never
reads or prints secret values.

### Run it
```bash
python scripts/guarded_live_conformance.py
python scripts/guarded_live_precheck.py --readiness-report-fixture tests/fixtures/sample_ready_for_manual_review_report.json
python scripts/guarded_live_approve.py --help
python scripts/guarded_live_arm_dry_run.py --help
python scripts/guarded_live_dry_run_order.py --venue kalshi --ticker EXAMPLE-TICKER --outcome YES --side BUY --price 0.45 --quantity 1
python scripts/guarded_live_report.py --latest
```

### Storage / API / dashboard
New idempotent tables: `guarded_live_state`, `guarded_live_prechecks(+results)`,
`manual_approvals`, `approval_batches`, `arming_tokens` (hash only),
`dry_run_order_intents`, `safety_envelope_decisions`, `conformance_runs(+checks)`,
`secret_policy_violations`, `guarded_live_audit_events`. API under
`/api/guarded-live/*` (status/precheck/approval-batches/arming/dry-run-intent/
conformance/report/audit/secret-policy) — all labeled `dry_run_only` /
`no_live_execution`, none submit orders. A read-only **Guarded Live Design** panel
shows state/precheck/conformance/kill-switch (no live/submit/wallet buttons).

### Env vars (see `.env.example`)
`GUARDED_LIVE_ENABLED` (only permits design/dry-run), `GUARDED_LIVE_MODE`,
`GUARDED_LIVE_DRY_RUN_ONLY`, required-readiness + approval + arming knobs, safety
envelope caps, kill-switch paths, and `GUARDED_LIVE_SECRET_POLICY_STRICT`.

### Required evidence before ANY future live-execution phase
shadow readiness report · conformance pass · manual approvals · secret-policy
pass · dry-run mapper validation · risk-envelope validation · kill-switch test ·
reconciliation clean — **plus a separate, manually-reviewed guarded-live adapter
design.** Live enablement is never automatic.

### Known limitations
No live orders, no real cancellations, no exchange acknowledgements, no private
user-channel reconciliation. Dry-run payloads are NOT signed and prove nothing
about exchange acceptance. Manual review is still required.

**Phase 8 does not add real order submission, real order cancellation, live
broker adapters, Polymarket wallet/private-key signing, Kalshi order placement,
or private user-channel subscriptions — and live execution remains impossible.**

---

## What you see on the dashboard

It closely follows the design you shared:

- **Big live P&L number** at the top (your pretend profit/loss), plus equity,
  win rate, Sharpe, and latency.
- **BTC 5-min Pulse** — a binary "will BTC be higher or lower in 5 minutes?"
  game with a live countdown, the price-to-beat, the current price, and UP/DOWN
  odds in cents. The bot places a pretend bet each round when it sees an edge.
- **Grok Brain line** — when enabled, shows Grok's UP/DOWN/HOLD call,
  confidence, and a one-line reason.
- **Markov Regime Model** — a live 3-state (Bull / Bear / Sideways) model.
- **Monte Carlo (500 paths)** — a 5-minute price forecast fan + distribution.
- **Pattern Scanner** — BOS / CHoCH / Liquidity-Sweep signals.
- **Trade Log** — recent pretend trades across crypto, stocks, and Polymarket.

The autonomous bot trades all three markets:
- **Crypto** (BTC/ETH/SOL) — momentum paper positions.
- **Stocks** (AAPL/NVDA/TSLA/SPY) — momentum paper positions (Yahoo data).
- **Polymarket** — pretend YES/NO bets on trending public markets.

No API keys are required for the market data — it all comes from free public
sources. Grok is optional (see below).

---

## How to run it on your Windows PC (with Docker Desktop)

You don't need to know any coding. Just follow these steps.

1. Make sure **Docker Desktop** is open and running (whale icon in the tray).
2. In **GitHub Desktop**, click **Fetch origin** / **Pull** to get the latest
   files, and select the branch I tell you to use.
3. Open a terminal: in GitHub Desktop, click the **Repository** menu →
   **Open in Command Prompt**.
4. Go into this folder:
   ```
   cd hermes-agent-main\plugins\hermes-trading-engine
   ```
5. Start it (first run downloads things — give it a few minutes):
   ```
   docker compose up --build
   ```
6. When you see `Uvicorn running on http://0.0.0.0:8800`, open your browser to:
   ```
   http://localhost:8800
   ```
7. To **stop** it: click the terminal window and press **Ctrl + C**.
   To stop and clean up: `docker compose down`.

### Buttons on the dashboard

- **AUTOTRADE** — turn the bot's pretend trading on/off.
- **RESET** — clear all pretend trades and start the pretend balance fresh.

---

## Turning on the Grok "brain" (optional)

By default the bot decides using math models (Markov + Monte Carlo). You can
add **Grok** as an extra brain that reads all the signals *plus the bot's own
recent win/loss record* and gives a smarter, more selective call. Over time the
bot learns from its track record — that's the "training on paper money" part.

> Heads up: Your **SuperGrok subscription does NOT work for this.** SuperGrok is
> the chatbot on grok.com. Programs need a separate **developer API key** from
> **console.x.ai** (free signup, ~$25 free credits, then very cheap pay-per-use).

Steps:

1. Go to **https://console.x.ai** and sign up (you can log in with your X
   account, but you still create a developer account there).
2. On the left, open **API Keys** and create one. It starts with `xai-`.
   Copy it.
3. Add a small amount of credits if asked (the free credits are usually enough
   to start).
4. In this folder, make a copy of the file **`.env.example`** and name the copy
   exactly **`.env`**. Open it in Notepad and paste your key:
   ```
   XAI_API_KEY=xai-your-key-here
   ```
   Save the file.
5. Start (or restart) the engine:
   ```
   docker compose up --build
   ```

On the dashboard, the **GROK BRAIN** line will switch from "OFF" to showing
live UP/DOWN/HOLD calls with reasons. If you ever see an error there, it usually
means the key is wrong or out of credits.

To turn Grok back off, just empty the `XAI_API_KEY` line (or delete `.env`) and
restart.

### Want the Hermes chat agent itself to use Grok too?

Separately from this dashboard, the main Hermes agent can *think* with Grok:
- Put the same key in Hermes' own secrets file (`~/.hermes/.env`) as
  `XAI_API_KEY=...`, then run `hermes model` and pick **grok**. Hermes already
  ships the xAI provider, so no code changes are needed.

---

## Settings you can change (optional)

Edit `docker-compose.yml` (lines under `environment:`), then restart with
`docker compose up`. Common ones:

| Setting | Meaning | Default |
|---|---|---|
| `HTE_STARTING_BALANCE` | Pretend starting money | `100000` |
| `HTE_AUTOTRADE` | Bot trades on its own (`1`=yes, `0`=no) | `1` |
| `HTE_PULSE_ROUND_SECONDS` | Length of each BTC pulse round | `300` (5 min) |
| `HTE_MAX_STAKE_FRACTION` | Max % of money risked per bet | `0.02` (2%) |
| `HTE_DAILY_LOSS_LIMIT` | Bot pauses after losing this % in a day | `0.10` (10%) |
| `HTE_CRYPTO_SYMBOLS` | Which coins to trade | `BTCUSDT,ETHUSDT,SOLUSDT` |
| `HTE_STOCK_SYMBOLS` | Which stocks to trade | `AAPL,NVDA,TSLA,SPY` |
| `HTE_GROK_MODEL` | Which Grok model to use | `grok-4.1-fast` |
| `HTE_GROK_REFRESH_SECONDS` | How often Grok re-decides | `30` |

---

## Using it from inside Hermes (optional)

This folder is also a **Hermes plugin**. If you run the Hermes agent, it gains
three tools so you can chat about the engine:

- `trading_status` — "What's my paper P&L right now?"
- `trading_set_autotrade` — "Pause the paper bot."
- `trading_reset` — "Reset the paper portfolio."

The plugin talks to the running dashboard at `http://localhost:8800` (change
with `HTE_URL` if needed).

---

## Frequently asked

**Is any real money involved?** No. Never. It's a simulation against live prices.

**Does it need exchange/broker/Polymarket accounts?** No.

**Does it need Grok?** No — Grok is optional. Without it the bot uses its math
models. With it, the bot gets an extra LLM opinion and learns from its record.

**Will it keep my history if I restart?** Yes — saved in a Docker volume named
`hte_data`. "RESET" or `docker compose down -v` clears it.

**Why does the stock part go quiet sometimes?** Stock data (Yahoo) only moves
during US market hours; crypto and the BTC pulse run 24/7.

---

## What this is NOT (yet)

This is a **paper** engine on purpose. It does not, and will not without an
explicit, separate, security-reviewed build, place real orders. Going live would
require trade-enabled API keys, order-signing, hardened risk controls, and a
deliberate decision to risk real capital — none of which are in here.


## Phase 9 — Micro-Live Canary Execution (gated; DISABLED by default)

Phase 9 adds the **first real-execution surface**: a guarded micro-live adapter
that can submit **exactly one tiny canary order at a time**. It is **micro-live
canary execution, not autonomous live trading**.

- **Disabled by default.** Source build lock (`MICRO_LIVE_BUILD_ENABLED`, a code
  constant, not env) defaults `False`; runtime lock `MICRO_LIVE_ENABLED=0`.
- **Demo is the default** (`MICRO_LIVE_ENV=demo`). **Production requires an extra
  unlock** (`MICRO_LIVE_ALLOW_PRODUCTION=1`).
- **CLI-only real submission.** No dashboard live-submit button. No API submit
  endpoint. No strategy loop and no Grok/research path can submit, cancel, arm,
  approve, or size anything.
- **FOK / `fill_or_kill` only.** No GTC/GTD/resting maker orders, no market
  making, no batch orders, no amend/replace.
- **Hard caps enforced in code** (not just env): order notional can never exceed
  **$1** regardless of configuration.

### All locks/gates required before a real submit
source build lock · runtime env lock · real-money acknowledgement phrase ·
demo/prod lock · CLI-only lock · single-order-per-token lock · kill switches
clear · Phase 8 conformance PASS · latest shadow readiness
`READY_FOR_MANUAL_REVIEW` · micro-live preflight PASS · approval batch · arming
token · SafetyEnvelope PASS · RiskEngine PASS · venue allowlist · market
allowlist · account snapshot · reconciliation healthy. **If anything is
ambiguous, it fails closed.**

### Required sequence
1. Long shadow session → 2. `READY_FOR_MANUAL_REVIEW` readiness report →
3. Phase 8 conformance PASS → 4. Guarded-live precheck PASS → 5. Manual approvals →
6. Arming token → 7. Dry-run intent → 8. Canary plan → 9. Micro-live preflight →
10. CLI typed confirmation → 11. Single canary submit → 12. Reconciliation →
13. **Stop and review** (manual review required after every canary).

### Emergency cancel / idempotency / unknown-status / reconciliation
- **Emergency cancel** is CLI-only, explicit, audited, requires the exact typed
  confirmation `EMERGENCY CANCEL MICRO LIVE ORDER`, never places a new order.
- **Idempotency:** a deterministic `client_order_id` (`mlt-<venue>-<date>-<hash>-<nonce>`)
  is persisted **before** the network call; a network timeout marks the order
  `UNKNOWN` and **never** resubmits — it requires manual reconciliation.
- **Unknown status blocks** any further live order. Partial fills and
  reconciliation mismatches also block further orders.
- **Reconciliation is mandatory** after every submit (REST polling).

### Secret handling
Trading credentials are loaded only **after all locks pass**, kept in memory,
and are never logged, persisted, shown in API responses, sent to Grok, included
in reports, or written to artifacts. Only payload **hashes** / redacted metadata
are stored. The risk-acknowledgement phrase is redacted in lock output.

### Polymarket status
**Safely NOT implemented for live signing.** The Polymarket broker validates a
would-be FOK payload shape but raises `POLYMARKET_LIVE_SIGNING_NOT_IMPLEMENTED`
on any submit; it cannot sign or move funds. Kalshi demo micro-execution is the
only implemented live path.

### How to run
```bash
python scripts/micro_live_locks.py
python scripts/micro_live_create_canary_plan.py --help
python scripts/micro_live_preflight.py --canary-plan-id <id>
python scripts/micro_live_submit_canary.py --canary-plan-id <id> --arming-token <token>
python scripts/micro_live_reconcile.py --latest
python scripts/micro_live_emergency_cancel.py --help
python scripts/micro_live_report.py --latest
python scripts/micro_live_conformance.py
```
Fully-mocked demo canary (no real network, mocked exchange only):
```bash
python scripts/micro_live_create_canary_plan.py \
  --fixture tests/fixtures/sample_micro_live_dry_run_intent.json \
  --readiness-report-fixture tests/fixtures/sample_ready_for_manual_review_report.json \
  --venue kalshi --environment demo
python scripts/micro_live_submit_canary.py --canary-plan-id <id> \
  --arming-token test_fixture_token --non-interactive-test-fixture \
  --confirm "SUBMIT ONE MICRO LIVE CANARY ORDER"
```

### Key env vars
`MICRO_LIVE_BUILD_ENABLED`, `MICRO_LIVE_ENABLED`, `MICRO_LIVE_ENV`,
`MICRO_LIVE_ALLOW_PRODUCTION`, `MICRO_LIVE_ACKNOWLEDGE_REAL_MONEY_RISK`,
`MICRO_LIVE_MAX_ORDER_NOTIONAL_USD`, `MICRO_LIVE_ALLOWED_VENUES`,
`MICRO_LIVE_ALLOWED_ORDER_TYPES`, `MICRO_LIVE_ALLOWED_TIF`,
`KALSHI_MICRO_LIVE_ENABLED`, `KALSHI_MICRO_LIVE_ENV`,
`KALSHI_TRADING_ACCESS_KEY_ID`, `KALSHI_TRADING_PRIVATE_KEY_PATH`,
`POLYMARKET_MICRO_LIVE_ENABLED`, `POLYMARKET_PRIVATE_KEY_PATH`,
`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`.

### Known limitations
One canary order only · no autonomous live execution · no production by default ·
no live scaling · no market making · no resting orders · no batch orders · no
amend/replace · exchange acceptance still depends on account
eligibility/funds/permissions · tests use mocked exchanges · demo may differ from
production · **manual review required after every canary**.

> **Phase 9 adds a gated micro-live execution path, but it remains disabled by
> default and must not run without all locks, approvals, and manual CLI
> confirmation.**


## Phase 10 — Post-Canary Analysis & Scaling VETO (analysis only)

Phase 10 ingests every Phase 9 micro-live canary attempt and produces a **hard
recommendation**. It is **post-canary analysis and a scaling veto** — it does
**not** scale size, **does not** enable production, **does not** enable autonomous
live trading, and **adds no submit/cancel endpoints**.

> A canary is not a success because it submitted. A canary is a success only if
> the entire chain was clean: shadow evidence → approvals → arming → dry-run
> intent → preflight → submit → ack → fills → reconciliation → account state →
> markout → report → manual review.

### Analysis categories
`reconciliation` · `execution quality` · `markout` · `market data` · `research`
· `risk` · `audit chain` · `secrets` · `eligibility`.

### Veto recommendations (the only allowed outputs)
- `STOP`
- `FIX_AND_REPEAT_SHADOW`
- `REPEAT_DEMO_CANARY_SAME_SIZE`  ← maximum positive recommendation
- `MANUAL_REVIEW_FOR_PRODUCTION_CANARY_DESIGN`
- `MANUAL_REVIEW_FOR_NEXT_PHASE`

It can **never** return `AUTO_SCALE`, `INCREASE_SIZE`, `ENABLE_AUTONOMOUS_LIVE`,
`ENABLE_PRODUCTION`, `READY_FOR_PRODUCTION`, or `READY_FOR_LIVE_LOOP`
(`veto.assert_safe` downgrades any such value to `STOP`).

- **"Production design review" is NOT production execution.** It only authorizes
  a manual design review; production order submission remains unimplemented.
- **"Repeat demo same size" is NOT a size increase.** `eligible_for_size_increase`
  and `eligible_for_autonomous_live` are forced `False` in code in every scenario.

### Required sequence after a canary
1. Terminal exchange status → 2. Reconciliation clean → 3. Post-canary analysis →
4. Veto report → 5. Manual review → 6. Renewed shadow evidence → 7. Optional
same-size demo canary **only if** the veto allows `REPEAT_DEMO_CANARY_SAME_SIZE`.

### Hard vetoes (immediate STOP)
unknown exchange status · missing/mismatched reconciliation · duplicate
client/exchange order id · idempotency failure · secret leak · forbidden network
call · missing RiskEngine/SafetyEnvelope decision · over-notional · payload drift
· wrong venue/environment/market/side/outcome · unexpected resting order · failed
emergency cancel · missing audit chain / chain-hash mismatch · kill switch active
at submit · market closed/resolved at submit · sequence/tick dirty (FIX) ·
grok-triggered live action.

### How to run
```bash
python scripts/post_canary_analyze.py --latest
python scripts/post_canary_report.py --latest
python scripts/post_canary_eligibility.py --venue kalshi --environment demo
python scripts/post_canary_veto.py --latest --fail-on-stop
python scripts/post_canary_export_dataset.py --out post_canary_dataset
```
Fully-mocked fixture analysis (no network, no orders):
```bash
python scripts/post_canary_analyze.py --fixture tests/fixtures/sample_clean_demo_canary.json
```

### Env vars
`POST_CANARY_ENABLED`, `POST_CANARY_AUTO_ANALYZE_AFTER_SUBMIT`,
`POST_CANARY_REQUIRE_ANALYSIS_BEFORE_NEXT_CANARY`, `POST_CANARY_MARKOUT_HORIZONS_MS`,
`POST_CANARY_MAX_ACK_LATENCY_MS`, `POST_CANARY_MAX_RECONCILIATION_LATENCY_MS`,
`POST_CANARY_MAX_SLIPPAGE_BPS`, `POST_CANARY_ALLOW_PARTIAL_FILL`,
`POST_CANARY_ALLOW_UNEXPECTED_RESTING_ORDER`,
`POST_CANARY_MIN_CLEAN_DEMO_CANARIES_FOR_PROD_REVIEW`,
`POST_CANARY_MIN_RENEWED_SHADOW_HOURS_AFTER_CANARY`,
`POST_CANARY_SIZE_INCREASE_ALLOWED` (forced 0), `POST_CANARY_AUTONOMOUS_LIVE_ALLOWED`
(forced 0), `POST_CANARY_PRODUCTION_CANARY_IMPLEMENTED` (forced 0).

### API (read-only / non-execution)
`GET /api/post-canary/analyses[/{id}[/checks|/markout|/report]]`,
`GET /api/post-canary/eligibility`, `GET /api/post-canary/latest`,
`POST /api/post-canary/analyze` (no order calls), `POST /api/post-canary/report`.
There is **no** submit / cancel / scale / production-unlock / size-increase route.

### Known limitations
markout depends on captured market-data quality · exchange API fields vary by
venue · demo behavior may differ from production · short canary history does not
prove profitability · clean canaries do not authorize scaling · production
execution remains unimplemented · manual review remains mandatory.

> **Phase 10 does not authorize size increase, autonomous live trading,
> production execution, dashboard live submit, or strategy live routing.**


## Phase 11 — Production-Canary DESIGN REVIEW (review only)

Phase 11 determines whether the system is organizationally, operationally,
technically, and safety-wise ready to *design* a future production canary. It
produces a review dossier, mock-only production conformance, manual attestations,
endpoint-separation + credential-custody audits, runbooks, change control, and a
human checklist, then a formal veto.

> A production canary is not a code feature — it is an operational decision that
> requires evidence, account readiness, legal/eligibility review, exchange-
> permission review, secret-custody review, incident-response review, and human
> signoff.

**Phase 11 does NOT** add production execution, production cancellation,
production signing, size increase, or autonomous live trading. It adds no
submit/cancel/scale/arm API routes and no dashboard production controls.

### Shadow vs demo canary vs post-canary vs design review vs implementation
- **Shadow readiness** (Phase 7): would-have-traded decisions, no orders.
- **Demo micro-live canary** (Phase 9): one tiny real FOK order on the **demo**
  venue, disabled-by-default, CLI-only.
- **Post-canary analysis** (Phase 10): forensic audit + scaling **veto** of each
  canary.
- **Production design review** (Phase 11): decides whether you may *design* a
  production canary — **not** execute one.
- **Future production canary implementation** (Phase 12+): not in this repo.

### Analysis categories
evidence loading · account readiness · venue permissions · jurisdiction/
eligibility attestation · endpoint separation · credential custody · mock-only
production conformance · operational readiness (runbooks) · change control ·
human checklist.

### Recommendations (the only allowed outputs)
`NOT_READY` · `FIX_AND_REPEAT_SHADOW` · `FIX_AND_REPEAT_DEMO_CANARIES` ·
`READY_FOR_PRODUCTION_CANARY_DESIGN_REVIEW` ·
`APPROVED_TO_DRAFT_PHASE12_PRODUCTION_CANARY_PLAN` (maximum positive). It can
**never** return `READY_FOR_PRODUCTION_EXECUTION`, `ENABLE_PRODUCTION`,
`AUTO_PRODUCTION`, `INCREASE_SIZE`, or `ENABLE_AUTONOMOUS_LIVE`
(`veto.assert_safe` downgrades any such value to `NOT_READY`).

- **"Production design review" is NOT production execution.** It only authorizes
  a manual design review / drafting a Phase 12 plan.
- **Jurisdiction/eligibility attestation is manual and is NOT legal/tax advice.**
  The bot does not infer eligibility; missing/bot-authored/expired attestation is
  FAIL.
- **Endpoint separation** statically + at runtime proves no production order/
  cancel/funding endpoint is reachable (network guard blocks them).
- **Credential custody** scans `.env.example`, artifacts, and report candidates;
  any raw production secret is a CRITICAL FAIL. Keys are referenced by path, never
  loaded or stored.
- **Mock-only production conformance** makes zero real network calls and proves
  all production execution methods raise `ProductionExecutionNotImplemented`.

### How to run
```bash
python scripts/production_review_run.py
python scripts/production_review_conformance.py
python scripts/production_review_attest.py --help
python scripts/production_review_checklist.py --latest
python scripts/production_review_report.py --latest
python scripts/production_review_export_dossier.py --latest
python scripts/production_review_veto.py --latest
```
Fully-mocked ready dossier (no network, no execution):
```bash
python scripts/production_review_run.py \
  --fixture tests/fixtures/sample_production_review_ready_dossier.json \
  --include-mock-conformance
python scripts/production_review_veto.py --latest --fail-on-not-approved-to-draft-phase12
```

### Env vars
`PRODUCTION_REVIEW_ENABLED`, `PRODUCTION_REVIEW_ALLOW_READONLY_ACCOUNT_SNAPSHOT`,
`PRODUCTION_REVIEW_ALLOW_PRODUCTION_NETWORK`, `PRODUCTION_REVIEW_MOCK_ONLY_CONFORMANCE`,
`PRODUCTION_REVIEW_MIN_CLEAN_DEMO_CANARIES`, `PRODUCTION_REVIEW_MIN_RENEWED_SHADOW_HOURS`,
`PRODUCTION_REVIEW_MIN_RENEWED_SHADOW_DECISIONS`,
`PRODUCTION_REVIEW_REQUIRE_JURISDICTION_ATTESTATION`,
`PRODUCTION_REVIEW_REQUIRE_ACCOUNT_READINESS_ATTESTATION`,
`PRODUCTION_REVIEW_REQUIRE_SECRET_ROTATION_PLAN`,
`PRODUCTION_REVIEW_REQUIRE_INCIDENT_RESPONSE_PLAN`,
`PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION` (ignored/forced off; setting =1 FAILS the review),
`PRODUCTION_REVIEW_ALLOW_SIZE_INCREASE` (ignored), `PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE` (ignored).

### API (read-only / non-execution)
`GET/POST /api/production-review/run|runs|status|evidence|conformance|attestations|
change-control|checklist` (+ run sub-resources). There is **no** submit / cancel /
enable-production / increase-size / scale / arm-production / live-order route.

### Known limitations
Design review does not prove production execution safety · manual attestations are
required · no legal/tax/compliance advice is provided · production exchange
behavior may differ from demo · production canary implementation remains a future
phase · account eligibility must be confirmed outside the bot · production secrets
are not loaded by this phase.

> **Phase 11 does not authorize or implement production order submission,
> production cancellation, production signing, size increase, or autonomous live
> trading.**


## Operating Modes & Safe First-Run Guide (feature freeze)

Feature development is frozen. The system runs in one of these modes; all
live/micro/production flags are **disabled by default**:

| Mode | What it does | Live orders? |
|---|---|---|
| `paper` | Simulated OMS + PaperBroker, RiskEngine-gated | No |
| `replay` | Deterministic offline backtest (no network) | No |
| `shadow_live` | Live read-only data → would-have-traded decisions (ShadowOMS/PaperBroker) | No |
| `guarded_live_design_only` | Dry-run live-broker design skeleton; execution hard-disabled | No |
| `micro_live` (disabled by default) | One tiny FOK **demo** canary, CLI-only, all locks required | Only via explicit CLI + every lock open |
| `production_review` | Design-review-only dossier + veto | No (production execution unimplemented) |

### Safe first-run sequence
```bash
cp .env.example .env          # keep ALL live/micro/prod flags disabled (defaults)
python -m compileall -q engine __init__.py
pytest -q                     # all unit tests, no network
python scripts/run_replay.py --from-jsonl tests/fixtures/sample_polymarket_replay.jsonl \
    --policy noop --initial-cash 10000 --seed 42
python scripts/run_shadow.py --dry-run-config
python scripts/guarded_live_conformance.py
python scripts/micro_live_conformance.py
python scripts/micro_live_locks.py --json      # live_submit_blocked must be true
python scripts/production_review_conformance.py
# Only after all of the above pass, run read-only venue smokes (below).
```

### Manual smoke tests (read-only / no real orders)
```bash
python scripts/polymarket_clob_smoke.py        # Polymarket read-only market data
python scripts/kalshi_readonly_smoke.py        # Kalshi read-only demo
python scripts/run_research_once.py            # Grok online research (research-only)
```
These are the ONLY scripts that touch the network, and they place **no orders**.

### Explicit warnings
- **No real funds during initial testing.** Keep micro-live disabled.
- **Do not enable micro-live** until all conformance/readiness gates pass; even
  then it is demo-only, CLI-only, one-canary, FOK-only, hard-capped to $1.
- **Production execution is NOT implemented or authorized** by the production
  review — its maximum positive outcome only approves *drafting* a future plan.
- **Grok is research-only.** It cannot place, cancel, approve, arm, scale, or
  size any order.

See `TESTING_READINESS_REPORT.md` for the latest audited build status.
