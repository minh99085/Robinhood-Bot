# VPS deploy mandate (OPERATOR MEMORY — 2026-07-03)

**Operator memory (verbatim):** merge all branches into `main`, always push to `main`, then
sync the VPS, remove orphans, and rebuild the container.

**Non-negotiable on every completed change:** merge → push to `main` → sync VPS → remove orphans → rebuild.

Do not tell the operator to run deploy — **execute it yourself** before calling any task done.

## Standard sequence (every push to `main`)

1. Merge any feature branch into `main` (if work was on a `cursor/*` branch).
2. `git push origin main`
3. `.\scripts\sync-vps-robinhood.ps1` from repo root (**default** — includes full rebuild)
   - Git bundle sync → `/opt/Robinhood-Bot` (or `/opt/Grok-Bot-2` on legacy VPS layout)
   - `docker compose --profile robinhood down --remove-orphans`
   - `docker compose --profile robinhood build`
   - `docker compose --profile robinhood up -d --force-recreate --remove-orphans`
4. `.\scripts\verify-sync.ps1` — VPS HEAD SHA must equal `origin/main`

## Never

- Leave completed work on a feature branch without merging to `main`
- Push to `main` and stop without VPS sync
- Use `-SkipRebuild` unless the operator explicitly requests code-only sync in the current message
- `docker compose restart` or hot-swap a single service without `down --remove-orphans` → `build` → `up -d --remove-orphans`

## VPS access

- Host: `45.32.224.147`, user: `root`, key: `$env:USERPROFILE\.ssh\bot2_grok_temp` (or `$robinhood` env in cloud agents)
- Repo: `/opt/Robinhood-Bot` (canonical) or `/opt/Grok-Bot-2` (legacy)
- Robinhood compose: `.../plugins/hermes-trading-engine-robinhood` (profile `robinhood`)
