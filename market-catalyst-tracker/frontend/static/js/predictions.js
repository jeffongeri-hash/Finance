/**
 * predictions.js — Polymarket Prediction Markets view
 *
 * Four tabs:
 *   All        — top markets by volume across all categories
 *   Biotech    — FDA approvals, drug trials, clinical outcomes
 *   Macro      — Fed rates, CPI, recession, tariffs
 *   Geopolitical — trade war, elections, conflict
 *
 * Each market card shows:
 *   • The question text
 *   • YES probability (large, colour-coded)
 *   • NO probability
 *   • Volume ($)
 *   • End date
 *   • Deep link to Polymarket
 *
 * Data: Gamma API (gamma-api.polymarket.com) + CLOB midpoint (clob.polymarket.com)
 * No API key required.
 */

import { API, Fmt } from "./api.js";

// ── Extra Fmt helpers specific to prediction markets ──────────────────────────

function probColor(p) {
  if (p == null) return "var(--text-muted)";
  if (p >= 0.70) return "var(--green)";
  if (p >= 0.40) return "var(--amber)";
  return "var(--red)";
}

function probLabel(p) {
  if (p == null) return "—";
  return `${Math.round(p * 100)}%`;
}

function volLabel(v) {
  if (!v || v === 0) return "—";
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)    return `$${(v / 1_000).toFixed(0)}K`;
  return `$${v.toFixed(0)}`;
}

function categoryBadge(cat) {
  const map = {
    biotech_fda:   ["badge-green",  "Biotech/FDA"],
    macro:         ["badge-amber",  "Macro"],
    geopolitical:  ["badge-red",    "Geopolitical"],
    crypto:        ["badge-purple", "Crypto"],
    other:         ["badge-blue",   "Other"],
  };
  const [cls, label] = map[cat] || ["badge-blue", cat];
  return `<span class="badge ${cls}">${label}</span>`;
}

// ── Market card renderer ──────────────────────────────────────────────────────

function renderMarketCard(m) {
  const yesP  = m.yes_price;
  const noP   = m.no_price  ?? (yesP != null ? 1 - yesP : null);
  const color  = probColor(yesP);
  const pctStr = probLabel(yesP);
  const vol    = volLabel(m.volume);
  const liq    = volLabel(m.liquidity);
  const endStr = m.end_date ? Fmt.date(m.end_date.slice(0, 10)) : "—";
  const pmUrl  = m.url || `https://polymarket.com/event/${m.slug}`;

  // Progress bar: YES fill
  const yesFill = yesP != null ? Math.round(yesP * 100) : 50;
  const noFill  = 100 - yesFill;

  return `
    <div class="pm-card" data-cid="${m.condition_id}">
      <div class="pm-card-top">
        <div class="pm-question" title="${m.question}">${m.question}</div>
        <div class="pm-tags">
          ${categoryBadge(m.category || "other")}
          ${m.closed ? '<span class="badge badge-red">Closed</span>' : ""}
        </div>
      </div>

      <!-- Probability bar -->
      <div class="pm-prob-bar" title="YES ${pctStr} · NO ${probLabel(noP)}">
        <div class="pm-prob-yes" style="width:${yesFill}%;background:${color}"></div>
        <div class="pm-prob-no"  style="width:${noFill}%;"></div>
      </div>

      <div class="pm-stats">
        <div class="pm-stat">
          <div class="pm-stat-label">YES</div>
          <div class="pm-stat-value" style="color:${color};font-size:22px;font-weight:800">
            ${pctStr}
          </div>
        </div>
        <div class="pm-stat">
          <div class="pm-stat-label">NO</div>
          <div class="pm-stat-value" style="color:var(--red)">
            ${probLabel(noP)}
          </div>
        </div>
        <div class="pm-stat">
          <div class="pm-stat-label">Volume</div>
          <div class="pm-stat-value">${vol}</div>
        </div>
        <div class="pm-stat">
          <div class="pm-stat-label">Liquidity</div>
          <div class="pm-stat-value">${liq}</div>
        </div>
        <div class="pm-stat">
          <div class="pm-stat-label">Resolves</div>
          <div class="pm-stat-value fs-11">${endStr}</div>
        </div>
        <div class="pm-stat">
          <div class="pm-stat-label">Source</div>
          <div class="pm-stat-value fs-11">
            ${m.price_source === "clob_live"
              ? '<span class="badge badge-green" style="font-size:9px">LIVE</span>'
              : '<span class="badge badge-blue" style="font-size:9px">CACHED</span>'}
          </div>
        </div>
      </div>

      <a href="${pmUrl}" target="_blank" class="pm-link" onclick="event.stopPropagation()">
        View on Polymarket ↗
      </a>
    </div>
  `;
}

// ── Search result renderer (compact row) ─────────────────────────────────────

function renderSearchRow(m) {
  const yesP   = m.yes_price;
  const color  = probColor(yesP);
  const pmUrl  = m.url || `https://polymarket.com/event/${m.slug}`;
  return `
    <div class="pm-search-row">
      <div class="pm-search-q">${m.question}</div>
      <div class="pm-search-meta">
        <span style="font-family:var(--font-mono);font-weight:800;color:${color};font-size:14px">
          ${probLabel(yesP)} YES
        </span>
        <span class="text-muted fs-11">${volLabel(m.volume)} vol</span>
        ${categoryBadge(m.category || "other")}
        <a href="${pmUrl}" target="_blank" class="card-action" onclick="event.stopPropagation()">↗</a>
      </div>
    </div>
  `;
}

// ── Render helpers ────────────────────────────────────────────────────────────

function renderGrid(markets, container) {
  if (!container) return;
  if (!markets || markets.length === 0) {
    container.innerHTML = `<div class="loading-text" style="grid-column:1/-1;padding:40px">No markets found.</div>`;
    return;
  }
  container.innerHTML = markets.map(renderMarketCard).join("");
}

function renderStats(markets, statsEl) {
  if (!statsEl) return;
  const total   = markets.length;
  const highConv = markets.filter(m => m.yes_price != null && (m.yes_price >= 0.75 || m.yes_price <= 0.25)).length;
  const totalVol = markets.reduce((s, m) => s + (m.volume || 0), 0);
  statsEl.innerHTML = `
    <span class="text-dim fs-11">${total} markets</span>
    <span class="badge badge-green">${highConv} high-conviction (&gt;75% or &lt;25%)</span>
    <span class="badge badge-amber">${volLabel(totalVol)} total volume</span>
  `;
}

// ── Active tab state ──────────────────────────────────────────────────────────

let _loaded = new Set();
let _activeTab = "all";

// ── Tab loaders ───────────────────────────────────────────────────────────────

async function loadTab(tab) {
  _activeTab = tab;

  const grid  = document.getElementById(`pm-grid-${tab}`);
  const stats = document.getElementById(`pm-stats-${tab}`);

  if (_loaded.has(tab)) return;
  _loaded.add(tab);

  if (grid) grid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;padding:60px">Loading Polymarket data…</div>`;

  let endpoint, key;
  switch (tab) {
    case "all":         endpoint = API.predictions_top();     key = "markets"; break;
    case "biotech":     endpoint = API.predictions_biotech(); key = "markets"; break;
    case "macro":       endpoint = API.predictions_macro();   key = "markets"; break;
    case "geopolitical":endpoint = API.predictions_geo();     key = "markets"; break;
    default: return;
  }

  const { data, error } = await endpoint;

  if (error || !data) {
    if (grid) grid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;color:var(--red)">Error: ${error || "Unknown"}</div>`;
    return;
  }

  const markets = data[key] || [];
  renderGrid(markets, grid);
  renderStats(markets, stats);
}

// ── Search ────────────────────────────────────────────────────────────────────

let _searchTimeout = null;

async function handleSearch(query) {
  const container = document.getElementById("pm-search-results");
  if (!container) return;

  if (!query || query.length < 2) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = `<div class="loading-text">Searching…</div>`;

  const { data, error } = await API.predictions_search(query);

  if (error || !data?.results?.length) {
    container.innerHTML = `<div class="loading-text">${error || "No results"}</div>`;
    return;
  }

  container.innerHTML = data.results.map(renderSearchRow).join("");
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function initPredictionsView() {
  // Tab switching
  const tabBtns = document.querySelectorAll("[data-pm-tab]");
  const panels  = document.querySelectorAll("[data-pm-panel]");

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      panels.forEach(p => p.classList.add("hidden"));
      btn.classList.add("active");

      const tab = btn.dataset.pmTab;
      const panel = document.querySelector(`[data-pm-panel="${tab}"]`);
      if (panel) panel.classList.remove("hidden");
      loadTab(tab);
    });
  });

  // Search
  const searchInput = document.getElementById("pm-search-input");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      clearTimeout(_searchTimeout);
      _searchTimeout = setTimeout(() => handleSearch(e.target.value.trim()), 400);
    });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        searchInput.value = "";
        const c = document.getElementById("pm-search-results");
        if (c) c.innerHTML = "";
      }
    });
  }

  // Refresh button
  const refreshBtn = document.getElementById("pm-refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      _loaded.clear();
      loadTab(_activeTab);
    });
  }
}

export async function loadPredictions() {
  loadTab("all");
}

// Attach API extension methods (add to api.js API object)
// These are added here to keep api.js clean of prediction-specific endpoints.
import { API as _API } from "./api.js";

Object.assign(_API, {
  predictions_top:    ()      => fetch("/api/predictions/top").then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message })),
  predictions_biotech:()      => fetch("/api/predictions/biotech").then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message })),
  predictions_macro:  ()      => fetch("/api/predictions/macro").then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message })),
  predictions_geo:    ()      => fetch("/api/predictions/geopolitical").then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message })),
  predictions_search: (q)     => fetch(`/api/predictions/search?q=${encodeURIComponent(q)}`).then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message })),
  catalysts_enriched: (p, n)  => {
    const qs = [p && `priority=${p}`, n && `limit=${n}`].filter(Boolean).join("&");
    return fetch(`/api/catalysts/biotech/enriched${qs ? "?" + qs : ""}`).then(r => r.json()).then(d => ({ data: d, error: null })).catch(e => ({ data: null, error: e.message }));
  },
});
