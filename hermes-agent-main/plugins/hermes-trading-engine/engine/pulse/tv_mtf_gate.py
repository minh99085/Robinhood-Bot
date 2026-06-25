"""TradingView 1m+5m MTF gate (restrict-only, PAPER ONLY).

Layer 1 (always when enabled): block ``conflict`` — fresh 1m and 5m disagree.
Layer 2 (``require_confirm``): trade only when ``confirmed_up`` / ``confirmed_down``.
Optional ``require_side_align``: candidate side must match the MTF direction.
"""

from __future__ import annotations

import random
from typing import Optional


class TradingViewMtfConflictGate:
    """Restrict-only gate. ``evaluate`` returns ``{decision, reasons}``."""

    def __init__(self, *, enabled: bool = True, require_confirm: bool = True,
                 require_side_align: bool = True, exploration_rate: float = 0.02,
                 seed: Optional[int] = None):
        self.enabled = bool(enabled)
        self.require_confirm = bool(require_confirm)
        self.require_side_align = bool(require_side_align)
        self.exploration_rate = max(0.0, min(0.05, float(exploration_rate)))
        self.passed = 0
        self.blocked = 0
        self.explored = 0
        self.block_reasons: dict = {}
        self.explore_reasons: dict = {}
        self._rng = random.Random(seed)

    def violations(self, *, tf_confirm=None, tf_confirm_direction=None,
                   side: Optional[str] = None) -> list[str]:
        tc = str(tf_confirm or "").strip().lower()
        reasons = []
        if tc == "conflict":
            reasons.append("tv_mtf_1m_5m_conflict")
        if self.require_confirm:
            if tc == "single_tf":
                reasons.append("tv_mtf_single_tf_only")
            elif tc in ("none", ""):
                reasons.append("tv_mtf_no_fresh_confirm")
            elif tc not in ("confirmed_up", "confirmed_down"):
                reasons.append("tv_mtf_not_confirmed")
        if self.require_side_align and tc in ("confirmed_up", "confirmed_down"):
            want = "up" if tc == "confirmed_up" else "down"
            if side and str(side).strip().lower() != want:
                reasons.append("tv_mtf_opposes_side")
        return reasons

    def evaluate(self, *, tf_confirm=None, tf_confirm_direction=None,
                 side: Optional[str] = None) -> dict:
        if not self.enabled:
            return {"decision": "pass", "reasons": [], "active": False}
        reasons = self.violations(tf_confirm=tf_confirm,
                                  tf_confirm_direction=tf_confirm_direction,
                                  side=side)
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
        mode = ("restrict_only_mtf_require_confirm"
                if self.require_confirm else "restrict_only_mtf_conflict")
        passes = (["confirmed_up", "confirmed_down"]
                  if self.require_confirm else
                  ["confirmed_up", "confirmed_down", "single_tf", "none"])
        blocks = ["conflict"]
        if self.require_confirm:
            blocks.extend(["single_tf", "none"])
        if self.require_side_align:
            blocks.append("opposes_side")
        return {
            "enabled": self.enabled,
            "mode": mode,
            "require_confirm": self.require_confirm,
            "require_side_align": self.require_side_align,
            "affects_trading": self.enabled,
            "can_force_trade": False,
            "execution_gate_still_authoritative": True,
            "blocks": blocks,
            "passes": passes,
            "exploration_rate": self.exploration_rate,
            "passed": self.passed,
            "blocked": self.blocked,
            "explored": self.explored,
            "block_reasons": dict(self.block_reasons),
            "explore_reasons": dict(self.explore_reasons),
            "note": ("1m+5m MTF gate: block conflict; when require_confirm, only "
                     "confirmed_up/down pass; optional side alignment. Restrict-only."),
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