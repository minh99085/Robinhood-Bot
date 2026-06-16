"""Fix 3: read-only feed health surfaces EXACT chainlink/btc valid/stale/disabled
reasons (with age), never leaks secrets, and never affects live trading."""

from __future__ import annotations

from engine.training import PolymarketPaperTrainer, TrainingConfig

from tests._pmtrain_helpers import clean_live_env


def _trainer(tmp_path, monkeypatch, **cfg):
    clean_live_env(monkeypatch, tmp_path)
    return PolymarketPaperTrainer(TrainingConfig(mode="paper_train", **cfg),
                                  data_dir=tmp_path)


def test_chainlink_stale_reason_is_exact_with_age(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(t, "chainlink_oracle_status", lambda: {
        "enabled": True, "initialized": True, "valid": False, "stale": True,
        "age_seconds": 351.211, "max_age_seconds": 180, "error": "stale"})
    fh = t.status()["feeds_health"]
    assert fh["chainlink_valid"] is False
    assert fh["chainlink_age_seconds"] == 351.211
    assert "351.2s" in fh["chainlink_stale_reason"]
    assert "max_age 180s" in fh["chainlink_stale_reason"]
    assert fh["secrets_leaked"] is False and fh["affects_live_trading"] is False


def test_btc_fast_price_disabled_reason_is_explicit(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    t.btc_fast_price = None                       # feed off (read-only spot disabled)
    fh = t.status()["feeds_health"]
    assert fh["btc_fast_price_enabled"] is False
    assert fh["btc_fast_price_valid"] is False
    assert "btc_fast_price_disabled" in fh["btc_fast_price_disabled_reason"]


def test_valid_feeds_have_empty_reason(tmp_path, monkeypatch):
    t = _trainer(tmp_path, monkeypatch)
    monkeypatch.setattr(t, "chainlink_oracle_status", lambda: {
        "enabled": True, "initialized": True, "valid": True, "stale": False,
        "age_seconds": 5.0, "max_age_seconds": 180})

    class _Fast:
        def status(self):
            return {"enabled": True, "valid": True, "stale": False, "age_seconds": 1.0}
    t.btc_fast_price = _Fast()
    fh = t.status()["feeds_health"]
    assert fh["chainlink_valid"] is True and fh["chainlink_stale_reason"] == ""
    assert fh["btc_fast_price_valid"] is True
    assert fh["btc_fast_price_disabled_reason"] == ""
