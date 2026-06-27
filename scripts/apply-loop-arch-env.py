#!/usr/bin/env python3
"""Apply loop-engine architecture env on VPS: quant baseline owns trades; Grok/TV observe-only."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "hermes-agent-main" / "plugins" / "hermes-trading-engine"
sys.path.insert(0, str(ENGINE_ROOT))

from engine.pulse.config_coupling import (  # noqa: E402
    evaluate_context_cohort_coupling,
    window_seconds_for_slugs,
)

ENV_PATH = Path("/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine/.env")
if not ENV_PATH.exists():
    ENV_PATH = ENGINE_ROOT / ".env"

UPDATES = {
    # Grok observe-only: decide + grade every window, never place/size a trade.
    "PULSE_GROK_DECIDER_MODE": "shadow",
    "PULSE_GROK_DECIDER_FOLLOW_FRACTION": "0",
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
    # UP restrictor floors: block proven-losing UP contexts.
    "PULSE_TV_DOWN_BIAS_GATE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_AGAINST_CONFIRMED_DOWN": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_TOP": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_MARKOV_CHOP_NOISE": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_LATE_TTC": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_EARLY_TTC": "1",
    "PULSE_TV_DOWN_BIAS_UP_LATE_TTC_MIN_S": "240",
    "PULSE_TV_DOWN_BIAS_UP_EARLY_TTC_MAX_S": "120",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_NEUTRAL": "1",
    "PULSE_TV_DOWN_BIAS_BLOCK_UP_LOW_CONVICTION": "1",
    "PULSE_TV_DOWN_BIAS_UP_MIN_CONVICTION": "0.40",
    "PULSE_LATE_WINDOW_ENTRY": "0",
    # Unfreeze baseline-path / allowlist cold-start (Grok follow bypasses most of these).
    # Must exceed scaled cohort max (15m: 240*3=720). Coupling auto-clamps if too low.
    "PULSE_TV_CONTEXT_MAX_TTC_S": "900",
    "PULSE_TV_CONTEXT_EXPLORATION_RATE": "0",
    "PULSE_TV_DOWN_BIAS_EXPLORE_RATE": "0",
    # Baseline quant path: allowlist was deadlocking (no proven bucket + 0% explore).
    "PULSE_DIRECTIONAL_REQUIRE_WINNING": "0",
    "PULSE_DIRECTIONAL_EXPLORE_RATE": "0.12",
    "PULSE_MIN_EDGE": "0.02",
    "PULSE_MIN_REWARD_RISK": "0.45",
    "PULSE_MIN_REWARD_RISK_UP_PREMIUM": "0.28",
    "PULSE_GROK_UP_MIN_P_WIN": "0.58",
    # Gamma windows often appear >20s after open_ts; min_seconds_since_open=30 already delays entry.
    "PULSE_MAX_OPEN_LAG_S": "120",
    # Stop halt needs >30 settled before Wilson test (avoids freeze at exactly min_samples).
    "PULSE_STOP_MIN_SAMPLES": "40",
    # Mispricing stack (quant path only; Grok abstain follow disabled).
    "PULSE_MISPRICING_GATE_ENABLED": "1",
    "PULSE_MISPRICING_TTC_MIN_S": "90",
    "PULSE_MISPRICING_TTC_MAX_S": "240",
    "PULSE_MISPRICING_REQUIRE_CONFIRMED": "0",
    "PULSE_MISPRICING_REQUIRE_STALE_DOWN": "1",
    "PULSE_MISPRICING_MIN_EXECUTABLE_MARGIN": "0.02",
    "PULSE_MISPRICING_FOLLOW_ON_ABSTAIN": "0",
    "PULSE_MISPRICING_FOLLOW_SIZE_FRACTION": "0.5",
    "PULSE_EDGE_TTC_GATE_ENABLED": "1",
    "PULSE_CEX_LEAD_MIN_EDGE_VS_MARKET": "0.02",
    "PULSE_CEX_LEAD_TV_STRENGTH_THR": "0.72",
    # Tier 1: proven 180-240s cohort + high edge; block medium-edge bleed.
    "PULSE_BASELINE_COHORT_GATE_ENABLED": "1",
    "PULSE_BASELINE_COHORT_TTC_MIN_S": "180",
    "PULSE_BASELINE_COHORT_TTC_MAX_S": "240",
    "PULSE_BASELINE_COHORT_REQUIRE_HIGH_EDGE": "1",
    "PULSE_BASELINE_COHORT_REQUIRE_STRONG_CEX": "1",
    "PULSE_BASELINE_COHORT_15M_FAST_LANE": "1",
    "PULSE_BASELINE_COHORT_15M_TTC_MIN_S": "180",
    "PULSE_BASELINE_COHORT_15M_TTC_MAX_S": "240",
    "PULSE_BASELINE_UP_TV_GATE_ENABLED": "1",
    "PULSE_BASELINE_DOWN_TV_GATE_ENABLED": "1",
    "PULSE_BASELINE_DOWN_BLOCK_BULLISH_RANGE": "1",
    "PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_BULLISH": "1",
    # Arb + dependency scan 5m+15m; directional gated to 15m only (series split).
    "PULSE_SERIES_SLUGS": "btc-up-or-down-5m,btc-up-or-down-15m",
    "PULSE_DIRECTIONAL_SERIES_SLUGS": "btc-up-or-down-15m",
    "PULSE_ARB_EPSILON_15M": "0.03",
    "PULSE_DEPENDENCY_ARB_EPSILON": "0.02",
    "PULSE_GROK_DEPENDENCY_ENABLED": "1",
    "PULSE_GROK_DEPENDENCY_INTERVAL_S": "180",
    # Profit-discovery Phase 1–2: arb-first, stop directional bleed.
    "PULSE_ARB_EPSILON": "0.05",
    "PULSE_ARB_MAX_USD": "300",
    "PULSE_PRIMARY_EDGE_SOURCE": "arbitrage",
    "PULSE_DIRECTIONAL_MAX_BANKROLL_FRAC": "0.10",
    # DOWN-only directional: hard block every UP path (grok/cex/mispricing included).
    "PULSE_DIRECTIONAL_DOWN_ONLY": "1",
    "PULSE_DIRECTIONAL_BLOCK_UP_UNTIL_PROMOTED": "1",
    "PULSE_DIRECTIONAL_UP_RESTRICTIONS_ENABLED": "1",
    "PULSE_DEPENDENCY_ARB_ENABLED": "1",
    "PULSE_DEPENDENCY_ARB_EXECUTE": "1",
    "PULSE_DEPENDENCY_ARB_MAX_USD": "50",
    "PULSE_BREGMAN_PROJECTION_ENABLED": "1",
    "PULSE_ETH_SERIES_ENABLED": "0",
    "PULSE_RESEARCH_AUTO_APPLY": "0",
    "PULSE_RESEARCH_FORBID_SIZE_INCREASE": "1",
    "PULSE_ARB_GLOBAL_MAX_OPEN_USD": "600",
    "PULSE_ARB_NONATOMIC_ENABLED": "1",
    "PULSE_ARB_NONATOMIC_SLIPPAGE_BPS": "50",
    "PULSE_SIZING_PROMOTION_GATED": "1",
    "HERMES_SIZING_ENABLED": "0",
    # TradingView INDEX:BTCUSD — 4m + 5m + 10m + 13m + 15m chart alerts (five charts, same script).
    "PULSE_TV_FEATURE_SYMBOL": "BTCUSD",
    "TRADINGVIEW_ALLOWED_SYMBOLS": "BTCUSD,INDEX:BTCUSD,BTC/USD,BTC,XBTUSD",
    "TRADINGVIEW_MAX_AGE_S": "180",
    "PULSE_TV_MTF_TIMEFRAMES": "4,5,10,13,15",
    # ~2.5 bar lengths: Pine fires only on scored signals, not every bar close.
    "PULSE_TV_MTF_CONFIRM_WINDOW_4M_S": "600",
    "PULSE_TV_MTF_CONFIRM_WINDOW_S": "750",
    "PULSE_TV_MTF_CONFIRM_WINDOW_10M_S": "1500",
    "PULSE_TV_MTF_CONFIRM_WINDOW_13M_S": "1950",
    "PULSE_TV_MTF_CONFIRM_WINDOW_15M_S": "2250",
    # Tier 2: selectivity blocks need PF floor + higher min_samples + BH-FDR.
    "PULSE_SELECTIVITY_MIN_SAMPLES": "50",
    "PULSE_SELECTIVITY_MIN_PROFIT_FACTOR": "0.85",
    "PULSE_SELECTIVITY_FDR_Q": "0.10",
}


def _enforce_context_cohort_coupling(updates: dict) -> dict:
    """Raise PULSE_TV_CONTEXT_MAX_TTC_S if it would deadlock baseline cohort."""
    slugs = [s.strip() for s in updates.get("PULSE_SERIES_SLUGS", "").split(",") if s.strip()]
    rep = evaluate_context_cohort_coupling(
        baseline_cohort_enabled=updates.get("PULSE_BASELINE_COHORT_GATE_ENABLED", "1") == "1",
        tv_context_enabled=updates.get("PULSE_TV_CONTEXT_GATE", "1") == "1",
        configured_context_max_ttc_s=float(updates.get("PULSE_TV_CONTEXT_MAX_TTC_S", "0") or 0),
        cohort_ttc_min_s=float(updates.get("PULSE_BASELINE_COHORT_TTC_MIN_S", "180")),
        cohort_ttc_max_s=float(updates.get("PULSE_BASELINE_COHORT_TTC_MAX_S", "240")),
        window_seconds_list=window_seconds_for_slugs(slugs),
        auto_clamp=False,
    )
    if rep.get("active") and not rep.get("configured_ok"):
        fixed = str(int(rep["required_min_s"]))
        print(
            f"COUPLING: PULSE_TV_CONTEXT_MAX_TTC_S {updates['PULSE_TV_CONTEXT_MAX_TTC_S']} "
            f"-> {fixed} (required for cohort band on {slugs})"
        )
        updates = {**updates, "PULSE_TV_CONTEXT_MAX_TTC_S": fixed}
    return updates


UPDATES = _enforce_context_cohort_coupling(UPDATES)

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
out.append("# LOOP ENGINE ARCH (2026-06-25): Grok shadow + quant baseline + TV observe-only")
ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"Wrote {ENV_PATH} ({len(UPDATES)} loop-arch keys)")