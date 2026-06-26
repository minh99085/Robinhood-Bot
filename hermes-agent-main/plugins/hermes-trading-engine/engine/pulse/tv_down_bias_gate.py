"""TradingView DOWN-bias gate (restrict-only, PAPER ONLY).

Townhall P3: the bot's own signal-learning shows bearish_aligned contexts win while
bullish_aligned UP trades lose. This gate blocks proven-losing UP-aligned entries; it can
only make the bot MORE selective and never forces a trade.
"""

from __future__ import annotations

import random
from typing import Optional


class TradingViewDownBiasGate:
    """Restrict-only gate for the asymmetric DOWN/TV edge."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        block_bullish_aligned_up: bool = True,
        block_up_without_bearish: bool = True,
        block_up_on_bearish_down_stack: bool = True,
        block_up_tv_down_non_bearish: bool = True,
        block_up_against_confirmed_down: bool = True,
        block_mixed_mtf_up: bool = True,
        block_bullish_supertrend_up: bool = True,
        block_up_vwap_above: bool = True,
        block_up_bb_expansion_up: bool = True,
        block_up_range_breakout_down: bool = True,
        block_up_range_top: bool = True,
        block_up_bb_squeeze: bool = True,
        block_up_markov_chop_noise: bool = True,
        block_up_htf_bullish: bool = True,
        block_up_bear_close_near_low: bool = True,
        block_up_late_ttc: bool = True,
        block_up_early_ttc: bool = True,
        up_late_ttc_min_s: float = 240.0,
        up_early_ttc_max_s: float = 120.0,
        exploration_rate: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.enabled = bool(enabled)
        self.block_bullish_aligned_up = bool(block_bullish_aligned_up)
        self.block_up_without_bearish = bool(block_up_without_bearish)
        self.block_up_on_bearish_down_stack = bool(block_up_on_bearish_down_stack)
        self.block_up_tv_down_non_bearish = bool(block_up_tv_down_non_bearish)
        self.block_up_against_confirmed_down = bool(block_up_against_confirmed_down)
        self.block_mixed_mtf_up = bool(block_mixed_mtf_up)
        self.block_bullish_supertrend_up = bool(block_bullish_supertrend_up)
        self.block_up_vwap_above = bool(block_up_vwap_above)
        self.block_up_bb_expansion_up = bool(block_up_bb_expansion_up)
        self.block_up_range_breakout_down = bool(block_up_range_breakout_down)
        self.block_up_range_top = bool(block_up_range_top)
        self.block_up_bb_squeeze = bool(block_up_bb_squeeze)
        self.block_up_markov_chop_noise = bool(block_up_markov_chop_noise)
        self.block_up_htf_bullish = bool(block_up_htf_bullish)
        self.block_up_bear_close_near_low = bool(block_up_bear_close_near_low)
        self.block_up_late_ttc = bool(block_up_late_ttc)
        self.block_up_early_ttc = bool(block_up_early_ttc)
        self.up_late_ttc_min_s = max(0.0, float(up_late_ttc_min_s))
        self.up_early_ttc_max_s = max(0.0, float(up_early_ttc_max_s))
        self.exploration_rate = max(0.0, min(0.05, float(exploration_rate)))
        self.passed = 0
        self.blocked = 0
        self.explored = 0
        self.block_reasons: dict = {}
        self.explore_reasons: dict = {}
        self._rng = random.Random(seed)

    def violations(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
        tf_confirm=None,
        supertrend_direction=None,
        vwap_state=None,
        bb_state=None,
        range_state=None,
        markov_state=None,
        htf_bias=None,
        candle_pressure=None,
        ttc_s=None,
    ) -> list[str]:
        if not side or str(side).lower() != "up":
            return []
        reasons = []
        ma = str(mtf_alignment or "").strip().lower()
        td = str(tv_direction or "").strip().upper()
        tc = str(tf_confirm or "").strip().lower()
        st = str(supertrend_direction or "").strip().lower()
        vw = str(vwap_state or "").strip().lower()
        bb = str(bb_state or "").strip().lower()
        rs = str(range_state or "").strip().lower()
        ms = str(markov_state or "").strip().lower()
        hb = str(htf_bias or "").strip().lower()
        cp = str(candle_pressure or "").strip().lower()
        if self.block_bullish_aligned_up and ma == "bullish_aligned":
            reasons.append("tv_down_bias_bullish_aligned_up")
        if self.block_mixed_mtf_up and ma == "mixed":
            reasons.append("tv_down_bias_mixed_mtf_up")
        if self.block_bullish_supertrend_up and st == "bullish":
            reasons.append("tv_down_bias_bullish_supertrend_up")
        if self.block_up_without_bearish and td == "UP" and ma != "bearish_aligned":
            reasons.append("tv_down_bias_up_without_bearish")
        if self.block_up_on_bearish_down_stack and ma == "bearish_aligned" and td == "DOWN":
            reasons.append("tv_down_bias_up_on_bearish_down_stack")
        if (self.block_up_tv_down_non_bearish and td == "DOWN"
                and ma not in ("bearish_aligned",)):
            reasons.append("tv_down_bias_up_tv_down_non_bearish")
        if self.block_up_against_confirmed_down and tc == "confirmed_down":
            reasons.append("tv_down_bias_up_against_confirmed_down")
        if self.block_up_vwap_above and vw == "above":
            reasons.append("tv_down_bias_up_vwap_above")
        if self.block_up_bb_expansion_up and bb == "expansion_up":
            reasons.append("tv_down_bias_up_bb_expansion_up")
        if self.block_up_range_breakout_down and rs == "breakout_down":
            reasons.append("tv_down_bias_up_range_breakout_down")
        if self.block_up_range_top and rs == "range_top":
            reasons.append("tv_down_bias_up_range_top")
        if self.block_up_bb_squeeze and bb == "squeeze":
            reasons.append("tv_down_bias_up_bb_squeeze")
        if self.block_up_markov_chop_noise and ms == "chop_noise":
            reasons.append("tv_down_bias_up_markov_chop_noise")
        if self.block_up_htf_bullish and hb == "bullish":
            reasons.append("tv_down_bias_up_htf_bullish")
        if self.block_up_bear_close_near_low and cp == "bear_close_near_low":
            reasons.append("tv_down_bias_up_bear_close_near_low")
        if ttc_s is not None:
            ttc = float(ttc_s)
            if self.block_up_late_ttc and ttc >= self.up_late_ttc_min_s:
                reasons.append("tv_down_bias_up_late_ttc")
            if self.block_up_early_ttc and ttc < self.up_early_ttc_max_s:
                reasons.append("tv_down_bias_up_early_ttc")
        return reasons

    def evaluate(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
        tf_confirm=None,
        supertrend_direction=None,
        vwap_state=None,
        bb_state=None,
        range_state=None,
        markov_state=None,
        htf_bias=None,
        candle_pressure=None,
        ttc_s=None,
    ) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        reasons = self.violations(side=side, mtf_alignment=mtf_alignment,
                                  tv_direction=tv_direction, tf_confirm=tf_confirm,
                                  supertrend_direction=supertrend_direction,
                                  vwap_state=vwap_state, bb_state=bb_state,
                                  range_state=range_state, markov_state=markov_state,
                                  htf_bias=htf_bias, candle_pressure=candle_pressure,
                                  ttc_s=ttc_s)
        if not reasons:
            self.passed += 1
            return {"decision": "pass", "reasons": [], "active": True}
        if self.exploration_rate > 0 and self._rng.random() < self.exploration_rate:
            self.explored += 1
            for r in reasons:
                self.explore_reasons[r] = self.explore_reasons.get(r, 0) + 1
            return {"decision": "explore", "reasons": reasons, "active": True}
        self.blocked += 1
        for r in reasons:
            self.block_reasons[r] = self.block_reasons.get(r, 0) + 1
        return {"decision": "block", "reasons": reasons, "active": True}

    def report(self) -> dict:
        return {
            "enabled": self.enabled,
            "block_bullish_aligned_up": self.block_bullish_aligned_up,
            "block_up_without_bearish": self.block_up_without_bearish,
            "block_up_on_bearish_down_stack": self.block_up_on_bearish_down_stack,
            "block_up_tv_down_non_bearish": self.block_up_tv_down_non_bearish,
            "block_up_against_confirmed_down": self.block_up_against_confirmed_down,
            "block_mixed_mtf_up": self.block_mixed_mtf_up,
            "block_bullish_supertrend_up": self.block_bullish_supertrend_up,
            "block_up_vwap_above": self.block_up_vwap_above,
            "block_up_bb_expansion_up": self.block_up_bb_expansion_up,
            "block_up_range_breakout_down": self.block_up_range_breakout_down,
            "block_up_range_top": self.block_up_range_top,
            "block_up_bb_squeeze": self.block_up_bb_squeeze,
            "block_up_markov_chop_noise": self.block_up_markov_chop_noise,
            "block_up_htf_bullish": self.block_up_htf_bullish,
            "block_up_bear_close_near_low": self.block_up_bear_close_near_low,
            "block_up_late_ttc": self.block_up_late_ttc,
            "block_up_early_ttc": self.block_up_early_ttc,
            "up_late_ttc_min_s": self.up_late_ttc_min_s,
            "up_early_ttc_max_s": self.up_early_ttc_max_s,
            "exploration_rate": self.exploration_rate,
            "passed": self.passed,
            "blocked": self.blocked,
            "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
            "note": "restrict-only: harvest DOWN/TV asymmetry by blocking proven-losing UP stacks",
        }

    def to_state(self) -> dict:
        return {"passed": self.passed, "blocked": self.blocked, "explored": self.explored,
                "block_reasons": dict(self.block_reasons),
                "explore_reasons": dict(self.explore_reasons)}

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.passed = int(data.get("passed", 0) or 0)
        self.blocked = int(data.get("blocked", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.block_reasons = {k: int(v or 0) for k, v in (data.get("block_reasons") or {}).items()}
        self.explore_reasons = {k: int(v or 0)
                                for k, v in (data.get("explore_reasons") or {}).items()}