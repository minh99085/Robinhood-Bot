#!/usr/bin/env python3
"""Print a simple Polymarket PAPER training status: scan counts, open paper
positions, PnL, risk status, and safety locks. Read-only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for inspection_metrics


def _print_benchmarks(st: dict) -> None:
    """Print the algorithmic benchmark scorecard + consistency checks (read-only).

    Reuses the inspection benchmark layer so the CLI and the report agree. Best
    effort: if the inspection module isn't importable, this is skipped silently.
    """
    try:
        import inspection_metrics as m
    except Exception:  # noqa: BLE001
        return
    feats = m.extract_features(st, api={}, tests={}, env={})
    bench = m.build_benchmarks(feats)
    incons = m.detect_inconsistencies(feats, st, {})
    s = bench.get("summary", {})
    print("=" * 56)
    print(f"  BENCHMARKS (quant): pass={s.get('pass', 0)} warn={s.get('warn', 0)} "
          f"fail={s.get('fail', 0)} missing={s.get('missing', 0)}")
    for b in bench.get("benchmarks", []):
        mark = {"pass": "OK ", "warn": "WARN", "fail": "FAIL", "missing": "-- "}.get(
            b["status"], "?")
        print(f"    {mark} {b['name']}={b['value']} "
              f"(target {b['direction']} {b['target']})")
    if incons:
        print("  CONSISTENCY:")
        for c in incons:
            print(f"    [{c.get('severity')}] {c.get('check')}: {c.get('detail')}")
    else:
        print("  CONSISTENCY: OK (dashboard/paper equity, live flags, cost accounting)")


def _print_edge_audit(st: dict) -> None:
    """Print the mandatory Algorithmic Edge Audit summary (read-only).

    Reuses the inspection audit builder so the CLI and the generated report agree.
    Fails loudly (prints FAIL + missing core fields) when not decision-grade.
    """
    try:
        import inspection_metrics as m
    except Exception:  # noqa: BLE001
        return
    feats = m.extract_features(st, api={}, tests={}, env={})
    audit = m.build_algorithmic_edge_audit(feats, st)
    print("=" * 56)
    banner = "PASS" if audit.get("ok") else "FAIL (not decision-grade)"
    print(f"  ALGORITHMIC EDGE AUDIT: {banner}")
    if not audit.get("ok"):
        miss = audit.get("missing_core_fields") or []
        if miss:
            print(f"    missing core fields: {', '.join(miss)}")
        if audit.get("stale"):
            print("    status is STALE")
    for b in (audit.get("top_5_blockers") or [])[:5]:
        print(f"    blocker: {b}")


def _print_execution_monitoring(st: dict) -> None:
    """Print execution + final-validation monitoring fields (read-only).

    Surfaces Bregman opportunity decay, rejected bad fills, latency, stale-data,
    calibration rollbacks, kill-switch reasons, and after-cost PnL — the signals
    needed to trust paper execution. Best-effort; missing fields show as '-'.
    """
    pnl = st.get("pnl", {}) or {}
    mon = st.get("monitoring", {}) or {}
    breg = st.get("bregman", {}) or {}
    cal = st.get("calibration", {}) or {}
    bp = st.get("btc_pulse", {}) or {}

    def _f(*vals, default="-"):
        for v in vals:
            if v is not None:
                return v
        return default

    print("=" * 56)
    print("  EXECUTION MONITORING (paper):")
    print(f"    after_cost_pnl       : {_f(pnl.get('after_cost_pnl'), pnl.get('after_cost'), bp.get('btc_pulse_after_cost_pnl'))}")
    print(f"    bregman_opp_decay    : {_f(breg.get('opportunity_decay'), mon.get('bregman_opportunity_decay'))}")
    print(f"    rejected_bad_fills   : {_f(pnl.get('fantasy_fill_rejections'), mon.get('fantasy_fill_rejections'))}")
    print(f"    latency_ms           : {_f(mon.get('latency_ms'), breg.get('latency_ms'))}")
    print(f"    stale_data_events    : {_f(mon.get('stale_data_events'))}")
    print(f"    calibration_rollbacks: {_f(cal.get('rollbacks'), mon.get('calibration_rollbacks'))}")
    print(f"    kill_switch_reasons  : {_f(mon.get('kill_switch_reasons'), st.get('kill_switch_reasons'), default=[])}")


def _data_dir() -> Path:
    try:
        from engine.config import Settings
        return Path(Settings().data_dir)
    except Exception:  # noqa: BLE001
        import os
        return Path(os.getenv("HTE_DATA_DIR") or ".")


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print Polymarket PAPER training status (read-only).")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--json", action="store_true", help="print raw JSON status")
    ap.add_argument("--campaign-json", action="store_true",
                    help="print ONLY the institutional paper-training campaign JSON")
    args = ap.parse_args(argv)

    dd = Path(args.data_dir) if args.data_dir else _data_dir()
    path = dd / "polymarket_training.json"
    if args.campaign_json:
        camp_path = dd / "polymarket_training_campaign.json"
        if not camp_path.exists():
            print("{}")
            return 0
        data = json.loads(camp_path.read_text(encoding="utf-8"))
        print(json.dumps(data.get("report", data), indent=2, default=str))
        return 0
    if not path.exists():
        print(f"no training status at {path} — start training first.")
        return 0
    st = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(st, indent=2, default=str))
        return 0

    pnl = st.get("pnl", {})
    scan = st.get("scan_metrics", {})
    risk = st.get("risk", {})
    safety = st.get("safety", {})
    print("=" * 56)
    print(f"Polymarket PAPER Training — {st.get('run_id')}")
    print(f"  mode: {st.get('mode')} (PAPER) · tick: {st.get('tick')} · "
          f"runtime: {st.get('runtime_seconds')}s")
    print(f"  scanned: {scan.get('scanned')} kept: {scan.get('kept')} "
          f"subscribed_assets: {scan.get('subscribed_assets')} "
          f"scan_ms: {scan.get('scan_latency_ms')}")
    print(f"  open positions: {pnl.get('open_positions')} · closed: {pnl.get('trades_closed')} "
          f"· win_rate: {pnl.get('win_rate')}")
    print(f"  equity: {pnl.get('equity')} (start {pnl.get('starting_bankroll')}) · "
          f"total PnL: {pnl.get('total_pnl')}")
    print(f"  risk: approvals={risk.get('approvals')} rejections={risk.get('rejections')}")
    print(f"  safety: preflight_ok={safety.get('ok')} live_detected={safety.get('live_detected')}")
    print(f"  arbitrage_disabled: {safety.get('checks', {}).get('arbitrage_disabled')}")
    mon = st.get("monitoring", {}) or {}
    ks = st.get("kill_switch", {}) or {}
    if mon:
        print(f"  profile: {st.get('profile', mon.get('profile'))} · kill_switch: "
              f"{ks.get('severity', 'OK')}"
              + (f" (downgraded: {', '.join(ks.get('triggered', []))})"
                 if st.get("downgraded") else ""))
        print(f"  learning: trades/hr={mon.get('paper_trades_per_hour')} "
              f"feedback/hr={mon.get('useful_feedback_per_hour')} "
              f"labels/day={mon.get('labels_resolved_per_day')}")
        print(f"  quality: calib_improvement={mon.get('calibration_improvement')} "
              f"brier_trend={mon.get('brier_trend')} ece_trend={mon.get('ece_trend')} "
              f"loss_streak={mon.get('loss_streak')}")
        print(f"  bregman: opps={mon.get('bregman_opportunities')} "
              f"certified_profit={mon.get('certified_bregman_profit')} "
              f"fp_rate={mon.get('bregman_false_positive_rate')}")
    camp = st.get("training_campaign") or {}
    if camp and camp.get("enabled") is not False:
        ev = camp.get("evidence", {}) or {}
        th = camp.get("thresholds", {}) or {}
        prog = (camp.get("progress", {}) or {})
        print("=" * 56)
        print(f"  CAMPAIGN: {camp.get('campaign_name')} · freeze="
              f"{camp.get('algorithm_freeze_mode')} · verdict={camp.get('state')} · "
              f"no_live_orders={camp.get('no_live_orders')}")
        print(f"  elapsed: {ev.get('elapsed_days')}d / {ev.get('runtime_hours')}h · "
              f"overall progress: {prog.get('overall_pct')}%")
        print(f"  decisions: {ev.get('decisions')}/{th.get('target_min_decisions')} · "
              f"trades: {ev.get('paper_trades')}/{th.get('target_min_paper_trades')} · "
              f"resolved: {ev.get('resolved_labels')}/{th.get('target_min_resolved_labels')} · "
              f"clean: {ev.get('clean_labels')}/{th.get('target_min_clean_labels')}")
        print(f"  bregman candidates: {ev.get('bregman_candidates')}/"
              f"{th.get('target_min_bregman_candidates')} · "
              f"certified: {ev.get('bregman_certified')} · "
              f"false_positives: {ev.get('bregman_false_positives')}")
        print(f"  after-cost: {ev.get('after_cost_expectancy')} · realistic-fill: "
              f"{ev.get('realistic_fill_expectancy')}")
        print(f"  next required evidence: {camp.get('next_target')}")
        print(f"  blockers: {', '.join(camp.get('blockers', [])) or 'none'}")
    sp = st.get("campaign_safety") or (camp.get("safety_profile") if camp else None) or {}
    if sp:
        print("=" * 56)
        print(f"  CAMPAIGN-SAFE PROFILE: {sp.get('campaign_safe_profile')} · "
              f"startup_safety_passed: {sp.get('startup_safety_passed')}")
        print(f"  clob_read_only: {sp.get('clob_read_only_enabled')} · "
              f"chainlink_read_only: {sp.get('chainlink_read_only_enabled')} · "
              f"realistic_fill: {sp.get('realistic_fill_enabled')} · "
              f"clean_label_guard: {sp.get('clean_label_guard_enabled')}")
        print(f"  live_disabled: {sp.get('live_disabled')} · "
              f"micro_live_disabled: {sp.get('micro_live_disabled')} · "
              f"guarded_live_disabled: {sp.get('guarded_live_disabled')} · "
              f"btc_autotrade_disabled: {sp.get('btc_autotrade_disabled')} · "
              f"risk_gates_required: {sp.get('risk_gates_required')}")
        if "news_scanner_enabled" in sp:
            print(f"  news_scanner: {sp.get('news_scanner_enabled')} · "
                  f"provider_mode: {sp.get('news_provider_mode')} · "
                  f"read_only: {sp.get('news_read_only')} · "
                  f"replay_safe: {sp.get('news_replay_timestamp_safe')} · "
                  f"cannot_trigger_live_orders: {sp.get('news_cannot_trigger_live_orders')}")
        if sp.get("fail_closed_reason"):
            print(f"  fail_closed_reason: {sp.get('fail_closed_reason')}")
    bp = st.get("btc_pulse") or {}
    if bp:
        print("=" * 56)
        print(f"  BTC 5-min PULSE (PAPER, isolated): enabled={bp.get('btc_pulse_enabled')} "
              f"frozen={bp.get('btc_pulse_frozen')}")
        print(f"    paper_only={bp.get('paper_only')} isolated_learning={bp.get('isolated_learning')} "
              f"live_enabled={bp.get('live_enabled')} legacy_autotrade={bp.get('legacy_autotrade_enabled')}")
        print(f"    ticks={bp.get('btc_pulse_ticks')} rounds={bp.get('btc_pulse_rounds_seen')} "
              f"decisions={bp.get('btc_pulse_decisions')} "
              f"paper_trades={bp.get('btc_pulse_paper_trades')} "
              f"(opened={bp.get('btc_pulse_paper_trades')} "
              f"resolved={bp.get('btc_pulse_resolved_trades')} "
              f"open={bp.get('btc_pulse_open_trades')}) "
              f"no_trades={bp.get('btc_pulse_no_trade_decisions')} "
              f"rejected={bp.get('btc_pulse_rejected_trades')}")
        print(f"    win_rate={bp.get('btc_pulse_win_rate')} sharpe={bp.get('btc_pulse_sharpe')} "
              f"brier={bp.get('btc_pulse_brier')} after_cost_pnl={bp.get('btc_pulse_after_cost_pnl')} "
              f"max_dd={bp.get('btc_pulse_max_drawdown')}")
        print(f"    ev_positive={bp.get('btc_pulse_ev_positive_count')} "
              f"ev_negative_rejected={bp.get('btc_pulse_ev_negative_rejected_count')} "
              f"rejection_reasons={bp.get('btc_pulse_rejection_reasons')}")
        print(f"    last_tick_ts={bp.get('btc_pulse_last_tick_ts')} "
              f"last_error={bp.get('btc_pulse_last_error')} "
              f"blockers={bp.get('btc_pulse_blockers')}")
    nw = st.get("news") or {}
    if nw:
        print("=" * 56)
        print(f"  NEWS SCANNER (PAPER, advisory): enabled={nw.get('news_scanner_enabled')} "
              f"· provider={nw.get('news_provider_mode')}")
        print(f"    markets_scanned={nw.get('news_markets_scanned')} "
              f"queries={nw.get('news_queries')} items_fetched={nw.get('news_items_fetched')} "
              f"items_used={nw.get('news_items_used')} rejected={nw.get('news_items_rejected')}")
        print(f"    items_used/hr={nw.get('news_items_used_per_hour')} "
              f"provider_errors={nw.get('news_provider_errors')} "
              f"last_error={nw.get('news_last_error')}")
        for it in (nw.get("news_last_packet_sample") or []):
            print(f"    • {str(it.get('title'))[:70]} ({it.get('source_name')}, {it.get('direction')})")
    fa = st.get("feedback_accelerator") or {}
    if fa:
        print("=" * 56)
        print(f"  FEEDBACK ACCELERATOR (PAPER ONLY): enabled={fa.get('feedback_accelerator_enabled')} "
              f"· target x{fa.get('target_multiplier')} · mode={fa.get('mode')}")
        cap = fa.get("capacity", {})
        print(f"    capacity: decisions/tick={cap.get('paper_decision_budget')} "
              f"candidates={cap.get('trade_candidate_limit')} shortlist={cap.get('shortlist_limit')}")
        print(f"    exploration={fa.get('exploration_enabled')} tiny={fa.get('exploration_tiny_size_enabled')} "
              f"counts_for_readiness={fa.get('exploration_counts_for_readiness')} "
              f"shadow={fa.get('shadow_decision_logging_enabled')} "
              f"no_trade_labels={fa.get('no_trade_labeling_enabled')}")
        sg = fa.get("soft_gates", {})
        print(f"    exploit edge>={sg.get('exploit_min_edge')} conf>={sg.get('exploit_min_confidence')} "
              f"| exploration edge>={sg.get('exploration_min_edge')} conf>={sg.get('exploration_min_confidence')}")
        hl = fa.get("hard_gates_locked", {})
        print(f"    hard gates locked: bypass={hl.get('exploration_can_bypass_hard_gate')} "
              f"risk_required={hl.get('exploration_requires_risk_gate')} "
              f"fill_required={hl.get('exploration_requires_realistic_fill')} "
              f"fresh_book_required={hl.get('exploration_min_book_freshness_required')}")
    _print_benchmarks(st)
    _print_execution_monitoring(st)
    _print_edge_audit(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
