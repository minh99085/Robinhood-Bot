// Hermes Trading Engine - dashboard client (paper / armed-live).

const COLORS = {
  gold: "#ffce4a", green: "#2fd17f", red: "#ff5566", dim: "#6c7a85",
  cyan: "#34d3e0", line: "#1e272e", text: "#c9d6df", purple: "#b07bff",
};

const $ = (id) => document.getElementById(id);
const fmtUSD = (n, d = 0) =>
  (n < 0 ? "-$" : "$") + Math.abs(Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const cents = (p) => Math.round((Number(p) || 0) * 100) + "\u00A2";
const pct = (x, d = 0) => (x == null ? "--" : (Number(x) * 100).toFixed(d) + "%");
const signPct = (x) => (x == null ? "--" : (x >= 0 ? "+" : "") + (Number(x) * 100).toFixed(1) + "%");
const fmtHold = (s) => (s == null ? "\u2013" : s < 3600 ? Math.round(s / 60) + "m" : (s / 3600).toFixed(1) + "h");

let lastMode = "paper";

function ctxOf(id) {
  const cv = $(id);
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth || 300;
  const h = cv.clientHeight || parseInt(cv.getAttribute("height")) || 120;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function renderReplayPanel(s) {
  const rp = (s && s.replay) || null;
  if (!rp) return;
  let panel = document.getElementById("replay-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "replay-panel";
    panel.style.cssText =
      "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
      "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
    (document.querySelector(".wrap") || document.body).appendChild(panel);
  }
  const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
  const runs = (rp.recent_runs || []).slice(0, 6);
  const rows = runs.length
    ? runs.map((r) =>
        '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
        `<span style="min-width:150px;color:#9a9ab0">${esc(String(r.replay_run_id).slice(0, 16))}</span>` +
        `<span style="min-width:80px">${esc(r.status)}</span>` +
        `<span style="min-width:120px">eq ${esc(r.ending_equity)}</span>` +
        `<span style="min-width:110px">pnl ${esc(r.total_pnl)}</span>` +
        `<span style="min-width:90px">dd ${esc(r.max_drawdown)}</span>` +
        `<span style="color:#9a9ab0">fill ${esc(r.fill_ratio)} brier ${esc(r.brier)}</span></div>`
      ).join("")
    : '<div style="color:#777">no replay runs yet — see scripts/run_replay.py</div>';
  panel.innerHTML =
    '<div style="margin-bottom:6px"><b style="color:#fff">Replay / backtest ' +
    '<span style="color:#777;font-weight:400">(offline; no live orders)</span></b></div>' + rows;
}

function renderResearchPanel(s) {
  const rs = (s && s.research) || null;
  if (!rs) return;
  let panel = document.getElementById("research-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "research-panel";
    panel.style.cssText =
      "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
      "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
    (document.querySelector(".wrap") || document.body).appendChild(panel);
  }
  const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
  const ests = (rs.recent_estimates || []).slice(0, 6);
  const rows = ests.length
    ? ests.map((e) =>
        '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
        `<span style="min-width:140px;color:#9a9ab0">${esc(String(e.market_id).slice(0, 16))}</span>` +
        `<span style="min-width:90px">p ${esc(e.p_ensemble)}</span>` +
        `<span style="min-width:90px">conf ${esc(e.confidence)}</span>` +
        `<span style="min-width:90px">amb ${esc(e.ambiguity_score)}</span>` +
        `<span style="min-width:90px">ev ${esc(e.evidence_score)}</span>` +
        `<span style="color:#d08770">${esc(e.no_trade_reason || "")}</span></div>`
      ).join("")
    : '<div style="color:#777">no estimates yet — research is OFF by default</div>';
  panel.innerHTML =
    '<div style="margin-bottom:6px"><b style="color:#fff">AI research &amp; probability ' +
    `<span style="color:#777;font-weight:400">(Grok research-only · mode ${esc(rs.mode)} · ` +
    `strategy ${rs.use_in_strategy ? "on" : "off"})</span></b></div>` + rows;
}

let _venueFetchTs = 0;
function renderVenuePanel() {
  // Venue health is fetched from /api/venues/status (read-only; no secrets).
  const now = Date.now();
  if (now - _venueFetchTs < 5000) return;  // throttle
  _venueFetchTs = now;
  fetch("/api/venues/status").then((r) => r.json()).then((data) => {
    let panel = document.getElementById("venue-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "venue-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
        "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    const rows = (data.venues || []).map((v) =>
      '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
      `<span style="min-width:110px;color:#9a9ab0">${esc(v.venue)}</span>` +
      `<span style="min-width:90px">${v.enabled ? "enabled" : "disabled"}</span>` +
      `<span style="min-width:140px">${esc(v.status)}</span>` +
      `<span style="color:#9a9ab0">md=${v.supports_market_data} meta=${v.supports_metadata} ${esc(v.detail || "")}</span></div>`
    ).join("");
    panel.innerHTML =
      '<div style="margin-bottom:6px"><b style="color:#fff">Exchange connections ' +
      '<span style="color:#777;font-weight:400">(read-only; no orders)</span></b></div>' +
      (rows || '<div style="color:#777">no venues</div>');
  }).catch(() => { /* venue panel is non-critical */ });
}

let _shadowFetchTs = 0;
function renderShadowPanel() {
  // Shadow status is fetched from /api/shadow/status (read-only; no secrets).
  const now = Date.now();
  if (now - _shadowFetchTs < 5000) return;
  _shadowFetchTs = now;
  fetch("/api/shadow/status").then((r) => r.json()).then((st) => {
    let panel = document.getElementById("shadow-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "shadow-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
        "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    const sid = st.active_session_id;
    let readinessHtml = "";
    const finish = () => {
      panel.innerHTML =
        '<div style="margin-bottom:6px"><b style="color:#fff">Shadow test (watch-only) ' +
        '<span style="color:#777;font-weight:400">(shadow_live · no live orders)</span></b></div>' +
        '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
        `<span style="min-width:90px">${st.enabled ? "enabled" : "disabled"}</span>` +
        `<span style="min-width:130px">status ${esc(st.session_status)}</span>` +
        `<span style="min-width:120px">venues ${esc((st.venues || []).join(","))}</span>` +
        `<span>orders ${st.new_orders_allowed ? "allowed" : "paused"}</span></div>` +
        `<div style="color:#9a9ab0;margin-top:4px">session ${esc((sid || "—"))}</div>` +
        readinessHtml;
    };
    if (sid) {
      fetch(`/api/shadow/sessions/${sid}/readiness`).then((r) => r.json()).then((rd) => {
        readinessHtml = `<div style="margin-top:4px">readiness: <b>${esc(rd.overall_status)}</b> ` +
          `<span style="color:#777">${esc(rd.recommended_next_step || "")}</span></div>`;
        finish();
      }).catch(finish);
    } else {
      finish();
    }
  }).catch(() => { /* shadow panel is non-critical */ });
}

let _glFetchTs = 0;
function renderGuardedLivePanel() {
  // Read-only design panel. NO live/submit/cancel/wallet buttons by design.
  const now = Date.now();
  if (now - _glFetchTs < 5000) return;
  _glFetchTs = now;
  fetch("/api/guarded-live/status").then((r) => r.json()).then((st) => {
    let panel = document.getElementById("guarded-live-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "guarded-live-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #3a2a2a;border-radius:10px;" +
        "background:#1c1414;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    panel.innerHTML =
      '<div style="margin-bottom:6px"><b style="color:#fff">Live-trading safety (off) ' +
      '<span style="color:#d08770;font-weight:400">(DRY-RUN ONLY · real execution DISABLED)</span></b></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      `<span style="min-width:90px">${st.enabled ? "enabled" : "disabled"}</span>` +
      `<span style="min-width:150px">state ${esc(st.state)}</span>` +
      `<span style="min-width:110px">mode ${esc(st.mode)}</span>` +
      `<span>no_live_execution=${st.no_live_execution}</span>` +
      `<span>kill_switch=${st.kill_switch_active}</span></div>` +
      '<div style="color:#9a9ab0;margin-top:4px">No live orders. No submit/cancel/wallet actions exist.</div>';
  }).catch(() => { /* guarded-live panel is non-critical */ });
}

function renderMarketUniversePanel() {
  // Read-only selection panel. The universe manager only scans/ranks/tiers
  // markets; it never places, cancels, or sizes an order.
  const now = Date.now();
  if (now - (window._muFetchTs || 0) < 5000) return;
  window._muFetchTs = now;
  fetch("/api/markets/universe").then((r) => r.json()).then((u) => {
    let panel = document.getElementById("market-universe-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "market-universe-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #2a3a3a;border-radius:10px;" +
        "background:#141c1c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    const cfg = u.config || {};
    let head =
      '<div style="margin-bottom:6px"><b style="color:#fff">Markets being scanned ' +
      '<span style="color:#7fd1c4;font-weight:400">(selection only · no orders placed here)</span></b></div>';
    if (!u.available) {
      panel.innerHTML = head +
        `<div style="color:#9a9ab0">no scan yet — pipeline: scan ${cfg.scan_limit || 1000} ` +
        `&rarr; shortlist ${cfg.shortlist_limit || 100} &rarr; live-watch ${cfg.live_watchlist_limit || 80} ` +
        `&rarr; trade top ${cfg.trade_candidate_limit || 20} &rarr; hold &le; ${u.max_open_trades}.</div>` +
        '<div style="color:#777;margin-top:4px">run scripts/scan_polymarket_universe.py to populate.</div>';
      return;
    }
    const rej = u.rejected_by_reason || {};
    const rejStr = Object.keys(rej).length
      ? Object.entries(rej).map(([k, v]) => `${esc(k)}:${v}`).join("  ")
      : "none";
    const top = (u.top_markets || []).slice(0, 10).map((m, i) =>
      '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20302c">' +
      `<span style="color:#888;min-width:22px">#${i + 1}</span>` +
      `<span style="color:${m.tier === "A" ? "#5cff9d" : m.tier === "B" ? "#7fd1c4" : "#9a9ab0"};min-width:20px">${esc(m.tier)}</span>` +
      `<span style="min-width:62px">${esc(m.score)}</span>` +
      `<span style="flex:1">${esc(m.question)}</span>` +
      `<span style="color:#9a9ab0;min-width:200px">${esc((m.reasons || []).join(", "))}</span></div>`
    ).join("") || '<div style="color:#777">no markets</div>';
    panel.innerHTML = head +
      '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:4px">' +
      `<span>scanned <b>${u.total_markets_scanned}</b></span>` +
      `<span>passed <b>${u.markets_passing_filters}</b></span>` +
      `<span>Tier A <b style="color:#5cff9d">${u.tier_a_count}</b></span>` +
      `<span>Tier B <b style="color:#7fd1c4">${u.tier_b_count}</b></span>` +
      `<span>Tier C <b>${u.tier_c_count}</b></span>` +
      `<span>live subs <b>${u.live_websocket_subscriptions}</b></span>` +
      `<span>trade candidates <b>${u.trade_candidates}</b></span>` +
      `<span>open PM trades <b>${u.open_polymarket_trades}</b>/<b>${u.max_open_trades}</b></span>` +
      `<span style="color:#9a9ab0">live_subscribe=${u.live_subscribe_enabled}</span></div>` +
      `<div style="color:#9a9ab0;margin:2px 0">rejected by reason: ${esc(rejStr)}</div>` +
      '<div style="color:#9a9ab0;margin:6px 0 2px">top 10 market scores</div>' + top;
  }).catch(() => { /* universe panel is non-critical */ });
}

function renderMicroLivePanel() {
  // Read-only status panel. NO submit/cancel/wallet/api-key/production buttons by design.
  const now = Date.now();
  if (now - (window._mlFetchTs || 0) < 5000) return;
  window._mlFetchTs = now;
  fetch("/api/micro-live/status").then((r) => r.json()).then((st) => {
    let panel = document.getElementById("micro-live-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "micro-live-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #3a2a2a;border-radius:10px;" +
        "background:#14181c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    panel.innerHTML =
      '<div style="margin-bottom:6px"><b style="color:#fff">Tiny live test (off) ' +
      '<span style="color:#d08770;font-weight:400">(CLI-only · one canary · FOK-only · DISABLED by default)</span></b></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      `<span style="min-width:90px">${st.enabled ? "enabled" : "disabled"}</span>` +
      `<span style="min-width:110px">env ${esc(st.environment)}</span>` +
      `<span>prod_allowed=${st.production_allowed}</span>` +
      `<span>live_submit_blocked=${st.live_submit_blocked}</span>` +
      `<span>max_notional=$${esc(st.max_order_notional_usd)}</span></div>` +
      '<div style="color:#9a9ab0;margin-top:4px">No autonomous live trading. CLI-only submit. ' +
      'No submit/cancel/wallet/API-key/production controls exist in the dashboard.</div>';
  }).catch(() => { /* micro-live panel is non-critical */ });
}

function renderPostCanaryPanel() {
  // Read-only post-canary analysis + veto panel. NO submit/cancel/scale/production buttons.
  const now = Date.now();
  if (now - (window._pcFetchTs || 0) < 5000) return;
  window._pcFetchTs = now;
  fetch("/api/post-canary/latest").then((r) => r.json()).then((d) => {
    const a = (d && d.latest) || null;
    let panel = document.getElementById("post-canary-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "post-canary-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #2a3a2a;border-radius:10px;" +
        "background:#141c16;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    panel.innerHTML =
      '<div style="margin-bottom:6px"><b style="color:#fff">Live-test review ' +
      '<span style="color:#a3be8c;font-weight:400">(analysis only · never scales)</span></b></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      `<span style="min-width:160px">recommendation ${esc(a ? a.recommendation : "n/a")}</span>` +
      `<span style="min-width:120px">status ${esc(a ? a.status : "n/a")}</span>` +
      `<span>hard_fail=${a ? a.hard_fail_count : 0}</span>` +
      `<span>unknown=${a ? a.unknown_blocking_count : 0}</span></div>` +
      '<div style="color:#9a9ab0;margin-top:4px">size increase: NO &nbsp;·&nbsp; autonomous live: NO ' +
      '&nbsp;·&nbsp; production execution: NOT IMPLEMENTED. ' +
      'No submit/cancel/scale/production controls exist in the dashboard.</div>';
  }).catch(() => { /* post-canary panel is non-critical */ });
}

function renderProductionReviewPanel() {
  // Read-only production DESIGN REVIEW panel. NO production submit/cancel/scale/arm controls.
  const now = Date.now();
  if (now - (window._prFetchTs || 0) < 5000) return;
  window._prFetchTs = now;
  fetch("/api/production-review/status").then((r) => r.json()).then((st) => {
    const a = (st && st.latest) || null;
    let panel = document.getElementById("production-review-panel");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "production-review-panel";
      panel.style.cssText =
        "margin:16px;padding:12px 16px;border:1px solid #3a3a2a;border-radius:10px;" +
        "background:#1a1814;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
      (document.querySelector(".wrap") || document.body).appendChild(panel);
    }
    const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
    panel.innerHTML =
      '<div style="margin-bottom:6px"><b style="color:#fff">Go-live checklist ' +
      '<span style="color:#ebcb8b;font-weight:400">(DESIGN REVIEW only · no execution)</span></b></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
      `<span style="min-width:200px">recommendation ${esc(a ? a.recommendation : "n/a")}</span>` +
      `<span style="min-width:110px">status ${esc(a ? a.status : "n/a")}</span>` +
      `<span>draft_phase12=${a ? !!a.eligible_to_draft_phase12_plan : false}</span></div>` +
      '<div style="color:#9a9ab0;margin-top:4px">Production execution: NOT IMPLEMENTED &nbsp;·&nbsp; ' +
      'Size increase: NOT APPROVED &nbsp;·&nbsp; Autonomous live: NOT APPROVED &nbsp;·&nbsp; ' +
      'Dashboard submit: NOT AVAILABLE &nbsp;·&nbsp; API submit: NOT AVAILABLE. ' +
      'No production submit/cancel/scale/arm controls exist in the dashboard.</div>';
  }).catch(() => { /* production-review panel is non-critical */ });
}

function renderOmsPanel(s) {
  const oms = (s && s.oms) || null;
  if (!oms) return;
  let panel = document.getElementById("oms-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "oms-panel";
    panel.style.cssText =
      "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
      "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
    (document.querySelector(".wrap") || document.body).appendChild(panel);
  }
  const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
  const recon = oms.reconciliation || {};
  const sev = String(recon.severity || "info");
  const sevCol = { info: "#5cff9d", warning: "#ffd479", high: "#ff5c5c" }[sev] || "#cfcfe0";
  const fills = (oms.recent_fills || []).slice(0, 6);
  const fillRows = fills.length
    ? fills.map((f) =>
        '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
        `<span style="min-width:150px;color:#9a9ab0">${esc(String(f.client_order_id).slice(0, 14))}</span>` +
        `<span style="min-width:130px">${esc(f.venue)}:${esc(f.market_id)}</span>` +
        `<span style="min-width:120px">${esc(f.side)} ${esc(f.quantity)} @ ${esc(f.price)}</span>` +
        `<span style="color:#9a9ab0">${esc(f.liquidity_flag)} fee ${esc(f.fee)}</span></div>`
      ).join("")
    : '<div style="color:#777">no fills yet</div>';
  const positions = (oms.positions || []).filter((p) => p.quantity && p.quantity !== "0").slice(0, 6);
  const posRows = positions.length
    ? positions.map((p) =>
        '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
        `<span style="min-width:160px">${esc(p.venue)}:${esc(p.market_id)}</span>` +
        `<span style="min-width:120px">qty ${esc(p.quantity)} @ ${esc(p.avg_price)}</span>` +
        `<span style="color:#9a9ab0">rPnL ${esc(p.realized_pnl)} fees ${esc(p.fees_paid)}</span></div>`
      ).join("")
    : '<div style="color:#777">no open positions</div>';
  panel.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
    '<b style="color:#fff">Orders &amp; fills <span style="color:#777;font-weight:400">(paper — fake money)</span></b>' +
    `<span>open: <b>${oms.open_orders || 0}</b> &middot; ` +
    `recon: <b style="color:${sevCol}">${esc(sev.toUpperCase())}</b>` +
    `${oms.degraded ? ' &middot; <span style="color:#ff5c5c;font-weight:700">DEGRADED</span>' : ""}</span></div>` +
    '<div style="color:#9a9ab0;margin:4px 0 2px">recent fills</div>' + fillRows +
    '<div style="color:#9a9ab0;margin:6px 0 2px">positions</div>' + posRows;
}

function renderMarketDataPanel(s) {
  const md = (s && s.market_data) || null;
  if (!md) return;
  let panel = document.getElementById("md-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "md-panel";
    panel.style.cssText =
      "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
      "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
    (document.querySelector(".wrap") || document.body).appendChild(panel);
  }
  const st = md.status || {};
  const status = String(st.status || "disabled");
  const colors = { connected: "#5cff9d", disabled: "#777", connecting: "#ffd479",
    reconnecting: "#ffd479", degraded: "#ff8c42", disconnected: "#ff5c5c", error: "#ff5c5c" };
  const col = colors[status] || "#cfcfe0";
  const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
  const ageS = st.last_message_age_ms != null ? Math.round(st.last_message_age_ms / 1000) + "s" : "\u2013";
  const assets = (md.assets || []).slice(0, 8);
  const rows = assets.length
    ? assets.map((a) =>
        '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
        `<span style="min-width:170px;color:#9a9ab0" title="${esc(a.asset_id)}">${esc(String(a.asset_id).slice(0, 16))}\u2026</span>` +
        `<span style="min-width:120px">bid ${esc(a.best_bid)} / ask ${esc(a.best_ask)}</span>` +
        `<span style="min-width:70px">age ${a.age_ms != null ? Math.round(a.age_ms / 1000) + "s" : "\u2013"}</span>` +
        `<span style="color:${a.stale ? "#ff8c42" : "#5cff9d"}">${a.stale ? "STALE" : "fresh"}</span>` +
        `${a.tick_size_dirty ? '<span style="color:#ff5c5c">TICK\u0394</span>' : ""}` +
        `${a.resolved ? '<span style="color:#ff5c5c">RESOLVED</span>' : ""}</div>`
      ).join("")
    : '<div style="color:#777">no subscribed assets</div>';
  panel.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
    '<b style="color:#fff">Live market data <span style="color:#777;font-weight:400">(read-only price feed)</span></b>' +
    `<span style="color:${col};font-weight:700">${esc(status.toUpperCase())}</span></div>` +
    '<div style="color:#9a9ab0;margin-bottom:4px">' +
    `subscribed: <b>${st.subscribed_asset_count || 0}</b> &middot; ` +
    `tracked: <b>${st.tracked_asset_count || 0}</b> &middot; ` +
    `stale: <b>${st.stale_asset_count || 0}</b> &middot; ` +
    `last msg: <b>${ageS}</b> &middot; ` +
    `msgs: <b>${st.messages_received || 0}</b> &middot; ` +
    `parse errors: <b>${st.parse_errors || 0}</b> &middot; ` +
    `reconnects: <b>${st.reconnect_count || 0}</b></div>` +
    rows;
}

function renderRiskPanel(s) {
  const r = (s && s.risk) || null;
  if (!r) return;
  let panel = document.getElementById("risk-panel");
  if (!panel) {
    panel = document.createElement("section");
    panel.id = "risk-panel";
    panel.style.cssText =
      "margin:16px;padding:12px 16px;border:1px solid #2a2a3a;border-radius:10px;" +
      "background:#14141c;font:13px/1.5 system-ui,sans-serif;color:#cfcfe0";
    (document.querySelector(".wrap") || document.body).appendChild(panel);
  }
  const rej = r.recent_rejections || [];
  const apr = r.recent_approvals || [];
  const ks = r.kill_switch
    ? '<span style="color:#ff5c5c;font-weight:700">KILL SWITCH ACTIVE</span>'
    : '<span style="color:#5cff9d">armed</span>';
  const esc = (t) => String(t || "").replace(/</g, "&lt;");
  const decisionRow = (x, ok) =>
    '<div style="display:flex;gap:8px;padding:2px 0;border-top:1px solid #20202c">' +
    `<span style="color:#888;min-width:64px">${new Date((x.ts || 0) * 1000).toLocaleTimeString()}</span>` +
    `<span style="color:${ok ? "#5cff9d" : "#ffd479"};min-width:96px">${ok ? "APPROVED" : esc(x.code)}</span>` +
    `<span style="color:#777;min-width:70px">${esc(x.risk_decision_id)}</span>` +
    `<span style="min-width:150px">${esc(x.market)}:${esc(x.symbol)} ${esc(x.side)} $${x.notional || 0}</span>` +
    `<span style="color:#9a9ab0;flex:1">${ok ? esc(x.strategy) : esc(x.reason)}</span></div>`;
  const aprRows = apr.length
    ? apr.map((x) => decisionRow(x, true)).join("")
    : '<div style="color:#777">no approved risk decisions yet</div>';
  const rejRows = rej.length
    ? rej.map((x) => decisionRow(x, false)).join("")
    : '<div style="color:#777">no risk rejections yet</div>';
  panel.innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
    '<b style="color:#fff">Risk checks <span style="color:#777;font-weight:400">(safety limits)</span></b>' +
    `<span>${ks} &middot; approvals: <b style="color:#5cff9d">${r.approvals_total || 0}</b>` +
    ` &middot; rejections: <b style="color:#ffd479">${r.rejections_total || 0}</b></span></div>` +
    '<div style="color:#9a9ab0;margin:4px 0 2px">latest approved decisions ' +
    '<span style="color:#666">(every simulated fill follows a prior approved decision)</span></div>' +
    aprRows +
    '<div style="color:#9a9ab0;margin:6px 0 2px">latest rejected decisions</div>' +
    rejRows;
}

function renderHeader(s) {
  $("clock").textContent = (s.now_utc || "--:--:--") + " UTC";
  $("round").textContent = "#" + (s.round || "----");
  $("wallet").textContent = shorten(s.wallet_label || "");
}
function shorten(addr) {
  if (!addr || addr.length < 12) return addr || "----";
  return addr.slice(0, 7) + "\u2026" + addr.slice(-4);
}

function renderMode(s) {
  const mode = s.mode || "paper";
  lastMode = mode;
  try { localStorage.setItem("hermes_trading_mode", mode); } catch (_) {}
  $("mode-tint").className = "mode-tint " + mode;
  const pill = $("mode-pill");
  pill.className = "mode-pill " + mode;
  pill.textContent = mode === "live" ? "LIVE TRADING \u00B7 REAL FUNDS" : "PAPER TRADING \u00B7 SIMULATED";
  const rd = s.readiness || {};
  const toggle = $("mode-toggle");
  toggle.checked = mode === "live";
  toggle.disabled = mode === "paper" && !rd.ready;
  $("rd-score").textContent = rd.score ?? 0;
  $("rd-fill").style.width = (rd.score ?? 0) + "%";
  const checks = rd.checks || {};
  const label = { min_500_trades: "500 trades", sharpe_gt_1_5: "Sharpe>1.5",
                  winrate_gt_55: "win>55%", maxdd_lt_15: "DD<15%", no_crashes_24h: "no crashes" };
  $("rd-checks").innerHTML = Object.keys(label).map((k) =>
    `<span class="chk ${checks[k] ? "ok" : "no"}">${checks[k] ? "\u2713" : "\u2717"} ${label[k]}</span>`).join("");
  const c = s.circuit || {}, banner = $("circuit-banner");
  let msg = "";
  if (c.halted) msg = `\u26D4 CIRCUIT HALTED \u2014 ${c.halt_reason || "emergency stop"}. Switch to PAPER to reset.`;
  else if (c.paused) msg = `\u23F8 TRADING PAUSED ${c.paused_seconds_left}s \u2014 ${(c.last_alert && c.last_alert.reason) || "circuit breaker"}.`;
  else if (mode === "live" && c.last_alert) msg = `\u26A0 ${c.last_alert.reason}`;
  banner.className = "circuit-banner" + (msg ? " show" : "");
  banner.textContent = msg;
}

function fmtDuration(sec) {
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m";
  return Math.floor(sec / 3600) + "h";
}

function renderHero(s) {
  const p = s.portfolio || {};
  const pnl = $("pnl");
  pnl.textContent = fmtUSD(p.total_pnl, 0);
  pnl.className = "pnl" + (p.total_pnl < 0 ? " neg" : "");
  $("pnl-sub").innerHTML = `total P&amp;L &middot; equity <b>${fmtUSD(p.equity)}</b> &middot; realized <b>${fmtUSD(p.realized)}</b> &middot; Sharpe <b>${p.sharpe ?? 0}</b>`;
  $("lat").textContent = (s.latency_ms ?? 0) + "ms";
  $("winrate").textContent = Math.round((p.win_rate || 0) * 100) + "%";
  $("uptime").textContent = fmtDuration(s.uptime_seconds || 0);
  $("hero-trades").textContent = p.trades || 0;
  const lab = $("lab-badge");
  if (lab) lab.textContent = "5MIN LAB \u00B7 " + String(s.aggressiveness || "paper").toUpperCase();
}

function arbCard(lbl, val, cls) {
  return `<div class="arb-card"><div class="lbl">${lbl}</div><div class="val ${cls || ""}">${val}</div></div>`;
}

function renderArb(s) {
  // Arbitrage is permanently disabled (Polymarket-only PAPER training): keep the
  // panel hidden and never render opportunities/trades for it.
  const panel = $("arb-panel");
  if (panel) panel.style.display = "none";
  return;
  // eslint-disable-next-line no-unreachable
  const a = s.arb;
  if (!a) return;
  $("btn-arb").classList.toggle("on", !!a.enabled);
  $("arb-panel").classList.toggle("arb-off", !a.enabled);
  $("arb-status").textContent = a.enabled ? (a.status || "scanning").toUpperCase() : "OFF";
  const inc = $("arb-incident");
  if (a.last_incident && a.last_incident.level === "CRITICAL") {
    inc.className = "circuit-banner show";
    inc.textContent = "\u26D4 " + a.last_incident.message + " \u00B7 est loss " + fmtUSD(a.last_incident.estimated_loss_usd || 0);
  } else if (a.status === "paused" && a.enabled) {
    inc.className = "circuit-banner show";
    inc.textContent = "\u23F8 Arb paused " + a.paused_seconds_left + "s after an incident";
  } else { inc.className = "circuit-banner"; inc.textContent = ""; }
  const m = a.metrics || {}, tot = m.total_profit || 0;
  $("arb-cards").innerHTML =
    arbCard("Total arb profit", fmtUSD(tot), tot > 0 ? "g" : tot < 0 ? "r" : "") +
    arbCard("Arb trades", (m.trades_today || 0) + " today / " + (m.trades || 0), "") +
    arbCard("Arb win rate", m.win_rate != null ? pct(m.win_rate) : "\u2013", "") +
    arbCard("Best single arb", fmtUSD(m.best || 0), (m.best || 0) > 0 ? "g" : "");
  $("arb-opps").innerHTML = (a.opportunities || []).slice(0, 6).map((o) =>
    `<div class="arb-row"><span class="sym">${o.symbol}${o.simulated ? " *" : ""}</span>` +
    `<span>${o.buyExchange}\u2192${o.sellExchange}</span>` +
    `<span class="net ${o.netPct > 0 ? "g" : "r"}">${o.netPct}%</span>` +
    `<span class="tier">${o.tier}</span></div>`).join("") ||
    `<div style="color:var(--dim)">${a.enabled ? "no opportunities \u2014 markets efficient after fees" : "arbitrage is OFF"}</div>`;
  $("arb-trades").innerHTML = (a.recent_trades || []).slice(0, 8).map((t) => {
    const cls = t.outcome === "profit" ? "profit" : t.outcome === "incident" ? "incident" : "loss";
    return `<div class="arb-row ${cls}"><span class="sym">${t.symbol}</span>` +
      `<span>${(t.leg1 && t.leg1.exchange) || ""}\u2192${(t.leg2 && t.leg2.exchange) || ""}</span>` +
      `<span>${t.grossPct_quoted ?? "?"}\u2192${t.netPct_actual ?? "?"}%</span>` +
      `<span>${fmtUSD(t.profitUSD_actual || 0)}</span></div>`;
  }).join("") || `<div style="color:var(--dim)">no executed arb trades yet</div>`;
  $("arb-note").textContent = !a.enabled ? "Arbitrage execution is turned off."
    : a.simulate ? "Simulation mode ON: synthetic opportunities injected to exercise the pipeline (fills use real prices, so quoted% rarely survives \u2014 that is the lesson)."
    : (a.last_skip ? "last: " + a.last_skip : "");
}

function renderTraining(s) {
  const tr = s.training || {}, m = tr.metrics || {};
  $("phase-badge").textContent = `PHASE ${tr.phase || 1} \u00B7 ${tr.phase_name || "OBSERVATION"}`;
  const note = $("train-note");
  if (tr.notification) { note.className = "train-note show"; note.textContent = "\u2713 " + tr.notification; }
  else { note.className = "train-note"; note.textContent = ""; }
  $("train-stats").innerHTML =
    `<div><span>trades</span><b>${m.total_trades ?? 0}</b></div>` +
    `<div><span>win rate</span><b>${pct(m.win_rate)}</b></div>` +
    `<div><span>Sortino</span><b>${m.sortino ?? 0}</b></div>` +
    `<div><span>avg hold</span><b>${fmtHold(m.avg_hold_seconds)}</b></div>` +
    `<div><span>Grok acc</span><b>${m.grok_accuracy != null ? pct(m.grok_accuracy) : "\u2013"}</b> <span style="opacity:.6">(${m.grok_signals || 0})</span></div>`;
  const next = tr.next_phase_at, tot = tr.trades || 0;
  $("phase-fill").style.width = (next ? Math.min(100, Math.round(100 * tot / next)) : 100) + "%";
  $("phase-prog").textContent = next ? `${tot} / ${next} trades to next phase` : "final phase reached";
  const pm = m.per_market || {};
  $("train-markets").innerHTML = Object.keys(pm).map((k) => {
    const e = pm[k], wl = e.trades ? Math.round(100 * e.wins / e.trades) : 0, cls = e.pnl > 0 ? "g" : "r";
    return `<div class="brk"><span>${k}</span><span>${e.trades}t \u00B7 ${wl}%w \u00B7 <b class="${cls}">${fmtUSD(e.pnl)}</b></span></div>`;
  }).join("") || `<div style="color:var(--dim)">no closed trades yet</div>`;
  drawSpark(s.equity_curve || []);
}

function drawSpark(curve) {
  const { ctx, w, h } = ctxOf("pnl-spark");
  const eq = curve.map((c) => c.equity).filter((x) => typeof x === "number");
  if (eq.length < 2) return;
  const lo = Math.min(...eq), hi = Math.max(...eq), span = (hi - lo) || 1;
  const x = (i) => (i / (eq.length - 1)) * (w - 4) + 2;
  const y = (v) => h - 4 - ((v - lo) / span) * (h - 8);
  const up = eq[eq.length - 1] >= eq[0], col = up ? COLORS.green : COLORS.red;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, up ? "rgba(47,209,127,.25)" : "rgba(255,85,102,.25)"); grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath(); ctx.moveTo(x(0), y(eq[0])); eq.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(eq.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(x(0), y(eq[0])); eq.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke();
}

function renderPulse(s) {
  const p = s.pulse || {};
  $("price-beat").textContent = fmtUSD(p.start_price, 2);
  $("price-now").textContent = fmtUSD(p.current_price, 2);
  const d = $("delta");
  d.textContent = (p.delta >= 0 ? "+" : "") + fmtUSD(p.delta, 2);
  d.className = "delta " + (p.delta >= 0 ? "pos" : "neg");
  const left = p.seconds_left ?? 0;
  $("countdown").innerHTML = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}<small>MINS &middot; SECS</small>`;
  $("odd-up").innerHTML = `${cents(p.up_price)}<small>UP</small>`;
  $("odd-down").innerHTML = `${cents(p.down_price)}<small>DOWN</small>`;
  const bet = p.bet;
  $("bet-tag").textContent = bet
    ? `BET ${bet.side} @ ${cents(bet.entry_price)} \u00B7 ${fmtUSD(bet.stake)} \u00B7 EV ${signPct(bet.ev)}`
    : "no +EV bet this round";
  renderEdge(s, p);
  drawPulseChart(p);
}

function renderEdge(s, p) {
  const el = $("edge-line");
  if (!el) return;
  const cal = s.calibration || {}, sig = s.signal || {};
  const cls = (x) => (x > 0 ? "g" : "r");
  const calTxt = cal.samples
    ? `Brier ${cal.brier_raw ?? "–"}\u2192<b>${cal.brier_cal ?? "–"}</b> (n=${cal.samples}${cal.calibrated ? "" : ", warming"})`
    : "Brier – (collecting)";
  let sigTxt = "";
  if (sig && (sig.obi != null || sig.samples)) {
    const obi = sig.obi != null ? (sig.obi >= 0 ? "+" : "") + sig.obi : "–";
    const state = sig.active ? `<span class="g">ON</span>` : (sig.ready ? "off" : "learning");
    const cmp = (sig.feat_brier != null && sig.base_brier != null) ? ` ${sig.feat_brier} vs base ${sig.base_brier}` : "";
    sigTxt = `&middot; OBI <b>${obi}</b> &middot; signal ${state}${cmp}`;
  }
  el.innerHTML =
    `model <b>${pct(p.p_cal)}</b> <span style="opacity:.6">(raw ${pct(p.p_model)})</span> ` +
    `&middot; market <b>${pct(p.market_up)}</b> ` +
    `&middot; EV <span class="${cls(p.ev_up)}">U ${signPct(p.ev_up)}</span> / <span class="${cls(p.ev_down)}">D ${signPct(p.ev_down)}</span> ` +
    `&middot; Kelly <span class="tag">${(((p.bet ? p.bet.stake_frac : p.stake_frac) || 0) * 100).toFixed(1)}%</span> ` +
    `&middot; ${calTxt}${sigTxt}`;
}

function renderBrain(s) {
  const b = s.brain || {};
  const chip = $("brain-chip"), why = $("brain-why");
  if (!chip || !why) return;
  // Grok ON/OFF toggle (research-only — toggling never places or cancels orders).
  let gbtn = document.getElementById("grok-toggle");
  if (!gbtn && chip.parentElement) {
    gbtn = document.createElement("button");
    gbtn.id = "grok-toggle";
    gbtn.style.cssText = "margin-left:8px;padding:2px 10px;border-radius:6px;border:1px solid #3a3a4a;" +
      "background:#1c1c26;color:#cfcfe0;cursor:pointer;font:12px system-ui;font-weight:600";
    gbtn.onclick = () => {
      const turnOn = !window._grokOn;
      gbtn.disabled = true;
      fetch("/api/grok/" + (turnOn ? "on" : "off"), { method: "POST" })
        .catch(() => {})
        .finally(() => { gbtn.disabled = false; if (typeof poll === "function") poll(); });
    };
    chip.parentElement.appendChild(gbtn);
  }
  if (gbtn) {
    window._grokOn = !!b.enabled;
    gbtn.textContent = b.enabled ? "\u25CF Grok ON \u2014 click to turn OFF"
                                 : "\u25CB Grok OFF \u2014 click to turn ON";
    gbtn.style.borderColor = b.enabled ? "#5cff9d" : "#ff7b7b";
    gbtn.style.color = b.enabled ? "#5cff9d" : "#ff9b9b";
  }
  if (!b.enabled) {
    chip.className = "chip off";
    const src = b.grok_source || "disabled";
    if (src === "offline_cache" || src === "legacy_cached") {
      chip.textContent = "RESEARCH-ONLY";
      why.textContent =
        "legacy Grok disabled \u2014 research mode " + (b.research_mode || "offline_cache") +
        ", no live xAI calls. Set RESEARCH_MODE=online_paper (or GROK_BRAIN_ONLINE=1) to enable.";
    } else {
      chip.textContent = "OFF";
      why.textContent = "Add an xAI/Grok API key to enable the Grok research layer.";
    }
    return;
  }
  const dir = (b.direction || "HOLD").toUpperCase();
  chip.className = "chip " + (dir === "UP" ? "up" : dir === "DOWN" ? "down" : "hold");
  const conf = b.confidence != null ? ` ${Math.round(b.confidence * 100)}%` : "";
  const act = b.action ? `${b.action}${b.urgency ? "/" + b.urgency : ""} ` : "";
  chip.textContent = act + dir + conf;
  if (b.last_error) why.textContent = "Grok error: " + b.last_error;
  else if (b.rationale) {
    const age = b.age_seconds != null ? ` (${Math.round(b.age_seconds)}s ago)` : "";
    const mem = b.memory_lessons ? ` \u00B7 ${b.memory_lessons} lessons` : "";
    why.textContent = `${b.model ? b.model + ": " : ""}${b.rationale}${b.fresh ? "" : " [stale]"}${age}${mem}`;
  } else why.textContent = `${b.model || "grok"} \u2014 warming up\u2026`;
}

function drawPulseChart(p) {
  const { ctx, w, h } = ctxOf("pulse-chart");
  let pts = (Array.isArray(p.samples) && p.samples.length >= 2) ? p.samples.slice() : null;
  if (!pts) { pts = []; if (p.start_price != null) pts.push(p.start_price); if (p.current_price != null) pts.push(p.current_price); }
  pts = pts.filter((x) => typeof x === "number" && isFinite(x));
  if (pts.length < 2) { const v = p.current_price || p.start_price || 0; pts = [v, v]; }
  const beat = (typeof p.start_price === "number") ? p.start_price : pts[0];
  const min = Math.min(...pts, beat), max = Math.max(...pts, beat);
  const pad = (max - min) * 0.15 || Math.max(1, max * 0.0005);
  const lo = min - pad, hi = max + pad, span = (hi - lo) || 1;
  const x = (i) => (i / (pts.length - 1)) * (w - 8) + 4;
  const y = (v) => h - 6 - ((v - lo) / span) * (h - 16);
  ctx.strokeStyle = COLORS.gold; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, y(beat)); ctx.lineTo(w, y(beat)); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = COLORS.dim; ctx.font = "9px monospace"; ctx.fillText("price to beat", 6, Math.max(10, y(beat) - 4));
  const last = pts[pts.length - 1], up = last >= beat, col = up ? COLORS.green : COLORS.red;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, up ? "rgba(47,209,127,.28)" : "rgba(255,85,102,.28)"); grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath(); ctx.moveTo(x(0), y(pts[0])); pts.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(pts.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath(); ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath(); ctx.moveTo(x(0), y(pts[0])); pts.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x(pts.length - 1), y(last), 3, 0, 7); ctx.fill();
}

const STATE_CLS = { BULL: "bull", BEAR: "bear", SIDE: "side" };

function renderMarkov(s) {
  const r = s.regime || {};
  const labels = r.labels || ["BULL", "BEAR", "SIDE"];
  const m = r.matrix || [[.33, .33, .33], [.33, .33, .33], [.33, .33, .33]];
  let html = `<div class="cell hd"></div>`;
  labels.forEach((l) => html += `<div class="cell hd">${l}</div>`);
  m.forEach((row, i) => {
    html += `<div class="cell rowlbl ${STATE_CLS[labels[i]]}">${labels[i]}</div>`;
    row.forEach((v, j) => {
      const intensity = Math.min(1, v * 1.4);
      const bg = i === j ? `rgba(255,206,74,${0.08 + intensity * 0.25})` : `rgba(80,100,120,${intensity * 0.18})`;
      html += `<div class="cell" style="background:${bg}">${v.toFixed(2)}</div>`;
    });
  });
  $("matrix").innerHTML = html;
  $("regime-tag").textContent = (r.regime_strength != null) ? `STRENGTH ${r.regime_strength}` : "3-STATE";
  const cls = STATE_CLS[r.current_state] || "side";
  $("state-now").innerHTML = `state &rarr; <b class="${cls}">${r.current_state || "?"}</b> &middot; P(up next) <b>${(r.p_up ?? 0.5).toFixed(2)}</b>`;
  const stat = r.stationary || [.33, .33, .33];
  $("stat-bars").innerHTML = labels.map((l, i) =>
    `<div class="bar-row"><span class="lbl">${l}</span>
       <span class="track"><span class="fill ${STATE_CLS[l]}" style="width:${Math.round(stat[i] * 100)}%"></span></span>
       <span>${(stat[i] || 0).toFixed(2)}</span></div>`).join("");
  drawStateGraph(labels, m, r.current_state);
}

function drawStateGraph(labels, m, current) {
  const { ctx, w, h } = ctxOf("state-graph");
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 22;
  const nodeColors = { BULL: COLORS.green, BEAR: COLORS.red, SIDE: COLORS.gold };
  const pos = labels.map((_, i) => { const a = -Math.PI / 2 + (i * 2 * Math.PI) / labels.length; return [cx + R * Math.cos(a), cy + R * Math.sin(a)]; });
  for (let i = 0; i < labels.length; i++) for (let j = 0; j < labels.length; j++) {
    if (i === j) continue; const p = m[i][j]; if (p < 0.12) continue;
    ctx.strokeStyle = `rgba(120,140,160,${p})`; ctx.lineWidth = 0.5 + p * 2.5;
    ctx.beginPath(); ctx.moveTo(pos[i][0], pos[i][1]); ctx.lineTo(pos[j][0], pos[j][1]); ctx.stroke();
  }
  labels.forEach((l, i) => {
    const isCur = l === current;
    ctx.beginPath(); ctx.arc(pos[i][0], pos[i][1], isCur ? 13 : 10, 0, 7);
    ctx.fillStyle = nodeColors[l] || COLORS.dim; ctx.globalAlpha = isCur ? 1 : 0.55; ctx.fill(); ctx.globalAlpha = 1;
    if (isCur) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.2; ctx.stroke(); }
    ctx.fillStyle = "#07090b"; ctx.font = "bold 8px monospace"; ctx.textAlign = "center";
    ctx.fillText(l[0], pos[i][0], pos[i][1] + 3);
  });
  ctx.textAlign = "left";
}

function renderMonteCarlo(s) {
  const mc = s.montecarlo || {};
  $("mc-paths").textContent = mc.paths || 500;
  const conv = $("mc-conv"), pu = mc.p_up ?? 0.5;
  conv.textContent = `${Math.round(pu * 100)}% ${pu >= 0.5 ? "UP" : "DOWN"}`;
  conv.style.color = pu >= 0.5 ? COLORS.green : COLORS.red;
  $("p-up").textContent = (pu * 100).toFixed(0) + "%";
  $("p-down").textContent = ((1 - pu) * 100).toFixed(0) + "%";
  $("mc-exp").textContent = mc.expected ? fmtUSD(mc.expected, 0) : "--";
  drawMcFan(mc); drawMcRadial(mc); drawMcHist(mc);
}

function drawMcFan(mc) {
  const { ctx, w, h } = ctxOf("mc-fan");
  const q = mc.quantiles || {};
  const series = [q.p05, q.p25, q.p50, q.p75, q.p95].filter((a) => a && a.length);
  if (series.length < 5) return;
  const n = q.p50.length, all = series.flat(), lo = Math.min(...all), hi = Math.max(...all);
  const x = (i) => (i / (n - 1)) * (w - 6) + 3, y = (v) => h - 4 - ((v - lo) / (hi - lo || 1)) * (h - 8);
  const band = (a, b, color) => {
    ctx.beginPath(); a.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
    for (let i = b.length - 1; i >= 0; i--) ctx.lineTo(x(i), y(b[i])); ctx.closePath(); ctx.fillStyle = color; ctx.fill();
  };
  band(q.p05, q.p95, "rgba(74,140,255,.10)"); band(q.p25, q.p75, "rgba(74,140,255,.22)");
  ctx.beginPath(); q.p50.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
  ctx.strokeStyle = COLORS.gold; ctx.lineWidth = 1.5; ctx.stroke();
  if (mc.spot) { ctx.strokeStyle = "rgba(200,210,220,.3)"; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(0, y(mc.spot)); ctx.lineTo(w, y(mc.spot)); ctx.stroke(); ctx.setLineDash([]); }
}

function drawMcRadial(mc) {
  const { ctx, w, h } = ctxOf("mc-radial");
  const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 6;
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * 2 * Math.PI, jitter = 0.4 + Math.abs(Math.sin(i * 1.7)) * 0.6, up = Math.sin(i * 0.9) >= 0;
    const len = R * jitter * (mc.converged ? 1 : 0.6);
    ctx.strokeStyle = up ? "rgba(47,209,127,.5)" : "rgba(255,85,102,.5)"; ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + len * Math.cos(a), cy + len * Math.sin(a)); ctx.stroke();
  }
  const pu = mc.p_up ?? 0.5;
  ctx.beginPath(); ctx.arc(cx, cy, R, -Math.PI / 2, -Math.PI / 2 + pu * 2 * Math.PI); ctx.strokeStyle = COLORS.green; ctx.lineWidth = 3; ctx.stroke();
  ctx.beginPath(); ctx.arc(cx, cy, R, -Math.PI / 2 + pu * 2 * Math.PI, 1.5 * Math.PI); ctx.strokeStyle = COLORS.red; ctx.lineWidth = 3; ctx.stroke();
}

function drawMcHist(mc) {
  const { ctx, w, h } = ctxOf("mc-hist");
  const hist = mc.terminal_hist || {}, counts = hist.counts || [];
  if (!counts.length) return;
  const max = Math.max(...counts), bw = w / counts.length, spot = mc.spot || 0, bins = hist.bins || [];
  counts.forEach((c, i) => {
    const bh = (c / max) * (h - 4), above = bins[i] >= spot;
    ctx.fillStyle = above ? "rgba(47,209,127,.7)" : "rgba(255,85,102,.7)";
    ctx.fillRect(i * bw + 1, h - bh, bw - 1.5, bh);
  });
}

function renderPatterns(s) {
  const p = s.patterns || {}, tag = $("bias-tag");
  const bias = (p.bias || "neutral").toUpperCase();
  tag.textContent = bias;
  tag.style.color = bias === "BULLISH" ? COLORS.green : bias === "BEARISH" ? COLORS.red : COLORS.dim;
  const set = (id, sig) => {
    const el = $(id), active = sig && sig.signal, dir = sig ? sig.dir : "flat";
    el.className = "pat" + (active ? " active " + (dir === "up" ? "up" : "down") : "");
    el.querySelector(".state").textContent = active ? (dir === "up" ? "\u25B2 BULLISH" : "\u25BC BEARISH") : "no signal";
  };
  set("pat-bos", p.bos); set("pat-choch", p.choch); set("pat-sweep", p.liquidity_sweep);
}

function tradeMode(t) { try { return (JSON.parse(t.meta || "{}").mode || "").toLowerCase(); } catch (_) { return ""; } }

function renderTrades(s) {
  const rows = (s.recent_trades || []).slice(0, 20);
  const open = new Set((s.open_trades || []).map((t) => t.id));
  $("trades").innerHTML = rows.map((t) => {
    const sideCls = "side-" + (t.side || "").toLowerCase();
    const pnlCls = t.pnl > 0 ? "pos" : t.pnl < 0 ? "neg" : "";
    const status = open.has(t.id) ? "OPEN" : (t.status || "").toUpperCase();
    const sym = (t.market === "polymarket") ? "MKT " + String(t.symbol).slice(0, 6) : t.symbol;
    const md = tradeMode(t);
    const stamp = md ? `<span class="stamp ${md}">${md === "live" ? "LIVE" : "PPR"}</span> ` : "";
    return `<div class="trade-row">
      <span class="mk">${t.market}</span>
      <span>${stamp}${sym} <b class="${sideCls}">${t.side}</b></span>
      <span style="color:var(--dim)">${status}</span>
      <span class="pnl ${pnlCls}">${t.pnl ? fmtUSD(t.pnl, 0) : "\u00B7"}</span>
    </div>`;
  }).join("") || `<div style="color:var(--dim);padding:8px 0;">No trades yet \u2014 the bot is warming up the models\u2026</div>`;
}

// =========================================================================
function render(s) {
  // Paper-trading panels first (what you use day-to-day); advanced / live-only
  // panels last. Order is purely cosmetic — every panel still renders.
  try { renderMarketUniversePanel(); } catch (e) { /* markets panel is non-critical */ }
  try { renderMarketDataPanel(s); } catch (e) { /* market-data panel is non-critical */ }
  try { renderOmsPanel(s); } catch (e) { /* orders panel is non-critical */ }
  try { renderRiskPanel(s); } catch (e) { /* risk panel is non-critical */ }
  try { renderResearchPanel(s); } catch (e) { /* research panel is non-critical */ }
  try { renderReplayPanel(s); } catch (e) { /* backtest panel is non-critical */ }
  try { renderVenuePanel(); } catch (e) { /* venue panel is non-critical */ }
  try { renderShadowPanel(); } catch (e) { /* shadow panel is non-critical */ }
  try { renderGuardedLivePanel(); } catch (e) { /* guarded-live panel is non-critical */ }
  try { renderMicroLivePanel(); } catch (e) { /* micro-live panel is non-critical */ }
  try { renderPostCanaryPanel(); } catch (e) { /* post-canary panel is non-critical */ }
  try { renderProductionReviewPanel(); } catch (e) { /* production-review panel is non-critical */ }
  if (!s || s.error) { if (s && s.error) console.warn("engine error:", s.error); return; }
  const steps = [renderHeader, renderMode, renderHero, renderArb, renderTraining, renderPulse,
                 renderBrain, renderMarkov, renderMonteCarlo, renderPatterns, renderTrades];
  for (const fn of steps) { try { fn(s); } catch (e) { console.error(fn.name, e); } }
  try { $("btn-auto").classList.toggle("on", !!s.autotrade); } catch (_) {}
}

function closeModal() {
  $("live-modal").classList.remove("show");
  $("confirm-input").value = ""; $("confirm-ack").checked = false;
  $("modal-confirm").disabled = true; $("modal-err").textContent = "";
}
function updateConfirmEnabled() {
  $("modal-confirm").disabled = !($("confirm-input").value === "CONFIRM" && $("confirm-ack").checked);
}
$("mode-toggle").onchange = async () => {
  const goingLive = $("mode-toggle").checked;
  $("mode-toggle").checked = (lastMode === "live");
  if (goingLive) { $("live-modal").classList.add("show"); $("confirm-input").focus(); }
  else { await fetch("/api/mode/paper", { method: "POST" }); }
};
$("confirm-input").oninput = updateConfirmEnabled;
$("confirm-ack").onchange = updateConfirmEnabled;
$("modal-cancel").onclick = closeModal;
$("live-modal").onclick = (e) => { if (e.target === $("live-modal")) closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
$("modal-confirm").onclick = async () => {
  $("modal-err").textContent = "";
  try {
    const r = await fetch("/api/mode/live", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: $("confirm-input").value, ack: $("confirm-ack").checked }),
    });
    const j = await r.json();
    if (j.ok) closeModal();
    else $("modal-err").textContent = (j.reason || "blocked") + (j.missing ? " — missing: " + j.missing.join(", ") : "");
  } catch (e) { $("modal-err").textContent = "request failed"; }
};

// Arbitrage is permanently disabled (Polymarket-only PAPER training): the
// toggle button is inert and never enables arbitrage.
if ($("btn-arb")) $("btn-arb").onclick = () => {};

// ---- Polymarket PAPER Training panel (read-only; polls its own endpoint) ----
async function renderPmTraining() {
  const panel = $("pmtrain-panel");
  if (!panel) return;
  let s;
  try {
    s = await (await fetch("/api/polymarket/training/status")).json();
  } catch (e) { return; }
  const card = (lbl, val, cls) =>
    `<div class="arb-card"><div class="lbl">${lbl}</div><div class="val ${cls || ""}">${val}</div></div>`;
  if (!s || s.available === false) {
    $("pmtrain-status").textContent = "IDLE";
    $("pmtrain-cards").innerHTML = card("Status", "no run yet", "");
    $("pmtrain-note").textContent = (s && s.reason) || "start training to populate this panel.";
    $("pmtrain-detail").innerHTML = "";
    return;
  }
  const scan = s.scan_metrics || {}, pnl = s.pnl || {}, risk = s.risk || {};
  const learn = s.learning || {}, fb = s.feedback || {}, safety = s.safety || {};
  const subs = s.subscription || {}, bl = s.baselines || [];
  $("pmtrain-status").textContent = "PAPER · " + (s.mode || "observe_only").toUpperCase();
  const tot = pnl.total_pnl || 0;
  $("pmtrain-cards").innerHTML =
    card("Markets scanned", scan.scanned || 0, "") +
    card("Candidates / subs", (scan.candidates || 0) + " / " + (scan.subscribed_assets || 0), "") +
    card("Scan time", (scan.scan_latency_ms || 0) + " ms", "") +
    card("Trades opened", pnl.trades_opened || 0, "") +
    card("Paper PnL", "$" + tot.toFixed(2), tot > 0 ? "g" : tot < 0 ? "r" : "") +
    card("Win rate", pnl.win_rate != null ? (pnl.win_rate * 100).toFixed(0) + "%" : "\u2013", "") +
    card("Calibration err", (learn.calibration_error != null ? learn.calibration_error : "\u2013"), "") +
    card("Edge adj", (fb.edge_adjustment != null ? fb.edge_adjustment : "\u2013"), "");
  const ntr = Object.entries(learn.no_trade_reasons || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
  const topReasons = ntr.map(([k, v]) => `${k}: ${v}`).join(" \u00B7 ") || "none";
  const blLine = bl.map((b) => `${b.baseline_name}: ${b.trade_count} (${b.pnl})`).join(" \u00B7 ") || "n/a";
  // Live list of the markets being scanned RIGHT NOW (real Polymarket
  // questions), plus how fresh the last scan is — so it's obvious what the bot
  // is looking at and that it's actively ticking.
  const wl = s.watchlist || [];
  const esc = (x) => String(x == null ? "" : x).replace(/[<>&"]/g, (c) =>
    ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const ageSec = s.last_scan_ts ? Math.max(0, Math.round(Date.now() / 1000 - s.last_scan_ts)) : null;
  const freshTxt = ageSec == null ? "no scan yet"
    : ageSec < 90 ? `scanned ${ageSec}s ago` : `last scan ${Math.round(ageSec / 60)}m ago (stale?)`;
  const wlRows = wl.length
    ? wl.map((m) =>
        `<div style="display:flex;gap:8px;justify-content:space-between;border-bottom:1px solid #1c1c1c;padding:2px 0">` +
        `<span style="color:#cfd8d4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:64%" title="${esc(m.question)}">${esc(m.question || m.market_id)}</span>` +
        `<span style="color:#7fd1c4;white-space:nowrap">${esc(m.category)}</span>` +
        `<span style="color:#999;white-space:nowrap">mid ${m.mid} \u00B7 sp ${m.spread}</span></div>`).join("")
    : '<div style="color:#888">no markets in the current scan window yet</div>';
  $("pmtrain-detail").innerHTML =
    `<div style="margin:2px 0 4px"><b style="color:#fff">Markets it is scanning now</b> ` +
    `<span style="color:#777">(showing ${wl.length} of ${scan.scanned || 0} \u00B7 tick #${s.tick || 0} \u00B7 ${freshTxt})</span></div>` +
    `<div style="max-height:170px;overflow:auto;margin-bottom:6px">${wlRows}</div>` +
    `<div>subscribed: ${subs.subscribed_assets || 0} &middot; churn: ${subs.churn_count || 0} &middot; ` +
    `stale books: ${subs.stale_books || scan.stale_books || 0} &middot; avg spread: ${(subs.avg_spread || scan.avg_spread || 0)}</div>` +
    `<div>risk approvals/rejections: ${risk.approvals || 0}/${risk.rejections || 0}</div>` +
    `<div>top no-trade reasons: ${topReasons}</div>` +
    `<div>baselines: ${blLine}</div>`;
  // Plain-English status. NOTE: safety.live_detected is the live-EXECUTION
  // safety flag (true only if a forbidden real-order flag is on); in PAPER mode
  // it is correctly false. It is NOT a data-connection signal — so we show a
  // separate, accurate "live data" indicator (real Polymarket markets / CLOB
  // order books / Chainlink feeds the bot is actually reading right now).
  const cl = s.chainlink || {};
  const scannedN = scan.scanned || 0;
  const subsN = scan.subscribed_assets || subs.subscribed_assets || 0;
  const clFeeds = cl.feeds_scanned || 0;
  const dataBits = [];
  if (scannedN > 0) dataBits.push(`${scannedN} Polymarket markets`);
  if (subsN > 0) dataBits.push(`${subsN} live order books`);
  if (clFeeds > 0) dataBits.push(`${clFeeds} Chainlink feeds`);
  const dataOn = dataBits.length > 0;
  $("pmtrain-note").textContent =
    `PAPER ONLY (no real orders) \u00B7 live data: ` +
    `${dataOn ? "ON \u2014 " + dataBits.join(", ") : "OFF (waiting for first scan)"} ` +
    `\u00B7 live execution: ${safety.live_detected ? "ON \u26A0" : "OFF (safe)"} ` +
    `\u00B7 preflight_ok=${safety.ok} \u00B7 arbitrage_disabled=` +
    `${(safety.checks || {}).arbitrage_disabled}`;
}
renderPmTraining();
setInterval(renderPmTraining, 5000);

// SystemsStatusPanel — simple "what's running right now" overview (read-only).
async function renderSystems() {
  const grid = $("systems-grid");
  if (!grid) return;
  const esc = (t) => String(t == null ? "" : t).replace(/</g, "&lt;");
  let d;
  try {
    d = await (await fetch("/api/running-status")).json();
  } catch (_) {
    return;
  }
  const sys = (d && d.systems) || [];
  const badge = $("systems-badge");
  if (badge) badge.textContent = `${d.running_count || 0}/${d.total || sys.length} ON`;
  grid.innerHTML = sys.map((s) =>
    `<div class="sys-item">` +
      `<span class="sys-dot ${esc(s.state)}"></span>` +
      `<span class="sys-text">` +
        `<span class="sys-name">${esc(s.label)}</span>` +
        `<span class="sys-detail" title="${esc(s.detail)}">${esc(s.detail)}</span>` +
      `</span>` +
    `</div>`
  ).join("");
}
renderSystems();
setInterval(renderSystems, 5000);

let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $("conn").classList.add("ok");
  ws.onclose = () => { $("conn").classList.remove("ok"); setTimeout(connect, 2000); };
  ws.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (_) {} };
}
async function poll() { try { const r = await fetch("/api/state"); render(await r.json()); } catch (_) {} }

$("btn-auto").onclick = async () => {
  const on = !$("btn-auto").classList.contains("on");
  await fetch(`/api/autotrade/${on ? "on" : "off"}`, { method: "POST" });
};
$("btn-reset").onclick = async () => {
  if (confirm("Reset the paper portfolio and clear all simulated trades?")) await fetch("/api/reset", { method: "POST" });
};

connect();
poll();
setInterval(poll, 5000);
window.addEventListener("resize", poll);
