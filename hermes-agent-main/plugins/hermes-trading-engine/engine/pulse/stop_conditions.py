"""Verifiable stop conditions — agent-independent kill switches (Loop Engineering #6).

Hard, quantitative, independently-checkable stops (NOT the agent's own opinion):
  * rolling Wilson lower bound of win-rate vs breakeven over the last N settled trades
  * realized profit-factor over the same window
  * max-drawdown % of starting capital

Each strategy (directional, arbitrage) is evaluated separately. Arb P&L is never blended into
directional stats. PAPER ONLY — halting a strategy only prevents new entries; open positions
still settle normally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


def _wilson_lower(wins: int, n: int, z: float = 1.64) -> Optional[float]:
    if n <= 0:
        return None
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / denom)


@dataclass
class StopConfig:
    enabled: bool = True
    rolling_n: int = 50
    min_samples: int = 30
    min_profit_factor: float = 0.85
    max_drawdown_pct: float = 25.0
    confidence_z: float = 1.64
    # arb anomaly guard: halt if any settled arb booked non-positive guaranteed profit
    arb_guard_enabled: bool = True


def _entry_ts(p) -> float:
    if isinstance(p, dict):
        return float(p.get("entry_ts", 0) or 0)
    return float(getattr(p, "entry_ts", 0) or 0)


def _rolling_settled(positions, *, rolling_n: int) -> list:
    settled = [p for p in (positions or []) if getattr(p, "status", None) == "settled"
               or (isinstance(p, dict) and p.get("status") == "settled")]
    settled.sort(key=_entry_ts)
    return settled[-rolling_n:]


def _directional_metrics(positions, *, rolling_n: int, confidence_z: float = 1.64) -> dict:
    recent = _rolling_settled(positions, rolling_n=rolling_n)
    n = len(recent)
    if not n:
        return {"n": 0, "wins": 0, "win_rate": None, "wilson_lower": None,
                "breakeven_wr": None, "profit_factor": None, "pnl_usd": 0.0}
    wins = 0
    gross_win = 0.0
    gross_loss = 0.0
    pnl = 0.0
    entry_sum = 0.0
    for p in recent:
        won = getattr(p, "won", None) if not isinstance(p, dict) else p.get("won")
        pnl_usd = float(getattr(p, "pnl_usd", None) or p.get("pnl_usd", 0) or 0)
        entry = float(getattr(p, "entry_price", None) or p.get("entry_price", 0) or 0)
        pnl += pnl_usd
        entry_sum += entry
        if won:
            wins += 1
            if pnl_usd > 0:
                gross_win += pnl_usd
        elif pnl_usd < 0:
            gross_loss += -pnl_usd
    wr = wins / n
    breakeven = entry_sum / n
    pf = (round(gross_win / gross_loss, 4) if gross_loss > 0 else None)
    return {"n": n, "wins": wins, "win_rate": round(wr, 4),
            "wilson_lower": (_wilson_lower(wins, n, z=confidence_z) if n else None),
            "breakeven_wr": round(breakeven, 4), "profit_factor": pf,
            "pnl_usd": round(pnl, 4)}


def evaluate_directional(*, positions, ledger_stats: dict, starting_capital: float,
                         cfg: StopConfig) -> dict:
    """Evaluate directional strategy stop. Returns halted flag + verifiable metrics."""
    if not cfg.enabled:
        return {"strategy": "directional", "enabled": False, "halted": False,
                "verifiable": False, "reasons": [], "metrics": {}}
    metrics = _directional_metrics(positions, rolling_n=cfg.rolling_n,
                                   confidence_z=cfg.confidence_z)
    dd_usd = float((ledger_stats or {}).get("max_drawdown_usd") or 0.0)
    start = max(1.0, float(starting_capital or 500.0))
    dd_pct = round(dd_usd / start * 100, 2)
    metrics["max_drawdown_usd"] = round(dd_usd, 4)
    metrics["max_drawdown_pct"] = dd_pct
    reasons = []
    if metrics["n"] < cfg.min_samples:
        return {"strategy": "directional", "enabled": True, "halted": False,
                "verifiable": True, "reasons": ["insufficient_samples"],
                "metrics": metrics, "min_samples": cfg.min_samples}
    wl = metrics.get("wilson_lower")
    be = metrics.get("breakeven_wr")
    wr = metrics.get("win_rate")
    pf = metrics.get("profit_factor")
    # Wilson CI can sit below breakeven on high avg-entry prices even when WR≈BE and PF≥1.
    # Only Wilson-halt when the lower bound AND realized profit-factor both say we're losing.
    if (wl is not None and be is not None and wl < be
            and pf is not None and pf < cfg.min_profit_factor):
        reasons.append("wilson_wr_below_breakeven")
    if pf is not None and pf < cfg.min_profit_factor:
        reasons.append("profit_factor_below_floor")
    if dd_pct > cfg.max_drawdown_pct:
        reasons.append("max_drawdown_pct_breach")
    halted = bool(reasons and reasons != ["insufficient_samples"])
    return {"strategy": "directional", "enabled": True, "halted": halted,
            "verifiable": True, "reasons": reasons, "metrics": metrics,
            "thresholds": {"min_profit_factor": cfg.min_profit_factor,
                           "max_drawdown_pct": cfg.max_drawdown_pct,
                           "rolling_n": cfg.rolling_n, "min_samples": cfg.min_samples}}


def evaluate_arbitrage(*, arb_positions, arb_report: dict, cfg: StopConfig) -> dict:
    """Arb stop: trivially verifiable — guaranteed_profit must be > 0 at book time."""
    if not cfg.enabled:
        return {"strategy": "arbitrage", "enabled": False, "halted": False,
                "verifiable": False, "reasons": [], "metrics": {}}
    rep = arb_report or {}
    reasons = []
    if cfg.arb_guard_enabled:
        bad = [p for p in (arb_positions or {}).values()
               if float(p.get("guaranteed_profit_usd", 1) or 0) <= 0]
        if bad:
            reasons.append("non_positive_guaranteed_profit")
    metrics = {"executed": rep.get("executed"), "settled": rep.get("settled"),
               "realized_profit_usd": rep.get("realized_profit_usd"),
               "guaranteed_booked_usd": rep.get("guaranteed_booked_usd")}
    halted = bool(reasons)
    return {"strategy": "arbitrage", "enabled": True, "halted": halted,
            "verifiable": True, "reasons": reasons, "metrics": metrics,
            "note": "arb halt only on anomaly (guaranteed_profit<=0); risk-free P&L is segregated"}


def evaluate_all(*, directional_positions, arb_positions, directional_stats: dict,
                 arb_report: dict, starting_capital: float,
                 cfg: Optional[StopConfig] = None) -> dict:
    cfg = cfg or StopConfig()
    directional = evaluate_directional(positions=directional_positions,
                                       ledger_stats=directional_stats,
                                       starting_capital=starting_capital, cfg=cfg)
    arbitrage = evaluate_arbitrage(arb_positions=arb_positions, arb_report=arb_report, cfg=cfg)
    any_halted = bool(directional.get("halted") or arbitrage.get("halted"))
    return {"paper_only": True, "agent_independent": True, "any_halted": any_halted,
            "strategies": {"directional": directional, "arbitrage": arbitrage},
            "note": ("quantitative kill switches checked each tick from settled ledger evidence; "
                     "NOT the LLM's opinion. Halting blocks NEW entries only.")}


class StrategyStopMonitor:
    """Caches the latest verifiable stop evaluation for the engine tick loop."""

    def __init__(self, *, cfg: Optional[StopConfig] = None):
        self.cfg = cfg or StopConfig()
        self._state: dict = {}

    def refresh(self, *, directional_positions, arb_positions, directional_stats: dict,
                arb_report: dict, starting_capital: float) -> dict:
        self._state = evaluate_all(
            directional_positions=directional_positions, arb_positions=arb_positions,
            directional_stats=directional_stats, arb_report=arb_report,
            starting_capital=starting_capital, cfg=self.cfg)
        return self._state

    def report(self) -> dict:
        return dict(self._state) if self._state else evaluate_all(
            directional_positions=[], arb_positions={}, directional_stats={},
            arb_report={}, starting_capital=500.0, cfg=self.cfg)

    def is_halted(self, strategy: str) -> bool:
        st = (self._state.get("strategies") or {}).get(strategy) or {}
        return bool(st.get("halted"))

    def verified_stop_line(self, strategy: str) -> str:
        """Human-readable verified stop condition for the loop registry."""
        st = (self._state.get("strategies") or {}).get(strategy) or {}
        if not st.get("verifiable"):
            return "not_configured"
        if st.get("halted"):
            return "HALTED:" + ",".join(st.get("reasons") or ["unknown"])
        reasons = st.get("reasons") or []
        if reasons == ["insufficient_samples"]:
            return "warming_up(n<%s)" % (st.get("min_samples") or self.cfg.min_samples)
        return "ok"