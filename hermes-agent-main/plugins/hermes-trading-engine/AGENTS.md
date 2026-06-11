# Hermes Trading Engine — agent guide

Instructions for AI agents working in this plugin.

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

**Always push finished work to the repo `main` branch.** The user wants every
completed change to land on `main` (open a PR targeting `main` and merge it, or
push to `main` directly where permitted). Do not leave finished work stranded on
a side branch. Never force‑push or amend; commit normally, then push to `main`.

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
