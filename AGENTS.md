# Grok-Bot-2 — project rules

## Quant team mandate (ALWAYS follow)

Operate as a **quant research + engineer + trader** team targeting **~80% WR** on selective entries.
Each cycle: read live performance → hypothesize from market + bot data → implement minimal gate/strategy
changes → measure on 15-min soak. See `.grok/rules/quant-team.md`.

## Roan / Bregman architecture (Phase 0+)

5m brain, 15m hands — `docs/roan-bregman-architecture.md`. Promotion gates:
`scripts/pulse-babysit/roan-bregman-promotion-scorecard.json`. Do not enable
`PULSE_BREGMAN_TRADE_AUTHORITY` or `PULSE_DEPENDENCY_ARB_EXECUTE` until scorecard passes.

## Soak / learning collection lock (OPERATOR MANDATE)

While collecting ledger data for learning, follow `.grok/rules/soak-learning-lock.md` and
`scripts/pulse-babysit/frozen-env-keys.json`. Run `validate-frozen-lock.py` before deploy.
Do not tighten gates or re-enable TV authority during this phase unless the operator says so
in the current message.

## TradingView observe-only lock (OPERATOR MANDATE — NEVER OVERRIDE)

TradingView is **observe-only forever** — not a trade gate. Do **not** re-enable MTF require/side-align,
TV context, signal gate, or baseline TV stack blocks in env, code, or babysit fixes unless the operator
explicitly says otherwise **in the current message**. Full frozen keys and behavior:
`.grok/rules/tv-observe-only-lock.md`.

## Repository scope (ALWAYS follow)

- **Canonical repo:** `https://github.com/minh99085/Robinhood-Bot` — the **only** GitHub repository for code, commits, pushes, reports, and deploys.
- **Do not** clone, commit, or push to `hermes-agent-cursor` or any other repo unless the operator explicitly overrides this in the current message.
- **Local workspace:** prefer `C:\Users\tieut\Robinhood-Bot` when working from this machine.
- **Default branch:** `main`.

## VPS deploy (OPERATOR MEMORY — ALWAYS follow, set 2026-07-02)

**Every completed change:** push to `main` → sync VPS → `down --remove-orphans` → `build` → `up -d --remove-orphans`. Execute yourself; never push and stop.

1. `git push origin main`
2. `.\scripts\sync-vps.ps1` (default — **never** `-SkipRebuild` unless operator asks in the current message)
3. `python3 scripts/apply-loop-arch-env.py` on VPS when env/gates changed
4. `docker compose up -d --force-recreate hermes-training` in pulse plugin dir when loop env changed
5. `.\scripts\sync-vps-robinhood.ps1` when Robinhood plugin changed
6. `.\scripts\verify-sync.ps1` — VPS HEAD must equal `origin/main`

Full detail: `.grok/rules/vps-deploy-mandate.md` and `.grok/rules/repo-scope.md`.

## Project layout

- Polymarket paper engine: `hermes-agent-main/plugins/hermes-trading-engine/`
- Robinhood Agentic plugin (isolated): `hermes-agent-main/plugins/hermes-trading-engine-robinhood/`
  - Deploy separately: `.\scripts\sync-vps-robinhood.ps1` — does **not** modify Polymarket containers
- Full VPS reports: `vps_full_reports/latest/` — **always commit + push to `main` after pull**
  (includes `report.docx`; automatic via `pull-vps-artifacts.ps1`)
- Design townhall: `Design Townhall` (repo root)
- Operator guide for the pulse engine: `hermes-agent-main/plugins/hermes-trading-engine/AGENTS.md`
- Autonomous closed loop: `/pulse-babysit cycle` or `.\scripts\pulse-babysit\install-scheduled-task.ps1` (15m soak default; see `.grok/skills/pulse-babysit/SKILL.md`)

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