"""Read-only CLOB order-book hydration for targeted Bregman groups (PAPER ONLY).

Before strict certification, fill each :class:`SimplexLeg` with REAL best bid/ask +
side-specific depth + book age from the Polymarket CLOB ``/book`` endpoint (per token).
This lets the certifier use a REAL NO-token ask instead of the synthetic ``1 − YES bid``
diagnostic price — the synthetic price stays diagnostic/shadow only and is NEVER
executable.

Strict-safety invariants (never violated):
* read-only — only fetches public books; no wallet, no order path, no signing;
* the executable price is the REAL best ASK (never the midpoint/reference price);
* if hydration fails, the leg keeps its synthetic price and the group stays
  shadow/diagnostic only with an exact failure reason;
* never loosens depth/spread/freshness/edge gates.

The book fetcher is INJECTED (``book_fetcher(token_id) -> dict | None``) so this is
fully unit-testable with no network. A default public-CLOB fetcher is provided but is
OFF unless explicitly enabled, and never raises.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("hte.training.clob_hydration")

DEFAULT_CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def _levels(side) -> list:
    """Normalize a book side to a list of (price, size) floats (accepts dict rows
    ``{"price","size"}`` or pair rows ``[price, size]``). Pure."""
    out = []
    for row in (side or []):
        try:
            if isinstance(row, dict):
                p = float(row.get("price"))
                s = float(row.get("size", row.get("amount", 0.0)) or 0.0)
            else:
                p = float(row[0])
                s = float(row[1]) if len(row) > 1 else 0.0
            if p > 0 and s >= 0:
                out.append((p, s))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def parse_clob_book(book: dict) -> Optional[dict]:
    """Parse a CLOB ``/book`` payload into best bid/ask + side depth + timestamp.

    Executable ASK = LOWEST ask price; best BID = HIGHEST bid price (NEVER the
    midpoint). Side depth is the notional at the best level (price * size). Returns
    None when the book has no usable asks (missing-ask is never fabricated)."""
    if not isinstance(book, dict):
        return None
    asks = _levels(book.get("asks"))
    bids = _levels(book.get("bids"))
    if not asks:
        return None                              # no executable ask -> unusable
    best_ask, ask_size = min(asks, key=lambda x: x[0])
    best_bid, bid_size = (max(bids, key=lambda x: x[0]) if bids else (None, 0.0))
    ts = book.get("timestamp") or book.get("ts") or book.get("time")
    from engine.arbitrage.price_parsing import parse_epoch_seconds
    ts_s = parse_epoch_seconds(ts)
    return {
        "best_ask": round(best_ask, 6),
        "best_bid": (round(best_bid, 6) if best_bid is not None else None),
        "ask_depth_usd": round(best_ask * ask_size, 4),
        "bid_depth_usd": (round(best_bid * bid_size, 4) if best_bid is not None else 0.0),
        "book_ts": ts_s,
    }


class BregmanClobHydrator:
    """Hydrate Bregman group legs with REAL CLOB books (read-only, injectable)."""

    def __init__(self, book_fetcher: Optional[Callable[[str], Optional[dict]]] = None, *,
                 enabled: bool = True, max_book_age_s: float = 20.0,
                 max_groups_per_tick: int = 40,
                 clock: Optional[Callable[[], float]] = None):
        self.book_fetcher = book_fetcher
        self.enabled = bool(enabled and book_fetcher is not None)
        self.max_book_age_s = float(max_book_age_s)
        self.max_groups_per_tick = int(max_groups_per_tick)
        self._clock = clock or time.time

    def _hydrate_leg(self, leg, now: float) -> "tuple[bool, Optional[str]]":
        tok = getattr(leg, "token_id", "") or ""
        if not tok or tok.endswith(":YES") or tok.endswith(":NO") or ":" in tok and \
                tok.split(":")[-1] in ("YES", "NO"):
            return False, "no_real_token_id"
        try:
            book = self.book_fetcher(tok)
        except Exception as exc:  # noqa: BLE001 — hydration must never raise
            return False, f"fetch_error:{type(exc).__name__}"
        parsed = parse_clob_book(book) if book else None
        if parsed is None:
            return False, "no_book_or_no_ask"
        # populate REAL executable values (best ask = executable price, NOT midpoint)
        leg.ask = parsed["best_ask"]
        if parsed["best_bid"] is not None:
            leg.bid = parsed["best_bid"]
        leg.depth_usd = parsed["ask_depth_usd"]          # ask-side for BUY
        leg.visible_ask_depth_usd = parsed["ask_depth_usd"]
        leg.visible_bid_depth_usd = parsed["bid_depth_usd"]
        leg.synthetic_price = False                       # REAL book, not derived
        leg.hydrated_from_clob = True
        if parsed["book_ts"] is not None:
            age = max(0.0, now - parsed["book_ts"])
            leg.book_age_s = round(age, 3)
            leg.fresh_book = age <= self.max_book_age_s   # freshness gate UNCHANGED
            leg.stale = not leg.fresh_book
        return True, None

    @staticmethod
    def _binary_first(groups: list) -> list:
        """Order groups so binary YES/NO (the targeted-scan hydration target) are
        hydrated FIRST within the per-tick budget. Selection-only; no gate change."""
        binary, other = [], []
        for g in (groups or []):
            (binary if getattr(g, "group_type", "") == "binary_yes_no" else other).append(g)
        return binary + other

    def hydrate(self, groups: list, *, now: Optional[float] = None) -> dict:
        """Hydrate up to ``max_groups_per_tick`` groups with real CLOB books (binary
        YES/NO groups first). Returns metrics. On any leg failure the group's leg keeps
        its synthetic price and is flagged so the certifier treats it as
        diagnostic/shadow only."""
        now = float(now if now is not None else self._clock())
        attempted = success = failed = real_books = synthetic_only = 0
        failure_reasons: dict = {}
        if not self.enabled:
            return {
                "bregman_clob_hydration_enabled": False,
                "bregman_clob_hydration_attempted": 0,
                "bregman_clob_hydration_success": 0,
                "bregman_clob_hydration_failed": 0,
                "bregman_real_yes_no_books_seen": 0,
                "bregman_synthetic_no_diagnostic_only_count": 0,
                "bregman_certifier_used_real_clob_books": False,
                "bregman_hydration_failure_reasons": {},
            }
        for g in self._binary_first(groups)[: self.max_groups_per_tick]:
            legs = list(getattr(g, "legs", None) or [])
            if not legs:
                continue
            attempted += 1
            all_real = True
            for leg in legs:
                ok, reason = self._hydrate_leg(leg, now)
                if ok:
                    real_books += 1
                else:
                    all_real = False
                    if reason:
                        failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            if all_real:
                success += 1
            else:
                failed += 1
                # any leg still synthetic -> the group is diagnostic/shadow only
                if any(getattr(l, "synthetic_price", False) for l in legs):
                    synthetic_only += 1
        return {
            "bregman_clob_hydration_enabled": True,
            "bregman_clob_hydration_attempted": attempted,
            "bregman_clob_hydration_success": success,
            "bregman_clob_hydration_failed": failed,
            "bregman_real_yes_no_books_seen": real_books,
            "bregman_synthetic_no_diagnostic_only_count": synthetic_only,
            "bregman_certifier_used_real_clob_books": bool(success > 0),
            "bregman_hydration_failure_reasons": failure_reasons,
        }


def clob_book_fetcher(*, base_url: Optional[str] = None,
                      timeout_s: float = 3.0) -> Callable[[str], Optional[dict]]:
    """Build a READ-ONLY public-CLOB ``/book`` fetcher (httpx GET, one per token id)
    backed by a single keep-alive client. ALWAYS returns a callable — callers decide
    when to attach it. Never signs/trades; never raises (returns None on any error).

    This is the production hydration path (used by the paper-training entrypoint when
    CLOB read-only is enabled). It hits only the public order-book endpoint."""
    import os
    url = base_url or os.getenv("BREGMAN_CLOB_BOOK_URL", DEFAULT_CLOB_BOOK_URL)
    _client_box: dict = {}

    def _client():
        c = _client_box.get("c")
        if c is None:
            import httpx
            c = httpx.Client(timeout=timeout_s,
                             headers={"User-Agent": "hermes-bregman-clob/1.0"})
            _client_box["c"] = c
        return c

    def _fetch(token_id: str) -> Optional[dict]:
        try:
            resp = _client().get(url, params={"token_id": token_id})
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:  # noqa: BLE001 — read-only hydration never breaks a tick
            return None
    return _fetch


def default_clob_book_fetcher(*, base_url: str = DEFAULT_CLOB_BOOK_URL,
                              timeout_s: float = 3.0) -> Optional[Callable[[str], Optional[dict]]]:
    """Constructor default: OFF unless ``BREGMAN_CLOB_HYDRATION_ENABLED`` is set (keeps
    unit tests that build a trainer fully offline). Production wiring instead calls
    :func:`clob_book_fetcher` explicitly. Returns None when not enabled."""
    import os
    if str(os.getenv("BREGMAN_CLOB_HYDRATION_ENABLED", "")).strip().lower() \
            not in ("1", "true", "yes", "on"):
        return None
    return clob_book_fetcher(base_url=base_url, timeout_s=timeout_s)
