"""Scan + learning metrics for the Polymarket paper training engine.

Pure dataclasses + small bucketing helpers. No network, no side effects.

Quant scope — *Live Trading & Monitoring* + *Backtesting & Simulation*: scan-loop
health metrics plus the deterministic bucketing helpers reused for calibration,
edge-bucket P&L, and Bregman-arbitrage reporting. (Bregman certified-opportunity
aggregation lives in :mod:`engine.replay.metrics`.)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScanMetrics:
    """Rolling scan-loop metrics (separate from the trade loop)."""

    scanned: int = 0
    kept: int = 0
    shortlisted: int = 0
    candidates: int = 0
    subscribed_assets: int = 0
    scan_latency_ms: float = 0.0
    candidates_per_second: float = 0.0
    markets_per_second: float = 0.0
    last_scan_ts: float = 0.0
    scans: int = 0
    # CLOB subscription health
    stale_books: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    avg_bbo_age_ms: float = 0.0
    avg_spread: float = 0.0
    avg_depth: float = 0.0
    subscription_refresh_ms: float = 0.0
    subscription_churn: int = 0
    # feature / grouping quality (Data Preprocessing & Feature Engineering)
    feature_coverage: float = 0.0
    null_rate: float = 0.0
    stale_rate: float = 0.0
    group_coverage: float = 0.0
    groups_detected: int = 0
    norm_cache_hits: int = 0
    # paper decision funnel + feedback yield
    candidates_evaluated: int = 0
    candidates_traded: int = 0
    paper_feedback_samples: int = 0
    paper_feedback_yield: float = 0.0

    def record_scan(self, *, scanned: int, kept: int, shortlisted: int,
                    candidates: int, latency_ms: float, ts: float) -> None:
        self.scanned = scanned
        self.kept = kept
        self.shortlisted = shortlisted
        self.candidates = candidates
        self.scan_latency_ms = round(latency_ms, 2)
        secs = (latency_ms / 1000.0) if latency_ms > 0 else 0.0
        self.candidates_per_second = round((candidates / secs) if secs else 0.0, 2)
        self.markets_per_second = round((scanned / secs) if secs else 0.0, 2)
        self.last_scan_ts = ts
        self.scans += 1

    def record_feature_quality(self, *, feature_coverage: float, null_rate: float,
                               stale_rate: float, group_coverage: float,
                               groups_detected: int) -> None:
        """Record feature null-rate / coverage + grouping coverage for a scan."""
        self.feature_coverage = round(float(feature_coverage), 4)
        self.null_rate = round(float(null_rate), 4)
        self.stale_rate = round(float(stale_rate), 4)
        self.group_coverage = round(float(group_coverage), 4)
        self.groups_detected = int(groups_detected)

    def record_decisions(self, *, evaluated: int, traded: int,
                         feedback_samples: int) -> None:
        """Accumulate the paper decision funnel + feedback-loop sample yield.

        ``feedback_samples`` is the number of learner-recorded decisions this
        tick (every evaluation feeds the online learner). ``paper_feedback_yield``
        is the cumulative trade-conversion rate (trades / evaluations) — a higher
        value means more evaluations turned into paper trades + feedback.
        """
        self.candidates_evaluated += int(evaluated)
        self.candidates_traded += int(traded)
        self.paper_feedback_samples += int(feedback_samples)
        ev = self.candidates_evaluated
        self.paper_feedback_yield = round(
            (self.candidates_traded / ev) if ev else 0.0, 4)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "kept": self.kept,
            "shortlisted": self.shortlisted,
            "candidates": self.candidates,
            "subscribed_assets": self.subscribed_assets,
            "scan_latency_ms": self.scan_latency_ms,
            "candidates_per_second": self.candidates_per_second,
            "markets_per_second": self.markets_per_second,
            "last_scan_ts": self.last_scan_ts,
            "scans": self.scans,
            "stale_books": self.stale_books,
            "reconnects": self.reconnects,
            "parse_errors": self.parse_errors,
            "avg_bbo_age_ms": round(self.avg_bbo_age_ms, 2),
            "avg_spread": round(self.avg_spread, 4),
            "avg_depth": round(self.avg_depth, 2),
            "subscription_refresh_ms": round(self.subscription_refresh_ms, 2),
            "subscription_churn": self.subscription_churn,
            "feature_coverage": round(self.feature_coverage, 4),
            "null_rate": round(self.null_rate, 4),
            "stale_rate": round(self.stale_rate, 4),
            "group_coverage": round(self.group_coverage, 4),
            "groups_detected": self.groups_detected,
            "norm_cache_hits": self.norm_cache_hits,
            "candidates_evaluated": self.candidates_evaluated,
            "candidates_traded": self.candidates_traded,
            "paper_feedback_samples": self.paper_feedback_samples,
            "paper_feedback_yield": round(self.paper_feedback_yield, 4),
        }


@dataclass
class LabelQualityMetrics:
    """Settlement-label quality for training reports + live monitoring.

    Tracks how many closed positions produced a clean (trainable) label vs. a
    dirty one that was *suppressed* from learning, the ambiguous-label rate,
    label coverage (fraction with any terminal settlement), and settlement
    delay. Pure counters — no side effects. (Quant scope: Strategy Optimization
    & Robustness Testing + Live Trading & Monitoring.)
    """

    total: int = 0
    trainable: int = 0
    suppressed: int = 0
    by_state: dict = field(default_factory=dict)
    confidence_sum: float = 0.0
    delay_samples: int = 0
    delay_sum_ms: float = 0.0

    def record(self, *, state: str, trainable: bool, confidence: float = 1.0,
               delay_ms=None) -> None:
        self.total += 1
        self.by_state[state] = self.by_state.get(state, 0) + 1
        self.confidence_sum += float(confidence or 0.0)
        if trainable:
            self.trainable += 1
        else:
            self.suppressed += 1
        if delay_ms is not None:
            try:
                self.delay_sum_ms += float(delay_ms)
                self.delay_samples += 1
            except (TypeError, ValueError):
                pass

    @property
    def ambiguous_rate(self) -> float:
        return round(self.by_state.get("ambiguous", 0) / self.total, 6) if self.total else 0.0

    @property
    def label_coverage(self) -> float:
        """Fraction of outcomes that reached a terminal settlement (not unresolved)."""
        if not self.total:
            return 0.0
        return round((self.total - self.by_state.get("unresolved", 0)) / self.total, 6)

    @property
    def suppression_rate(self) -> float:
        return round(self.suppressed / self.total, 6) if self.total else 0.0

    @property
    def avg_delay_ms(self) -> float:
        return round(self.delay_sum_ms / self.delay_samples, 2) if self.delay_samples else 0.0

    @property
    def avg_confidence(self) -> float:
        return round(self.confidence_sum / self.total, 6) if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total, "trainable": self.trainable,
            "suppressed": self.suppressed, "by_state": dict(self.by_state),
            "ambiguous_rate": self.ambiguous_rate, "label_coverage": self.label_coverage,
            "suppression_rate": self.suppression_rate, "avg_delay_ms": self.avg_delay_ms,
            "avg_confidence": self.avg_confidence,
        }


@dataclass
class ActiveLearningMetrics:
    """Cumulative active-learning selection diagnostics for training reports +
    live monitoring. The headline metric is ``feedback_samples_per_risk_unit`` —
    useful feedback samples generated per paper dollar put at exploration risk —
    which aggressive mode is designed to raise. Pure counters; no side effects.
    """

    ticks: int = 0
    candidates_skipped: int = 0
    selected_for_edge: int = 0
    selected_for_feedback: int = 0
    selected_for_bregman: int = 0
    rejected_by_hard_gate: int = 0
    diversity_skipped: int = 0
    budget_skipped: int = 0
    exploration_budget_used: float = 0.0
    feedback_value_sum: float = 0.0

    def record(self, diagnostics: dict) -> None:
        self.ticks += 1
        for k in ("candidates_skipped", "selected_for_edge", "selected_for_feedback",
                  "selected_for_bregman", "rejected_by_hard_gate",
                  "diversity_skipped", "budget_skipped"):
            setattr(self, k, getattr(self, k) + int(diagnostics.get(k, 0) or 0))
        self.exploration_budget_used = round(
            self.exploration_budget_used + float(diagnostics.get("exploration_budget_used", 0.0) or 0.0), 6)
        self.feedback_value_sum = round(
            self.feedback_value_sum + float(diagnostics.get("feedback_value_sum", 0.0) or 0.0), 6)

    @property
    def feedback_samples_per_risk_unit(self) -> float:
        return round(self.selected_for_feedback / self.exploration_budget_used, 6) \
            if self.exploration_budget_used > 0 else 0.0

    @property
    def feedback_value_per_risk_unit(self) -> float:
        return round(self.feedback_value_sum / self.exploration_budget_used, 6) \
            if self.exploration_budget_used > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "ticks": self.ticks,
            "candidates_skipped": self.candidates_skipped,
            "selected_for_edge": self.selected_for_edge,
            "selected_for_feedback": self.selected_for_feedback,
            "selected_for_bregman": self.selected_for_bregman,
            "rejected_by_hard_gate": self.rejected_by_hard_gate,
            "diversity_skipped": self.diversity_skipped,
            "budget_skipped": self.budget_skipped,
            "exploration_budget_used": round(self.exploration_budget_used, 6),
            "feedback_value_sum": round(self.feedback_value_sum, 6),
            "feedback_samples_per_risk_unit": self.feedback_samples_per_risk_unit,
            "feedback_value_per_risk_unit": self.feedback_value_per_risk_unit,
        }


def loss_streak(realized_pnls: list) -> int:
    """Trailing consecutive losing trades (Live Monitoring kill-switch input).

    Counts losses (realized_pnl < 0) from the end of the chronological sequence;
    a flat (0.0) trade ends the streak."""
    n = 0
    for pnl in reversed(list(realized_pnls or [])):
        try:
            v = float(pnl)
        except (TypeError, ValueError):
            break
        if v < 0:
            n += 1
        else:
            break
    return n


def metric_trend(history: list, key: str) -> float:
    """Trend (last - first) of ``key`` across an ordered metric ``history``.

    Negative is improving for loss-like metrics (Brier/ECE); 0 with < 2 points.
    Pure + side-effect-free (Statistical Modeling)."""
    vals = [float(h.get(key)) for h in (history or [])
            if isinstance(h, dict) and h.get(key) is not None]
    if len(vals) < 2:
        return 0.0
    return round(vals[-1] - vals[0], 6)


def after_cost_profitability_by(trades: list, *, key: str) -> dict:
    """Group closed trades by ``key`` (market / event / category / liquidity /
    spread / strategy) and report per-group after-cost net expectancy + win rate
    + trade count (Profitability Governor reporting). Pure + side-effect-free.

    Each trade dict carries ``net_edge`` (or ``realized_pnl``) + the grouping
    field; ``liquidity``/``spread`` keys are bucketed via the helpers above."""
    out: dict = {}
    for t in trades or []:
        if key == "liquidity":
            g = liquidity_bucket(t.get("liquidity", 0.0))
        elif key == "spread":
            g = spread_bucket(t.get("spread", 0.0))
        else:
            g = str(t.get(key, "unknown"))
        d = out.setdefault(g, {"n": 0, "net_sum": 0.0, "wins": 0})
        d["n"] += 1
        net = float(t.get("net_edge", t.get("realized_pnl", 0.0)) or 0.0)
        d["net_sum"] += net
        d["wins"] += int(net > 0.0)
    for g, d in out.items():
        n = max(1, d["n"])
        d["net_expectancy"] = round(d["net_sum"] / n, 6)
        d["win_rate"] = round(d["wins"] / n, 6)
        d.pop("net_sum", None)
    return out


def profitability_summary(closed_trades: list, *, memory=None) -> dict:
    """After-cost profitability headline for the training report.

    Reports net expectancy, profit factor, edge-survival (net/gross), and the
    rejected-bad-market counts from the graylist memory. ``closed_trades`` carry
    ``net_edge``/``realized_pnl`` and optionally ``gross_edge``. Pure."""
    rows = closed_trades or []
    nets = [float(t.get("net_edge", t.get("realized_pnl", 0.0)) or 0.0) for t in rows]
    grosses = [float(t.get("gross_edge", 0.0) or 0.0) for t in rows]
    wins = sum(n for n in nets if n > 0.0)
    losses = -sum(n for n in nets if n < 0.0)
    net_sum = sum(nets)
    gross_sum = sum(grosses)
    rep = {
        "trades": len(rows),
        "net_expectancy": round(net_sum / len(rows), 6) if rows else 0.0,
        "net_total": round(net_sum, 6),
        "profit_factor": round(wins / losses, 6) if losses > 1e-12 else (
            float("inf") if wins > 0 else 0.0),
        "win_rate": round(sum(1 for n in nets if n > 0) / len(rows), 6) if rows else 0.0,
        "edge_survival": round(net_sum / gross_sum, 6) if gross_sum > 1e-12 else 0.0,
    }
    if memory is not None:
        m = memory.to_report()
        rep["rejected_graylisted"] = m.get("graylist_count", 0)
        rejected_blacklisted = m.get("blacklist_count", 0)
        rep["rejected_blacklisted"] = rejected_blacklisted
        rep["rejected_bad_markets"] = rep["rejected_graylisted"] + rejected_blacklisted
    return rep


def variant_attribution_table(experiment: dict) -> list:
    """Flatten an experiment report's per-variant metrics into report rows.

    Each row is one strategy variant with its trade/feedback counts and the core
    risk-adjusted + calibration metrics, marked champion/challenger. Pure +
    side-effect-free (Monitoring + Strategy Optimization)."""
    variants = (experiment or {}).get("variants", {}) or {}
    cc = (experiment or {}).get("champion_challenger", {}) or {}
    champion = cc.get("champion")
    challengers = set(cc.get("challengers", []) or [])
    rows = []
    for name, m in variants.items():
        role = ("champion" if name == champion
                else "challenger" if name in challengers else "inactive")
        rows.append({"strategy_variant": name, "role": role,
                     "trade_count": m.get("trade_count", 0),
                     "feedback_count": m.get("feedback_count", 0),
                     "sharpe": m.get("sharpe"), "sortino": m.get("sortino"),
                     "calmar": m.get("calmar"), "max_drawdown": m.get("max_drawdown"),
                     "brier": m.get("brier"), "log_loss": m.get("log_loss"),
                     "ece": m.get("ece"), "realized_edge": m.get("realized_edge"),
                     "fill_quality": m.get("fill_quality")})
    rows.sort(key=lambda r: (r["role"] != "champion", -(r["realized_edge"] or 0.0)))
    return rows


def label_delay_bucket(delay_ms: float) -> str:
    """Settlement-delay (ms) -> labelled bucket: <1m, <1h, <1d, <1w, >=1w."""
    return bucket_label(float(delay_ms or 0.0),
                        [60_000, 3_600_000, 86_400_000, 604_800_000])


def bucket_label(value: float, edges: list) -> str:
    """Return a human-readable bucket label for `value` given ascending edges."""
    prev = None
    for e in edges:
        if value < e:
            lo = "-inf" if prev is None else _fmt(prev)
            return f"[{lo},{_fmt(e)})"
        prev = e
    return f"[{_fmt(edges[-1]) if edges else '0'},inf)"


def _fmt(v) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def liquidity_bucket(liq: float) -> str:
    return bucket_label(float(liq or 0.0), [1000, 5000, 25000, 100000])


def spread_bucket(spread: float) -> str:
    return bucket_label(float(spread or 0.0), [0.01, 0.02, 0.04, 0.08])


def prob_bucket(p: float) -> str:
    return bucket_label(float(p or 0.0), [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])


def edge_bucket(edge: float) -> str:
    return bucket_label(float(edge or 0.0), [0.0, 0.02, 0.04, 0.08, 0.15])


def ambiguity_bucket(a: float) -> str:
    return bucket_label(float(a or 0.0), [0.1, 0.2, 0.3, 0.5])


def evidence_bucket(e: float) -> str:
    return bucket_label(float(e or 0.0), [0.2, 0.4, 0.6, 0.8])


def depth_bucket(depth: float) -> str:
    return bucket_label(float(depth or 0.0), [100, 250, 1000, 5000])


def microprice_bucket(mp: float) -> str:
    return bucket_label(float(mp or 0.0), [0.1, 0.3, 0.5, 0.7, 0.9])


def imbalance_bucket(imb: float) -> str:
    """Order-book imbalance in [-1, 1] -> labelled bucket."""
    return bucket_label(float(imb or 0.0), [-0.5, -0.2, 0.2, 0.5])


def entropy_bucket(h: float) -> str:
    return bucket_label(float(h or 0.0), [0.2, 0.5, 0.8, 0.95])


def ttr_bucket(days: float) -> str:
    """Time-to-resolution (days) -> labelled bucket."""
    return bucket_label(float(days or 0.0), [1, 7, 30, 90])
