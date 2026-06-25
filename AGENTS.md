# Grok-Bot-2 — project rules

## Repository scope (ALWAYS follow)

- **Canonical repo:** `https://github.com/minh99085/Grok-Bot-2` — the **only** GitHub repository for code, commits, pushes, reports, and deploys.
- **Do not** clone, commit, or push to `hermes-agent-cursor` or any other repo unless the operator explicitly overrides this in the current message.
- **Local workspace:** prefer `C:\Users\tieut\Grok-Bot-2` when working from this machine.
- **Default branch:** `main`.
- **VPS sync:** after every push to `main`, run `.\scripts\sync-vps.ps1` (always syncs code +
  `docker compose down --remove-orphans` → `build` → `up -d --remove-orphans`). Use `-SkipRebuild`
  only for rare code-only checks. Goal: `origin/main` SHA == VPS `/opt/Grok-Bot-2` HEAD.
  Check anytime: `.\scripts\verify-sync.ps1`.

## Project layout

- Trading bot plugin: `hermes-agent-main/plugins/hermes-trading-engine/`
- Full VPS reports: `vps_full_reports/latest/`
- Design townhall: `Design Townhall` (repo root)
- Operator guide for the pulse engine: `hermes-agent-main/plugins/hermes-trading-engine/AGENTS.md`
- Autonomous closed loop: `/pulse-babysit cycle` or `.\scripts\pulse-babysit\install-scheduled-task.ps1` (1h soak + 1h schedule default; see `.grok/skills/pulse-babysit/SKILL.md`)