"""Guarded-live report writer (Phase 8). Always states no live orders + real
execution disabled."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

NO_LIVE_STATEMENT = (
    "No live orders were submitted. Real execution remains DISABLED. Phase 8 is a "
    "guarded-live DESIGN/DRY-RUN-ONLY skeleton: no real order submission, no real "
    "cancellation, no live broker adapter, no Polymarket wallet/private-key signing, "
    "no Kalshi order placement, and no private user-channel subscriptions exist.")


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> int:
    if not rows:
        path.write_text("", encoding="utf-8")
        return 0
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def _md(config, state, precheck, conformance, blockers) -> str:
    lines = [
        "# Guarded-Live Design Report (DRY-RUN ONLY)",
        "",
        f"**{NO_LIVE_STATEMENT}**",
        "",
        f"- mode: `{config.mode}`  dry_run_only: `{config.dry_run_only}`",
        f"- current state: **{state}**",
        f"- precheck: **{(precheck or {}).get('status', 'n/a')}**",
        f"- conformance: **{(conformance or {}).get('status', 'n/a')}**",
        f"- kill switch active: {config.kill_switch_active()}",
        "",
        "## Remaining blockers before ANY future live phase",
    ]
    lines += [f"- {b}" for b in (blockers or ["manual_review_of_guarded_live_design"])]
    lines += [
        "",
        "## Known limitations",
        "- No live orders, no real cancellations, no exchange acknowledgements.",
        "- No private user-channel reconciliation.",
        "- Dry-run payloads are NOT signed and prove nothing about exchange acceptance.",
        "- Manual review is still required.",
        "",
        "## Recommended next step",
        "`do_not_enable_live` / `manual_review_of_guarded_live_design` — live "
        "enablement is never automatic and is out of scope for this phase.",
    ]
    return "\n".join(lines)


def write_report(store, config, *, state: str = "DESIGN_ONLY",
                 precheck: Optional[dict] = None, conformance: Optional[dict] = None,
                 blockers: Optional[list] = None, report_id: str = "latest",
                 base_dir: Optional[str] = None) -> Path:
    out = Path(base_dir or config.output_dir) / report_id
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "guarded_live_config.json", config.public_dict())
    if precheck is not None:
        _write_json(out / "precheck_report.json", precheck)
    if conformance is not None:
        _write_json(out / "conformance_report.json", conformance)
    (out / "guarded_live_design_report.md").write_text(
        _md(config, state, precheck, conformance, blockers), encoding="utf-8")

    if store is not None:
        try:
            _write_csv(out / "dry_run_intents.csv", store.get_guarded_rows("dry_run_order_intents"))
            _write_csv(out / "safety_envelope_decisions.csv",
                       store.get_guarded_rows("safety_envelope_decisions"))
            _write_csv(out / "secret_policy_violations.csv",
                       store.get_guarded_rows("secret_policy_violations"))
            _write_csv(out / "audit_events.csv", store.get_guarded_live_audit_events(1000))
        except Exception:  # noqa: BLE001
            pass
    return out
