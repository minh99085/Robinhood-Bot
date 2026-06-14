"""docker-compose.yml must be valid YAML with NO duplicate mapping keys.

Regression for the 100X patch that duplicated env keys in the hermes-training service
(``mapping key "POLYMARKET_EXPLORATION_RATE" already defined``), which made
``docker compose config -q`` / ``docker compose build`` fail to parse. This test is
CI-safe (pure Python duplicate-key detection); it ALSO runs ``docker compose config -q``
when the docker CLI is available. PAPER ONLY — it asserts no change weakens live safety.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"

# The 100X paper-training env vars that must each appear EXACTLY ONCE with these final
# effective default values on the hermes-training service.
REQUIRED_100X = {
    "AGGRESSIVE_PAPER_TRAINING": "1",
    "HERMES_ACCELERATED_DISCOVERY": "1",
    "FEEDBACK_ACCELERATOR_ENABLED": "1",
    "FEEDBACK_ACCELERATOR_TARGET_MULTIPLIER": "100",
    "POLYMARKET_ACTIVE_LEARNING_ENABLED": "1",
    "POLYMARKET_EXPLORATION_ENABLED": "1",
    "EXPLORATION_TINY_SIZE_ENABLED": "1",
}

# Live / real-money flags that must stay disabled (=0) on the training service.
LIVE_OFF = ("MICRO_LIVE_ENABLED", "GUARDED_LIVE_ENABLED",
            "PRODUCTION_REVIEW_ENABLE_PRODUCTION_EXECUTION")


class _NoDupLoader(yaml.SafeLoader):
    """SafeLoader that RAISES on duplicate mapping keys (docker compose rejects them)."""


def _no_dup_mapping(loader, node, deep=False):
    seen = set()
    dups = []
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            dups.append(key)
        seen.add(key)
    if dups:
        raise yaml.constructor.ConstructorError(
            None, None, f"duplicate mapping key(s): {sorted(set(dups))}", node.start_mark)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDupLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def _load_compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_NoDupLoader)


def _resolve_default(value: str) -> str:
    """Resolve a compose ``${VAR:-default}`` interpolation to its default; else literal."""
    m = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.*)\}", str(value))
    return m.group(1) if m else str(value)


def test_compose_has_no_duplicate_keys_and_parses():
    # raises ConstructorError if any mapping (e.g. an env block) has a duplicate key
    data = _load_compose()
    assert isinstance(data, dict) and "services" in data
    assert "hermes-training" in data["services"]


def test_hermes_training_has_100x_env_once_with_final_values():
    env = _load_compose()["services"]["hermes-training"]["environment"]
    assert isinstance(env, dict)                       # mapping form (dup keys impossible)
    for key, expected in REQUIRED_100X.items():
        assert key in env, f"missing 100X env var: {key}"
        assert _resolve_default(env[key]) == expected, f"{key} -> {env[key]}"


def test_live_trading_remains_disabled_in_compose():
    env = _load_compose()["services"]["hermes-training"]["environment"]
    for flag in LIVE_OFF:
        assert _resolve_default(env.get(flag, "0")) == "0", f"{flag} must be 0"


def test_no_duplicate_env_keys_in_raw_text_per_service():
    """Belt-and-suspenders: within each service's environment block, no env key appears
    twice in the raw text (catches a duplicate even before YAML construction)."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    # crude but effective: scan each `environment:` block and collect KEY: occurrences
    in_env = False
    env_indent = None
    keys: list = []
    blocks: list = []
    for ln in lines:
        stripped = ln.strip()
        if stripped == "environment:":
            if keys:
                blocks.append(keys)
            keys = []
            in_env = True
            env_indent = len(ln) - len(ln.lstrip())
            continue
        if in_env:
            indent = len(ln) - len(ln.lstrip())
            if stripped and not stripped.startswith("#") and indent <= env_indent:
                in_env = False
                blocks.append(keys)
                keys = []
                continue
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*):\s", ln)
            if m and not stripped.startswith("#"):
                keys.append(m.group(1))
    if keys:
        blocks.append(keys)
    for block in blocks:
        dups = sorted({k for k in block if block.count(k) > 1})
        assert not dups, f"duplicate env keys in an environment block: {dups}"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
def test_docker_compose_config_validates():
    """When docker is present, `docker compose config -q` must pass (no parse error)."""
    proc = subprocess.run(["docker", "compose", "config", "-q"],
                          cwd=str(COMPOSE.parent), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
