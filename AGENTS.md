# Grok-Bot-2 — project rules

## Quant team mandate (ALWAYS follow)

Operate as a **quant research + engineer + trader** team targeting **~80% WR** on selective entries.
Each cycle: read live performance → hypothesize from market + bot data → implement minimal gate/strategy
changes → measure on 15-min soak. See `.grok/rules/quant-team.md`.

## Repository scope (ALWAYS follow)

- **Canonical repo:** `https://github.com/minh99085/Grok-Bot-2` — the **only** GitHub repository for code, commits, pushes, reports, and deploys.
- **Do not** clone, commit, or push to `hermes-agent-cursor` or any other repo unless the operator explicitly overrides this in the current message.
- **Local workspace:** prefer `C:\Users\tieut\Grok-Bot-2` when working from this machine.
- **Default branch:** `main`.
- **VPS deploy (MANDATORY after every push to `main`):** You MUST run the full deploy yourself —
  never push and stop. Sequence:
  1. `git push origin main`
  2. `.\scripts\sync-vps.ps1` — always default (sync + `down --remove-orphans` → `build` →
     `up -d --remove-orphans`). **Never** `-SkipRebuild` unless operator explicitly requests it.
  3. SSH: `python3 scripts/apply-loop-arch-env.py` when env/gates changed
  4. SSH: `docker compose up -d --force-recreate hermes-training` in the plugin dir
  5. `.\scripts\verify-sync.ps1` — VPS HEAD must equal `origin/main`
  See `.grok/rules/repo-scope.md`.

## Project layout

- Trading bot plugin: `hermes-agent-main/plugins/hermes-trading-engine/`
- Full VPS reports: `vps_full_reports/latest/` — **always commit + push to `main` after pull**
  (includes `report.docx`; automatic via `pull-vps-artifacts.ps1`)
- Design townhall: `Design Townhall` (repo root)
- Operator guide for the pulse engine: `hermes-agent-main/plugins/hermes-trading-engine/AGENTS.md`
- Autonomous closed loop: `/pulse-babysit cycle` or `.\scripts\pulse-babysit\install-scheduled-task.ps1` (15m soak default; see `.grok/skills/pulse-babysit/SKILL.md`)