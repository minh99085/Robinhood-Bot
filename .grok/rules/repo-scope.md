# Repo scope

Work only in `https://github.com/minh99085/Grok-Bot-2`.

Never commit or push to `hermes-agent-cursor` unless the operator explicitly says otherwise in the current turn.

## Destructive change guard (mandatory)

Read **`.grok/rules/destructive-change-guard.md`** before any delete/remove/disable that could damage the bot. **Warn the operator and get explicit confirmation before executing** — no commit, push, or deploy until they say proceed.

## Self-improve closed loop (operator ON — 2026-06-28)

When `scripts/pulse-babysit/state.json` has `babysit_autopilot: true` and `phase` is not `hands_off`:

- **Run** babysit cycles on schedule — soak → pull → eval → fix → deploy.
- **Read** `.grok/rules/self-improve-loop.md` — adjust layer (`PULSE_RESEARCH_AUTO_APPLY=1`, learning ON).
- **Read** `.grok/rules/hands-off-untouchable.md` — profitable-bot untouchables (Grok shadow, TV observe-only, no live).

If `phase: hands_off` and `now < hands_off_until`: pause all cycles/deploys; respect untouchables only.

**Baseline** for compare: `baseline_at_hands_off` in state.json (103 trades, $584.91, 61.2% WR).

## VPS deploy — MANDATORY after every push to `main` (except hands_off)

**Non-negotiable:** Whenever you commit and push to `origin/main`, you MUST immediately deploy to the live VPS with full orphan cleanup and rebuild — **unless** `state.json` is in `hands_off` phase. Do not tell the operator to run it — execute it yourself.

This applies to **every** push — engine/env, babysit scripts, `.grok` rules/skills, and report-only commits. Goal: `origin/main` HEAD == VPS HEAD always (paused during hands_off).

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