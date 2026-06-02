"""Production-review dossier report + artifact bundle (Phase 11). Explicitly
states production execution is unimplemented and not authorized."""

from __future__ import annotations

import time
from pathlib import Path

from . import artifacts
from .operational_readiness import _templates
from .schemas import _nid


def _redact_attestation(a) -> dict:
    d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
    # account identifier already redacted; never include raw text beyond confirmation
    return d


def write_report(store, cfg, res, ctx) -> str:
    base = Path(cfg.output_dir) / res.review_id
    base.mkdir(parents=True, exist_ok=True)
    ev = res.evidence_summary

    artifacts.write_json(base / "production_review_summary.json", {
        "review_id": res.review_id, "status": res.status, "recommendation": res.recommendation,
        "hard_fail_count": res.hard_fail_count, "warning_count": res.warning_count,
        "blocked_count": res.blocked_count,
        "eligible_to_draft_phase12_plan": res.eligible_to_draft_phase12_plan,
        "eligible_for_production_execution": False, "eligible_for_size_increase": False,
        "eligible_for_autonomous_live": False, "blocking_reasons": res.blocking_reasons,
        "next_required_actions": res.next_required_actions})
    if ev:
        artifacts.write_json(base / "evidence_summary.json", ev.model_dump())
    if res.endpoint_separation:
        artifacts.write_json(base / "endpoint_separation.json", res.endpoint_separation.model_dump())
    if res.credential_custody:
        artifacts.write_json(base / "credential_custody.json", res.credential_custody.model_dump())
    if res.production_conformance:
        artifacts.write_json(base / "production_conformance.json",
                             res.production_conformance.model_dump())
    if res.operational_readiness:
        artifacts.write_json(base / "operational_readiness.json",
                             res.operational_readiness.model_dump())
    if res.account_readiness:
        artifacts.write_json(base / "account_readiness.json", res.account_readiness.model_dump())
    artifacts.write_json(base / "venue_permissions.json",
                         [v.model_dump() for v in res.venue_permissions])
    artifacts.write_json(base / "jurisdiction_attestations_redacted.json",
                         [_redact_attestation(a) for a in res.jurisdiction_attestations])
    if res.change_control:
        artifacts.write_json(base / "change_control.json", res.change_control.model_dump())
    if res.human_checklist:
        artifacts.write_json(base / "human_checklist.json", res.human_checklist.model_dump())
    artifacts.write_csv(base / "production_review_checks.csv",
                        ["category", "check_name", "status", "severity", "reason"],
                        [[c.category or cat, c.check_name, c.status, c.severity, c.reason]
                         for cat, c in res.all_checks()])
    (base / "phase12_scope_template.md").write_text(_templates()["phase12_scope_template.md"])

    md = base / "production_review_report.md"
    checks_tbl = "\n".join(f"| {c.category or cat} | {c.check_name} | {c.status} | {c.severity} |"
                           for cat, c in res.all_checks())
    lines = [
        "# Production-Canary Design Review Dossier",
        "",
        "> **Production execution is not implemented in Phase 11.**",
        "> **No production orders were submitted.**",
        "> **No production cancellations were sent.**",
        "> **No size increase is approved.**",
        "> **No autonomous live trading is approved.**",
        "> This is not legal, tax, or compliance advice.",
        "",
        f"- review_id: `{res.review_id}`",
        f"- recommendation: **{res.recommendation}**",
        f"- status: **{res.status}**",
        f"- eligible_to_draft_phase12_plan: **{res.eligible_to_draft_phase12_plan}**",
        f"- eligible_for_production_execution: **NO**",
        f"- eligible_for_size_increase: **NO**",
        f"- eligible_for_autonomous_live: **NO**",
        "",
        "## Evidence summary",
        f"- clean demo canaries: {getattr(ev, 'clean_demo_canary_count', 0)}",
        f"- unresolved canaries: {getattr(ev, 'unresolved_canary_count', 0)}",
        f"- failed canaries: {getattr(ev, 'failed_canary_count', 0)}",
        f"- renewed shadow hours: {getattr(ev, 'renewed_shadow_hours', None)}",
        f"- renewed shadow decisions: {getattr(ev, 'renewed_shadow_decisions', None)}",
        f"- Phase 8 conformance: {getattr(ev, 'guarded_live_conformance_status', None)}",
        f"- Phase 9 conformance: {getattr(ev, 'micro_live_conformance_status', None)}",
        f"- Phase 10 eligibility: {getattr(ev, 'post_canary_eligibility_status', None)}",
        f"- missing evidence: {getattr(ev, 'missing_evidence', [])}",
        "",
        "## Review results",
        f"- endpoint separation: {getattr(res.endpoint_separation, 'status', None)} "
        f"(api_submit_routes={getattr(res.endpoint_separation, 'api_submit_routes_found', 0)})",
        f"- credential custody: {getattr(res.credential_custody, 'status', None)} "
        f"(raw_secret_findings={getattr(res.credential_custody, 'raw_secret_findings', 0)})",
        f"- account readiness: {getattr(res.account_readiness, 'status', None)}",
        f"- venue permissions: {[v.status for v in res.venue_permissions]}",
        f"- jurisdiction attestations (valid): {len(res.jurisdiction_attestations)}",
        f"- production conformance: {getattr(res.production_conformance, 'status', None)} "
        f"(real_network_calls={getattr(res.production_conformance, 'real_network_calls', 0)})",
        f"- operational readiness: {getattr(res.operational_readiness, 'status', None)}",
        f"- change control: {getattr(res.change_control, 'approval_status', None)}",
        f"- human checklist: {getattr(res.human_checklist, 'status', None)}",
        "",
        "## Audit checks",
        "| category | check | status | severity |",
        "|---|---|---|---|",
        checks_tbl or "| - | - | - | - |",
        "",
        "## Blocking reasons",
        ("\n".join(f"- {r}" for r in res.blocking_reasons) or "- none"),
        "",
        "## Required next actions",
        ("\n".join(f"- {r}" for r in res.next_required_actions) or "- manual review"),
        "",
        "## Known limitations",
        "- Design review does not prove production execution safety.",
        "- Manual attestations are required; account eligibility must be confirmed outside the bot.",
        "- No legal/tax/compliance advice is provided.",
        "- Production exchange behavior may differ from demo.",
        "- Production canary implementation remains a FUTURE phase (Phase 12+).",
        "- Production secrets are not loaded by this phase.",
        "",
        "_Phase 11 does not authorize or implement production order submission, production "
        "cancellation, production signing, size increase, or autonomous live trading._",
    ]
    md.write_text("\n".join(lines))

    # dossier index
    (base / "production_dossier_index.md").write_text(
        "# Production Dossier Index\n\n"
        f"- recommendation: {res.recommendation}\n- status: {res.status}\n\n"
        "Files: production_review_summary.json, production_review_report.md, "
        "production_review_checks.csv, evidence_summary.json, endpoint_separation.json, "
        "credential_custody.json, production_conformance.json, operational_readiness.json, "
        "account_readiness.json, venue_permissions.json, "
        "jurisdiction_attestations_redacted.json, change_control.json, human_checklist.json, "
        "phase12_scope_template.md\n\n"
        "_Production execution remains unimplemented in Phase 11._\n")

    if store is not None:
        try:
            store.add_production_review_report({
                "report_id": _nid("prrep"), "review_id": res.review_id,
                "ts_ms": int(time.time() * 1000), "status": res.status,
                "recommendation": res.recommendation, "report_path": str(md),
                "summary_json": {"recommendation": res.recommendation}})
        except Exception:  # noqa: BLE001
            pass
    return str(md)
