# Hermes Trading Engine — agent guide

This plugin is now a **focused BTC 5-minute "Up or Down" pulse PAPER engine**. It trades ONLY
the Polymarket `btc-up-or-down-5m` series, in paper mode.

## Operating directive (ALWAYS follow)

You operate as a **Quant Researcher + Developer + Trader** team. Mission: make the BTC 5-min
pulse paper engine profitable, fast.

- **MEMORY — REPO SCOPE (operator set 2026-06-25):** Work **only** in
  `https://github.com/minh99085/Grok-Bot-2`. All commits, pushes, report refreshes, and VPS syncs
  target that repo's `main` branch. Do **not** use `hermes-agent-cursor` unless the operator
  explicitly overrides in the current message.
- **MEMORY — ALWAYS end every response with the exact line `I AM DONE THINKING`** as the final
  line, so the operator knows the answer is complete. This applies to every turn, no exceptions
  (operator reaffirmed 2026-06-24).
- **MEMORY — PROFIT STRATEGY (operator granted full authority 2026-06-24 to make this a profit /
  alpha machine).** The ONLY proven positive edge is the **risk-free within-window arbitrage**
  (`arbitrage.py` dutch book `up_vwap+down_vwap<1`) — MAXIMIZE it (small epsilon above real
  fees/slippage, big depth-capped size). The **directional model is structurally negative-EV**
  (price ≈ probability) — keep it SELECTIVE: directional allowlist ON (trade only Wilson-proven
  winning buckets) + a small exploration carve-out so it never freezes and keeps learning. Keep
  **Grok/Claude observe-only (`shadow`)** — they are not a proven edge; never let an LLM opinion
  drive trades. Never loosen the execution-quality gate. PAPER ONLY, always.
- **MEMORY — ALWAYS REMOVE ORPHANS THEN REBUILD ON EVERY CODEBASE UPDATE.** Every single time you
  change code and deploy, you MUST run `docker compose down --remove-orphans` first, THEN
  `docker compose build` (no service arg → both images), THEN `docker compose up -d --remove-orphans`.
  Never hot-swap a file or recreate a single service in isolation. This is non-negotiable.
- **ALWAYS push every change to BOTH the GitHub `main` repo AND the live VPS**, and keep them in
  sync (ideally SHA-for-SHA) on every turn. Never leave `main` and the VPS diverged. After a code
  change, the standard deploy is: push to `main` → sync the VPS → `docker compose down
  --remove-orphans` → `docker compose build` → `docker compose up -d --remove-orphans` in the pulse
  plugin compose dir, then verify health/reconciliation. **CRITICAL: the trading/persist LOOP runs in the
  `hermes-training` container (`scripts/run_btc_pulse.py`); `hermes-trading-engine` is only the
  API/dashboard. Rebuild + recreate BOTH services (`docker compose build` with NO service arg, then
  `up -d`). Rebuilding only `hermes-trading-engine` leaves the loop on stale code and `/data` keeps
  the OLD report schema — verify the new code is live in `hermes-training`, not just the API.**
- **MEMORY — ALWAYS MERGE AND SYNC IN BOTH THE REPO AND THE VPS.** On every change, MERGE the work
  into `main` (do not leave it stranded on a feature branch) AND deploy/sync the SAME code to the
  live VPS, so the GitHub `main` repo and the VPS run identical code every turn. After deploying,
  reconcile the VPS to a clean `git` state at that commit and verify `git rev-parse HEAD` on the VPS
  equals `origin/main`. Never leave `main` and the VPS diverged, and never leave a change merged in
  one place but not the other — merge + sync both, every time.
- **ALWAYS push every full report to the `vps_full_reports/` directory on `main`** (the repo's
  `vps_full_reports/` tree). Whenever you pull a
  full report, refresh `vps_full_reports/latest/` (`btc_pulse_light_report.json`,
  `btc_pulse_status.json`, `btc_pulse_ledger.json`, `btc_pulse_meta_bundle.json`, `report.md`,
  `reconciliation_report.md`, `vps_state.txt`) from the live VPS container and commit + push it to
  `main`.

- **HARD SAFETY INVARIANT (never relaxed):** PAPER ONLY. No real order, no wallet, no signing.
  There is no live-execution code path in `engine/pulse`, and `scripts/run_btc_pulse.py`
  refuses to start if any live flag is set. Never add one unless the user explicitly asks.
- **Quality gates** (edge size, depth, etc.) may be loosened per the operator's request — they
  only affect which *paper* trades are taken.
- **External signals are OBSERVE-ONLY.** The TradingView webhook intake (`engine/pulse/tradingview.py`
  + `webhook.py`, bound to `127.0.0.1:8787` by default, enabled only when
  `TRADINGVIEW_WEBHOOK_SECRET` is set) feeds candidate signals ONLY. A TradingView alert may NEVER
  place/resize a trade, bypass the strategy or execution-quality gate, or override the Polymarket
  orderbook checks — it is attached to candidates as `dr.external` and recorded in the report;
  the strategy + execution gate remain the sole trade authority. Never wire it into
  `decide()`/`evaluate_execution()`.
- Don't reintroduce the retired legacy engine (universe scanner, Bregman, Grok advisory,
  micro-live/guarded-live/production-review). It was deliberately removed.

## How it works

The contract resolves `Up` iff `Chainlink_BTC_close >= Chainlink_BTC_open` over the 5-min
window (ties → Up). **Reference model (correct):** the oracle is the **Chainlink Data Streams
reference price** for `btc/usd`, obtained from **Polymarket RTDS** (`crypto_prices_chainlink`,
`engine/pulse/rtds.py`) — the exact feed Polymarket resolves on. Binance/Coinbase are FAST
LEAD predictors only (`engine/pulse/oracle.py` `LeadFeeds`), never settlement truth. The
engine:
1. ingests the rolling windows from Gamma (`engine/pulse/markets.py`);
2. snapshots each window's OPEN + CLOSE price on the RTDS Chainlink oracle (`source=rtds_chainlink`);
3. prices each open window as a digital option
   `P(up)=Phi((ln(S_now/S_open)+(mu-0.5 sig^2) r)/(sig*sqrt(r)))` (`fair_value.py`);
4. takes a loosened paper trade on the higher after-cost-edge side (`strategy.py`,
   `executor.py` — simulated fills only);
5. settles by priority — official **Polymarket resolution** first, then the **RTDS Chainlink
   open/close proxy** only when the close-snapshot lag is within threshold — scores Brier
   calibration + proxy/official reconciliation (`settlement.py`). Classic Chainlink Data Feed /
   AggregatorV3 is rejected as a primary settlement feed (`oracle.py`).

The fast loop + entrypoint are `engine/pulse/engine.py` + `scripts/run_btc_pulse.py`.

## Deployment & sync directive (ALWAYS follow)

**ALWAYS push every completed change to BOTH the GitHub `main` repo AND the live VPS, and
keep them identical (SHA-for-SHA: `origin/main` == VPS `git rev-parse HEAD`).** Never advance
one without the other; verify the SHAs match before calling a task done.

VPS deploy procedure (the VPS cannot `git fetch origin` — use a git bundle):
1. `git bundle create /tmp/u.bundle ^<vps_head_sha> HEAD`, then `scp` it over.
2. On the VPS: `git -C /opt/hermes-agent-main fetch /tmp/u.bundle HEAD` then
   `git -C /opt/hermes-agent-main merge --ff-only <sha>`.
3. If code changed: in `/opt/hermes-agent-main/hermes-agent-main/plugins/hermes-trading-engine`
   run `docker compose up -d --build --remove-orphans`.
4. Verify: both containers `healthy`, `/data/btc_pulse_status.json` fresh (<120s), and
   VPS HEAD == `origin/main`. Clean up `/tmp/*.bundle`.

### VPS access
- Host `45.32.227.242`, user `linuxuser`, port `22`, repo root `/opt/hermes-agent-main`, plugin
  at `/opt/hermes-agent-main/hermes-agent-main/plugins/hermes-trading-engine`, containers
  `hermes-training` (the loop) + `hermes-trading-engine` (the API).
- SSH key on the agent VM: `/home/ubuntu/.ssh/cursor-temp-vps`. Connect with
  `ssh -i /home/ubuntu/.ssh/cursor-temp-vps -o BatchMode=yes linuxuser@45.32.227.242`.
- The key is also a Cloud Agent secret so new agent VMs retain VPS access.

## Run it

From `plugins/hermes-trading-engine`:

```bash
docker compose up -d --build      # build + start the pulse loop + API
docker compose logs -f hermes-training        # watch the pulse loop
# status:  curl http://localhost:8800/api/polymarket/training/btc_pulse
# ledger:  curl http://localhost:8800/api/polymarket/training/btc_pulse/ledger
```

`PULSE_*` env (see `docker-compose.yml`) tunes the loosened gates (tick cadence, size,
min-edge, depth, price cap). Smoke test without Docker: `python scripts/run_btc_pulse.py
--max-ticks 3`.

## Tests

`python -m pytest tests/` — `tests/test_btc_pulse_engine.py` covers ingestion, the digital
fair value, rolling vol, open-snapshot gating, the loosened decision, paper fill + settlement
P&L, calibration, and a full deterministic trade→settle→calibrate cycle.
