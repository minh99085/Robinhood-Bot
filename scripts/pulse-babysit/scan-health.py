#!/usr/bin/env python3
"""Full pulse bot health scan — runtime + loop-arch invariants. Prints JSON."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "http://45.32.224.147"


def _issue(code: str, severity: str, detail: str, hint: str = "") -> dict:
    return {"code": code, "severity": severity, "detail": detail, "hint": hint}


def _fetch(url: str, timeout: float = 15.0) -> dict:
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    issues: list[dict] = []
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    try:
        health = _fetch(f"{base}/api/health")
        status = _fetch(f"{base}/api/polymarket/training/btc_pulse")
        ledger = _fetch(f"{base}/api/polymarket/training/btc_pulse/ledger")
    except Exception as exc:
        out = {
            "verdict": "blocked",
            "healthy": False,
            "issues": [_issue("unreachable", "P0", str(exc), "check VPS / docker")],
            "checks": [],
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(out, indent=2))
        return 2

    record("api_health", health.get("status") == "ok" or health.get("pulse") is not None,
           str(health.get("status") or health.get("pulse") or health))
    record("status_available", status.get("available") is True)

    ts = float(status.get("ts") or 0)
    age = max(0.0, time.time() - ts) if ts else 9999.0
    record("status_fresh", age < 45, f"age_s={age:.1f}")
    record("ticks_positive", int(status.get("ticks") or 0) > 0, f"ticks={status.get('ticks')}")

    gd = status.get("grok_decider") or {}
    if not record("grok_follow", gd.get("mode") == "follow" and gd.get("affects_trading") is True,
                  f"mode={gd.get('mode')} affects={gd.get('affects_trading')}"):
        issues.append(_issue("grok_not_follow", "P0", f"mode={gd.get('mode')}",
                             "set PULSE_GROK_DECIDER_MODE=follow on VPS"))
    if int(gd.get("errors") or 0) >= 10:
        issues.append(_issue("grok_errors", "P1", f"errors={gd.get('errors')}", "check XAI_API_KEY"))

    ver = status.get("verifier") or {}
    if not record("verifier_enabled", ver.get("enabled") is True, f"enabled={ver.get('enabled')}"):
        issues.append(_issue("verifier_disabled", "P0", "verifier.enabled is false",
                             "set ANTHROPIC_API_KEY + PULSE_VERIFIER_ENABLED=1; recreate container"))
    record("verifier_no_errors", int(ver.get("errors") or 0) == 0,
           f"verified={ver.get('verified')} vetoes={ver.get('vetoes')}")

    tv = status.get("tradingview") or {}
    mg = tv.get("mtf_gate") or {}
    sg = tv.get("signal_gate") or {}
    record("tv_webhook", tv.get("enabled") is True,
           f"valid={tv.get('tradingview_alerts_valid')} rejected={tv.get('tradingview_alerts_rejected')}")
    if sg.get("enabled"):
        issues.append(_issue("tv_signal_gate_on", "P1", "signal_gate enabled",
                             "set PULSE_TRADINGVIEW_SIGNAL_GATE=0 for loop-arch"))
    if mg.get("require_confirm"):
        issues.append(_issue("mtf_require_confirm_on", "P1", "require_confirm=true",
                             "set PULSE_TV_MTF_REQUIRE_CONFIRM=0"))
    record("tv_observe_only", not sg.get("enabled") and not mg.get("require_confirm"))

    loops = (status.get("loops") or {}).get("loops") or {}
    for name in ("heartbeat", "data_ingestion", "tradingview", "signal_generation", "verifier", "execution"):
        record(f"loop_{name}", name in loops)

    cfg = status.get("config") or {}
    record("config_follow", cfg.get("grok_decider_mode") == "follow")
    _rr = float(cfg.get("min_reward_risk") or 0)
    record("config_reward_risk", 0.35 <= _rr <= 0.50)

    L = status.get("ledger") or {}
    trades = int(L.get("trades") or 0)
    ticks = int(status.get("ticks") or 0)
    record("ledger_reconciled", (status.get("decision_lifecycle") or {}).get("reconciled") is True)
    eg = status.get("execution_gate") or {}
    record("exec_gate_reconciled", eg.get("reconciled") is True)

    p = status.get("price") or {}
    record("price_feed", p.get("last_fetch_ok") is True and p.get("sampler_running") is True,
           f"age_s={p.get('age_s')}")
    rt = (status.get("oracle") or {}).get("rtds") or {}
    record("rtds_connected", rt.get("connected") is True)

    stop = status.get("stop_conditions") or {}
    if stop.get("any_halted"):
        issues.append(_issue("strategy_halted", "P0", str(stop.get("stalled") or stop),
                             "inspect stop_conditions"))

    lc = status.get("decision_lifecycle") or {}
    rbs = lc.get("rejected_by_stage") or {}
    if int(rbs.get("verifier") or 0) > 100:
        issues.append(_issue("verifier_blocking", "P1", f"verifier_rejects={rbs.get('verifier')}",
                             "check verifier latency / fail_open"))

    sk = lc.get("skipped_by_reason") or {}
    recent = status.get("recent_evaluations") or []
    recent_reasons = [e.get("reason") or e.get("terminal_reason") for e in recent[-15:]]
    if ticks > 30 and recent_reasons and all(
            r in ("open_snapshot_late", "no_open_snapshot", "untrusted_vol") for r in recent_reasons if r):
        issues.append(_issue("window_skip_storm", "P1", f"recent={recent_reasons[:5]}",
                             "raise PULSE_MAX_OPEN_LAG_S or fix price sampler"))

    if ticks > 60 and trades <= 30 and int(rbs.get("directional") or 0) > 5000:
        if gd.get("mode") != "follow":
            issues.append(_issue("trade_freeze", "P1", f"trades={trades} ticks={ticks}",
                                 "enable grok follow + relax gates"))

    metrics = {
        "trades": trades,
        "open_positions": L.get("open_positions"),
        "win_rate": L.get("win_rate"),
        "ticks": ticks,
        "grok_mode": gd.get("mode"),
        "verifier_enabled": ver.get("enabled"),
        "tv_valid": tv.get("tradingview_alerts_valid"),
        "status_age_s": round(age, 1),
        "top_gate_blockers": sorted(rbs.items(), key=lambda x: -x[1])[:5],
    }

    failed = [c for c in checks if not c["ok"]]
    healthy = len(issues) == 0 and len(failed) == 0
    verdict = "healthy" if healthy else ("blocked" if any(i["severity"] == "P0" for i in issues) else "issues")

    out = {
        "verdict": verdict,
        "healthy": healthy,
        "issues": issues,
        "checks": checks,
        "failed_checks": [c["name"] for c in failed],
        "metrics": metrics,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(out, indent=2))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())