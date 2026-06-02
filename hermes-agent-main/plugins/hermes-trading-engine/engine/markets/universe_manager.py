"""Adaptive Polymarket Market Universe Manager.

Pipeline (selection only — NEVER places/cancels/sizes orders):

    scan ~1000  ->  filter  ->  score  ->  rank/tier  ->  live-watch A+B  ->  trade from A  ->  hold <= N

Quant scope — *Data Acquisition & Ingestion* + *Data Preprocessing & Feature
Engineering*: the ``MarketRecord`` (and its ``group_key``) produced here is the
input to flagship Bregman-arbitrage simplex grouping — markets sharing an event
``group_key`` form the mutually-exclusive outcome set whose executable prices
are tested against the probability simplex.

Tiers
-----
- **Tier A** (top ``trade_candidate_limit``, default 20): eligible for trade
  *decisions* (still gated by model edge, depth, RiskEngine, dedup, max-open).
- **Tier B** (next ``live_watchlist_limit``, default 80): live WebSocket watchlist.
- **Tier C** (next up to ``max_shortlist``, default 100-200): refreshed periodically,
  NOT live-subscribed.
- **Tier D**: ignored until the next full catalog refresh.

Live order-book subscription targets = Tier A + Tier B token ids. Subscription is
only ever *requested*; it is still gated by ``POLYMARKET_CLOB_ENABLED`` upstream.

Safety: this module is pure/offline for everything except :func:`fetch_catalog`,
which is the only function that touches the network and is invoked explicitly
(CLI / opt-in engine loop). Filtering, scoring and tiering take raw dicts and
make no network calls, so tests and replay never hit the network.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Hard upper bounds (never exceeded regardless of env).
MAX_CATALOG_SCAN = 2000
MAX_SHORTLIST = 200
MAX_LIVE_WATCHLIST = 120
MAX_TRADE_CANDIDATES = 25
MAX_OPEN_TRADES_PAPER = 5
MAX_OPEN_TRADES_HARD_CAP = 8


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() not in ("0", "false", "False", "")


@dataclass
class UniverseConfig:
    """All knobs for the universe pipeline. Values are clamped to safe maxima."""

    # pipeline sizes
    scan_limit: int = 1000
    shortlist_limit: int = 100
    live_watchlist_limit: int = 80      # size of Tier B
    trade_candidate_limit: int = 20     # size of Tier A
    max_open_polymarket_trades: int = 3
    max_open_trades_hard_cap: int = MAX_OPEN_TRADES_HARD_CAP

    # quality thresholds
    min_liquidity_usd: float = 1000.0
    min_volume_24h_usd: float = 500.0
    max_allowed_spread: float = 0.04
    min_top_of_book_depth_usd: float = 100.0

    # refresh cadence (seconds)
    catalog_refresh_seconds: int = 600
    score_refresh_seconds: int = 60

    # behavioural toggles
    allow_longshot: bool = False        # permit price < 0.03 or > 0.97
    live_event_mode: bool = False       # permit markets ending very soon
    min_hours_to_resolution: float = 6.0
    rebalance_jaccard_threshold: float = 0.20

    def __post_init__(self) -> None:
        self.scan_limit = max(1, min(int(self.scan_limit), MAX_CATALOG_SCAN))
        self.shortlist_limit = max(1, min(int(self.shortlist_limit), MAX_SHORTLIST))
        self.live_watchlist_limit = max(0, min(int(self.live_watchlist_limit), MAX_LIVE_WATCHLIST))
        self.trade_candidate_limit = max(1, min(int(self.trade_candidate_limit), MAX_TRADE_CANDIDATES))
        self.max_open_polymarket_trades = max(
            0, min(int(self.max_open_polymarket_trades), MAX_OPEN_TRADES_HARD_CAP))
        self.max_open_trades_hard_cap = max(
            1, min(int(self.max_open_trades_hard_cap), MAX_OPEN_TRADES_HARD_CAP))

    @classmethod
    def from_env(cls) -> "UniverseConfig":
        return cls(
            scan_limit=_env_int("MARKET_SCAN_LIMIT", 1000),
            shortlist_limit=_env_int("MARKET_SHORTLIST_LIMIT", 100),
            live_watchlist_limit=_env_int("MARKET_LIVE_WATCHLIST_LIMIT", 80),
            trade_candidate_limit=_env_int("MARKET_TRADE_CANDIDATE_LIMIT", 20),
            max_open_polymarket_trades=_env_int("MAX_OPEN_POLYMARKET_TRADES", 3),
            max_open_trades_hard_cap=_env_int("MAX_OPEN_TRADES_HARD_CAP", MAX_OPEN_TRADES_HARD_CAP),
            min_liquidity_usd=_env_float("MIN_MARKET_LIQUIDITY_USD", 1000.0),
            min_volume_24h_usd=_env_float("MIN_MARKET_VOLUME_24H_USD", 500.0),
            max_allowed_spread=_env_float("MAX_ALLOWED_SPREAD", 0.04),
            min_top_of_book_depth_usd=_env_float("MIN_TOP_OF_BOOK_DEPTH_USD", 100.0),
            catalog_refresh_seconds=_env_int("CATALOG_REFRESH_SECONDS", 600),
            score_refresh_seconds=_env_int("SCORE_REFRESH_SECONDS", 60),
            allow_longshot=_env_bool("MARKET_ALLOW_LONGSHOT", False),
            live_event_mode=_env_bool("MARKET_LIVE_EVENT_MODE", False),
        )

    def effective_max_open_trades(self, paper: bool = True) -> int:
        cap = self.max_open_trades_hard_cap
        if paper:
            cap = min(cap, MAX_OPEN_TRADES_PAPER)
        return max(0, min(self.max_open_polymarket_trades, cap))

    def as_dict(self) -> dict:
        return {
            "scan_limit": self.scan_limit,
            "shortlist_limit": self.shortlist_limit,
            "live_watchlist_limit": self.live_watchlist_limit,
            "trade_candidate_limit": self.trade_candidate_limit,
            "max_open_polymarket_trades": self.max_open_polymarket_trades,
            "max_open_trades_paper": MAX_OPEN_TRADES_PAPER,
            "max_open_trades_hard_cap": self.max_open_trades_hard_cap,
            "min_liquidity_usd": self.min_liquidity_usd,
            "min_volume_24h_usd": self.min_volume_24h_usd,
            "max_allowed_spread": self.max_allowed_spread,
            "min_top_of_book_depth_usd": self.min_top_of_book_depth_usd,
            "catalog_refresh_seconds": self.catalog_refresh_seconds,
            "score_refresh_seconds": self.score_refresh_seconds,
            "allow_longshot": self.allow_longshot,
            "live_event_mode": self.live_event_mode,
        }


# ---------------------------------------------------------------------------
# Raw -> MarketRecord normalisation
# ---------------------------------------------------------------------------

def _as_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


def _parse_list(v) -> list:
    """Gamma often encodes list fields as a JSON-encoded string."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _parse_end_ts(raw: dict) -> Optional[float]:
    for k in ("endDate", "end_date_iso", "endDateIso", "end_date"):
        val = raw.get(k)
        if not val:
            continue
        if isinstance(val, (int, float)):
            return float(val)
        try:
            s = str(val).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _group_key(raw: dict) -> str:
    """Identify the correlated-event group a market belongs to.

    Markets in the same event (e.g. multiple candidates of one election) must
    not all be traded at once. We derive a stable key from event metadata.
    """
    events = raw.get("events")
    if isinstance(events, list) and events:
        ev = events[0]
        if isinstance(ev, dict):
            for k in ("id", "slug", "ticker", "title"):
                if ev.get(k):
                    return f"event:{ev[k]}"
    for k in ("negRiskMarketID", "negRiskMarketId", "conditionId", "groupItemTitle"):
        if raw.get(k):
            return f"{k}:{raw[k]}"
    return f"market:{raw.get('id') or raw.get('slug') or id(raw)}"


@dataclass
class MarketRecord:
    market_id: str
    question: str
    group_key: str
    category: str
    clob_token_ids: list
    yes_price: Optional[float]
    liquidity_usd: float
    volume_24h_usd: float
    volume_total_usd: float
    spread: float
    top_depth_usd: float
    end_ts: Optional[float]
    book_age_s: Optional[float]
    has_resolution_text: bool
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict, now: Optional[float] = None) -> "MarketRecord":
        now = now or time.time()
        prices = _parse_list(raw.get("outcomePrices"))
        yes_price = _as_float(prices[0], None) if prices else None
        if yes_price is None and raw.get("lastTradePrice") not in (None, ""):
            yes_price = _as_float(raw.get("lastTradePrice"), None)
        best_bid = _as_float(raw.get("bestBid"), 0.0)
        best_ask = _as_float(raw.get("bestAsk"), 0.0)
        if raw.get("spread") not in (None, ""):
            spread = _as_float(raw.get("spread"), 0.0)
        elif best_bid and best_ask:
            spread = max(0.0, best_ask - best_bid)
        else:
            spread = 0.0
        liq = _as_float(raw.get("liquidityNum"), 0.0) or _as_float(raw.get("liquidity"), 0.0) \
            or _as_float(raw.get("liquidityClob"), 0.0)
        vol24 = _as_float(raw.get("volume24hr"), 0.0) or _as_float(raw.get("volume24hrClob"), 0.0)
        vol_total = _as_float(raw.get("volumeNum"), 0.0) or _as_float(raw.get("volume"), 0.0)
        depth = _as_float(raw.get("topDepthUsd"), 0.0) or _as_float(raw.get("orderMinSize"), 0.0)
        if not depth and liq:
            depth = liq * 0.02  # heuristic top-of-book fraction when not provided
        book_ts = raw.get("bookUpdatedTs") or raw.get("orderBookTs")
        book_age = (now - _as_float(book_ts)) if book_ts else None
        desc = (raw.get("description") or "").strip()
        rules = (raw.get("rules") or raw.get("resolutionSource") or "").strip()
        return cls(
            market_id=str(raw.get("id") or raw.get("slug") or ""),
            question=str(raw.get("question") or raw.get("title") or raw.get("slug") or ""),
            group_key=_group_key(raw),
            category=str(raw.get("category") or "uncategorized"),
            clob_token_ids=[str(t) for t in _parse_list(raw.get("clobTokenIds")) if t],
            yes_price=yes_price,
            liquidity_usd=liq,
            volume_24h_usd=vol24,
            volume_total_usd=vol_total,
            spread=spread,
            top_depth_usd=depth,
            end_ts=_parse_end_ts(raw),
            book_age_s=book_age,
            has_resolution_text=bool(desc or rules),
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Step 1: filtering
# ---------------------------------------------------------------------------

def passes_filters(raw: dict, cfg: UniverseConfig, now: Optional[float] = None) -> tuple[bool, str]:
    """Return (kept, reason). reason == "ok" when kept."""
    now = now or time.time()
    if not _as_bool(raw.get("active")):
        return False, "inactive"
    if _as_bool(raw.get("closed")):
        return False, "closed"
    if _as_bool(raw.get("archived")):
        return False, "archived"
    if not _as_bool(raw.get("enableOrderBook")):
        return False, "orderbook_disabled"
    if not _as_bool(raw.get("acceptingOrders")):
        return False, "not_accepting_orders"
    if not _parse_list(raw.get("clobTokenIds")):
        return False, "missing_clob_token_ids"
    if not _parse_list(raw.get("outcomePrices")):
        return False, "missing_outcome_prices"
    end_ts = _parse_end_ts(raw)
    if end_ts is None:
        return False, "no_end_date"
    if end_ts <= now:
        return False, "expired"
    desc = (raw.get("description") or "").strip()
    rules = (raw.get("rules") or raw.get("resolutionSource") or "").strip()
    if not desc and not rules:
        return False, "ambiguous_resolution"
    return True, "ok"


# ---------------------------------------------------------------------------
# Step 2: scoring
# ---------------------------------------------------------------------------

def _log_score(value: float, full: float) -> float:
    if value <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(1.0 + value) / math.log10(1.0 + full)))


def score_market(rec: MarketRecord, cfg: UniverseConfig,
                 category_counts: Optional[dict] = None,
                 open_event_groups: Optional[set] = None,
                 now: Optional[float] = None) -> dict:
    """Compute the MarketQualityScore for one market.

    Returns {"score", "components", "penalties", "reasons"}.
    """
    now = now or time.time()
    category_counts = category_counts or {}
    open_event_groups = open_event_groups or set()
    reasons: list[str] = []

    liquidity_score = _log_score(rec.liquidity_usd, 100_000.0)
    volume_24h_score = _log_score(rec.volume_24h_usd, 100_000.0)

    # velocity = how much of typical daily volume is happening now
    if rec.volume_total_usd > 0 and rec.end_ts:
        daily_avg = rec.volume_total_usd / 30.0  # crude 30d average proxy
        velocity = rec.volume_24h_usd / daily_avg if daily_avg > 0 else 0.0
        volume_velocity_score = max(0.0, min(1.0, velocity / 2.0))
    else:
        volume_velocity_score = 0.5  # neutral when unknown

    spread_score = max(0.0, 1.0 - (rec.spread / cfg.max_allowed_spread)) if cfg.max_allowed_spread else 0.0
    spread_score = max(0.0, min(1.0, spread_score))

    depth_score = max(0.0, min(1.0, rec.top_depth_usd / (cfg.min_top_of_book_depth_usd * 10.0)))

    # time-to-resolution: prefer a window of ~1-30 days; very soon or very far scores lower
    if rec.end_ts:
        hours = (rec.end_ts - now) / 3600.0
        if hours <= 0:
            ttr_score = 0.0
        elif hours < cfg.min_hours_to_resolution:
            ttr_score = 0.2
        elif hours <= 24 * 30:
            ttr_score = 1.0 - abs(math.log10(max(hours, 1.0)) - math.log10(72.0)) / 2.0
            ttr_score = max(0.2, min(1.0, ttr_score))
        else:
            ttr_score = 0.4
    else:
        ttr_score = 0.0

    cat_n = category_counts.get(rec.category, 1)
    category_diversity_score = 1.0 / math.sqrt(max(1, cat_n))

    components = {
        "liquidity": liquidity_score,
        "volume_24h": volume_24h_score,
        "volume_velocity": volume_velocity_score,
        "spread": spread_score,
        "depth": depth_score,
        "time_to_resolution": ttr_score,
        "category_diversity": category_diversity_score,
    }

    base = (0.25 * liquidity_score + 0.20 * volume_24h_score + 0.15 * volume_velocity_score
            + 0.15 * spread_score + 0.10 * depth_score + 0.10 * ttr_score
            + 0.05 * category_diversity_score)

    # positive descriptive tags for the top markets
    if spread_score >= 0.8:
        reasons.append("tight_spread")
    if liquidity_score >= 0.6:
        reasons.append("high_liquidity")
    if depth_score >= 0.6:
        reasons.append("deep_book")
    if volume_24h_score >= 0.6:
        reasons.append("active_volume")

    penalties: dict[str, float] = {}
    if rec.spread > cfg.max_allowed_spread:
        penalties["wide_spread"] = 0.20
    if rec.top_depth_usd < cfg.min_top_of_book_depth_usd:
        penalties["low_depth"] = 0.10
    if rec.book_age_s is not None and rec.book_age_s > 60.0:
        penalties["stale_book"] = 0.10
    if not rec.clob_token_ids:
        penalties["missing_clob_token_ids"] = 0.50
    if rec.volume_24h_usd < cfg.min_volume_24h_usd:
        penalties["low_volume_24h"] = 0.15
    if rec.liquidity_usd < cfg.min_liquidity_usd:
        penalties["low_liquidity"] = 0.10
    if rec.yes_price is not None and (rec.yes_price < 0.03 or rec.yes_price > 0.97) \
            and not cfg.allow_longshot:
        penalties["extreme_price"] = 0.25
    if rec.end_ts:
        hours = (rec.end_ts - now) / 3600.0
        if hours < cfg.min_hours_to_resolution and not cfg.live_event_mode:
            penalties["ending_too_soon"] = 0.20
    if not rec.has_resolution_text:
        penalties["unclear_resolution"] = 0.20
    if rec.group_key in open_event_groups:
        penalties["duplicate_event_exposure"] = 0.30

    penalty_total = sum(penalties.values())
    score = max(0.0, base - penalty_total)
    for p in penalties:
        reasons.append(f"PENALTY:{p}")

    return {"score": round(score, 6), "base": round(base, 6),
            "components": components, "penalties": penalties, "reasons": reasons}


# ---------------------------------------------------------------------------
# Step 3+: build the tiered universe
# ---------------------------------------------------------------------------

@dataclass
class ScoredMarket:
    record: MarketRecord
    score: float
    tier: str
    reasons: list

    def to_dict(self) -> dict:
        return {
            "market_id": self.record.market_id,
            "question": self.record.question[:140],
            "group_key": self.record.group_key,
            "category": self.record.category,
            "score": round(self.score, 4),
            "tier": self.tier,
            "yes_price": self.record.yes_price,
            "liquidity_usd": round(self.record.liquidity_usd, 2),
            "volume_24h_usd": round(self.record.volume_24h_usd, 2),
            "spread": round(self.record.spread, 4),
            "clob_token_ids": self.record.clob_token_ids,
            "reasons": self.reasons,
        }


@dataclass
class UniverseSnapshot:
    generated_ts: float
    scanned: int
    passed_filters: int
    rejected_by_reason: dict
    scored: list                # list[ScoredMarket], ranked desc
    cfg: UniverseConfig
    paper: bool = True
    live_subscribe_enabled: bool = False

    def tier(self, name: str) -> list:
        return [s for s in self.scored if s.tier == name]

    def live_token_ids(self) -> list:
        """Token ids for Tier A + Tier B markets, deduped.

        The number of *markets* subscribed is capped at the live-watchlist upper
        bound (Tier A size + Tier B size, never more than MAX_LIVE_WATCHLIST)."""
        market_cap = min(self.cfg.trade_candidate_limit + self.cfg.live_watchlist_limit,
                         MAX_LIVE_WATCHLIST)
        out: list[str] = []
        seen: set[str] = set()
        markets_used = 0
        for s in self.scored:
            if s.tier not in ("A", "B"):
                continue
            if markets_used >= market_cap:
                break
            markets_used += 1
            for t in s.record.clob_token_ids:
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    def trade_candidate_ids(self) -> list:
        return [s.record.market_id for s in self.tier("A")]

    def to_status(self, open_polymarket_trades: int = 0) -> dict:
        a, b, c, d = (self.tier("A"), self.tier("B"), self.tier("C"), self.tier("D"))
        top = [s.to_dict() for s in self.scored[:10]]
        target_token_ids = len(self.live_token_ids())
        # actual subscriptions are 0 unless the CLOB feed is enabled upstream
        actual_subs = target_token_ids if self.live_subscribe_enabled else 0
        return {
            "generated_ts": self.generated_ts,
            "generated_utc": datetime.fromtimestamp(self.generated_ts, timezone.utc)
                                      .strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "paper" if self.paper else "live",
            "live_subscribe_enabled": self.live_subscribe_enabled,
            "total_markets_scanned": self.scanned,
            "markets_passing_filters": self.passed_filters,
            "rejected_total": sum(self.rejected_by_reason.values()),
            "rejected_by_reason": self.rejected_by_reason,
            "tier_a_count": len(a),
            "tier_b_count": len(b),
            "tier_c_count": len(c),
            "tier_d_count": len(d),
            "live_websocket_subscriptions": actual_subs,
            "live_watchlist_target_token_ids": target_token_ids,
            "trade_candidates": len(a),
            "max_open_trades": self.cfg.effective_max_open_trades(self.paper),
            "open_polymarket_trades": open_polymarket_trades,
            "top_markets": top,
            "config": self.cfg.as_dict(),
        }


def build_universe(raw_markets: Iterable[dict], cfg: Optional[UniverseConfig] = None,
                   open_event_groups: Optional[set] = None, paper: bool = True,
                   live_subscribe_enabled: bool = False,
                   now: Optional[float] = None) -> UniverseSnapshot:
    cfg = cfg or UniverseConfig()
    now = now or time.time()
    open_event_groups = set(open_event_groups or set())

    raw_list = list(raw_markets)[: cfg.scan_limit]
    rejected: dict[str, int] = {}
    kept: list[MarketRecord] = []
    for raw in raw_list:
        ok, reason = passes_filters(raw, cfg, now=now)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        kept.append(MarketRecord.from_raw(raw, now=now))

    category_counts: dict[str, int] = {}
    for rec in kept:
        category_counts[rec.category] = category_counts.get(rec.category, 0) + 1

    scored: list[ScoredMarket] = []
    for rec in kept:
        res = score_market(rec, cfg, category_counts=category_counts,
                           open_event_groups=open_event_groups, now=now)
        scored.append(ScoredMarket(record=rec, score=res["score"], tier="D",
                                   reasons=res["reasons"]))

    scored.sort(key=lambda s: s.score, reverse=True)

    a_end = cfg.trade_candidate_limit
    b_end = cfg.trade_candidate_limit + cfg.live_watchlist_limit
    c_end = cfg.shortlist_limit + MAX_SHORTLIST
    for i, s in enumerate(scored):
        if i < a_end:
            s.tier = "A"
        elif i < b_end:
            s.tier = "B"
        elif i < c_end:
            s.tier = "C"
        else:
            s.tier = "D"

    return UniverseSnapshot(
        generated_ts=now, scanned=len(raw_list), passed_filters=len(kept),
        rejected_by_reason=rejected, scored=scored, cfg=cfg, paper=paper,
        live_subscribe_enabled=live_subscribe_enabled)


# ---------------------------------------------------------------------------
# Step 5: trade eligibility (selection only — does NOT place orders)
# ---------------------------------------------------------------------------

def select_trade_candidates(snapshot: UniverseSnapshot, open_event_groups: Optional[set] = None,
                            open_trades_count: int = 0, paper: bool = True) -> list:
    """Return Tier-A markets eligible to be *considered* for a trade.

    Enforces: Tier A only, max open trades, and one trade per correlated event
    group. Model edge / depth-for-size / RiskEngine approval remain the caller's
    responsibility — this only narrows the candidate set.
    """
    open_event_groups = set(open_event_groups or set())
    cfg = snapshot.cfg
    max_open = cfg.effective_max_open_trades(paper)
    remaining = max(0, max_open - open_trades_count)
    if remaining <= 0:
        return []
    out: list[ScoredMarket] = []
    used_groups = set(open_event_groups)
    for s in snapshot.tier("A"):
        if len(out) >= remaining:
            break
        if s.record.group_key in used_groups:
            continue  # no duplicate / correlated-event exposure
        used_groups.add(s.record.group_key)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Network fetch (ONLY function that touches the network)
# ---------------------------------------------------------------------------

_GAMMA = "https://gamma-api.polymarket.com"


def fetch_catalog(cfg: Optional[UniverseConfig] = None, client=None) -> list:
    """Fetch up to ``cfg.scan_limit`` active markets from the Gamma API.

    Paginates in pages of 100. This is the only place that performs network I/O;
    it is never called by the pure pipeline, tests, or replay.
    """
    import httpx  # local import keeps the module import-safe & offline by default

    cfg = cfg or UniverseConfig()
    own = client is None
    client = client or httpx.Client(timeout=15.0)
    out: list[dict] = []
    try:
        offset = 0
        page = 100
        while len(out) < cfg.scan_limit:
            want = min(page, cfg.scan_limit - len(out))
            r = client.get(
                f"{_GAMMA}/markets",
                params={"active": "true", "closed": "false", "archived": "false",
                        "limit": want, "offset": offset,
                        "order": "volume24hr", "ascending": "false"},
            )
            r.raise_for_status()
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < want:
                break
            offset += len(batch)
    finally:
        if own:
            client.close()
    return out[: cfg.scan_limit]


# ---------------------------------------------------------------------------
# Manager: holds config + last snapshot, computes subscription deltas
# ---------------------------------------------------------------------------

class UniverseManager:
    """Stateful orchestrator. Holds the latest snapshot and the live-subscription
    target set, and decides when a rebalance is "meaningful" enough to act on
    (to avoid reconnect storms)."""

    def __init__(self, cfg: Optional[UniverseConfig] = None, paper: bool = True,
                 live_subscribe_enabled: bool = False):
        self.cfg = cfg or UniverseConfig.from_env()
        self.paper = paper
        self.live_subscribe_enabled = live_subscribe_enabled
        self.snapshot: Optional[UniverseSnapshot] = None
        self._subscribed: set[str] = set()
        self._last_catalog_ts = 0.0

    def ingest(self, raw_markets: Iterable[dict], open_event_groups: Optional[set] = None,
               now: Optional[float] = None) -> UniverseSnapshot:
        self.snapshot = build_universe(
            raw_markets, cfg=self.cfg, open_event_groups=open_event_groups,
            paper=self.paper, live_subscribe_enabled=self.live_subscribe_enabled, now=now)
        self._last_catalog_ts = self.snapshot.generated_ts
        return self.snapshot

    def subscription_targets(self) -> list:
        if not self.snapshot or not self.live_subscribe_enabled:
            return []
        return self.snapshot.live_token_ids()

    def should_rebalance(self, new_targets: Iterable[str]) -> bool:
        new = set(new_targets)
        if not self._subscribed:
            return bool(new)
        union = self._subscribed | new
        if not union:
            return False
        churn = len(self._subscribed ^ new) / len(union)
        return churn >= self.cfg.rebalance_jaccard_threshold

    def apply_subscription(self, new_targets: Iterable[str]) -> dict:
        """Record a (would-be) subscription rebalance. Returns the add/remove delta.
        Actual WebSocket wiring stays in the market-data layer and gated by
        ``POLYMARKET_CLOB_ENABLED``; this only tracks intent to avoid storms."""
        new = set(new_targets)
        add = sorted(new - self._subscribed)
        remove = sorted(self._subscribed - new)
        self._subscribed = new
        return {"add": add, "remove": remove, "total": len(new)}

    def status(self, open_polymarket_trades: int = 0) -> dict:
        if not self.snapshot:
            return {
                "available": False,
                "reason": "no scan yet — run scripts/scan_polymarket_universe.py "
                          "or enable the engine universe loop",
                "config": self.cfg.as_dict(),
                "live_subscribe_enabled": self.live_subscribe_enabled,
                "max_open_trades": self.cfg.effective_max_open_trades(self.paper),
            }
        st = self.snapshot.to_status(open_polymarket_trades=open_polymarket_trades)
        st["available"] = True
        st["currently_subscribed"] = len(self._subscribed)
        return st


# ---------------------------------------------------------------------------
# Snapshot persistence (so the dashboard never triggers a network scan)
# ---------------------------------------------------------------------------

def save_status(path, status: dict) -> None:
    import pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, default=str), encoding="utf-8")


def load_status(path) -> Optional[dict]:
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
