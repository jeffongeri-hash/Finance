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
  tradingStart:    () => API._post("/api/trading/start"),
  tradingStop:     () => API._post("/api/trading/stop"),
  tradingStats:    () => API._fetch("/api/trading/stats"),
  tradingSignals:  () => API._fetch("/api/trading/signals"),
  tradingLog:      () => API._fetch("/api/trading/log"),
  nasdaqStatus:    () => API._fetch("/api/nasdaq/status"),
  backtestStatus:  () => API._fetch("/api/backtest/status"),
  backtestSummary: () => API._fetch("/api/backtest/summary"),
  backtestResults: (strategy) => API._fetch("/api/backtest/results", strategy ? { strategy } : {}),
  backtestRunNow:  () => API._post("/api/backtest/run"),
  pennyScan:       (params = {}) => API._fetch("/api/penny/scan", params),
  pennyEV:         (price) => API._fetch("/api/penny/ev", { price }),
  pennyPositions:  () => API._fetch("/api/penny/positions"),
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

// ── Penny harvest rendering ───────────────────────────────────────────────────

function evColor(ev) {
  if (ev > 0.025) return "var(--green)";
  if (ev > 0.010) return "#6ee7b7";
  return "var(--amber)";
}

function renderPennyRow(opp) {
  const ev    = opp.ev ?? 0;
  const score = opp.score ?? 0;
  const days  = opp.days_to_expiry ?? 0;
  const liq   = opp.liquidity ?? 0;
  return `
    <tr>
      <td style="max-width:280px;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${opp.question}">${opp.question}</td>
      <td><span class="badge ${opp.side === 'yes' ? 'badge-green' : 'badge-red'}" style="font-size:10px">${(opp.outcome || opp.side).toUpperCase()}</span></td>
      <td class="mono" style="color:var(--amber)">${(opp.entry_price * 100).toFixed(1)}¢</td>
      <td class="mono" style="color:${evColor(ev)}">+$${ev.toFixed(4)}</td>
      <td class="mono" style="color:${evColor(ev)}">${(opp.confidence * 100).toFixed(0)}%</td>
      <td class="mono">$${(liq / 1000).toFixed(0)}k</td>
      <td class="mono">${days.toFixed(0)}d</td>
      <td class="mono" style="color:var(--text-muted)">${score.toFixed(0)}</td>
    </tr>`;
}

async function refreshPenny() {
  const { data } = await API.pennyScan();
  if (!data) return;

  const posLabel = document.getElementById("penny-pos-label");
  if (posLabel && data.portfolio_ev) {
    const needed = data.portfolio_ev.positions_needed ?? 0;
    const curr   = (data.parameters?.target_pos ?? 50) - needed;
    posLabel.textContent = `${curr}/${data.parameters?.target_pos ?? 50} positions`;
  }

  const el = document.getElementById("penny-opportunities");
  if (!el) return;

  const opps = data.opportunities || [];
  if (!opps.length) {
    el.innerHTML = `<div class="loading-text">No penny opportunities found (all markets above 3¢ or low liquidity)</div>`;
    return;
  }

  const pev = data.portfolio_ev || {};
  const statsHtml = `
    <div class="flex items-center gap-8 fs-11" style="margin-bottom:8px;flex-wrap:wrap">
      <div><span style="color:var(--text-dim)">Opportunities found: </span><strong class="mono">${opps.length}</strong></div>
      <div><span style="color:var(--text-dim)">Portfolio EV (50 pos): </span><span class="mono" style="color:var(--green)">+$${(pev.portfolio_ev ?? 0).toFixed(2)}</span></div>
      <div><span style="color:var(--text-dim)">Kelly fraction: </span><span class="mono">${((pev.kelly_fraction ?? 0) * 100).toFixed(1)}%</span></div>
      <div><span style="color:var(--text-dim)">Positions needed: </span><span class="mono">${pev.positions_needed ?? 50}</span></div>
    </div>`;

  el.innerHTML = statsHtml + `
    <div style="overflow-x:auto">
      <table class="data-table">
        <thead>
          <tr>
            <th>Question</th><th>Side</th><th>Price</th><th>EV/share</th>
            <th>Conf</th><th>Liquidity</th><th>Days</th><th>Score</th>
          </tr>
        </thead>
        <tbody>${opps.slice(0, 30).map(renderPennyRow).join("")}</tbody>
      </table>
    </div>`;
}

// ── Backtest rendering ────────────────────────────────────────────────────────

function stratColor(avgReturn) {
  if (avgReturn > 5)  return "var(--green)";
  if (avgReturn > 0)  return "#6ee7b7";
  if (avgReturn > -5) return "var(--amber)";
  return "var(--red)";
}

async function refreshBacktest() {
  const summaryEl = document.getElementById("bt-summary-grid");
  const topEl     = document.getElementById("bt-top-markets");
  const statusLbl = document.getElementById("bt-status-label");

  // Status
  const { data: st } = await API.backtestStatus();
  if (st && statusLbl) {
    const lastRun = st.last_run
      ? `Last run: ${new Date(st.last_run * 1000).toLocaleTimeString()}`
      : "Not yet run";
    const nextIn = st.next_run_in_s > 0
      ? ` · next in ${Math.round(st.next_run_in_s / 60)}m`
      : "";
    statusLbl.textContent = `${lastRun}${nextIn} · ${st.result_count} results`;
  }

  const { data } = await API.backtestSummary();
  if (!data || !data.by_strategy) {
    if (summaryEl) summaryEl.innerHTML = `<div class="loading-text">Backtest sweep in progress — results appear after first run (~2 min)</div>`;
    return;
  }

  // Strategy summary cards
  if (summaryEl) {
    const entries = Object.entries(data.by_strategy)
      .sort((a, b) => b[1].avg_sharpe - a[1].avg_sharpe);

    summaryEl.innerHTML = entries.map(([name, s]) => {
      const retColor = stratColor(s.avg_return_pct);
      const isBest   = name === data.best_strategy;
      return `
        <div class="engine-stat" style="${isBest ? "border-color:var(--green);box-shadow:0 0 8px rgba(34,197,94,.2)" : ""}">
          <div style="font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">
            ${name.replace(/_/g," ")}${isBest ? " ⭐" : ""}
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px;font-size:11px">
            <div><span style="color:var(--text-dim)">Ret </span><span class="mono" style="color:${retColor}">${s.avg_return_pct > 0 ? "+" : ""}${s.avg_return_pct}%</span></div>
            <div><span style="color:var(--text-dim)">Sharpe </span><span class="mono">${s.avg_sharpe}</span></div>
            <div><span style="color:var(--text-dim)">WR </span><span class="mono">${(s.avg_win_rate * 100).toFixed(0)}%</span></div>
            <div><span style="color:var(--text-dim)">EV </span><span class="mono" style="color:${s.avg_ev > 0 ? "var(--green)" : "var(--red)"}">$${s.avg_ev.toFixed(2)}</span></div>
            <div><span style="color:var(--text-dim)">PF </span><span class="mono">${s.avg_profit_factor}×</span></div>
            <div><span style="color:var(--text-dim)">DD </span><span class="mono" style="color:var(--amber)">${(s.avg_max_drawdown * 100).toFixed(1)}%</span></div>
          </div>
          <div style="font-size:10px;color:var(--text-dim);margin-top:4px">${s.markets_tested} markets · ${(s.positive_ev_pct * 100).toFixed(0)}% +EV</div>
        </div>`;
    }).join("");
  }

  // Top markets
  if (topEl && data.top_markets?.length) {
    topEl.innerHTML = `
      <div class="fs-11 text-muted" style="margin-bottom:6px">Top performing market/strategy combos:</div>
      <table class="data-table">
        <thead><tr><th>#</th><th>Market</th><th>Strategy</th><th>Return</th><th>Sharpe</th><th>Trades</th></tr></thead>
        <tbody>
          ${data.top_markets.map((r, i) => `
            <tr>
              <td class="mono">${i + 1}</td>
              <td class="mono fs-11" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.market_slug}</td>
              <td><span class="badge badge-blue" style="font-size:10px">${r.strategy.replace(/_/g," ")}</span></td>
              <td class="mono" style="color:${stratColor(r.total_return)}">${r.total_return > 0 ? "+" : ""}${r.total_return}%</td>
              <td class="mono">${r.sharpe}</td>
              <td class="mono">${r.trades}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  }
}

export async function initTradingView() {
  // Wire up buttons
  const startBtn    = document.getElementById("engine-start-btn");
  const stopBtn     = document.getElementById("engine-stop-btn");
  const btRunBtn    = document.getElementById("bt-run-btn");
  const pennyScanBtn = document.getElementById("penny-scan-btn");

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

  if (btRunBtn) {
    btRunBtn.addEventListener("click", async () => {
      btRunBtn.disabled = true;
      btRunBtn.textContent = "Running…";
      await API.backtestRunNow();
      let polls = 0;
      const poll = setInterval(async () => {
        polls++;
        await refreshBacktest();
        const statusLbl = document.getElementById("bt-status-label");
        if ((statusLbl?.textContent || "").includes("results") || polls > 60) {
          clearInterval(poll);
          btRunBtn.disabled = false;
          btRunBtn.textContent = "Run Now";
        }
      }, 5_000);
    });
  }

  if (pennyScanBtn) {
    pennyScanBtn.addEventListener("click", async () => {
      pennyScanBtn.disabled = true;
      pennyScanBtn.textContent = "Scanning…";
      const el = document.getElementById("penny-opportunities");
      if (el) el.innerHTML = `<div class="loading-text" style="padding:30px">Scanning all Polymarket markets for 1¢ contracts…</div>`;
      await refreshPenny();
      pennyScanBtn.disabled = false;
      pennyScanBtn.textContent = "Scan Now";
    });
  }
}

export async function loadTrading() {
  await Promise.all([refreshStats(), refreshBacktest(), refreshPenny()]);
  setInterval(refreshBacktest, 5 * 60 * 1000);
  setInterval(refreshPenny, 10 * 60 * 1000);  // refresh penny scan every 10 min
}
