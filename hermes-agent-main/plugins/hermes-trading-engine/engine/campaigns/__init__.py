"""Controlled PAPER-trading campaigns.

A campaign orchestrates the existing market-universe selection + a fully
isolated *paper* fill simulator and ledger to measure edge, slippage, fills,
positions, and P&L over time. It NEVER places real orders, enables Micro Live,
or touches a production execution path; a preflight safety check aborts (with a
red warning) if any live-trading config is detected.
"""

from .paper_campaign import (  # noqa: F401
    CampaignConfig,
    PaperCampaign,
    PaperFillSimulator,
    SimulatedSignalModel,
    CampaignRiskGate,
    preflight_check,
)
