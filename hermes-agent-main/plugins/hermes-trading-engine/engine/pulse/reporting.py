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
        "schema": "btc_pulse_light_report/1.1", "report_only": True, "live_trading_enabled": False,
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


def build_full_report_md(light: dict, status: Optional[dict] = None,
                         ledger: Optional[dict] = None) -> str:
    """Render a COMPLETE human-readable performance report from the bot's own JSON artifacts so an
    external reviewer (ChatGPT / Grok) can inspect everything: capital, full P&L, reconciliation,
    candidate lifecycle, execution gate, calibration, PnL by EVERY bucket, the learned selectivity
    gate (+ bucket evidence + counterfactual), entry gates, the Grok Decision Engine (view accuracy,
    per-context accuracy, edge candidates, aggression, policy, breaker, news), Grok intel,
    TradingView learning, edge signal, readiness, and recent positions. Pure (dict -> markdown)."""
    light = light or {}
    status = status or {}
    ledger = ledger or {}
    out: list = []

    def h(t):
        out.append("\n## " + t + "\n")

    def kv(d, keys=None):
        d = d or {}
        items = [(k, d.get(k)) for k in (keys or d.keys())]
        for k, v in items:
            if isinstance(v, (dict, list)):
                out.append("- **%s:** `%s`" % (k, json.dumps(v, default=str)[:600]))
            else:
                out.append("- **%s:** %s" % (k, v))

    def table(rows, header):
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * len(header)) + "|")
        for r in rows:
            out.append("| " + " | ".join(str(x) for x in r) + " |")

    led = light.get("ledger", {}) or {}
    cap = light.get("capital", status.get("capital", {})) or {}
    rec = light.get("reconciliation", {}) or {}
    lc = light.get("candidate_lifecycle", {}) or {}
    gd = light.get("grok_decider", {}) or {}
    sg = light.get("learned_selectivity_gate", {}) or {}
    lw = light.get("late_window_entry", {}) or {}
    gi = light.get("grok_signal_intel", {}) or {}
    tv = light.get("tradingview", {}) or {}
    es = light.get("edge_signal", {}) or {}
    ev = light.get("ev_before_after_costs", {}) or {}

    out.append("# BTC 5-Minute Pulse — FULL Performance Report\n")
    out.append("_PAPER ONLY. `live_trading_enabled=%s` · `global_reconciled=%s` · ticks %s._\n"
               % (light.get("live_trading_enabled"), light.get("global_reconciled"),
                  status.get("ticks")))

    h("1. Capital & P&L")
    table([["On-hand capital", "$%s" % cap.get("on_hand_capital_usd")],
           ["Starting capital", "$%s" % cap.get("starting_capital_usd")],
           ["Return", "%s%%" % cap.get("return_pct")],
           ["Open exposure", "$%s (%s pos)" % (cap.get("open_exposure_usd"),
                                               cap.get("open_positions"))],
           ["Trades / settled", "%s / %s" % (led.get("trades"), led.get("settled"))],
           ["Win rate", led.get("win_rate")],
           ["Realized PnL", "$%s" % led.get("realized_pnl_usd")],
           ["Profit factor", led.get("profit_factor")],
           ["Avg win / avg loss", "%s / %s" % (led.get("avg_win"), led.get("avg_loss"))],
           ["Max drawdown", led.get("max_drawdown")],
           ["Avg PnL/trade", led.get("avg_pnl_per_trade")],
           ["EV before/after cost", "%s / %s" % (ev.get("avg_ev_before_costs"),
                                                 ev.get("avg_ev_after_costs"))]],
          ["metric", "value"])

    h("2. Accounting integrity (reconciliation)")
    kv(rec, [k for k in rec if not isinstance(rec[k], (dict, list))])

    h("3. Candidate lifecycle")
    out.append("created %s · terminals `%s`" % (lc.get("created"), lc.get("terminals")))
    out.append("\nrejected_by_stage `%s`" % lc.get("rejected_by_stage"))

    h("4. Execution gate & calibration")
    es_stats = light.get("execution_stats", {}) or {}
    out.append("candidates %s · accepted %s · rejects `%s`"
               % (es_stats.get("candidates"), es_stats.get("accepted"), light.get("reject_reasons")))
    out.append("\ncalibration `%s`" % (light.get("calibration", {})))

    h("5. PnL by bucket (all dimensions)")
    for k in sorted(k for k in light if k.startswith("pnl_by_")):
        out.append("**%s:** `%s`" % (k, json.dumps(light.get(k), default=str)[:900]))

    h("6. Learned selectivity gate")
    kv(sg, ["decision_rule", "confidence_z", "accepted", "rejected", "explored"])
    be = (sg.get("bucket_evidence", {}) or {}).get("buckets", [])
    if be:
        table([[r.get("dimension"), r.get("bucket"), r.get("n"), r.get("win_rate"),
                r.get("breakeven_win_rate"), r.get("win_rate_upper_ci"), r.get("ev_per_trade"),
                r.get("confidently_losing")] for r in be],
              ["dim", "bucket", "n", "WR", "breakeven", "WR_upperCI", "EV/trade", "blocked"])
    out.append("\ncounterfactual `%s`" % (sg.get("counterfactual", {})))

    h("7. Entry gates (context / late-window / reward-risk)")
    cgx = tv.get("context_gate", {}) or {}
    out.append("context_gate enabled=%s · blocked %s · `%s`"
               % (cgx.get("enabled"), cgx.get("blocked"), cgx.get("block_reasons")))
    out.append("\nlate_window gate=%s · verdict %s · LHC `%s` · other `%s`"
               % ((lw.get("gate", {}) or {}).get("enabled"),
                  (lw.get("edge_measurement", {}) or {}).get("verdict"),
                  (lw.get("edge_measurement", {}) or {}).get("cohort_late_high_conviction"),
                  (lw.get("edge_measurement", {}) or {}).get("cohort_other")))

    h("8. Grok Decision Engine (decides; bot executes)")
    kv(gd, ["mode", "affects_trading", "decided", "errors", "skipped_budget", "avg_latency_s",
            "graded_directional", "direction_accuracy", "brier", "views_graded", "view_accuracy",
            "view_brier", "abstains", "follow_fraction", "explore_rate", "adaptive_enabled"])
    out.append("\nby_action `%s`" % gd.get("by_action"))
    out.append("\nadaptive_policy_counts `%s`" % gd.get("adaptive_policy_counts"))
    out.append("\naggression `%s`" % gd.get("aggression"))
    out.append("\naccuracy_by_context `%s`" % json.dumps(gd.get("accuracy_by_context"),
                                                         default=str)[:1200])
    out.append("\nview_edge_candidates `%s`" % gd.get("view_edge_candidates"))
    out.append("\ncircuit_breaker `%s`" % gd.get("circuit_breaker"))
    out.append("\nnews_digest `%s`" % json.dumps(gd.get("news_digest"), default=str)[:700])
    out.append("\nrecent_decisions `%s`" % json.dumps(gd.get("recent_decisions"), default=str)[:900])

    h("9. Grok signal intel (analyst + predictor + budget)")
    out.append("budget `%s`" % gi.get("budget"))
    out.append("\npredictor_B `%s`" % gi.get("predictor_B"))
    aa = gi.get("analyst_A", {}) or {}
    out.append("\nanalyst_A last_note `%s`" % json.dumps(aa.get("last_note"), default=str)[:1200])

    h("10. TradingView learning")
    kv(tv, ["tradingview_alerts_received", "tradingview_alerts_valid", "tradingview_alerts_rejected"])
    sl = tv.get("signal_learning", {}) or {}
    out.append("\nsettled_with_signal %s" % sl.get("settled_with_signal"))
    out.append("\nbest_buckets `%s`" % json.dumps(sl.get("best_buckets"), default=str)[:900])
    out.append("\nworst_buckets `%s`" % json.dumps(sl.get("worst_buckets"), default=str)[:900])
    rsi = tv.get("rsi_trend", {}) or {}
    out.append("\nrsi_trend hit_rate %s (n %s) · pred_acc %s"
               % (rsi.get("signal_direction_hit_rate"), rsi.get("signals_evaluated"),
                  rsi.get("prediction_accuracy")))

    h("11. Edge signal & readiness")
    out.append("edge_signal `%s`" % json.dumps({k: es.get(k) for k in list(es)[:8]},
                                               default=str)[:700])
    out.append("\nreadiness `%s`" % (light.get("readiness", {})))

    h("12. Recent paper positions")
    positions = (ledger.get("positions") or [])[:15]
    if positions:
        table([[(p.get("title") or "")[-18:], p.get("side"),
                (p.get("research") or {}).get("entry_mode", "—"),
                p.get("entry_price"), p.get("fair_at_entry"),
                ("up" if p.get("outcome_up") else "down") if p.get("outcome_up") is not None else "—",
                ("✓" if p.get("won") else "✗") if p.get("won") is not None else "—",
                p.get("pnl_usd")] for p in positions],
              ["window", "side", "entry_mode", "entry", "fair", "outcome", "won", "pnl"])
    else:
        out.append("_no positions_")
    return "\n".join(out) + "\n"
