"""ReplayRunner — deterministic, offline, event-driven backtest.

Replays saved raw market events, reconstructs order books, runs a policy's
proposals through RiskEngine + OMS + PaperBroker against the *replayed* book,
and records reproducible orders/fills/positions/equity/metrics/calibration.

Quant scope — *Backtesting & Simulation* + *Strategy Optimization & Robustness
Testing*: the offline harness for validating strategies (including flagship
Bregman arbitrage) against replayed books. Bregman certified-opportunity
aggregates (certified profit, false-positive rate, persistence, capital
efficiency, rejection reasons) are available via
:func:`engine.replay.metrics.bregman_metrics`.

Determinism: same config (``config_hash``) + same events + same seed -> same
output. No wall-clock sleeps, no network, no Grok calls. All OMS state lives in
an ISOLATED in-memory store so operational orders/fills/positions are never
touched; results are mirrored into the operational ``replay_*`` tables keyed by
``replay_run_id``.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..execution import OrderManagementSystem, PaperBroker
from ..execution.fees import FeeModel
from ..execution.slippage import SlippageModel
from ..execution.types import D, OrderRequest, OrderSide, OrderType, new_client_order_id
from ..market_data.polymarket_ws import PolymarketWSClient
from ..risk import MarketDataSnapshot, RiskContext, RiskEngine, RiskLimits
from ..storage import Store
from . import calibration as cal
from . import metrics as met
from .clock import ReplayClock
from .episode import MARKET_DATA_EVENT_TYPES, ReplayConfig, ReplayEpisode, ReplayEvent
from .policy import build_policy


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Ctx:
    """Replay-safe view handed to the policy each strategy tick."""

    def __init__(self, runner: "ReplayRunner"):
        self._r = runner
        self.rng = runner.rng

    def now_ms(self) -> int:
        return self._r.clock.now_ms()

    @property
    def config(self):
        return self._r.config

    def markets(self):
        out = []
        wanted = set(self._r.config.asset_ids or [])
        for asset_id, book in self._r.md._books.items():
            if wanted and asset_id not in wanted:
                continue
            out.append((book.market_id or asset_id, asset_id))
        return out

    def get_orderbook(self, asset_id):
        return self._r.md.get_orderbook(asset_id)

    def get_bbo(self, asset_id):
        return self._r.md.get_bbo(asset_id)

    def cached_prob(self, market_id, asset_id) -> Optional[float]:
        cp = self._r.config.policy_params.get("cached_probabilities", {}) or {}
        if asset_id in cp:
            return float(cp[asset_id])
        if market_id in cp:
            return float(cp[market_id])
        fp = self._r.config.policy_params.get("fair_probability")
        return float(fp) if fp is not None else None

    def position_qty(self, asset_id) -> float:
        return self._r.pos_qty.get(asset_id, 0.0)


class ReplayRunner:
    def __init__(self, config: ReplayConfig, out_store, events: list[ReplayEvent]):
        self.config = config
        self.out_store = out_store
        self.events = events
        self.run_id = config.replay_run_id or ("rp-" + uuid.uuid4().hex[:16])
        self.config.replay_run_id = self.run_id
        self.config_hash = config.config_hash()
        self.seed = int(config.seed)
        self.rng = random.Random(self.seed)
        self.clock = ReplayClock()

        # ISOLATED OMS state (in-memory) — operational tables untouched
        self._oms_store = Store(":memory:")
        self.broker = PaperBroker(
            fee_model=FeeModel(), slippage_model=SlippageModel(),
            reject_on_stale=False,  # replay gates staleness via RiskEngine + clock
            allow_pm_reference=config.allow_reference_price_fills,
            stale_ms=config.stale_ms)
        self.oms = OrderManagementSystem(self._oms_store, self.broker,
                                         mode_provider=lambda: "paper", run_id=self.run_id)
        # Replay RiskEngine: default limits (NOT operational kill-switch driven)
        self.risk = RiskEngine(RiskLimits())
        # order-book reconstructor (no network, no persistence)
        self.md = PolymarketWSClient(event_store=None, persist_raw=False, stale_ms=config.stale_ms)

        self.policy = build_policy(config.policy_name, config.policy_params)
        self.ctx = _Ctx(self)

        self.cash = float(config.initial_cash)
        self.pos_qty: dict[str, float] = {}
        self._order_seq = 0
        self._fill_seq = 0

        # collected (in-memory, deterministic) rows
        self.proc_events: list[dict] = []
        self.proposals: list[dict] = []
        self.risk_decisions: list[dict] = []
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.equity_rows: list[dict] = []
        self.positions: list[dict] = []
        self.counters: dict = {"events_processed": 0, "max_gap_ms": 0, "malformed": 0}
        self._last_event_ts: Optional[int] = None
        self.status = "created"
        self.error: Optional[str] = None

    # ================================================================== #
    def episode(self) -> ReplayEpisode:
        start = self.events[0].ts_ms if self.events else None
        end = self.events[-1].ts_ms if self.events else None
        return ReplayEpisode(
            episode_id=self.config.episode_id or self.run_id, venue=self.config.venue or "",
            market_ids=self.config.market_ids, asset_ids=self.config.asset_ids,
            start_ts_ms=start, end_ts_ms=end, event_count=len(self.events),
            source=self.config.from_jsonl or "sqlite", config_hash=self.config_hash,
            seed=self.seed, notes="")

    def _persist_run(self, status: str) -> None:
        ep = self.episode()
        self.out_store.upsert_replay_run({
            "replay_run_id": self.run_id, "episode_id": ep.episode_id,
            "config_json": self.config.model_dump_json(), "config_hash": self.config_hash,
            "seed": self.seed, "started_at": getattr(self, "_started_at", _now_iso()),
            "finished_at": _now_iso() if status in ("finished", "failed") else None,
            "status": status, "venue": ep.venue, "market_ids_json": _json(ep.market_ids),
            "asset_ids_json": _json(ep.asset_ids), "start_ts_ms": ep.start_ts_ms,
            "end_ts_ms": ep.end_ts_ms, "event_count": ep.event_count, "notes": ep.notes})

    # ================================================================== #
    def run(self) -> dict:
        self._started_at = _now_iso()
        if not self.events:
            self.status = "failed"
            self.error = "no_events"
            self._persist_run("failed")
            return {"replay_run_id": self.run_id, "status": "failed", "error": "no_events"}

        self._persist_run("running")
        timeline = self._build_timeline()
        try:
            for ev in timeline:
                self.clock.advance_to(ev.ts_ms)
                if ev.event_type in MARKET_DATA_EVENT_TYPES:
                    self._on_market_event(ev)
                elif ev.event_type == "strategy_tick":
                    self._on_strategy_tick()
                elif ev.event_type == "equity_snapshot":
                    self._snapshot_equity()
            self._finalize()
            self.status = "finished"
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            self.error = str(exc)[:200]
        self._persist_run(self.status)
        report = self._build_report()
        return report

    # ------------------------------------------------------------------ #
    def _build_timeline(self) -> list[ReplayEvent]:
        evs = list(self.events)
        start = evs[0].ts_ms
        end = evs[-1].ts_ms
        merged: list[tuple] = [(e.ts_ms, 0, e.sequence, e) for e in evs]
        # strategy ticks
        tick = self.config.strategy_tick_ms
        t = start
        seq = 0
        while tick > 0 and t <= end:
            merged.append((t, 1, seq, ReplayEvent(ts_ms=t, event_type="strategy_tick", sequence=seq)))
            t += tick
            seq += 1
        # equity snapshots
        eq = self.config.equity_snapshot_ms
        t = start
        seq = 0
        while eq > 0 and t <= end:
            merged.append((t, 2, seq, ReplayEvent(ts_ms=t, event_type="equity_snapshot", sequence=seq)))
            t += eq
            seq += 1
        merged.sort(key=lambda x: (x[0], x[1], x[2]))
        return [m[3] for m in merged]

    # ------------------------------------------------------------------ #
    def _on_market_event(self, ev: ReplayEvent) -> None:
        if self._last_event_ts is not None:
            gap = ev.ts_ms - self._last_event_ts
            if gap > self.counters["max_gap_ms"]:
                self.counters["max_gap_ms"] = gap
        self._last_event_ts = ev.ts_ms
        self.counters["events_processed"] += 1
        self.counters[ev.event_type] = self.counters.get(ev.event_type, 0) + 1
        try:
            self.md._dispatch_event(ev.payload)
        except Exception:  # noqa: BLE001
            self.counters["malformed"] += 1
        if self.config.persist_processed_events:
            rec = {"replay_run_id": self.run_id, "ts_ms": ev.ts_ms, "sequence": ev.sequence,
                   "source_event_id": ev.raw_event_id, "venue": ev.venue,
                   "market_id": ev.market_id, "asset_id": ev.asset_id,
                   "event_type": ev.event_type, "payload_hash": ev.payload_hash()}
            self.proc_events.append(rec)
            self.out_store.add_replay_event_processed(rec)
        # let resting orders fill on a crossing book update
        self.oms.process_resting(self._book_provider)

    def _book_provider(self, order):
        return self.md.get_orderbook(getattr(order, "asset_id", None))

    # ------------------------------------------------------------------ #
    def _on_strategy_tick(self) -> None:
        for proposal in self.policy.on_tick(self.ctx) or []:
            self._handle_proposal(proposal)

    def _handle_proposal(self, proposal) -> None:
        asset_id = (proposal.meta or {}).get("asset_id")
        market_id = proposal.symbol
        # persist proposal
        prec = {
            "replay_run_id": self.run_id, "ts_ms": self.clock.now_ms(),
            "proposal_id": proposal.proposal_id, "policy_name": self.policy.name,
            "venue": "polymarket", "market_id": market_id, "asset_id": asset_id,
            "side": proposal.side, "outcome": (proposal.meta or {}).get("outcome"),
            "fair_probability": _s((proposal.meta or {}).get("fair_probability")),
            "confidence": _s((proposal.meta or {}).get("confidence")),
            "limit_price": _s(proposal.price), "quantity": _s((proposal.meta or {}).get("quantity")),
            "notional": _s(proposal.notional), "edge_after_costs": _s(proposal.edge_after_costs),
            "payload_json": _json(proposal.meta or {}),
        }
        self.proposals.append(prec)
        self.out_store.add_replay_proposal(prec)

        # build risk context from replay state
        ctx = self._risk_context(proposal, asset_id)
        decision = self.risk.evaluate(proposal, ctx)
        drec = {"replay_run_id": self.run_id, "ts_ms": self.clock.now_ms(),
                "proposal_id": proposal.proposal_id, "client_order_id": None,
                "approved": 1 if decision.approved else 0, "reason": decision.code,
                "payload_json": _json(decision.as_record())}
        self.risk_decisions.append(drec)
        self.out_store.add_replay_risk_decision(drec)
        if not decision.approved:
            return

        # build + submit order
        book = self.md.get_orderbook(asset_id) if asset_id else None
        side = OrderSide.BUY if str(proposal.side).upper() in ("BUY", "UP", "YES") else OrderSide.SELL
        qty = float((proposal.meta or {}).get("quantity") or 1.0)
        tif = str((proposal.meta or {}).get("time_in_force") or "IOC")
        coid = new_client_order_id(f"{self.config_hash}:{self._order_seq}")
        self._order_seq += 1
        order = OrderRequest(
            client_order_id=coid, venue="polymarket", market_id=market_id, asset_id=asset_id,
            outcome=(proposal.meta or {}).get("outcome"), side=side,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=D(proposal.price) if proposal.price is not None else None,
            quantity=D(qty), time_in_force=tif, source=self.policy.name,
            proposal_id=proposal.proposal_id, venue_kind="pm",
            created_ts_ms=self.clock.now_ms())
        result = self.oms.submit(order, decision, book=book, reference_price=proposal.price)
        self._record_order(order, result)

    def _risk_context(self, proposal, asset_id) -> RiskContext:
        equity = self._equity()
        total_exp = sum(abs(self.pos_qty.get(a, 0.0)) * self._mark(a) for a in self.pos_qty)
        md = None
        book = self.md.get_orderbook(asset_id) if asset_id else None
        if book is not None:
            age = self.clock.now_ms() - book.last_update_ms if book.last_update_ms else 1 << 60
            spread = float(book.spread_pct) if book.spread_pct is not None else None
            md = MarketDataSnapshot(
                required=True, status="connected",
                bbo_present=book.best_bid is not None and book.best_ask is not None,
                stale=age > self.config.stale_ms,
                resolved=book.resolved or (book.market_id in self.md._resolved_markets),
                tick_size_dirty=book.tick_size_dirty, unreliable=book.unreliable, spread=spread)
        elif proposal.market == "polymarket":
            md = MarketDataSnapshot(required=True, status="connected", bbo_present=False,
                                    stale=True, unreliable=True)
        return RiskContext(equity=equity, total_exposure=total_exp,
                           market_exposure=total_exp, has_open_same_market_side=False,
                           open_orders=len(self.oms.get_open_orders()), day_pnl=0.0,
                           market_data=md)

    def _record_order(self, order: OrderRequest, result) -> None:
        orec = {
            "replay_run_id": self.run_id, "client_order_id": order.client_order_id,
            "ts_ms": self.clock.now_ms(), "venue": order.venue, "market_id": order.market_id,
            "asset_id": order.asset_id, "side": order.side, "order_type": order.order_type,
            "limit_price": _s(order.limit_price), "quantity": _s(order.quantity),
            "notional": _s(order.notional), "status": result.status,
            "reject_reason": result.reject_reason, "payload_json": _json(order.record()),
        }
        self.orders.append(orec)
        self.out_store.add_replay_order(orec)
        for f in result.fills:
            self._fill_seq += 1
            fid = f"fl-{order.client_order_id}-{self._fill_seq}"
            qty = float(f.quantity)
            notional = float(f.price) * qty
            fee = float(f.fee)
            # deterministic cash accounting
            if f.side == OrderSide.BUY:
                self.cash -= (notional + fee)
                self.pos_qty[order.asset_id] = self.pos_qty.get(order.asset_id, 0.0) + qty
            else:
                self.cash += (notional - fee)
                self.pos_qty[order.asset_id] = self.pos_qty.get(order.asset_id, 0.0) - qty
            frec = {
                "replay_run_id": self.run_id, "fill_id": fid,
                "client_order_id": order.client_order_id, "ts_ms": self.clock.now_ms(),
                "venue": order.venue, "market_id": order.market_id, "asset_id": order.asset_id,
                "side": f.side, "price": _s(f.price), "quantity": _s(f.quantity),
                "notional": _s(Decimal(str(notional))), "fee": _s(f.fee),
                "liquidity_flag": f.liquidity_flag, "payload_json": _json(f.record()),
            }
            self.fills.append(frec)
            self.out_store.add_replay_fill(frec)

    # ------------------------------------------------------------------ #
    def _mark(self, asset_id) -> float:
        book = self.md.get_orderbook(asset_id)
        if book is None:
            return 0.0
        if book.midpoint is not None:
            return float(book.midpoint)
        if book.last_trade_price is not None:
            return float(book.last_trade_price)
        return 0.0

    def _position_value(self) -> float:
        return sum(self.pos_qty.get(a, 0.0) * self._mark(a) for a in self.pos_qty)

    def _equity(self) -> float:
        return self.cash + self._position_value()

    def _snapshot_equity(self) -> None:
        positions = self.oms.recon.rebuild_positions()
        realized = sum(float(p.realized_pnl) for p in positions)
        equity = self._equity()
        unreal = equity - self.cash - realized
        fees = sum(float(f.get("fee") or 0) for f in self.fills)
        exposure = abs(self._position_value())
        peak = max([r["equity"] for r in self.equity_rows] + [equity]) if self.equity_rows else equity
        dd = peak - equity
        row = {"replay_run_id": self.run_id, "ts_ms": self.clock.now_ms(),
               "cash": round(self.cash, 6), "equity": round(equity, 6),
               "realized_pnl": round(realized, 6), "unrealized_pnl": round(unreal, 6),
               "fees_paid": round(fees, 6), "drawdown": round(dd, 6),
               "exposure": round(exposure, 6)}
        self.equity_rows.append(row)
        self.out_store.add_replay_equity(row)

    # ------------------------------------------------------------------ #
    def _finalize(self) -> None:
        # end-of-run open-order policy
        if self.config.end_open_order_policy == "cancel":
            self.oms.cancel_all()
        # rebuild + mark positions (mark-to-market using final midpoint)
        positions = self.oms.recon.rebuild_positions(
            price_provider=lambda v, m, a: Decimal(str(self._mark(a))) if self.config.mark_to_market else None)
        for p in positions:
            rec = {"replay_run_id": self.run_id, "ts_ms": self.clock.now_ms(),
                   "venue": p.venue, "market_id": p.market_id, "asset_id": p.asset_id,
                   "outcome": p.outcome, "quantity": _s(p.quantity), "avg_price": _s(p.avg_price),
                   "realized_pnl": _s(p.realized_pnl), "unrealized_pnl": _s(p.unrealized_pnl),
                   "fees_paid": _s(p.fees_paid), "payload_json": _json(p.record())}
            self.positions.append(rec)
            self.out_store.add_replay_position(rec)
        self._snapshot_equity()  # final equity (mark-to-market)

        # calibration: match proposal probabilities to outcomes
        preds = [{"venue": "polymarket", "market_id": p["market_id"], "asset_id": p["asset_id"],
                  "outcome": p["outcome"], "predicted_probability": float(p["fair_probability"])
                  if p["fair_probability"] is not None else None}
                 for p in self.proposals if p.get("fair_probability") is not None]
        outcomes = self.out_store.get_market_outcomes(venue="polymarket")
        calib = cal.summarize_calibration(preds, outcomes)
        for row in calib.get("rows", []):
            self.out_store.add_replay_calibration({
                "replay_run_id": self.run_id, "market_id": row.get("market_id"),
                "asset_id": row.get("asset_id"), "outcome": row.get("outcome"),
                "predicted_probability": _s(row.get("predicted_probability")),
                "confidence": None, "realized_outcome": row.get("realized_outcome"),
                "bucket": None, "brier": None, "log_loss": None, "ts_ms": self.clock.now_ms()})
        self._calibration = {k: v for k, v in calib.items() if k != "rows"}

        # metrics
        self.metrics = met.summarize(
            config=self.config, equity_rows=self.equity_rows, orders=self.orders,
            fills=self.fills, proposals=self.proposals, risk_decisions=self.risk_decisions,
            positions=self.positions, md_counters=self.counters, calibration=self._calibration)
        for name, value in self.metrics.items():
            if isinstance(value, (int, float, str)):
                self.out_store.set_replay_metric(self.run_id, name, value, None)
            else:
                self.out_store.set_replay_metric(self.run_id, name, None, value)

    # ------------------------------------------------------------------ #
    def _build_report(self) -> dict:
        return {
            "replay_run_id": self.run_id, "status": self.status, "error": self.error,
            "config_hash": self.config_hash, "seed": self.seed,
            "episode": self.episode().record(),
            "event_count": len(self.events),
            "metrics": getattr(self, "metrics", {}),
            "calibration": getattr(self, "_calibration", {}),
            "counts": {"orders": len(self.orders), "fills": len(self.fills),
                       "proposals": len(self.proposals), "equity_points": len(self.equity_rows)},
            "no_live_orders": True,
        }


# --------------------------------------------------------------------------- #
def _json(obj) -> str:
    import json
    try:
        return json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def _s(x):
    return None if x is None else str(x)
