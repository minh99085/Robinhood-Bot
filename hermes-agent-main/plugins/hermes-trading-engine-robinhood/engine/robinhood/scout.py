"""Scout — sweep the liquid universe with real Robinhood data and surface
the best-set-up candidates to chart this week (bullish + inverse-ETF
downside), using a multi-factor cross-sectional model.

Data path: schema-verified ``get_equity_historicals`` only (symbols ≤10
per call), ~150 calendar days of daily bars. Fetch → per-symbol factors
(scout_factors) → cross-sectional composite rank. A liquidity screen drops
thin names; the leveraged blacklist is never scanned.

Scanning the LITERAL whole market (thousands) server-side is a separate,
future path (Robinhood's run_scan), wired only once its input schema is
verified — the strict MCP validator makes an unverified scanner call a
non-starter. This client-side sweep covers the liquid, tradeable universe,
which is what a $10k account can actually act on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from engine.chart_vision.mcp_validator import _closes_from_historicals
from engine.robinhood.scout_factors import (
    MIN_ABS_MOM63,
    compute_factors,
    rank_multifactor,
)
from engine.robinhood.scout_universe import (  # re-exported for callers/tests
    INVERSE_ETFS,
    LEVERAGED_BLACKLIST,
    SCOUT_UNIVERSE,
)

_UNDERLYING_TO_INVERSE = {v: k for k, v in INVERSE_ETFS.items()}

# 20-day average dollar volume floor: below this, spreads eat the trade.
MIN_DOLLAR_VOLUME = 2_000_000.0


def rank_candidates(
    closes_by_symbol: Dict[str, List[float]],
    *,
    top_n: int = 8,
    dollar_volume_series: Optional[Dict[str, List[float]]] = None,
    dollar_volumes: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Compute factors per symbol (dropping illiquid / leveraged) and rank."""
    factors: Dict[str, Dict[str, Any]] = {}
    illiquid = 0
    for sym, closes in closes_by_symbol.items():
        if sym in LEVERAGED_BLACKLIST:
            continue
        dvs = (dollar_volume_series or {}).get(sym)
        # liquidity screen: prefer the 20d mean of the series; fall back to a
        # supplied scalar (tests / partial data).
        dv_mean = None
        if dvs:
            tail = dvs[-20:]
            dv_mean = sum(tail) / len(tail) if tail else None
        elif dollar_volumes is not None:
            dv_mean = dollar_volumes.get(sym)
        if dv_mean is not None and dv_mean < MIN_DOLLAR_VOLUME:
            illiquid += 1
            continue
        f = compute_factors(closes, dvs)
        if f is not None:
            factors[sym] = f

    ranked = rank_multifactor(
        factors, inverse_map=INVERSE_ETFS,
        underlying_to_inverse=_UNDERLYING_TO_INVERSE, top_n=top_n)
    ranked["scanned"] = len(closes_by_symbol)
    ranked["illiquid_filtered"] = illiquid
    return ranked


def _series_from_historicals(payload: Any) -> Dict[str, List[float]]:
    """Extract {closes, dollar_volume} series from one symbol's payload."""
    closes = _closes_from_historicals(payload)
    rows = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("text"), str):
            try:
                import json
                payload = json.loads(payload["text"])
            except Exception:  # noqa: BLE001
                pass
        for key in ("historicals", "data", "results", "candles", "bars"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    dvol: List[float] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                v = c = None
                for k in ("volume", "v"):
                    if k in row:
                        try:
                            v = float(row[k])
                        except (TypeError, ValueError):
                            v = None
                        break
                for k in ("close_price", "close", "c", "price"):
                    if k in row:
                        try:
                            c = float(row[k])
                        except (TypeError, ValueError):
                            c = None
                        break
                if v is not None and c is not None:
                    dvol.append(v * c)
    return {"closes": closes, "dollar_volume": dvol}


async def run_scout(client: Any, *, top_n: int = 8,
                    universe: List[str] | None = None) -> Dict[str, Any]:
    """Fetch ~5 months of daily bars for the universe (batched ≤10 symbols
    per call) and rank by multi-factor composite."""
    from datetime import datetime, timedelta, timezone

    names = list(universe or SCOUT_UNIVERSE)
    start_time = (
        datetime.now(timezone.utc) - timedelta(days=150)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    closes_by_symbol: Dict[str, List[float]] = {}
    dvol_by_symbol: Dict[str, List[float]] = {}
    errors: List[str] = []
    for i in range(0, len(names), 10):
        batch = names[i: i + 10]
        try:
            reply = await client.call_tool(
                "get_equity_historicals",
                {"symbols": batch, "start_time": start_time,
                 "interval": "day"},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{','.join(batch)}: {exc}")
            continue
        for sym, payload in _split_by_symbol(reply, batch).items():
            series = _series_from_historicals(payload)
            if series["closes"]:
                closes_by_symbol[sym] = series["closes"]
                if series["dollar_volume"]:
                    dvol_by_symbol[sym] = series["dollar_volume"]

    result = rank_candidates(closes_by_symbol, top_n=top_n,
                             dollar_volume_series=dvol_by_symbol)
    result["universe_size"] = len(names)
    result["errors"] = errors[:10]
    return result


def _split_by_symbol(reply: Any, batch: List[str]) -> Dict[str, Any]:
    """Best-effort split of a multi-symbol historicals reply."""
    out: Dict[str, Any] = {}
    if isinstance(reply, dict):
        direct = {k.upper(): v for k, v in reply.items()
                  if isinstance(k, str) and k.upper() in batch}
        if direct:
            return direct
        for key in ("results", "historicals", "data"):
            if isinstance(reply.get(key), list):
                reply = reply[key]
                break
    if isinstance(reply, list):
        for item in reply:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or item.get("ticker") or "").upper()
            if sym in batch:
                out[sym] = item
        if out:
            return out
    if len(batch) == 1:
        return {batch[0]: reply}
    return out
