"""ShadowOutcomeTracker — capture market state after each decision and compute
markout / adverse selection. Tolerates missing observations (records nulls)."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from .config import ShadowConfig
from .schemas import ShadowDecision, ShadowObservation


def _d(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


class ShadowOutcomeTracker:
    def __init__(self, config: ShadowConfig, store=None):
        self.cfg = config
        self.store = store

    def horizons(self) -> list[int]:
        return list(self.cfg.outcome_horizons_ms)

    def observe(self, decision: ShadowDecision, *, horizon_ms: int = 0,
                best_bid=None, best_ask=None, last_trade_price=None,
                depth_near_touch=None, resolved_outcome=None, fill_price=None,
                shadow_order_id: Optional[str] = None, now_ms: Optional[int] = None
                ) -> ShadowObservation:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        bb, ba = _d(best_bid), _d(best_ask)
        mid = (bb + ba) / 2 if bb is not None and ba is not None else None
        spread = (ba - bb) if bb is not None and ba is not None else None
        ref = _d(fill_price) if fill_price is not None else decision.intended_limit_price
        markout = None
        if mid is not None and ref is not None:
            side = (decision.intended_side or "BUY").upper()
            markout = (mid - ref) if side == "BUY" else (ref - mid)
        obs = ShadowObservation(
            shadow_session_id=decision.shadow_session_id, decision_id=decision.decision_id,
            shadow_order_id=shadow_order_id, venue=decision.venue, market_id=decision.market_id,
            market_ticker=decision.market_ticker, asset_id=decision.asset_id,
            outcome=decision.outcome, horizon_ms=horizon_ms, observed_ts_ms=now,
            best_bid=bb, best_ask=ba, spread=spread, midpoint=mid,
            last_trade_price=_d(last_trade_price), depth_near_touch=_d(depth_near_touch),
            resolved_outcome=resolved_outcome, markout=markout)
        if self.store is not None:
            try:
                self.store.add_shadow_observation(obs.record())
            except Exception:  # noqa: BLE001
                pass
        return obs
