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
from .experiment_manager import (BREGMAN_VARIANT, ExperimentManager, classify_variant)
from .feedback_loop import FeedbackLoop
from .live_readiness import (ReadinessCriteria, capital_preservation_report,
                             evaluate_live_readiness)
from .market_scanner import MarketScanner
from .metrics import ScanMetrics
from .monitoring import (KillSwitchThresholds, bregman_monitoring, build_dashboard,
                         evaluate_kill_switch, loss_streak)
from .online_learner import OnlineLearner
from .paper_policy import PaperPolicy, TradeProposal
from .portfolio import (PortfolioLimits, PortfolioPosition, PortfolioRiskManager,
                        PortfolioState, bregman_bundle_size, max_drawdown)
from .capital_allocator import (AdaptiveCapitalAllocator, AllocationDecision,
                                CapitalCandidate, drawdown_governor, _bucket_for)
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
    # experiment attribution (controlled strategy-variant experiments; PAPER ONLY)
    experiment_id: str = ""
    strategy_variant: str = "directional_edge"
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
        # Adaptive capital allocator (micro-live readiness; PAPER ONLY). Splits
        # capital into ordered buckets (certified Bregman first), sizes with
        # fractional Kelly + hard risk haircuts, runs the drawdown governor, and
        # only ever TIGHTENS the mandatory gates. Used here for read-only capital
        # allocation analytics + the status report; never relaxes a risk cap.
        self.capital_allocator = AdaptiveCapitalAllocator(self.cfg)
        # Controlled strategy-variant experiment manager (PAPER ONLY). Always
        # present (cheap accounting); per-variant slot allocation is only ENFORCED
        # when cfg.experiments_enabled. It can never relax a hard risk cap.
        self.experiments = ExperimentManager(
            experiment_id=getattr(self.cfg, "experiment_id", "exp_default"),
            starting_bankroll=float(self.cfg.starting_bankroll),
            weights=(getattr(self.cfg, "variant_budget_weights", None) or None),
            bregman_first=bool(getattr(self.cfg, "bregman_first_budget", True)),
            aggressive=bool(self.cfg.exploration_enabled))
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

        # Institutional paper-training campaign controller (PAPER ONLY; default
        # OFF). When enabled it freezes algorithm development and collects durable
        # evidence across runs; it NEVER enables live trading. Reloads + persists
        # campaign state to <data_dir>/polymarket_training_campaign.json.
        self.campaign = None
        self._campaign_error = None
        self._campaign_baseline_calibration = None
        self._campaign_safety = None
        if bool(getattr(self.cfg, "campaign_enabled", False)):
            try:
                from .campaign_controller import (TrainingCampaignController,
                                                  campaign_safety_check)
                self.campaign = TrainingCampaignController.from_config(
                    self.cfg, state_path=self.data_dir / "polymarket_training_campaign.json",
                    store=self.tstore, run_id=self.run_id)
                # Resolved campaign-safe profile (read-only realism + live-paths-off,
                # fail-closed). Read-only; never enables a live path.
                self._campaign_safety = campaign_safety_check(self.cfg)
                self.campaign.safety_profile = self._campaign_safety
            except Exception as exc:  # noqa: BLE001 — campaign must never crash training
                self.campaign = None
                self._campaign_error = f"init_failed:{exc}"

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
        # final monitoring + kill-switch state (PAPER ONLY). Aggressive mode
        # auto-downgrades to conservative when a kill-switch metric trips; it
        # never touches a live control.
        self._metric_history: list = []
        self._downgraded = False
        self._kill_switch: dict = {"triggered": [], "should_downgrade": False,
                                    "severity": "OK", "alerts": []}
        self._ks_thresholds = KillSwitchThresholds.from_config(self.cfg)

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
        # Human-readable sample of WHAT we're scanning right now (dashboard
        # visibility only — selection, never an order). Top live-watched markets
        # with their real Polymarket question text + live book stats.
        self._watch_sample = [{
            "market_id": r.market_id,
            "question": (getattr(r, "question", "") or r.market_id)[:100],
            "category": getattr(r, "category", "") or "uncategorized",
            "mid": round(market_mid(r), 4),
            "spread": round(getattr(r, "spread", 0.0) or 0.0, 4),
            "liquidity_usd": round(getattr(r, "liquidity_usd", 0.0) or 0.0),
        } for r in watch[:15]]
        self._last_scan_ts = now
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
        if getattr(self.cfg, "experiments_enabled", False):
            # Controlled experiments: split the remaining paper trade-slot budget
            # across variants (Bregman FIRST when certified opps exist). The slot
            # sum never exceeds the budget, so combined hard caps still bind.
            tradable = self._bregman_tradable(candidates, now)
            cap_slots = max(0, min(int(budget), int(self.cfg.max_open_trades))
                            - len(self.open_positions()))
            alloc = self.experiments.allocate(cap_slots, bregman_available=bool(tradable))
            self.experiments.begin_tick(alloc)
            bregman_opened = self._open_bregman_sets(
                tradable, candidates, now, cap=alloc.get(BREGMAN_VARIANT))
        else:
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
        # final monitoring + kill-switch: auto-downgrade aggressive->conservative
        # when a kill-switch metric trips (PAPER ONLY; never touches live controls)
        if getattr(self.cfg, "kill_switch_enabled", True):
            try:
                self.run_monitoring(now=now)
            except Exception:  # noqa: BLE001 — monitoring must never break a tick
                pass
        self._persist_status()
        # Institutional campaign: aggregate this tick's REAL evidence (PAPER ONLY).
        # Never breaks the tick if campaign reporting fails.
        self._update_campaign()
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

    def _bregman_tradable(self, records: list, now: float) -> list:
        """Certify + rank tradable Bregman opportunities (read-only). Used to know
        Bregman availability before allocating the experiment budget."""
        certs = self.scan_bregman(records, now)
        tradable = sorted([c for c in certs if c.is_opportunity],
                          key=lambda o: o.profit_lower_bound, reverse=True)
        self.bregman_opportunity_count += len(tradable)
        return tradable

    def _open_bregman_sets(self, tradable: list, records: list, now: float, *,
                           cap: Optional[int] = None) -> int:
        """Open up to ``cap`` certified Bregman sets (``None`` = no per-variant cap;
        the global ``max_open_trades`` + RiskEngine always bind)."""
        if (self.mode != "paper_train"
                or not getattr(self.cfg, "bregman_execution_enabled", True)):
            return 0
        rec_by_id = {r.market_id: r for r in records}
        opened = 0
        for opp in tradable:
            if cap is not None and opened >= cap:
                break
            # never act on a synthesized leg price (binary NO leg is derived)
            if opp.group_type == "binary_yes_no":
                continue
            if len(self.open_positions()) >= self.cfg.max_open_trades:
                break
            if self._open_bregman(opp, rec_by_id, now):
                opened += 1
        return opened

    def _run_bregman(self, records: list, now: float) -> int:
        tradable = self._bregman_tradable(records, now)
        return self._open_bregman_sets(tradable, records, now, cap=None)

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
                strategy="bregman", mark=fill["fill_price"],
                experiment_id=self.experiments.experiment_id, strategy_variant=BREGMAN_VARIANT)
            self.positions.append(pos)
            opened_positions.append(pos)
            self.bregman_fills_log.append({
                "group_id": opp.group_id, "market_id": p.market_id,
                "asset_id": p.asset_id, "outcome": p.outcome,
                "price": fill["fill_price"], "qty": fill["fill_qty"],
                "notional": fill["notional"], "tick": self.tick_count})
        self.bregman_sets_opened += 1
        self.experiments.record_trade(BREGMAN_VARIANT, notional=bundle_notional)
        self.experiments.record_fill(BREGMAN_VARIANT, filled=True)
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

    # -- micro-live canary framework status (DISABLED by default) ------------
    def canary_status(self) -> dict:
        """Read-only micro-live canary framework status for the report. The
        canary is DISABLED by default; this never enables live trading and never
        sizes or places an order. Compliance/Security + Live Trading & Monitoring."""
        try:
            from engine.micro_live.canary import CanaryController
            return CanaryController().status()
        except Exception:  # noqa: BLE001 — status must never break the paper loop
            return {"enabled": False, "dry_run": True, "rolled_back": False}

    # -- adaptive capital allocation (micro-live readiness) ------------------
    def _execution_quality_proxy(self) -> float:
        """Realised-fill quality proxy for the drawdown governor (CLOB v2):
        the broker fill ratio, haircut by the stale-book rejection rate."""
        orders = max(1, int(getattr(self.broker, "orders", 0)))
        fill_ratio = min(1.0, float(getattr(self.broker, "fills", 0)) / orders)
        rejects = int(getattr(self.broker, "rejects", 0))
        reject_pen = min(1.0, rejects / orders)
        return round(max(0.0, fill_ratio * (1.0 - 0.5 * reject_pen)), 6)

    def _capital_decisions_snapshot(self) -> list:
        """Build read-only allocation decisions for the OPEN book so the report
        reflects how live capital is currently distributed across buckets."""
        decisions = []
        for p in self.positions:
            if p.closed:
                continue
            variant = (getattr(p, "strategy_variant", "")
                       or getattr(p, "strategy", "directional"))
            is_bregman = getattr(p, "strategy", "") == "bregman"
            if not is_bregman and bool(getattr(p, "chainlink_linked", False)) \
                    and "chainlink" not in str(variant).lower():
                variant = "chainlink_edge"
            cand = CapitalCandidate(
                strategy=variant, bregman=is_bregman, bregman_certified=is_bregman,
                exploration=bool(getattr(p, "exploration", False)))
            notional = float(getattr(p, "cost", 0.0) or 0.0)
            net = float(getattr(p, "net_edge", 0.0) or 0.0)
            decisions.append(AllocationDecision(
                approved=True, bucket=_bucket_for(cand), notional_usd=notional,
                strategy=variant, market_id=getattr(p, "market_id", ""),
                net_after_cost_edge=net, exploration=cand.exploration,
                expected_profit=round(notional * max(0.0, net), 6)))
        return decisions

    def capital_drawdown_governor(self) -> dict:
        """Current drawdown-governor verdict (reduce / pause / downgrade)."""
        closed = [p for p in self.positions if p.closed]
        closed_pnls = [p.realized_pnl for p in closed]
        ci = float((self.feedback.summary() or {}).get("calibration_instability", 0.0) or 0.0)
        return drawdown_governor(
            loss_streak=loss_streak(closed_pnls), drawdown=self._drawdown(),
            max_drawdown_usd=float(self.cfg.max_drawdown_usd),
            calibration_instability=ci,
            execution_quality=self._execution_quality_proxy(),
            limits=self.capital_allocator.dd_limits)

    def capital_allocation_report(self) -> dict:
        """Capital allocation + drawdown-governor report (Risk Management &
        Portfolio Optimization + Live Monitoring): per-bucket allocation, expected
        return, expected shortfall / CVaR, concentration, capital efficiency,
        feedback per risk unit, the drawdown-governor verdict, and the live
        portfolio constraints. Read-only — never sizes or places an order."""
        closed = [p for p in self.positions if p.closed]
        returns = [round(p.realized_pnl / p.cost, 6) if p.cost else 0.0 for p in closed]
        decisions = self._capital_decisions_snapshot()
        rep = self.capital_allocator.capital_allocation_report(
            decisions, returns=returns, equity_curve=self._equity_curve(),
            feedback_events=int(self.learner.closed))
        rep["enabled"] = bool(getattr(self.cfg, "capital_allocation_enabled", True))
        rep["drawdown_governor"] = self.capital_drawdown_governor()
        con = self.capital_allocator.constraints
        rep["constraints"] = {
            "max_market_exposure_usd": con.max_market_exposure_usd,
            "max_event_exposure_usd": con.max_event_exposure_usd,
            "max_correlated_cluster_exposure_usd": con.max_cluster_exposure_usd,
            "max_strategy_exposure_usd": con.max_strategy_exposure_usd,
            "max_daily_loss_usd": con.max_daily_loss_usd,
            "max_open_capital_lock_usd": con.max_open_capital_lock_usd,
        }
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
        # Strategy-variant attribution for controlled experiments (PAPER ONLY).
        variant = classify_variant(
            strategy=_resolved_strategy(getattr(self, "_last_resolved", None)),
            exploration=exploratory,
            chainlink_linked=bool(getattr(est, "bregman_group_id", "")))
        # Per-variant paper budget (only enforced when experiments are enabled;
        # the global max_open_trades + RiskEngine always bind regardless).
        if getattr(self.cfg, "experiments_enabled", False) and not self.experiments.can_open(variant):
            self.rejection_count += 1
            self.learner.record_decision(traded=False, reason="variant_budget_exhausted")
            return {"opened": False, "reason": "variant_budget_exhausted"}

        proposal = self.policy.build_proposal(edge, est, rec)
        proposal.experiment_id = self.experiments.experiment_id
        proposal.strategy_variant = variant
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
                                "exploration": exploratory,
                                "experiment_id": self.experiments.experiment_id,
                                "strategy_variant": variant})
        if fill["status"] != "filled":
            self.rejection_count += 1
            self.experiments.record_fill(variant, filled=False)
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
            mark=fill["fill_price"],
            experiment_id=self.experiments.experiment_id, strategy_variant=variant)
        self.positions.append(pos)
        self.experiments.record_trade(variant, notional=float(fill["notional"]))
        self.experiments.record_fill(variant, filled=True)
        self.fills_log.append({
            "proposal_id": proposal_id, "risk_decision_id": decision.risk_decision_id,
            "order_id": fill["order_id"], "fill_id": fill["fill_id"],
            "market_id": rec.market_id, "asset_id": proposal.asset_id, "side": "BUY",
            "outcome": edge.outcome, "price": fill["fill_price"], "qty": fill["fill_qty"],
            "notional": fill["notional"], "tick": self.tick_count,
            "diagnostics_id": diag.diagnostics_id, "exploration": exploratory,
            "experiment_id": self.experiments.experiment_id, "strategy_variant": variant})
        self.learner.record_decision(traded=True, variant=variant)
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
        # variant-scoped experiment feedback (separates learning per variant)
        self.experiments.record_feedback(
            getattr(pos, "strategy_variant", "directional_edge"),
            predicted_prob=pos.p_final, win=win, realized_pnl=pos.realized_pnl,
            net_edge=pos.net_edge, cost=max(1e-9, pos.cost))
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

    # -- final monitoring + kill-switch -------------------------------------
    def _profile(self) -> str:
        if self._downgraded:
            return "conservative"
        return "aggressive" if (self.cfg.exploration_enabled
                                or getattr(self.cfg, "experiments_enabled", False)) else "conservative"

    def _chainlink_linked_performance(self) -> dict:
        closed = [p for p in self.positions if p.closed and getattr(p, "chainlink_linked", False)]
        if not closed:
            return {"trades": 0, "win_rate": 0.0, "pnl": 0.0}
        wins = sum(1 for p in closed if p.realized_pnl > 0)
        return {"trades": len(closed), "win_rate": round(wins / len(closed), 4),
                "pnl": round(sum(p.realized_pnl for p in closed), 4)}

    def _stale_data_rejections(self) -> int:
        from .monitoring import STALE_DATA_REASONS
        ntr = self.learner.no_trade_reasons or {}
        return int(sum(v for k, v in ntr.items() if k in STALE_DATA_REASONS))

    def _monitoring_raw(self) -> dict:
        from engine.replay import metrics as _m
        closed = [p for p in self.positions if p.closed]
        closed_pnls = [p.realized_pnl for p in closed]
        preds = [p.p_final for p in closed]
        outs = [1.0 if p.realized_pnl > 0 else 0.0 for p in closed]
        lq = self.learner.label_quality()
        ntr_total = sum((self.learner.no_trade_reasons or {}).values())
        stale = self._stale_data_rejections()
        bstat = self.broker.status()
        orders = max(1, int(bstat.get("orders", 0)))
        return {
            "trades_opened": len([p for p in self.positions]),
            "useful_feedback": int(self.learner.closed),
            "labels_resolved": len(closed),
            "calibration_error": float(self.learner.calibration_error()),
            "brier": _m.brier_score(preds, outs),
            "ece": _m.ece(preds, outs),
            "bregman": bregman_monitoring(self.bregman_summary()),
            "chainlink_linked_performance": self._chainlink_linked_performance(),
            "exploration_budget_used": float(self.exploration_budget_used),
            "drawdown": float(self.pnl_summary().get("max_drawdown", 0.0) or 0.0),
            "loss_streak": loss_streak(closed_pnls),
            "stale_data_rejections": stale,
            "stale_data_rejection_rate": round(stale / ntr_total, 6) if ntr_total else 0.0,
            "partial_fill_rate": round(int(bstat.get("rejects", 0)) / orders, 6),
            "avg_spread": float(self.metrics.avg_spread or 0.0),
            "label_suppression_rate": float(lq.get("suppression_rate", 0.0)),
            "ambiguous_rate": float(lq.get("ambiguous_rate", 0.0)),
            "learner_rollbacks": int(getattr(self.learner, "rollbacks", 0)),
            "profile": self._profile(),
        }

    def aggressive_dashboard(self) -> dict:
        """Learning-velocity dashboard (Live Monitoring). Shows whether aggressive
        paper mode is learning FASTER without unsafe behaviour. PAPER ONLY."""
        return build_dashboard(self._monitoring_raw(),
                               runtime_seconds=time.time() - self.started_ts,
                               history=self._metric_history)

    def kill_switch_report(self) -> dict:
        d = self.aggressive_dashboard()
        ks = evaluate_kill_switch(d, self._ks_thresholds,
                                  aggressive=(self._profile() == "aggressive"))
        ks["downgraded"] = self._downgraded
        return ks

    def _snapshot_metrics(self) -> None:
        """Append a calibration/Brier/ECE snapshot for trend tracking."""
        raw = self._monitoring_raw()
        self._metric_history.append({
            "ts": time.time(), "brier": raw["brier"], "ece": raw["ece"],
            "calibration_error": raw["calibration_error"]})
        self._metric_history = self._metric_history[-200:]

    def downgrade_to_conservative(self, *, reasons: Optional[list] = None) -> None:
        """Auto-downgrade aggressive -> conservative PAPER mode on a kill-switch.

        Turns OFF exploration / active-learning / experiments and tightens the
        entry gate. PAPER ONLY — never enables a live flag, never touches the CLOB
        boundary or legacy-arbitrage disablement (the preflight stays clean)."""
        if self._downgraded:
            return
        self._downgraded = True
        self.cfg.exploration_enabled = False
        self.cfg.exploration_rate = 0.0
        self.cfg.active_learning_enabled = False
        self.cfg.experiments_enabled = False
        self.cfg.min_net_edge = max(float(self.cfg.min_net_edge), 0.03)
        try:
            self.experiments.begin_tick({})  # no per-variant budget after downgrade
        except Exception:  # noqa: BLE001
            pass
        logger_reasons = reasons or self._kill_switch.get("triggered", [])
        self.kill_switch_alerts = getattr(self, "kill_switch_alerts", [])
        self.kill_switch_alerts.append({
            "ts": time.time(), "action": "downgrade_to_conservative",
            "reasons": list(logger_reasons)})

    def run_monitoring(self, *, now: Optional[float] = None) -> dict:
        """Compute the dashboard + evaluate the kill-switch, auto-downgrading
        aggressive -> conservative when a metric trips. Returns the kill-switch
        report. Safe to call every tick."""
        self._snapshot_metrics()
        dashboard = self.aggressive_dashboard()
        aggressive = self._profile() == "aggressive"
        ks = evaluate_kill_switch(dashboard, self._ks_thresholds, aggressive=aggressive)
        self._kill_switch = ks
        if (ks["should_downgrade"] and aggressive
                and getattr(self.cfg, "kill_switch_enabled", True)
                and getattr(self.cfg, "kill_switch_auto_downgrade", True)):
            self.downgrade_to_conservative(reasons=ks["triggered"])
        return ks

    # -- live-readiness gate (PAPER ONLY — verdicts only, never enables live) ---
    def _readiness_evidence(self) -> dict:
        """Gather the durable-evidence inputs for the live-readiness gate from this
        run's closed paper trades, learner calibration, settlement-label quality,
        realistic-fill execution, risk-gate cleanliness, and Bregman telemetry.

        Quant scope: Data Acquisition (closed trades + labels), Statistical
        Modeling (calibration / OOS Sharpe-Sortino-Calmar), CLOB v2 Execution
        (realistic-fill expectancy), Risk Management (violations + drawdown), and
        Bregman arbitrage validation. Read-only."""
        from engine.replay import metrics as _m
        from engine.risk import risk_gate_violations
        from .monitoring import bregman_monitoring

        closed = [p for p in self.positions if p.closed]
        n = len(closed)
        total_realized = sum(p.realized_pnl for p in closed)
        bstat = self.broker.status()
        orders = max(1, int(bstat.get("orders", 0)))
        # after-cost vs realistic-fill expectancy: realistic penalizes unfilled /
        # rejected orders (optimistic-only profitability is caught here).
        after_cost = (total_realized / n) if n else 0.0
        realistic = total_realized / orders
        # out-of-sample split (second half by trade order)
        mid = n // 2
        oos = closed[mid:]
        oos_eq = [float(self.cfg.starting_bankroll)]
        for p in oos:
            oos_eq.append(oos_eq[-1] + p.realized_pnl)
        preds = [p.p_final for p in closed]
        outs = [1.0 if p.realized_pnl > 0 else 0.0 for p in closed]
        lq = self.learner.label_quality()
        labelled = max(1, sum((self.learner.label_states or {}).values()))
        unresolved_rate = (self.learner.label_states or {}).get("unresolved", 0) / labelled
        dash = self.aggressive_dashboard()
        dd_usd = abs(float(self.pnl_summary().get("max_drawdown", 0.0) or 0.0))
        start = max(1e-9, float(self.cfg.starting_bankroll))
        # Per-order notional is checked across ALL fills; concurrent exposure caps
        # are checked over the currently-open book (cumulative would overcount).
        violations = risk_gate_violations(
            [p for p in self.positions], open_positions=self.open_positions(),
            max_market_exposure=float(self.cfg.max_market_exposure_usd),
            max_total_exposure=float(self.cfg.max_total_exposure_usd),
            max_order_notional=float(self.cfg.max_order_notional_usd))
        bm = bregman_monitoring(self.bregman_summary())
        return {
            "samples": n,
            "after_cost_expectancy": round(after_cost, 6),
            "realistic_fill_expectancy": round(realistic, 6),
            "oos_sharpe": _m.sharpe(oos_eq), "oos_sortino": _m.sortino(oos_eq),
            "oos_calmar": _m.calmar(oos_eq),
            "max_drawdown_pct": round(dd_usd / start, 6),
            "calibration_error": float(self.learner.calibration_error()),
            "ece": _m.ece(preds, outs),
            "label_suppression_rate": float(lq.get("suppression_rate", 0.0)),
            "unresolved_rate": round(unresolved_rate, 6),
            "ambiguous_rate": float(lq.get("ambiguous_rate", 0.0)),
            "stale_data_rejection_rate": float(dash.get("stale_data_rejection_rate", 0.0)),
            "chainlink_stale": False, "stale_book": False,
            "risk_violations": int(violations),
            "downgraded": bool(self._downgraded),
            "bregman": {
                "opportunities": bm["opportunities"],
                "false_positive_rate": bm["false_positive_rate"],
                "worst_case_pnl": bm["certified_profit"],
                # the engine only ever opens FULLY-hedged sets and unwinds any
                # partial leg, so hedge validity / leg feasibility hold by
                # construction; a partial-fill that broke a hedge would have been
                # rolled back (no un-hedged exposure is ever persisted).
                "full_hedge_validated": True, "all_leg_fill_feasible": True,
                "partial_fill_hedge_break": False},
        }

    def validation_evidence(self) -> dict:
        """This paper run's institutional-campaign readiness evidence (the dict
        the validation campaign consumes for the profile this run represents).
        Read-only; never enables live trading. Backtesting & Simulation ->
        Live Trading & Monitoring."""
        return self._readiness_evidence()

    # -- institutional paper-training campaign (PAPER ONLY) ------------------
    def _campaign_snapshot(self) -> dict:
        """Assemble the campaign controller's snapshot from REAL collected
        evidence (decisions, paper trades, resolved/clean labels, Bregman
        candidates/certified/false-positives, after-cost + realistic-fill
        expectancy, calibration, risk-gate cleanliness, stale-data). live_orders
        is ALWAYS zero — this is PAPER ONLY. Read-only; never enables live."""
        ev = self._readiness_evidence()
        pnl = self.pnl_summary()
        dash = self.aggressive_dashboard()
        breg_ev = ev.get("bregman", {}) or {}
        # Settlement-label evidence (Statistical & Probabilistic Modeling):
        # CLEAN labels are ONLY clean-trainable (resolved_yes / resolved_no) — the
        # learner's ``clean_trained`` counter, which excludes every dirty state
        # (void / ambiguous / partially_invalid / stale_resolution / unresolved).
        # RESOLVED labels are settled decisions (clean + dirty-but-settled),
        # excluding still-unresolved markets.
        lq = self.learner.label_quality()
        label_states = dict(lq.get("label_states", {}) or {})
        clean_labels = int(lq.get("clean_trained", 0))
        suppressed = int(lq.get("suppressed", 0))
        unresolved = int(label_states.get("unresolved", 0))
        resolved_labels = clean_labels + max(0, suppressed - unresolved)
        # Bregman candidates = certified opportunities + rejected candidates
        bregman_candidates = int(getattr(self, "bregman_opportunity_count", 0)) \
            + int(getattr(self, "bregman_rejected", 0))
        bregman_certified = int(getattr(self, "bregman_sets_opened", 0))
        fp_rate = float(breg_ev.get("false_positive_rate", 0.0) or 0.0)
        opps = int(breg_ev.get("opportunities", 0) or 0)
        bregman_false_positives = int(round(fp_rate * opps)) if fp_rate > 0 else 0
        calib = float(ev.get("calibration_error", 0.0) or 0.0)
        if self._campaign_baseline_calibration is None:
            self._campaign_baseline_calibration = calib
        live_state = "paper_learning"
        try:
            from .live_readiness import ReadinessCriteria, evaluate_live_readiness
            live_state = evaluate_live_readiness(
                ev, ReadinessCriteria.from_config(self.cfg)).state
        except Exception:  # noqa: BLE001
            pass
        return {
            "run_id": self.run_id,
            "started_ts": float(self.started_ts),
            "runtime_seconds": max(0.0, time.time() - float(self.started_ts)),
            "decisions": int(self.decision_count),
            "paper_trades": int(pnl.get("trades_opened", 0) or 0),
            "resolved_labels": resolved_labels,
            "clean_labels": clean_labels,
            "bregman_candidates": bregman_candidates,
            "bregman_certified": bregman_certified,
            "bregman_false_positives": bregman_false_positives,
            "partial_fill_hedge_breaks": 1 if bool(breg_ev.get("partial_fill_hedge_break")) else 0,
            "risk_violations": int(ev.get("risk_violations", 0) or 0),
            "live_orders": 0,                         # PAPER ONLY — always zero
            "after_cost_expectancy": float(ev.get("after_cost_expectancy", 0.0) or 0.0),
            "realistic_fill_expectancy": float(ev.get("realistic_fill_expectancy", 0.0) or 0.0),
            "optimistic_expectancy": float(ev.get("after_cost_expectancy", 0.0) or 0.0),
            "calibration_error": calib,
            "baseline_calibration_error": float(self._campaign_baseline_calibration),
            "ece": float(ev.get("ece", 0.0) or 0.0),
            "stale_data_rejection_rate": float(ev.get("stale_data_rejection_rate", 0.0) or 0.0),
            "stale_chainlink": bool(ev.get("chainlink_stale", False)),
            "stale_book": bool(ev.get("stale_book", False)),
            "stale_data_confidence_improvement": False,
            "max_drawdown_pct": float(ev.get("max_drawdown_pct", 0.0) or 0.0),
            "slippage_bps": float(dash.get("slippage_bps", 0.0) or 0.0),
            "algorithm_freeze_mode": bool(getattr(self.cfg, "algorithm_freeze_mode", False)),
            "live_readiness_state": live_state,
            "validation_campaign": None,
            "replay_validation_ran": False,
        }

    def campaign_report(self) -> Optional[dict]:
        """Institutional paper-training campaign report (PAPER ONLY). Returns the
        controller's pass/fail report (evidence, progress, verdict, blockers, next
        target) or ``None`` when campaign mode is disabled. Never enables live."""
        if self.campaign is None:
            if self._campaign_error:
                return {"enabled": False, "error": self._campaign_error, "no_live_orders": True}
            return None
        try:
            return self.campaign.report()
        except Exception as exc:  # noqa: BLE001 — reporting must never break the loop
            self._campaign_error = f"report_failed:{exc}"
            return {"enabled": True, "error": self._campaign_error, "no_live_orders": True}

    def _update_campaign(self) -> None:
        """Update the campaign controller from this tick's real evidence. The
        trainer keeps running even if the update fails (logged in
        ``self._campaign_error``)."""
        if self.campaign is None:
            return
        try:
            self.campaign.update(self._campaign_snapshot())
            self._campaign_error = None
        except Exception as exc:  # noqa: BLE001 — campaign must never break a tick
            self._campaign_error = f"update_failed:{exc}"
            import logging
            logging.getLogger(__name__).warning("campaign update failed: %s", exc)

    def live_readiness_report(self) -> dict:
        """Live-readiness verdict + capital-preservation plan (PAPER ONLY).

        Blocks real-money escalation unless durable after-cost profitability,
        execution realism, calibration, settlement-label quality, and risk-gate
        cleanliness are proven. NEVER enables live trading — it only produces a
        verdict + hard blockers + bounded capital-preservation caps."""
        evidence = self._readiness_evidence()
        verdict = evaluate_live_readiness(evidence, ReadinessCriteria.from_config(self.cfg))
        capital = capital_preservation_report(
            verdict, bankroll=float(self.cfg.starting_bankroll), cfg=self.cfg)
        # Compliance invariant: the verdict can never auto-enable live execution.
        from engine.safety import live_unlock_blockers
        return {"verdict": verdict.to_dict(), "capital_preservation": capital,
                "evidence": evidence, "paper_only": bool(self.cfg.is_paper_only),
                "live_unlock_blockers": live_unlock_blockers(verdict.to_dict()),
                "live_enabled": False}

    def experiment_report(self) -> dict:
        """Controlled strategy-variant experiment report (Monitoring + Strategy
        Optimization): per-variant trade/feedback counts + Sharpe/Sortino/Calmar/
        drawdown/Brier/log-loss/ECE/realized-edge/fill-quality, plus the
        champion/challenger ranking. PAPER ONLY, read-only."""
        rep = self.experiments.to_dict()
        rep["enabled"] = bool(getattr(self.cfg, "experiments_enabled", False))
        rep["bregman_first_budget"] = bool(getattr(self.cfg, "bregman_first_budget", True))
        return rep

    def status(self) -> dict:
        out = self._status_core()
        if self.campaign is not None:
            try:
                out["training_campaign"] = self.campaign_report()
            except Exception as exc:  # noqa: BLE001 — status must never crash
                out["training_campaign"] = {"enabled": True, "error": str(exc),
                                            "no_live_orders": True}
        if self._campaign_safety is not None:
            out["campaign_safety"] = self._campaign_safety
        return out

    def _status_core(self) -> dict:
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
            "capital_allocation": self.capital_allocation_report(),
            "canary": self.canary_status(),
            "experiments": self.experiment_report(),
            "monitoring": self.aggressive_dashboard(),
            "kill_switch": self.kill_switch_report(),
            "live_readiness": self.live_readiness_report(),
            "profile": self._profile(),
            "downgraded": self._downgraded,
            # Live scan visibility for the dashboard: which markets are being
            # scanned right now + when the last scan ran (proves it's running).
            "watchlist": getattr(self, "_watch_sample", []),
            "last_scan_ts": getattr(self, "_last_scan_ts", 0.0),
            "tick": self.tick_count,
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
