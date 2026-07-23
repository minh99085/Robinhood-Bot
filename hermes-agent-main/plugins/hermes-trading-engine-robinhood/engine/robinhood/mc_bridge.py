"""Monte-Carlo-Sim → Robinhood bridge (paper mode, review-only pipeline).

Reads TRADE/NO_TRADE verdict JSON files produced by Monte-Carlo-Sim
(``run_weekly_from_tv.py`` / ``paper_train.py`` write them to
``outputs/verdicts`` and ``outputs/paper_verdicts``), maps each fresh TRADE
into a ``place_equity_order``-shaped argument dict, runs it through this
plugin's :class:`~engine.robinhood.safety_gates.RobinhoodSafetyGates`, and
appends the outcome to an append-only paper ledger
(``$RH_DATA_DIR/mc_bridge_ledger.jsonl``).

Phase-1 guarantees (deliberate):

* **No network calls.** Nothing here talks to Robinhood — no review, no
  place, no OAuth required. The bridge is a rehearsal of the mapping and the
  safety gates only; connecting the real ``review_equity_order`` /
  ``place_equity_order`` calls is a separate, later stage.
* **Long-only.** Monte-Carlo-Sim emits ``short`` verdicts, but a retail
  Robinhood account cannot short shares. Shorts are logged and skipped
  (converting them to long puts is a possible later phase).
* **Idempotent.** Each verdict file is processed exactly once, tracked in
  ``$RH_DATA_DIR/mc_bridge_state.json`` — restarts and re-runs never
  double-process.
* **Freshness.** Verdicts older than ``max_age_hours`` are skipped: a stale
  decision must not become an order later.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.robinhood.audit_log import AuditLog
from engine.robinhood.config import RobinhoodConfig
from engine.robinhood.safety_gates import RobinhoodSafetyGates

DEFAULT_MAX_AGE_HOURS = 48.0
STATE_FILENAME = "mc_bridge_state.json"
LEDGER_FILENAME = "mc_bridge_ledger.jsonl"
# Monte-Carlo-Sim's settled paper-trade log (outcome_tracker.py), visible to
# the bridge container at /mc-outputs/trade_log.jsonl. Settled entries carry
# realized_pnl_pct — the bridge feeds them into the daily-loss accumulator so
# the halt gate is exercised by real (paper) settlements, not dead code.
SETTLE_LOG_FILENAME = "trade_log.jsonl"


@dataclass
class OrderPlan:
    """A verdict translated into equity-order arguments (not yet an order)."""

    symbol: str
    side: str                 # always "buy" in phase 1 (long-only)
    quantity: int
    limit_price: float
    notional: float
    clamped_from: int | None = None   # original MC share count when capped

    def as_args(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": "limit",
            "limit_price": self.limit_price,
            "time_in_force": "gfd",
        }


def verdict_id(path: Path) -> str:
    """Stable identity of one verdict file (filenames embed timestamp+ticker)."""
    return path.name


def _verdict_age_hours(verdict: dict[str, Any], *, now: datetime | None = None) -> float | None:
    ts = verdict.get("timestamp_utc") or verdict.get("signal_received_at_utc")
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - then).total_seconds() / 3600.0


def map_verdict(
    verdict: dict[str, Any],
    config: RobinhoodConfig,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
) -> tuple[OrderPlan | None, str]:
    """Translate one Monte-Carlo-Sim verdict into an :class:`OrderPlan`.

    Returns ``(plan, reason)``; ``plan`` is None when the verdict must not
    become an order, with ``reason`` explaining why (every skip is a valid,
    logged outcome — most verdicts are NO_TRADE by design).
    """
    if verdict.get("verdict") != "TRADE":
        return None, "not a TRADE verdict"

    side = str(verdict.get("side") or "long").lower()
    if side != "long":
        return None, ("short verdict skipped (phase 1 is long-only: Robinhood "
                      "cannot short shares; long puts are a later phase)")

    age_h = _verdict_age_hours(verdict, now=now)
    if age_h is None:
        return None, "verdict has no parseable timestamp — refusing to act on undatable decisions"
    if age_h > max_age_hours:
        return None, f"stale verdict ({age_h:.1f}h old > {max_age_hours:.0f}h) — skipped"

    ticker = str(verdict.get("ticker") or "").upper()
    if not ticker or ticker == "UNKNOWN":
        return None, "verdict has no usable ticker"

    try:
        s0 = float(verdict.get("s0") or 0.0)
        shares = int((verdict.get("sizing") or {}).get("shares") or 0)
    except (TypeError, ValueError):
        return None, "verdict sizing/price fields unparseable"
    if s0 <= 0.0:
        return None, "verdict has no usable entry price (s0)"
    if shares <= 0:
        return None, "verdict sized to zero shares"

    cap = float(config.max_order_notional_usd)
    max_qty = int(math.floor(cap / s0))
    if max_qty < 1:
        return None, (f"one share of {ticker} (${s0:.2f}) exceeds the "
                      f"${cap:.0f} per-order cap — raise "
                      f"RH_MAX_ORDER_NOTIONAL_USD to at least ${math.ceil(s0)} "
                      "to paper-trade this symbol")
    qty = min(shares, max_qty)
    plan = OrderPlan(
        symbol=ticker,
        side="buy",
        quantity=qty,
        limit_price=round(s0, 2),
        notional=round(qty * s0, 2),
        clamped_from=shares if qty < shares else None,
    )
    reason = "mapped"
    if plan.clamped_from is not None:
        reason = (f"mapped (quantity clamped {plan.clamped_from} → {qty} by the "
                  f"${cap:.0f} per-order cap)")
    return plan, reason


@dataclass
class BridgeState:
    """Processed-verdict + ingested-settlement registry (each exactly once)."""

    path: Path
    processed: dict[str, dict[str, Any]] = field(default_factory=dict)
    settlements: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: str | Path) -> "BridgeState":
        path = Path(data_dir) / STATE_FILENAME
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return cls(
                    path=path,
                    processed=dict(raw.get("processed") or {}),
                    settlements=dict(raw.get("settlements") or {}),
                )
            except (json.JSONDecodeError, OSError):
                pass
        return cls(path=path)

    def is_processed(self, vid: str) -> bool:
        return vid in self.processed

    def mark(self, vid: str, outcome: str) -> None:
        self.processed[vid] = {"ts": time.time(), "outcome": outcome}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"processed": self.processed,
                        "settlements": self.settlements}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)


def _paper_notional(entry: dict[str, Any], config: RobinhoodConfig) -> float | None:
    """Dollar exposure the bridge would have paper-planned for this entry.

    Mirrors :func:`map_verdict`'s clamp (min of MC shares and the per-order
    notional cap) so settled paper P&L is measured on the same position size
    the bridge actually planned, not the MC's unclamped size.
    """
    try:
        s0 = float(entry.get("s0") or 0.0)
        shares = int((entry.get("sizing") or {}).get("shares") or 0)
    except (TypeError, ValueError):
        return None
    if s0 <= 0.0 or shares <= 0:
        return None
    max_qty = int(math.floor(float(config.max_order_notional_usd) / s0))
    if max_qty < 1:
        return None
    return min(shares, max_qty) * s0


def ingest_settlements(
    trade_log: Path,
    config: RobinhoodConfig,
    *,
    gates: RobinhoodSafetyGates,
    state: BridgeState,
    audit: AuditLog | None = None,
) -> dict[str, int]:
    """Feed settled paper trades into the daily-loss accumulator.

    Reads Monte-Carlo-Sim's ``trade_log.jsonl`` (written by
    ``outcome_tracker.py settle``) and calls
    :meth:`RobinhoodSafetyGates.record_realized_pnl` once per newly settled
    TRADE. This is what makes the daily-loss halt real in phase 1: paper
    losses accumulate exactly like live ones will, and the same gate trips.
    Each settlement is ingested exactly once (tracked in bridge state).
    """
    audit = audit or AuditLog(config.data_dir)
    summary = {"settled_seen": 0, "ingested": 0, "unsizable": 0}
    if not trade_log.is_file():
        return summary
    for line in trade_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or not entry.get("settled"):
            continue
        if entry.get("verdict") != "TRADE":
            continue
        settlement = entry.get("settlement")
        if not isinstance(settlement, dict):
            continue
        summary["settled_seen"] += 1
        ticker = str(entry.get("ticker") or "").upper()
        ts = str(entry.get("timestamp_utc")
                 or entry.get("signal_received_at_utc") or "")
        sid = f"{ts}_{ticker}"
        if sid in state.settlements:
            continue
        notional = _paper_notional(entry, config)
        if notional is None:
            # Entry the bridge could never have planned (no price/size or
            # over-cap) — record it as seen so it is not retried forever.
            state.settlements[sid] = {"ts": time.time(), "pnl_usd": None,
                                      "reason": "unsizable"}
            summary["unsizable"] += 1
            continue
        try:
            pnl_pct = float(settlement.get("realized_pnl_pct"))
        except (TypeError, ValueError):
            state.settlements[sid] = {"ts": time.time(), "pnl_usd": None,
                                      "reason": "no realized_pnl_pct"}
            summary["unsizable"] += 1
            continue
        pnl_usd = round(pnl_pct * notional, 2)
        gates.record_realized_pnl(pnl_usd)
        state.settlements[sid] = {"ts": time.time(), "pnl_usd": pnl_usd}
        summary["ingested"] += 1
        _append_ledger(config.data_dir, {
            "type": "paper_settlement",
            "settlement_id": sid,
            "ticker": ticker,
            "pnl_usd": pnl_usd,
            "notional": round(notional, 2),
            "realized_pnl_pct": pnl_pct,
            "exit_reason": settlement.get("exit_reason"),
            "mode": "paper",
        })
        audit.record("mc_bridge_settlement", tool="mc_bridge",
                     details={"settlement_id": sid, "pnl_usd": pnl_usd})
    return summary


def _append_ledger(data_dir: str | Path, row: dict[str, Any]) -> None:
    path = Path(data_dir) / LEDGER_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(row)
    row.setdefault("ts", time.time())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def process_once(
    verdicts_dirs: list[Path],
    config: RobinhoodConfig,
    *,
    gates: RobinhoodSafetyGates | None = None,
    audit: AuditLog | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    trade_log: Path | None = None,
) -> dict[str, Any]:
    """One bridge pass: settlements first, then map + gate + ledger every
    unseen verdict file.

    Purely local — no Robinhood calls. Gate evaluation uses the *review*
    tool name so the (deliberately) disabled live-trading flag does not mask
    the informative gates (notional cap, daily loss halt). Settled paper
    P&L is ingested *before* verdicts so today's losses already count
    against the daily-loss halt when new orders are gated.
    """
    audit = audit or AuditLog(config.data_dir)
    gates = gates or RobinhoodSafetyGates(config, audit)
    state = BridgeState.load(config.data_dir)

    if trade_log is None and verdicts_dirs:
        # Co-hosted layout: verdicts dirs live inside MC's outputs/, and the
        # settled trade log sits beside them.
        trade_log = verdicts_dirs[0].parent / SETTLE_LOG_FILENAME
    settle_summary = {"settled_seen": 0, "ingested": 0, "unsizable": 0}
    if trade_log is not None:
        settle_summary = ingest_settlements(
            trade_log, config, gates=gates, state=state, audit=audit)

    summary = {"seen": 0, "new": 0, "planned": 0, "gate_blocked": 0, "skipped": 0}
    summary.update(settle_summary)
    for vdir in verdicts_dirs:
        if not vdir.is_dir():
            continue
        for path in sorted(vdir.glob("*.json")):
            summary["seen"] += 1
            vid = verdict_id(path)
            if state.is_processed(vid):
                continue
            summary["new"] += 1
            try:
                verdict = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                outcome = f"unreadable verdict file: {exc}"
                summary["skipped"] += 1
                _append_ledger(config.data_dir, {
                    "verdict_id": vid, "outcome": outcome, "mode": "paper",
                })
                state.mark(vid, outcome)
                continue

            plan, reason = map_verdict(
                verdict, config, max_age_hours=max_age_hours, now=now)
            row: dict[str, Any] = {
                "verdict_id": vid,
                "ticker": verdict.get("ticker"),
                "mc_verdict": verdict.get("verdict"),
                "mc_side": verdict.get("side"),
                "mode": "paper",
                "map_reason": reason,
            }
            if plan is None:
                outcome = f"skipped: {reason}"
                summary["skipped"] += 1
            else:
                gate = gates.evaluate("review_equity_order", plan.as_args())
                row["order_plan"] = plan.as_args() | {
                    "notional": plan.notional,
                    "clamped_from": plan.clamped_from,
                }
                row["gate_allowed"] = gate.allowed
                row["gate_reason"] = gate.reason
                if gate.allowed:
                    outcome = "paper_planned (no order placed — phase 1 has no Robinhood calls)"
                    summary["planned"] += 1
                else:
                    outcome = f"gate_blocked: {gate.reason}"
                    summary["gate_blocked"] += 1
            row["outcome"] = outcome
            _append_ledger(config.data_dir, row)
            state.mark(vid, outcome)

    state.save()
    return summary
