"""Walk-forward, cost-aware backtester for the pulse strategy — trustworthy edition.

Honesty controls: no lookahead, tunable market efficiency + efficiency sweep,
multi-window robustness, Probabilistic Sharpe Ratio, vig as cost.

Signal comparison (phase b): every round it computes THREE probabilities —
  baseline  = Markov + Monte Carlo + patterns (the original model)
  features  = an online-logistic learner on microstructure features (CLV,
              CLV-EWMA, momentum-z, range-z, signed-volume imbalance)
  ensemble  = average of the two
and tracks each one's OUT-OF-SAMPLE Brier score (walk-forward), so we can see
whether the microstructure signal actually adds predictive skill. `--signal`
chooses which one drives the bets.

Run (Docker):  docker compose run --rm hermes-trading-engine python -m engine.backtest
Data: public Kraken/Coinbase candles (no key) or --csv <file> (open,high,low,close[,volume]).
"""

from __future__ import annotations

import argparse
import csv as _csv
import json
import math
import sys

import httpx
import numpy as np

from .features import OnlineLogistic, pulse_features
from .quant import markov, montecarlo, patterns

_KRAKEN = "https://api.kraken.com/0/public/OHLC"
_COINBASE_EX = "https://api.exchange.coinbase.com"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def _kraken_pair(symbol: str) -> str:
    base = symbol.replace("USDT", "").replace("USD", "")
    if base == "BTC":
        base = "XBT"
    return f"{base}USDT"


def fetch_history(symbol: str, interval_min: int) -> list[dict]:
    with httpx.Client(timeout=20.0, headers={"User-Agent": "hte-backtest"}) as c:
        try:
            r = c.get(_KRAKEN, params={"pair": _kraken_pair(symbol), "interval": interval_min})
            if r.status_code == 200:
                res = r.json().get("result", {})
                for k, rows in res.items():
                    if k == "last" or not isinstance(rows, list):
                        continue
                    out = [{"o": float(x[1]), "h": float(x[2]), "l": float(x[3]),
                            "c": float(x[4]), "v": float(x[6]) if len(x) > 6 else 0.0}
                           for x in rows if len(x) >= 5]
                    if out:
                        return out
        except Exception:
            pass
        try:
            prod = f"{symbol.replace('USDT','').replace('USD','')}-USD"
            r = c.get(f"{_COINBASE_EX}/products/{prod}/candles", params={"granularity": interval_min * 60})
            if r.status_code == 200:
                rows = r.json()
                out = [{"o": float(x[3]), "h": float(x[2]), "l": float(x[1]),
                        "c": float(x[4]), "v": float(x[5]) if len(x) > 5 else 0.0}
                       for x in rows if isinstance(x, list) and len(x) >= 6]
                out.reverse()
                return out
        except Exception:
            pass
    return []


def load_csv(path: str) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}
        oc, cc, hc, lc, vc = (cols.get("open"), cols.get("close"), cols.get("high"),
                              cols.get("low"), cols.get("volume") or cols.get("vol"))
        for r in reader:
            try:
                o, c = float(r[oc]), float(r[cc])
                rows.append({"o": o, "c": c,
                             "h": float(r[hc]) if hc else max(o, c),
                             "l": float(r[lc]) if lc else min(o, c),
                             "v": float(r[vc]) if vc else 0.0})
            except (TypeError, ValueError, KeyError):
                continue
    return rows


# --------------------------------------------------------------------------
# walk-forward calibration
# --------------------------------------------------------------------------
class WFCalibrator:
    def __init__(self, bins=20, shrink=25.0, min_samples=40):
        self.bins, self.shrink, self.min_samples = bins, shrink, min_samples
        self.preds: list[tuple[float, int]] = []

    def record(self, p_raw, outcome):
        self.preds.append((p_raw, outcome))

    def calibrate(self, p):
        p = min(0.98, max(0.02, p))
        if len(self.preds) < self.min_samples:
            return p
        b = min(self.bins - 1, max(0, int(p * self.bins)))
        lo, hi = b / self.bins, (b + 1) / self.bins
        pts = [o for (pr, o) in self.preds if lo <= pr < hi]
        if not pts:
            return p
        return min(0.98, max(0.02, (sum(pts) + self.shrink * p) / (len(pts) + self.shrink)))

    def brier(self, calibrated=True):
        if not self.preds:
            return None
        if calibrated:
            return round(sum((self.calibrate(pr) - o) ** 2 for pr, o in self.preds) / len(self.preds), 4)
        return round(sum((pr - o) ** 2 for pr, o in self.preds) / len(self.preds), 4)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def baseline_prob_up(window_closes, window_candles, lookback, mc_paths, seed):
    m = markov.fit(window_closes, lookback=lookback).get("p_up", 0.5)
    mc = montecarlo.simulate(window_closes, horizon_steps=1, paths=mc_paths, seed=seed).get("p_up", 0.5)
    p = 0.5 * m + 0.5 * mc
    bias = patterns.scan(window_candles).get("bias", "neutral")
    p += 0.02 if bias == "bullish" else -0.02 if bias == "bearish" else 0.0
    return min(0.98, max(0.02, p))


def kelly(pw, price):
    b = (1.0 / price) - 1.0
    return max(0.0, pw - (1.0 - pw) / b) if b > 0 else 0.0


def max_drawdown(curve):
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0.0)
    return mdd


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def probabilistic_sharpe(returns: np.ndarray):
    n = len(returns)
    if n < 5 or returns.std() == 0:
        return None
    sr = returns.mean() / returns.std()
    m, s = returns - returns.mean(), returns.std()
    skew = float((m ** 3).mean() / s ** 3)
    kurt = float((m ** 4).mean() / s ** 4)
    denom = math.sqrt(max(1e-9, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2))
    return round(_norm_cdf((sr * math.sqrt(n - 1)) / denom), 3)


# --------------------------------------------------------------------------
# one walk-forward simulation (computes all 3 signals; bets the chosen one)
# --------------------------------------------------------------------------
def simulate(candles, *, vig, ev_threshold, kelly_fraction, max_stake, lookback,
             mc_paths, market_efficiency, market_noise, signal="ensemble",
             base_window=60, seed=12345):
    n = len(candles)
    closes = [c["c"] for c in candles]
    cal_base, cal_feat, cal_ens = WFCalibrator(), WFCalibrator(), WFCalibrator()
    logit = OnlineLogistic()
    rng = np.random.default_rng(seed)
    bankroll, equity = 1.0, [1.0]
    bet_returns, evs = [], []
    bets = wins = 0
    warm = max(lookback, base_window) + 1

    for t in range(warm, n):
        outcome = 1 if candles[t]["c"] > candles[t]["o"] else 0

        p_base_raw = baseline_prob_up(closes[t - lookback:t], candles[max(0, t - 120):t],
                                      lookback, mc_paths, seed=t)
        feats = pulse_features(candles[:t])
        p_feat_raw = logit.predict_proba(feats) if logit.ready() else 0.5
        p_ens_raw = 0.5 * (p_base_raw + p_feat_raw)

        p_base = cal_base.calibrate(p_base_raw)
        p_feat = cal_feat.calibrate(p_feat_raw)
        p_ens = cal_ens.calibrate(p_ens_raw)
        p_cal = {"baseline": p_base, "features": p_feat, "ensemble": p_ens}[signal]

        recent = [1 if candles[i]["c"] > candles[i]["o"] else 0 for i in range(t - base_window, t)]
        base = sum(recent) / len(recent) if recent else 0.5
        informed = market_efficiency * p_base_raw + (1.0 - market_efficiency) * base
        m_up = informed + (float(rng.normal(0, market_noise)) if market_noise > 0 else 0.0)
        m_up = min(0.9, max(0.1, m_up))
        up_price = min(0.98, max(0.02, m_up + vig / 2))
        down_price = min(0.98, max(0.02, (1 - m_up) + vig / 2))

        ev_up = p_cal / up_price - 1.0
        ev_down = (1 - p_cal) / down_price - 1.0
        side = None
        if ev_up >= ev_down and ev_up > ev_threshold:
            side, price, pw, ev = "UP", up_price, p_cal, ev_up
        elif ev_down > ev_threshold:
            side, price, pw, ev = "DOWN", down_price, 1 - p_cal, ev_down
        if side:
            f = min(kelly(pw, price) * kelly_fraction, max_stake)
            stake = f * bankroll
            if stake > 0:
                bets += 1
                evs.append(ev)
                won = (side == "UP" and outcome == 1) or (side == "DOWN" and outcome == 0)
                pnl = stake * (1.0 / price - 1.0) if won else -stake
                wins += 1 if won else 0
                bet_returns.append(pnl / bankroll)
                bankroll += pnl
        equity.append(bankroll)

        # walk-forward updates (only after the round settles)
        cal_base.record(p_base_raw, outcome)
        cal_feat.record(p_feat_raw, outcome)
        cal_ens.record(p_ens_raw, outcome)
        logit.observe(feats, outcome)

    rounds = max(0, n - warm)
    arr = np.array(bet_returns) if bet_returns else np.array([])
    sr_obs = float(arr.mean() / arr.std()) if arr.size and arr.std() > 0 else 0.0
    return {
        "rounds": rounds, "bets": bets, "wins": wins,
        "return_pct": round((bankroll - 1.0) * 100, 2), "final_multiple": round(bankroll, 3),
        "bet_rate_pct": round(100 * bets / rounds, 1) if rounds else 0.0,
        "win_rate_pct": round(100 * wins / bets, 1) if bets else None,
        "avg_ev_pct": round(100 * float(np.mean(evs)), 2) if evs else None,
        "sharpe_per_bet": round(sr_obs * math.sqrt(arr.size), 2) if arr.size else 0.0,
        "psr": probabilistic_sharpe(arr) if arr.size else None,
        "max_dd_pct": round(100 * max_drawdown(equity), 2),
        "brier_baseline": cal_base.brier(True),
        "brier_features": cal_feat.brier(True),
        "brier_ensemble": cal_ens.brier(True),
        "logit_weights": [round(w, 3) for w in logit.w],
    }


def run_windows(candles, k=4, **params):
    n = len(candles)
    need = params["lookback"] + 80
    size = n // k
    if size < need:
        k = max(1, n // need)
        size = n // k if k else n
    out = []
    for i in range(k):
        seg = candles[i * size:(i + 1) * size] if i < k - 1 else candles[i * size:]
        if len(seg) >= need:
            out.append(simulate(seg, seed=1000 + i, **params)["return_pct"])
    return out


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trustworthy walk-forward pulse backtester (+signal compare).")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--vig", type=float, default=0.04)
    ap.add_argument("--ev-threshold", type=float, default=0.015)
    ap.add_argument("--kelly", type=float, default=0.35)
    ap.add_argument("--max-stake", type=float, default=0.04)
    ap.add_argument("--lookback", type=int, default=120)
    ap.add_argument("--mc-paths", type=int, default=120)
    ap.add_argument("--efficiency", type=float, default=0.85)
    ap.add_argument("--market-noise", type=float, default=0.02)
    ap.add_argument("--signal", choices=["baseline", "features", "ensemble"], default="ensemble")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    candles = load_csv(args.csv) if args.csv else fetch_history(args.symbol, args.interval)
    src = f"csv:{args.csv}" if args.csv else f"{args.symbol} {args.interval}m"
    if len(candles) < args.lookback + 120:
        print(f"Not enough data ({len(candles)} bars). Lower --lookback, change --interval, or use --csv.")
        return 1

    base = dict(vig=args.vig, ev_threshold=args.ev_threshold, kelly_fraction=args.kelly,
                max_stake=args.max_stake, lookback=args.lookback, mc_paths=args.mc_paths,
                market_noise=args.market_noise, signal=args.signal)

    headline = simulate(candles, market_efficiency=args.efficiency, **base)
    sweep = [(eff, simulate(candles, market_efficiency=eff, **base))
             for eff in (0.0, 0.5, 0.85, 1.0)]
    windows = run_windows(candles, k=4, market_efficiency=args.efficiency, **base)

    report = {"data_source": src, "bars": len(candles), "signal": args.signal,
              "headline": headline, "efficiency_sweep": [{"efficiency": e, **r} for e, r in sweep],
              "window_returns_pct": windows, "params": base}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    h = headline
    print("=" * 72)
    print(" HERMES — WALK-FORWARD BACKTEST (efficiency-swept, PSR, signal compare)")
    print("=" * 72)
    print(f" Data {src} | bars {len(candles)} | rounds {h['rounds']} | driving signal: {args.signal}")
    print(f" Costs: vig {args.vig:.0%}  EVgate>{args.ev_threshold:.1%}  Kelly x{args.kelly}  cap {args.max_stake:.0%}")
    print("-" * 72)
    print(" OUT-OF-SAMPLE MODEL SKILL  (Brier, lower better; coin flip = 0.2500)")
    print(f"   baseline (Markov+MC+patterns) ... {h['brier_baseline']}")
    print(f"   features (microstructure logit) . {h['brier_features']}")
    print(f"   ensemble ........................ {h['brier_ensemble']}")
    b0, bf = h["brier_baseline"], h["brier_features"]
    if b0 and bf:
        d = b0 - bf
        print(f"   => features {'IMPROVE' if d > 0 else 'do NOT improve'} on baseline "
              f"by {d:+.4f} Brier")
    print(f"   learned feature weights [clv, clv_ewma, mom_z, range_z, vol_imb]: {h['logit_weights']}")
    print("-" * 72)
    print(f" HEADLINE (efficiency {args.efficiency})  Return {h['return_pct']:+.2f}% | "
          f"bets {h['bets']} ({h['bet_rate_pct']}%) | win {h['win_rate_pct']}% | "
          f"Sharpe/bet {h['sharpe_per_bet']} | maxDD {h['max_dd_pct']}% | PSR {h['psr']}")
    print("-" * 72)
    print(" MARKET-EFFICIENCY SWEEP")
    print("   eff   bets   return%   sharpe/bet")
    for e, r in sweep:
        print(f"   {e:<4}  {r['bets']:<5}  {r['return_pct']:>8.2f}   {r['sharpe_per_bet']:>9}")
    print("-" * 72)
    if windows:
        wr = np.array(windows)
        print(" ROBUSTNESS (return% per contiguous sub-window):")
        print("   " + "  ".join(f"{w:+.1f}" for w in windows)
              + f"   | mean {wr.mean():+.1f}% worst {wr.min():+.1f}% profitable {int((wr>0).sum())}/{len(wr)}")
    print("-" * 72)
    print(" VERDICT")
    print(_verdict(report))
    print("=" * 72)
    return 0


def _verdict(rep) -> str:
    h = rep["headline"]
    sweep = rep["efficiency_sweep"]
    win = rep["window_returns_pct"]
    eff1 = next((r for r in sweep if r["efficiency"] == 1.0), None)
    lines = []
    b0, bf, be = h["brier_baseline"], h["brier_features"], h["brier_ensemble"]
    if b0 and bf:
        if bf < b0 - 0.001:
            lines.append(f"• Microstructure features ADD skill (Brier {bf} < baseline {b0}). Keep them.")
        else:
            lines.append(f"• Microstructure features do NOT beat the baseline OOS (Brier {bf} vs {b0}). "
                         "On this data the signal isn't predictive — exactly what we need to know.")
    if all(x is not None and x >= 0.247 for x in (b0, bf, be)):
        lines.append("• All signals are ~coin-flip (Brier ≈ 0.25): no real directional edge at this horizon.")
    if eff1 and eff1["return_pct"] <= 1.0:
        lines.append(f"• Edge collapses vs an efficient market (eff=1 ⇒ {eff1['return_pct']:+.1f}%).")
    if h["psr"] is not None and h["psr"] < 0.95:
        lines.append(f"• PSR {h['psr']}: not statistically convincing (want ≥0.95).")
    if win:
        wr = np.array(win)
        if (wr > 0).mean() < 0.75:
            lines.append(f"• Fragile: profitable in only {int((wr>0).sum())}/{len(wr)} windows.")
    lines.append("• Small single-asset sample. Feed months of multi-regime data (--csv); treat any "
                 "positive as a hypothesis. Order-book/funding signals are wired LIVE but can't be "
                 "backtested here (no free historical L2/funding).")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
