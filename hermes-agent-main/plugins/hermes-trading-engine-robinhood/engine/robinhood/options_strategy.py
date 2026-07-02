"""Manual directional-bias options strategy (long calls / long puts only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engine.robinhood.config import OptionBias, RobinhoodConfig
from engine.robinhood.options_market import (
    OptionContract,
    OptionQuote,
    SymbolMarketSnapshot,
)


@dataclass(frozen=True)
class OrderIntent:
    """Normalized long-option order the loop may send to Robinhood MCP."""

    symbol: str
    option_type: str
    instrument_id: str
    quantity: int
    limit_price: float
    premium_usd: float
    expiration_date: str
    strike: float
    bias: OptionBias
    reason: str

    def to_mcp_args(self) -> dict[str, Any]:
        # Robinhood MCP schemas vary by rollout — include common aliases.
        return {
            "symbol": self.symbol,
            "chain_symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "option_instrument_id": self.instrument_id,
            "option_id": self.instrument_id,
            "quantity": self.quantity,
            "qty": self.quantity,
            "side": "buy",
            "order_type": "limit",
            "type": "limit",
            "limit_price": self.limit_price,
            "price": self.limit_price,
            "time_in_force": "gfd",
        }


def _pick_contract(
    snapshot: SymbolMarketSnapshot,
    bias: OptionBias,
    spot: float,
) -> OptionContract | None:
    if bias == "none":
        return None
    want = "call" if bias == "call" else "put"
    typed = [c for c in snapshot.contracts if c.option_type == want]
    if not typed:
        return None
    if bias == "call":
        # Nearest OTM call at or above spot.
        above = [c for c in typed if c.strike >= spot]
        return min(above, key=lambda c: (c.strike - spot, c.dte or 9999)) if above else None
    # Nearest OTM put at or below spot.
    below = [c for c in typed if c.strike <= spot]
    return min(below, key=lambda c: (spot - c.strike, c.dte or 9999)) if below else None


def _limit_price(quote: OptionQuote, *, aggressive: bool = False) -> float | None:
    if aggressive and quote.ask is not None:
        return round(quote.ask, 2)
    if quote.mid is not None:
        return round(quote.mid, 2)
    if quote.bid is not None and quote.ask is not None:
        return round((quote.bid + quote.ask) / 2.0, 2)
    return None


def decide_order(
    snapshot: SymbolMarketSnapshot,
    config: RobinhoodConfig,
    bias: OptionBias,
) -> tuple[OrderIntent | None, str]:
    """Return (intent, stage_reason). stage_reason explains skips for the funnel."""
    if bias == "none":
        return None, "bias_none"
    contract = _pick_contract(snapshot, bias, snapshot.spot.last_price)
    if not contract:
        return None, "no_contract_in_band"
    quote = snapshot.quotes.get(contract.instrument_id)
    if not quote:
        return None, "no_quote"
    if quote.spread_pct is not None and quote.spread_pct > config.options_max_spread_pct:
        return None, f"spread_too_wide_{quote.spread_pct:.1f}pct"
    limit = _limit_price(quote)
    if limit is None or limit <= 0:
        return None, "no_limit_price"
    qty = min(config.options_contracts, config.options_max_contracts)
    premium = limit * qty * 100.0
    if premium > config.options_max_premium_usd:
        return None, f"premium_too_high_{premium:.0f}"
    intent = OrderIntent(
        symbol=snapshot.symbol,
        option_type=contract.option_type,
        instrument_id=contract.instrument_id,
        quantity=qty,
        limit_price=limit,
        premium_usd=premium,
        expiration_date=contract.expiration_date,
        strike=contract.strike,
        bias=bias,
        reason=f"manual_{bias}_otm",
    )
    return intent, "intent_ready"
