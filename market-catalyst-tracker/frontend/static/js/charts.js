/**
 * charts.js — TradingView Lightweight Charts integration
 * Renders candlestick + volume charts with news event markers.
 */

import { API, Fmt, changeClass, sentimentBadge } from "./api.js";

// LightweightCharts is loaded globally via CDN (standalone build)
const LC = window.LightweightCharts;

let _chart = null;
let _candleSeries = null;
let _volumeSeries = null;
let _currentSymbol = null;
let _currentPeriod = "3mo";

// ── Chart creation ────────────────────────────────────────────────────────────

function createChart() {
  const container = document.getElementById("tv-chart");
  if (!container || !LC) return null;

  container.innerHTML = ""; // clear previous

  const chart = LC.createChart(container, {
    layout: {
      background: { color: "#111620" },
      textColor: "#64748b",
    },
    grid: {
      vertLines: { color: "#1f2d45" },
      horzLines: { color: "#1f2d45" },
    },
    crosshair: {
      vertLine: { color: "#3b82f6", width: 1, style: 2 },
      horzLine: { color: "#3b82f6", width: 1, style: 2 },
    },
    rightPriceScale: {
      borderColor: "#1f2d45",
    },
    timeScale: {
      borderColor: "#1f2d45",
      timeVisible: true,
      secondsVisible: false,
    },
    width:  container.clientWidth,
    height: 420,
  });

  // Resize observer
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);

  return chart;
}

function addSeries(chart) {
  // Candlestick series
  const candle = chart.addSeries(LC.CandlestickSeries, {
    upColor:          "#22c55e",
    downColor:        "#ef4444",
    borderUpColor:    "#22c55e",
    borderDownColor:  "#ef4444",
    wickUpColor:      "#22c55e",
    wickDownColor:    "#ef4444",
  });

  // Volume histogram (secondary)
  const vol = chart.addSeries(LC.HistogramSeries, {
    priceFormat:   { type: "volume" },
    priceScaleId:  "volume",
    color:         "#3b82f633",
  });

  chart.priceScale("volume").applyOptions({
    scaleMargins: { top: 0.8, bottom: 0 },
    borderVisible: false,
  });

  return { candle, vol };
}

// ── Markers ───────────────────────────────────────────────────────────────────

function buildNewsMarkers(newsItems, candleData) {
  if (!newsItems || !candleData.length) return [];

  const candleTimestamps = new Set(candleData.map(c => c.time));
  const markers = [];

  newsItems.forEach(n => {
    // Snap to nearest candle timestamp
    let ts = n.timestamp;
    const dayTs = Math.floor(ts / 86400) * 86400;

    // Find the closest candle
    let closest = null;
    let minDiff = Infinity;
    for (const ct of candleTimestamps) {
      const diff = Math.abs(ct - dayTs);
      if (diff < minDiff) { minDiff = diff; closest = ct; }
    }
    if (!closest || minDiff > 7 * 86400) return;  // > 7 days away — skip

    const isBullish = (n.sentiment_score || 0) > 0.1;
    const isBearish = (n.sentiment_score || 0) < -0.1;

    markers.push({
      time: closest,
      position: isBearish ? "aboveBar" : "belowBar",
      color:    isBullish ? "#22c55e" : isBearish ? "#ef4444" : "#3b82f6",
      shape:    "circle",
      text:     n.source ? `${n.source}: ${n.headline.substring(0, 40)}…` : n.headline.substring(0, 50),
      size:     1,
    });
  });

  // Deduplicate by time (keep first per timestamp)
  const seen = new Set();
  return markers.filter(m => {
    if (seen.has(m.time)) return false;
    seen.add(m.time);
    return true;
  });
}

// ── Period buttons ────────────────────────────────────────────────────────────

const PERIODS = [
  { label: "1D",  period: "1d",  interval: "5m"  },
  { label: "5D",  period: "5d",  interval: "15m" },
  { label: "1M",  period: "1mo", interval: "1d"  },
  { label: "3M",  period: "3mo", interval: "1d"  },
  { label: "6M",  period: "6mo", interval: "1d"  },
  { label: "1Y",  period: "1y",  interval: "1wk" },
  { label: "2Y",  period: "2y",  interval: "1wk" },
];

function renderPeriodButtons(onSelect) {
  const container = document.getElementById("period-buttons");
  if (!container) return;
  container.innerHTML = PERIODS.map(p => `
    <button class="period-btn${p.period === _currentPeriod ? " active" : ""}" data-period="${p.period}" data-interval="${p.interval}">
      ${p.label}
    </button>
  `).join("");

  container.querySelectorAll(".period-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      _currentPeriod = btn.dataset.period;
      container.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      if (typeof onSelect === "function") onSelect(btn.dataset.period, btn.dataset.interval);
    });
  });
}

// ── Main load ─────────────────────────────────────────────────────────────────

async function loadChart(symbol, period = _currentPeriod, interval = "1d") {
  if (!LC) {
    document.getElementById("tv-chart").innerHTML = `
      <div class="loading-text" style="padding:40px">
        TradingView Lightweight Charts failed to load. Check your connection.
      </div>`;
    return;
  }

  _currentSymbol = symbol.toUpperCase();
  _currentPeriod = period;

  // Update symbol label
  const symLabel = document.getElementById("chart-symbol-label");
  if (symLabel) symLabel.textContent = _currentSymbol;

  // Show chart loading state
  document.getElementById("tv-chart").innerHTML = `<div class="loading-text" style="padding:60px">Loading chart…</div>`;

  // Fetch quote + chart data + news in parallel
  const [quoteRes, chartRes, newsRes] = await Promise.all([
    API.quote(_currentSymbol),
    API.chartData(_currentSymbol, period, interval),
    API.symbolNews(_currentSymbol),
  ]);

  if (!chartRes.data || chartRes.error) {
    document.getElementById("tv-chart").innerHTML = `
      <div class="loading-text" style="padding:60px">
        No chart data for ${_currentSymbol}${chartRes.error ? `: ${chartRes.error}` : ""}
      </div>`;
    return;
  }

  // ── Render quote stats ────────────────────────────────────────────────────
  const q = quoteRes.data;
  if (q) {
    const cls = changeClass(q.change_pct);
    const statsEl = document.getElementById("chart-stats");
    if (statsEl) {
      statsEl.innerHTML = `
        <span class="mono fw-700 fs-13" style="color:var(--text-primary)">${Fmt.price(q.price)}</span>
        <span class="mono fw-700 fs-12 ${cls}">${Fmt.pct(q.change_pct)}</span>
        <span class="text-muted fs-11">Mkt Cap: ${Fmt.bigNum(q.market_cap)}</span>
        <span class="text-muted fs-11">Avg Vol: ${q.avg_volume ? (q.avg_volume / 1e6).toFixed(1) + "M" : "—"}</span>
        ${q.sector ? `<span class="badge badge-blue">${q.sector}</span>` : ""}
      `;
    }
  }

  // ── Build chart ───────────────────────────────────────────────────────────
  _chart = createChart();
  if (!_chart) return;

  const { candle, vol } = addSeries(_chart);
  _candleSeries = candle;
  _volumeSeries = vol;

  const candles = chartRes.data.candles;
  candle.setData(candles.map(c => ({
    time:  c.time,
    open:  c.open,
    high:  c.high,
    low:   c.low,
    close: c.close,
  })));

  vol.setData(candles.map(c => ({
    time:  c.time,
    value: c.volume,
    color: c.close >= c.open ? "#22c55e22" : "#ef444422",
  })));

  // ── News markers on chart ─────────────────────────────────────────────────
  const newsItems = newsRes.data?.news || [];
  const markers = buildNewsMarkers(newsItems, candles);
  if (markers.length) {
    candle.setMarkers(markers);
  }

  _chart.timeScale().fitContent();

  // ── News panel ────────────────────────────────────────────────────────────
  const newsPanel = document.getElementById("chart-news");
  if (newsPanel && newsItems.length) {
    newsPanel.innerHTML = newsItems.slice(0, 8).map(n => `
      <div class="news-item" onclick="${n.url ? `window.open('${n.url}','_blank')` : ""}">
        <div class="news-meta">
          <span class="news-source">${n.source || "News"}</span>
          <span class="news-time">${Fmt.timeAgo(n.timestamp)}</span>
          ${sentimentBadge(n.sentiment_score || 0)}
        </div>
        <div class="news-headline">${n.headline}</div>
      </div>
    `).join("");
  }

  // ── Symbol correlation (why is this moving) ───────────────────────────────
  const corrEl = document.getElementById("chart-why-panel");
  if (corrEl) {
    corrEl.innerHTML = `<div class="loading-text">Analyzing ${_currentSymbol}…</div>`;
    API.symbolCorrelation(_currentSymbol).then(({ data }) => {
      if (!data) return;
      const cls = changeClass(data.change_pct);
      corrEl.innerHTML = `
        <div class="driver-label">${data.driver_label}</div>
        <div class="driver-summary">${data.summary}</div>
        <div class="driver-confidence mt-8">
          <div class="conf-bar">
            <div class="conf-bar-fill" style="width:${Math.round(data.confidence * 100)}%"></div>
          </div>
          <div class="conf-text">${Math.round(data.confidence * 100)}% confidence</div>
        </div>
      `;
    });
  }
}

// ── Period button click → reload chart ────────────────────────────────────────

export function initChartView() {
  renderPeriodButtons((period, interval) => {
    if (_currentSymbol) loadChart(_currentSymbol, period, interval);
  });

  // Search within chart view
  const chartSearch = document.getElementById("chart-search-input");
  if (chartSearch) {
    chartSearch.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const val = chartSearch.value.trim().toUpperCase();
        if (val) loadChart(val);
      }
    });
  }
}

// ── Public API ────────────────────────────────────────────────────────────────

export function openChart(symbol) {
  // Switch to chart view
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  const chartNav = document.querySelector('[data-view="chart"]');
  if (chartNav) chartNav.classList.add("active");
  const chartView = document.getElementById("view-chart");
  if (chartView) chartView.classList.add("active");

  loadChart(symbol);
}

export { loadChart };
