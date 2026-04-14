/**
 * Equity AI View — ai-hedge-fund analyst engine integration
 * ==========================================================
 * Displays 19 LLM-powered analyst signals (bullish/bearish/neutral + confidence)
 * and the portfolio manager's final action recommendation per ticker.
 *
 * Requires:
 *   FINANCIAL_DATASETS_API_KEY  — fundamental data
 *   OPENAI_API_KEY (or another LLM key) — agent reasoning
 */

import { API, Fmt } from "/static/js/api.js";

// ── API extension ──────────────────────────────────────────────────────────────

Object.assign(API, {
  equityStatus:  ()                          => API._fetch("/api/equity/status"),
  equityAgents:  ()                          => API._fetch("/api/equity/agents"),
  equityAnalyze: (tickers, opts = {})        => {
    const params = new URLSearchParams({ tickers });
    if (opts.analysts)  params.set("analysts",  opts.analysts);
    if (opts.model)     params.set("model",     opts.model);
    if (opts.provider)  params.set("provider",  opts.provider);
    if (opts.startDate) params.set("start_date",opts.startDate);
    if (opts.endDate)   params.set("end_date",  opts.endDate);
    if (opts.reasoning) params.set("reasoning", "true");
    return fetch(`${location.origin}/api/equity/analyze?${params}`, { method: "POST" })
      .then(async r => {
        const data = await r.json();
        return r.ok ? { data, error: null } : { data: null, error: data.detail || `HTTP ${r.status}` };
      })
      .catch(e => ({ data: null, error: e.message }));
  },
});

// ── State ──────────────────────────────────────────────────────────────────────

let _agents     = [];      // Full agent catalogue from /api/equity/agents
let _lastResult = null;    // Last run_analysis result
let _selectedAgents = new Set(); // Keys of selected agents (empty = all)
let _showReasoning  = false;

// ── Signal colours ─────────────────────────────────────────────────────────────

const SIG_CFG = {
  bullish:  { cls: "eq-bull",    label: "Bullish",  icon: "▲" },
  bearish:  { cls: "eq-bear",    label: "Bearish",  icon: "▼" },
  neutral:  { cls: "eq-neutral", label: "Neutral",  icon: "◆" },
};

const ACTION_CFG = {
  buy:        { cls: "eq-buy",        label: "BUY" },
  sell:       { cls: "eq-sell",       label: "SELL" },
  short:      { cls: "eq-sell",       label: "SHORT" },
  cover:      { cls: "eq-buy",        label: "COVER" },
  hold:       { cls: "eq-hold",       label: "HOLD" },
};

// ── Confidence bar ─────────────────────────────────────────────────────────────

function _confBar(conf, sigClass) {
  const pct = Math.round(Math.min(100, Math.max(0, (conf || 0) * 100)));
  return `<div class="eq-conf-bar-wrap">
    <div class="eq-conf-bar ${sigClass}" style="width:${pct}%"></div>
    <span class="eq-conf-label">${pct}%</span>
  </div>`;
}

// ── Status panel ───────────────────────────────────────────────────────────────

function renderStatusPanel(status) {
  const el = document.getElementById("eq-status-panel");
  if (!el) return;

  if (status.available) {
    el.innerHTML = `
      <div class="eq-status-ok">
        <span class="eq-dot eq-dot-green"></span>
        Equity AI ready &nbsp;·&nbsp; <span class="dim">${status.model_name} via ${status.model_provider}</span>
      </div>`;
  } else {
    const warnings = (status.warnings || []).map(w =>
      `<li>${w}</li>`
    ).join("");
    el.innerHTML = `
      <div class="eq-status-warn">
        <span class="eq-dot eq-dot-amber"></span>
        <strong>Equity AI not configured</strong>
        <ul class="eq-warn-list">${warnings}</ul>
        <div class="dim" style="margin-top:6px;font-size:11px">
          Add keys to your <code>.env</code> file and restart the server.
        </div>
      </div>`;
  }
}

// ── Agent selector ─────────────────────────────────────────────────────────────

function renderAgentSelector(agents) {
  const wrap = document.getElementById("eq-agent-selector");
  if (!wrap) return;

  _agents = agents;

  const rows = agents.map(a => {
    const checked = _selectedAgents.size === 0 || _selectedAgents.has(a.key) ? "checked" : "";
    return `
      <label class="eq-agent-chip ${checked ? "selected" : ""}" data-key="${a.key}">
        <input type="checkbox" value="${a.key}" ${checked} hidden>
        <span class="eq-agent-name">${a.display_name}</span>
      </label>`;
  }).join("");

  wrap.innerHTML = `
    <div class="eq-agent-header">
      <span class="dim" style="font-size:12px">Select analysts (all selected by default)</span>
      <span>
        <button id="eq-select-all"  class="btn-xs">All</button>
        <button id="eq-select-none" class="btn-xs">None</button>
      </span>
    </div>
    <div class="eq-agent-grid">${rows}</div>`;

  // Toggle individual agents
  wrap.querySelectorAll(".eq-agent-chip").forEach(chip => {
    chip.addEventListener("click", () => {
      const key   = chip.dataset.key;
      const input = chip.querySelector("input");
      input.checked = !input.checked;
      chip.classList.toggle("selected", input.checked);
      _syncSelectedAgents();
    });
  });

  document.getElementById("eq-select-all")?.addEventListener("click", () => {
    wrap.querySelectorAll("input[type=checkbox]").forEach(i => { i.checked = true; });
    wrap.querySelectorAll(".eq-agent-chip").forEach(c => c.classList.add("selected"));
    _selectedAgents.clear();
  });

  document.getElementById("eq-select-none")?.addEventListener("click", () => {
    wrap.querySelectorAll("input[type=checkbox]").forEach(i => { i.checked = false; });
    wrap.querySelectorAll(".eq-agent-chip").forEach(c => c.classList.remove("selected"));
    _selectedAgents = new Set(_agents.map(a => a.key));   // treat "none selected" as "show all but run none"
  });
}

function _syncSelectedAgents() {
  const wrap    = document.getElementById("eq-agent-selector");
  const checked = [...wrap.querySelectorAll("input:checked")].map(i => i.value);
  _selectedAgents = checked.length === _agents.length ? new Set() : new Set(checked);
}

function _getSelectedKeys() {
  return _selectedAgents.size === 0 ? "" : [..._selectedAgents].join(",");
}

// ── Summary bar (per ticker) ────────────────────────────────────────────────────

function renderTickerSummaryBar(ticker, summary) {
  const { bullish, bearish, neutral, total } = summary;
  if (!total) return `<span class="dim">no signals</span>`;

  const bullW = Math.round((bullish / total) * 100);
  const bearW = Math.round((bearish / total) * 100);
  const neutW = 100 - bullW - bearW;

  return `
    <div class="eq-vote-bar" title="${bullish} bullish / ${bearish} bearish / ${neutral} neutral">
      <div class="eq-vote-bull" style="width:${bullW}%"></div>
      <div class="eq-vote-neut" style="width:${neutW}%"></div>
      <div class="eq-vote-bear" style="width:${bearW}%"></div>
    </div>
    <div class="eq-vote-counts">
      <span class="eq-bull">▲ ${bullish}</span>
      <span class="eq-neutral">◆ ${neutral}</span>
      <span class="eq-bear">▼ ${bearish}</span>
    </div>`;
}

// ── Decision card ──────────────────────────────────────────────────────────────

function renderDecisionCard(ticker, decision, summary) {
  if (!decision) return "";
  const action   = (decision.action || "hold").toLowerCase();
  const cfg      = ACTION_CFG[action] || ACTION_CFG.hold;
  const confPct  = Math.round((decision.confidence || 0) * 100);
  const qty      = decision.quantity ?? "—";
  const reasoning = decision.reasoning ? `
    <div class="eq-reasoning" style="display:none">
      <p class="dim" style="font-size:12px;line-height:1.5">${decision.reasoning}</p>
    </div>` : "";

  return `
    <div class="eq-decision-card">
      <div class="eq-decision-header">
        <span class="eq-ticker-badge">${ticker}</span>
        <span class="eq-action-badge ${cfg.cls}">${cfg.label}</span>
        <span class="dim" style="font-size:12px">qty: ${qty} &nbsp;·&nbsp; conf: ${confPct}%</span>
        ${decision.reasoning ? `<button class="btn-xs eq-reasoning-toggle" data-target="eq-r-${ticker}">Reasoning ▾</button>` : ""}
      </div>
      <div style="margin-top:8px">${renderTickerSummaryBar(ticker, summary)}</div>
      ${reasoning ? `<div id="eq-r-${ticker}">${reasoning}</div>` : ""}
    </div>`;
}

// ── Signal matrix (analysts × tickers) ────────────────────────────────────────

function renderSignalMatrix(result) {
  const { tickers, analysts_used, analyst_signals, decisions, summary } = result;

  // Decision cards row
  const decisionCards = tickers.map(t =>
    renderDecisionCard(t, decisions?.[t], summary?.[t] || { bullish:0, bearish:0, neutral:0, total:0 })
  ).join("");

  // Build header row
  const headerCells = tickers.map(t => `<th class="eq-th">${t}</th>`).join("");

  // Build one row per analyst
  const agentMeta = Object.fromEntries(_agents.map(a => [a.key, a]));

  const rows = analysts_used.map(aKey => {
    const meta  = agentMeta[aKey] || { display_name: aKey };
    const cells = tickers.map(ticker => {
      const sig    = analyst_signals?.[aKey]?.[ticker];
      if (!sig)    return `<td class="eq-td"><span class="dim">—</span></td>`;
      const s      = (sig.signal || "neutral").toLowerCase();
      const cfg    = SIG_CFG[s] || SIG_CFG.neutral;
      const conf   = sig.confidence || 0;
      const tip    = sig.reasoning
        ? sig.reasoning.replace(/"/g, "&quot;").substring(0, 200)
        : "";
      return `<td class="eq-td">
        <span class="${cfg.cls}" title="${tip}">${cfg.icon} ${cfg.label}</span>
        ${_confBar(conf, cfg.cls)}
      </td>`;
    }).join("");

    return `<tr>
      <td class="eq-agent-td">
        <span class="eq-agent-name-sm">${meta.display_name}</span>
      </td>
      ${cells}
    </tr>`;
  }).join("");

  return `
    <div class="eq-decisions-row">${decisionCards}</div>
    <div class="eq-matrix-wrap">
      <table class="eq-matrix">
        <thead>
          <tr>
            <th class="eq-th eq-agent-th">Analyst</th>
            ${headerCells}
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="eq-meta">
      ${result.analysts_used.length} analysts &nbsp;·&nbsp;
      ${result.model} via ${result.provider} &nbsp;·&nbsp;
      ${result.start_date} → ${result.end_date} &nbsp;·&nbsp;
      ran in ${result.elapsed_s}s
    </div>`;
}

// ── Main run function ──────────────────────────────────────────────────────────

async function runEquityAnalysis() {
  const input   = document.getElementById("eq-ticker-input");
  const results = document.getElementById("eq-results");
  const btn     = document.getElementById("eq-run-btn");
  if (!input || !results || !btn) return;

  const rawTickers = input.value.trim();
  if (!rawTickers) {
    input.focus();
    return;
  }

  btn.disabled  = true;
  btn.textContent = "Analysing…";
  results.innerHTML = `
    <div class="eq-loading">
      <div class="eq-spinner"></div>
      <p>Running ${_agents.length || "all"} analyst agents — this may take 30-120s per ticker…</p>
    </div>`;

  const { data, error } = await API.equityAnalyze(rawTickers, {
    analysts:  _getSelectedKeys(),
    reasoning: _showReasoning,
  });

  btn.disabled    = false;
  btn.textContent = "Run Analysis";

  if (error || !data) {
    results.innerHTML = `<div class="eq-error">${error || "Unknown error"}</div>`;
    return;
  }

  _lastResult = data;
  results.innerHTML = renderSignalMatrix(data);

  // Wire reasoning toggles
  results.querySelectorAll(".eq-reasoning-toggle").forEach(btn => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      const inner = target.querySelector(".eq-reasoning");
      if (inner) {
        const visible = inner.style.display !== "none";
        inner.style.display = visible ? "none" : "block";
        btn.textContent = visible ? "Reasoning ▾" : "Reasoning ▴";
      }
    });
  });
}

// ── Initialise view ────────────────────────────────────────────────────────────

export function initEquityView() {
  const btn   = document.getElementById("eq-run-btn");
  const input = document.getElementById("eq-ticker-input");
  const toggle = document.getElementById("eq-reasoning-toggle");

  btn?.addEventListener("click", runEquityAnalysis);

  input?.addEventListener("keydown", e => {
    if (e.key === "Enter") runEquityAnalysis();
  });

  toggle?.addEventListener("change", () => {
    _showReasoning = toggle.checked;
  });

  // Quick-pick badges
  document.querySelectorAll("[data-eq-ticker]").forEach(el => {
    el.addEventListener("click", () => {
      if (input) input.value = el.dataset.eqTicker;
    });
  });
}

export async function loadEquity() {
  // Status check
  const { data: status } = await API.equityStatus();
  if (status) renderStatusPanel(status);

  // Load agent catalogue
  const { data: agents } = await API.equityAgents();
  if (agents?.agents) renderAgentSelector(agents.agents);
}
