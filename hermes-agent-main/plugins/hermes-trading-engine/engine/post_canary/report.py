"""Post-canary report + artifact bundle (Phase 10). Markdown report explicitly
states no scaling / no autonomous live / production unimplemented."""

from __future__ import annotations

import time
from pathlib import Path

from . import artifacts
from .schemas import _nid


def write_report(store, cfg, res, ctx, elig) -> str:
    base = Path(cfg.output_dir) / res.analysis_id
    base.mkdir(parents=True, exist_ok=True)
    a = ctx.get("attempt") or {}
    plan = ctx.get("plan") or {}

    artifacts.write_json(base / "post_canary_summary.json", {
        "analysis_id": res.analysis_id, "status": res.status,
        "recommendation": res.recommendation, "hard_fail_count": res.hard_fail_count,
        "warning_count": res.warning_count, "unknown_blocking_count": res.unknown_blocking_count,
        "clean_for_repeat_demo_same_size": res.clean_for_repeat_demo_same_size,
        "eligible_for_production_design_review": res.eligible_for_production_design_review,
        "eligible_for_size_increase": False, "eligible_for_autonomous_live": False,
        "blocking_reasons": res.blocking_reasons, "next_required_actions": res.next_required_actions})
    if res.reconciliation:
        artifacts.write_json(base / "reconciliation_audit.json", res.reconciliation.model_dump())
    if res.execution_quality:
        artifacts.write_json(base / "execution_quality.json", res.execution_quality.model_dump())
    if res.market_data:
        artifacts.write_json(base / "market_data_audit.json", res.market_data.model_dump())
    if res.research:
        artifacts.write_json(base / "research_audit.json", res.research.model_dump())
    if res.risk:
        artifacts.write_json(base / "risk_audit.json", res.risk.model_dump())
    if res.chain:
        artifacts.write_json(base / "chain_audit.json", res.chain.model_dump())
    if res.secrets:
        artifacts.write_json(base / "secret_audit.json", res.secrets.model_dump())
    artifacts.write_json(base / "eligibility.json", elig.model_dump())
    artifacts.write_json(base / "redacted_trace.json", artifacts.redacted_trace(ctx))

    artifacts.write_csv(base / "audit_checks.csv",
                        ["category", "check_name", "status", "severity", "reason",
                         "observed", "expected", "threshold"],
                        [[cat, c.check_name, c.status, c.severity, c.reason, c.observed_value,
                          c.expected_value, c.threshold] for cat, c in res.all_checks()])
    if res.markout:
        artifacts.write_csv(base / "markout.csv",
                            ["horizon_ms", "midpoint", "markout_vs_mid", "markout_vs_touch",
                             "adverse_selection", "data_missing"],
                            [[o.horizon_ms, o.midpoint, o.markout_vs_mid, o.markout_vs_touch,
                              o.adverse_selection, o.data_missing] for o in res.markout.observations])

    md = base / "post_canary_report.md"
    mk = res.markout
    rows = "\n".join(f"| {o.horizon_ms} | {o.midpoint} | {o.markout_vs_mid} | "
                     f"{'missing' if o.data_missing else 'ok'} |"
                     for o in (mk.observations if mk else []))
    chk = "\n".join(f"| {cat} | {c.check_name} | {c.status} | {c.severity} | {c.reason} |"
                    for cat, c in res.all_checks())
    lines = [
        "# Post-Canary Analysis Report",
        "",
        "> **No scaling is approved.**",
        "> **No autonomous live trading is approved.**",
        "> **Production execution remains unimplemented in Phase 10.**",
        "",
        f"- analysis_id: `{res.analysis_id}`",
        f"- recommendation: **{res.recommendation}**",
        f"- status: **{res.status}**",
        f"- venue / environment: `{plan.get('venue')}` / `{plan.get('environment')}`",
        f"- live_order_attempt_id: `{a.get('live_order_attempt_id')}`",
        f"- dry_run_intent_id: `{plan.get('source_dry_run_intent_id')}`",
        f"- readiness_report_id: `{plan.get('readiness_report_id')}`",
        f"- approval_batch_id: `{plan.get('approval_batch_id')}`  "
        f"arming_token_id: `{'[REDACTED]' if plan.get('arming_token_id') else None}`",
        f"- order status: `{a.get('status')}`  filled: `{a.get('filled_quantity')}`  "
        f"fee: `{a.get('fee')}`",
        f"- slippage_bps: `{getattr(res.execution_quality, 'slippage_bps', None)}`",
        f"- eligible_for_size_increase: **NO**",
        f"- eligible_for_autonomous_live: **NO**",
        f"- production_execution: **NOT IMPLEMENTED**",
        "",
        "## Markout",
        "| horizon_ms | midpoint | markout_vs_mid | data |",
        "|---|---|---|---|",
        rows or "| - | - | - | - |",
        "",
        "## Audit checks",
        "| category | check | status | severity | reason |",
        "|---|---|---|---|---|",
        chk or "| - | - | - | - | - |",
        "",
        "## Veto recommendation",
        f"**{res.recommendation}**",
        "",
        "### Blocking reasons",
        ("\n".join(f"- {r}" for r in res.blocking_reasons) or "- none"),
        "",
        "### Required next actions",
        ("\n".join(f"- {r}" for r in res.next_required_actions) or "- manual review"),
        "",
        "### Eligibility",
        f"- repeat demo same size: {elig.eligible_repeat_demo_same_size}",
        f"- production design review (NOT execution): {elig.eligible_production_design_review}",
        f"- size increase: NO",
        f"- clean demo streak: {elig.clean_demo_canary_streak}",
        "",
        "_Secrets are redacted from this report and all artifacts. A clean canary does "
        "not authorize scaling; production execution is not implemented; manual review is "
        "mandatory after every canary._",
    ]
    md.write_text("\n".join(lines))

    if store is not None:
        try:
            store.add_post_canary_report({
                "report_id": _nid("pcrep"), "analysis_id": res.analysis_id,
                "ts_ms": int(time.time() * 1000), "status": res.status,
                "recommendation": res.recommendation, "report_path": str(md),
                "summary_json": {"recommendation": res.recommendation}})
        except Exception:  # noqa: BLE001
            pass
    return str(md)
