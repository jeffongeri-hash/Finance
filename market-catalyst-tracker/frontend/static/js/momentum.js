/**
 * momentum.js — Exponential Move & Short Squeeze Scanner view
 * Displays high-RVOL, gap-up, breakout, and squeeze candidates
 * from the backend momentum scan.
 */

import { API, Fmt, changeClass, signalBadge, scoreColor } from "./api.js";

// ── DOM refs ──────────────────────────────────────────────────────────────────
const grid         = document.getElementById("momentum-grid");
const squeezeGrid  = document.getElementById("squeeze-grid");
const scanStats    = document.getElementById("momentum-stats");
const refreshBtn   = document.getElementById("momentum-refresh-btn");
const rvolFilter   = document.getElementById("rvol-filter");
const scoreFilter  = document.getElementById("score-filter");
const modeToggle   = document.getElementById("scan-mode-toggle");

let _currentMode = "momentum"; // "momentum" | "squeeze"
let _allCandidates = [];

// ── Render helpers ────────────────────────────────────────────────────────────

function renderMomentumCard(c) {
  const pctCls = changeClass(c.change_pct);
  const fill = Math.round(c.score);
  const color = scoreColor(c.score);

  return `
    <div class="momentum-card" data-symbol="${c.symbol}" onclick="window.openChart && window.openChart('${c.symbol}')">
      <div class="flex justify-between items-center">
        <div>
          <div class="mc-sym">${c.symbol}</div>
          <div class="mc-name" title="${c.company}">${c.company}</div>
        </div>
        <div style="text-align:right">
          <div class="mono fw-700 fs-13">${Fmt.price(c.price)}</div>
          <div class="mono fs-12 ${pctCls} fw-700">${Fmt.pct(c.change_pct)}</div>
        </div>
      </div>

      <div class="mc-stats">
        <div>
          <div class="mc-stat-label">Rel Volume</div>
          <div class="mc-stat-value" style="color:${c.relative_volume >= 3 ? 'var(--amber)' : 'var(--text-primary)'}">
            ${Fmt.rvol(c.relative_volume)}
          </div>
        </div>
        <div>
          <div class="mc-stat-label">Gap</div>
          <div class="mc-stat-value ${changeClass(c.gap_pct || 0)}">
            ${c.gap_pct != null ? Fmt.pct(c.gap_pct) : "—"}
          </div>
        </div>
        <div>
          <div class="mc-stat-label">Vs 52W High</div>
          <div class="mc-stat-value ${c.from_52w_high_pct >= 0 ? 'pos' : 'text-muted'}">
            ${Fmt.pct(c.from_52w_high_pct)}
          </div>
        </div>
        <div>
          <div class="mc-stat-label">Short Int</div>
          <div class="mc-stat-value ${c.short_interest_pct >= 20 ? 'neg' : 'text-muted'}">
            ${c.short_interest_pct != null ? c.short_interest_pct.toFixed(1) + "%" : "—"}
          </div>
        </div>
      </div>

      <div class="mc-signals">
        ${(c.signals || []).map(signalBadge).join("")}
      </div>

      <div class="score-bar" title="Score: ${fill}/100">
        <div class="score-bar-fill" style="width:${fill}%;background:${color}"></div>
      </div>
      <div class="flex justify-between mt-8">
        <span class="fs-10 text-dim">Score</span>
        <span class="mono fs-11 fw-700" style="color:${color}">${fill}</span>
      </div>
    </div>
  `;
}

function renderStats(result) {
  if (!scanStats) return;
  const { candidates, scanned } = result;
  const breakouts = candidates.filter(c => c.signals?.includes("BREAKOUT")).length;
  const squeezes  = candidates.filter(c => c.signals?.includes("SQUEEZE")).length;
  const gapUps    = candidates.filter(c => c.signals?.includes("GAP_UP")).length;
  scanStats.innerHTML = `
    <span class="text-dim fs-11">Scanned ${scanned} tickers</span>
    <span class="badge badge-green">${breakouts} Breakouts</span>
    <span class="badge badge-cyan">${gapUps} Gap Ups</span>
    <span class="badge badge-purple">${squeezes} Squeezes</span>
    <span class="badge badge-amber">${candidates.length} total candidates</span>
  `;
}

function applyFilters() {
  const minRvol  = parseFloat(rvolFilter?.value  || 1.5);
  const minScore = parseFloat(scoreFilter?.value || 20);
  const filtered = _allCandidates.filter(c =>
    c.relative_volume >= minRvol && c.score >= minScore
  );
  const targetGrid = _currentMode === "squeeze" ? squeezeGrid : grid;
  if (!targetGrid) return;
  if (!filtered.length) {
    targetGrid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;padding:40px">No candidates match current filters.</div>`;
    return;
  }
  targetGrid.innerHTML = filtered.map(renderMomentumCard).join("");
}

// ── Load ──────────────────────────────────────────────────────────────────────

async function loadMomentumScan() {
  if (grid) grid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;padding:60px">Running momentum scan…</div>`;
  _currentMode = "momentum";

  const minRvol  = parseFloat(rvolFilter?.value  || 1.5);
  const minScore = parseFloat(scoreFilter?.value || 20);

  const { data, error } = await API.momentumScan({ min_rvol: minRvol, min_score: minScore, top_n: 50 });

  if (error) {
    grid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;color:var(--red)">Error: ${error}</div>`;
    return;
  }

  _allCandidates = data.candidates || [];
  renderStats(data);
  applyFilters();
}

async function loadSqueezeScan() {
  if (squeezeGrid) squeezeGrid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;padding:60px">Scanning for short squeeze setups…</div>`;
  _currentMode = "squeeze";

  const { data, error } = await API.squeezeScan(30);

  if (error) {
    if (squeezeGrid) squeezeGrid.innerHTML = `<div class="loading-text" style="grid-column:1/-1;color:var(--red)">Error: ${error}</div>`;
    return;
  }

  _allCandidates = data.candidates || [];
  if (scanStats) {
    const { scanned } = data;
    scanStats.innerHTML = `
      <span class="text-dim fs-11">Scanned ${scanned} tickers for squeeze</span>
      <span class="badge badge-purple">${_allCandidates.length} squeeze candidates</span>
    `;
  }
  applyFilters();
}

// ── Exported init ─────────────────────────────────────────────────────────────

export function initMomentumView() {
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      if (_currentMode === "squeeze") loadSqueezeScan();
      else loadMomentumScan();
    });
  }

  if (rvolFilter)  rvolFilter.addEventListener("change",  applyFilters);
  if (scoreFilter) scoreFilter.addEventListener("change",  applyFilters);

  // Mode toggle tabs
  const modeBtns = document.querySelectorAll("[data-scan-mode]");
  modeBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      modeBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      const mode = btn.dataset.scanMode;
      document.getElementById("momentum-scan-panel")?.classList.toggle("hidden", mode !== "momentum");
      document.getElementById("squeeze-scan-panel")?.classList.toggle("hidden", mode !== "squeeze");

      if (mode === "squeeze" && squeezeGrid?.children.length === 0) {
        loadSqueezeScan();
      } else if (mode === "momentum" && grid?.children.length === 0) {
        loadMomentumScan();
      }
    });
  });
}

export { loadMomentumScan, loadSqueezeScan };
