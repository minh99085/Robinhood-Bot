#!/usr/bin/env python3
"""Apply loop-engine architecture env on VPS: Grok→verifier→execution owns trades; TV observe-only."""
from pathlib import Path

ENV_PATH = Path("/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine/.env")

UPDATES = {
    # Loop arch: Grok drives direction (not shadow baseline + TV gates).
    "PULSE_GROK_DECIDER_MODE": "follow",
    "PULSE_GROK_DECIDER_FOLLOW_FRACTION": "1.0",
    "PULSE_GROK_DECIDER_EXPLORE_RATE": "0",
    "PULSE_GROK_DECIDER_MIN_CONFIDENCE": "0.62",
    "PULSE_GROK_DECIDER_EXPLORE_MIN_VIEW_MARGIN": "0.08",
    "PULSE_VERIFIER_ENABLED": "1",
    "PULSE_VERIFIER_FAIL_OPEN": "0",
    "PULSE_VERIFIER_FOLLOW_REQUIRE_VERDICT": "1",
    # TV observe-only — conflict veto only, not trade authority.
    "PULSE_TRADINGVIEW_SIGNAL_GATE": "0",
    "PULSE_TV_MIN_SIGNAL_STRENGTH": "0",
    "PULSE_TV_MTF_REQUIRE_CONFIRM": "0",
    "PULSE_TV_MTF_REQUIRE_SIDE_ALIGN": "0",
    "PULSE_TV_DOWN_BIAS_GATE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_AGAINST_CONFIRMED_DOWN": "1",
    "PULSE_LATE_WINDOW_ENTRY": "0",
    # Unfreeze baseline-path / allowlist cold-start (Grok follow bypasses most of these).
    "PULSE_TV_CONTEXT_MAX_TTC_S": "240",
    "PULSE_TV_CONTEXT_EXPLORATION_RATE": "0.05",
    "PULSE_TV_DOWN_BIAS_EXPLORE_RATE": "0.05",
    "PULSE_DIRECTIONAL_EXPLORE_RATE": "0",
    "PULSE_MIN_REWARD_RISK": "0.35",
    # Gamma windows often appear >20s after open_ts; min_seconds_since_open=30 already delays entry.
    "PULSE_MAX_OPEN_LAG_S": "45",
    # Stop halt needs >30 settled before Wilson test (avoids freeze at exactly min_samples).
    "PULSE_STOP_MIN_SAMPLES": "40",
}

text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
lines = [ln for ln in text.splitlines() if not ln.strip().startswith("# LOOP ENGINE ARCH")]
seen = set()
out = []
remaining = dict(UPDATES)
for ln in lines:
    if "=" in ln and not ln.lstrip().startswith("#"):
        key = ln.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
            seen.add(key)
        elif key not in seen:
            out.append(ln)
            seen.add(key)
    elif ln.strip():
        out.append(ln)
for key, val in remaining.items():
    out.append(f"{key}={val}")
out.append("# LOOP ENGINE ARCH (2026-06-25): Grok follow + TV observe-only context")
ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"Wrote {ENV_PATH} ({len(UPDATES)} loop-arch keys)")