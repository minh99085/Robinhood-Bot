# Hermes Trading Engine — agent guide

Instructions for AI agents working in this plugin.

## Operating directive — Quant team role (ALWAYS follow)

You operate as a combined **Quant Researcher + Quant Developer + Quant Trader** team for
the Hermes Agent autonomous Polymarket paper-trading bot.

- **Mission:** improve the bot's algorithms and trading strategies to reach a *profitable*
  trading bot **fast**.
- **Trading restrictions:** you may **only loosen a trading restriction / gate when the user
  explicitly prompts you to**. Otherwise keep every gate, threshold, and realism control as-is
  (or tighten). Default to discovery improvements that do **not** weaken gates.
- **Do NOT alter:**
  - **project architecture** (service layout, module boundaries, data/contract structure,
    the two-Docker-setup design, deployment model);
  - **live-trading safety controls** (paper-only enforcement, no real order placement, kill
    switch, RiskEngine gating, fake-fill / stale-book bans, readiness/credible/OOS gates).

When a change could touch architecture or a safety control, stop and surface it instead of
doing it. Everything stays **PAPER ONLY**.

## Deployment & sync directive (ALWAYS follow)

**Keep GitHub `main` and the live VPS in lockstep.** Every time work is finished, land it on
GitHub `main` **and** deploy the same commit to the VPS so `main` == VPS HEAD (SHA-for-SHA).
Never leave the VPS running code that is not on `main`, and never advance `main` without
deploying it to the VPS.

VPS deploy procedure (proven; the VPS cannot `git fetch origin` — its deploy key is
passphrase-protected, so use a git bundle):

1. `git bundle create /tmp/u.bundle ^<vps_head_sha> <branch-or-commit>` then `scp` it over.
2. On the VPS: `git -C /opt/hermes-agent-main fetch /tmp/u.bundle <ref>` then
   `git -C /opt/hermes-agent-main merge --ff-only FETCH_HEAD`.
3. If **code** changed (not just reports/docs): in
   `/opt/hermes-agent-main/hermes-agent-main/plugins/hermes-trading-engine` run
   `docker compose build && docker compose up -d`. Docs/report-only commits need no rebuild.
4. Verify: both containers `healthy`, `/data/polymarket_training.json` fresh (< ~5 min),
   and `git rev-parse HEAD` on the VPS equals `origin/main`. Clean up `/tmp/*.bundle`.

### VPS access (so access is never lost)

- Connection coordinates live in the repo-root `.laptop_agent.json` (gitignored): host
  `45.32.227.242`, user `linuxuser`, port `22`, repo root `/opt/hermes-agent-main`, plugin
  `/opt/hermes-agent-main/hermes-agent-main/plugins/hermes-trading-engine`, containers
  `hermes-training` + `hermes-trading-engine`.
- SSH key path on the agent VM: `/home/ubuntu/.ssh/cursor-temp-vps`. Connect with
  `ssh -i /home/ubuntu/.ssh/cursor-temp-vps -o BatchMode=yes linuxuser@45.32.227.242`.
- **Cross-run persistence:** cloud-agent VMs are ephemeral and the private key must NEVER be
  committed to git. To retain VPS access on every future run, the key (and coordinates) must
  be stored as **Cloud Agent secrets** in the Cursor Dashboard (Cloud Agents > Secrets) so
  they are injected into new VMs — e.g. a secret holding the private key written to
  `~/.ssh/cursor-temp-vps` at startup.

## Repo layout — IMPORTANT (read first)

There are **two** Docker setups in this repo. They build **different apps**:

| Path | Builds | Port | Use it? |
|------|--------|------|---------|
| `hermes-agent-main/docker-compose.yml` (repo root) | the generic **Hermes AI agent** (gateway + web dashboard) | 9119 | NOT the trading bot |
| `hermes-agent-main/plugins/hermes-trading-engine/docker-compose.yml` (**here**) | the **Polymarket paper-trading bot** (`engine.app` + training loop) | 8800 | ✅ THIS is the bot |

The trading bot lives **only** here: `hermes-agent-main/plugins/hermes-trading-engine/`.
It is a standalone container (`build: .` → this folder's `Dockerfile`, `python:3.11-slim`
+ `uvicorn engine.app:app`). It does NOT use the root Dockerfile / s6 / the 9119 dashboard.

**Common pitfall:** a stray top-level `hermes-trading-engine/` copy (a sibling of
`hermes-agent-main/`) or a stale Docker image will show OLD behavior (e.g. the
removed "Paper Campaign" panel, wrong Grok model). Always build from **this**
path and use `--remove-orphans`. The paper campaign has been removed from the
codebase — if you still see it, you are running a stale image / stray copy.

## User preference (ALWAYS follow)

**Push finished work to the repo `main` branch ONLY. Do NOT create new branches.**
The user wants every completed change committed and pushed directly to `main`
(`git add -A && git commit && git push origin main`). Never spin up feature/side
branches and never open PRs for routine work — commit straight onto `main`. Never
force‑push or amend; commit normally, then push.

**At the end of every task, give simple copy‑paste instructions to start the
system** — short, runnable commands, no long prose. The user runs this on
Windows + Docker Desktop and wants the same easy "how to start it" block each
time (just like prior sessions). Lead with the start commands; keep
explanation minimal.

## Easy start (paste this)

Run from `plugins/hermes-trading-engine`:

```bash
docker compose up -d --build      # build + start (first time / after code changes)
# then open the dashboard:
#   http://localhost:8800
```

Day-to-day:

```bash
docker compose up -d              # start (no rebuild needed)
docker compose stop               # pause (keep containers)
docker compose start              # resume after a stop
docker compose down               # remove containers (data kept in the hte_data volume)
docker compose logs -f hermes-trading-engine   # watch logs
```

Rule of thumb: `stop`↔`start` = pause/resume; `up`↔`down` = create/remove.
After a `down`, always come back with `up` (not `start`).

## What runs

- `hermes-trading-engine` — paper dashboard + API on **:8800** (the core).
- `hermes-training` — Polymarket PAPER training loop (scan → rank → edge → learn).

PAPER ONLY: no real orders. Grok is research-only. Optional: put
`GROK_API_KEY` (or `XAI_API_KEY`) in `.env` to enable the Grok research layer.
