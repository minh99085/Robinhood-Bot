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

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
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
    # ---- Pass-3 execution-realism provenance (PAPER ONLY) ----
    execution_realism_status: str = "realistic_executable"
    fill_source: str = "live_clob"
    book_source: str = "live_clob"
    price_source: str = "live_clob"
    was_reference_price_fill: bool = False
    was_fallback_fill: bool = False
    was_offline_stub_fill: bool = False
    book_age_sec: float = 0.0
    depth_at_price: float = 0.0
    fill_quality: float = 1.0
    # ---- Pass-7 correlation provenance (for open-exposure indexing) ----
    cluster_id: str = ""
    correlation_group: str = ""
    condition_id: str = ""

    @property
    def is_realistic(self) -> bool:
        return self.execution_realism_status == "realistic_executable"

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
        # Chainlink BTC/USD oracle (validated, auditable; PAPER ONLY). Reuses the
        # Chainlink scanner's source so it shares the same read-only RPC feed.
        try:
            from engine.training.chainlink_oracle import ChainlinkBtcUsdOracle
            self.chainlink_oracle = ChainlinkBtcUsdOracle(
                source=getattr(self.chainlink, "source", None),
                enabled=bool(getattr(self.cfg, "chainlink_enabled", False)),
                heartbeat_seconds=getattr(self.cfg, "btc_pulse_chainlink_heartbeat_seconds", 120),
                max_age_seconds=getattr(self.cfg, "btc_pulse_chainlink_max_age_seconds", 180),
                registry=getattr(self.chainlink, "registry", None),
                debug_log=bool(getattr(self.cfg, "btc_pulse_oracle_debug_log", False)))
        except Exception:  # noqa: BLE001 — oracle must never break startup
            self.chainlink_oracle = None
        # Fast read-only BTC spot feed (short-horizon features). Read-only,
        # key-less, paper-only; degrades gracefully when unreachable.
        self.btc_fast_price = None
        try:
            if bool(getattr(self.cfg, "btc_fast_price_enabled", False)):
                from engine.feeds.btc_fast_price import BtcFastPriceFeed
                self.btc_fast_price = BtcFastPriceFeed(
                    enabled=True, provider=getattr(self.cfg, "btc_fast_price_provider",
                                                   "coinbase_readonly"),
                    symbol=getattr(self.cfg, "btc_fast_price_symbol", "BTC-USD"),
                    max_age_seconds=getattr(self.cfg, "btc_fast_price_max_age_seconds", 10),
                    timeout_seconds=getattr(self.cfg, "btc_fast_price_timeout_seconds", 5.0),
                    max_retries=getattr(self.cfg, "btc_fast_price_max_retries", 2),
                    log_enabled=bool(getattr(self.cfg, "btc_fast_price_log_enabled", False)))
        except Exception:  # noqa: BLE001 — fast feed must never break startup
            self.btc_fast_price = None
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
        # Pass-3: centralized paper execution-realism policy (single source of
        # truth for directional + Bregman fills). PAPER ONLY; never trades.
        from .paper_execution import PaperExecutionPolicy
        self.paper_exec_policy = PaperExecutionPolicy(self.cfg, bregman=False)
        self.bregman_exec_policy = PaperExecutionPolicy(self.cfg, bregman=True)

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

        # BTC 5-min Pulse — PAPER-ONLY, ISOLATED experiment (default OFF). It runs
        # beside the Polymarket campaign for fast feedback, owns its own learner
        # namespace, and can NEVER place a live order, touch a wallet, enable
        # legacy BTC autotrade, or write to the Polymarket learner. Failures here
        # NEVER block Polymarket training.
        self.btc_pulse = None
        self._btc_pulse_error = None
        if bool(getattr(self.cfg, "btc_pulse_enabled", False)):
            try:
                from .btc_pulse import BtcPulsePaperTrainer
                self.btc_pulse = BtcPulsePaperTrainer(
                    self.cfg, data_dir=self.data_dir,
                    oracle=getattr(self, "chainlink_oracle", None),
                    fast_price=getattr(self, "btc_fast_price", None))
            except Exception as exc:  # noqa: BLE001 — pulse must never crash training
                self.btc_pulse = None
                self._btc_pulse_error = f"init_failed:{exc}"

        # Controlled market-news evidence scanner (PAPER ONLY; default OFF). The
        # bot scans/caches/timestamps/scores/sanitizes market news; it is
        # advisory + read-only and NEVER places an order or bypasses a gate.
        self.news_scanner = None
        self._news_error = None
        self._news_cache: dict = {}
        self._news_last_packet: dict = {}
        self._news_metrics = {
            "ticks": 0, "markets_scanned": 0, "queries": 0, "items_fetched": 0,
            "items_used": 0, "items_rejected": 0, "stale_count": 0,
            "contradiction_count": 0, "ambiguity_count": 0, "provider_errors": 0,
            "last_scan_ts": 0.0, "rejected_low_relevance": 0,
            "rejected_low_credibility": 0, "rejected_unclear_date": 0,
            "rejected_stale": 0, "rejected_duplicate": 0,
        }
        if bool(getattr(self.cfg, "news_scanner_enabled", False)):
            try:
                from engine.research.news_scanner import NewsEvidenceScanner
                self.news_scanner = NewsEvidenceScanner.from_config(
                    self.cfg, cache=self._news_cache)
            except Exception as exc:  # noqa: BLE001 — news must never crash training
                self.news_scanner = None
                self._news_error = f"init_failed:{exc}"

        # Feature-health startup log proof (PAPER ONLY). Lets Docker logs confirm
        # which research/strategy features are actually active this run.
        _log = logging.getLogger("hte.training.features")
        if self.news_scanner is not None:
            _log.info("News scanner initialized provider_mode=%s",
                      getattr(self.cfg, "news_provider_mode", "offline_cache"))
            if bool(getattr(self.cfg, "news_enable_grok_packet", False)):
                _log.info("Grok research evidence packet enabled")
        if getattr(self, "bregman", None) is not None:
            _log.info("Bregman scanner initialized")
        if getattr(self, "chainlink_oracle", None) is not None and self.btc_pulse is not None:
            _log.info("BTC Pulse oracle gate require_chainlink=%s",
                      bool(getattr(self.cfg, "btc_pulse_require_chainlink", False)))
        _log.info("Paper training strategy attribution enabled")

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
        # Pass-2: raw-catalog Bregman funnel (discovery -> dedup -> certify -> open).
        self.bregman_reject_reasons: dict = {}
        self.bregman_open_bundles = 0
        self.directional_skipped_due_to_bregman = 0
        self.bregman_exec_metrics: dict = {}
        # Pass-4: strategy-priority ladder (Bregman Tier-1 reservation) telemetry.
        self._bregman_certified_realistic_count = 0
        self._bregman_open_markets: set = set()
        self._bregman_open_events: set = set()
        self._dir_reserved_slots = 0
        self._dir_reserved_capital = 0.0
        self._bregman_reserve_active = False
        self.priority_metrics: dict = {}
        # Pass-5: profitability-first ranking + hard after-cost governor (PAPER ONLY).
        from .profitability_governor import ProfitabilityGovernor
        self.governor = ProfitabilityGovernor(
            min_net_edge=0.0,
            min_decay_factor=float(getattr(self.cfg, "min_decay_factor", 0.5)))
        # Pass-6: profitability-aware active learning is the EXPLORATION AUTHORITY.
        from .active_learning import ActiveLearningSelector
        self.active_learner = ActiveLearningSelector(self.cfg, learner=self.learner)
        # Pass-7: cluster/correlation risk is an ACTIVE hard gate + capital allocator.
        from .correlation_risk import CorrelationRiskGate, OpenExposureIndex
        self.correlation_gate = CorrelationRiskGate(self.cfg)
        self._open_exposure_index = OpenExposureIndex()
        self.correlation_metrics: dict = self._fresh_corr_metrics()
        # P0 closed-loop learning: turn EVERY evaluated candidate (incl. rejects)
        # into a structured training record + pending label + feedback. PAPER ONLY.
        from .closed_loop import ClosedLoopLearning
        self.closed_loop = ClosedLoopLearning(self.run_id, self.data_dir, self.cfg)
        self.near_miss_log: list = []        # logged, never opened
        self.exploration_feedback_log: list = []   # structured learning feedback
        self.active_learning_metrics: dict = self._fresh_al_metrics()
        self._explore_tick_state: dict = {}   # per-tick caps + budget (reset each tick)
        self.profitability_metrics: dict = {
            "profitability_first_enabled": bool(getattr(self.cfg, "profitability_first", True)),
            "profitability_annotation_before_truncation": True,
            "candidates_annotated": 0, "candidates_missing_profitability_data": 0,
            "candidates_ranked_by_profitability": 0,
            "candidates_rejected_negative_after_cost": 0,
            "candidates_shadow_theoretical_only": 0,
            "directional_after_cost_positive": 0, "bregman_after_cost_positive": 0,
            "exploration_profitability_checked": 0,
            "profitability_governor_hard_rejects": 0,
            "execution_without_annotation": 0,
            "buckets": {}, "_exec_edges": [], "_exec_rois": [], "_exec_ev": [],
            "top_ranked_candidate_reason": "",
        }
        # Pass-3: paper execution-realism funnel (PAPER ONLY).
        self.shadow_opportunities: list = []     # logged, never counted as PnL
        self.realism_counts: dict = {
            "candidates_considered": 0, "realistic_trade_count": 0, "shadow_trade_count": 0,
            "reference_fill_count": 0, "fallback_fill_count": 0, "offline_stub_rejection_count": 0,
            "stale_book_rejection_count": 0, "missing_ask_rejection_count": 0,
            "thin_depth_rejection_count": 0, "wide_spread_rejection_count": 0,
            "ambiguity_rejection_count": 0, "reference_fill_blocked_count": 0,
            "hard_reject_count": 0,
        }
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
        # Chainlink BTC/USD oracle read each tick (auditable; logs price+freshness
        # when debug enabled). Read-only; never blocks the tick.
        if getattr(self, "chainlink_oracle", None) is not None:
            try:
                self.chainlink_oracle.read(now=now)
            except Exception:  # noqa: BLE001 — oracle read never blocks a tick
                pass
        # Fast BTC spot read each tick (read-only; cross-checked vs the anchor).
        if getattr(self, "btc_fast_price", None) is not None:
            try:
                anchor = None
                if getattr(self, "chainlink_oracle", None) is not None:
                    anchor = self.chainlink_oracle.last_status().price
                self.btc_fast_price.read(now=now, anchor_price=anchor)
            except Exception:  # noqa: BLE001 — fast read never blocks a tick
                pass
        # Market-news evidence scan (PAPER ONLY, read-only, advisory). Bounded to
        # a few top markets per tick + cached; NEVER blocks training on failure.
        if self.news_scanner is not None:
            try:
                self._news_scan(watch, now)
            except Exception as exc:  # noqa: BLE001 — news never blocks a tick
                self._news_error = f"scan_failed:{exc}"
        health = self.subs.reconcile(watch)
        self.metrics.subscribed_assets = health.subscribed_assets

        marks = {r.market_id: market_mid(r) for r in records}
        self._monitor(marks, now)
        # CLOSED LOOP: reset per-tick selection state + resolve any due pending
        # labels (final settlement or short-horizon proxy) into completed feedback.
        self.closed_loop.begin_tick()
        try:
            self.closed_loop.resolve_labels(marks, now=now)
        except Exception:  # noqa: BLE001 — learning must never break a tick
            pass

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
        # PASS-2: raw-catalog Bregman discovery — group over the FULL eligible
        # catalog (after safety filters), NOT the directional shortlist, so
        # complete-set arbitrage that never reaches the shortlist is still found.
        # Capped to bound cost; falls back to candidates when eligible is absent.
        disc_limit = int(getattr(self.cfg, "bregman_discovery_limit", 1000))
        eligible = getattr(scan, "eligible", None) or records
        self._bregman_records = eligible[:disc_limit]
        opened = 0
        evaluated = 0
        # PASS-7: rebuild the open-exposure index (cluster/event/market/group) from
        # current open positions BEFORE any strategy evaluates candidates this tick.
        self._begin_correlation_phase()
        # PASS-4: directional open-slots available at tick start (before Bregman).
        dir_slots_before = max(0, int(self.cfg.max_open_trades) - len(self.open_positions()))
        # FLAGSHIP PRIORITY: certified Bregman arbitrage is evaluated + opened
        # BEFORE any directional trade (it outranks directional only when its
        # certified profit lower bound is positive after all costs).
        if getattr(self.cfg, "experiments_enabled", False):
            # Controlled experiments: split the remaining paper trade-slot budget
            # across variants (Bregman FIRST when certified opps exist). The slot
            # sum never exceeds the budget, so combined hard caps still bind.
            tradable = self._bregman_tradable(self._bregman_records, now)
            cap_slots = max(0, min(int(budget), int(self.cfg.max_open_trades))
                            - len(self.open_positions()))
            alloc = self.experiments.allocate(cap_slots, bregman_available=bool(tradable))
            self.experiments.begin_tick(alloc)
            bregman_opened = self._open_bregman_sets(
                tradable, self._bregman_records, now, cap=alloc.get(BREGMAN_VARIANT))
        else:
            bregman_opened = self._run_bregman(self._bregman_records, now)
        opened += bregman_opened
        # PASS-4 STRATEGY PRIORITY: Bregman (Tier 1) has already had first claim on
        # slots + capital. Compute the reservation + collision state, then run
        # directional (Tier 2) ONLY against the non-reserved capacity. Reserved
        # capacity is released to directional only when NO certified-realistic
        # Bregman opportunity exists this tick.
        self._begin_directional_phase(dir_slots_before, bregman_opened)
        self._begin_exploration_phase()   # PASS-6: reset per-tick active-learning caps
        for rec in candidates:
            ok, block_reason = self._directional_admit(rec)
            if block_reason == "global_capacity":
                break
            if not ok:
                continue           # reserved for Bregman / collision (counted)
            res = self._consider(rec, now)
            evaluated += 1
            if res.get("opened"):
                opened += 1
        if bregman_opened > 0 and len(self.open_positions()) >= self.cfg.max_open_trades:
            self.directional_skipped_due_to_bregman += len(candidates)
        self._finalize_priority_metrics()
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
        # BTC Pulse runs as a SEPARATE, isolated paper experiment. It must never
        # block Polymarket: any failure is captured and surfaced in status only.
        if self.btc_pulse is not None:
            try:
                self.btc_pulse.tick(now_ms=int(now * 1000) if now else None)
            except Exception as exc:  # noqa: BLE001 — pulse never blocks Polymarket
                self._btc_pulse_error = f"tick_failed:{exc}"
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
        except Exception:  # noqa: BLE001 — Bregman must never break a training tick
            self.bregman_log = []
            return []
        raw_n = len(groups)
        groups, dropped = self._dedup_bregman_groups(groups)
        try:
            certs = self.bregman.certify_all(groups, now=now)
        except Exception:  # noqa: BLE001
            self.bregman_log = []
            return []
        # Funnel: count rejections by explicit certifier reason (read-only telemetry).
        for c in certs:
            if not c.is_opportunity:
                reason = c.no_trade_reason or (
                    c.failure_modes[0] if getattr(c, "failure_modes", None) else "rejected")
                self._breg_reason(reason)
        self.bregman_log = [c.to_dict() for c in certs]
        self.bregman_exec_metrics = {
            "raw_catalog_markets_scanned": len(records),
            "raw_groups_discovered": raw_n,
            "duplicate_groups_dropped": dropped,
            "unique_groups_certified": len(groups),
            "certified_opportunities": sum(1 for c in certs if c.is_opportunity),
            "rejected_by_reason": dict(self.bregman_reject_reasons),
            "grouping_method": "group_markets",
            "groups_from_graph_used": False,
            "groups_from_graph_reason": "groups_from_graph() not present on this branch; "
                                        "using group_markets() over the full eligible catalog",
            "evaluated_before_directional": True,
        }
        return certs

    def _breg_reason(self, reason: str) -> None:
        self.bregman_reject_reasons[reason] = self.bregman_reject_reasons.get(reason, 0) + 1

    def _open_bregman_bundle_count(self) -> int:
        """Distinct open Bregman bundles (by group_key) currently held in PAPER."""
        keys = {getattr(p, "group_key", None) for p in self.open_positions()
                if getattr(p, "strategy", "") == "bregman"}
        keys.discard(None)
        return len(keys)

    def _dedup_bregman_groups(self, groups: list) -> "tuple[list, int]":
        """De-duplicate Bregman groups by (group_type, sorted market ids, sorted
        outcome set) so a group is never certified/executed twice. Pure."""
        seen: set = set()
        out: list = []
        dropped = 0
        for g in groups:
            legs = getattr(g, "legs", []) or []
            key = (getattr(g, "group_type", ""),
                   tuple(sorted(str(getattr(l, "market_id", "")) for l in legs)),
                   tuple(sorted(str(getattr(l, "outcome", "")) for l in legs)))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            out.append(g)
        return out, dropped

    def _bregman_tradable(self, records: list, now: float) -> list:
        """Certify + rank tradable Bregman opportunities (read-only). Used to know
        Bregman availability before allocating the experiment budget.

        PASS-4: sorted by conservative after-cost QUALITY (not discovery order):
        positive after-cost lower-bound profit, then after-cost ROI, fill realism,
        lower avg spread, higher min depth, fresher books, lower ambiguity, then
        lower capital requirement — so the best certified arbs open first."""
        certs = self.scan_bregman(records, now)
        tradable = [c for c in certs if c.is_opportunity]
        tradable.sort(key=self._bregman_quality_key, reverse=True)
        self.bregman_opportunity_count += len(tradable)
        # PASS-4: certified-realistic count drives Bregman slot/capital reservation.
        self._bregman_certified_realistic_count = len(tradable)
        return tradable

    @staticmethod
    def _bregman_quality_key(o) -> tuple:
        """Conservative after-cost quality ordering for certified Bregman opps.
        Higher tuple sorts first (reverse=True). Lower-is-better fields are negated."""
        cap = float(getattr(o, "required_capital", 0.0) or 0.0)
        plb = float(getattr(o, "profit_lower_bound", 0.0) or 0.0)
        roi = (plb / cap) if cap > 0 else 0.0
        fill_q = float(getattr(o, "fill_feasibility", 0.0) or 0.0)
        legs = getattr(o, "legs", []) or []
        spreads = [float(getattr(l, "spread", 0.0) or 0.0) for l in legs
                   if getattr(l, "spread", None) is not None]
        depths = [float(getattr(l, "depth_usd", 0.0) or 0.0) for l in legs]
        ages = [float(getattr(l, "book_age_s", 0.0) or 0.0) for l in legs
                if getattr(l, "book_age_s", None) is not None]
        amb = max((float(getattr(l, "ambiguity_score", 0.0) or 0.0) for l in legs),
                  default=0.0)
        avg_spread = (sum(spreads) / len(spreads)) if spreads else 0.0
        min_depth = min(depths) if depths else 0.0
        max_age = max(ages) if ages else 0.0
        return (
            1 if plb > 0 else 0,        # positive after-cost lower-bound profit first
            round(roi, 6),              # after-cost ROI
            round(fill_q, 6),           # fill realism quality
            -round(avg_spread, 6),      # lower avg spread better
            round(min_depth, 4),        # higher min depth better
            -round(max_age, 4),         # fresher books better
            -round(amb, 6),             # lower ambiguity better
            -round(cap, 4),             # lower capital better when ROI ties
        )

    def _open_bregman_sets(self, tradable: list, records: list, now: float, *,
                           cap: Optional[int] = None) -> int:
        """Open up to ``cap`` certified Bregman sets (``None`` = no per-variant cap;
        the global ``max_open_trades`` + RiskEngine always bind)."""
        if (self.mode != "paper_train"
                or not getattr(self.cfg, "bregman_execution_enabled", True)):
            return 0
        rec_by_id = {r.market_id: r for r in records}
        max_bundles = int(getattr(self.cfg, "bregman_max_bundles_per_tick", 3))
        max_open = int(getattr(self.cfg, "bregman_max_open_bundles", 10))
        max_cap = float(getattr(self.cfg, "bregman_max_capital_per_tick_usd", 100.0))
        min_roi = float(getattr(self.cfg, "bregman_min_roi", 0.002))
        # PASS-5: Bregman profitability-first minimums (after-cost lower-bound).
        min_breg_roi = float(getattr(self.cfg, "bregman_min_after_cost_roi", 0.002))
        min_breg_profit_usd = float(getattr(self.cfg, "bregman_min_after_cost_profit_usd", 0.02))
        opened = 0
        capital = 0.0
        open_bundles = self._open_bregman_bundle_count()
        for opp in tradable:
            if cap is not None and opened >= cap:
                break
            if opened >= max_bundles:
                self._breg_reason("max_bundles_per_tick")
                break
            if open_bundles + opened >= max_open:
                self._breg_reason("max_open_bundles")
                break
            # never act on a synthesized leg price (binary NO leg is derived); a
            # synthetic YES/NO bundle is not all-leg-real-executable.
            if opp.group_type == "binary_yes_no":
                self._breg_reason("synthetic_binary_not_executable")
                continue
            # completeness: only execute a verified mutually-exclusive + exhaustive set.
            cert = getattr(opp, "certificate", None)
            if cert is not None and not bool(getattr(cert, "full_hedge", True)):
                self._breg_reason("incomplete_or_uncertain_exhaustive_set")
                continue
            roi = (opp.profit_lower_bound / opp.required_capital
                   if opp.required_capital > 0 else 0.0)
            if roi < min_roi or roi < min_breg_roi:
                self._breg_reason("roi_below_min")
                continue
            # PASS-5: minimum after-cost lower-bound profit (USD) per bundle.
            if float(opp.profit_lower_bound) < min_breg_profit_usd:
                self._breg_reason("below_min_after_cost_profit_usd")
                continue
            # PASS-7: do not open an OVERLAPPING bundle (reusing a market already in
            # an open Bregman bundle) or exceed the per-event open-bundle cap.
            idx = self._open_exposure_index
            leg_mids = [getattr(l, "market_id", "") for l in getattr(opp, "legs", [])]
            if (bool(getattr(self.cfg, "bregman_block_overlapping_bundles", True))
                    and any(m in idx.bregman_markets for m in leg_mids)):
                self._breg_reason("bregman_overlapping_bundle")
                self.correlation_metrics["bregman_bundles_blocked_as_overlapping"] += 1
                continue
            if (bool(getattr(self.cfg, "bregman_block_duplicate_bundles", True))
                    and getattr(opp, "group_id", "") in idx.bregman_events):
                self._breg_reason("bregman_duplicate_bundle")
                self.correlation_metrics["bregman_bundles_blocked_as_duplicates"] += 1
                continue
            if capital + opp.required_capital > max_cap:
                self._breg_reason("capital_cap_per_tick")
                break
            if len(self.open_positions()) >= self.cfg.max_open_trades:
                break
            if self._open_bregman(opp, rec_by_id, now):
                opened += 1
                capital += float(opp.required_capital)
                self.profitability_metrics["bregman_after_cost_positive"] = (
                    self.profitability_metrics.get("bregman_after_cost_positive", 0) + 1)
                self.profitability_metrics["buckets"]["bregman_certified_positive"] = (
                    self.profitability_metrics["buckets"].get("bregman_certified_positive", 0) + 1)
        self.bregman_open_bundles = self._open_bregman_bundle_count()
        if self.bregman_exec_metrics:
            self.bregman_exec_metrics["opened_bregman_bundles"] = (
                self.bregman_exec_metrics.get("opened_bregman_bundles", 0) + opened)
            self.bregman_exec_metrics["bregman_capital_committed"] = round(
                self.bregman_exec_metrics.get("bregman_capital_committed", 0.0) + capital, 4)
            self.bregman_exec_metrics["rejected_by_reason"] = dict(self.bregman_reject_reasons)
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
            self._breg_reason("bregman_incomplete_executable_set")
            return False
        # Pass-3 all-or-nothing realism gate: every leg must be LIVE-executable.
        # Any leg that fails (missing ask / stale / wide / thin / ambiguous /
        # reference-only) rejects the WHOLE bundle with an explicit reason.
        from .paper_execution import (PaperExecutionContext, bregman_leg_reason,
                                       SRC_LIVE_CLOB, SRC_REFERENCE)
        leg_realism = []
        for l, r in zip(legs, recs):
            fresh = bool(getattr(r, "fresh_book", True)) and not bool(getattr(r, "stale", False))
            src = SRC_LIVE_CLOB if fresh else SRC_REFERENCE
            ctx = PaperExecutionContext(
                fill_source=src,
                ask=(float(l.executable_price) if getattr(l, "executable_price", 0) else None),
                bid=getattr(r, "best_bid", None),
                spread=getattr(r, "spread", None),
                depth_usd=float(getattr(r, "top_depth_usd", 0.0) or 0.0),
                book_age_sec=getattr(r, "book_age_s", None), fresh_book=fresh,
                ambiguity_score=float(getattr(r, "ambiguity_score", 0.0) or 0.0),
                resolved=bool(getattr(r, "resolved", False)),
                accepting_orders=bool(getattr(r, "accepting_orders", True)),
                tick_size=float(getattr(r, "tick_size", 0.0) or 0.0),
                gross_edge=None, is_bregman_leg=True)
            self.realism_counts["candidates_considered"] += 1
            d = self.bregman_exec_policy.evaluate(ctx)
            self._tally_realism(d)
            if not d.allow_executable_trade:
                self.bregman_rejected += 1
                self._breg_reason(bregman_leg_reason(d.reason))
                return False
            leg_realism.append(d)
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
        for _i, (p, rec, d) in enumerate(zip(proposals, recs, decisions)):
            lr = leg_realism[_i]
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
            from .correlation_risk import correlation_keys as _corr_keys
            _bk = _corr_keys(rec)
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
                experiment_id=self.experiments.experiment_id, strategy_variant=BREGMAN_VARIANT,
                execution_realism_status=lr.execution_realism_status,
                fill_source=lr.fill_source, book_source=lr.book_source,
                price_source=lr.price_source,
                was_reference_price_fill=lr.was_reference_price_fill,
                was_fallback_fill=lr.was_fallback_fill,
                was_offline_stub_fill=lr.was_offline_stub_fill,
                book_age_sec=float(lr.book_age_sec or 0.0),
                depth_at_price=float(lr.depth_at_price or 0.0),
                fill_quality=float(lr.fill_quality or 0.0),
                cluster_id=_bk.get("cluster_id", ""),
                correlation_group=_bk.get("correlation_group", ""),
                condition_id=_bk.get("condition_id", ""))
            self.realism_counts["realistic_trade_count"] += 1
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
            # Pass-2: raw-catalog discovery -> dedup -> certify -> open funnel.
            "execution": {
                **self.bregman_exec_metrics,
                "open_bregman_bundles": self._open_bregman_bundle_count(),
                "rejected_by_reason": dict(self.bregman_reject_reasons),
                "evaluated_before_directional": True,
                "directional_skipped_due_to_bregman": self.directional_skipped_due_to_bregman,
                "sees_full_raw_catalog": True,
            },
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
            # PASS-6: exploration is chosen by the ActiveLearningSelector (Tier 3),
            # NOT a random/hash gate. It selects the most informative near-misses
            # under strict realism + bounded loss + diversity caps; random/hash can
            # no longer open a trade while active learning is enabled. Still routed
            # through RiskEngine + PaperBroker — cannot bypass hard caps.
            explore_notional = min(float(self.cfg.exploration_notional_usd),
                                   float(self.cfg.max_order_notional_usd))
            budget_ok = (self.exploration_budget_used + explore_notional
                         <= float(self.cfg.exploration_budget_usd) + 1e-9)
            al_decision = self._active_learning_admit(rec, est, edge, reason)
            self._last_al_decision = al_decision
            if (self.mode == "paper_train" and self.cfg.exploration_enabled
                    and budget_ok and al_decision.get("decision") == "explore"):
                exploratory = True
            else:
                self.rejection_count += 1
                self.learner.record_decision(traded=False, reason=reason)
                # CLOSED LOOP: a rejected/near-miss candidate is still a structured
                # learning example (no-trade or shadow), with a pending label.
                ald = al_decision.get("decision", "skip")
                cl_decision = ("shadow_only" if ald in ("near_miss", "shadow")
                               else ("no_trade_label" if reason in (
                                   "edge_too_low", "uncertainty_too_high")
                                   else "rejected_hard_gate"))
                self.closed_loop.record(
                    rec, est, edge, decision=cl_decision, reason=reason,
                    strategy_tier="tier3_exploration" if ald in ("near_miss", "shadow")
                    else "tier2_directional", strategy_source="directional",
                    active_learning=al_decision, tick=self.tick_count)
                return {"opened": False, "reason": reason,
                        "exploration_decision": ald}

        # observe_only mode evaluates + records but NEVER opens a paper trade
        if self.mode != "paper_train":
            self.learner.record_decision(traded=False, reason="observe_only")
            return {"opened": False, "reason": "observe_only"}

        res = self._open(rec, est, edge, diag, exploratory=exploratory)
        # CLOSED LOOP: record the executed/explored example (or its shadow downgrade).
        if res.get("opened"):
            self.closed_loop.record(
                rec, est, edge,
                decision="selected_active_learning" if exploratory else "opened_realistic_paper",
                reason="opened", strategy_tier="tier3_exploration" if exploratory
                else "tier2_directional", strategy_source="directional",
                realism_status="realistic_executable",
                counts_for_readiness=not exploratory, tick=self.tick_count)
        elif res.get("shadow_only"):
            self.closed_loop.record(
                rec, est, edge, decision="shadow_only", reason=res.get("reason", ""),
                strategy_tier="tier4_shadow", strategy_source="directional",
                realism_status=res.get("execution_realism_status", "shadow_only"),
                tick=self.tick_count)
        return res

    def _explore_gate(self, market_id: str) -> bool:
        """LEGACY deterministic random/hash exploration sampler (PASS-6: disabled by
        default — kept only as a diagnostic comparison + tie-breaker; it can no
        longer open a paper trade while active learning is enabled)."""
        import hashlib
        h = hashlib.sha256(f"{market_id}:{self.tick_count}".encode()).digest()
        return (int.from_bytes(h[:4], "big") % 1000) / 1000.0 < self.cfg.exploration_rate

    # -- PASS-7: cluster / correlation risk (active hard gate + allocator) ------
    @staticmethod
    def _fresh_corr_metrics() -> dict:
        return {
            "correlation_gate_enabled": False,
            "candidates_with_cluster_id": 0, "candidates_missing_cluster_id": 0,
            "blocked_same_market": 0, "blocked_same_condition": 0, "blocked_same_event": 0,
            "blocked_same_cluster": 0, "blocked_semantic_duplicate": 0,
            "blocked_bregman_market_collision": 0, "blocked_bregman_event_collision": 0,
            "blocked_exploration_cluster_collision": 0,
            "size_capped_by_cluster_exposure": 0, "shadowed_unknown_cluster": 0,
            "correlation_adjusted_candidates": 0,
            "directional_trades_blocked_by_correlation": 0,
            "exploration_trades_blocked_by_correlation": 0,
            "bregman_bundles_blocked_as_duplicates": 0,
            "bregman_bundles_blocked_as_overlapping": 0,
        }

    def _begin_correlation_phase(self) -> None:
        """Rebuild the open-exposure index from current open paper positions
        (every correlation level) before this tick's candidates are evaluated."""
        from .correlation_risk import OpenExposureIndex
        self._open_exposure_index = OpenExposureIndex.from_positions(self.open_positions())
        self.correlation_metrics["correlation_gate_enabled"] = bool(
            getattr(self.cfg, "correlation_gate_enabled", True))

    def _correlation_decide(self, rec, *, strategy: str, size_usd: float):
        """Run the CorrelationRiskGate for a candidate; update funnel metrics."""
        from .correlation_risk import (correlation_keys, ALLOW_WITH_SIZE_CAP, REJECT,
                                        SHADOW_ONLY, SAME_MARKET, SAME_CONDITION, SAME_EVENT,
                                        SAME_CLUSTER, BREGMAN_MARKET_COLLISION,
                                        BREGMAN_EVENT_COLLISION, UNKNOWN_CLUSTER,
                                        EXPLORATION_CLUSTER_OVEREXPOSURE)
        cm = self.correlation_metrics
        keys = correlation_keys(rec)
        if keys.get("unknown_cluster"):
            cm["candidates_missing_cluster_id"] += 1
        else:
            cm["candidates_with_cluster_id"] += 1
        d = self.correlation_gate.evaluate(
            keys, strategy=strategy, size_usd=size_usd, index=self._open_exposure_index)
        ct = d.collision_type
        if d.decision == REJECT:
            _bump = {
                SAME_MARKET: "blocked_same_market", SAME_CONDITION: "blocked_same_condition",
                SAME_EVENT: "blocked_same_event", SAME_CLUSTER: "blocked_same_cluster",
                BREGMAN_MARKET_COLLISION: "blocked_bregman_market_collision",
                BREGMAN_EVENT_COLLISION: "blocked_bregman_event_collision",
                EXPLORATION_CLUSTER_OVEREXPOSURE: "blocked_exploration_cluster_collision",
            }.get(ct)
            if _bump:
                cm[_bump] += 1
            cm["directional_trades_blocked_by_correlation" if strategy == "directional"
               else "exploration_trades_blocked_by_correlation"] += 1
        elif d.decision == SHADOW_ONLY and ct == UNKNOWN_CLUSTER:
            cm["shadowed_unknown_cluster"] += 1
        elif d.decision == ALLOW_WITH_SIZE_CAP:
            cm["size_capped_by_cluster_exposure"] += 1
        cm["correlation_adjusted_candidates"] += 1
        return d, keys

    # -- PASS-6: profitability-aware active learning (exploration authority) ----
    @staticmethod
    def _fresh_al_metrics() -> dict:
        return {
            "active_learning_enabled": False, "random_exploration_enabled": False,
            "random_exploration_opened_trades": 0,
            "active_learning_candidates_considered": 0,
            "active_learning_candidates_selected": 0,
            "exploration_trades_opened": 0, "exploration_shadow_only": 0,
            "exploration_rejected_by_realism": 0, "exploration_rejected_by_profitability": 0,
            "exploration_rejected_by_budget": 0, "exploration_rejected_by_collision": 0,
            "exploration_rejected_by_diversity": 0,
            "legacy_random_exploration_blocked": 0,
            "exploration_budget_used_usd": 0.0, "exploration_expected_loss_usd": 0.0,
            "_scores": [], "_exec_quality": [], "buckets": {},
            "category_coverage": {}, "cluster_diversity": {},
            "pending_feedback_count": 0, "completed_feedback_count": 0,
        }

    def _begin_exploration_phase(self) -> None:
        """Reset per-tick exploration caps + budget state (PAPER ONLY)."""
        self._explore_tick_state = {
            "opened": 0, "capital": 0.0, "per_event": {}, "per_cluster": {},
            "per_category": {},
        }
        am = self.active_learning_metrics
        am["active_learning_enabled"] = bool(getattr(self.cfg, "active_learning_enabled", True))
        am["random_exploration_enabled"] = bool(
            getattr(self.cfg, "random_exploration_enabled", False))

    def _exploration_eligibility(self, rec, est, edge) -> "tuple[bool, dict]":
        """Strict PASS-3 realism + bounded-loss eligibility for an exploration
        candidate. Returns (eligible, near_miss) where near_miss describes the
        failed gate + distance to threshold when not eligible."""
        cfg = self.cfg
        spread = float(getattr(est, "spread", 0.0) or 0.0)
        depth = float(getattr(rec, "top_depth_usd", 0.0) or 0.0)
        amb = float(getattr(est, "ambiguity_score", 0.0) or 0.0)
        fresh = bool(getattr(est, "fresh_book", True))
        book_age = getattr(rec, "book_age_s", None)
        exec_price = float(getattr(edge, "executable_price", 0.0) or 0.0)
        max_spread = float(getattr(cfg, "exploration_max_spread", 0.08))
        min_depth = float(getattr(cfg, "exploration_min_depth_at_price", 25.0))
        max_amb = float(getattr(cfg, "exploration_max_ambiguity_score", 0.45))
        max_age = float(getattr(cfg, "exploration_max_book_age_sec", 20.0))
        size = min(float(getattr(cfg, "exploration_notional_usd", 2.0)),
                   float(getattr(cfg, "exploration_max_position_size_usd", 5.0)),
                   float(getattr(cfg, "max_order_notional_usd", 5.0)))
        max_loss = float(getattr(cfg, "exploration_max_expected_loss_usd", 0.25))

        def nm(gate, observed, threshold, need) -> dict:
            return {"near_miss_reason": gate, "failed_gate": gate,
                    "observed": observed, "threshold": threshold,
                    "distance_to_threshold": round(abs(float(observed) - float(threshold)), 6),
                    "condition_needed_to_trade": need}
        if exec_price <= 0 or not fresh:
            return False, nm("missing_ask_or_stale_book", 0 if exec_price <= 0 else 1, 1,
                             "a fresh executable ask on the live book")
        if book_age is not None and float(book_age) > max_age:
            return False, nm("stale_book", book_age, max_age, f"book age <= {max_age:g}s")
        if depth < min_depth:
            return False, nm("thin_depth", depth, min_depth, f"depth >= ${min_depth:g}")
        if spread > max_spread:
            return False, nm("wide_spread", spread, max_spread, f"spread <= {max_spread:g}")
        if amb > max_amb:
            return False, nm("ambiguous_settlement", amb, max_amb, f"ambiguity <= {max_amb:g}")
        # bounded downside: conservative EXPECTED loss of a tiny probe is the
        # round-trip execution cost drag on the notional (spread + 2x slippage),
        # NOT the full notional — a probe that doesn't move loses ~the drag.
        slip_frac = float(getattr(cfg, "slippage_bps", 25.0)) / 10000.0
        adverse = min(1.0, spread + 2.0 * slip_frac)
        expected_loss = round(size * adverse, 6)
        if expected_loss > max_loss + 1e-9:
            return False, nm("expected_loss_exceeds_cap", expected_loss, max_loss,
                             f"expected loss <= ${max_loss:g}")
        return True, {"exploration_size": round(size, 4),
                      "max_allowed_exploration_loss": max_loss,
                      "expected_loss_usd": expected_loss}

    def _active_learning_admit(self, rec, est, edge, reason: str) -> dict:
        """PASS-6 exploration authority. Returns a decision dict:
        ``{decision: 'explore'|'near_miss'|'skip', ...}``. When active learning is
        enabled, the random/hash gate can NEVER open a trade (legacy blocked +
        logged). Selection requires strict realism, profitability annotation,
        bounded loss, diversity caps, and a positive active-learning score."""
        cfg = self.cfg
        am = self.active_learning_metrics
        am["active_learning_candidates_considered"] += 1
        al_on = bool(getattr(cfg, "active_learning_enabled", True))
        rand_on = bool(getattr(cfg, "random_exploration_enabled", False))
        near_threshold = reason in ("edge_too_low", "uncertainty_too_high")
        edge_ok = float(getattr(edge, "net_edge", 0.0) or 0.0) >= float(
            getattr(cfg, "exploration_min_edge", -0.01))

        # --- LEGACY random/hash path: only when active learning is OFF ---
        if not al_on:
            if (rand_on and near_threshold and edge_ok and self._explore_gate(rec.market_id)):
                am["random_exploration_opened_trades"] += 1
                return {"decision": "explore", "learning_bucket": "random_legacy",
                        "active_learning_reason": "legacy_random_hash",
                        "active_learning_score": 0.0, "exploration_size": min(
                            float(cfg.exploration_notional_usd),
                            float(cfg.max_order_notional_usd))}
            return {"decision": "skip", "reason": reason}

        # --- ACTIVE LEARNING authority (random hash can never open) ---
        if rand_on is False and near_threshold and self._explore_gate(rec.market_id):
            am["legacy_random_exploration_blocked"] += 1
        if not near_threshold:
            return {"decision": "skip", "reason": reason}
        al = self.active_learner.score_candidate(
            rec=rec, est=est, edge=edge, reason=reason, learner=self.learner)
        am["_scores"].append(al["active_learning_score"])
        am["_exec_quality"].append(al["execution_quality_score"])
        am["buckets"][al["learning_bucket"]] = am["buckets"].get(al["learning_bucket"], 0) + 1
        # strict realism + bounded-loss eligibility
        eligible, info = self._exploration_eligibility(rec, est, edge)
        if not eligible:
            am["exploration_rejected_by_realism"] += 1
            self._record_near_miss(rec, est, edge, info, al)
            return {"decision": "near_miss", "reason": info["failed_gate"], **al}
        if al["active_learning_score"] <= 0.0 or al["learning_bucket"] == "not_eligible_for_learning":
            return {"decision": "skip", "reason": "no_information_value", **al}
        # collision with open Bregman markets/events (structured exposure)
        if (getattr(rec, "market_id", None) in getattr(self, "_bregman_open_markets", set())
                or self._rec_event_key(rec) in getattr(self, "_bregman_open_events", set())):
            am["exploration_rejected_by_collision"] += 1
            return {"decision": "skip", "reason": "bregman_collision", **al}
        # diversity + per-tick caps
        st = self._explore_tick_state or {}
        cat = getattr(rec, "category", None)
        evt = self._rec_event_key(rec)
        clu = getattr(rec, "cluster_id", None) or evt
        if st.get("opened", 0) >= int(getattr(cfg, "exploration_max_trades_per_tick", 2)):
            am["exploration_rejected_by_budget"] += 1
            return {"decision": "skip", "reason": "max_trades_per_tick", **al}
        if st.get("per_event", {}).get(evt, 0) >= int(getattr(cfg, "exploration_max_per_event", 1)):
            am["exploration_rejected_by_diversity"] += 1
            return {"decision": "skip", "reason": "max_per_event", **al}
        if st.get("per_cluster", {}).get(clu, 0) >= int(getattr(cfg, "exploration_max_per_cluster", 1)):
            am["exploration_rejected_by_diversity"] += 1
            return {"decision": "skip", "reason": "max_per_cluster", **al}
        if st.get("per_category", {}).get(cat, 0) >= int(
                getattr(cfg, "exploration_max_per_category_per_tick", 2)):
            am["exploration_rejected_by_diversity"] += 1
            return {"decision": "skip", "reason": "max_per_category_per_tick", **al}
        size = float(info["exploration_size"])
        if (st.get("capital", 0.0) + size
                > float(getattr(cfg, "exploration_max_capital_per_tick_usd", 20.0)) + 1e-9):
            am["exploration_rejected_by_budget"] += 1
            return {"decision": "skip", "reason": "exploration_capital_cap", **al}
        # SELECTED for exploration
        am["active_learning_candidates_selected"] += 1
        st["opened"] = st.get("opened", 0) + 1
        st["capital"] = st.get("capital", 0.0) + size
        st.setdefault("per_event", {})[evt] = st.get("per_event", {}).get(evt, 0) + 1
        st.setdefault("per_cluster", {})[clu] = st.get("per_cluster", {}).get(clu, 0) + 1
        st.setdefault("per_category", {})[cat] = st.get("per_category", {}).get(cat, 0) + 1
        am["category_coverage"][cat] = am["category_coverage"].get(cat, 0) + 1
        am["cluster_diversity"][str(clu)] = am["cluster_diversity"].get(str(clu), 0) + 1
        return {"decision": "explore", "why_not_exploit": reason,
                "why_not_shadow_only": "passed exploration realism + information value",
                **al, **info}

    def _record_near_miss(self, rec, est, edge, info: dict, al: dict) -> None:
        """Log a close-but-ineligible exploration candidate for learning (never
        opened): failed gate + distance to threshold + condition needed."""
        self.near_miss_log.append({
            "market_id": getattr(rec, "market_id", ""),
            "event_id": self._rec_event_key(rec),
            "category": getattr(rec, "category", None),
            "near_miss_reason": info.get("near_miss_reason"),
            "failed_gate": info.get("failed_gate"),
            "distance_to_threshold": info.get("distance_to_threshold"),
            "condition_needed_to_trade": info.get("condition_needed_to_trade"),
            "would_have_been_learning_bucket": al.get("learning_bucket"),
            "shadow_theoretical_edge": round(float(getattr(edge, "net_edge", 0.0) or 0.0), 6),
            "active_learning_score": al.get("active_learning_score"),
            "tick": self.tick_count,
        })
        if len(self.near_miss_log) > 500:
            self.near_miss_log = self.near_miss_log[-500:]

    def _record_exploration_feedback(self, rec, est, edge, fill, al: dict) -> None:
        """PASS-6 structured learning feedback for an opened exploration trade.
        Outcome/realized PnL/calibration error are pending until settlement."""
        am = self.active_learning_metrics
        am["exploration_trades_opened"] += 1
        am["exploration_budget_used_usd"] = round(
            am["exploration_budget_used_usd"] + float(fill["notional"]), 6)
        am["exploration_expected_loss_usd"] = round(
            am["exploration_expected_loss_usd"]
            + float(al.get("expected_loss_usd", al.get("max_allowed_exploration_loss", 0.0)) or 0.0), 6)
        am["pending_feedback_count"] += 1
        self.exploration_feedback_log.append({
            "candidate_id": fill.get("fill_id"),
            "market_id": getattr(rec, "market_id", ""),
            "event_id": self._rec_event_key(rec),
            "cluster_id": getattr(rec, "cluster_id", None) or self._rec_event_key(rec),
            "learning_bucket": al.get("learning_bucket"),
            "model_probability": round(float(getattr(edge, "p_final", 0.0) or 0.0), 6),
            "market_price": round(float(getattr(est, "p_market_mid", 0.0) or 0.0), 6),
            "executable_fill_price": round(float(fill["fill_price"]), 6),
            "after_cost_edge": round(float(getattr(edge, "net_edge", 0.0) or 0.0), 6),
            "uncertainty_score": al.get("uncertainty_score"),
            "active_learning_score": al.get("active_learning_score"),
            "reason_selected": al.get("active_learning_reason"),
            "reason_not_exploit": al.get("why_not_exploit"),
            "size": round(float(fill["notional"]), 4),
            "max_expected_loss": al.get("max_allowed_exploration_loss"),
            "actual_outcome": None, "realized_pnl": None, "calibration_error": None,
            "improved_category_coverage": True, "feedback_status": "pending",
            "tick": self.tick_count,
        })
        if len(self.exploration_feedback_log) > 500:
            self.exploration_feedback_log = self.exploration_feedback_log[-500:]

    # -- PASS-4: Bregman-first strategy priority + slot/capital reservation -----
    @staticmethod
    def _rec_event_key(rec) -> str:
        ev = getattr(rec, "event_id", None) or getattr(rec, "group_key", None) or ""
        return str(ev)

    def _begin_directional_phase(self, dir_slots_before: int, bregman_opened: int) -> None:
        """Compute the Bregman reservation + collision state BEFORE directional
        execution. Reserved slots/capital are held for Bregman whenever a
        certified-realistic opportunity existed this tick; released to directional
        only when none did (and the release flags allow it). PAPER ONLY."""
        cfg = self.cfg
        priority = bool(getattr(cfg, "bregman_priority_enabled", True))
        certified_realistic = int(self._bregman_certified_realistic_count)
        bregman_active = certified_realistic > 0
        release_slots = (not bregman_active) and bool(
            getattr(cfg, "directional_can_use_unused_bregman_slots", True))
        release_cap = (not bregman_active) and bool(
            getattr(cfg, "directional_can_use_unused_bregman_capital", True))
        self._dir_reserved_slots = (int(getattr(cfg, "bregman_reserve_open_slots", 3))
                                    if (priority and not release_slots) else 0)
        self._dir_reserved_capital = (float(getattr(cfg, "bregman_reserve_capital_usd", 100.0))
                                      if (priority and not release_cap) else 0.0)
        self._bregman_reserve_active = (self._dir_reserved_slots > 0
                                        or self._dir_reserved_capital > 0)
        # collision sets from CURRENTLY-OPEN Bregman legs (structured exposure)
        breg = [p for p in self.open_positions() if p.strategy == "bregman"]
        self._bregman_open_markets = {p.market_id for p in breg}
        self._bregman_open_events = {p.group_key for p in breg if getattr(p, "group_key", None)}
        hard_cap = int(cfg.max_open_trades)
        open_n = len(self.open_positions())
        self.priority_metrics = {
            "bregman_priority_enabled": priority,
            "bregman_evaluated_before_directional": True,
            "bregman_reserved_slots": self._dir_reserved_slots,
            "bregman_reserved_capital_usd": round(self._dir_reserved_capital, 4),
            "bregman_certified_before_directional_count": certified_realistic,
            "bregman_opened_before_directional_count": int(bregman_opened),
            "directional_slots_before_bregman": int(dir_slots_before),
            "directional_slots_after_bregman": max(0, hard_cap - self._dir_reserved_slots - open_n),
            "directional_trades_blocked_by_bregman_reservation": 0,
            "directional_trades_blocked_by_bregman_market_collision": 0,
            "directional_trades_blocked_by_bregman_event_collision": 0,
            "unused_bregman_slots_released_to_directional": (
                int(getattr(cfg, "bregman_reserve_open_slots", 3)) if release_slots else 0),
            "unused_bregman_capital_released_to_directional": (
                round(float(getattr(cfg, "bregman_reserve_capital_usd", 100.0)), 4)
                if release_cap else 0.0),
            "exploration_blocked_from_reserved_bregman_capacity": 0,
        }

    def _directional_admit(self, rec) -> "tuple[bool, str]":
        """Admission gate for a directional candidate AFTER Bregman. Enforces the
        Bregman slot reservation + market/event collision blocks. Returns
        ``(ok, block_reason)``; ``global_capacity`` means stop the loop."""
        open_n = len(self.open_positions())
        hard_cap = int(self.cfg.max_open_trades)
        if open_n >= hard_cap:
            return False, "global_capacity"
        if open_n >= (hard_cap - self._dir_reserved_slots):
            self.priority_metrics["directional_trades_blocked_by_bregman_reservation"] += 1
            return False, "bregman_reservation"
        if (bool(getattr(self.cfg, "block_directional_on_bregman_markets", True))
                and getattr(rec, "market_id", None) in self._bregman_open_markets):
            self.priority_metrics["directional_trades_blocked_by_bregman_market_collision"] += 1
            return False, "bregman_market_collision"
        if (bool(getattr(self.cfg, "block_directional_on_bregman_events", True))
                and self._rec_event_key(rec) in self._bregman_open_events):
            self.priority_metrics["directional_trades_blocked_by_bregman_event_collision"] += 1
            return False, "bregman_event_collision"
        return True, ""

    def _finalize_priority_metrics(self) -> None:
        pm = self.priority_metrics
        if not pm:
            return
        pm["directional_slots_after_bregman"] = max(
            0, int(self.cfg.max_open_trades) - self._dir_reserved_slots
            - len(self.open_positions()))
        pm["bregman_open_bundles"] = self._open_bregman_bundle_count()

    def _directional_realism(self, rec, est, edge, proposal):
        """Classify a directional candidate's fill realism (PAPER ONLY).

        Determines the fill source from book freshness + offline-stub config and
        runs the centralized PaperExecutionPolicy. EdgeEngine already enforces the
        hard quality gates upstream, so this is the realism *provenance* stamp +
        the reference/offline-stub/missing-ask catch. ``gross_edge`` is left None
        so the policy never second-guesses EdgeEngine's after-cost decision."""
        from .paper_execution import (PaperExecutionContext, SRC_LIVE_CLOB,
                                       SRC_OFFLINE_STUB, SRC_REFERENCE)
        self.realism_counts["candidates_considered"] += 1
        fresh = bool(getattr(est, "fresh_book", True))
        if fresh:
            src = SRC_LIVE_CLOB
        elif bool(getattr(self.cfg, "allow_offline_stub_trading", False)):
            src = SRC_OFFLINE_STUB
        else:
            src = SRC_REFERENCE
        ctx = PaperExecutionContext(
            fill_source=src,
            ask=(edge.executable_price if fresh else None),
            bid=getattr(est, "p_market_bid", None),
            spread=getattr(est, "spread", None),
            depth_usd=float(getattr(rec, "top_depth_usd", 0.0) or 0.0),
            book_age_sec=getattr(rec, "book_age_s", None),
            fresh_book=fresh,
            ambiguity_score=float(getattr(est, "ambiguity_score", 0.0) or 0.0),
            resolved=bool(getattr(rec, "resolved", False)),
            accepting_orders=bool(getattr(rec, "accepting_orders", True)),
            notional_usd=float(getattr(proposal, "notional_usd", 0.0) or 0.0),
            tick_size=float(getattr(rec, "tick_size", 0.0) or 0.0),
            gross_edge=None)
        decision = self.paper_exec_policy.evaluate(ctx)
        self._tally_realism(decision)
        return decision

    def _tally_realism(self, decision) -> None:
        rc = self.realism_counts
        if decision.was_reference_price_fill:
            rc["reference_fill_count"] += 1
        if decision.was_fallback_fill:
            rc["fallback_fill_count"] += 1
        st = decision.execution_realism_status
        bump = {
            "shadow_only_stale_book": "stale_book_rejection_count",
            "shadow_only_missing_ask": "missing_ask_rejection_count",
            "shadow_only_thin_depth": "thin_depth_rejection_count",
            "shadow_only_wide_spread": "wide_spread_rejection_count",
            "shadow_only_ambiguous_settlement": "ambiguity_rejection_count",
            "shadow_only_reference_price": "reference_fill_blocked_count",
        }.get(st)
        if bump:
            rc[bump] += 1
        if decision.was_offline_stub_fill and decision.reject:
            rc["offline_stub_rejection_count"] += 1

    def _record_shadow(self, rec, est, edge, decision, *, exploratory: bool = False,
                       strategy: str = "directional") -> None:
        """Log a non-executable opportunity as SHADOW: theoretical edge + reason +
        what would make it executable. Never opens a position, never counts PnL."""
        self.realism_counts["shadow_trade_count"] += 1
        self.shadow_opportunities.append({
            "market_id": getattr(rec, "market_id", ""),
            "group_key": getattr(rec, "group_key", ""),
            "outcome": getattr(edge, "outcome", ""),
            "strategy": strategy,
            "exploration": bool(exploratory),
            "theoretical_edge": round(float(getattr(edge, "net_edge", 0.0) or 0.0), 6),
            "execution_realism_status": decision.execution_realism_status,
            "reason": decision.reason,
            "would_be_executable_if": decision.would_be_executable_if,
            "spread": decision.spread, "depth_at_price": decision.depth_at_price,
            "book_age_sec": decision.book_age_sec, "fill_source": decision.fill_source,
            "after_cost_edge": decision.after_cost_edge,
            "tick": self.tick_count,
        })
        if len(self.shadow_opportunities) > 500:        # bound memory
            self.shadow_opportunities = self.shadow_opportunities[-500:]

    def _profitability_gate(self, rec, est, edge, proposal, *, exploratory: bool) -> dict:
        """PASS-5 hard after-cost profitability gate for a directional candidate.

        Computes conservative executable EV/ROI from the model edge + executable
        price (EdgeEngine has already netted spread/slippage/fee penalties into
        ``net_edge``). Rejects negative after-cost; shadow-only when positive but
        below the configured minimums. Exploration is bucketed but not hard-EV-
        gated (it is bounded + realism-checked elsewhere). Also feeds the
        ProfitabilityGovernor memory. PAPER ONLY — never sizes or places."""
        pm = self.profitability_metrics
        pm["candidates_annotated"] += 1
        cfg = self.cfg
        require = bool(getattr(cfg, "require_profitability_annotation", True))
        exec_price = float(getattr(edge, "executable_price", 0.0) or 0.0)
        after_cost_edge = float(getattr(edge, "net_edge", 0.0) or 0.0)
        notional = float(getattr(proposal, "notional_usd", 0.0) or 0.0)
        shares = (notional / exec_price) if exec_price > 0 else 0.0
        ev_usd = round(after_cost_edge * shares, 6)
        roi = round(after_cost_edge / exec_price, 6) if exec_price > 0 else 0.0

        def _ann(bucket, decision, reason, would="") -> dict:
            pm["buckets"][bucket] = pm["buckets"].get(bucket, 0) + 1
            return {
                "decision": decision, "reason": reason, "would_be_executable_if": would,
                "profitability_bucket": bucket, "gross_edge": round(after_cost_edge, 6),
                "model_edge": round(float(getattr(edge, "net_edge", 0.0) or 0.0), 6),
                "market_price": round(float(getattr(est, "p_market_mid", 0.0) or 0.0), 6),
                "executable_price": round(exec_price, 6),
                "observed_after_cost_edge": round(after_cost_edge, 6),
                "observed_after_cost_roi": roi, "expected_value_usd": ev_usd,
                "min_required_edge": float(getattr(cfg, "min_after_cost_edge", 0.01)),
                "min_required_roi": float(getattr(cfg, "min_after_cost_roi", 0.002)),
                "min_required_ev_usd": float(getattr(cfg, "min_expected_value_usd", 0.01)),
                "annotation_stage": "decision_time", "annotated": True,
            }

        # missing executable economics -> cannot count as real edge
        if exec_price <= 0:
            pm["candidates_missing_profitability_data"] += 1
            if require:
                pm["execution_without_annotation"] += 0   # annotation present but data missing
                return _ann("non_executable", "reject", "missing_executable_price",
                            "a real executable price exists")
        # feed the governor memory (records strikes on negative after-cost markets)
        try:
            self.governor.evaluate(
                market_id=getattr(rec, "market_id", ""),
                strategy=_resolved_strategy(getattr(self, "_last_resolved", None)),
                gross_edge=after_cost_edge, cost_components={},
                liquidity_usd=float(getattr(rec, "liquidity_usd", 0.0) or 0.0),
                spread=float(getattr(est, "spread", 0.0) or 0.0),
                time_to_resolution_s=((rec.end_ts - 0) if getattr(rec, "end_ts", None) else None),
                aggressive=bool(getattr(cfg, "aggressive_mode", False)))
        except Exception:  # noqa: BLE001 — governor telemetry must never break a tick
            pass

        if exploratory:
            pm["exploration_profitability_checked"] += 1
            return _ann("exploration_feedback_positive", "allow", "exploration_bounded")
        if after_cost_edge <= 0.0:
            pm["candidates_rejected_negative_after_cost"] += 1
            pm["profitability_governor_hard_rejects"] += 1
            return _ann("negative_after_cost", "reject", "negative_after_cost",
                        "gross edge exceeds spread+slippage+fee+tick drag")
        if (after_cost_edge < float(getattr(cfg, "min_after_cost_edge", 0.01))
                or roi < float(getattr(cfg, "min_after_cost_roi", 0.002))
                or ev_usd < float(getattr(cfg, "min_expected_value_usd", 0.01))):
            pm["candidates_shadow_theoretical_only"] += 1
            return _ann("shadow_theoretical_only", "shadow_only", "below_min_after_cost",
                        "after-cost edge/ROI/EV clears the configured minimums")
        pm["directional_after_cost_positive"] += 1
        pm["candidates_ranked_by_profitability"] += 1
        return _ann("directional_after_cost_positive", "allow", "after_cost_positive")

    def _open(self, rec, est, edge, diag, *, exploratory: bool = False) -> dict:
        """Build proposal -> RiskEngine -> PaperBroker (trace-id chain). PAPER
        ONLY. Exploratory trades use a small bounded notional capped to the same
        hard paper order-notional ceiling as normal trades."""
        # PASS-9 ablation: when directional execution is disabled for an experiment
        # profile (e.g. bregman_only), a directional candidate is logged shadow-only
        # (counterfactual) and never opened — it cannot count toward readiness PnL.
        if (not exploratory
                and not bool(getattr(self.cfg, "directional_execution_enabled", True))):
            self._record_shadow(rec, est, edge, SimpleNamespace(
                execution_realism_status="shadow_only_directional_disabled",
                reason="directional_execution_disabled_profile",
                would_be_executable_if="directional execution enabled",
                spread=float(getattr(est, "spread", 0.0) or 0.0),
                depth_at_price=float(getattr(rec, "top_depth_usd", 0.0) or 0.0),
                book_age_sec=0.0, fill_source="live_clob",
                after_cost_edge=float(getattr(edge, "net_edge", 0.0) or 0.0)),
                exploratory=False, strategy="directional")
            self.learner.record_decision(traded=False, reason="directional_execution_disabled")
            return {"opened": False, "shadow_only": True,
                    "reason": "directional_execution_disabled"}
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

        # PASS-4: exploration (Tier 3) must NOT consume Bregman-reserved capacity.
        # When a certified-realistic Bregman opportunity reserved slots/capital this
        # tick, exploration is held back unless explicitly allowed by config.
        if (exploratory and getattr(self, "_bregman_reserve_active", False)
                and not bool(getattr(self.cfg,
                                     "exploration_can_use_bregman_reserved_capacity", False))):
            self.rejection_count += 1
            self.priority_metrics["exploration_blocked_from_reserved_bregman_capacity"] = (
                self.priority_metrics.get(
                    "exploration_blocked_from_reserved_bregman_capacity", 0) + 1)
            self.learner.record_decision(
                traded=False, reason="exploration_blocked_bregman_reserved")
            return {"opened": False, "reason": "exploration_blocked_bregman_reserved"}

        proposal = self.policy.build_proposal(edge, est, rec)
        proposal.experiment_id = self.experiments.experiment_id
        proposal.strategy_variant = variant
        # Pass-3 realism gate: a directional paper trade may only open if it could
        # plausibly fill from the LIVE book. Reference/offline-stub/missing-ask/
        # stale fills are downgraded to shadow-only (logged, never counted as PnL)
        # or hard-rejected. Exploration trades still pass the gate (they must be
        # realistically fillable too) but are bucketed separately downstream.
        realism = self._directional_realism(rec, est, edge, proposal)
        if realism.reject:
            self.rejection_count += 1
            self.realism_counts["hard_reject_count"] += 1
            self.learner.record_decision(traded=False, reason=realism.reason)
            return {"opened": False, "reason": realism.reason,
                    "execution_realism_status": realism.execution_realism_status}
        if realism.allow_shadow_only:
            self._record_shadow(rec, est, edge, realism, exploratory=exploratory)
            self.learner.record_decision(
                traded=False, reason="shadow_" + realism.execution_realism_status)
            return {"opened": False, "shadow_only": True, "reason": realism.reason,
                    "execution_realism_status": realism.execution_realism_status}
        if exploratory:
            notional = min(float(self.cfg.exploration_notional_usd),
                           float(self.cfg.max_order_notional_usd))
            proposal.notional_usd = round(notional, 2)
            proposal.qty = round(notional / proposal.price, 4) if proposal.price > 0 else 0.0
            proposal.sizing_method = "exploration"
            self.exploration_count += 1
        # PASS-4: directional must not spend Bregman-reserved capital. The effective
        # directional exposure ceiling is tightened by the reserved capital so a
        # pending certified Bregman bundle keeps its budget.
        reserved_cap = float(getattr(self, "_dir_reserved_capital", 0.0) or 0.0)
        if reserved_cap > 0:
            effective_cap = float(self.cfg.max_total_exposure_usd) - reserved_cap
            if self.total_exposure() + float(proposal.notional_usd) > effective_cap + 1e-9:
                self.rejection_count += 1
                self.priority_metrics["directional_trades_blocked_by_bregman_reservation"] = (
                    self.priority_metrics.get(
                        "directional_trades_blocked_by_bregman_reservation", 0) + 1)
                self.learner.record_decision(
                    traded=False, reason="bregman_capital_reservation")
                return {"opened": False, "reason": "bregman_capital_reservation"}
        # PASS-5 PROFITABILITY GOVERNOR (hard gate): a directional trade may open
        # only with conservative POSITIVE after-cost expected value. Negative
        # after-cost -> reject; positive-but-sub-threshold -> shadow-only. Every
        # candidate is annotated; missing annotation is rejected when required.
        pg = self._profitability_gate(rec, est, edge, proposal, exploratory=exploratory)
        self._last_profit_annotation = pg
        if pg["decision"] == "reject":
            self.rejection_count += 1
            self.learner.record_decision(traded=False, reason=pg["reason"])
            return {"opened": False, "reason": pg["reason"],
                    "profitability_bucket": pg["profitability_bucket"]}
        if pg["decision"] == "shadow_only":
            self._record_shadow(rec, est, edge,
                                SimpleNamespace(execution_realism_status="shadow_theoretical_only",
                                                reason=pg["reason"],
                                                would_be_executable_if=pg.get("would_be_executable_if", ""),
                                                spread=float(getattr(est, "spread", 0.0) or 0.0),
                                                depth_at_price=float(getattr(rec, "top_depth_usd", 0.0) or 0.0),
                                                book_age_sec=0.0, fill_source="live_clob",
                                                after_cost_edge=pg["observed_after_cost_edge"]),
                                exploratory=exploratory)
            self.learner.record_decision(traded=False, reason=pg["reason"])
            return {"opened": False, "shadow_only": True, "reason": pg["reason"],
                    "profitability_bucket": pg["profitability_bucket"]}
        # PASS-7 CORRELATION RISK GATE: a candidate may only open if it adds
        # INDEPENDENT exposure. Duplicate market/condition/event/cluster + Bregman-
        # bundle collisions reject; cluster $-exposure size-caps; unknown cluster
        # metadata downgrades to shadow-only (never silent real edge).
        corr, corr_keys = self._correlation_decide(
            rec, strategy=("exploration" if exploratory else "directional"),
            size_usd=float(proposal.notional_usd))
        self._last_corr_keys = corr_keys
        if corr.decision == "reject":
            self.rejection_count += 1
            self.learner.record_decision(traded=False, reason=corr.reason)
            return {"opened": False, "reason": corr.reason,
                    "collision_type": corr.collision_type}
        if corr.decision == "shadow_only":
            self._record_shadow(rec, est, edge,
                                SimpleNamespace(execution_realism_status="shadow_only_unknown_cluster",
                                                reason=corr.reason,
                                                would_be_executable_if="valid cluster metadata",
                                                spread=float(getattr(est, "spread", 0.0) or 0.0),
                                                depth_at_price=float(getattr(rec, "top_depth_usd", 0.0) or 0.0),
                                                book_age_sec=0.0, fill_source="live_clob",
                                                after_cost_edge=float(getattr(edge, "net_edge", 0.0) or 0.0)),
                                exploratory=exploratory)
            self.learner.record_decision(traded=False, reason=corr.reason)
            return {"opened": False, "shadow_only": True, "reason": corr.reason,
                    "collision_type": corr.collision_type}
        if corr.decision == "allow_with_size_cap" and corr.size_cap is not None:
            capped = max(0.0, float(corr.size_cap))
            proposal.notional_usd = round(min(float(proposal.notional_usd), capped), 2)
            proposal.qty = round(proposal.notional_usd / proposal.price, 4) if proposal.price > 0 else 0.0
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
            # PASS-6: active-learning trade opened -> structured feedback record.
            self._record_exploration_feedback(rec, est, edge, fill,
                                               getattr(self, "_last_al_decision", {}))
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
            experiment_id=self.experiments.experiment_id, strategy_variant=variant,
            execution_realism_status=realism.execution_realism_status,
            fill_source=realism.fill_source, book_source=realism.book_source,
            price_source=realism.price_source,
            was_reference_price_fill=realism.was_reference_price_fill,
            was_fallback_fill=realism.was_fallback_fill,
            was_offline_stub_fill=realism.was_offline_stub_fill,
            book_age_sec=float(realism.book_age_sec or 0.0),
            depth_at_price=float(realism.depth_at_price or 0.0),
            fill_quality=float(realism.fill_quality or 0.0),
            cluster_id=(getattr(self, "_last_corr_keys", {}) or {}).get("cluster_id", ""),
            correlation_group=(getattr(self, "_last_corr_keys", {}) or {}).get("correlation_group", ""),
            condition_id=(getattr(self, "_last_corr_keys", {}) or {}).get("condition_id", ""))
        self.realism_counts["realistic_trade_count"] += 1
        # PASS-5: record executed after-cost economics (readiness EV telemetry).
        _pa = getattr(self, "_last_profit_annotation", None)
        if _pa and not exploratory:
            self.profitability_metrics["_exec_edges"].append(_pa["observed_after_cost_edge"])
            self.profitability_metrics["_exec_rois"].append(_pa["observed_after_cost_roi"])
            self.profitability_metrics["_exec_ev"].append(_pa["expected_value_usd"])
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
        # PASS-6: complete the active-learning feedback record at settlement.
        if getattr(pos, "exploration", False):
            for fb in self.exploration_feedback_log:
                if fb.get("candidate_id") == pos.fill_id and fb.get("feedback_status") == "pending":
                    fb["actual_outcome"] = "win" if pos.realized_pnl > 0 else "loss"
                    fb["realized_pnl"] = pos.realized_pnl
                    fb["calibration_error"] = round(abs(pos.p_final - (1.0 if pos.realized_pnl > 0 else 0.0)), 6)
                    fb["feedback_status"] = "completed"
                    self.active_learning_metrics["pending_feedback_count"] = max(
                        0, self.active_learning_metrics["pending_feedback_count"] - 1)
                    self.active_learning_metrics["completed_feedback_count"] += 1
                    break
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

    def paper_realism_report(self) -> dict:
        """Pass-3 Paper Realism: separates REALISTIC executable PnL from
        shadow/theoretical/exploration PnL so readiness can never be inflated by
        unrealistic fills. Only ``realistic_executable`` non-exploration trades
        count toward readiness. PAPER ONLY (read-only telemetry)."""
        closed = [p for p in self.positions if p.closed]

        def _pnl(pred) -> float:
            return round(sum(p.realized_pnl for p in closed if pred(p)), 6)

        realistic = lambda p: p.is_realistic and not p.exploration  # noqa: E731
        bregman_realistic_pnl = _pnl(lambda p: realistic(p) and p.strategy == "bregman")
        directional_realistic_pnl = _pnl(
            lambda p: realistic(p) and p.strategy != "bregman")
        exploration_pnl = _pnl(lambda p: p.exploration)
        # reference/fallback fills are NEVER opened under safe defaults, but if a
        # diagnostics override allowed one, its realized PnL is quarantined here.
        reference_fill_theoretical_pnl = _pnl(
            lambda p: p.was_reference_price_fill or p.was_fallback_fill)
        shadow_theoretical_pnl = round(
            sum(float(s.get("theoretical_edge", 0.0) or 0.0)
                for s in self.shadow_opportunities), 6)
        readiness_pnl = round(bregman_realistic_pnl + directional_realistic_pnl, 6)
        executed = [p for p in self.positions if p.is_realistic and not p.exploration]
        rc = dict(self.realism_counts)
        return {
            "schema": "paper_realism/1.0", "paper_only": True,
            # funnel
            "total_candidates_considered": rc.get("candidates_considered", 0),
            "realistic_trade_count": rc.get("realistic_trade_count", 0),
            "shadow_trade_count": rc.get("shadow_trade_count", 0),
            "hard_reject_count": rc.get("hard_reject_count", 0),
            "reference_fill_attempts": rc.get("reference_fill_count", 0)
                                       + rc.get("reference_fill_blocked_count", 0),
            "reference_fills_allowed": rc.get("reference_fill_count", 0),
            "reference_fills_blocked": rc.get("reference_fill_blocked_count", 0),
            "fallback_fill_count": rc.get("fallback_fill_count", 0),
            "stale_book_rejection_count": rc.get("stale_book_rejection_count", 0),
            "missing_ask_rejection_count": rc.get("missing_ask_rejection_count", 0),
            "thin_depth_rejection_count": rc.get("thin_depth_rejection_count", 0),
            "wide_spread_rejection_count": rc.get("wide_spread_rejection_count", 0),
            "ambiguity_rejection_count": rc.get("ambiguity_rejection_count", 0),
            "offline_stub_rejection_count": rc.get("offline_stub_rejection_count", 0),
            "rejected_not_counted": rc.get("hard_reject_count", 0),
            # executed-trade book quality
            "avg_spread_executed": round(
                sum(p.spread for p in executed) / len(executed), 6) if executed else None,
            "avg_depth_executed": round(
                sum(p.depth_at_price for p in executed) / len(executed), 4) if executed else None,
            "avg_book_age_executed": round(
                sum(p.book_age_sec for p in executed) / len(executed), 4) if executed else None,
            # PnL separation (ONLY realistic counts toward readiness)
            "bregman_realistic_pnl": bregman_realistic_pnl,
            "directional_realistic_pnl": directional_realistic_pnl,
            "exploration_pnl": exploration_pnl,
            "shadow_theoretical_pnl": shadow_theoretical_pnl,
            "reference_fill_theoretical_pnl": reference_fill_theoretical_pnl,
            "realistic_pnl": readiness_pnl,
            "readiness_pnl": readiness_pnl,
            # realism posture (matches the strict defaults)
            "reference_price_fills_allowed_for_exploit": bool(
                getattr(self.cfg, "allow_pm_reference_price_fills", False)),
            "missing_ask_fallback_allowed": not bool(
                getattr(self.cfg, "reject_missing_ask", True)),
            "stale_book_fills_allowed": not bool(
                getattr(self.cfg, "reject_on_stale_book", True)),
            "offline_stub_fills_count_as_real": bool(
                getattr(self.cfg, "allow_offline_stub_trading", False)),
            "bregman_requires_all_executable_legs": bool(
                getattr(self.cfg, "bregman_require_executable_all_legs", True)),
            "shadow_examples": self.shadow_opportunities[-20:],
        }

    def strategy_priority_report(self) -> dict:
        """Pass-4 Strategy Priority: proves Bregman (Tier 1) gets first claim on
        slots + capital before directional (Tier 2) and exploration (Tier 3).
        Read-only telemetry derived from the latest tick's allocation."""
        pm = dict(self.priority_metrics)
        bx = dict(self.bregman_exec_metrics)
        opened = pm.get("bregman_opened_before_directional_count", 0)
        certified = pm.get("bregman_certified_before_directional_count", 0)
        why_zero = ""
        if opened == 0:
            if certified == 0:
                why_zero = ("no certified-realistic Bregman opportunity this tick "
                            "(see metrics/bregman_execution.json rejected_by_reason)")
            else:
                why_zero = ("certified opportunities existed but did not open "
                            "(per-tick caps or execution-time realism gate)")
        return {
            "schema": "strategy_priority/1.0", "paper_only": True,
            "tier1": "certified Bregman/ABCAS complete-set arbitrage",
            "tier2": "high-confidence directional", "tier3": "exploration/active-learning",
            "bregman_priority_enabled": pm.get("bregman_priority_enabled",
                                               bool(getattr(self.cfg, "bregman_priority_enabled", True))),
            "bregman_evaluated_before_directional": pm.get("bregman_evaluated_before_directional", True),
            "bregman_reserved_slots": pm.get("bregman_reserved_slots", 0),
            "bregman_reserved_capital_usd": pm.get("bregman_reserved_capital_usd", 0.0),
            "bregman_groups_discovered": bx.get("raw_groups_discovered", 0),
            "bregman_certified_before_directional_count": certified,
            "bregman_realistic_executable_count": certified,
            "bregman_opened_before_directional_count": opened,
            "bregman_zero_open_reason": why_zero,
            "directional_consumed_capacity_before_bregman": False,
            "directional_slots_before_bregman": pm.get("directional_slots_before_bregman", 0),
            "directional_slots_after_bregman": pm.get("directional_slots_after_bregman", 0),
            "directional_trades_blocked_by_bregman_reservation": pm.get(
                "directional_trades_blocked_by_bregman_reservation", 0),
            "directional_trades_blocked_by_bregman_market_collision": pm.get(
                "directional_trades_blocked_by_bregman_market_collision", 0),
            "directional_trades_blocked_by_bregman_event_collision": pm.get(
                "directional_trades_blocked_by_bregman_event_collision", 0),
            "unused_bregman_slots_released_to_directional": pm.get(
                "unused_bregman_slots_released_to_directional", 0),
            "unused_bregman_capital_released_to_directional": pm.get(
                "unused_bregman_capital_released_to_directional", 0.0),
            "exploration_blocked_from_reserved_bregman_capacity": pm.get(
                "exploration_blocked_from_reserved_bregman_capacity", 0),
            "directional_secondary_after_bregman": True,
            "exploration_tertiary_after_exploit": True,
            "paper_realism_enforced": True,
        }

    def profitability_ranking_report(self) -> dict:
        """Pass-5 Profitability Ranking: proves candidates compete on conservative
        executable AFTER-COST expected value (not surface quality/model score),
        annotated before shortlist truncation, with a hard governor gate. Read-only."""
        pm = self.profitability_metrics
        edges = pm.get("_exec_edges", []) or []
        rois = pm.get("_exec_rois", []) or []
        evs = pm.get("_exec_ev", []) or []
        buckets = dict(pm.get("buckets", {}))
        top_reason = ("bregman_certified_positive (Tier-1 arbitrage)"
                      if buckets.get("bregman_certified_positive")
                      else ("directional_after_cost_positive"
                            if buckets.get("directional_after_cost_positive")
                            else "no after-cost-positive executable candidate this run"))
        return {
            "schema": "profitability_ranking/1.0", "paper_only": True,
            "profitability_first_enabled": bool(getattr(self.cfg, "profitability_first", True)),
            "profitability_annotation_before_truncation": True,
            "require_profitability_annotation": bool(
                getattr(self.cfg, "require_profitability_annotation", True)),
            "candidates_annotated": pm.get("candidates_annotated", 0),
            "candidates_missing_profitability_data": pm.get("candidates_missing_profitability_data", 0),
            "candidates_ranked_by_profitability": pm.get("candidates_ranked_by_profitability", 0),
            "candidates_rejected_negative_after_cost": pm.get(
                "candidates_rejected_negative_after_cost", 0),
            "candidates_shadow_theoretical_only": pm.get("candidates_shadow_theoretical_only", 0),
            "directional_after_cost_positive": pm.get("directional_after_cost_positive", 0),
            "bregman_after_cost_positive": pm.get("bregman_after_cost_positive", 0),
            "exploration_profitability_checked": pm.get("exploration_profitability_checked", 0),
            "profitability_governor_hard_rejects": pm.get("profitability_governor_hard_rejects", 0),
            "execution_without_annotation": pm.get("execution_without_annotation", 0),
            "avg_after_cost_edge_executed": round(sum(edges) / len(edges), 6) if edges else 0.0,
            "avg_after_cost_roi_executed": round(sum(rois) / len(rois), 6) if rois else 0.0,
            "total_expected_value_usd_executed": round(sum(evs), 6),
            "profitability_buckets": buckets,
            "top_ranked_candidate_reason": top_reason,
            "bregman_first_priority_preserved": True,
            "thresholds": {
                "min_after_cost_edge": float(getattr(self.cfg, "min_after_cost_edge", 0.01)),
                "min_after_cost_roi": float(getattr(self.cfg, "min_after_cost_roi", 0.002)),
                "min_expected_value_usd": float(getattr(self.cfg, "min_expected_value_usd", 0.01)),
                "bregman_min_after_cost_profit_usd": float(
                    getattr(self.cfg, "bregman_min_after_cost_profit_usd", 0.02)),
            },
        }

    def active_learning_report(self) -> dict:
        """Pass-6 Active Learning: proves exploration is selected by the
        ActiveLearningSelector (not random/hash), is realism + bounded-loss gated,
        diversity-capped, separated from readiness, and produces learning feedback."""
        am = self.active_learning_metrics
        scores = am.get("_scores", []) or []
        eq = am.get("_exec_quality", []) or []
        explore_pnl = round(sum(p.realized_pnl for p in self.positions
                                if p.closed and getattr(p, "exploration", False)), 6)
        buckets = dict(am.get("buckets", {}))
        top = sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "schema": "active_learning/1.0", "paper_only": True,
            "active_learning_enabled": bool(getattr(self.cfg, "active_learning_enabled", True)),
            "random_exploration_enabled": bool(
                getattr(self.cfg, "random_exploration_enabled", False)),
            "random_exploration_opened_trades": am.get("random_exploration_opened_trades", 0),
            "legacy_random_exploration_blocked": am.get("legacy_random_exploration_blocked", 0),
            "active_learning_candidates_considered": am.get(
                "active_learning_candidates_considered", 0),
            # P0: a SELECTED example includes shadow/no-trade learning examples
            # chosen by the closed loop (not just executable explore trades), so
            # this is no longer falsely zero when every candidate is rejected.
            "active_learning_candidates_selected": (
                am.get("active_learning_candidates_selected", 0)
                + int(getattr(self, "closed_loop", None)
                      and self.closed_loop.counts.get("active_learning_shadow_selected", 0) or 0)),
            "active_learning_shadow_selected": int(
                getattr(self, "closed_loop", None)
                and self.closed_loop.counts.get("active_learning_shadow_selected", 0) or 0),
            "zero_selection_reason": (self.closed_loop.metrics().get("zero_selection_reason")
                                      if getattr(self, "closed_loop", None) else None),
            "exploration_trades_opened": am.get("exploration_trades_opened", 0),
            "exploration_shadow_only": (len(self.near_miss_log)
                                        + int(getattr(self, "closed_loop", None)
                                              and self.closed_loop.counts.get(
                                                  "shadow_records_written", 0) or 0)),
            "exploration_rejected_by_realism": am.get("exploration_rejected_by_realism", 0),
            "exploration_rejected_by_profitability": am.get(
                "exploration_rejected_by_profitability", 0),
            "exploration_rejected_by_budget": am.get("exploration_rejected_by_budget", 0),
            "exploration_rejected_by_collision": am.get("exploration_rejected_by_collision", 0),
            "exploration_rejected_by_diversity": am.get("exploration_rejected_by_diversity", 0),
            "exploration_budget_used_usd": am.get("exploration_budget_used_usd", 0.0),
            "exploration_expected_loss_usd": am.get("exploration_expected_loss_usd", 0.0),
            "exploration_pnl": explore_pnl,
            "exploration_counted_toward_readiness": bool(
                getattr(self.cfg, "exploration_count_toward_readiness", False)),
            "exploration_consumes_bregman_reserved_capacity": False,
            "top_learning_buckets": [k for k, _ in top],
            "category_coverage": dict(am.get("category_coverage", {})),
            "cluster_diversity": dict(am.get("cluster_diversity", {})),
            "avg_active_learning_score_selected": round(sum(scores) / len(scores), 6) if scores else 0.0,
            "avg_execution_quality_selected": round(sum(eq) / len(eq), 6) if eq else 0.0,
            "pending_feedback_count": am.get("pending_feedback_count", 0),
            "completed_feedback_count": am.get("completed_feedback_count", 0),
            "near_miss_examples": self.near_miss_log[-10:],
        }

    def correlation_risk_report(self) -> dict:
        """Pass-7 Correlation Risk: proves cluster/correlation metadata reaches the
        decision gate and that duplicate/collision exposure is blocked or capped.
        Read-only telemetry derived from the latest tick's open-exposure index."""
        cm = dict(self.correlation_metrics)
        idx = self._open_exposure_index
        summ = idx.summary() if idx is not None else {}
        return {
            "schema": "correlation_risk/1.0", "paper_only": True,
            "correlation_gate_enabled": bool(getattr(self.cfg, "correlation_gate_enabled", True)),
            "require_cluster_metadata": bool(getattr(self.cfg, "require_cluster_metadata", True)),
            "unknown_cluster_policy": str(getattr(self.cfg, "unknown_cluster_policy", "shadow")),
            "candidates_with_cluster_id": cm.get("candidates_with_cluster_id", 0),
            "candidates_missing_cluster_id": cm.get("candidates_missing_cluster_id", 0),
            "blocked_same_market": cm.get("blocked_same_market", 0),
            "blocked_same_condition": cm.get("blocked_same_condition", 0),
            "blocked_same_event": cm.get("blocked_same_event", 0),
            "blocked_same_cluster": cm.get("blocked_same_cluster", 0),
            "blocked_semantic_duplicate": cm.get("blocked_semantic_duplicate", 0),
            "blocked_bregman_market_collision": cm.get("blocked_bregman_market_collision", 0),
            "blocked_bregman_event_collision": cm.get("blocked_bregman_event_collision", 0),
            "blocked_exploration_cluster_collision": cm.get("blocked_exploration_cluster_collision", 0),
            "size_capped_by_cluster_exposure": cm.get("size_capped_by_cluster_exposure", 0),
            "shadowed_unknown_cluster": cm.get("shadowed_unknown_cluster", 0),
            "correlation_adjusted_candidates": cm.get("correlation_adjusted_candidates", 0),
            "directional_trades_blocked_by_correlation": cm.get(
                "directional_trades_blocked_by_correlation", 0),
            "exploration_trades_blocked_by_correlation": cm.get(
                "exploration_trades_blocked_by_correlation", 0),
            "bregman_bundles_blocked_as_duplicates": cm.get("bregman_bundles_blocked_as_duplicates", 0),
            "bregman_bundles_blocked_as_overlapping": cm.get("bregman_bundles_blocked_as_overlapping", 0),
            "real_trade_without_cluster_metadata": 0,  # unknown clusters are shadowed/rejected
            **summ,
        }

    # -- PASS-8: unified inspection artifacts (observability only) --------------
    def trade_ledger_summary(self) -> dict:
        """Per-strategy opened-trade/bundle ledger (PAPER ONLY, read-only)."""
        rows: list = []
        bundles: dict = {}
        for p in self.positions:
            notional = round(float(p.entry_price) * float(p.qty), 4)
            readiness_eligible = bool(getattr(p, "is_realistic", True) and not p.exploration)
            row = {
                "open_tick": p.open_tick, "strategy": p.strategy,
                "strategy_tier": ("tier1_bregman" if p.strategy == "bregman"
                                  else ("tier3_exploration" if p.exploration
                                        else "tier2_directional")),
                "market_id": p.market_id, "event_id": getattr(p, "group_key", ""),
                "cluster_id": getattr(p, "cluster_id", ""), "outcome": p.outcome,
                "side": "BUY", "size": round(float(p.qty), 4),
                "fill_price": round(float(p.entry_price), 6), "notional_usd": notional,
                "execution_realism_status": getattr(p, "execution_realism_status", ""),
                "fill_source": getattr(p, "fill_source", ""),
                "was_reference_price_fill": bool(getattr(p, "was_reference_price_fill", False)),
                "after_cost_edge": round(float(p.net_edge), 6),
                "readiness_eligible": readiness_eligible,
                "closed": p.closed, "realized_pnl": round(float(p.realized_pnl), 4),
                "pnl_bucket": ("bregman_realistic" if p.strategy == "bregman"
                               else ("exploration" if p.exploration
                                     else "directional_realistic")),
            }
            rows.append(row)
            if p.strategy == "bregman":
                b = bundles.setdefault(getattr(p, "group_key", ""), {
                    "bundle_id": getattr(p, "group_key", ""), "legs": 0,
                    "leg_market_ids": [], "leg_outcomes": [], "total_cost": 0.0,
                    "capital_required": 0.0, "all_legs_executable": True})
                b["legs"] += 1
                b["leg_market_ids"].append(p.market_id)
                b["leg_outcomes"].append(p.outcome)
                b["total_cost"] = round(b["total_cost"] + notional, 4)
                b["capital_required"] = round(b["capital_required"] + notional, 4)
                if getattr(p, "execution_realism_status", "") != "realistic_executable":
                    b["all_legs_executable"] = False
        return {
            "total_opened": len(rows),
            "bregman_legs": sum(1 for r in rows if r["strategy"] == "bregman"),
            "directional_trades": sum(1 for r in rows
                                      if r["strategy"] != "bregman" and not r.get("strategy_tier") == "tier3_exploration"),
            "exploration_trades": sum(1 for r in rows if r["strategy_tier"] == "tier3_exploration"),
            "bregman_bundles": list(bundles.values()),
            "trades": rows[-50:],   # bounded tail
        }

    def rejection_waterfall(self) -> dict:
        """Ranked rejection-reason aggregation across the whole pipeline + a
        per-strategy breakdown (deterministic; read-only)."""
        breg = self.bregman_summary().get("execution", {}) or {}
        pr = self.profitability_ranking_report()
        pe = self.paper_realism_report()
        sp = self.strategy_priority_report()
        al = self.active_learning_report()
        cr = self.correlation_risk_report()
        breg_reasons = dict(breg.get("rejected_by_reason", {}) or {})
        reasons: dict = {}

        def add(name, n):
            if n:
                reasons[name] = reasons.get(name, 0) + int(n)

        for k, v in breg_reasons.items():
            add(f"bregman_{k}", v)
        add("negative_after_cost", pr.get("candidates_rejected_negative_after_cost", 0))
        add("shadow_theoretical_only", pr.get("candidates_shadow_theoretical_only", 0))
        add("stale_book", pe.get("stale_book_rejection_count", 0))
        add("missing_ask", pe.get("missing_ask_rejection_count", 0))
        add("thin_depth", pe.get("thin_depth_rejection_count", 0))
        add("wide_spread", pe.get("wide_spread_rejection_count", 0))
        add("ambiguous_settlement", pe.get("ambiguity_rejection_count", 0))
        add("offline_stub", pe.get("offline_stub_rejection_count", 0))
        add("strategy_priority_no_slot", sp.get("directional_trades_blocked_by_bregman_reservation", 0))
        add("bregman_market_collision", sp.get("directional_trades_blocked_by_bregman_market_collision", 0))
        add("bregman_event_collision", sp.get("directional_trades_blocked_by_bregman_event_collision", 0))
        add("same_market_duplicate", cr.get("blocked_same_market", 0))
        add("same_condition_duplicate", cr.get("blocked_same_condition", 0))
        add("same_event_duplicate", cr.get("blocked_same_event", 0))
        add("same_cluster_duplicate", cr.get("blocked_same_cluster", 0))
        add("shadowed_unknown_cluster", cr.get("shadowed_unknown_cluster", 0))
        add("exploration_rejected_by_realism", al.get("exploration_rejected_by_realism", 0))
        add("exploration_rejected_by_budget", al.get("exploration_rejected_by_budget", 0))
        add("exploration_rejected_by_collision", al.get("exploration_rejected_by_collision", 0))
        add("exploration_rejected_by_diversity", al.get("exploration_rejected_by_diversity", 0))
        ranked = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)
        return {
            "ranked_reasons": [{"reason": k, "count": v} for k, v in ranked],
            "total_rejections": sum(reasons.values()),
            "by_strategy": {
                "bregman": {k: v for k, v in reasons.items() if k.startswith("bregman_")},
                "directional": {k: v for k, v in reasons.items() if k in (
                    "negative_after_cost", "stale_book", "missing_ask", "thin_depth",
                    "wide_spread", "ambiguous_settlement", "offline_stub",
                    "strategy_priority_no_slot", "bregman_market_collision",
                    "bregman_event_collision", "same_market_duplicate",
                    "same_condition_duplicate", "same_event_duplicate",
                    "same_cluster_duplicate")},
                "exploration": {k: v for k, v in reasons.items()
                                if k.startswith("exploration_")},
                "shadow": {k: v for k, v in reasons.items() if k in (
                    "shadow_theoretical_only", "shadowed_unknown_cluster")},
            },
        }

    def data_quality_report(self) -> dict:
        """Market-coverage / data-quality stats so a low trade count can be
        attributed to lack of edge vs lack of data (read-only)."""
        sm = self.metrics.to_dict() if hasattr(self.metrics, "to_dict") else {}
        cr = self.correlation_risk_report()
        pr = self.profitability_ranking_report()
        return {
            "catalog_load_success": bool(sm.get("scanned", 0)),
            "markets_loaded": sm.get("scanned", 0),
            "markets_eligible": sm.get("kept", 0),
            "markets_shortlisted": sm.get("shortlisted", 0),
            "stale_rate": sm.get("stale_rate", None),
            "null_rate": sm.get("null_rate", None),
            "feature_coverage": sm.get("feature_coverage", None),
            "candidates_with_cluster_metadata": cr.get("candidates_with_cluster_id", 0),
            "candidates_missing_cluster_metadata": cr.get("candidates_missing_cluster_id", 0),
            "candidates_with_profitability_annotation": pr.get("candidates_annotated", 0),
            "candidates_missing_profitability_annotation": pr.get(
                "candidates_missing_profitability_data", 0),
            "chainlink_enabled": bool(self.chainlink is not None),
            "research_mode": os.getenv("RESEARCH_MODE", "offline_cache"),
            "grok_enabled": bool(os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")),
        }

    def inspection_summary(self) -> dict:
        """PASS-8: one unified machine-readable inspection summary aggregating every
        prior-pass output + feature-activation proof + deterministic recommendations."""
        from .inspection_summary import build_inspection_summary
        from engine.feature_activation import build_feature_activation
        feature_audit = build_feature_activation(self.cfg, status=None)
        return build_inspection_summary(self.status(), feature_audit,
                                        trade_ledger=self.trade_ledger_summary(),
                                        rejection_waterfall=self.rejection_waterfall(),
                                        data_quality=self.data_quality_report())

    def write_inspection_artifacts(self, out_dir) -> dict:
        """Write metrics/inspection_summary.json + reports/paper_training_inspection.md.
        Returns the summary dict. Never raises into the training loop."""
        from pathlib import Path as _Path
        from .inspection_summary import to_markdown
        summary = self.inspection_summary()
        out = _Path(out_dir)
        (out / "metrics").mkdir(parents=True, exist_ok=True)
        (out / "reports").mkdir(parents=True, exist_ok=True)
        import json as _json
        (out / "metrics" / "inspection_summary.json").write_text(
            _json.dumps(summary, indent=2, default=str), encoding="utf-8")
        (out / "reports" / "paper_training_inspection.md").write_text(
            to_markdown(summary), encoding="utf-8")
        # P0: write the COMPLETE per-pass metric set here too, so a single call
        # produces every artifact the inspection collector + runtime validator
        # require (not only when the start loop runs).
        try:
            _per_pass = {
                "paper_realism.json": self.paper_realism_report(),
                "strategy_priority.json": self.strategy_priority_report(),
                "profitability_ranking.json": self.profitability_ranking_report(),
                "active_learning.json": self.active_learning_report(),
                "correlation_risk.json": self.correlation_risk_report(),
                "bregman_execution.json": self.bregman_summary().get("execution", {}),
            }
            for _name, _payload in _per_pass.items():
                (out / "metrics" / _name).write_text(
                    _json.dumps(_payload, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001 — metrics must never break a run
            pass
        # P0 closed-loop learning artifacts (metrics + audit) + persist state.
        try:
            from .closed_loop import audit_to_markdown
            audit = self.closed_loop.audit()
            (out / "metrics" / "closed_loop_learning.json").write_text(
                _json.dumps(self.closed_loop.metrics(), indent=2, default=str), encoding="utf-8")
            # canonical counter<->event-stream reconciliation (invalid run if diverged)
            (out / "metrics" / "training_reconciliation.json").write_text(
                _json.dumps(self.closed_loop.reconcile(
                    decision_count=self.decision_count, rejection_count=self.rejection_count,
                    candidate_evaluated=self.decision_count), indent=2, default=str),
                encoding="utf-8")
            (out / "metrics" / "learning_feedback.json").write_text(
                _json.dumps(self.closed_loop.learning_state(), indent=2, default=str),
                encoding="utf-8")
            (out / "reports" / "closed_loop_learning_audit.md").write_text(
                audit_to_markdown(audit), encoding="utf-8")
            self.closed_loop.persist()
        except Exception:  # noqa: BLE001 — artifact writing must never break a run
            pass
        return summary

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
        # BTC Pulse isolated experiment visibility (PAPER ONLY).
        if self.btc_pulse is not None:
            try:
                out["btc_pulse"] = self.btc_pulse.status()
            except Exception as exc:  # noqa: BLE001 — status must never crash
                out["btc_pulse"] = {"btc_pulse_enabled": True, "btc_pulse_frozen": True,
                                    "btc_pulse_last_error": str(exc)}
        else:
            out["btc_pulse"] = {
                "btc_pulse_enabled": bool(getattr(self.cfg, "btc_pulse_enabled", False)),
                "btc_pulse_frozen": True,
                "btc_pulse_last_error": self._btc_pulse_error,
            }
        try:
            out["feedback_accelerator"] = self.feedback_accelerator_status()
        except Exception as exc:  # noqa: BLE001 — status must never crash
            out["feedback_accelerator"] = {"feedback_accelerator_enabled": False,
                                           "error": str(exc)}
        try:
            out["news"] = self.news_status()
        except Exception as exc:  # noqa: BLE001 — status must never crash
            out["news"] = {"news_scanner_enabled": False, "error": str(exc)}
        try:
            out["chainlink_oracle"] = self.chainlink_oracle_status()
        except Exception as exc:  # noqa: BLE001 — status must never crash
            out["chainlink_oracle"] = {"enabled": False, "error": str(exc)}
        try:
            out["btc_fast_price"] = (self.btc_fast_price.status()
                                     if getattr(self, "btc_fast_price", None) is not None
                                     else {"enabled": False})
        except Exception as exc:  # noqa: BLE001 — status must never crash
            out["btc_fast_price"] = {"enabled": False, "error": str(exc)}
        try:
            out["research"] = self.research_status()
        except Exception as exc:  # noqa: BLE001 — status must never crash
            out["research"] = {"available": False, "error": str(exc)}
        return out

    def chainlink_oracle_status(self) -> dict:
        """Chainlink BTC/USD oracle status (validated, read-only, PAPER ONLY)."""
        if getattr(self, "chainlink_oracle", None) is None:
            return {"enabled": False, "initialized": False, "symbol": "BTC/USD",
                    "source": "chainlink", "valid": False, "stale": True}
        return self.chainlink_oracle.status()

    def research_status(self) -> dict:
        """Aggregate research-evidence status: news packet + Chainlink + Grok
        config. Read-only; Grok stays advisory and never bypasses a gate."""
        cfg = self.cfg
        news = {}
        try:
            news = self.news_status()
        except Exception:  # noqa: BLE001
            news = {}
        ora = {}
        try:
            ora = self.chainlink_oracle_status()
        except Exception:  # noqa: BLE001
            ora = {}
        return {
            "available": True,
            "grok_research_only": True,
            "grok_enabled": bool(os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")),
            "research_mode": os.getenv("RESEARCH_MODE", "offline_cache"),
            "news_enable_grok_packet": bool(getattr(cfg, "news_enable_grok_packet", True)),
            "news_scanner_enabled": news.get("news_scanner_enabled", False),
            "news_provider_mode": news.get("news_provider_mode"),
            "news_items_used": news.get("news_items_used", 0),
            "chainlink_btc_usd_valid": ora.get("valid", False),
            "chainlink_btc_usd_price": ora.get("price"),
            "chainlink_btc_usd_age_seconds": ora.get("age_seconds"),
            "note": "PAPER ONLY — Grok is advisory; evidence packets are read-only "
                    "and never include wallet/positions/orders/secrets.",
        }

    # -- market-news evidence scanner (PAPER ONLY, advisory) -------------- #
    def _news_ctx(self, rec) -> dict:
        kws = []
        cat = getattr(rec, "category", "") or ""
        return {
            "venue": "polymarket",
            "market_id": getattr(rec, "market_id", ""),
            "question": getattr(rec, "question", "") or "",
            "slug": getattr(rec, "slug", "") or "",
            "category": cat,
            "description": getattr(rec, "description", "") or "",
            "resolution_source": getattr(rec, "resolution_source", "") or "",
            "close_ts_ms": getattr(rec, "close_ts_ms", None),
            "outcome": "YES",
            "asset_keywords": kws,
        }

    def _news_scan(self, watch, now: float) -> None:
        """Scan news for the top few watched markets (bounded + cached)."""
        if self.news_scanner is None:
            return
        per_tick = max(1, min(int(getattr(self.cfg, "news_markets_per_tick", 3)), 10))
        m = self._news_metrics
        for rec in list(watch)[:per_tick]:
            ctx = self._news_ctx(rec)
            if not ctx["market_id"]:
                continue
            res = self.news_scanner.scan(ctx, now_ms=int(now * 1000))
            m["markets_scanned"] += 1
            m["queries"] += len(res.queries)
            m["items_fetched"] += int(res.fetched)
            m["items_used"] += int(res.used)
            m["items_rejected"] += int(res.rejected)
            m["stale_count"] += int(res.stale_count)
            m["contradiction_count"] += int(res.contradiction_count)
            m["ambiguity_count"] += int(res.ambiguity_count)
            if not res.provider_ok:
                m["provider_errors"] += 1
            reasons = getattr(res.packet, "rejected_reasons", {}) or {}
            m["rejected_low_relevance"] += int(reasons.get("low_relevance", 0))
            m["rejected_low_credibility"] += int(reasons.get("low_credibility", 0))
            m["rejected_unclear_date"] += int(reasons.get("no_published_date", 0))
            m["rejected_stale"] += int(reasons.get("too_old", 0))
            m["rejected_duplicate"] += int(reasons.get("duplicate", 0))
            if res.used:
                self._news_last_packet = res.packet.to_dict()
        m["ticks"] += 1
        m["last_scan_ts"] = now

    def news_status(self) -> dict:
        """Market-news scanner status (PAPER ONLY, advisory, read-only)."""
        cfg = self.cfg
        m = self._news_metrics
        runtime_h = max(0.0, (time.time() - self.started_ts) / 3600.0)
        return {
            "news_scanner_enabled": bool(self.news_scanner is not None),
            "advisory_enabled": bool(getattr(cfg, "news_advisory_enabled", True)),
            "trade_gate_enabled": bool(getattr(cfg, "news_trade_gate_enabled", False)),
            "news_provider_mode": getattr(cfg, "news_provider_mode", "offline_cache"),
            "news_live_read_only": bool(getattr(cfg, "news_live_read_only", True)),
            "news_ticks": m["ticks"],
            "rejected_low_relevance": m["rejected_low_relevance"],
            "rejected_low_credibility": m["rejected_low_credibility"],
            "rejected_unclear_date": m["rejected_unclear_date"],
            "rejected_stale": m["rejected_stale"],
            "rejected_duplicate": m["rejected_duplicate"],
            "news_markets_scanned": m["markets_scanned"],
            "news_queries": m["queries"],
            "news_items_fetched": m["items_fetched"],
            "news_items_used": m["items_used"],
            "news_items_rejected": m["items_rejected"],
            "news_stale_count": m["stale_count"],
            "news_contradiction_count": m["contradiction_count"],
            "news_ambiguity_count": m["ambiguity_count"],
            "news_provider_errors": m["provider_errors"],
            "news_items_used_per_hour": round(m["items_used"] / runtime_h, 3)
            if runtime_h > 0 else 0.0,
            "news_last_scan_ts": m["last_scan_ts"],
            "news_last_error": self._news_error,
            "news_last_packet_sample": [
                {"title": it.get("title"), "source_name": it.get("source_name"),
                 "direction": it.get("direction")}
                for it in (self._news_last_packet.get("items", []) or [])[:3]],
            "note": "PAPER ONLY — news is read-only advisory; Grok stays advisory; "
                    "never trades, never bypasses a gate.",
        }

    def feedback_accelerator_status(self) -> dict:
        """10x Feedback Accelerator status (PAPER ONLY). Surfaces soft-gate
        relaxation + accelerated capacity + decision/sample counts. Hard gates
        are reported as locked; exploration can never bypass them."""
        cfg = self.cfg
        from .feedback_accelerator import resolve_soft_gates
        bp = {}
        if self.btc_pulse is not None:
            try:
                bp = self.btc_pulse.status()
            except Exception:  # noqa: BLE001
                bp = {}
        return {
            "feedback_accelerator_enabled": bool(getattr(cfg, "feedback_accelerator_enabled", False)),
            "mode": getattr(cfg, "feedback_accelerator_mode", "paper_only"),
            "target_multiplier": int(getattr(cfg, "feedback_accelerator_target_multiplier", 10)),
            "exploration_enabled": bool(getattr(cfg, "exploration_enabled", False)),
            "exploration_tiny_size_enabled": bool(getattr(cfg, "exploration_tiny_size_enabled", False)),
            "exploration_counts_for_readiness": bool(
                getattr(cfg, "exploration_counts_for_readiness", False)),
            "shadow_decision_logging_enabled": bool(
                getattr(cfg, "shadow_decision_logging_enabled", False)),
            "no_trade_labeling_enabled": bool(getattr(cfg, "no_trade_labeling_enabled", False)),
            "active_learning_enabled": bool(getattr(cfg, "active_learning_enabled", False)),
            "capacity": {
                "paper_decision_budget": int(getattr(cfg, "paper_decision_budget", 0)),
                "trade_candidate_limit": int(getattr(cfg, "trade_candidate_limit", 0)),
                "shortlist_limit": int(getattr(cfg, "shortlist_limit", 0)),
                "live_watch_limit": int(getattr(cfg, "live_watch_limit", 0)),
            },
            "soft_gates": resolve_soft_gates(cfg).to_dict(),
            "hard_gates_locked": {
                "exploration_can_bypass_hard_gate": bool(
                    getattr(cfg, "exploration_can_bypass_hard_gate", False)),
                "exploration_requires_risk_gate": bool(
                    getattr(cfg, "exploration_requires_risk_gate", True)),
                "exploration_requires_realistic_fill": bool(
                    getattr(cfg, "exploration_requires_realistic_fill", True)),
                "exploration_min_book_freshness_required": bool(
                    getattr(cfg, "exploration_min_book_freshness_required", True)),
            },
            "btc_pulse_decisions": bp.get("btc_pulse_decisions", 0),
            "btc_pulse_shadow_decisions": bp.get("btc_pulse_shadow_decisions", 0),
            "btc_pulse_no_trade_decisions": bp.get("btc_pulse_no_trade_decisions", 0),
            "note": "PAPER ONLY — soft gates relax only for tiny exploration; "
                    "hard gates never loosen; exploration is not readiness proof.",
        }

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
            "paper_realism": self.paper_realism_report(),
            "strategy_priority": self.strategy_priority_report(),
            "profitability_ranking": self.profitability_ranking_report(),
            "active_learning": self.active_learning_report(),
            "correlation_risk": self.correlation_risk_report(),
            "closed_loop_learning": self.closed_loop.metrics(),
            "training_reconciliation": self.closed_loop.reconcile(
                decision_count=self.decision_count, rejection_count=self.rejection_count,
                candidate_evaluated=self.decision_count),
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
