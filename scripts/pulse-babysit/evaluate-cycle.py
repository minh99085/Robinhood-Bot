#!/usr/bin/env python3
"""Deterministic pulse health check for the closed loop. Prints JSON to stdout."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LATEST = ROOT / "vps_full_reports" / "latest"
STATUS = LATEST / "btc_pulse_status.json"
LIGHT = LATEST / "btc_pulse_light_report.json"
STATE = Path(__file__).resolve().parent / "state.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _issue(code: str, severity: str, detail: str, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "detail": detail, "hint": hint}


def main() -> int:
    status = _load(STATUS)
    light = _load(LIGHT)
    if not status and not light:
        out = {
            "verdict": "blocked",
            "healthy": False,
            "issues": [_issue("no_report", "P0", f"missing {STATUS}", "run pull-vps-artifacts.ps1")],
            "metrics": {},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(out, indent=2))
        return 2

    data = status if status.get("available") is not False else light
    ledger = data.get("ledger") or {}
    capital = data.get("capital") or {}
    tv = data.get("tradingview") or {}
    config = data.get("config") or {}
    reconciled = bool((light or {}).get("global_reconciled", True))

    trades = int(ledger.get("settled") or ledger.get("trades") or 0)
    wr = ledger.get("win_rate")
    pf = ledger.get("profit_factor")
    wr_up = ledger.get("win_rate_up")
    wr_down = ledger.get("win_rate_down")
    pnl = float(capital.get("realized_pnl_usd") or 0.0)

    mtf = tv.get("mtf_gate") or {}
    ctx = tv.get("context_gate") or {}
    dbg = tv.get("down_bias_gate") or {}
    sg = tv.get("signal_gate") or {}
    learning = data.get("learning") or {}

    issues: list[dict] = []

    gd = data.get("grok_decider") or {}
    ver = data.get("verifier") or {}
    stop = data.get("stop_conditions") or {}
    loops = (data.get("loops") or {}).get("loops") or {}

    if gd.get("mode") != "shadow" or gd.get("affects_trading"):
        issues.append(_issue("grok_not_shadow", "P0",
                             f"mode={gd.get('mode')} affects={gd.get('affects_trading')}",
                             "PULSE_GROK_DECIDER_MODE=shadow (Grok must not trade)"))
    if gd.get("mode") == "follow":
        issues.append(_issue("grok_follow_on", "P0", "Grok follow mode is enabled",
                             "set PULSE_GROK_DECIDER_MODE=shadow"))
    if not ver.get("enabled"):
        issues.append(_issue("verifier_disabled", "P0", "verifier.enabled is false",
                             "ANTHROPIC_API_KEY + PULSE_VERIFIER_ENABLED=1; recreate container"))
    if float(gd.get("explore_rate") or 0) > 0:
        issues.append(_issue("grok_explore_on", "P0",
                             f"explore_rate={gd.get('explore_rate')}",
                             "PULSE_GROK_DECIDER_EXPLORE_RATE=0 (coin-flip abstain trades lose)"))
    if float(gd.get("min_confidence") or 0) < 0.62:
        issues.append(_issue("grok_min_conf_low", "P1",
                             f"min_confidence={gd.get('min_confidence')}",
                             "PULSE_GROK_DECIDER_MIN_CONFIDENCE=0.62"))
    if (gd.get("mode") == "follow" and float(gd.get("direction_accuracy") or 1) < 0.52
            and int(gd.get("graded_directional") or 0) >= 20):
        issues.append(_issue("grok_no_edge", "P1",
                             f"direction_accuracy={gd.get('direction_accuracy')}",
                             "Grok at coin-flip — keep shadow or block weak UP"))
    if stop.get("any_halted"):
        dir_h = (stop.get("strategies") or {}).get("directional") or {}
        issues.append(_issue("strategy_halted", "P0",
                             f"directional halted reasons={dir_h.get('reasons')}",
                             "inspect stop_conditions or raise PULSE_STOP_MIN_SAMPLES"))
    for loop in ("tradingview", "signal_generation", "verifier", "execution"):
        if loop not in loops:
            issues.append(_issue("loop_missing", "P1", f"missing loop {loop}",
                                 "redeploy engine 202d40c+ and restart"))
    if sg.get("enabled"):
        issues.append(_issue("tv_signal_gate_on", "P1", "TV signal gate enabled",
                             "PULSE_TRADINGVIEW_SIGNAL_GATE=0"))
    if mtf.get("require_confirm"):
        issues.append(_issue("mtf_require_confirm_on", "P1", "MTF require_confirm on",
                             "PULSE_TV_MTF_REQUIRE_CONFIRM=0"))

    if not reconciled:
        issues.append(_issue("reconciliation_broken", "P0", "global_reconciled is false",
                             "fix accounting before tuning gates"))

    if trades >= 10:
        if wr is not None and float(wr) < 0.55:
            issues.append(_issue("win_rate_low", "P1", f"win_rate={wr}",
                                 "tighten gates / high-WR profile / block weak UP"))
        if pf is not None and float(pf) < 1.0:
            issues.append(_issue("profit_factor_low", "P1", f"profit_factor={pf}",
                                 "raise min_reward_risk, context_gate, late-window"))
        if wr_up is not None and wr_down is not None:
            if float(wr_up) < 0.52 and float(wr_down) >= 0.60:
                issues.append(_issue("up_side_bleed", "P1",
                                     f"win_rate_up={wr_up} win_rate_down={wr_down}",
                                     "down_bias_gate, TV STRONG-only, block bullish_aligned UP"))

    tv_valid = int(tv.get("tradingview_alerts_valid") or 0)
    if tv.get("enabled") and tv_valid < 5:
        issues.append(_issue("tv_feed_unhealthy", "P2", f"valid_alerts={tv_valid}",
                             "check TradingView webhooks and secret"))

    if (mtf.get("enabled") and mtf.get("require_confirm")
            and int(mtf.get("passed") or 0) == 0 and int(mtf.get("blocked") or 0) >= 20):
        mtf_c = (tv.get("tradingview_mtf_confirmation") or {}).get("confirm")
        issues.append(_issue("mtf_starved", "P2",
                             f"mtf_passed=0 blocked={mtf.get('blocked')} confirm={mtf_c}",
                             "require_confirm is on — ensure 1m+5m+10m+15m INDEX:BTCUSD alerts active "
                             "or disable PULSE_TV_MTF_REQUIRE_CONFIRM for loop-arch"))

    bench = learning.get("market_benchmark") or {}
    if learning.get("active") and bench.get("model_beats_market") is False:
        issues.append(_issue("learning_hurts", "P2",
                             f"model_brier={bench.get('model_brier')} market={bench.get('market_brier')}",
                             "veto learning blend when model_not_beating_market"))

    sev_order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: sev_order.get(x["severity"], 9))

    healthy = len(issues) == 0
    verdict = "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues")

    metrics = {
        "settled": trades,
        "win_rate": wr,
        "profit_factor": pf,
        "win_rate_up": wr_up,
        "win_rate_down": wr_down,
        "realized_pnl_usd": round(pnl, 2),
        "tv_valid": tv_valid,
        "mtf_passed": mtf.get("passed"),
        "mtf_blocked": mtf.get("blocked"),
        "context_blocked": ctx.get("blocked"),
        "down_bias_blocked": dbg.get("blocked"),
        "signal_gate": sg.get("enabled"),
        "min_reward_risk": config.get("min_reward_risk"),
        "global_reconciled": reconciled,
    }

    out = {
        "verdict": verdict,
        "healthy": healthy,
        "issues": issues,
        "metrics": metrics,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))

    # append to state history
    try:
        st = _load(STATE)
        st["last_eval_at"] = out["ts"]
        st["last_verdict"] = verdict
        hist = st.setdefault("history", [])
        hist.append({"ts": out["ts"], "verdict": verdict, "metrics": metrics,
                     "issue_codes": [i["code"] for i in issues]})
        st["history"] = hist[-100:]
        STATE.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())