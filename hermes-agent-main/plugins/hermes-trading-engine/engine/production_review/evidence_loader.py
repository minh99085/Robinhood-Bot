"""ProductionEvidenceLoader (Phase 11). Gathers all upstream evidence (Phase
8/9/10 + shadow + attestations) into an analysis context. Fails closed on
missing required evidence. Never calls production order endpoints."""

from __future__ import annotations

import time
from typing import Optional

from .jurisdiction import categorize
from .schemas import ProductionEvidenceSummary


class EvidenceError(Exception):
    pass


def _guarded_conformance(store) -> str:
    try:
        from ..guarded_live import ConformanceHarness, GuardedLiveConfig
        return ConformanceHarness(store=store, config=GuardedLiveConfig()).run().status
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def _micro_conformance() -> str:
    try:
        from ..micro_live import MicroLiveConfig
        from ..micro_live.conformance import MicroLiveConformanceHarness
        return MicroLiveConformanceHarness(MicroLiveConfig()).run()["status"]
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def _build_evidence_from_store(store, cfg) -> ProductionEvidenceSummary:
    from ..post_canary import PostCanaryConfig, compute_eligibility
    elig = compute_eligibility(store, PostCanaryConfig.from_env(), "kalshi", "demo")
    analyses = store.get_post_canary_analyses(1) if store else []
    missing, stale = [], []
    ev = ProductionEvidenceSummary(
        clean_demo_canary_count=elig.clean_canaries,
        unresolved_canary_count=elig.unresolved_canaries,
        failed_canary_count=elig.failed_canaries,
        renewed_shadow_hours=(float(elig.renewed_shadow_hours_after_last_canary)
                              if elig.renewed_shadow_hours_after_last_canary is not None else None),
        renewed_shadow_decisions=elig.renewed_shadow_decisions_after_last_canary,
        guarded_live_conformance_status=_guarded_conformance(store),
        micro_live_conformance_status=_micro_conformance(),
        post_canary_eligibility_status=("eligible" if elig.eligible_production_design_review
                                        else "not_eligible"),
        latest_post_canary_analysis_id=(analyses[0]["analysis_id"] if analyses else None))
    if not analyses:
        missing.append("post_canary_analysis")
    if ev.renewed_shadow_hours is None:
        missing.append("renewed_shadow_evidence")
    ev.missing_evidence = missing
    ev.stale_evidence = stale
    return ev


def load(store, cfg, *, fixture: Optional[dict] = None) -> dict:
    if fixture is not None:
        ctx = dict(fixture)
        evd = ctx.get("evidence") or {}
        ctx["evidence_summary"] = ProductionEvidenceSummary(
            **{k: evd.get(k) for k in ProductionEvidenceSummary.model_fields if k in evd})
        ctx.setdefault("jurisdiction", [])
        ctx.setdefault("custody", {})
        ctx.setdefault("venues", ["kalshi", "polymarket"])
        ctx.setdefault("scan_blobs", [])
        # categorize attestations list if provided flat
        atts = ctx.pop("attestations", None)
        if atts is not None:
            ctx["jurisdiction"] = [a for a in atts if categorize(a) == "jurisdiction"]
            accs = [a for a in atts if categorize(a) == "account"]
            vts = [a for a in atts if categorize(a) == "venue_terms"]
            ctx["account"] = accs[0] if accs else ctx.get("account")
            ctx["venue_terms"] = vts[0] if vts else ctx.get("venue_terms")
        return ctx

    ev = _build_evidence_from_store(store, cfg)
    atts = []
    try:
        atts = store.get_production_jurisdiction_attestations(200)
    except Exception:  # noqa: BLE001
        atts = []
    juris = [a for a in atts if categorize(a) == "jurisdiction"]
    accs = [a for a in atts if categorize(a) == "account"]
    vts = [a for a in atts if categorize(a) == "venue_terms"]
    cc = None
    hc = None
    try:
        ccs = store.get_production_change_control(1)
        cc = ccs[0] if ccs else None
        hcs = store.get_production_human_checklists(1)
        hc = hcs[0] if hcs else None
    except Exception:  # noqa: BLE001
        pass
    return {
        "evidence_summary": ev, "jurisdiction": juris, "account": (accs[0] if accs else None),
        "venue_terms": (vts[0] if vts else None), "venues": ["kalshi", "polymarket"],
        "custody": {}, "change_control": cc, "human_checklist": hc, "scan_blobs": [],
        "network_guard_events": [], "secret_violations": [], "ts_ms": int(time.time() * 1000),
    }
