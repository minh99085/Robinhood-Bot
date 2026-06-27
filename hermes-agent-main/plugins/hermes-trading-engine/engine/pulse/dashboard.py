"""Read-only BTC pulse dashboard HTML (embedded SPA)."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/>
<title>BTC Pulse · Hermes (paper)</title>
<style>
:root{
  --bg:#14161c;--bg2:#1a1d26;--card:#1e222c;--card2:#252a36;
  --text:#d4dae3;--text2:#9aa3b2;--text3:#6e7687;
  --line:#2d3340;--line2:#383f4d;
  --good:#7dcea0;--bad:#d4a5a5;--warn:#d4c4a5;--accent:#8eb8e8;
  --radius:14px;--gap:18px;
}
*{box-sizing:border-box}
body{
  margin:0;background:var(--bg);color:var(--text);
  font:21px/1.65 "Segoe UI",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;
}
header{
  padding:20px 24px 16px;display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);background:var(--bg2);
}
h1{font-size:28px;font-weight:600;margin:0;letter-spacing:-.02em;color:var(--text)}
.tag{font-size:17px;color:var(--text3);padding:4px 10px;border-radius:20px;background:var(--card)}
.tag.live{color:var(--good);background:rgba(125,206,160,.12)}
.tag.off{color:var(--bad);background:rgba(212,165,165,.12)}
main{max-width:1180px;margin:0 auto;padding:24px 20px 40px}
.hero{
  display:grid;grid-template-columns:1.2fr 1fr;gap:var(--gap);margin-bottom:var(--gap);
}
@media(max-width:820px){.hero{grid-template-columns:1fr}}
.panel{
  background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px 22px;
}
.panel.soft{background:var(--bg2);border-color:transparent}
.panel h2{
  margin:0 0 14px;font-size:18px;font-weight:600;color:var(--text2);
  letter-spacing:.01em;text-transform:none;
}
.money{font-size:48px;font-weight:600;letter-spacing:-.03em;line-height:1.2}
.money-sub{margin-top:8px;color:var(--text2);font-size:20px}
.money-sub b{font-weight:600;color:var(--text)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:var(--gap)}
.prose p{margin:0 0 10px;color:var(--text2);font-size:20px}
.prose p:last-child{margin-bottom:0}
.prose b{color:var(--text);font-weight:600}
.kv{display:grid;gap:8px}
.kv-row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)}
.kv-row:last-child{border-bottom:0}
.kv-k{color:var(--text2);font-size:18px}
.kv-v{font-variant-numeric:tabular-nums;color:var(--text);font-size:18px;text-align:right}
.pos{color:var(--good)}.neg{color:var(--bad)}.neu{color:var(--text3)}
.market-table{width:100%;border-collapse:collapse;font-size:18px}
.market-table th,.market-table td{padding:10px 8px;text-align:right;border-bottom:1px solid var(--line)}
.market-table th:first-child,.market-table td:first-child{text-align:left}
.market-table th{color:var(--text3);font-weight:500;font-size:17px}
details.tech{margin-top:28px}
details.tech>summary{
  cursor:pointer;list-style:none;color:var(--text2);font-size:18px;
  padding:12px 0;border-top:1px solid var(--line);user-select:none;
}
details.tech>summary::-webkit-details-marker{display:none}
details.tech[open]>summary{margin-bottom:var(--gap);color:var(--text)}
table.data{width:100%;border-collapse:collapse;font-size:18px}
table.data th,table.data td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
table.data th:first-child,table.data td:first-child{text-align:left}
table.data th{color:var(--text3);font-weight:500}
table.data tr:hover td{background:rgba(255,255,255,.02)}
.coupling-banner{
  display:none;margin:0 20px 0;max-width:1180px;margin-left:auto;margin-right:auto;
  padding:14px 18px;border-radius:var(--radius);border:1px solid rgba(212,196,165,.35);
  background:rgba(212,196,165,.1);color:var(--warn);font-size:18px;
}
.coupling-banner.show{display:block}
.coupling-banner b{color:var(--text)}
.foot{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);color:var(--text3);font-size:17px}
</style>
</head>
<body>
<header>
  <h1>BTC Pulse</h1>
  <span class="tag">Paper only</span>
  <span class="tag" id="health">Connecting…</span>
  <span class="tag neu" id="meta"></span>
</header>
<div class="coupling-banner" id="coupling-banner"></div>
<main>
  <div class="hero" id="hero"></div>
  <div class="grid" id="summary"></div>
  <details class="tech" id="tech-wrap">
    <summary>Show technical details</summary>
    <div class="grid" id="tech"></div>
    <div id="positions"></div>
  </details>
  <div class="foot">Refreshes every 5s · read-only · Chainlink oracle via Polymarket RTDS</div>
</main>
<script>
const $=(h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild};
const f=(x,d=2)=>x==null?'—':(typeof x==='number'?x.toFixed(d):x);
const money=(x)=>x==null?'—':(x>=0?'+$':'-$')+Math.abs(x).toFixed(2);
const pnlCls=(x)=>x==null?'neu':(x>=0?'pos':'neg');
function kvCard(title,rows){
  const c=$('<div class="panel"><h2></h2><div class="kv"></div></div>');
  c.querySelector('h2').textContent=title;
  const kv=c.querySelector('.kv');
  rows.forEach(([k,v,cls])=>{
    const r=$('<div class="kv-row"><span class="kv-k"></span><span class="kv-v"></span></div>');
    r.querySelector('.kv-k').textContent=k;
    const vEl=r.querySelector('.kv-v');
    vEl.textContent=v;
    if(cls) vEl.classList.add(cls);
    kv.appendChild(r);
  });
  return c;
}
function proseCard(title,lines){
  const c=$('<div class="panel soft"><h2></h2><div class="prose"></div></div>');
  c.querySelector('h2').textContent=title;
  const p=c.querySelector('.prose');
  lines.forEach(html=>{const el=$('<p></p>');el.innerHTML=html;p.appendChild(el)});
  return c;
}
async function tick(){
  let s,l;
  try{
    s=await (await fetch('/api/polymarket/training/btc_pulse',{cache:'no-store'})).json();
    l=await (await fetch('/api/polymarket/training/btc_pulse/ledger',{cache:'no-store'})).json();
  }catch(e){
    const h=document.getElementById('health');
    h.textContent='Unreachable';h.className='tag off';return;
  }
  const h=document.getElementById('health');
  if(!s.available){h.textContent='No data';h.className='tag off';return}
  h.textContent='Live';h.className='tag live';
  document.getElementById('meta').textContent='Ticks '+s.ticks+' · '+new Date().toLocaleTimeString();

  const coupling=s.config_coupling||{};
  const cBanner=document.getElementById('coupling-banner');
  if(coupling.active&&!coupling.configured_ok){
    cBanner.className='coupling-banner show';
    cBanner.innerHTML='Gate coupling: <b>PULSE_TV_CONTEXT_MAX_TTC_S='+coupling.configured_s
      +'</b> is too low (need &gt;='+coupling.required_min_s+'s). Runtime clamped to '
      +coupling.effective_s+'s — fix .env.';
  }else if(coupling.active&&coupling.auto_clamped){
    cBanner.className='coupling-banner show';
    cBanner.innerHTML='Gate coupling: env context max was raised at runtime to <b>'
      +coupling.effective_s+'s</b> (configured '+coupling.configured_s+'). Update .env.';
  }else{cBanner.className='coupling-banner';cBanner.innerHTML='';}

  const L=s.ledger||{},cap=s.capital||{},gd=s.grok_decider||{},ver=s.verifier||{};
  const hero=document.getElementById('hero');hero.innerHTML='';
  const onhand=cap.on_hand_capital_usd, start0=cap.starting_capital_usd||500;
  const diff=(onhand!=null&&start0!=null)?(onhand-start0):null;
  const up=diff!=null&&diff>=0;
  hero.appendChild($(`<div class="panel">
    <h2>Capital (paper)</h2>
    <div class="money ${up?'pos':'neg'}">$${f(onhand,2)}</div>
    <div class="money-sub">
      Started $${f(start0,2)} · PnL <b class="${pnlCls(diff)}">${money(diff)}</b>
      (${f(cap.return_pct,1)}%)<br>
      Arb ${money(cap.arb_realized_pnl_usd)} · Total ${money(cap.total_realized_pnl_usd)}
      (${f(cap.total_return_pct,1)}%)
    </div></div>`));

  const lc=s.decision_lifecycle||{}, rbs=lc.rejected_by_stage||{};
  const topGate=Object.entries(rbs).sort((a,b)=>b[1]-a[1])[0];
  const wr=(L.win_rate||0)*100;
  hero.appendChild(proseCard('At a glance',[
    'Status: <b>Running</b> · '+(L.open_positions>0?'has open position':'scanning markets'),
    'Trades <b>'+(L.trades||0)+'</b> ('+(L.settled||0)+' settled) · Win rate <b>'+f(wr,1)+'%</b>',
    topGate?('Most blocks: <b>'+topGate[0]+'</b> ('+topGate[1]+')'):'No gate blocks recorded yet',
    'Grok mode <b>'+(gd.mode||'—')+'</b> · Verifier '+((ver.approvals||0)+' ok / '+(ver.vetoes||0)+' veto')
  ]));

  const summary=document.getElementById('summary');summary.innerHTML='';
  const bySeries=s.by_market_series||{};
  const seriesKeys=Object.keys(bySeries);
  if(seriesKeys.length){
    const mPanel=$('<div class="panel" style="grid-column:1/-1"><h2>Performance by market</h2></div>');
    const tb=$('<table class="market-table"><thead><tr><th>Market</th><th>Settled</th><th>Win rate</th><th>PF</th><th>PnL</th><th>UP</th><th>DOWN</th></tr></thead><tbody></tbody></table>');
    seriesKeys.sort((a,b)=>(bySeries[a].series_label||'').localeCompare(bySeries[b].series_label||''))
      .forEach(k=>{const r=bySeries[k];
        tb.querySelector('tbody').appendChild($(`<tr>
          <td>${r.series_label||k}</td><td>${r.settled||0}</td>
          <td>${r.win_rate==null?'—':f(r.win_rate*100,1)+'%'}</td>
          <td>${f(r.profit_factor,2)}</td>
          <td class="${pnlCls(r.pnl_usd)}">${money(r.pnl_usd)}</td>
          <td>${r.win_rate_up==null?'—':f(r.win_rate_up*100,0)+'%'}</td>
          <td>${r.win_rate_down==null?'—':f(r.win_rate_down*100,0)+'%'}</td>
        </tr>`));
      });
    mPanel.appendChild(tb);summary.appendChild(mPanel);
  }

  const va=gd.view_accuracy, edges=(gd.view_edge_candidates||[]);
  summary.appendChild(proseCard('Edge & learning',[
    "Grok direction accuracy <b>"+(va==null?'—':f(va*100,0)+'%')+"</b> <span class='neu'>(50% = coin flip)</span>",
    'Winning setups: <b>'+(edges.length?edges.slice(0,3).map(e=>e.dimension+'='+e.bucket).join(', '):'still collecting data')+'</b>',
    'Lessons stored: <b>'+((s.lessons||{}).count||0)+'</b>'
  ]));

  const cb=gd.circuit_breaker||{};
  const balanced=((s.reconciliation||{}).global_reconciled!==false);
  summary.appendChild(proseCard('Safety',[
    'Circuit breaker: <b class="'+(cb.tripped?'neg':'pos')+'">'+(cb.tripped?(cb.reason||'tripped'):'OK')+'</b>',
    'Daily loss cap $'+f(cb.daily_loss_cap_usd,0)+' · used $'+f(cb.daily_follow_loss_usd,2),
    'Books reconciled: <b class="'+(balanced?'pos':'neg')+'">'+(balanced?'Yes':'No')+'</b>'
  ]));

  const tv=s.tradingview||{};
  const tvActive=tv.enabled||(tv.tradingview_alerts_valid>0);
  if(tvActive){
    const mtf=tv.tradingview_mtf_confirmation||{};
    const byTf=tv.tradingview_latest_by_timeframe||{};
    const featSym=tv.tradingview_feature_symbol||'BTCUSD';
    const tfs=(tv.tradingview_mtf_timeframes||mtf.mtf_timeframes||['5','10','15']);
    const mtfN=mtf.mtf_count||tfs.length;
    const mtfVerdict=mtf['confirm_'+mtfN+'tf']||mtf.confirm_mtf||mtf.confirm_3tf||mtf.confirm||'none';
    const mtfCls=(mtfVerdict.includes('confirmed')?'pos':(mtfVerdict.includes('conflict')?'neg':'neu'));
    const tvPanel=$('<div class="panel" style="grid-column:1/-1"><h2>BTC trend · TradingView alerts</h2></div>');
    const tb=$('<table class="market-table"><thead><tr><th>Chart</th><th>Direction</th><th>Strength</th><th>Age</th></tr></thead><tbody></tbody></table>');
    tfs.forEach((tf)=>{
      const label=tf+'m';
      const snap=byTf[featSym+'@'+tf]||{};
      const freshDir=mtf['tf_'+tf+'m_dir'];
      const storedDir=snap.direction||null;
      const dir=freshDir||storedDir;
      const stale=freshDir==null&&storedDir!=null;
      const dirCls=dir==='UP'?'pos':(dir==='DOWN'?'neg':'neu');
      const age=mtf['tf_'+tf+'m_age_s'];
      tb.querySelector('tbody').appendChild($(`<tr>
        <td>${label}</td>
        <td class="${dirCls}">${dir||'—'}${stale?' <span class="neu">(stale)</span>':''}</td>
        <td>${snap.strength==null?'—':f(snap.strength,2)}</td>
        <td class="neu">${age==null?'—':f(age,0)+'s'}</td>
      </tr>`));
    });
    tvPanel.appendChild(tb);
    const foot=$('<div class="money-sub" style="margin-top:12px"></div>');
    foot.innerHTML=mtfN+'-TF trend: <b class="'+mtfCls+'">'+mtfVerdict+'</b> · '
      +(mtf.trend_fresh_count==null?'—':mtf.trend_fresh_count)+'/'+mtfN+' fresh · '
      +(tv.tradingview_alerts_valid||0)+' alerts received';
    tvPanel.appendChild(foot);
    summary.appendChild(tvPanel);
  }

  const tech=document.getElementById('tech');tech.innerHTML='';
  const o=s.oracle||{},c=s.calibration||{},p=s.price||{},eg=s.execution_gate||{};
  const lf=(o.lead_features||{}).feeds||{},rt=o.rtds||{},rec=L.proxy_official_reconciliation||{};
  tech.appendChild(kvCard('Ledger',[
    ['Trades',L.trades],['Settled',L.settled],['Wins',L.wins],
    ['Win rate',f((L.win_rate||0)*100,1)+'%'],['Avg PnL',money(L.avg_pnl_per_trade)],
    ['Open',L.open_positions]
  ]));
  tech.appendChild(kvCard('Oracle & price',[
    ['Feed',o.oracle_feed_type||'—'],['Source',p.source||'—'],
    ['Chainlink',f(rt.latest&&rt.latest['crypto_prices_chainlink:btc/usd'])],
    ['σ/sec',f(p.sigma_per_sec,6)],['RTDS',rt.connected?'connected':'off',rt.connected?'pos':'neg']
  ]));
  tech.appendChild(kvCard('Execution gate',[
    ['Candidates',eg.candidates],['Fills',eg.accepted,'pos'],['Rejected',eg.rejected_total,'neg'],
    ['Reconciled',eg.reconciled?'yes':'no',eg.reconciled?'pos':'neg']
  ]));
  const sg=s.learned_selectivity_gate||{},cohort=s.baseline_cohort_gate||{};
  tech.appendChild(kvCard('Gates',[
    ['Selectivity rejects',sg.rejected||0,(sg.rejected>0?'neg':'neu')],
    ['Cohort blocks',cohort.blocked||0,(cohort.blocked>0?'neg':'neu')],
    ['Context max TTC',coupling.effective_s!=null?coupling.effective_s+'s':'—'],
    ['Coupling OK',coupling.configured_ok==null?'—':(coupling.configured_ok?'yes':'no'),
      coupling.configured_ok?'pos':'neg'],
    ['Brier',f(c.brier,3)],['Calib samples',c.samples||0]
  ]));
  if(gd.enabled){
    const cb2=gd.circuit_breaker||{};
    tech.appendChild(kvCard('Grok decider',[
      ['Mode',gd.mode||'off'],['Decided',gd.decided],['Direction acc',gd.direction_accuracy==null?'—':f(gd.direction_accuracy*100,1)+'%'],
      ['Abstains',gd.abstains],['Breaker',cb2.tripped?'tripped':'ok',cb2.tripped?'neg':'pos']
    ]));
  }
  const ar=s.arbitrage||{};
  if(ar&&ar.risk_free){
    tech.appendChild(kvCard('Arbitrage',[
      ['Executed',ar.executed||0],['Settled',ar.settled||0],
      ['Profit',money(ar.realized_profit_usd),pnlCls(ar.realized_profit_usd)]
    ]));
  }

  const posWrap=document.getElementById('positions');posWrap.innerHTML='';
  const pos=(l&&l.positions)||[];
  if(pos.length){
    const pc=$('<div class="panel" style="margin-top:18px"><h2>Recent positions</h2></div>');
    const tb=$('<table class="data"><thead><tr><th>Mkt</th><th>Side</th><th>Entry</th><th>Fair</th><th>Result</th><th>PnL</th></tr></thead><tbody></tbody></table>');
    pos.slice(0,12).forEach(x=>{
      const res=x.won==null?'—':(x.won?'Win':'Loss');
      const mkt=(x.research&&x.research.series_label)||'5m';
      tb.querySelector('tbody').appendChild($(`<tr>
        <td>${mkt}</td><td>${x.side||'—'}</td><td>${f(x.entry_price,3)}</td>
        <td>${f(x.fair_at_entry,3)}</td>
        <td class="${x.won==null?'neu':(x.won?'pos':'neg')}">${res}</td>
        <td class="${pnlCls(x.pnl_usd)}">${x.pnl_usd==null?'—':money(x.pnl_usd)}</td>
      </tr>`));
    });
    pc.appendChild(tb);posWrap.appendChild(pc);
  }
}
tick();setInterval(tick,5000);
</script>
</body>
</html>"""