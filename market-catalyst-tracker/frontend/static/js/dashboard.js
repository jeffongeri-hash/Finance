/**
 * dashboard.js — Market Overview view
 * Handles: index cards, sector grid, top movers table,
 *          "Why is the market moving?" panel, live WebSocket updates.
 */

import { API, Fmt, changeClass, priorityBadge, eventTypeBadge, sentimentBadge, createPriceStream } from "./api.js";

// ── DOM refs ──────────────────────────────────────────────────────────────────
const indicesGrid   = document.getElementById("indices-grid");
const sectorsGrid   = document.getElementById("sectors-grid");
const gainersBody   = document.getElementById("gainers-body");
const losersBody    = document.getElementById("losers-body");
const whyPanel      = document.getElementById("why-moving-panel");
const newsContainer = document.getElementById("dashboard-news");
const topbarTicker  = document.getElementById("index-ticker");
const wsStatus      = document.getElementById("ws-status");

// ── State ─────────────────────────────────────────────────────────────────────
const _liveQuotes = {};   // symbol → latest quote data

// ── Renderers ─────────────────────────────────────────────────────────────────

function renderQuoteCard(q, clickFn) {
  const cls = changeClass(q.change_pct);
  const card = document.createElement("div");
  card.className = "quote-card";
  card.dataset.symbol = q.symbol;
  card.innerHTML = `
    <div class="sym">${q.symbol}</div>
    <div class="name" title="${q.name || q.symbol}">${q.name || q.symbol}</div>
    <div class="price">${Fmt.price(q.price)}</div>
    <div class="change ${cls}">${Fmt.pct(q.change_pct)} ${q.change >= 0 ? "▲" : "▼"} ${Fmt.price(Math.abs(q.change))}</div>
  `;
  if (typeof clickFn === "function") card.addEventListener("click", () => clickFn(q.symbol));
  return card;
}

function renderTopbarTicker(quotes) {
  topbarTicker.innerHTML = quotes.map(q => {
    const cls = changeClass(q.change_pct);
    return `
      <div class="ticker-item">
        <span class="sym">${q.symbol}</span>
        <span class="price">${Fmt.price(q.price)}</span>
        <span class="chg ${cls}">${Fmt.pct(q.change_pct)}</span>
      </div>
    `;
  }).join("");
}

function renderMoverRow(q, rank) {
  const cls = changeClass(q.change_pct);
  return `
    <tr data-symbol="${q.symbol}" onclick="window.openChart && window.openChart('${q.symbol}')">
      <td class="text-dim font-mono fs-11">${rank}</td>
      <td>
        <div class="font-mono fw-700 fs-13" style="color:var(--accent)">${q.symbol}</div>
        <div class="text-dim fs-11">${(q.name || "").substring(0, 22)}</div>
      </td>
      <td class="mono">${Fmt.price(q.price)}</td>
      <td class="mono ${cls} fw-700">${Fmt.pct(q.change_pct)}</td>
      <td class="text-muted fs-11">${Fmt.bigNum(q.market_cap)}</td>
    </tr>
  `;
}

function renderWhyPanel(corr) {
  const confPct = Math.round((corr.confidence || 0) * 100);
  const cls = changeClass(corr.market_change_pct);
  whyPanel.innerHTML = `
    <div class="flex items-center justify-between" style="margin-bottom:8px">
      <div class="driver-label">${corr.driver_label}</div>
      <div class="mono fw-700 fs-13 ${cls}">${Fmt.pct(corr.market_change_pct)} SPY</div>
    </div>
    <div class="driver-summary">${corr.summary}</div>
    <div class="driver-confidence mt-8">
      <div class="conf-bar">
        <div class="conf-bar-fill" style="width:${confPct}%"></div>
      </div>
      <div class="conf-text">${confPct}% confidence</div>
    </div>
  `;
}

function renderNewsItem(n) {
  const sentBadge = sentimentBadge(n.sentiment_score || 0);
  return `
    <div class="news-item" onclick="${n.url ? `window.open('${n.url}','_blank')` : ""}">
      <div class="news-meta">
        <span class="news-source">${n.source || "Market News"}</span>
        <span class="news-time">${Fmt.timeAgo(n.timestamp)}</span>
        ${sentBadge}
        ${n.driver_category !== "other" ? `<span class="badge badge-blue">${n.driver_category.replace("_"," ")}</span>` : ""}
      </div>
      <div class="news-headline">${n.headline}</div>
      ${n.summary ? `<div class="news-summary">${n.summary.substring(0, 120)}${n.summary.length > 120 ? "…" : ""}</div>` : ""}
    </div>
  `;
}

// ── Live quote update from WebSocket ─────────────────────────────────────────

function applyLiveUpdate(msg) {
  if (!msg.quotes) return;
  msg.quotes.forEach(q => {
    _liveQuotes[q.symbol] = q;
    // Update existing quote cards in DOM
    const card = document.querySelector(`.quote-card[data-symbol="${q.symbol}"]`);
    if (card) {
      const cls = changeClass(q.change_pct);
      const priceEl = card.querySelector(".price");
      const changeEl = card.querySelector(".change");
      if (priceEl) priceEl.textContent = Fmt.price(q.price);
      if (changeEl) {
        changeEl.className = `change ${cls}`;
        changeEl.textContent = `${Fmt.pct(q.change_pct)} ${q.change >= 0 ? "▲" : "▼"} ${Fmt.price(Math.abs(q.change))}`;
      }
    }
    // Update topbar ticker
    const tickerItems = topbarTicker.querySelectorAll(".ticker-item");
    tickerItems.forEach(item => {
      const symEl = item.querySelector(".sym");
      if (symEl && symEl.textContent === q.symbol) {
        const priceEl = item.querySelector(".price");
        const chgEl = item.querySelector(".chg");
        if (priceEl) priceEl.textContent = Fmt.price(q.price);
        if (chgEl) {
          const cls = changeClass(q.change_pct);
          chgEl.className = `chg ${cls}`;
          chgEl.textContent = Fmt.pct(q.change_pct);
        }
      }
    });
  });
}

// ── Main load ─────────────────────────────────────────────────────────────────

export async function loadDashboard() {
  // Clear and show skeletons
  indicesGrid.innerHTML = `<div class="loading-text">Loading indices…</div>`;
  sectorsGrid.innerHTML = `<div class="loading-text">Loading sectors…</div>`;
  gainersBody.innerHTML = `<tr><td colspan="5" class="loading-text">Scanning…</td></tr>`;
  losersBody.innerHTML  = `<tr><td colspan="5" class="loading-text">Scanning…</td></tr>`;
  newsContainer.innerHTML = `<div class="loading-text">Fetching news…</div>`;
  whyPanel.innerHTML = `<div class="loading-text">Analyzing market drivers…</div>`;

  // Fetch all in parallel
  const [overviewRes, corrRes, newsRes] = await Promise.all([
    API.marketOverview(),
    API.marketCorrelation(),
    API.marketNews(),
  ]);

  // ── Indices ───────────────────────────────────────────────────────────────
  if (overviewRes.data) {
    const { indices, sectors, top_gainers, top_losers } = overviewRes.data;

    // Topbar ticker (SPY, QQQ, IWM, DIA)
    renderTopbarTicker(indices);

    // Index cards
    indicesGrid.innerHTML = "";
    indices.forEach(q => {
      indicesGrid.appendChild(renderQuoteCard(q, sym => window.openChart && window.openChart(sym)));
    });

    // Sector cards
    sectorsGrid.innerHTML = "";
    sectors.forEach(q => {
      sectorsGrid.appendChild(renderQuoteCard(q, sym => window.openChart && window.openChart(sym)));
    });

    // Gainers
    gainersBody.innerHTML = top_gainers.map((q, i) => renderMoverRow(q, i + 1)).join("");

    // Losers
    losersBody.innerHTML = top_losers.map((q, i) => renderMoverRow(q, i + 1)).join("");
  } else {
    indicesGrid.innerHTML = `<div class="text-dim fs-12">Failed to load: ${overviewRes.error}</div>`;
  }

  // ── Why moving ─────────────────────────────────────────────────────────────
  if (corrRes.data) {
    renderWhyPanel(corrRes.data);
  } else {
    whyPanel.innerHTML = `<div class="text-dim fs-12">Analysis unavailable</div>`;
  }

  // ── News ──────────────────────────────────────────────────────────────────
  if (newsRes.data && newsRes.data.length) {
    newsContainer.innerHTML = newsRes.data.slice(0, 12).map(renderNewsItem).join("");
  } else {
    newsContainer.innerHTML = `<div class="loading-text">No news available</div>`;
  }
}

// ── WebSocket setup ───────────────────────────────────────────────────────────

export function initWebSocket() {
  const stream = createPriceStream(
    (msg) => {
      if (msg.type === "quote_update" || msg.type === "quote_snapshot") {
        applyLiveUpdate(msg);
      }
    },
    (ws) => {
      wsStatus.className = "connected";
      wsStatus.title = "Live prices connected";
    },
    () => {
      wsStatus.className = "error";
    }
  );
  return stream;
}
