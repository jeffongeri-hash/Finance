/**
 * QQQ Options view — defined-risk credit-spread research & PAPER trading.
 * Every entry and exit is a proposal. Approve/Reject buttons call authenticated
 * routes; the backend re-runs the deterministic risk engine before any paper fill.
 */
import { API, Fmt } from "/static/js/api.js";

const Q = {
  status:      () => API._fetch("/api/qqq/status"),
  latest:      () => API._fetch("/api/qqq/cycle/latest"),
  proposals:   () => API._fetch("/api/qqq/proposals", { limit: 30 }),
  positions:   () => API._fetch("/api/qqq/paper/positions"),
  orders:      () => API._fetch("/api/qqq/paper/orders"),
  monitorLast: () => API._fetch("/api/qqq/monitor/latest"),
  backtests:   () => API._fetch("/api/qqq/backtests", { limit: 5 }),
  validations: () => API._fetch("/api/qqq/validations", { limit: 10 }),
  research:    () => API._fetch("/api/qqq/research", { limit: 20 }),
  activity:    () => API._fetch("/api/qqq/activity", { limit: 60 }),
  alerts:      () => API._fetch("/api/qqq/alerts", { limit: 20 }),
  cycle:       () => API._postJson("/api/qqq/cycle"),
  monitor:     () => API._postJson("/api/qqq/monitor"),
  review:      () => API._postJson("/api/qqq/review"),
  approve:     (id, approver, note) => API._postJson(`/api/qqq/proposals/${id}/approve`, { approver, note }),
  reject:      (id, approver, note) => API._postJson(`/api/qqq/proposals/${id}/reject`, { approver, note }),
  kill:        (engaged, reason, by) => API._postJson("/api/qqq/kill-switch", { engaged, reason, by }),
  backtest:    (body) => API._postJson("/api/qqq/backtest", body),
};

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "—" : Number(v).toFixed(d);
const money = (v) => (v === null || v === undefined) ? "—" : Fmt.currency(v);
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const $ = (id) => document.getElementById(id);
const badge = (cls, text) => `<span class="badge ${cls}">${esc(text)}</span>`;

function approverName() {
  let n = "";
  try { n = localStorage.getItem("qqq_approver") || ""; } catch { /* ignore */ }
  if (!n) {
    n = (prompt("Your name for the approval audit log:") || "").trim();
    if (n) { try { localStorage.setItem("qqq_approver", n); } catch { /* ignore */ } }
  }
  return n;
}

function flash(msg, isErr = false) {
  const el = $("qq-flash");
  if (!el) return;
  el.textContent = msg;
  el.style.color = isErr ? "var(--red)" : "var(--green)";
  setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 8000);
}

// ── renderers ─────────────────────────────────────────────────────────────────

function renderStatus(s) {
  const ks = s.kill_switch || {};
  const trend = s.last_cycle?.snapshot?.trend;
  $("qq-controls").innerHTML = `
    <div class="qq-row">
      ${badge(s.env === "paper" ? "badge-blue" : "badge-purple", `ENV: ${s.env}`)}
      ${badge("badge-green", "LIVE EXECUTION: NOT AVAILABLE")}
      ${badge(s.market_open ? "badge-green" : "badge-amber", s.market_open ? "Market open" : "Market closed")}
      ${badge(ks.engaged ? "badge-red" : "badge-green", ks.engaged ? "KILL SWITCH ENGAGED" : "Kill switch off")}
      ${badge(s.reconciliation?.ok ? "badge-green" : "badge-red", s.reconciliation?.ok ? "Reconciled" : "NOT reconciled")}
      ${badge(s.audit_chain_ok ? "badge-green" : "badge-red", s.audit_chain_ok ? "Audit chain OK" : "AUDIT CHAIN BROKEN")}
      ${badge(s.strategy?.validation === "UNVALIDATED" ? "badge-amber" : "badge-green", `Strategy: ${s.strategy?.validation}`)}
    </div>
    <div class="qq-muted">Kill switch: ${esc(ks.reason || "")} ${ks.by ? `(${esc(ks.by)}, ${when(ks.at)})` : ""}
      ${(s.reconciliation?.issues || []).length ? `<br><span style="color:var(--red)">${esc(s.reconciliation.issues.join("; "))}</span>` : ""}</div>
    <div class="qq-row" style="margin-top:8px">
      <button class="btn-xs" id="qq-run">Run screening cycle</button>
      <button class="btn-xs" id="qq-monitor">Check exits / orders</button>
      <button class="btn-xs" id="qq-review">Run review agent</button>
      <button class="btn-xs" id="qq-kill">${ks.engaged ? "Disengage kill switch" : "ENGAGE kill switch"}</button>
      <span id="qq-flash" class="qq-muted"></span>
    </div>`;
  $("qq-run").onclick = async () => { flash("Running cycle…"); const r = await Q.cycle(); r.error ? flash(r.error, true) : flash(r.data.message); loadQqq(); };
  $("qq-monitor").onclick = async () => { const r = await Q.monitor(); r.error ? flash(r.error, true) : flash("Monitor complete"); loadQqq(); };
  $("qq-review").onclick = async () => { const r = await Q.review(); r.error ? flash(r.error, true) : flash("Review logged"); loadQqq(); };
  $("qq-kill").onclick = async () => {
    const by = approverName(); if (!by) return;
    const reason = prompt(ks.engaged ? "Reason for disengaging (reconciliation must pass):" : "Reason for engaging:");
    if (!reason) return;
    const r = await Q.kill(!ks.engaged, reason, by);
    r.error ? flash(r.error, true) : flash(r.data.engaged ? "Kill switch engaged" : "Kill switch disengaged");
    loadQqq();
  };

  const snap = s.last_cycle?.snapshot;
  const tcls = trend === "BULLISH" ? "badge-green" : trend === "BEARISH" ? "badge-red" : "badge-amber";
  $("qq-market").innerHTML = snap ? `
    <div class="qq-big">${Fmt.price(snap.price)} ${badge(tcls, trend)}</div>
    <table class="data-table qq-kv"><tbody>
      <tr><td>SMA 50</td><td>${num(snap.sma50)}</td></tr>
      <tr><td>SMA 200</td><td>${num(snap.sma200)}</td></tr>
      <tr><td>RSI 14</td><td>${num(snap.rsi14, 1)}</td></tr>
      <tr><td>HV 20 (realised)</td><td>${num((snap.hv20 || 0) * 100, 1)}%</td></tr>
      <tr><td>VXN (implied)</td><td>${snap.iv_context?.available ? `${num(snap.iv_context.vxn)} · ${num(snap.iv_context.percentile_1y, 0)}th pct (1y)` : esc(snap.iv_context?.reason || "—")}</td></tr>
      <tr><td>Last bar</td><td>${esc(snap.last_bar_day)} · ${esc(snap.source)}</td></tr>
    </tbody></table>
    <div class="qq-muted">${esc(snap.trend_reason)}</div>` : `<div class="loading-text">No snapshot yet — run a screening cycle.</div>`;

  const m = s.macro || {};
  const ev = m.next_event;
  $("qq-macro").innerHTML = `
    ${ev ? `<div class="qq-big">${esc(ev.kind)} <span class="qq-muted">${when(ev.at)}</span></div>
      <div>Exit deadline: <strong>${when(m.next_event_exit_deadline)}</strong></div>
      <div class="qq-muted"><a href="${esc(ev.source)}" target="_blank" rel="noopener">source</a></div>`
      : `<div class="qq-big" style="color:var(--amber)">No verified event</div>`}
    ${(m.coverage_gaps_21d || []).length ? `<div class="qq-warn">Calendar gaps (trading blocked):<br>${m.coverage_gaps_21d.map(esc).join("<br>")}
       <br>Fill <code>backend/qqq/data/macro_events.json</code> from official schedules.</div>` : ""}`;

  const L = s.limits || {}, F = s.fees || {};
  $("qq-limits").innerHTML = `<table class="data-table qq-kv"><tbody>
      <tr><td>Max loss / trade (incl. fees)</td><td>$${num(L.max_loss_usd)}</td></tr>
      <tr><td>Open positions</td><td>${L.max_open_positions}</td></tr>
      <tr><td>DTE window</td><td>${L.min_dte}–${L.max_dte}</td></tr>
      <tr><td>Short |Δ| band</td><td>${num(L.short_delta_min)}–${num(L.short_delta_max)}</td></tr>
      <tr><td>Daily loss limit</td><td>$${num(L.daily_loss_limit_usd)}</td></tr>
      <tr><td>Gamma/credit ceiling</td><td>${L.max_gamma_to_credit ?? "<span style='color:var(--amber)'>unvalidated</span>"}</td></tr>
      <tr><td>Fees (est.)</td><td>$${num((F.commission_per_contract || 0) + (F.regulatory_per_contract || 0))}/contract/leg</td></tr>
      <tr><td>Limits version</td><td class="mono">${esc(L.version)}</td></tr>
    </tbody></table>`;

  const p = s.paper || {};
  $("qq-paper").innerHTML = `<table class="data-table qq-kv"><tbody>
      <tr><td>Starting balance</td><td>${money(p.starting_balance)}</td></tr>
      <tr><td>Cash</td><td>${money(p.cash)}</td></tr>
      <tr><td>Collateral held</td><td>${money(p.collateral)}</td></tr>
      <tr><td>Available</td><td>${money(p.available)}</td></tr>
      <tr><td>Realized P&amp;L</td><td>${money(p.realized_pnl)}</td></tr>
      <tr><td>Closed trades / win rate</td><td>${p.closed_trades} / ${p.win_rate == null ? "—" : (p.win_rate * 100).toFixed(0) + "%"}</td></tr>
      <tr><td>Today realized</td><td>${money(p.today_realized)}</td></tr>
    </tbody></table>`;
}

function renderCycle(c) {
  if (!c || !c.id) { $("qq-cycle").innerHTML = `<div class="loading-text">No cycle yet.</div>`; return; }
  const rs = c.rejection_summary ? Object.entries(c.rejection_summary).sort((a, b) => b[1] - a[1]) : [];
  $("qq-cycle").innerHTML = `
    <div>${badge(c.outcome === "PROPOSAL" ? "badge-green" : "badge-amber", c.outcome)} ${esc(c.message)} <span class="qq-muted">${when(c.at)}</span></div>
    ${rs.length ? `<div class="qq-muted" style="margin-top:6px">Failed checks across candidates: ${rs.map(([k, v]) => `${esc(k)} ×${v}`).join(", ")}</div>` : ""}
    ${c.note ? `<div class="qq-muted">${esc(c.note)}</div>` : ""}
    ${(c.issues || []).length ? `<div class="qq-warn">${c.issues.slice(0, 5).map(esc).join("<br>")}</div>` : ""}`;
  const rows = (c.candidates || []).slice(0, 25);
  $("qq-candidates").innerHTML = rows.length ? `
    <div class="qq-scroll"><table class="data-table"><thead><tr>
      <th>Strategy</th><th>Expiry</th><th>DTE</th><th>Short/Long</th><th>Credit nat/mid/limit</th><th>Max loss</th>
      <th>Breakeven</th><th>Short Δ</th><th>Net θ</th><th>Net Γ</th><th>Net vega</th><th>Γ/credit</th><th>OI s/l</th><th>Risk</th>
    </tr></thead><tbody>${rows.map(r => {
      const fails = (r.risk?.checks || []).filter(x => !x.passed);
      return `<tr>
        <td>${esc(r.strategy)}</td><td>${esc(r.expiry)}</td><td>${r.dte}</td>
        <td>${r.short_strike}/${r.long_strike}</td>
        <td>${num(r.credit_natural)} / ${num(r.credit_mid)} / ${num(r.limit_credit)}</td>
        <td style="color:${r.max_loss > 50 ? "var(--red)" : "var(--green)"}">$${num(r.max_loss)}</td>
        <td>${num(r.breakeven)}</td><td>${num(r.short_delta, 3)}</td><td>${num(r.net_theta, 3)}</td>
        <td>${num(r.net_gamma, 4)}</td><td>${num(r.net_vega, 3)}</td><td>${num(r.gamma_to_credit, 4)}</td>
        <td>${r.short_oi ?? "—"}/${r.long_oi ?? "—"}</td>
        <td title="${esc(fails.map(f => `${f.name}: ${f.detail}`).join("\n"))}">${r.risk?.approved ? badge("badge-green", "PASS") : badge("badge-red", `${fails.length} fail`)}
          <div class="qq-muted" style="max-width:260px">${esc(fails.map(f => f.name).join(", "))}</div></td></tr>`;
    }).join("")}</tbody></table></div>
    <div class="qq-muted">Greeks source: ${esc(rows[0].greeks_source)}. Credits: natural = short bid − long ask (what the entry limit demands).</div>`
    : `<div class="loading-text">No candidates in the last cycle.</div>`;
}

function renderProposals(list) {
  if (!list?.length) { $("qq-proposals").innerHTML = `<div class="loading-text">No proposals yet.</div>`; return; }
  $("qq-proposals").innerHTML = list.map(p => {
    const s = p.spread || {};
    const pending = p.status === "pending_approval" && new Date(p.expires_at) > new Date();
    const head = p.kind === "exit"
      ? `EXIT ${esc(s.position_id)} · ${esc(s.strategy)} ${s.short_strike}/${s.long_strike} · limit debit ${num(s.limit_debit)}`
      : `${esc(s.strategy)} ${s.short_strike}/${s.long_strike} ${esc(s.expiry)} (${s.dte} DTE) · limit credit ${num(s.limit_credit)} · max profit ${money(s.max_profit)} · <strong>max loss ${money(s.max_loss)}</strong> · BE ${num(s.breakeven)}`;
    const plan = p.exit_plan;
    return `<div class="qq-prop">
      <div>${badge(pending ? "badge-blue" : p.status === "submitted" ? "badge-green" : "badge-amber", p.status)} ${head}
        <span class="qq-muted">${when(p.created_at)} · expires ${when(p.expires_at)} · ${esc(p.strategy_validation)}</span></div>
      <ul>${(p.thesis || []).map(t => `<li>${esc(t)}</li>`).join("")}</ul>
      ${p.kind === "entry" ? `<div class="qq-muted">Invalidation:</div><ul>${(p.invalidation || []).map(t => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
      ${plan ? `<div class="qq-muted">Exit plan: take profit at debit ≤ ${num(plan.profit_target_debit)} (50% of max profit); flag at |Δ| ${num(plan.delta_warn)}–${num(plan.delta_urgent)};
        ${plan.event_exit_deadline ? `exit by <strong>${when(plan.event_exit_deadline)}</strong> before ${esc(plan.event_exit_reason)}` : "no macro event before expiry"}.</div>
        <div class="qq-warn">${esc(plan.disclaimer)}</div>` : ""}
      ${(p.warnings || []).length ? `<div class="qq-warn">${p.warnings.map(esc).join("<br>")}</div>` : ""}
      ${p.decision_note ? `<div class="qq-muted">Decision: ${esc(p.decided_by)} — ${esc(p.decision_note)}</div>` : ""}
      ${pending ? `<div class="qq-row"><button class="btn-xs" data-approve="${esc(p.id)}">Approve (paper)</button>
        <button class="btn-xs" data-reject="${esc(p.id)}">Reject</button></div>` : ""}
    </div>`;
  }).join("");
  document.querySelectorAll("[data-approve]").forEach(b => b.onclick = async () => {
    const who = approverName(); if (!who) return;
    if (!confirm("Submit this to the PAPER broker? The risk engine re-checks with fresh quotes first.")) return;
    const r = await Q.approve(b.dataset.approve, who, "");
    r.error ? flash(r.error, true) : flash(`Proposal ${r.data.proposal.status}${r.data.order ? `, order ${r.data.order.status}` : ""}`);
    loadQqq();
  });
  document.querySelectorAll("[data-reject]").forEach(b => b.onclick = async () => {
    const who = approverName(); if (!who) return;
    const r = await Q.reject(b.dataset.reject, who, prompt("Reason (optional):") || "");
    r.error ? flash(r.error, true) : flash("Rejected");
    loadQqq();
  });
}

function renderPositions(positions, orders, mon) {
  const live = Object.fromEntries((mon?.positions || []).map(r => [r.position_id, r]));
  $("qq-positions").innerHTML = positions?.length ? `<div class="qq-scroll"><table class="data-table"><thead><tr>
      <th>ID</th><th>Status</th><th>Spread</th><th>Expiry</th><th>Qty</th><th>Entry credit</th><th>Debit (mid)</th>
      <th>Short Δ</th><th>P&amp;L</th><th>Exit flags</th></tr></thead><tbody>
      ${positions.map(p => { const m = live[p.id] || {}; return `<tr>
        <td class="mono">${esc(p.id)}</td><td>${esc(p.status)}</td><td>${esc(p.strategy)} ${p.short_strike}/${p.long_strike}</td>
        <td>${esc(p.expiry)}</td><td>${p.contracts}</td><td>${num(p.entry_credit)}</td><td>${num(m.debit_mid)}</td>
        <td>${num(m.short_delta, 3)}</td><td>${p.status === "closed" ? money(p.realized_pnl) : money(m.unrealized_pnl)}</td>
        <td>${(m.exit_reasons || []).map(esc).join("<br>")}</td></tr>`; }).join("")}
      </tbody></table></div>` : `<div class="loading-text">No paper positions.</div>`;
  $("qq-orders").innerHTML = orders?.length ? `<table class="data-table"><thead><tr><th>Order</th><th>Intent</th><th>Status</th>
      <th>Filled</th><th>Limit</th><th>Avg fill</th><th>Note</th></tr></thead><tbody>
      ${orders.slice(0, 15).map(o => `<tr><td class="mono">${esc(o.client_order_id)}</td><td>${esc(o.intent)}</td>
        <td>${esc(o.status)}</td><td>${o.filled_contracts}/${o.contracts}</td><td>${num(o.limit_price)}</td>
        <td>${num(o.avg_fill_price)}</td><td>${esc(o.note)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="loading-text">No paper orders.</div>`;
}

function renderBacktests(bts, vals) {
  const vById = Object.fromEntries((vals || []).map(v => [v.backtest_id, v]));
  $("qq-backtests").innerHTML = bts?.length ? bts.map(b => {
    const v = vById[b.id]; const m = b.metrics || {}; const o = v?.metrics?.out_of_sample || {};
    return `<div class="qq-prop">
      <div>${badge(v?.status === "APPROVED" ? "badge-green" : "badge-red", v?.status || "not validated")}
        <span class="mono">${esc(b.id)}</span> · ${esc(b.data_source)} ${b.synthetic ? badge("badge-amber", "SYNTHETIC") : ""}
        ${b.params?.sandbox_limits ? badge("badge-amber", "WHAT-IF LIMITS") : ""} · ${esc(b.start)} → ${esc(b.end)}</div>
      <div class="qq-muted">Full period: ${m.trades} trades · net ${money(m.net_pnl)} · win ${m.win_rate == null ? "—" : (m.win_rate * 100).toFixed(0) + "%"}
        · expectancy ${num(m.expectancy)} · PF ${esc(m.profit_factor ?? "—")} · Sharpe ${num(m.sharpe)} · max DD ${money(m.max_drawdown)}</div>
      <div class="qq-muted">Out-of-sample: ${o.trades ?? 0} trades · expectancy ${num(o.expectancy)} · PF ${esc(o.profit_factor ?? "—")} · max DD ${money(o.max_drawdown)}</div>
      <div class="qq-muted">Fill: mid ± ${b.slippage_fraction}×half-spread, lag ${b.fill_lag_days}d · missing data ${b.days_missing_data}/${b.days_total} days · missing Greeks ${b.days_missing_greeks}</div>
      ${v?.reasons?.length ? `<div class="qq-warn">${v.reasons.map(esc).join("<br>")}</div>` : ""}
    </div>`;
  }).join("") : `<div class="loading-text">No backtests yet.</div>`;
}

function renderResearch(r) {
  $("qq-hypotheses").innerHTML = (r?.hypotheses || []).map(h => `<div class="qq-prop">
      <div><strong>${esc(h.id)}</strong> ${esc(h.title)} ${badge("badge-blue", h.status)}</div>
      <div class="qq-muted">Edge: ${esc(h.edge_rationale)}</div>
      <div class="qq-muted">Disproven if: ${esc(h.falsification)}</div></div>`).join("") || `<div class="loading-text">—</div>`;
  $("qq-log").innerHTML = (r?.log || []).map(e => `<div class="qq-logline"><span class="mono">v${e.version}</span>
      <span class="qq-muted">${when(e.at)} ${esc(e.author)}</span> ${esc(e.text)}</div>`).join("") || `<div class="loading-text">Empty.</div>`;
}

function renderActivity(acts, alerts) {
  $("qq-activity").innerHTML = (acts || []).map(a => `<div class="qq-logline"><span class="qq-muted">${when(a.at)}</span>
      <strong>${esc(a.agent)}</strong> ${esc(a.message)}</div>`).join("") || `<div class="loading-text">No activity.</div>`;
  $("qq-alerts").innerHTML = (alerts || []).map(a => `<div class="qq-logline">${badge(a.level === "risk" ? "badge-red" : "badge-blue", a.level)}
      <span class="qq-muted">${when(a.at)}</span> <strong>${esc(a.title)}</strong> ${esc(a.body)}</div>`).join("") || `<div class="loading-text">No alerts.</div>`;
}

// ── public ────────────────────────────────────────────────────────────────────

let _timer = null;

export function initQqqView() {
  const form = $("qq-bt-form");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const body = { source: $("qq-bt-source").value, start: $("qq-bt-start").value, end: $("qq-bt-end").value,
                   csv_dir: $("qq-bt-csv").value || null, require_macro_calendar: !$("qq-bt-nomacro").checked };
    $("qq-bt-msg").textContent = "Running backtest + walk-forward + validation (may take minutes)…";
    const r = await Q.backtest(body);
    $("qq-bt-msg").textContent = r.error ? r.error : `Done: ${r.data.validation.status}`;
    loadQqq();
  };
}

export async function loadQqq() {
  const [st, cyc, props, pos, ords, mon, bts, vals, res, act, al] = await Promise.all([
    Q.status(), Q.latest(), Q.proposals(), Q.positions(), Q.orders(), Q.monitorLast(),
    Q.backtests(), Q.validations(), Q.research(), Q.activity(), Q.alerts()]);
  if (st.error) { $("qq-controls").innerHTML = `<div class="qq-warn">${esc(st.error)}</div>`; return; }
  renderStatus(st.data);
  renderCycle(cyc.data);
  renderProposals(props.data);
  renderPositions(pos.data, ords.data, mon.data);
  renderBacktests(bts.data, vals.data);
  renderResearch(res.data);
  renderActivity(act.data, al.data);
  if (!_timer) _timer = setInterval(() => {
    if ($("view-qqq")?.classList.contains("active") && document.visibilityState === "visible") loadQqq();
  }, 30_000);
}
