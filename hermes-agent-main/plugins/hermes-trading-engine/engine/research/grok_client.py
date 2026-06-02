"""GrokResearchClient — controlled, research-only Grok 4.3 integration.

Turns a market question into an audited ProbabilityEstimateBundle (or a typed
ResearchFailure). It enforces budget/rate limits (fail-closed), timeouts,
retries, strict-schema validation, evidence persistence, and secret redaction.

Grok may research and estimate probability. It may NOT execute, size, or
bypass the RiskEngine. Any execution/size field in the output is stripped and
flagged. Replay never uses this client (see ReplayResearchCache).
"""

from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Callable, Optional, Union

from .budget import ResearchBudget
from .evidence_store import EvidenceStore
from .probability import ProbabilityEstimator
from .prompts import build_messages, prompt_hash
from .schemas import (
    ONLINE_MODES,
    ProbabilityEstimateBundle,
    ResearchFailure,
    _nid,
)
from .source_cache import SourceCache
from .validators import (
    forbidden_execution_keys,
    redact,
    validate_probability_output,
)

logger = logging.getLogger("hte.research")

ResearchResult = Union[ProbabilityEstimateBundle, ResearchFailure]


def _now_ms() -> int:
    return int(time.time() * 1000)


class GrokResearchClient:
    def __init__(self, *, store=None, budget: Optional[ResearchBudget] = None,
                 source_cache: Optional[SourceCache] = None,
                 evidence_store: Optional[EvidenceStore] = None,
                 estimator: Optional[ProbabilityEstimator] = None,
                 mode: str = "offline_cache", model: Optional[str] = None,
                 base_url: Optional[str] = None, timeout_s: Optional[float] = None,
                 max_retries: Optional[int] = None, clock: Optional[Callable[[], int]] = None):
        self.store = store
        self.budget = budget or ResearchBudget.from_env(clock=clock)
        self.cache = source_cache or SourceCache(
            ttl_seconds=int(os.getenv("RESEARCH_CACHE_TTL_SECONDS", "900") or 900))
        self.evidence_store = evidence_store or EvidenceStore(store, self.cache)
        self.estimator = estimator or ProbabilityEstimator()
        self.mode = mode
        self.model = (model or os.getenv("GROK_MODEL") or "grok-4.3").strip()
        self.base_url = (base_url or os.getenv("GROK_BASE_URL") or "https://api.x.ai/v1").strip()
        self.timeout_s = timeout_s if timeout_s is not None else float(
            os.getenv("GROK_TIMEOUT_SECONDS", "30") or 30)
        self.max_retries = max_retries if max_retries is not None else int(
            os.getenv("GROK_MAX_RETRIES", "2") or 2)
        self.now_ms = clock or _now_ms
        self.enable_web_search = os.getenv("GROK_ENABLE_WEB_SEARCH", "0") in ("1", "true", "True")
        self.enable_x_search = os.getenv("GROK_ENABLE_X_SEARCH", "0") in ("1", "true", "True")
        self.reasoning_effort = os.getenv("GROK_REASONING_EFFORT", "low")
        # Secrets are read here but NEVER logged or persisted.
        self._api_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or ""

    # -- public --------------------------------------------------------- #
    def research(self, market_ctx: dict, mode: Optional[str] = None) -> ResearchResult:
        mode = mode or self.mode
        venue = market_ctx.get("venue") or "polymarket"
        market_id = str(market_ctx.get("market_id") or "")
        asset_id = market_ctx.get("asset_id")
        outcome = market_ctx.get("outcome") or "YES"
        run_id = _nid("rr")
        ts = self.now_ms()
        cfg = self._config_for_hash()

        if mode not in ONLINE_MODES:
            return self._fail(run_id, market_id, asset_id, outcome, venue, mode,
                              "FAILED", "research_mode_not_online", retryable=False)

        ok, reason = self.budget.check(market_id)
        if not ok:
            self._record_budget_event("blocked", market_id, reason)
            self._record_run(run_id, ts, "BUDGET_BLOCKED", mode, venue, market_id, asset_id,
                             outcome, prompt_hash_=None, cfg=cfg, error_type=reason)
            return self._fail(run_id, market_id, asset_id, outcome, venue, mode,
                              "BUDGET_BLOCKED", reason or "budget", retryable=True)

        cached_evidence = self._cached_evidence(venue, market_id)
        messages = build_messages(market_ctx, cached_evidence)
        phash = prompt_hash(messages, cfg)

        raw, usage, latency_ms, err = self._invoke_with_retries(messages)
        self.budget.record(market_id, self._estimate_cost(usage))

        if raw is None:
            self._record_run(run_id, ts, "FAILED", mode, venue, market_id, asset_id, outcome,
                             prompt_hash_=phash, cfg=cfg, latency_ms=latency_ms,
                             error_type="model_call_failed", error_message=redact(err or ""))
            return self._fail(run_id, market_id, asset_id, outcome, venue, mode,
                              "FAILED", "model_call_failed", retryable=True,
                              diagnostics={"error": redact(err or "")})

        # Strip + flag any execution/sizing fields Grok tried to emit.
        forbidden = forbidden_execution_keys(raw)
        if forbidden:
            self._record_validation(run_id, "warning", "execution_attempt",
                                    f"stripped execution/size keys: {forbidden}")
            logger.warning("research: stripped forbidden keys %s", forbidden)

        raw_ids = dict(raw) if isinstance(raw, dict) else {}
        raw_ids.update({"market_id": market_id, "asset_id": asset_id, "outcome": outcome})
        output = validate_probability_output(raw_ids)
        if output is None:
            self._record_validation(run_id, "error", "schema_invalid", "grok output failed schema")
            self._record_run(run_id, ts, "VALIDATION_FAILED", mode, venue, market_id, asset_id,
                             outcome, prompt_hash_=phash, cfg=cfg, latency_ms=latency_ms,
                             error_type="VALIDATION_FAILED")
            return self._fail(run_id, market_id, asset_id, outcome, venue, mode,
                              "VALIDATION_FAILED", "schema_invalid", retryable=False)

        if not output.evidence:
            self._record_run(run_id, ts, "NO_EVIDENCE", mode, venue, market_id, asset_id,
                             outcome, prompt_hash_=phash, cfg=cfg, latency_ms=latency_ms,
                             error_type="NO_EVIDENCE")
            return self._fail(run_id, market_id, asset_id, outcome, venue, mode,
                              "NO_EVIDENCE", "no_evidence", retryable=False)

        bundle = self.estimator.estimate(
            output, p_market=market_ctx.get("p_market_mid"),
            p_model=market_ctx.get("p_model"), research_run_id=run_id,
            venue=venue, mode=mode, ts_ms=ts)

        if self.store is not None:
            try:
                self.evidence_store.persist_all(
                    list(output.evidence), research_run_id=run_id,
                    estimate_id=bundle.estimate_id, venue=venue, market_id=market_id,
                    asset_id=asset_id)
                self.store.add_probability_estimate(bundle.record())
            except Exception:  # noqa: BLE001
                pass
        self._record_run(run_id, ts, "SUCCEEDED", mode, venue, market_id, asset_id, outcome,
                         prompt_hash_=phash, cfg=cfg, usage=usage, latency_ms=latency_ms)
        return bundle

    # -- model call (mock this in tests) -------------------------------- #
    def _invoke_with_retries(self, messages):
        last_err = None
        start = time.time()
        for _ in range(max(1, self.max_retries + 1)):
            try:
                raw, usage = self._call_model(messages)
                return raw, usage, int((time.time() - start) * 1000), None
            except Exception as e:  # noqa: BLE001 — never let secrets/excs escape
                last_err = redact(str(e))
                continue
        return None, None, int((time.time() - start) * 1000), last_err

    def _call_model(self, messages):
        """Perform the actual xAI call. Returns (raw_dict, usage_dict). Raises on
        any transport/timeout error (handled by the retry wrapper)."""
        if not self._api_key:
            raise RuntimeError("missing XAI_API_KEY/GROK_API_KEY")
        import json as _json

        import httpx  # local import keeps the module importable without httpx

        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}",
                   "Content-Type": "application/json"}
        resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        raw = _json.loads(content) if isinstance(content, str) else content
        return raw, data.get("usage")

    # -- helpers -------------------------------------------------------- #
    def _config_for_hash(self) -> dict:
        return {"model": self.model, "base_url": self.base_url,
                "web_search": self.enable_web_search, "x_search": self.enable_x_search,
                "reasoning_effort": self.reasoning_effort}

    def _cached_evidence(self, venue: str, market_id: str) -> list[dict]:
        if self.store is None:
            return []
        try:
            return self.store.get_research_evidence(limit=10)
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _estimate_cost(usage) -> Decimal:
        if not isinstance(usage, dict):
            return Decimal(0)
        try:
            inp = int(usage.get("prompt_tokens") or 0)
            out = int(usage.get("completion_tokens") or 0)
            # rough grok-4.3 estimate; only used for the daily cap, not billing
            return (Decimal(inp) * Decimal("0.000003")) + (Decimal(out) * Decimal("0.000015"))
        except Exception:  # noqa: BLE001
            return Decimal(0)

    def _record_run(self, run_id, ts, status, mode, venue, market_id, asset_id, outcome,
                    *, prompt_hash_=None, cfg=None, usage=None, latency_ms=None,
                    error_type=None, error_message=None) -> None:
        if self.store is None:
            return
        u = usage if isinstance(usage, dict) else {}
        try:
            self.store.add_research_run({
                "research_run_id": run_id, "ts_ms": ts, "status": status, "mode": mode,
                "model": self.model, "venue": venue, "market_id": market_id,
                "asset_id": asset_id, "outcome": outcome, "prompt_hash": prompt_hash_,
                "config_hash": None, "request_tokens": u.get("prompt_tokens"),
                "response_tokens": u.get("completion_tokens"),
                "reasoning_tokens": u.get("reasoning_tokens"),
                "cached_tokens": u.get("cached_tokens"),
                "estimated_cost_usd": str(self._estimate_cost(usage)),
                "latency_ms": latency_ms, "error_type": error_type,
                "error_message": error_message, "payload_json": {"cfg": cfg or {}},
            })
        except Exception:  # noqa: BLE001
            pass

    def _record_validation(self, run_id, severity, event_type, reason) -> None:
        if self.store is None:
            return
        try:
            self.store.add_research_validation_event({
                "research_run_id": run_id, "severity": severity,
                "event_type": event_type, "reason": redact(reason)})
        except Exception:  # noqa: BLE001
            pass

    def _record_budget_event(self, event_type, market_id, reason) -> None:
        if self.store is None:
            return
        try:
            self.store.add_research_budget_event({
                "event_type": event_type, "model": self.model, "market_id": market_id,
                "reason": reason})
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, run_id, market_id, asset_id, outcome, venue, mode, status, reason,
              *, retryable=False, diagnostics=None) -> ResearchFailure:
        return ResearchFailure(
            research_run_id=run_id, market_id=market_id, asset_id=asset_id,
            status=status, reason=reason, retryable=retryable, ts_ms=self.now_ms(),
            diagnostics={"venue": venue, "mode": mode, "outcome": outcome,
                         **(diagnostics or {})})

    @classmethod
    def from_env(cls, store=None, clock=None) -> "GrokResearchClient":
        mode = (os.getenv("RESEARCH_MODE") or "offline_cache").strip().lower()
        return cls(store=store, mode=mode, clock=clock)
