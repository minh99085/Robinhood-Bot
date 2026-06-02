"""PolymarketTrainingEngine (v2) — Polymarket-only PAPER training orchestrator.

Flow per tick:

  scan many markets -> filter -> rank -> subscribe top live-watch assets ->
  build probability stack -> compute TRUE net edge after costs + uncertainty ->
  apply no-trade rules -> RiskEngine -> OMS/PaperBroker -> track fill/markout/PnL
  -> learn from every decision -> compare against baselines -> report.

Modes: ``disabled`` (loop off), ``observe_only`` (evaluate + record, NO trades —
the default), ``paper_train`` (simulated paper trades; still NO real orders).

SAFETY (hard): PAPER only. No real orders, no Micro Live, no production, no
wallet/private-key signing, no live-submit route. Every fill links proposal_id,
risk_decision_id, order_id, fill_id. Grok is research-only and can never place,
cancel, approve, arm, scale, or size an order. Fail closed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.markets import universe_manager as um
from engine.campaigns import signal_models as sm

from .baselines import BaselineComparator
from .bregman_execution import BregmanArbitrageEngine, CertifiedBregmanOpportunity
from .bregman_grouping import group_markets
from .candidate_ranker import CandidateRanker
from .config import (TrainingConfig, FORBIDDEN_LIVE_FLAGS, _envb, _envf, _envi)
from .diagnostics import build_record
from .edge_engine import EdgeEngine
from .feedback_loop import FeedbackLoop
from .market_scanner import MarketScanner
from .metrics import ScanMetrics
from .online_learner import OnlineLearner
from .paper_policy import PaperPolicy, TradeProposal
from .portfolio import (PortfolioLimits, PortfolioPosition, PortfolioRiskManager,
                        PortfolioState, bregman_bundle_size, max_drawdown)
from .probability_stack import ProbabilityStack, market_mid
from .signal_resolver import SignalResolver
from .subscription_manager import SubscriptionManager

# Back-compat: PaperPolicy.evaluate_edge re-exports EdgeResult.
from .paper_policy import EdgeResult  # noqa: F401

PolymarketTrainingEngine = None  # set to PolymarketPaperTrainer at end (alias)


# ---------------------------------------------------------------------------
# Deterministic RiskEngine gate (paper limits) + paper broker
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    proposal_id: str
    approved: bool
    code: str
    reasons: list = field(default_factory=list)
    risk_decision_id: str = ""
    adjusted_notional: Optional[float] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class TrainingRiskGate:
    """Deterministic RiskEngine for paper training. No order opens without an
    APPROVED decision here (mirrors the engine-wide fail-closed risk rule)."""

    def __init__(self, cfg: TrainingConfig):
        self.cfg = cfg
        self.approvals = 0
        self.rejections = 0
        self.recent: list = []

    def evaluate(self, proposal: TradeProposal, *, fresh_book: bool,
                 market_exposure: float, total_exposure: float,
                 open_orders: int, daily_loss: float) -> RiskDecision:
        rid = f"risk-{uuid.uuid4().hex[:12]}"
        reasons: list = []
        cfg = self.cfg
        n = float(proposal.notional_usd)
        if n <= 0:
            reasons.append("zero_notional")
        if n > cfg.max_order_notional_usd + 1e-9:
            reasons.append("order_notional_exceeds_cap")
        if cfg.reject_on_stale_book and not fresh_book:
            reasons.append("stale_book")
        if market_exposure + n > cfg.max_market_exposure_usd + 1e-9:
            reasons.append("market_exposure_exceeded")
        if total_exposure + n > cfg.max_total_exposure_usd + 1e-9:
            reasons.append("total_exposure_exceeded")
        if open_orders >= cfg.max_open_orders:
            reasons.append("max_open_orders")
        if daily_loss <= -abs(cfg.max_daily_loss_usd):
            reasons.append("daily_loss_limit")

        approved = not reasons
        dec = RiskDecision(proposal_id="", approved=approved,
                           code="OK" if approved else reasons[0],
                           reasons=reasons, risk_decision_id=rid,
                           adjusted_notional=n if approved else None)
        if approved:
            self.approvals += 1
        else:
            self.rejections += 1
        self.recent.append({"risk_decision_id": rid, "approved": approved,
                            "code": dec.code, "market_id": proposal.market_id})
        self.recent = self.recent[-50:]
        return dec

    def status(self) -> dict:
        return {"approvals": self.approvals, "rejections": self.rejections,
                "recent": self.recent[-10:]}


class PaperBroker:
    """Simulated broker (PAPER ONLY). Fills against the local CLOB book; NO
    fantasy fills for Polymarket unless reference-price fills are explicitly
    enabled (default OFF). Never submits a real order."""

    def __init__(self, cfg: TrainingConfig):
        self.cfg = cfg
        self.orders = 0
        self.fills = 0
        self.rejects = 0

    def place(self, proposal: TradeProposal, rec, *, fresh_book: bool,
              risk_decision_id: str) -> dict:
        self.orders += 1
        order_id = f"ord-{uuid.uuid4().hex[:12]}"
        if not fresh_book and not self.cfg.allow_pm_reference_price_fills:
            self.rejects += 1
            return {"status": "rejected", "reason": "missing_orderbook_no_fantasy_fills",
                    "order_id": order_id, "fill_id": None, "fill_price": None,
                    "fill_qty": 0.0, "notional": 0.0}
        ask = float(proposal.price)
        slip = ask * (self.cfg.slippage_bps / 10000.0)
        fill_price = min(0.999, ask + slip)
        depth_cap_usd = max(0.0, rec.top_depth_usd * self.cfg.max_fill_depth_fraction)
        notional = float(proposal.notional_usd)
        if depth_cap_usd > 0:
            notional = min(notional, depth_cap_usd)
        fill_qty = round(notional / fill_price, 4) if fill_price > 0 else 0.0
        if fill_qty <= 0:
            self.rejects += 1
            return {"status": "rejected", "reason": "no_fillable_size",
                    "order_id": order_id, "fill_id": None, "fill_price": None,
                    "fill_qty": 0.0, "notional": 0.0}
        self.fills += 1
        return {"status": "filled", "reason": "ok", "order_id": order_id,
                "fill_id": f"fill-{uuid.uuid4().hex[:12]}",
                "fill_price": round(fill_price, 4), "fill_qty": fill_qty,
                "notional": round(fill_qty * fill_price, 2),
                "risk_decision_id": risk_decision_id}

    def status(self) -> dict:
        return {"orders": self.orders, "fills": self.fills, "rejects": self.rejects}


@dataclass
class PaperPosition:
    proposal_id: str
    risk_decision_id: str
    order_id: str
    fill_id: str
    market_id: str
    asset_id: str
    group_key: str
    category: str
    outcome: str
    entry_price: float
    qty: float
    p_final: float
    net_edge: float
    ambiguity: float
    evidence: float
    spread: float
    liquidity: float
    open_tick: int
    yes_price_entry: float
    executable_price_entry: float
    p_market_entry: float
    exploration: bool = False
    strategy: str = "directional"        # "directional" | "bregman"
    chainlink_linked: bool = False       # linked to a (fresh) Chainlink oracle feed
    mark: float = 0.0
    closed: bool = False
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    close_reason: str = ""

    @property
    def cost(self) -> float:
        return self.entry_price * self.qty

    def unrealized(self) -> float:
        return (self.mark - self.entry_price) * self.qty


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------

class PolymarketPaperTrainer:
    def __init__(self, cfg: Optional[TrainingConfig] = None, *, store=None,
                 data_dir: Optional[Path] = None, signal_model=None, chainlink=None):
        self.cfg = cfg or TrainingConfig.from_env()
        self._chainlink_injected = chainlink
        self.mode = self.cfg.mode
        self.store_backend = store
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self.run_id = f"pmtrain-{int(time.time())}"

        self.metrics = ScanMetrics()
        self.scanner = MarketScanner(self.cfg, metrics=self.metrics)
        learner_path = self.data_dir / "polymarket_training_learner.json"
        self.learner = OnlineLearner(path=learner_path)
        self.scanner.learner = self.learner
        self.ranker = CandidateRanker(self.cfg, learner=self.learner)
        # Optional Chainlink oracle layer (additive, default OFF). When enabled it
        # feeds Chainlink confidence + no-trade flags into the probability stack.
        self.chainlink = self._chainlink_injected
        if self.chainlink is None and getattr(self.cfg, "chainlink_enabled", False):
            try:
                from engine.chainlink_scanner import ChainlinkScanner
                from engine.feeds.chainlink import RpcChainlinkSource
                self.chainlink = ChainlinkScanner(
                    RpcChainlinkSource(), history_limit=self.cfg.chainlink_history_limit)
            except Exception:  # noqa: BLE001 — Chainlink must never break startup
                self.chainlink = None
        # Chainlink feeds market ranking (coverage expansion) + probability stack.
        if self.chainlink is not None:
            self.scanner.chainlink = self.chainlink
            self.ranker.chainlink = self.chainlink
        self.prob = ProbabilityStack(self.cfg, learner=self.learner, chainlink=self.chainlink)
        self.policy = PaperPolicy(self.cfg)
        self.edge_engine = EdgeEngine(self.cfg)
        # Flagship Polymarket Bregman arbitrage engine (PAPER ONLY). Scans the
        # candidate set every tick for fully-hedged, certified, all-leg-executable
        # opportunities and prioritizes them over directional trades when the
        # certified profit lower bound is positive after all costs.
        self.bregman = BregmanArbitrageEngine(self.cfg, chainlink=self.chainlink)
        # Hierarchical signal resolver: Bregman arbitrage (P1) > calibrated
        # statistical mispricing (P2) > directional predictive edge (P3). Advisory
        # selection only — sizing + risk + execution stay with the gate/broker.
        self.resolver = SignalResolver(self.cfg)
        # Portfolio risk manager: additive caps (event / category / Bregman-bundle
        # exposure, CVaR, drawdown budget, exploration budget) + reporting. It only
        # ever TIGHTENS — the mandatory gates are TrainingRiskGate + RiskEngine.
        self.portfolio = PortfolioRiskManager(PortfolioLimits.from_config(self.cfg))
        self.exploration_budget_used = 0.0
        self.bregman_worst_case_leg_failure = 0.0
        self.subs = SubscriptionManager(self.cfg)
        self.baselines = BaselineComparator()
        self.signal_model = signal_model or sm.build_signal_model(
            self.cfg.signal_model, store=store)
        calib = sm.FeedbackCalibrator(
            path=self.data_dir / "polymarket_training_feedback.json",
            enabled=self.cfg.feedback_enabled)
        self.feedback = FeedbackLoop(self.learner, calibrator=calib,
                                     interval_seconds=self.cfg.feedback_interval_seconds,
                                     enabled=self.cfg.feedback_enabled)

        self.risk = TrainingRiskGate(self.cfg)
        self.broker = PaperBroker(self.cfg)

        # idempotent SQLite training store (own DB file; never wipes engine tables)
        self.tstore = None
        try:
            from .store import TrainingStore
            self.tstore = TrainingStore(self.data_dir)
            self.tstore.record_run(self.run_id, self.mode, config_hash=_cfg_hash(self.cfg))
        except Exception:  # noqa: BLE001 - persistence must never crash training
            self.tstore = None

        self.cash = float(self.cfg.starting_bankroll)
        self.positions: list = []
        self.fills_log: list = []
        self.orders_log: list = []
        self.edge_log: list = []
        self.candidates_log: list = []
        self.diagnostics_log: list = []
        self.tick_count = 0
        self.daily_realized = 0.0
        self.decision_count = 0
        self.rejection_count = 0
        self.exploration_count = 0
        # Bregman flagship telemetry
        self.bregman_log: list = []          # latest certified-scan dicts
        self.bregman_fills_log: list = []
        self.bregman_opportunity_count = 0
        self.bregman_sets_opened = 0
        self.bregman_rejected = 0
        self.started_ts = time.time()

    # -- safety preflight ----------------------------------------------------
    def preflight(self) -> dict:
        checks = {}
        for flag in FORBIDDEN_LIVE_FLAGS:
            checks[f"{flag}_off"] = not _envb(flag, False)
        checks["polymarket_only"] = bool(self.cfg.polymarket_only)
        checks["btc_pulse_trading_disabled"] = bool(self.cfg.disable_btc_pulse_trading)
        checks["clob_enabled"] = bool(self.cfg.clob_enabled)
        checks["paper_order_notional_capped"] = self.cfg.max_order_notional_usd <= 50.0
        checks["arbitrage_disabled"] = _arbitrage_disabled()
        checks["no_wallet_or_private_key"] = _no_wallet_or_key()
        live_detected = not all(checks[f"{f}_off"] for f in FORBIDDEN_LIVE_FLAGS)
        ok = all(checks.values())
        return {"ok": ok, "live_detected": live_detected, "checks": checks}

    # -- helpers -------------------------------------------------------------
    def open_positions(self) -> list:
        return [p for p in self.positions if not p.closed]

    def open_event_groups(self) -> set:
        return {p.group_key for p in self.open_positions()}

    def market_exposure(self, group_key: str) -> float:
        return sum(p.cost for p in self.open_positions() if p.group_key == group_key)

    def total_exposure(self) -> float:
        return sum(p.cost for p in self.open_positions())

    def equity(self) -> float:
        return round(self.cash + sum(p.qty * p.mark for p in self.open_positions()), 4)

    # -- one training tick ---------------------------------------------------
    def run_tick(self, raw_catalog: Optional[list] = None, *,
                 client=None, now: Optional[float] = None) -> dict:
        now = now or time.time()
        if self.mode == "disabled":
            return {"tick": self.tick_count, "mode": "disabled", "opened": 0}
        self.tick_count += 1
        scan = self.scanner.scan(raw_catalog, client=client, now=now)
        records = scan.records  # ranked + shortlisted

        # live-watch subscription (selection only, churn-capped)
        watch = records[:self.cfg.live_watch_limit]
        health = self.subs.reconcile(watch)
        self.metrics.subscribed_assets = health.subscribed_assets

        marks = {r.market_id: market_mid(r) for r in records}
        self._monitor(marks, now)

        if self.tstore is not None:
            try:
                self.tstore.record_scan(self.run_id, {
                    "markets_fetched": scan.scanned, "markets_scanned": scan.scanned,
                    "markets_ranked": scan.shortlisted, "tier_a_count": min(
                        len(watch), self.cfg.trade_candidate_limit),
                    "tier_b_count": len(watch), "scan_ms": scan.latency_ms,
                    "candidates_per_second": self.metrics.candidates_per_second,
                    "rejected_by_reason": scan.reject_reasons})
            except Exception:  # noqa: BLE001
                pass

        # Paper decision budget: evaluate up to trade_candidate_limit candidates,
        # capped by paper_decision_budget. The aggressive profile widens BOTH
        # (trade_candidate_limit 60, budget 120) so more candidates are evaluated;
        # an explicitly small trade_candidate_limit is always respected.
        budget = min(int(self.cfg.trade_candidate_limit),
                     int(getattr(self.cfg, "paper_decision_budget",
                                 self.cfg.trade_candidate_limit)))
        candidates = watch[:budget]
        opened = 0
        evaluated = 0
        # FLAGSHIP PRIORITY: certified Bregman arbitrage is evaluated + opened
        # BEFORE any directional trade (it outranks directional only when its
        # certified profit lower bound is positive after all costs).
        bregman_opened = self._run_bregman(candidates, now)
        opened += bregman_opened
        for rec in candidates:
            if len(self.open_positions()) >= self.cfg.max_open_trades:
                break
            res = self._consider(rec, now)
            evaluated += 1
            if res.get("opened"):
                opened += 1
            if len(self.open_positions()) >= self.cfg.max_open_trades:
                break
        # decision funnel + feedback-loop sample yield
        self.metrics.record_decisions(evaluated=evaluated, traded=opened,
                                       feedback_samples=evaluated)

        self.feedback.maybe_update(now)
        self._persist_status()
        return {"tick": self.tick_count, "mode": self.mode, "scanned": scan.scanned,
                "kept": scan.kept, "candidates": len(candidates), "opened": opened,
                "bregman_opened": bregman_opened,
                "open_positions": len(self.open_positions()), "equity": self.equity()}

    # -- flagship Bregman arbitrage -----------------------------------------
    def scan_bregman(self, records: list, now: float) -> list:
        """Certify Bregman opportunities across the candidate set (read-only).

        Bregman arbitrage priority — builds simplex groups, certifies every one,
        records the certified-scan telemetry, and returns the certification
        objects. Never raises: a Bregman failure must not break the tick."""
        if not records or not getattr(self.cfg, "bregman_enabled", True):
            self.bregman_log = []
            return []
        try:
            groups = group_markets(records, chainlink=self.chainlink, now=now)
            certs = self.bregman.certify_all(groups, now=now)
        except Exception:  # noqa: BLE001 — Bregman must never break a training tick
            self.bregman_log = []
            return []
        self.bregman_log = [c.to_dict() for c in certs]
        return certs

    def _run_bregman(self, records: list, now: float) -> int:
        certs = self.scan_bregman(records, now)
        tradable = sorted([c for c in certs if c.is_opportunity],
                          key=lambda o: o.profit_lower_bound, reverse=True)
        self.bregman_opportunity_count += len(tradable)
        if (self.mode != "paper_train"
                or not getattr(self.cfg, "bregman_execution_enabled", True)):
            return 0
        rec_by_id = {r.market_id: r for r in records}
        opened = 0
        for opp in tradable:
            # never act on a synthesized leg price (binary NO leg is derived)
            if opp.group_type == "binary_yes_no":
                continue
            if len(self.open_positions()) >= self.cfg.max_open_trades:
                break
            if self._open_bregman(opp, rec_by_id, now):
                opened += 1
        return opened

    def _open_bregman(self, opp: CertifiedBregmanOpportunity, rec_by_id: dict,
                      now: float) -> bool:
        """Open a fully-hedged Bregman set in PAPER. Every leg routes through the
        deterministic RiskEngine FIRST; the set is placed only if ALL legs are
        approved and fill — never leaving partial, un-hedged exposure. PAPER
        ONLY: no real orders, no signing, no live submit."""
        legs = opp.legs
        recs = [rec_by_id.get(l.market_id) for l in legs]
        if any(r is None for r in recs):
            self.bregman_rejected += 1
            return False
        cap = float(self.cfg.max_order_notional_usd)
        proposals: list = []
        for l in legs:
            notional = min(cap, max(0.0, l.executable_price * l.quantity))
            if notional <= 0 or l.executable_price <= 0:
                self.bregman_rejected += 1
                return False
            qty = round(notional / l.executable_price, 4)
            proposals.append(TradeProposal(
                market_id=l.market_id, asset_id=str(l.token_id), outcome=l.outcome,
                side="BUY", price=round(l.executable_price, 4),
                notional_usd=round(notional, 2), qty=qty,
                p_final=round(l.executable_price, 4),
                net_edge=round(opp.profit_lower_bound, 5), confidence=1.0,
                research_source="bregman", sizing_method="bregman_hedge"))
        # Additive portfolio cap: bundle exposure must fit the Bregman-bundle
        # exposure budget (only ever TIGHTENS; mandatory gate is still per-leg risk).
        bundle_notional = sum(p.notional_usd for p in proposals)
        ok, preason = self.portfolio.check(
            notional=bundle_notional, state=self._portfolio_state(),
            strategy="bregman_arbitrage", category=getattr(recs[0], "category", "uncategorized"),
            event_group=opp.group_id, bregman=True,
            day_pnl=self.daily_realized, drawdown=self._drawdown())
        if not ok:
            self.bregman_rejected += 1
            self.learner.record_decision(traded=False, reason=preason)
            return False
        # leg-failure haircut + bundle sizing analytics
        sizing = bregman_bundle_size(
            opp, bankroll=self.cash,
            max_bundle_usd=float(self.cfg.max_bregman_bundle_exposure_usd),
            leg_failure_haircut=float(getattr(self.cfg, "leg_failure_haircut", 0.5)))
        self.bregman_worst_case_leg_failure = round(
            self.bregman_worst_case_leg_failure + float(sizing.get("worst_case_leg_failure", 0.0)), 6)

        # PRE-CHECK: all legs must pass the RiskEngine before any leg is placed.
        decisions: list = []
        for p in proposals:
            d = self.risk.evaluate(
                p, fresh_book=True, market_exposure=self.market_exposure(opp.group_id),
                total_exposure=self.total_exposure() + sum(pp.notional_usd for pp in proposals[:len(decisions)]),
                open_orders=len(self.open_positions()) + len(decisions),
                daily_loss=self.daily_realized)
            if not d.approved:
                self.bregman_rejected += 1
                self.learner.record_decision(traded=False, reason="risk_rejected")
                return False
            decisions.append(d)
        # place every leg (fully hedged); roll back the set if any leg fails.
        opened_positions: list = []
        for p, rec, d in zip(proposals, recs, decisions):
            proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
            d.proposal_id = proposal_id
            fill = self.broker.place(p, rec, fresh_book=True,
                                     risk_decision_id=d.risk_decision_id)
            if fill["status"] != "filled":
                self.bregman_rejected += 1
                self.learner.record_decision(traded=False, reason="paperbroker_rejected")
                for pos in opened_positions:      # unwind partial hedge
                    self.cash += pos.qty * pos.entry_price
                    self.positions.remove(pos)
                return False
            self.cash -= fill["notional"]
            pos = PaperPosition(
                proposal_id=proposal_id, risk_decision_id=d.risk_decision_id,
                order_id=fill["order_id"], fill_id=fill["fill_id"],
                market_id=p.market_id, asset_id=p.asset_id, group_key=opp.group_id,
                category=getattr(rec, "category", "uncategorized"), outcome=p.outcome,
                entry_price=fill["fill_price"], qty=fill["fill_qty"],
                p_final=p.p_final, net_edge=opp.profit_lower_bound, ambiguity=0.0,
                evidence=1.0, spread=float(getattr(rec, "spread", 0.0) or 0.0),
                liquidity=float(getattr(rec, "liquidity_usd", 0.0) or 0.0),
                open_tick=self.tick_count, yes_price_entry=fill["fill_price"],
                executable_price_entry=fill["fill_price"], p_market_entry=fill["fill_price"],
                strategy="bregman", mark=fill["fill_price"])
            self.positions.append(pos)
            opened_positions.append(pos)
            self.bregman_fills_log.append({
                "group_id": opp.group_id, "market_id": p.market_id,
                "asset_id": p.asset_id, "outcome": p.outcome,
                "price": fill["fill_price"], "qty": fill["fill_qty"],
                "notional": fill["notional"], "tick": self.tick_count})
        self.bregman_sets_opened += 1
        self.learner.record_decision(traded=True)
        self.learner.record_signal(strategy="bregman_arbitrage",
                                   attribution={"bregman_divergence": opp.divergence_gap,
                                                "execution_edge": opp.profit_lower_bound},
                                   traded=True)
        return True

    def bregman_summary(self) -> dict:
        """Flagship Bregman telemetry: certified profit, false-positive rate,
        rejection reasons, persistence, capital efficiency (Live Monitoring)."""
        from engine.replay import metrics as _m
        return {
            "enabled": bool(getattr(self.cfg, "bregman_enabled", True)),
            "execution_enabled": bool(getattr(self.cfg, "bregman_execution_enabled", True)),
            "opportunity_count": self.bregman_opportunity_count,
            "sets_opened": self.bregman_sets_opened,
            "rejected": self.bregman_rejected,
            "last_scan_metrics": _m.bregman_metrics(self.bregman_log),
        }

    # -- portfolio risk + analytics -----------------------------------------
    def _portfolio_state(self) -> PortfolioState:
        st = PortfolioState()
        for p in self.open_positions():
            st.add(PortfolioPosition(
                strategy=getattr(p, "strategy", "directional"), category=p.category,
                event_group=p.group_key, notional=p.cost, side="BUY",
                bregman=(getattr(p, "strategy", "") == "bregman"),
                chainlink_linked=bool(getattr(p, "chainlink_linked", False))))
        return st

    def _equity_curve(self) -> list:
        eq = float(self.cfg.starting_bankroll)
        curve = [eq]
        for p in self.positions:
            if p.closed:
                eq += p.realized_pnl
                curve.append(eq)
        return curve

    def _drawdown(self) -> float:
        return max_drawdown(self._equity_curve())

    def portfolio_report(self) -> dict:
        """Portfolio risk report (Risk Management & Portfolio Optimization +
        Live Monitoring): gross/net/event/strategy/Bregman/Chainlink-linked
        exposure, expected shortfall, worst-case leg failure, concentration,
        aggressive exploration budget used, and feedback generated per unit risk."""
        closed = [p for p in self.positions if p.closed]
        returns = [round(p.realized_pnl / p.cost, 6) if p.cost else 0.0 for p in closed]
        rep = self.portfolio.portfolio_report(
            self._portfolio_state(), day_pnl=self.daily_realized, returns=returns,
            equity_curve=self._equity_curve(),
            exploration_used=self.exploration_budget_used,
            worst_case_leg_failure=self.bregman_worst_case_leg_failure,
            feedback_events=int(self.learner.closed))
        rep["profile"] = "aggressive" if self.cfg.exploration_enabled else "conservative"
        return rep

    def _consider(self, rec, now: float) -> dict:
        est = self.prob.estimate(rec, self.signal_model, now=now)
        edge = self.edge_engine.best_side(
            est, rec, open_event_groups=self.open_event_groups(),
            open_trades=len(self.open_positions()))
        # recursive feedback scales the edge (good calibration eases the gate)
        gates_ok = edge.reason in ("trade", "edge_too_low", "uncertainty_too_high")
        adj = self.feedback.edge_adjustment()
        adjusted_net = edge.net_edge * (adj if adj > 0 else 1.0)
        would_trade = gates_ok and (adjusted_net > edge.threshold)
        reason = "trade" if would_trade else (
            edge.reason if not gates_ok else "edge_too_low")

        # hierarchical signal resolution (Bregman P1 already preempts upstream;
        # here we classify P2 statistical vs P3 directional + attribute alpha).
        ttr = ((rec.end_ts - now) if getattr(rec, "end_ts", None) else None)
        resolved = self.resolver.resolve(est=est, edge=edge, bregman_opp=None,
                                         time_to_resolution_s=ttr)
        self.learner.record_signal(strategy=resolved.strategy,
                                   attribution=resolved.alpha_attribution,
                                   traded=bool(resolved.should_trade and would_trade))

        # diagnostics for EVERY evaluated candidate (trade + skip)
        diag = build_record(ts_ms=int(now * 1000), est=est, edge=edge, rec=rec,
                            resolved=resolved)
        self._last_resolved = resolved
        self.diagnostics_log.append(diag.to_dict())
        if self.tstore is not None:
            try:
                self.tstore.record_diagnostics(self.run_id, diag.to_dict())
            except Exception:  # noqa: BLE001
                pass
        self.candidates_log.append({
            "tick": self.tick_count, "market_id": rec.market_id, "category": rec.category,
            "outcome": edge.outcome, "p_market_mid": round(est.p_market_mid, 4),
            "p_final": round(edge.p_final, 4), "net_edge": round(edge.net_edge, 5),
            "adjusted_net_edge": round(adjusted_net, 5), "edge_adjustment": adj,
            "threshold": round(edge.threshold, 5), "decision": reason})
        self.edge_log.append({**edge.to_dict(), "tick": self.tick_count,
                              "p_research": round(est.p_research, 4),
                              "research_source": est.research_source})

        # baselines observe every candidate (trade COUNTS)
        self.baselines.observe(
            yes_price=est.p_market_mid, executable_price=(edge.executable_price or est.p_market_mid),
            p_market=est.p_market_mid, min_net_edge=self.cfg.min_net_edge, traded=would_trade)

        self.decision_count += 1
        exploratory = False
        if not would_trade:
            # Controlled exploration (aggressive paper mode only): open a near-miss
            # candidate at a small bounded size for extra feedback signal. Still
            # routed through RiskEngine + PaperBroker — cannot bypass hard caps.
            explore_notional = min(float(self.cfg.exploration_notional_usd),
                                   float(self.cfg.max_order_notional_usd))
            budget_ok = (self.exploration_budget_used + explore_notional
                         <= float(self.cfg.exploration_budget_usd) + 1e-9)
            if (self.mode == "paper_train" and self.cfg.exploration_enabled
                    and reason in ("edge_too_low", "uncertainty_too_high")
                    and edge.net_edge >= self.cfg.exploration_min_edge
                    and budget_ok
                    and self._explore_gate(rec.market_id)):
                exploratory = True
            else:
                self.rejection_count += 1
                self.learner.record_decision(traded=False, reason=reason)
                return {"opened": False, "reason": reason}

        # observe_only mode evaluates + records but NEVER opens a paper trade
        if self.mode != "paper_train":
            self.learner.record_decision(traded=False, reason="observe_only")
            return {"opened": False, "reason": "observe_only"}

        return self._open(rec, est, edge, diag, exploratory=exploratory)

    def _explore_gate(self, market_id: str) -> bool:
        """Deterministic exploration sampler keyed on market+tick (no RNG state)."""
        import hashlib
        h = hashlib.sha256(f"{market_id}:{self.tick_count}".encode()).digest()
        return (int.from_bytes(h[:4], "big") % 1000) / 1000.0 < self.cfg.exploration_rate

    def _open(self, rec, est, edge, diag, *, exploratory: bool = False) -> dict:
        """Build proposal -> RiskEngine -> PaperBroker (trace-id chain). PAPER
        ONLY. Exploratory trades use a small bounded notional capped to the same
        hard paper order-notional ceiling as normal trades."""
        proposal = self.policy.build_proposal(edge, est, rec)
        if exploratory:
            notional = min(float(self.cfg.exploration_notional_usd),
                           float(self.cfg.max_order_notional_usd))
            proposal.notional_usd = round(notional, 2)
            proposal.qty = round(notional / proposal.price, 4) if proposal.price > 0 else 0.0
            proposal.sizing_method = "exploration"
            self.exploration_count += 1
        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        decision = self.risk.evaluate(
            proposal, fresh_book=est.fresh_book,
            market_exposure=self.market_exposure(rec.group_key),
            total_exposure=self.total_exposure(),
            open_orders=len(self.open_positions()), daily_loss=self.daily_realized)
        decision.proposal_id = proposal_id
        if not decision.approved:
            self.rejection_count += 1
            self.learner.record_decision(traded=False, reason="risk_rejected")
            return {"opened": False, "reason": "risk_rejected"}

        fill = self.broker.place(proposal, rec, fresh_book=est.fresh_book,
                                 risk_decision_id=decision.risk_decision_id)
        self.orders_log.append({"order_id": fill["order_id"], "proposal_id": proposal_id,
                                "risk_decision_id": decision.risk_decision_id,
                                "market_id": rec.market_id, "outcome": edge.outcome,
                                "status": fill["status"], "reason": fill["reason"],
                                "exploration": exploratory})
        if fill["status"] != "filled":
            self.rejection_count += 1
            self.learner.record_decision(traded=False, reason="paperbroker_rejected")
            return {"opened": False, "reason": "paperbroker_rejected"}

        self.cash -= fill["notional"]
        if exploratory:
            self.exploration_budget_used = round(
                self.exploration_budget_used + float(fill["notional"]), 6)
        pos = PaperPosition(
            proposal_id=proposal_id, risk_decision_id=decision.risk_decision_id,
            order_id=fill["order_id"], fill_id=fill["fill_id"], market_id=rec.market_id,
            asset_id=proposal.asset_id, group_key=rec.group_key, category=rec.category,
            outcome=edge.outcome, entry_price=fill["fill_price"], qty=fill["fill_qty"],
            p_final=edge.p_final, net_edge=edge.net_edge, ambiguity=est.ambiguity_score,
            evidence=est.evidence_score, spread=est.spread, liquidity=est.liquidity_usd,
            open_tick=self.tick_count, yes_price_entry=est.p_market_mid,
            executable_price_entry=(edge.executable_price or est.p_market_mid),
            p_market_entry=est.p_market_mid, exploration=exploratory,
            strategy=_resolved_strategy(getattr(self, "_last_resolved", None)),
            chainlink_linked=bool(getattr(est, "bregman_group_id", "")),
            mark=fill["fill_price"])
        self.positions.append(pos)
        self.fills_log.append({
            "proposal_id": proposal_id, "risk_decision_id": decision.risk_decision_id,
            "order_id": fill["order_id"], "fill_id": fill["fill_id"],
            "market_id": rec.market_id, "asset_id": proposal.asset_id, "side": "BUY",
            "outcome": edge.outcome, "price": fill["fill_price"], "qty": fill["fill_qty"],
            "notional": fill["notional"], "tick": self.tick_count,
            "diagnostics_id": diag.diagnostics_id, "exploration": exploratory})
        self.learner.record_decision(traded=True)
        if self.tstore is not None:
            try:
                self.tstore.record_learning_event(
                    self.run_id, "fill", market_id=rec.market_id, asset_id=proposal.asset_id,
                    diagnostics_id=diag.diagnostics_id, order_id=fill["order_id"],
                    fill_id=fill["fill_id"], payload={"notional": fill["notional"],
                                                      "exploration": exploratory})
            except Exception:  # noqa: BLE001
                pass
        return {"opened": True, "fill_id": fill["fill_id"], "exploration": exploratory}

    def _monitor(self, marks: dict, now: float) -> None:
        for pos in self.open_positions():
            if pos.market_id in marks:
                pos.mark = marks[pos.market_id]
            held = self.tick_count - pos.open_tick
            reason = None
            if pos.mark >= pos.entry_price + self.cfg.take_profit:
                reason = "take_profit"
            elif pos.mark <= pos.entry_price - self.cfg.stop_loss:
                reason = "stop_loss"
            elif held >= self.cfg.max_hold_ticks:
                reason = "time_stop"
            if reason:
                self._close(pos, reason)

    def finalize(self) -> None:
        for pos in self.open_positions():
            self._close(pos, "run_end")
        self.feedback.maybe_update(force=True)
        if self.tstore is not None:
            try:
                for r in self.baselines.results():
                    self.tstore.record_baseline(self.run_id, r)
                self.tstore.stop_run(self.run_id, status="finalized")
            except Exception:  # noqa: BLE001
                pass
        self._persist_status()

    def _close(self, pos: PaperPosition, reason: str) -> None:
        pos.closed = True
        pos.exit_price = pos.mark
        pos.realized_pnl = round((pos.exit_price - pos.entry_price) * pos.qty, 4)
        pos.close_reason = reason
        self.cash += pos.qty * pos.exit_price
        self.daily_realized = round(self.daily_realized + pos.realized_pnl, 4)
        win = pos.realized_pnl > 0
        self.feedback.record_outcome(
            predicted_prob=pos.p_final, predicted_edge=pos.net_edge,
            realized_pnl=pos.realized_pnl, size_usd=max(1e-9, pos.cost), win=win,
            category=pos.category, net_edge=pos.net_edge, spread=pos.spread,
            liquidity=pos.liquidity, ambiguity=pos.ambiguity, evidence=pos.evidence)
        # baselines settle on this opportunity (realized = exit mark as settle proxy)
        per_unit = (pos.realized_pnl / pos.qty) if pos.qty else 0.0
        self.baselines.settle(
            yes_price=pos.yes_price_entry, executable_price=pos.executable_price_entry,
            p_market=pos.p_market_entry, realized=pos.exit_price,
            min_net_edge=self.cfg.min_net_edge, strategy_pnl=per_unit, strategy_win=win)

    # -- multi-tick run ------------------------------------------------------
    def run(self, ticks: int, catalog_provider=None, *, raw_catalog: Optional[list] = None,
            client=None) -> dict:
        for _ in range(int(ticks)):
            cat = catalog_provider() if catalog_provider else raw_catalog
            self.run_tick(cat, client=client)
        self.finalize()
        return self.status()

    # -- reporting -----------------------------------------------------------
    def pnl_summary(self) -> dict:
        closed = [p for p in self.positions if p.closed]
        wins = [p for p in closed if p.realized_pnl > 0]
        realized = round(sum(p.realized_pnl for p in closed), 4)
        unreal = round(sum(p.unrealized() for p in self.open_positions()), 4)
        eq_curve, eq, dd = [], 0.0, 0.0
        for p in closed:
            eq += p.realized_pnl
            dd = min(dd, eq)
        return {
            "starting_bankroll": self.cfg.starting_bankroll,
            "cash": round(self.cash, 4), "equity": self.equity(),
            "realized_pnl": realized, "unrealized_pnl": unreal,
            "total_pnl": round(realized + unreal, 4), "max_drawdown": round(dd, 4),
            "trades_opened": len([p for p in self.positions]),
            "trades_closed": len(closed),
            "win_rate": round(len(wins) / len(closed), 4) if closed else None,
            "open_positions": len(self.open_positions()),
            "avg_net_edge": round(sum(p.net_edge for p in self.positions) /
                                  len(self.positions), 5) if self.positions else None,
            "decision_count": self.decision_count,
            "rejection_count": self.rejection_count,
            "rejection_rate": round(self.rejection_count / self.decision_count, 4)
                              if self.decision_count else 0.0,
            "exploration_count": self.exploration_count,
            "exploration_rate": round(self.exploration_count / len(self.positions), 4)
                                if self.positions else 0.0,
        }

    def baseline_report(self) -> dict:
        """Quantitative baseline: deterministic algorithm inventory + the full
        institutional metric suite computed from this run's closed paper trades.
        Reports Chainlink presence and the (now flagship) Bregman arbitrage
        status. Does not change any default behaviour — purely a read-only summary."""
        from engine.training.algorithm_inventory import algorithm_inventory
        from engine.replay import metrics as _m
        inv = algorithm_inventory()
        closed = [p for p in self.positions if p.closed]
        trades = [{"realized_pnl": p.realized_pnl, "cost": p.cost, "net_edge": p.net_edge,
                   "category": p.category, "outcome": p.outcome} for p in closed]
        eqs = [float(self.cfg.starting_bankroll)]
        for p in closed:
            eqs.append(eqs[-1] + p.realized_pnl)
        preds = [p.p_final for p in closed]
        outs = [1.0 if p.realized_pnl > 0 else 0.0 for p in closed]
        inst = _m.institutional_metrics(
            equities=eqs, trades=trades, decisions=self.decision_count,
            rejections=self.rejection_count, explorations=self.exploration_count,
            predictions=preds, outcomes=outs,
            notional_traded=sum(p.cost for p in self.positions))
        return {
            "mode": self.mode,
            "profile": "aggressive" if self.cfg.exploration_enabled else "conservative",
            "paper_only": self.cfg.is_paper_only,
            "algorithm_inventory": inv,
            "institutional_metrics": inst,
            "chainlink_present": inv["chainlink_present"],
            "bregman_present": inv["bregman_present"],
            "bregman_status": "active" if inv["bregman_present"] else "absent",
            "bregman": self.bregman_summary(),
            "signal_priority": ["bregman_arbitrage", "statistical_mispricing", "directional"],
            "signal_strategies": self.learner.summary().get("signal_strategies", {}),
            "alpha_attribution": self.learner.summary().get("alpha_attribution", {}),
            "portfolio": self.portfolio_report(),
            "legacy_arb_disabled": inv["legacy_arb_disabled"],
        }

    def status(self) -> dict:
        return {
            "available": True,
            "run_id": self.run_id,
            "mode": self.mode,
            "execution_mode": "paper",
            "polymarket_only": self.cfg.polymarket_only,
            "tick": self.tick_count,
            "runtime_seconds": round(time.time() - self.started_ts, 1),
            "config": self.cfg.as_dict(),
            "scan_metrics": self.metrics.to_dict(),
            "subscription": self.subs.health.to_dict(),
            "pnl": self.pnl_summary(),
            "risk": self.risk.status(),
            "broker": self.broker.status(),
            "baselines": self.baselines.results(),
            "signal_model": self.signal_model.status(),
            "feedback": self.feedback.summary(),
            "learning": self.learner.summary(),
            "chainlink": (self.chainlink.metrics() if self.chainlink
                          else {"enabled": False}),
            "bregman": self.bregman_summary(),
            "portfolio": self.portfolio_report(),
            "safety": self.preflight(),
        }

    def _persist_status(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "polymarket_training.json").write_text(
                _dumps(self.status()), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolved_strategy(resolved) -> str:
    """Position strategy tag from the resolved signal (defaults to directional)."""
    s = getattr(resolved, "strategy", None)
    return s if s and s != "none" else "directional"


def _arbitrage_disabled() -> bool:
    try:
        from engine.arb.execution import ARBITRAGE_PERMANENTLY_DISABLED
        return bool(ARBITRAGE_PERMANENTLY_DISABLED)
    except Exception:  # noqa: BLE001
        return True


def _no_wallet_or_key() -> bool:
    """True when no Polymarket wallet/private-key signing material is configured."""
    import os
    keys = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_PRIVATE_KEY",
            "POLY_PRIVATE_KEY", "PK", "POLYMARKET_API_SECRET_SIGNER")
    return not any(os.getenv(k) for k in keys)


def _default_data_dir() -> Path:
    import os
    try:
        from engine.config import Settings
        return Path(Settings().data_dir)
    except Exception:  # noqa: BLE001
        return Path(os.getenv("HTE_DATA_DIR") or os.getcwd())


def _cfg_hash(cfg: TrainingConfig) -> str:
    import hashlib
    import json
    try:
        blob = json.dumps(cfg.as_dict(), sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = str(id(cfg))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str, indent=2)


PolymarketTrainingEngine = PolymarketPaperTrainer  # spec name alias
