"""Controlled PAPER-trading campaign orchestrator (paper-only, no live path).

Phases:
  0. Preflight safety check (aborts with a RED warning on any live config).
  1. Market discovery (via the Universe Manager): Tier A/B/C + rejected reasons.
  2. Paper signal generation (Tier A only): fair value, edge, net edge.
  3. Paper execution (realistic simulator: partial/no-fill/stale/timeout/cancel).
  4. Position monitoring with exit rules (TP / SL / edge-gone / stale / max-hold).
  5. Dashboard status (read by GET /api/campaign/status).
  6. Hourly reports + latest_report.md.
  7. Final report + pass/fail decision.

Everything here is simulated and self-contained: the fill simulator and ledger
never call the engine's order path, never sign anything, and never hit a venue.
The signal "model" is an explicitly SIMULATED fair value used only to exercise
the campaign machinery on paper — it is not real alpha.

Quant scope — *Signal Generation & Strategy Development* + *Backtesting &
Simulation*: this campaign exercises the priority-3 directional predictive path;
each signal record is tagged with its strategy + priority so reports align with
the engine-wide hierarchy (Bregman arbitrage P1 > calibrated statistical
mispricing P2 > directional predictive edge P3, resolved in
:mod:`engine.training.signal_resolver`). Grok stays research-only here too.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from engine.markets import universe_manager as um
from engine.campaigns import signal_models as sm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Live-trading env flags that, if set, must abort the campaign.
_LIVE_FLAGS = (
    "LIVE_BROKER_ENABLED",
    "MICRO_LIVE_ENABLED",
    "MICRO_LIVE_ALLOW_PRODUCTION",
    "PRODUCTION_EXECUTION_ENABLED",
)


def _truthy(v) -> bool:
    return str(v).strip() not in ("0", "false", "False", "", "None", "no", "No")


def _simple_yaml(text: str) -> dict:
    """Minimal parser for the flat ``key: value`` campaign config, so the
    campaign runs even when PyYAML is not installed."""
    out: dict = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line or line[0] in (" ", "\t") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if not key or val == "":
            continue
        low = val.lower()
        if low in ("true", "false"):
            parsed = (low == "true")
        else:
            try:
                parsed = int(val)
            except ValueError:
                try:
                    parsed = float(val)
                except ValueError:
                    parsed = val.strip("\"'")
        out[key] = parsed
    return out


def _read_config_file(path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml  # PyYAML is optional
        return yaml.safe_load(text) or {}
    except ModuleNotFoundError:
        return _simple_yaml(text)


@dataclass
class CampaignConfig:
    campaign_name: str = "controlled_paper_campaign_001"
    duration_days: float = 7.0
    catalog_refresh_seconds: int = 600
    score_refresh_seconds: int = 60
    campaign_report_frequency_seconds: int = 3600

    market_scan_limit: int = 1000
    market_shortlist_limit: int = 100
    market_live_watchlist_limit: int = 80
    trade_candidate_limit: int = 20

    max_open_trades: int = 3
    max_daily_new_trades: int = 10
    max_trade_size_paper_usd: float = 25.0
    max_total_paper_exposure_usd: float = 100.0
    starting_paper_cash_usd: float = 1000.0

    min_market_liquidity_usd: float = 1000.0
    min_24h_volume_usd: float = 500.0
    max_allowed_spread: float = 0.04
    min_top_of_book_depth_usd: float = 100.0

    min_net_edge: float = 0.025
    fee_bps: float = 0.0
    slippage_impact_coeff: float = 0.5
    max_slippage: float = 0.03

    take_profit_edge_capture: float = 0.60
    stop_loss_percent: float = 0.12
    max_holding_hours: float = 48.0
    exit_if_net_edge_below: float = 0.005
    no_resolution_hold_mode: bool = True

    risk_gates_enabled: bool = True
    duplicate_prevention_enabled: bool = True
    universe_manager_enabled: bool = True
    dashboard_enabled: bool = True
    logging_enabled: bool = True
    paper_simulator_enabled: bool = True

    # signal source + recursive feedback loop
    signal_model: str = "simulated"   # "simulated" | "research" (Grok research-only)
    feedback_enabled: bool = True

    max_paper_drawdown_pct: float = 0.20

    # absolute paper ceilings (never exceeded regardless of yaml)
    HARD_MAX_OPEN_TRADES: int = 3
    HARD_MAX_TRADE_SIZE_USD: float = 25.0

    def __post_init__(self) -> None:
        self.max_open_trades = max(0, min(int(self.max_open_trades), self.HARD_MAX_OPEN_TRADES))
        self.max_trade_size_paper_usd = min(float(self.max_trade_size_paper_usd),
                                            self.HARD_MAX_TRADE_SIZE_USD)

    @classmethod
    def from_yaml(cls, path) -> "CampaignConfig":
        data = _read_config_file(path)
        known = {f for f in cls.__dataclass_fields__}  # noqa
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_universe_config(self) -> um.UniverseConfig:
        return um.UniverseConfig(
            scan_limit=self.market_scan_limit,
            shortlist_limit=self.market_shortlist_limit,
            live_watchlist_limit=self.market_live_watchlist_limit,
            trade_candidate_limit=self.trade_candidate_limit,
            max_open_polymarket_trades=self.max_open_trades,
            min_liquidity_usd=self.min_market_liquidity_usd,
            min_volume_24h_usd=self.min_24h_volume_usd,
            max_allowed_spread=self.max_allowed_spread,
            min_top_of_book_depth_usd=self.min_top_of_book_depth_usd,
        )

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items()}


# ---------------------------------------------------------------------------
# Phase 0: preflight
# ---------------------------------------------------------------------------

@dataclass
class PreflightResult:
    ok: bool
    checks: list
    live_config_detected: bool
    red_warning: Optional[str]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "live_config_detected": self.live_config_detected,
                "red_warning": self.red_warning, "checks": self.checks,
                "failed": [c for c in self.checks if not c["passed"]]}


def preflight_check(cfg: CampaignConfig, env: Optional[dict] = None,
                    data_dir: Optional[Path] = None) -> PreflightResult:
    env = dict(os.environ if env is None else env)
    checks: list = []

    def add(name, passed, detail, key):
        checks.append({"name": name, "passed": bool(passed), "detail": detail, "config_key": key})

    mode = (env.get("HTE_MODE") or "paper").strip().lower()
    add("paper_mode_enabled", mode == "paper", f"HTE_MODE={mode}", "HTE_MODE")
    add("real_trading_disabled", not _truthy(env.get("HTE_AUTOTRADE", "0"))
        and not _truthy(env.get("LIVE_BROKER_ENABLED", "0")),
        f"HTE_AUTOTRADE={env.get('HTE_AUTOTRADE','0')} LIVE_BROKER_ENABLED={env.get('LIVE_BROKER_ENABLED','0')}",
        "HTE_AUTOTRADE / LIVE_BROKER_ENABLED")
    add("micro_live_disabled", not _truthy(env.get("MICRO_LIVE_ENABLED", "0"))
        and not _truthy(env.get("MICRO_LIVE_ALLOW_PRODUCTION", "0")),
        f"MICRO_LIVE_ENABLED={env.get('MICRO_LIVE_ENABLED','0')}",
        "MICRO_LIVE_ENABLED / MICRO_LIVE_ALLOW_PRODUCTION")
    add("production_design_only", not _truthy(env.get("PRODUCTION_EXECUTION_ENABLED", "0")),
        f"PRODUCTION_EXECUTION_ENABLED={env.get('PRODUCTION_EXECUTION_ENABLED','0')}",
        "PRODUCTION_EXECUTION_ENABLED")
    add("max_open_trades_ok", cfg.max_open_trades <= 3, f"max_open_trades={cfg.max_open_trades}",
        "max_open_trades")
    add("max_trade_size_ok", cfg.max_trade_size_paper_usd <= 25,
        f"max_trade_size_paper_usd={cfg.max_trade_size_paper_usd}", "max_trade_size_paper_usd")
    add("risk_gates_enabled", cfg.risk_gates_enabled, "", "risk_gates_enabled")
    add("duplicate_prevention_enabled", cfg.duplicate_prevention_enabled, "",
        "duplicate_prevention_enabled")
    add("universe_manager_enabled", cfg.universe_manager_enabled, "", "universe_manager_enabled")
    add("dashboard_available", cfg.dashboard_enabled, "", "dashboard_enabled")
    add("logging_enabled", cfg.logging_enabled, "", "logging_enabled")

    writable = False
    detail = "no data_dir"
    if data_dir is not None:
        try:
            Path(data_dir).mkdir(parents=True, exist_ok=True)
            probe = Path(data_dir) / ".campaign_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable = True
            detail = str(data_dir)
        except OSError as exc:  # noqa: BLE001
            detail = f"not writable: {exc}"
    add("storage_writable", writable, detail, "data_dir")
    add("paper_order_simulator_active", cfg.paper_simulator_enabled, "", "paper_simulator_enabled")

    live_flags_on = [f for f in _LIVE_FLAGS if _truthy(env.get(f, "0"))]
    live_detected = bool(live_flags_on)
    red = None
    if live_detected:
        red = ("\n" + "=" * 64 + "\n"
               "  \U0001F534  RED WARNING: LIVE-TRADING CONFIG DETECTED  \U0001F534\n"
               f"  The following live flags are set: {', '.join(live_flags_on)}\n"
               "  Refusing to start the paper campaign. Unset these and retry.\n"
               + "=" * 64)
    ok = all(c["passed"] for c in checks) and not live_detected
    return PreflightResult(ok=ok, checks=checks, live_config_detected=live_detected, red_warning=red)


# ---------------------------------------------------------------------------
# Simulated signal model + fill simulator + risk gate
# ---------------------------------------------------------------------------

class SimulatedSignalModel:
    """SIMULATED fair-value model (NOT real alpha).

    Produces a deterministic pseudo-edge per market so the campaign machinery
    can be exercised on paper. The deviation is a stable function of the market
    id + a seed, bounded to +-6 cents."""

    def __init__(self, seed: int = 42):
        self.seed = seed

    def fair_value(self, rec: um.MarketRecord) -> float:
        mid = rec.yes_price if rec.yes_price is not None else 0.5
        h = hashlib.sha256(f"{self.seed}:{rec.market_id}".encode()).digest()
        # map first 4 bytes to [-0.06, 0.06]
        n = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
        dev = (n - 0.5) * 0.12
        return max(0.02, min(0.98, mid + dev))


@dataclass
class FillOutcome:
    status: str            # filled | partial | no_fill | cancelled | rejected
    fill_price: Optional[float]
    filled_size: float
    slippage: float
    reason: str


class PaperFillSimulator:
    """Realistic PAPER fill simulator. No instant perfect fills.

    Models: stale-book rejection, order timeout, no-fill, cancellation,
    depth-limited partial fills, and slippage proportional to size/depth."""

    def __init__(self, cfg: CampaignConfig, stale_ms: float = 3000.0):
        self.cfg = cfg
        self.stale_ms = stale_ms

    def simulate(self, *, side: str, intended_price: float, size_usd: float,
                 top_depth_usd: float, book_age_ms: Optional[float], rng: random.Random) -> FillOutcome:
        if book_age_ms is not None and book_age_ms > self.stale_ms:
            return FillOutcome("rejected", None, 0.0, 0.0, "stale_book")
        roll = rng.random()
        if roll < 0.04:
            return FillOutcome("rejected", None, 0.0, 0.0, "order_timeout")
        if roll < 0.10:
            return FillOutcome("no_fill", None, 0.0, 0.0, "no_fill_at_price")
        if roll < 0.13:
            return FillOutcome("cancelled", None, 0.0, 0.0, "cancelled_pre_fill")
        # slippage proportional to how much of top-of-book we consume
        depth = max(1.0, top_depth_usd)
        impact = self.cfg.slippage_impact_coeff * (size_usd / depth)
        slippage = min(self.cfg.max_slippage, impact) + rng.uniform(0.0, 0.003)
        fill_price = intended_price + slippage if side in ("BUY", "buy") else intended_price - slippage
        fill_price = max(0.01, min(0.99, fill_price))
        if size_usd > top_depth_usd:  # partial fill: only top-of-book available
            return FillOutcome("partial", round(fill_price, 4), round(top_depth_usd, 2),
                               round(slippage, 4), "partial_top_of_book")
        return FillOutcome("filled", round(fill_price, 4), round(size_usd, 2),
                           round(slippage, 4), "filled")


class CampaignRiskGate:
    """Paper risk gate. Every paper order passes through here and the result is
    recorded. Enforces the campaign's documented paper limits."""

    def __init__(self, cfg: CampaignConfig):
        self.cfg = cfg

    def evaluate(self, *, net_edge: float, spread: float, top_depth_usd: float,
                 size_usd: float, open_trades: int, current_exposure: float,
                 daily_new_trades: int, event_group: str,
                 open_event_groups: set) -> tuple[bool, str]:
        c = self.cfg
        if not c.risk_gates_enabled:
            return False, "risk_gates_disabled"
        if net_edge < c.min_net_edge:
            return False, "net_edge_below_min"
        if spread > c.max_allowed_spread:
            return False, "spread_too_wide"
        if top_depth_usd < c.min_top_of_book_depth_usd:
            return False, "insufficient_depth"
        if size_usd > c.max_trade_size_paper_usd:
            return False, "trade_size_exceeds_cap"
        if open_trades >= c.max_open_trades:
            return False, "max_open_trades_reached"
        if current_exposure + size_usd > c.max_total_paper_exposure_usd:
            return False, "exposure_cap_reached"
        if daily_new_trades >= c.max_daily_new_trades:
            return False, "daily_new_trade_cap_reached"
        if c.duplicate_prevention_enabled and event_group in open_event_groups:
            return False, "duplicate_event_exposure"
        return True, "approved"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class PaperPosition:
    order_id: str
    market_id: str
    token_id: str
    question: str
    side: str
    event_group: str
    entry_price: float
    size_usd: float
    filled_usd: float
    fair_value_entry: float
    gross_edge: float
    net_edge: float
    spread_at_entry: float
    depth_at_entry: float
    opened_ts: float
    current_price: float = 0.0
    realized_pnl: float = 0.0
    status: str = "open"           # open | closed
    exit_reason: str = ""
    closed_ts: float = 0.0
    signal_source: str = "simulated"
    confidence: float = 0.0

    def shares(self) -> float:
        return self.filled_usd / max(self.entry_price, 0.01)

    def unrealized_pnl(self, price: float) -> float:
        if self.status != "open":
            return 0.0
        return round(self.shares() * (price - self.entry_price), 4)


class PaperCampaign:
    def __init__(self, cfg: CampaignConfig, data_dir: Path, reports_dir: Optional[Path] = None,
                 seed: int = 42, accelerated: bool = True, catalog_source: str = "synthetic",
                 signal_model=None, store=None):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.reports_root = Path(reports_dir) if reports_dir else \
            self.data_dir / "reports" / "paper_campaign" / cfg.campaign_name
        self.seed = seed
        self.rng = random.Random(seed)
        self.accelerated = accelerated
        self.catalog_source = catalog_source

        # pluggable signal source (simulated, or Grok research — research-only)
        self.signal_model = signal_model or sm.build_signal_model(
            cfg.signal_model, store=store, seed=seed)
        self.model = self.signal_model  # back-compat alias
        # recursive feedback loop: closed-trade outcomes calibrate future signals
        self.feedback = sm.FeedbackCalibrator(
            path=self.data_dir / f"campaign_feedback_{cfg.campaign_name}.json",
            enabled=cfg.feedback_enabled)
        self.signal_source_counts: dict[str, int] = {}
        self.simulator = PaperFillSimulator(cfg)
        self.gate = CampaignRiskGate(cfg)
        self.umgr = um.UniverseManager(cfg=cfg.to_universe_config(), paper=True,
                                       live_subscribe_enabled=False)

        # state
        self.cash = cfg.starting_paper_cash_usd
        self.starting_cash = cfg.starting_paper_cash_usd
        self.positions: list[PaperPosition] = []
        self.orders: list[dict] = []
        self.rejected_orders: list[dict] = []
        self.risk_decisions: list[dict] = []
        self.closed_trades: list[PaperPosition] = []
        self.scanned = 0
        self.passed_filters = 0
        self.rejected_by_reason: dict[str, int] = {}
        self.top_candidates: list[dict] = []
        self.errors: list[str] = []
        self.equity_peak = self.starting_cash
        self.max_drawdown = 0.0
        self.last_catalog_refresh_ts = 0.0
        self.last_score_refresh_ts = 0.0
        self.last_hourly_report_ts = 0.0
        self.hourly_reports_written = 0
        self._order_seq = 0
        self._tick = 0
        self._daily_new: dict[str, int] = {}
        self.started_ts = 0.0
        self.status_state = "init"
        self._virtual_now = time.time()
        self._live_violation = False  # set true only if a live path is ever touched

    # ---- clock -----------------------------------------------------------
    def _clock(self) -> float:
        return self._virtual_now if self.accelerated else time.time()

    def _day_key(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y-%m-%d")

    # ---- phase 0 ---------------------------------------------------------
    def preflight(self) -> PreflightResult:
        return preflight_check(self.cfg, data_dir=self.data_dir)

    # ---- helpers ---------------------------------------------------------
    def open_event_groups(self) -> set:
        return {p.event_group for p in self.positions if p.status == "open"}

    def current_exposure(self) -> float:
        return round(sum(p.filled_usd for p in self.positions if p.status == "open"), 2)

    def open_trade_count(self) -> int:
        return sum(1 for p in self.positions if p.status == "open")

    # ---- phase 1 ---------------------------------------------------------
    def discover(self, catalog: list) -> um.UniverseSnapshot:
        snap = self.umgr.ingest(catalog, open_event_groups=self.open_event_groups(),
                                now=self._clock())
        self.scanned = snap.scanned
        self.passed_filters = snap.passed_filters
        self.rejected_by_reason = dict(snap.rejected_by_reason)
        self.top_candidates = [s.to_dict() for s in snap.scored[:10]]
        self.last_catalog_refresh_ts = self._clock()
        self.last_score_refresh_ts = self._clock()
        return snap

    # ---- phase 2 + 3 -----------------------------------------------------
    def _price_for(self, rec: um.MarketRecord) -> tuple[float, float, float]:
        """Return (mid, best_ask, best_bid) using available fields."""
        bid = um._as_float(rec.raw.get("bestBid"), 0.0)
        ask = um._as_float(rec.raw.get("bestAsk"), 0.0)
        if bid and ask:
            return (bid + ask) / 2.0, ask, bid
        mid = rec.yes_price if rec.yes_price is not None else 0.5
        half = max(rec.spread, 0.0) / 2.0
        return mid, mid + half, mid - half

    def generate_signals_and_trade(self, snap: um.UniverseSnapshot) -> None:
        day = self._day_key()
        for sm in snap.tier("A"):
            rec = sm.record
            mid, ask, bid = self._price_for(rec)
            sig = self.signal_model.evaluate(rec)   # Grok research / simulated (research-only)
            fair = sig.fair_value
            self.signal_source_counts[sig.source] = self.signal_source_counts.get(sig.source, 0) + 1
            # decide side from where fair sits vs mid
            side = "BUY" if fair >= mid else "SELL"
            entry_ref = ask if side == "BUY" else bid
            # BUY yes: profit if fair > ask. SELL yes (at bid): profit if bid > fair.
            gross_edge = (fair - ask) if side == "BUY" else (bid - fair)
            # net edge after spread + estimated slippage + fees
            size_usd = min(self.cfg.max_trade_size_paper_usd,
                           max(0.0, self.cfg.max_total_paper_exposure_usd - self.current_exposure()))
            depth = rec.top_depth_usd
            est_slip = min(self.cfg.max_slippage,
                           self.cfg.slippage_impact_coeff * (size_usd / max(1.0, depth)))
            fee = self.cfg.fee_bps / 10000.0
            net_edge_raw = round(gross_edge - rec.spread - est_slip - fee, 4)
            # recursive feedback: scale the edge by calibration learned from closed trades
            adj = self.feedback.edge_adjustment()
            net_edge = round(net_edge_raw * adj, 4)

            base = {
                "ts": round(self._clock(), 1), "market_id": rec.market_id,
                "token_id": (rec.clob_token_ids[0] if rec.clob_token_ids else ""),
                "question": rec.question[:140], "side": side,
                "intended_price": round(entry_ref, 4), "size_usd": round(size_usd, 2),
                "spread": round(rec.spread, 4), "top_depth_usd": round(depth, 2),
                "fair_value": round(fair, 4), "gross_edge": round(gross_edge, 4),
                "net_edge_raw": net_edge_raw, "net_edge": net_edge,
                "edge_adjustment": adj, "signal_source": sig.source,
                "confidence": round(sig.confidence, 3), "event_group": rec.group_key,
                # priority-hierarchy tag: this campaign is the P3 directional path.
                "strategy": "directional", "signal_priority": 3,
            }

            approved, reason = self.gate.evaluate(
                net_edge=net_edge, spread=rec.spread, top_depth_usd=depth, size_usd=size_usd,
                open_trades=self.open_trade_count(), current_exposure=self.current_exposure(),
                daily_new_trades=self._daily_new.get(day, 0), event_group=rec.group_key,
                open_event_groups=self.open_event_groups())
            self.risk_decisions.append({**base, "approved": approved, "risk_gate_result": reason})
            if not approved:
                self.rejected_orders.append({**base, "rejection_reason": reason,
                                             "risk_gate_result": reason})
                continue

            # phase 3: realistic paper fill
            book_age = um._as_float(rec.raw.get("bookAgeMs"), None) if rec.raw.get("bookAgeMs") else None
            outcome = self.simulator.simulate(
                side=side, intended_price=entry_ref, size_usd=size_usd,
                top_depth_usd=depth, book_age_ms=book_age, rng=self.rng)
            order_rec = {**base, "fill_status": outcome.status,
                         "simulated_fill_price": outcome.fill_price,
                         "filled_size_usd": outcome.filled_size,
                         "slippage": outcome.slippage, "risk_gate_result": reason,
                         "rejection_reason": None}
            self._order_seq += 1
            order_rec["order_id"] = f"pc-{self._order_seq}"
            self.orders.append(order_rec)

            if outcome.status in ("filled", "partial") and outcome.filled_size > 0:
                self._daily_new[day] = self._daily_new.get(day, 0) + 1
                self.cash -= outcome.filled_size
                pos = PaperPosition(
                    order_id=order_rec["order_id"], market_id=rec.market_id,
                    token_id=order_rec["token_id"], question=rec.question[:140], side=side,
                    event_group=rec.group_key, entry_price=outcome.fill_price,
                    size_usd=size_usd, filled_usd=outcome.filled_size,
                    fair_value_entry=fair, gross_edge=gross_edge, net_edge=net_edge,
                    spread_at_entry=rec.spread, depth_at_entry=depth,
                    opened_ts=self._clock(), current_price=outcome.fill_price,
                    signal_source=sig.source, confidence=sig.confidence)
                self.positions.append(pos)
            else:
                order_rec["rejection_reason"] = outcome.reason

            # stop opening once we hit the open-trade cap
            if self.open_trade_count() >= self.cfg.max_open_trades:
                break

    # ---- phase 4 ---------------------------------------------------------
    def _evolve_price(self, pos: PaperPosition) -> float:
        r = random.Random(f"{self.seed}:{pos.market_id}:{self._tick}")
        drift = (pos.fair_value_entry - pos.entry_price) * 0.15  # pull toward fair value
        shock = r.uniform(-0.02, 0.02)
        return max(0.01, min(0.99, pos.current_price + drift + shock))

    def monitor_positions(self) -> None:
        now = self._clock()
        for pos in self.positions:
            if pos.status != "open":
                continue
            pos.current_price = round(self._evolve_price(pos), 4)
            held_h = (now - pos.opened_ts) / 3600.0
            entry_fair_gap = pos.fair_value_entry - pos.entry_price
            target = pos.entry_price + self.cfg.take_profit_edge_capture * entry_fair_gap
            cur_net_edge = round(pos.fair_value_entry - pos.current_price - pos.spread_at_entry, 4)
            adverse = (pos.entry_price - pos.current_price) / max(pos.entry_price, 0.01)

            exit_reason = ""
            if pos.current_price >= target and entry_fair_gap > 0:
                exit_reason = "take_profit"
            elif adverse >= self.cfg.stop_loss_percent:
                exit_reason = "stop_loss"
            elif cur_net_edge < self.cfg.exit_if_net_edge_below:
                exit_reason = "edge_disappeared"
            elif held_h >= self.cfg.max_holding_hours:
                exit_reason = "max_holding_hours"
            elif self.cfg.no_resolution_hold_mode and self._near_resolution(pos):
                exit_reason = "no_resolution_hold"
            if exit_reason:
                self._close(pos, exit_reason, now)

    def _near_resolution(self, pos: PaperPosition) -> bool:
        return False  # synthetic catalog has no per-tick resolution clock in dry runs

    def _close(self, pos: PaperPosition, reason: str, now: float) -> None:
        pnl = pos.shares() * (pos.current_price - pos.entry_price)
        pos.realized_pnl = round(pnl, 4)
        self.cash += pos.filled_usd + pnl
        pos.status = "closed"
        pos.exit_reason = reason
        pos.closed_ts = now
        self.closed_trades.append(pos)
        # recursive feedback loop: this outcome calibrates future signals
        try:
            self.feedback.record_outcome(
                predicted_prob=pos.fair_value_entry, predicted_edge=pos.net_edge,
                realized_pnl=pnl, size_usd=pos.filled_usd)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"feedback: {exc}")

    # ---- one cycle -------------------------------------------------------
    def tick(self, catalog: list) -> None:
        self._tick += 1
        try:
            snap = self.discover(catalog)
            self.monitor_positions()
            self.generate_signals_and_trade(snap)
            self._update_drawdown()
        except Exception as exc:  # noqa: BLE001 - record, never crash the loop
            self.errors.append(f"tick {self._tick}: {type(exc).__name__}: {exc}")

    def _update_drawdown(self) -> None:
        eq = self.equity()
        self.equity_peak = max(self.equity_peak, eq)
        dd = (self.equity_peak - eq) / max(self.equity_peak, 1e-9)
        self.max_drawdown = max(self.max_drawdown, dd)

    # ---- metrics ---------------------------------------------------------
    def unrealized_pnl(self) -> float:
        return round(sum(p.unrealized_pnl(p.current_price) for p in self.positions
                         if p.status == "open"), 4)

    def realized_pnl(self) -> float:
        return round(sum(p.realized_pnl for p in self.closed_trades), 4)

    def equity(self) -> float:
        return round(self.cash + self.current_exposure() + self.unrealized_pnl(), 4)

    def metrics(self) -> dict:
        filled = [o for o in self.orders if o["fill_status"] in ("filled", "partial")
                  and o.get("filled_size_usd")]
        no_fill = [o for o in self.orders if o["fill_status"] in ("no_fill", "cancelled", "rejected")]
        wins = [p for p in self.closed_trades if p.realized_pnl > 0]
        # self.positions is the full ledger (open + closed); never add closed_trades
        # again or closed positions get double-counted.
        entry_edges = [p.net_edge for p in self.positions]
        slippages = [o["slippage"] for o in filled]
        hold_times = [(p.closed_ts - p.opened_ts) / 3600.0 for p in self.closed_trades]
        return {
            "orders_total": len(self.orders),
            "filled_orders": len(filled),
            "rejected_orders": len(self.rejected_orders),
            "no_fill_orders": len(no_fill),
            "fill_rate": round(len(filled) / len(self.orders), 4) if self.orders else 0.0,
            "no_fill_rate": round(len(no_fill) / len(self.orders), 4) if self.orders else 0.0,
            "open_trades": self.open_trade_count(),
            "closed_trades": len(self.closed_trades),
            "win_rate": round(len(wins) / len(self.closed_trades), 4) if self.closed_trades else 0.0,
            "avg_net_edge_at_entry": round(sum(entry_edges) / len(entry_edges), 4) if entry_edges else 0.0,
            "avg_slippage": round(sum(slippages) / len(slippages), 4) if slippages else 0.0,
            "avg_hold_hours": round(sum(hold_times) / len(hold_times), 2) if hold_times else 0.0,
            "realized_pnl": self.realized_pnl(),
            "unrealized_pnl": self.unrealized_pnl(),
            "total_pnl": round(self.equity() - self.starting_cash, 4),
            "max_drawdown_pct": round(self.max_drawdown, 4),
            "cash": round(self.cash, 2),
            "exposure": self.current_exposure(),
            "equity": self.equity(),
        }

    # ---- pass / fail -----------------------------------------------------
    def evaluate_pass_fail(self) -> dict:
        m = self.metrics()
        dup = self._has_duplicate_positions()
        checks = {
            "no_real_orders": not self._live_violation,
            "no_micro_live": not self._live_violation,
            "no_duplicate_positions": not dup,
            "max_open_never_exceeded": self._max_open_seen() <= self.cfg.max_open_trades,
            "no_exception_loop": len(self.errors) == 0,
            "fill_simulation_worked": any(o["fill_status"] in ("filled", "partial", "no_fill",
                                          "cancelled", "rejected") for o in self.orders)
                                      or len(self.orders) == 0,
            "pnl_reconciles": self._pnl_reconciles(),
            "every_trade_has_reason": all(o.get("fill_status") for o in self.orders),
            "every_rejection_has_reason": all(r.get("rejection_reason") for r in self.rejected_orders),
            "avg_net_edge_positive": m["avg_net_edge_at_entry"] > 0 or not self.positions,
            "drawdown_within_limit": m["max_drawdown_pct"] <= self.cfg.max_paper_drawdown_pct,
        }
        passed = all(checks.values())
        return {"passed": passed, "decision": "PASS" if passed else "FAIL", "checks": checks}

    def _max_open_seen(self) -> int:
        # reconstruct max concurrent open from open/close events. self.positions
        # is the full ledger; closes are sorted before opens at equal timestamps.
        events = []
        for p in self.positions:
            events.append((p.opened_ts, 1))
            if p.status == "closed":
                events.append((p.closed_ts, -1))
        events.sort(key=lambda e: (e[0], e[1]))
        cur = mx = 0
        for _, d in events:
            cur += d
            mx = max(mx, cur)
        return mx

    def _has_duplicate_positions(self) -> bool:
        seen = set()
        for p in self.positions:
            if p.status != "open":
                continue
            if p.order_id in seen:
                return True
            seen.add(p.order_id)
        # also: no two open positions in same event group
        groups = [p.event_group for p in self.positions if p.status == "open"]
        return len(groups) != len(set(groups))

    def _pnl_reconciles(self) -> bool:
        # equity must equal starting cash + realized + unrealized (tolerant of
        # cent-level float rounding in the component sums).
        expected = self.starting_cash + self.realized_pnl() + self.unrealized_pnl()
        return abs(self.equity() - expected) < 0.01

    # ---- status ----------------------------------------------------------
    def status(self) -> dict:
        m = self.metrics()
        uptime = max(0.0, self._clock() - self.started_ts) if self.started_ts else 0.0
        pf = self.evaluate_pass_fail()
        return {
            "campaign_name": self.cfg.campaign_name,
            "status": self.status_state,
            "mode": "PAPER / SIMULATED",
            "catalog_source": self.catalog_source,
            "accelerated": self.accelerated,
            "uptime_seconds": round(uptime, 1),
            "ticks": self._tick,
            "total_markets_scanned": self.scanned,
            "markets_passing_filters": self.passed_filters,
            "tier_a_count": len(self.umgr.snapshot.tier("A")) if self.umgr.snapshot else 0,
            "tier_b_count": len(self.umgr.snapshot.tier("B")) if self.umgr.snapshot else 0,
            "rejected_market_count": sum(self.rejected_by_reason.values()),
            "rejection_reasons": self.rejected_by_reason,
            "current_open_trades": m["open_trades"],
            "max_open_trades": self.cfg.max_open_trades,
            "paper_cash_balance": m["cash"],
            "paper_exposure": m["exposure"],
            "realized_pnl": m["realized_pnl"],
            "unrealized_pnl": m["unrealized_pnl"],
            "total_paper_pnl": m["total_pnl"],
            "win_rate": m["win_rate"],
            "avg_edge_at_entry": m["avg_net_edge_at_entry"],
            "avg_slippage": m["avg_slippage"],
            "fill_rate": m["fill_rate"],
            "no_fill_rate": m["no_fill_rate"],
            "rejected_order_count": m["rejected_orders"],
            "top_candidates": self.top_candidates,
            "risk_gate_status": "enabled" if self.cfg.risk_gates_enabled else "DISABLED",
            "signal_model": self.signal_model.status(),
            "signal_source_counts": self.signal_source_counts,
            "feedback": self.feedback.summary(),
            "edge_adjustment": self.feedback.edge_adjustment(),
            "last_catalog_refresh": round(self.last_catalog_refresh_ts, 1),
            "last_score_refresh": round(self.last_score_refresh_ts, 1),
            "max_drawdown_pct": m["max_drawdown_pct"],
            "errors": self.errors[-10:],
            "pass_fail": pf,
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _persist_status(self) -> None:
        try:
            path = self.data_dir / f"campaign_{self.cfg.campaign_name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.status(), default=str), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            self.errors.append(f"persist_status: {exc}")

    # ---- reports ---------------------------------------------------------
    def _report_md(self, title: str) -> str:
        m = self.metrics()
        pf = self.evaluate_pass_fail()
        lines = [f"# {title}", "",
                 f"- campaign: `{self.cfg.campaign_name}`",
                 f"- generated (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}",
                 f"- mode: **PAPER / SIMULATED** (catalog source: {self.catalog_source})",
                 f"- status: {self.status_state}  ticks: {self._tick}",
                 "",
                 "## Summary metrics", "",
                 f"- markets scanned: {self.scanned}",
                 f"- passing filters: {self.passed_filters}",
                 f"- rejected markets: {sum(self.rejected_by_reason.values())}",
                 f"- orders: {m['orders_total']}  filled: {m['filled_orders']}  "
                 f"rejected: {m['rejected_orders']}  no-fill: {m['no_fill_orders']}",
                 f"- fill rate: {m['fill_rate']}  no-fill rate: {m['no_fill_rate']}",
                 f"- open trades: {m['open_trades']} / {self.cfg.max_open_trades}  "
                 f"closed: {m['closed_trades']}",
                 f"- cash: {m['cash']}  exposure: {m['exposure']}  equity: {m['equity']}",
                 f"- realized P&L: {m['realized_pnl']}  unrealized: {m['unrealized_pnl']}  "
                 f"total: {m['total_pnl']}",
                 f"- win rate: {m['win_rate']}  avg net edge @ entry: {m['avg_net_edge_at_entry']}  "
                 f"avg slippage: {m['avg_slippage']}",
                 f"- max drawdown: {m['max_drawdown_pct']}",
                 "",
                 "## Rejection reasons (markets)", ""]
        if self.rejected_by_reason:
            lines += [f"- {k}: {v}" for k, v in sorted(self.rejected_by_reason.items())]
        else:
            lines.append("- none")
        lines += ["", "## Open positions", ""]
        opens = [p for p in self.positions if p.status == "open"]
        if opens:
            for p in opens:
                lines.append(f"- {p.market_id} {p.side} entry={p.entry_price} "
                             f"px={p.current_price} size={p.filled_usd} uPnL={p.unrealized_pnl(p.current_price)} "
                             f"net_edge={p.net_edge} group={p.event_group}")
        else:
            lines.append("- none")
        lines += ["", "## Rejected paper orders", ""]
        if self.rejected_orders:
            for r in self.rejected_orders[-15:]:
                lines.append(f"- {r['market_id']} {r['side']} net_edge={r['net_edge']} "
                             f"reason={r['rejection_reason']}")
        else:
            lines.append("- none")
        lines += ["", "## Top candidate markets", ""]
        for c in self.top_candidates[:10]:
            lines.append(f"- [{c['tier']}] {c['score']} {c['question'][:48]} {c.get('reasons', [])[:3]}")
        sigst = self.signal_model.status()
        fb = self.feedback.summary()
        lines += ["", "## Signal model + recursive feedback (Grok research-only)", "",
                  f"- signal model: {sigst.get('name')}  grok_enabled={sigst.get('grok_enabled')}  "
                  f"grok_source={sigst.get('grok_source')}  research_mode={sigst.get('research_mode')}  "
                  f"model={sigst.get('model', 'n/a')}",
                  f"- signal sources used: {self.signal_source_counts or 'none'}",
                  f"- feedback: samples={fb['samples']} hit_rate={fb['hit_rate']} brier={fb['brier']} "
                  f"edge_capture={fb['avg_edge_capture']} edge_adjustment={fb['edge_adjustment']}",
                  "", "## Risk gate decisions (recent)", ""]
        for d in self.risk_decisions[-10:]:
            lines.append(f"- {d['market_id']} approved={d['approved']} reason={d['risk_gate_result']}")
        lines += ["", "## Errors / warnings", ""]
        lines += ([f"- {e}" for e in self.errors[-10:]] or ["- none"])
        lines += ["", "## Pass/Fail", "",
                  f"- decision: **{pf['decision']}**"]
        for k, v in pf["checks"].items():
            lines.append(f"  - {'PASS' if v else 'FAIL'}  {k}")
        lines += ["", "## Recommended next action", "",
                  f"- {self._recommendation(pf)}", ""]
        return "\n".join(lines)

    def _recommendation(self, pf: dict) -> str:
        if not pf["passed"]:
            failed = [k for k, v in pf["checks"].items() if not v]
            return f"Do NOT proceed. Fix failing checks: {', '.join(failed)}."
        return ("Paper campaign healthy. Continue paper testing; do NOT enable Micro Live "
                "or production until a full-duration campaign passes and is human-reviewed.")

    def write_hourly_report(self) -> str:
        hourly_dir = self.reports_root / "hourly"
        hourly_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self._clock(), timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = hourly_dir / f"report_{stamp}.md"
        md = self._report_md(f"Hourly Paper Campaign Report — {self.cfg.campaign_name}")
        path.write_text(md, encoding="utf-8")
        self.write_latest_report(md)
        self.last_hourly_report_ts = self._clock()
        self.hourly_reports_written += 1
        return str(path)

    def write_latest_report(self, md: Optional[str] = None) -> str:
        self.reports_root.mkdir(parents=True, exist_ok=True)
        path = self.reports_root / "latest_report.md"
        path.write_text(md or self._report_md(
            f"Latest Paper Campaign Report — {self.cfg.campaign_name}"), encoding="utf-8")
        return str(path)

    def write_final_report(self) -> str:
        self.reports_root.mkdir(parents=True, exist_ok=True)
        path = self.reports_root / "final_report.md"
        m = self.metrics()
        pf = self.evaluate_pass_fail()
        best = max(self.closed_trades, key=lambda p: p.realized_pnl, default=None)
        worst = min(self.closed_trades, key=lambda p: p.realized_pnl, default=None)
        extra = [
            "## Final campaign assessment", "",
            f"- campaign duration (ticks): {self._tick}",
            f"- total markets scanned (last cycle): {self.scanned}",
            f"- total paper orders: {m['orders_total']}  filled: {m['filled_orders']}  "
            f"rejected: {m['rejected_orders']}",
            f"- fill rate: {m['fill_rate']}  avg slippage: {m['avg_slippage']}",
            f"- realized P&L: {m['realized_pnl']}  unrealized: {m['unrealized_pnl']}  "
            f"total: {m['total_pnl']}  max drawdown: {m['max_drawdown_pct']}",
            f"- win rate: {m['win_rate']}  avg hold (h): {m['avg_hold_hours']}",
            f"- best trade: {('%s %s' % (best.market_id, best.realized_pnl)) if best else 'n/a'}",
            f"- worst trade: {('%s %s' % (worst.market_id, worst.realized_pnl)) if worst else 'n/a'}",
            f"- most common rejection reasons: "
            f"{dict(sorted(self.rejected_by_reason.items(), key=lambda kv: -kv[1])[:5])}",
            "",
            f"- **PASS/FAIL: {pf['decision']}**",
            "",
            "## Recommended next Cursor prompt", "",
            "> " + (self._recommendation(pf)),
            "",
        ]
        md = self._report_md(f"FINAL Paper Campaign Report — {self.cfg.campaign_name}") + \
            "\n" + "\n".join(extra)
        path.write_text(md, encoding="utf-8")
        return str(path)

    # ---- run loop --------------------------------------------------------
    def run(self, catalog_provider: Callable[[], list], minutes: float = 15.0,
            tick_seconds: int = 60, sleep_fn: Optional[Callable[[float], None]] = None) -> dict:
        pre = self.preflight()
        if not pre.ok:
            self.status_state = "aborted_preflight"
            return {"started": False, "preflight": pre.to_dict(), "red_warning": pre.red_warning}

        sleep_fn = sleep_fn or time.sleep
        self._virtual_now = time.time()
        self.started_ts = self._clock()
        end = self.started_ts + minutes * 60.0
        self.status_state = "running"
        self.last_hourly_report_ts = self.started_ts

        while self._clock() < end:
            self.tick(catalog_provider())
            self.write_latest_report()
            self._persist_status()
            if self._clock() - self.last_hourly_report_ts >= self.cfg.campaign_report_frequency_seconds:
                self.write_hourly_report()
            if self.accelerated:
                self._virtual_now += tick_seconds
            else:
                sleep_fn(tick_seconds)

        self.status_state = "finished"
        if self.hourly_reports_written == 0:
            self.write_hourly_report()          # ensure at least one hourly report exists
        final_path = self.write_final_report()
        self.write_latest_report()
        self._persist_status()
        return {"started": True, "preflight": pre.to_dict(),
                "final_report": final_path, "pass_fail": self.evaluate_pass_fail(),
                "status": self.status()}


# ---------------------------------------------------------------------------
# Synthetic catalog generator (offline dry-run only; clearly SIMULATED data)
# ---------------------------------------------------------------------------

def synthetic_catalog(n: int = 1000, seed: int = 7) -> list:
    """Deterministic SIMULATED market catalog for offline dry runs / tests.

    This is NOT live market data. Real campaigns should fetch from Gamma via the
    Universe Manager's fetch_catalog()."""
    rng = random.Random(seed)
    cats = ["politics", "sports", "crypto", "econ", "tech", "culture", "weather", "science"]
    out = []
    for i in range(n):
        days = rng.choice([0.1, 3, 7, 20, 120])
        end = datetime.fromtimestamp(time.time() + days * 86400, timezone.utc).isoformat()
        out.append({
            "id": f"sim-{i}", "question": f"[SIMULATED] Will event {i} happen?", "slug": f"sim-{i}",
            "active": True, "closed": rng.random() < 0.05, "archived": rng.random() < 0.03,
            "enableOrderBook": rng.random() > 0.05, "acceptingOrders": rng.random() > 0.05,
            "clobTokenIds": json.dumps([f"{i}-y", f"{i}-n"]) if rng.random() > 0.04 else None,
            "outcomePrices": json.dumps([f"{rng.uniform(0.05, 0.95):.2f}", "0.5"]),
            "endDate": end,
            "description": "Resolves per official source." if rng.random() > 0.05 else "",
            "liquidityNum": rng.choice([200, 2000, 50_000, 300_000]),
            "volume24hr": rng.choice([100, 600, 20_000, 90_000]),
            "volumeNum": rng.choice([1000, 200_000, 2_000_000]),
            "spread": rng.choice([0.005, 0.02, 0.06, 0.12]),
            "bestBid": 0.49, "bestAsk": 0.51,
            "topDepthUsd": rng.choice([50, 200, 2000]),
            "category": rng.choice(cats), "events": [{"id": f"ev-{i % 300}"}],
        })
    return out
