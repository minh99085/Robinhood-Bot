"""Profit-discovery queue + contextual-bandit learning router (PAPER ONLY).

Turns Bregman near-misses into a prioritized LEARNING plan — durable shadow labels,
Grok advisory tasks, book-refresh / sibling-search tasks — and a minimal
contextual-bandit that decides WHERE to spend scan/learning budget. It allocates
*learning effort only*: it never trades, sizes, lowers a gate, or bypasses the
certifier / paper-realism. Pure + deterministic (UCB1, no RNG).
"""

from __future__ import annotations

import math
from typing import Optional

# --- profit-discovery queue priorities (1 = highest learning value) --------- #
P_NOT_EXHAUSTIVE_POS_EDGE = 1   # incomplete family, positive raw edge, depth+fresh OK
P_DEPTH_ONLY = 2                # depth-only one-fix-away
P_BINARY_BASKET_CLOSE = 3       # binary YES/NO ask basket close to profitable
P_GROK_NEWS_LINKED = 4          # Grok/news-linked near-miss
P_STALE_INVALID = 5            # stale / invalid / malformed diagnostic

_PRIORITY_ACTION = {
    P_NOT_EXHAUSTIVE_POS_EDGE: "catalog_sibling_search",  # find the missing legs
    P_DEPTH_ONLY: "book_refresh_task",
    P_BINARY_BASKET_CLOSE: "shadow_label",
    P_GROK_NEWS_LINKED: "grok_advisory_task",
    P_STALE_INVALID: "diagnostic_only_reject",
}


def _depth_ok(nm: dict) -> bool:
    return int((nm.get("depth_quality", {}) or {}).get("thin_legs", 1)) == 0


def _fresh_ok(nm: dict) -> bool:
    return int((nm.get("freshness", {}) or {}).get("stale_legs", 1)) == 0


def _grok_relevance(nm: dict) -> float:
    return float((nm.get("advisory_features", {}) or {}).get("grok_news_relevance_score", 0.0) or 0.0)


def classify_priority(nm: dict) -> int:
    """Assign a near-miss to a profit-discovery priority tier (read-only)."""
    reason = nm.get("reject_reason", "")
    alb = nm.get("after_cost_lower_bound")
    pos_edge = alb is not None and alb > 0
    if reason in ("not_exhaustive", "not_mutually_exclusive") and pos_edge \
            and _depth_ok(nm) and _fresh_ok(nm):
        return P_NOT_EXHAUSTIVE_POS_EDGE
    if nm.get("one_fix_away") and nm.get("primary_fix") == "depth":
        return P_DEPTH_ONLY
    if nm.get("group_type") == "binary_yes_no" and alb is not None and alb > -0.02:
        return P_BINARY_BASKET_CLOSE
    if _grok_relevance(nm) > 0:
        return P_GROK_NEWS_LINKED
    return P_STALE_INVALID


def queued_action(priority: int) -> str:
    return _PRIORITY_ACTION.get(priority, "diagnostic_only_reject")


def build_profit_discovery_queue(near_misses: list, *, sample_n: int = 15) -> dict:
    """Build the prioritized profit-discovery queue from near-misses (read-only).

    A near-miss yields a durable shadow label, a Grok advisory task, a book-refresh
    task, a catalog-sibling search, or a diagnostic-only reject — but a STRICT
    candidate ONLY if all gates pass (which is decided by the certifier, not here).
    Never executes."""
    items: list = []
    by_priority: dict = {}
    for nm in (near_misses or []):
        p = classify_priority(nm)
        by_priority[p] = by_priority.get(p, 0) + 1
        items.append({
            "group_key": nm.get("group_key"),
            "group_type": nm.get("group_type"),
            "priority": p, "action": queued_action(p),
            "reject_reason": nm.get("reject_reason"),
            "learning_label": nm.get("learning_label"),
            "shadow_label_candidate": bool(nm.get("shadow_label_candidate")),
            "after_cost_lower_bound": nm.get("after_cost_lower_bound"),
            "executed": False, "advisory_only": True,
        })
    items.sort(key=lambda it: (it["priority"],
                               -(it.get("after_cost_lower_bound") or -1e9)))
    return {
        "profit_discovery_queue_items": len(items),
        "profit_discovery_queue_by_priority": {str(k): by_priority[k]
                                               for k in sorted(by_priority)},
        "profit_discovery_queue_actions": _action_counts(items),
        "profit_discovery_queue_sample": items[:max(0, int(sample_n))],
    }


def _action_counts(items: list) -> dict:
    out: dict = {}
    for it in items:
        out[it["action"]] = out.get(it["action"], 0) + 1
    return out


# --- contextual-bandit learning router (UCB1; allocates LEARNING budget) ----- #
BANDIT_ACTIONS = (
    "bregman_depth_watchlist", "bregman_not_exhaustive_completer",
    "bregman_rebalancing_watchlist", "grok_news_linked_near_miss",
    "active_learning_shadow",
)

# reward schedule (LEARNING value only — never trade PnL):
REWARD_DEPTH_SUFFICIENT_POSITIVE = 3.0
REWARD_SHADOW_LABEL_WRITTEN = 2.0
REWARD_USEFUL_GROK = 1.0
REWARD_ORDINARY_NO_TRADE = 0.0
REWARD_WASTE = -1.0


class ProfitDiscoveryBandit:
    """Deterministic UCB1 router over LEARNING sources (never trades/sizes/gates).

    ``select()`` picks which learning source to spend budget on; ``update()`` records
    the realized LEARNING reward. It has no order/size/gate surface at all."""

    def __init__(self, *, enabled: bool = True, actions=BANDIT_ACTIONS):
        self.enabled = bool(enabled)
        self.actions = tuple(actions)
        self.counts = {a: 0 for a in self.actions}
        self.rewards = {a: 0.0 for a in self.actions}
        self.total = 0
        self.last_selected: Optional[str] = None
        # hard invariant — a learning router can NEVER act on the market
        self.can_execute = False
        self.can_size = False
        self.can_override_gates = False

    def select(self, available: Optional[list] = None) -> Optional[str]:
        if not self.enabled:
            return None
        pool = [a for a in self.actions if (available is None or a in available)]
        if not pool:
            return None
        # UCB1: play any unplayed action first (deterministic order), else argmax.
        for a in pool:
            if self.counts[a] == 0:
                self.last_selected = a
                return a
        t = max(1, self.total)

        def ucb(a):
            mean = self.rewards[a] / max(1, self.counts[a])
            return mean + math.sqrt(2.0 * math.log(t) / self.counts[a])
        self.last_selected = max(pool, key=ucb)
        return self.last_selected

    def update(self, action: str, reward: float) -> None:
        if action not in self.counts:
            return
        self.counts[action] += 1
        self.rewards[action] = round(self.rewards[action] + float(reward), 6)
        self.total += 1

    @staticmethod
    def reward_for(action: str, *, near_misses: list, shadow_written: int,
                   grok_analyzed: int) -> float:
        """Realized LEARNING reward for an action from this tick's outcomes."""
        if shadow_written > 0 and action == "active_learning_shadow":
            return REWARD_SHADOW_LABEL_WRITTEN
        if action == "grok_news_linked_near_miss":
            return REWARD_USEFUL_GROK if grok_analyzed > 0 else REWARD_WASTE
        depth_pos = sum(1 for nm in (near_misses or [])
                        if int((nm.get("depth_quality", {}) or {}).get("thin_legs", 1)) == 0
                        and (nm.get("after_cost_lower_bound") or -1) > 0)
        if depth_pos > 0 and action in ("bregman_depth_watchlist",
                                        "bregman_not_exhaustive_completer",
                                        "bregman_rebalancing_watchlist"):
            return REWARD_DEPTH_SUFFICIENT_POSITIVE
        waste = sum(1 for nm in (near_misses or [])
                    if nm.get("reject_reason") in ("stale_book", "invalid_simplex",
                                                   "malformed_group"))
        if waste > 0 and not near_misses:
            return REWARD_WASTE
        return REWARD_ORDINARY_NO_TRADE

    def status(self) -> dict:
        return {
            "bandit_router_enabled": self.enabled,
            "bandit_action_counts": dict(self.counts),
            "bandit_action_rewards": dict(self.rewards),
            "bandit_selected_action": self.last_selected,
            "bandit_no_gate_override": True,        # hard invariant
            "bandit_can_execute": False, "bandit_can_size": False,
        }
