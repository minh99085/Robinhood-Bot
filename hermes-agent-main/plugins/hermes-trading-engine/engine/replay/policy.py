"""Replay policies. A policy ONLY emits TradeProposal objects — it never places
orders. The ReplayRunner sends proposals through RiskEngine + OMS + PaperBroker.

Policies see only replay-safe state (simulated time, reconstructed book/BBO,
cached probability estimates, current positions, risk config). No network, no
Grok calls.
"""

from __future__ import annotations

from typing import Optional

from ..schemas import TradeProposal


def _mk_proposal(policy_name: str, market_id: str, asset_id: Optional[str], side: str,
                 price: float, qty: float, fair: Optional[float],
                 conf: Optional[float], edge: float) -> TradeProposal:
    return TradeProposal(
        strategy=policy_name, market="polymarket", symbol=market_id, side=side,
        notional=float(price) * float(qty), price=float(price), edge_after_costs=float(edge),
        spread=0.0, ambiguity_score=0.0, allow_duplicate=False, mode="paper",
        rationale=f"{policy_name} fair={fair} edge={round(edge, 4)}",
        meta={"asset_id": asset_id, "fair_probability": fair, "confidence": conf,
              "quantity": float(qty), "outcome": side, "policy": policy_name})


class ReplayPolicy:
    name = "base"

    def __init__(self, params: Optional[dict] = None):
        self.params = params or {}

    def on_tick(self, ctx) -> list[TradeProposal]:  # noqa: D401
        return []


class NoOpPolicy(ReplayPolicy):
    """Baseline: never trades. Used for sanity checks + zero-edge baseline."""

    name = "noop"


class SimpleEdgePolicy(ReplayPolicy):
    """Deterministic baseline: BUY when fair_probability exceeds the ask by min_edge.

    No randomness. ``policy_params``: fair_probability (float, default 0.5),
    cached_probabilities ({asset_id|market_id: p}), min_edge (default 0.05),
    quantity (default 10).
    """

    name = "simple_edge"

    def on_tick(self, ctx) -> list[TradeProposal]:
        min_edge = float(self.params.get("min_edge", 0.05))
        qty = float(self.params.get("quantity", 10))
        out: list[TradeProposal] = []
        for market_id, asset_id in ctx.markets():
            book = ctx.get_orderbook(asset_id)
            if book is None or book.best_ask is None:
                continue
            price = float(book.best_ask)
            fair = ctx.cached_prob(market_id, asset_id)
            if fair is None:
                continue
            edge = fair - price
            if edge > min_edge and ctx.position_qty(asset_id) == 0:
                out.append(_mk_proposal(self.name, market_id, asset_id, "YES",
                                        price=price, qty=qty, fair=fair, conf=None, edge=edge))
        return out


class CachedGrokPolicy(ReplayPolicy):
    """Uses cached Grok probability estimates only (no network). Same edge gate
    as SimpleEdge but gated by a cached confidence floor."""

    name = "cached_grok"

    def on_tick(self, ctx) -> list[TradeProposal]:
        min_edge = float(self.params.get("min_edge", 0.05))
        min_conf = float(self.params.get("min_confidence", 0.0))
        qty = float(self.params.get("quantity", 10))
        confs = self.params.get("cached_confidences", {}) or {}
        out: list[TradeProposal] = []
        for market_id, asset_id in ctx.markets():
            book = ctx.get_orderbook(asset_id)
            if book is None or book.best_ask is None:
                continue
            price = float(book.best_ask)
            fair = ctx.cached_prob(market_id, asset_id)
            conf = confs.get(asset_id, confs.get(market_id))
            if fair is None or (conf is not None and conf < min_conf):
                continue
            edge = fair - price
            if edge > min_edge and ctx.position_qty(asset_id) == 0:
                out.append(_mk_proposal(self.name, market_id, asset_id, "YES",
                                        price=price, qty=qty, fair=fair, conf=conf, edge=edge))
        return out


class ExistingStrategyPolicy(ReplayPolicy):
    """Thin offline adapter approximating the live engine's EV gate using cached
    probabilities and the reconstructed book — without starting any live loop or
    network feed. (A simplified stand-in for the production strategy.)"""

    name = "existing"

    def on_tick(self, ctx) -> list[TradeProposal]:
        ev_threshold = float(self.params.get("ev_threshold", 0.03))
        qty = float(self.params.get("quantity", 10))
        out: list[TradeProposal] = []
        for market_id, asset_id in ctx.markets():
            book = ctx.get_orderbook(asset_id)
            if book is None or book.best_ask is None:
                continue
            price = float(book.best_ask)
            fair = ctx.cached_prob(market_id, asset_id)
            if fair is None or price <= 0:
                continue
            ev = fair / price - 1.0  # binary payout EV
            if ev > ev_threshold and ctx.position_qty(asset_id) == 0:
                out.append(_mk_proposal(self.name, market_id, asset_id, "YES",
                                        price=price, qty=qty, fair=fair, conf=None,
                                        edge=fair - price))
        return out


class RandomPolicy(ReplayPolicy):
    """Seed-controlled random baseline (test/benchmark only). Reproducible per seed."""

    name = "random"

    def on_tick(self, ctx) -> list[TradeProposal]:
        prob = float(self.params.get("trade_probability", 0.1))
        qty = float(self.params.get("quantity", 10))
        out: list[TradeProposal] = []
        for market_id, asset_id in ctx.markets():
            book = ctx.get_orderbook(asset_id)
            if book is None or book.best_ask is None:
                continue
            if ctx.rng.random() < prob and ctx.position_qty(asset_id) == 0:
                price = float(book.best_ask)
                out.append(_mk_proposal(self.name, market_id, asset_id, "YES",
                                        price=price, qty=qty, fair=None, conf=None, edge=0.0))
        return out


_POLICIES = {
    "noop": NoOpPolicy,
    "simple_edge": SimpleEdgePolicy,
    "cached_grok": CachedGrokPolicy,
    "existing": ExistingStrategyPolicy,
    "random": RandomPolicy,
}


def build_policy(name: str, params: Optional[dict] = None) -> ReplayPolicy:
    cls = _POLICIES.get((name or "noop").lower(), NoOpPolicy)
    return cls(params or {})
