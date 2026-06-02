"""Artifact writers for replay runs (JSON + CSV; optional charts)."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    cols = columns
    if cols is None:
        cols = sorted({k for r in rows for k in r.keys()}) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def maybe_charts(out_dir: Path, equity_rows: list[dict], calibration: dict) -> list[str]:
    """Render simple charts only if matplotlib is already installed."""
    try:
        import matplotlib  # noqa: F401
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — no heavy viz dependency added
        return []
    written = []
    try:
        if equity_rows:
            xs = [r["ts_ms"] for r in equity_rows]
            eq = [r["equity"] for r in equity_rows]
            dd = [r.get("drawdown", 0) for r in equity_rows]
            plt.figure(); plt.plot(xs, eq); plt.title("Equity curve"); plt.xlabel("ts_ms")
            plt.ylabel("equity"); plt.tight_layout(); plt.savefig(out_dir / "equity_curve.png"); plt.close()
            written.append("equity_curve.png")
            plt.figure(); plt.plot(xs, dd); plt.title("Drawdown"); plt.tight_layout()
            plt.savefig(out_dir / "drawdown.png"); plt.close()
            written.append("drawdown.png")
        table = (calibration or {}).get("calibration_by_probability_bucket") or []
        pts = [(r["avg_predicted"], r["realized_frequency"]) for r in table
               if r.get("avg_predicted") is not None and r.get("realized_frequency") is not None]
        if pts:
            plt.figure(); plt.plot([0, 1], [0, 1], "--")
            plt.plot([p for p, _ in pts], [f for _, f in pts], "o-")
            plt.title("Calibration"); plt.xlabel("predicted"); plt.ylabel("realized")
            plt.tight_layout(); plt.savefig(out_dir / "calibration_curve.png"); plt.close()
            written.append("calibration_curve.png")
    except Exception:  # noqa: BLE001
        return written
    return written
