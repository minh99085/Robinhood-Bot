"""FeeModel + SlippageModel unit tests."""

from __future__ import annotations

from decimal import Decimal

from engine.execution.fees import FeeModel
from engine.execution.slippage import SlippageModel
from engine.execution.types import LiquidityFlag, OrderSide


def test_fee_model_applies_taker_fee():
    fm = FeeModel(taker_bps=Decimal("30"), maker_bps=Decimal("10"), min_fee=Decimal("0"))
    assert fm.fee(Decimal("1000"), LiquidityFlag.TAKER) == Decimal("3.0")
    assert fm.fee(Decimal("1000"), LiquidityFlag.MAKER) == Decimal("1.0")
    # SIMULATED uses the conservative taker assumption
    assert fm.fee(Decimal("1000"), LiquidityFlag.SIMULATED) == Decimal("3.0")


def test_fee_model_honours_minimum():
    fm = FeeModel(taker_bps=Decimal("1"), maker_bps=Decimal("1"), min_fee=Decimal("0.50"))
    assert fm.fee(Decimal("10"), LiquidityFlag.TAKER) == Decimal("0.50")  # min floor


def test_slippage_model_is_conservative():
    sm = SlippageModel(slippage_bps=Decimal("25"), spread_aware=False)
    buy = sm.adjust(Decimal("100"), OrderSide.BUY)
    sell = sm.adjust(Decimal("100"), OrderSide.SELL)
    assert buy > Decimal("100")    # buyer pays up
    assert sell < Decimal("100")   # seller receives less
    assert buy == Decimal("100.25")
    assert sell == Decimal("99.75")


def test_slippage_spread_aware_adds_half_spread():
    sm = SlippageModel(slippage_bps=Decimal("0"), spread_aware=True)
    buy = sm.adjust(Decimal("100"), OrderSide.BUY, spread=Decimal("0.10"))
    assert buy == Decimal("100.05")  # + half of 0.10


def test_slippage_never_negative():
    sm = SlippageModel(slippage_bps=Decimal("100000"), spread_aware=False)
    assert sm.adjust(Decimal("0.10"), OrderSide.SELL) >= Decimal("0")
