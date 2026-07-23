"""Scout — scan a broad universe with real Robinhood data, suggest 5–10
symbols worth charting this week.

Deliberately does NOT use Robinhood's saved-scanner tools yet (their input
schemas are unverified and the MCP validator is strict); it uses only the
schema-verified ``get_equity_historicals`` (symbols ≤10 per call). The
universe is fixed and rule-based — every sector ETF plus the most liquid
single names across sectors — so the scout cannot quietly become a
cherry-picker.

Ranking is transparent momentum/trend agreement, not a claim of edge:
    score = |21-day return| weighted by trend alignment
    alignment = EMA9/21 cross direction matches both 5d and 21d direction
Long-only account → bullish candidates rank first; strongly bearish names
are listed only as "avoid / consider selling" context.
"""

from __future__ import annotations

from typing import Any, Dict, List

from engine.chart_vision.mcp_validator import indicators_from_closes

# Broad, fixed, rule-based: 11 sector/size/geography ETFs + liquid large
# caps across sectors. Edit deliberately, never mid-analysis.
SCOUT_UNIVERSE: List[str] = [
    # market / sectors / size / geography
    "SPY", "QQQ", "IWM", "EEM",
    "XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "XLB", "XLY", "XLK", "XLRE",
    # mega/large caps across sectors
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "JPM", "BAC", "GS", "V", "MA",
    "UNH", "JNJ", "LLY", "PFE", "ABBV",
    "XOM", "CVX", "COP", "SLB",
    "CAT", "DE", "BA", "GE", "UPS",
    "WMT", "COST", "PG", "KO", "PEP", "MCD", "NKE",
    "HD", "LOW", "DIS", "NFLX",
    "AMD", "INTC", "MU", "QCOM", "CRM", "ORCL", "PLTR",
    "F", "GM", "UBER", "ABNB",
    "NEE", "DUK", "SO",
    "FCX", "NEM", "LIN",
]


def rank_candidates(
    closes_by_symbol: Dict[str, List[float]],
    *,
    top_n: int = 8,
) -> Dict[str, Any]:
    """Rank symbols by momentum + trend agreement. Pure function."""
    rows: List[Dict[str, Any]] = []
    for sym, closes in closes_by_symbol.items():
        if len(closes) < 30:
            continue
        last = closes[-1]
        ret5 = last / closes[-6] - 1.0 if len(closes) >= 6 else 0.0
        ret21 = last / closes[-22] - 1.0 if len(closes) >= 22 else 0.0
        ind = indicators_from_closes(closes)
        cross = ind.get("ema_cross")
        rsi = ind.get("rsi14")
        # alignment: cross direction agrees with both horizons' direction
        up = ret5 > 0 and ret21 > 0 and cross == "bullish"
        down = ret5 < 0 and ret21 < 0 and cross == "bearish"
        aligned = up or down
        score = abs(ret21) * (1.5 if aligned else 0.5)
        direction = "bullish" if ret21 > 0 else "bearish"
        why = (f"{'+' if ret21 >= 0 else ''}{ret21 * 100:.1f}% / 21d, "
               f"EMA cross {cross or '—'}"
               + (f", RSI {rsi:.0f}" if rsi is not None else "")
               + (", trend aligned" if aligned else ", trend mixed"))
        rows.append({
            "symbol": sym, "direction": direction, "aligned": aligned,
            "score": round(score, 5), "ret_21d": round(ret21, 4),
            "ret_5d": round(ret5, 4), "rsi14": rsi, "ema_cross": cross,
            "why": why,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    # Long-only account: chart-worthy candidates are the aligned bullish
    # names; aligned bearish names are context (avoid / exit if held).
    # Materiality floor: don't ask the operator to chart noise — the
    # 21-day move must be at least ±2%.
    bullish = [r for r in rows
               if r["direction"] == "bullish" and r["aligned"]
               and abs(r["ret_21d"]) >= 0.02]
    bearish = [r for r in rows
               if r["direction"] == "bearish" and r["aligned"]
               and abs(r["ret_21d"]) >= 0.02]
    return {
        "scanned": len(closes_by_symbol),
        "usable": len(rows),
        "suggest": bullish[:top_n],
        "avoid": bearish[: max(3, top_n // 2)],
    }


async def run_scout(client: Any, *, top_n: int = 8,
                    universe: List[str] | None = None) -> Dict[str, Any]:
    """Fetch ~5 months of daily bars for the universe (batched ≤10 symbols
    per call) and rank. ``client`` needs async call_tool."""
    from datetime import datetime, timedelta, timezone

    from engine.chart_vision.mcp_validator import _closes_from_historicals

    names = list(universe or SCOUT_UNIVERSE)
    start_time = (
        datetime.now(timezone.utc) - timedelta(days=150)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    closes_by_symbol: Dict[str, List[float]] = {}
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
            closes = _closes_from_historicals(payload)
            if closes:
                closes_by_symbol[sym] = closes

    result = rank_candidates(closes_by_symbol, top_n=top_n)
    result["errors"] = errors[:10]
    return result


def _split_by_symbol(reply: Any, batch: List[str]) -> Dict[str, Any]:
    """Best-effort split of a multi-symbol historicals reply."""
    out: Dict[str, Any] = {}
    if isinstance(reply, dict):
        # {"AAPL": {...}, "MSFT": {...}} keyed directly?
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
    # single-symbol fallback: attribute whole payload to a 1-name batch
    if len(batch) == 1:
        return {batch[0]: reply}
    return out
