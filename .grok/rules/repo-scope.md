# Repo scope

Work only in `https://github.com/minh99085/Grok-Bot-2`.

Never commit or push to `hermes-agent-cursor` unless the operator explicitly says otherwise in the current turn.

## VPS deploy — MANDATORY after every push to `main`

**Non-negotiable:** Whenever you commit and push to `origin/main`, you MUST immediately deploy to the live VPS with full orphan cleanup and rebuild. Do not tell the operator to run it — execute it yourself.

### Standard sequence (every code or env change)

1. `git push origin main`
2. `.\scripts\sync-vps.ps1` from repo root (default — **never** use `-SkipRebuild` unless operator explicitly asks for code-only sync)
   - Syncs git bundle to `/opt/Grok-Bot-2`
   - `docker compose down --remove-orphans`
   - `docker compose build` (both images — no service arg)
   - `docker compose up -d --remove-orphans`
3. On VPS: `python3 scripts/apply-loop-arch-env.py` (when env/gate keys changed)
4. On VPS plugin dir: `docker compose up -d --force-recreate hermes-training` (loop runs in `hermes-training`; API alone is not enough)
5. Verify: `.\scripts\verify-sync.ps1` — VPS HEAD SHA == `origin/main`; both containers healthy

### Never do

- Push to `main` and leave VPS on an old SHA
- `docker compose restart` or recreate a single service without `down --remove-orphans` → `build` → `up -d --remove-orphans`
- Assume deploy is done because you pushed to GitHub only

### VPS access

- Host: `45.32.224.147`, user `root`, repo: `/opt/Grok-Bot-2`
- SSH key: `$env:USERPROFILE\.ssh\bot2_grok_temp`
- Plugin compose: `/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine`