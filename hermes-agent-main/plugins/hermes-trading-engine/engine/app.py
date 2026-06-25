"""Slim read-only API for the BTC 5-minute pulse PAPER engine.

After the focused redesign, the only HTTP surface is health + read-only pulse status/ledger
(served from the JSON the pulse engine writes to ``HTE_DATA_DIR``). There is no trading,
mode, or live-execution endpoint — this engine is PAPER ONLY and the loop runs in the
separate ``scripts/run_btc_pulse.py`` process.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

logger = logging.getLogger("hte.app")

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
                                       "/api/polymarket/training/btc_pulse/ledger",
                                       _tv_webhook_path()]})


def _tv_webhook_path() -> str:
    return (os.getenv("TRADINGVIEW_WEBHOOK_PATH", "/webhooks/tradingview")
            or "/webhooks/tradingview").strip()


def _tv_webhook_upstream() -> str:
    return (os.getenv("TRADINGVIEW_WEBHOOK_UPSTREAM", "http://hermes-training:8787")
            or "http://hermes-training:8787").rstrip("/")


@app.post(_tv_webhook_path())
async def tradingview_webhook_proxy(request: Request) -> Response:
    """Proxy TradingView alerts to the pulse loop webhook on port 80.

    TradingView only allows HTTP on port 80. The real listener runs inside ``hermes-training``;
    this endpoint forwards POST bodies unchanged (observe-only intake).
    """
    body = await request.body()
    headers: dict[str, str] = {}
    for name in ("Content-Type", "X-Tradingview-Secret"):
        val = request.headers.get(name)
        if val:
            headers[name] = val
    url = f"{_tv_webhook_upstream()}{_tv_webhook_path()}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            upstream = await client.post(url, content=body, headers=headers)
    except httpx.ConnectError:
        logger.warning("tradingview webhook upstream unavailable url=%s", url)
        return JSONResponse(
            {"ok": False, "reason": "webhook_upstream_unavailable", "observe_only": True,
             "hint": "set TRADINGVIEW_WEBHOOK_SECRET on hermes-training and redeploy"},
            status_code=503,
        )
    except httpx.HTTPError as exc:
        logger.warning("tradingview webhook proxy error url=%s err=%s", url, exc)
        return JSONResponse(
            {"ok": False, "reason": "webhook_proxy_error", "observe_only": True},
            status_code=502,
        )
    media = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media)


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
 // ===== PLAIN-ENGLISH SUMMARY (read this first) =====
 const cap=s.capital||{}, gd=s.grok_decider||{}, ver=s.verifier||{}, rl=s.research_loop||{},
       les=s.lessons||{}, lp=(s.loops||{}).loops||{}, cl=s.cex_lead_edge||{}, ar=s.arbitrage||{};
 const onhand=cap.on_hand_capital_usd, start0=cap.starting_capital_usd||500, up=(onhand>=start0);
 const info=(title,lines)=>{const c=$(`<div class="card"><h2>${title}</h2></div>`);
   lines.forEach(t=>{const r=$(`<div style="padding:4px 0;border-bottom:1px dashed #1c2533">${t}</div>`);c.appendChild(r)});return c};
 // 1) Money (full width)
 const diff=(onhand!=null&&start0!=null)?(onhand-start0):null;
 const col=up?'var(--grn)':'var(--red)';
 cards.appendChild($(`<div class="card" style="grid-column:1/-1"><h2>Money on hand (practice money)</h2>`+
   `<div class="big" style="color:${col}">$${(onhand||0).toFixed(2)}</div>`+
   `<div class="sub" style="font-size:14px">$${(onhand||0).toFixed(2)} − $${(start0||0).toFixed(2)} = `+
   `<b style="color:${col}">${money(diff)} (${f(cap.return_pct,1)}%)</b>`+
   ` &nbsp;<span style="color:${col}">${up?'▲ winning':'▼ losing'}</span></div>`+
   `<div class="sub">Directional. <span style="color:var(--grn)">Risk-free arbitrage: ${money(cap.arb_realized_pnl_usd)}</span>`+
   ` &nbsp;·&nbsp; <b>Total alpha: ${money(cap.total_realized_pnl_usd)}</b> (${f(cap.total_return_pct,1)}%)</div>`+
   `<div class="sub">Practice money — no real funds at risk.</div></div>`));
 // 2) Is it working?
 const trading=(L.open_positions>0)?'Placing a trade right now':'Waiting for a setup it likes';
 const wr=(L.win_rate||0)*100, wrtxt=f(wr,0)+'% of trades won'+((wr>47&&wr<53)?' (about a coin flip)':'');
 cards.appendChild(info('Is the bot working?',[
   'Status: <b style="color:var(--grn)">Running</b>',
   'Right now: <b>'+trading+'</b>',
   'Trades placed: <b>'+(L.trades||0)+'</b> ('+(L.settled||0)+' finished)',
   'Track record: <b>'+wrtxt+'</b>']));
 // 3) How it decides
 cards.appendChild(info('How the bot decides a trade',[
   '1) <b>Grok</b> (AI #1) reads the market and picks UP, DOWN, or SKIP',
   '2) <b>Claude</b> (AI #2) double-checks it and can say NO',
   '3) Safety brakes can still stop it',
   '&rarr; Only trades <b>both AIs allow</b> are placed',
   '<span class="muted">Grok decided '+(gd.decided||0)+' times · Claude approved '+(ver.approvals||0)+', blocked '+(ver.vetoes||0)+'</span>']));
 // 4) edge
 const va=gd.view_accuracy, edges=(gd.view_edge_candidates||[]);
 cards.appendChild(info('Has it found a winning edge yet?',[
   "Grok's up/down guesses are right <b>"+(va==null?'—':f(va*100,0)+'%')+"</b> of the time <span class=\"muted\">(50% = pure luck)</span>",
   'Proven winning setups: <b>'+(edges.length?edges.map(e=>e.dimension+'='+e.bucket).join(', '):'none yet')+'</b>',
   '<b style="color:'+(edges.length?'var(--grn)':'var(--mut)')+'">'+(edges.length?'Found some — it now bets bigger on those':'Not yet — still learning, keeping bets small and safe')+'</b>']));
 // 5) boldness
 const ag=(gd.aggression||{}).aggression, aglvl=(ag==null?'—':(ag<0.34?'Low (careful)':(ag<0.67?'Medium':'High (bold)')));
 cards.appendChild(info('How bold is the bot right now?',[
   'Boldness: <b>'+aglvl+'</b>',
   '<span class="muted">It automatically gets bolder when it wins and more careful when it loses.</span>']));
 // 6) safety
 const cb=gd.circuit_breaker||{}, balanced=((s.reconciliation||{}).global_reconciled!==false);
 cards.appendChild(info('Safety',[
   'Safety brake: '+(cb.tripped?('<b style="color:var(--red)">STOPPED ('+(cb.reason||'')+')</b>'):'<b style="color:var(--grn)">OK</b>'),
   'Max loss allowed per day: <b>$'+(cb.daily_loss_cap_usd!=null?cb.daily_loss_cap_usd:'—')+'</b> (used $'+f(cb.daily_follow_loss_usd,2)+')',
   'Books balanced: '+(balanced?'<b style="color:var(--grn)">Yes</b>':'<b style="color:var(--red)">No</b>'),
   'Real money at risk: <b>None (paper)</b>']));
 // 7) lessons
 const lessons=(les.recent||[]).slice(-6).reverse();
 cards.appendChild(info('What the bot has learned ('+(les.count||0)+' rules)',
   lessons.length?lessons.map(l=>'<span class="muted">['+(l.kind||'')+']</span> '+(l.rule||'')):
   ['<span class="muted">No lessons yet — it writes a rule each time something wins or loses.</span>']));
 // 8) helper loops (plain names)
 const friendly={heartbeat:'Heartbeat',data_ingestion:'Market data feed',signal_generation:'Decider (Grok)',verifier:'Double-checker (Claude)',execution:'Order placer',arbitrage:'Arbitrage (risk-free)',risk_monitor:'Risk monitor',news:'News reader',research_meta:'Researcher (Claude)',lessons:'Memory'};
 cards.appendChild(info("The bot's helpers (all running loops)",
   Object.keys(friendly).filter(k=>lp[k]).map(k=>'<span style="color:var(--grn)">&#10003;</span> '+friendly[k])));
 // divider into technical detail
 cards.appendChild($(`<div style="grid-column:1/-1;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;font-size:11px;padding:6px 2px;border-top:1px solid var(--bd)">Technical details below &darr;</div>`));
 cards.appendChild(card('Ledger',[['trades',L.trades],['settled',L.settled],['wins',L.wins],['win-rate',f((L.win_rate||0)*100,1)+'%'],['avg entry',f(L.avg_entry_price,3)],['edge realized',f(L.edge_realized,3)],['avg pnl/trade',money(L.avg_pnl_per_trade)],['open',L.open_positions]]));
 cards.appendChild(card('Oracle (reference model)',[['feed type',o.oracle_feed_type||'—'],['symbol',o.oracle_symbol||'—'],['price source',p.source||'—'],['Chainlink btc/usd',f(rt.latest&&rt.latest['crypto_prices_chainlink:btc/usd'])],['RTDS connected',rt.connected?'yes':'no',rt.connected?'ok':'bad'],['open/close snap',o.open_snapshot_source||'—'],['σ/sec',f(p.sigma_per_sec,6)],['sampler',p.sampler_running?(p.sampler_interval_s+'s'):'off']]));
 cards.appendChild(card('Lead feeds (features only)',[['binance btcusdt',f(lf.binance_btcusdt&&lf.binance_btcusdt.price)],['coinbase btcusd',f(lf.coinbase_btcusd&&lf.coinbase_btcusd.price)],['settlement eligible','no','muted']]));
 cards.appendChild(card('Execution gate',[['candidates',eg.candidates],['accepted (fills)',eg.accepted,'ok'],['rejected',eg.rejected_total,'bad'],...Object.entries(eg.rejected||{}).filter(([,v])=>v>0).map(([k,v])=>['· '+k,v,'bad']),['reconciled',eg.reconciled?'yes':'NO',eg.reconciled?'ok':'bad']]));
 cards.appendChild(card('Settlement & calibration',[['sources',JSON.stringify(L.settle_sources||{})],['proxy vs official','both '+(rec.both||0)+' · agree '+(rec.agree||0)+' · disagree '+(rec.disagree||0)],['Brier',f(c.brier,3)+' (base 0.25)'],['log-loss',f(c.log_loss,3)],['samples',c.samples],['base-rate up',f(c.base_rate_up,2)]]));
 cards.appendChild(card('Grok event-risk overlay',[['enabled',g.enabled?'yes':'no'],['regime',(g.state||{}).regime||'—'],['blackout',(g.state||{}).blackout?'YES':'no',(g.state||{}).blackout?'bad':''],['calls',g.calls],['reason',(g.state||{}).reason||'—']]));
 // Grok Decision Engine — detailed (Grok decides, bot executes; PAPER ONLY)
 if(gd.enabled){const cb=gd.circuit_breaker||{},nd=gd.news_digest||{};
   cards.appendChild(card('Grok Decision Engine',[['mode',gd.mode||'off',gd.mode==='follow'?'ok':'muted'],['follows trades',gd.affects_trading?'YES':'no (shadow)',gd.affects_trading?'ok':'muted'],['decided',gd.decided],['errors',gd.errors,(gd.errors>0?'bad':'')],['avg latency',gd.avg_latency_s==null?'—':f(gd.avg_latency_s,1)+'s'],['direction acc',gd.direction_accuracy==null?'—':f(gd.direction_accuracy*100,1)+'%',(gd.direction_accuracy>0.5?'ok':(gd.direction_accuracy==null?'muted':'bad'))],['brier',f(gd.brier,3)],['abstains',gd.abstains],['follow fraction',f(gd.follow_fraction,2)],['breaker',cb.tripped?('TRIPPED: '+(cb.reason||'')):'ok',cb.tripped?'bad':'ok'],['consec losses',cb.consecutive_losses],['news',nd.enabled?((nd.latest&&nd.latest.sentiment||'—')+' · risk '+((nd.latest&&nd.latest.event_risk)||'—')):'off',nd.enabled?'':'muted']]));}
 if(cl.enabled){const clb=(cl.buckets||[]).slice(0,4).map(b=>['· '+b.bucket+' (n'+b.n+')','acc '+f((b.accuracy||0)*100,0)+'% · cexBrier '+f(b.brier_cex,3)+' vs mkt '+f(b.brier_market,3),(b.proven?'ok':(b.beats_market?'':'muted'))]);cards.appendChild(card('CEX-lead latency edge',[['mode',cl.mode||'off',cl.mode==='gated'?'ok':'muted'],['drives trades',cl.affects_trading?'YES':'no (shadow)',cl.affects_trading?'ok':'muted'],['signals seen',cl.signals_seen||0],['graded',cl.graded||0],['drove entries',cl.drove_entries||0],['any proven (beats market)',cl.any_proven?'YES':'not yet',cl.any_proven?'ok':'muted'],...clb]));}
if(ar&&ar.risk_free){cards.appendChild(card('Risk-free arbitrage (dutch book)',[['strategy','buy<$1 / sell>$1','muted'],['P&L segregated',ar.segregated_from_directional?'yes (not blended)':'no',ar.segregated_from_directional?'ok':'bad'],['opportunities found',ar.detected_actionable||0,(ar.detected_actionable>0?'ok':'muted')],['sell-both seen',ar.sell_both_detected||0,'muted'],['executed',ar.executed||0,(ar.executed>0?'ok':'muted')],['· buy / sell',(ar.executed_buy||0)+' / '+(ar.executed_sell||0),'muted'],['settled',ar.settled||0],['open',ar.open||0],['realized profit',money(ar.realized_profit_usd),(ar.realized_profit_usd>0?'ok':'muted')]]));}
if(ver.enabled){cards.appendChild(card('Verifier (Claude maker-checker)',[['verified',ver.verified],['approved',ver.approvals,'ok'],['vetoed',ver.vetoes,'bad'],['errors',ver.errors,(ver.errors>0?'bad':'')],['approve rate',ver.approve_rate==null?'—':f(ver.approve_rate*100,0)+'%'],['avg latency',ver.avg_latency_s==null?'—':f(ver.avg_latency_s,1)+'s']]));}
 if(rl.enabled){cards.appendChild(card('Research loop (Claude)',[['runs',rl.calls],['lessons added',rl.lessons_added],['auto-apply',rl.auto_apply?'on':'off'],['last summary',((rl.last_note||{}).summary||'—').slice(0,80)]]));}
 // Gating architecture: learned selectivity + entry gates (apply on the baseline arm; bypassed when Grok follows)
 const sg=s.learned_selectivity_gate||{},cgx=(s.tradingview||{}).context_gate||{},lw=s.late_window_entry||{},cfgs=s.config||{};
 cards.appendChild(card('Learned selectivity gate',[['rule',sg.decision_rule||'—'],['accepted',sg.accepted],['rejected',sg.rejected,(sg.rejected>0?'bad':'')],['explored',sg.explored],['confidence z',f(sg.confidence_z,2)]]));
 const mg=(s.tradingview||{}).mtf_gate||{};
 cards.appendChild(card('Entry gates',[['context gate',cgx.enabled?'on':'off',cgx.enabled?'ok':'muted'],['· blocked',cgx.blocked||0,(cgx.blocked>0?'bad':'')],...Object.entries(cgx.block_reasons||{}).map(([k,v])=>['· '+k,v,'bad']),['mtf gate',mg.enabled?'on':'off',mg.enabled?'ok':'muted'],['require confirm',mg.require_confirm?'yes':'no','muted'],['side align',mg.require_side_align?'yes':'no','muted'],['· mtf blocked',mg.blocked||0,(mg.blocked>0?'bad':'')],...Object.entries(mg.block_reasons||{}).map(([k,v])=>['· '+k,v,'bad']),['late-window',(lw.gate||{}).enabled?'on':'off',(lw.gate||{}).enabled?'ok':'muted'],['late-window verdict',(lw.edge_measurement||{}).verdict||'—'],['reward/risk floor',f(cfgs.min_reward_risk,2)]]));
 // Closed-loop learning (the bot's own experience adjusting its decisions)
 const ln=s.learning||{};
 cards.appendChild(card('Learning (closed loop)',[['enabled',ln.enabled?'yes':'no',ln.enabled?'ok':'muted'],['active (influencing)',ln.active?'YES':'no',ln.active?'ok':'muted'],['weight',ln.weight==null?'—':f(ln.weight,3)],['reason',ln.reason||'—'],['model labels',ln.model_n_labeled],['calibration err',ln.model_calibration_error==null?'—':f(ln.model_calibration_error,3)],['paper-only',ln.paper_only?'yes':'no','muted'],['gate authoritative',ln.execution_gate_still_authoritative?'yes':'no','muted']]));
 // TradingView TA intake (observe-only)
 const tv=s.tradingview||{};
 if(tv.enabled){
   const vbs=tv.tradingview_valid_by_symbol||{};
   cards.appendChild(card('TradingView signals (observe-only)',[['received',tv.tradingview_alerts_received],['valid',tv.tradingview_alerts_valid,'ok'],['rejected',tv.tradingview_alerts_rejected,'bad'],...Object.entries(vbs).map(([k,v])=>['· valid '+k,v]),...Object.entries(tv.tradingview_reject_reasons||{}).filter(([,v])=>v>0).map(([k,v])=>['· rej '+k,v,'bad']),['observe-only',tv.tradingview_observe_only?'yes':'no','muted']]));
   const mtf=tv.tradingview_mtf_confirmation||{};
   const mtfOk=mtf.confirm==='confirmed_up'||mtf.confirm==='confirmed_down';
   cards.appendChild(card('TV 1m+5m cross-confirm (BTCUSDT)',[
     ['feature symbol',tv.tradingview_feature_symbol||'BTCUSDT'],
     ['confirm',mtf.confirm||'none',mtfOk?'ok':(mtf.confirm==='conflict'?'bad':'muted')],
     ['1m direction',mtf.tf_1m_dir||'—'],
     ['5m direction',mtf.tf_5m_dir||'—'],
     ['1m age',mtf.tf_1m_age_s==null?'—':mtf.tf_1m_age_s+'s'],
     ['5m age',mtf.tf_5m_age_s==null?'—':mtf.tf_5m_age_s+'s'],
     ['window',(tv.tradingview_mtf_confirmation&&'6min fresh window')||'—','muted']]));
   const tfb=tv.tradingview_latest_by_timeframe||{};
   const tfRows=Object.entries(tfb).filter(([k])=>k.startsWith('BTCUSDT@')).map(([k,e])=>[k.replace('BTCUSDT@',''),(e.direction||'—')+' · str '+(e.strength==null?'—':e.strength)]);
   if(tfRows.length) cards.appendChild(card('TradingView by timeframe (BTCUSDT)',tfRows));
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
