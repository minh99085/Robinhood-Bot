"""Slim read-only API for the BTC 5-minute pulse PAPER engine.

After the focused redesign, the only HTTP surface is health + read-only pulse status/ledger
(served from the JSON the pulse engine writes to ``HTE_DATA_DIR``). There is no trading,
mode, or live-execution endpoint — this engine is PAPER ONLY and the loop runs in the
separate ``scripts/run_btc_pulse.py`` process.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Hermes BTC 5-min Pulse (paper)", version="2.0")


def _data_dir() -> Path:
    return Path(os.environ.get("HTE_DATA_DIR", "/data"))


def _read_json(name: str) -> "dict | None":
    path = _data_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/health")
def health() -> dict:
    """Liveness + freshness of the pulse engine (status JSON written every tick)."""
    st = _read_json("btc_pulse_status.json")
    fresh = False
    age = None
    p = _data_dir() / "btc_pulse_status.json"
    if p.exists():
        age = round(time.time() - p.stat().st_mtime, 1)
        fresh = age < 120
    return {"status": "ok", "paper_only": True, "live_trading_enabled": False,
            "pulse_status_fresh": fresh, "pulse_status_age_s": age,
            "ticks": (st or {}).get("ticks")}


@app.get("/api/polymarket/training/btc_pulse")
def btc_pulse_status() -> dict:
    """BTC 5-min pulse engine status: price/vol health, paper ledger, calibration, gating."""
    st = _read_json("btc_pulse_status.json")
    if not st:
        return {"available": False,
                "reason": "pulse engine has not written status yet — start run_btc_pulse.py"}
    return {"available": True, **st}


@app.get("/api/polymarket/training/btc_pulse/ledger")
def btc_pulse_ledger() -> dict:
    """BTC 5-min pulse PAPER ledger: paper positions + realized P&L."""
    led = _read_json("btc_pulse_ledger.json")
    if not led:
        return {"available": False, "reason": "no pulse ledger yet."}
    return {"available": True, **led}


@app.get("/api")
def api_index() -> JSONResponse:
    return JSONResponse({"engine": "btc-5min-pulse", "paper_only": True,
                         "endpoints": ["/api/health", "/api/polymarket/training/btc_pulse",
                                       "/api/polymarket/training/btc_pulse/ledger"]})


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Read-only live dashboard for the BTC 5-min pulse paper engine."""
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BTC 5-min Pulse · Hermes (paper)</title>
<style>
:root{--bg:#0b0e14;--card:#141925;--mut:#8b95a7;--fg:#e6edf3;--grn:#3fb950;--red:#f85149;--acc:#58a6ff;--bd:#222b3a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:650}.pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--bd);color:var(--mut)}
.ok{color:var(--grn);border-color:#1d3a26}.bad{color:var(--red);border-color:#3a1d1d}
.wrap{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px;max-width:1400px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 10px}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px dashed #1c2533}
.row:last-child{border-bottom:0}.k{color:var(--mut)}.v{font-variant-numeric:tabular-nums;text-align:right}
.big{font-size:26px;font-weight:680;font-variant-numeric:tabular-nums}.sub{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:right;padding:4px 6px;border-bottom:1px solid #1c2533}
th:first-child,td:first-child{text-align:left}.muted{color:var(--mut)}.foot{padding:10px 20px;color:var(--mut);font-size:12px;border-top:1px solid var(--bd)}
</style></head><body>
<header>
  <h1>BTC 5-min Pulse</h1>
  <span class="pill" id="paper">PAPER ONLY</span>
  <span class="pill" id="health">connecting…</span>
  <span class="pill" id="ticks"></span>
  <span class="pill" id="updated"></span>
</header>
<div class="wrap" id="cards"></div>
<div class="foot">Auto-refreshes every 3s · read-only · oracle = Chainlink Data Streams ref price via Polymarket RTDS</div>
<script>
const $=(h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild};
const f=(x,d=2)=>x==null?'—':(typeof x==='number'?x.toFixed(d):x);
const money=(x)=>x==null?'—':(x>=0?'+$':'-$')+Math.abs(x).toFixed(2);
function card(title,rows){const c=$(`<div class="card"><h2>${title}</h2></div>`);
  rows.forEach(([k,v,cls])=>{const r=$(`<div class="row"><span class="k">${k}</span><span class="v ${cls||''}">${v}</span></div>`);c.appendChild(r)});return c}
async function tick(){
 let s,l;
 try{s=await (await fetch('/api/polymarket/training/btc_pulse',{cache:'no-store'})).json();
     l=await (await fetch('/api/polymarket/training/btc_pulse/ledger',{cache:'no-store'})).json();}
 catch(e){document.getElementById('health').textContent='unreachable';document.getElementById('health').className='pill bad';return}
 const h=document.getElementById('health');
 if(!s.available){h.textContent='no data yet';h.className='pill bad';return}
 h.textContent='live';h.className='pill ok';
 document.getElementById('ticks').textContent='ticks '+s.ticks;
 document.getElementById('updated').textContent=new Date().toLocaleTimeString();
 const L=s.ledger||{},o=s.oracle||{},c=s.calibration||{},p=s.price||{},g=s.grok_overlay||{},eg=s.execution_gate||{};
 const lf=(o.lead_features||{}).feeds||{},rt=o.rtds||{},rec=L.proxy_official_reconciliation||{};
 const cards=document.getElementById('cards');cards.innerHTML='';
 // P&L hero
 const pnl=$(`<div class="card"><h2>Paper P&L</h2><div class="big ${(L.realized_pnl_usd||0)>=0?'':'bad'}" style="color:${(L.realized_pnl_usd||0)>=0?'var(--grn)':'var(--red)'}">${money(L.realized_pnl_usd)}</div><div class="sub">${L.settled||0} settled · win-rate ${f((L.win_rate||0)*100,1)}% · edge ${f(L.edge_realized,3)}</div></div>`);cards.appendChild(pnl);
 cards.appendChild(card('Ledger',[['trades',L.trades],['settled',L.settled],['wins',L.wins],['win-rate',f((L.win_rate||0)*100,1)+'%'],['avg entry',f(L.avg_entry_price,3)],['edge realized',f(L.edge_realized,3)],['avg pnl/trade',money(L.avg_pnl_per_trade)],['open',L.open_positions]]));
 cards.appendChild(card('Oracle (reference model)',[['feed type',o.oracle_feed_type||'—'],['symbol',o.oracle_symbol||'—'],['price source',p.source||'—'],['Chainlink btc/usd',f(rt.latest&&rt.latest['crypto_prices_chainlink:btc/usd'])],['RTDS connected',rt.connected?'yes':'no',rt.connected?'ok':'bad'],['open/close snap',o.open_snapshot_source||'—'],['σ/sec',f(p.sigma_per_sec,6)],['sampler',p.sampler_running?(p.sampler_interval_s+'s'):'off']]));
 cards.appendChild(card('Lead feeds (features only)',[['binance btcusdt',f(lf.binance_btcusdt&&lf.binance_btcusdt.price)],['coinbase btcusd',f(lf.coinbase_btcusd&&lf.coinbase_btcusd.price)],['settlement eligible','no','muted']]));
 cards.appendChild(card('Execution gate',[['candidates',eg.candidates],['accepted (fills)',eg.accepted,'ok'],['rejected',eg.rejected_total,'bad'],...Object.entries(eg.rejected||{}).filter(([,v])=>v>0).map(([k,v])=>['· '+k,v,'bad']),['reconciled',eg.reconciled?'yes':'NO',eg.reconciled?'ok':'bad']]));
 cards.appendChild(card('Settlement & calibration',[['sources',JSON.stringify(L.settle_sources||{})],['proxy vs official','both '+(rec.both||0)+' · agree '+(rec.agree||0)+' · disagree '+(rec.disagree||0)],['Brier',f(c.brier,3)+' (base 0.25)'],['log-loss',f(c.log_loss,3)],['samples',c.samples],['base-rate up',f(c.base_rate_up,2)]]));
 cards.appendChild(card('Grok event-risk overlay',[['enabled',g.enabled?'yes':'no'],['regime',(g.state||{}).regime||'—'],['blackout',(g.state||{}).blackout?'YES':'no',(g.state||{}).blackout?'bad':''],['calls',g.calls],['reason',(g.state||{}).reason||'—']]));
 // TradingView TA intake (observe-only)
 const tv=s.tradingview||{};
 if(tv.enabled){
   const vbs=tv.tradingview_valid_by_symbol||{};
   cards.appendChild(card('TradingView signals (observe-only)',[['received',tv.tradingview_alerts_received],['valid',tv.tradingview_alerts_valid,'ok'],['rejected',tv.tradingview_alerts_rejected,'bad'],...Object.entries(vbs).map(([k,v])=>['· valid '+k,v]),...Object.entries(tv.tradingview_reject_reasons||{}).filter(([,v])=>v>0).map(([k,v])=>['· rej '+k,v,'bad']),['observe-only',tv.tradingview_observe_only?'yes':'no','muted']]));
   const lbs=tv.tradingview_latest_by_symbol||{};
   const latestRows=Object.entries(lbs).map(([sym,e])=>[sym,(e.direction||'—')+' · '+(e.timeframe||'?')+'m · '+(e.indicator_name||'')]);
   if(latestRows.length) cards.appendChild(card('TradingView latest signal',latestRows));
   // RSI alert-history trend + next-5min prediction
   const rs=tv.rsi_trend||{},ct=rs.current_trend||{},np=rs.next_window_prediction||{};
   const rrows=[];
   Object.entries(ct).forEach(([sym,t])=>rrows.push(['trend '+sym,(t.last_direction||'—')+' · streak '+(t.streak||0)]));
   Object.entries(np).forEach(([sym,pr])=>rrows.push(['next '+sym, pr&&pr.prediction?(pr.prediction+' '+f((pr.prob_up||0)*100,0)+'%'):((pr&&pr.reason)||'—'), pr&&pr.prediction?(pr.prediction==='UP'?'ok':'bad'):'muted']));
   rrows.push(['pred accuracy', rs.prediction_accuracy==null?'—':f(rs.prediction_accuracy*100,1)+'%']);
   rrows.push(['scored', rs.predictions_scored||0]);
   cards.appendChild(card('RSI trend → next 5-min',rrows));
   // does the TA actually predict the 5-min outcome?
   const ed=tv.edge_vs_5min_outcome||{};
   cards.appendChild(card('TA edge vs 5-min outcome',[['verdict',ed.verdict||'—',(ed.verdict==='signal_predictive_edge'?'ok':(ed.verdict==='signal_inverse_edge'?'bad':'muted'))],['signal hit-rate',ed.signal_hit_rate==null?'—':f(ed.signal_hit_rate*100,1)+'%'],['baseline up-rate',ed.baseline_up_rate==null?'—':f(ed.baseline_up_rate*100,1)+'%'],['aligned win-rate',ed.aligned_bot_win_rate==null?'—':f(ed.aligned_bot_win_rate*100,1)+'%'],['opposed win-rate',ed.opposed_bot_win_rate==null?'—':f(ed.opposed_bot_win_rate*100,1)+'%'],['settled w/ signal',ed.n_settled_with_signal||0]]));
 }
 // positions
 const pos=(l&&l.positions)||[];const pc=$(`<div class="card" style="grid-column:1/-1"><h2>Recent paper positions</h2></div>`);
 const tb=$(`<table><thead><tr><th>window</th><th>side</th><th>entry</th><th>fair</th><th>s_open→s_close</th><th>won</th><th>pnl</th></tr></thead><tbody></tbody></table>`);
 pos.slice(0,12).forEach(x=>{const won=x.won==null?'—':(x.won?'✓':'✗');const cl=x.won==null?'muted':(x.won?'':'bad');
   tb.querySelector('tbody').appendChild($(`<tr><td>${(x.title||'').slice(-20)}</td><td>${x.side}</td><td>${f(x.entry_price,3)}</td><td>${f(x.fair_at_entry,3)}</td><td class="muted">${f(x.s_open)}→${f(x.s_close)}</td><td class="${cl}">${won}</td><td class="${(x.pnl_usd||0)>=0?'':'bad'}" style="color:${x.pnl_usd==null?'var(--mut)':((x.pnl_usd>=0)?'var(--grn)':'var(--red)')}">${x.pnl_usd==null?'—':money(x.pnl_usd)}</td></tr>`))});
 pc.appendChild(tb);cards.appendChild(pc);
}
tick();setInterval(tick,3000);
</script></body></html>"""
