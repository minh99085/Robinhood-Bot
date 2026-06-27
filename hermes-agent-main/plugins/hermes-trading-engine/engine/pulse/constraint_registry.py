"""Machine-checkable LCMM constraints for dependency arbitrage (Roan Layer 1).

LLM-proposed constraints must map to registered types before any trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ConstraintSpec:
    constraint_type: str
    parent_key: str
    child_keys: list
    description: str


REGISTERED_TYPES = frozenset({
    "nested_implication",
    "sum_to_one",
    "mutex",
})


def validate_constraint_type(constraint_type: str) -> bool:
    return str(constraint_type or "") in REGISTERED_TYPES


def nested_implication_violation(
    parent_up: float,
    child_up: float,
    *,
    epsilon: float = 0.02,
) -> Optional[dict]:
    """P(up_parent) >= P(up_child) for nested windows."""
    if parent_up is None or child_up is None:
        return None
    mag = float(child_up) - float(parent_up)
    if mag <= float(epsilon):
        return None
    return {
        "constraint_type": "nested_implication",
        "violation_magnitude": round(mag, 6),
        "parent_up": round(float(parent_up), 6),
        "child_up": round(float(child_up), 6),
    }