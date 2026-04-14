/**
 * Settings View
 * =============
 * In-browser API key management + launch readiness checklist.
 * Keys are written to the server's .env file via POST /api/settings.
 * Sensitive keys (private keys, API tokens) are never returned to the browser
 * after saving — only a masked preview (sk-ab…ef12) is shown.
 */

import { API } from "/static/js/api.js";

// ── API extension ──────────────────────────────────────────────────────────────

Object.assign(API, {
  settingsGet:    ()          => API._fetch("/api/settings"),
  settingsStatus: ()          => API._fetch("/api/settings/status"),
  settingsSave:   (payload)   => {
    return fetch(`${location.origin}/api/settings`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    }).then(async r => {
      const data = await r.json();
      return r.ok ? { data, error: null } : { data: null, error: data.detail || `HTTP ${r.status}` };
    }).catch(e => ({ data: null, error: e.message }));
  },
  settingsDelete: (key) => {
    return fetch(`${location.origin}/api/settings/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }).then(async r => {
      const data = await r.json();
      return r.ok ? { data, error: null } : { data: null, error: data.detail || `HTTP ${r.status}` };
    }).catch(e => ({ data: null, error: e.message }));
  },
});

// ── Helpers ────────────────────────────────────────────────────────────────────

function _statusDot(ready) {
  return ready
    ? `<span class="st-dot st-dot-green" title="Ready"></span>`
    : `<span class="st-dot st-dot-red"   title="Not configured"></span>`;
}

function _keyDot(isSet) {
  return isSet
    ? `<span class="st-dot st-dot-green" title="Configured"></span>`
    : `<span class="st-dot st-dot-amber" title="Not set"></span>`;
}

// ── Launch checklist ────────────────────────────────────────────────────────────

function renderChecklist(status) {
  const el = document.getElementById("st-checklist");
  if (!el) return;

  const systems = [
    status.paper_trading,
    status.polymarket_live,
    status.equity_ai,
  ];

  const rows = systems.map(s => `
    <div class="st-check-row">
      ${_statusDot(s.ready)}
      <span class="st-check-label">${s.label}</span>
      <span class="st-check-msg ${s.ready ? "" : "dim"}">${s.message}</span>
    </div>`).join("");

  const md = status.market_data;
  const dataRow = `
    <div class="st-check-row">
      ${_statusDot(md.finnhub || md.fred || md.nasdaq)}
      <span class="st-check-label">Market Data Enrichment</span>
      <span class="dim" style="font-size:12px">
        Finnhub ${md.finnhub ? "✓" : "✗"} &nbsp;
        FRED ${md.fred ? "✓" : "✗"} &nbsp;
        Nasdaq ${md.nasdaq ? "✓" : "✗"}
        &nbsp;(all optional — yfinance is always available)
      </span>
    </div>`;

  el.innerHTML = rows + dataRow;
}

// ── Key group forms ─────────────────────────────────────────────────────────────

function _groupHtml(groupName, keys, keyData) {
  const groupKeys = Object.entries(keyData).filter(([, v]) => v.group === groupName);
  if (!groupKeys.length) return "";

  const rows = groupKeys.map(([key, meta]) => {
    const docsLink = meta.docs_url
      ? `<a class="st-docs-link" href="${meta.docs_url}" target="_blank" rel="noopener">Get key ↗</a>`
      : "";
    const inputType = meta.sensitive ? "password" : "text";
    const currentVal = meta.sensitive ? "" : (meta.display_value || "");
    return `
      <div class="st-key-row" data-key="${key}">
        <div class="st-key-header">
          ${_keyDot(meta.is_set)}
          <label class="st-key-label" for="st-input-${key}">${meta.label}</label>
          ${docsLink}
        </div>
        <div class="st-key-input-row">
          <input
            id="st-input-${key}"
            type="${inputType}"
            class="input-field st-input"
            placeholder="${meta.is_set ? (meta.sensitive ? "••••••••  (set — enter new value to replace)" : meta.display_value) : meta.placeholder}"
            value="${currentVal}"
            autocomplete="off"
            data-key="${key}"
          >
          <button class="btn-xs st-clear-btn" data-key="${key}" title="Remove this key">✕</button>
        </div>
        ${meta.is_set && meta.sensitive ? `<div class="st-masked">${meta.display_value}</div>` : ""}
      </div>`;
  }).join("");

  return `
    <div class="card st-group-card">
      <div class="card-header">
        <span class="card-title">${groupName}</span>
      </div>
      ${rows}
      <div style="margin-top:14px">
        <button class="btn-primary st-save-group-btn" data-group="${groupName}">Save ${groupName} Keys</button>
        <span class="st-save-feedback" id="st-feedback-${groupName.replace(/\s+/g,"-")}"></span>
      </div>
    </div>`;
}

function renderKeyForms(keyData) {
  const container = document.getElementById("st-key-forms");
  if (!container) return;

  const groups = [...new Set(Object.values(keyData).map(v => v.group))];
  container.innerHTML = groups.map(g => _groupHtml(g, null, keyData)).join("");

  // Wire Save buttons
  container.querySelectorAll(".st-save-group-btn").forEach(btn => {
    btn.addEventListener("click", () => _saveGroup(btn.dataset.group, keyData));
  });

  // Wire Clear (✕) buttons
  container.querySelectorAll(".st-clear-btn").forEach(btn => {
    btn.addEventListener("click", () => _clearKey(btn.dataset.key));
  });
}

async function _saveGroup(groupName, keyData) {
  const groupKeys = Object.keys(keyData).filter(k => keyData[k].group === groupName);
  const payload = {};
  groupKeys.forEach(key => {
    const input = document.getElementById(`st-input-${key}`);
    if (input?.value?.trim()) payload[key] = input.value.trim();
  });

  const feedbackId = `st-feedback-${groupName.replace(/\s+/g, "-")}`;
  const feedback   = document.getElementById(feedbackId);

  if (!Object.keys(payload).length) {
    if (feedback) { feedback.textContent = "No new values entered."; feedback.className = "st-save-feedback dim"; }
    return;
  }

  if (feedback) { feedback.textContent = "Saving…"; feedback.className = "st-save-feedback dim"; }

  const { data, error } = await API.settingsSave(payload);
  if (error) {
    if (feedback) { feedback.textContent = `Error: ${error}`; feedback.className = "st-save-feedback st-feedback-err"; }
    return;
  }

  if (feedback) {
    feedback.textContent = data.message || "Saved!";
    feedback.className   = "st-save-feedback st-feedback-ok";
    setTimeout(() => { if (feedback) feedback.textContent = ""; }, 4000);
  }

  // Clear input fields and reload status
  groupKeys.forEach(key => {
    const input = document.getElementById(`st-input-${key}`);
    if (input && payload[key]) input.value = "";
  });

  // Refresh the whole page section to show updated dots
  await loadSettings();
}

async function _clearKey(key) {
  if (!confirm(`Remove ${key} from .env?`)) return;
  const { error } = await API.settingsDelete(key);
  if (error) { alert(`Error: ${error}`); return; }
  await loadSettings();
}

// ── Env file path display ──────────────────────────────────────────────────────

function renderEnvPath(envFile) {
  const el = document.getElementById("st-env-path");
  if (el) el.textContent = envFile;
}

// ── Main load ──────────────────────────────────────────────────────────────────

export function initSettingsView() {
  // nothing to wire on init — all wiring happens after render in loadSettings()
}

export async function loadSettings() {
  const [settingsRes, statusRes] = await Promise.all([
    API.settingsGet(),
    API.settingsStatus(),
  ]);

  if (statusRes.data) renderChecklist(statusRes.data);
  if (settingsRes.data) {
    renderKeyForms(settingsRes.data.keys);
    renderEnvPath(settingsRes.data.env_file);
  }
}
