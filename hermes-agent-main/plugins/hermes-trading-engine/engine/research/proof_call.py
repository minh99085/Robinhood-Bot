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

    def __init__(self, *, enabled: bool, max_per_hour: int = 1,
                 advisory_only: bool = True, clock: Optional[Callable[[], float]] = None):
        self.enabled = bool(enabled)
        self.max_per_hour = int(max_per_hour)
        self.advisory_only = bool(advisory_only)
        self._clock = clock or time.time
        self._calls: list[float] = []          # timestamps of proof calls made
        self.calls_total = 0
        self.calls_with_news = 0
        self.evidence_records_written = 0
        self.last_reason: Optional[str] = None
        self.last_call_ts: Optional[float] = None

    def _recent_calls(self, now: float) -> int:
        cutoff = now - 3600.0
        self._calls = [t for t in self._calls if t >= cutoff]
        return len(self._calls)

    def maybe_call(self, *, client, online: bool, has_key: bool,
                   news_packet, market_ctx: Optional[dict],
                   evidence_sink: Optional[Callable[[dict], None]] = None,
                   now: Optional[float] = None) -> dict:
        """Attempt at most one advisory proof call. Returns a result dict with
        ``called`` (bool), ``reason`` (precise zero-call reason when not called),
        ``grok_calls_total`` / ``grok_calls_with_news`` (cumulative), and
        ``advisory_only=True``. Never raises."""
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
        elif self.max_per_hour > 0 and self._recent_calls(now) >= self.max_per_hour:
            reason = REASON_RATE_LIMIT
        else:
            reason = None

        if reason is not None:
            self.last_reason = reason
            result["reason"] = reason
            return result

        # --- make ONE advisory-only call (research only; never trades) ---
        try:
            res = client.research(market_ctx, news_packet=news_packet)
        except Exception as exc:  # noqa: BLE001 — provider error is a precise reason
            self.last_reason = REASON_PROVIDER_ERROR
            result["reason"] = REASON_PROVIDER_ERROR
            result["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            return result

        # a cache-only / non-online result is NOT a real proof call
        source = getattr(res, "source", None) or (res.get("source") if isinstance(res, dict) else None)
        if source in ("grok_cache", "offline_stub", "cache", None):
            self.last_reason = REASON_CACHE_ONLY
            result["reason"] = REASON_CACHE_ONLY
            return result

        self._calls.append(now)
        self.last_call_ts = now
        self.calls_total += 1
        self.calls_with_news += 1            # proof call always attaches a news packet
        evidence = {
            "kind": "grok_advisory_proof_call", "advisory_only": True,
            "ts": round(now, 3), "market_id": market_ctx.get("market_id"),
            "news_items": (len(news_packet) if hasattr(news_packet, "__len__") else 1),
            "source": source, "is_edge_proof": False,
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

    def status(self) -> dict:
        return {
            "grok_proof_call_enabled": self.enabled,
            "grok_proof_call_advisory_only": self.advisory_only,
            "grok_proof_call_max_per_hour": self.max_per_hour,
            "grok_calls_total": self.calls_total,
            "grok_calls_with_news": self.calls_with_news,
            "grok_evidence_records_written": self.evidence_records_written,
            "grok_proof_call_last_reason": self.last_reason,
            "grok_proof_call_last_ts": self.last_call_ts,
        }
