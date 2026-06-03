"""BTC Pulse can never place a live order or reach a wallet/order-submission path."""

from __future__ import annotations

import inspect

from engine.training import btc_pulse as bp
from engine.training.btc_pulse import BtcPulsePaperTrainer
from engine.training.config import TrainingConfig


def test_module_has_no_order_submission_imports():
    src = inspect.getsource(bp).lower()
    # The isolated pulse module must not import or call any live order /
    # wallet / OMS / guarded-live / micro-live submission path.
    for forbidden in ("submit_order", "place_order(", ".submit(",
                      "from engine.execution", "import wallet", "wallet_address",
                      "send_transaction", "guarded_live", "micro_live", "oms("):
        assert forbidden not in src, forbidden


def test_safety_marks_no_wallet_or_submission_path():
    t = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True))
    checks = t.safety["checks"]
    assert checks["no_wallet_access"] is True
    assert checks["no_order_submission_path"] is True
    assert checks["live_disabled"] is True


def test_live_enabled_never_trades():
    cfg = TrainingConfig(btc_pulse_enabled=True, btc_pulse_live_enabled=True)
    t = BtcPulsePaperTrainer(cfg, clock=lambda: 1, price_fn=lambda: 100000.0)
    assert t.frozen is True
    for i in range(50):
        t.tick(now_ms=i)
    assert t.paper_trades == 0
    assert t.status()["live_enabled"] is False


def test_status_never_advertises_live():
    st = BtcPulsePaperTrainer(TrainingConfig(btc_pulse_enabled=True)).status()
    assert st["live_enabled"] is False
    assert st["legacy_autotrade_enabled"] is False
