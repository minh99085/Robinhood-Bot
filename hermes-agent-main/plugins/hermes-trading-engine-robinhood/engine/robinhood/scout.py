"""Scout — scan a broad universe with real Robinhood data, suggest what to
chart this week, including inverse-ETF downside ideas.

Deliberately does NOT use Robinhood's saved-scanner tools yet (their input
schemas are unverified and the MCP validator is strict); it uses only the
schema-verified ``get_equity_historicals`` (symbols ≤10 per call). The
universe is fixed and rule-based — sectors, sizes, geographies, bonds,
commodities, liquid large caps, crypto proxies, and 1x inverse ETFs — so
the scout cannot quietly become a cherry-picker.

Ranking is transparent momentum/trend agreement, not a claim of edge:
    score = |21-day return| weighted by trend alignment
    alignment = EMA9/21 cross direction matches both 5d and 21d direction
plus a liquidity screen (20-day average dollar volume) and a 2%
materiality floor. Long-only account → bullish candidates rank first;
aligned-bearish names surface as avoid/exit context, and when an aligned-
bearish index has a 1x inverse ETF, the scout emits a downside idea
("SPY falling → chart SH") — profit from drawdowns WITHOUT margin or
shorting. Leveraged inverse products are blacklisted outright: daily
rebalancing decay makes them poison for weekly/monthly holds.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from engine.chart_vision.mcp_validator import indicators_from_closes

# ---------------------------------------------------------------------------
# Universe (fixed, rule-based; edit deliberately, never mid-analysis)
# ---------------------------------------------------------------------------

# 1x inverse ETFs: the cash-account-safe downside tools (bought LONG).
INVERSE_ETFS: Dict[str, str] = {
    "SH": "SPY",     # short S&P 500
    "PSQ": "QQQ",    # short Nasdaq-100
    "DOG": "DIA",    # short Dow
    "RWM": "IWM",    # short Russell 2000
    "EUM": "EEM",    # short emerging markets
    "MYY": "MDY",    # short S&P MidCap 400
    "TBF": "TLT",    # short 20+yr Treasuries
    "BITI": "IBIT",  # short Bitcoin
}
_UNDERLYING_TO_INVERSE = {v: k for k, v in INVERSE_ETFS.items()}

# Leveraged / volatility products: never suggested, never charted. Daily
# rebalancing decay destroys multi-day holds regardless of direction.
LEVERAGED_BLACKLIST = frozenset({
    "SQQQ", "TQQQ", "SDS", "SSO", "SPXU", "UPRO", "SPXL", "SPXS",
    "SDOW", "UDOW", "TZA", "TNA", "URTY", "SRTY", "SOXL", "SOXS",
    "FAZ", "FAS", "LABU", "LABD", "NUGT", "DUST", "JNUG", "JDST",
    "YINN", "YANG", "TMF", "TMV", "BOIL", "KOLD", "UCO", "SCO",
    "UVXY", "SVXY", "VXX", "VIXY", "UVIX", "SVIX", "BITX", "ETHU",
})

_TIERS: Dict[str, List[str]] = {
    "broad": ["SPY", "QQQ", "DIA", "IWM", "MDY", "RSP", "VTI", "VUG", "VTV",
              "VOO", "VB", "VO", "QUAL", "MTUM", "USMV"],
    "sectors": ["XLE", "XLF", "XLV", "XLI", "XLP", "XLU", "XLB", "XLY",
                "XLK", "XLRE", "XLC", "SMH", "SOXX", "XBI", "IBB", "KRE",
                "KBE", "XHB", "ITB", "XOP", "OIH", "GDX", "GDXJ", "XME",
                "IYT", "JETS", "XRT", "ITA", "TAN", "ICLN", "HACK", "IGV",
                "VNQ", "IYR"],
    "geography": ["EEM", "EFA", "VEA", "VWO", "FXI", "MCHI", "KWEB", "EWZ",
                  "EWJ", "EWG", "EWU", "EWY", "EWT", "INDA", "EWW", "EWC",
                  "EWA", "EZA", "TUR", "EWP", "EWQ", "EWL", "ARGT", "ILF"],
    "bonds_commodities": ["TLT", "IEF", "SHY", "AGG", "BND", "LQD", "HYG",
                          "JNK", "TIP", "EMB", "GLD", "SLV", "GDX", "USO",
                          "UNG", "DBC", "DBA", "CPER", "PPLT", "URA", "LIT"],
    "megacap": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                "AVGO", "BRK.B", "LLY", "JPM", "V", "MA", "UNH", "XOM",
                "WMT", "JNJ", "PG", "HD", "COST", "ORCL", "NFLX", "CRM",
                "ADBE", "AMD", "QCOM", "TXN", "INTC", "MU", "AMAT", "LRCX",
                "KLAC", "PLTR", "SNOW", "CRWD", "PANW", "NOW", "INTU",
                "UBER", "ABNB", "SHOP", "SQ", "PYPL", "COIN", "HOOD"],
    "financials": ["BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP",
                   "COF", "USB", "PNC", "KEY", "MET", "PRU", "AIG", "ALL"],
    "health": ["PFE", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY", "AMGN",
               "GILD", "VRTX", "REGN", "ISRG", "MDT", "CVS", "CI", "HUM",
               "MRNA", "BIIB"],
    "energy_industrial": ["CVX", "COP", "SLB", "EOG", "OXY", "MPC", "PSX",
                          "VLO", "HAL", "DVN", "FANG", "CAT", "DE", "BA",
                          "GE", "HON", "UNP", "UPS", "FDX", "LMT", "RTX",
                          "NOC", "GD", "MMM", "EMR", "ETN", "PH"],
    "consumer": ["KO", "PEP", "MCD", "SBUX", "NKE", "LULU", "TGT", "LOW",
                 "TJX", "ROST", "DG", "DLTR", "KHC", "GIS", "K", "HSY",
                 "CL", "KMB", "EL", "DIS", "CMCSA", "T", "VZ", "TMUS",
                 "F", "GM", "RIVN", "DAL", "UAL", "AAL", "CCL", "RCL",
                 "MAR", "HLT", "MGM", "LVS", "DKNG"],
    "utilities_materials": ["NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE",
                            "XEL", "ED", "FCX", "NEM", "LIN", "APD", "SHW",
                            "NUE", "STLD", "DOW", "ALB", "MOS"],
    "crypto_proxies": ["IBIT", "ETHA", "MSTR", "MARA", "RIOT", "CLSK"],
    "inverse_1x": sorted(INVERSE_ETFS.keys()),
}

SCOUT_UNIVERSE: List[str] = sorted(
    {sym for tier in _TIERS.values() for sym in tier} - LEVERAGED_BLACKLIST
)

# Liquidity screen: 20-day average dollar volume below this is too thin to
# suggest to a retail operator (spreads eat the trade).
MIN_DOLLAR_VOLUME = 2_000_000.0
# Materiality floor: 21-day move must be at least ±2% to be chart-worthy.
MIN_ABS_RET21 = 0.02


def rank_candidates(
    closes_by_symbol: Dict[str, List[float]],
    *,
    top_n: int = 8,
    dollar_volumes: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Rank symbols by momentum + trend agreement. Pure function."""
    rows: List[Dict[str, Any]] = []
    illiquid = 0
    for sym, closes in closes_by_symbol.items():
        if sym in LEVERAGED_BLACKLIST:
            continue
        if len(closes) < 30:
            continue
        dv = (dollar_volumes or {}).get(sym)
        if dv is not None and dv < MIN_DOLLAR_VOLUME:
            illiquid += 1
            continue
        last = closes[-1]
        ret5 = last / closes[-6] - 1.0 if len(closes) >= 6 else 0.0
        ret21 = last / closes[-22] - 1.0 if len(closes) >= 22 else 0.0
        ind = indicators_from_closes(closes)
        cross = ind.get("ema_cross")
        rsi = ind.get("rsi14")
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
            "inverse_of": INVERSE_ETFS.get(sym),
            "why": why,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    bullish = [r for r in rows
               if r["direction"] == "bullish" and r["aligned"]
               and abs(r["ret_21d"]) >= MIN_ABS_RET21]
    bearish = [r for r in rows
               if r["direction"] == "bearish" and r["aligned"]
               and abs(r["ret_21d"]) >= MIN_ABS_RET21]

    # Downside ideas: an aligned-bearish underlying with a 1x inverse →
    # "chart the inverse". Profit from the drawdown as a plain long buy.
    downside: List[Dict[str, Any]] = []
    for r in bearish:
        inv = _UNDERLYING_TO_INVERSE.get(r["symbol"])
        if inv:
            downside.append({
                "inverse": inv,
                "underlying": r["symbol"],
                "why": (f"{r['symbol']} {r['ret_21d'] * 100:+.1f}% / 21d and "
                        f"trend-aligned down → {inv} (1x inverse) rises when "
                        f"it falls — chart {inv}"),
            })

    return {
        "scanned": len(closes_by_symbol),
        "usable": len(rows),
        "illiquid_filtered": illiquid,
        "suggest": bullish[:top_n],
        "avoid": bearish[: max(3, top_n // 2)],
        "downside_ideas": downside[:5],
    }


def _mean_dollar_volume(payload: Any, closes: List[float]) -> Optional[float]:
    """20-day average close×volume from a historicals payload."""
    vols: List[float] = []
    rows = payload
    if isinstance(payload, dict):
        for key in ("historicals", "data", "results", "candles", "bars"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict):
            for k in ("volume", "v"):
                if k in row:
                    try:
                        vols.append(float(row[k]))
                    except (TypeError, ValueError):
                        pass
                    break
    if not vols or not closes:
        return None
    n = min(20, len(vols), len(closes))
    return float(sum(v * c for v, c in
                     zip(vols[-n:], closes[-n:])) / n)


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
    dollar_volumes: Dict[str, float] = {}
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
                dv = _mean_dollar_volume(payload, closes)
                if dv is not None:
                    dollar_volumes[sym] = dv

    result = rank_candidates(closes_by_symbol, top_n=top_n,
                             dollar_volumes=dollar_volumes)
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
