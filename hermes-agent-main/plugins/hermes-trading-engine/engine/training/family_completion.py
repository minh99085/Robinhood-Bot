"""Targeted event-family completion for Bregman scanning (Priority 1).

Polymarket event payloads embed the FULL set of sibling markets of a mutually-exclusive
event in ``raw["events"][0]["markets"]`` — each with its REAL ``clobTokenIds``. The
volume-ranked flat ``/markets`` scan, however, usually contains only a SUBSET of an
event's siblings, so multi-outcome MECE families never assemble and families that are
1-2 legs short are (correctly) rejected ``not_exhaustive``. That is the dominant reason
the engine discovers only binary complements and zero complete-set arbitrage.

This module performs a READ-ONLY, pre-grouping expansion: for each scanned market that
belongs to an event family, it enumerates the family's declared sibling markets from the
event metadata we ALREADY fetched, and synthesizes :class:`MarketRecord`s for any
siblings missing from the scan slice — carrying ONLY the authoritative token ids + the
shared event context (NO fabricated prices). The existing grouping then assembles the
complete family, the existing read-only CLOB hydrator fetches each missing leg's REAL
book, and the UNCHANGED certifier proves completeness (declared == legs) + after-cost
positivity.

Strict-safety invariants (never violated):
* read-only — synthesizes records only from metadata already fetched; no venue write;
* NO fabricated prices — a synthesized leg has no bid/ask until a REAL book hydrates it;
  if hydration fails the leg stays synthetic and the family stays shadow/diagnostic only;
* completeness is still PROVEN by the certifier from the declared outcome count — this
  module only assembles candidate legs, it never asserts a family is complete or tradable;
* bounded — caps total + per-family new records so a tick's cost stays bounded.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("hte.training.family_completion")

_EVENT_KEYS = ("id", "slug", "ticker", "title")
_NEGRISK_KEYS = ("negRiskMarketID", "negRiskMarketId", "negRisk", "negRiskRequestID")


def _raw(rec) -> dict:
    return getattr(rec, "raw", None) or {}


def _event(rec) -> Optional[dict]:
    """The primary event dict for a record (``raw['events'][0]``), or None."""
    ev = _raw(rec).get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict):
        return ev[0]
    return None


def _sibling_markets(ev: dict) -> list:
    mk = ev.get("markets")
    return [m for m in mk if isinstance(m, dict)] if isinstance(mk, list) else []


def _event_id(rec) -> Optional[str]:
    """The Polymarket event id for a record (``raw['events'][0]['id']``), or None.
    The flat ``/markets`` payload carries this id but NOT the sibling ``markets`` list —
    that is fetched on demand from the ``/events/{id}`` endpoint."""
    ev = _event(rec)
    if ev and ev.get("id") not in (None, ""):
        return str(ev["id"])
    return None


def _sib_id(m: dict) -> str:
    return str(m.get("id") or m.get("conditionId") or m.get("slug") or "")


def _family_liquidity(recs: list) -> float:
    best = 0.0
    for r in recs:
        try:
            v = max(float(getattr(r, "liquidity_usd", 0.0) or 0.0),
                    float(getattr(r, "top_depth_usd", 0.0) or 0.0))
        except (TypeError, ValueError):
            v = 0.0
        best = max(best, v)
    return best


def expand_event_families(records: list, *, now: Optional[float] = None,
                          max_total_new: int = 40, max_per_family: int = 8,
                          min_family_liquidity_usd: float = 0.0,
                          event_fetcher=None, max_events_fetched: int = 20
                          ) -> "tuple[list, dict]":
    """Append authoritative missing sibling records for event families present in
    ``records``. The flat ``/markets`` scan carries each market's event id but not the
    sibling list, so when ``event_fetcher(event_id) -> event_dict`` is provided the full
    sibling set is fetched on demand (bounded by ``max_events_fetched``); embedded
    ``events[0].markets`` is used first when present. Returns ``(records_out, telemetry)``.
    Never raises on a single malformed family or fetch."""
    from engine.markets.universe_manager import MarketRecord

    records = list(records or [])
    existing_ids = {str(getattr(r, "market_id", "") or "") for r in records}
    # group scanned records by event family key; track a representative event + event id
    fam_members: dict[str, list] = {}
    fam_event: dict[str, dict] = {}
    fam_event_id: dict[str, str] = {}
    for rec in records:
        ev = _event(rec)
        if not ev:
            continue
        key = str(getattr(rec, "group_key", "") or "")
        if not key:
            continue
        fam_members.setdefault(key, []).append(rec)
        sibs = _sibling_markets(ev)
        # keep the richest event payload (most siblings listed) as the family reference
        if key not in fam_event or len(sibs) > len(_sibling_markets(fam_event[key])):
            fam_event[key] = ev
        eid = _event_id(rec)
        if eid and key not in fam_event_id:
            fam_event_id[key] = eid

    added: list = []
    families_examined = families_with_gap = 0
    enumerated = added_n = skipped_no_tokens = skipped_low_liq = 0
    events_fetched = events_fetch_failed = 0
    capped = False
    _event_cache: dict[str, list] = {}

    for key, members in fam_members.items():
        families_examined += 1
        ev = fam_event[key]
        sibs = _sibling_markets(ev)
        # On-demand event fetch when the flat payload omits the sibling list.
        if len(sibs) < 2 and event_fetcher is not None:
            eid = fam_event_id.get(key)
            if eid and eid in _event_cache:
                sibs = _event_cache[eid]
            elif eid and events_fetched < int(max_events_fetched):
                events_fetched += 1
                try:
                    fetched = event_fetcher(eid)
                except Exception:  # noqa: BLE001 — a fetch failure never breaks a tick
                    fetched = None
                fsibs = _sibling_markets(fetched) if isinstance(fetched, dict) else []
                _event_cache[eid] = fsibs
                if fsibs:
                    sibs = fsibs
                    if not ev.get("id"):
                        ev = dict(ev); ev["id"] = eid
                else:
                    events_fetch_failed += 1
        if len(sibs) < 2:                       # still nothing enumerable -> skip
            continue
        present = {str(getattr(r, "market_id", "") or "") for r in members}
        gap = [m for m in sibs if _sib_id(m) and _sib_id(m) not in existing_ids
               and _sib_id(m) not in present]
        if not gap:
            continue
        families_with_gap += 1
        enumerated += len(gap)
        if _family_liquidity(members) < float(min_family_liquidity_usd):
            skipped_low_liq += 1
            continue
        # shared, recursion-free event context so the synthesized siblings group with the
        # scanned ones (same group_key) and inherit the declared outcome count.
        shared_event = {k: ev.get(k) for k in _EVENT_KEYS if ev.get(k) is not None}
        shared_event["markets"] = sibs
        # carry neg-risk family identity from a scanned member when the sibling omits it
        ref_raw = _raw(members[0])
        negrisk = {k: ref_raw.get(k) for k in _NEGRISK_KEYS if ref_raw.get(k) is not None}
        per_family = 0
        for m in gap:
            if len(added) >= int(max_total_new):
                capped = True
                break
            if per_family >= int(max_per_family):
                break
            sib_raw = dict(m)
            sib_raw["events"] = [shared_event]
            sib_raw.setdefault("outcomeCount", len(sibs))
            for k, v in negrisk.items():
                sib_raw.setdefault(k, v)
            try:
                rec_new = MarketRecord.from_raw(sib_raw, now=now)
            except Exception:  # noqa: BLE001 — one bad sibling never breaks the family
                continue
            if not rec_new.clob_token_ids:      # no real token -> cannot hydrate -> skip
                skipped_no_tokens += 1
                continue
            added.append(rec_new)
            existing_ids.add(str(rec_new.market_id or ""))
            per_family += 1
            added_n += 1
        if capped:
            break

    tel = {
        "family_completion_enabled": True,
        "family_completion_event_fetch_enabled": event_fetcher is not None,
        "family_completion_families_examined": families_examined,
        "family_completion_families_with_gap": families_with_gap,
        "family_completion_events_fetched": events_fetched,
        "family_completion_events_fetch_failed": events_fetch_failed,
        "family_completion_missing_siblings_enumerated": enumerated,
        "family_completion_missing_siblings_added": added_n,
        "family_completion_skipped_no_tokens": skipped_no_tokens,
        "family_completion_skipped_low_liquidity": skipped_low_liq,
        "family_completion_records_in": len(records),
        "family_completion_records_out": len(records) + len(added),
        "family_completion_capped": capped,
    }
    if added:
        logger.debug("family_completion added %d sibling records across %d families",
                     added_n, families_with_gap)
    return records + added, tel


DEFAULT_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def event_markets_fetcher(*, base_url: Optional[str] = None, timeout_s: float = 4.0):
    """Build a READ-ONLY Polymarket ``/events/{id}`` fetcher (one GET per event id) backed
    by a keep-alive client. Returns the event dict (with its full ``markets`` list) or
    None. Never signs/trades; never raises (returns None on any error)."""
    import os
    url = base_url or os.getenv("FAMILY_COMPLETION_EVENTS_URL", DEFAULT_GAMMA_EVENTS_URL)
    _box: dict = {}

    def _client():
        c = _box.get("c")
        if c is None:
            import httpx
            c = httpx.Client(timeout=timeout_s,
                             headers={"User-Agent": "hermes-family-completion/1.0"})
            _box["c"] = c
        return c

    def _fetch(event_id: str) -> Optional[dict]:
        try:
            resp = _client().get(f"{url}/{event_id}")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data[0] if isinstance(data, list) and data else (
                data if isinstance(data, dict) else None)
        except Exception:  # noqa: BLE001 — read-only enrichment never breaks a tick
            return None
    return _fetch


def default_event_markets_fetcher():
    """Constructor default: the event fetcher is ON only when family completion's event
    fetch is explicitly enabled (env ``FAMILY_COMPLETION_EVENT_FETCH_ENABLED``, or the
    shared read-only CLOB hydration flag ``BREGMAN_CLOB_HYDRATION_ENABLED``). Returns None
    when not enabled so unit tests / offline runs never hit the network."""
    import os
    def _on(name: str) -> bool:
        return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")
    if _on("FAMILY_COMPLETION_EVENT_FETCH_ENABLED") or _on("BREGMAN_CLOB_HYDRATION_ENABLED"):
        return event_markets_fetcher()
    return None
