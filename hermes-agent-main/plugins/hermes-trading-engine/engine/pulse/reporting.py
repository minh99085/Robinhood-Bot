"""Light-report assembly + learning loop for the BTC 5-min pulse.

Aggregates settled-outcome PnL/calibration across every entry-time tag dimension (Hurst regime,
z-score bucket, half-life bucket, Markov state, time-to-resolution, spread bucket, depth bucket,
confidence tier) and assembles the full latest light report — including candidate lifecycle
reconciliation, execution stats, reject reasons, EV before/after costs, calibration table,
sample sizes, missing-data reasons, and promotion/demotion candidates. Report-only.
"""

from __future__ import annotations

import json
from typing import Optional


def spread_bucket(s: Optional[float]) -> str:
    if s is None:
        return "na"
    if s <= 0.01:
        return "<=0.01"
    if s <= 0.03:
        return "0.01-0.03"
    if s <= 0.06:
        return "0.03-0.06"
    return ">0.06"


def depth_bucket(d: Optional[float]) -> str:
    if d is None:
        return "na"
    if d < 50:
        return "<50"
    if d < 200:
        return "50-200"
    if d < 1000:
        return "200-1000"
    return ">=1000"


def confidence_tier(c: Optional[float]) -> str:
    if c is None:
        return "na"
    if c < 0.34:
        return "low"
    if c < 0.67:
        return "medium"
    return "high"


class OutcomeGroups:
    """Groups settled paper PnL / win-rate / Brier by every entry-time tag dimension."""

    def __init__(self):
        self.dims: dict = {}

    def record(self, tags: dict, *, pnl: float, won: bool, fair_at_entry: Optional[float],
               outcome_up: Optional[bool]) -> None:
        for dim, bucket in (tags or {}).items():
            d = self.dims.setdefault(dim, {})
            g = d.setdefault(str(bucket if bucket is not None else "na"),
                             {"n": 0, "wins": 0, "pnl": 0.0, "brier_sum": 0.0, "brier_n": 0})
            g["n"] += 1
            g["wins"] += int(bool(won))
            g["pnl"] = round(g["pnl"] + float(pnl), 6)
            if fair_at_entry is not None and outcome_up is not None:
                g["brier_sum"] += (float(fair_at_entry) - (1.0 if outcome_up else 0.0)) ** 2
                g["brier_n"] += 1

    def summary(self) -> dict:
        out = {}
        for dim, buckets in self.dims.items():
            out[dim] = {b: {"n": g["n"],
                            "win_rate": (round(g["wins"] / g["n"], 4) if g["n"] else None),
                            "pnl_usd": round(g["pnl"], 4),
                            "brier": (round(g["brier_sum"] / g["brier_n"], 4) if g["brier_n"] else None)}
                        for b, g in buckets.items()}
        return out


def _pos_field(pos, name: str, default=None):
    """Read a field from a PulsePosition or a persisted position dict."""
    if isinstance(pos, dict):
        return pos.get(name, default)
    return getattr(pos, name, default)


def ledger_stats_by_market_series(positions: dict) -> dict:
    """Concise per-series performance (5m vs 15m) from settled ledger positions."""
    rows = {}
    for pos in (positions or {}).values():
        status = _pos_field(pos, "status")
        if status != "settled":
            continue
        research = _pos_field(pos, "research") or {}
        series = str(research.get("market_series") or research.get("series_slug")
                     or "btc-up-or-down-5m")
        label = str(research.get("series_label") or ("15m" if "15m" in series else "5m"))
        key = series
        st = rows.setdefault(key, {
            "series_slug": series, "series_label": label,
            "settled": 0, "wins": 0, "pnl_usd": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0,
            "side_n": {"up": 0, "down": 0}, "side_wins": {"up": 0, "down": 0},
        })
        pnl = float(_pos_field(pos, "pnl_usd") or 0.0)
        won = bool(_pos_field(pos, "won"))
        side = str(_pos_field(pos, "side") or "").lower()
        st["settled"] += 1
        st["wins"] += int(won)
        st["pnl_usd"] = round(st["pnl_usd"] + pnl, 4)
        if pnl > 0:
            st["gross_win"] = round(st["gross_win"] + pnl, 4)
        elif pnl < 0:
            st["gross_loss"] = round(st["gross_loss"] + (-pnl), 4)
        if side in st["side_n"]:
            st["side_n"][side] += 1
            if won:
                st["side_wins"][side] += 1
    out = {}
    for series, st in rows.items():
        n = st["settled"]
        wr = round(st["wins"] / n, 4) if n else None
        pf = None
        if st["gross_loss"] > 0:
            pf = round(st["gross_win"] / st["gross_loss"], 4)
        elif st["gross_win"] > 0:
            pf = 999.0
        out[series] = {
            **st,
            "win_rate": wr,
            "profit_factor": pf,
            "avg_pnl_per_trade": (round(st["pnl_usd"] / n, 4) if n else None),
            "win_rate_up": (round(st["side_wins"]["up"] / st["side_n"]["up"], 4)
                            if st["side_n"]["up"] else None),
            "win_rate_down": (round(st["side_wins"]["down"] / st["side_n"]["down"], 4)
                              if st["side_n"]["down"] else None),
        }
    return out


def promotion_demotion(tier_table: dict) -> dict:
    """From the report-only tier table, list promotion (A+/A) and demotion (C/D) candidates."""
    table = (tier_table or {}).get("table", {})
    promote = [k for k, v in table.items() if v.get("tier") in ("A+", "A")]
    demote = [k for k, v in table.items() if v.get("tier") in ("C", "D")]
    return {"promotion_candidates": promote, "demotion_candidates": demote}


def build_light_report(*, lifecycle: dict, execution_gate: dict, ledger_stats: dict,
                       calibration: dict, ev_stats: dict, outcome_groups: OutcomeGroups,
                       tier_table: dict, edge_model: dict, sizing: dict,
                       missing_data_reasons: dict, baseline: dict,
                       gate_thresholds: dict, gate_observations: dict) -> dict:
    from engine.pulse.reconciliation import global_reconciliation, zero_reject_diagnostic
    grouped = outcome_groups.summary()
    accepted = lifecycle.get("terminals", {}).get("accepted", 0)
    settled = ledger_stats.get("settled", 0)
    pnl_by = {f"pnl_by_{dim}": g for dim, g in grouped.items()}
    recon = global_reconciliation(lifecycle=lifecycle, exec_gate=execution_gate,
                                  ledger_stats=ledger_stats, baseline=baseline)
    zero_diag = zero_reject_diagnostic(
        exec_gate=execution_gate, thresholds=gate_thresholds, observations=gate_observations,
        rejected_before_execution=recon.get("rejected_before_execution", 0))
    return {
        "schema": "btc_pulse_light_report/1.3", "report_only": True, "live_trading_enabled": False,
        # headline integrity flag — true ONLY when every lifecycle/exec/ledger identity holds
        "global_reconciled": recon["global_reconciled"],
        "reconciliation": recon,
        "execution_gate_zero_reject_diagnostic": zero_diag,
        "candidate_lifecycle": lifecycle,
        "execution_stats": execution_gate,
        "reject_reasons": execution_gate.get("rejected", {}),
        "ev_before_after_costs": ev_stats,
        "ledger": ledger_stats,
        "calibration": calibration,
        "edge_model_calibration": edge_model.get("calibration_table", {}),
        "sample_sizes": {"accepted": accepted, "settled": settled,
                         "candidates": lifecycle.get("created", 0),
                         "edge_model_labeled": edge_model.get("n_labeled", 0)},
        "missing_data_reasons": missing_data_reasons,
        "confidence_tier_table": tier_table,
        "sizing": sizing,
        **pnl_by,
        **promotion_demotion(tier_table),
    }


def _pnl_buckets(light: dict) -> dict:
    return {k: light[k] for k in (light or {}) if k.startswith("pnl_by_")}


def build_report_sections(light: dict, *, status: Optional[dict] = None,
                          ledger: Optional[dict] = None) -> dict:
    """Organize the light report into three operator-facing sections."""
    light = light or {}
    status = status or {}
    ledger = ledger or {}
    cap = light.get("capital", {}) or {}
    led = light.get("ledger", {}) or {}
    arb = light.get("arbitrage", {}) or {}
    sg = light.get("learned_selectivity_gate", {}) or {}
    tv = light.get("tradingview", {}) or {}
    gd = light.get("grok_decider", {}) or {}

    dir_pnl = None
    if cap.get("total_realized_pnl_usd") is not None and cap.get("arb_realized_pnl_usd") is not None:
        dir_pnl = round(float(cap["total_realized_pnl_usd"]) - float(cap["arb_realized_pnl_usd"]), 4)

    by_series = light.get("by_market_series") or {}
    trading_performance = {
        "headline": {
            "on_hand_capital_usd": cap.get("on_hand_capital_usd"),
            "total_on_hand_usd": cap.get("total_on_hand_usd"),
            "starting_capital_usd": cap.get("starting_capital_usd"),
            "return_pct": cap.get("return_pct"),
            "total_return_pct": cap.get("total_return_pct"),
            "directional_realized_pnl_usd": cap.get("realized_pnl_usd") or dir_pnl,
            "arb_realized_pnl_usd": cap.get("arb_realized_pnl_usd") or arb.get("realized_profit_usd"),
            "total_realized_pnl_usd": cap.get("total_realized_pnl_usd"),
            "win_rate": led.get("win_rate"),
            "win_rate_up": led.get("win_rate_up"),
            "win_rate_down": led.get("win_rate_down"),
            "profit_factor": led.get("profit_factor"),
            "trades": led.get("trades"),
            "settled": led.get("settled"),
        },
        "by_market_series": by_series,
        "capital": cap,
        "ledger": led,
        "arbitrage": arb,
        "reconciliation": light.get("reconciliation"),
        "execution_stats": light.get("execution_stats"),
        "reject_reasons": light.get("reject_reasons"),
        "calibration": light.get("calibration"),
        "ev_before_after_costs": light.get("ev_before_after_costs"),
        "execution_realistic_edge": light.get("execution_realistic_edge"),
        "pnl_by_bucket": _pnl_buckets(light),
        "selectivity_impact": sg.get("counterfactual"),
        "selectivity_bucket_evidence": (sg.get("bucket_evidence") or {}).get("buckets"),
        "promotion_candidates": light.get("promotion_candidates"),
        "demotion_candidates": light.get("demotion_candidates"),
        "recent_positions": (ledger.get("positions") or [])[:15],
    }

    loops = light.get("loops", {}) or {}
    operation = {
        "engine": {
            "ticks": status.get("ticks"),
            "paper_only": light.get("report_only", True),
            "live_trading_enabled": light.get("live_trading_enabled", False),
            "global_reconciled": light.get("global_reconciled"),
            "sample_sizes": light.get("sample_sizes"),
        },
        "candidate_lifecycle": light.get("candidate_lifecycle"),
        "loops": loops,
        "verifier": light.get("verifier"),
        "research_loop": light.get("research_loop"),
        "lessons": light.get("lessons"),
        "stop_conditions": light.get("stop_conditions"),
        "readiness": light.get("readiness"),
        "learned_selectivity_gate": {
            k: sg.get(k) for k in ("enabled", "decision_rule", "confidence_z", "accepted",
                                    "rejected", "explored", "block_reasons")
        },
        "directional_allowlist": light.get("directional_allowlist"),
        "learning_loop": light.get("learning"),
        "late_window_entry": light.get("late_window_entry"),
        "missing_data_reasons": light.get("missing_data_reasons"),
        "grok_decider_ops": {k: gd.get(k) for k in ("mode", "affects_trading", "decided", "errors",
                                                    "skipped_budget", "avg_latency_s", "abstains",
                                                    "follow_fraction", "circuit_breaker",
                                                    "adaptive_policy_counts", "aggression")},
    }

    edge5 = tv.get("edge_vs_5min_outcome", {}) or {}
    external_signals = {
        "impact_summary": {
            "tv_signal_hit_rate": edge5.get("signal_hit_rate"),
            "tv_aligned_bot_win_rate": edge5.get("aligned_bot_win_rate"),
            "tv_opposed_bot_win_rate": edge5.get("opposed_bot_win_rate"),
            "tv_settled_with_signal": edge5.get("n_settled_with_signal"),
            "tv_verdict": edge5.get("verdict"),
            "grok_direction_accuracy": gd.get("direction_accuracy"),
            "grok_view_accuracy": gd.get("view_accuracy"),
            "grok_view_edge_candidates": gd.get("view_edge_candidates"),
            "cex_lead_any_proven": (light.get("cex_lead_edge") or {}).get("any_proven"),
            "edge_signal_enabled": (light.get("edge_signal") or {}).get("enabled"),
        },
        "tradingview": {
            k: tv.get(k) for k in (
                "tradingview_alerts_received", "tradingview_alerts_valid",
                "tradingview_alerts_rejected", "tradingview_mtf_confirmation",
                "signal_learning", "rsi_trend", "edge_vs_5min_outcome",
                "context_gate", "down_bias_gate", "mtf_gate", "signal_gate", "webhook")
            if tv.get(k) is not None
        },
        "grok_decider": gd,
        "grok_signal_intel": light.get("grok_signal_intel"),
        "cex_lead_edge": light.get("cex_lead_edge"),
        "edge_signal": light.get("edge_signal"),
        "down_stack": light.get("down_stack"),
    }

    return {
        "schema": "btc_pulse_report_sections/1.0",
        "trading_performance": trading_performance,
        "operation": operation,
        "external_signals": external_signals,
    }


def build_full_report_md(light: dict, status: Optional[dict] = None,
                         ledger: Optional[dict] = None) -> str:
    """Render a human-readable report in three sections: Trading Performance, Operation,
    External Signals. Pure (dict -> markdown)."""
    light = light or {}
    status = status or {}
    ledger = ledger or {}
    sec = light.get("sections") or build_report_sections(light, status=status, ledger=ledger)
    scores = light.get("scores")
    if scores is None:
        from engine.pulse.performance_scoring import compute_report_scores
        scores = compute_report_scores(sec, global_reconciled=bool(light.get("global_reconciled")))
    hist = light.get("score_history") or {}
    tp = sec.get("trading_performance", {}) or {}
    op = sec.get("operation", {}) or {}
    ex = sec.get("external_signals", {}) or {}
    out: list = []

    def h(t):
        out.append("\n## " + t + "\n")

    def h3(t):
        out.append("\n### " + t + "\n")

    def kv(d, keys=None):
        d = d or {}
        for k, v in [(k, d.get(k)) for k in (keys or d.keys())]:
            if isinstance(v, (dict, list)):
                out.append("- **%s:** `%s`" % (k, json.dumps(v, default=str)[:600]))
            else:
                out.append("- **%s:** %s" % (k, v))

    def table(rows, header):
        if not rows:
            return
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * len(header)) + "|")
        for r in rows:
            out.append("| " + " | ".join(str(x) for x in r) + " |")

    eng = op.get("engine", {}) or {}
    hl = tp.get("headline", {}) or {}
    cap = tp.get("capital", {}) or {}
    led = tp.get("ledger", {}) or {}
    arb = tp.get("arbitrage", {}) or {}
    ev = tp.get("ev_before_after_costs", {}) or {}
    imp = ex.get("impact_summary", {}) or {}

    out.append("# BTC Pulse — 5m + 15m Performance Report\n")
    out.append("_PAPER ONLY · `global_reconciled=%s` · ticks %s_\n"
               % (eng.get("global_reconciled"), eng.get("ticks")))

    h("Performance Scorecard")
    overall = (scores or {}).get("overall", {}) or {}
    table([
        ["Overall", overall.get("score"), overall.get("grade"), "100%"],
        ["Trading Performance", (scores.get("trading_performance") or {}).get("score"),
         (scores.get("trading_performance") or {}).get("grade"), "50%"],
        ["Operation", (scores.get("operation") or {}).get("score"),
         (scores.get("operation") or {}).get("grade"), "25%"],
        ["External Signals", (scores.get("external_signals") or {}).get("score"),
         (scores.get("external_signals") or {}).get("grade"), "25%"],
    ], ["section", "score", "grade", "weight"])
    entries = (hist.get("entries") or [])[-15:]
    if entries:
        h3("Score history (recent)")
        table([[e.get("utc"), e.get("settled"),
                (e.get("scores") or {}).get("trading_performance"),
                (e.get("scores") or {}).get("operation"),
                (e.get("scores") or {}).get("external_signals"),
                (e.get("scores") or {}).get("overall")]
               for e in entries],
              ["utc", "settled", "trading", "operation", "signals", "overall"])

    h("1. Trading Performance")
    table([
        ["Total on-hand", "$%s" % (hl.get("total_on_hand_usd") or cap.get("total_on_hand_usd"))],
        ["Directional on-hand", "$%s" % cap.get("on_hand_capital_usd")],
        ["Starting capital", "$%s" % hl.get("starting_capital_usd")],
        ["Total return", "%s%%" % (hl.get("total_return_pct") or hl.get("return_pct"))],
        ["Directional PnL", "$%s" % hl.get("directional_realized_pnl_usd")],
        ["Arb PnL (segregated)", "$%s" % hl.get("arb_realized_pnl_usd")],
        ["Total PnL", "$%s" % hl.get("total_realized_pnl_usd")],
        ["Trades / settled", "%s / %s" % (hl.get("trades"), hl.get("settled"))],
        ["Win rate", hl.get("win_rate")],
        ["Win rate up / down", "%s / %s" % (hl.get("win_rate_up"), hl.get("win_rate_down"))],
        ["Profit factor", hl.get("profit_factor")],
        ["Avg win / avg loss", "$%s / $%s" % (led.get("avg_win_usd"), led.get("avg_loss_usd"))],
        ["Max drawdown", "$%s" % led.get("max_drawdown_usd")],
        ["Avg PnL/trade", led.get("avg_pnl_per_trade")],
        ["EV before/after cost", "%s / %s" % (ev.get("avg_ev_before_costs"),
                                              ev.get("avg_ev_after_costs"))],
    ], ["metric", "value"])

    by_series = tp.get("by_market_series") or light.get("by_market_series") or {}
    if by_series:
        h3("Performance by market (concise)")
        table([
            [v.get("series_label"), v.get("settled"), v.get("win_rate"), v.get("profit_factor"),
             "$%s" % v.get("pnl_usd"), v.get("win_rate_up"), v.get("win_rate_down")]
            for v in sorted(by_series.values(), key=lambda r: r.get("series_label") or "")
        ], ["market", "settled", "WR", "PF", "PnL", "UP WR", "DOWN WR"])

    h3("Risk-free arbitrage")
    if arb:
        kv(arb, ["executed", "settled", "open", "realized_profit_usd", "detected_actionable",
                 "segregated_from_directional"])
    else:
        out.append("_no arb activity_")

    h3("Accounting integrity")
    rec = tp.get("reconciliation", {}) or {}
    kv(rec, [k for k in rec if not isinstance(rec[k], (dict, list))])

    h3("Execution gate & calibration")
    es_stats = tp.get("execution_stats", {}) or {}
    out.append("candidates %s · accepted %s · rejects `%s`"
               % (es_stats.get("candidates"), es_stats.get("accepted"), tp.get("reject_reasons")))
    out.append("\ncalibration `%s`" % (tp.get("calibration", {})))

    h3("PnL by bucket")
    pnl_by = tp.get("pnl_by_bucket", {}) or {}
    if pnl_by:
        for k in sorted(pnl_by):
            out.append("**%s:** `%s`" % (k, json.dumps(pnl_by[k], default=str)[:900]))
    else:
        out.append("_no bucket PnL yet_")

    h3("Selectivity impact on performance")
    out.append("counterfactual `%s`" % (tp.get("selectivity_impact", {})))
    be = tp.get("selectivity_bucket_evidence") or []
    if be:
        table([[r.get("dimension"), r.get("bucket"), r.get("n"), r.get("win_rate"),
                r.get("breakeven_win_rate"), r.get("ev_per_trade"), r.get("confidently_losing")]
               for r in be],
              ["dim", "bucket", "n", "WR", "breakeven", "EV/trade", "blocked"])

    h3("Recent positions")
    positions = tp.get("recent_positions") or []
    if positions:
        table([[(p.get("research") or {}).get("series_label", "5m"),
                (p.get("title") or "")[-18:], p.get("side"),
                (p.get("research") or {}).get("entry_mode", "—"),
                p.get("entry_price"), p.get("fair_at_entry"),
                ("up" if p.get("outcome_up") else "down") if p.get("outcome_up") is not None else "—",
                ("✓" if p.get("won") else "✗") if p.get("won") is not None else "—",
                p.get("pnl_usd")] for p in positions],
              ["mkt", "window", "side", "entry_mode", "entry", "fair", "outcome", "won", "pnl"])
    else:
        out.append("_no positions_")

    h("2. Operation")
    h3("Engine health")
    kv(eng, ["ticks", "global_reconciled", "paper_only", "live_trading_enabled", "sample_sizes"])
    kv(op.get("readiness", {}), ["status", "reason", "checks"] if op.get("readiness") else None)

    h3("Candidate lifecycle")
    lc = op.get("candidate_lifecycle", {}) or {}
    out.append("created %s · terminals `%s`" % (lc.get("created"), lc.get("terminals")))
    out.append("\nrejected_by_stage `%s`" % lc.get("rejected_by_stage"))

    h3("Looping engine (sub-loops)")
    loops = (op.get("loops", {}) or {}).get("loops", {})
    if loops:
        rows = []
        for name, info in sorted(loops.items()):
            st = info.get("status") or info.get("last_status") or {}
            rows.append([name, info.get("role", "—"), info.get("trigger", "—"),
                         info.get("interval_s", "—"), info.get("stop_condition", "—"),
                         st.get("enabled", st.get("halted", "—"))])
        table(rows, ["loop", "role", "trigger", "interval_s", "stop", "status"])
    else:
        out.append("_no loop registry_")

    h3("Maker-checker verifier")
    kv(op.get("verifier", {}), ["enabled", "verified", "approvals", "vetoes", "errors",
                                "approve_rate", "avg_latency_s"])

    h3("Research meta-loop")
    rl = op.get("research_loop", {}) or {}
    kv(rl, ["enabled", "calls", "auto_apply", "lessons_added"])
    if rl.get("last_note"):
        out.append("- **summary:** %s" % (rl["last_note"] or {}).get("summary"))

    h3("Compounding lessons")
    les = op.get("lessons", {}) or {}
    out.append("count %s" % les.get("count"))
    for ln in (les.get("recent") or [])[-10:]:
        out.append("- [`%s`] %s" % (ln.get("kind"), ln.get("rule")))

    h3("Internal gates & allowlist")
    kv(op.get("learned_selectivity_gate", {}),
       ["decision_rule", "accepted", "rejected", "explored", "block_reasons"])
    kv(op.get("directional_allowlist", {}), ["enabled", "explore_rate", "explored", "blocked"])
    kv(op.get("learning_loop", {}), ["enabled", "active", "weight", "reason"])
    kv(op.get("stop_conditions", {}), ["enabled", "halted_directional", "halted_arbitrage",
                                       "rolling_profit_factor", "rolling_win_rate"])

    h3("Grok decider (operations)")
    kv(op.get("grok_decider_ops", {}), ["mode", "affects_trading", "decided", "errors",
                                        "avg_latency_s", "abstains", "circuit_breaker"])

    h("3. External Signals")
    h3("Signal impact on trading performance")
    table([
        ["TV aligned bot WR", imp.get("tv_aligned_bot_win_rate")],
        ["TV opposed bot WR", imp.get("tv_opposed_bot_win_rate")],
        ["TV signal hit-rate", imp.get("tv_signal_hit_rate")],
        ["TV settled w/ signal", imp.get("tv_settled_with_signal")],
        ["TV edge verdict", imp.get("tv_verdict")],
        ["Grok direction accuracy", imp.get("grok_direction_accuracy")],
        ["Grok view accuracy", imp.get("grok_view_accuracy")],
        ["CEX-lead proven edge", imp.get("cex_lead_any_proven")],
    ], ["signal", "value"])

    h3("TradingView")
    tv = ex.get("tradingview", {}) or {}
    kv(tv, ["tradingview_alerts_received", "tradingview_alerts_valid",
            "tradingview_alerts_rejected", "tradingview_mtf_confirmation"])
    sl = tv.get("signal_learning", {}) or {}
    out.append("\nsettled_with_signal %s" % sl.get("settled_with_signal"))
    out.append("\nbest_buckets `%s`" % json.dumps(sl.get("best_buckets"), default=str)[:900])
    out.append("\nworst_buckets `%s`" % json.dumps(sl.get("worst_buckets"), default=str)[:900])
    rsi = tv.get("rsi_trend", {}) or {}
    out.append("\nrsi_trend hit_rate %s (n %s)" % (rsi.get("signal_direction_hit_rate"),
                                                  rsi.get("signals_evaluated")))
    for gate in ("context_gate", "down_bias_gate", "mtf_gate", "signal_gate"):
        g = tv.get(gate, {}) or {}
        if g:
            out.append("\n**%s:** enabled=%s blocked=%s explored=%s `%s`"
                       % (gate, g.get("enabled"), g.get("blocked"), g.get("explored"),
                          g.get("block_reasons")))

    h3("Grok Decision Engine (signal quality)")
    gd = ex.get("grok_decider", {}) or {}
    kv(gd, ["mode", "affects_trading", "direction_accuracy", "brier", "view_accuracy",
            "view_brier", "views_graded", "view_edge_candidates"])
    out.append("\naccuracy_by_context `%s`" % json.dumps(gd.get("accuracy_by_context"),
                                                         default=str)[:1200])
    out.append("\nrecent_decisions `%s`" % json.dumps(gd.get("recent_decisions"), default=str)[:900])

    h3("Grok signal intel (analyst + predictor)")
    gi = ex.get("grok_signal_intel", {}) or {}
    out.append("budget `%s`" % gi.get("budget"))
    out.append("\npredictor_B `%s`" % gi.get("predictor_B"))
    aa = gi.get("analyst_A", {}) or {}
    out.append("\nanalyst_A last_note `%s`" % json.dumps(aa.get("last_note"), default=str)[:1200])

    h3("CEX-lead latency edge")
    cl = ex.get("cex_lead_edge", {}) or {}
    if cl.get("enabled"):
        kv(cl, ["mode", "affects_trading", "signals_seen", "graded", "drove_entries",
                "any_proven"])
        rows = cl.get("buckets") or []
        if rows:
            table([[b.get("bucket"), b.get("n"), b.get("accuracy"), b.get("beats_market"),
                    b.get("avg_pnl_per_trade"), b.get("proven")] for b in rows[:6]],
                  ["divergence", "n", "acc", "beats_mkt", "avg_pnl/u", "proven"])
    else:
        out.append("_disabled_")

    h3("Pulse edge signal")
    es = ex.get("edge_signal", {}) or {}
    out.append("`%s`" % json.dumps({k: es.get(k) for k in list(es)[:10]}, default=str)[:800])

    ds = ex.get("down_stack", {}) or {}
    if ds:
        h3("DOWN stack grader")
        out.append("`%s`" % json.dumps(ds, default=str)[:600])

    return "\n".join(out) + "\n"
