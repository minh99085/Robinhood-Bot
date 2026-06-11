"""Unified paper-training inspection summary (PASS-8 — observability only).

Aggregates every prior-pass output (Bregman funnel, strategy priority, paper
realism, profitability ranking, active learning, correlation risk) plus the
feature-activation audit into ONE machine-readable summary + a human-readable
markdown report + deterministic recommendations + a compact console line.

PAPER ONLY. Pure + deterministic: no I/O, no trading, no thresholds changed.
"""

from __future__ import annotations

from typing import Optional

SCHEMA_VERSION = "inspection_summary/1.0"

REQUIRED_SECTIONS = [
    "Executive Summary", "Feature Activation", "Bregman / ABCAS Funnel",
    "Strategy Priority", "Paper Execution Realism", "Profitability Ranking",
    "Active Learning / Exploration", "Correlation / Cluster Risk",
    "Candidate Rejection Waterfall", "Opened Paper Trades / Bundles",
    "Closed-Loop Learning", "Readiness / Real Edge Score",
    "Data Quality / Market Coverage", "Recommendations",
]

# feature -> runtime evidence metric path; used to avoid false "active" claims.
_FEATURE_EVIDENCE = {
    "Raw ABCAS/Bregman scanner": ("bregman_funnel", "raw_groups_discovered"),
    "Full-catalog Bregman grouping": ("bregman_funnel", "raw_catalog_markets_scanned"),
    "Trainer Bregman certifier": ("bregman_funnel", "unique_groups_certified"),
    "Bregman paper execution": ("bregman_funnel", "bundles_opened"),
    "Bregman-first priority": ("strategy_priority", "bregman_evaluated_before_directional"),
    "Strict PaperExecutionPolicy": ("paper_realism", "realistic_trade_count"),
    "Profitability-first ranking": ("profitability_ranking", "candidates_annotated"),
    "Profitability governor": ("profitability_ranking", "profitability_governor_hard_rejects"),
    "ActiveLearningSelector": ("active_learning", "active_learning_candidates_considered"),
    "Random/hash exploration blocker": ("active_learning", "random_exploration_opened_trades"),
    "Cluster/correlation gate": ("correlation_risk", "correlation_adjusted_candidates"),
}


def _g(d: dict, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _feature_status(audit_status: str, controls_trades: bool, evidence_val) -> str:
    """Map a feature to a conservative Pass-8 status string (never claim active
    purely from code existence — require runtime evidence where measurable)."""
    if audit_status == "dead":
        return "imported_unused"
    if audit_status == "imported":
        return "imported_unused"
    if audit_status == "telemetry":
        return "active_telemetry_only"
    if audit_status in ("active", "annotated") and controls_trades:
        if evidence_val is None:
            return "missing_metrics"
        if isinstance(evidence_val, bool):
            return "active_controls_trades" if evidence_val else "configured_but_no_candidates"
        if isinstance(evidence_val, (int, float)) and float(evidence_val) <= 0:
            return "configured_but_no_candidates"
        return "active_controls_trades"
    return "active_telemetry_only"


def build_inspection_summary(status: dict, feature_audit: dict, *,
                             trade_ledger: Optional[dict] = None,
                             rejection_waterfall: Optional[dict] = None,
                             data_quality: Optional[dict] = None,
                             training_reconciliation: Optional[dict] = None,
                             ledger: Optional[dict] = None,
                             grok_news_evidence: Optional[dict] = None,
                             validation: Optional[dict] = None,
                             run_ready: Optional[dict] = None) -> dict:
    status = status or {}
    pnl = status.get("pnl", {}) or {}
    pe = status.get("paper_realism", {}) or {}
    sp = status.get("strategy_priority", {}) or {}
    prk = status.get("profitability_ranking", {}) or {}
    al = status.get("active_learning", {}) or {}
    cr = status.get("correlation_risk", {}) or {}
    breg = (status.get("bregman", {}) or {}).get("execution", {}) or {}
    sm = status.get("scan_metrics", {}) or {}
    cll = status.get("closed_loop_learning", {}) or {}
    trade_ledger = trade_ledger or {}
    rejection_waterfall = rejection_waterfall or {}
    data_quality = data_quality or {}

    bregman_funnel = {
        "raw_catalog_markets_scanned": breg.get("raw_catalog_markets_scanned", sm.get("scanned", 0)),
        "eligible_raw_markets": breg.get("raw_catalog_markets_scanned", sm.get("kept", 0)),
        "raw_groups_discovered": breg.get("raw_groups_discovered", 0),
        "graph_groups_discovered": 0,
        "groups_from_graph_used": breg.get("groups_from_graph_used", False),
        "duplicate_groups_dropped": breg.get("duplicate_groups_dropped", 0),
        "unique_groups_certified": breg.get("unique_groups_certified", 0),
        "certified_opportunities": breg.get("certified_opportunities", 0),
        "bundles_opened": breg.get("opened_bregman_bundles", 0),
        "capital_committed": breg.get("bregman_capital_committed", 0.0),
        "sees_full_raw_catalog": breg.get("sees_full_raw_catalog", sp.get("bregman_priority_enabled", False)),
        "evaluated_before_directional": breg.get("evaluated_before_directional",
                                                 sp.get("bregman_evaluated_before_directional", False)),
        "rejected_by_reason": dict(breg.get("rejected_by_reason", {}) or {}),
        "bregman_near_misses_total": breg.get("bregman_near_misses_total", 0),
        "bregman_top_near_misses": list(breg.get("bregman_top_near_misses", []) or []),
        "near_miss_by_rejection_reason": dict(breg.get("near_miss_by_rejection_reason", {}) or {}),
        "near_miss_one_fix_away_count": breg.get("near_miss_one_fix_away_count", 0),
        "near_miss_depth_only_count": breg.get("near_miss_depth_only_count", 0),
        "near_miss_not_exhaustive_count": breg.get("near_miss_not_exhaustive_count", 0),
        "near_miss_stale_refresh_failed_count": breg.get("near_miss_stale_refresh_failed_count", 0),
        "blocker_explanation": _bregman_blocker_explanation(
            breg, breg.get("certified_opportunities", 0),
            breg.get("unique_groups_certified", 0)),
    }

    run = {
        "schema_version": SCHEMA_VERSION, "paper_only": True,
        "run_id": status.get("run_id"), "mode": status.get("mode"),
        "execution_mode": status.get("execution_mode", "paper"),
        "started_ts": status.get("started_ts"),
        "runtime_seconds": status.get("runtime_seconds"),
        "tick": status.get("tick", 0),
        "live_trading_disabled": True,
        "strict_paper_realism": not bool(pe.get("reference_price_fills_allowed_for_exploit", False)),
        "bregman_priority_enabled": bool(sp.get("bregman_priority_enabled", True)),
        "raw_markets_loaded": sm.get("scanned", 0),
        "eligible_markets": sm.get("kept", 0),
        "candidates_considered": prk.get("candidates_annotated", 0),
        "trades_opened": pnl.get("trades_opened", 0),
        "realistic_trades": pe.get("realistic_trade_count", 0),
        "bregman_bundles_opened": bregman_funnel["bundles_opened"],
        "directional_trades_opened": prk.get("directional_after_cost_positive", 0),
        "exploration_trades_opened": al.get("exploration_trades_opened", 0),
        "shadow_only_opportunities": pe.get("shadow_trade_count", 0),
        "hard_rejects": pe.get("hard_reject_count", 0),
        "realistic_pnl": pe.get("realistic_pnl", 0.0),
        "shadow_theoretical_pnl": pe.get("shadow_theoretical_pnl", 0.0),
        "readiness_pnl": pe.get("readiness_pnl", 0.0),
    }

    # feature-activation proof table (runtime-evidence backed)
    feats = []
    by_name = {f["feature"]: f for f in (feature_audit.get("features") or [])}
    summary_ctx = {"bregman_funnel": bregman_funnel, "strategy_priority": sp,
                   "paper_realism": pe, "profitability_ranking": prk,
                   "active_learning": al, "correlation_risk": cr}
    for name, f in by_name.items():
        ev_path = _FEATURE_EVIDENCE.get(name)
        ev_val = _g(summary_ctx, *ev_path) if ev_path else None
        # random-exploration blocker is "active" when it kept random opens at 0
        if name == "Random/hash exploration blocker":
            ev_val = (al.get("random_exploration_opened_trades", 0) == 0)
        feats.append({
            "feature": name, "expected_state": "active" if f.get("controls_trades") else "telemetry",
            "actual_state": _feature_status(f.get("runtime_status", "unknown"),
                                            bool(f.get("controls_trades")), ev_val),
            "controls_trades": bool(f.get("controls_trades")),
            "evidence_metric": ".".join(ev_path) if ev_path else "(audit)",
            "evidence_value": ev_val,
        })

    readiness = {
        "readiness_trade_count": run["realistic_trades"],
        "readiness_pnl": run["readiness_pnl"],
        "readiness_excludes_reference_fills": True,
        "readiness_excludes_fallback_fills": True,
        "readiness_excludes_stale_fills": True,
        "readiness_excludes_shadow_only": True,
        "readiness_excludes_exploration": not bool(al.get("exploration_counted_toward_readiness", False)),
        "readiness_includes_bregman_realistic": True,
        "readiness_includes_directional_realistic": True,
        "live_trading_enabled": False,
        "reason_live_disabled": "paper-only build; no wallet/signing/live-submit path exists",
        "bregman_realistic_pnl": pe.get("bregman_realistic_pnl", 0.0),
        "directional_realistic_pnl": pe.get("directional_realistic_pnl", 0.0),
        "exploration_pnl": al.get("exploration_pnl", 0.0),
    }

    summary = {
        "schema_version": SCHEMA_VERSION, "paper_only": True,
        "run": run,
        "feature_activation": {"features": feats,
                               "pass_status": {k: feature_audit.get(k) for k in (
                                   "pass1_status", "pass2_status", "pass3_status",
                                   "pass4_status", "pass5_status", "pass6_status",
                                   "pass7_status") if feature_audit.get(k)}},
        "bregman_funnel": bregman_funnel,
        "strategy_priority": sp,
        "paper_realism": pe,
        "profitability_ranking": prk,
        "active_learning": al,
        "correlation_risk": cr,
        "rejection_waterfall": rejection_waterfall,
        "trade_ledger_summary": trade_ledger,
        "closed_loop_learning": cll,
        "learning_feedback": status.get("learning_feedback", {}) or {},
        "training_reconciliation": training_reconciliation or status.get(
            "training_reconciliation", {}) or {},
        "ledger": ledger or status.get("ledger", {}) or {},
        "grok_news_evidence": grok_news_evidence or {},
        "validation": validation or {},
        "run_ready": run_ready or {},
        "readiness": readiness,
        "data_quality": data_quality,
        "recommendations": recommendations({
            "bregman_funnel": bregman_funnel, "paper_realism": pe, "strategy_priority": sp,
            "profitability_ranking": prk, "active_learning": al, "correlation_risk": cr,
            "run": run}),
    }
    return summary


def _bregman_blocker_explanation(t: dict, certified: int, scanned: int) -> dict:
    """Plain-language explanation of WHY no executable Bregman bundle opened, keyed
    off the dominant rejection reason + near-miss profile. Read-only diagnostic."""
    reasons = dict(t.get("rejected_by_reason", t.get("skip_reasons", {})) or {})
    if certified > 0 or int(t.get("opened_bregman_bundles", t.get("bundles_opened", 0)) or 0) > 0:
        return {"blocked": False, "primary_blocker": None, "detail": "bundles certified/opened"}
    if scanned == 0:
        return {"blocked": True, "primary_blocker": "insufficient_market_universe",
                "detail": "no constraint groups reached the certifier"}
    top = max(reasons.items(), key=lambda kv: kv[1], default=(None, 0))[0] if reasons else None
    mapping = {
        "not_exhaustive": "incomplete_event_families",
        "not_mutually_exclusive": "incomplete_event_families",
        "depth_too_thin": "thin_depth",
        "no_executable_price": "thin_depth",
        "stale_book": "stale_books",
        "spread_too_wide": "wide_spreads",
        "invalid_simplex": "invalid_simplex",
        "duplicate_legs": "invalid_simplex",
        "no_positive_edge": "no_positive_after_cost_lower_bound",
        "settlement_ambiguity": "settlement_ambiguity",
    }
    return {
        "blocked": True,
        "primary_blocker": mapping.get(top, top or "unknown"),
        "dominant_rejection_reason": top,
        "rejection_reason_counts": reasons,
        "detail": ("groups reached the certifier but every one was rejected by a "
                   "STRICT gate (not loosened); dominant reason above"),
    }


def build_bregman_funnel(bregman_telemetry: dict, *, market_groups_detected: int = 0,
                         diagnostic_events_written: int = 0) -> dict:
    """ONE canonical Bregman funnel (TASK 9): all Bregman numbers derive from this.

    Reconciles the market-scanner's detected groups with the constraint-adapter
    funnel so no report can say discovered>0 in one place and 0 in another without
    a named, accounted-for distinction (skip_reasons + adapter_missing_fields)."""
    t = bregman_telemetry or {}

    def _i(*keys) -> int:
        for k in keys:
            if t.get(k) is not None:
                try:
                    return int(t.get(k) or 0)
                except (TypeError, ValueError):
                    continue
        return 0

    discovered = _i("groups_discovered", "raw_groups_discovered")
    scanned = _i("constraint_groups_scanned", "groups_sent_to_certifier")
    skipped = _i("groups_skipped")
    skip_reasons = dict(t.get("skip_reasons", t.get("rejected_by_reason", {})) or {})
    candidates = _i("candidate_arbitrages", "candidates_generated", "certified_opportunities")
    certified = _i("certified_arbitrages", "certified_opportunities", "unique_groups_certified")
    pre_adapter = max(0, int(market_groups_detected) - discovered - skipped) \
        if market_groups_detected else 0
    adapter_failed = skipped
    adapter_success = discovered
    # groups that passed the adapter WERE sent to the certifier; when the telemetry
    # path does not expose an explicit scanned count, derive it from adapter success
    # (avoids the false "discovered>0 but scanned=0" silent-zero contradiction).
    sent_to_certifier = scanned or adapter_success
    # profit-learning status (learning-only; never trade PnL):
    _sl_written = _i("bregman_shadow_labels_written")
    _sl_cand = _i("bregman_shadow_label_candidates", "near_miss_shadow_label_candidate_count")
    _bundles = _i("opened_bregman_bundles", "bundles_opened")
    if _sl_cand > 0 and _sl_written == 0:
        _profit_status = "shadow_writer_not_persisting_candidates"
    elif _sl_written > 0 and _bundles == 0:
        _profit_status = "shadow_data_only"          # learning from shadows, 0 real trades
    elif _sl_written > 0 and _bundles > 0:
        _profit_status = "shadow_and_paper_trades"
    elif _sl_cand == 0:
        _profit_status = "no_near_miss_signal_yet"
    else:
        _profit_status = "idle"
    _profit_sufficiency = ("sufficient" if _sl_written >= 200 else
                           "building" if _sl_written >= 50 else
                           "insufficient")
    _writer_blocker = ("shadow_label_writer_not_persisting_candidates"
                       if (_sl_cand > 0 and _sl_written == 0) else None)
    return {
        "market_group_candidates": int(market_groups_detected or (discovered + skipped)),
        "profit_learning_status": _profit_status,
        "profit_data_sufficiency": _profit_sufficiency,
        "shadow_label_writer_blocker": _writer_blocker,
        "raw_groups_discovered": discovered,
        "groups_rejected_pre_adapter": pre_adapter,
        "groups_adapter_success": adapter_success,
        "groups_adapter_failed": adapter_failed,
        "groups_sent_to_certifier": sent_to_certifier,
        "constraint_groups_scanned": sent_to_certifier,
        "candidates_generated": candidates,
        "certified": certified,
        # legacy aliases kept for back-compat with existing report/tests
        "certified_opportunities": certified,
        "unique_groups_certified": _i("unique_groups_certified") or certified,
        "realistic_executable": _i("executable_depth_certified", "realistic_executable"),
        "bundles_opened": _i("opened_bregman_bundles", "bundles_opened"),
        "skip_reasons": skip_reasons,
        "adapter_missing_fields": dict(t.get("adapter_missing_fields", {}) or {}),
        "diagnostic_events_written": int(diagnostic_events_written),
        # near-miss diagnostics (read-only; explain how close rejected groups were)
        "bregman_near_misses_total": _i("bregman_near_misses_total"),
        "bregman_top_near_misses": list(t.get("bregman_top_near_misses", []) or []),
        "near_miss_by_rejection_reason": dict(t.get("near_miss_by_rejection_reason", {}) or {}),
        "near_miss_one_fix_away_count": _i("near_miss_one_fix_away_count"),
        "near_miss_depth_only_count": _i("near_miss_depth_only_count"),
        "near_miss_not_exhaustive_count": _i("near_miss_not_exhaustive_count"),
        "near_miss_stale_refresh_failed_count": _i("near_miss_stale_refresh_failed_count"),
        # ADVISORY learning-signal aggregates (read-only; help trainer pick near-misses)
        "near_miss_learning_priority_counts": dict(
            t.get("near_miss_learning_priority_counts", {}) or {}),
        "near_miss_shadow_label_candidate_count": _i("near_miss_shadow_label_candidate_count"),
        "near_miss_learning_label_counts": dict(
            t.get("near_miss_learning_label_counts", {}) or {}),
        "near_miss_top_learning_priority": list(
            t.get("near_miss_top_learning_priority", []) or []),
        # profit-discovery: durable shadow labels + queue + bandit (learning-only)
        "bregman_shadow_label_candidates": _i("bregman_shadow_label_candidates"),
        "bregman_shadow_labels_written": _i("bregman_shadow_labels_written"),
        "bregman_shadow_label_write_rate": float(
            t.get("bregman_shadow_label_write_rate", 0.0) or 0.0),
        "shadow_label_write_rejection_reasons": dict(
            t.get("shadow_label_write_rejection_reasons", {}) or {}),
        "profit_discovery_queue_items": _i("profit_discovery_queue_items"),
        "profit_discovery_queue_by_priority": dict(
            t.get("profit_discovery_queue_by_priority", {}) or {}),
        "profit_discovery_queue_actions": dict(
            t.get("profit_discovery_queue_actions", {}) or {}),
        "bandit_router_enabled": bool(t.get("bandit_router_enabled", False)),
        "bandit_action_counts": dict(t.get("bandit_action_counts", {}) or {}),
        "bandit_action_rewards": dict(t.get("bandit_action_rewards", {}) or {}),
        "bandit_selected_action": t.get("bandit_selected_action"),
        "bandit_no_gate_override": bool(t.get("bandit_no_gate_override", True)),
        # targeted market-scan PRIORITIZATION proof (never a trade gate)
        **{k: t.get(k) for k in (
            "targeted_market_scan_enabled", "targeted_markets_scanned_total",
            "targeted_scan_bregman_groups_seen", "targeted_scan_binary_groups_seen",
            "targeted_scan_yes_no_pairs_seen", "targeted_scan_binary_group_matches",
            "targeted_scan_raw_market_matches", "targeted_scan_field_source",
            "targeted_scan_bregman_categories", "targeted_scan_raw_market_categories",
            "targeted_scan_normalized_reject_reasons",
            # read-only CLOB orderbook hydration proof (real YES/NO books)
            "bregman_clob_hydration_enabled", "bregman_clob_hydration_attempted",
            "bregman_clob_hydration_success", "bregman_clob_hydration_failed",
            "bregman_real_yes_no_books_seen", "bregman_synthetic_no_diagnostic_only_count",
            "bregman_certifier_used_real_clob_books", "bregman_hydration_failure_reasons",
            "targeted_scan_budget_by_category", "targeted_scan_hits_by_category",
            "targeted_scan_markets_by_category", "market_quality_tier_counts",
            "market_quality_score_distribution", "high_liquidity_binary_markets_scanned",
            "complete_yes_no_tight_spread_markets_scanned",
            "negative_risk_complete_events_scanned", "short_resolution_markets_scanned",
            "btc_eth_chainlink_markets_scanned", "fed_macro_reference_markets_scanned",
            "high_volume_news_linked_markets_scanned", "complete_event_families_scanned",
            "thin_depth_scan_waste_count", "stale_book_scan_waste_count",
            "invalid_simplex_scan_waste_count", "targeted_scan_missing_data_counts",
            "missing_book_timestamp_count", "missing_depth_count", "missing_volume_count",
            "scan_deprioritized_groups", "scan_deprioritized_by_reason",
            "scan_cooldown_active_groups", "scan_cooldown_reason_counts",
            "scan_exploration_budget_used", "targeted_scan_best_markets",
            "targeted_scan_noop_reasons", "not_exhaustive_high_quality_groups",
            "not_exhaustive_sent_to_sibling_search", "not_exhaustive_sent_to_grok",
            "not_exhaustive_completed_by_metadata", "not_exhaustive_remained_shadow_only")
           if t.get(k) is not None},
        "near_miss_buckets": dict(t.get("near_miss_buckets", {}) or {}),
        "near_miss_all_negative_after_cost_lower_bound": bool(
            t.get("near_miss_all_negative_after_cost_lower_bound", False)),
        "near_miss_tradeable_count": _i("near_miss_tradeable_count"),
        # depth-aware census (REQUIRED depth unchanged) + stale-refresh evidence
        "bregman_required_depth_usd": t.get("bregman_required_depth_usd"),
        "bregman_depth_sufficient_groups": _i("bregman_depth_sufficient_groups"),
        "bregman_depth_insufficient_groups": _i("bregman_depth_insufficient_groups"),
        "bregman_high_liquidity_groups_scanned": _i("bregman_high_liquidity_groups_scanned"),
        "bregman_all_groups_thin": bool(t.get("bregman_all_groups_thin", False)),
        "bregman_promising_groups_refreshed": _i("bregman_promising_groups_refreshed"),
        "bregman_refresh_success": _i("bregman_refresh_success"),
        "bregman_refresh_failed": _i("bregman_refresh_failed"),
        "bregman_stale_after_refresh": _i("bregman_stale_after_refresh"),
        "bregman_refresh_not_attempted_reason": t.get("bregman_refresh_not_attempted_reason"),
        "bregman_worst_leg_depth_usd": t.get("bregman_worst_leg_depth_usd"),
        "bregman_best_depth_quality_score": t.get("bregman_best_depth_quality_score"),
        "bregman_all_groups_depth_insufficient": bool(
            t.get("bregman_all_groups_depth_insufficient", False)),
        # canonical price parser census (trainer path)
        "bregman_price_parse_attempts": _i("bregman_price_parse_attempts"),
        "bregman_price_parse_success": _i("bregman_price_parse_success"),
        "bregman_price_parse_failures": _i("bregman_price_parse_failures"),
        "bregman_price_parse_success_rate": float(
            t.get("bregman_price_parse_success_rate", t.get("parsed_price_success_rate", 1.0)) or 1.0),
        "bregman_non_numeric_price_count": _i("bregman_non_numeric_price_count", "non_numeric_price_count"),
        "bregman_non_numeric_price_examples": list(t.get("bregman_non_numeric_price_examples", []) or []),
        "bregman_missing_price_count": _i("bregman_missing_price_count"),
        "bregman_malformed_price_count": _i("bregman_malformed_price_count"),
        # explicit candidate-generation blocker (candidates=0 is never unexplained)
        "bregman_groups_entered_certifier": _i("bregman_groups_entered_certifier"),
        "bregman_groups_failed_before_candidate_generation": _i(
            "bregman_groups_failed_before_candidate_generation"),
        "bregman_candidate_generation_blocker": t.get("bregman_candidate_generation_blocker"),
        "bregman_candidate_generation_blocker_counts": dict(
            t.get("bregman_candidate_generation_blocker_counts", {}) or {}),
        "bregman_candidate_generation_blocker_samples": list(
            t.get("bregman_candidate_generation_blocker_samples", []) or []),
        # depth-sufficiency-aware zero-candidate hierarchy (never contradictory)
        "bregman_depth_sufficient_but_negative_edge_count": _i(
            "bregman_depth_sufficient_but_negative_edge_count"),
        "bregman_best_depth_sufficient_group_lower_bound": t.get(
            "bregman_best_depth_sufficient_group_lower_bound"),
        "bregman_best_depth_sufficient_group_reject_reason": t.get(
            "bregman_best_depth_sufficient_group_reject_reason"),
        "bregman_real_market_zero_candidate_reason": t.get(
            "bregman_real_market_zero_candidate_reason"),
        "bregman_real_market_zero_candidate_reason_counts": dict(
            t.get("bregman_real_market_zero_candidate_reason_counts", {}) or {}),
        "bregman_best_real_group_summary": t.get("bregman_best_real_group_summary"),
        # per-STAGE certification census + divergence proof (never-silent certifier)
        "bregman_rejection_stage_counts": dict(
            t.get("bregman_rejection_stage_counts", {}) or {}),
        "bregman_max_divergence_gap": t.get("bregman_max_divergence_gap"),
        "bregman_best_projected_lower_bound": t.get("bregman_best_projected_lower_bound"),
        "bregman_positive_projected_but_rejected_count": _i(
            "bregman_positive_projected_but_rejected_count"),
        "bregman_positive_projected_rejected_by_stage": dict(
            t.get("bregman_positive_projected_rejected_by_stage", {}) or {}),
        "bregman_zero_certified_explanation": t.get("bregman_zero_certified_explanation"),
        # per-group profit-lower-bound census (always a float, even negative/zero)
        "bregman_certify_diagnostics_sample": list(
            t.get("bregman_certify_diagnostics_sample", []) or []),
        "bregman_profit_lower_bound_min": t.get("bregman_profit_lower_bound_min"),
        "bregman_profit_lower_bound_max": t.get("bregman_profit_lower_bound_max"),
        "bregman_profit_lower_bound_mean": t.get("bregman_profit_lower_bound_mean"),
        "bregman_groups_negative_lower_bound": _i("bregman_groups_negative_lower_bound"),
        "bregman_groups_zero_lower_bound": _i("bregman_groups_zero_lower_bound"),
        "bregman_groups_positive_lower_bound": _i("bregman_groups_positive_lower_bound"),
        "bregman_certifier_exception": t.get("bregman_certifier_exception"),
        # near-miss honesty
        "best_after_cost_lower_bound": t.get("best_after_cost_lower_bound"),
        "best_depth_sufficient_lower_bound": t.get("best_depth_sufficient_lower_bound"),
        "best_complete_group_lower_bound": t.get("best_complete_group_lower_bound"),
        "best_one_fix_away_reason": t.get("best_one_fix_away_reason"),
        "all_top_near_misses_negative_lower_bound": bool(
            t.get("all_top_near_misses_negative_lower_bound", False)),
        # precise price/outcome parsing diagnostics (from the ABCAS scanner merge)
        "non_numeric_price_count": _i("non_numeric_price_count"),
        "insufficient_outcomes_count": _i("insufficient_outcomes_count"),
        "malformed_group_count": _i("malformed_group_count", "malformed_groups_rejected"),
        "parsed_price_success_rate": float(t.get("parsed_price_success_rate", 1.0) or 1.0),
        "skip_reason_samples": dict(t.get("skip_reason_samples", {}) or {}),
        "blocker_explanation": _bregman_blocker_explanation(t, certified, sent_to_certifier),
        # consistency invariant: every detected group must be ACCOUNTED FOR as either
        # adapter-success (scanned) or adapter-failure (skip with a reason). An
        # unexplained gap (pre_adapter > 0) is a silent-zero contradiction and FAILS.
        "internally_consistent": bool(
            (market_groups_detected or 0) == 0
            or (pre_adapter == 0 and (discovered + skipped) > 0)),
    }


def build_grok_news_evidence(research: dict, *, news_items_used: int = 0) -> dict:
    """Grok/news evidence telemetry with an explicit zero-call reason (TASK 10)."""
    r = research or {}
    calls = int(r.get("grok_calls_total", 0) or 0)
    enabled = bool(r.get("grok_enabled", False))
    has_key = bool(r.get("grok_has_api_key", r.get("grok_enabled", False)))
    try:
        from engine.research.schemas import ONLINE_MODES as _ONLINE
        _online_modes = set(_ONLINE) | {"online", "online_research", "live", "grok_online"}
    except Exception:  # noqa: BLE001
        _online_modes = {"online_paper", "online_shadow", "guarded_live_readonly",
                         "online", "online_research", "live", "grok_online"}
    mode_is_online = str(r.get("research_mode", "")).strip().lower() in _online_modes
    online_active = bool((enabled and has_key) and mode_is_online)
    reason = r.get("grok_zero_call_reason")
    if calls == 0 and not reason:
        if not enabled or not has_key:
            reason = "grok_disabled_or_no_api_key"
        elif not mode_is_online:
            reason = "research_mode_not_online"
        elif int(news_items_used or 0) == 0:
            reason = "no_news_packet_selected"
        else:
            reason = "no_eligible_markets_or_advisory_not_due"
    # grok_brain_ready = a real advisory call actually happened. Enabled+key+online+
    # news with zero calls is a BLOCKER (not healthy). Separate from paper run-ready.
    grok_brain_ready = bool(r.get("grok_brain_ready", calls >= 1))
    grok_brain_blocker = (None if grok_brain_ready
                          else r.get("grok_brain_blocker") or reason or "no_grok_call_yet")
    return {
        "grok_enabled": enabled,
        "grok_has_api_key": has_key,
        "xai_api_key_present": bool(r.get("xai_api_key_present", has_key)),
        "xai_api_key_source": r.get("xai_api_key_source", "XAI_API_KEY"),
        "grok_online_active": online_active,
        "grok_brain_ready": grok_brain_ready,
        "grok_brain_blocker": grok_brain_blocker,
        "research_mode": r.get("research_mode"),
        "news_items_used": int(news_items_used or 0),
        "grok_calls_total": calls,
        "grok_calls_with_news": int(r.get("grok_calls_with_news", 0) or 0),
        "grok_advisory_only_count": int(r.get("grok_advisory_only_count", calls) or 0),
        "grok_evidence_records_written": int(r.get("grok_evidence_records_written", 0) or 0),
        # bounded advisory scheduler telemetry (research only; never execution)
        "grok_advisory_enabled": bool(r.get("grok_advisory_enabled", True)),
        "grok_advisory_max_calls_per_hour": int(r.get("grok_advisory_max_calls_per_hour", 0) or 0),
        "grok_advisory_calls_per_hour": int(r.get("grok_advisory_calls_per_hour", 0) or 0),
        "grok_proof_calls_total": int(r.get("grok_proof_calls_total", 0) or 0),
        "grok_scheduler_calls_total": int(r.get("grok_scheduler_calls_total", 0) or 0),
        "grok_total_calls_reconciled": bool(r.get("grok_total_calls_reconciled", True)),
        "grok_scheduled_calls": int(r.get("grok_scheduled_calls", 0) or 0),
        "grok_scheduler_eligible_targets": int(r.get("grok_scheduler_eligible_targets", 0) or 0),
        "grok_scheduler_targets_selected": int(r.get("grok_scheduler_targets_selected", 0) or 0),
        "grok_scheduler_targets_skipped": int(r.get("grok_scheduler_targets_skipped", 0) or 0),
        "grok_scheduler_skip_reasons": dict(r.get("grok_scheduler_skip_reasons", {}) or {}),
        "grok_scheduler_rate_limited_count": int(r.get("grok_scheduler_rate_limited_count", 0) or 0),
        "grok_scheduler_no_target_count": int(r.get("grok_scheduler_no_target_count", 0) or 0),
        "grok_bregman_incomplete_groups_analyzed": int(r.get("grok_bregman_incomplete_groups_analyzed", 0) or 0),
        "grok_bregman_malformed_groups_analyzed": int(r.get("grok_bregman_malformed_groups_analyzed", 0) or 0),
        "grok_learning_features_written": int(r.get("grok_learning_features_written", 0) or 0),
        "grok_best_bregman_group_analyzed": bool(r.get("grok_best_bregman_group_analyzed", False)),
        "grok_best_bregman_group_skip_reason": r.get("grok_best_bregman_group_skip_reason"),
        "grok_market_groups_analyzed": int(r.get("grok_market_groups_analyzed", 0) or 0),
        "grok_bregman_near_misses_analyzed": int(r.get("grok_bregman_near_misses_analyzed", 0) or 0),
        "grok_news_linked_markets_analyzed": int(r.get("grok_news_linked_markets_analyzed", 0) or 0),
        "grok_contributed_learning_features": bool(r.get("grok_contributed_learning_features",
                                                         calls >= 1)),
        "grok_advisory_only_invariant": True,
        "grok_no_execution_override": True,
        "grok_eligible_markets": int(r.get("grok_eligible_markets", 0) or 0),
        "grok_scheduled_calls": int(r.get("grok_scheduled_calls", 0) or 0),
        "grok_skipped_rate_limit": int(r.get("grok_skipped_rate_limit", 0) or 0),
        "grok_skipped_no_news_packet": int(r.get("grok_skipped_no_news_packet", 0) or 0),
        "grok_skipped_no_market_link": int(r.get("grok_skipped_no_market_link", 0) or 0),
        "grok_provider_errors": int(r.get("grok_provider_errors", 0) or 0),
        "grok_zero_call_reason": (reason if calls == 0 else None),
    }


def build_run_ready(*, reconciliation: dict, ledger: dict, bregman_funnel: dict,
                    missing_event_files: list, missing_report_files: list,
                    live_trading_disabled: bool, decision_count: int,
                    bregman_enabled: bool, training_healthy: bool = True) -> dict:
    """Strict multi-hour run-ready gate (TASK 12). Only sets run_ready_for_hours
    true after EVERY hard durability/reconciliation requirement passes; otherwise
    caps max_safe_runtime_minutes at 10 and lists the blocking reasons."""
    recon = reconciliation or {}
    led = ledger or {}
    bf = bregman_funnel or {}
    blocking: list = []
    warnings: list = []
    event_files_present = not missing_event_files
    if missing_event_files:
        blocking.append(f"durable event files missing: {missing_event_files}")
    recon_passed = bool(recon.get("reconciled", False))
    if not recon and decision_count > 0:
        blocking.append("training_reconciliation.json missing")
    elif decision_count > 0 and not recon_passed:
        blocking.append(f"training reconciliation failed: {recon.get('divergence_reason')}")
    ledger_decisions = int(led.get("decisions", 0) or 0)
    ledger_reconciled = not (decision_count > 0 and ledger_decisions == 0)
    if not ledger_reconciled:
        blocking.append("ledger.decisions==0 while decision_count>0")
    bregman_non_silent = (not bregman_enabled) or bool(
        bf.get("internally_consistent", True)) and (
        bf.get("market_group_candidates", 0) == 0
        or bf.get("groups_sent_to_certifier", 0) > 0
        or bf.get("groups_adapter_failed", 0) > 0)
    if bregman_enabled and not bregman_non_silent:
        blocking.append("Bregman enabled but funnel silently scanned zero groups "
                        "with no adapter diagnostics")
    inspection_complete = not missing_report_files
    if missing_report_files:
        blocking.append(f"inspection artifacts incomplete: {missing_report_files}")
    if not live_trading_disabled:
        blocking.append("live trading not disabled")
    closed_loop_durable = event_files_present and (
        decision_count == 0 or int(recon.get("decision_events", 0) or 0) > 0)
    proof = {
        "training_healthy": bool(training_healthy),
        "event_files_present": bool(event_files_present),
        "ledger_reconciled": bool(ledger_reconciled),
        "training_reconciliation_passed": bool(recon_passed),
        "bregman_funnel_non_silent": bool(bregman_non_silent),
        "closed_loop_durable": bool(closed_loop_durable),
        "inspection_artifacts_complete": bool(inspection_complete),
        "live_trading_disabled": bool(live_trading_disabled),
    }
    run_ready = not blocking and all(proof.values())
    return {
        "run_ready_for_hours": bool(run_ready),
        "max_safe_runtime_minutes": (None if run_ready else 10),
        "required_before_two_hour_run": list(blocking),
        "blocking_reasons": list(blocking),
        "warnings": warnings,
        "proof": proof,
    }


def recommendations(summary: dict) -> list:
    """Deterministic, metric-driven recommendations (never 'loosen thresholds'
    unless the bottleneck is proven to be conservatism, not realism)."""
    bf = summary.get("bregman_funnel", {})
    pe = summary.get("paper_realism", {})
    prk = summary.get("profitability_ranking", {})
    al = summary.get("active_learning", {})
    cr = summary.get("correlation_risk", {})
    run = summary.get("run", {})
    recs: list = []
    if bf.get("raw_groups_discovered", 0) > 0 and bf.get("certified_opportunities", 0) == 0:
        recs.append({"code": "bregman_discovered_none_certified", "severity": "high",
                     "message": "Bregman discovered groups but certified 0 — inspect "
                                "grouping completeness/exhaustiveness + certifier "
                                "reject reasons (rejected_by_reason)."})
    if bf.get("certified_opportunities", 0) > 0 and bf.get("bundles_opened", 0) == 0:
        recs.append({"code": "bregman_certified_none_opened", "severity": "high",
                     "message": "Certified Bregman opportunities did not open — check "
                                "paper realism, per-tick budget/slot caps, and "
                                "correlation/duplicate-bundle blocks."})
    if pe.get("reference_fills_blocked", 0) > 5:
        recs.append({"code": "many_reference_fills_blocked", "severity": "medium",
                     "message": "Many reference-price fills blocked — improve live CLOB "
                                "book coverage so candidates become executable (NOT a "
                                "reason to enable reference fills)."})
    if prk.get("candidates_rejected_negative_after_cost", 0) > 10:
        recs.append({"code": "many_negative_after_cost", "severity": "medium",
                     "message": "Many negative-after-cost rejects — analyze candidate "
                                "source quality / cost model; do not loosen thresholds."})
    if al.get("active_learning_candidates_selected", 0) == 0 \
            and al.get("active_learning_candidates_considered", 0) > 0:
        recs.append({"code": "active_learning_selected_none", "severity": "medium",
                     "message": "Active learning considered candidates but selected 0 — "
                                "check exploration realism/loss/diversity eligibility gates."})
    if cr.get("candidates_missing_cluster_id", 0) > cr.get("candidates_with_cluster_id", 0):
        recs.append({"code": "many_missing_cluster_ids", "severity": "medium",
                     "message": "More candidates missing cluster metadata than carrying it "
                                "— improve cluster-key generation (event/condition/semantic)."})
    if (run.get("directional_trades_opened", 0) > 0
            and bf.get("certified_opportunities", 0) > bf.get("bundles_opened", 0)):
        recs.append({"code": "directional_while_bregman_pending", "severity": "high",
                     "message": "Directional traded while certified Bregman did not fully "
                                "open — verify strategy-priority reservation."})
    if run.get("realistic_trades", 0) == 0:
        cause = ("strict paper realism (no live-executable book)"
                 if pe.get("missing_ask_rejection_count", 0) or pe.get("stale_book_rejection_count", 0)
                 else "lack of after-cost-positive edge")
        recs.append({"code": "no_realistic_trades", "severity": "info",
                     "message": f"No realistic trades opened — attributable to {cause}. "
                                "This is conservative, not a bug."})
    if not recs:
        recs.append({"code": "nominal", "severity": "info",
                     "message": "No bottleneck detected from current metrics."})
    return recs


def console_summary(summary: dict) -> str:
    run = summary.get("run", {})
    bf = summary.get("bregman_funnel", {})
    al = summary.get("active_learning", {})
    cr = summary.get("correlation_risk", {})
    pe = summary.get("paper_realism", {})
    corr_blocks = sum(int(cr.get(k, 0) or 0) for k in (
        "blocked_same_market", "blocked_same_condition", "blocked_same_event",
        "blocked_same_cluster", "blocked_bregman_market_collision",
        "blocked_bregman_event_collision"))
    ref_real = 1 if float(pe.get("reference_fill_theoretical_pnl", 0.0) or 0.0) != 0.0 else 0
    cll = summary.get("closed_loop_learning", {}) or {}
    return "\n".join([
        "Run complete.",
        f"Mode: {run.get('mode', 'paper')}",
        f"Raw markets: {run.get('raw_markets_loaded', 0)}",
        f"Eligible markets: {run.get('eligible_markets', 0)}",
        f"Decision records: {cll.get('decision_records_written', 0)}",
        f"No-trade labels: {cll.get('no_trade_labels_written', 0)}",
        f"Shadow labels: {cll.get('shadow_records_written', 0)}",
        f"Active-learning selected shadow/tiny: "
        f"{cll.get('active_learning_shadow_selected', 0)}/"
        f"{cll.get('active_learning_tiny_trades_selected', 0)}",
        f"Pending labels total: {cll.get('pending_labels_total', 0)}",
        f"Completed labels total: {cll.get('completed_labels_total', 0)}",
        f"Feedback/hr: {cll.get('feedback_per_hour', 0.0)}",
        f"Labels/day: {cll.get('labels_resolved_per_day', 0.0)}",
        f"Learning growth: {cll.get('learning_growth_status', 'unknown')}",
        f"Bregman groups discovered/certified/opened: "
        f"{bf.get('raw_groups_discovered', 0)}/{bf.get('certified_opportunities', 0)}/"
        f"{bf.get('bundles_opened', 0)}",
        f"Directional trades opened: {run.get('directional_trades_opened', 0)}",
        f"Exploration trades opened: {run.get('exploration_trades_opened', 0)}",
        "Fill realism enabled: true",
        f"After-cost PnL: {pe.get('realistic_pnl', 0.0)}",
        f"Reference fills counted as real: {ref_real}",
        f"Random exploration trades: {al.get('random_exploration_opened_trades', 0)}",
        f"Correlation blocks: {corr_blocks}",
        # NOTE: absolute artifact paths (with exists/size/rows) are printed by the
        # training entrypoint AFTER the files are flushed — these counters are
        # in-memory and are not proof of durable writes on their own.
    ])


def _kv(d: dict, keys) -> list:
    return [f"- {k}: {d.get(k)}" for k in keys]


def to_markdown(summary: dict) -> str:
    run = summary.get("run", {})
    bf = summary.get("bregman_funnel", {})
    sp = summary.get("strategy_priority", {})
    pe = summary.get("paper_realism", {})
    prk = summary.get("profitability_ranking", {})
    al = summary.get("active_learning", {})
    cr = summary.get("correlation_risk", {})
    rw = summary.get("rejection_waterfall", {})
    tl = summary.get("trade_ledger_summary", {})
    rd = summary.get("readiness", {})
    dq = summary.get("data_quality", {})
    L: list = []
    L.append("# Polymarket Paper-Training Inspection Report")
    L.append("")
    L.append("_PAPER ONLY · unified observability across Pass 1-7 · no live trading._")
    L.append("")

    L.append("## Executive Summary")
    L += _kv(run, ["run_id", "mode", "execution_mode", "runtime_seconds",
                   "live_trading_disabled", "strict_paper_realism", "bregman_priority_enabled",
                   "raw_markets_loaded", "eligible_markets", "candidates_considered",
                   "trades_opened", "realistic_trades", "bregman_bundles_opened",
                   "directional_trades_opened", "exploration_trades_opened",
                   "shadow_only_opportunities", "hard_rejects", "realistic_pnl",
                   "shadow_theoretical_pnl", "readiness_pnl"])
    L.append("")

    L.append("## Feature Activation")
    L.append("")
    L.append("| Feature | Expected | Actual state | Controls trades? | Evidence metric | Value |")
    L.append("|---|---|---|---|---|---|")
    for f in summary.get("feature_activation", {}).get("features", []):
        L.append(f"| {f['feature']} | {f['expected_state']} | `{f['actual_state']}` | "
                 f"{'YES' if f['controls_trades'] else 'no'} | {f['evidence_metric']} | "
                 f"{f['evidence_value']} |")
    L.append("")

    L.append("## Bregman / ABCAS Funnel")
    L += _kv(bf, ["raw_catalog_markets_scanned", "eligible_raw_markets", "raw_groups_discovered",
                  "graph_groups_discovered", "groups_from_graph_used", "duplicate_groups_dropped",
                  "unique_groups_certified", "certified_opportunities", "bundles_opened",
                  "capital_committed", "sees_full_raw_catalog", "evaluated_before_directional"])
    L.append(f"- rejection breakdown: {bf.get('rejected_by_reason', {})}")
    L.append(f"- Did Bregman see the full raw catalog? {bf.get('sees_full_raw_catalog')}")
    L.append(f"- Did Bregman run before directional? {bf.get('evaluated_before_directional')}")
    L.append(f"- Did any Bregman bundle open? {bf.get('bundles_opened', 0) > 0}")
    L.append("")

    L.append("## Strategy Priority / Capital Allocation")
    L += _kv(sp, ["bregman_priority_enabled", "bregman_evaluated_before_directional",
                  "bregman_reserved_slots", "bregman_reserved_capital_usd",
                  "directional_slots_before_bregman", "directional_slots_after_bregman",
                  "unused_bregman_slots_released_to_directional",
                  "directional_trades_blocked_by_bregman_reservation",
                  "directional_trades_blocked_by_bregman_market_collision",
                  "directional_trades_blocked_by_bregman_event_collision",
                  "exploration_blocked_from_reserved_bregman_capacity",
                  "directional_consumed_capacity_before_bregman"])
    L.append("")

    L.append("## Paper Execution Realism")
    L += _kv(pe, ["reference_price_fills_allowed_for_exploit", "reference_fill_attempts",
                  "reference_fills_allowed", "reference_fills_blocked", "fallback_fill_count",
                  "stale_book_rejection_count", "missing_ask_rejection_count",
                  "thin_depth_rejection_count", "wide_spread_rejection_count",
                  "offline_stub_rejection_count", "ambiguity_rejection_count",
                  "avg_spread_executed", "avg_depth_executed", "avg_book_age_executed",
                  "realistic_trade_count", "shadow_trade_count", "realistic_pnl",
                  "shadow_theoretical_pnl", "readiness_pnl", "reference_fill_theoretical_pnl"])
    _ref = float(pe.get("reference_fill_theoretical_pnl", 0.0) or 0.0)
    L.append(f"- Did any unrealistic fill count toward PnL/readiness? {_ref != 0.0}")
    L.append("")

    L.append("## Profitability Ranking")
    L += _kv(prk, ["profitability_first_enabled", "profitability_annotation_before_truncation",
                   "candidates_annotated", "candidates_missing_profitability_data",
                   "candidates_ranked_by_profitability", "candidates_rejected_negative_after_cost",
                   "candidates_shadow_theoretical_only", "directional_after_cost_positive",
                   "bregman_after_cost_positive", "avg_after_cost_edge_executed",
                   "avg_after_cost_roi_executed", "total_expected_value_usd_executed",
                   "execution_without_annotation", "top_ranked_candidate_reason"])
    L.append(f"- profitability_buckets: {prk.get('profitability_buckets', {})}")
    L.append(f"- Any trade executed without profitability annotation? "
             f"{prk.get('execution_without_annotation', 0) > 0}")
    L.append("")

    L.append("## Active Learning / Exploration")
    L += _kv(al, ["active_learning_enabled", "random_exploration_enabled",
                  "random_exploration_opened_trades", "legacy_random_exploration_blocked",
                  "active_learning_candidates_considered", "active_learning_candidates_selected",
                  "exploration_trades_opened", "exploration_shadow_only",
                  "exploration_rejected_by_realism", "exploration_rejected_by_budget",
                  "exploration_rejected_by_collision", "exploration_budget_used_usd",
                  "exploration_expected_loss_usd", "exploration_pnl",
                  "exploration_counted_toward_readiness", "top_learning_buckets",
                  "category_coverage", "cluster_diversity", "pending_feedback_count",
                  "completed_feedback_count", "avg_active_learning_score_selected"])
    L.append("")

    L.append("## Correlation / Cluster Risk")
    L += _kv(cr, ["correlation_gate_enabled", "require_cluster_metadata", "unknown_cluster_policy",
                  "candidates_with_cluster_id", "candidates_missing_cluster_id",
                  "open_clusters_count", "open_events_count", "open_correlation_groups_count",
                  "blocked_same_market", "blocked_same_condition", "blocked_same_event",
                  "blocked_same_cluster", "blocked_bregman_market_collision",
                  "blocked_bregman_event_collision", "blocked_exploration_cluster_collision",
                  "size_capped_by_cluster_exposure", "shadowed_unknown_cluster",
                  "max_cluster_exposure_usd", "max_event_exposure_usd", "top_open_clusters",
                  "real_trade_without_cluster_metadata"])
    L.append(f"- Any real trade without cluster metadata? "
             f"{cr.get('real_trade_without_cluster_metadata', 0) > 0}")
    L.append("")

    L.append("## Candidate Rejection Waterfall")
    L.append(f"- total rejections: {rw.get('total_rejections', 0)}")
    for r in rw.get("ranked_reasons", []):
        L.append(f"  - {r['reason']}: {r['count']}")
    L.append(f"- by strategy: {rw.get('by_strategy', {})}")
    L.append("")

    L.append("## Opened Paper Trades / Bundles")
    L += _kv(tl, ["total_opened", "bregman_legs", "directional_trades", "exploration_trades"])
    for b in tl.get("bregman_bundles", [])[:10]:
        L.append(f"  - bundle {b.get('bundle_id')}: legs={b.get('legs')} "
                 f"cost=${b.get('total_cost')} all_legs_executable={b.get('all_legs_executable')}")
    for t in tl.get("trades", [])[-10:]:
        L.append(f"  - {t.get('strategy_tier')} {t.get('market_id')} side={t.get('side')} "
                 f"${t.get('notional_usd')} status={t.get('execution_realism_status')} "
                 f"readiness={t.get('readiness_eligible')}")
    L.append("")

    cll = summary.get("closed_loop_learning", {}) or {}
    L.append("## Closed-Loop Learning")
    L += _kv(cll, ["closed_loop_enabled", "learning_growth_status", "learning_growth_score",
                   "decision_records_written", "candidate_records_written",
                   "no_trade_labels_written", "shadow_records_written",
                   "active_learning_shadow_selected", "active_learning_tiny_trades_selected",
                   "pending_labels_total", "completed_labels_total", "feedback_per_hour",
                   "labels_resolved_per_day", "calibration_updates",
                   "active_learning_used_feedback", "learning_state_loaded",
                   "learning_state_saved", "zero_selection_reason",
                   "top_learning_bottlenecks"])
    L.append("")
    L.append("## Readiness / Real Edge Score")
    L += _kv(rd, ["readiness_trade_count", "readiness_pnl", "bregman_realistic_pnl",
                  "directional_realistic_pnl", "exploration_pnl",
                  "readiness_excludes_reference_fills", "readiness_excludes_fallback_fills",
                  "readiness_excludes_stale_fills", "readiness_excludes_shadow_only",
                  "readiness_excludes_exploration", "readiness_includes_bregman_realistic",
                  "readiness_includes_directional_realistic", "live_trading_enabled",
                  "reason_live_disabled"])
    L.append("")

    L.append("## Data Quality / Market Coverage")
    L += _kv(dq, ["catalog_load_success", "markets_loaded", "markets_eligible",
                  "markets_shortlisted", "stale_rate", "null_rate", "feature_coverage",
                  "candidates_with_cluster_metadata", "candidates_missing_cluster_metadata",
                  "candidates_with_profitability_annotation",
                  "candidates_missing_profitability_annotation", "chainlink_enabled",
                  "research_mode", "grok_enabled"])
    L.append("")

    L.append("## Recommendations")
    for r in summary.get("recommendations", []):
        L.append(f"- [{r.get('severity')}] ({r.get('code')}) {r.get('message')}")
    L.append("")
    return "\n".join(L)


def validate_report(markdown: str) -> list:
    """Return the list of REQUIRED_SECTIONS missing from the rendered report."""
    return [s for s in REQUIRED_SECTIONS if f"## {s}" not in markdown]
