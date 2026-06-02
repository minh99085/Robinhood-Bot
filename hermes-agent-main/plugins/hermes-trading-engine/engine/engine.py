"""The paper-trading engine.

PAPER by default; paper/live MODE with a triple safeguard. NO real-exchange
execution adapter — LIVE is armed simulation.

Training pipeline: phases gate Grok's influence (OBSERVATION -> GROK_ASSIST ->
GROK_PRIMARY -> LIVE_READY), realistic fee/slippage fills, metrics, atomic paper
ledger, auto-reports.

Realism guards (so training metrics are trustworthy enough to gate real money):
  * Polymarket paper bets resolve PROBABILISTICALLY at the market's implied odds
    (a fair binary, EV ~ 0) — no free money / runaway wins.
  * Stake sizing uses a CAPPED bankroll (HTE_MAX_COMPOUND x starting balance) so
    paper equity cannot explode exponentially.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from . import training
from .brain import GrokBrain
from .calibration import Calibrator
from .config import Settings
from .features import N_FEATURES, OnlineLogistic, pulse_features
from .feeds import crypto, polymarket, stocks
from .fees import FeeModel
from .quant import markov, montecarlo, patterns
from .execution import OrderManagementSystem, PaperBroker
from .risk import MarketDataSnapshot, RiskContext, RiskEngine, RiskLimits
from .safety import AgentReadiness, CircuitBreaker, equity_max_drawdown, equity_sharpe
from .schemas import TradeProposal
from .storage import Store

_MAX_SAMPLES = 240
_SIGNAL_FEATURES = N_FEATURES + 1


class TradingEngine:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self._client = httpx.Client(timeout=8.0, headers={"User-Agent": "HermesTradingEngine/paper"})
        self.started_at = time.time()

        if self.store.get_meta("initialized") is None:
            self.store.set_meta("initialized", True)
            self.store.set_meta("starting_balance", self.s.starting_balance)

        self.autotrade = self.s.autotrade_enabled
        self.round_no = int(self.store.get_meta("round_no", 7000))
        self.max_compound = float(os.getenv("HTE_MAX_COMPOUND", "10"))
        self.pm_min_hold_s = float(os.getenv("HTE_PM_MIN_HOLD_S", "120"))

        # SAFETY: never initialize to LIVE on startup — always boot in PAPER.
        self.mode = "paper"
        self.circuit = CircuitBreaker(log_path=self.s.data_dir / "circuit_breaker.log")
        self._errors: deque = deque(maxlen=400)

        # Deterministic pre-trade risk gate. EVERY simulated order (pulse,
        # crypto, stock, Polymarket, arb) must clear this before it opens.
        self.risk = RiskEngine(RiskLimits.from_env(self.s.data_dir))
        self._risk_rejections: deque = deque(maxlen=100)
        self._risk_rejection_count = 0
        self._risk_approvals: deque = deque(maxlen=100)
        self._risk_approval_count = 0
        self._risk_decision_seq = 0  # monotonic id for risk-decision traceability
        self._last_data_ts = time.time()  # freshness anchor for the stale-data check

        # Phase 2: read-only Polymarket CLOB market-data feed (default OFF).
        # Set by app.py after construction when POLYMARKET_CLOB_ENABLED=1.
        self.market_data = None
        self._clob_enabled = os.getenv("POLYMARKET_CLOB_ENABLED", "0") not in ("0", "false", "False", "")
        self._clob_subscribe_trending = os.getenv(
            "POLYMARKET_CLOB_SUBSCRIBE_TRENDING", "1") not in ("0", "false", "False")

        # Phase 3: internal OMS + simulated PaperBroker. Every simulated open is
        # routed here (no direct fantasy trade insert). PAPER ONLY.
        self.oms = OrderManagementSystem(self.store, PaperBroker(),
                                         mode_provider=lambda: self.mode)
        self._paper_default_tif = (os.getenv("PAPER_DEFAULT_TIME_IN_FORCE", "IOC") or "IOC").strip().upper()
        self._last_recon = 0.0

        self.pulse: dict = {}
        self.regime: dict = {}
        self.mc: dict = {}
        self.patt: dict = {}
        self.prices: dict[str, float] = {}
        self.klines_cache: list[dict] = []
        self._last_position_eval = 0.0
        self._last_pm_eval = 0.0
        self._latency_ms = 0.0
        self._day_anchor = self._today_key()
        self._day_start_realized = self.store.realized_pnl()
        self._cur_obi = None

        self.calibrator = Calibrator(self.store)
        self.signal_logit = OnlineLogistic(_SIGNAL_FEATURES)
        self.signal_logit.load(self.store.get_meta("signal_logit"))
        self._feat_hist: deque = deque(maxlen=400)
        self._base_hist: deque = deque(maxlen=400)

        self.reporter = training.Reporter(self.s.data_dir)
        self._phase = training.phase_for(self.store.stats()["total"], False)
        self._rd: dict = {}
        self._metrics_cache: dict = {}
        self._metrics_ts = 0.0
        self._last_ledger = 0.0
        self._live_ready_seen = bool(self.store.get_meta("live_ready_seen"))

        self.brain = GrokBrain(self.s)
        self.brain.attach_context_provider(self._brain_context)
        self.brain.start()

        self._init_pulse()

    # ------------------------------------------------------------------ #
    @property
    def starting_balance(self) -> float:
        return float(self.store.get_meta("starting_balance", self.s.starting_balance))

    def _today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def record_error(self, msg: str = "") -> None:
        self._errors.append(time.time())

    def _errors_24h(self) -> int:
        cutoff = time.time() - 86400
        return sum(1 for ts in self._errors if ts >= cutoff)

    def unrealized_pnl(self) -> float:
        total = 0.0
        for t in self.store.open_trades():
            if t["market"] in ("pulse", "polymarket"):
                continue  # binary-resolved markets settle to realized, not marked here
            spot = self._mark_price(t)
            if spot is None:
                continue
            direction = 1 if t["side"] in ("BUY", "UP", "YES") else -1
            total += direction * (spot - t["price"]) * t["qty"]
        return total

    def equity(self) -> float:
        return self.starting_balance + self.store.realized_pnl() + self.unrealized_pnl()

    def _sizing_base(self) -> float:
        """Bankroll used for stake sizing — capped so paper equity can't explode."""
        return max(0.0, min(self.equity(), self.starting_balance * self.max_compound))

    def _mark_price(self, trade: dict):
        sym = trade["symbol"]
        if trade["market"] == "crypto":
            return self.prices.get(sym) or crypto.get_spot(sym, client=self._client)
        if trade["market"] == "stock":
            q = stocks.get_quote(sym, client=self._client)
            return q["price"] if q else None
        return None

    def _daily_loss_breached(self) -> bool:
        if self._today_key() != self._day_anchor:
            self._day_anchor = self._today_key()
            self._day_start_realized = self.store.realized_pnl()
        return self._day_pnl() <= -abs(self.s.daily_loss_limit_fraction) * self.equity()

    def _day_pnl(self) -> float:
        return self.store.realized_pnl() - self._day_start_realized

    # ------------------------------------------------------------------ #
    def readiness(self) -> dict:
        stats = self.store.stats()
        curve = self.store.equity_curve(2000)
        return AgentReadiness.evaluate(
            trades=stats["total"], win_rate=stats["win_rate"],
            sharpe=equity_sharpe(curve), max_dd=equity_max_drawdown(curve),
            errors_24h=self._errors_24h())

    def set_mode(self, target: str, confirmed: bool = False, reason: str = "") -> dict:
        target = (target or "").lower()
        if target == "paper":
            if self.mode != "paper":
                self.circuit._event("downgrade", reason or "switched to PAPER")
            self.mode = "paper"
            self.circuit.reset_session()
            return {"ok": True, "mode": self.mode}
        if target == "live":
            rd = self.readiness()
            if not rd["ready"]:
                return {"ok": False, "mode": self.mode, "reason": "readiness gate not met",
                        "missing": rd["missing"], "readiness": rd}
            if not confirmed:
                return {"ok": False, "mode": self.mode, "reason": "confirmation required"}
            self.circuit.reset_session()
            self.mode = "live"
            self.circuit._event("arm", "switched to LIVE (armed simulation; no real orders)")
            return {"ok": True, "mode": self.mode}
        return {"ok": False, "mode": self.mode, "reason": "unknown mode"}

    def _live(self) -> bool:
        return self.mode == "live"

    def _can_open(self) -> bool:
        # Polymarket-only PAPER training: the legacy crypto/stock pulse engine
        # never opens. Polymarket paper trades flow through engine.training
        # (scan -> rank -> probability -> edge -> RiskEngine -> PaperBroker).
        if getattr(self.s, "polymarket_only_mode", False):
            return False
        if not self.autotrade or self._daily_loss_breached():
            return False
        if self._live() and not self.circuit.trading_allowed():
            return False
        return True

    def _cap_frac(self, frac: float) -> float:
        return self.circuit.cap_stake_fraction(frac) if self._live() else frac

    # ------------------------------------------------------------------ #
    def _log_trade(self, event: str, record: dict) -> None:
        try:
            with open(self.s.data_dir / f"{self.mode}_trades.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": round(time.time(), 1), "event": event,
                                    "mode": self.mode, **record}, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Deterministic risk gate (mandatory for every simulated order)
    # ------------------------------------------------------------------ #
    def _risk_context(self, proposal: TradeProposal) -> RiskContext:
        opens = self.store.open_trades()
        total = sum(float(t.get("stake") or 0.0) for t in opens)
        market = sum(float(t.get("stake") or 0.0) for t in opens
                     if t.get("market") == proposal.market)
        dup = any(t.get("market") == proposal.market and t.get("symbol") == proposal.symbol
                  and t.get("side") == proposal.side for t in opens)
        ctx = RiskContext(
            equity=self.equity(), total_exposure=total, market_exposure=market,
            has_open_same_market_side=dup, open_orders=len(opens), day_pnl=self._day_pnl())
        # Attach live CLOB market-data freshness for tracked Polymarket markets.
        # When CLOB is disabled or the market isn't tracked, market_data stays
        # None and the RiskEngine skips those checks (Phase 1 behavior intact).
        if proposal.market == "polymarket" and self.market_data is not None and self._clob_enabled:
            try:
                fr = self.market_data.freshness_for_market(
                    proposal.symbol, max_spread=self.risk.limits.max_spread)
                if fr:
                    ctx.market_data = MarketDataSnapshot(**fr)
            except Exception:  # noqa: BLE001 — market-data must never break risk eval
                pass
        return ctx

    def assess_trade(self, proposal: TradeProposal):
        """Run the deterministic RiskEngine and record any rejection."""
        decision = self.risk.evaluate(proposal, self._risk_context(proposal))
        self._risk_decision_seq += 1
        decision_id = f"rd-{self._risk_decision_seq}"
        try:
            setattr(decision, "decision_id", decision_id)
        except Exception:  # noqa: BLE001 - decision may be a frozen/namedtuple
            pass
        if decision.approved:
            self._record_risk_approval(proposal, decision, decision_id)
        else:
            self._record_risk_rejection(proposal, decision, decision_id)
        return decision

    def assess_arb_proposal(self, opp: dict, size: float):
        """Build a TradeProposal for a cross-exchange arb leg and risk-check it."""
        proposal = TradeProposal(
            strategy="arb", market="arb", symbol=str(opp.get("symbol") or ""),
            side="BUY", notional=float(size or 0.0), price=opp.get("buyAsk"),
            edge_after_costs=float(opp.get("executionNetPct", 0.0) or 0.0) / 100.0,
            spread=0.0, data_age_s=float(opp.get("staleness_ms", 0.0) or 0.0) / 1000.0,
            ambiguity_score=0.0, allow_duplicate=True, mode=self.mode,
            rationale=f"arb {opp.get('buyExchange')}->{opp.get('sellExchange')}")
        return self.assess_trade(proposal)

    def market_data_status(self) -> dict:
        if self.market_data is None:
            return {"enabled": False, "status": {"source": "polymarket_clob", "status": "disabled"},
                    "assets": [], "recent_markets": []}
        try:
            return self.market_data.health()
        except Exception:  # noqa: BLE001
            return {"enabled": self._clob_enabled,
                    "status": {"source": "polymarket_clob", "status": "error"},
                    "assets": [], "recent_markets": []}

    def replay_summary(self) -> dict:
        try:
            runs = self.store.get_replay_runs(5)
            out = []
            for r in runs:
                m = self.store.get_replay_metrics(r["replay_run_id"])
                out.append({
                    "replay_run_id": r["replay_run_id"], "status": r.get("status"),
                    "policy": None, "ending_equity": m.get("ending_equity"),
                    "total_pnl": m.get("total_pnl"), "max_drawdown": m.get("max_drawdown"),
                    "fill_ratio": m.get("fill_ratio"),
                    "brier": (m.get("calibration") or {}).get("brier_score")
                    if isinstance(m.get("calibration"), dict) else None,
                })
            return {"recent_runs": out}
        except Exception:  # noqa: BLE001
            return {"recent_runs": []}

    def research_summary(self) -> dict:
        """Light read-only summary of the research engine for the dashboard.
        Research is OFF by default (RESEARCH_USE_IN_STRATEGY=0)."""
        import os as _os
        try:
            runs = self.store.get_research_runs(5)
            ests = self.store.get_probability_estimates(limit=5)
            return {
                "mode": (_os.getenv("RESEARCH_MODE") or "offline_cache").strip().lower(),
                "use_in_strategy": _os.getenv("RESEARCH_USE_IN_STRATEGY", "0")
                not in ("0", "false", "False", ""),
                "recent_runs": [
                    {"research_run_id": r.get("research_run_id"), "status": r.get("status"),
                     "market_id": r.get("market_id"), "ts_ms": r.get("ts_ms")} for r in runs],
                "recent_estimates": [
                    {"estimate_id": e.get("estimate_id"), "market_id": e.get("market_id"),
                     "p_ensemble": e.get("p_ensemble"), "confidence": e.get("confidence"),
                     "ambiguity_score": e.get("ambiguity_score"),
                     "evidence_score": e.get("evidence_score"),
                     "no_trade_reason": e.get("no_trade_reason")} for e in ests],
            }
        except Exception:  # noqa: BLE001
            return {"mode": "offline_cache", "use_in_strategy": False,
                    "recent_runs": [], "recent_estimates": []}

    def risk_status(self) -> dict:
        return {
            "kill_switch": self.risk.kill_switch_active(),
            "approvals_total": self._risk_approval_count,
            "rejections_total": self._risk_rejection_count,
            "decisions_total": self._risk_approval_count + self._risk_rejection_count,
            "recent_approvals": list(self._risk_approvals)[-12:][::-1],
            "recent_rejections": list(self._risk_rejections)[-12:][::-1],
            "limits": self.risk.limits.as_dict(),
        }

    def risk_decisions(self, limit: int = 25) -> dict:
        """Recent risk decisions (both approvals and rejections), newest first."""
        approvals = list(self._risk_approvals)[-limit:][::-1]
        rejections = list(self._risk_rejections)[-limit:][::-1]
        merged = sorted(approvals + rejections, key=lambda d: d.get("ts", 0), reverse=True)
        return {
            "approvals_total": self._risk_approval_count,
            "rejections_total": self._risk_rejection_count,
            "approvals": approvals,
            "rejections": rejections,
            "decisions": merged[:limit],
        }

    def _record_risk_approval(self, proposal: TradeProposal, decision, decision_id: str) -> None:
        self._risk_approval_count += 1
        rec = {
            "ts": round(time.time(), 1), "mode": self.mode, "decision": "approved",
            "risk_decision_id": decision_id,
            "market": proposal.market, "symbol": proposal.symbol, "side": proposal.side,
            "notional": round(proposal.notional, 2),
            "code": getattr(decision, "code", "OK"), "strategy": proposal.strategy,
        }
        self._risk_approvals.append(rec)

    def _record_risk_rejection(self, proposal: TradeProposal, decision,
                               decision_id=None) -> None:
        self._risk_rejection_count += 1
        rec = {
            "ts": round(time.time(), 1), "mode": self.mode, "decision": "rejected",
            "risk_decision_id": decision_id,
            "market": proposal.market, "symbol": proposal.symbol, "side": proposal.side,
            "notional": round(proposal.notional, 2), "code": decision.code,
            "reason": "; ".join(decision.reasons)[:240], "strategy": proposal.strategy,
        }
        self._risk_rejections.append(rec)
        try:
            with open(self.s.data_dir / "risk_rejections.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def _open_trade(self, **kw) -> int:
        market, side = kw.get("market"), kw.get("side")
        # Risk hints (popped so they never reach the trade store).
        risk_edge = float(kw.pop("risk_edge", 0.0) or 0.0)
        risk_spread = float(kw.pop("risk_spread", 0.0) or 0.0)
        risk_ambiguity = float(kw.pop("risk_ambiguity", 0.0) or 0.0)
        allow_duplicate = bool(kw.pop("allow_duplicate", False))

        proposal = TradeProposal(
            strategy=str((kw.get("meta") or {}).get("strategy", market or "")),
            market=str(market or "crypto"), symbol=str(kw.get("symbol") or ""),
            side=str(side or "BUY"), notional=float(kw.get("stake") or 0.0),
            price=kw.get("price"), edge_after_costs=risk_edge, spread=risk_spread,
            ambiguity_score=max(0.0, min(1.0, risk_ambiguity)),
            data_age_s=max(0.0, time.time() - self._last_data_ts),
            allow_duplicate=allow_duplicate, mode=self.mode,
            rationale=str(kw.get("rationale") or ""))
        decision = self.assess_trade(proposal)
        if not decision.approved:
            return 0  # rejected by RiskEngine — no simulated order is opened

        # Phase 3: route every open through the OMS + PaperBroker. The broker
        # decides the fill from local CLOB state where available, else a
        # conservative reference-price simulation. No direct fantasy insert.
        result = self._submit_via_oms(kw, proposal, decision)
        if result is None or float(result.filled_quantity) <= 0:
            if result is not None and result.reject_reason:
                self._record_broker_rejection(kw, result)
            return 0

        filled = float(result.filled_quantity)
        avg = float(result.avg_fill_price) if result.avg_fill_price is not None else None
        req_qty = float(kw.get("qty") or filled or 1.0)
        fill_frac = (filled / req_qty) if req_qty > 0 else 1.0

        legacy = dict(kw)
        legacy["qty"] = round(filled, 8)
        if market != "pulse" and avg:
            legacy["price"] = round(avg, 6)
        if legacy.get("stake") is not None:
            legacy["stake"] = round(float(legacy["stake"]) * fill_frac, 2)
        meta = dict(legacy.get("meta") or {})
        meta["mode"] = self.mode
        meta["client_order_id"] = result.order.client_order_id
        meta["fill_liquidity"] = result.fills[0].liquidity_flag if result.fills else None
        legacy["meta"] = meta
        tid = self.store.add_trade(**legacy)
        if self._live():
            self.circuit.record_order()
        self._log_trade("open", {"id": tid, "market": market, "symbol": kw.get("symbol"),
                                 "side": side, "price": legacy.get("price"),
                                 "stake": legacy.get("stake"),
                                 "client_order_id": result.order.client_order_id,
                                 "filled_qty": filled})
        return tid

    # ------------------------------------------------------------------ #
    def _submit_via_oms(self, kw: dict, proposal, decision):
        """Build an OrderRequest and submit it through the OMS/PaperBroker."""
        from decimal import Decimal

        from .execution import (
            OrderRequest,
            OrderSide,
            OrderType,
            new_client_order_id,
        )

        market = str(kw.get("market") or "")
        symbol = str(kw.get("symbol") or "")
        side = (OrderSide.BUY if str(kw.get("side") or "").upper() in ("BUY", "UP", "YES")
                else OrderSide.SELL)
        price = kw.get("price")
        qty = kw.get("qty") or 1.0
        venue_kind = "pm" if market == "polymarket" else "legacy"
        asset_id = None
        book = None
        reference_price = price
        if market == "polymarket" and self.market_data is not None and self._clob_enabled:
            try:
                asset_id = self.market_data.asset_for_market(symbol)
                if asset_id:
                    book = self.market_data.get_orderbook(asset_id)
            except Exception:  # noqa: BLE001
                book = None
        order = OrderRequest(
            client_order_id=new_client_order_id(),
            venue=market, market_id=symbol, asset_id=asset_id,
            outcome=str(kw.get("side") or ""), side=side,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=Decimal(str(price)) if price is not None else None,
            quantity=Decimal(str(qty)), time_in_force=self._paper_default_tif,
            source=str((kw.get("meta") or {}).get("strategy", market)),
            proposal_id=getattr(proposal, "proposal_id", None), venue_kind=venue_kind)
        return self.oms.submit(order, decision, book=book, reference_price=reference_price)

    def _record_broker_rejection(self, kw: dict, result) -> None:
        self._risk_rejection_count += 1
        rec = {
            "ts": round(time.time(), 1), "mode": self.mode, "market": kw.get("market"),
            "symbol": kw.get("symbol"), "side": kw.get("side"),
            "notional": round(float(kw.get("stake") or 0.0), 2),
            "code": result.reject_reason or "rejected",
            "reason": f"paper broker: {result.reject_reason}",
            "strategy": (kw.get("meta") or {}).get("strategy", ""),
        }
        self._risk_rejections.append(rec)
        try:
            with open(self.s.data_dir / "risk_rejections.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    def _oms_price_provider(self, venue: str, market_id: str, asset_id):
        from decimal import Decimal
        try:
            if venue == "polymarket" and asset_id and self.market_data is not None:
                ob = self.market_data.get_orderbook(asset_id)
                if ob is not None and ob.midpoint is not None:
                    return ob.midpoint
            p = self.prices.get(market_id)
            if p is not None:
                return Decimal(str(p))
        except Exception:  # noqa: BLE001
            return None
        return None

    def _oms_book_provider(self, order):
        if getattr(order, "venue", "") == "polymarket" and getattr(order, "asset_id", None) \
                and self.market_data is not None:
            try:
                return self.market_data.get_orderbook(order.asset_id)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _maybe_reconcile(self) -> None:
        now = time.time()
        if now - self._last_recon < 30:
            return
        self._last_recon = now
        try:
            if self._clob_enabled and self.market_data is not None:
                self.oms.process_resting(self._oms_book_provider)
            self.oms.reconcile(self._oms_price_provider)
        except Exception as exc:  # noqa: BLE001
            self.record_error(f"reconcile: {exc}")

    def oms_summary(self) -> dict:
        try:
            st = self.oms.status()
            positions = self.oms.get_positions()  # already deduped per logical key
            active = [p for p in positions
                      if str(p.get("quantity", "0")) not in ("0", "0.0", "", "0E-8", None)]
            try:
                raw_snaps = self.store.position_snapshot_count()
            except Exception:  # noqa: BLE001
                raw_snaps = len(positions)
            return {
                "open_orders": st.get("open_orders", 0), "degraded": st.get("degraded", False),
                "open_order_list": self.oms.get_open_orders()[:10],
                "recent_fills": self.oms.get_fills(10),
                "positions": active[:10],
                "position_count": len(positions),
                "active_position_count": len(active),
                "unique_position_count": len(positions),
                "duplicate_snapshot_count": max(0, raw_snaps - len(positions)),
                "reconciliation": st.get("last_reconciliation", {}),
            }
        except Exception:  # noqa: BLE001
            return {"open_orders": 0, "degraded": False, "open_order_list": [],
                    "recent_fills": [], "positions": [], "position_count": 0,
                    "active_position_count": 0, "unique_position_count": 0,
                    "duplicate_snapshot_count": 0, "reconciliation": {}}

    def accounting_summary(self) -> dict:
        """Single reconciled view of trade/order/fill/position counts + P&L.

        Counts are defined precisely so the dashboard panels reconcile:
          trade_count           = completed/closed simulated trade decisions
          order_count           = OMS orders ever recorded
          fill_count            = execution fills
          active_position_count = unique open (non-zero) positions
        """
        try:
            stats = self.store.stats()
        except Exception:  # noqa: BLE001
            stats = {"total": 0}
        try:
            orders = self.oms.get_recent_orders(100000)
        except Exception:  # noqa: BLE001
            orders = []
        try:
            fills = self.oms.get_fills(100000)
        except Exception:  # noqa: BLE001
            fills = []
        oms = self.oms_summary()

        def _fee(f):
            try:
                return float(f.get("fee", 0) or 0)
            except Exception:  # noqa: BLE001
                return 0.0
        return {
            "trade_count": int(stats.get("total", 0)),
            "order_count": len(orders),
            "fill_count": len(fills),
            "active_position_count": oms.get("active_position_count", 0),
            "unique_position_count": oms.get("unique_position_count", 0),
            "duplicate_snapshot_count": oms.get("duplicate_snapshot_count", 0),
            "equity": round(self.equity(), 2),
            "realized_pnl": round(self.store.realized_pnl(), 2),
            "unrealized_pnl": round(self.unrealized_pnl(), 2),
            "fees": round(sum(_fee(f) for f in fills), 6),
            "starting_balance": self.starting_balance,
            "mode": self.mode, "simulated": True,
        }

    def _close_trade(self, trade_id: int, status: str, pnl: float, extra_meta: dict | None = None) -> None:
        meta = dict(extra_meta or {})
        meta["mode"] = self.mode
        meta["closed_ts"] = round(time.time(), 1)
        self.store.update_trade(trade_id, status=status, pnl=round(pnl, 2), meta=meta)
        if self._live():
            self.circuit.record_result(pnl)
        self._log_trade("close", {"id": trade_id, "status": status, "pnl": round(pnl, 2)})

    # ------------------------------------------------------------------ #
    def _feature_active(self) -> bool:
        if not self.signal_logit.ready() or len(self._feat_hist) < 60:
            return False
        fb, bb = self._brier(self._feat_hist), self._brier(self._base_hist)
        return fb is not None and bb is not None and fb < bb - 0.001

    @staticmethod
    def _brier(hist) -> float | None:
        if len(hist) < 20:
            return None
        return round(sum((p - o) ** 2 for p, o in hist) / len(hist), 4)

    # ------------------------------------------------------------------ #
    def _track_record(self) -> dict:
        recent = self.store.recent_trades(40)
        settled = [t for t in recent if t["market"] == "pulse" and t["status"] in ("won", "lost")]
        outcomes = [{"side": t["side"], "result": t["status"], "pnl": round(t["pnl"], 2)} for t in settled[:8]]
        wins = sum(1 for t in settled if t["status"] == "won")
        streak = 0
        for t in settled:
            if t["status"] == settled[0]["status"]:
                streak += 1
            else:
                break
        return {
            "pulse_win_rate": round(wins / len(settled), 3) if settled else None,
            "pulse_rounds_settled": len(settled), "recent_pulse_outcomes": outcomes,
            "current_streak": f"{streak} x {settled[0]['status']}" if settled else "none",
            "training_phase": self._phase[1],
            "today_realized_pnl": round(self._day_pnl(), 2),
            "model_calibration_brier": self.calibrator.stats().get("brier_cal"),
            "order_book_imbalance": self._cur_obi, "daily_loss_breached": self._daily_loss_breached(),
        }

    def _brain_context(self) -> dict | None:
        if not self.regime:
            return None
        p = self.pulse or {}
        return {
            "regime": self.regime, "montecarlo": self.mc, "patterns": self.patt,
            "candles": self.klines_cache[-30:] if self.klines_cache else [],
            "pulse": {"start_price": p.get("start_price"), "current_price": p.get("current_price"),
                      "seconds_left": max(0, int(p.get("end_ts", time.time()) - time.time())),
                      "market_implied_p_up": p.get("market_up")},
            "track_record": self._track_record(),
        }

    # ------------------------------------------------------------------ #
    # pulse market
    # ------------------------------------------------------------------ #
    def _init_pulse(self) -> None:
        spot = crypto.get_spot(self.s.pulse_symbol, client=self._client) or 0.0
        self.round_no += 1
        self.store.set_meta("round_no", self.round_no)
        now = time.time()
        self.pulse = {
            "round": self.round_no, "symbol": self.s.pulse_symbol,
            "start_price": round(spot, 2), "current_price": round(spot, 2),
            "start_ts": now, "end_ts": now + self.s.pulse_round_seconds,
            "crowd_bias": random.gauss(0.0, 0.05), "market_up": 0.5,
            "up_price": round(0.5 + self.s.pulse_vig / 2, 2),
            "down_price": round(0.5 + self.s.pulse_vig / 2, 2),
            "p_model": 0.5, "p_cal": 0.5, "p_feat": None, "p_feat_raw": 0.5,
            "feats": [0.0] * _SIGNAL_FEATURES,
            "ev_up": 0.0, "ev_down": 0.0, "edge": 0.0, "kelly": 0.0, "stake_frac": 0.0,
            "bet": None, "samples": [round(spot, 2)], "history": [],
        }

    def _update_signal_features(self) -> None:
        p = self.pulse
        feats = pulse_features(self.klines_cache) if self.klines_cache else [0.0] * N_FEATURES
        obi = crypto.order_book_imbalance(self.s.pulse_symbol, client=self._client)
        self._cur_obi = obi
        feats6 = list(feats) + [obi if obi is not None else 0.0]
        p["feats"] = feats6
        pf = self.signal_logit.predict_proba(feats6) if self.signal_logit.ready() else None
        p["p_feat_raw"] = pf if pf is not None else 0.5
        p["p_feat"] = round(pf, 3) if pf is not None else None

    def _quant_prob(self) -> float:
        m, mc = self.regime.get("p_up", 0.5), self.mc.get("p_up", 0.5)
        quant = 0.5 * m + 0.5 * mc
        pf = self.pulse.get("p_feat")
        if pf is not None and self._feature_active():
            quant = 0.7 * quant + 0.3 * pf
        return max(0.02, min(0.98, quant))

    def _pulse_prob_up(self) -> float:
        quant = self._quant_prob()
        g = self.brain.prob_up()
        phase_n = self._phase[0]
        if g is None or phase_n == 1:
            return quant
        if phase_n == 2:
            if (g > 0.5) == (quant > 0.5):
                blended = 0.55 * g + 0.45 * quant
            else:
                blended = quant
        else:
            blended = 0.7 * g + 0.3 * quant
        return max(0.02, min(0.98, blended))

    def _update_market_quote(self) -> None:
        p = self.pulse
        beat = p["start_price"] or 1.0
        drift = (p["current_price"] - beat) / max(beat * 0.0008, 1e-9)
        m_up = min(0.9, max(0.1, 0.5 + 0.18 * math.tanh(drift) + p.get("crowd_bias", 0.0)))
        p["market_up"] = round(m_up, 3)
        vig = self.s.pulse_vig
        p["up_price"] = round(min(0.98, max(0.02, m_up + vig / 2)), 2)
        p["down_price"] = round(min(0.98, max(0.02, (1.0 - m_up) + vig / 2)), 2)

    @staticmethod
    def _kelly(pw: float, price: float) -> float:
        b = (1.0 / price) - 1.0
        return max(0.0, pw - (1.0 - pw) / b) if b > 0 else 0.0

    def _maybe_place_pulse_bet(self) -> None:
        p = self.pulse
        p_raw = self._pulse_prob_up()
        p_cal = self.calibrator.calibrate(p_raw)
        p["p_model"], p["p_cal"] = round(p_raw, 3), round(p_cal, 3)
        up_price, down_price = p["up_price"], p["down_price"]
        ev_up = p_cal / up_price - 1.0
        ev_down = (1.0 - p_cal) / down_price - 1.0
        p["ev_up"], p["ev_down"] = round(ev_up, 3), round(ev_down, 3)

        if p.get("bet") is not None or not self._can_open():
            return
        if ev_up >= ev_down and ev_up > self.s.ev_threshold:
            side, price, pw, ev = "UP", up_price, p_cal, ev_up
        elif ev_down > self.s.ev_threshold:
            side, price, pw, ev = "DOWN", down_price, 1.0 - p_cal, ev_down
        else:
            return
        kelly_full = self._kelly(pw, price)
        stake_frac = self._cap_frac(min(kelly_full * self.s.kelly_fraction, self.s.max_stake_fraction))
        p["edge"], p["kelly"], p["stake_frac"] = round(ev, 3), round(kelly_full, 3), round(stake_frac, 4)
        if stake_frac <= 0:
            return
        stake = round(stake_frac * self._sizing_base(), 2)
        if stake <= 0:
            return
        quant0 = self._quant_prob()
        g = self.brain.prob_up()
        markov_dir = "up" if quant0 >= 0.5 else "down"
        grok_dir = "up" if (g is not None and g > 0.5) else "down" if (g is not None and g < 0.5) else "none"
        disagree = grok_dir in ("up", "down") and grok_dir != markov_dir
        adv = self.brain.latest()
        gnote = f" | Grok:{adv.get('action')}({adv.get('confidence')})" if adv.get("fresh") else ""
        rationale = (f"[{self._phase[1]}] p_cal={round(pw,3)} vs price={price} -> EV={round(ev,3)}; "
                     f"Kelly={round(kelly_full,3)}*{self.s.kelly_fraction}={round(stake_frac,4)} "
                     f"markov={markov_dir} grok={grok_dir}{gnote}")
        vig_spread = max(0.0, up_price + down_price - 1.0)
        tid = self._open_trade(market="pulse", symbol=self.s.pulse_symbol, side=side, qty=1.0,
                               price=price, stake=stake, status="open", rationale=rationale,
                               risk_edge=round(ev, 4), risk_spread=round(vig_spread, 4),
                               meta={"strategy": "pulse", "round": p["round"], "ev": round(ev, 3),
                                     "phase": self._phase[0], "grok_dir": grok_dir,
                                     "markov_dir": markov_dir, "disagree": disagree})
        if not tid:  # RiskEngine rejected the bet — leave the round unbet
            return
        p["bet"] = {"side": side, "entry_price": price, "stake": stake, "trade_id": tid,
                    "ev": round(ev, 3), "stake_frac": round(stake_frac, 4),
                    "grok_dir": grok_dir, "markov_dir": markov_dir, "disagree": disagree}

    def _settle_pulse(self) -> None:
        p = self.pulse
        result = "UP" if p["current_price"] > p["start_price"] else "DOWN"
        y = 1 if result == "UP" else 0
        self.calibrator.record(p.get("p_model", 0.5), y)
        self._base_hist.append((p.get("p_model", 0.5), y))
        self._feat_hist.append((p.get("p_feat_raw", 0.5), y))
        self.signal_logit.observe(p.get("feats", [0.0] * _SIGNAL_FEATURES), y)
        if self.round_no % 10 == 0:
            self.store.set_meta("signal_logit", self.signal_logit.to_dict())

        bet = p.get("bet")
        outcome = None
        if bet:
            won = bet["side"] == result
            pnl = bet["stake"] * (1.0 / max(bet["entry_price"], 0.01) - 1.0) if won else -bet["stake"]
            self._close_trade(bet["trade_id"], "won" if won else "lost", pnl,
                              {"result": result, "ev": bet.get("ev"), "grok_dir": bet.get("grok_dir"),
                               "markov_dir": bet.get("markov_dir"), "disagree": bet.get("disagree")})
            outcome = {"side": bet["side"], "result": result, "pnl": round(pnl, 2), "won": won}
            if bet.get("disagree"):
                res_dir = "up" if result == "UP" else "down"
                self._append_report("disagreements.jsonl", {
                    "ts": round(time.time(), 1), "round": p["round"], "phase": self._phase[0],
                    "grok_dir": bet["grok_dir"], "markov_dir": bet["markov_dir"], "result": res_dir,
                    "grok_right": bet["grok_dir"] == res_dir, "markov_right": bet["markov_dir"] == res_dir})
        p["history"].append({"round": p["round"], "start": p["start_price"],
                             "close": p["current_price"], "result": result, "outcome": outcome})
        p["history"] = p["history"][-40:]

    def _append_report(self, filename: str, rec: dict) -> None:
        try:
            with open(self.reporter.reports_dir / filename, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass

    def _update_pulse(self) -> None:
        p = self.pulse
        spot = self.prices.get(self.s.pulse_symbol)
        if spot:
            p["current_price"] = round(spot, 2)
            samples = p.setdefault("samples", [])
            samples.append(round(spot, 2))
            if len(samples) > _MAX_SAMPLES:
                del samples[: len(samples) - _MAX_SAMPLES]
        self._update_signal_features()
        self._update_market_quote()
        self._maybe_place_pulse_bet()
        if time.time() >= p["end_ts"]:
            self._settle_pulse()
            hist = p["history"]
            self._init_pulse()
            self.pulse["history"] = hist

    # ------------------------------------------------------------------ #
    def _update_quant(self) -> None:
        t0 = time.time()
        candles = crypto.get_klines(self.s.pulse_symbol, interval="1m",
                                    limit=self.s.markov_lookback, client=self._client)
        self._latency_ms = round((time.time() - t0) * 1000, 1)
        if self._live():
            self.circuit.record_api(bool(candles))
        if candles:
            self._last_data_ts = time.time()
            self.klines_cache = candles
            closes = [c["c"] for c in candles]
            self.regime = markov.fit(closes, lookback=self.s.markov_lookback)
            self.mc = montecarlo.simulate(closes, horizon_steps=60, paths=self.s.montecarlo_paths)
            self.patt = patterns.scan(candles)

    def _refresh_prices(self) -> None:
        got = False
        for sym in self.s.crypto_symbols:
            sp = crypto.get_spot(sym, client=self._client)
            if sp:
                self.prices[sym] = sp
                got = True
        if got:
            self._last_data_ts = time.time()
        if self._live():
            self.circuit.record_api(got)

    def _eval_positions(self) -> None:
        now = time.time()
        if now - self._last_position_eval < 20:
            return
        self._last_position_eval = now
        if not self._can_open():
            return
        bias = self.patt.get("bias", "neutral")
        prob = self.regime.get("p_up", 0.5)
        for sym in self.s.crypto_symbols:
            spot = self.prices.get(sym)
            if spot:
                self._manage_position("crypto", sym, spot, prob, bias)
        for sym in self.s.stock_symbols:
            q = stocks.get_quote(sym, client=self._client)
            if not q:
                continue
            chg = q["change_pct"]
            sym_bias = "bullish" if chg > 0.3 else "bearish" if chg < -0.3 else "neutral"
            self._manage_position("stock", sym, q["price"], 0.5 + chg / 100.0, sym_bias)
        if now - self._last_pm_eval > 30:
            self._last_pm_eval = now
            self._eval_polymarket()

    def _manage_position(self, market, symbol, spot, prob, bias) -> None:
        open_for = [t for t in self.store.open_trades(market) if t["symbol"] == symbol]
        if open_for:
            t = open_for[0]
            move = (spot - t["price"]) / t["price"]
            ret = (1 if t["side"] == "BUY" else -1) * move
            flip = (t["side"] == "BUY" and bias == "bearish") or (t["side"] == "SELL" and bias == "bullish")
            if ret >= 0.015 or ret <= -0.01 or flip:
                pnl = ret * t["stake"] - FeeModel.round_trip_cost(t["stake"], market)
                self._close_trade(t["id"], "closed", pnl)
            return
        if bias == "bullish" and prob > 0.5 + self.s.min_edge:
            side = "BUY"
        elif bias == "bearish" and prob < 0.5 - self.s.min_edge:
            side = "SELL"
        else:
            return
        open_notional = sum(t["stake"] for t in self.store.open_trades()
                            if t["market"] in ("crypto", "stock", "polymarket"))
        if open_notional >= self.s.max_exposure_fraction * self._sizing_base():
            return
        stake = round(self._cap_frac(self.s.max_stake_fraction) * self._sizing_base(), 2)
        self._open_trade(market=market, symbol=symbol, side=side, qty=round(stake / spot, 6),
                         price=round(spot, 4), stake=stake, status="open",
                         risk_edge=round(abs(prob - 0.5), 4),
                         rationale=f"{bias} bias, prob={round(prob,3)}",
                         meta={"strategy": f"{market}_momentum"})

    def _eval_polymarket(self) -> None:
        markets = polymarket.get_trending_markets(limit=6, client=self._client)
        # Drive the read-only CLOB subscription from the same trending markets
        # we paper-trade, so risk freshness lines up with the bets we place.
        if self._clob_enabled and self._clob_subscribe_trending and self.market_data is not None:
            try:
                self.market_data.ensure_subscribed(polymarket.clob_asset_map(markets))
            except Exception:  # noqa: BLE001
                pass
        for m in markets:
            if m["yes_price"] is not None:
                self.prices[f"pm:{m['id']}"] = m["yes_price"]
        # resolve open paper bets PROBABILISTICALLY at the implied odds (fair binary)
        now = time.time()
        for t in self.store.open_trades("polymarket"):
            if now - (t.get("ts") or now) < self.pm_min_hold_s:
                continue
            p_win = min(0.97, max(0.03, t["price"]))   # the side's price = its implied win prob
            won = random.random() < p_win
            pnl = t["stake"] * (1.0 / max(t["price"], 0.01) - 1.0) if won else -t["stake"]
            self._close_trade(t["id"], "won" if won else "lost", round(pnl, 2), {"resolved": True})
        # open at most a couple of new fair-priced paper bets
        open_pm = self.store.open_trades("polymarket")
        if len(open_pm) >= 2 or not self._can_open():
            return
        for m in markets:
            yp = m["yes_price"]
            if yp is None or [t for t in open_pm if t["symbol"] == m["id"]]:
                continue
            if yp >= 0.65 or yp <= 0.35:
                side = "YES" if yp >= 0.65 else "NO"
                price = yp if side == "YES" else (1 - yp)
                stake = round(self._cap_frac(self.s.max_stake_fraction) * self._sizing_base(), 2)
                self._open_trade(market="polymarket", symbol=m["id"], side=side,
                                 qty=round(stake / max(price, 0.01), 2), price=round(price, 3),
                                 stake=stake, status="open",
                                 risk_edge=round(abs(yp - 0.5), 4),
                                 risk_ambiguity=round(1.0 - abs(2.0 * yp - 1.0), 4),
                                 rationale=f"{m['question'][:80]} @ {price}",
                                 meta={"strategy": "polymarket"})
                break

    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        self._refresh_prices()
        self._update_quant()
        self._rd = self.readiness()
        new_phase = training.phase_for(self.store.stats()["total"], self._rd["ready"])
        if new_phase[0] != self._phase[0]:
            self.reporter.write_phase_summary(
                new_phase[0], {"entered": new_phase[1], **self._training_metrics(force=True)})
        self._phase = new_phase
        self._update_pulse()
        self._eval_positions()
        if self._live() and self.circuit.daily_loss_breached(self._day_pnl(), self.starting_balance):
            self.set_mode("paper", reason=f"circuit: daily loss {round(self._day_pnl(),2)}")
        self._maybe_report()
        self._persist_ledger()
        self._maybe_reconcile()
        self.store.snapshot_equity(round(self.equity(), 2),
                                   round(self.store.realized_pnl(), 2),
                                   round(self.unrealized_pnl(), 2))

    def set_autotrade(self, enabled: bool) -> None:
        self.autotrade = enabled

    def reset(self) -> None:
        with self.store._conn:
            self.store._conn.execute("DELETE FROM trades")
            self.store._conn.execute("DELETE FROM equity")
        self._day_start_realized = 0.0
        self._metrics_cache = {}
        self._init_pulse()

    # ------------------------------------------------------------------ #
    def _training_metrics(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._metrics_cache and now - self._metrics_ts < 60:
            return self._metrics_cache
        m = training.compute_metrics(self.store.recent_trades(1000), self.store.equity_curve(2000))
        self._metrics_cache, self._metrics_ts = m, now
        return m

    def _maybe_report(self) -> None:
        now = time.time()
        last_daily = float(self.store.get_meta("last_daily_report", 0) or 0)
        if now - last_daily > 86400:
            self.reporter.write_daily({**self._training_metrics(force=True),
                                       "mode": self.mode, "equity": round(self.equity(), 2),
                                       "phase": self._phase[1]})
            self.store.set_meta("last_daily_report", now)
        rd = self._rd or self.readiness()
        if rd.get("ready") and not self._live_ready_seen:
            self._live_ready_seen = True
            self.store.set_meta("live_ready_seen", True)
            self.reporter.write_phase_summary(4, {**self._training_metrics(force=True), "event": "LIVE_READY"})

    def _persist_ledger(self) -> None:
        now = time.time()
        if now - self._last_ledger < 5:
            return
        self._last_ledger = now
        opens = self.store.open_trades()
        self.reporter.persist_ledger({
            "ts": round(now, 1), "mode": self.mode, "phase": self._phase[1],
            "starting_balance": self.starting_balance, "equity": round(self.equity(), 2),
            "realized": round(self.store.realized_pnl(), 2), "unrealized": round(self.unrealized_pnl(), 2),
            "total_pnl": round(self.equity() - self.starting_balance, 2),
            "closed_count": self.store.stats()["total"],
            "open_positions": [{"market": t["market"], "symbol": t["symbol"], "side": t["side"],
                                "stake": t["stake"], "price": t["price"]} for t in opens],
        })

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        realized = round(self.store.realized_pnl(), 2)
        unrealized = round(self.unrealized_pnl(), 2)
        equity = round(self.equity(), 2)
        stats = self.store.stats()
        curve = self.store.equity_curve(300)
        rd = self._rd or self.readiness()

        pulse = dict(self.pulse)
        pulse["seconds_left"] = max(0, int(pulse["end_ts"] - time.time()))
        pulse["delta"] = round(pulse["current_price"] - pulse["start_price"], 2)

        return {
            "engine_name": self.s.engine_name, "wallet_label": self.s.wallet_label,
            "mode": self.mode, "autotrade": self.autotrade,
            "daily_loss_breached": self._daily_loss_breached(),
            "now_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"), "round": self.round_no,
            "portfolio": {
                "equity": equity, "starting_balance": self.starting_balance,
                "realized": realized, "unrealized": unrealized,
                "total_pnl": round(equity - self.starting_balance, 2),
                "win_rate": stats["win_rate"], "trades": stats["total"],
                "wins": stats["wins"], "losses": stats["losses"], "sharpe": self._sharpe(curve),
            },
            "latency_ms": self._latency_ms, "uptime_seconds": int(time.time() - self.started_at),
            "pulse": pulse, "regime": self.regime, "montecarlo": self.mc, "patterns": self.patt,
            "brain": self.brain.status(), "calibration": self.calibrator.stats(),
            "signal": {
                "obi": self._cur_obi, "feat_brier": self._brier(self._feat_hist),
                "base_brier": self._brier(self._base_hist), "active": self._feature_active(),
                "ready": self.signal_logit.ready(), "samples": len(self._feat_hist),
            },
            "readiness": rd, "circuit": self.circuit.status(), "risk": self.risk_status(),
            "market_data": self.market_data_status(), "oms": self.oms_summary(),
            "accounting": self.accounting_summary(),
            "replay": self.replay_summary(),
            "research": self.research_summary(),
            "training": {
                "phase": self._phase[0], "phase_name": self._phase[1], "phase_desc": self._phase[2],
                "trades": stats["total"], "next_phase_at": training.next_phase_at(stats["total"]),
                "metrics": self._training_metrics(),
                "live_ready": rd.get("ready", False),
                "notification": ("Agent has passed all readiness checks. You may now enable live trading."
                                 if rd.get("ready") else None),
            },
            "prices": {k: round(v, 2) for k, v in self.prices.items()},
            "equity_curve": curve,
            "open_trades": self.store.open_trades(), "recent_trades": self.store.recent_trades(40),
        }

    @staticmethod
    def _sharpe(curve: list[dict]) -> float:
        import numpy as np
        if len(curve) < 3:
            return 0.0
        eq = np.array([c["equity"] for c in curve], dtype=float)
        rets = np.diff(eq) / eq[:-1]
        if rets.std() == 0:
            return 0.0
        return round(float(rets.mean() / rets.std() * (len(rets) ** 0.5)), 2)
