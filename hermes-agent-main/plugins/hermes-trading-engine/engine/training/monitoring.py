"""Final monitoring + kill-switch metrics for aggressive paper training.

Exposes whether aggressive paper mode is truly learning FASTER without unsafe
behaviour or corrupted data, and trips a kill-switch (auto-downgrade to
conservative paper mode) when it is not.

Quant scope (all PAPER-ONLY, read-only analytics + a paper-mode behaviour gate):

* **Live Monitoring** — :func:`build_dashboard` assembles the learning-velocity
  dashboard (paper trades/hour, useful feedback/hour, labels resolved/day,
  calibration improvement, Brier/ECE trend, Bregman + Chainlink performance,
  exploration budget, drawdown, loss streak, stale-data rejections).
* **Bregman arbitrage monitoring** — :func:`bregman_monitoring` surfaces
  opportunities, certified profit, and the false-positive rate.
* **Risk Management / Compliance** — :func:`evaluate_kill_switch` flags
  calibration deterioration, excessive drawdown, bad labels, stale data, high
  partial-fill rate, Bregman false positives, spread blowout, and feedback
  corruption; the trainer auto-downgrades aggressive -> conservative on a
  trigger. Nothing here touches live execution, the CLOB boundary, or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .metrics import loss_streak, metric_trend  # re-exported helpers

# Reasons that indicate stale / unusable market data (drives the stale-data
# kill-switch). Sourced from EdgeEngine + RiskGate no-trade reasons.
STALE_DATA_REASONS = frozenset({
    "stale_book", "no_fresh_book", "stale_data", "stale_research",
    "chainlink_stale_or_irrelevant"})

KILL_SWITCH_ALERTS = (
    "calibration_deterioration", "excessive_drawdown", "bad_labels", "stale_data",
    "high_partial_fill_rate", "bregman_false_positives", "spread_blowout",
    "feedback_corruption")


def per_hour(count: float, seconds: float) -> float:
    """Per-hour rate (0 when no elapsed time)."""
    return round(float(count) * 3600.0 / seconds, 4) if seconds and seconds > 0 else 0.0


def per_day(count: float, seconds: float) -> float:
    """Per-day rate (0 when no elapsed time)."""
    return round(float(count) * 86400.0 / seconds, 4) if seconds and seconds > 0 else 0.0


# --------------------------------------------------------------------------- #
# Bregman monitoring
# --------------------------------------------------------------------------- #
def bregman_monitoring(summary: Optional[dict]) -> dict:
    """Extract the Bregman monitoring headline from a trainer ``bregman_summary``.

    Returns cumulative ``opportunities`` / ``sets_opened`` / ``rejected`` plus the
    last certified-scan ``certified_profit`` and ``false_positive_rate``."""
    s = summary or {}
    last = s.get("last_scan_metrics", {}) or {}
    return {
        "opportunities": int(s.get("opportunity_count", 0) or 0),
        "sets_opened": int(s.get("sets_opened", 0) or 0),
        "rejected": int(s.get("rejected", 0) or 0),
        "certified_profit": round(float(last.get("certified_profit", 0.0) or 0.0), 6),
        "false_positive_rate": round(float(last.get("false_positive_rate", 0.0) or 0.0), 6),
        "scan_opportunity_count": int(last.get("opportunity_count", 0) or 0),
    }


# --------------------------------------------------------------------------- #
# Learning-velocity dashboard
# --------------------------------------------------------------------------- #
def build_dashboard(raw: dict, *, runtime_seconds: float,
                    history: Optional[list] = None) -> dict:
    """Assemble the aggressive-learning dashboard from a trainer-computed ``raw``
    block + the elapsed runtime + a metric ``history`` (for trend/improvement).

    ``raw`` carries the values that need the trainer's state (counts, drawdown,
    loss streak, Bregman/Chainlink performance, label quality, partial-fill rate,
    avg spread, rollbacks). This function only computes rates + trends and lays
    out the final dashboard — it is pure + side-effect-free."""
    rt = float(runtime_seconds or 0.0)
    hist = history or []
    breg = raw.get("bregman", {}) or {}
    first = hist[0] if hist else {}
    cur_cal = float(raw.get("calibration_error", 0.0) or 0.0)
    return {
        # learning velocity
        "paper_trades_per_hour": per_hour(raw.get("trades_opened", 0), rt),
        "useful_feedback_per_hour": per_hour(raw.get("useful_feedback", 0), rt),
        "labels_resolved_per_day": per_day(raw.get("labels_resolved", 0), rt),
        # calibration quality + trend
        "calibration_error": round(cur_cal, 6),
        "calibration_improvement": round(
            float(first.get("calibration_error", cur_cal)) - cur_cal, 6),
        "brier": round(float(raw.get("brier", 0.0) or 0.0), 6),
        "brier_trend": round(float(raw.get("brier", 0.0) or 0.0)
                             - float(first.get("brier", raw.get("brier", 0.0) or 0.0)), 6),
        "ece": round(float(raw.get("ece", 0.0) or 0.0), 6),
        "ece_trend": round(float(raw.get("ece", 0.0) or 0.0)
                           - float(first.get("ece", raw.get("ece", 0.0) or 0.0)), 6),
        # Bregman monitoring
        "bregman_opportunities": int(breg.get("opportunities", 0) or 0),
        "certified_bregman_profit": round(float(breg.get("certified_profit", 0.0) or 0.0), 6),
        "bregman_false_positive_rate": round(
            float(breg.get("false_positive_rate", 0.0) or 0.0), 6),
        # Chainlink-linked trade performance
        "chainlink_linked_performance": dict(raw.get("chainlink_linked_performance", {}) or {}),
        # risk / safety telemetry
        "exploration_budget_used": round(float(raw.get("exploration_budget_used", 0.0) or 0.0), 6),
        "drawdown": round(float(raw.get("drawdown", 0.0) or 0.0), 6),
        "loss_streak": int(raw.get("loss_streak", 0) or 0),
        "stale_data_rejections": int(raw.get("stale_data_rejections", 0) or 0),
        "stale_data_rejection_rate": round(
            float(raw.get("stale_data_rejection_rate", 0.0) or 0.0), 6),
        "partial_fill_rate": round(float(raw.get("partial_fill_rate", 0.0) or 0.0), 6),
        "avg_spread": round(float(raw.get("avg_spread", 0.0) or 0.0), 6),
        "label_suppression_rate": round(float(raw.get("label_suppression_rate", 0.0) or 0.0), 6),
        "ambiguous_rate": round(float(raw.get("ambiguous_rate", 0.0) or 0.0), 6),
        "learner_rollbacks": int(raw.get("learner_rollbacks", 0) or 0),
        "profile": raw.get("profile", "aggressive"),
        "samples": int(raw.get("useful_feedback", 0) or 0),
    }


# --------------------------------------------------------------------------- #
# Kill-switch
# --------------------------------------------------------------------------- #
@dataclass
class KillSwitchThresholds:
    max_calibration_error: float = 0.20
    max_brier_trend: float = 0.05          # Brier rising this much = deterioration
    max_drawdown_usd: float = 50.0
    max_loss_streak: int = 10
    max_label_suppression_rate: float = 0.5
    max_ambiguous_rate: float = 0.5
    max_stale_rejection_rate: float = 0.5
    max_partial_fill_rate: float = 0.5
    max_bregman_fp_rate: float = 0.10
    max_avg_spread: float = 0.15
    max_learner_rollbacks: int = 3
    min_samples: int = 10                  # statistical alerts need >= this many

    @classmethod
    def from_config(cls, cfg) -> "KillSwitchThresholds":
        g = lambda n, d: float(getattr(cfg, n, d))
        return cls(
            max_calibration_error=g("ks_max_calibration_error", 0.20),
            max_brier_trend=g("ks_max_brier_trend", 0.05),
            max_drawdown_usd=g("max_drawdown_usd", 50.0),
            max_loss_streak=int(g("ks_max_loss_streak", 10)),
            max_label_suppression_rate=g("ks_max_label_suppression_rate", 0.5),
            max_ambiguous_rate=g("ks_max_ambiguous_rate", 0.5),
            max_stale_rejection_rate=g("ks_max_stale_rejection_rate", 0.5),
            max_partial_fill_rate=g("ks_max_partial_fill_rate", 0.5),
            max_bregman_fp_rate=g("ks_max_bregman_fp_rate", 0.10),
            max_avg_spread=g("ks_max_avg_spread", 0.15),
            max_learner_rollbacks=int(g("ks_max_learner_rollbacks", 3)),
            min_samples=int(g("ks_min_samples", 10)))


# Market-DATA-quality alerts (external book conditions, not bot risk). In PAPER-only
# training these must NOT auto-downgrade the aggressive profile: the hard paper-realism
# gates already REJECT stale/wide books from fills (a high stale-REJECTION rate is the
# gate WORKING, not a runaway), and no real money is at risk. Auto-downgrading on them
# would silently disable active-learning/exploration and kill multi-day paper learning.
# Bot-RISK alerts (drawdown, loss streak, calibration, labels, partial-fill, Bregman FP,
# feedback corruption) still force the downgrade.
MARKET_QUALITY_ALERTS = frozenset({"stale_data", "spread_blowout"})


def evaluate_kill_switch(dashboard: dict, thresholds: Optional[KillSwitchThresholds] = None,
                        *, aggressive: bool = True, paper_only: bool = True) -> dict:
    """Evaluate kill-switch conditions against a dashboard.

    Returns ``{alerts, triggered, should_downgrade, severity}``. ``should_downgrade``
    is True only in ``aggressive`` mode (conservative mode has nothing to
    downgrade). Statistical alerts (calibration / labels / Bregman FP /
    partial-fill) only fire once ``min_samples`` feedback samples exist; the hard
    safety alerts (drawdown, loss streak, stale data, spread blowout, feedback
    corruption) always fire so a runaway is caught immediately.

    In ``paper_only`` mode, MARKET-DATA-QUALITY alerts (``stale_data`` / ``spread_blowout``)
    still surface in ``alerts``/``triggered``/``severity`` for visibility but do NOT by
    themselves force a downgrade — they reflect external market data the realism gates
    already reject, not bot risk, so they must not disable paper active-learning. Bot-RISK
    alerts always downgrade (in aggressive mode)."""
    thr = thresholds or KillSwitchThresholds()
    d = dashboard or {}
    samples = int(d.get("samples", d.get("useful_feedback", 0)) or 0)
    enough = samples >= thr.min_samples
    alerts: list = []

    def add(kind: str, detail: str) -> None:
        alerts.append({"type": kind, "detail": detail})

    # calibration deterioration (statistical)
    if enough and (float(d.get("calibration_error", 0.0)) > thr.max_calibration_error
                   or float(d.get("brier_trend", 0.0)) > thr.max_brier_trend):
        add("calibration_deterioration",
            f"calib_err={d.get('calibration_error')} brier_trend={d.get('brier_trend')}")
    # excessive drawdown / loss streak (hard safety)
    if (float(d.get("drawdown", 0.0)) <= -abs(thr.max_drawdown_usd)
            or int(d.get("loss_streak", 0)) >= thr.max_loss_streak):
        add("excessive_drawdown",
            f"drawdown={d.get('drawdown')} loss_streak={d.get('loss_streak')}")
    # bad labels (statistical)
    if enough and (float(d.get("label_suppression_rate", 0.0)) > thr.max_label_suppression_rate
                   or float(d.get("ambiguous_rate", 0.0)) > thr.max_ambiguous_rate):
        add("bad_labels",
            f"suppression={d.get('label_suppression_rate')} ambiguous={d.get('ambiguous_rate')}")
    # stale data (hard safety)
    if float(d.get("stale_data_rejection_rate", 0.0)) > thr.max_stale_rejection_rate:
        add("stale_data", f"stale_rejection_rate={d.get('stale_data_rejection_rate')}")
    # high partial-fill rate (statistical)
    if enough and float(d.get("partial_fill_rate", 0.0)) > thr.max_partial_fill_rate:
        add("high_partial_fill_rate", f"partial_fill_rate={d.get('partial_fill_rate')}")
    # Bregman false positives (statistical)
    if enough and float(d.get("bregman_false_positive_rate", 0.0)) > thr.max_bregman_fp_rate:
        add("bregman_false_positives", f"fp_rate={d.get('bregman_false_positive_rate')}")
    # spread blowout (hard safety)
    if float(d.get("avg_spread", 0.0)) > thr.max_avg_spread:
        add("spread_blowout", f"avg_spread={d.get('avg_spread')}")
    # feedback corruption (hard safety): the learner kept rolling back its state
    if int(d.get("learner_rollbacks", 0)) > thr.max_learner_rollbacks:
        add("feedback_corruption", f"learner_rollbacks={d.get('learner_rollbacks')}")

    triggered = [a["type"] for a in alerts]
    # market-quality-only alerts never force a paper downgrade (see MARKET_QUALITY_ALERTS).
    downgrade_triggers = ([t for t in triggered if t not in MARKET_QUALITY_ALERTS]
                          if paper_only else triggered)
    should_downgrade = bool(aggressive and downgrade_triggers)
    return {"alerts": alerts, "triggered": triggered,
            "should_downgrade": should_downgrade,
            "downgrade_triggers": downgrade_triggers,
            "severity": "CRITICAL" if alerts else "OK"}


def kill_switch_markdown(dashboard: dict, ks: dict) -> list:
    """Concise markdown lines for the kill-switch + learning dashboard."""
    d = dashboard or {}
    ks = ks or {}
    lines = [
        "## Aggressive learning monitor (PAPER ONLY)",
        f"- profile: **{d.get('profile')}** · kill-switch: **{ks.get('severity', 'OK')}**"
        + (f" → triggered: {', '.join(ks.get('triggered', []))}" if ks.get("triggered") else ""),
        f"- paper trades/hr: {d.get('paper_trades_per_hour')} · useful feedback/hr: "
        f"{d.get('useful_feedback_per_hour')} · labels/day: {d.get('labels_resolved_per_day')}",
        f"- calibration_improvement: {d.get('calibration_improvement')} · brier_trend: "
        f"{d.get('brier_trend')} · ece_trend: {d.get('ece_trend')}",
        f"- bregman: opps={d.get('bregman_opportunities')} certified_profit="
        f"{d.get('certified_bregman_profit')} fp_rate={d.get('bregman_false_positive_rate')}",
        f"- chainlink_linked: {d.get('chainlink_linked_performance')}",
        f"- exploration_budget_used: {d.get('exploration_budget_used')} · drawdown: "
        f"{d.get('drawdown')} · loss_streak: {d.get('loss_streak')} · stale_data_rejections: "
        f"{d.get('stale_data_rejections')}",
    ]
    return lines
