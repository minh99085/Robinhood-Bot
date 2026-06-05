"""Per-strategy PnL attribution with exploration/validation separation (pure).

Exploration trades (Tier-4 tiny paper bets) must NEVER count as live-readiness
validation evidence. This module keeps a clean split:

* ``validation_pnl`` — PnL from certified/edge trades (Tiers 1-3).
* ``exploration_pnl`` — PnL from exploration-only trades (Tier 4).
* ``by_strategy`` / ``by_tier`` — attribution breakdowns.

Pure, deterministic, no I/O. PAPER ONLY.

Quant responsibilities
----------------------
* Quant researcher — defines what counts as validation vs exploration.
* Quant developer — owns this attribution accounting (typed, tested).
* Trader/monitoring — reads validation-only PnL for readiness; never conflates
  exploration PnL with validated edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

logger = logging.getLogger("hte.strategies.attribution")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


@dataclass
class AttributionRecord:
    strategy: str
    pnl: float
    tier: Optional[int] = None
    is_exploration: bool = False


@dataclass
class PnLAttribution:
    """Accumulates trade PnL split by exploration/validation + strategy/tier."""

    validation_pnl: float = 0.0
    exploration_pnl: float = 0.0
    by_strategy: dict = field(default_factory=dict)
    by_tier: dict = field(default_factory=dict)
    n_validation: int = 0
    n_exploration: int = 0

    def record(self, strategy: str, pnl, *, tier: Optional[int] = None,
               is_exploration: bool = False) -> None:
        """Record one resolved paper trade's PnL (validation unless flagged)."""
        p = _num(pnl)
        if p is None:
            logger.debug("ignoring non-numeric pnl for %s: %r", strategy, pnl)
            return
        self.by_strategy[strategy] = round(self.by_strategy.get(strategy, 0.0) + p, 10)
        if tier is not None:
            self.by_tier[int(tier)] = round(self.by_tier.get(int(tier), 0.0) + p, 10)
        if is_exploration:
            self.exploration_pnl = round(self.exploration_pnl + p, 10)
            self.n_exploration += 1
        else:
            self.validation_pnl = round(self.validation_pnl + p, 10)
            self.n_validation += 1

    def total_pnl(self) -> float:
        return round(self.validation_pnl + self.exploration_pnl, 10)

    def summary(self) -> dict:
        return {
            "validation_pnl": self.validation_pnl,
            "exploration_pnl": self.exploration_pnl,
            "total_pnl": self.total_pnl(),
            "n_validation": self.n_validation,
            "n_exploration": self.n_exploration,
            "by_strategy": dict(self.by_strategy),
            "by_tier": dict(self.by_tier),
            # Validation-only is the readiness number; exploration excluded.
            "exploration_excluded_from_validation": True,
        }


def split_exploration_validation(records: Iterable[Mapping]) -> dict:
    """Split a list of ``{strategy, pnl, tier?, is_exploration?}`` records into a
    PnLAttribution summary. Convenience wrapper around :class:`PnLAttribution`."""
    attr = PnLAttribution()
    for r in records or []:
        attr.record(r.get("strategy", "unknown"), r.get("pnl"),
                    tier=r.get("tier"), is_exploration=bool(r.get("is_exploration")))
    return attr.summary()
