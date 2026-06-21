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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from engine.pulse.markets import PulseMarketFeed
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

logger = logging.getLogger("hte.pulse.engine")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


@dataclass
class PulseConfig:
    tick_seconds: float = 4.0
    size_usd: float = 5.0
    min_edge: float = 0.03
    min_seconds_to_close: float = 4.0
    min_depth_usd: float = 1.0
    edge_buffer: float = 0.01
    max_price: float = 0.97
    max_open_lag_s: float = 20.0
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
    # strict execution-quality gate (orderbook-reality EV after VWAP/slippage)
    exec_max_spread: float = 0.06
    exec_min_order_usd: float = 1.0
    exec_max_depth_consume_frac: float = 0.5
    exec_min_ev_after_slippage: float = 0.0
    exec_max_book_age_s: float = 30.0        # reject stale orderbook older than this
    research_features_enabled: bool = True   # OBSERVE-ONLY EP Chan features (never trade)
    signal_engine_enabled: bool = True       # OBSERVE-ONLY Simons-style raw signals (never trade)
    factor_model_enabled: bool = True        # OBSERVE-ONLY BTC-pulse factor/context model
    markov_enabled: bool = True              # OBSERVE-ONLY Markov regime machine
    edge_model_enabled: bool = True          # OBSERVE-ONLY calibrated edge model (no authority)
    sizing_enabled: bool = False             # paper Kelly sizing: default OFF (size unchanged)
    sizing_hard_cap_usd: float = 10.0
    sizing_daily_loss_cap_usd: float = 50.0
    sizing_bankroll_usd: float = 1000.0
    data_dir: str = "/data"

    @classmethod
    def from_env(cls) -> "PulseConfig":
        return cls(
            tick_seconds=_envf("PULSE_TICK_SECONDS", 4.0),
            size_usd=_envf("PULSE_SIZE_USD", 5.0),
            min_edge=_envf("PULSE_MIN_EDGE", 0.03),
            min_seconds_to_close=_envf("PULSE_MIN_SECONDS_TO_CLOSE", 4.0),
            min_depth_usd=_envf("PULSE_MIN_DEPTH_USD", 1.0),
            edge_buffer=_envf("PULSE_EDGE_BUFFER", 0.01),
            max_price=_envf("PULSE_MAX_PRICE", 0.97),
            max_open_lag_s=_envf("PULSE_MAX_OPEN_LAG_S", 20.0),
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
            rtds_enabled=str(os.getenv("HERMES_RTDS_ENABLED", "1")).strip().lower()
            in ("1", "true", "yes", "on"),
            exec_max_spread=_envf("PULSE_EXEC_MAX_SPREAD", 0.06),
            exec_min_order_usd=_envf("PULSE_EXEC_MIN_ORDER_USD", 1.0),
            exec_max_depth_consume_frac=_envf("PULSE_EXEC_MAX_DEPTH_CONSUME_FRAC", 0.5),
            exec_min_ev_after_slippage=_envf("PULSE_EXEC_MIN_EV", 0.0),
            exec_max_book_age_s=_envf("PULSE_EXEC_MAX_BOOK_AGE_S", 30.0),
            research_features_enabled=str(os.getenv("HERMES_RESEARCH_FEATURES_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            signal_engine_enabled=str(os.getenv("HERMES_SIGNAL_ENGINE_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            factor_model_enabled=str(os.getenv("HERMES_FACTOR_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            markov_enabled=str(os.getenv("HERMES_MARKOV_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            edge_model_enabled=str(os.getenv("HERMES_EDGE_MODEL_ENABLED", "1"))
            .strip().lower() in ("1", "true", "yes", "on"),
            sizing_enabled=str(os.getenv("HERMES_SIZING_ENABLED", "0"))
            .strip().lower() in ("1", "true", "yes", "on"),
            sizing_hard_cap_usd=_envf("HERMES_SIZING_HARD_CAP_USD", 10.0),
            sizing_daily_loss_cap_usd=_envf("HERMES_SIZING_DAILY_LOSS_CAP_USD", 50.0),
            sizing_bankroll_usd=_envf("HERMES_SIZING_BANKROLL_USD", 1000.0),
            data_dir=os.getenv("HTE_DATA_DIR", "/data"))


class PulseEngine:
    def __init__(self, cfg: Optional[PulseConfig] = None, *, market_feed=None,
                 price_feed=None):
        self.cfg = cfg or PulseConfig()
        # reject classic Chainlink Data Feed / AggregatorV3 as the primary settlement feed
        from engine.pulse.oracle import validate_oracle_feed_type, LeadFeeds
        self.oracle_feed_type = validate_oracle_feed_type(self.cfg.oracle_feed_type)
        self.market = market_feed or PulseMarketFeed()
        self.rtds = None
        if price_feed is not None:
            self.price = price_feed
        elif self.cfg.rtds_enabled:
            # CANONICAL oracle: Chainlink ref price via Polymarket RTDS crypto_prices_chainlink.
            from engine.pulse.rtds import RTDSClient, TOPIC_CHAINLINK, TOPIC_BINANCE
            self.rtds = RTDSClient(subscriptions=[(TOPIC_CHAINLINK, self.cfg.oracle_symbol),
                                                  (TOPIC_BINANCE, "btcusdt")])
            self.rtds.start()
            self.price = PulsePriceFeed(
                fetcher=self.rtds.oracle_price, source_name="rtds_chainlink",
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
                sampler_interval_s=self.cfg.price_sampler_interval_s)
            self.price.start_sampler()
        else:
            fetcher, src = build_price_source(self.cfg.price_source)
            self.price = PulsePriceFeed(
                fetcher=fetcher, source_name=src,
                vol=RollingVol(window_s=self.cfg.vol_window_s),
                max_open_lag_s=self.cfg.max_open_lag_s,
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
        self.reconciler = LifecycleReconciler()   # GS-Quant-style candidate lifecycle audit
        from engine.pulse.promotion import PromotionLadder
        self.promotion = PromotionLadder()        # all features default to observe-only (level 0)
        self._daily_loss = 0.0                    # for the Kelly daily-loss-cap diagnostic
        self._daily_key = None
        from engine.pulse.reporting import OutcomeGroups
        self._groups = OutcomeGroups()            # settled PnL grouped by every entry-time tag
        self._ev_before_sum = 0.0                 # EV before/after costs (accepted candidates)
        self._ev_after_sum = 0.0
        self._ev_n = 0
        self.overlay = None
        if bool(getattr(self.cfg, "grok_overlay_enabled", False)):
            try:
                from engine.pulse.overlay import GrokEventOverlay, xai_key_present
                if xai_key_present():
                    self.overlay = GrokEventOverlay(
                        interval_s=self.cfg.grok_overlay_interval_s,
                        max_calls_per_hour=self.cfg.grok_overlay_max_calls_per_hour)
                    self.overlay.start()
            except Exception:  # noqa: BLE001 — overlay never blocks startup
                self.overlay = None
        self.ticks = 0
        self.last_tick_ts = 0.0
        self._reasons: dict = {}
        self._last_eval: list = []
        self._data_dir = Path(self.cfg.data_dir)
        self._ledger_path = self._data_dir / "btc_pulse_ledger.json"
        if not self.cfg.fresh_start:
            self._load_state()
        elif self._ledger_path.exists():
            self._archive_prior_state()

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
        logger.info("pulse state restored: trades=%d settled=%d realized_pnl=%.3f calib_n=%d",
                    self.ledger.trades, self.ledger.settled, self.ledger.realized_pnl,
                    self.calib.n)

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
        self.price.poll(now)               # oracle: RTDS Chainlink ref price
        self.leads.poll(now)               # lead predictors (Binance/Coinbase) — features only
        if self.research is not None:
            self.research.observe_oracle(self.price.current())
        if self.signals is not None:
            self.signals.observe_price(self.price.current(), now)
        windows = self.market.active_windows(now=now)
        keep_keys = {w.event_id for w in windows} | set(self.ledger.positions)
        self.price.prune_opens(keep_keys)
        reasons: dict = {}
        evald = []
        ov = self.overlay.current(now) if self.overlay is not None else None
        ov_blackout = bool(ov and ov.get("blackout"))
        ov_vol_mult = float(ov.get("vol_multiplier", 1.0)) if ov else 1.0

        def _bump(r):
            reasons[r] = reasons.get(r, 0) + 1

        def _finalize(dr, terminal, *, reason=None, stage=None):
            """Close a candidate in exactly one terminal state — no candidate disappears."""
            dr.finalize(terminal, reason=reason, stage=stage)
            self.reconciler.record(dr)
            evald.append(dr.to_dict())
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
                open_ts=w.open_ts, close_ts=w.close_ts, ttc_s=ttc,
                s_open=(snap.price if snap else None), s_now=s_now, sigma_per_sec=sigma,
                lead_prices={k: (v[0] if v else None)
                             for k, v in (getattr(self.leads, "_latest", {}) or {}).items()})
            dr = DecisionResult(market_context=mc,
                                candidate=CandidateDecision(None, None, None, 0.0, False, "pending"))
            # early terminal classifications (each candidate ends classified)
            if ttc <= 0:
                _finalize(dr, "expired", reason="window_closed")
                continue
            if snap is None:
                _finalize(dr, "missing_data", reason="no_open_snapshot")
                continue
            if snap.lag_s > self.cfg.max_open_lag_s:
                _finalize(dr, "skipped", reason="open_snapshot_late")
                continue
            if s_now is None or sigma is None:
                _finalize(dr, "missing_data", reason="no_price_or_vol")
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
            # the overlay can only RAISE sigma (>=1.0) -> more conservative P(up)
            fair = digital_p_up(s_now, snap.price, sigma * ov_vol_mult, ttc)
            d = decide(w, fair, now, min_edge=self.cfg.min_edge,
                       min_seconds_to_close=self.cfg.min_seconds_to_close,
                       min_depth_usd=self.cfg.min_depth_usd,
                       edge_buffer=self.cfg.edge_buffer, max_price=self.cfg.max_price,
                       min_seconds_since_open=self.cfg.min_seconds_since_open,
                       basis_buffer=self.cfg.basis_buffer)
            outcome_prob = (fair if d.side == "up" else (1.0 - fair)) if fair is not None else None
            dr.candidate = CandidateDecision(side=d.side, fair_p_up=fair, outcome_prob=outcome_prob,
                                             model_edge=d.edge, tradeable=d.trade, reason=d.reason)
            # OBSERVE-ONLY research features (every candidate; NEVER affects decision/gate).
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
            # OBSERVE-ONLY Simons-style raw signal snapshot (never affects decision/gate).
            if self.signals is not None:
                self.signals.observe_poly(mc.poly_yes, mc.spread, mc.ask_depth_usd, now)
                dr.signals = self.signals.snapshot(ttc_s=ttc, now=now).to_dict()
            # OBSERVE-ONLY BTC-pulse factor/context model + edge_quality_score.
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
            # OBSERVE-ONLY Markov regime classification (never affects decision/gate).
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
            # OBSERVE-ONLY calibrated edge model (NO trade authority). Predict from entry-time
            # features; the realized label trains it later (no leakage).
            model_vec = None
            if self.edge_model is not None:
                from engine.pulse.edge_model import extract_features
                model_vec = extract_features(features=dr.features, signals=dr.signals,
                                             factors=dr.factors)
                dr.model = self.edge_model.predict(model_vec)
            if not d.trade:
                dr.action = RejectAction(stage="directional", reason=d.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=d.reason, stage="directional")
                continue
            # STRICT execution-quality gate (AUTHORITATIVE): EV from the live ask-ladder VWAP.
            book = w.up_book if d.side == "up" else w.down_book
            ex = evaluate_execution(
                side=d.side, book=book, outcome_prob=outcome_prob,
                size_usd=self.cfg.size_usd, tick_size=w.tick_size, ttc_s=ttc,
                min_seconds_to_close=self.cfg.min_seconds_to_close,
                max_spread=self.cfg.exec_max_spread, min_depth_usd=self.cfg.min_depth_usd,
                min_order_usd=self.cfg.exec_min_order_usd,
                max_depth_consume_frac=self.cfg.exec_max_depth_consume_frac,
                min_ev_after_slippage=self.cfg.exec_min_ev_after_slippage,
                now=now, max_book_age_s=self.cfg.exec_max_book_age_s)
            self.ledger.record_exec(ex.accepted, ex.reason)
            dr.cost = ExecutionCostEstimate.from_exec_result(ex)
            dr.mark("execution_costed")
            if not ex.accepted:
                dr.action = RejectAction(stage="execution_gate", reason=ex.reason)
                if self.markov is not None:
                    self.markov.record_terminal(state=cand_state, accepted=False)
                _finalize(dr, "rejected", reason=ex.reason, stage="execution_gate")
                continue
            d.price = ex.fill_price               # paper fill at realistic VWAP price
            pos = self.ledger.open_position(w, d, now, size_usd=self.cfg.size_usd,
                                            s_open=snap.price)
            if pos is not None:
                if rfeat is not None:             # observe-only entry-time tags
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
            # EV before (midpoint) vs after (VWAP/slippage) costs — accepted candidates
            if ex.ev_at_mid is not None and ex.ev_after_slippage is not None:
                self._ev_before_sum += ex.ev_at_mid
                self._ev_after_sum += ex.ev_after_slippage
                self._ev_n += 1
                dr.fill = PaperFill(window_key=w.event_id, side=d.side, fill_price=ex.fill_price,
                                    shares=pos.shares, size_usd=pos.size_usd)
            dr.action = TradeAction(side=d.side, token_id=d.token_id, fill_price=ex.fill_price,
                                    size_usd=self.cfg.size_usd, shares=(pos.shares if pos else 0.0))
            if self.markov is not None:
                self.markov.record_terminal(state=cand_state, accepted=True)
            # PAPER-ONLY Kelly sizing DIAGNOSTIC (default OFF -> actual size unchanged).
            from engine.pulse.sizing import sizing_diagnostics
            _pwin = (dr.model or {}).get("p_up")
            if _pwin is None:
                _pwin = outcome_prob          # fall back to the model fair value
            dr.sizing = sizing_diagnostics(
                p_win=_pwin, price=ex.fill_price, ev_after_costs=ex.ev_after_slippage,
                bankroll_usd=self.cfg.sizing_bankroll_usd, hard_cap_usd=self.cfg.sizing_hard_cap_usd,
                daily_loss_cap_usd=self.cfg.sizing_daily_loss_cap_usd,
                daily_loss_so_far=self._daily_loss, base_size_usd=self.cfg.size_usd,
                sizing_enabled=self.cfg.sizing_enabled)
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
                "confidence_tier")}
            self._groups.record(tags, pnl=float(pos.pnl_usd or 0.0), won=bool(pos.won),
                                 fair_at_entry=pos.fair_at_entry, outcome_up=outcome)
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

    def light_report(self) -> dict:
        """The latest light report (report-only): full lifecycle reconciliation, exec stats,
        reject reasons, EV before/after costs, PnL grouped by every bucket dimension, calibration,
        sample sizes, missing-data reasons, and promotion/demotion candidates."""
        from engine.pulse.reporting import build_light_report
        ev_stats = {"n": self._ev_n,
                    "avg_ev_before_costs": (round(self._ev_before_sum / self._ev_n, 6)
                                            if self._ev_n else None),
                    "avg_ev_after_costs": (round(self._ev_after_sum / self._ev_n, 6)
                                           if self._ev_n else None)}
        miss = self.research.report().get("missing_data_reasons", {}) if self.research else {}
        return build_light_report(
            lifecycle=self.reconciler.report(), execution_gate=self.ledger.exec_gate_stats(),
            ledger_stats=self.ledger.stats(), calibration=self.calib.to_dict(),
            ev_stats=ev_stats, outcome_groups=self._groups, tier_table=self._tier_report(),
            edge_model=(self.edge_model.report() if self.edge_model else {}),
            sizing={"enabled": self.cfg.sizing_enabled, "actual_size_usd": self.cfg.size_usd},
            missing_data_reasons=miss)

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
        return {
            "schema": "btc_pulse/1.0", "paper_only": True, "live_trading_enabled": False,
            "ts": self.last_tick_ts, "ticks": self.ticks,
            "config": {"tick_seconds": self.cfg.tick_seconds, "size_usd": self.cfg.size_usd,
                       "min_edge": self.cfg.min_edge, "edge_buffer": self.cfg.edge_buffer,
                       "min_depth_usd": self.cfg.min_depth_usd, "max_price": self.cfg.max_price},
            "price": self.price.status(),
            "ledger": self.ledger.stats(),
            "decision_lifecycle": self.reconciler.report(),
            "signal_engine": (self.signals.report() if self.signals is not None
                              else {"enabled": False}),
            "factor_model": (self.factors.report() if self.factors is not None
                             else {"enabled": False}),
            "markov_regime": (self.markov.report() if self.markov is not None
                              else {"enabled": False}),
            "edge_model": (self.edge_model.report() if self.edge_model is not None
                           else {"enabled": False}),
            "tier_table": self._tier_report(),
            "meta_learning": self._meta_learning_status(),
            "promotion_ladder": self.promotion.report(),
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
            "tick_reasons": self._reasons,
            "recent_evaluations": self._last_eval,
        }

    def _persist(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            (self._data_dir / "btc_pulse_status.json").write_text(
                json.dumps(self.status(), default=str, indent=1))
            ledger_doc = {**self.ledger.to_dict(),
                          "calibration_state": self.calib.to_state()}
            (self._data_dir / "btc_pulse_ledger.json").write_text(
                json.dumps(ledger_doc, default=str, indent=1))
            lr = self.light_report()
            (self._data_dir / "btc_pulse_light_report.json").write_text(
                json.dumps(lr, default=str, indent=1))
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
