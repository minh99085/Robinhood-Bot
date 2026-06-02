"""Grok output must validate to a safe action: invalid -> WAIT, never a trade."""

from __future__ import annotations

import types

from engine.brain import GrokBrain
from engine.schemas import GrokAction, parse_grok_action


def test_parse_grok_action_invalid_json_forces_wait():
    assert parse_grok_action("{not valid json").action == "WAIT"
    assert parse_grok_action("").action == "WAIT"
    assert parse_grok_action(None).action == "WAIT"
    assert parse_grok_action(12345).action == "WAIT"


def test_parse_grok_action_invalid_fields_force_wait():
    act = parse_grok_action({"action": "MOON", "confidence": "high"})
    assert act.action == "WAIT"
    assert act.confidence == 0.0


def test_low_confidence_buy_demoted_to_wait():
    act = parse_grok_action({"action": "BUY", "confidence": 0.2})
    assert act.action == "WAIT"  # below the 0.4 floor


def test_valid_high_confidence_action_survives():
    act = parse_grok_action({"action": "buy", "confidence": 0.9, "reasoning": "x"})
    assert act.action == "BUY"
    assert 0.0 <= act.confidence <= 1.0


def test_grok_action_size_is_clamped_and_advisory():
    act = GrokAction.safe_parse({"action": "BUY", "confidence": 0.9, "suggestedSizePct": 9999})
    assert act.suggestedSizePct == 100.0  # clamped; advisory only, never sizes an order


def test_brain_coerce_action_invalid_becomes_wait(tmp_path):
    settings = types.SimpleNamespace(data_dir=str(tmp_path), stance="cautious")
    brain = GrokBrain(settings)
    assert brain.enabled is False  # no creds in test env
    out = brain._coerce_action({"action": "GARBAGE", "confidence": "nope"})
    assert out["action"] == "WAIT"
    # A failed/empty call (None) still returns None so callers use the quant path.
    assert brain._coerce_action(None) is None


def test_brain_default_model_is_grok_43(tmp_path):
    settings = types.SimpleNamespace(data_dir=str(tmp_path), stance="cautious")
    brain = GrokBrain(settings)
    assert brain.model == "grok-4.3"
