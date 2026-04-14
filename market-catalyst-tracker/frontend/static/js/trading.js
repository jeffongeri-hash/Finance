/**
 * Trading Engine Dashboard
 * =========================
 * Controls and monitors the automated Polymarket paper-trading engine.
 *
 * Sections:
 *   • Engine Controls  — start / stop, balance display, mode badge
 *   • P&L Panel        — total PnL, win rate, daily PnL
 *   • Active Signals   — live signal cards with confidence bars
 *   • Open Positions   — current paper positions
 *   • Event Log        — ENGINE_START, TRADE_OPEN, STOP_LOSS, etc.
 *   • Trade History    — last 50 completed trades
 */

import { API, Fmt, changeClass } from "/static/js/api.js";

// ── API helpers ──────────────────────────────────────────────────────────────

Object.assign(API, {
  tradingStart:  () => API._post("/api/trading/start"),
  tradingStop:   () => API._post("/api/trading/stop"),
  tradingStats:  () => API._fetch("/api/trading/stats"),
  tradingSignals:() => API._fetch("/api/trading/signals"),
  tradingLog:    () => API._fetch("/api/trading/log"),
  nasdaqStatus:  () => API._fetch("/api/nasdaq/status"),
});

// Generic POST helper (api.js may not have one)
if (!API._post) {
  API._post = async (url) => {
    try {
      const res = await fetch(url, { method: "POST" });
      const data = await res.json();
      return { data, error: null };
    } catch (e) {
      return { data: null, error: e.message };
    }
  };
}

// ── Poll interval (5s while engine is running) ────────────────────────────────

let _pollTimer = null;
let _engineRunning = false;

function startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(refreshStats, 5_000);
}

function stopPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = null;
}

// ── Rendering helpers ─────────────────────────────────────────────────────────

function confBar(conf) {
  const pct = Math.round(conf * 100);
  const col = pct >= 70 ? "var(--green)" : pct >= 55 ? "var(--amber)" : "var(--red)";
  return `
    <div style="display:flex;align-items:center;gap:6px;margin-top:4px">
      <div style="flex:1;height:4px;background:var(--bg-elevated);border-radius:2px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${col};border-radius:2px"></div>
      </div>
      <span class="mono fs-11" style="color:${col};min-width:34px">${pct}%</span>
    </div>`;
}

function pnlColor(val) {
  return val > 0 ? "var(--green)" : val < 0 ? "var(--red)" : "var(--text-muted)";
}

function signalTypeBadge(type) {
  const colors = {
    order_book_imbalance: "badge-blue",
    price_divergence:     "badge-amber",
    arbitrage_macro:      "badge-green",
    biotech_catalyst:     "badge-purple",
    news_lag:             "badge-amber",
    momentum_correlation: "badge-blue",
    time_of_day:          "",
  };
  const cls = colors[type] || "";
  const label = type.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  return `<span class="badge ${cls}" style="font-size:10px">${label}</span>`;
}

function renderSignalCard(sig) {
  const side = sig.side === "yes"
    ? `<span style="color:var(--green);font-weight:700">YES</span>`
    : `<span style="color:var(--red);font-weight:700">NO</span>`;

  const price = sig.suggested_price
    ? `<span class="mono fs-11" style="color:var(--text-muted)">@ ${(sig.suggested_price * 100).toFixed(1)}¢</span>`
    : "";

  const size = sig.suggested_size
    ? `<span class="mono fs-11" style="color:var(--text-muted)">$${sig.suggested_size.toFixed(0)}</span>`
    : "";

  return `
    <div class="card" style="padding:10px 14px;margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
        <div style="flex:1;min-width:0">
          ${signalTypeBadge(sig.signal_type)}
          <div class="mono fs-11" style="color:var(--text-muted);margin:4px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
               title="${sig.market_id || ""}">${sig.market_id ? sig.market_id.substring(0, 16) + "…" : "No market"}</div>
          <div style="font-size:11px;color:var(--text-secondary);margin-top:2px;line-height:1.4">${sig.rationale || ""}</div>
          ${confBar(sig.confidence || 0)}
        </div>
        <div style="text-align:right;flex-shrink:0">
          ${side} ${price}<br>${size}
        </div>
      </div>
    </div>`;
}

function renderLogEntry(entry) {
  const dt = new Date(entry.ts * 1000);
  const timeStr = dt.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const eventColors = {
    ENGINE_START:    "var(--green)",
    ENGINE_STOP:     "var(--text-muted)",
    SCAN_COMPLETE:   "var(--text-secondary)",
    TRADE_OPEN:      "var(--blue)",
    STOP_LOSS:       "var(--red)",
    CIRCUIT_BREAKER: "var(--amber)",
    DAILY_RESET:     "var(--purple)",
  };
  const color = eventColors[entry.event] || "var(--text-muted)";
  return `
    <div style="display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--border-dim);font-size:11px;line-height:1.4">
      <span class="mono" style="color:var(--text-dim);min-width:70px;flex-shrink:0">${timeStr}</span>
      <span style="color:${color};font-weight:600;min-width:120px;flex-shrink:0">${entry.event}</span>
      <span style="color:var(--text-secondary);word-break:break-word">${entry.detail}</span>
    </div>`;
}

function renderTradeRow(t) {
  const pnlColor_ = pnlColor(t.net_pnl);
  const side = t.side === "yes"
    ? `<span style="color:var(--green)">YES</span>`
    : `<span style="color:var(--red)">NO</span>`;
  const outcome = t.outcome
    ? `<span class="badge ${t.outcome === "WIN" ? "badge-green" : "badge-red"}">${t.outcome}</span>`
    : "—";
  return `
    <tr>
      <td class="mono" style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.market_id}">${t.market_id.substring(0,12)}…</td>
      <td style="max-width:200px;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${t.question}">${(t.question||"").substring(0,40)}</td>
      <td>${side}</td>
      <td class="mono">${(t.entry * 100).toFixed(1)}¢</td>
      <td class="mono">${t.exit != null ? (t.exit * 100).toFixed(1) + "¢" : "—"}</td>
      <td class="mono" style="color:${pnlColor_}">${t.net_pnl >= 0 ? "+" : ""}$${t.net_pnl.toFixed(2)}</td>
      <td class="mono" style="color:${pnlColor_}">${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct.toFixed(1)}%</td>
      <td>${outcome}</td>
    </tr>`;
}

function renderPositionRow(p) {
  const settled = p.settled
    ? `<span class="badge badge-green" style="font-size:10px">ACTIVE</span>`
    : `<span class="badge" style="font-size:10px;background:var(--bg-elevated);color:var(--text-muted)">PENDING</span>`;
  const side = p.side === "yes"
    ? `<span style="color:var(--green)">YES</span>`
    : `<span style="color:var(--red)">NO</span>`;
  return `
    <tr>
      <td class="mono" title="${p.market_id}">${p.market_id.substring(0,12)}…</td>
      <td>${side}</td>
      <td class="mono">${p.shares ? p.shares.toFixed(2) : "—"}</td>
      <td class="mono">${p.avg_price != null ? (p.avg_price * 100).toFixed(1) + "¢" : "—"}</td>
      <td class="mono">$${p.cost_basis != null ? p.cost_basis.toFixed(2) : "—"}</td>
      <td>${settled}</td>
    </tr>`;
}

// ── Stats render ──────────────────────────────────────────────────────────────

function renderStats(stats) {
  // Status bar
  const statusEl = document.getElementById("engine-status-bar");
  if (statusEl) {
    const runColor = stats.is_running ? "var(--green)" : "var(--text-muted)";
    const runLabel = stats.is_running ? "RUNNING" : "STOPPED";
    const modeLabel = stats.live_mode ? "LIVE" : "PAPER";
    const modeColor = stats.live_mode ? "var(--red)" : "var(--blue)";
    statusEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:6px">
        <div style="width:8px;height:8px;border-radius:50%;background:${runColor};${stats.is_running ? "box-shadow:0 0 6px " + runColor : ""}"></div>
        <span style="color:${runColor};font-weight:700;font-size:13px">${runLabel}</span>
      </div>
      <span class="badge" style="background:${modeColor}20;color:${modeColor};border:1px solid ${modeColor}40">${modeLabel} MODE</span>
      <span class="mono fs-13" style="color:var(--text-primary)">$${Fmt.num(stats.balance, 2)}</span>
      <span style="font-size:11px;color:var(--text-muted)">(portfolio: $${Fmt.num(stats.portfolio_value, 2)})</span>
    `;
  }

  // PnL summary
  const pnlEl = document.getElementById("engine-pnl-summary");
  if (pnlEl) {
    const totalC = pnlColor(stats.total_pnl);
    const dailyC = pnlColor(stats.daily_pnl);
    const wr = (stats.win_rate * 100).toFixed(1);
    pnlEl.innerHTML = `
      <div class="engine-stat">
        <div class="engine-stat-val mono" style="color:${totalC}">${stats.total_pnl >= 0 ? "+" : ""}$${Fmt.num(stats.total_pnl, 2)}</div>
        <div class="engine-stat-label">Total P&amp;L</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono" style="color:${dailyC}">${stats.daily_pnl >= 0 ? "+" : ""}$${Fmt.num(stats.daily_pnl, 2)}</div>
        <div class="engine-stat-label">Today's P&amp;L</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono">${stats.total_trades}</div>
        <div class="engine-stat-label">Total Trades</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono" style="color:${parseFloat(wr) >= 55 ? "var(--green)" : "var(--amber)"}">${wr}%</div>
        <div class="engine-stat-label">Win Rate</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono">${stats.win_count}W / ${stats.loss_count}L</div>
        <div class="engine-stat-label">W / L</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono">${stats.open_positions}</div>
        <div class="engine-stat-label">Open Positions</div>
      </div>
      <div class="engine-stat">
        <div class="engine-stat-val mono">${stats.active_signals}</div>
        <div class="engine-stat-label">Active Signals</div>
      </div>
    `;
  }

  // Signals
  const sigEl = document.getElementById("engine-signals");
  if (sigEl) {
    const sigs = stats.signals || [];
    if (!sigs.length) {
      sigEl.innerHTML = `<div class="loading-text">No active signals — engine will scan every 60s</div>`;
    } else {
      sigEl.innerHTML = sigs.map(renderSignalCard).join("");
    }
  }

  // Event log
  const logEl = document.getElementById("engine-log");
  if (logEl) {
    const log = (stats.event_log || []).slice().reverse();
    if (!log.length) {
      logEl.innerHTML = `<div class="loading-text">No events yet</div>`;
    } else {
      logEl.innerHTML = log.map(renderLogEntry).join("");
    }
  }

  // Trade history
  const tradeBody = document.getElementById("engine-trades-body");
  if (tradeBody) {
    const trades = (stats.trade_log || []).slice().reverse();
    if (!trades.length) {
      tradeBody.innerHTML = `<tr><td colspan="8" class="loading-text" style="padding:24px">No completed trades yet</td></tr>`;
    } else {
      tradeBody.innerHTML = trades.map(renderTradeRow).join("");
    }
  }

  // Open positions
  const posBody = document.getElementById("engine-positions-body");
  if (posBody) {
    // positions are embedded in stats via open_positions count only;
    // we need the detail from a separate endpoint if available.
    // For now show placeholder — positions details come from /api/trading/stats "signals"
    posBody.innerHTML = `<tr><td colspan="6" class="loading-text" style="padding:16px">
      ${stats.open_positions > 0
        ? `${stats.open_positions} open position(s) — start engine to see details`
        : "No open positions"
      }
    </td></tr>`;
  }

  _engineRunning = stats.is_running;
  updateButtons(stats.is_running);
}

function updateButtons(running) {
  const startBtn = document.getElementById("engine-start-btn");
  const stopBtn  = document.getElementById("engine-stop-btn");
  if (startBtn) startBtn.disabled = running;
  if (stopBtn)  stopBtn.disabled  = !running;
  if (running) startPolling(); else stopPolling();
}

// ── Public API ─────────────────────────────────────────────────────────────────

export async function refreshStats() {
  const { data, error } = await API.tradingStats();
  if (error || !data) return;
  renderStats(data);
}

export async function initTradingView() {
  // Wire up buttons
  const startBtn = document.getElementById("engine-start-btn");
  const stopBtn  = document.getElementById("engine-stop-btn");

  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      startBtn.disabled = true;
      startBtn.textContent = "Starting…";
      const { data, error } = await API.tradingStart();
      if (error) {
        startBtn.disabled = false;
        startBtn.textContent = "Start Engine";
        return;
      }
      await refreshStats();
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener("click", async () => {
      stopBtn.disabled = true;
      stopBtn.textContent = "Stopping…";
      const { data, error } = await API.tradingStop();
      if (error) {
        stopBtn.disabled = false;
        stopBtn.textContent = "Stop Engine";
        return;
      }
      await refreshStats();
    });
  }
}

export async function loadTrading() {
  await refreshStats();
}
