"""BTC 5-minute pulse paper-trading engine (orchestrator).

One ``tick`` (run every few seconds): poll the BTC price, refresh the rolling 5-min
windows, snapshot each window's open price, price each open window as a digital option,
take LOOSENED paper trades, and settle/calibrate closed windows. Writes a status JSON +
paper ledger every tick.

PAPER ONLY: no order client, no wallet, no signing anywhere in this engine.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.markets import PulseMarketFeed, MultiSeriesMarketFeed, SERIES_SLUG_5M, SERIES_SLUG_15M
from engine.pulse.price import PulsePriceFeed, build_price_source
from engine.pulse.fair_value import RollingVol, digital_p_up
from engine.pulse.strategy import decide
from engine.pulse.execution_gate import evaluate_execution
from engine.pulse.executor import PulseLedger
from engine.pulse.decisions import (MarketContext, CandidateDecision, ExecutionCostEstimate,
                                     TradeAction, RejectAction, PaperFill, DecisionResult,
                                     LifecycleReconciler, ttc_bucket, half_life_bucket)
from engine.pulse.reporting import (spread_bucket as _spread_bucket,
                                     depth_bucket as _depth_bucket,
                                     confidence_tier as _confidence_tier)
from engine.pulse.settlement import (PulseCalibration, resolve_window, proxy_outcome)
from engine.pulse.reconciliation import (GateObservations, capture_baseline, empty_baseline)

logger = logging.getLogger("hte.pulse.engine")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _parse_tv_mtf_timeframes(raw) -> tuple[str, ...]:
    from engine.pulse.tradingview import parse_mtf_timeframes
    return parse_mtf_timeframes(raw)


def _tv_mtf_confirm_windows(cfg: "PulseConfig") -> dict[str, float]:
    from engine.pulse.tradingview import build_mtf_confirm_windows
    return build_mtf_confirm_windows(
        cfg.tradingview_mtf_timeframes,
        legacy_5m_s=cfg.tradingview_mtf_confirm_window_s,
        legacy_10m_s=cfg.tradingview_mtf_confirm_window_10m_s,
        legacy_15m_s=cfg.tradingview_mtf_confirm_window_15m_s,
        overrides={
            "2": cfg.tradingview_mtf_confirm_window_2m_s,
            "3": cfg.tradingview_mtf_confirm_window_3m_s,
            "4": cfg.tradingview_mtf_confirm_window_4m_s,
            "13": cfg.tradingview_mtf_confirm_window_13m_s,
        },
    )


@dataclass
class PulseConfig:
    tick_seconds: float = 4.0
    size_usd: float = 5.0
    min_edge: float = 0.03
    min_seconds_to_close: float = 4.0
    min_depth_usd: float = 1.0
    edge_buffer: float = 0.01
    max_price: float = 0.97
    # minimum reward-to-risk for a paper entry: at ask ``p`` a win nets (1-p)/p per $ staked while a
    # loss costs the full stake. 0.0 = off (default). e.g. 0.25 => skip entries priced above ~0.80
    # (which would win < ~$1.25 per $5 risked) so one loss can't wipe ~10 tiny wins. PAPER ONLY.
    min_reward_risk: float = 0.0
    # extra reward/risk floor for UP entries (asymmetric bleed guard; DOWN keeps base only).
    min_reward_risk_up_premium: float = 0.15
    max_open_lag_s: float = 20.0
    max_open_lag_15m_s: float = 240.0
    vol_window_s: float = 900.0
    settle_grace_s: float = 180.0          # prefer authoritative Polymarket(Chainlink) before proxy
    max_positions_kept: int = 500
    fresh_start: bool = False
    # trade-quality / expectancy gates
    min_seconds_since_open: float = 30.0   # skip the dead early window (digital ~0.5 noise)
    min_vol_samples: int = 12              # need a real vol estimate before trusting P(up)
    sigma_trust_floor: float = 2.0e-6      # below this, price is too flat -> digital untrusted
    basis_buffer: float = 0.02             # cover Coinbase-vs-Chainlink resolution basis drift
    # Grok event-risk overlay (advisory; can only make the bot MORE cautious)
    grok_overlay_enabled: bool = False
    grok_overlay_interval_s: float = 180.0
    grok_overlay_max_calls_per_hour: int = 20
    # Grok signal-intelligence layer (OBSERVE-ONLY, off hot path): A = batch analyst over the
    # TradingView signal-learning report; B = per-signal P(up) predictor graded vs realized move.
    # A shared budget caps daily cost + per-feature hourly calls. Neither can trade.
    grok_signal_analyst_enabled: bool = False        # A
    grok_signal_predictor_enabled: bool = False       # B
    grok_analyst_interval_s: float = 1800.0
    grok_budget_daily_usd: float = 50.0
    grok_est_usd_per_call: float = 0.02
    grok_predictor_max_calls_per_hour: int = 90
    grok_analyst_max_calls_per_hour: int = 8
    # ---- Grok DECISION ENGINE ("Grok decides, bot executes"; PAPER ONLY) ----
    # mode: off | shadow (decide+grade only, no trade — safe default) | follow (engine follows Grok
    # direction/size subject to the deterministic floor: execution realism, risk caps, freshness).
    grok_decider_mode: str = "shadow"        # observe-only by default (grades, never affects trading)
    grok_decider_model: str = "grok-4.3"
    grok_decider_timeout_s: float = 12.0
    grok_decider_use_search: bool = False            # enable xAI live web/X news search (slower/$$)
    grok_decider_min_confidence: float = 0.55
    grok_decider_ttl_s: float = 240.0
    grok_decider_max_calls_per_hour: int = 200
    grok_tiered_compute_enabled: bool = True
    grok_tier_full_divergence_min: float = 0.025
    grok_tier_deep_divergence_min: float = 0.04
    grok_decider_follow_fraction: float = 1.0        # A/B canary: fraction of windows to follow
    grok_decider_max_consecutive_losses: int = 4     # breaker: trip after N follow-losses in a row
    grok_decider_daily_loss_cap_usd: float = 30.0    # breaker: trip after this much follow-loss/day
    grok_decider_max_latency_s: float = 20.0         # breaker: trip on sustained high decision latency
    grok_decider_cooldown_s: float = 1800.0          # breaker: stay tripped (use baseline) this long
    # FOLLOW exploration: when Grok ABSTAINS, trade its directional VIEW at this rate (paper data
    # gathering so the bot keeps trading + learns action-level P&L). 0 = never (pure follow).
    grok_decider_explore_rate: float = 0.0
    grok_decider_explore_size_fraction: float = 0.5
    # Minimum |p_up - 0.5| required before an explore trade on Grok's abstain view (blocks coin-flip).
    grok_decider_explore_min_view_margin: float = 0.08
    # minimum P(UP wins) before any Grok-owned UP trade (follow/explore/adaptive/mispricing).
    grok_up_min_p_win: float = 0.58
    # adaptive self-improvement loop: auto-EXPLOIT contexts with a proven view-edge (Wilson lower >
    # 0.5), AVOID proven-losing contexts, and only EXPLORE the uncertain ones. Default ON.
    grok_decider_adaptive: bool = True
    # ---- #1 maker-checker VERIFIER (independent Claude model) + #4 research meta-loop ----
    verifier_enabled: bool = True          # maker-checker ON for paper by default (needs ANTHROPIC key)
    verifier_fail_open: bool = True          # no verdict in time -> approve (don't freeze) but log
    # FOLLOW trades wait for the actual Claude verdict (fail-CLOSED on pending) so the maker-checker
    # genuinely gates them rather than fail-opening before the async worker finishes.
    verifier_follow_require_verdict: bool = True
    verifier_max_calls_per_hour: int = 120
    research_loop_enabled: bool = False
    research_interval_s: float = 1800.0      # idle FLOOR; the loop is mainly EVENT-triggered
    research_event_min_gap_s: float = 600.0  # min gap between event-triggered research runs
    research_auto_apply: bool = False        # WS2: default observe-only; avoid blocks only when on
    research_forbid_size_increase: bool = True  # WS2: research_meta must never bump directional size
    research_avoid_max: int = 14             # cap on active research avoid-context rules
    research_exploit_max: int = 10           # cap on active research EXPLOIT-context rules
    lessons_revalidate_ttl_s: float = 21600.0  # avoid/exploit lesson retracts if unconfirmed this long
    research_exploit_size_mult: float = 1.5  # size-up multiplier for proven-winning exploit contexts
    research_max_calls_per_hour: int = 6
    claude_budget_daily_usd: float = 10.0
    claude_est_usd_per_call: float = 0.01
    grok_news_refresh_s: float = 300.0               # periodic web/X news digest cadence
    # price feed: 'auto' uses Chainlink Data Streams (exact resolution feed) when creds are
    # set, else the Coinbase proxy. A sub-second background sampler keeps the price fresh
    # between the slower trade ticks.
    price_source: str = "auto"
    price_sampler_interval_s: float = 1.0
    # ---- oracle reference model (Chainlink Data Streams ref price via Polymarket RTDS) ----
    oracle_feed_type: str = "chainlink_data_streams_refprice"
    oracle_symbol: str = "btc/usd"
    fast_feeds: tuple = ("binance_btcusdt", "coinbase_btcusd")
    settlement_source_priority: tuple = ("polymarket_resolution", "rtds_chainlink_proxy")
    proxy_max_close_lag_s: float = 30.0
    rtds_enabled: bool = True
    rtds_max_age_s: float = 45.0             # RTDS oracle tick older than this -> feed gets None (stale)
    price_max_age_s: float = 60.0            # abstain ("stale_price") if the price feed is older than this
    # strict execution-quality gate (orderbook-reality EV after VWAP/slippage)
    exec_max_spread: float = 0.06
    exec_min_order_usd: float = 1.0
    exec_max_depth_consume_frac: float = 0.5
    exec_min_ev_after_slippage: float = 0.02   # require a real calibrated edge buffer (per-share)
    # don't BUY the underdog side (VWAP fill below this) on opinion paths — the price is the best
    # probability and the bot's model has negative edge on cheap/tail sides (live: underdog buys
    # ~28% win = the entire net loss; favourites >0.5 were net-positive). Proven edges are exempt.
    min_entry_price: float = 0.50
    exec_max_book_age_s: float = 30.0        # reject stale orderbook older than this
    research_features_enabled: bool = True   # OBSERVE-ONLY EP Chan features (never trade)
    # OBSERVE-ONLY BTC Pulse Edge Signal layer (CEX basket momentum + stale-price divergence +
    # orderbook pressure + pulse_edge_score). Never trades/vetoes/bypasses the gate.
    edge_signal_enabled: bool = True
    edge_extra_cex_enabled: bool = False     # add Kraken+Bitstamp (extra REST; opt-in for hot path)
    edge_promotion_allowed: bool = False
    edge_promotion_min_samples: int = 50
    edge_promotion_min_win_rate: float = 0.80
    # ---- CEX-lead latency edge (grades CEX-implied P(up) vs the MARKET price; PAPER ONLY) ----
    # mode: "shadow" grades only; "gated" may PROPOSE a side on a Wilson-proven bucket (still
    # subject to the deterministic safety floor + execution gate). Default shadow = never trades.
    cex_lead_enabled: bool = True
    cex_lead_mode: str = "shadow"
    cex_lead_min_samples: int = 60
    cex_lead_min_divergence: float = 0.04
    cex_lead_confidence_z: float = 1.64
    cex_lead_min_edge_vs_market: float = 0.0   # required Brier improvement over the market
    cex_lead_tv_strength_thr: float = 0.5      # TradingView strength to count as TV-confirmed
    cex_lead_decisive_thr: float = 0.35        # |cex_p_up-0.5| >= this => late-window move ~decided
    cex_lead_late_ttc_s: float = 90.0          # ttc <= this => late-window convergence-lag zone
    cex_lead_kelly_scale: float = 0.5          # fractional-Kelly size for proven edges
    cex_lead_max_size_frac: float = 2.0        # hard cap on the edge-scaled size multiplier
    # ---- Grok-follow mispricing gate (restrict-only; CEX-lead + edge/TTC alignment) ----
    mispricing_gate_enabled: bool = False
    mispricing_ttc_min_s: float = 180.0
    mispricing_ttc_max_s: float = 240.0
    mispricing_require_confirmed: bool = True
    mispricing_require_stale_down: bool = True
    mispricing_min_executable_margin: float = 0.03
    edge_ttc_gate_enabled: bool = False
    # Tier-1 baseline cohort gate: trade only proven shadow buckets on the quant path.
    baseline_cohort_gate_enabled: bool = True
    baseline_cohort_ttc_min_s: float = 180.0
    baseline_cohort_ttc_max_s: float = 240.0
    baseline_cohort_require_high_edge: bool = True
    baseline_cohort_require_strong_cex: bool = True
    baseline_up_tv_gate_enabled: bool = True
    baseline_down_tv_gate_enabled: bool = True
    baseline_down_block_bullish_range: bool = True
    baseline_down_block_up_strong_bullish: bool = True
    baseline_down_block_volume_active: bool = True
    baseline_down_block_up_strong_range_top: bool = True
    baseline_down_block_bullish_mtf: bool = True
    baseline_down_block_not_stale: bool = True
    baseline_down_block_mid_entry: bool = True
    baseline_down_mid_entry_min: float = 0.55
    baseline_down_mid_entry_max: float = 0.60
    baseline_down_block_single_tf: bool = True
    baseline_down_block_medium_edge: bool = True
    baseline_down_block_bb_expansion_down: bool = True
    # 15m fast lane: scaled TTC band on 15m windows (proven 160-220s cohort → 480-660s).
    baseline_cohort_15m_fast_lane: bool = True
    baseline_cohort_15m_ttc_min_s: float = 160.0
    baseline_cohort_15m_ttc_max_s: float = 220.0
    # 15m DOWN baseline: cohort + MTF only; skip redundant opinion gates (context/tv/down-bias/late).
    green_path_enabled: bool = False
    # When Grok abstains, still follow a Wilson-aligned CEX-lead mispricing stack (not coin-flip explore).
    mispricing_follow_on_abstain: bool = False
    mispricing_follow_size_fraction: float = 0.5
    # ---- within-window RISK-FREE arbitrage (Roan dutch book up_vwap+down_vwap<1; PAPER ONLY) ----
    arbitrage_enabled: bool = True
    arb_fees: float = 0.0                       # modelled taker fee per $ (Polymarket BTC ~0)
    arb_epsilon: float = 0.05                   # min risk-free edge below $1 (Bible execution-risk
    #                                             floor; must cover fees + non-atomic slippage buffer).
    arb_epsilon_5m: float = 0.05                # per-series override (defaults to arb_epsilon)
    arb_epsilon_15m: float = 0.03               # 15m often thinner — lower bar captures near-misses
    arb_min_profit_usd: float = 0.0
    arb_size_usd: float = 100.0                 # fallback target when book depth is unknown
    arb_max_usd: float = 300.0                  # SIZE-TO-DEPTH ceiling: take the full available depth
    arb_global_max_open_usd: float = 600.0      # max total open arb exposure (all windows)
    arb_nonatomic_enabled: bool = True          # sequential leg-fill stress test before book
    arb_nonatomic_slippage_bps: float = 50.0    # adverse leg-2 slippage buffer (WS5)
    #                                             (capped at max_depth_consume_frac of the thinner leg
    #                                             + full-fill required, so still RISK-FREE) up to this
    # ---- directional de-risk (separate strategy; arb can run standalone) ----
    directional_enabled: bool = True            # PULSE_DIRECTIONAL_ENABLED
    # default OFF in code (backward-compatible); enabled via env on the live bot. When on, a
    # directional trade is allowed ONLY in a Wilson-proven-winning bucket (pre-execution block).
    directional_require_winning_bucket: bool = False
    directional_winning_min_samples: int = 30
    # cold-start carve-out: the allowlist would otherwise block EVERY directional trade until a
    # bucket is Wilson-proven-winning, but proving needs trades -> deadlock (bot looks frozen).
    # Allow this capped fraction of otherwise-eligible candidates through as exploration so the bot
    # keeps learning and can DISCOVER winning buckets. 0 = strict block-all; 1 = effectively off.
    directional_explore_rate: float = 0.05
    directional_max_bankroll_frac: float = 0.10   # cap directional open exposure vs starting capital
    directional_down_only: bool = True            # hard block ALL directional UP (no bypass)
    directional_block_up_until_promoted: bool = True  # hard block UP until direction=up promoted
    directional_up_restrictions_enabled: bool = True  # UP-only extra gates (TV/down-bias/RR premium)
    directional_series_slugs: tuple = ()        # empty = all series; else directional only on these
    primary_edge_source: str = "arbitrage"      # report field: arbitrage | directional | none
    dependency_arb_enabled: bool = True         # LCMM nested-window scanner
    dependency_arb_execute_enabled: bool = False  # paper execute validated violations (WS4)
    dependency_arb_max_usd: float = 50.0
    dependency_arb_epsilon: float = 0.02        # LCMM violation floor (separate from dutch-book eps)
    bregman_projection_enabled: bool = False  # WS4 Layer 2 diagnostics
    bregman_trade_authority: bool = False     # Bregman sizes Lane B when True
    bregman_alpha: float = 0.9
    bregman_epsilon_init: float = 0.1
    bregman_fw_max_iters: int = 50
    bregman_fw_time_budget_ms: float = 500.0
    ip_oracle_backend: str = "ortools"
    clob_websocket_enabled: bool = True
    stop_min_sharpe: float = 0.0
    stop_sharpe_min_samples: int = 20
    grok_dependency_enabled: bool = False       # advisory dependency screener (shadow)
    grok_dependency_interval_s: float = 180.0
    eth_series_enabled: bool = False            # append ETH 5m/15m slugs when listed
    sizing_promotion_gated: bool = True       # Kelly only on promoted buckets (WS3)
    # ---- Learned Selectivity Gate v1 (between decision and execution; PAPER ONLY) ----
    # Uses live settled-trade bucket evidence to REJECT proven-losing buckets. Can only make the
    # bot MORE selective; never trades/resizes/bypasses the execution gate.
    selectivity_gate_enabled: bool = True
    selectivity_min_samples: int = 50
    selectivity_min_win_rate: float = 0.52
    selectivity_min_profit_factor: float = 0.85
    selectivity_fdr_q: float = 0.10
    selectivity_confidence_z: float = 1.64   # one-sided z for "confidently below breakeven" test
    selectivity_exploration_rate: float = 0.05
    calibration_min_samples: int = 30
    calibration_max_shrink: float = 0.5
    # ---- TradingView Context Gate (hard prior, restrict-only; PAPER ONLY) ----
    # Blocks proven-losing entry contexts (TradingView volume spikes, the noise hurst regime, and
    # entries too far from resolution) IMMEDIATELY — before the learned selectivity gate has enough
    # samples. Can only make the bot MORE selective; never trades/resizes/bypasses the execution
    # gate. Default OFF (no behavior change); enabled per-deployment via env.
    tv_context_gate_enabled: bool = False
    tv_context_blocked_volume_states: tuple = ("spike",)
    tv_context_blocked_hurst_regimes: tuple = ("noise",)
    tv_context_max_ttc_s: float = 240.0
    tv_context_block_liquidation_spike: bool = True
    tv_context_block_event_blackout: bool = True
    tv_context_block_grok_event_risk_high: bool = True
    tv_context_exploration_rate: float = 0.0
    # ---- TradingView DOWN-bias gate (Townhall P3; restrict-only) ----
    tv_down_bias_gate_enabled: bool = False
    tv_down_bias_exploration_rate: float = 0.0
    tv_down_bias_block_up_on_bearish_down_stack: bool = True
    tv_down_bias_block_up_tv_down_non_bearish: bool = True
    tv_down_bias_block_up_against_confirmed_down: bool = True
    tv_down_bias_block_mixed_mtf_up: bool = True
    tv_down_bias_block_bullish_supertrend_up: bool = True
    tv_down_bias_block_up_vwap_above: bool = True
    tv_down_bias_block_up_bb_expansion_up: bool = True
    tv_down_bias_block_up_range_breakout_down: bool = True
    tv_down_bias_block_up_range_top: bool = True
    tv_down_bias_block_up_bb_squeeze: bool = True
    tv_down_bias_block_up_markov_chop_noise: bool = True
    tv_down_bias_block_up_htf_bullish: bool = True
    tv_down_bias_block_up_bear_close_near_low: bool = True
    tv_down_bias_block_up_medium_edge: bool = True
    tv_down_bias_block_up_weak_cex: bool = True
    tv_down_bias_block_up_late_ttc: bool = True
    tv_down_bias_block_up_early_ttc: bool = True
    tv_down_bias_block_up_ask_heavy_ob: bool = True
    tv_down_bias_block_up_tf_confirm_conflict: bool = True
    tv_down_bias_block_up_cvd_neutral: bool = True
    tv_down_bias_block_up_cvd_buy_pressure: bool = True
    tv_down_bias_block_up_low_conviction: bool = True
    tv_down_bias_block_up_bearish_mtf_tv_up: bool = True
    tv_down_bias_block_up_mid_ttc: bool = True
    tv_down_bias_block_up_neutral_zscore: bool = True
    tv_down_bias_block_up_medium_confidence: bool = True
    tv_down_bias_block_up_not_stale: bool = True
    tv_down_bias_block_up_volume_active: bool = True
    tv_down_bias_block_up_underdog_entry: bool = True
    tv_down_bias_up_underdog_entry_max: float = 0.55
    tv_down_bias_up_late_ttc_min_s: float = 240.0
    tv_down_bias_up_early_ttc_max_s: float = 120.0
    tv_down_bias_up_mid_ttc_min_s: float = 120.0
    tv_down_bias_up_mid_ttc_max_s: float = 180.0
    tv_down_bias_up_min_conviction: float = 0.40
    tv_mtf_conflict_gate_enabled: bool = True
    tv_mtf_require_confirm: bool = False   # loop arch: conflict veto only, not MTF trade authority
    tv_mtf_require_all_confirm: bool = False  # require all MTF TFs (e.g. 2/3/4) agree on direction
    tv_mtf_require_side_align: bool = False
    tv_mtf_conflict_exploration_rate: float = 0.0
    # ---- verifiable stop conditions (agent-independent kill switches; Loop Eng #6) ----
    stop_enabled: bool = True
    stop_rolling_n: int = 50
    stop_min_samples: int = 30
    stop_min_profit_factor: float = 0.85
    stop_max_drawdown_pct: float = 25.0
    # ---- Late-window high-conviction entry mode (time-decay edge; PAPER ONLY) ----
    # When enabled, only late-window AND high-conviction setups may trade (restrict-only). The edge
    # is ALWAYS measured observe-only (cohort vs other) so it can be graded before being enabled.
    late_window_entry_enabled: bool = False
    late_window_max_ttc_s: float = 120.0
    late_window_min_conviction: float = 0.40
    signal_engine_enabled: bool = True       # OBSERVE-ONLY Simons-style raw signals (never trade)
    factor_model_enabled: bool = True        # OBSERVE-ONLY BTC-pulse factor/context model
    markov_enabled: bool = True              # OBSERVE-ONLY Markov regime machine
    edge_model_enabled: bool = True          # OBSERVE-ONLY calibrated edge model (no authority)
    # ---- closed-loop learning: blend the calibrated edge model into the DIRECTIONAL decision ----
    # The bot's own settled-trade experience (online logistic edge model) adjusts P(up) used by
    # decide(). Influence is EARNED (ramps with sample count), GATED (only when calibrated), and
    # SELF-DISABLING (drops to 0 if calibration error exceeds the cap). The strict execution gate,
    # paper-realism, and ledger reconciliation are UNTOUCHED — learning can never bypass them, and
    # this is PAPER ONLY. Default OFF (no behavior change); enabled per-deployment via env.
    learning_enabled: bool = False
    learning_min_samples: int = 60           # min settled labels before any influence
    learning_max_weight: float = 0.5         # cap on the model's weight in the blend (<=0.5)
    learning_ramp_samples: float = 300.0     # labels over which weight ramps 0 -> max
    learning_max_calib_error: float = 0.15   # disable influence if ECE worse than this
    learning_bench_min_samples: int = 20     # graded windows before the market-beating gate applies
    learning_bench_margin: float = 0.0       # model Brier must beat market Brier by >= this to blend
    sizing_enabled: bool = False             # paper Kelly sizing: default OFF (size unchanged)
    sizing_hard_cap_usd: float = 10.0
    sizing_daily_loss_cap_usd: float = 50.0
    sizing_bankroll_usd: float = 1000.0
    # notional starting capital for the on-hand-capital display (paper). on_hand = start + realized.
    starting_capital_usd: float = 500.0
    # ---- TradingView indicator webhook intake (OBSERVE-ONLY external signal) ----
    # Enabled only when a shared secret is set. Bound to 127.0.0.1 by default (private to host);
    # alerts are candidate signals only — they can never place/resize/bypass a paper trade.
    tradingview_secret: str = ""
    tradingview_allowed_symbols: tuple = ("BTCUSD", "INDEX:BTCUSD", "BTC/USD", "BTC", "XBTUSD")
    tradingview_bot_name: str = "hermes"
    tradingview_webhook_host: str = "127.0.0.1"
    tradingview_webhook_port: int = 8787
    tradingview_webhook_path: str = "/webhooks/tradingview"
    tradingview_max_age_s: float = 90.0
    tradingview_feature_symbol: str = "BTCUSD"   # TV INDEX:BTCUSD — 5m/10m/15m MTF
    tradingview_mtf_timeframes: tuple = ("2", "3", "4")
    tradingview_mtf_confirm_window_s: float = 360.0
    tradingview_mtf_confirm_window_10m_s: float = 660.0
    tradingview_mtf_confirm_window_15m_s: float = 960.0
    tradingview_mtf_confirm_window_2m_s: float = 300.0
    tradingview_mtf_confirm_window_3m_s: float = 450.0
    tradingview_mtf_confirm_window_4m_s: float = 600.0
    tradingview_mtf_confirm_window_13m_s: float = 840.0
    # Polymarket series to trade (default 15m only; set PULSE_SERIES_SLUGS for multi-series).
    pulse_series_slugs: tuple = (SERIES_SLUG_15M,)
    tradingview_signal_max_feature_age_s: float = 300.0   # only attach signals fresher than this
    # TradingView as the DIRECTIONAL INDICATION SIGNAL (restrict-only): when on, a paper trade is
    # only taken if a FRESH TradingView signal exists and its direction matches the trade side. It
    # can only PREVENT trades (never force one or bypass the execution gate). Default OFF.
    tradingview_signal_gate_enabled: bool = False
    tradingview_min_signal_strength: float = 0.0   # 0=off; e.g. 0.72 blocks WEAK, keeps STRONG
    # TV confidence tier: observe-only min_edge/max_price modulation at 15m sweet spot (not a gate).
    tv_confidence_tier_enabled: bool = True
    tv_tier_require_sweet_spot: bool = True
    tv_tier_15m_only: bool = True
    tv_tier_aligned_strength_min: float = 0.72
    tv_tier_a_min_edge_delta: float = -0.005
    tv_tier_a_max_price_delta: float = 0.02
    tv_tier_c_min_edge_delta: float = 0.005
    tv_tier_c_max_price_delta: float = -0.03
    # forward-return horizon (s): for EVERY TradingView signal, the bot snapshots the oracle BTC
    # price and re-checks it this many seconds later to learn whether the signal predicted the
    # move — building a prediction from the history of ALL signals (traded or not). Observe-only.
    tradingview_signal_horizon_s: float = 300.0
    # TradingView signal-bucket PROMOTION diagnostics (observe-only by default). A bucket is only
    # flagged eligible if win_rate >= min_win_rate, EV-after-slippage > 0, clean reconciliation,
    # and >= min_samples. Promotion to trading authority requires this flag AND explicit wiring.
    tradingview_promotion_allowed: bool = False
    tradingview_promotion_min_samples: int = 50
    tradingview_promotion_min_win_rate: float = 0.80
    data_dir: str = "/data"

    @classmethod
    def from_env(cls) -> "PulseConfig":
        from engine.pulse.tradingview import normalize_symbol
        from engine.pulse.markets import SERIES_SLUG_ETH_5M, SERIES_SLUG_ETH_15M
        _series_slugs = tuple(
            s.strip() for s in os.getenv(
                "PULSE_SERIES_SLUGS",
                "btc-up-or-down-15m").split(",") if s.strip())
        _dir_series = tuple(
            s.strip() for s in os.getenv(
                "PULSE_DIRECTIONAL_SERIES_SLUGS",
                "btc-up-or-down-15m").split(",") if s.strip())
        _arb_eps = _envf("PULSE_ARB_EPSILON", 0.05)
        if str(os.getenv("PULSE_ETH_SERIES_ENABLED", "0")).strip().lower() in (
                "1", "true", "yes", "on"):
            _series_slugs = tuple(dict.fromkeys(
                _series_slugs + (SERIES_SLUG_ETH_5M, SERIES_SLUG_ETH_15M)))
        return cls(
            tick_seconds=_envf("PULSE_TICK_SECONDS", 4.0),
            size_usd=_envf("PULSE_SIZE_USD", 5.0),
            min_edge=_envf("PULSE_MIN_EDGE", 0.03),
            min_seconds_to_close=_envf("PULSE_MIN_SECONDS_TO_CLOSE", 4.0),
            min_depth_usd=_envf("PULSE_MIN_DEPTH_USD", 1.0),
            edge_buffer=_envf("PULSE_EDGE_BUFFER", 0.01),
            max_price=_envf("PULSE_MAX_PRICE", 0.97),
            min_reward_risk=_envf("PULSE_MIN_REWARD_RISK", 0.0),
            min_reward_risk_up_premium=_envf("PULSE_MIN_REWARD_RISK_UP_PREMIUM", 0.15),
            max_open_lag_s=_envf("PULSE_MAX_OPEN_LAG_S", 20.0),
            max_open_lag_15m_s=_envf("PULSE_MAX_OPEN_LAG_15M_S", 240.0),
            vol_window_s=_envf("PULSE_VOL_WINDOW_S", 900.0),
            settle_grace_s=_envf("PULSE_SETTLE_GRACE_S", 180.0),
            fresh_start=str(os.getenv("PULSE_FRESH_START", "")).strip().lower()
            in ("1", "true", "yes", "on"),
            min_seconds_since_open=_envf("PULSE_MIN_SECONDS_SINCE_OPEN", 30.0),
            min_vol_samples=int(_envf("PULSE_MIN_VOL_SAMPLES", 12)),
            sigma_trust_floor=_envf("PULSE_SIGMA_TRUST_FLOOR", 2.0e-6),
            basis_buffer=_envf("PULSE_BASIS_BUFFER", 0.02),
            grok_overlay_enabled=str(os.getenv("GROK_OVERLAY_ENABLED", "")).strip().lower()
            in ("1", "true", "yes", "on"),
            grok_overlay_interval_s=_envf("GROK_OVERLAY_INTERVAL_S", 180.0),
            grok_overlay_max_calls_per_hour=int(_envf("GROK_OVERLAY_MAX_CALLS_PER_HOUR", 20)),
            grok_signal_analyst_enabled=str(os.getenv("GROK_SIGNAL_ANALYST_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_signal_predictor_enabled=str(os.getenv("GROK_SIGNAL_PREDICTOR_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_analyst_interval_s=_envf("GROK_ANALYST_INTERVAL_S", 1800.0),
            grok_budget_daily_usd=_envf("GROK_BUDGET_DAILY_USD", 50.0),
            grok_est_usd_per_call=_envf("GROK_EST_USD_PER_CALL", 0.02),
            grok_predictor_max_calls_per_hour=int(_envf("GROK_PREDICTOR_MAX_CALLS_PER_HOUR", 90)),
            grok_analyst_max_calls_per_hour=int(_envf("GROK_ANALYST_MAX_CALLS_PER_HOUR", 8)),
            grok_decider_mode=(os.getenv("PULSE_GROK_DECIDER_MODE", "shadow") or "shadow").strip().lower(),
            grok_decider_model=(os.getenv("PULSE_GROK_DECIDER_MODEL", "grok-4.3")
                                or "grok-4.3").strip(),
            grok_decider_timeout_s=_envf("PULSE_GROK_DECIDER_TIMEOUT_S", 12.0),
            grok_decider_use_search=str(os.getenv("PULSE_GROK_DECIDER_USE_SEARCH", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_decider_min_confidence=_envf("PULSE_GROK_DECIDER_MIN_CONFIDENCE", 0.55),
            grok_decider_ttl_s=_envf("PULSE_GROK_DECIDER_TTL_S", 240.0),
            grok_decider_max_calls_per_hour=int(_envf("PULSE_GROK_DECIDER_MAX_CALLS_PER_HOUR", 200)),
            grok_tiered_compute_enabled=str(os.getenv("PULSE_GROK_TIERED_COMPUTE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_tier_full_divergence_min=_envf("PULSE_GROK_TIER_FULL_DIVERGENCE_MIN", 0.025),
            grok_tier_deep_divergence_min=_envf("PULSE_GROK_TIER_DEEP_DIVERGENCE_MIN", 0.04),
            grok_decider_follow_fraction=_envf("PULSE_GROK_DECIDER_FOLLOW_FRACTION", 1.0),
            grok_decider_max_consecutive_losses=int(
                _envf("PULSE_GROK_DECIDER_MAX_CONSECUTIVE_LOSSES", 4)),
            grok_decider_daily_loss_cap_usd=_envf("PULSE_GROK_DECIDER_DAILY_LOSS_CAP_USD", 30.0),
            grok_decider_max_latency_s=_envf("PULSE_GROK_DECIDER_MAX_LATENCY_S", 20.0),
            grok_decider_cooldown_s=_envf("PULSE_GROK_DECIDER_COOLDOWN_S", 1800.0),
            grok_decider_explore_rate=_envf("PULSE_GROK_DECIDER_EXPLORE_RATE", 0.0),
            grok_decider_explore_size_fraction=_envf("PULSE_GROK_DECIDER_EXPLORE_SIZE_FRACTION", 0.5),
            grok_decider_explore_min_view_margin=_envf(
                "PULSE_GROK_DECIDER_EXPLORE_MIN_VIEW_MARGIN", 0.08),
            grok_up_min_p_win=_envf("PULSE_GROK_UP_MIN_P_WIN", 0.58),
            grok_decider_adaptive=str(os.getenv("PULSE_GROK_DECIDER_ADAPTIVE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_enabled=str(os.getenv("PULSE_VERIFIER_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_fail_open=str(os.getenv("PULSE_VERIFIER_FAIL_OPEN", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_follow_require_verdict=str(os.getenv("PULSE_VERIFIER_FOLLOW_REQUIRE_VERDICT", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            verifier_max_calls_per_hour=int(_envf("PULSE_VERIFIER_MAX_CALLS_PER_HOUR", 120)),
            research_loop_enabled=str(os.getenv("PULSE_RESEARCH_LOOP_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            research_interval_s=_envf("PULSE_RESEARCH_INTERVAL_S", 1800.0),
            research_event_min_gap_s=_envf("PULSE_RESEARCH_EVENT_MIN_GAP_S", 600.0),
            research_avoid_max=int(_envf("PULSE_RESEARCH_AVOID_MAX", 14)),
            research_exploit_max=int(_envf("PULSE_RESEARCH_EXPLOIT_MAX", 10)),
            lessons_revalidate_ttl_s=_envf("PULSE_LESSONS_REVALIDATE_TTL_S", 21600.0),
            research_exploit_size_mult=_envf("PULSE_RESEARCH_EXPLOIT_SIZE_MULT", 1.5),
            research_auto_apply=str(os.getenv("PULSE_RESEARCH_AUTO_APPLY", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            research_forbid_size_increase=str(
                os.getenv("PULSE_RESEARCH_FORBID_SIZE_INCREASE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            research_max_calls_per_hour=int(_envf("PULSE_RESEARCH_MAX_CALLS_PER_HOUR", 6)),
            claude_budget_daily_usd=_envf("CLAUDE_BUDGET_DAILY_USD", 10.0),
            claude_est_usd_per_call=_envf("CLAUDE_EST_USD_PER_CALL", 0.01),
            grok_news_refresh_s=_envf("PULSE_GROK_NEWS_REFRESH_S", 300.0),
            price_source=(os.getenv("PULSE_PRICE_SOURCE", "auto") or "auto").strip().lower(),
            price_sampler_interval_s=_envf("PULSE_PRICE_SAMPLER_INTERVAL_S", 1.0),
            oracle_feed_type=(os.getenv("HERMES_ORACLE_FEED_TYPE",
                                        "chainlink_data_streams_refprice") or "").strip().lower(),
            oracle_symbol=(os.getenv("HERMES_ORACLE_SYMBOL", "btc/usd") or "btc/usd").strip().lower(),
            fast_feeds=tuple(s.strip().lower() for s in os.getenv(
                "HERMES_FAST_FEEDS", "binance_btcusdt,coinbase_btcusd").split(",") if s.strip()),
            settlement_source_priority=tuple(s.strip().lower() for s in os.getenv(
                "HERMES_SETTLEMENT_SOURCE_PRIORITY",
                "polymarket_resolution,rtds_chainlink_proxy").split(",") if s.strip()),
            proxy_max_close_lag_s=_envf("HERMES_PROXY_MAX_CLOSE_LAG_S", 30.0),
            rtds_max_age_s=_envf("PULSE_RTDS_MAX_AGE_S", 45.0),
            price_max_age_s=_envf("PULSE_PRICE_MAX_AGE_S", 60.0),
            rtds_enabled=str(os.getenv("HERMES_RTDS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            exec_max_spread=_envf("PULSE_EXEC_MAX_SPREAD", 0.06),
            exec_min_order_usd=_envf("PULSE_EXEC_MIN_ORDER_USD", 1.0),
            exec_max_depth_consume_frac=_envf("PULSE_EXEC_MAX_DEPTH_CONSUME_FRAC", 0.5),
            exec_min_ev_after_slippage=_envf("PULSE_EXEC_MIN_EV", 0.02),
            min_entry_price=_envf("PULSE_MIN_ENTRY_PRICE", 0.50),
            exec_max_book_age_s=_envf("PULSE_EXEC_MAX_BOOK_AGE_S", 30.0),
            research_features_enabled=str(os.getenv("HERMES_RESEARCH_FEATURES_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_signal_enabled=str(os.getenv("HERMES_EDGE_SIGNAL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_extra_cex_enabled=str(os.getenv("HERMES_EDGE_EXTRA_CEX_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_promotion_allowed=str(os.getenv("HERMES_EDGE_PROMOTION_ALLOWED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_promotion_min_samples=int(_envf("HERMES_EDGE_PROMOTION_MIN_SAMPLES", 50)),
            edge_promotion_min_win_rate=_envf("HERMES_EDGE_PROMOTION_MIN_WIN_RATE", 0.80),
            cex_lead_enabled=str(os.getenv("PULSE_CEX_LEAD_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            cex_lead_mode=str(os.getenv("PULSE_CEX_LEAD_MODE", "shadow")).strip().lower(),
            cex_lead_min_samples=int(_envf("PULSE_CEX_LEAD_MIN_SAMPLES", 60)),
            cex_lead_min_divergence=_envf("PULSE_CEX_LEAD_MIN_DIVERGENCE", 0.04),
            cex_lead_confidence_z=_envf("PULSE_CEX_LEAD_CONFIDENCE_Z", 1.64),
            cex_lead_min_edge_vs_market=_envf("PULSE_CEX_LEAD_MIN_EDGE_VS_MARKET", 0.0),
            cex_lead_tv_strength_thr=_envf("PULSE_CEX_LEAD_TV_STRENGTH_THR", 0.5),
            cex_lead_decisive_thr=_envf("PULSE_CEX_LEAD_DECISIVE_THR", 0.35),
            cex_lead_late_ttc_s=_envf("PULSE_CEX_LEAD_LATE_TTC_S", 90.0),
            cex_lead_kelly_scale=_envf("PULSE_CEX_LEAD_KELLY_SCALE", 0.5),
            cex_lead_max_size_frac=_envf("PULSE_CEX_LEAD_MAX_SIZE_FRAC", 2.0),
            mispricing_gate_enabled=str(os.getenv("PULSE_MISPRICING_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            mispricing_ttc_min_s=_envf("PULSE_MISPRICING_TTC_MIN_S", 180.0),
            mispricing_ttc_max_s=_envf("PULSE_MISPRICING_TTC_MAX_S", 240.0),
            mispricing_require_confirmed=str(
                os.getenv("PULSE_MISPRICING_REQUIRE_CONFIRMED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_require_stale_down=str(
                os.getenv("PULSE_MISPRICING_REQUIRE_STALE_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_min_executable_margin=_envf(
                "PULSE_MISPRICING_MIN_EXECUTABLE_MARGIN", 0.03),
            edge_ttc_gate_enabled=str(os.getenv("PULSE_EDGE_TTC_GATE_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            baseline_cohort_gate_enabled=str(
                os.getenv("PULSE_BASELINE_COHORT_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_ttc_min_s=_envf("PULSE_BASELINE_COHORT_TTC_MIN_S", 180.0),
            baseline_cohort_ttc_max_s=_envf("PULSE_BASELINE_COHORT_TTC_MAX_S", 240.0),
            baseline_cohort_require_high_edge=str(
                os.getenv("PULSE_BASELINE_COHORT_REQUIRE_HIGH_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_require_strong_cex=str(
                os.getenv("PULSE_BASELINE_COHORT_REQUIRE_STRONG_CEX", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_up_tv_gate_enabled=str(
                os.getenv("PULSE_BASELINE_UP_TV_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_tv_gate_enabled=str(
                os.getenv("PULSE_BASELINE_DOWN_TV_GATE_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bullish_range=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BULLISH_RANGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_up_strong_bullish=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_BULLISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_volume_active=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_VOLUME_ACTIVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_up_strong_range_top=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_UP_STRONG_RANGE_TOP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bullish_mtf=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BULLISH_MTF", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_not_stale=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_NOT_STALE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_mid_entry=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_MID_ENTRY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_mid_entry_min=_envf("PULSE_BASELINE_DOWN_MID_ENTRY_MIN", 0.55),
            baseline_down_mid_entry_max=_envf("PULSE_BASELINE_DOWN_MID_ENTRY_MAX", 0.60),
            baseline_down_block_single_tf=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_SINGLE_TF", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_medium_edge=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_MEDIUM_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_down_block_bb_expansion_down=str(
                os.getenv("PULSE_BASELINE_DOWN_BLOCK_BB_EXPANSION_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_15m_fast_lane=str(
                os.getenv("PULSE_BASELINE_COHORT_15M_FAST_LANE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            baseline_cohort_15m_ttc_min_s=_envf("PULSE_BASELINE_COHORT_15M_TTC_MIN_S", 160.0),
            baseline_cohort_15m_ttc_max_s=_envf("PULSE_BASELINE_COHORT_15M_TTC_MAX_S", 220.0),
            green_path_enabled=str(os.getenv("PULSE_GREEN_PATH_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            mispricing_follow_on_abstain=str(
                os.getenv("PULSE_MISPRICING_FOLLOW_ON_ABSTAIN", "0")).strip().lower()
            in ("1", "true", "yes", "on"),
            mispricing_follow_size_fraction=_envf("PULSE_MISPRICING_FOLLOW_SIZE_FRACTION", 0.5),
            arbitrage_enabled=str(os.getenv("PULSE_ARB_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            arb_fees=_envf("PULSE_ARB_FEES", 0.0),
            arb_epsilon=_arb_eps,
            arb_epsilon_5m=_envf("PULSE_ARB_EPSILON_5M", _arb_eps),
            arb_epsilon_15m=_envf("PULSE_ARB_EPSILON_15M", min(_arb_eps, 0.03)),
            arb_min_profit_usd=_envf("PULSE_ARB_MIN_PROFIT_USD", 0.0),
            arb_size_usd=_envf("PULSE_ARB_SIZE_USD", 100.0),
            arb_max_usd=_envf("PULSE_ARB_MAX_USD", 300.0),
            arb_global_max_open_usd=_envf("PULSE_ARB_GLOBAL_MAX_OPEN_USD", 600.0),
            arb_nonatomic_enabled=str(os.getenv("PULSE_ARB_NONATOMIC_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            arb_nonatomic_slippage_bps=_envf("PULSE_ARB_NONATOMIC_SLIPPAGE_BPS", 50.0),
            directional_enabled=str(os.getenv("PULSE_DIRECTIONAL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            directional_require_winning_bucket=str(os.getenv("PULSE_DIRECTIONAL_REQUIRE_WINNING", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            directional_winning_min_samples=int(_envf("PULSE_DIRECTIONAL_WINNING_MIN_SAMPLES", 30)),
            directional_explore_rate=_envf("PULSE_DIRECTIONAL_EXPLORE_RATE", 0.05),
            directional_max_bankroll_frac=_envf("PULSE_DIRECTIONAL_MAX_BANKROLL_FRAC", 0.10),
            directional_down_only=str(
                os.getenv("PULSE_DIRECTIONAL_DOWN_ONLY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_block_up_until_promoted=str(
                os.getenv("PULSE_DIRECTIONAL_BLOCK_UP_UNTIL_PROMOTED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_up_restrictions_enabled=str(
                os.getenv("PULSE_DIRECTIONAL_UP_RESTRICTIONS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            directional_series_slugs=_dir_series,
            primary_edge_source=str(os.getenv("PULSE_PRIMARY_EDGE_SOURCE", "arbitrage")).strip()
            or "arbitrage",
            dependency_arb_enabled=str(os.getenv("PULSE_DEPENDENCY_ARB_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            dependency_arb_execute_enabled=str(os.getenv("PULSE_DEPENDENCY_ARB_EXECUTE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            dependency_arb_max_usd=_envf("PULSE_DEPENDENCY_ARB_MAX_USD", 50.0),
            dependency_arb_epsilon=_envf("PULSE_DEPENDENCY_ARB_EPSILON", 0.02),
            bregman_projection_enabled=str(os.getenv("PULSE_BREGMAN_PROJECTION_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            bregman_trade_authority=str(os.getenv("PULSE_BREGMAN_TRADE_AUTHORITY", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            bregman_alpha=_envf("PULSE_BREGMAN_ALPHA", 0.9),
            bregman_epsilon_init=_envf("PULSE_BREGMAN_EPSILON_INIT", 0.1),
            bregman_fw_max_iters=int(_envf("PULSE_BREGMAN_FW_MAX_ITERS", 50)),
            bregman_fw_time_budget_ms=_envf("PULSE_BREGMAN_FW_TIME_BUDGET_MS", 500.0),
            ip_oracle_backend=str(os.getenv("PULSE_IP_ORACLE_BACKEND", "ortools")).strip()
            or "ortools",
            clob_websocket_enabled=str(os.getenv("PULSE_CLOB_WEBSOCKET_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            stop_min_sharpe=_envf("PULSE_STOP_MIN_SHARPE", 0.0),
            stop_sharpe_min_samples=int(_envf("PULSE_STOP_SHARPE_MIN_SAMPLES", 20)),
            eth_series_enabled=str(os.getenv("PULSE_ETH_SERIES_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_dependency_enabled=str(os.getenv("PULSE_GROK_DEPENDENCY_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            grok_dependency_interval_s=_envf("PULSE_GROK_DEPENDENCY_INTERVAL_S", 180.0),
            sizing_promotion_gated=str(os.getenv("PULSE_SIZING_PROMOTION_GATED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            selectivity_gate_enabled=str(os.getenv("PULSE_SELECTIVITY_GATE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            selectivity_min_samples=int(_envf("PULSE_SELECTIVITY_MIN_SAMPLES", 50)),
            selectivity_min_win_rate=_envf("PULSE_SELECTIVITY_MIN_WIN_RATE", 0.52),
            selectivity_min_profit_factor=_envf("PULSE_SELECTIVITY_MIN_PROFIT_FACTOR", 0.85),
            selectivity_fdr_q=_envf("PULSE_SELECTIVITY_FDR_Q", 0.10),
            selectivity_confidence_z=_envf("PULSE_SELECTIVITY_CONFIDENCE_Z", 1.64),
            selectivity_exploration_rate=_envf("PULSE_SELECTIVITY_EXPLORATION_RATE", 0.05),
            calibration_min_samples=int(_envf("PULSE_CALIB_MIN_SAMPLES", 30)),
            calibration_max_shrink=_envf("PULSE_CALIB_MAX_SHRINK", 0.5),
            tv_context_gate_enabled=str(os.getenv("PULSE_TV_CONTEXT_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_context_blocked_volume_states=tuple(
                s.strip().lower() for s in os.getenv("PULSE_TV_CONTEXT_BLOCK_VOLUME", "spike")
                .split(",") if s.strip()),
            tv_context_blocked_hurst_regimes=tuple(
                s.strip().lower() for s in os.getenv("PULSE_TV_CONTEXT_BLOCK_HURST", "noise")
                .split(",") if s.strip()),
            tv_context_max_ttc_s=_envf("PULSE_TV_CONTEXT_MAX_TTC_S", 240.0),
            tv_context_block_liquidation_spike=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_LIQUIDATION", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_block_event_blackout=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_EVENT_BLACKOUT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_block_grok_event_risk_high=str(
                os.getenv("PULSE_TV_CONTEXT_BLOCK_GROK_EVENT_RISK", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_context_exploration_rate=_envf("PULSE_TV_CONTEXT_EXPLORATION_RATE", 0.0),
            tv_down_bias_gate_enabled=str(os.getenv("PULSE_TV_DOWN_BIAS_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_down_bias_exploration_rate=_envf("PULSE_TV_DOWN_BIAS_EXPLORE_RATE", 0.0),
            tv_down_bias_block_up_on_bearish_down_stack=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_ON_BEARISH_DOWN_STACK", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_tv_down_non_bearish=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_TV_DOWN_NON_BEARISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_against_confirmed_down=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_AGAINST_CONFIRMED_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_mixed_mtf_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_MIXED_MTF_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_bullish_supertrend_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_BULLISH_SUPERTREND_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_vwap_above=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_VWAP_ABOVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bb_expansion_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BB_EXPANSION_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_range_breakout_down=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_BREAKOUT_DOWN", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bb_squeeze=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BB_SQUEEZE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_range_top=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_RANGE_TOP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_markov_chop_noise=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MARKOV_CHOP_NOISE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_htf_bullish=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_HTF_BULLISH", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bear_close_near_low=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BEAR_CLOSE_NEAR_LOW", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_medium_edge=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MEDIUM_EDGE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_weak_cex=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_WEAK_CEX", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_late_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_LATE_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_early_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_EARLY_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_ask_heavy_ob=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_ASK_HEAVY_OB", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_tf_confirm_conflict=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_TF_CONFIRM_CONFLICT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_cvd_neutral=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_NEUTRAL", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_cvd_buy_pressure=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_CVD_BUY_PRESSURE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_low_conviction=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_LOW_CONVICTION", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_bearish_mtf_tv_up=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_BEARISH_MTF_TV_UP", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_mid_ttc=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MID_TTC", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_neutral_zscore=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_NEUTRAL_ZSCORE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_medium_confidence=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_MEDIUM_CONFIDENCE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_not_stale=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_NOT_STALE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_volume_active=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_VOLUME_ACTIVE", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_block_up_underdog_entry=str(
                os.getenv("PULSE_TV_DOWN_BIAS_BLOCK_UP_UNDERDOG_ENTRY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_down_bias_up_underdog_entry_max=_envf(
                "PULSE_TV_DOWN_BIAS_UP_UNDERDOG_ENTRY_MAX", 0.55),
            tv_down_bias_up_late_ttc_min_s=_envf("PULSE_TV_DOWN_BIAS_UP_LATE_TTC_MIN_S", 240.0),
            tv_down_bias_up_early_ttc_max_s=_envf("PULSE_TV_DOWN_BIAS_UP_EARLY_TTC_MAX_S", 120.0),
            tv_down_bias_up_mid_ttc_min_s=_envf("PULSE_TV_DOWN_BIAS_UP_MID_TTC_MIN_S", 120.0),
            tv_down_bias_up_mid_ttc_max_s=_envf("PULSE_TV_DOWN_BIAS_UP_MID_TTC_MAX_S", 180.0),
            tv_down_bias_up_min_conviction=_envf("PULSE_TV_DOWN_BIAS_UP_MIN_CONVICTION", 0.40),
            tv_mtf_conflict_gate_enabled=str(os.getenv("PULSE_TV_MTF_CONFLICT_GATE", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_confirm=str(os.getenv("PULSE_TV_MTF_REQUIRE_CONFIRM", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_all_confirm=str(os.getenv("PULSE_TV_MTF_REQUIRE_ALL_CONFIRM", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_require_side_align=str(os.getenv("PULSE_TV_MTF_REQUIRE_SIDE_ALIGN", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tv_mtf_conflict_exploration_rate=_envf("PULSE_TV_MTF_CONFLICT_EXPLORE_RATE", 0.0),
            stop_enabled=str(os.getenv("PULSE_STOP_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            stop_rolling_n=int(_envf("PULSE_STOP_ROLLING_N", 50)),
            stop_min_samples=int(_envf("PULSE_STOP_MIN_SAMPLES", 30)),
            stop_min_profit_factor=_envf("PULSE_STOP_MIN_PROFIT_FACTOR", 0.85),
            stop_max_drawdown_pct=_envf("PULSE_STOP_MAX_DRAWDOWN_PCT", 25.0),
            late_window_entry_enabled=str(os.getenv("PULSE_LATE_WINDOW_ENTRY", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            late_window_max_ttc_s=_envf("PULSE_LATE_WINDOW_MAX_TTC_S", 120.0),
            late_window_min_conviction=_envf("PULSE_LATE_WINDOW_MIN_CONVICTION", 0.40),
            signal_engine_enabled=str(os.getenv("HERMES_SIGNAL_ENGINE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            factor_model_enabled=str(os.getenv("HERMES_FACTOR_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            markov_enabled=str(os.getenv("HERMES_MARKOV_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_model_enabled=str(os.getenv("HERMES_EDGE_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            learning_enabled=str(os.getenv("PULSE_LEARNING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            learning_min_samples=int(_envf("PULSE_LEARNING_MIN_SAMPLES", 60)),
            learning_bench_min_samples=int(_envf("PULSE_LEARNING_BENCH_MIN_SAMPLES", 20)),
            learning_bench_margin=_envf("PULSE_LEARNING_BENCH_MARGIN", 0.0),
            learning_max_weight=_envf("PULSE_LEARNING_MAX_WEIGHT", 0.5),
            learning_ramp_samples=_envf("PULSE_LEARNING_RAMP_SAMPLES", 300.0),
            learning_max_calib_error=_envf("PULSE_LEARNING_MAX_CALIB_ERROR", 0.15),
            sizing_enabled=str(os.getenv("HERMES_SIZING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            sizing_hard_cap_usd=_envf("HERMES_SIZING_HARD_CAP_USD", 10.0),
            sizing_daily_loss_cap_usd=_envf("HERMES_SIZING_DAILY_LOSS_CAP_USD", 50.0),
            sizing_bankroll_usd=_envf("HERMES_SIZING_BANKROLL_USD", 1000.0),
            starting_capital_usd=_envf("PULSE_STARTING_CAPITAL_USD", 500.0),
            tradingview_secret=(os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "") or "").strip(),
            tradingview_allowed_symbols=tuple(
                s.strip().upper() for s in os.getenv(
                    "TRADINGVIEW_ALLOWED_SYMBOLS",
                    "BTCUSD,INDEX:BTCUSD,BTC/USD,BTC,XBTUSD").split(",")
                if s.strip()),
            # bot name: TRADINGVIEW_BOT_NAME takes precedence, else BOT_NAME, else "hermes"
            tradingview_bot_name=((os.getenv("TRADINGVIEW_BOT_NAME") or os.getenv("BOT_NAME")
                                   or "hermes").strip()),
            tradingview_webhook_host=(os.getenv("TRADINGVIEW_WEBHOOK_HOST", "127.0.0.1")
                                      or "127.0.0.1").strip(),
            tradingview_webhook_port=int(_envf("TRADINGVIEW_WEBHOOK_PORT", 8787)),
            tradingview_webhook_path=(os.getenv("TRADINGVIEW_WEBHOOK_PATH", "/webhooks/tradingview")
                                      or "/webhooks/tradingview").strip(),
            tradingview_max_age_s=_envf("TRADINGVIEW_MAX_AGE_S", 90.0),
            tradingview_feature_symbol=normalize_symbol(
                os.getenv("PULSE_TV_FEATURE_SYMBOL", "BTCUSD") or "BTCUSD") or "BTCUSD",
            tradingview_mtf_timeframes=_parse_tv_mtf_timeframes(
                os.getenv("PULSE_TV_MTF_TIMEFRAMES", "2,3,4")),
            tradingview_mtf_confirm_window_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_S", 360.0),
            tradingview_mtf_confirm_window_10m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_10M_S", 660.0),
            tradingview_mtf_confirm_window_15m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_15M_S", 960.0),
            tradingview_mtf_confirm_window_2m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_2M_S", 300.0),
            tradingview_mtf_confirm_window_3m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_3M_S", 450.0),
            tradingview_mtf_confirm_window_4m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_4M_S", 600.0),
            tradingview_mtf_confirm_window_13m_s=_envf("PULSE_TV_MTF_CONFIRM_WINDOW_13M_S", 840.0),
            pulse_series_slugs=_series_slugs,
            tradingview_signal_max_feature_age_s=_envf("PULSE_TV_SIGNAL_MAX_FEATURE_AGE_S", 300.0),
            tradingview_signal_gate_enabled=str(os.getenv("PULSE_TRADINGVIEW_SIGNAL_GATE", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tradingview_min_signal_strength=_envf("PULSE_TV_MIN_SIGNAL_STRENGTH", 0.0),
            tv_confidence_tier_enabled=str(
                os.getenv("PULSE_TV_CONFIDENCE_TIER_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_require_sweet_spot=str(
                os.getenv("PULSE_TV_TIER_REQUIRE_SWEET_SPOT", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_15m_only=str(os.getenv("PULSE_TV_TIER_15M_ONLY", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            tv_tier_aligned_strength_min=_envf("PULSE_TV_TIER_ALIGNED_STRENGTH_MIN", 0.72),
            tv_tier_a_min_edge_delta=_envf("PULSE_TV_TIER_A_MIN_EDGE_DELTA", -0.005),
            tv_tier_a_max_price_delta=_envf("PULSE_TV_TIER_A_MAX_PRICE_DELTA", 0.02),
            tv_tier_c_min_edge_delta=_envf("PULSE_TV_TIER_C_MIN_EDGE_DELTA", 0.005),
            tv_tier_c_max_price_delta=_envf("PULSE_TV_TIER_C_MAX_PRICE_DELTA", -0.03),
            tradingview_signal_horizon_s=_envf("PULSE_TV_SIGNAL_HORIZON_S", 300.0),
            tradingview_promotion_allowed=str(os.getenv("PULSE_TV_PROMOTION_ALLOWED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            tradingview_promotion_min_samples=int(_envf("PULSE_TV_PROMOTION_MIN_SAMPLES", 50)),
            tradingview_promotion_min_win_rate=_envf("PULSE_TV_PROMOTION_MIN_WIN_RATE", 0.80),
            data_dir=os.getenv("HTE_DATA_DIR", "/data"))


class PulseEngine:
    def __init__(self, cfg: Optional[PulseConfig] = None, *, market_feed=None,
                 price_feed=None):
        self.cfg = cfg or PulseConfig()
        # reject classic Chainlink Data Feed / AggregatorV3 as the primary settlement feed
        from engine.pulse.oracle import validate_oracle_feed_type, LeadFeeds
        self.oracle_feed_type = validate_oracle_feed_type(self.cfg.oracle_feed_type)
        if market_feed is not None:
            self.market = market_feed
        elif len(self.cfg.pulse_series_slugs) > 1:
            self.market = MultiSeriesMarketFeed(self.cfg.pulse_series_slugs)
        else:
            slug = self.cfg.pulse_series_slugs[0] if self.cfg.pulse_series_slugs else SERIES_SLUG_5M
            self.market = PulseMarketFeed(series_slug=slug)
        self.rtds = None
        if price_feed is not None:
            self.price = price_feed
        elif self.cfg.rtds_enabled:
            # CANONICAL oracle: Chainlink ref price via Polymarket RTDS crypto_prices_chainlink.
            from engine.pulse.rtds import RTDSClient, TOPIC_CHAINLINK, TOPIC_BINANCE
            self.rtds = RTDSClient(subscriptions=[(TOPIC_CHAINLINK, self.cfg.oracle_symbol),
                                                  (TOPIC_BINANCE, "btcusdt")])
            self.rtds.max_age_s = float(self.cfg.rtds_max_age_s)
            self.rtds.start()
            # poll the FRESH oracle price: a stale/dead socket returns None so the feed fails CLOSED
            # (last_ts stops advancing) instead of serving an aged cached level as 'live'.
            _rtds = self.rtds
            self.price = PulsePriceFeed(
                fetcher=lambda: _rtds.fresh_oracle_price(self.cfg.rtds_max_age_s),
                source_name="rtds_chainlink",
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
                max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self.price.start_sampler()
        else:
            fetcher, src = build_price_source(self.cfg.price_source)
            self.price = PulsePriceFeed(
                fetcher=fetcher, source_name=src,
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
                max_open_lag_15m_s=self.cfg.max_open_lag_15m_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self.price.start_sampler()
        # fast LEAD feeds (Binance via RTDS, Coinbase via REST) — FEATURES ONLY, never truth
        self.leads = LeadFeeds(self.cfg.fast_feeds, rtds=self.rtds,
                               window_s=self.cfg.vol_window_s)
        self.ledger = PulseLedger()
        self.calib = PulseCalibration()
        # OBSERVE-ONLY research features (EP Chan-inspired) — logged, never trade/size/veto.
        self.research = None
        if bool(getattr(self.cfg, "research_features_enabled", True)):
            from engine.pulse.research_features import ResearchObservatory
            self.research = ResearchObservatory()
        self.signals = None
        if bool(getattr(self.cfg, "signal_engine_enabled", True)):
            from engine.pulse.signals import SignalEngine
            self.signals = SignalEngine()
        self.factors = None
        if bool(getattr(self.cfg, "factor_model_enabled", True)):
            from engine.pulse.factors import FactorEngine
            self.factors = FactorEngine()
        self.markov = None
        if bool(getattr(self.cfg, "markov_enabled", True)):
            from engine.pulse.markov import MarkovRegime
            self.markov = MarkovRegime()
        self.edge_model = None
        if bool(getattr(self.cfg, "edge_model_enabled", True)):
            from engine.pulse.edge_model import EdgeModel
            self.edge_model = EdgeModel()
        # Learned Selectivity Gate v1 — live-evidence bucket gate between decision and execution.
        from engine.pulse.late_window import LateWindowEntry, LateWindowEdge
        self.late_window_gate = LateWindowEntry(
            enabled=bool(self.cfg.late_window_entry_enabled),
            max_ttc_s=self.cfg.late_window_max_ttc_s,
            min_conviction=self.cfg.late_window_min_conviction)
        self.late_window_edge = LateWindowEdge(   # OBSERVE-ONLY time-decay edge measurement
            max_ttc_s=self.cfg.late_window_max_ttc_s,
            min_conviction=self.cfg.late_window_min_conviction)
        from engine.pulse.config_coupling import (
            apply_context_cohort_coupling, window_seconds_for_slugs)
        _ctx_max, self._config_coupling = apply_context_cohort_coupling(
            baseline_cohort_enabled=bool(self.cfg.baseline_cohort_gate_enabled),
            tv_context_enabled=bool(self.cfg.tv_context_gate_enabled),
            configured_context_max_ttc_s=self.cfg.tv_context_max_ttc_s,
            cohort_ttc_min_s=self.cfg.baseline_cohort_ttc_min_s,
            cohort_ttc_max_s=self.cfg.baseline_cohort_ttc_max_s,
            window_seconds_list=window_seconds_for_slugs(self.cfg.pulse_series_slugs),
        )
        if self._config_coupling.get("auto_clamped"):
            logger.warning(
                "PULSE_TV_CONTEXT_MAX_TTC_S=%s below required %s for baseline cohort "
                "— auto-raised effective max to %s",
                self._config_coupling.get("configured_s"),
                self._config_coupling.get("required_min_s"),
                self._config_coupling.get("effective_s"),
            )
        elif self._config_coupling.get("active") and not self._config_coupling.get("configured_ok"):
            logger.error(
                "Gate coupling deadlock: PULSE_TV_CONTEXT_MAX_TTC_S=%s; need >= %s. %s",
                self._config_coupling.get("configured_s"),
                self._config_coupling.get("required_min_s"),
                self._config_coupling.get("fix_hint"),
            )
        from engine.pulse.context_gate import TradingViewContextGate
        self.tv_context_gate = TradingViewContextGate(
            enabled=bool(self.cfg.tv_context_gate_enabled),
            blocked_volume_states=self.cfg.tv_context_blocked_volume_states,
            blocked_hurst_regimes=self.cfg.tv_context_blocked_hurst_regimes,
            max_ttc_s=_ctx_max,
            block_liquidation_spike=self.cfg.tv_context_block_liquidation_spike,
            block_event_blackout=self.cfg.tv_context_block_event_blackout,
            block_grok_event_risk_high=self.cfg.tv_context_block_grok_event_risk_high,
            exploration_rate=self.cfg.tv_context_exploration_rate)
        from engine.pulse.tv_down_bias_gate import TradingViewDownBiasGate
        self.tv_down_bias_gate = TradingViewDownBiasGate(
            enabled=bool(self.cfg.tv_down_bias_gate_enabled),
            block_up_on_bearish_down_stack=bool(
                self.cfg.tv_down_bias_block_up_on_bearish_down_stack),
            block_up_tv_down_non_bearish=bool(
                self.cfg.tv_down_bias_block_up_tv_down_non_bearish),
            block_up_against_confirmed_down=bool(
                self.cfg.tv_down_bias_block_up_against_confirmed_down),
            block_mixed_mtf_up=bool(self.cfg.tv_down_bias_block_mixed_mtf_up),
            block_bullish_supertrend_up=bool(
                self.cfg.tv_down_bias_block_bullish_supertrend_up),
            block_up_vwap_above=bool(self.cfg.tv_down_bias_block_up_vwap_above),
            block_up_bb_expansion_up=bool(self.cfg.tv_down_bias_block_up_bb_expansion_up),
            block_up_range_breakout_down=bool(
                self.cfg.tv_down_bias_block_up_range_breakout_down),
            block_up_range_top=bool(self.cfg.tv_down_bias_block_up_range_top),
            block_up_bb_squeeze=bool(self.cfg.tv_down_bias_block_up_bb_squeeze),
            block_up_markov_chop_noise=bool(
                self.cfg.tv_down_bias_block_up_markov_chop_noise),
            block_up_htf_bullish=bool(self.cfg.tv_down_bias_block_up_htf_bullish),
            block_up_bear_close_near_low=bool(
                self.cfg.tv_down_bias_block_up_bear_close_near_low),
            block_up_medium_edge=bool(self.cfg.tv_down_bias_block_up_medium_edge),
            block_up_weak_cex=bool(self.cfg.tv_down_bias_block_up_weak_cex),
            block_up_late_ttc=bool(self.cfg.tv_down_bias_block_up_late_ttc),
            block_up_early_ttc=bool(self.cfg.tv_down_bias_block_up_early_ttc),
            block_up_ask_heavy_ob=bool(self.cfg.tv_down_bias_block_up_ask_heavy_ob),
            block_up_tf_confirm_conflict=bool(
                self.cfg.tv_down_bias_block_up_tf_confirm_conflict),
            block_up_cvd_neutral=bool(self.cfg.tv_down_bias_block_up_cvd_neutral),
            block_up_cvd_buy_pressure=bool(self.cfg.tv_down_bias_block_up_cvd_buy_pressure),
            block_up_low_conviction=bool(self.cfg.tv_down_bias_block_up_low_conviction),
            block_up_bearish_mtf_tv_up=bool(self.cfg.tv_down_bias_block_up_bearish_mtf_tv_up),
            block_up_mid_ttc=bool(self.cfg.tv_down_bias_block_up_mid_ttc),
            block_up_neutral_zscore=bool(self.cfg.tv_down_bias_block_up_neutral_zscore),
            block_up_medium_confidence=bool(self.cfg.tv_down_bias_block_up_medium_confidence),
            block_up_not_stale=bool(self.cfg.tv_down_bias_block_up_not_stale),
            block_up_volume_active=bool(self.cfg.tv_down_bias_block_up_volume_active),
            block_up_underdog_entry=bool(self.cfg.tv_down_bias_block_up_underdog_entry),
            up_underdog_entry_max=self.cfg.tv_down_bias_up_underdog_entry_max,
            up_late_ttc_min_s=self.cfg.tv_down_bias_up_late_ttc_min_s,
            up_early_ttc_max_s=self.cfg.tv_down_bias_up_early_ttc_max_s,
            up_mid_ttc_min_s=self.cfg.tv_down_bias_up_mid_ttc_min_s,
            up_mid_ttc_max_s=self.cfg.tv_down_bias_up_mid_ttc_max_s,
            up_min_conviction=self.cfg.tv_down_bias_up_min_conviction,
            exploration_rate=self.cfg.tv_down_bias_exploration_rate)
        from engine.pulse.tv_mtf_gate import TradingViewMtfConflictGate
        self.tv_mtf_gate = TradingViewMtfConflictGate(
            enabled=bool(self.cfg.tv_mtf_conflict_gate_enabled),
            require_confirm=bool(self.cfg.tv_mtf_require_confirm),
            require_all_confirm=bool(self.cfg.tv_mtf_require_all_confirm),
            require_side_align=bool(self.cfg.tv_mtf_require_side_align),
            exploration_rate=self.cfg.tv_mtf_conflict_exploration_rate)
        from engine.pulse.down_stack import DownStackGrader
        self.down_stack = DownStackGrader()
        from engine.pulse.stop_conditions import StrategyStopMonitor, StopConfig
        self.stop_monitor = StrategyStopMonitor(cfg=StopConfig(
            enabled=bool(self.cfg.stop_enabled),
            rolling_n=self.cfg.stop_rolling_n,
            min_samples=self.cfg.stop_min_samples,
            min_profit_factor=self.cfg.stop_min_profit_factor,
            max_drawdown_pct=self.cfg.stop_max_drawdown_pct,
            min_sharpe=float(self.cfg.stop_min_sharpe),
            sharpe_min_samples=int(self.cfg.stop_sharpe_min_samples)))
        from engine.pulse.clob_feed import ClobBookFeed
        self.clob_feed = ClobBookFeed(websocket_enabled=bool(self.cfg.clob_websocket_enabled))
        self._wire_clob_feed_metrics()
        from engine.pulse.selectivity import SelectivityEvidence, LearnedSelectivityGate
        self.selectivity_evidence = SelectivityEvidence()
        self.selectivity_gate = LearnedSelectivityGate(
            enabled=bool(self.cfg.selectivity_gate_enabled),
            min_samples=self.cfg.selectivity_min_samples,
            min_win_rate=self.cfg.selectivity_min_win_rate,
            min_profit_factor=self.cfg.selectivity_min_profit_factor,
            fdr_q=self.cfg.selectivity_fdr_q,
            confidence_z=self.cfg.selectivity_confidence_z,
            exploration_rate=self.cfg.selectivity_exploration_rate)
        self.reconciler = LifecycleReconciler()   # GS-Quant-style candidate lifecycle audit
        self.gate_obs = GateObservations()        # orderbook-reality observations seen at the gate
        self._baseline: Optional[dict] = None     # legacy ledger totals that predate accounting
        from engine.pulse.promotion import PromotionLadder
        self.promotion = PromotionLadder()        # all features default to observe-only (level 0)
        self._daily_loss = 0.0                    # for the Kelly daily-loss-cap diagnostic
        self._daily_key = None
        from engine.pulse.reporting import OutcomeGroups
        self._groups = OutcomeGroups()            # settled PnL grouped by every entry-time tag
        from engine.pulse.tradingview import (TradingViewEdge, RSITrendModel,
                                              TradingViewSignalLearner)
        self._tv_edge = TradingViewEdge()         # OBSERVE-ONLY TradingView signal-vs-outcome edge
        self._rsi_model = RSITrendModel()         # OBSERVE-ONLY RSI alert-history next-trend model
        self._tv_learner = TradingViewSignalLearner()   # OBSERVE-ONLY bucketed perf + promotion
        self._tv_pending: list = []               # pending forward-return evals for ALL signals
        # OBSERVE-ONLY BTC Pulse Edge Signal layer (CEX basket + stale divergence + OB pressure).
        self.edge_signal = None
        self._cex_extra: dict = {}                # optional Kraken/Bitstamp fetchers (opt-in)
        if bool(getattr(self.cfg, "edge_signal_enabled", True)):
            from engine.pulse.edge_signal import EdgeSignalEngine
            members = ["binance_btcusdt", "coinbase_btcusd"]
            if bool(self.cfg.edge_extra_cex_enabled):
                members += ["kraken_btcusd", "bitstamp_btcusd"]
                try:
                    from engine.pulse.cex_feeds import kraken_spot_fetcher, bitstamp_spot_fetcher
                    self._cex_extra = {"kraken_btcusd": kraken_spot_fetcher(),
                                       "bitstamp_btcusd": bitstamp_spot_fetcher()}
                except Exception:  # noqa: BLE001
                    self._cex_extra = {}
            self.edge_signal = EdgeSignalEngine(members)
        # CEX-lead latency edge (grades CEX-implied P(up) vs the market; PAPER ONLY, shadow default).
        self.cex_lead = None
        self._cex_lead_pending: list = []
        # directional allowlist cold-start exploration (avoids the proven-winning deadlock/freeze)
        self._allowlist_rng = random.Random(1729)
        self._allowlist_explored = 0
        self._allowlist_blocked = 0
        # within-window RISK-FREE arbitrage (separate ledger -> P&L NEVER blended with directional)
        self.arb_ledger = None
        if bool(getattr(self.cfg, "arbitrage_enabled", True)):
            from engine.pulse.arbitrage import ArbLedger
            self.arb_ledger = ArbLedger()
        self.dep_arb_ledger = None
        if bool(getattr(self.cfg, "dependency_arb_enabled", True)):
            from engine.pulse.dependency_arb import DependencyArbLedger
            self.dep_arb_ledger = DependencyArbLedger(
                execute_enabled=bool(self.cfg.dependency_arb_execute_enabled))
        # market-beating benchmark for the learning blend: grade the edge model's P(up) vs the MARKET
        # price (poly_yes) per window; the blend only activates when the model actually beats the
        # market out-of-sample (kills phantom edge — calibrated != more accurate than the market).
        self._mkt_bench_pending: list = []
        self._mkt_bench_recent: deque = deque(maxlen=400)   # (model_se, market_se, fair_se)
        if bool(getattr(self.cfg, "cex_lead_enabled", True)):
            from engine.pulse.cex_lead import CexLeadEdge
            self.cex_lead = CexLeadEdge(
                enabled=True, mode=self.cfg.cex_lead_mode,
                min_samples=self.cfg.cex_lead_min_samples,
                min_divergence=self.cfg.cex_lead_min_divergence,
                confidence_z=self.cfg.cex_lead_confidence_z,
                min_edge_vs_market=self.cfg.cex_lead_min_edge_vs_market,
                tv_strength_thr=self.cfg.cex_lead_tv_strength_thr,
                decisive_thr=self.cfg.cex_lead_decisive_thr,
                late_ttc_s=self.cfg.cex_lead_late_ttc_s,
                kelly_scale=self.cfg.cex_lead_kelly_scale,
                max_size_frac=self.cfg.cex_lead_max_size_frac)
        self._ev_before_sum = 0.0                 # EV before/after costs (accepted candidates)
        self._ev_after_sum = 0.0
        self._ev_n = 0
        self._exec_realistic_samples: list = []
        self._payoff_guard_counts: dict = {
            "rejected_tiny_upside": 0,
            "rejected_bad_reward_to_risk": 0,
            "rejected_high_entry_insufficient_margin": 0,
        }
        self._last_simplex: dict = {}
        # ---- Grok consumers share ONE budget guard (daily $ cap + per-feature hourly calls) ----
        # All OBSERVE-ONLY / off hot path / fail-open; none can place, size, or bypass a trade.
        self.grok_budget = None
        self.overlay = None
        self.grok_analyst = None
        self.grok_predictor = None
        self.grok_decider = None
        self.grok_news = None
        self.grok_dep_screener = None
        self._grok_pending: list = []             # pending decision grades (decision_id/price0/close)
        self._grok_tv_fp: dict = {}               # decision_id -> last MTF fingerprint (refresh Grok)
        self._grok_entry_band_seen: set = set()   # windows that got entry-band Grok refresh
        self._verifier_pending: list = []        # pending verifier counterfactual grades at window close
        self._recent_windows: list = []           # rolling recent BTC 5m window outcomes (for Grok)
        import random as _random
        self._grok_rng = _random.Random()         # exploration sampler (follow-mode data gathering)
        self._grok_policy_counts = {"exploit": 0, "explore": 0, "avoid": 0}   # adaptive-loop tally
        self._mispricing_gate_counts: dict = {}
        self._baseline_cohort_gate_counts: dict = {}
        self._tv_tier_counts: dict = {}
        try:
            from engine.pulse.grok_intel import (GrokBudget, GrokSignalAnalyst,
                                                 GrokSignalPredictor, xai_key)
            decider_on = str(self.cfg.grok_decider_mode).strip().lower() in ("shadow", "follow")
            any_grok = (bool(self.cfg.grok_overlay_enabled)
                        or bool(self.cfg.grok_signal_analyst_enabled)
                        or bool(self.cfg.grok_signal_predictor_enabled)
                        or bool(self.cfg.grok_dependency_enabled)
                        or decider_on)
            if any_grok and xai_key():
                self.grok_budget = GrokBudget(
                    daily_usd_cap=self.cfg.grok_budget_daily_usd,
                    est_usd_per_call=self.cfg.grok_est_usd_per_call,
                    per_feature_hourly={"predictor": self.cfg.grok_predictor_max_calls_per_hour,
                                        "analyst": self.cfg.grok_analyst_max_calls_per_hour,
                                        "overlay": self.cfg.grok_overlay_max_calls_per_hour,
                                        "decider": self.cfg.grok_decider_max_calls_per_hour,
                                        "news": 40,
                                        "dependency": 12})
            if bool(self.cfg.grok_overlay_enabled) and xai_key():
                from engine.pulse.overlay import GrokEventOverlay
                self.overlay = GrokEventOverlay(
                    interval_s=self.cfg.grok_overlay_interval_s,
                    max_calls_per_hour=self.cfg.grok_overlay_max_calls_per_hour,
                    budget=self.grok_budget)
                self.overlay.start()
            if bool(self.cfg.grok_signal_predictor_enabled) and xai_key():
                self.grok_predictor = GrokSignalPredictor(budget=self.grok_budget).start()
            if bool(self.cfg.grok_signal_analyst_enabled) and xai_key():
                self.grok_analyst = GrokSignalAnalyst(
                    budget=self.grok_budget, interval_s=self.cfg.grok_analyst_interval_s,
                    report_provider=self._grok_analyst_report).start()
            if decider_on and xai_key():
                from engine.pulse.grok_decider import (GrokDecider, make_decider_fn,
                                                       GrokNewsDigest, make_news_fn)
                # news digest is a SEPARATE periodic search worker; the per-window decision reuses it
                # (cheaper/faster than searching every window). Enabled via use_search.
                if bool(self.cfg.grok_decider_use_search) or self.cfg.grok_tiered_compute_enabled:
                    self.grok_news = GrokNewsDigest(
                        budget=self.grok_budget,
                        news_fn=make_news_fn(model=self.cfg.grok_decider_model,
                                             timeout_s=max(35.0, self.cfg.grok_decider_timeout_s)),
                        interval_s=self.cfg.grok_news_refresh_s).start()
                self.grok_decider = GrokDecider(
                    decider_fn=make_decider_fn(
                        model=self.cfg.grok_decider_model,
                        timeout_s=self.cfg.grok_decider_timeout_s,
                        use_search=bool(self.cfg.grok_decider_use_search),
                        use_search_deep_only=True,
                        default_ttl_s=self.cfg.grok_decider_ttl_s),
                    budget=self.grok_budget, mode=self.cfg.grok_decider_mode,
                    min_confidence=self.cfg.grok_decider_min_confidence,
                    ttl_s=self.cfg.grok_decider_ttl_s,
                    max_consecutive_losses=self.cfg.grok_decider_max_consecutive_losses,
                    daily_loss_cap_usd=self.cfg.grok_decider_daily_loss_cap_usd,
                    max_latency_s=self.cfg.grok_decider_max_latency_s,
                    cooldown_s=self.cfg.grok_decider_cooldown_s).start()
            if bool(self.cfg.grok_dependency_enabled) and xai_key():
                from engine.pulse.grok_dependency import GrokDependencyScreener
                self.grok_dep_screener = GrokDependencyScreener(
                    windows_fn=lambda: self.market.active_windows(),
                    budget=self.grok_budget,
                    interval_s=self.cfg.grok_dependency_interval_s).start()
        except Exception:  # noqa: BLE001 — Grok never blocks startup
            logger.exception("grok init failed; continuing as pure quant")
            self.grok_budget = self.overlay = self.grok_analyst = self.grok_predictor = None
            self.grok_decider = self.grok_news = self.grok_dep_screener = None
        # ---- #2 compounding lessons + #3 loop registry ----
        from engine.pulse.lessons import LessonsBook
        from engine.pulse.loops import LoopRegistry
        from engine.pulse.decision_history import TradeDecisionHistory
        self.lessons = LessonsBook(revalidate_ttl_s=self.cfg.lessons_revalidate_ttl_s)
        self.trade_history = TradeDecisionHistory(max_trades=50)
        self.loops = LoopRegistry()
        # ---- #1 independent Claude maker-checker verifier + #4 research meta-loop ----
        self.claude_budget = None
        self.verifier = None
        self.research_loop = None
        self._research_avoid: set = set()      # canonical "dim=bucket" contexts auto-blocked by Claude
        self._research_exploit: set = set()    # "dim=bucket" contexts Claude flags AND data proves WINNING
        try:
            from engine.pulse.claude_client import anthropic_key
            need_claude = bool(self.cfg.verifier_enabled) or bool(self.cfg.research_loop_enabled)
            if need_claude and anthropic_key():
                from engine.pulse.grok_intel import GrokBudget
                self.claude_budget = GrokBudget(
                    daily_usd_cap=self.cfg.claude_budget_daily_usd,
                    est_usd_per_call=self.cfg.claude_est_usd_per_call,
                    per_feature_hourly={"verifier": self.cfg.verifier_max_calls_per_hour,
                                        "research": self.cfg.research_max_calls_per_hour})
                if self.cfg.verifier_enabled:
                    from engine.pulse.verifier import ClaudeVerifier
                    self.verifier = ClaudeVerifier(budget=self.claude_budget, enabled=True,
                                                   fail_open=self.cfg.verifier_fail_open).start()
                if self.cfg.research_loop_enabled:
                    from engine.pulse.research_loop import ResearchLoop
                    self.research_loop = ResearchLoop(
                        budget=self.claude_budget, interval_s=self.cfg.research_interval_s,
                        event_min_gap_s=self.cfg.research_event_min_gap_s,
                        report_provider=self._research_report, lessons=self.lessons,
                        apply_fn=self._research_apply,
                        auto_apply=self.cfg.research_auto_apply).start()
        except Exception:  # noqa: BLE001 — verifier/research never block startup
            logger.exception("claude verifier/research init failed; continuing")
            self.claude_budget = self.verifier = self.research_loop = None
        # OBSERVE-ONLY TradingView indicator webhook intake (enabled only when a secret is set).
        # Alerts become candidate signals only; they can never place/resize/bypass a paper trade.
        self.tradingview = None
        self.webhook = None
        if str(getattr(self.cfg, "tradingview_secret", "") or "").strip():
            try:
                from engine.pulse.tradingview import TradingViewIntake
                from engine.pulse.webhook import WebhookServer
                self.tradingview = TradingViewIntake(
                    secret=self.cfg.tradingview_secret,
                    allowed_symbols=self.cfg.tradingview_allowed_symbols,
                    bot_name=self.cfg.tradingview_bot_name,
                    max_age_s=self.cfg.tradingview_max_age_s, data_dir=self.cfg.data_dir,
                    feature_symbol=self.cfg.tradingview_feature_symbol,
                    mtf_timeframes=self.cfg.tradingview_mtf_timeframes,
                    confirm_windows_by_tf=_tv_mtf_confirm_windows(self.cfg),
                    confirm_window_s=self.cfg.tradingview_mtf_confirm_window_s,
                    confirm_window_10m_s=self.cfg.tradingview_mtf_confirm_window_10m_s,
                    confirm_window_15m_s=self.cfg.tradingview_mtf_confirm_window_15m_s)
                self.webhook = WebhookServer(
                    self.tradingview, host=self.cfg.tradingview_webhook_host,
                    port=self.cfg.tradingview_webhook_port,
                    path=self.cfg.tradingview_webhook_path).start()
            except Exception:  # noqa: BLE001 — intake never blocks the paper loop
                logger.exception("tradingview webhook init failed; continuing without it")
                self.tradingview = None
                self.webhook = None
        self._register_loops()
        self.ticks = 0
        self.last_tick_ts = 0.0
        self._reasons: dict = {}
        self._last_eval: list = []
        self._data_dir = Path(self.cfg.data_dir)
        self._ledger_path = self._data_dir / "btc_pulse_ledger.json"
        from engine.pulse.performance_scoring import PerformanceScoreHistory
        self._score_history = PerformanceScoreHistory(
            self._data_dir / "btc_pulse_score_history.json")
        if not self.cfg.fresh_start:
            self._load_state()
        elif self._ledger_path.exists():
            self._archive_prior_state()
        self._maybe_reset_capital()   # token-gated SURGICAL capital reset (keeps all learning)
        self._resolve_baseline()

    @staticmethod
    def _selectivity_tags_from_pos(pos) -> dict:
        """Entry-time bucket tags for a settled position (settlement + counterfactual)."""
        rt = pos.research or {}
        return {"hurst_regime": rt.get("hurst_regime"), "zscore_bucket": rt.get("zscore_bucket"),
                "ttc_bucket": rt.get("ttc_bucket"), "confidence_tier": rt.get("confidence_tier"),
                "spread_bucket": rt.get("spread_bucket"), "depth_bucket": rt.get("depth_bucket"),
                "markov_state": rt.get("markov_state"),
                "edge_quality_bucket": rt.get("edge_quality_bucket"),
                "stale_divergence": rt.get("edge_stale_divergence"), "direction": pos.side}

    def _selectivity_positions(self) -> list:
        """Settled positions as (tags, won, pnl) rows for the counterfactual replay."""
        rows = []
        for pos in self.ledger.positions.values():
            if pos.status == "settled":
                rows.append({"tags": self._selectivity_tags_from_pos(pos),
                             "won": bool(pos.won), "pnl": float(pos.pnl_usd or 0.0)})
        return rows

    def _maybe_reset_capital(self) -> None:
        """Token-gated SURGICAL reset: zero the paper CAPITAL / ledger / arbitrage / reconciliation
        back to a fresh ``starting_capital_usd`` while KEEPING everything the bot has LEARNED
        (probability models, calibration, selectivity evidence, lessons, signal gradings, research
        rules, CEX-lead/TV/Grok learning). Runs exactly ONCE per new ``PULSE_RESET_CAPITAL_TOKEN``
        (idempotent across restarts via a marker file). PAPER ONLY."""
        token = (os.getenv("PULSE_RESET_CAPITAL_TOKEN") or "").strip()
        if not token:
            return
        marker = self._data_dir / ".capital_reset_token"
        try:
            prior = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
        except Exception:  # noqa: BLE001
            prior = ""
        if prior == token:
            return                      # this reset token was already applied — do nothing
        # --- reset ONLY money/operational state to fresh instances ---
        self.ledger = PulseLedger()
        self.gate_obs = GateObservations()
        self.reconciler = LifecycleReconciler()
        if self.arb_ledger is not None:
            from engine.pulse.arbitrage import ArbLedger
            self.arb_ledger = ArbLedger()
        if self.dep_arb_ledger is not None:
            from engine.pulse.dependency_arb import DependencyArbLedger
            self.dep_arb_ledger = DependencyArbLedger(
                execute_enabled=bool(self.cfg.dependency_arb_execute_enabled))
        self._ev_before_sum = 0.0
        self._ev_after_sum = 0.0
        self._ev_n = 0
        self._allowlist_explored = 0
        self._allowlist_blocked = 0
        self._baseline = empty_baseline()
        self._reasons = {}
        self._last_eval = []
        # KEPT (learning, untouched): self.calib, self.edge_model, self.selectivity_evidence,
        #   self.selectivity_gate, self.lessons, self._research_avoid/_exploit, self.cex_lead,
        #   self._tv_edge/_rsi_model/_tv_learner, self.edge_signal, self.grok_*, self.verifier,
        #   self.research_loop, self._mkt_bench_*, tv_context_gate, late_window_* .
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(token, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.warning("PULSE_RESET_CAPITAL applied (token=%s): capital/ledger/arbitrage/"
                       "reconciliation reset to fresh $%.2f; ALL learning retained.",
                       token, float(self.cfg.starting_capital_usd))
        self._persist()

    def _resolve_baseline(self) -> None:
        """Establish the one-time accounting baseline. If a baseline was persisted, keep it. Else,
        if the ledger already holds trades from BEFORE this canonical accounting existed, capture
        them as an explicit legacy bucket so every count still reconciles. Otherwise start clean."""
        if self._baseline is not None and self._baseline.get("captured") is not None:
            self._repair_accounting_drift()
            return
        ls = self.ledger.stats()
        eg = self.ledger.exec_gate_stats()
        if not self.reconciler.has_history and int(ls.get("trades", 0) or 0) > 0:
            self._baseline = capture_baseline(ls, eg)
            logger.info("reconciliation baseline captured (legacy ledger): trades=%d settled=%d "
                        "exec_candidates=%d exec_accepted=%d", self._baseline["trades"],
                        self._baseline["settled"], self._baseline["exec_candidates"],
                        self._baseline["exec_accepted"])
        else:
            self._baseline = empty_baseline()
        self._repair_accounting_drift()

    def _repair_accounting_drift(self) -> None:
        """Heal ledger/lifecycle count skew from a persistence race by absorbing into baseline."""
        from engine.pulse.reconciliation import global_reconciliation, repair_accounting_drift
        lc = self.reconciler.report()
        eg = self.ledger.exec_gate_stats()
        ls = self.ledger.stats()
        if global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                 baseline=self._baseline)["global_reconciled"]:
            return
        repaired, changed = repair_accounting_drift(
            lifecycle=lc, exec_gate=eg, ledger_stats=ls, baseline=self._baseline)
        if not changed:
            return
        self._baseline = repaired
        if global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                 baseline=self._baseline)["global_reconciled"]:
            logger.warning("reconciliation drift absorbed into baseline: trades=%d settled=%d "
                           "exec_candidates=%d exec_accepted=%d",
                           self._baseline["trades"], self._baseline["settled"],
                           self._baseline["exec_candidates"], self._baseline["exec_accepted"])
            self._persist()

    def _load_state(self) -> None:
        """Restore the paper ledger + calibration from disk so P&L survives restarts."""
        if not self._ledger_path.exists():
            return
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — corrupt state never blocks startup
            logger.warning("could not read prior pulse ledger; starting empty")
            return
        self.ledger.load_state(data)
        self.calib.load_state(data.get("calibration_state") or {})
        # restore the CUMULATIVE lifecycle funnel + gate observations + EV + baseline so the
        # report no longer mixes a per-session funnel with a cross-restart ledger.
        acct = data.get("accounting_state") or {}
        self.reconciler.load_state(acct.get("lifecycle") or {})
        self.gate_obs.load_state(acct.get("gate_observations") or {})
        self._tv_edge.load_state(acct.get("tv_edge") or {})
        self._rsi_model.load_state(acct.get("rsi_trend") or {})
        self._rsi_model.canonicalize_storage(self.cfg.tradingview_feature_symbol)
        self._tv_learner.load_state(acct.get("tv_learner") or {})
        from engine.pulse.tradingview import canonical_storage_symbol
        feat_sym = self.cfg.tradingview_feature_symbol
        self._tv_pending = [
            {**row, "symbol": canonical_storage_symbol(row.get("symbol"), feat_sym)}
            for row in (acct.get("tv_pending") or [])
        ]
        if self.edge_signal is not None:
            self.edge_signal.load_state(acct.get("edge_signal") or {})
        if self.cex_lead is not None:
            self.cex_lead.load_state(acct.get("cex_lead") or {})
            self._cex_lead_pending = list(acct.get("cex_lead_pending") or [])
        self._mkt_bench_pending = list(acct.get("mkt_bench_pending") or [])
        self._mkt_bench_recent = deque((tuple(x) for x in (acct.get("mkt_bench_recent") or [])),
                                       maxlen=400)
        if self.arb_ledger is not None:
            self.arb_ledger.load_state(acct.get("arb_ledger") or {})
        if self.dep_arb_ledger is not None:
            self.dep_arb_ledger.load_state(acct.get("dep_arb_ledger") or {})
            # Config is authoritative — persisted execute flag must not block env updates.
            self.dep_arb_ledger.execute_enabled = bool(
                self.cfg.dependency_arb_execute_enabled)
        self._allowlist_explored = int(acct.get("allowlist_explored", 0) or 0)
        self._allowlist_blocked = int(acct.get("allowlist_blocked", 0) or 0)
        # restore research avoid-rules, but RE-VALIDATE each against current evidence (drops legacy
        # 'direction=', excluded liquidity dims, and any rule no longer confidently losing).
        self._research_avoid = set()
        for k in (acct.get("research_avoid") or []):
            d, _, b = str(k).partition("=")
            b = b.lower()
            if (d in self._RESEARCH_AVOID_DIMS and b
                    and self._research_rule_evidence_backed(d, b)):
                self._research_avoid.add("%s=%s" % (d, b))
        self._research_exploit = set()
        for k in (acct.get("research_exploit") or []):
            d, _, b = str(k).partition("=")
            b = b.lower()
            if d in self._RESEARCH_AVOID_DIMS and b and self._research_exploit_backed(d, b):
                self._research_exploit.add("%s=%s" % (d, b))
        self.selectivity_evidence.load_state(acct.get("selectivity_evidence") or {})
        self.selectivity_gate.load_state(acct.get("selectivity_gate") or {})
        self.tv_context_gate.load_state(acct.get("tv_context_gate") or {})
        self.tv_down_bias_gate.load_state(acct.get("tv_down_bias_gate") or {})
        self.tv_mtf_gate.load_state(acct.get("tv_mtf_gate") or {})
        self.down_stack.load_state(acct.get("down_stack") or {})
        self.late_window_gate.load_state(acct.get("late_window_gate") or {})
        self.late_window_edge.load_state(acct.get("late_window_edge") or {})
        # one-time bootstrap: if no evidence persisted yet, seed it from the existing settled
        # ledger positions so the gate uses LIVE history immediately (not hard-coded numbers).
        if not self.selectivity_evidence.has_data:
            for pos in self.ledger.positions.values():
                if pos.status == "settled":
                    self.selectivity_evidence.record(
                        self._selectivity_tags_from_pos(pos), won=bool(pos.won),
                        pnl=float(pos.pnl_usd or 0.0),
                        ev_after_cost=(pos.research or {}).get("ev_after_cost"),
                        outcome_up=pos.outcome_up)
        if self.grok_predictor is not None:
            self.grok_predictor.load_state(acct.get("grok_predictor") or {})
        if self.grok_analyst is not None:
            self.grok_analyst.load_state(acct.get("grok_analyst") or {})
        if self.grok_decider is not None:
            self.grok_decider.load_state(acct.get("grok_decider") or {})
        if self.grok_news is not None:
            self.grok_news.load_state(acct.get("grok_news") or {})
        self._grok_pending = list(acct.get("grok_pending") or [])
        self._verifier_pending = list(acct.get("verifier_pending") or [])
        self._recent_windows = list(acct.get("recent_windows") or [])
        self.lessons.load_state(acct.get("lessons") or {})
        self.trade_history.load_state(acct.get("trade_history") or {})
        if not self.trade_history.recent(1):
            self.trade_history.backfill_from_positions(list(self.ledger.positions.values()))
        if self.verifier is not None:
            self.verifier.load_state(acct.get("verifier") or {})
        if self.research_loop is not None:
            self.research_loop.load_state(acct.get("research_loop") or {})
        if self.edge_model is not None:          # the learned edge model accumulates across runs
            self.edge_model.load_state(acct.get("edge_model") or {})
        ev = acct.get("ev") or {}
        self._ev_before_sum = float(ev.get("before_sum", 0.0) or 0.0)
        self._ev_after_sum = float(ev.get("after_sum", 0.0) or 0.0)
        self._ev_n = int(ev.get("n", 0) or 0)
        if acct.get("baseline"):
            self._baseline = acct.get("baseline")
        _opens = acct.get("open_snapshots") or []
        if _opens:
            n = self.price.load_open_state(_opens)
            if n:
                logger.info("restored %d open snapshot(s) from disk", n)
        logger.info("pulse state restored: trades=%d settled=%d realized_pnl=%.3f calib_n=%d "
                    "lifecycle_created=%d", self.ledger.trades, self.ledger.settled,
                    self.ledger.realized_pnl, self.calib.n, self.reconciler.created)

    def _archive_prior_state(self) -> None:
        """Fresh-start: move the existing ledger aside so we begin from a clean baseline."""
        try:
            self._ledger_path.rename(
                self._data_dir / f"btc_pulse_ledger.archived_{int(time.time())}.json")
            logger.info("PULSE_FRESH_START set — archived prior ledger, starting fresh")
        except Exception:  # noqa: BLE001
            pass

    # -- one evaluation/trade/settle pass ----------------------------------- #
    def tick(self, now: Optional[float] = None) -> dict:
        now = float(now if now is not None else time.time())
        self.ticks += 1
        self.last_tick_ts = now
        self.loops.beat("heartbeat", now)      # liveness watchdog: main loop alive
        self.loops.beat("data_ingestion", now)
        self.price.poll(now)               # oracle: RTDS Chainlink ref price
        self.leads.poll(now)               # lead predictors (Binance/Coinbase) — features only
        if self.edge_signal is not None:   # feed the OBSERVE-ONLY CEX basket (lead feeds + extras)
            latest = getattr(self.leads, "_latest", {}) or {}
            prices = {"binance_btcusdt": ((latest.get("binance_btcusdt") or (None,))[0], "no_data"),
                      "coinbase_btcusd": ((latest.get("coinbase_btcusd") or (None,))[0], "no_data")}
            for name, fetch in self._cex_extra.items():
                try:
                    px = fetch()
                except Exception:  # noqa: BLE001 — an extra CEX feed never breaks a tick
                    px = None
                prices[name] = (px, "fetch_failed" if px is None else None)
            if not self._cex_extra:            # extras disabled -> mark missing reason
                for nm in ("kraken_btcusd", "bitstamp_btcusd"):
                    if nm in self.edge_signal.basket.buf:
                        prices[nm] = (None, "disabled_by_config")
            self.edge_signal.observe_prices(prices, now)
        if self.research is not None:
            self.research.observe_oracle(self.price.current())
        if self.signals is not None:
            self.signals.observe_price(self.price.current(), now)
        windows = self.market.active_windows(now=now)
        keep_keys = {w.event_id for w in windows} | set(self.ledger.positions)
        self.price.prune_opens(keep_keys)
        reasons: dict = {}
        evald = []
        # OBSERVE-ONLY external signal (TradingView): drain freshly-received alerts and compute the
        # latest signal feature for this tick. NEVER used by decide()/evaluate_execution().
        tv_feature = None
        if self.tradingview is not None:
            px_now = self.price.current()
            for ev in self.tradingview.drain_pending():   # build the per-symbol RSI alert history
                store_sym = self.tradingview._storage_symbol(ev.symbol)
                self._rsi_model.observe(symbol=store_sym, direction=ev.direction,
                                        ts=(ev.bar_time or ev.received_at))
                # B: ask Grok (async, off hot path) for P(up) given this signal + BTC context
                if self.grok_predictor is not None:
                    self.grok_predictor.request(ev.event_id, {
                        "signal": {"direction": ev.direction, "strength": ev.strength,
                                   "signal_level": ev.signal_level,
                                   "indicator": ev.indicator_name, "symbol": ev.symbol,
                                   "timeframe": ev.timeframe},
                        "btc_price": px_now, "sigma_per_sec": self.price.sigma_per_sec(now),
                        "regime": (self.overlay.current(now).get("regime")
                                   if self.overlay is not None else None),
                        "horizon_s": self.cfg.tradingview_signal_horizon_s})
                # schedule a forward-return eval for EVERY signal (traded or not) so the prediction
                # is built from the full signal history, not only windows the bot traded.
                if px_now is not None:
                    self._tv_pending.append({
                        "symbol": store_sym, "direction": ev.direction, "event_id": ev.event_id,
                        "state": self._rsi_model.trend(store_sym).get("state"),
                        "model_pred": self._rsi_model.predict(store_sym).get("prediction"),
                        "price0": float(px_now),
                        "due_ts": float(ev.bar_time or ev.received_at)
                        + self.cfg.tradingview_signal_horizon_s})
            self._evaluate_tv_forward_returns(now)
            feat = self.tradingview.latest_feature(now=now,
                                                   symbol=self.cfg.tradingview_feature_symbol)
            self.loops.beat("tradingview", now)
            if feat is not None and (feat.get("age_s") is None
                                     or feat["age_s"] <= self.cfg.tradingview_signal_max_feature_age_s):
                tv_feature = feat
                # attach Grok's observe-only P(up) for this signal if it has answered (fail-open)
                if self.grok_predictor is not None:
                    gp = self.grok_predictor.get(feat.get("event_id"))
                    if gp is not None:
                        tv_feature = {**feat, "grok_p_up": gp.get("p_up")}
        self._grade_grok_decisions(now)   # grade prior Grok decisions vs realized window close
        self._grade_verifier_decisions(now)  # counterfactual grade for vetoed (and shadow) setups
        self._grade_cex_lead(now)         # grade prior CEX-lead signals vs realized window close
        self._grade_market_benchmark(now) # grade model-vs-market accuracy (learning-blend gate)
        if self.arb_ledger is not None:   # settle risk-free arb positions at window close (deterministic)
            self.arb_ledger.settle_due(now)
        if self.dep_arb_ledger is not None:
            self.dep_arb_ledger.settle_due(now)
        ov = self.overlay.current(now) if self.overlay is not None else None
        ov_blackout = bool(ov and ov.get("blackout"))
        ov_vol_mult = float(ov.get("vol_multiplier", 1.0)) if ov else 1.0
        # verifiable stop conditions (agent-independent; refreshed each tick from ledger evidence)
        self.stop_monitor.refresh(
            directional_positions=list(self.ledger.positions.values()),
            arb_positions=(self.arb_ledger.positions if self.arb_ledger is not None else {}),
            directional_stats=self.ledger.stats(),
            arb_report=(self.arb_ledger.report() if self.arb_ledger is not None else {}),
            dep_positions=(self.dep_arb_ledger.positions
                           if self.dep_arb_ledger is not None else {}),
            dep_report=(self.dep_arb_ledger.report()
                        if self.dep_arb_ledger is not None else {}),
            starting_capital=self.cfg.starting_capital_usd)
        if getattr(self, "clob_feed", None) and windows:
            _tids = []
            for _w in windows:
                if _w.open_ts <= now < _w.close_ts:
                    _tids.extend([_w.up_token_id, _w.down_token_id])
            self.clob_feed.start_ws_background([t for t in _tids if t])
        self._scan_arbitrage_all_windows(windows, now)
        self._scan_dependency_arb(windows, now)
        _grok_news = ((self.grok_news.latest() if self.grok_news is not None else None) or {})

        def _bump(r):
            reasons[r] = reasons.get(r, 0) + 1

        def _finalize(dr, terminal, *, reason=None, stage=None):
            """Close a candidate in exactly one terminal state — no candidate disappears."""
            dr.finalize(terminal, reason=reason, stage=stage)
            self.reconciler.record(dr)
            evald.append(dr.to_dict())
            # count accepted/rejected outcomes for candidates that carried a TradingView signal
            if dr.external and (dr.external.get("source") == "tradingview"):
                self._tv_learner.record_candidate(dr.external.get("direction"),
                                                  accepted=(terminal == "accepted"))
            _bump(terminal if reason is None else f"{terminal}:{reason}")

        for w in windows:
            # snapshot the open price the moment the window begins
            self.price.snapshot_open(w.event_id, w.open_ts, now=now)
            if now < w.open_ts:
                _bump("not_open_yet")            # upcoming window — not a candidate yet
                continue
            if self.ledger.has_position(w.event_id):
                _bump("already_positioned")      # existing position — not a NEW candidate
                continue
            # ---- CANDIDATE CREATED (every open, non-positioned window) ----
            s_now = self.price.current()
            sigma = self.price.sigma_per_sec(now)
            snap = self.price.open_snapshot(w.event_id)
            ttc = w.seconds_to_close(now)
            mc = MarketContext(
                event_id=w.event_id, market_id=w.market_id, title=w.title,
                decision_id=w.event_id,          # canonical id == window key == ledger position key
                series_slug=getattr(w, "series_slug", SERIES_SLUG_5M),
                series_label=getattr(w, "series_label", "5m"),
                window_seconds=int(getattr(w, "window_seconds", 300) or 300),
                open_ts=w.open_ts, close_ts=w.close_ts, ttc_s=ttc,
                s_open=(snap.price if snap else None), s_now=s_now, sigma_per_sec=sigma,
                lead_prices={k: (v[0] if v else None)
                             for k, v in (getattr(self.leads, "_latest", {}) or {}).items()})
            dr = DecisionResult(market_context=mc,
                                candidate=CandidateDecision(None, None, None, 0.0, False, "pending"))
            dr.external = tv_feature          # OBSERVE-ONLY external signal (never trades/sizes)
            # early terminal classifications (each candidate ends classified)
            if ttc <= 0:
                _finalize(dr, "expired", reason="window_closed")
                continue
            if snap is None:
                _finalize(dr, "missing_data", reason="no_open_snapshot")
                continue
            _ws_lag = int(getattr(w, "window_seconds", 300) or 300)
            _max_lag = self.price.effective_max_open_lag(_ws_lag)
            if snap.lag_s > _max_lag:
                _finalize(dr, "skipped", reason="open_snapshot_late")
                continue
            if s_now is None or sigma is None:
                _finalize(dr, "missing_data", reason="no_price_or_vol")
                continue
            # FAIL-CLOSED on a stale oracle: never compute fair value / trade on an aged price.
            if not self.price.is_fresh(self.cfg.price_max_age_s, now):
                _finalize(dr, "skipped", reason="stale_price")
                continue
            if self.price.vol.samples < self.cfg.min_vol_samples \
                    or sigma <= self.cfg.sigma_trust_floor:
                _finalize(dr, "skipped", reason="untrusted_vol")
                continue
            if ov_blackout:
                _finalize(dr, "skipped", reason="grok_event_blackout")
                continue
            self.market.hydrate_books(w)
            mc.poly_yes = w.up_book.mid if w.up_book else None
            mc.best_bid = w.up_book.best_bid if w.up_book else None
            mc.best_ask = w.up_book.best_ask if w.up_book else None
            mc.spread = w.up_book.spread if w.up_book else None
            mc.ask_depth_usd = w.up_book.ask_depth_usd if w.up_book else None
            # ---- ARB-FIRST: scanned in _scan_arbitrage_all_windows (no vol/snapshot gate). ----
            if self.arb_ledger is not None:
                dr.arbitrage = self.arb_ledger.last_scan.get(w.event_id)
                if self.arb_ledger.has_arb(w.event_id):
                    _finalize(dr, "skipped", reason="arbitrage_taken")
                    continue
            # directional strategy can be disabled (arb runs standalone) — Loop-Eng scope lock
            if not self.cfg.directional_enabled:
                _finalize(dr, "skipped", reason="directional_disabled")
                continue
            if not self._directional_series_allowed(w):
                _finalize(dr, "skipped", reason="directional_series_not_allowed")
                continue
            if self.stop_monitor.is_halted("directional"):
                _finalize(dr, "skipped", reason="directional_stop_halted")
                continue
            # ---- entry-time features (computed BEFORE the decision so the bot's learned
            #      experience can inform it). These never place/size/bypass a trade themselves. ----
            rfeat = None
            if self.research is not None:
                cex_px = (getattr(self.leads, "_latest", {}) or {}).get(
                    "binance_btcusdt", (None,))[0]
                cex_implied = digital_p_up(cex_px, snap.price, sigma, ttc) if cex_px else None
                poly_yes = w.up_book.mid if w.up_book else None
                divergence = (poly_yes - cex_implied) if (poly_yes is not None
                                                          and cex_implied is not None) else None
                self.research.observe_divergence(divergence, cex_implied)
                rfeat = self.research.evaluate(current_divergence=divergence)
                dr.features = rfeat.to_dict()
                dr.mark("feature_scored")
            if self.signals is not None:
                self.signals.observe_poly(mc.poly_yes, mc.spread, mc.ask_depth_usd, now)
                dr.signals = self.signals.snapshot(ttc_s=ttc, now=now).to_dict()
            fsnap = None
            if self.factors is not None:
                from engine.pulse.factors import compute_factors
                _div = (dr.features or {}).get("divergence") if dr.features else None
                fsnap = compute_factors(
                    poly_yes=mc.poly_yes, spread=mc.spread, ask_depth_usd=mc.ask_depth_usd,
                    bid_depth_usd=(w.up_book.bid_depth_usd if w.up_book else None),
                    ttc_s=ttc, signal=dr.signals, divergence=_div,
                    overlay_regime=((ov or {}).get("regime") if ov else None))
                self.factors.observe(fsnap)
                dr.factors = fsnap.to_dict()
            cand_state = None
            if self.markov is not None:
                from engine.pulse.markov import classify_state
                from engine.pulse.decisions import RegimeSnapshot
                cand_state = classify_state(
                    hurst_regime=(rfeat.hurst_regime if rfeat else None),
                    signal_direction=(dr.signals or {}).get("direction"),
                    stale_factor=(fsnap.polymarket_stale_factor if fsnap else None),
                    settlement_boundary_risk=(fsnap.settlement_boundary_risk if fsnap else None),
                    spread=mc.spread, ask_depth_usd=mc.ask_depth_usd)
                self.markov.observe(cand_state)
                dr.regime = RegimeSnapshot(
                    state=cand_state, probs=self.markov.state_outputs(cand_state)).to_dict()
            # calibrated edge model: predict from entry-time features (the realized label trains
            # it later — no leakage). Reported via dr.model; used in the decision blend below.
            model_vec = None
            if self.edge_model is not None:
                from engine.pulse.edge_model import extract_features
                model_vec = extract_features(features=dr.features, signals=dr.signals,
                                             factors=dr.factors)
                dr.model = self.edge_model.predict(model_vec)
            # OBSERVE-ONLY BTC Pulse Edge Signal (CEX basket momentum + stale divergence + OB
            # pressure + pulse_edge_score). NEVER used by decide()/evaluate_execution().
            esnap = None
            if self.edge_signal is not None:
                _rv = (dr.features or {}).get("realized_vol") if dr.features else None
                esnap = self.edge_signal.snapshot(
                    now=now, poly_yes=mc.poly_yes, spread=mc.spread,
                    up_book=w.up_book, down_book=w.down_book, ttc_s=ttc,
                    hurst_regime=(rfeat.hurst_regime if rfeat else None), realized_vol=_rv,
                    tv_strength=(tv_feature or {}).get("strength"), size_usd=self.cfg.size_usd)
                dr.edge = esnap.to_dict()
            # ---- digital fair value, then the CLOSED-LOOP LEARNED-EDGE BLEND ----
            # the overlay can only RAISE sigma (>=1.0) -> more conservative P(up)
            fair = digital_p_up(s_now, snap.price, sigma * ov_vol_mult, ttc)
            fair_used = fair
            # ALWAYS grade the model's P(up) vs the MARKET price (poly_yes) per window so the blend
            # can self-gate on out-of-sample market-beating accuracy (independent of whether it's
            # currently active). Leakage-free: snapshot at decision, grade at window close.
            if (self.edge_model is not None and model_vec is not None and fair is not None
                    and mc.poly_yes is not None):
                _mp_grade = self.edge_model.decision_p_up(model_vec)
                if _mp_grade is not None:
                    self._schedule_market_benchmark(mc.decision_id, snap.price, w.close_ts,
                                                    _mp_grade, mc.poly_yes, fair)
            if (fair is not None and self.cfg.learning_enabled and self.edge_model is not None
                    and model_vec is not None):
                w_learn, why = self._learning_weight()
                mp = self.edge_model.decision_p_up(model_vec) if w_learn > 0 else None
                if mp is not None:
                    blended = min(0.99, max(0.01, (1.0 - w_learn) * fair + w_learn * mp))
                    dr.learning = {"applied": True, "weight": round(w_learn, 4),
                                   "digital_p_up": round(fair, 4), "model_p_up": round(mp, 4),
                                   "blended_p_up": round(blended, 4), "reason": why,
                                   "paper_only": True, "gate_still_authoritative": True}
                    fair_used = blended
                else:
                    dr.learning = {"applied": False, "weight": round(w_learn, 4), "reason": why}
            # ---- CEX-LEAD LATENCY EDGE (grade CEX-implied P(up) vs the MARKET price; PAPER ONLY) ----
            # cex_p_up uses the SAME sigma as fair, so its only difference from fair is the price
            # SOURCE (fresh CEX spot vs the bot's RTDS price) -> isolates the lead-lag. Graded vs the
            # realized close every window; in SHADOW it only measures; in GATED a Wilson-PROVEN bucket
            # may PROPOSE a side (still subject to the safety floor + execution gate below).
            cex_lead_drive = None
            if self.cex_lead is not None:
                cex_px_l = (getattr(self.leads, "_latest", {}) or {}).get(
                    "binance_btcusdt", (None,))[0]
                cex_p_up = (digital_p_up(cex_px_l, snap.price, sigma * ov_vol_mult, ttc)
                            if cex_px_l else None)
                # ORDERFLOW microstructure from the observe-only edge snapshot (short-horizon CEX
                # momentum direction, cross-exchange agreement, orderbook pressure) -> confirmation.
                _mom = (esnap.cex_momentum if esnap else {}) or {}
                _basket_dir = _mom.get("basket_direction")
                _agreement = _mom.get("exchange_agreement")
                _ob_imb = ((esnap.orderbook_pressure if esnap else {}) or {}).get("imbalance")
                # TradingView confirmation (direction + strength) — observe-only signal feed
                _tv_dir = (tv_feature or {}).get("direction")
                _tv_str = (tv_feature or {}).get("strength")
                # Grok news/X sentiment (mispricing confirmation via fresh context)
                _news = ((self.grok_news.latest() if self.grok_news is not None else None) or {})
                _news_sent = _news.get("sentiment")
                cl_sig = self.cex_lead.signal(cex_p_up=cex_p_up, poly_yes=mc.poly_yes,
                                              fair=fair_used, ttc_s=ttc, basket_direction=_basket_dir,
                                              exchange_agreement=_agreement, ob_imbalance=_ob_imb,
                                              tv_direction=_tv_dir, tv_strength=_tv_str,
                                              news_sentiment=_news_sent)
                dr.cex_lead = cl_sig
                if cl_sig.get("has_signal"):
                    self._schedule_cex_lead_grade(mc.decision_id, snap.price, w.close_ts, cl_sig)
                cex_lead_drive = self.cex_lead.decide(
                    cex_p_up=cex_p_up, poly_yes=mc.poly_yes, fair=fair_used, ttc_s=ttc,
                    basket_direction=_basket_dir, exchange_agreement=_agreement, ob_imbalance=_ob_imb,
                    tv_direction=_tv_dir, tv_strength=_tv_str, news_sentiment=_news_sent)
            # ---- GROK DECISION ENGINE ("Grok decides, bot executes"; PAPER ONLY) ----
            # Request one decision per window (async, off the tick loop), record it observe-only, and
            # schedule a grade vs the realized close (traded or not). In SHADOW mode this is the only
            # effect. In FOLLOW mode the decision drives side/size below, subject to the floor.
            grok_dec = None
            grok_size_frac = 1.0
            grok_verdict = None
            if self.grok_decider is not None:
                self.loops.beat("signal_generation", now)
                _grok_bundle = self._grok_decision_bundle(mc, dr, w, fair_used, ttc, tv_feature)
                _refresh = self._grok_refresh_token(mc.decision_id, _grok_bundle, ttc=ttc,
                                                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                from engine.pulse.grok_bundle import (classify_grok_compute_tier,
                                                      compact_bundle_for_light_tier)
                _tier = classify_grok_compute_tier(
                    _grok_bundle, refresh_token=_refresh,
                    tiered_enabled=self.cfg.grok_tiered_compute_enabled,
                    full_divergence_min=self.cfg.grok_tier_full_divergence_min,
                    deep_divergence_min=self.cfg.grok_tier_deep_divergence_min)
                _grok_bundle["grok_compute_tier"] = _tier
                if _tier == "light":
                    _grok_bundle = compact_bundle_for_light_tier(_grok_bundle)
                self.grok_decider.request(
                    mc.decision_id,
                    _grok_bundle,
                    context=self._grok_decision_context(dr.features, cand_state, ttc, fair_used),
                    refresh_token=_refresh)
                grok_dec = self.grok_decider.get(mc.decision_id)
                dr.grok_decision = grok_dec
                if grok_dec is not None:
                    self._schedule_grok_grade(mc.decision_id, snap.price, w.close_ts, grok_dec)
                    if self.verifier is not None:
                        self.loops.beat("verifier", now)
                        self.verifier.request(mc.decision_id, {
                            "decision": {k: grok_dec.get(k) for k in
                                         ("action", "p_up", "confidence", "size_fraction",
                                          "rationale")},
                            "context": grok_dec.get("context"),
                            "payoff": {"up_ask": (w.up_book.best_ask if w.up_book else None),
                                       "down_ask": (w.down_book.best_ask if w.down_book else None),
                                       "min_reward_risk": self.cfg.min_reward_risk},
                            "digital_fair_p_up": fair_used, "poly_yes": mc.poly_yes,
                            # mispricing context for the checker: divergence + the CEX-lead signal +
                            # the proof the bot's model is worse than the market (veto if edge<costs)
                            "fair_minus_poly": (round(float(fair_used) - float(mc.poly_yes), 4)
                                                if (fair_used is not None and mc.poly_yes is not None)
                                                else None),
                            "cex_lead_mispricing": {k: (dr.cex_lead or {}).get(k) for k in
                                                    ("divergence", "side", "confirmed",
                                                     "tv_confirms", "late_decisive")},
                            "edge_signal": {k: (dr.edge or {}).get(k) for k in
                                            ("stale_divergence_class", "pulse_edge_score_bucket",
                                             "ttc_bucket", "cex_agreement_bucket")},
                            "model_vs_market": self._market_benchmark(),
                            "recent_windows": self._recent_windows_view(6),
                            "lessons": self.lessons.recent(10),
                            "view_accuracy": self.grok_decider.report().get("view_accuracy")})
                self._maybe_schedule_verifier_counterfactual(
                    mc, w, snap, grok_dec, acted=False)
            # FOLLOW only when: mode=follow, breaker not tripped, and this window is in the A/B canary
            # follow-fraction. Otherwise fall through to the baseline path (the A/B control arm).
            grok_follow = False
            cex_lead_active = False
            if self.cfg.grok_decider_mode == "follow" and self.grok_decider is not None:
                ok, _br = self.grok_decider.should_follow(now)
                grok_follow = ok and self._follow_canary(mc.decision_id)
            # the directional decision uses the (possibly learning-adjusted) probability; the
            # STRICT execution gate below is UNCHANGED and remains the sole trade authority.
            if grok_follow:
                # FOLLOW: act on Grok; opinion gates bypassed, only the deterministic floor
                # (freshness, max_price, execution realism, caps, breaker) may abstain. When Grok is
                # actionable -> follow its action. When it ABSTAINS, EXPLORE its directional VIEW
                # (p_up) at a capped rate so the bot keeps trading + gathers action-level P&L data
                # (paper; breaker-protected) instead of freezing.
                actionable = self.grok_decider.is_actionable(grok_dec, now=now)
                # ADAPTIVE SELF-IMPROVEMENT: when Grok abstains, consult the live per-context policy.
                pol = ({"mode": "explore"} if (not self.cfg.grok_decider_adaptive or grok_dec is None
                                               or grok_dec.get("p_up") is None or actionable)
                       else self.grok_decider.context_policy(grok_dec.get("context") or {}))
                exploit = (pol["mode"] == "exploit")           # proven-edge context -> act on view
                # self-tuning: aggression raises the exploration rate as acted trades turn profitable
                eff_explore_rate = self.grok_decider.aggr.effective_explore_rate(
                    self.cfg.grok_decider_explore_rate)
                if not self._grok_up_side_allowed():
                    eff_explore_rate = 0.0
                _pu_view = (float(grok_dec.get("p_up"))
                            if grok_dec is not None and grok_dec.get("p_up") is not None else None)
                _view_margin = (abs(_pu_view - 0.5) if _pu_view is not None else 0.0)
                explore = (not actionable and not exploit and grok_dec is not None
                           and _pu_view is not None and pol["mode"] != "avoid"
                           and _view_margin >= float(self.cfg.grok_decider_explore_min_view_margin)
                           and eff_explore_rate > 0.0
                           and self._grok_rng.random() < eff_explore_rate)
                misprice_entry = (None if (actionable or exploit or explore)
                                  else self._mispricing_follow_entry(
                                      dr.cex_lead, ttc, esnap, tv_feature))
                if not actionable and not exploit and not explore and misprice_entry is None:
                    if pol["mode"] == "avoid":
                        self._grok_policy_counts["avoid"] += 1
                    reason = ("grok_avoid_proven_bad" if pol["mode"] == "avoid"
                              else ("grok_explore_view_too_weak"
                                    if (not actionable and not exploit and grok_dec is not None
                                        and _pu_view is not None
                                        and _view_margin < float(
                                            self.cfg.grok_decider_explore_min_view_margin))
                                    else ("grok_no_decision" if not grok_dec
                                          else ("grok_abstain" if grok_dec.get("action") == "no_trade"
                                                else "grok_low_confidence_or_stale"))))
                    dr.candidate = CandidateDecision(side=None, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=reason, stage="grok_decider")
                    continue
                if misprice_entry is not None:
                    side = misprice_entry["side"]
                    entry_mode = "mispricing_follow"
                    grok_oprob = misprice_entry["p_win"]
                    grok_size_frac = misprice_entry["size_frac"]
                    self._mispricing_gate_counts["mispricing_follow_on_abstain"] = (
                        self._mispricing_gate_counts.get("mispricing_follow_on_abstain", 0) + 1)
                elif actionable:
                    side = grok_dec["action"]
                    entry_mode = "grok_follow"
                    # EV uses the side-aligned P(win) = p_up (up) / 1-p_up (down), NOT raw confidence
                    # (confidence is 'how sure', not the win probability the EV gate needs).
                    _pu = grok_dec.get("p_up")
                    if _pu is not None:
                        grok_oprob = float(_pu) if side == "up" else (1.0 - float(_pu))
                    else:
                        grok_oprob = float(grok_dec.get("confidence") or 0.5)
                    grok_size_frac = max(0.0, min(1.0, float(grok_dec.get("size_fraction") or 1.0)))
                elif exploit:                      # proven-edge context: act on Grok's view, sized up
                    pu = float(grok_dec.get("p_up"))
                    side = "up" if pu >= 0.5 else "down"
                    entry_mode = "grok_adaptive"
                    grok_oprob = pu if side == "up" else (1.0 - pu)
                    grok_size_frac = max(0.0, min(1.0, 0.5 * float(pol.get("size_mult") or 1.0)
                                                  * self.grok_decider.aggr.size_scale()))
                    self._grok_policy_counts["exploit"] += 1
                else:                              # exploration trade on Grok's directional view
                    pu = float(grok_dec.get("p_up"))
                    side = "up" if pu >= 0.5 else "down"
                    entry_mode = "grok_explore"
                    grok_oprob = pu if side == "up" else (1.0 - pu)
                    grok_size_frac = max(0.0, min(1.0,
                                                  float(self.cfg.grok_decider_explore_size_fraction)
                                                  * self.grok_decider.aggr.size_scale()))
                    self._grok_policy_counts["explore"] += 1
                up_blk, up_reason = self._directional_up_blocked(side)
                if up_blk:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=up_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=up_reason, stage="directional")
                    continue
                if (entry_mode != "mispricing_follow" and side == "up" and grok_oprob is not None
                        and float(grok_oprob) < float(self.cfg.grok_up_min_p_win)):
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False,
                                                     reason="grok_up_p_win_too_low")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="grok_up_p_win_too_low", stage="grok_decider")
                    continue
                mp_ok, mp_reason = self._mispricing_gate_ok(
                    side=side, cex_sig=dr.cex_lead, ttc_s=ttc, esnap=esnap,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                if not mp_ok:
                    self._mispricing_gate_counts[mp_reason] = (
                        self._mispricing_gate_counts.get(mp_reason, 0) + 1)
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=mp_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=mp_reason, stage="mispricing_gate")
                    continue
                et_ok, et_reason = self._edge_ttc_gate_ok(
                    esnap=esnap, ttc_s=ttc,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                if not et_ok:
                    self._mispricing_gate_counts[et_reason] = (
                        self._mispricing_gate_counts.get(et_reason, 0) + 1)
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=et_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=et_reason, stage="mispricing_gate")
                    continue
                if (entry_mode != "mispricing_follow" and side == "up"
                        and not self._grok_up_side_allowed()):
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="grok_no_edge_up")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="grok_no_edge_up", stage="grok_decider")
                    continue
                if side == "up":
                    up_tv_ok, up_tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
                    if not up_tv_ok:
                        dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                         outcome_prob=None, model_edge=0.0,
                                                         tradeable=False, reason=up_tv_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=up_tv_reason, stage="grok_decider")
                        continue
                # Restrict-only DOWN/TV asymmetry gate applies to all Grok-owned UP trades.
                if side == "up":
                    _up_book = w.up_book
                    _up_ask = _up_book.best_ask if _up_book else None
                    db_res = self._down_bias_eval(side=side, tv_feature=tv_feature,
                                                  markov_state=cand_state, ttc_s=ttc, esnap=esnap,
                                                  fair_p_up=fair_used,
                                                  zscore_bucket=(rfeat.zscore_bucket if rfeat else None),
                                                  confidence_tier=self._entry_confidence_tier(dr),
                                                  ask_price=_up_ask)
                    if db_res["decision"] in ("block", "explore"):
                        dr.down_bias_gate = {"decision": db_res["decision"],
                                             "reasons": db_res["reasons"]}
                        dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                         outcome_prob=None, model_edge=0.0,
                                                         tradeable=False,
                                                         reason=db_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=db_res["reasons"][0],
                                  stage="down_bias_gate")
                        continue
                # #1 MAKER-CHECKER: veto/shrink Grok-owned trades. When the mispricing gate is ON,
                # CEX-stack alignment already validated the entry — skip the Grok-opinion verifier.
                grok_verdict = None
                if (self.verifier is not None and not self.cfg.mispricing_gate_enabled):
                    if self.cfg.verifier_follow_require_verdict:
                        # fail-CLOSED on a pending verdict: WAIT for the real maker-checker (it's
                        # cached per decision_id, so a later tick on this same window proceeds).
                        grok_verdict = self.verifier.get(mc.decision_id) or {
                            "approve": False, "pending": True, "reason": "verifier_pending"}
                    else:
                        grok_verdict = self.verifier.verdict_or_failopen(mc.decision_id)
                    if not grok_verdict.get("approve"):
                        vr = "verifier_pending" if grok_verdict.get("pending") else "verifier_veto"
                        if not grok_verdict.get("pending"):
                            book_v = w.up_book if side == "up" else w.down_book
                            ask_v = book_v.best_ask if book_v else None
                            self._maybe_schedule_verifier_counterfactual(
                                mc, w, snap, grok_dec, side=side, entry_ask=ask_v,
                                size_frac=grok_size_frac, acted=False)
                        dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                         outcome_prob=None, model_edge=0.0,
                                                         tradeable=False, reason=vr)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=vr, stage="verifier")
                        continue
                    grok_size_frac = min(grok_size_frac,
                                         float(grok_verdict.get("max_size_fraction") or 1.0))
                book = w.up_book if side == "up" else w.down_book
                ask = book.best_ask if book else None
                cap = min(self.cfg.max_price, grok_dec.get("max_price") or self.cfg.max_price)
                if ask is None:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="no_tradeable_ask")
                    _finalize(dr, "rejected", reason="no_tradeable_ask", stage="grok_decider")
                    continue
                if float(ask) > cap:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="grok_max_price")
                    _finalize(dr, "rejected", reason="grok_max_price", stage="grok_decider")
                    continue
                ex_ok, ex_reason = self._executable_mispricing_ok(p_win=grok_oprob, ask=float(ask))
                if not ex_ok:
                    self._mispricing_gate_counts[ex_reason] = (
                        self._mispricing_gate_counts.get(ex_reason, 0) + 1)
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=ex_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=ex_reason, stage="mispricing_gate")
                    continue
                if (entry_mode != "mispricing_follow"
                        and not self._ask_reward_risk_ok(side, float(ask))):
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="reward_risk_too_low")
                    self._payoff_guard_counts["rejected_bad_reward_to_risk"] += 1
                    _finalize(dr, "rejected", reason="reward_risk_too_low", stage="grok_decider")
                    continue
                from engine.pulse.strategy import PulseDecision
                d = PulseDecision(trade=True, side=side,
                                  token_id=(w.up_token_id if side == "up" else w.down_token_id),
                                  price=float(ask), fair_p_up=fair_used, edge=0.0, reason=entry_mode)
                context_explored = False
            elif cex_lead_drive is not None:
                # CEX-LEAD DRIVE: a Wilson-PROVEN divergence bucket proposes the side. Opinion gates
                # bypassed (the proven edge owns the direction); the safety floor (selectivity +
                # calibration + EV gate + caps + breaker) below still applies and stays authoritative.
                cex_lead_active = True
                side = cex_lead_drive["side"]
                up_blk, up_blk_reason = self._directional_up_blocked(side)
                if up_blk:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason=up_blk_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=up_blk_reason, stage="directional")
                    continue
                if side == "up":
                    up_ok, up_reason = self._up_side_tv_bias_ok(
                        tv_feature, ttc_s=ttc, markov_state=cand_state,
                        esnap=esnap, fair_p_up=fair_used, dr=dr, rfeat=rfeat)
                    if not up_ok:
                        dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                         outcome_prob=None, model_edge=0.0,
                                                         tradeable=False, reason=up_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=up_reason, stage="down_bias_gate")
                        continue
                entry_mode = ("cex_lead_late" if cex_lead_drive.get("late_decisive") else "cex_lead")
                cex_oprob = float(cex_lead_drive["outcome_prob"])
                # D: edge-scaled (fractional-Kelly) sizing for the proven edge, clamped to a sane band
                grok_size_frac = max(0.25, min(self.cfg.cex_lead_max_size_frac,
                                               float(cex_lead_drive.get("size_frac") or 1.0)))
                book = w.up_book if side == "up" else w.down_book
                ask = book.best_ask if book else None
                if ask is None:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="no_tradeable_ask")
                    _finalize(dr, "rejected", reason="no_tradeable_ask", stage="cex_lead")
                    continue
                if float(ask) > self.cfg.max_price:
                    dr.candidate = CandidateDecision(side=side, fair_p_up=fair_used,
                                                     outcome_prob=None, model_edge=0.0,
                                                     tradeable=False, reason="cex_lead_max_price")
                    _finalize(dr, "rejected", reason="cex_lead_max_price", stage="cex_lead")
                    continue
                from engine.pulse.strategy import PulseDecision
                d = PulseDecision(trade=True, side=side,
                                  token_id=(w.up_token_id if side == "up" else w.down_token_id),
                                  price=float(ask), fair_p_up=fair_used, edge=0.0, reason=entry_mode)
                context_explored = False
            else:
                _up_rr = self._reward_risk_floor("up")
                _force_side = ("down" if self.cfg.directional_down_only else None)
                _ws = int(getattr(w, "window_seconds", 300) or 300)
                from engine.pulse.tv_confidence_tier import (
                    params_from_engine_cfg,
                    resolve_tv_entry_params,
                )
                _proposed_side = _force_side or "down"
                _tv_tier_snap = resolve_tv_entry_params(
                    side=_proposed_side,
                    tv_feature=tv_feature,
                    ttc_s=ttc,
                    window_seconds=_ws,
                    base_min_edge=self.cfg.min_edge,
                    base_max_price=self.cfg.max_price,
                    params=params_from_engine_cfg(self.cfg),
                )
                dr.tv_confidence_tier = _tv_tier_snap
                _eff_min_edge = float(_tv_tier_snap.get("min_edge") or self.cfg.min_edge)
                _eff_max_price = float(_tv_tier_snap.get("max_price") or self.cfg.max_price)
                _tier_key = str(_tv_tier_snap.get("tier") or "base")
                self._tv_tier_counts[_tier_key] = self._tv_tier_counts.get(_tier_key, 0) + 1
                d = decide(w, fair_used, now, min_edge=_eff_min_edge,
                           min_seconds_to_close=self.cfg.min_seconds_to_close,
                           min_depth_usd=self.cfg.min_depth_usd,
                           edge_buffer=self.cfg.edge_buffer, max_price=_eff_max_price,
                           min_seconds_since_open=self.cfg.min_seconds_since_open,
                           basis_buffer=self.cfg.basis_buffer,
                           min_reward_risk=self.cfg.min_reward_risk,
                           min_reward_risk_up=_up_rr if _up_rr > float(self.cfg.min_reward_risk or 0)
                           else None,
                           force_side=_force_side)
            if grok_follow:
                outcome_prob = grok_oprob              # Grok's P(chosen side wins) (action or view)
            elif cex_lead_active:
                outcome_prob = cex_oprob               # CEX-lead P(chosen side wins) on a proven bucket
            else:
                outcome_prob = (fair_used if d.side == "up" else (1.0 - fair_used)) \
                    if fair_used is not None else None
            dr.candidate = CandidateDecision(side=d.side, fair_p_up=fair_used,
                                             outcome_prob=outcome_prob, model_edge=d.edge,
                                             tradeable=d.trade, reason=d.reason)
            if not d.trade:
                if d.reason == "reward_risk_too_low":
                    self._payoff_guard_counts["rejected_bad_reward_to_risk"] += 1
                elif d.reason == "edge_below_min" and d.price is not None and float(d.price) >= 0.80:
                    self._payoff_guard_counts["rejected_tiny_upside"] += 1
                dr.action = RejectAction(stage="directional", reason=d.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=d.reason, stage="directional")
                continue
            up_blk, up_reason = self._directional_up_blocked(d.side)
            if up_blk:
                dr.action = RejectAction(stage="directional", reason=up_reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=up_reason, stage="directional")
                continue
            # --- quant OPINION gates (TV-signal / context / late-window / selectivity). These are
            # the quant's directional opinion; in FOLLOW / CEX-LEAD-DRIVE mode the direction is owned
            # by the proven driver so they are bypassed. The deterministic FLOOR (selectivity +
            # calibration + execution-quality gate + caps) below still applies in every mode.
            if not grok_follow and not cex_lead_active:
                cohort_ok, cohort_reason = self._baseline_quant_cohort_ok(
                    side=d.side, esnap=esnap, ttc_s=ttc, tv_feature=tv_feature,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300),
                    ask_price=d.price)
                if not cohort_ok:
                    self._baseline_cohort_gate_counts[cohort_reason] = (
                        self._baseline_cohort_gate_counts.get(cohort_reason, 0) + 1)
                    dr.action = RejectAction(stage="baseline_cohort_gate", reason=cohort_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=cohort_reason, stage="baseline_cohort_gate")
                    continue
            if (self.cfg.directional_up_restrictions_enabled
                    and not grok_follow and not cex_lead_active and d.side == "up"
                    and not self._grok_up_side_allowed()):
                dr.action = RejectAction(stage="grok_decider", reason="grok_no_edge_up")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason="grok_no_edge_up", stage="grok_decider")
                continue
            if (self.cfg.directional_up_restrictions_enabled
                    and not grok_follow and not cex_lead_active and d.side == "up"):
                up_tv_ok, up_tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
                if not up_tv_ok:
                    dr.action = RejectAction(stage="directional", reason=up_tv_reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=up_tv_reason, stage="directional")
                    continue
            green_path = False
            if not grok_follow and not cex_lead_active:
                green_path = self._green_path_active(
                    side=d.side,
                    window_seconds=int(getattr(w, "window_seconds", 300) or 300))
                if green_path:
                    dr.green_path = {
                        "active": True,
                        "skipped": ["tv_signal", "context", "down_bias", "late_window",
                                      "down_tv_dup", "mtf_gate"],
                    }
                elif d.side == "down":
                    down_tv_ok, down_tv_reason = self._baseline_down_tv_context_ok(tv_feature)
                    if not down_tv_ok:
                        dr.action = RejectAction(stage="directional", reason=down_tv_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=down_tv_reason, stage="directional")
                        continue
                if not green_path:
                    tv_reason = self._tv_signal_gate(tv_feature, d.side)
                    if tv_reason is not None:
                        dr.action = RejectAction(stage="directional", reason=tv_reason)
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=tv_reason, stage="directional")
                        continue
                    ctx_res = self.tv_context_gate.evaluate(
                        volume_state=(tv_feature or {}).get("volume_state"),
                        hurst_regime=(rfeat.hurst_regime if rfeat else None), ttc_s=ttc,
                        liquidation_spike=(tv_feature or {}).get("liquidation_spike"),
                        event_blackout=(tv_feature or {}).get("event_blackout"),
                        grok_event_risk=_grok_news.get("event_risk"))
                    dr.context_gate = {"decision": ctx_res["decision"], "reasons": ctx_res["reasons"]}
                    if ctx_res["decision"] == "block":
                        dr.action = RejectAction(stage="context_gate", reason=ctx_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=ctx_res["reasons"][0], stage="context_gate")
                        continue
                    context_explored = (ctx_res["decision"] == "explore")
                    db_res = self._down_bias_eval(side=d.side, tv_feature=tv_feature,
                                                  markov_state=cand_state, ttc_s=ttc, esnap=esnap,
                                                  fair_p_up=fair_used,
                                                  zscore_bucket=(rfeat.zscore_bucket if rfeat else None),
                                                  confidence_tier=self._entry_confidence_tier(dr),
                                                  ask_price=d.price)
                    dr.down_bias_gate = {"decision": db_res["decision"], "reasons": db_res["reasons"]}
                    db_block = (db_res["decision"] == "block"
                                or (d.side == "up" and db_res["decision"] == "explore"))
                    if db_block:
                        dr.action = RejectAction(stage="down_bias_gate", reason=db_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=db_res["reasons"][0], stage="down_bias_gate")
                        continue
                if green_path or not self.cfg.tv_mtf_conflict_gate_enabled:
                    dr.mtf_gate = {
                        "decision": "pass",
                        "reasons": [],
                        "observe_only": True,
                        "tf_confirm": (tv_feature or {}).get("tf_confirm"),
                        "tf_confirm_direction": (tv_feature or {}).get("tf_confirm_direction"),
                        "tf_confirm_mtf": (tv_feature or {}).get("tf_confirm_mtf"),
                        "mtf_timeframes": (tv_feature or {}).get("mtf_timeframes"),
                        "trend_by_tf": (tv_feature or {}).get("trend_by_tf"),
                    }
                else:
                    mtf_res = self.tv_mtf_gate.evaluate(
                        tf_confirm=(tv_feature or {}).get("tf_confirm"),
                        tf_confirm_direction=(tv_feature or {}).get("tf_confirm_direction"),
                        tf_confirm_mtf=(tv_feature or {}).get("tf_confirm_mtf"),
                        mtf_count=(tv_feature or {}).get("mtf_count"),
                        trend_fresh_count=(tv_feature or {}).get("trend_fresh_count"),
                        side=d.side)
                    dr.mtf_gate = {"decision": mtf_res["decision"], "reasons": mtf_res["reasons"],
                                   "tf_confirm": (tv_feature or {}).get("tf_confirm"),
                                   "tf_confirm_direction": (tv_feature or {}).get("tf_confirm_direction"),
                                   "mtf_timeframes": (tv_feature or {}).get("mtf_timeframes"),
                                   "tf_confirm_mtf": (tv_feature or {}).get("tf_confirm_mtf"),
                                   "trend_by_tf": (tv_feature or {}).get("trend_by_tf")}
                    if mtf_res["decision"] == "block":
                        dr.action = RejectAction(stage="mtf_gate", reason=mtf_res["reasons"][0])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=mtf_res["reasons"][0], stage="mtf_gate")
                        continue
                if green_path:
                    entry_mode = "green_path"
                else:
                    lw_res = self.late_window_gate.evaluate(ttc_s=ttc, p_up=fair_used)
                    dr.late_window = {"decision": lw_res["decision"], "reason": lw_res["reason"],
                                      "conviction": lw_res["conviction"], "late": lw_res["late"],
                                      "high_conviction": lw_res["high_conviction"]}
                    if lw_res["decision"] == "reject":
                        dr.action = RejectAction(stage="late_window_gate", reason=lw_res["reason"])
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason=lw_res["reason"], stage="late_window_gate")
                        continue
                    entry_mode = ("late_window" if (lw_res["late"] and lw_res["high_conviction"])
                                  else "standard")
                    if (self.cfg.directional_up_restrictions_enabled
                            and entry_mode == "late_window" and d.side == "up"):
                        dr.action = RejectAction(stage="late_window_gate",
                                                 reason="late_window_up_blocked")
                        if self.markov is not None:
                            self.markov.record_terminal(state=cand_state, accepted=False)
                        _finalize(dr, "rejected", reason="late_window_up_blocked",
                                  stage="late_window_gate")
                        continue
            # SAFETY FLOOR (ALL MODES incl. grok-follow): proven-loss selectivity block + probability
            # CALIBRATION. Blocking a statistically-proven losing bucket and de-biasing an OVER-
            # CONFIDENT probability are SAFETY/realism, not directional opinion, so they apply even
            # when Grok owns the side. Both can only make the bot MORE selective / LESS over-confident
            # — they never create, force, resize-up, or fast-track a trade, and the execution gate
            # remains authoritative. Buckets below min_samples are untouched, so unproven contexts are
            # still explored (cold-start). This is the fix for the negative-expectancy bleed: the
            # model claimed ~+0.11 EV/share while realized win-rate was ~0.52, so over-priced
            # favourites in proven-flat/losing buckets used to pass a zero EV floor.
            sel_tags = {
                "market_series": getattr(w, "series_label", mc.series_label),
                "hurst_regime": (rfeat.hurst_regime if rfeat else None),
                "zscore_bucket": (rfeat.zscore_bucket if rfeat else None),
                "ttc_bucket": ttc_bucket(ttc),
                "confidence_tier": _confidence_tier((dr.model or {}).get("model_confidence")
                                                    if (dr.model or {}).get("trained")
                                                    else (dr.signals or {}).get("confidence")),
                "spread_bucket": _spread_bucket(mc.spread),
                "depth_bucket": _depth_bucket(mc.ask_depth_usd),
                "markov_state": cand_state,
                "edge_quality_bucket": (fsnap.edge_quality_bucket if fsnap else None),
                "stale_divergence": (esnap.stale_divergence_class if esnap else None),
                "direction": d.side}
            from engine.pulse.selectivity import calibrate_fair, calibrate_chosen_prob
            raw_fp, cal_fp, cal_diag = calibrate_fair(
                fair, sel_tags, self.selectivity_evidence,
                min_samples=self.cfg.calibration_min_samples,
                max_shrink=self.cfg.calibration_max_shrink)
            # de-bias the probability the EV gate will actually use toward the bucket's REALIZED
            # win-rate so the model's over-claimed edge cannot pass the EV floor in proven contexts.
            raw_op, cal_op, op_diag = calibrate_chosen_prob(
                outcome_prob, sel_tags, self.selectivity_evidence,
                min_samples=self.cfg.calibration_min_samples,
                max_shrink=self.cfg.calibration_max_shrink)
            gate_outcome_prob = cal_op if cal_op is not None else outcome_prob
            dr.calibration = {"raw_fair_p_up": raw_fp, "calibrated_fair_p_up": cal_fp,
                              "diag": cal_diag, "raw_outcome_prob": raw_op,
                              "calibrated_outcome_prob": cal_op, "outcome_prob_diag": op_diag}
            # RESEARCH AUTO-APPLY (self-improving loop): hard-block contexts the Claude research loop
            # flagged as proven-losing. Safety-only / more-selective. Exempt the proven CEX-lead edge.
            if self.cfg.research_auto_apply and not cex_lead_active:
                ra_hit = self._research_avoid_hit(sel_tags)
                if ra_hit is not None:
                    reason = "research_avoid:" + ra_hit
                    dr.action = RejectAction(stage="research_avoid", reason=reason)
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason=reason, stage="research_avoid")
                    continue
            # DIRECTIONAL ALLOWLIST (de-risk): the directional model is structurally negative-EV in a
            # near-efficient market, so only take a directional trade in a CONFIDENTLY-WINNING bucket
            # (Wilson lower-bound > breakeven, n>=min). Pre-execution BLOCK, not advisory. Driven
            # strategies (grok-follow / cex-lead) and arb are exempt (they have their own proof).
            if (self.cfg.directional_require_winning_bucket and not grok_follow
                    and not cex_lead_active
                    and (not self._any_winning_bucket(sel_tags)
                         or not self._directional_market_benchmark_ok())):
                # cold-start carve-out: let a small capped fraction through as EXPLORATION so the
                # bot keeps trading + learning (otherwise it deadlocks — no trades => no bucket can
                # ever be proven-winning => permanent block => looks frozen). The rest stay blocked.
                if self._allowlist_rng.random() >= float(self.cfg.directional_explore_rate):
                    self._allowlist_blocked += 1
                    dr.action = RejectAction(stage="directional_allowlist",
                                             reason="no_proven_winning_bucket")
                    if self.markov is not None:
                        self.markov.record_terminal(state=cand_state, accepted=False)
                    _finalize(dr, "rejected", reason="no_proven_winning_bucket",
                              stage="directional_allowlist")
                    continue
                self._allowlist_explored += 1   # kept active for learning (exploration trade)
            gate_res = self.selectivity_gate.evaluate(sel_tags, self.selectivity_evidence)
            dr.selectivity = {"decision": gate_res["decision"], "reasons": gate_res["reasons"],
                              "bad_buckets": gate_res["bad_buckets"]}
            if gate_res["decision"] == "reject":
                dr.action = RejectAction(stage="selectivity_gate", reason=gate_res["reasons"][0])
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=gate_res["reasons"][0], stage="selectivity_gate")
                continue
            if grok_follow:
                gate_decision = ("grok_follow_explored" if gate_res["decision"] == "explore"
                                 else "grok_follow")
            elif cex_lead_active:
                gate_decision = ("cex_lead_explored" if gate_res["decision"] == "explore"
                                 else "cex_lead")
            else:
                gate_decision = "explored" if gate_res["decision"] == "explore" else "passed"
            # B (EXPLOIT side): size UP a proven-winning research exploit-context (baseline opinion
            # path only; capped). The execution gate + caps below remain authoritative.
            if (not grok_follow and not cex_lead_active and self.cfg.research_auto_apply
                    and not self.cfg.research_forbid_size_increase
                    and self._research_exploit_hit(sel_tags)):
                grok_size_frac = min(self.cfg.cex_lead_max_size_frac,
                                     grok_size_frac * self.cfg.research_exploit_size_mult)
                gate_decision = "exploit_" + gate_decision
            elif (self.cfg.research_forbid_size_increase
                  and self._research_exploit_hit(sel_tags)):
                gate_decision = "exploit_blocked_size_" + gate_decision
            # Execution-realistic edge block (Roan Part IV) + margin-based high-entry guard.
            book = w.up_book if d.side == "up" else w.down_book
            from engine.pulse.execution_realistic import (compute_candidate_edge,
                                                          high_entry_margin_reject)
            edge_block = compute_candidate_edge(
                side=d.side, raw_fair_p=raw_fp, calibrated_fair_p=cal_fp,
                market_price=mc.poly_yes, outcome_prob=gate_outcome_prob, book=book,
                size_usd=round(self.cfg.size_usd * grok_size_frac, 2),
                up_book=w.up_book, down_book=w.down_book)
            dr.execution_realistic = edge_block
            self._exec_realistic_samples.append(edge_block)
            if len(self._exec_realistic_samples) > 200:
                self._exec_realistic_samples = self._exec_realistic_samples[-200:]
            self._last_simplex = edge_block.get("simplex") or {}
            hre = high_entry_margin_reject(
                ask=(book.best_ask if book else d.price),
                calibrated_prob=gate_outcome_prob,
                min_margin=max(0.04, self.cfg.min_edge),
            )
            if hre and not cex_lead_active:
                self._payoff_guard_counts["rejected_high_entry_insufficient_margin"] += 1
                dr.action = RejectAction(stage="execution_realistic", reason=hre)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=hre, stage="execution_realistic")
                continue
            # STRICT execution-quality gate (AUTHORITATIVE): EV from the live ask-ladder VWAP, using
            # the CALIBRATED probability so the floor reflects realized edge, not the model's claim.
            # Mispricing-follow buys the CEX-indicated (often underdog) side; waive the favourite
            # floor the same way as Wilson-proven cex-lead drive entries.
            _waive_underdog_floor = cex_lead_active or entry_mode == "mispricing_follow"
            ex = evaluate_execution(
                side=d.side, book=book, outcome_prob=gate_outcome_prob,
                size_usd=round(self.cfg.size_usd * grok_size_frac, 2),
                tick_size=w.tick_size, ttc_s=ttc,
                min_seconds_to_close=self.cfg.min_seconds_to_close,
                max_spread=self.cfg.exec_max_spread, min_depth_usd=self.cfg.min_depth_usd,
                min_order_usd=self.cfg.exec_min_order_usd,
                max_depth_consume_frac=self.cfg.exec_max_depth_consume_frac,
                min_ev_after_slippage=self.cfg.exec_min_ev_after_slippage,
                min_fill_price=(0.0 if _waive_underdog_floor else self.cfg.min_entry_price),
                now=now, max_book_age_s=self.cfg.exec_max_book_age_s)
            self.ledger.record_exec(ex.accepted, ex.reason)
            # observe what the gate actually SEES (drives the zero-reject diagnostic)
            self.gate_obs.observe(spread=ex.spread, ask_depth_usd=mc.ask_depth_usd,
                                  slippage=ex.slippage, ev_after_slippage=ex.ev_after_slippage,
                                  ttc_s=ttc)
            dr.cost = ExecutionCostEstimate.from_exec_result(ex)
            dr.mark("execution_costed")
            if not ex.accepted:
                if entry_mode == "mispricing_follow":
                    _fk = f"follow_blocked_{ex.reason}"
                    self._mispricing_gate_counts[_fk] = (
                        self._mispricing_gate_counts.get(_fk, 0) + 1)
                dr.action = RejectAction(stage="execution_gate", reason=ex.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=ex.reason, stage="execution_gate")
                continue
            d.price = ex.fill_price               # paper fill at realistic VWAP price
            from engine.pulse.sizing import sizing_diagnostics_promoted, sizing_diagnostics
            _pwin_sz = (dr.model or {}).get("p_up") or outcome_prob
            if self.cfg.sizing_promotion_gated:
                _sz = sizing_diagnostics_promoted(
                    sel_tags=sel_tags, is_promoted=self._research_exploit_backed,
                    p_win=_pwin_sz, price=ex.fill_price, ev_after_costs=ex.ev_after_slippage,
                    bankroll_usd=self.cfg.sizing_bankroll_usd,
                    hard_cap_usd=self.cfg.sizing_hard_cap_usd,
                    daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
                    daily_loss_so_far=self._daily_loss, base_size_usd=self.cfg.size_usd,
                    global_sizing_enabled=self.cfg.sizing_enabled)
            else:
                _sz = sizing_diagnostics(
                    p_win=_pwin_sz, price=ex.fill_price, ev_after_costs=ex.ev_after_slippage,
                    bankroll_usd=self.cfg.sizing_bankroll_usd,
                    hard_cap_usd=self.cfg.sizing_hard_cap_usd,
                    daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
                    daily_loss_so_far=self._daily_loss, base_size_usd=self.cfg.size_usd,
                    sizing_enabled=self.cfg.sizing_enabled)
            dr.sizing = _sz
            trade_size = round(float(_sz.get("actual_size_usd") or self.cfg.size_usd)
                               * grok_size_frac, 2)
            dir_cap = (float(self.cfg.starting_capital_usd)
                       * float(self.cfg.directional_max_bankroll_frac))
            open_dir = self._directional_open_exposure()
            if open_dir + trade_size > dir_cap + 1e-6:
                dr.action = RejectAction(stage="directional", reason="directional_bankroll_cap")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason="directional_bankroll_cap", stage="directional")
                continue
            pos = self.ledger.open_position(w, d, now, size_usd=trade_size,
                                            s_open=snap.price, decision_id=mc.decision_id)
            if pos is None:
                # gate accepted but the paper fill could not be recorded — do NOT claim a trade;
                # classify as skipped so accepted-terminals == paper-fills == ledger-trades.
                dr.action = RejectAction(stage="execution_gate", reason="paper_fill_not_recorded")
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "skipped", reason="paper_fill_not_recorded")
                continue
            if rfeat is not None:                 # observe-only entry-time tags
                pos.research = {"hurst_regime": rfeat.hurst_regime,
                                "zscore_bucket": rfeat.zscore_bucket,
                                "half_life_bucket": half_life_bucket(rfeat.half_life_s),
                                "ttc_bucket": ttc_bucket(ttc),
                                "edge_quality_bucket": (fsnap.edge_quality_bucket
                                                        if fsnap else "na"),
                                "markov_state": cand_state,
                                "model_features": model_vec,
                                "spread_bucket": _spread_bucket(mc.spread),
                                "depth_bucket": _depth_bucket(mc.ask_depth_usd),
                                    "confidence_tier": _confidence_tier(
                                        (dr.model or {}).get("model_confidence")
                                        if (dr.model or {}).get("trained")
                                        else (dr.signals or {}).get("confidence"))}
            # OBSERVE-ONLY edge-signal entry tags + EV-after-cost (recorded for every trade)
            if pos.research is None:
                pos.research = {}
            pos.research["market_series"] = getattr(w, "series_label", mc.series_label)
            pos.research["series_slug"] = getattr(w, "series_slug", mc.series_slug)
            pos.research["series_label"] = getattr(w, "series_label", mc.series_label)
            pos.research["window_seconds"] = int(getattr(w, "window_seconds", mc.window_seconds) or 300)
            pos.research["ev_after_cost"] = ex.ev_after_slippage
            pos.research["gate_decision"] = gate_decision     # passed | explored (selectivity gate)
            pos.research["context_gate"] = ("explore" if context_explored else "pass")
            # late-window high-conviction tags (for the observe-only time-decay edge measurement)
            from engine.pulse.late_window import conviction_bucket as _conv_bucket
            pos.research["entry_mode"] = entry_mode
            pos.research["entry_ttc_s"] = float(ttc)
            pos.research["conviction_bucket"] = _conv_bucket(fair_used)
            if grok_dec is not None:
                pos.research["grok_snapshot"] = {
                    "action": grok_dec.get("action"),
                    "p_up": grok_dec.get("p_up"),
                    "confidence": grok_dec.get("confidence"),
                }
            if self.verifier is not None:
                vv_snap = self.verifier.get(mc.decision_id)
                if vv_snap and not vv_snap.get("pending"):
                    pos.research["verifier_snapshot"] = {
                        "approved": bool(vv_snap.get("approve")),
                        "reason": str(vv_snap.get("reason") or "")[:120],
                    }
            if self.verifier is not None and grok_verdict:
                pos.research["verifier"] = {"approved": True,
                                            "max_size_fraction": grok_verdict.get("max_size_fraction"),
                                            "reason": grok_verdict.get("reason")}
            if esnap is not None:
                pos.research.update({"edge_stale_divergence": esnap.stale_divergence_class,
                                     "edge_ttc_bucket": esnap.ttc_bucket,
                                     "edge_ob_pressure": esnap.orderbook_pressure.get("bucket"),
                                     "edge_score_bucket": esnap.pulse_edge_score_bucket,
                                     "edge_cex_agreement": esnap.cex_agreement_bucket})
            if tv_feature is not None:
                pos.research.update({
                    "tv_signal_level": tv_feature.get("signal_level"),
                    "tv_mtf_alignment": tv_feature.get("mtf_alignment"),
                    "tv_range_state": tv_feature.get("range_state"),
                    "tv_direction": tv_feature.get("direction"),
                    "tv_strength": tv_feature.get("strength"),
                })
            if tv_feature is not None:            # observe-only external signal present at entry
                _sym = tv_feature.get("symbol")
                _pred = self._rsi_model.predict(_sym) if _sym else {}
                _trend = self._rsi_model.trend(_sym) if _sym else {}
                pos.external = {"source": "tradingview",
                                "direction": tv_feature.get("direction"),
                                "timeframe": tv_feature.get("timeframe"),
                                "tf_confirm": tv_feature.get("tf_confirm"),
                                "tf_confirm_direction": tv_feature.get("tf_confirm_direction"),
                                "symbol": _sym,
                                "indicator_name": tv_feature.get("indicator_name"),
                                "strength": tv_feature.get("strength"),
                                "strength_bucket": tv_feature.get("strength_bucket"),
                                "signal_level": tv_feature.get("signal_level"),
                                "price": tv_feature.get("price"),
                                "ev_after_cost": ex.ev_after_slippage,   # EV after VWAP/slippage
                                # Composite v2 (observe-only)
                                "vwap_state": tv_feature.get("vwap_state"),
                                "bb_state": tv_feature.get("bb_state"),
                                "volume_state": tv_feature.get("volume_state"),
                                "htf_bias": tv_feature.get("htf_bias"),
                                "composite_version": tv_feature.get("composite_version"),
                                # Composite v3 (observe-only)
                                "adx_state": tv_feature.get("adx_state"),
                                "supertrend_direction": tv_feature.get("supertrend_direction"),
                                "candle_pressure": tv_feature.get("candle_pressure"),
                                "range_state": tv_feature.get("range_state"),
                                "mtf_alignment": tv_feature.get("mtf_alignment"),
                                # Composite v4 order-flow / event (observe-only)
                                "cvd_state": tv_feature.get("cvd_state"),
                                "funding_state": tv_feature.get("funding_state"),
                                "liquidation_spike": tv_feature.get("liquidation_spike"),
                                "event_blackout": tv_feature.get("event_blackout"),
                                # RSI alert-history next-window prediction at entry (observe-only,
                                # leakage-free: scored at settlement before counts are updated)
                                "rsi_trend_state": _trend.get("state"),
                                "rsi_predicted_next": _pred.get("prediction"),
                                "rsi_pred_prob_up": _pred.get("prob_up")}
            # the canonical paper fill — set for EVERY accepted trade (independent of EV stats)
            # so reconciler.ledgered == accepted == ledger.trades by construction.
            dr.fill = PaperFill(window_key=w.event_id, side=d.side, fill_price=ex.fill_price,
                                shares=pos.shares, size_usd=pos.size_usd,
                                decision_id=mc.decision_id)
            # EV before (midpoint) vs after (VWAP/slippage) costs — accepted candidates
            if ex.ev_at_mid is not None and ex.ev_after_slippage is not None:
                self._ev_before_sum += ex.ev_at_mid
                self._ev_after_sum += ex.ev_after_slippage
                self._ev_n += 1
            dr.action = TradeAction(side=d.side, token_id=d.token_id, fill_price=ex.fill_price,
                                    size_usd=self.cfg.size_usd, shares=pos.shares)
            if self.markov is not None:
                self.markov.record_terminal(state=cand_state, accepted=True)
            _finalize(dr, "accepted")

        self._settle_due(now)
        self._reasons = reasons
        if evald:                          # rolling window of recent structured DecisionResults
            self._last_eval = (self._last_eval + evald)[-12:]
        self._prune_positions()
        self._persist()
        return {"ticks": self.ticks, "reasons": reasons, "stats": self.ledger.stats()}

    def _settle_due(self, now: float) -> None:
        for pos in list(self.ledger.open_positions()):
            if pos.close_ts > now:
                continue
            # capture the RTDS Chainlink CLOSE snapshot once, the first post-close tick, so the
            # proxy uses a close price near the actual window close (lag-gated).
            if pos.s_close is None:
                px = self.price.current()
                if px is not None:
                    pos.s_close = px
                    pos.close_lag_s = max(0.0, now - pos.close_ts)
            # settle by the configured PRIORITY: official Polymarket first, RTDS proxy only if
            # the close snapshot lag is within threshold. Wait until grace before proxy so the
            # official result has a chance to publish.
            priority = self.cfg.settlement_source_priority
            if (now - pos.close_ts) <= self.cfg.settle_grace_s:
                priority = tuple(s for s in priority if s == "polymarket_resolution") or priority
            outcome, source = resolve_window(
                pos.market_id, gamma_feed=self.market, priority=priority,
                s_open=pos.s_open, s_close=pos.s_close, close_lag_s=pos.close_lag_s,
                proxy_max_close_lag_s=self.cfg.proxy_max_close_lag_s)
            if outcome is None:
                continue                      # not resolvable yet — retry next tick
            # reconciliation: compare the proxy verdict against the official one when both exist
            proxy_up = proxy_outcome(pos.s_open, pos.s_close) \
                if (pos.close_lag_s is not None
                    and pos.close_lag_s <= self.cfg.proxy_max_close_lag_s) else None
            if source == "polymarket_resolution":
                self.ledger.reconcile(proxy_up, outcome)
            self.ledger.settle(pos.window_key, outcome, s_open=pos.s_open, s_close=pos.s_close,
                               source=source)
            # daily-loss tracker for the Kelly diagnostic (reset per UTC day)
            day = int(now // 86400)
            if day != self._daily_key:
                self._daily_key, self._daily_loss = day, 0.0
            if (pos.pnl_usd or 0.0) < 0:
                self._daily_loss += -float(pos.pnl_usd)
            self.calib.observe(pos.fair_at_entry, outcome)
            if self.research is not None:                # observe-only grouped PnL/calibration
                rt = pos.research or {}
                self.research.record_settled(
                    regime=rt.get("hurst_regime"), zbucket=rt.get("zscore_bucket"),
                    half_life_bucket=rt.get("half_life_bucket"), ttc_bucket=rt.get("ttc_bucket"),
                    pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won),
                    fair_at_entry=pos.fair_at_entry, outcome_up=outcome)
            if self.factors is not None:
                self.factors.record_settled(bucket=(pos.research or {}).get("edge_quality_bucket"),
                                            pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won))
            if self.markov is not None:
                self.markov.record_resolution(state=(pos.research or {}).get("markov_state"),
                                              outcome_up=outcome)
            if self.edge_model is not None:
                mvec = (pos.research or {}).get("model_features")
                if isinstance(mvec, dict):           # train on entry features + realized outcome
                    self.edge_model.observe_label(mvec, bool(outcome))
            # learning loop: group this settled outcome by every entry-time tag dimension
            rt = pos.research or {}
            tags = {dim: rt.get(dim) for dim in (
                "hurst_regime", "zscore_bucket", "half_life_bucket", "ttc_bucket",
                "edge_quality_bucket", "markov_state", "spread_bucket", "depth_bucket",
                "confidence_tier", "conviction_bucket", "entry_mode")}
            self._groups.record(tags, pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won),
                                 fair_at_entry=pos.fair_at_entry, outcome_up=outcome)
            # OBSERVE-ONLY time-decay edge measurement: grade late-window high-conviction trades
            # (cohort vs other) from this live settled trade. Never affects trading.
            self.late_window_edge.record_settled(
                ttc_s=rt.get("entry_ttc_s"), p_up=pos.fair_at_entry, won=bool(pos.won),
                pnl=float(pos.pnl_usd or 0.0), ev_after_cost=rt.get("ev_after_cost"),
                entry_mode=rt.get("entry_mode"))
            # OBSERVE-ONLY: measure whether the TradingView signal at entry predicted this 5-min
            # outcome and whether aligning helped the bot win (computed AFTER the outcome is known).
            self._tv_edge.record(tv=pos.external, traded_side=pos.side, outcome_up=bool(outcome),
                                 won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0))
            # NOTE: the RSI alert-history model now learns from EVERY signal's forward return
            # (see _evaluate_tv_forward_returns), not just traded windows, so we do NOT also score
            # it here (that would double-count traded windows).
            # OBSERVE-ONLY bucketed learning: if this traded window carried a TradingView signal,
            # record win/PnL/EV by every signal + market-context bucket (for promotion diagnostics).
            # OBSERVE-ONLY edge-signal bucketed learning for EVERY settled trade (CEX/stale/OB).
            from engine.pulse.down_stack import classify_down_stack
            rt = pos.research or {}
            ext = pos.external or {}
            stack_bucket = classify_down_stack(
                mtf_alignment=ext.get("mtf_alignment"),
                stale_divergence=rt.get("edge_stale_divergence"),
                ttc_s=rt.get("entry_ttc_s"),
            )
            self.down_stack.record(
                bucket=stack_bucket, won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                entry_price=pos.entry_price)
            if self.edge_signal is not None:
                self.edge_signal.record_settled(
                    {"stale_divergence": rt.get("edge_stale_divergence"),
                     "ttc_bucket": rt.get("edge_ttc_bucket"),
                     "ob_pressure": rt.get("edge_ob_pressure"),
                     "edge_score": rt.get("edge_score_bucket"),
                     "cex_agreement": rt.get("edge_cex_agreement")},
                    won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                    ev_after_cost=rt.get("ev_after_cost"),
                    reconciled=bool(self.reconciler.report().get("reconciled")))
            # Learned Selectivity Gate: feed bucket evidence + per-gate-decision settled stats.
            _sel_tags = self._selectivity_tags_from_pos(pos)
            self.selectivity_evidence.record(
                _sel_tags, won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                ev_after_cost=(pos.research or {}).get("ev_after_cost"), outcome_up=outcome)
            self.selectivity_gate.record_settled((pos.research or {}).get("gate_decision"),
                                                 won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0))
            # feed FOLLOW/EXPLORE trades back to the Grok decider's circuit breaker
            if (self.grok_decider is not None
                    and (pos.research or {}).get("entry_mode")
                    in ("grok_follow", "grok_explore", "grok_adaptive")):
                self.grok_decider.record_follow_result(
                    won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0), now=now)
            # grade the maker-checker (approved trade outcome) + record lessons from this settlement
            if self.verifier is not None and (pos.research or {}).get("verifier"):
                self.verifier.grade(pos.decision_id or pos.window_key, won=bool(pos.won),
                                    pnl=float(pos.pnl_usd or 0.0), acted=True)
            rt_hist = pos.research or {}
            self.trade_history.record_settled(
                decision_id=pos.decision_id or pos.window_key,
                title=pos.title,
                side=pos.side,
                entry_mode=rt_hist.get("entry_mode") or "unknown",
                entry_price=float(pos.entry_price),
                size_usd=float(pos.size_usd),
                outcome_up=bool(outcome),
                won=bool(pos.won),
                pnl_usd=float(pos.pnl_usd or 0.0),
                research=rt_hist,
                grok=rt_hist.get("grok_snapshot"),
                verifier=rt_hist.get("verifier_snapshot") or rt_hist.get("verifier"),
            )
            self._record_lessons_from_settlement(pos)
            ext = pos.external or {}
            if ext.get("source") == "tradingview":
                rt = pos.research or {}
                self._tv_learner.record_settled(
                    {"direction": ext.get("direction"), "signal_level": ext.get("signal_level"),
                     "strength_bucket": ext.get("strength_bucket"),
                     "indicator_name": ext.get("indicator_name"),
                     "hurst_regime": rt.get("hurst_regime"), "zscore_bucket": rt.get("zscore_bucket"),
                     "ttc_bucket": rt.get("ttc_bucket"), "spread_bucket": rt.get("spread_bucket"),
                     "depth_bucket": rt.get("depth_bucket"),
                     "vwap_state": ext.get("vwap_state"), "bb_state": ext.get("bb_state"),
                     "volume_state": ext.get("volume_state"), "htf_bias": ext.get("htf_bias"),
                     "composite_version": ext.get("composite_version"),
                     "adx_state": ext.get("adx_state"),
                     "supertrend_direction": ext.get("supertrend_direction"),
                     "candle_pressure": ext.get("candle_pressure"),
                     "range_state": ext.get("range_state"),
                     "mtf_alignment": ext.get("mtf_alignment"),
                     "cvd_state": ext.get("cvd_state"), "funding_state": ext.get("funding_state"),
                     "liquidation_spike": ext.get("liquidation_spike"),
                     "event_blackout": ext.get("event_blackout")},
                    won=bool(pos.won), pnl=float(pos.pnl_usd or 0.0),
                    ev_after_cost=ext.get("ev_after_cost"),
                    reconciled=bool(self.reconciler.report().get("reconciled")))
            logger.info("pulse settled %s side=%s won=%s pnl=%.3f via=%s",
                        pos.title, pos.side, pos.won, pos.pnl_usd or 0.0, source)

    def _prune_positions(self) -> None:
        if len(self.ledger.positions) <= self.cfg.max_positions_kept:
            return
        settled = [p for p in self.ledger.positions.values() if p.status == "settled"]
        settled.sort(key=lambda p: p.close_ts)
        for p in settled[: len(self.ledger.positions) - self.cfg.max_positions_kept]:
            self.ledger.positions.pop(p.window_key, None)

    # -- persistence -------------------------------------------------------- #
    def readiness(self) -> dict:
        """Success-gate readiness report (report-only). Never claims an 80% bot unless ALL gates
        pass. Inputs come from the reconciled ledger + lifecycle (no unmodeled fill assumptions:
        paper fills use the live ask-ladder VWAP)."""
        from engine.pulse.readiness import readiness_report
        from engine.pulse.reconciliation import global_reconciliation
        ls = self.ledger.stats()
        lc = self.reconciler.report()
        eg = self.ledger.exec_gate_stats()
        cal = self.calib.to_dict()
        gr = global_reconciliation(lifecycle=lc, exec_gate=eg, ledger_stats=ls,
                                   baseline=(self._baseline or empty_baseline()))
        recon_ok = bool(gr.get("global_reconciled"))
        # calibration_error gate expects an ECE (<=0.10), NOT the Brier score — pass the model's
        # actual ECE (None if unavailable -> gate stays unmet, which is the honest default).
        model_ece = (self.edge_model.calibration_error() if self.edge_model is not None else None)
        return readiness_report(
            accepted=int(ls.get("settled", 0) or 0), win_rate=ls.get("win_rate"),
            net_pnl=ls.get("realized_pnl_usd"), profit_factor=ls.get("profit_factor"),
            calibration_error=model_ece, max_drawdown=ls.get("max_drawdown_usd"),
            avg_win=ls.get("avg_win_usd"), avg_loss=ls.get("avg_loss_usd"),
            reconciliation_ok=recon_ok, missing_settlement=False, unmodeled_fill=False,
            safety_bypass=False)

    def _meta_learning_status(self) -> dict:
        """Status of the LLM batch meta-learning layer (bundle written; integration availability).
        Never makes live trade decisions."""
        try:
            from engine.pulse.overlay import xai_key_present
            available = bool(xai_key_present())
        except Exception:  # noqa: BLE001
            available = False
        return {"enabled": True, "report_only": True, "no_live_trading_decisions": True,
                "bundle_artifact": "btc_pulse_meta_bundle.json",
                "grok_integration_available": available}

    def _evaluate_tv_forward_returns(self, now: float) -> None:
        """Resolve due forward-return evals: compare the oracle BTC price now vs at signal time and
        teach the RSI model whether each signal predicted the move. Builds the prediction from ALL
        signals. Observe-only; leakage-free (model_pred was snapshotted at signal time)."""
        if not self._tv_pending:
            return
        px_now = self.price.current()
        still = []
        for pend in self._tv_pending:
            if now < pend["due_ts"]:
                still.append(pend)
                continue
            if px_now is not None:
                outcome_up = float(px_now) >= float(pend["price0"])
                self._rsi_model.record_signal_outcome(
                    symbol=pend["symbol"], state=pend.get("state"),
                    model_pred=pend.get("model_pred"), signal_direction=pend.get("direction"),
                    outcome_up=outcome_up)
                # B: grade Grok's per-signal P(up) against the same realized move (leakage-free)
                if self.grok_predictor is not None and pend.get("event_id"):
                    self.grok_predictor.score(pend["event_id"], outcome_up)
            elif now <= pend["due_ts"] + 600:    # grace: retry until an oracle price is available
                still.append(pend)
            # else: stale with no price -> drop
        self._tv_pending = still[-1000:]

    @staticmethod
    def _r(v, nd=4):
        """Round floats for a compact, well-typed payload; pass through non-numerics."""
        try:
            return round(float(v), nd) if v is not None else None
        except (TypeError, ValueError):
            return v

    @staticmethod
    def _reward_risk(ask):
        """Binary payoff at ask price p: a win nets (1-p)/p per $; breakeven win-rate ~= p."""
        try:
            p = float(ask)
            if p <= 0 or p >= 1:
                return None
            return {"ask": round(p, 4), "win_payoff_per_$": round((1.0 - p) / p, 4),
                    "breakeven_win_rate": round(p, 4)}
        except (TypeError, ValueError):
            return None

    def _grok_decision_context(self, rf, cand_state, ttc, fair_used) -> dict:
        """Compact entry-time context tags used to bucket the decider's OWN accuracy as it learns."""
        conv = abs(float(fair_used) - 0.5) * 2 if fair_used is not None else None
        conv_bucket = ("na" if conv is None else
                       ("strong" if conv >= 0.4 else ("lean" if conv >= 0.2 else "coinflip")))
        return {"hurst_regime": (rf.get("hurst_regime") if rf else None),
                "markov_state": cand_state, "ttc_bucket": ttc_bucket(ttc),
                "conviction_bucket": conv_bucket}

    def _grok_tv_fingerprint(self, tv_trend: Optional[dict]) -> str:
        """Stable key for MTF changes that should trigger a fresh Grok read."""
        tv_trend = tv_trend or {}
        parts = [str(tv_trend.get("confirm_mtf")), str(tv_trend.get("fresh_tf_count"))]
        for label, row in sorted((tv_trend.get("charts") or {}).items()):
            if not isinstance(row, dict):
                continue
            parts.append("%s:%s:%s:%s" % (
                label, row.get("direction"), row.get("signal_level"), row.get("strength")))
        return "|".join(parts)

    def _grok_refresh_token(self, decision_id: str, bundle: dict, *, ttc: float,
                            window_seconds: int) -> Optional[str]:
        """Return refresh_token when TV MTF flips or 15m window enters baseline entry band."""
        import hashlib
        tv_trend = bundle.get("tradingview_trend") or {}
        fp = self._grok_tv_fingerprint(tv_trend)
        prev = self._grok_tv_fp.get(decision_id)
        tokens: list[str] = []
        if prev is not None and prev != fp:
            tokens.append("tv:" + hashlib.sha256(fp.encode()).hexdigest()[:12])
        ws = int(window_seconds or 300)
        if ws >= 900 and 480.0 <= float(ttc) <= 660.0:
            if decision_id not in self._grok_entry_band_seen:
                self._grok_entry_band_seen.add(decision_id)
                if prev is not None:
                    tokens.append("entry15m")
        self._grok_tv_fp[decision_id] = fp
        return "+".join(tokens) if tokens else None

    def _book_side_snapshot(self, book) -> "dict | None":
        if book is None:
            return None
        return {
            "mid": self._r(book.mid), "spread": self._r(book.spread),
            "best_bid": self._r(book.best_bid), "best_ask": self._r(book.best_ask),
            "bid_depth_usd": self._r(book.bid_depth_usd, 1),
            "ask_depth_usd": self._r(book.ask_depth_usd, 1),
            "ask_levels": len(book.asks or []), "bid_levels": len(book.bids or []),
        }

    def _market_window_snapshot(self, w, *, now=None) -> dict:
        now = now if now is not None else (self.last_tick_ts or time.time())
        return {
            "series_slug": getattr(w, "series_slug", SERIES_SLUG_5M),
            "series_label": getattr(w, "series_label", "5m"),
            "window_seconds": int(getattr(w, "window_seconds", 300) or 300),
            "event_id": w.event_id, "title": w.title,
            "ttc_s": self._r(w.seconds_to_close(now), 1),
            "up": self._book_side_snapshot(w.up_book),
            "down": self._book_side_snapshot(w.down_book),
        }

    def _active_markets_for_grok(self) -> list:
        try:
            windows = self.market.active_windows(now=self.last_tick_ts)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for win in windows:
            try:
                self.market.hydrate_books(win)
            except Exception:  # noqa: BLE001
                pass
            out.append(self._market_window_snapshot(win))
        return out

    def _cex_prices_snapshot(self) -> dict:
        out = {}
        if self.leads is not None:
            for k, v in (getattr(self.leads, "_latest", {}) or {}).items():
                px = v[0] if isinstance(v, (tuple, list)) else v
                out[k] = self._r(px, 2)
        return out

    def _grok_decision_bundle(self, mc, dr, w, fair_used, ttc, tv_feature) -> dict:
        """Fully-structured 'analyze everything' payload for the Grok decider. Numerics rounded,
        nulls allowed, ordered so the decision-critical fields lead. Includes: market microstructure
        + binary payoff (the breakeven bar), the digital fair vs Polymarket divergence, the
        TradingView signal, live news, regime/research, edge signal, account/risk state, the bot's
        OWN learned evidence, and the decider's track record (so Grok LEARNS as it trades)."""
        rf = dr.features or {}
        try:
            sel_be = self.selectivity_gate.bucket_evidence(self.selectivity_evidence, top=6)
        except Exception:  # noqa: BLE001
            sel_be = {}
        ls = self.ledger.stats()
        up_ask = (w.up_book.best_ask if w.up_book else None)
        dn_ask = (w.down_book.best_ask if w.down_book else None)
        poly_yes = mc.poly_yes
        divergence = (round(float(fair_used) - float(poly_yes), 4)
                      if (fair_used is not None and poly_yes is not None) else None)
        # compact TradingView signal: drop nulls/unknowns to keep the payload tight + readable
        tv = None
        if tv_feature:
            tv = {k: v for k, v in tv_feature.items()
                  if v is not None and v != "unknown" and k not in ("observe_only", "source")}
        series_label = getattr(w, "series_label", mc.series_label)
        mtf = None
        tv_trend = None
        if self.tradingview is not None:
            mtf = self.tradingview.mtf_confirmation(
                symbol=self.cfg.tradingview_feature_symbol, now=self.last_tick_ts)
            tv_rep = self.tradingview.report()
            from engine.pulse.grok_bundle import tv_trend_snapshot
            tv_trend = tv_trend_snapshot(
                mtf=mtf,
                latest_by_timeframe=tv_rep.get("tradingview_latest_by_timeframe") or {},
                feature_symbol=tv_rep.get("tradingview_feature_symbol")
                or self.cfg.tradingview_feature_symbol,
            )
        from engine.pulse.grok_bundle import (compact_tv_learning, gate_funnel_top,
                                              grok_task_for_window)
        from engine.pulse.reporting import ledger_stats_by_market_series
        lifecycle = self.reconciler.report()
        _ws = int(getattr(w, "window_seconds", mc.window_seconds) or 300)
        return {
            "schema_version": "grok_decision_bundle/1.4",
            "grok_task": grok_task_for_window(series_label=series_label, window_seconds=_ws,
                                               ttc_s=ttc),
            "market": "polymarket_btc_%s_up_or_down" % series_label,
            "series_slug": getattr(w, "series_slug", mc.series_slug),
            "series_label": series_label,
            "window_seconds": _ws,
            "objective": ("settles UP if BTC Chainlink close >= window open (%s window); "
                          "pick up/down/no_trade") % series_label,
            "decision_id": mc.decision_id,
            "by_market_series": ledger_stats_by_market_series(self.ledger.positions),
            "gate_funnel": gate_funnel_top(lifecycle.get("rejected_by_stage") or {}),
            "tradingview_trend": tv_trend,
            "tv_signal_learning": compact_tv_learning(self._tv_learner.report(
                promotion_allowed=self.cfg.tradingview_promotion_allowed,
                min_samples=self.cfg.tradingview_promotion_min_samples,
                min_win_rate=self.cfg.tradingview_promotion_min_win_rate)),
            "timing": {"seconds_to_close": self._r(ttc, 1),
                       "window_seconds": int(getattr(w, "window_seconds", mc.window_seconds) or 300),
                       "utc_minute_of_hour": int((self.last_tick_ts or time.time()) // 60 % 60)},
            "price": {"btc_now": self._r(mc.s_now, 2), "btc_open": self._r(mc.s_open, 2),
                      "move_from_open": (self._r(mc.s_now - mc.s_open, 2)
                                         if (mc.s_now is not None and mc.s_open is not None) else None),
                      "sigma_per_sec": self._r(mc.sigma_per_sec, 6),
                      "lead_prices": {k: self._r(v, 2) for k, v in (mc.lead_prices or {}).items()
                                      if v is not None}},
            "digital_fair_p_up": self._r(fair_used),
            "polymarket": {
                "yes_mid": self._r(poly_yes), "spread": self._r(mc.spread),
                "up_best_ask": self._r(up_ask), "down_best_ask": self._r(dn_ask),
                "ask_depth_usd": self._r(mc.ask_depth_usd, 1),
                "fair_minus_poly": divergence,
                "up_book": self._book_side_snapshot(w.up_book),
                "down_book": self._book_side_snapshot(w.down_book),
            },
            "active_markets": self._active_markets_for_grok(),
            "cex_prices": self._cex_prices_snapshot(),
            "payoff": {"up": self._reward_risk(up_ask), "down": self._reward_risk(dn_ask),
                       "min_reward_risk_floor": self.cfg.min_reward_risk,
                       "note": "only trade a side if your P(win) clears its breakeven_win_rate after costs"},
            "recent_windows": self._recent_windows_view(10),
            "trade_decision_history": self.trade_history.view_for_grok(50),
            "lessons": self.lessons.recent(10),
            "tradingview_signal": tv,
            "news": (self.grok_news.latest() if self.grok_news is not None else None),
            "research": {"hurst_regime": rf.get("hurst_regime"),
                         "zscore_bucket": rf.get("zscore_bucket"),
                         "half_life_s": self._r(rf.get("half_life_s"), 1),
                         "regime": (dr.regime or {}).get("state")},
            "edge_signal": {k: (dr.edge or {}).get(k) for k in
                            ("pulse_edge_score", "stale_divergence_class", "cex_agreement_bucket",
                             "orderbook_pressure")},
            # PRIMARY mispricing signal: fresh CEX-implied P(up) vs the market price (lead-lag), with
            # orderflow + TradingView + late-window confirmation. This is the credible edge to exploit.
            "cex_lead_mispricing": {k: (dr.cex_lead or {}).get(k) for k in
                                    ("divergence", "side", "confirmed", "tv_confirms",
                                     "late_decisive", "news_state", "cex_p_up", "poly_yes")},
            # the bot's directional model is graded WORSE than the market price out-of-sample; trust
            # the market price + divergence-based mispricing over the model's raw opinion.
            "model_vs_market": self._market_benchmark(),
            "edge_model_p_up": self._r((dr.model or {}).get("p_up")),
            "grok_per_signal_p_up": (tv_feature or {}).get("grok_p_up"),
            "account_state": {"open_positions": ls.get("open_positions"),
                              "settled": ls.get("settled"), "win_rate": self._r(ls.get("win_rate")),
                              "realized_pnl_usd": self._r(ls.get("realized_pnl_usd"), 2),
                              "daily_loss_so_far_usd": self._r(self._daily_loss, 2),
                              "size_usd": self.cfg.size_usd},
            "bot_learned_evidence": {
                "selectivity_blocked_or_notable": sel_be.get("buckets", [])[:6],
                "late_window_edge_verdict": self.late_window_edge.report().get("verdict"),
                "pnl_by_ttc_bucket": self._groups.summary().get("ttc_bucket", {}),
                "pnl_by_hurst_regime": self._groups.summary().get("hurst_regime", {})},
            "decider_track_record": (self.grok_decider.report() if self.grok_decider else {}),
            "note": ("advisory PAPER decision; the bot enforces a realism/risk floor (execution gate, "
                     "caps, freshness) and follows your direction; learn from decider_track_record."),
        }

    def _follow_canary(self, decision_id: str) -> bool:
        """A/B canary: deterministically follow Grok on ``follow_fraction`` of windows (stable per
        decision_id), leaving the rest on the baseline arm for live comparison."""
        frac = float(self.cfg.grok_decider_follow_fraction)
        if frac >= 1.0:
            return True
        if frac <= 0.0:
            return False
        import hashlib
        h = int(hashlib.sha256(str(decision_id).encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        return h < frac

    @staticmethod
    def _counterfactual_side_pnl(side: str, entry_price: float, size_usd: float,
                                   outcome_up: bool):
        if side not in ("up", "down") or not entry_price or entry_price <= 0 or entry_price >= 1:
            return None, 0.0
        won = (side == "up" and outcome_up) or (side == "down" and not outcome_up)
        shares = float(size_usd) / float(entry_price)
        pnl = round((shares if won else 0.0) - float(size_usd), 6)
        return bool(won), pnl

    @staticmethod
    def _grok_proposed_side(grok_dec: Optional[dict]) -> Optional[str]:
        if not grok_dec:
            return None
        act = grok_dec.get("action")
        return act if act in ("up", "down") else None

    def _schedule_verifier_grade(self, decision_id: str, *, price0, close_ts: float, side: str,
                                 entry_ask: float, size_usd: float, acted: bool) -> None:
        """Queue a verifier verdict for counterfactual grading at window close."""
        if (not decision_id or side not in ("up", "down") or price0 is None
                or entry_ask is None or self.verifier is None):
            return
        for p in self._verifier_pending:
            if p["decision_id"] == decision_id:
                return
        self._verifier_pending.append({
            "decision_id": decision_id,
            "price0": float(price0),
            "close_ts": float(close_ts),
            "side": side,
            "entry_ask": float(entry_ask),
            "size_usd": float(size_usd),
            "acted": bool(acted),
        })

    def _maybe_schedule_verifier_counterfactual(self, mc, w, snap, grok_dec, *,
                                                side=None, entry_ask=None,
                                                size_frac: float = 1.0, acted: bool = False) -> None:
        """Schedule counterfactual P&L grade when Claude has a final verdict on a proposed side."""
        if self.verifier is None or acted:
            return
        verdict = self.verifier.get(mc.decision_id)
        if not verdict or verdict.get("pending") or verdict.get("approve"):
            return
        side = side or self._grok_proposed_side(grok_dec)
        if side not in ("up", "down"):
            return
        if entry_ask is None:
            book = w.up_book if side == "up" else w.down_book
            entry_ask = book.best_ask if book else None
        if entry_ask is None or snap.price is None:
            return
        size_usd = float(self.cfg.size_usd) * float(size_frac or 1.0)
        self._schedule_verifier_grade(
            mc.decision_id, price0=snap.price, close_ts=w.close_ts, side=side,
            entry_ask=float(entry_ask), size_usd=size_usd, acted=False)

    def _grade_verifier_decisions(self, now: float) -> None:
        """Grade due verifier verdicts vs the realized 5-min outcome (veto counterfactual P&L)."""
        if not self._verifier_pending or self.verifier is None:
            return
        px = self.price.current()
        still = []
        for p in self._verifier_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                outcome_up = float(px) >= float(p["price0"])
                won, pnl = self._counterfactual_side_pnl(
                    p["side"], p["entry_ask"], p["size_usd"], outcome_up)
                if won is not None:
                    self.verifier.grade(p["decision_id"], won=won, pnl=pnl,
                                        acted=bool(p.get("acted")))
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._verifier_pending = still[-2000:]

    def _schedule_grok_grade(self, decision_id: str, price0, close_ts: float, decision: dict) -> None:
        """Queue a decision for grading at window close. The gradeable fields (action/p_up/context)
        are SNAPSHOTTED here and persisted, so grading survives a process restart (the decider's
        in-memory result cache does not)."""
        if price0 is None:
            return
        for p in self._grok_pending:
            if p["decision_id"] == decision_id:
                return
        self._grok_pending.append({"decision_id": decision_id, "price0": float(price0),
                                   "close_ts": float(close_ts),
                                   "action": decision.get("action"), "p_up": decision.get("p_up"),
                                   "context": decision.get("context") or {}})

    def _grade_grok_decisions(self, now: float) -> None:
        """Grade due Grok decisions vs the realized 5-min outcome (UP if close >= open), traded or
        not. Leakage-free (price0 snapshotted at entry). Uses the persisted snapshot so it survives
        restarts. This is the always-on directional edge data Grok learns from."""
        if not self._grok_pending or self.grok_decider is None:
            return
        px = self.price.current()
        still = []
        for p in self._grok_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                s_open, s_close = float(p["price0"]), float(px)
                outcome_up = s_close >= s_open
                self.grok_decider.grade_fields(
                    action=p.get("action"), p_up=p.get("p_up"), context=p.get("context") or {},
                    outcome_up=outcome_up)
                # record the resolved window so Grok sees the recent sequence of outcomes
                self._recent_windows.append({
                    "close_ts": round(float(p["close_ts"]), 1), "s_open": round(s_open, 2),
                    "s_close": round(s_close, 2), "outcome": ("up" if outcome_up else "down"),
                    "move_pct": (round((s_close - s_open) / s_open * 100, 4) if s_open else None)})
                self._recent_windows = self._recent_windows[-40:]
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._grok_pending = still[-2000:]

    def _schedule_cex_lead_grade(self, decision_id: str, price0, close_ts: float,
                                 sig: dict) -> None:
        """Queue a CEX-lead signal for grading at window close. The gradeable fields are SNAPSHOTTED
        and persisted, so grading survives a restart (leakage-free: price0 captured at entry)."""
        if price0 is None or self.cex_lead is None or not sig.get("has_signal"):
            return
        for p in self._cex_lead_pending:
            if p["decision_id"] == decision_id:
                return
        self._cex_lead_pending.append({
            "decision_id": decision_id, "price0": float(price0), "close_ts": float(close_ts),
            "bucket": sig.get("bucket"), "context_keys": sig.get("context_keys") or [],
            "side": sig.get("side"), "cex_p_up": sig.get("cex_p_up"),
            "poly_yes": sig.get("poly_yes"), "fair": sig.get("fair")})

    def _grade_cex_lead(self, now: float) -> None:
        """Grade due CEX-lead signals vs the realized 5-min outcome (UP if close >= open), traded or
        not. This is the always-on, unbiased measurement of whether the CEX-implied probability beats
        the market price — the gate for ever promoting it to drive trades."""
        if not self._cex_lead_pending or self.cex_lead is None:
            return
        px = self.price.current()
        still = []
        for p in self._cex_lead_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                outcome_up = float(px) >= float(p["price0"])
                if p.get("cex_p_up") is not None and p.get("poly_yes") is not None:
                    self.cex_lead.record(
                        bucket=p.get("bucket"), context_keys=p.get("context_keys"),
                        side=p.get("side"), cex_p_up=p["cex_p_up"], poly_yes=p["poly_yes"],
                        fair=p.get("fair"), outcome_up=outcome_up)
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._cex_lead_pending = still[-2000:]

    def _schedule_market_benchmark(self, decision_id: str, price0, close_ts: float,
                                   model_p_up, market_p_up, fair_p_up) -> None:
        """Queue a model-vs-market accuracy grade at window close (leakage-free snapshot)."""
        if price0 is None or model_p_up is None or market_p_up is None:
            return
        for p in self._mkt_bench_pending:
            if p["decision_id"] == decision_id:
                return
        self._mkt_bench_pending.append({
            "decision_id": decision_id, "price0": float(price0), "close_ts": float(close_ts),
            "model_p_up": float(model_p_up), "market_p_up": float(market_p_up),
            "fair_p_up": (float(fair_p_up) if fair_p_up is not None else None)})

    def _grade_market_benchmark(self, now: float) -> None:
        """Grade due windows: accumulate squared error of model P(up), market price, and digital fair
        vs the realized outcome — the rolling comparison powering the learning blend's market gate."""
        if not self._mkt_bench_pending:
            return
        px = self.price.current()
        still = []
        for p in self._mkt_bench_pending:
            if now < p["close_ts"]:
                still.append(p)
                continue
            if px is not None:
                o = 1.0 if float(px) >= float(p["price0"]) else 0.0
                m_se = (float(p["model_p_up"]) - o) ** 2
                k_se = (float(p["market_p_up"]) - o) ** 2
                f_se = ((float(p["fair_p_up"]) - o) ** 2 if p.get("fair_p_up") is not None else None)
                self._mkt_bench_recent.append((m_se, k_se, f_se))
            elif now <= p["close_ts"] + 600:
                still.append(p)
        self._mkt_bench_pending = still[-2000:]

    def _market_benchmark(self) -> dict:
        """Rolling Brier of model vs market vs digital-fair on graded windows (out-of-sample)."""
        rows = list(self._mkt_bench_recent)
        n = len(rows)
        if n == 0:
            return {"n": 0, "model_brier": None, "market_brier": None, "fair_brier": None,
                    "model_beats_market": None}
        mb = sum(r[0] for r in rows) / n
        kb = sum(r[1] for r in rows) / n
        fr = [r[2] for r in rows if r[2] is not None]
        fb = (sum(fr) / len(fr)) if fr else None
        return {"n": n, "model_brier": round(mb, 5), "market_brier": round(kb, 5),
                "fair_brier": (round(fb, 5) if fb is not None else None),
                "model_beats_market": bool(mb < kb)}

    def _recent_windows_view(self, n: int = 10) -> dict:
        """Recent resolved BTC 5m windows + a momentum summary (up-rate + current streak) for Grok."""
        rows = self._recent_windows[-n:]
        full = self._recent_windows[-20:]
        ups = sum(1 for w in full if w.get("outcome") == "up")
        streak = 0
        last = None
        for w in reversed(full):
            o = w.get("outcome")
            if last is None:
                last, streak = o, 1
            elif o == last:
                streak += 1
            else:
                break
        return {"windows": rows,
                "n": len(full),
                "up_rate": (round(ups / len(full), 3) if full else None),
                "current_streak": ((last or "") + "x" + str(streak)) if last else None}

    def _reward_risk_floor(self, side: "str | None") -> float:
        base = float(self.cfg.min_reward_risk or 0.0)
        if (base <= 0.0 or not side or str(side).lower() != "up"
                or not self.cfg.directional_up_restrictions_enabled):
            return base
        return base + float(self.cfg.min_reward_risk_up_premium or 0.15)

    def _ask_reward_risk_ok(self, side: "str | None", ask: "float | None") -> bool:
        floor = self._reward_risk_floor(side)
        if floor <= 0.0 or ask is None or float(ask) <= 0.0:
            return True
        return ((1.0 - float(ask)) / float(ask)) >= floor

    def _grok_up_side_allowed(self) -> bool:
        if self.grok_decider is None:
            return True
        rep = self.grok_decider.report()
        graded = int(rep.get("graded_directional") or 0)
        acc = rep.get("direction_accuracy")
        if graded >= 20 and acc is not None and float(acc) < 0.52:
            return False
        return True

    def _green_path_active(self, *, side: str, window_seconds: int) -> bool:
        """15m DOWN baseline quant: cohort + MTF; skip stacked opinion gates."""
        if not self.cfg.green_path_enabled:
            return False
        if side != "down":
            return False
        ws = int(window_seconds or 300)
        if ws < 900:
            return False
        return bool(self.cfg.baseline_cohort_15m_fast_lane)

    def _baseline_quant_cohort_ok(self, *, side: str, esnap=None, ttc_s: "float | None",
                                  tv_feature: "dict | None",
                                  window_seconds: int = 300,
                                  ask_price: "float | None" = None) -> "tuple[bool, str]":
        """Tier-1: baseline trades only in high-edge + strong-CEX + scaled TTC band; UP needs TV."""
        if not self.cfg.baseline_cohort_gate_enabled:
            return True, ""
        if side not in ("up", "down"):
            return False, "baseline_cohort_bad_side"
        if ttc_s is None:
            return False, "baseline_cohort_ttc_unknown"
        ws = int(window_seconds or 300)
        scale = float(ws) / 300.0
        fast_lane = (self.cfg.baseline_cohort_15m_fast_lane and ws >= 900)
        if fast_lane:
            ttc_min = float(self.cfg.baseline_cohort_15m_ttc_min_s) * scale
            ttc_max = float(self.cfg.baseline_cohort_15m_ttc_max_s) * scale
        else:
            ttc_min = float(self.cfg.baseline_cohort_ttc_min_s) * scale
            ttc_max = float(self.cfg.baseline_cohort_ttc_max_s) * scale
        ttc_f = float(ttc_s)
        if ttc_f > ttc_max:
            return False, "baseline_cohort_ttc_too_late"
        if ttc_f < ttc_min:
            return False, "baseline_cohort_ttc_too_early"
        edge_bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        down_edge_relaxed = (
            fast_lane and side == "down"
            and not self.cfg.baseline_cohort_require_high_edge)
        if down_edge_relaxed:
            if edge_bucket not in ("medium", "high", "very_high"):
                return False, "baseline_cohort_edge_not_high"
        elif self.cfg.baseline_cohort_require_high_edge:
            if edge_bucket not in ("high", "very_high"):
                return False, "baseline_cohort_edge_not_high"
        cex_bucket = self._edge_snap_field(esnap, "cex_agreement_bucket")
        down_cex_relaxed = (
            fast_lane and side == "down"
            and not self.cfg.baseline_cohort_require_strong_cex)
        if down_cex_relaxed:
            if cex_bucket not in ("moderate", "strong"):
                return False, "baseline_cohort_cex_not_strong"
        elif self.cfg.baseline_cohort_require_strong_cex:
            if cex_bucket != "strong":
                return False, "baseline_cohort_cex_not_strong"
        if side == "up" and self.cfg.directional_up_restrictions_enabled:
            tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
            if not tv_ok:
                return False, tv_reason
        if side == "down":
            if self.cfg.baseline_down_block_medium_edge:
                edge_bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
                if str(edge_bucket or "").strip().lower() == "medium":
                    return False, "baseline_down_medium_edge"
            if self.cfg.baseline_down_block_not_stale:
                stale = self._edge_snap_field(esnap, "stale_divergence_class")
                if str(stale or "").strip().lower() == "not_stale":
                    return False, "baseline_down_not_stale"
            if self.cfg.baseline_down_block_mid_entry and ask_price is not None:
                try:
                    ap = float(ask_price)
                except (TypeError, ValueError):
                    ap = None
                if ap is not None:
                    lo = float(self.cfg.baseline_down_mid_entry_min)
                    hi = float(self.cfg.baseline_down_mid_entry_max)
                    if lo <= ap < hi:
                        return False, "baseline_down_mid_entry_band"
            down_tv_ok, down_tv_reason = self._baseline_down_tv_context_ok(tv_feature)
            if not down_tv_ok:
                return False, down_tv_reason
        return True, ""

    def _config_coupling_report(self) -> dict:
        rep = dict(getattr(self, "_config_coupling", None) or {})
        if rep and self.tv_context_gate is not None:
            rep = {**rep, "runtime_context_max_ttc_s": self.tv_context_gate.max_ttc_s}
        return rep

    def _baseline_cohort_gate_report(self) -> dict:
        return {
            "enabled": bool(self.cfg.baseline_cohort_gate_enabled),
            "ttc_min_s": self.cfg.baseline_cohort_ttc_min_s,
            "ttc_max_s": self.cfg.baseline_cohort_ttc_max_s,
            "require_high_edge": self.cfg.baseline_cohort_require_high_edge,
            "require_strong_cex": self.cfg.baseline_cohort_require_strong_cex,
            "blocked": sum(self._baseline_cohort_gate_counts.values()),
            "block_reasons": dict(self._baseline_cohort_gate_counts),
            "15m_fast_lane": bool(self.cfg.baseline_cohort_15m_fast_lane),
            "15m_ttc_band_s": [self.cfg.baseline_cohort_15m_ttc_min_s,
                               self.cfg.baseline_cohort_15m_ttc_max_s],
            "up_restrictions_enabled": bool(self.cfg.directional_up_restrictions_enabled),
            "down_tv_gate_enabled": bool(self.cfg.baseline_down_tv_gate_enabled),
            "down_block_bullish_range": bool(self.cfg.baseline_down_block_bullish_range),
            "down_block_volume_active": bool(self.cfg.baseline_down_block_volume_active),
            "down_block_up_strong_range_top": bool(
                self.cfg.baseline_down_block_up_strong_range_top),
            "down_block_bullish_mtf": bool(self.cfg.baseline_down_block_bullish_mtf),
            "down_block_not_stale": bool(self.cfg.baseline_down_block_not_stale),
            "down_block_mid_entry": bool(self.cfg.baseline_down_block_mid_entry),
            "down_block_single_tf": bool(self.cfg.baseline_down_block_single_tf),
            "down_block_medium_edge": bool(self.cfg.baseline_down_block_medium_edge),
            "down_block_bb_expansion_down": bool(self.cfg.baseline_down_block_bb_expansion_down),
            "down_mid_entry_band": [self.cfg.baseline_down_mid_entry_min,
                                    self.cfg.baseline_down_mid_entry_max],
            "green_path_enabled": bool(self.cfg.green_path_enabled),
            "note": ("baseline quant path: 180-240s TTC band (scaled on 15m), high edge + "
                     "strong CEX; UP blocked until promoted; "
                     "green_path=15m DOWN cohort only (TV observe-only)"),
        }

    def _entry_confidence_tier(self, dr) -> "str | None":
        model = dr.model or {}
        if model.get("trained"):
            return _confidence_tier(model.get("model_confidence"))
        return _confidence_tier((dr.signals or {}).get("confidence"))

    def _down_bias_eval(self, *, side: str, tv_feature: "dict | None",
                        markov_state: "str | None" = None,
                        ttc_s: "float | None" = None,
                        esnap=None,
                        fair_p_up: "float | None" = None,
                        zscore_bucket: "str | None" = None,
                        confidence_tier: "str | None" = None,
                        ask_price: "float | None" = None) -> dict:
        from engine.pulse.late_window import conviction as _conviction
        feat = tv_feature or {}
        return self.tv_down_bias_gate.evaluate(
            side=side,
            mtf_alignment=feat.get("mtf_alignment"),
            tv_direction=feat.get("direction"),
            tf_confirm=feat.get("tf_confirm"),
            supertrend_direction=feat.get("supertrend_direction"),
            vwap_state=feat.get("vwap_state"),
            bb_state=feat.get("bb_state"),
            range_state=feat.get("range_state"),
            markov_state=markov_state,
            htf_bias=feat.get("htf_bias"),
            candle_pressure=feat.get("candle_pressure"),
            edge_score_bucket=self._edge_snap_field(esnap, "pulse_edge_score_bucket"),
            cex_agreement_bucket=self._edge_snap_field(esnap, "cex_agreement_bucket"),
            ob_pressure_bucket=self._edge_snap_ob_pressure(esnap),
            cvd_state=feat.get("cvd_state"),
            conviction=_conviction(fair_p_up),
            ttc_s=ttc_s,
            zscore_bucket=zscore_bucket,
            confidence_tier=confidence_tier,
            stale_divergence=self._edge_snap_field(esnap, "stale_divergence_class"),
            volume_state=feat.get("volume_state"),
            ask_price=ask_price,
        )

    def _up_side_tv_bias_ok(self, tv_feature: "dict | None",
                            ttc_s: "float | None" = None,
                            markov_state: "str | None" = None,
                            esnap=None,
                            fair_p_up: "float | None" = None,
                            dr=None,
                            rfeat=None,
                            ask_price: "float | None" = None) -> "tuple[bool, str]":
        """UP restrict-only: TV UP_STRONG plus down_bias pass (all entry modes)."""
        tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
        if not tv_ok:
            return False, tv_reason
        db_res = self._down_bias_eval(side="up", tv_feature=tv_feature,
                                      markov_state=markov_state, ttc_s=ttc_s, esnap=esnap,
                                      fair_p_up=fair_p_up,
                                      zscore_bucket=(rfeat.zscore_bucket if rfeat else None),
                                      confidence_tier=(self._entry_confidence_tier(dr)
                                                       if dr is not None else None),
                                      ask_price=ask_price)
        if db_res["decision"] in ("block", "explore"):
            return False, db_res["reasons"][0]
        return True, ""

    def _baseline_down_tv_context_ok(self, tv_feature: "dict | None") -> "tuple[bool, str]":
        """Block DOWN in proven-losing bullish TV stacks (15m evening loss cluster)."""
        if not self.cfg.baseline_down_tv_gate_enabled:
            return True, ""
        feat = tv_feature or {}
        mtf = str(feat.get("mtf_alignment") or "").strip().lower()
        range_state = str(feat.get("range_state") or "").strip().lower()
        signal_level = str(feat.get("signal_level") or "").strip().upper()
        volume_state = str(feat.get("volume_state") or "").strip().lower()
        tf_confirm = str(feat.get("tf_confirm") or "").strip().lower()
        bb_state = str(feat.get("bb_state") or "").strip().lower()
        if (self.cfg.baseline_down_block_bb_expansion_down
                and bb_state == "expansion_down"):
            return False, "baseline_down_tv_bb_expansion_down"
        if self.cfg.baseline_down_block_single_tf and tf_confirm == "single_tf":
            return False, "baseline_down_tv_single_tf"
        if self.cfg.baseline_down_block_volume_active and volume_state == "active":
            return False, "baseline_down_tv_volume_active"
        if self.cfg.baseline_down_block_bullish_mtf and mtf == "bullish_aligned":
            return False, "baseline_down_tv_bullish_mtf"
        if (self.cfg.baseline_down_block_up_strong_range_top
                and signal_level == "UP_STRONG" and range_state == "range_top"
                and mtf != "bullish_aligned"):
            return False, "baseline_down_tv_up_strong_range_top"
        if self.cfg.baseline_down_block_bullish_range:
            if mtf == "bullish_aligned" and range_state in ("range_top", "breakout_up"):
                return False, "baseline_down_tv_bullish_range_top"
            if signal_level == "UP_STRONG" and range_state == "breakout_up":
                return False, "baseline_down_tv_up_strong_breakout"
        if self.cfg.baseline_down_block_up_strong_bullish:
            if signal_level == "UP_STRONG" and mtf == "bullish_aligned":
                return False, "baseline_down_tv_up_strong_bullish"
        return True, ""

    def _baseline_up_tv_strength_ok(self, tv_feature: "dict | None") -> "tuple[bool, str]":
        """Baseline UP requires fresh TV UP_STRONG (direction UP, strength >= 0.8)."""
        if not self.cfg.baseline_up_tv_gate_enabled:
            return True, ""
        if not tv_feature:
            return False, "baseline_up_tv_missing"
        direction = str(tv_feature.get("direction") or "").upper()
        if direction != "UP":
            return False, "baseline_up_tv_opposes"
        try:
            strength = float(tv_feature.get("strength"))
        except (TypeError, ValueError):
            return False, "baseline_up_tv_strength_missing"
        if strength < 0.8:
            return False, "baseline_up_tv_weak"
        level = str(tv_feature.get("signal_level") or "").upper()
        if level != "UP_STRONG":
            return False, "baseline_up_tv_not_strong"
        return True, ""

    def _edge_snap_field(self, esnap, field: str):
        if esnap is None:
            return None
        val = getattr(esnap, field, None)
        if val is None and isinstance(esnap, dict):
            val = esnap.get(field)
        return val

    def _edge_snap_ob_pressure(self, esnap) -> "str | None":
        obp = self._edge_snap_field(esnap, "orderbook_pressure")
        if isinstance(obp, dict):
            return obp.get("bucket")
        return None

    def _mispricing_follow_up_ok(self, esnap=None,
                                 tv_feature: "dict | None" = None) -> "tuple[bool, str]":
        """UP mispricing-follow needs TV UP_STRONG + proven Grok UP edge + high score + CEX agree."""
        tv_ok, tv_reason = self._baseline_up_tv_strength_ok(tv_feature)
        if not tv_ok:
            return False, f"misprice_{tv_reason}"
        if not self._grok_up_side_allowed():
            return False, "misprice_up_grok_no_edge"
        bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        if bucket not in ("high", "very_high"):
            return False, "misprice_up_low_edge_score"
        if self._edge_snap_field(esnap, "cex_agreement_bucket") != "strong":
            return False, "misprice_up_weak_cex_agreement"
        return True, ""

    def _mispricing_follow_entry(self, cex_sig: "dict | None", ttc_s: "float | None",
                                 esnap=None, tv_feature: "dict | None" = None) -> "dict | None":
        """When Grok abstains, follow a confirmed CEX-lead mispricing stack (gates pre-checked)."""
        if not self.cfg.mispricing_follow_on_abstain:
            return None
        cl = cex_sig or {}
        side = cl.get("side")
        if side not in ("up", "down"):
            return None
        if side == "up" and self.cfg.directional_up_restrictions_enabled:
            self._mispricing_gate_counts["misprice_up_side_disabled"] = (
                self._mispricing_gate_counts.get("misprice_up_side_disabled", 0) + 1)
            return None
        mp_ok, _ = self._mispricing_gate_ok(side=side, cex_sig=cl, ttc_s=ttc_s, esnap=esnap)
        et_ok, _ = self._edge_ttc_gate_ok(esnap=esnap, ttc_s=ttc_s)
        if not (mp_ok and et_ok):
            return None
        cex_p = cl.get("cex_p_up")
        if cex_p is None:
            return None
        p_win = float(cex_p) if side == "up" else (1.0 - float(cex_p))
        size_frac = max(0.25, min(1.0, float(self.cfg.mispricing_follow_size_fraction)))
        return {"side": side, "p_win": p_win, "size_frac": size_frac}

    def _mispricing_gate_ok(self, *, side: str, cex_sig: "dict | None", ttc_s: "float | None",
                            esnap=None, window_seconds: int = 300) -> "tuple[bool, str]":
        """Restrict-only: Grok-follow trades require aligned CEX-lead mispricing in the TTC window."""
        if not self.cfg.mispricing_gate_enabled:
            return True, ""
        sig = cex_sig or {}
        if not sig.get("has_signal"):
            return False, "misprice_no_cex_signal"
        try:
            div = abs(float(sig.get("divergence") or 0))
        except (TypeError, ValueError):
            return False, "misprice_no_cex_signal"
        if div < float(self.cfg.cex_lead_min_divergence):
            return False, "misprice_divergence_too_small"
        if str(sig.get("side") or "") != str(side):
            return False, "misprice_side_mismatch"
        if self.cfg.mispricing_require_confirmed and not sig.get("confirmed"):
            return False, "misprice_not_confirmed"
        if ttc_s is None:
            return False, "misprice_ttc_unknown"
        ttc_f = float(ttc_s)
        scale = float(window_seconds or 300) / 300.0
        ttc_min = float(self.cfg.mispricing_ttc_min_s) * scale
        ttc_max = float(self.cfg.mispricing_ttc_max_s) * scale
        if ttc_f < ttc_min or ttc_f > ttc_max:
            return False, "misprice_ttc_out_of_window"
        if side == "down" and self.cfg.mispricing_require_stale_down:
            stale = getattr(esnap, "stale_divergence_class", None) if esnap is not None else None
            if stale is None and isinstance(esnap, dict):
                stale = esnap.get("stale_divergence_class")
            if stale != "stale_polymarket_down":
                return False, "misprice_stale_down_required"
        return True, ""

    def _edge_ttc_gate_ok(self, *, esnap=None, ttc_s: "float | None" = None,
                          window_seconds: int = 300) -> "tuple[bool, str]":
        """Restrict-only: block mid/late TTC unless pulse_edge_score is high or very_high."""
        if not self.cfg.edge_ttc_gate_enabled or ttc_s is None:
            return True, ""
        ttc_f = float(ttc_s)
        scale = float(window_seconds or 300) / 300.0
        mid_lo = 90.0 * scale
        mid_hi = 180.0 * scale
        late_thr = 240.0 * scale
        bucket = self._edge_snap_field(esnap, "pulse_edge_score_bucket")
        if mid_lo <= ttc_f < mid_hi:
            if bucket not in ("high", "very_high"):
                return False, "edge_ttc_mid_window_low_score"
        if ttc_f >= late_thr and bucket not in ("high", "very_high"):
            return False, "edge_ttc_late_window_low_score"
        return True, ""

    def _executable_mispricing_ok(self, *, p_win: "float | None",
                                  ask: "float | None") -> "tuple[bool, str]":
        """Restrict-only: require p_win - ask - edge_buffer >= min executable margin."""
        if not self.cfg.mispricing_gate_enabled:
            return True, ""
        if p_win is None or ask is None:
            return False, "misprice_executable_unknown"
        margin = float(p_win) - float(ask) - float(self.cfg.edge_buffer)
        if margin < float(self.cfg.mispricing_min_executable_margin):
            return False, "misprice_executable_margin_low"
        return True, ""

    def _tv_signal_gate(self, tv_feature: "dict | None", side: "str | None") -> "str | None":
        """Restrict-only TradingView indication gate. Returns None if the trade is permitted, else
        a rejection reason. Only ACTIVE when the intake exists; it can never force a trade."""
        if not self.cfg.tradingview_signal_gate_enabled or self.tradingview is None:
            return None
        if not tv_feature:
            return "tv_gate_no_signal"            # no fresh TradingView indication -> don't trade
        direction = str(tv_feature.get("direction") or "").upper()
        if direction == "FLAT":
            return "tv_gate_flat_signal"
        want = "up" if direction == "UP" else ("down" if direction == "DOWN" else None)
        if want is None:
            return "tv_gate_no_direction"
        if side != want:
            return "tv_gate_opposes_signal"       # bot side disagrees with the TradingView signal
        min_str = float(self.cfg.tradingview_min_signal_strength or 0.0)
        if min_str > 0:
            try:
                strength = float(tv_feature.get("strength"))
            except (TypeError, ValueError):
                strength = None
            if strength is None or strength < min_str:
                return "tv_gate_weak_signal"
        return None

    def _learning_weight(self) -> "tuple[float, str]":
        """How much the learned edge model influences the directional probability. Influence is
        EARNED (ramps with sample count past the minimum), GATED (only when calibrated), and
        SELF-DISABLING (0 if calibration error exceeds the cap). Returns (weight, reason)."""
        if not self.cfg.learning_enabled or self.edge_model is None:
            return 0.0, "disabled"
        if self.edge_model.n_labeled < self.cfg.learning_min_samples:
            return 0.0, "insufficient_samples"
        ece = self.edge_model.calibration_error()
        if ece is None:
            return 0.0, "calibration_unknown"
        if ece > self.cfg.learning_max_calib_error:
            return 0.0, "calibration_degraded"          # auto-disable a miscalibrated model
        # MARKET-BEATING GATE (kills phantom edge): a calibrated model is not necessarily MORE
        # accurate than the Polymarket price. Only blend when the model's out-of-sample Brier
        # actually beats the market's by the required margin over enough graded windows.
        bench = self._market_benchmark()
        if (bench["n"] >= self.cfg.learning_bench_min_samples
                and bench["model_brier"] is not None and bench["market_brier"] is not None
                and bench["model_brier"] > bench["market_brier"] - self.cfg.learning_bench_margin):
            return 0.0, "model_not_beating_market"
        ramp = max(1.0, float(self.cfg.learning_ramp_samples))
        progress = (self.edge_model.n_labeled - self.cfg.learning_min_samples) / ramp
        weight = self.cfg.learning_max_weight * min(1.0, max(0.0, progress))
        return round(weight, 4), "active"

    def _learning_report(self) -> dict:
        weight, reason = self._learning_weight()
        return {"enabled": bool(self.cfg.learning_enabled),
                "active": weight > 0, "weight": weight, "reason": reason,
                "paper_only": True, "execution_gate_still_authoritative": True,
                "max_weight": self.cfg.learning_max_weight,
                "min_samples": self.cfg.learning_min_samples,
                "ramp_samples": self.cfg.learning_ramp_samples,
                "max_calibration_error": self.cfg.learning_max_calib_error,
                "model_n_labeled": (self.edge_model.n_labeled if self.edge_model else 0),
                "model_calibration_error": (self.edge_model.calibration_error()
                                            if self.edge_model else None),
                "market_benchmark": self._market_benchmark(),
                "note": ("the bot's own settled-trade experience (calibrated edge model) adjusts "
                         "the directional probability; it grows as more trades settle. The "
                         "execution-quality gate, paper-realism, and reconciliation are unchanged "
                         "and still veto every trade — learning can never bypass them.")}

    def _global_reconciliation(self) -> dict:
        from engine.pulse.reconciliation import global_reconciliation
        return global_reconciliation(
            lifecycle=self.reconciler.report(), exec_gate=self.ledger.exec_gate_stats(),
            ledger_stats=self.ledger.stats(), baseline=(self._baseline or empty_baseline()))

    def _gate_thresholds(self) -> dict:
        """The configured execution-gate thresholds (for the zero-reject diagnostic)."""
        return {"size_usd": self.cfg.size_usd, "max_spread": self.cfg.exec_max_spread,
                "min_depth_usd": self.cfg.min_depth_usd,
                "min_order_usd": self.cfg.exec_min_order_usd,
                "max_depth_consume_frac": self.cfg.exec_max_depth_consume_frac,
                "min_ev_after_slippage": self.cfg.exec_min_ev_after_slippage,
                "min_seconds_to_close": self.cfg.min_seconds_to_close,
                "max_book_age_s": self.cfg.exec_max_book_age_s}

    def light_report(self) -> dict:
        """The latest light report (report-only): full lifecycle reconciliation, exec stats,
        reject reasons, EV before/after costs, PnL grouped by every bucket dimension, calibration,
        sample sizes, missing-data reasons, and promotion/demotion candidates."""
        self._repair_accounting_drift()
        from engine.pulse.reporting import build_light_report
        ev_stats = {"n": self._ev_n,
                    "avg_ev_before_costs": (round(self._ev_before_sum / self._ev_n, 6)
                                            if self._ev_n else None),
                    "avg_ev_after_costs": (round(self._ev_after_sum / self._ev_n, 6)
                                           if self._ev_n else None)}
        miss = self.research.report().get("missing_data_reasons", {}) if self.research else {}
        report = build_light_report(
            lifecycle=self.reconciler.report(), execution_gate=self.ledger.exec_gate_stats(),
            ledger_stats=self.ledger.stats(), calibration=self.calib.to_dict(),
            ev_stats=ev_stats, outcome_groups=self._groups, tier_table=self._tier_report(),
            edge_model=(self.edge_model.report() if self.edge_model else {}),
            sizing={"enabled": self.cfg.sizing_enabled, "actual_size_usd": self.cfg.size_usd},
            missing_data_reasons=miss, baseline=(self._baseline or empty_baseline()),
            gate_thresholds=self._gate_thresholds(), gate_observations=self.gate_obs.ranges())
        report["readiness"] = self.readiness()
        report["tradingview"] = self._tradingview_report()
        report["down_stack"] = self.down_stack.report()
        report["learning"] = self._learning_report()
        report["capital"] = self._capital_status()
        report["grok_signal_intel"] = self._grok_intel_report()
        report["grok_decider"] = self._grok_decider_report()
        report["verifier"] = (self.verifier.report() if self.verifier is not None
                              else {"enabled": False})
        report["research_loop"] = (self.research_loop.report() if self.research_loop is not None
                                   else {"enabled": False})
        if isinstance(report["research_loop"], dict):
            report["research_loop"]["auto_applied_avoid_contexts"] = sorted(self._research_avoid)
            report["research_loop"]["auto_applied_exploit_contexts"] = sorted(self._research_exploit)
        report["lessons"] = self.lessons.report()
        report["loops"] = self._loops_report()
        report["edge_signal"] = self._edge_signal_report()
        report["cex_lead_edge"] = (self.cex_lead.report() if self.cex_lead is not None
                                   else {"enabled": False})
        report["arbitrage"] = (self.arb_ledger.report() if self.arb_ledger is not None
                               else {"enabled": False})
        report["dependency_arbitrage"] = (self.dep_arb_ledger.report()
                                          if self.dep_arb_ledger is not None
                                          else {"enabled": False})
        report["arb_graph"] = getattr(self, "_arb_graph_report", None) or {"nodes": 0}
        report["grok_dependency"] = getattr(self, "_grok_dependency_report", None) or {
            "dependency_proposals": 0}
        report["bregman_projection"] = getattr(self, "_bregman_projection_report", None) or {
            "enabled": False}
        report["clob_feed"] = (
            self.clob_feed.latency_report() if getattr(self, "clob_feed", None) else {})
        report["walk_forward"] = self._walk_forward_status()
        report["series_architecture"] = {
            "design": "5m_brain_15m_hands",
            "scan_slugs": list(self.cfg.pulse_series_slugs),
            "directional_slugs": list(self.cfg.directional_series_slugs),
        }
        report["profit_discovery"] = self._profit_discovery_status()
        report["five_x_improvement"] = report["profit_discovery"]
        report["directional_risk"] = {
            "max_bankroll_frac": self.cfg.directional_max_bankroll_frac,
            "bankroll_cap_usd": round(
                float(self.cfg.starting_capital_usd) * float(self.cfg.directional_max_bankroll_frac),
                2),
            "open_exposure_usd": round(self._directional_open_exposure(), 2),
            "block_up_until_promoted": bool(self.cfg.directional_block_up_until_promoted),
            "directional_down_only": bool(self.cfg.directional_down_only),
            "directional_series_slugs": list(self.cfg.directional_series_slugs),
            "arb_series_slugs": list(self.cfg.pulse_series_slugs),
            "research_auto_apply": bool(self.cfg.research_auto_apply),
            "research_forbid_size_increase": bool(self.cfg.research_forbid_size_increase),
            "up_promoted": self._up_direction_promoted(),
        }
        report["directional_allowlist"] = {
            "enabled": bool(self.cfg.directional_require_winning_bucket),
            "explore_rate": self.cfg.directional_explore_rate,
            "explored": self._allowlist_explored, "blocked": self._allowlist_blocked}
        from engine.pulse.reporting import ledger_stats_by_market_series
        report["by_market_series"] = ledger_stats_by_market_series(self.ledger.positions)
        report["markets_feed"] = (self.market.report() if hasattr(self.market, "report")
                                  else {"multi_series": False})
        report["baseline_cohort_gate"] = self._baseline_cohort_gate_report()
        report["learned_selectivity_gate"] = self._selectivity_report()
        report["late_window_entry"] = self._late_window_report()
        report["stop_conditions"] = self.stop_monitor.report()
        from engine.pulse.execution_realistic import aggregate_report
        bench = self._market_benchmark()
        kl_agg = {
            "observe_only": True,
            "latest_model_p": None,
            "latest_market_p": None,
            "kl": None,
            "market_benchmark_n": bench.get("n"),
            "model_beats_market": bench.get("model_beats_market"),
        }
        if bench.get("n"):
            kl_agg["market_benchmark_n"] = bench["n"]
        report["execution_realistic_edge"] = aggregate_report(
            samples=self._exec_realistic_samples,
            payoff_guards=self._payoff_guard_counts,
            kl_aggregate=kl_agg,
        )
        report["simplex_diagnostics"] = self._last_simplex
        from engine.pulse.reporting import build_report_sections
        report["sections"] = build_report_sections(
            report, status={"ticks": self.ticks}, ledger=self.ledger.to_dict())
        from engine.pulse.performance_scoring import compute_report_scores
        report["scores"] = compute_report_scores(
            report["sections"], global_reconciled=bool(report.get("global_reconciled")))
        report["score_history"] = self._score_history.to_dict()
        report["schema"] = "btc_pulse_light_report/1.3"
        return report

    def _late_window_report(self) -> dict:
        """Late-window high-conviction entry mode (gate) + observe-only time-decay edge grade."""
        return {"gate": self.late_window_gate.report(),
                "edge_measurement": self.late_window_edge.report()}

    def _selectivity_report(self) -> dict:
        """Learned Selectivity Gate report: counts, reject reasons, per-decision PnL/win-rate, and
        the counterfactual replay over the existing ledger."""
        return self.selectivity_gate.report(evidence=self.selectivity_evidence,
                                             positions=self._selectivity_positions())

    def _edge_signal_report(self) -> dict:
        """Observe-only BTC Pulse Edge Signal report (CEX coverage, bucketed PnL/win/EV,
        best/worst-after-cost, promotion diagnostics)."""
        if self.edge_signal is None:
            return {"enabled": False, "observe_only": True, "affects_trading": False}
        return {"enabled": True, **self.edge_signal.report(
            now=self.last_tick_ts or time.time(),
            promotion_allowed=self.cfg.edge_promotion_allowed,
            min_samples=self.cfg.edge_promotion_min_samples,
            min_win_rate=self.cfg.edge_promotion_min_win_rate)}

    def _grok_analyst_report(self) -> dict:
        """Snapshot the bot's GROWING learned evidence for the Grok batch analyst (observe-only), so
        Grok learns the bot's trading patterns and scrubs the data better as the bot accumulates
        experience: settled-trade bucket performance, the learned-selectivity bucket evidence
        (win-rate vs its own breakeven + confidence), the late-window time-decay edge, gate stats,
        edge-model calibration, and the TradingView signal learning."""
        try:
            rep = {"signal_learning": self._tv_learner.report(
                        promotion_allowed=self.cfg.tradingview_promotion_allowed,
                        min_samples=self.cfg.tradingview_promotion_min_samples,
                        min_win_rate=self.cfg.tradingview_promotion_min_win_rate),
                   "edge_vs_5min_outcome": self._tv_edge.report(),
                   "rsi_trend": self._rsi_model.report(),
                   "ledger": self.ledger.stats()}
            # the bot's OWN learned trading patterns (this is what makes Grok "grow with the bot")
            rep["learned_pnl_by_bucket"] = self._groups.summary()
            rep["learned_selectivity"] = self.selectivity_gate.report(
                evidence=self.selectivity_evidence, positions=self._selectivity_positions())
            rep["late_window_edge"] = self.late_window_edge.report()
            rep["context_gate"] = self.tv_context_gate.report()
            rep["reward_risk_floor"] = {"min_reward_risk": self.cfg.min_reward_risk}
            if self.edge_model is not None:
                em = self.edge_model.report()
                rep["edge_model"] = {"n_labeled": em.get("n_labeled"),
                                     "calibration_error": em.get("calibration_error"),
                                     "calibration_table": em.get("calibration_table")}
            return rep
        except Exception:  # noqa: BLE001
            return {}

    def _record_lessons_from_settlement(self, pos) -> None:
        """Turn proven outcomes into compounding rules (deduped): avoid confidently-losing buckets,
        exploit proven-edge contexts, and note breaker trips. Fed back to maker + checker."""
        try:
            new_lesson = False
            now = self.last_tick_ts or time.time()
            self.loops.beat("lessons", now)
            self.loops.beat("risk_monitor", now)
            active_keys = set()              # (kind,key) currently evidence-backed -> not retracted
            be = self.selectivity_gate.bucket_evidence(self.selectivity_evidence, top=8)
            for r in be.get("buckets", []):
                if r.get("confidently_losing"):
                    key = "sel:%s=%s" % (r["dimension"], r["bucket"])
                    active_keys.add(("avoid", key))
                    new_lesson |= self.lessons.add(
                        kind="avoid", key=key,
                        rule=("AVOID %s=%s — confidently below breakeven (WR %s vs %s, n %s, "
                              "EV/trade %s)." % (r["dimension"], r["bucket"], r.get("win_rate"),
                              r.get("breakeven_win_rate"), r.get("n"), r.get("ev_per_trade"))), now=now)
            if self.grok_decider is not None:
                for c in (self.grok_decider.report().get("view_edge_candidates") or []):
                    key = "edge:%s=%s" % (c["dimension"], c["bucket"])
                    active_keys.add(("exploit", key))
                    if self.lessons.add(
                            kind="exploit", key=key,
                            rule=("EXPLOIT %s=%s — Grok's directional view is a real edge (acc %s, "
                                  "lowerCI %s, n %s)." % (c["dimension"], c["bucket"], c.get("accuracy"),
                                  c.get("accuracy_lower_ci"), c.get("n"))), now=now):
                        new_lesson = True
                        if self.research_loop is not None:
                            self.research_loop.request_run("new_edge")
                br = self.grok_decider.breaker_status()
                if br.get("tripped"):
                    day = int((self.last_tick_ts or time.time()) // 86400)
                    if self.lessons.add(kind="risk", key="breaker:%s:%s" % (br.get("reason"), day),
                                        rule="Circuit breaker tripped (%s) — follow paused, baseline "
                                             "only." % br.get("reason")):
                        new_lesson = True
                        if self.research_loop is not None:
                            self.research_loop.request_run("breaker")
            # RETRACT avoid/exploit lessons no longer backed by live evidence (regime changed) so the
            # maker/checker stop reading stale rules. Risk (breaker) lessons are historical, not synced.
            retracted = self.lessons.sync(active_keys=active_keys, now=now).get("n", 0)
            # event-trigger the research meta-loop on any new/retracted lesson + a fresh-sample cadence
            if self.research_loop is not None:
                if new_lesson or retracted:
                    self.research_loop.request_run("new_lesson" if new_lesson else "lesson_retracted")
                elif int(self.ledger.stats().get("settled", 0) or 0) % 15 == 0:
                    self.research_loop.request_run("fresh_samples")
        except Exception:  # noqa: BLE001 — lessons never break settlement
            pass

    def _research_report(self) -> dict:
        """Compact report for the research meta-loop: full light report + the compounding lessons."""
        try:
            rep = self.light_report()
            rep["lessons"] = self.lessons.recent(20)
            return rep
        except Exception:  # noqa: BLE001
            return {"lessons": self.lessons.recent(20)}

    # research dimensions -> selectivity-tag dimensions (so Claude's avoid_contexts map to live tags)
    _RESEARCH_DIM_ALIAS = {"regime": "hurst_regime", "hurst": "hurst_regime",
                           "edge_quality": "edge_quality_bucket", "confidence": "confidence_tier",
                           "spread": "spread_bucket", "depth": "depth_bucket",
                           "zscore": "zscore_bucket", "ttc": "ttc_bucket"}
    # NOTE: "direction" is EXCLUDED (a whole side is too coarse); "depth_bucket"/"spread_bucket" are
    # EXCLUDED too — they are liquidity ATTRIBUTES, not directional edge contexts, and blocking them
    # (e.g. depth>=1000 = most of the book) would freeze nearly all trading. We avoid losing
    # directional CONTEXTS only.
    _RESEARCH_AVOID_DIMS = {"hurst_regime", "zscore_bucket", "ttc_bucket", "confidence_tier",
                            "markov_state", "edge_quality_bucket", "stale_divergence"}

    def _research_rule_evidence_backed(self, dim: str, bucket: str) -> bool:
        """MAKER-CHECKER: only auto-apply a Claude-proposed avoid-rule if the bot's OWN live evidence
        CONFIDENTLY proves that bucket is losing (Wilson upper bound < its breakeven + net-negative),
        the SAME bar the selectivity gate uses. This grounds the self-improving loop in data and stops
        the LLM from hallucinating / over-broad blocks (e.g. a confidence tier that doesn't exist)."""
        try:
            st = self.selectivity_evidence.stat(dim, bucket)
            if not st or st["n"] < self.selectivity_gate.min_samples:
                return False
            return bool(self.selectivity_gate._assess(st).get("confidently_losing"))
        except Exception:  # noqa: BLE001
            return False

    def _research_exploit_backed(self, dim: str, bucket: str) -> bool:
        """MAKER-CHECKER for the EXPLOIT side: only promote a Claude-proposed exploit-context if the
        bot's OWN data CONFIDENTLY proves it WINNING — Wilson LOWER bound of win-rate above the
        bucket's own breakeven AND net-positive PnL. Mirrors the avoid checker; grounds size-ups in
        evidence, never opinion."""
        try:
            from engine.pulse.cex_lead import _wilson_lower
            from engine.pulse.selectivity import breakeven_win_rate
            st = self.selectivity_evidence.stat(dim, bucket)
            if not st or st["n"] < self.selectivity_gate.min_samples or st["pnl_usd"] <= 0:
                return False
            n = int(st["n"]); wins = int(round(float(st["win_rate"]) * n))
            wl = _wilson_lower(wins, n, self.selectivity_gate.confidence_z)
            be = breakeven_win_rate(st["avg_win"], st["avg_loss"])
            return wl is not None and wl > be
        except Exception:  # noqa: BLE001
            return False

    def _research_apply(self, note: dict) -> list:
        """Bounded, evidence-gated, SAFETY-only auto-apply of the research loop's avoid_contexts:
        turn a Claude proposal into a hard block ONLY when the bot's own data confirms it is
        confidently losing (maker-checker). Only-more-selective, capped, deduplicated; never loosens a
        gate, changes size, enables live, or applies exploit/knob nudges. Closes the self-improving
        loop on EVIDENCE, not opinion."""
        applied = []
        for ctx in (note.get("avoid_contexts") or []):
            if "=" not in str(ctx):
                continue
            dim, _, bucket = str(ctx).partition("=")
            dim = dim.strip().lower()
            bucket = bucket.strip()
            for sep in (" (", "(", " "):                     # drop any prose the model appended
                if sep in bucket:
                    bucket = bucket.split(sep, 1)[0]
            bucket = bucket.strip().strip(",").strip().lower()   # tags are lowercase
            cdim = self._RESEARCH_DIM_ALIAS.get(dim, dim)
            if cdim not in self._RESEARCH_AVOID_DIMS or not bucket:
                continue
            if not self._research_rule_evidence_backed(cdim, bucket):   # maker-checker on live data
                continue
            key = "%s=%s" % (cdim, bucket)
            if key not in self._research_avoid and len(self._research_avoid) < self.cfg.research_avoid_max:
                self._research_avoid.add(key)
                applied.append(key)
        # EXPLOIT side (dual of avoid): promote Claude exploit-contexts that the data proves WINNING
        for ctx in (note.get("exploit_contexts") or []):
            if "=" not in str(ctx):
                continue
            dim, _, bucket = str(ctx).partition("=")
            dim = dim.strip().lower()
            bucket = bucket.strip()
            for sep in (" (", "(", " "):
                if sep in bucket:
                    bucket = bucket.split(sep, 1)[0]
            bucket = bucket.strip().strip(",").strip().lower()
            cdim = self._RESEARCH_DIM_ALIAS.get(dim, dim)
            if cdim not in self._RESEARCH_AVOID_DIMS or not bucket:
                continue
            if not self._research_exploit_backed(cdim, bucket):
                continue
            key = "%s=%s" % (cdim, bucket)
            if (key not in self._research_exploit
                    and len(self._research_exploit) < self.cfg.research_exploit_max):
                self._research_exploit.add(key)
                applied.append("exploit:" + key)
        return applied

    def _arb_open_exposure(self) -> float:
        if self.arb_ledger is None:
            return 0.0
        return sum(float(p.get("cost_usd") or 0.0)
                   for p in self.arb_ledger.positions.values()
                   if p.get("status") == "open")

    def _directional_series_allowed(self, w) -> bool:
        """Directional trades only on configured series (arb/dependency still scan all)."""
        allowed = tuple(self.cfg.directional_series_slugs or ())
        if not allowed:
            return True
        return str(getattr(w, "series_slug", "") or "") in allowed

    def _directional_up_blocked(self, side: Optional[str]) -> tuple:
        """Return (blocked, reason) for directional UP — no grok/cex bypass when down_only."""
        if str(side or "").lower() != "up":
            return False, ""
        if bool(getattr(self.cfg, "directional_down_only", False)):
            return True, "directional_down_only"
        if (self.cfg.directional_block_up_until_promoted
                and not self._up_direction_promoted()):
            return True, "up_blocked_until_promoted"
        return False, ""

    def _arb_epsilon_for(self, w) -> float:
        ws = int(getattr(w, "window_seconds", 0) or 0)
        if ws >= 900:
            return float(self.cfg.arb_epsilon_15m)
        return float(self.cfg.arb_epsilon_5m)

    def _scan_arbitrage_all_windows(self, windows: list, now: float) -> None:
        """ARB-FIRST pass: scan every open window (5m+15m) without directional vol/snapshot gates."""
        if self.arb_ledger is None or self.stop_monitor.is_halted("arbitrage"):
            return
        from engine.pulse.arbitrage import detect_arbitrage
        open_exp = self._arb_open_exposure()
        cap = float(self.cfg.arb_global_max_open_usd)
        for w in windows:
            if now < w.open_ts or w.seconds_to_close(now) <= 0:
                continue
            if self.arb_ledger.has_arb(w.event_id):
                continue
            if open_exp >= cap - 1e-6:
                continue
            self.market.hydrate_books(w)
            room = max(0.0, cap - open_exp)
            # Matched dutch-book cost ≈ 2× per-leg notional when legs share similar VWAP.
            max_usd = min(float(self.cfg.arb_max_usd), room * 0.5) if room > 0 else 0.0
            if max_usd <= 0:
                continue
            window_eps = self._arb_epsilon_for(w)
            _arb_kw = dict(
                size_usd=self.cfg.arb_size_usd, fees=self.cfg.arb_fees,
                epsilon=window_eps,
                max_depth_consume_frac=self.cfg.exec_max_depth_consume_frac,
                tick_size=w.tick_size, now=now, max_book_age_s=self.cfg.exec_max_book_age_s,
                min_profit_usd=self.cfg.arb_min_profit_usd, max_usd=max_usd,
                nonatomic_check=bool(self.cfg.arb_nonatomic_enabled),
                nonatomic_slippage_bps=self.cfg.arb_nonatomic_slippage_bps)
            opp = detect_arbitrage(w.up_book, w.down_book, **_arb_kw)
            if opp is not None and opp.actionable:
                cost = float(opp.cost_usd or 0.0)
                if cost > room + 1e-6 and cost > 0:
                    shrink = max(0.0, max_usd * room / cost * 0.99)
                    if shrink > 0:
                        opp = detect_arbitrage(w.up_book, w.down_book, **{**_arb_kw,
                                                                            "max_usd": shrink})
            self.arb_ledger.record_scan(
                opp, near_miss_eps=max(0.02, window_eps),
                window_key=w.event_id, series_label=getattr(w, "series_label", None))
            if opp is None:
                continue
            if opp.kind == "sell_both":
                self.arb_ledger.sell_both_detected += 1
            if opp.actionable:
                cost = float(opp.cost_usd or 0.0)
                if open_exp + cost > cap + 1e-6:
                    continue
                self.arb_ledger.detected += 1
                if self.arb_ledger.book(w.event_id, opp, close_ts=w.close_ts, now=now,
                                        series_label=getattr(w, "series_label", None)):
                    open_exp += cost
                    self.loops.beat("arbitrage", now)

    def _scan_dependency_arb(self, windows: list, now: float) -> None:
        """LCMM dependency scan; optional paper execution on validated violations (WS4)."""
        if self.dep_arb_ledger is None:
            return
        open_w = [w for w in windows if w.open_ts <= now < w.close_ts]
        if not open_w:
            return
        for w in open_w:
            if w.up_book is None or w.down_book is None:
                self.market.hydrate_books(w)
        from engine.pulse.arb_graph import MarketGraph
        from engine.pulse.dependency_arb import (
            scan_windows, validate_violation, try_execute_nested_implication,
        )
        from engine.pulse.grok_dependency import validate_grok_proposals

        graph = MarketGraph().build_from_windows(open_w)
        grok_props = []
        if self.grok_dep_screener is not None:
            grok_props = list(self.grok_dep_screener.latest_proposals() or [])
        grok_props = grok_props or list(getattr(self, "_grok_dependency_proposals", None) or [])
        if grok_props:
            graph.add_grok_proposals(grok_props)
        self._arb_graph_report = graph.report()
        self._grok_dependency_report = validate_grok_proposals(
            grok_props, windows_by_id={w.event_id: w for w in open_w})

        eps = max(0.01, float(self.cfg.dependency_arb_epsilon))
        violations = scan_windows(
            open_w, epsilon=eps, max_usd=self.cfg.dependency_arb_max_usd,
            vwap_enrich=True)

        bregman_by_parent: dict[str, dict] = {}
        if self.cfg.bregman_projection_enabled:
            from engine.pulse.bregman_projection import project_dependency_group
            fw_kw = dict(
                alpha=self.cfg.bregman_alpha,
                epsilon_init=self.cfg.bregman_epsilon_init,
                max_iterations=self.cfg.bregman_fw_max_iters,
                time_budget_ms=self.cfg.bregman_fw_time_budget_ms,
                ip_backend=self.cfg.ip_oracle_backend,
            )
            samples = []
            for v in violations:
                if (v.constraint_type == "nested_implication" and v.parent_up_mid is not None
                        and v.child_up_mids):
                    diag = project_dependency_group(
                        v.parent_up_mid, v.child_up_mids[0], epsilon=eps,
                        use_frank_wolfe=True, fw_kwargs=fw_kw)
                    samples.append(diag)
                    bregman_by_parent[str(v.parent_window_key)] = diag
            authority = bool(self.cfg.bregman_trade_authority)
            self._bregman_projection_report = {
                "enabled": True,
                "trade_authority": authority,
                "samples": samples[-12:],
                "frank_wolfe": {
                    "max_iters": self.cfg.bregman_fw_max_iters,
                    "time_budget_ms": self.cfg.bregman_fw_time_budget_ms,
                    "ip_backend": self.cfg.ip_oracle_backend,
                },
                "note": ("Bregman+FW Layer-2; trade authority=%s" % authority),
            }
        else:
            self._bregman_projection_report = {"enabled": False}

        self.dep_arb_ledger.record_scan(violations)
        if not self.dep_arb_ledger.execute_enabled:
            return
        by_id = {w.event_id: w for w in open_w}
        for v in violations:
            if not v.actionable:
                continue
            ok, reason = validate_violation(v)
            if not ok:
                self.dep_arb_ledger.rejected_invalid += 1
                continue
            if self.dep_arb_ledger.has_open(v.parent_window_key):
                continue
            parent = by_id.get(v.parent_window_key)
            child_id = (v.child_window_keys or [None])[0]
            child = by_id.get(child_id) if child_id else None
            if parent is None or child is None:
                continue
            bdiag = bregman_by_parent.get(str(v.parent_window_key))
            trade = try_execute_nested_implication(
                parent, child, v, max_usd=self.cfg.dependency_arb_max_usd,
                epsilon=eps,
                bregman_diag=bdiag,
                bregman_authority=bool(self.cfg.bregman_trade_authority),
            )
            if trade and self.dep_arb_ledger.book(trade, now=now):
                self.loops.beat("dependency_arb", now)

    def _directional_open_exposure(self) -> float:
        exp = 0.0
        for pos in self.ledger.positions.values():
            if pos.status == "open":
                exp += float(getattr(pos, "size_usd", 0.0) or 0.0)
        return exp

    def _up_direction_promoted(self) -> bool:
        """True when direction=up bucket clears Wilson LB promotion (n>=min, PnL>0)."""
        return self._research_exploit_backed("direction", "up")

    def _wire_clob_feed_metrics(self) -> None:
        """Record REST book fetch latency on the CLOB feed dashboard."""
        feed = getattr(self, "clob_feed", None)
        mkt = getattr(self, "market", None)
        if feed is None or mkt is None:
            return

        def _on_fetch(token_id: str, elapsed_ms: float) -> None:
            feed.record_fetch(token_id, elapsed_ms)

        if hasattr(mkt, "_feeds"):
            for sub in mkt._feeds.values():
                sub.on_book_fetch = _on_fetch
        elif hasattr(mkt, "fetch_book"):
            mkt.on_book_fetch = _on_fetch

    def _walk_forward_status(self) -> dict:
        try:
            from engine.pulse.walk_forward import passes_walk_forward
            _dep_pos = list((self.dep_arb_ledger.positions or {}).values()
                            if self.dep_arb_ledger else [])
            return {
                "directional": passes_walk_forward(list(self.ledger.positions.values())),
                "dependency_arb": passes_walk_forward(_dep_pos, min_holdout_n=5),
            }
        except Exception:
            return {}

    def _profit_discovery_status(self) -> dict:
        """5x improvement tracker vs baseline; honest status only."""
        baseline_total = float(getattr(self, "_profit_baseline_usd", 35.95) or 35.95)
        ls = self.ledger.stats()
        arb_pnl = float((self.arb_ledger.realized_profit_usd if self.arb_ledger else 0.0) or 0.0)
        dir_pnl = float(ls.get("realized_pnl_usd") or 0.0)
        dep_pnl = float((self.dep_arb_ledger.realized_profit_usd
                         if self.dep_arb_ledger else 0.0) or 0.0)
        total = arb_pnl + dir_pnl + dep_pnl
        risk_free = arb_pnl + dep_pnl
        ratio = (total / baseline_total) if baseline_total > 0 else None
        target = 5.0
        proven = bool(ratio is not None and ratio >= target and risk_free >= total * 0.9)
        blockers = []
        if ratio is None or ratio < target:
            blockers.append("total_pnl_below_5x_baseline")
        if total > 0 and risk_free < total * 0.9:
            blockers.append("risk_free_not_dominant_source")
        arb_n = int(self.arb_ledger.executed if self.arb_ledger else 0)
        dep_n = int(self.dep_arb_ledger.executed if self.dep_arb_ledger else 0)
        if arb_n + dep_n < 8:
            blockers.append("insufficient_risk_free_sample")
        sources = (
            ("dependency_arbitrage", dep_pnl),
            ("arbitrage", arb_pnl),
            ("directional", dir_pnl),
        )
        primary = max(sources, key=lambda x: x[1])[0] if total > 0 else self.cfg.primary_edge_source
        return {"five_x_target": target, "baseline_total_pnl_usd": baseline_total,
                "current_total_pnl_usd": round(total, 4),
                "arb_pnl_usd": round(arb_pnl, 4), "directional_pnl_usd": round(dir_pnl, 4),
                "dependency_arb_pnl_usd": round(dep_pnl, 4),
                "risk_free_pnl_usd": round(risk_free, 4),
                "improvement_ratio": (round(ratio, 4) if ratio is not None else None),
                "five_x_improvement_status": ("proven" if proven else "not_proven_yet"),
                "primary_edge_source": primary,
                "top_blockers": blockers[:3]}

    def _directional_market_benchmark_ok(self) -> bool:
        """Directional allowlist requires model Brier <= market Brier once enough graded windows."""
        bench = self._market_benchmark()
        n = int(bench.get("n") or 0)
        if n < self.cfg.learning_bench_min_samples:
            return True
        if bench.get("model_brier") is None or bench.get("market_brier") is None:
            return True
        return bool(bench.get("model_beats_market"))

    def _any_winning_bucket(self, sel_tags: dict) -> bool:
        """True if ANY of the candidate's buckets is CONFIDENTLY WINNING (Wilson lower-bound win-rate
        above its breakeven, n>=min) per live evidence — the directional allowlist (Roan/loop-eng:
        only trade proven edges, not opinion). Reuses the same maker-checker test as research-exploit."""
        for dim, val in (sel_tags or {}).items():
            if dim == "direction" or val is None:
                continue
            if self._research_exploit_backed(dim, str(val)):
                return True
        return False

    def _research_exploit_hit(self, sel_tags: dict) -> bool:
        """True if a candidate's context matches a proven-winning research exploit-rule (never on
        'direction'). Used to SIZE UP proven-winning contexts (capped)."""
        if not self._research_exploit:
            return False
        for dim, val in (sel_tags or {}).items():
            if dim != "direction" and val is not None and (
                    "%s=%s" % (dim, str(val).lower())) in self._research_exploit:
                return True
        return False

    def _research_avoid_hit(self, sel_tags: dict):
        """Return the first sel_tag that matches an active research avoid-rule, else None. Never
        blocks on 'direction' (a whole side is too coarse); matching is case-insensitive."""
        if not self._research_avoid:
            return None
        for dim, val in (sel_tags or {}).items():
            if dim == "direction" or val is None:
                continue
            if ("%s=%s" % (dim, str(val).lower())) in self._research_avoid:
                return "%s=%s" % (dim, str(val).lower())
        return None

    def _loops_report(self) -> dict:
        """Loop registry with live verified stop-condition strings (refreshed each tick)."""
        rep = self.loops.report()
        loops = rep.get("loops") or {}
        for name, strat in (("directional", "directional"), ("arbitrage", "arbitrage")):
            if name in loops:
                loops[name]["stop_condition"] = self.stop_monitor.verified_stop_line(strat)
        return rep

    def _register_loops(self) -> None:
        """Formalize the sub-loops for uniform observability (#3)."""
        r = self.loops
        r.register("heartbeat", role="automation", trigger="tick",
                   interval_s=self.cfg.tick_seconds, skill="AGENTS.md",
                   stop_condition="process running")
        r.register("directional", role="strategy", trigger="per_window",
                   skill="digital model + allowlist",
                   stop_condition=self.stop_monitor.verified_stop_line("directional"),
                   status_fn=lambda: {"enabled": self.cfg.directional_enabled,
                                      "halted": self.stop_monitor.is_halted("directional")})
        r.register("data_ingestion", role="data", trigger="tick", skill="price/book/CEX/RTDS",
                   status_fn=lambda: {"enabled": True})
        if self.tradingview is not None:
            r.register("tradingview", role="context", trigger="webhook",
                       skill="TV alerts observe-only (feeds Grok/CEX context)",
                       stop_condition="ingest only — never trade authority",
                       status_fn=lambda: {
                           "enabled": True,
                           "received": self.tradingview.received,
                           "valid": self.tradingview.valid,
                           "rejected": self.tradingview.rejected,
                           "observe_only": True,
                       })
        r.register("signal_generation", role="signal", trigger="per_window",
                   skill="research/factors/markov/edge_model",
                   status_fn=(lambda: self._grok_decider_report()) if self.grok_decider else None)
        r.register("verifier", role="verify(maker-checker)", trigger="per_decision",
                   skill="independent Claude verdict", verifier="claude",
                   stop_condition="approve/veto verdict",
                   status_fn=(lambda: self.verifier.report()) if self.verifier else None)
        r.register("execution", role="execute", trigger="per_decision",
                   skill="execution-quality gate (authoritative)", stop_condition="fill or reject")
        if self.arb_ledger is not None:
            r.register("arbitrage", role="risk_free_arb", trigger="per_window",
                       skill="within-window dutch book (up+down<1)",
                       stop_condition=self.stop_monitor.verified_stop_line("arbitrage"),
                       status_fn=lambda: self.arb_ledger.report())
        r.register("risk_monitor", role="risk", trigger="per_settlement",
                   skill="breaker + reconciliation",
                   status_fn=(lambda: self.grok_decider.breaker_status()) if self.grok_decider else None)
        r.register("news", role="context", trigger="interval",
                   interval_s=self.cfg.grok_news_refresh_s,
                   status_fn=(lambda: self.grok_news.report()) if self.grok_news else None)
        r.register("research_meta", role="research(/goal)", trigger="interval",
                   interval_s=self.cfg.research_interval_s, verifier="claude",
                   stop_condition="verifiable metric improvement",
                   status_fn=(lambda: self.research_loop.report()) if self.research_loop else None)
        r.register("lessons", role="memory", trigger="per_settlement", skill="LESSONS.md",
                   status_fn=lambda: {"calls": len(self.lessons.lessons)})

    def _capital_status(self) -> dict:
        """On-hand paper capital = starting capital + realized PnL, with open exposure (stake at risk
        in open positions). Display-only; PAPER ONLY (no real funds)."""
        ls = self.ledger.stats()
        start = float(self.cfg.starting_capital_usd)
        realized = float(ls.get("realized_pnl_usd") or 0.0)
        open_exposure = 0.0
        for pos in self.ledger.positions.values():
            if pos.status == "open":
                open_exposure += float(getattr(pos, "size_usd", 0.0) or 0.0)
        on_hand = start + realized
        # risk-free arbitrage P&L is SEGREGATED in stats but is real paper profit, so add it to the
        # TOTAL alpha the operator sees (directional vs arb stay separately reported).
        arb_pnl = float((self.arb_ledger.realized_profit_usd if self.arb_ledger is not None else 0.0) or 0.0)
        dep_pnl = float((self.dep_arb_ledger.realized_profit_usd
                         if self.dep_arb_ledger is not None else 0.0) or 0.0)
        total_realized = realized + arb_pnl + dep_pnl
        risk_free_pnl = arb_pnl + dep_pnl
        dir_cap = round(start * float(self.cfg.directional_max_bankroll_frac), 2)
        sources = (("dependency_arbitrage", dep_pnl), ("arbitrage", arb_pnl), ("directional", realized))
        primary = max(sources, key=lambda x: x[1])[0] if total_realized > 0 else self.cfg.primary_edge_source
        return {"paper_only": True, "starting_capital_usd": round(start, 2),
                "realized_pnl_usd": round(realized, 2),
                "on_hand_capital_usd": round(on_hand, 2),
                "return_pct": (round(realized / start * 100, 2) if start else None),
                "arb_realized_pnl_usd": round(arb_pnl, 2),
                "dependency_arb_realized_pnl_usd": round(dep_pnl, 2),
                "risk_free_realized_pnl_usd": round(risk_free_pnl, 2),
                "total_realized_pnl_usd": round(total_realized, 2),
                "total_on_hand_usd": round(start + total_realized, 2),
                "total_return_pct": (round(total_realized / start * 100, 2) if start else None),
                "open_exposure_usd": round(open_exposure, 2),
                "directional_bankroll_cap_usd": dir_cap,
                "directional_cap_remaining_usd": round(max(0.0, dir_cap - open_exposure), 2),
                "primary_edge_source": primary,
                "open_positions": ls.get("open_positions")}

    def _grok_decider_report(self) -> dict:
        """Grok Decision Engine status (off/shadow/follow): decisions, direction accuracy, Brier,
        latency, abstains, per-action breakdown. PAPER ONLY; shadow does not trade."""
        if self.grok_decider is None:
            return {"enabled": False, "mode": self.cfg.grok_decider_mode, "paper_only": True,
                    "affects_trading": False}
        rep = self.grok_decider.report()
        rep["pending_grades"] = len(self._grok_pending)
        rep["use_search"] = bool(self.cfg.grok_decider_use_search)
        rep["follow_fraction"] = self.cfg.grok_decider_follow_fraction
        rep["adaptive_enabled"] = bool(self.cfg.grok_decider_adaptive)
        rep["adaptive_policy_counts"] = dict(self._grok_policy_counts)
        rep["mispricing_gate"] = {
            "enabled": bool(self.cfg.mispricing_gate_enabled),
            "edge_ttc_gate_enabled": bool(self.cfg.edge_ttc_gate_enabled),
            "follow_on_abstain": bool(self.cfg.mispricing_follow_on_abstain),
            "follow_size_fraction": self.cfg.mispricing_follow_size_fraction,
            "ttc_window_s": [self.cfg.mispricing_ttc_min_s, self.cfg.mispricing_ttc_max_s],
            "min_executable_margin": self.cfg.mispricing_min_executable_margin,
            "reject_counts": dict(self._mispricing_gate_counts),
        }
        rep["explore_rate"] = self.cfg.grok_decider_explore_rate
        rep["news_digest"] = (self.grok_news.report() if self.grok_news is not None
                              else {"enabled": False})
        return rep

    def _grok_intel_report(self) -> dict:
        """Observe-only Grok signal-intelligence status (A analyst + B predictor + budget)."""
        return {
            "observe_only": True, "affects_trading": False, "off_hot_path": True,
            "budget": (self.grok_budget.status() if self.grok_budget is not None
                       else {"enabled": False}),
            "analyst_A": (self.grok_analyst.report() if self.grok_analyst is not None
                          else {"enabled": False}),
            "predictor_B": (self.grok_predictor.report() if self.grok_predictor is not None
                            else {"enabled": False}),
            "note": ("A analyzes signal-learning performance; B predicts P(up) per signal and is "
                     "graded vs realized moves. Both observe-only — never place/size/bypass a "
                     "trade; the execution gate remains the sole trade authority."),
        }

    def _tradingview_report(self) -> dict:
        """Observe-only TradingView intake counters + latest signal + signal-vs-5min-outcome edge
        measurement (report-only)."""
        if self.tradingview is None:
            rep = {"enabled": False, "tradingview_observe_only": True,
                   "tradingview_alerts_received": 0, "tradingview_alerts_valid": 0,
                   "tradingview_alerts_rejected": 0, "tradingview_reject_reasons": {},
                   "tradingview_latest_signal": None}
        else:
            rep = self.tradingview.report()
        # always surface the webhook listener status (req: listener status in the light report)
        rep["webhook"] = (self.webhook.status() if self.webhook is not None
                          else {"listening": False, "observe_only": True,
                                "reason": ("no_secret_configured" if self.tradingview is None
                                           else "listener_not_started")})
        rep["edge_vs_5min_outcome"] = self._tv_edge.report()
        rep["rsi_trend"] = self._rsi_model.report()
        rep["rsi_trend"]["forward_horizon_s"] = self.cfg.tradingview_signal_horizon_s
        rep["rsi_trend"]["pending_forward_evals"] = len(self._tv_pending)
        rep["rsi_trend"]["learns_from"] = "all_signals_forward_return"
        rep["signal_learning"] = self._tv_learner.report(
            promotion_allowed=self.cfg.tradingview_promotion_allowed,
            min_samples=self.cfg.tradingview_promotion_min_samples,
            min_win_rate=self.cfg.tradingview_promotion_min_win_rate)
        rep["signal_gate"] = {
            "enabled": bool(self.cfg.tradingview_signal_gate_enabled),
            "active": bool(self.cfg.tradingview_signal_gate_enabled and self.tradingview is not None),
            "mode": "directional_indication_restrict_only",
            "requires_fresh_aligned_signal": True, "can_force_trade": False,
            "execution_gate_still_authoritative": True,
            "max_signal_age_s": self.cfg.tradingview_signal_max_feature_age_s,
            "min_signal_strength": (self.cfg.tradingview_min_signal_strength
                                    if self.cfg.tradingview_min_signal_strength > 0 else None),
            "note": ("when active, a paper trade is taken only if a fresh TradingView signal agrees "
                     "with the side; it can only PREVENT trades, never force or bypass them.")}
        rep["context_gate"] = self.tv_context_gate.report()
        rep["down_bias_gate"] = self.tv_down_bias_gate.report()
        rep["mtf_gate"] = self.tv_mtf_gate.report()
        rep["confidence_tier"] = self._tv_confidence_tier_report()
        return rep

    def _tv_confidence_tier_report(self) -> dict:
        return {
            "enabled": bool(self.cfg.tv_confidence_tier_enabled),
            "observe_only": True,
            "affects_trading": bool(self.cfg.tv_confidence_tier_enabled),
            "can_force_trade": False,
            "can_block_trade": False,
            "mode": "param_modulation_restrict_only",
            "only_15m": bool(self.cfg.tv_tier_15m_only),
            "require_sweet_spot": bool(self.cfg.tv_tier_require_sweet_spot),
            "tier_counts": dict(self._tv_tier_counts),
            "deltas": {
                "tier_a_min_edge": self.cfg.tv_tier_a_min_edge_delta,
                "tier_a_max_price": self.cfg.tv_tier_a_max_price_delta,
                "tier_c_min_edge": self.cfg.tv_tier_c_min_edge_delta,
                "tier_c_max_price": self.cfg.tv_tier_c_max_price_delta,
            },
            "aligned_strength_min": self.cfg.tv_tier_aligned_strength_min,
            "note": ("At 15m TTC sweet spot, TV MTF regime adjusts min_edge/max_price only. "
                     "TV trade gates remain off per operator lock."),
        }

    def _tier_report(self) -> dict:
        """REPORT-ONLY tier table across bucket dimensions (no trade/veto authority)."""
        from engine.pulse.tiers import tier_report
        dims = {}
        if self.factors is not None:
            dims["edge_quality"] = self.factors.report().get("pnl_by_edge_quality_bucket", {})
        if self.research is not None:
            rr = self.research.report()
            dims["regime"] = rr.get("pnl_by_regime", {})
            dims["zscore_bucket"] = rr.get("pnl_by_zscore_bucket", {})
            dims["ttc_bucket"] = rr.get("pnl_by_ttc_bucket", {})
        reconciled = bool(self.reconciler.report().get("reconciled"))
        return tier_report(dims, reconciled=reconciled, safety_ok=reconciled)

    def status(self) -> dict:
        from engine.pulse.reporting import ledger_stats_by_market_series
        return {
            "schema": "btc_pulse/1.1", "paper_only": True, "live_trading_enabled": False,
            "ts": self.last_tick_ts, "ticks": self.ticks,
            "config": {"tick_seconds": self.cfg.tick_seconds, "size_usd": self.cfg.size_usd,
                       "min_edge": self.cfg.min_edge, "edge_buffer": self.cfg.edge_buffer,
                       "min_depth_usd": self.cfg.min_depth_usd, "max_price": self.cfg.max_price,
                       "min_reward_risk": self.cfg.min_reward_risk,
                       "grok_decider_mode": self.cfg.grok_decider_mode},
            "price": self.price.status(),
            "capital": self._capital_status(),
            "ledger": self.ledger.stats(),
            "decision_lifecycle": self.reconciler.report(),
            "reconciliation": self._global_reconciliation(),
            "signal_engine": (self.signals.report() if self.signals is not None
                              else {"enabled": False}),
            "factor_model": (self.factors.report() if self.factors is not None
                             else {"enabled": False}),
            "markov_regime": (self.markov.report() if self.markov is not None
                              else {"enabled": False}),
            "edge_model": (self.edge_model.report(affects_trading=self._learning_report()["active"])
                           if self.edge_model is not None else {"enabled": False}),
            "learning": self._learning_report(),
            "tier_table": self._tier_report(),
            "meta_learning": self._meta_learning_status(),
            "promotion_ladder": self.promotion.report(),
            "readiness": self.readiness(),
            "sizing": {"enabled": self.cfg.sizing_enabled, "paper_only": True,
                       "hard_cap_usd": self.cfg.sizing_hard_cap_usd,
                       "daily_loss_cap_usd": self.cfg.sizing_daily_loss_cap_usd,
                       "daily_loss_so_far": round(self._daily_loss, 4),
                       "bankroll_usd": self.cfg.sizing_bankroll_usd,
                       "no_martingale": True, "actual_size_usd": self.cfg.size_usd},
            "execution_gate": self.ledger.exec_gate_stats(),
            "research_features": (self.research.report() if self.research is not None
                                  else {"enabled": False}),
            "calibration": self.calib.to_dict(),
            "oracle": {
                "oracle_feed_type": self.oracle_feed_type,
                "oracle_symbol": self.cfg.oracle_symbol,
                "fast_feed_symbols": list(self.cfg.fast_feeds),
                "open_snapshot_source": "rtds_chainlink",
                "close_snapshot_source": "rtds_chainlink",
                "settlement_source_priority": list(self.cfg.settlement_source_priority),
                "settlement_sources_used": self.ledger.stats().get("settle_sources"),
                "proxy_official_reconciliation":
                    self.ledger.stats().get("proxy_official_reconciliation"),
                "proxy_max_close_lag_s": self.cfg.proxy_max_close_lag_s,
                "rtds": (self.rtds.status() if self.rtds is not None else {"enabled": False}),
                "lead_features": self.leads.features(),
            },
            "grok_overlay": (self.overlay.status() if self.overlay is not None
                             else {"enabled": False}),
            "grok_signal_intel": self._grok_intel_report(),
            "grok_decider": self._grok_decider_report(),
            "verifier": (self.verifier.report() if self.verifier is not None else {"enabled": False}),
            "research_loop": (self.research_loop.report() if self.research_loop is not None
                              else {"enabled": False}),
            "lessons": self.lessons.report(),
            "loops": self._loops_report(),
            "stop_conditions": self.stop_monitor.report(),
            "edge_signal": self._edge_signal_report(),
            "cex_lead_edge": (self.cex_lead.report() if self.cex_lead is not None
                              else {"enabled": False}),
            "arbitrage": (self.arb_ledger.report() if self.arb_ledger is not None
                          else {"enabled": False}),
            "dependency_arbitrage": (self.dep_arb_ledger.report()
                                     if self.dep_arb_ledger is not None
                                     else {"enabled": False}),
            "arb_graph": getattr(self, "_arb_graph_report", None) or {"nodes": 0},
            "grok_dependency": getattr(self, "_grok_dependency_report", None) or {
                "dependency_proposals": 0},
            "bregman_projection": getattr(self, "_bregman_projection_report", None) or {
                "enabled": False},
            "clob_feed": (
                self.clob_feed.latency_report() if getattr(self, "clob_feed", None) else {}),
            "walk_forward": self._walk_forward_status(),
            "series_architecture": {
                "design": "5m_brain_15m_hands",
                "scan_slugs": list(self.cfg.pulse_series_slugs),
                "directional_slugs": list(self.cfg.directional_series_slugs),
            },
            "profit_discovery": self._profit_discovery_status(),
            "five_x_improvement": self._profit_discovery_status(),
            "directional_risk": {
                "max_bankroll_frac": self.cfg.directional_max_bankroll_frac,
                "bankroll_cap_usd": round(
                    float(self.cfg.starting_capital_usd)
                    * float(self.cfg.directional_max_bankroll_frac), 2),
                "open_exposure_usd": round(self._directional_open_exposure(), 2),
                "block_up_until_promoted": bool(self.cfg.directional_block_up_until_promoted),
                "directional_down_only": bool(self.cfg.directional_down_only),
                "directional_series_slugs": list(self.cfg.directional_series_slugs),
                "arb_series_slugs": list(self.cfg.pulse_series_slugs),
                "up_promoted": self._up_direction_promoted(),
            },
            "directional_allowlist": {
                "enabled": bool(self.cfg.directional_require_winning_bucket),
                "explore_rate": self.cfg.directional_explore_rate,
                "explored": self._allowlist_explored, "blocked": self._allowlist_blocked},
            "by_market_series": ledger_stats_by_market_series(self.ledger.positions),
            "markets_feed": (self.market.report() if hasattr(self.market, "report")
                             else {"multi_series": False}),
            "pulse_series_slugs": list(self.cfg.pulse_series_slugs),
            "config_coupling": self._config_coupling_report(),
            "baseline_cohort_gate": self._baseline_cohort_gate_report(),
            "learned_selectivity_gate": self._selectivity_report(),
            "late_window_entry": self._late_window_report(),
            "tradingview": self._tradingview_report(),
            "tick_reasons": self._reasons,
            "recent_evaluations": self._last_eval,
        }

    def _persist(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            (self._data_dir / "btc_pulse_status.json").write_text(
                json.dumps(self.status(), default=str, indent=1))
            ledger_doc = {**self.ledger.to_dict(),
                          "calibration_state": self.calib.to_state(),
                          "accounting_state": {
                              "lifecycle": self.reconciler.to_state(),
                              "gate_observations": self.gate_obs.to_state(),
                              "ev": {"before_sum": round(self._ev_before_sum, 6),
                                     "after_sum": round(self._ev_after_sum, 6), "n": self._ev_n},
                              "tv_edge": self._tv_edge.to_state(),
                              "rsi_trend": self._rsi_model.to_state(),
                              "tv_learner": self._tv_learner.to_state(),
                              "tv_pending": self._tv_pending[-1000:],
                              "edge_signal": (self.edge_signal.to_state()
                                              if self.edge_signal is not None else {}),
                              "cex_lead": (self.cex_lead.to_state()
                                           if self.cex_lead is not None else {}),
                              "cex_lead_pending": self._cex_lead_pending[-2000:],
                              "mkt_bench_pending": self._mkt_bench_pending[-2000:],
                              "mkt_bench_recent": [list(x) for x in self._mkt_bench_recent],
                              "arb_ledger": (self.arb_ledger.to_state()
                                             if self.arb_ledger is not None else {}),
                              "dep_arb_ledger": (self.dep_arb_ledger.to_state()
                                                 if self.dep_arb_ledger is not None else {}),
                              "allowlist_explored": self._allowlist_explored,
                              "allowlist_blocked": self._allowlist_blocked,
                              "research_avoid": sorted(self._research_avoid),
                              "research_exploit": sorted(self._research_exploit),
                              "grok_predictor": (self.grok_predictor.to_state()
                                                 if self.grok_predictor is not None else {}),
                              "grok_analyst": (self.grok_analyst.to_state()
                                               if self.grok_analyst is not None else {}),
                              "grok_decider": (self.grok_decider.to_state()
                                               if self.grok_decider is not None else {}),
                              "grok_news": (self.grok_news.to_state()
                                            if self.grok_news is not None else {}),
                              "grok_pending": self._grok_pending[-2000:],
                              "verifier_pending": self._verifier_pending[-2000:],
                              "recent_windows": self._recent_windows[-40:],
                              "verifier": (self.verifier.to_state() if self.verifier is not None
                                           else {}),
                              "research_loop": (self.research_loop.to_state()
                                                if self.research_loop is not None else {}),
                              "lessons": self.lessons.to_state(),
                              "trade_history": self.trade_history.to_state(),
                              "edge_model": (self.edge_model.to_state()
                                             if self.edge_model is not None else {}),
                              "selectivity_evidence": self.selectivity_evidence.to_state(),
                              "selectivity_gate": self.selectivity_gate.to_state(),
                              "tv_context_gate": self.tv_context_gate.to_state(),
                              "tv_down_bias_gate": self.tv_down_bias_gate.to_state(),
                              "tv_mtf_gate": self.tv_mtf_gate.to_state(),
                              "down_stack": self.down_stack.to_state(),
                              "late_window_gate": self.late_window_gate.to_state(),
                              "late_window_edge": self.late_window_edge.to_state(),
                              "open_snapshots": self.price.to_open_state(),
                              "baseline": (self._baseline or empty_baseline())}}
            (self._data_dir / "btc_pulse_ledger.json").write_text(
                json.dumps(ledger_doc, default=str, indent=1))
            lr = self.light_report()
            settled_n = int((lr.get("ledger") or {}).get("settled") or 0)
            self._score_history.record(lr.get("scores") or {}, ticks=self.ticks,
                                       settled=settled_n)
            lr["score_history"] = self._score_history.to_dict()
            (self._data_dir / "btc_pulse_light_report.json").write_text(
                json.dumps(lr, default=str, indent=1))
            self._score_history.save()
            # always write the COMPLETE human-readable performance report (for ChatGPT/Grok review)
            try:
                from engine.pulse.reporting import build_full_report_md
                from engine.pulse.word_report import build_word_report
                st = self.status()
                led = self.ledger.to_dict()
                full_md = build_full_report_md(lr, st, led)
                (self._data_dir / "report.md").write_text(full_md, encoding="utf-8")
                (self._data_dir / "FULL_REPORT.md").write_text(full_md, encoding="utf-8")
                build_word_report(lr, status=st, ledger=led,
                                  score_history=lr.get("score_history"),
                                  output_path=self._data_dir / "report.docx")
                (self._data_dir / "LESSONS.md").write_text(self.lessons.to_markdown(),
                                                           encoding="utf-8")
                from engine.pulse.state import build_state_md
                (self._data_dir / "STATE.md").write_text(
                    build_state_md(status=self.status(), ledger=self.ledger.to_dict(),
                                   stop_conditions=self.stop_monitor.report(),
                                   lessons=self.lessons.report()),
                    encoding="utf-8")
                from engine.pulse.provenance import write_provenance_artifacts
                write_provenance_artifacts(
                    self._data_dir, light_report=lr, status=self.status(),
                    ledger=self.ledger.to_dict())
            except Exception:  # noqa: BLE001 — report writing never breaks the loop
                pass
            from engine.pulse.meta_learning import build_bundle
            (self._data_dir / "btc_pulse_meta_bundle.json").write_text(
                json.dumps(build_bundle(lr), default=str, indent=1))
        except Exception as exc:  # noqa: BLE001 — persistence never breaks the loop
            logger.debug("pulse persist failed: %s", exc)

    def run(self, *, max_ticks: Optional[int] = None) -> None:
        logger.info("BTC 5-min pulse engine starting (PAPER ONLY) tick=%.1fs size=$%.2f "
                    "min_edge=%.3f", self.cfg.tick_seconds, self.cfg.size_usd, self.cfg.min_edge)
        n = 0
        while True:
            t0 = time.time()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad tick never kills the loop
                logger.exception("pulse tick error")
            n += 1
            if max_ticks is not None and n >= max_ticks:
                return
            time.sleep(max(0.5, self.cfg.tick_seconds - (time.time() - t0)))
