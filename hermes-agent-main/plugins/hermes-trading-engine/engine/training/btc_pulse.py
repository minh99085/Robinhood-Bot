"""BTC 5-minute Pulse — PAPER-ONLY, isolated training experiment.

This is a self-contained simulated training module that runs *beside* the
Polymarket institutional paper campaign to generate fast learning feedback. It
is deliberately isolated from the Polymarket trainer:

* PAPER ONLY — it never submits a real order, never touches a wallet, and never
  imports a live-execution / order-submission path.
* ISOLATED LEARNING — it owns its own learner state under the
  ``btc_5min_pulse`` experiment namespace and never writes to the Polymarket
  ``OnlineLearner`` namespace, so it cannot contaminate Polymarket learning.
* RISK-GATED — every simulated paper trade must pass the deterministic
  :class:`~engine.risk.RiskEngine` and a realistic-fill check first.
* FAIL-CLOSED — if any unsafe flag is set (live enabled, legacy BTC autotrade,
  paper_only off, isolated_learning off, RiskEngine off, realistic-fill off)
  the module freezes and never trades.

It deliberately does NOT enable the legacy ``engine.engine`` BTC autotrade or
any live BTC trading. Decision/no-trade samples are both recorded so the
isolated learner improves from every round.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Optional

from .config import _envb

logger = logging.getLogger("hte.btc_pulse")

EXPERIMENT_ID = "btc_5min_pulse"
STRATEGY_FAMILY = "btc_pulse"

# Binary up/down market house edge (cost above 0.50 to enter a side).
_PULSE_VIG = 0.04


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


class _IsolatedPulseLearner:
    """Tiny, self-contained learner for the BTC pulse experiment ONLY.

    Holds win/loss counts, per-round paper PnL, and (probability, outcome)
    pairs for calibration. It has NO reference to the Polymarket learner and
    exposes ``namespace`` so every record is tagged to the pulse experiment.
    """

    namespace = EXPERIMENT_ID

    def __init__(self) -> None:
        self.wins = 0
        self.losses = 0
        self.pnls: list[float] = []
        self.preds: list[tuple[float, int]] = []   # (p_chosen_side, won 0/1)

    def record_round(self, *, p_pred: float, won: bool, pnl: float) -> None:
        self.preds.append((float(p_pred), 1 if won else 0))
        self.pnls.append(float(pnl))
        if won:
            self.wins += 1
        else:
            self.losses += 1

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    def win_rate(self) -> float:
        return round(_safe_div(self.wins, self.settled), 4) if self.settled else 0.0


class BtcPulsePaperTrainer:
    """Isolated, PAPER-ONLY BTC 5-min pulse simulated trainer."""

    def __init__(self, cfg, *, data_dir=None, clock: Optional[Callable[[], int]] = None,
                 price_fn: Optional[Callable[[], float]] = None, rng_seed: int = 1337,
                 risk_engine=None):
        self.cfg = cfg
        self.data_dir = data_dir
        self._clock = clock or _now_ms
        self._price_fn = price_fn
        self._rng = random.Random(rng_seed)

        # resolved pulse config (read-only snapshot)
        self.enabled_flag = bool(getattr(cfg, "btc_pulse_enabled", False))
        self.paper_only = bool(getattr(cfg, "btc_pulse_paper_only", True))
        self.isolated_learning = bool(getattr(cfg, "btc_pulse_isolated_learning", True))
        self.transfer_allowed = bool(getattr(cfg, "btc_pulse_allow_transfer_learning", False))
        self.live_enabled = bool(getattr(cfg, "btc_pulse_live_enabled", False))
        self.legacy_autotrade = bool(getattr(cfg, "btc_pulse_legacy_autotrade_enabled", False))
        self.tick_seconds = max(1, int(getattr(cfg, "btc_pulse_tick_seconds", 30)))
        self.round_seconds = max(1, int(getattr(cfg, "btc_pulse_round_seconds", 300)))
        self.max_notional = float(getattr(cfg, "btc_pulse_max_paper_notional_per_trade", 5.0))
        self.max_trades_per_hour = int(getattr(cfg, "btc_pulse_max_paper_trades_per_hour", 60))
        self.max_daily_loss = float(getattr(cfg, "btc_pulse_max_daily_paper_loss", 50.0))
        self.min_ev = float(getattr(cfg, "btc_pulse_min_ev_threshold", 0.0))
        self.require_positive_ev = bool(getattr(cfg, "btc_pulse_require_positive_ev", True))
        self.require_risk_gate = bool(getattr(cfg, "btc_pulse_require_risk_gate", True))
        self.require_realistic_fill = bool(getattr(cfg, "btc_pulse_require_realistic_fill", True))
        self.vig = float(getattr(cfg, "pulse_vig", _PULSE_VIG))

        # isolated risk engine (default deterministic gate)
        if risk_engine is not None:
            self.risk = risk_engine
        else:
            try:
                from engine.risk import RiskEngine
                self.risk = RiskEngine()
            except Exception:  # noqa: BLE001
                self.risk = None

        # isolated learner — NEVER the Polymarket learner
        self.learner = _IsolatedPulseLearner()

        # simulated market state
        self._price = 100_000.0
        self._closes: list[float] = []
        self._ticks_per_round = max(1, round(self.round_seconds / self.tick_seconds))

        # counters / metrics
        self.ticks = 0
        self.rounds_seen = 0
        self.decisions = 0
        self.no_trade_decisions = 0
        self.paper_trades = 0
        self.rejected_trades = 0
        self.rejection_reasons: dict[str, int] = {}
        self.ev_positive_count = 0
        self.ev_negative_rejected_count = 0
        self.realistic_fill_pnl = 0.0
        self.after_cost_pnl = 0.0
        self.equity = float(getattr(cfg, "starting_bankroll", 500.0))
        self._start_equity = self.equity
        self._peak_equity = self.equity
        self.max_drawdown = 0.0
        self._day_loss = 0.0
        self._trades_this_hour = 0
        self._hour_anchor_ms: Optional[int] = None
        self.last_tick_ts: Optional[int] = None
        self.last_error: Optional[str] = None
        self.kill_switch_active = False

        # feedback acceleration (PAPER ONLY): record a shadow decision on
        # near-threshold no-trade rounds so every round yields a learning sample.
        # NEVER forces a trade when the signal is off or EV is clearly negative.
        self.accel_enabled = (bool(getattr(cfg, "feedback_accelerator_enabled", False))
                              and bool(getattr(cfg, "btc_pulse_feedback_acceleration_enabled", True)))
        self.near_threshold_floor = -0.03
        self.shadow_decisions = 0

        # active round
        self._round: Optional[dict] = None

        # safety / freeze
        self.safety = self.safety_check()
        self.frozen = (not self.enabled_flag) or (not self.safety["passed"])

    # -- namespace tag (stamped on every record) ----------------------- #
    def namespace(self) -> dict:
        return {
            "experiment_id": EXPERIMENT_ID,
            "strategy_family": STRATEGY_FAMILY,
            "paper_only": True,
            "isolated_learning": bool(self.isolated_learning),
            "live_enabled": False,
            "transfer_allowed": bool(self.transfer_allowed),
        }

    # -- fail-closed safety -------------------------------------------- #
    def safety_check(self) -> dict:
        risk_engine_on = bool(getattr(self.cfg, "risk_engine_enabled", True))
        autotrade_env = (_envb("BTC_AUTOTRADE_ENABLED", False)
                         or _envb("BTC_PULSE_LEGACY_AUTOTRADE_ENABLED", False))
        live_env = _envb("BTC_PULSE_LIVE_ENABLED", False)
        checks = {
            "paper_only": bool(self.paper_only),
            "isolated_learning": bool(self.isolated_learning),
            "live_disabled": (not self.live_enabled) and (not live_env),
            "btc_autotrade_disabled": (not self.legacy_autotrade) and (not autotrade_env),
            "risk_gate_available": (not self.require_risk_gate) or (
                risk_engine_on and self.risk is not None),
            "realistic_fill_required": bool(self.require_realistic_fill),
            # structural invariants — this module never touches these paths
            "no_wallet_access": True,
            "no_order_submission_path": True,
            "no_polymarket_learner_write": (self.learner.namespace == EXPERIMENT_ID),
        }
        passed = all(checks.values())
        reason = None if passed else next(k for k, v in checks.items() if not v)
        return {"passed": passed, "fail_closed_reason": reason, "checks": checks}

    # -- one simulated tick -------------------------------------------- #
    def tick(self, *, now_ms: Optional[int] = None) -> dict:
        if self.frozen:
            return {"frozen": True, "reason": self.safety.get("fail_closed_reason")
                    or "btc_pulse_disabled"}
        try:
            return self._tick_inner(now_ms)
        except Exception as exc:  # noqa: BLE001 — must never block Polymarket
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("btc_pulse tick error: %s", self.last_error)
            return {"frozen": False, "error": self.last_error}

    def _tick_inner(self, now_ms: Optional[int]) -> dict:
        now = int(now_ms) if now_ms is not None else self._clock()
        self.ticks += 1
        self.last_tick_ts = now
        if self._hour_anchor_ms is None:
            self._hour_anchor_ms = now
        if now - self._hour_anchor_ms >= 3_600_000:
            self._hour_anchor_ms = now
            self._trades_this_hour = 0

        self._advance_price()

        result: dict = {"frozen": False, "event": "observe"}
        if self._round is None:
            result = self._open_round(now)
        elif self.ticks >= self._round["resolve_tick"]:
            result = self._resolve_round(now)
        result["ticks"] = self.ticks
        return result

    # -- round lifecycle ----------------------------------------------- #
    def _open_round(self, now: int) -> dict:
        self.rounds_seen += 1
        self.decisions += 1
        p_up = self._regime_p_up()
        side = "UP" if p_up >= 0.5 else "DOWN"
        p_pred = p_up if side == "UP" else (1.0 - p_up)
        entry = round(0.5 + self.vig / 2.0, 4)
        # EV per $1 staked for a binary share bought at ``entry`` paying $1.
        ev_frac = round(p_pred * (1.0 / entry - 1.0) - (1.0 - p_pred), 6)
        if ev_frac > 0:
            self.ev_positive_count += 1

        decision = {
            "round": self.rounds_seen, "side": side, "p_pred": round(p_pred, 4),
            "entry": entry, "ev_frac": ev_frac, "start_price": round(self._price, 2),
            "ts": now, **self.namespace(),
        }

        reject = self._gate(decision)
        if reject is not None:
            self.no_trade_decisions += 1
            self.rejected_trades += 1
            self.rejection_reasons[reject] = self.rejection_reasons.get(reject, 0) + 1
            if reject in ("negative_ev", "below_ev_threshold"):
                self.ev_negative_rejected_count += 1
            # Feedback acceleration: when a round is a NEAR-THRESHOLD no-trade
            # (EV just below break-even), record a shadow decision so the round
            # still yields a learning sample. This NEVER opens a position and
            # NEVER fires when the signal is off or EV is clearly negative.
            shadow = (self.accel_enabled and reject in ("negative_ev", "below_ev_threshold")
                      and ev_frac >= self.near_threshold_floor)
            if shadow:
                self.shadow_decisions += 1
            # a no-trade is still a useful (isolated) training sample
            self._round = {
                "decision": decision, "traded": False, "stake": 0.0, "fill_frac": 0.0,
                "resolve_tick": self.ticks + self._ticks_per_round, "no_trade_reason": reject,
                "shadow": shadow,
            }
            return {"frozen": False, "event": "shadow_decision" if shadow else "no_trade",
                    "reason": reject, "round": self.rounds_seen, **self.namespace()}

        # approved -> open a paper position (NO real order, ever)
        stake = round(min(self.max_notional, max(0.0, self.max_notional)), 2)
        fill_frac = self._simulate_fill()
        self.paper_trades += 1
        self._trades_this_hour += 1
        self._round = {
            "decision": decision, "traded": True, "stake": stake, "fill_frac": fill_frac,
            "resolve_tick": self.ticks + self._ticks_per_round, "no_trade_reason": None,
        }
        return {"frozen": False, "event": "paper_trade", "side": side, "stake": stake,
                "ev_frac": ev_frac, "round": self.rounds_seen, **self.namespace()}

    def _resolve_round(self, now: int) -> dict:
        rnd = self._round or {}
        self._round = None
        dec = rnd.get("decision", {})
        side = dec.get("side", "UP")
        start_price = float(dec.get("start_price", self._price))
        end_price = float(self._price)
        up_won = end_price > start_price
        won = up_won if side == "UP" else (end_price < start_price)
        p_pred = float(dec.get("p_pred", 0.5))

        pnl = 0.0
        if rnd.get("traded"):
            stake = float(rnd.get("stake", 0.0))
            fill_frac = float(rnd.get("fill_frac", 1.0))
            entry = float(dec.get("entry", 0.52))
            filled = stake * fill_frac
            if won:
                pnl = round(filled * (1.0 / entry - 1.0), 4)
            else:
                pnl = round(-filled, 4)
            # realistic-fill PnL already reflects fill_frac; after-cost subtracts
            # a tiny simulated taker cost so profit is never a fantasy fill.
            cost = round(filled * 0.002, 4)
            self.realistic_fill_pnl = round(self.realistic_fill_pnl + pnl, 4)
            self.after_cost_pnl = round(self.after_cost_pnl + pnl - cost, 4)
            self.equity = round(self.equity + pnl - cost, 4)
            if pnl - cost < 0:
                self._day_loss = round(self._day_loss + (cost - pnl), 4)
            self._update_drawdown()
            if self._day_loss >= self.max_daily_loss:
                self.kill_switch_active = True

        # isolated learner update (pulse namespace ONLY)
        self.learner.record_round(p_pred=p_pred, won=won, pnl=pnl)
        return {"frozen": False, "event": "resolve", "won": won, "pnl": pnl,
                "round": dec.get("round"), **self.namespace()}

    # -- decision gates ------------------------------------------------- #
    def _gate(self, decision: dict) -> Optional[str]:
        if self.kill_switch_active:
            return "drawdown_kill_switch"
        if self._day_loss >= self.max_daily_loss:
            self.kill_switch_active = True
            return "drawdown_kill_switch"
        if self.max_trades_per_hour > 0 and self._trades_this_hour >= self.max_trades_per_hour:
            return "hourly_trade_cap"
        ev = float(decision["ev_frac"])
        if self.require_positive_ev and ev <= 0:
            return "negative_ev"
        if ev < self.min_ev:
            return "below_ev_threshold"
        # realistic fill must pass
        if self.require_realistic_fill and self._simulate_fill() <= 0.0:
            return "fill_failed"
        # deterministic RiskEngine gate (paper)
        if self.require_risk_gate:
            ok = self._risk_ok(decision)
            if not ok:
                return "risk_rejected"
        return None

    def _risk_ok(self, decision: dict) -> bool:
        if self.risk is None:
            return False
        try:
            from engine.risk import RiskContext
            from engine.schemas import TradeProposal
            proposal = TradeProposal(
                strategy=STRATEGY_FAMILY, market="crypto", symbol="BTCUSDT",
                side="BUY", notional=float(min(self.max_notional, self.max_notional)),
                price=float(decision.get("entry", 0.52)),
                edge_after_costs=max(0.0, float(decision.get("ev_frac", 0.0))),
                spread=0.01, data_age_s=1.0, ambiguity_score=0.0, mode="paper",
                rationale="btc_pulse paper experiment", meta=self.namespace())
            ctx = RiskContext(equity=self.equity, total_exposure=0.0,
                              market_exposure=0.0, open_orders=0, day_pnl=-self._day_loss)
            decision_obj = self.risk.evaluate(proposal, ctx)
            return bool(getattr(decision_obj, "approved", False))
        except Exception:  # noqa: BLE001 — fail closed (no trade) on any gate error
            return False

    # -- simulation helpers -------------------------------------------- #
    def _advance_price(self) -> None:
        if self._price_fn is not None:
            try:
                self._price = float(self._price_fn())
            except Exception:  # noqa: BLE001
                pass
        else:
            # deterministic seeded random walk
            drift = self._rng.uniform(-0.0015, 0.0015)
            self._price = round(self._price * (1.0 + drift), 2)
        self._closes.append(self._price)
        if len(self._closes) > 1000:
            self._closes = self._closes[-1000:]

    def _regime_p_up(self) -> float:
        try:
            from engine.quant import markov
            return float(markov.fit(self._closes).get("p_up", 0.5))
        except Exception:  # noqa: BLE001
            return 0.5

    def _simulate_fill(self) -> float:
        # realistic partial-fill model: full fill for a tight book, small chance
        # of a partial fill. Deterministic given the seed.
        return 1.0 if self._rng.random() > 0.05 else 0.6

    def _update_drawdown(self) -> None:
        self._peak_equity = max(self._peak_equity, self.equity)
        dd = self._peak_equity - self.equity
        self.max_drawdown = round(max(self.max_drawdown, dd), 4)

    # -- metrics + status ---------------------------------------------- #
    def _calibration(self) -> dict:
        try:
            from engine.replay.calibration import (brier_score,
                                                   expected_calibration_error, log_loss)
            pairs = list(self.learner.preds)
            return {"brier": brier_score(pairs), "log_loss": log_loss(pairs),
                    "ece": expected_calibration_error(pairs)}
        except Exception:  # noqa: BLE001
            return {"brier": None, "log_loss": None, "ece": None}

    def _risk_metrics(self) -> dict:
        pnls = list(self.learner.pnls)
        n = len(pnls)
        if n == 0:
            return {"sharpe": 0.0, "sortino": 0.0, "calmar": 0.0}
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / n
        std = var ** 0.5
        downside = [p for p in pnls if p < 0]
        dvar = (sum(p * p for p in downside) / len(downside)) if downside else 0.0
        dstd = dvar ** 0.5
        total_return = self.equity - self._start_equity
        return {
            "sharpe": round(_safe_div(mean, std) * (n ** 0.5), 4) if std else 0.0,
            "sortino": round(_safe_div(mean, dstd) * (n ** 0.5), 4) if dstd else 0.0,
            "calmar": round(_safe_div(total_return, self.max_drawdown), 4)
            if self.max_drawdown else 0.0,
        }

    def transfer_gate_status(self) -> str:
        # Transfer learning into the Polymarket namespace is OFF by default and
        # this module never performs it regardless of the flag.
        return "blocked" if not self.transfer_allowed else "advisory_only"

    def blockers(self) -> list[str]:
        out = []
        if self.frozen and not self.enabled_flag:
            out.append("disabled")
        if not self.safety["passed"]:
            out.append(f"safety:{self.safety['fail_closed_reason']}")
        if self.kill_switch_active:
            out.append("drawdown_kill_switch")
        if self.last_error:
            out.append("last_error")
        return out

    def status(self) -> dict:
        cal = self._calibration()
        rm = self._risk_metrics()
        return {
            "btc_pulse_enabled": bool(self.enabled_flag),
            "btc_pulse_frozen": bool(self.frozen),
            "paper_only": True,
            "isolated_learning": bool(self.isolated_learning),
            "live_enabled": False,
            "legacy_autotrade_enabled": False,
            "transfer_allowed": bool(self.transfer_allowed),
            "experiment_id": EXPERIMENT_ID,
            "strategy_family": STRATEGY_FAMILY,
            "btc_pulse_tick_seconds": self.tick_seconds,
            "btc_pulse_round_seconds": self.round_seconds,
            "btc_pulse_risk_gate_required": bool(self.require_risk_gate),
            "btc_pulse_realistic_fill_required": bool(self.require_realistic_fill),
            "btc_pulse_ticks": self.ticks,
            "btc_pulse_rounds_seen": self.rounds_seen,
            "btc_pulse_decisions": self.decisions,
            "btc_pulse_no_trade_decisions": self.no_trade_decisions,
            "btc_pulse_shadow_decisions": self.shadow_decisions,
            "btc_pulse_feedback_acceleration_enabled": bool(self.accel_enabled),
            "btc_pulse_paper_trades": self.paper_trades,
            "btc_pulse_rejected_trades": self.rejected_trades,
            "btc_pulse_rejection_reasons": dict(self.rejection_reasons),
            "btc_pulse_ev_positive_count": self.ev_positive_count,
            "btc_pulse_ev_negative_rejected_count": self.ev_negative_rejected_count,
            "btc_pulse_win_rate": self.learner.win_rate(),
            "btc_pulse_sharpe": rm["sharpe"],
            "btc_pulse_sortino": rm["sortino"],
            "btc_pulse_calmar": rm["calmar"],
            "btc_pulse_max_drawdown": self.max_drawdown,
            "btc_pulse_brier": cal["brier"],
            "btc_pulse_log_loss": cal["log_loss"],
            "btc_pulse_ece": cal["ece"],
            "btc_pulse_realistic_fill_pnl": self.realistic_fill_pnl,
            "btc_pulse_after_cost_pnl": self.after_cost_pnl,
            "btc_pulse_equity": self.equity,
            "btc_pulse_transfer_gate_status": self.transfer_gate_status(),
            "btc_pulse_last_tick_ts": self.last_tick_ts,
            "btc_pulse_last_error": self.last_error,
            "btc_pulse_kill_switch_active": self.kill_switch_active,
            "btc_pulse_safety": self.safety,
            "btc_pulse_blockers": self.blockers(),
        }


def resolved_pulse_config(cfg) -> dict:
    """Compact resolved BTC Pulse config for preflight printing (read-only)."""
    return {
        "BTC_PULSE_ENABLED": bool(getattr(cfg, "btc_pulse_enabled", False)),
        "BTC_PULSE_PAPER_ONLY": bool(getattr(cfg, "btc_pulse_paper_only", True)),
        "BTC_PULSE_ISOLATED_LEARNING": bool(getattr(cfg, "btc_pulse_isolated_learning", True)),
        "BTC_PULSE_ALLOW_TRANSFER_LEARNING": bool(
            getattr(cfg, "btc_pulse_allow_transfer_learning", False)),
        "BTC_PULSE_LIVE_ENABLED": bool(getattr(cfg, "btc_pulse_live_enabled", False)),
        "BTC_AUTOTRADE_ENABLED": bool(getattr(cfg, "btc_pulse_legacy_autotrade_enabled", False)
                                      or _envb("BTC_AUTOTRADE_ENABLED", False)),
        "btc_pulse_tick_seconds": int(getattr(cfg, "btc_pulse_tick_seconds", 30)),
        "btc_pulse_round_seconds": int(getattr(cfg, "btc_pulse_round_seconds", 300)),
        "btc_pulse_risk_gate_required": bool(getattr(cfg, "btc_pulse_require_risk_gate", True)),
        "btc_pulse_realistic_fill_required": bool(
            getattr(cfg, "btc_pulse_require_realistic_fill", True)),
    }


def pulse_preflight(cfg) -> dict:
    """Fail-closed BTC Pulse preflight. Returns resolved config + checks +
    ``btc_pulse_status`` (frozen/active/disabled)."""
    trainer = BtcPulsePaperTrainer(cfg)
    resolved = resolved_pulse_config(cfg)
    safety = trainer.safety_check()
    if not bool(getattr(cfg, "btc_pulse_enabled", False)):
        status = "disabled"
    elif not safety["passed"]:
        status = "frozen"
    else:
        status = "active"
    return {
        "resolved": resolved,
        "checks": safety["checks"],
        "passed": (status != "frozen"),     # disabled or active are both OK
        "fail_closed_reason": safety["fail_closed_reason"] if status == "frozen" else None,
        "btc_pulse_status": status,
    }
