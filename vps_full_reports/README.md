# VPS full reports (for ChatGPT inspection)

This folder holds the **latest full diagnostic report pulled from the VPS**, extracted into
plain text / JSON so it can be read directly here in the repo (ChatGPT inspects these files;
it cannot read a binary `.zip`).

- `latest/` — the most recent full report, extracted. It is **overwritten on every pull**
  so the repo does not grow unbounded; the previous version stays in git history.
- `latest/MANIFEST.txt` — what was captured + the source commit + UTC timestamp.

## What's inside `latest/`
- `report.json`, `report.md` — the bot inspection report (from the embedded light bundle).
- `validation_full.txt`, `validation_light.txt` — run-ready / SAFE-TO-RUN verdicts + blockers.
- `git_commit.txt`, `git_status.txt` — exactly which code produced the report.
- `hermes_training_env_proof.txt` — the running container's effective 100X paper-profile env
  (live flags proven OFF; secrets are presence-only, never values).
- `docker_compose_ps.txt` — container status.
- `runtime_metrics/*.json` — durable metrics (active_learning, bregman_execution, grok_news,
  paper_realism, run_ready, closed_loop_learning, etc.).

Large raw streams (multi-MB `*.jsonl` tail files) and the embedded `*.zip` are intentionally
excluded to keep the folder lean and readable; they remain available in the VPS zip.

## How it is produced (kept consistent)
On every full-report pull the agent runs:
`python scripts/save_full_report_to_repo.py --zip <pulled_vps_full_report.zip>`
then commits + pushes to `main` (and the VPS stays in sync). PAPER ONLY; read-only — these
are reports, never control.
