"""Grok advisory proof call (PAPER ONLY, research-only, rate-limited).

When Grok is enabled, an API key is present, and news packets exist, but no real
xAI call has happened recently, this schedules AT MOST one advisory-only proof call
per hour so the inspection report can show ``grok_calls_total > 0`` /
``grok_calls_with_news > 0`` instead of an ambiguous (and contradictory) zero-call
reason. It writes a Grok evidence record and increments the call counters.

Hard invariants (never violated): the proof call is ADVISORY ONLY — it never places,
sizes, or cancels a trade, never bypasses a quant gate, and is never proof of edge by
itself. If no call is made, a PRECISE non-contradictory zero-call reason is returned.

The xAI client + clock are injected so this is unit-testable with no network.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

# precise, non-contradictory zero-call reasons (never vague when online + news>0)
REASON_DISABLED = "proof_call_disabled_by_config"
REASON_NO_KEY = "no_api_key"
REASON_NOT_ONLINE = "research_mode_not_online"
REASON_NO_NEWS = "no_news_packet_available"
REASON_NO_MARKET = "no_market_link_available"
REASON_RATE_LIMIT = "rate_limit_budget_exhausted"
REASON_NOT_DUE = "not_due_yet"
REASON_PROVIDER_ERROR = "provider_error"
REASON_CACHE_ONLY = "cache_only_mode_enabled"


class GrokProofCaller:
    """Stateful, rate-limited advisory proof-caller. Hourly budget is enforced via an
    in-memory ring of recent call timestamps (paper-only; no persistence required)."""

    def __init__(self, *, enabled: bool, max_per_hour: int = 1, max_per_run: int = 1,
                 min_interval_seconds: int = 900, advisory_only: bool = True,
                 clock: Optional[Callable[[], float]] = None):
        self.enabled = bool(enabled)
        self.max_per_hour = int(max_per_hour)
        self.max_per_run = int(max_per_run)
        self.min_interval_seconds = int(min_interval_seconds)
        self.advisory_only = bool(advisory_only)
        self._clock = clock or time.time
        self._calls: list[float] = []          # timestamps of proof calls made
        self.calls_total = 0
        self.calls_with_news = 0
        self.evidence_records_written = 0
        self.last_reason: Optional[str] = None
        self.last_call_ts: Optional[float] = None
        # bounded-advisory-scheduler analytics (research only; never execution)
        self.market_groups_analyzed = 0
        self.bregman_near_misses_analyzed = 0
        self.news_linked_markets_analyzed = 0
        # clearly separate single PROOF calls (no target) from SCHEDULER advisory
        # calls (a chosen high-value target) so report metrics never contradict.
        self.proof_calls_total = 0
        self.scheduler_calls_total = 0
        # scheduler target accounting (reconciles "0 scheduled calls" honestly)
        self.scheduler_eligible_targets = 0
        self.scheduler_targets_selected = 0
        self.scheduler_targets_skipped = 0
        self.scheduler_skip_reasons: dict = {}
        self.scheduler_no_target_count = 0
        self.scheduler_rate_limited_count = 0
        self.incomplete_groups_analyzed = 0
        self.malformed_groups_analyzed = 0
        self.learning_features_written = 0

    @staticmethod
    def _classify_result(res) -> Optional[str]:
        """Return a precise zero-call reason if ``res`` is NOT a real live call, else
        None (a real advisory call happened). A successful estimate bundle has no
        failure ``status``; only an explicit cache/offline source is cache-only."""
        if res is None:
            return REASON_PROVIDER_ERROR
        # explicit cache/offline markers (dict or attr) => not a live call
        src = getattr(res, "source", None)
        if isinstance(res, dict):
            src = res.get("source", src)
        cached = getattr(res, "from_cache", None) or getattr(res, "cached", None)
        if isinstance(res, dict):
            cached = res.get("from_cache", res.get("cached", cached))
        if src in ("grok_cache", "offline_stub", "cache", "legacy_cached") or cached:
            return REASON_CACHE_ONLY
        status = getattr(res, "status", None)
        if isinstance(res, dict):
            status = res.get("status", status)
        if status is None:
            return None                       # estimate bundle (no status) => success
        s = str(status).upper()
        if s in ("SUCCEEDED", "OK", "SUCCESS"):
            return None
        if s == "BUDGET_BLOCKED":
            return REASON_RATE_LIMIT
        reason_field = getattr(res, "reason", None)
        if isinstance(res, dict):
            reason_field = res.get("reason", reason_field)
        if reason_field == "research_mode_not_online":
            return REASON_NOT_ONLINE
        if s in ("NO_EVIDENCE", "VALIDATION_FAILED"):
            # the network call DID hit xAI (advisory proven) but returned no usable
            # evidence — still a real call; count it (connectivity proven).
            return None
        return REASON_PROVIDER_ERROR

    def _recent_calls(self, now: float) -> int:
        cutoff = now - 3600.0
        self._calls = [t for t in self._calls if t >= cutoff]
        return len(self._calls)

    def maybe_call(self, *, client, online: bool, has_key: bool,
                   news_packet, market_ctx: Optional[dict],
                   evidence_sink: Optional[Callable[[dict], None]] = None,
                   now: Optional[float] = None, target_kind: Optional[str] = None,
                   advisory_features: Optional[dict] = None,
                   analyzed_increments: Optional[dict] = None,
                   eligible_targets: int = 0) -> dict:
        """Attempt at most one advisory call (bounded scheduler). Returns a result
        dict with ``called`` (bool), ``reason`` (precise zero-call reason when not
        called), ``grok_calls_total`` / ``grok_calls_with_news`` (cumulative), and
        ``advisory_only=True``. ``target_kind`` / ``advisory_features`` /
        ``analyzed_increments`` annotate the durable evidence + analyzed counters.
        Never raises; never executes, sizes, or bypasses a gate."""
        now = float(now if now is not None else self._clock())
        result = {"called": False, "reason": None, "advisory_only": True,
                  "grok_calls_total": self.calls_total,
                  "grok_calls_with_news": self.calls_with_news,
                  "evidence_records_written": self.evidence_records_written}

        if not self.enabled:
            reason = REASON_DISABLED
        elif not has_key:
            reason = REASON_NO_KEY
        elif not online:
            reason = REASON_NOT_ONLINE
        elif not news_packet:
            reason = REASON_NO_NEWS
        elif not market_ctx or not market_ctx.get("market_id"):
            reason = REASON_NO_MARKET
        elif self.max_per_run > 0 and self.calls_total >= self.max_per_run:
            reason = REASON_RATE_LIMIT      # per-RUN cap: bounds total API spend
        elif self.max_per_hour > 0 and self._recent_calls(now) >= self.max_per_hour:
            reason = REASON_RATE_LIMIT
        elif (self.last_call_ts is not None and self.min_interval_seconds > 0
              and (now - self.last_call_ts) < self.min_interval_seconds):
            reason = REASON_NOT_DUE         # min spacing between proof calls
        else:
            reason = None

        # scheduler target accounting (only when this is a SCHEDULER attempt).
        self.scheduler_eligible_targets = max(self.scheduler_eligible_targets,
                                              int(eligible_targets or 0))
        if target_kind and reason is not None:
            self.scheduler_targets_skipped += 1
            self.scheduler_skip_reasons[reason] = self.scheduler_skip_reasons.get(reason, 0) + 1
            if reason in (REASON_RATE_LIMIT, REASON_NOT_DUE):
                self.scheduler_rate_limited_count += 1
            if reason in (REASON_NO_MARKET, REASON_NO_NEWS):
                self.scheduler_no_target_count += 1

        if reason is not None:
            self.last_reason = reason
            result["reason"] = reason
            return result

        # --- make ONE advisory-only call (research only; never trades) ---
        # Force an ONLINE research mode so the proof call hits xAI even if the
        # client's default mode is offline_cache (the call result, not the client's
        # ambient mode, decides success).
        try:
            try:
                res = client.research(market_ctx, mode="online_paper", news_packet=news_packet)
            except TypeError:
                res = client.research(market_ctx, news_packet=news_packet)
        except Exception as exc:  # noqa: BLE001 — provider error is a precise reason
            self.last_reason = REASON_PROVIDER_ERROR
            result["reason"] = REASON_PROVIDER_ERROR
            result["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            return result

        # Classify by the result, not by an ambient client mode:
        #  * a successful estimate bundle has NO failure `status` -> a REAL call;
        #  * a ResearchFailure carries `.status` (BUDGET_BLOCKED / FAILED / ...);
        #  * an EXPLICIT cache/offline source marker -> cache-only (not a live call).
        reason = self._classify_result(res)
        if reason is not None:
            self.last_reason = reason
            result["reason"] = reason
            return result

        self._calls.append(now)
        self.last_call_ts = now
        self.calls_total += 1
        self.calls_with_news += 1            # proof call always attaches a news packet
        inc = analyzed_increments or {}
        self.market_groups_analyzed += int(inc.get("groups_analyzed", 0) or 0)
        self.bregman_near_misses_analyzed += int(inc.get("near_misses_analyzed", 0) or 0)
        self.news_linked_markets_analyzed += int(inc.get("news_linked_analyzed", 0) or 0)
        if target_kind:
            self.scheduler_calls_total += 1     # chose a high-value advisory target
            self.scheduler_targets_selected += 1
        else:
            self.proof_calls_total += 1         # bare liveness proof call
        self.incomplete_groups_analyzed += int(inc.get("incomplete_groups_analyzed", 0) or 0)
        self.malformed_groups_analyzed += int(inc.get("malformed_groups_analyzed", 0) or 0)
        if advisory_features:
            self.learning_features_written += 1
        evidence = {
            "kind": "grok_advisory_call", "advisory_only": True,
            "ts": round(now, 3), "market_id": market_ctx.get("market_id"),
            "group_ids": list(market_ctx.get("group_ids", []) or []),
            "target_kind": target_kind,
            "model": getattr(client, "model", None),
            "news_items": (len(news_packet) if hasattr(news_packet, "__len__") else 1),
            "news_included": bool(news_packet),
            "call_attempted": True, "call_succeeded": True,
            "result_status": str(getattr(res, "status", None) or "SUCCEEDED"),
            "advisory_features": dict(advisory_features or {}),
            "executed": False, "sized_trade": False, "trade_gate_bypassed": False,
            "no_execution_override": True, "is_edge_proof": False,
        }
        if evidence_sink is not None:
            try:
                evidence_sink(evidence)
            except Exception:  # noqa: BLE001
                pass
        self.evidence_records_written += 1
        result.update(called=True, reason=None, evidence=evidence,
                      grok_calls_total=self.calls_total,
                      grok_calls_with_news=self.calls_with_news,
                      evidence_records_written=self.evidence_records_written)
        return result

    def advisory_calls_per_hour(self, now: Optional[float] = None) -> int:
        """Number of advisory calls made in the trailing hour (read-only)."""
        return self._recent_calls(float(now if now is not None else self._clock()))

    def status(self) -> dict:
        return {
            "grok_proof_call_enabled": self.enabled,
            "grok_proof_call_advisory_only": self.advisory_only,
            "grok_proof_call_max_per_hour": self.max_per_hour,
            "grok_proof_call_max_per_run": self.max_per_run,
            "grok_advisory_max_calls_per_hour": self.max_per_hour,
            "grok_advisory_min_interval_seconds": self.min_interval_seconds,
            "grok_calls_total": self.calls_total,
            "grok_calls_with_news": self.calls_with_news,
            "grok_proof_calls_total": self.proof_calls_total,
            "grok_scheduler_calls_total": self.scheduler_calls_total,
            "grok_total_calls_reconciled": bool(
                self.proof_calls_total + self.scheduler_calls_total == self.calls_total),
            "grok_evidence_records_written": self.evidence_records_written,
            "grok_advisory_calls_per_hour": self.advisory_calls_per_hour(),
            "grok_scheduler_eligible_targets": self.scheduler_eligible_targets,
            "grok_scheduler_targets_selected": self.scheduler_targets_selected,
            "grok_scheduler_targets_skipped": self.scheduler_targets_skipped,
            "grok_scheduler_skip_reasons": dict(self.scheduler_skip_reasons),
            "grok_scheduler_rate_limited_count": self.scheduler_rate_limited_count,
            "grok_scheduler_no_target_count": self.scheduler_no_target_count,
            "grok_market_groups_analyzed": self.market_groups_analyzed,
            "grok_bregman_near_misses_analyzed": self.bregman_near_misses_analyzed,
            "grok_bregman_incomplete_groups_analyzed": self.incomplete_groups_analyzed,
            "grok_bregman_malformed_groups_analyzed": self.malformed_groups_analyzed,
            "grok_news_linked_markets_analyzed": self.news_linked_markets_analyzed,
            "grok_learning_features_written": self.learning_features_written,
            "grok_proof_call_last_reason": self.last_reason,
            "grok_proof_call_last_ts": self.last_call_ts,
        }
