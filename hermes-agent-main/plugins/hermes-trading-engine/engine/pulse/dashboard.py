"""Read-only BTC pulse dashboard HTML (embedded SPA) — one-screen traffic lights."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/>
<title>BTC Pulse · Master view</title>
<style>
:root{
  --bg:#12141a;--bg2:#181b24;--card:#1c2029;--line:#2a3040;
  --text:#f0f4f8;--text2:#dde4ee;--text3:#c8d2de;
  --green:#4ade80;--yellow:#facc15;--red:#f87171;--accent:#a8c8f0;
  --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:26px/1.45 "Segoe UI",system-ui,sans-serif}
header{
  padding:14px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);background:var(--bg2);
}
h1{font-size:34px;font-weight:600;margin:0}
.tag{font-size:22px;padding:5px 14px;border-radius:16px;background:var(--card);color:var(--text2)}
.tag.live{color:var(--green)}
.tag.warn{color:var(--yellow)}
.tag.off{color:var(--red)}
main{max-width:min(1680px,100%);margin:0 auto;padding:14px 20px 24px}
.cap-bar{
  display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 20px;
  background:linear-gradient(135deg,var(--card) 0%,#222836 100%);
  border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 20px;margin-bottom:14px;
}
.cap-main{font-size:40px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums;color:var(--text)}
.cap-label{font-size:22px;color:var(--text2);margin-top:2px}
.cap-sub{font-size:22px;color:var(--text2)}
.cap-sub b{color:var(--text);font-weight:600}
.cap-sub .up{color:var(--green)}.cap-sub .dn{color:var(--red)}
.verdict{
  display:flex;align-items:center;gap:8px;font-size:26px;font-weight:600;
  padding:10px 16px;border-radius:var(--radius);background:var(--card);border:1px solid var(--line);
  margin-bottom:12px;
}
.content-split{
  display:grid;grid-template-columns:1fr 396px;gap:12px 24px;align-items:start;
}
.trades-col{padding:0;margin:0}
.trades-head{
  margin:0 0 8px;font-size:20px;font-weight:600;color:var(--accent);
  text-transform:uppercase;letter-spacing:.06em;
}
.trade-line{
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:6px 0;font-size:22px;line-height:1.35;
  border-bottom:1px solid rgba(42,48,64,.45);
}
.trade-line:last-child{border-bottom:0}
.trade-info{min-width:0;color:var(--text2)}
.trade-side{font-weight:600;color:var(--text)}
.trade-side.up{color:var(--green)}.trade-side.down{color:var(--red)}
.trade-tag{font-size:19px;color:var(--text2);margin-left:4px}
.trade-tag.win{color:var(--green)}.trade-tag.loss{color:var(--red)}.trade-tag.open{color:var(--yellow)}
.trade-pnl{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap;font-size:22px}
.trade-pnl.up{color:var(--green)}.trade-pnl.dn{color:var(--red)}.trade-pnl.neu{color:var(--text2)}
.trades-empty{color:var(--text2);font-size:22px}
.tl-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(456px,1fr));gap:8px;min-width:0;
}
.tl-row{
  display:grid;grid-template-columns:26px 1fr auto;gap:8px;align-items:center;
  padding:8px 12px;background:var(--card);border:1px solid var(--line);border-radius:8px;
}
.tl-row:hover{border-color:#3d4658}
.tl-dot{width:17px;height:17px;border-radius:50%;flex-shrink:0}
.tl-green{background:var(--green);box-shadow:0 0 6px rgba(74,222,128,.55)}
.tl-yellow{background:var(--yellow);box-shadow:0 0 6px rgba(250,204,21,.45)}
.tl-red{background:var(--red);box-shadow:0 0 6px rgba(248,113,113,.55)}
.tl-name{font-size:24px;color:var(--text)}
.tl-val{font-size:22px;color:var(--text2);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tl-hint{
  grid-column:1/-1;font-size:20px;color:var(--text2);
  padding-top:6px;margin-top:2px;border-top:1px solid var(--line);
}
.tl-section{
  grid-column:1/-1;font-size:20px;font-weight:600;color:var(--accent);
  text-transform:uppercase;letter-spacing:.06em;padding:8px 2px 4px;
}
.foot{margin-top:14px;color:var(--text2);font-size:20px}
@media(max-width:960px){
  .content-split{grid-template-columns:1fr}
  .trades-col{margin-top:4px}
}
@media(max-width:420px){
  .tl-grid{grid-template-columns:1fr}
  .cap-main{font-size:34px}
}
</style>
</head>
<body>
<header>
  <h1>BTC Pulse</h1>
  <span class="tag">Paper only</span>
  <span class="tag" id="health">Loading…</span>
  <span class="tag" id="meta" style="color:var(--text3)"></span>
</header>
<main>
  <div class="cap-bar" id="cap-bar"></div>
  <div class="verdict" id="verdict"></div>
  <div class="content-split">
    <div class="tl-grid" id="tl-grid"></div>
    <aside class="trades-col">
      <div class="trades-head">Last 10 trades</div>
      <div id="trades-list"></div>
    </aside>
  </div>
  <div class="foot">Refreshes every 5s · read-only · total capital = start + arb + dep-arb + directional</div>
</main>
<script>
const $=(h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild};
const f=(x,d=2)=>x==null||x===''?'—':(typeof x==='number'?x.toFixed(d):String(x));
const usd=(x)=>x==null?'—':'$'+Number(x).toFixed(2);
const pct=(x)=>x==null?'—':(x>=0?'+':'')+Number(x).toFixed(2)+'%';
const dot=(c)=>'<span class="tl-dot tl-'+c+'"></span>';

function fmtAge(sec){
  if(sec==null)return '—';
  const s=Math.round(Number(sec));
  if(!Number.isFinite(s)||s<0)return '—';
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m';
  return Math.floor(s/3600)+'h';
}
function fmtTsShort(ts){
  if(ts==null)return '—';
  try{
    const d=new Date(Number(ts)*1000);
    return d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  }catch(e){return '—';}
}
function tradeOutcome(x){
  const st=(x.status||'').toLowerCase();
  if(st==='open')return {label:'open',cls:'open',pnlCls:'neu',pnl:'—'};
  if(x.won===true)return {label:'win',cls:'win',pnlCls:'up',pnl:usd(x.pnl_usd)};
  if(x.won===false)return {label:'loss',cls:'loss',pnlCls:'dn',pnl:usd(x.pnl_usd)};
  return {label:st||'—',cls:'',pnlCls:'neu',pnl:x.pnl_usd==null?'—':usd(x.pnl_usd)};
}
function renderTrades(listEl,positions){
  listEl.innerHTML='';
  const pos=(positions||[]).slice(0,10);
  if(!pos.length){
    listEl.innerHTML='<div class="trades-empty">No trades yet.</div>';
    return;
  }
  pos.forEach(x=>{
    const r=x.research||{};
    const side=(x.side||'—').toUpperCase();
    const sideCls=side==='UP'?'up':(side==='DOWN'?'down':'');
    const series=r.series_label||r.market_series||'—';
    const oc=tradeOutcome(x);
    const row=$('<div class="trade-line"></div>');
    row.innerHTML=
      '<div class="trade-info"><span class="trade-side '+sideCls+'">'+side+'</span>'
      +'<span class="trade-tag '+oc.cls+'">'+oc.label+'</span>'
      +' <span class="trade-tag">'+series+' @'+f(x.entry_price,2)+'</span>'
      +'<br><span class="trade-tag">'+fmtTsShort(x.entry_ts)+'</span></div>'
      +'<div class="trade-pnl '+oc.pnlCls+'">'+oc.pnl+'</div>';
    listEl.appendChild(row);
  });
}

function addRow(rows,name,val,hint,light){
  rows.push({name,val,hint,light});
}
function addSection(rows,title){
  rows.push({section:title});
}

function buildRows(s){
  const rows=[];
  const cap=s.capital||{};
  const L=s.ledger||{};
  const price=s.price||{};
  const tv=s.tradingview||{};
  const gd=s.grok_decider||{};
  const gsi=s.grok_signal_intel||{};
  const ver=s.verifier||{};
  const rl=s.research_loop||{};
  const les=s.lessons||{};
  const arb=s.arbitrage||{};
  const dep=s.dependency_arbitrage||{};
  const dr=s.directional_risk||{};
  const cb=(gd.circuit_breaker||{});
  const loops=s.loops||{};
  const loopMap=(loops.loops||{});
  const lc=s.decision_lifecycle||{};
  const rbs=lc.rejected_by_stage||{};
  const topGate=Object.entries(rbs).sort((a,b)=>b[1]-a[1])[0];
  const mtf=tv.tradingview_mtf_confirmation||{};
  const tfs=tv.tradingview_mtf_timeframes||mtf.mtf_timeframes||['2','3','4'];
  const mtfN=mtf.mtf_count||tfs.length;
  const mtfVerdict=mtf['confirm_'+mtfN+'tf']||mtf.confirm_mtf||mtf.confirm_3tf||'none';
  const fresh=mtf.trend_fresh_count||0;
  const rej=tv.tradingview_reject_reasons||{};
  const wh=tv.webhook||{};
  const aA=gsi.analyst_A||{};
  const pB=gsi.predictor_B||{};
  const reconciled=(s.reconciliation||{}).global_reconciled!==false;
  const statusAge=(Date.now()/1000)-(Number(s.ts)||0);
  const stalled=(loops.stalled||[]).length;
  const totalOnHand=cap.total_on_hand_usd;
  const totalPnl=cap.total_realized_pnl_usd;
  const wr=(L.win_rate||0)*100;

  addSection(rows,'Money');
  addRow(rows,'Total on-hand',usd(totalOnHand),
    'Start '+usd(cap.starting_capital_usd)+' + all realized PnL',
    totalOnHand>=500?'green':(totalOnHand>=480?'yellow':'red'));
  addRow(rows,'Total PnL',pct(cap.total_return_pct)+' ('+usd(totalPnl)+')',
    'Arb '+usd(cap.arb_realized_pnl_usd)+' · Dep '+usd(cap.dependency_arb_realized_pnl_usd)+' · Dir '+usd(cap.realized_pnl_usd),
    totalPnl>0?'green':(totalPnl>=0?'yellow':'red'));
  addRow(rows,'Win rate',f(wr,1)+'% · '+f(L.trades,0)+' trades',
    f(L.settled,0)+' settled · open '+f(L.open_positions,0),
    wr>=55?'green':(wr>=45?'yellow':'red'));
  addRow(rows,'Open exposure',usd(dr.open_exposure_usd||cap.open_exposure_usd)+' / '+usd(dr.bankroll_cap_usd||cap.directional_bankroll_cap_usd||50),
    'Directional cap remaining',
    (dr.open_exposure_usd||cap.open_exposure_usd||0)<=(dr.bankroll_cap_usd||50)?'green':'red');

  addSection(rows,'Engine');
  addRow(rows,'Bot alive','ticks '+f(s.ticks,0)+' · age '+fmtAge(statusAge),
    s.paper_only&&!s.live_trading_enabled?'paper mode OK':'CHECK LIVE FLAG',
    statusAge<45&&s.ticks>5?'green':(statusAge<120?'yellow':'red'));
  addRow(rows,'Loops',(loops.all_live?'all live':'check')+' · '+f(loops.count,0)+' registered',
    stalled?stalled+' stalled':'0 stalled',
    stalled===0&&loops.all_live!==false?'green':(stalled?'red':'yellow'));
  addRow(rows,'Ledger reconcile',reconciled?'yes':'NO',
    'Books must match',
    reconciled?'green':'red');
  addRow(rows,'Circuit breaker',cb.tripped?(cb.reason||'TRIPPED'):'OK',
    'Daily loss used '+usd(cb.daily_follow_loss_usd)+' / cap '+usd(cb.daily_loss_cap_usd),
    cb.tripped?'red':'green');

  addSection(rows,'Price feed');
  const pAge=price.age_s;
  addRow(rows,'BTC price',usd(price.last_price)+' · '+f(pAge,1)+'s old',
    (price.source||'—')+' · polls '+f(price.polls,0),
    pAge!=null&&pAge<15?'green':(pAge!=null&&pAge<60?'yellow':'red'));
  addRow(rows,'Vol sampler',price.sampler_running?'running':'off',
    'samples '+f(price.vol_samples,0),
    price.sampler_running?'green':'red');

  addSection(rows,'TradingView');
  addRow(rows,'Webhook',wh.listening?'listening':'DOWN',
    f(tv.tradingview_alerts_valid,0)+' valid / '+f(tv.tradingview_alerts_received,0)+' recv',
    wh.listening?'green':'red');
  const tvRej=tv.tradingview_alerts_rejected||0;
  const rejBits=Object.entries(rej).filter(([,n])=>Number(n)>0)
    .sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+' '+v);
  const rejHint=tvRej===0?'no rejects':(rejBits.length?rejBits.join(' · '):'legacy scrubbed');
  addRow(rows,'TV rejects',f(tvRej,0),
    rejHint,
    tvRej===0?'green':(tvRej<=3?'yellow':'red'));
  tfs.forEach(tf=>{
    const dir=mtf['tf_'+tf+'m_dir'];
    const age=mtf['tf_'+tf+'m_age_s'];
    const win=mtf['tf_'+tf+'m_window_s'];
    const stale=dir==null&&age!=null&&win!=null&&age>win;
    addRow(rows,tf+'m chart',(dir||'—')+(age!=null?' · '+f(age,0)+'s':''),
      stale?'stale — refire alert':'fresh',
      dir&&!stale?'green':(dir?'yellow':'red'));
  });
  addRow(rows,'MTF verdict',mtfVerdict,
    fresh+'/'+mtfN+' fresh · observe-only trade gate',
    mtfVerdict.includes('confirmed')&&fresh>=2?'green':(fresh>=1?'yellow':'red'));

  addSection(rows,'Grok AI');
  addRow(rows,'Decider C',(gd.mode||'off')+' · '+f(gd.decided,0)+' decided',
    'abstains '+f(gd.abstains,0)+' · errors '+f(gd.errors,0),
    gd.enabled&&gd.mode==='shadow'&&(gd.errors||0)<10?'green':((gd.errors||0)>=10?'red':'yellow'));
  addRow(rows,'Predictor B',pB.enabled?'on':'off',
    f(pB.predicted,0)+' predicted · acc '+f((pB.accuracy||0)*100,1)+'%',
    pB.enabled&&(pB.errors||0)<5?'green':'yellow');
  addRow(rows,'Analyst A',aA.enabled?'on':'off',
    f(aA.calls,0)+' calls · errors '+f(aA.errors,0),
    aA.enabled&&aA.errors===0?'green':'yellow');
  const bud=(gsi.budget||{});
  addRow(rows,'Grok budget','$'+f(bud.daily_usd_cap||50,0)+'/day',
    'spent '+usd(bud.spent_usd_today||0),
    'green');

  addSection(rows,'Claude');
  addRow(rows,'Verifier',ver.enabled?'on':'off',
    f(ver.verified,0)+' verified · '+f(ver.vetoes,0)+' vetoes · err '+f(ver.errors,0),
    ver.enabled&&ver.errors===0?'green':(ver.errors>0?'red':'yellow'));
  addRow(rows,'Research loop',rl.enabled?'on':'off',
    rl.enabled?(f(rl.calls,0)+' calls · auto-apply '+(rl.auto_apply?'yes':'no')):'disabled in env',
    rl.enabled?'green':'yellow');
  addRow(rows,'Lessons',f(les.active||0,0)+' active / '+f(les.count||0,0)+' total',
    'compounding memory',
    (les.count||0)>0?'green':'yellow');

  addSection(rows,'Gates & readiness');
  addRow(rows,'Top gate block',topGate?topGate[0]+' ('+topGate[1]+')':'none',
    'expected heavy blocks in learning mode',
    'yellow');
  const readiness=(s.readiness||{});
  addRow(rows,'Promotion',readiness.status||'—',
    readiness.reason||'directional not ready until ladder green',
    readiness.status==='ready'?'green':'yellow');
  addRow(rows,'Live trading',s.live_trading_enabled?'ON':'OFF',
    'must stay OFF in learning',
    s.live_trading_enabled?'red':'green');

  addSection(rows,'Arbitrage');
  addRow(rows,'Dutch-book arb',f(arb.arb_scan_count||arb.executed,0)+' scans · '+f(arb.executed,0)+' exec',
    'PnL '+usd(arb.realized_profit_usd),
    (arb.realized_profit_usd||0)>0?'green':'yellow');
  addRow(rows,'Dependency arb',f(dep.scans,0)+' scans · '+f(dep.executed,0)+' exec',
    'PnL '+usd(dep.realized_profit_usd),
    (dep.realized_profit_usd||0)>0?'green':'yellow');

  return rows;
}

function overallLight(s,rows){
  const reds=rows.filter(r=>!r.section&&r.light==='red').length;
  const cap=s.capital||{};
  if(!s.available)return {light:'red',text:'NO DATA'};
  if(s.live_trading_enabled)return {light:'red',text:'LIVE TRADING ON'};
  if((s.reconciliation||{}).global_reconciled===false)return {light:'red',text:'LEDGER BROKEN'};
  if(((s.grok_decider||{}).circuit_breaker||{}).tripped)return {light:'red',text:'BREAKER TRIPPED'};
  if(reds>=3)return {light:'red',text:'MULTIPLE ISSUES ('+reds+')'};
  if(reds>=1)return {light:'yellow',text:'WATCH — '+reds+' issue(s)'};
  if((cap.total_on_hand_usd||0)>500)return {light:'green',text:'HEALTHY · PROFITABLE'};
  return {light:'green',text:'HEALTHY'};
}

function renderRows(grid,rows){
  grid.innerHTML='';
  rows.forEach(r=>{
    if(r.section){
      grid.appendChild($('<div class="tl-section">'+r.section+'</div>'));
      return;
    }
    const el=$('<div class="tl-row"></div>');
    let html=dot(r.light)+'<span class="tl-name">'+r.name+'</span><span class="tl-val">'+r.val+'</span>';
    if(r.hint)html+='<div class="tl-hint">'+r.hint+'</div>';
    el.innerHTML=html;
    grid.appendChild(el);
  });
}

async function fetchJson(url,timeoutMs=20000){
  const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{
    const r=await fetch(url,{cache:'no-store',signal:ctrl.signal});
    if(!r.ok)throw new Error('HTTP '+r.status);
    return await r.json();
  }finally{clearTimeout(t);}
}

function setTag(id,text,cls){
  const el=document.getElementById(id);
  el.textContent=text;
  el.className='tag'+(cls?' '+cls:'');
}

async function tick(){
  setTag('health','Loading…','');
  let s,l;
  try{
    [s,l]=await Promise.all([
      fetchJson('/api/polymarket/training/btc_pulse'),
      fetchJson('/api/polymarket/training/btc_pulse/ledger?summary=1'),
    ]);
  }catch(e){setTag('health',e&&e.name==='AbortError'?'Timed out':'Unreachable','off');return;}
  if(!s.available){setTag('health','No data','off');return;}
  setTag('health','Live','live');
  const cfg=s.config||{};
  document.getElementById('meta').textContent=
    'tick '+f(cfg.tick_seconds,0)+'s · '+new Date().toLocaleTimeString();

  const cap=s.capital||{};
  const total=cap.total_on_hand_usd;
  const pnl=cap.total_realized_pnl_usd;
  const pnlCls=pnl>=0?'up':'dn';
  document.getElementById('cap-bar').innerHTML=
    '<div><div class="cap-main">'+usd(total)+'</div>'
    +'<div class="cap-label">total on-hand paper capital</div></div>'
    +'<div class="cap-sub">Started <b>'+usd(cap.starting_capital_usd)+'</b></div>'
    +'<div class="cap-sub">PnL <b class="'+pnlCls+'">'+(pnl>=0?'+':'')+usd(Math.abs(pnl))+'</b> ('+pct(cap.total_return_pct)+')</div>'
    +'<div class="cap-sub">Arb <b class="up">'+usd(cap.arb_realized_pnl_usd)+'</b></div>'
    +'<div class="cap-sub">Dep-arb <b class="up">'+usd(cap.dependency_arb_realized_pnl_usd)+'</b></div>'
    +'<div class="cap-sub">Directional <b>'+usd(cap.realized_pnl_usd)+'</b></div>';

  const rows=buildRows(s);
  const ov=overallLight(s,rows);
  const v=document.getElementById('verdict');
  v.innerHTML=dot(ov.light)+'<span>'+ov.text+'</span>';
  v.style.borderColor=ov.light==='green'?'rgba(74,222,128,.4)':(ov.light==='yellow'?'rgba(250,204,21,.4)':'rgba(248,113,113,.4)');
  renderRows(document.getElementById('tl-grid'),rows);
  renderTrades(document.getElementById('trades-list'),(l&&l.positions)||[]);
}
tick();setInterval(tick,5000);
</script>
</body>
</html>"""