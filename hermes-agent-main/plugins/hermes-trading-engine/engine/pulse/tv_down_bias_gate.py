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
        exploration_rate: float = 0.0,
        seed: Optional[int] = None,
    ):
        self.enabled = bool(enabled)
        self.block_bullish_aligned_up = bool(block_bullish_aligned_up)
        self.block_up_without_bearish = bool(block_up_without_bearish)
        self.block_up_on_bearish_down_stack = bool(block_up_on_bearish_down_stack)
        self.block_up_tv_down_non_bearish = bool(block_up_tv_down_non_bearish)
        self.block_up_against_confirmed_down = bool(block_up_against_confirmed_down)
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
    ) -> list[str]:
        if not side or str(side).lower() != "up":
            return []
        reasons = []
        ma = str(mtf_alignment or "").strip().lower()
        td = str(tv_direction or "").strip().upper()
        tc = str(tf_confirm or "").strip().lower()
        if self.block_bullish_aligned_up and ma == "bullish_aligned":
            reasons.append("tv_down_bias_bullish_aligned_up")
        if self.block_up_without_bearish and td == "UP" and ma != "bearish_aligned":
            reasons.append("tv_down_bias_up_without_bearish")
        if self.block_up_on_bearish_down_stack and ma == "bearish_aligned" and td == "DOWN":
            reasons.append("tv_down_bias_up_on_bearish_down_stack")
        if (self.block_up_tv_down_non_bearish and td == "DOWN"
                and ma not in ("bearish_aligned",)):
            reasons.append("tv_down_bias_up_tv_down_non_bearish")
        if self.block_up_against_confirmed_down and tc == "confirmed_down":
            reasons.append("tv_down_bias_up_against_confirmed_down")
        return reasons

    def evaluate(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
        tf_confirm=None,
    ) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        reasons = self.violations(side=side, mtf_alignment=mtf_alignment,
                                  tv_direction=tv_direction, tf_confirm=tf_confirm)
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