/**
 * catalyst.js — Biotech / FDA Catalyst Calendar view
 * Shows upcoming PDUFA dates, Phase 3 readouts, FDA advisory committee meetings,
 * and NDA submissions sourced from FDA API + SEC EDGAR + ClinicalTrials.gov.
 */

import { API, Fmt, priorityBadge, eventTypeBadge, changeClass } from "./api.js";
import {} from "./predictions.js";  // side-effect: attaches API.catalysts_enriched

// ── DOM refs ──────────────────────────────────────────────────────────────────
const catalystList   = document.getElementById("catalyst-list");
const catalystStats  = document.getElementById("catalyst-stats");
const catalystFilter = document.getElementById("catalyst-filter-priority");
const refreshBtn     = document.getElementById("catalyst-refresh-btn");

let _allEvents = [];

// ── Render ────────────────────────────────────────────────────────────────────

function renderStats(events) {
  if (!catalystStats) return;
  const high   = events.filter(e => e.priority === "HIGH").length;
  const within30 = events.filter(e => e.days_until != null && e.days_until >= 0 && e.days_until <= 30).length;
  const total  = events.length;
  catalystStats.innerHTML = `
    <span class="badge badge-green">${high} HIGH priority</span>
    <span class="badge badge-amber">${within30} within 30 days</span>
    <span class="text-dim fs-11">${total} total events</span>
  `;
}

function renderPMCell(pm) {
  if (!pm) return `<div class="text-dim fs-11">—</div>`;
  const yes = pm.yes_price;
  const color = yes >= 0.70 ? "var(--green)" : yes >= 0.40 ? "var(--amber)" : "var(--red)";
  const pctStr = yes != null ? `${Math.round(yes * 100)}%` : "—";
  const pmUrl  = pm.url || `https://polymarket.com/event/${pm.slug}`;
  return `
    <div style="display:flex;flex-direction:column;gap:3px">
      <div style="font-family:var(--font-mono);font-size:15px;font-weight:800;color:${color}">${pctStr} YES</div>
      <div class="fs-10 text-dim">$${pm.volume >= 1e6 ? (pm.volume/1e6).toFixed(1)+"M" : pm.volume >= 1e3 ? (pm.volume/1e3).toFixed(0)+"K" : "—"} vol</div>
      <a href="${pmUrl}" target="_blank" onclick="event.stopPropagation()" class="fs-10" style="color:var(--purple)">Polymarket ↗</a>
    </div>
  `;
}

function renderCatalystRow(ev) {
  const priceChgCls = changeClass(ev.price_change_pct || 0);
  const daysVal = ev.days_until;
  const daysClass = daysVal != null && daysVal <= 7 ? "pos" :
                    daysVal != null && daysVal <= 30 ? "" : "text-muted";
  const pm = ev.prediction_market || null;   // from enriched endpoint

  return `
    <div class="catalyst-row" data-symbol="${ev.symbol}" onclick="window.openChart && window.openChart('${ev.symbol}')">

      <div>
        <div class="catalyst-sym">${ev.symbol}</div>
        <div class="fs-11 text-dim" style="margin-top:2px">${Fmt.bigNum(ev.market_cap)}</div>
      </div>

      <div>
        ${eventTypeBadge(ev.event_type)}
        <div class="fs-11 text-muted" style="margin-top:4px">${priorityBadge(ev.priority)}</div>
      </div>

      <div class="catalyst-description" title="${ev.description}">
        ${ev.description}
        ${ev.source_url ? `<a href="${ev.source_url}" target="_blank" onclick="event.stopPropagation()"
           class="fs-11" style="margin-left:6px;color:var(--text-dim)">↗</a>` : ""}
      </div>

      <div class="countdown ${daysClass}">
        ${Fmt.countdown(daysVal)}
        ${ev.event_date ? `<div class="fs-11 text-dim">${Fmt.date(ev.event_date)}</div>` : ""}
      </div>

      <div>
        <div class="mono fw-700">${ev.price ? Fmt.price(ev.price) : "—"}</div>
        ${ev.price_change_pct != null
          ? `<div class="mono fs-11 ${priceChgCls}">${Fmt.pct(ev.price_change_pct)}</div>`
          : ""}
      </div>

      <div>${renderPMCell(pm)}</div>

    </div>
  `;
}

function renderCatalystHeader() {
  return `
    <div class="catalyst-row" style="cursor:default;opacity:0.5;pointer-events:none;border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:6px">
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)">Symbol</div>
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)">Event</div>
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)">Description</div>
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)">Countdown</div>
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim)">Price</div>
      <div class="fs-10 fw-700" style="letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim);color:var(--purple)">Polymarket %</div>
    </div>
  `;
}

function applyFilter() {
  const priority = catalystFilter?.value || "ALL";
  const filtered = priority === "ALL"
    ? _allEvents
    : _allEvents.filter(e => e.priority === priority);
  renderList(filtered);
}

function renderList(events) {
  if (!catalystList) return;
  if (!events || events.length === 0) {
    catalystList.innerHTML = `
      <div class="loading-text" style="padding:40px">
        No catalyst events found. The scan may still be in progress — try refreshing.
      </div>
    `;
    return;
  }

  renderStats(events);
  catalystList.innerHTML = renderCatalystHeader() + events.map(renderCatalystRow).join("");
}

// ── Legend ────────────────────────────────────────────────────────────────────

function renderLegend() {
  const el = document.getElementById("catalyst-legend");
  if (!el) return;
  const types = [
    ["FDA_PDUFA",       "PDUFA date — FDA must act on drug application by this date"],
    ["FDA_ADCOM",       "Advisory Committee meeting — expert panel vote before FDA decision"],
    ["PHASE3_RESULT",   "Phase 3 primary endpoint readout"],
    ["NDA_SUBMISSION",  "New Drug Application / BLA submitted to FDA"],
    ["SEC_8K_FDA",      "8-K filing disclosing FDA-related event"],
    ["SEC_8K_APPROVAL", "FDA approval confirmed in 8-K filing"],
    ["SEC_8K_CRL",      "Complete Response Letter (rejection/delay) in 8-K"],
  ];
  el.innerHTML = types.map(([type, desc]) => `
    <div class="flex items-center gap-8" style="margin-bottom:6px">
      ${eventTypeBadge(type)}
      <span class="fs-11 text-muted">${desc}</span>
    </div>
  `).join("");
}

// ── Load ──────────────────────────────────────────────────────────────────────

export async function loadCatalysts() {
  if (catalystList) catalystList.innerHTML = `<div class="loading-text" style="padding:60px">Running catalyst scan…<br><span class="fs-11 text-dim">(Querying FDA API, SEC EDGAR, ClinicalTrials.gov)</span></div>`;
  if (catalystStats) catalystStats.innerHTML = "";

  renderLegend();

  // Try enriched endpoint first (includes Polymarket odds); fall back to plain
  let data, error;
  ({ data, error } = await (API.catalysts_enriched
    ? API.catalysts_enriched(null, 30)
    : API.biotechCatalysts()));

  // catalysts_enriched returns {events: [...]} shape
  if (data && data.events) data = data.events;

  if (error || !data) {
    ({ data, error } = await API.biotechCatalysts());
  }

  if (error) {
    catalystList.innerHTML = `
      <div class="loading-text" style="padding:40px;color:var(--red)">
        Error loading catalysts: ${error}
      </div>
    `;
    return;
  }

  _allEvents = data || [];
  applyFilter();

  // Update badge count on nav
  const badge = document.getElementById("catalyst-nav-badge");
  if (badge) badge.textContent = _allEvents.filter(e => e.priority === "HIGH").length || "";
}

// ── Filter + refresh bindings ─────────────────────────────────────────────────

export function initCatalystView() {
  if (catalystFilter) {
    catalystFilter.addEventListener("change", applyFilter);
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", loadCatalysts);
  }
}
