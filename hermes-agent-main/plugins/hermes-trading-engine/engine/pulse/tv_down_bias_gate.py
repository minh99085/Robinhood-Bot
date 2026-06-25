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
        exploration_rate: float = 0.02,
        seed: Optional[int] = None,
    ):
        self.enabled = bool(enabled)
        self.block_bullish_aligned_up = bool(block_bullish_aligned_up)
        self.block_up_without_bearish = bool(block_up_without_bearish)
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
    ) -> list[str]:
        if not side or str(side).lower() != "up":
            return []
        reasons = []
        ma = str(mtf_alignment or "").strip().lower()
        td = str(tv_direction or "").strip().upper()
        if self.block_bullish_aligned_up and ma == "bullish_aligned":
            reasons.append("tv_down_bias_bullish_aligned_up")
        if self.block_up_without_bearish and td == "UP" and ma != "bearish_aligned":
            reasons.append("tv_down_bias_up_without_bearish")
        return reasons

    def evaluate(
        self,
        *,
        side: Optional[str],
        mtf_alignment=None,
        tv_direction=None,
    ) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        reasons = self.violations(side=side, mtf_alignment=mtf_alignment, tv_direction=tv_direction)
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
            "exploration_rate": self.exploration_rate,
            "passed": self.passed,
            "blocked": self.blocked,
            "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
            "note": "restrict-only: harvest DOWN/TV asymmetry by blocking proven-losing UP stacks",
        }

    def to_state(self) -> dict:
        return {
            "enabled": self.enabled,
            "block_bullish_aligned_up": self.block_bullish_aligned_up,
            "block_up_without_bearish": self.block_up_without_bearish,
            "exploration_rate": self.exploration_rate,
            "passed": self.passed,
            "blocked": self.blocked,
            "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
        }

    def load_state(self, data: dict) -> None:
        if not data:
            return
        self.enabled = bool(data.get("enabled", self.enabled))
        self.block_bullish_aligned_up = bool(
            data.get("block_bullish_aligned_up", self.block_bullish_aligned_up))
        self.block_up_without_bearish = bool(
            data.get("block_up_without_bearish", self.block_up_without_bearish))
        self.exploration_rate = max(0.0, min(0.05, float(data.get("exploration_rate", self.exploration_rate))))
        self.passed = int(data.get("passed", 0) or 0)
        self.blocked = int(data.get("blocked", 0) or 0)
        self.explored = int(data.get("explored", 0) or 0)
        self.block_reasons = dict(data.get("block_reasons") or {})
        self.explore_reasons = dict(data.get("explore_reasons") or {})