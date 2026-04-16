/**
 * IBKR Trading Dashboard
 * ======================
 * Portfolio P&L, positions, orders, trade journal, and strategy signals.
 * Connects to the IBKR Client Portal Gateway via backend proxy routes.
 */

import { API, Fmt } from "/static/js/api.js";

// ── API extension ──────────────────────────────────────────────────────────────

Object.assign(API, {
  ibkrStatus:      ()             => API._fetch("/api/ibkr/status"),
  ibkrPortfolio:   (acct)         => API._fetch(`/api/ibkr/portfolio?account_id=${acct}`),
  ibkrOrders:      ()             => API._fetch("/api/ibkr/orders"),
  ibkrQuote:       (sym)          => API._fetch(`/api/ibkr/quote/${sym}`),
  ibkrOrder:       (params)       => fetch(`${location.origin}/api/ibkr/order?${new URLSearchParams(params)}`, { method: "POST" }).then(r => r.json()),
  ibkrCancel:      (id, acct)     => fetch(`${location.origin}/api/ibkr/order/${id}?account_id=${acct}`, { method: "DELETE" }).then(r => r.json()),
  ibkrTickle:      ()             => fetch(`${location.origin}/api/ibkr/tickle`, { method: "POST" }).then(r => r.json()),

  ibkrDashboard:   (acct)         => API._fetch(`/api/journal/dashboard?account_id=${acct || ""}`),
  ibkrSignals:     (n=20)         => API._fetch(`/api/ibkr/strategy/signals?top_n=${n}`),
  ibkrEvaluate:    (sym)          => API._fetch(`/api/ibkr/strategy/evaluate/${sym}`),

  journalOpen:     (acct)         => API._fetch(`/api/journal/open?account_id=${acct || ""}`),
  journalTrades:   (acct, lim=50) => API._fetch(`/api/journal/trades?account_id=${acct || ""}&limit=${lim}&include_open=true`),
  journalStats:    (acct)         => API._fetch(`/api/journal/stats?account_id=${acct || ""}`),
  journalExit:     (id, price, reason, fees=0) =>
    fetch(`${location.origin}/api/journal/exit/${id}?exit_price=${price}&exit_reason=${reason}&fees=${fees}`, { method: "POST" }).then(r => r.json()),
});

// ── State ──────────────────────────────────────────────────────────────────────

let _account      = "";
let _tickleTimer  = null;
let _refreshTimer = null;

const _state = {
  gateway:    { authenticated: false },
  portfolio:  {},
  ibkrOrders: [],
  journalOpen:[],
  history:    [],
  perf:       {},
  signals:    [],
};

// ── Colour helpers ─────────────────────────────────────────────────────────────

const pnlClass  = v => v > 0 ? "ib-pos" : v < 0 ? "ib-neg" : "dim";
const pnlSign   = v => v > 0 ? "+" : "";
const actClass  = a => ({ BUY: "ib-bull", SELL: "ib-bear", HOLD: "dim", AVOID: "ib-muted" })[a] || "dim";
const sigBadge  = s => `<span class="ib-signal-badge ib-sig-${s.toLowerCase()}">${s}</span>`;
const sentBadge = s => {
  const cfg = { bullish: ["ib-bull", "▲"], bearish: ["ib-bear", "▼"], neutral: ["dim", "◆"], mixed: ["ib-amber", "~"] };
  const [cls, icon] = cfg[s] || ["dim", "?"];
  return `<span class="${cls}">${icon} ${s}</span>`;
};

// ── Gateway status bar ─────────────────────────────────────────────────────────

function renderGatewayBar(gw) {
  const bar = document.getElementById("ib-gateway-bar");
  if (!bar) return;
  if (gw.authenticated) {
    bar.className = "ib-gw-bar ib-gw-ok";
    bar.innerHTML = `<span class="ib-dot green"></span> Gateway connected${_account ? ` · Account ${_account}` : ""}
      <button class="btn-xs" id="ib-gw-tickle-btn" style="margin-left:8px">Ping</button>`;
    document.getElementById("ib-gw-tickle-btn")?.addEventListener("click", async () => {
      await API.ibkrTickle();
    });
  } else {
    bar.className = "ib-gw-bar ib-gw-off";
    bar.innerHTML = `<span class="ib-dot red"></span> Gateway offline —
      start the Client Portal Gateway then authenticate at
      <a href="https://localhost:5000" target="_blank">https://localhost:5000</a>
      ${gw.error ? `<span class="dim" style="margin-left:8px">${gw.error}</span>` : ""}`;
  }
}

// ── Portfolio summary cards ────────────────────────────────────────────────────

function renderPortfolioCards(p) {
  const el = document.getElementById("ib-portfolio-cards");
  if (!el) return;
  if (!p || !p.net_liquidation) {
    el.innerHTML = `<div class="ib-card dim" style="grid-column:1/-1">No portfolio data — gateway not connected or no account_id set.</div>`;
    return;
  }

  const dayPnl = (p.unrealized_pnl || 0);
  el.innerHTML = [
    { label: "Net Liquidation", value: Fmt.currency(p.net_liquidation), sub: "" },
    { label: "Cash",            value: Fmt.currency(p.cash || 0),       sub: "" },
    { label: "Buying Power",    value: Fmt.currency(p.buying_power || 0), sub: "" },
    { label: "Unrealized P&L",  value: Fmt.pct(dayPnl), sub: `${p.position_count || 0} positions`, cls: pnlClass(dayPnl) },
    { label: "Realized P&L",    value: Fmt.currency(p.realized_pnl || 0), sub: "all time", cls: pnlClass(p.realized_pnl || 0) },
    { label: "Gross Positions", value: Fmt.currency(p.gross_position || 0), sub: "" },
  ].map(c => `
    <div class="ib-card">
      <div class="ib-card-label">${c.label}</div>
      <div class="ib-card-value ${c.cls || ""}">${c.value}</div>
      ${c.sub ? `<div class="ib-card-sub dim">${c.sub}</div>` : ""}
    </div>`).join("");
}

// ── Open positions table ───────────────────────────────────────────────────────

function renderPositions(positions) {
  const el = document.getElementById("ib-positions-table");
  if (!el) return;
  if (!positions || !positions.length) {
    el.innerHTML = `<div class="dim" style="padding:16px">No open positions.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="ib-table">
      <thead><tr>
        <th>Symbol</th><th>Qty</th><th>Avg Cost</th><th>Last</th>
        <th>Mkt Value</th><th>Unreal P&L</th><th>P&L %</th>
      </tr></thead>
      <tbody>
        ${positions.map(p => {
          const pnl = p.unrealizedPnl || 0;
          const pct = p.avgCost ? (((p.mktPrice - p.avgCost) / p.avgCost) * 100) : 0;
          return `<tr>
            <td><strong>${p.contractDesc || p.ticker || "?"}</strong></td>
            <td>${p.position}</td>
            <td>${Fmt.price(p.avgCost)}</td>
            <td>${Fmt.price(p.mktPrice)}</td>
            <td>${Fmt.currency(p.mktValue)}</td>
            <td class="${pnlClass(pnl)}">${pnlSign(pnl)}${Fmt.currency(pnl)}</td>
            <td class="${pnlClass(pct)}">${pnlSign(pct)}${pct.toFixed(2)}%</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

// ── Orders table ──────────────────────────────────────────────────────────────

function renderOrders(orders) {
  const el = document.getElementById("ib-orders-table");
  if (!el) return;
  if (!orders || !orders.length) {
    el.innerHTML = `<div class="dim" style="padding:16px">No open orders.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="ib-table">
      <thead><tr>
        <th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th>
        <th>Limit</th><th>Status</th><th>Order ID</th><th></th>
      </tr></thead>
      <tbody>
        ${orders.map(o => `<tr>
          <td><strong>${o.ticker || o.symbol || "?"}</strong></td>
          <td class="${o.side === "BUY" ? "ib-bull" : "ib-bear"}">${o.side || "?"}</td>
          <td>${o.totalSize || o.quantity || "?"}</td>
          <td>${o.orderType || "?"}</td>
          <td>${o.price ? Fmt.price(o.price) : "—"}</td>
          <td><span class="ib-status-badge">${o.status || "?"}</span></td>
          <td class="dim" style="font-size:11px">${o.orderId || "?"}</td>
          <td>
            <button class="btn-xs ib-cancel-btn" data-orderid="${o.orderId}">✕ Cancel</button>
          </td>
        </tr>`).join("")}
      </tbody>
    </table>`;

  el.querySelectorAll(".ib-cancel-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      if (!_account) { alert("Set account ID first"); return; }
      if (!confirm(`Cancel order ${btn.dataset.orderid}?`)) return;
      await API.ibkrCancel(btn.dataset.orderid, _account);
      await refreshDashboard();
    });
  });
}

// ── Trade history table ────────────────────────────────────────────────────────

function renderTradeHistory(trades) {
  const el = document.getElementById("ib-history-table");
  if (!el) return;
  if (!trades || !trades.length) {
    el.innerHTML = `<div class="dim" style="padding:16px">No trade history yet.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="ib-table">
      <thead><tr>
        <th>Symbol</th><th>Entry</th><th>Exit</th>
        <th>Shares</th><th>Entry $</th><th>Exit $</th>
        <th>Net P&L</th><th>P&L %</th><th>Reason</th><th>Hold</th>
      </tr></thead>
      <tbody>
        ${trades.map(t => {
          const pnl    = t.net_pnl || 0;
          const pct    = t.pnl_pct || 0;
          const isOpen = !t.exit_time;
          return `<tr class="${isOpen ? "ib-row-open" : ""}">
            <td><strong>${t.symbol}</strong></td>
            <td class="dim" style="font-size:11px">${t.entry_time ? new Date(t.entry_time*1000).toLocaleDateString() : "—"}</td>
            <td class="dim" style="font-size:11px">${t.exit_time  ? new Date(t.exit_time*1000).toLocaleDateString()  : "<span class='ib-open-badge'>OPEN</span>"}</td>
            <td>${t.shares}</td>
            <td>${Fmt.price(t.entry_price)}</td>
            <td>${t.exit_price ? Fmt.price(t.exit_price) : "—"}</td>
            <td class="${pnlClass(pnl)}">${isOpen ? "—" : pnlSign(pnl) + Fmt.currency(pnl)}</td>
            <td class="${pnlClass(pct)}">${isOpen ? "—" : pnlSign(pct) + pct.toFixed(2) + "%"}</td>
            <td class="dim" style="font-size:11px">${t.exit_reason || "—"}</td>
            <td class="dim" style="font-size:11px">${t.hold_hours ? t.hold_hours.toFixed(1) + "h" : "—"}</td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

// ── Performance stats ──────────────────────────────────────────────────────────

function renderPerformance(perf) {
  const el = document.getElementById("ib-perf-section");
  if (!el) return;
  if (!perf || !perf.total_trades) {
    el.innerHTML = `<div class="dim" style="padding:16px">No completed trades yet — performance stats appear after the first exit.</div>`;
    return;
  }

  const wr = ((perf.win_rate || 0) * 100).toFixed(1);
  el.innerHTML = `
    <div class="ib-perf-grid">
      <div class="ib-stat-card"><div class="ib-stat-val">${perf.total_trades}</div><div class="ib-stat-lbl">Total Trades</div></div>
      <div class="ib-stat-card"><div class="ib-stat-val ${pnlClass(perf.win_rate - 0.5)}">${wr}%</div><div class="ib-stat-lbl">Win Rate</div></div>
      <div class="ib-stat-card"><div class="ib-stat-val ${pnlClass(perf.total_pnl)}">${pnlSign(perf.total_pnl)}$${Math.abs(perf.total_pnl).toFixed(2)}</div><div class="ib-stat-lbl">Total P&L</div></div>
      <div class="ib-stat-card"><div class="ib-stat-val ${pnlClass(perf.avg_pnl_pct)}">${pnlSign(perf.avg_pnl_pct)}${(perf.avg_pnl_pct||0).toFixed(2)}%</div><div class="ib-stat-lbl">Avg P&L %</div></div>
      <div class="ib-stat-card"><div class="ib-stat-val ib-pos">+${(perf.best_trade_pct||0).toFixed(2)}%</div><div class="ib-stat-lbl">Best Trade</div></div>
      <div class="ib-stat-card"><div class="ib-stat-val ib-neg">${(perf.worst_trade_pct||0).toFixed(2)}%</div><div class="ib-stat-lbl">Worst Trade</div></div>
    </div>

    ${perf.signal_accuracy?.length ? `
    <div style="margin-top:16px">
      <div class="card-title" style="margin-bottom:8px">Signal Accuracy (learning loop)</div>
      <table class="ib-table">
        <thead><tr><th>Signal</th><th>Trades</th><th>Win Rate</th><th>Avg P&L %</th></tr></thead>
        <tbody>
          ${perf.signal_accuracy.map(s => `<tr>
            <td>${sigBadge(s.signal_name)}</td>
            <td>${s.total}</td>
            <td class="${pnlClass(s.win_rate - 0.5)}">${(s.win_rate*100).toFixed(0)}%</td>
            <td class="${pnlClass(s.avg_pnl_pct)}">${pnlSign(s.avg_pnl_pct)}${(s.avg_pnl_pct||0).toFixed(2)}%</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>` : ""}

    ${perf.exit_breakdown?.length ? `
    <div style="margin-top:12px">
      <div class="card-title" style="margin-bottom:8px">Exit Reason Breakdown</div>
      <div class="ib-exit-grid">
        ${perf.exit_breakdown.map(e => `
          <div class="ib-exit-chip">
            <div class="ib-exit-reason">${e.exit_reason}</div>
            <div class="ib-exit-count">${e.count}×</div>
            <div class="ib-exit-pnl ${pnlClass(e.avg_pnl)}">${pnlSign(e.avg_pnl)}${(e.avg_pnl||0).toFixed(1)}%</div>
          </div>`).join("")}
      </div>
    </div>` : ""}`;
}

// ── Strategy signals table ─────────────────────────────────────────────────────

function renderSignals(signals) {
  const el = document.getElementById("ib-signals-table");
  if (!el) return;
  if (!signals || !signals.length) {
    el.innerHTML = `<div class="dim" style="padding:16px">No signals — run a scan first.</div>`;
    return;
  }
  el.innerHTML = `
    <table class="ib-table">
      <thead><tr>
        <th>Symbol</th><th>Action</th><th>Score</th><th>RVOL</th>
        <th>Change</th><th>Sentiment</th><th>Signals</th><th>Why</th><th>Why Not</th>
      </tr></thead>
      <tbody>
        ${signals.map(s => `<tr>
          <td>
            <strong>${s.symbol}</strong>
            <div class="dim" style="font-size:10px">${Fmt.price(s.price)}</div>
          </td>
          <td><span class="${actClass(s.action)} ib-action-badge">${s.action}</span></td>
          <td>${s.momentum_score?.toFixed(0) || "—"}</td>
          <td class="${s.rvol >= 2 ? "ib-pos" : ""}">${s.rvol?.toFixed(1) || "—"}×</td>
          <td class="${pnlClass(s.change_pct)}">${pnlSign(s.change_pct)}${(s.change_pct||0).toFixed(2)}%</td>
          <td>${sentBadge(s.sentiment || "neutral")}</td>
          <td>${(s.momentum_signals||[]).map(sigBadge).join(" ")}</td>
          <td class="dim" style="font-size:10px;max-width:160px">${(s.reasons||[]).join("; ")}</td>
          <td class="ib-neg" style="font-size:10px;max-width:160px">${(s.blockers||[]).join("; ")}</td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

// ── Account ID setup ──────────────────────────────────────────────────────────

function renderAccountInput(accounts) {
  const el = document.getElementById("ib-account-row");
  if (!el) return;
  if (accounts && accounts.length) {
    el.innerHTML = accounts.map(a => `
      <button class="btn-xs ib-acct-btn ${_account === a ? "active" : ""}" data-acct="${a}">${a}</button>
    `).join("");
    el.querySelectorAll(".ib-acct-btn").forEach(b => {
      b.addEventListener("click", () => {
        _account = b.dataset.acct;
        localStorage.setItem("ibkr_account", _account);
        refreshDashboard();
      });
    });
    if (!_account && accounts[0]) {
      _account = accounts[0];
      localStorage.setItem("ibkr_account", _account);
    }
  } else {
    el.innerHTML = `<input id="ib-acct-input" class="input-field" style="width:160px;height:28px;font-size:12px" placeholder="Account ID e.g. U1234567" value="${_account}">
      <button class="btn-xs" id="ib-acct-save">Set</button>`;
    document.getElementById("ib-acct-save")?.addEventListener("click", () => {
      _account = document.getElementById("ib-acct-input")?.value?.trim() || "";
      localStorage.setItem("ibkr_account", _account);
      refreshDashboard();
    });
  }
}

// ── Main refresh ──────────────────────────────────────────────────────────────

async function refreshDashboard() {
  const { data, error } = await API.ibkrDashboard(_account);
  if (error) {
    console.warn("IBKR dashboard error:", error);
    return;
  }

  Object.assign(_state, {
    gateway:    data.gateway    || {},
    portfolio:  data.portfolio  || {},
    ibkrOrders: data.ibkr_orders || [],
    journalOpen: data.journal_open || [],
    history:    data.trade_history || [],
    perf:       data.performance || {},
    signals:    data.top_signals || [],
  });

  renderGatewayBar(_state.gateway);
  renderPortfolioCards(_state.portfolio);
  renderPositions(_state.portfolio.positions || []);
  renderOrders(_state.ibkrOrders);
  renderTradeHistory([..._state.journalOpen, ..._state.history]);
  renderPerformance(_state.perf);
  renderSignals(_state.signals);

  // Account picker
  if (_state.gateway.accounts?.length) {
    renderAccountInput(_state.gateway.accounts);
  }
}

async function loadSignals() {
  const el = document.getElementById("ib-signals-table");
  if (el) el.innerHTML = `<div class="loading-text" style="padding:24px">Scanning universe…</div>`;
  const { data, error } = await API.ibkrSignals(25);
  if (data) renderSignals(data.signals || []);
  else if (el) el.innerHTML = `<div class="ib-neg" style="padding:16px">Signal scan error: ${error}</div>`;
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function initIbkrView() {
  _account = localStorage.getItem("ibkr_account") || "";

  // Wire account input if gateway is offline
  renderAccountInput([]);
  renderGatewayBar({ authenticated: false });

  // Tab buttons
  document.querySelectorAll(".ib-tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".ib-tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".ib-tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      const panel = document.getElementById(`ib-tab-${btn.dataset.tab}`);
      if (panel) panel.classList.add("active");
    });
  });

  // Scan signals button
  document.getElementById("ib-scan-signals-btn")?.addEventListener("click", loadSignals);

  // Manual exit form
  document.getElementById("ib-manual-exit-btn")?.addEventListener("click", async () => {
    const id    = document.getElementById("ib-exit-trade-id")?.value?.trim();
    const price = document.getElementById("ib-exit-price")?.value?.trim();
    const reason = document.getElementById("ib-exit-reason")?.value || "MANUAL";
    if (!id || !price) { alert("Enter trade ID and exit price"); return; }
    const res = await API.journalExit(id, price, reason);
    if (res.id) { alert(`Trade ${id} closed. P&L: ${res.pnl_pct?.toFixed(2)}%`); await refreshDashboard(); }
    else alert("Exit failed: " + JSON.stringify(res));
  });
}

export async function loadIbkrDashboard() {
  await refreshDashboard();

  // Keepalive: tickle gateway every 60s to prevent session timeout
  clearInterval(_tickleTimer);
  _tickleTimer = setInterval(() => {
    if (_state.gateway?.authenticated) API.ibkrTickle();
  }, 60_000);

  // Auto-refresh portfolio every 30s
  clearInterval(_refreshTimer);
  _refreshTimer = setInterval(refreshDashboard, 30_000);
}
