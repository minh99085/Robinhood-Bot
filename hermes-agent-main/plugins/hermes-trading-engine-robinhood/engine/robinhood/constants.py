"""Shared tool name constants for Robinhood Trading MCP."""

PLACE_TOOLS = frozenset({"place_equity_order", "place_option_order"})
REVIEW_TOOLS = frozenset({"review_equity_order", "review_option_order"})
ORDER_TOOLS = PLACE_TOOLS | REVIEW_TOOLS

OPTIONS_READ_TOOLS = frozenset(
    {
        "get_option_chains",
        "get_option_instruments",
        "get_option_quotes",
        "get_option_positions",
        "get_option_orders",
        "get_option_level_upgrade_info",
    }
)

OPTIONS_WRITE_TOOLS = frozenset(
    {
        "review_option_order",
        "place_option_order",
        "cancel_option_order",
    }
)

OPTIONS_TOOLS = OPTIONS_READ_TOOLS | OPTIONS_WRITE_TOOLS

Bias = str  # "call" | "put" | "none"