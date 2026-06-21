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
from engine.pulse.price import PulsePriceFeed
from engine.pulse.fair_value import RollingVol, digital_p_up
from engine.pulse.strategy import decide
from engine.pulse.executor import PulseLedger
from engine.pulse.settlement import PulseCalibration, resolve_outcome

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
    settle_grace_s: float = 60.0
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
            settle_grace_s=_envf("PULSE_SETTLE_GRACE_S", 60.0),
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
            data_dir=os.getenv("HTE_DATA_DIR", "/data"))


class PulseEngine:
    def __init__(self, cfg: Optional[PulseConfig] = None, *, market_feed=None,
                 price_feed=None):
        self.cfg = cfg or PulseConfig()
        self.market = market_feed or PulseMarketFeed()
        self.price = price_feed or PulsePriceFeed(
            vol=RollingVol(window_s=self.cfg.vol_window_s),
            max_open_lag_s=self.cfg.max_open_lag_s)
        self.ledger = PulseLedger()
        self.calib = PulseCalibration()
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
        self.price.poll(now)
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

        for w in windows:
            # snapshot the open price the moment the window begins
            self.price.snapshot_open(w.event_id, w.open_ts, now=now)
            if not w.is_open(now):
                _bump("not_open_yet")
                continue
            if self.ledger.has_position(w.event_id):
                _bump("already_positioned")
                continue
            snap = self.price.open_snapshot(w.event_id)
            if snap is None:
                _bump("no_open_snapshot")
                continue
            if snap.lag_s > self.cfg.max_open_lag_s:
                _bump("open_snapshot_late")
                continue
            s_now = self.price.current()
            sigma = self.price.sigma_per_sec(now)
            if s_now is None or sigma is None:
                _bump("no_price_or_vol")
                continue
            # trust gate: a floored/flat sigma or too-few samples => the digital P(up) is
            # not trustworthy; skip rather than trade noise.
            if self.price.vol.samples < self.cfg.min_vol_samples \
                    or sigma <= self.cfg.sigma_trust_floor:
                _bump("untrusted_vol")
                continue
            if ov_blackout:
                _bump("grok_event_blackout")     # imminent high-impact event — don't open
                continue
            self.market.hydrate_books(w)
            ttc = w.seconds_to_close(now)
            # the overlay can only RAISE sigma (>=1.0) -> more conservative P(up)
            fair = digital_p_up(s_now, snap.price, sigma * ov_vol_mult, ttc)
            d = decide(w, fair, now, min_edge=self.cfg.min_edge,
                       min_seconds_to_close=self.cfg.min_seconds_to_close,
                       min_depth_usd=self.cfg.min_depth_usd,
                       edge_buffer=self.cfg.edge_buffer, max_price=self.cfg.max_price,
                       min_seconds_since_open=self.cfg.min_seconds_since_open,
                       basis_buffer=self.cfg.basis_buffer)
            evald.append({"title": w.title, "fair_p_up": fair, **d.to_dict(),
                          "ttc_s": round(ttc, 1)})
            if d.trade:
                self.ledger.open_position(w, d, now, size_usd=self.cfg.size_usd,
                                          s_open=snap.price)
                _bump("opened")
            else:
                _bump(d.reason)

        self._settle_due(now)
        self._reasons = reasons
        self._last_eval = evald[-12:]
        self._prune_positions()
        self._persist()
        return {"ticks": self.ticks, "reasons": reasons, "stats": self.ledger.stats()}

    def _settle_due(self, now: float) -> None:
        for pos in list(self.ledger.open_positions()):
            if pos.close_ts > now:
                continue
            s_close = self.price.current()
            allow_proxy = (now - pos.close_ts) > self.cfg.settle_grace_s
            outcome, source = resolve_outcome(
                pos.market_id, gamma_feed=self.market, s_open=pos.s_open,
                s_close=s_close, allow_proxy=allow_proxy)
            if outcome is None:
                continue                      # not resolvable yet — retry next tick
            self.ledger.settle(pos.window_key, outcome, s_open=pos.s_open, s_close=s_close)
            self.calib.observe(pos.fair_at_entry, outcome)
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
    def status(self) -> dict:
        return {
            "schema": "btc_pulse/1.0", "paper_only": True, "live_trading_enabled": False,
            "ts": self.last_tick_ts, "ticks": self.ticks,
            "config": {"tick_seconds": self.cfg.tick_seconds, "size_usd": self.cfg.size_usd,
                       "min_edge": self.cfg.min_edge, "edge_buffer": self.cfg.edge_buffer,
                       "min_depth_usd": self.cfg.min_depth_usd, "max_price": self.cfg.max_price},
            "price": self.price.status(),
            "ledger": self.ledger.stats(),
            "calibration": self.calib.to_dict(),
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
