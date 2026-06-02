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
