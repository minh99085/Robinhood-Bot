#!/usr/bin/env python3
"""Read-only VPS operator dashboard (PAPER ONLY) — "what is running" at a glance.

This is a SAFE, READ-ONLY view of the live Hermes paper-training engine. It reads the
running container's durable status (``/data/polymarket_training.json`` + ``docker ps``)
and renders a single self-contained HTML page. It has NO control endpoints — it can never
start/stop a run, change a flag, toggle live, or place a trade — so it is safe to view and
is kept off the public internet (serve mode binds to localhost; view it over an SSH
tunnel). The full control dashboard remains the engine's own UI on :8800.

Two modes (stdlib only — no extra deps):

  # one-shot self-contained HTML snapshot (open the file in any browser; re-run to refresh)
  python3 scripts/vps_dashboard.py --once --out vps_dashboard.html

  # live, auto-refreshing, localhost-only server (view via:
  #   ssh -L 8801:localhost:8801 linuxuser@<vps>  ->  http://localhost:8801 )
  python3 scripts/vps_dashboard.py --serve --port 8801 --refresh 10

Status source resolution (best effort, read-only): a running container via
``docker exec <container> cat /data/polymarket_training.json``, else ``--data-dir`` /
``$HTE_DATA_DIR`` / ./runtime_data on disk.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_CONTAINER = os.getenv("HERMES_TRAINING_CONTAINER", "hermes-training")


# --------------------------------------------------------------------------- #
# Read-only data collection
# --------------------------------------------------------------------------- #
def _run(argv, timeout=15):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"{type(exc).__name__}: {exc}"


def fetch_status(container: str, data_dir: str | None) -> tuple[dict, str]:
    """Return (status_dict, source). Tries the running container first (live), then disk."""
    rc, out, _ = _run(["docker", "exec", container, "cat", "/data/polymarket_training.json"])
    if rc == 0 and out.strip():
        try:
            return json.loads(out), f"container:{container}:/data"
        except json.JSONDecodeError:
            pass
    candidates = []
    if data_dir:
        candidates.append(Path(data_dir) / "polymarket_training.json")
    candidates += [Path(os.getenv("HTE_DATA_DIR", "")) / "polymarket_training.json"
                   if os.getenv("HTE_DATA_DIR") else None,
                   Path("runtime_data") / "polymarket_training.json"]
    for c in candidates:
        if c and c.is_file():
            try:
                return json.loads(c.read_text(encoding="utf-8")), f"file:{c}"
            except (OSError, json.JSONDecodeError):
                continue
    return {}, "unavailable"


def fetch_containers() -> list[dict]:
    """Read-only `docker ps` for the hermes containers (name/status/health)."""
    rc, out, _ = _run(["docker", "ps", "--filter", "name=hermes", "--format",
                       "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    rows = []
    if rc == 0:
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                rows.append({"name": parts[0], "status": parts[1],
                             "image": parts[2] if len(parts) > 2 else ""})
    return rows


def git_commit() -> str:
    rc, out, _ = _run(["git", "rev-parse", "HEAD"])
    return out.strip()[:12] if rc == 0 else "unknown"


# --------------------------------------------------------------------------- #
# Shape the status into dashboard rows (read-only; never mutates anything)
# --------------------------------------------------------------------------- #
def build_view(status: dict, containers: list, source: str, commit: str) -> dict:
    st = status or {}
    al = st.get("active_learning", {}) or {}
    ks = st.get("kill_switch", {}) or {}
    mon = st.get("monitoring", {}) or {}
    pnl = st.get("pnl", {}) or {}
    pr = st.get("paper_realism", {}) or {}
    rr = st.get("run_ready", {}) or {}
    g = st.get("grok_news_evidence", {}) or {}
    bx = (st.get("bregman", {}) or {}).get("execution", st.get("bregman", {})) or {}
    scan = st.get("scan_metrics", {}) or {}

    def f(v, d=0):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return d

    profile = st.get("profile", "?")
    downgraded = bool(st.get("downgraded"))
    al_on = bool(al.get("active_learning_enabled"))
    mismatch = bool(al.get("active_learning_config_mismatch"))
    run_ready = bool(rr.get("run_ready_for_hours"))
    live_off = True  # this build is paper-only; engine refuses to start with live flags

    runtime_s = f(st.get("runtime_seconds"))
    hours = round(runtime_s / 3600.0, 2) if runtime_s else 0

    # subsystem chips
    chips = [
        ("Profile", profile, "good" if (profile == "aggressive" and not downgraded) else "warn"),
        ("Active learning", "ON" if al_on else "OFF", "good" if al_on else "bad"),
        ("Run-ready", "YES" if run_ready else "NO", "good" if run_ready else "warn"),
        ("Config match", "OK" if not mismatch else "MISMATCH", "good" if not mismatch else "bad"),
        ("Live trading", "DISABLED", "good"),
        ("Grok", "READY" if g.get("grok_brain_ready") else "off",
         "good" if g.get("grok_brain_ready") else "warn"),
    ]

    metrics = [
        ("Runtime", f"{hours} h ({int(runtime_s)} s)"),
        ("Equity", f"${pnl.get('equity', '?')}"),
        ("Decisions", scan.get("scanned", pnl.get("decision_count", "?"))),
        ("Markets scanned", scan.get("scanned", "?")),
        ("Tiny evaluator called", al.get("active_learning_tiny_evaluator_called", 0)),
        ("Tiny ≤$1 trades opened", al.get("active_learning_tiny_trades_opened", 0)),
        ("Exploration trades opened", al.get("exploration_trades_opened", 0)),
        ("Exploration P&L", f"${pr.get('exploration_pnl', 0)}"),
        ("Readiness P&L", f"${pr.get('readiness_pnl', 0)}"),
        ("Selected-but-not-evaluated", al.get("active_learning_selected_but_not_evaluated_count", 0)),
        ("Bregman groups discovered", bx.get("raw_groups_discovered", "?")),
        ("Bregman certified", bx.get("certified_opportunities", 0)),
        ("Relaxed trades opened", bx.get("paper_relaxed_trades_opened", 0)),
        ("Grok calls (with news)", f"{g.get('grok_calls_total', 0)} ({g.get('grok_calls_with_news', 0)})"),
        ("News items used", g.get("news_items_used", 0)),
        ("Kill-switch loss_streak", mon.get("loss_streak", 0)),
        ("Kill-switch drawdown", mon.get("drawdown", 0)),
        ("Realistic trade count", pr.get("realistic_trade_count", 0)),
        ("Reference fills allowed", pr.get("reference_fills_allowed", "?")),
    ]

    tiny_blocked = al.get("active_learning_tiny_blocked_by_reason", {}) or {}
    ks_triggers = ks.get("triggered", []) or []
    blockers = rr.get("blocking_reasons", []) or []

    return {
        "source": source, "commit": commit, "containers": containers,
        "chips": chips, "metrics": metrics, "tiny_blocked": tiny_blocked,
        "ks_triggers": ks_triggers, "should_downgrade": bool(ks.get("should_downgrade")),
        "blockers": blockers, "downgraded": downgraded,
        "generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
 background:#0d1117;color:#e6edf3}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b949e;font-size:12px;margin-bottom:20px}
.chips{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:22px}
.chip{padding:8px 14px;border-radius:999px;font-weight:600;font-size:13px;border:1px solid #30363d}
.chip .k{color:#8b949e;font-weight:500;margin-right:6px;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.good{background:#0f2417;border-color:#1f6f3f;color:#56d364}
.warn{background:#2a2412;border-color:#9e7a1a;color:#e3b341}
.bad{background:#2a1416;border-color:#a13b41;color:#f85149}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:18px;margin-bottom:18px}
.card h2{font-size:14px;margin:0 0 12px;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.m{display:flex;justify-content:space-between;border-bottom:1px solid #21262d;padding:6px 0}
.m .lab{color:#8b949e}.m .val{font-weight:600}
table{width:100%;border-collapse:collapse}td{padding:5px 8px;border-bottom:1px solid #21262d}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;background:#21262d;margin:2px;font-size:12px}
.foot{color:#8b949e;font-size:12px;margin-top:18px}
.banner{padding:10px 14px;border-radius:8px;margin-bottom:18px;font-weight:600}
"""


def render_html(view: dict, refresh: int = 0) -> str:
    e = html.escape
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    chips = "".join(
        f'<span class="chip {c[2]}"><span class="k">{e(c[0])}</span>{e(str(c[1]))}</span>'
        for c in view["chips"])
    conts = "".join(
        f'<tr><td><b>{e(r["name"])}</b></td><td>{e(r["status"])}</td>'
        f'<td style="color:#8b949e">{e(r["image"])}</td></tr>'
        for r in view["containers"]) or '<tr><td colspan=3 style="color:#8b949e">no hermes containers found</td></tr>'
    metrics = "".join(
        f'<div class="m"><span class="lab">{e(str(k))}</span><span class="val">{e(str(v))}</span></div>'
        for k, v in view["metrics"])
    tiny = "".join(f'<span class="pill">{e(str(k))}: {e(str(v))}</span>'
                   for k, v in view["tiny_blocked"].items()) or '<span style="color:#8b949e">none</span>'
    ks = "".join(f'<span class="pill">{e(str(t))}</span>'
                 for t in view["ks_triggers"]) or '<span style="color:#8b949e">none</span>'
    blockers = "".join(f'<span class="pill">{e(str(b))}</span>'
                       for b in view["blockers"]) or '<span style="color:#56d364">none — run-ready</span>'

    banner = ""
    if view["downgraded"]:
        banner = ('<div class="banner bad">Bot is DOWNGRADED to conservative — active learning is '
                  'paused. Check kill-switch triggers below.</div>')
    elif not any(c[0] == "Active learning" and c[1] == "ON" for c in view["chips"]):
        banner = '<div class="banner warn">Active learning is OFF.</div>'

    return f"""<!doctype html><html><head><meta charset="utf-8">{meta}
<title>Hermes VPS — what's running</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>Hermes VPS — what's running <span style="color:#8b949e;font-weight:400">(read-only)</span></h1>
<div class="sub">source: {e(view['source'])} · code commit: {e(view['commit'])} · generated {e(view['generated'])}
{' · auto-refresh '+str(refresh)+'s' if refresh else ''}</div>
{banner}
<div class="chips">{chips}</div>
<div class="card"><h2>Containers (docker ps)</h2><table>{conts}</table></div>
<div class="card"><h2>Live metrics</h2><div class="grid">{metrics}</div></div>
<div class="card"><h2>Tiny-exploration blockers (exact reasons)</h2>{tiny}</div>
<div class="card"><h2>Kill-switch triggers</h2>{ks}
<div style="margin-top:8px;color:#8b949e">should_downgrade: {e(str(view['should_downgrade']))}</div></div>
<div class="card"><h2>Run-ready blockers</h2>{blockers}</div>
<div class="foot">Read-only dashboard — no control actions. Full control UI: engine on :8800
(view via SSH tunnel). PAPER ONLY; live trading disabled.</div>
</div></body></html>"""


def collect(container: str, data_dir: str | None) -> dict:
    status, source = fetch_status(container, data_dir)
    return build_view(status, fetch_containers(), source, git_commit())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Hermes VPS status dashboard (PAPER).")
    ap.add_argument("--container", default=DEFAULT_CONTAINER)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--once", action="store_true", help="write a one-shot HTML snapshot")
    ap.add_argument("--out", default="vps_dashboard.html")
    ap.add_argument("--serve", action="store_true", help="serve a live page (localhost only)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default localhost — keep it off the public internet)")
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--refresh", type=int, default=10, help="serve-mode auto-refresh seconds")
    args = ap.parse_args(argv)

    if args.serve:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path not in ("/", "/index.html"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = render_html(collect(args.container, args.data_dir),
                                   refresh=args.refresh).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # quiet
                pass

        srv = HTTPServer((args.host, args.port), H)
        print(f"read-only dashboard at http://{args.host}:{args.port}  "
              f"(view via: ssh -L {args.port}:localhost:{args.port} <user>@<vps>)")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    # default: one-shot snapshot
    out = Path(args.out)
    out.write_text(render_html(collect(args.container, args.data_dir)), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
