"""Polymarket Training Engine v2 — configuration.

Single source of truth for the trainer config. PAPER ONLY. The default mode is
``observe_only`` (evaluate + record diagnostics, NEVER place paper trades);
``paper_train`` enables simulated paper trades (still no real orders); ``disabled``
turns the loop off entirely. Live-execution flags are tracked only so we can
FAIL CLOSED if any are set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from engine.markets import universe_manager as um

# Live-execution flags that must NEVER be on for a PAPER training run.
FORBIDDEN_LIVE_FLAGS = (
    "MICRO_LIVE_ENABLED", "KALSHI_MICRO_LIVE_ENABLED", "POLYMARKET_MICRO_LIVE_ENABLED",
    "MICRO_LIVE_ALLOW_PRODUCTION", "GUARDED_LIVE_ENABLED",
    "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION",
    "PRODUCTION_REVIEW_ALLOW_AUTONOMOUS_LIVE", "PRODUCTION_REVIEW_ALLOW_DASHBOARD_SUBMIT",
    "PRODUCTION_REVIEW_ALLOW_API_SUBMIT",
)

MODES = ("disabled", "observe_only", "paper_train")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _envb(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def _env_csv_floats(name: str, default: str) -> list:
    raw = os.getenv(name, default)
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    return out or [float(x) for x in default.split(",")]


@dataclass
class TrainingConfig:
    # ---- mode / universe gating ----
    mode: str = "observe_only"
    polymarket_only: bool = True
    disable_btc_pulse_trading: bool = True
    # ---- scanner ----
    scan_limit: int = 1000
    scan_interval_seconds: float = 60.0
    metadata_cache_ttl_s: float = 60.0
    incremental_refresh: bool = True
    async_scan: bool = True
    max_concurrent_requests: int = 8
    scan_timeout_s: float = 20.0
    rate_limit_sleep_ms: float = 100.0
    min_volume: float = 1000.0
    min_liquidity: float = 250.0
    min_time_to_close_s: float = 3600.0
    max_time_to_close_days: float = 90.0
    # ---- ranker ----
    shortlist_limit: int = 150
    live_watch_limit: int = 100
    trade_candidate_limit: int = 30
    # ---- subscription ----
    clob_enabled: bool = True
    subscribe_trending: bool = True
    clob_stale_ms: float = 3000.0
    subscription_refresh_s: float = 120.0
    max_subscription_churn: int = 20
    # ---- probability ----
    require_research_or_model_edge: bool = True
    allow_offline_stub_trading: bool = False
    research_max_age_s: float = 900.0
    min_evidence_score: float = 0.50
    min_source_count: int = 2
    max_ambiguity_score: float = 0.35
    base_shrink_factor: float = 0.25
    max_shrink_factor: float = 0.60
    min_shrink_factor: float = 0.05
    # ---- edge engine ----
    min_net_edge: float = 0.03
    base_uncertainty: float = 0.01
    spread_penalty_weight: float = 0.50
    slippage_penalty_weight: float = 1.00
    ambiguity_penalty_weight: float = 0.03
    stale_penalty_weight: float = 0.02
    evidence_penalty_weight: float = 0.02
    calibration_penalty_weight: float = 0.05
    liquidity_penalty_weight: float = 0.03
    ambiguity_uncertainty_weight: float = 0.05
    spread_uncertainty_weight: float = 0.50
    stale_uncertainty_weight: float = 0.03
    evidence_uncertainty_weight: float = 0.04
    calibration_uncertainty_weight: float = 0.50
    max_spread: float = 0.08
    min_depth_at_price: float = 50.0
    taker_fee_bps: float = 0.0
    slippage_bps: float = 25.0
    max_fill_depth_fraction: float = 0.35
    # ---- paper policy / sizing ----
    fixed_notional_usd: float = 5.0
    max_open_trades: int = 5
    max_open_trades_hard_cap: int = 8
    max_market_exposure_usd: float = 20.0
    max_total_exposure_usd: float = 100.0
    max_daily_loss_usd: float = 50.0
    max_open_orders: int = 20
    reject_on_stale_book: bool = True
    allow_pm_reference_price_fills: bool = False
    use_kelly_for_diagnostics: bool = True
    use_kelly_for_size: bool = False
    kelly_fraction: float = 0.10
    max_kelly_size_usd: float = 5.0
    # ---- learner / feedback ----
    learner_enabled: bool = True
    ewma_alpha: float = 0.05
    min_bucket_samples: int = 20
    feedback_interval_seconds: float = 300.0
    markout_horizons_s: tuple = (5.0, 30.0, 60.0, 300.0, 900.0, 3600.0)
    feedback_enabled: bool = True
    # ---- exploration (aggressive paper training; bounded, PAPER ONLY) ----
    exploration_enabled: bool = False
    exploration_rate: float = 0.0          # fraction of near-miss candidates explored
    exploration_notional_usd: float = 2.0  # small exploratory size (clamped to caps)
    exploration_min_edge: float = -0.01    # min net_edge eligible for exploration
    # ---- Chainlink oracle layer (additive; default OFF) ----
    chainlink_enabled: bool = False
    chainlink_history_limit: int = 30
    # ---- Bregman arbitrage (flagship Polymarket strategy; PAPER ONLY) ----
    # Bregman opportunities are scanned every tick and outrank directional trades
    # only when certified with a positive profit lower bound after all costs.
    bregman_enabled: bool = True
    bregman_execution_enabled: bool = True
    bregman_min_profit_usd: float = 0.001
    bregman_target_capital_usd: float = 50.0
    # ---- portfolio risk + aggressive sizing (PAPER ONLY; hard-clamped) ----
    # Additive caps that only ever TIGHTEN the mandatory TrainingRiskGate/RiskEngine.
    max_event_exposure_usd: float = 20.0
    max_category_exposure_usd: float = 40.0
    max_bregman_bundle_exposure_usd: float = 30.0
    diversity_target: int = 5
    exploration_budget_usd: float = 20.0
    max_drawdown_usd: float = 50.0
    cvar_alpha: float = 0.95
    kelly_max_fraction: float = 0.05
    leg_failure_haircut: float = 0.5
    chainlink_freshness_penalty_weight: float = 0.5
    settlement_ambiguity_penalty_weight: float = 0.5
    # ---- institutional features + grouping (additive; default ON, offline) ----
    feature_extraction_enabled: bool = True
    grouping_enabled: bool = True
    # ---- paper decision budget + feedback targets (aggressive widens these) ----
    paper_decision_budget: int = 30      # max candidates evaluated per tick
    feedback_sample_target: int = 200    # target feedback-loop samples
    tiny_trade_min_liquidity: float = 100.0  # liquidity floor for tiny paper trades
    # ---- run / sim ----
    take_profit: float = 0.05
    stop_loss: float = 0.05
    max_hold_ticks: int = 20
    signal_model: str = "research"
    starting_bankroll: float = 500.0
    universe: object = None

    # back-compat aliases used by older v1 code/tests
    @property
    def max_allowed_spread(self) -> float:
        return self.universe.max_allowed_spread if self.universe else 0.04

    @property
    def min_top_of_book_depth_usd(self) -> float:
        return self.min_depth_at_price

    @property
    def max_ambiguity(self) -> float:
        return self.max_ambiguity_score

    @property
    def min_evidence(self) -> float:
        return self.min_evidence_score

    @property
    def kelly_enabled(self) -> bool:
        return self.use_kelly_for_size

    @property
    def kelly_fraction_cap(self) -> float:
        return self.kelly_fraction

    @property
    def kelly_multiplier(self) -> float:
        return 1.0

    def __post_init__(self):
        if self.mode not in MODES:
            self.mode = "observe_only"
        # hard PAPER clamps (cannot exceed even if env is misconfigured)
        self.fixed_notional_usd = max(0.0, min(self.fixed_notional_usd, 50.0))
        self.max_kelly_size_usd = max(0.0, min(self.max_kelly_size_usd, 50.0))
        self.max_market_exposure_usd = max(0.0, min(self.max_market_exposure_usd, 500.0))
        self.max_total_exposure_usd = max(0.0, min(self.max_total_exposure_usd, 5000.0))
        self.max_open_trades = max(0, min(self.max_open_trades, self.max_open_trades_hard_cap, 8))
        self.scan_limit = max(1, min(self.scan_limit, 2000))
        self.base_shrink_factor = max(self.min_shrink_factor,
                                      min(self.base_shrink_factor, self.max_shrink_factor))
        # exploration is bounded + PAPER-only: clamp rate and cap exploratory size
        # to the same hard paper order-notional ceiling (cannot bypass risk caps).
        self.exploration_rate = max(0.0, min(1.0, self.exploration_rate))
        self.exploration_notional_usd = max(0.0, min(self.exploration_notional_usd,
                                                     self.max_order_notional_usd))
        # hard PAPER clamps for the portfolio caps (cannot be raised by config/env)
        self.max_event_exposure_usd = max(0.0, min(self.max_event_exposure_usd, 500.0))
        self.max_category_exposure_usd = max(0.0, min(self.max_category_exposure_usd, 1000.0))
        self.max_bregman_bundle_exposure_usd = max(
            0.0, min(self.max_bregman_bundle_exposure_usd, 1000.0))
        self.exploration_budget_usd = max(0.0, min(self.exploration_budget_usd, 200.0))
        self.max_drawdown_usd = max(0.0, min(self.max_drawdown_usd, 5000.0))
        self.diversity_target = max(0, min(int(self.diversity_target), 100))
        self.cvar_alpha = min(0.999, max(0.5, self.cvar_alpha))
        self.kelly_max_fraction = max(0.0, min(self.kelly_max_fraction, 0.5))
        self.leg_failure_haircut = max(0.0, min(self.leg_failure_haircut, 1.0))
        if self.universe is None:
            self.universe = um.UniverseConfig.from_env()

    @property
    def is_paper_only(self) -> bool:
        """Hard invariant: this config can only drive PAPER training. Live order
        execution is never reachable from a TrainingConfig (no live mode exists)."""
        return self.mode in ("disabled", "observe_only", "paper_train")

    @property
    def max_order_notional_usd(self) -> float:
        return max(self.fixed_notional_usd, self.max_kelly_size_usd)

    @classmethod
    def from_env(cls) -> "TrainingConfig":
        ucfg = um.UniverseConfig.from_env()
        mode = (os.getenv("POLYMARKET_TRAINING_MODE") or "observe_only").strip().lower()
        return cls(
            mode=mode,
            polymarket_only=_envb("POLYMARKET_ONLY_MODE", True),
            disable_btc_pulse_trading=_envb("DISABLE_BTC_PULSE_TRADING", True),
            scan_limit=_envi("POLYMARKET_SCAN_LIMIT", 1000),
            scan_interval_seconds=_envf("POLYMARKET_SCAN_INTERVAL_SECONDS", 60.0),
            metadata_cache_ttl_s=_envf("POLYMARKET_METADATA_CACHE_TTL_SECONDS", 60.0),
            incremental_refresh=_envb("POLYMARKET_INCREMENTAL_REFRESH", True),
            async_scan=_envb("POLYMARKET_ASYNC_SCAN", True),
            max_concurrent_requests=_envi("POLYMARKET_MAX_CONCURRENT_REQUESTS", 8),
            scan_timeout_s=_envf("POLYMARKET_SCAN_TIMEOUT_SECONDS", 20.0),
            rate_limit_sleep_ms=_envf("POLYMARKET_RATE_LIMIT_SLEEP_MS", 100.0),
            min_volume=_envf("POLYMARKET_MIN_VOLUME", 1000.0),
            min_liquidity=_envf("POLYMARKET_MIN_LIQUIDITY", 250.0),
            min_time_to_close_s=_envf("POLYMARKET_MIN_TIME_TO_CLOSE_SECONDS", 3600.0),
            max_time_to_close_days=_envf("POLYMARKET_MAX_TIME_TO_CLOSE_DAYS", 90.0),
            shortlist_limit=_envi("POLYMARKET_SHORTLIST_LIMIT", 150),
            live_watch_limit=_envi("POLYMARKET_LIVE_WATCH_LIMIT", 100),
            trade_candidate_limit=_envi("POLYMARKET_TRADE_CANDIDATE_LIMIT", 30),
            clob_enabled=_envb("POLYMARKET_CLOB_ENABLED", True),
            subscribe_trending=_envb("POLYMARKET_CLOB_SUBSCRIBE_TRENDING", True),
            clob_stale_ms=_envf("POLYMARKET_CLOB_STALE_MS", 3000.0),
            subscription_refresh_s=_envf("POLYMARKET_SUBSCRIPTION_REFRESH_SECONDS", 120.0),
            max_subscription_churn=_envi("POLYMARKET_MAX_SUBSCRIPTION_CHURN_PER_REFRESH", 20),
            require_research_or_model_edge=_envb("POLYMARKET_REQUIRE_RESEARCH_OR_MODEL_EDGE", True),
            allow_offline_stub_trading=_envb("POLYMARKET_ALLOW_OFFLINE_STUB_TRADING", False),
            research_max_age_s=_envf("POLYMARKET_RESEARCH_MAX_AGE_SECONDS", 900.0),
            min_evidence_score=_envf("POLYMARKET_MIN_EVIDENCE_SCORE", 0.50),
            min_source_count=_envi("POLYMARKET_MIN_SOURCE_COUNT", 2),
            max_ambiguity_score=_envf("POLYMARKET_MAX_AMBIGUITY_SCORE", 0.35),
            base_shrink_factor=_envf("POLYMARKET_BASE_SHRINK_FACTOR", 0.25),
            max_shrink_factor=_envf("POLYMARKET_MAX_SHRINK_FACTOR", 0.60),
            min_shrink_factor=_envf("POLYMARKET_MIN_SHRINK_FACTOR", 0.05),
            min_net_edge=_envf("POLYMARKET_MIN_NET_EDGE", 0.03),
            base_uncertainty=_envf("POLYMARKET_BASE_UNCERTAINTY", 0.01),
            spread_penalty_weight=_envf("POLYMARKET_SPREAD_PENALTY_WEIGHT", 0.50),
            slippage_penalty_weight=_envf("POLYMARKET_SLIPPAGE_PENALTY_WEIGHT", 1.00),
            ambiguity_penalty_weight=_envf("POLYMARKET_AMBIGUITY_PENALTY_WEIGHT", 0.03),
            stale_penalty_weight=_envf("POLYMARKET_STALE_PENALTY_WEIGHT", 0.02),
            evidence_penalty_weight=_envf("POLYMARKET_EVIDENCE_PENALTY_WEIGHT", 0.02),
            calibration_penalty_weight=_envf("POLYMARKET_CALIBRATION_PENALTY_WEIGHT", 0.05),
            liquidity_penalty_weight=_envf("POLYMARKET_LIQUIDITY_PENALTY_WEIGHT", 0.03),
            max_spread=_envf("POLYMARKET_MAX_SPREAD", 0.08),
            min_depth_at_price=_envf("POLYMARKET_MIN_DEPTH_AT_PRICE", 50.0),
            taker_fee_bps=_envf("PAPER_TAKER_FEE_BPS", 0.0),
            slippage_bps=_envf("PAPER_SLIPPAGE_BPS", 25.0),
            max_fill_depth_fraction=_envf("PAPER_MAX_FILL_DEPTH_FRACTION", 0.35),
            fixed_notional_usd=_envf("POLYMARKET_PAPER_FIXED_NOTIONAL_USD", 5.0),
            max_open_trades=_envi("POLYMARKET_MAX_OPEN_TRADES", 5),
            max_open_trades_hard_cap=_envi("POLYMARKET_MAX_OPEN_TRADES_HARD_CAP", 8),
            max_market_exposure_usd=_envf("POLYMARKET_MAX_MARKET_EXPOSURE_USD", 20.0),
            max_total_exposure_usd=_envf("POLYMARKET_MAX_TOTAL_EXPOSURE_USD", 100.0),
            max_daily_loss_usd=_envf("POLYMARKET_MAX_DAILY_LOSS_USD", 50.0),
            max_open_orders=_envi("PAPER_MAX_OPEN_ORDERS", 20),
            reject_on_stale_book=_envb("PAPER_REJECT_ON_STALE_BOOK", True),
            allow_pm_reference_price_fills=_envb("PAPER_ALLOW_PM_REFERENCE_PRICE_FILLS", False),
            use_kelly_for_diagnostics=_envb("POLYMARKET_USE_KELLY_FOR_DIAGNOSTICS", True),
            use_kelly_for_size=_envb("POLYMARKET_USE_KELLY_FOR_SIZE", False),
            kelly_fraction=_envf("POLYMARKET_KELLY_FRACTION", 0.10),
            max_kelly_size_usd=_envf("POLYMARKET_MAX_KELLY_SIZE_USD", 5.0),
            learner_enabled=_envb("POLYMARKET_LEARNER_ENABLED", True),
            ewma_alpha=_envf("POLYMARKET_LEARNER_EWMA_ALPHA", 0.05),
            min_bucket_samples=_envi("POLYMARKET_MIN_BUCKET_SAMPLES", 20),
            feedback_interval_seconds=_envf("POLYMARKET_FEEDBACK_INTERVAL_SECONDS", 300.0),
            markout_horizons_s=tuple(_env_csv_floats(
                "POLYMARKET_MARKOUT_HORIZONS_SECONDS", "5,30,60,300,900,3600")),
            feedback_enabled=_envb("POLYMARKET_TRAINING_FEEDBACK_ENABLED", True),
            exploration_enabled=_envb("POLYMARKET_EXPLORATION_ENABLED", False),
            exploration_rate=_envf("POLYMARKET_EXPLORATION_RATE", 0.0),
            exploration_notional_usd=_envf("POLYMARKET_EXPLORATION_NOTIONAL_USD", 2.0),
            exploration_min_edge=_envf("POLYMARKET_EXPLORATION_MIN_EDGE", -0.01),
            chainlink_enabled=_envb("CHAINLINK_ENABLED", False),
            chainlink_history_limit=_envi("CHAINLINK_HISTORY_LIMIT", 30),
            bregman_enabled=_envb("POLYMARKET_BREGMAN_ENABLED", True),
            bregman_execution_enabled=_envb("POLYMARKET_BREGMAN_EXECUTION_ENABLED", True),
            bregman_min_profit_usd=_envf("POLYMARKET_BREGMAN_MIN_PROFIT_USD", 0.001),
            bregman_target_capital_usd=_envf("POLYMARKET_BREGMAN_TARGET_CAPITAL_USD", 50.0),
            max_event_exposure_usd=_envf("POLYMARKET_MAX_EVENT_EXPOSURE_USD", 20.0),
            max_category_exposure_usd=_envf("POLYMARKET_MAX_CATEGORY_EXPOSURE_USD", 40.0),
            max_bregman_bundle_exposure_usd=_envf("POLYMARKET_MAX_BREGMAN_BUNDLE_EXPOSURE_USD", 30.0),
            diversity_target=_envi("POLYMARKET_DIVERSITY_TARGET", 5),
            exploration_budget_usd=_envf("POLYMARKET_EXPLORATION_BUDGET_USD", 20.0),
            max_drawdown_usd=_envf("POLYMARKET_MAX_DRAWDOWN_USD", 50.0),
            cvar_alpha=_envf("POLYMARKET_CVAR_ALPHA", 0.95),
            kelly_max_fraction=_envf("POLYMARKET_KELLY_MAX_FRACTION", 0.05),
            leg_failure_haircut=_envf("POLYMARKET_LEG_FAILURE_HAIRCUT", 0.5),
            chainlink_freshness_penalty_weight=_envf("POLYMARKET_CHAINLINK_FRESHNESS_PENALTY", 0.5),
            settlement_ambiguity_penalty_weight=_envf("POLYMARKET_AMBIGUITY_PENALTY", 0.5),
            feature_extraction_enabled=_envb("POLYMARKET_FEATURE_EXTRACTION_ENABLED", True),
            grouping_enabled=_envb("POLYMARKET_GROUPING_ENABLED", True),
            paper_decision_budget=_envi("POLYMARKET_PAPER_DECISION_BUDGET", 30),
            feedback_sample_target=_envi("POLYMARKET_FEEDBACK_SAMPLE_TARGET", 200),
            tiny_trade_min_liquidity=_envf("POLYMARKET_TINY_TRADE_MIN_LIQUIDITY", 100.0),
            signal_model=(os.getenv("POLYMARKET_TRAINING_SIGNAL_MODEL") or "research").lower(),
            starting_bankroll=_envf("HTE_STARTING_BALANCE", 500.0),
            universe=ucfg,
        )

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "universe"}
        d["markout_horizons_s"] = list(self.markout_horizons_s)
        d["universe"] = self.universe.as_dict() if self.universe else None
        return d

    @classmethod
    def aggressive_paper(cls, **overrides) -> "TrainingConfig":
        """Explicit AGGRESSIVE paper-training profile (PAPER ONLY).

        Turns on every available *non-live* learning feature and trades more
        often to generate more feedback-loop data:

        * wider market scan + larger shortlist/live-watch/trade-candidate limits,
        * lower minimum net edge + uncertainty (more candidates clear the gate),
        * higher (but still clamped) shrink so fair-value moves off the market,
        * wider spread tolerance + thinner depth floor (more eligible markets),
        * faster scan / subscription refresh / feedback cadence,
        * online learner + recursive feedback loop + Chainlink feature layer on,
        * bounded **controlled exploration** of near-miss candidates at a small
          exploratory size for extra training signal.

        SAFETY: ``mode='paper_train'`` (never live); hard paper risk caps
        (order/market/total notional, daily loss, max-open hard cap) are applied
        by ``__post_init__`` and CANNOT be exceeded by this profile. No live
        flags are touched.
        """
        base = dict(
            mode="paper_train", polymarket_only=True, disable_btc_pulse_trading=True,
            # wide scan + fast refresh
            scan_limit=2000, scan_interval_seconds=15.0, metadata_cache_ttl_s=30.0,
            shortlist_limit=200, live_watch_limit=120, trade_candidate_limit=60,
            subscription_refresh_s=30.0, max_subscription_churn=40,
            # lower edge thresholds -> more paper trades
            min_net_edge=0.005, base_uncertainty=0.005,
            base_shrink_factor=0.45, max_shrink_factor=0.80, min_shrink_factor=0.10,
            max_spread=0.12, min_depth_at_price=25.0,
            # looser eligibility (still filtered)
            min_evidence_score=0.30, min_liquidity=100.0, min_volume=250.0,
            max_time_to_close_days=180.0, min_time_to_close_s=1800.0,
            max_ambiguity_score=0.45,
            # smaller exploratory size; max concurrency at the hard cap
            fixed_notional_usd=3.0, max_open_trades=8, max_open_trades_hard_cap=8,
            # every non-live learning feature ON
            learner_enabled=True, feedback_enabled=True,
            feedback_interval_seconds=60.0, chainlink_enabled=True,
            feature_extraction_enabled=True, grouping_enabled=True,
            exploration_enabled=True, exploration_rate=0.25,
            exploration_notional_usd=2.0, exploration_min_edge=-0.01,
            # higher paper decision budget + feedback target -> more trades/feedback
            paper_decision_budget=120, feedback_sample_target=500,
            tiny_trade_min_liquidity=50.0,
            signal_model="research",
            # aggressive = trade MORE OFTEN, not recklessly: smaller paper sizes,
            # more diversified candidates, TIGHTER per-event/category/bundle hard
            # caps, and an explicit exploration budget.
            max_event_exposure_usd=8.0, max_category_exposure_usd=20.0,
            max_bregman_bundle_exposure_usd=15.0, diversity_target=8,
            exploration_budget_usd=15.0, max_drawdown_usd=40.0,
            kelly_max_fraction=0.03, leg_failure_haircut=0.6,
        )
        base.update(overrides)
        return cls(**base)


def AggressivePaperTrainingConfig(**overrides) -> "TrainingConfig":
    """Factory for the aggressive PAPER-only training profile. See
    :meth:`TrainingConfig.aggressive_paper`."""
    return TrainingConfig.aggressive_paper(**overrides)
