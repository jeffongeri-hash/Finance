/**
 * Multi-Timeframe Technical Analysis View
 * =========================================
 * Displays three stacked timeframes (1Y Daily, 3M Hourly, 24H Minute) with:
 *   • Candlestick chart (TradingView Lightweight Charts)
 *   • Moving average overlays (SMA20/50/200, EMA9/21, VWAP)
 *   • Volume bars with up/down colouring
 *   • Support & resistance horizontal lines
 *   • IV-based trade setup panel (entry, stop, target, R:R)
 */

import { API, Fmt } from "/static/js/api.js";

// ── API methods ──────────────────────────────────────────────────────────────

Object.assign(API, {
  analysisFull:   (ticker, side = "long") =>
    API._fetch(`/api/analysis/full/${encodeURIComponent(ticker)}`, { side }),
  analysisMTF:    (ticker) =>
    API._fetch(`/api/analysis/technical/${encodeURIComponent(ticker)}`),
  analysisVol:    (ticker) =>
    API._fetch(`/api/analysis/volatility/${encodeURIComponent(ticker)}`),
  analysisSetup:  (ticker, side, entry = 0) =>
    API._fetch(`/api/analysis/setup/${encodeURIComponent(ticker)}`, { side, entry }),
});

// ── Chart instances (one per timeframe) ──────────────────────────────────────

const _charts = {};       // { daily, hourly, minute }
const _series = {};       // { daily: { candle, volume, ma: {} }, hourly: {…}, minute: {…} }
const _lineHandles = {};  // S/R horizontal lines per chart

const TF_ORDER = ["daily", "hourly", "minute"];
const TF_LABEL = { daily: "1Y Daily", hourly: "3M Hourly", minute: "24H Minute" };

// ── Colour palette ────────────────────────────────────────────────────────────

const COLORS = {
  up:     "rgba(34,197,94,0.9)",
  down:   "rgba(239,68,68,0.9)",
  volUp:  "rgba(34,197,94,0.4)",
  volDn:  "rgba(239,68,68,0.4)",
  ma: {
    ema9:   "#f59e0b",   // amber
    ema21:  "#60a5fa",   // blue
    sma20:  "#818cf8",   // indigo
    sma50:  "#34d399",   // green
    sma200: "#f87171",   // red
    vwap:   "#e879f9",   // purple
  },
};

const MA_LABELS = {
  ema9: "EMA9", ema21: "EMA21",
  sma20: "SMA20", sma50: "SMA50",
  sma200: "SMA200", vwap: "VWAP",
};

// ── Chart factory ─────────────────────────────────────────────────────────────

function _makeChart(containerId, height = 300) {
  const el = document.getElementById(containerId);
  if (!el || !window.LightweightCharts) return null;
  el.innerHTML = "";

  const chart = LightweightCharts.createChart(el, {
    width:  el.clientWidth || 700,
    height,
    layout: {
      background: { color: "transparent" },
      textColor:  "#9ca3af",
    },
    grid: {
      vertLines:   { color: "rgba(255,255,255,0.05)" },
      horzLines:   { color: "rgba(255,255,255,0.05)" },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.1)" },
    timeScale:       { borderColor: "rgba(255,255,255,0.1)", timeVisible: true },
    handleScroll:    true,
    handleScale:     true,
  });

  // Responsive resize
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: el.clientWidth });
  });
  ro.observe(el);

  return chart;
}

// ── Render one timeframe ──────────────────────────────────────────────────────

function _renderTimeframe(tf, data) {
  const chartId = `ta-chart-${tf}`;
  const volId   = `ta-vol-${tf}`;

  if (!data || data.error) {
    const el = document.getElementById(chartId);
    if (el) el.innerHTML = `<div class="loading-text" style="padding:40px">No ${TF_LABEL[tf]} data available</div>`;
    return;
  }

  const candles = data.candles || [];
  const overlay = data.ma_overlay || {};
  const volume  = data.volume || [];

  if (candles.length === 0) return;

  // Destroy old chart if exists
  if (_charts[tf]) {
    try { _charts[tf].remove(); } catch (_) {}
  }

  // Main price chart
  const chart = _makeChart(chartId, tf === "daily" ? 340 : 280);
  if (!chart) return;
  _charts[tf] = chart;
  _series[tf] = { ma: {} };

  // Candlesticks
  const candleSeries = chart.addCandlestickSeries({
    upColor:         COLORS.up,
    downColor:       COLORS.down,
    borderUpColor:   COLORS.up,
    borderDownColor: COLORS.down,
    wickUpColor:     COLORS.up,
    wickDownColor:   COLORS.down,
  });
  candleSeries.setData(candles);
  _series[tf].candle = candleSeries;

  // MA overlays
  Object.entries(overlay).forEach(([key, pts]) => {
    if (!pts || !pts.length) return;
    const color = COLORS.ma[key] || "#fff";
    const width  = key === "sma200" ? 2 : 1;
    const lineSeries = chart.addLineSeries({
      color, lineWidth: width, lastValueVisible: true,
      priceLineVisible: false,
      title: MA_LABELS[key] || key,
    });
    lineSeries.setData(pts);
    _series[tf].ma[key] = lineSeries;
  });

  // Support / Resistance lines
  if (_lineHandles[tf]) _lineHandles[tf].forEach(h => { try { h.remove(); } catch (_) {} });
  _lineHandles[tf] = [];

  const price = data.price || 0;
  (data.resistance || []).forEach(r => {
    const line = candleSeries.createPriceLine({
      price: r, color: "rgba(239,68,68,0.6)",
      lineWidth: 1, lineStyle: 2, /* dashed */
      axisLabelVisible: true,
      title: `R ${r < price * 1.02 ? "↑" : ""}`,
    });
    _lineHandles[tf].push(line);
  });
  (data.support || []).forEach(s => {
    const line = candleSeries.createPriceLine({
      price: s, color: "rgba(34,197,94,0.6)",
      lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true,
      title: `S ${s > price * 0.98 ? "↓" : ""}`,
    });
    _lineHandles[tf].push(line);
  });

  // Pivot points (daily only)
  if (tf === "daily") {
    const pivots = data.pivots || {};
    if (pivots.P) {
      const pvtLine = candleSeries.createPriceLine({
        price: pivots.P, color: "rgba(232,121,249,0.6)",
        lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: "Pivot",
      });
      _lineHandles[tf].push(pvtLine);
    }
  }

  // Volume pane
  const volEl = document.getElementById(volId);
  if (volEl && volume.length) {
    if (_charts[`${tf}_vol`]) {
      try { _charts[`${tf}_vol`].remove(); } catch (_) {}
    }
    const volChart = _makeChart(volId, 80);
    if (volChart) {
      _charts[`${tf}_vol`] = volChart;
      const volSeries = volChart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "",
      });
      volSeries.setData(volume);
      volChart.priceScale("").applyOptions({ scaleMargins: { top: 0.1, bottom: 0 } });
      // Sync crosshair / timeScale with main chart
      chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
        if (range) volChart.timeScale().setVisibleLogicalRange(range);
      });
      _series[tf].volume = volSeries;
    }
  }
}

// ── Trade setup panel ─────────────────────────────────────────────────────────

function _directionBadge(direction) {
  const map = {
    STRONG_LONG:  ["STRONG LONG",  "var(--green)", "rgba(34,197,94,0.15)"],
    LONG:         ["LONG",         "#6ee7b7",      "rgba(34,197,94,0.10)"],
    NEUTRAL:      ["NEUTRAL",      "var(--text-muted)", "var(--bg-elevated)"],
    SHORT:        ["SHORT",        "#f87171",      "rgba(239,68,68,0.10)"],
    STRONG_SHORT: ["STRONG SHORT", "var(--red)",   "rgba(239,68,68,0.15)"],
  };
  const [label, color, bg] = map[direction] || ["UNKNOWN", "var(--text-muted)", "transparent"];
  return `<span style="background:${bg};color:${color};border:1px solid ${color}40;
    padding:4px 10px;border-radius:4px;font-weight:700;font-size:12px;letter-spacing:.5px">${label}</span>`;
}

function _ivpBar(ivp) {
  const pct = Math.min(100, Math.max(0, ivp));
  const col = pct < 25 ? "var(--green)" : pct < 75 ? "var(--amber)" : "var(--red)";
  return `
    <div style="display:flex;align-items:center;gap:8px;margin-top:2px">
      <div style="flex:1;height:5px;background:var(--bg-elevated);border-radius:3px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:${col};border-radius:3px"></div>
      </div>
      <span class="mono" style="color:${col};font-size:11px;min-width:38px">${pct.toFixed(0)}th</span>
    </div>`;
}

function _rrColor(rr) {
  if (rr >= 2.5) return "var(--green)";
  if (rr >= 2.0) return "#6ee7b7";
  if (rr >= 1.5) return "var(--amber)";
  return "var(--red)";
}

function renderSetupPanel(data) {
  const el = document.getElementById("ta-setup-panel");
  if (!el) return;

  const mtf   = data.mtf || {};
  const vol   = data.vol || {};
  const setup = data.setup || {};

  if (!mtf.price && !vol.price) {
    el.innerHTML = `<div class="loading-text">Awaiting analysis…</div>`;
    return;
  }

  const price   = mtf.price || vol.price || 0;
  const dir     = mtf.direction || "NEUTRAL";
  const score   = mtf.confluence_score || 0;
  const ivpct   = vol.iv_annual || 0;
  const ivp     = vol.iv_percentile || 50;
  const atr     = vol.atr_14 || 0;
  const dailyEM = vol.daily_em || 0;
  const weekEM  = vol.weekly_em || 0;
  const hv30    = vol.hv_30 || 0;
  const ivSrc   = vol.iv_source || "historical";

  // Setup values
  const entry   = setup.entry   || price;
  const stop    = setup.stop    || 0;
  const target  = setup.target  || 0;
  const rr      = setup.rr_ratio || 0;
  const stopPct = setup.stop_pct || 0;
  const tgtPct  = setup.target_pct || 0;
  const side    = data.side || "long";
  const isLong  = side === "long";

  const stopColor   = "var(--red)";
  const targetColor = "var(--green)";
  const rrColor_    = _rrColor(rr);

  // Score bar
  const scoreColor = score >= 2 ? "var(--green)" : score <= -2 ? "var(--red)" : "var(--amber)";
  const scoreBar = `
    <div style="display:flex;align-items:center;gap:8px;margin-top:4px">
      <div style="flex:1;height:5px;background:var(--bg-elevated);border-radius:3px;position:relative;overflow:hidden">
        <div style="position:absolute;left:50%;top:0;height:100%;background:var(--border);width:1px"></div>
        <div style="position:absolute;${score >= 0 ? "left:50%" : `left:${(50 + score * 8)}%`};top:0;
          height:100%;width:${Math.abs(score) * 8}%;background:${scoreColor};border-radius:3px"></div>
      </div>
      <span class="mono" style="color:${scoreColor};font-size:11px;min-width:28px">${score >= 0 ? "+" : ""}${score}</span>
    </div>`;

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start">

      <!-- Left: Direction + Confluence -->
      <div>
        <div style="margin-bottom:12px">
          ${_directionBadge(dir)}
          <div style="font-size:11px;color:var(--text-dim);margin-top:6px">MTF Confluence Score (−6 to +6)</div>
          ${scoreBar}
        </div>

        <!-- IV Profile -->
        <div class="ta-stat-grid">
          <div class="ta-stat">
            <div class="ta-stat-val mono" style="color:var(--amber)">${ivpct.toFixed(1)}%</div>
            <div class="ta-stat-label">IV Annual<br><span style="color:var(--text-dim);font-size:9px">${ivSrc}</span></div>
          </div>
          <div class="ta-stat">
            <div class="ta-stat-val mono">${hv30.toFixed(1)}%</div>
            <div class="ta-stat-label">HV 30d</div>
          </div>
          <div class="ta-stat">
            <div class="ta-stat-val mono">$${Fmt.num(atr, 2)}</div>
            <div class="ta-stat-label">ATR 14</div>
          </div>
          <div class="ta-stat">
            <div class="ta-stat-val mono">$${Fmt.num(dailyEM, 2)}</div>
            <div class="ta-stat-label">Daily EM</div>
          </div>
          <div class="ta-stat">
            <div class="ta-stat-val mono">$${Fmt.num(weekEM, 2)}</div>
            <div class="ta-stat-label">Weekly EM</div>
          </div>
          <div class="ta-stat">
            <div class="ta-stat-val mono">${vol.atr_pct ? vol.atr_pct.toFixed(2) + "%" : "—"}</div>
            <div class="ta-stat-label">ATR %</div>
          </div>
        </div>

        <!-- IV Percentile bar -->
        <div style="margin-top:12px">
          <div style="font-size:11px;color:var(--text-secondary);margin-bottom:2px">IV Percentile (rank vs 52-wk)</div>
          ${_ivpBar(ivp)}
          <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-dim);margin-top:2px">
            <span>Low IV → wide R:R</span><span>High IV → tight R:R</span>
          </div>
        </div>
      </div>

      <!-- Right: Trade Setup -->
      <div>
        <div style="font-size:11px;color:var(--text-secondary);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px">
          ${isLong ? "⬆ Long Setup" : "⬇ Short Setup"} — $${Fmt.num(price, 4)}
        </div>

        <!-- Price levels -->
        <div style="display:grid;gap:6px;margin-bottom:12px">
          <!-- Target -->
          <div style="display:flex;justify-content:space-between;align-items:center;
            background:rgba(34,197,94,0.07);border:1px solid rgba(34,197,94,0.25);
            border-radius:6px;padding:8px 12px">
            <div>
              <div style="font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px">Target</div>
              <div class="mono" style="font-size:16px;color:${targetColor};font-weight:700">$${Fmt.num(target, 2)}</div>
            </div>
            <div style="text-align:right">
              <div class="mono" style="color:${targetColor};font-size:13px">${isLong ? "+" : "−"}${tgtPct.toFixed(2)}%</div>
              <div style="font-size:10px;color:var(--text-dim)">$${Fmt.num(setup.target_distance || 0, 2)} move</div>
            </div>
          </div>

          <!-- Entry -->
          <div style="display:flex;justify-content:space-between;align-items:center;
            background:var(--bg-elevated);border:1px solid var(--border);
            border-radius:6px;padding:8px 12px">
            <div>
              <div style="font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px">Entry</div>
              <div class="mono" style="font-size:16px;color:var(--text-primary);font-weight:700">$${Fmt.num(entry, 2)}</div>
            </div>
            <div style="text-align:right">
              <div class="mono" style="color:${rrColor_};font-size:14px;font-weight:700">${rr.toFixed(2)}:1 R/R</div>
              <div style="font-size:10px;color:var(--text-dim)">R:R ratio</div>
            </div>
          </div>

          <!-- Stop -->
          <div style="display:flex;justify-content:space-between;align-items:center;
            background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.25);
            border-radius:6px;padding:8px 12px">
            <div>
              <div style="font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px">Stop Loss</div>
              <div class="mono" style="font-size:16px;color:${stopColor};font-weight:700">$${Fmt.num(stop, 2)}</div>
            </div>
            <div style="text-align:right">
              <div class="mono" style="color:${stopColor};font-size:13px">−${stopPct.toFixed(2)}%</div>
              <div style="font-size:10px;color:var(--text-dim)">$${Fmt.num(setup.stop_distance || 0, 2)} risk</div>
            </div>
          </div>
        </div>

        <!-- Rationale -->
        ${setup.rationale ? `
        <div style="font-size:10px;color:var(--text-dim);line-height:1.6;
          background:var(--bg-elevated);border-radius:4px;padding:8px;
          font-family:var(--font-mono)">
          ${setup.rationale}
        </div>` : ""}

        <!-- Signal strength -->
        ${setup.signal_strength ? `
        <div style="margin-top:8px;font-size:11px;color:var(--text-secondary)">
          Signal: <strong style="color:${
            setup.signal_strength === "strong"   ? "var(--green)" :
            setup.signal_strength === "moderate" ? "var(--amber)" : "var(--text-muted)"
          }">${setup.signal_strength.toUpperCase()}</strong>
        </div>` : ""}
      </div>
    </div>`;
}

// ── Indicators summary strip ──────────────────────────────────────────────────

function _trendDot(trend) {
  const map = {
    bullish: "var(--green)", bearish: "var(--red)",
    rising:  "var(--green)", falling: "var(--red)",
    surging: "var(--green)", drying_up: "var(--amber)",
    flat:    "var(--text-dim)", mixed: "var(--amber)",
  };
  const color = map[trend] || "var(--text-dim)";
  return `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${color};margin-right:4px;vertical-align:middle"></span>`;
}

function renderIndicatorStrip(tf, data) {
  const el = document.getElementById(`ta-indicators-${tf}`);
  if (!el || !data) return;

  const pnl = (v, ref) => v > ref
    ? `<span style="color:var(--green)">↑</span>`
    : `<span style="color:var(--red)">↓</span>`;

  const price = data.price || 0;
  const mk    = (label, value, color = "var(--text-primary)") =>
    `<div class="ta-ind-item">
      <span class="ta-ind-label">${label}</span>
      <span class="ta-ind-val mono" style="color:${color}">${value}</span>
    </div>`;

  const green = "var(--green)", red = "var(--red)", amber = "var(--amber)";
  const trendColor = t => t === "bullish" || t === "rising" || t === "surging" ? green :
                          t === "bearish" || t === "falling" ? red : amber;

  const items = [
    mk("EMA9",    data.ema9   ? `$${Fmt.num(data.ema9, 2)} ${pnl(price, data.ema9)}`   : "—"),
    mk("EMA21",   data.ema21  ? `$${Fmt.num(data.ema21, 2)} ${pnl(price, data.ema21)}` : "—"),
    mk("SMA20",   data.sma20  ? `$${Fmt.num(data.sma20, 2)} ${pnl(price, data.sma20)}` : "—"),
    mk("SMA50",   data.sma50  ? `$${Fmt.num(data.sma50, 2)} ${pnl(price, data.sma50)}` : "—"),
    mk("SMA200",  data.sma200 ? `$${Fmt.num(data.sma200, 2)} ${pnl(price, data.sma200)}` : "—"),
    data.vwap ? mk("VWAP", `$${Fmt.num(data.vwap, 2)} ${pnl(price, data.vwap)}`) : "",
    mk("MA Trend", `${_trendDot(data.ma_trend)}${data.ma_trend || "—"}`, trendColor(data.ma_trend)),
    mk("OBV",      `${_trendDot(data.obv_trend)}${data.obv_trend || "—"}`, trendColor(data.obv_trend)),
    mk("Vol",      `${_trendDot(data.vol_trend)}${data.vol_trend || "—"} (${(data.rvol||1).toFixed(1)}×)`, trendColor(data.vol_trend)),
    mk("ATR14",    `$${Fmt.num(data.atr14, 2)} (${(data.atr_pct||0).toFixed(2)}%)`),
    mk("Nearest R",`$${Fmt.num(data.nearest_r, 2)} (+${(data.dist_to_r||0).toFixed(1)}%)`, red),
    mk("Nearest S",`$${Fmt.num(data.nearest_s, 2)} (−${(data.dist_to_s||0).toFixed(1)}%)`, green),
    mk("Score",    `${data.score >= 0 ? "+" : ""}${data.score || 0}`, data.score >= 1 ? green : data.score <= -1 ? red : amber),
  ];

  el.innerHTML = items.join("");
}

// ── Main render ───────────────────────────────────────────────────────────────

let _currentTicker  = "";
let _currentSide    = "long";
let _analysisData   = null;

async function runAnalysis(ticker, side = "long") {
  if (!ticker) return;
  _currentTicker = ticker.toUpperCase();
  _currentSide   = side;

  // Show loading state
  ["daily", "hourly", "minute"].forEach(tf => {
    const el = document.getElementById(`ta-chart-${tf}`);
    if (el) el.innerHTML = `<div class="loading-text" style="padding:60px;text-align:center">
      Loading ${TF_LABEL[tf]}…</div>`;
  });
  const panelEl = document.getElementById("ta-setup-panel");
  if (panelEl) panelEl.innerHTML = `<div class="loading-text">Fetching IV + options data…</div>`;

  // Update ticker display
  const titleEl = document.getElementById("ta-ticker-title");
  if (titleEl) titleEl.textContent = _currentTicker;

  const { data, error } = await API.analysisFull(_currentTicker, side);
  if (error || !data) {
    const errMsg = error || "Analysis failed";
    if (panelEl) panelEl.innerHTML = `<div style="color:var(--red);padding:12px">${errMsg}</div>`;
    return;
  }

  _analysisData = data;

  // Render each timeframe
  const timeframes = data.mtf?.timeframes || {};
  TF_ORDER.forEach(tf => {
    const tfData = timeframes[tf];
    _renderTimeframe(tf, tfData);
    if (tfData) renderIndicatorStrip(tf, tfData);
  });

  // Render trade setup
  renderSetupPanel(data);

  // Update side toggle buttons
  document.querySelectorAll(".ta-side-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.side === side);
  });
}

// ── Wire up buttons ───────────────────────────────────────────────────────────

export function initTechnicalView() {
  const input    = document.getElementById("ta-ticker-input");
  const runBtn   = document.getElementById("ta-run-btn");

  function _run() {
    const ticker = (input?.value || "").trim().toUpperCase();
    if (!ticker) return;
    runAnalysis(ticker, _currentSide);
  }

  if (runBtn)  runBtn.addEventListener("click", _run);
  if (input) {
    input.addEventListener("keydown", e => { if (e.key === "Enter") _run(); });
  }

  // Side toggle (Long / Short)
  document.querySelectorAll(".ta-side-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      _currentSide = btn.dataset.side;
      document.querySelectorAll(".ta-side-btn").forEach(b =>
        b.classList.toggle("active", b.dataset.side === _currentSide)
      );
      if (_currentTicker) runAnalysis(_currentTicker, _currentSide);
    });
  });

  // Quick-pick buttons (pre-built tickers)
  document.querySelectorAll("[data-ta-ticker]").forEach(el => {
    el.addEventListener("click", () => {
      const t = el.dataset.taTicker;
      if (input) input.value = t;
      runAnalysis(t, _currentSide);
    });
  });

  // Run default on load if input has value
  const defaultTicker = input?.value?.trim();
  if (defaultTicker) runAnalysis(defaultTicker, "long");
}

export async function loadTechnical(ticker = "") {
  if (ticker) {
    const input = document.getElementById("ta-ticker-input");
    if (input) input.value = ticker;
    await runAnalysis(ticker, "long");
  }
}
