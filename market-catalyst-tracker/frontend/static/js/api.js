/**
 * api.js — Thin fetch wrapper for the FastAPI backend.
 * All endpoints return { data, error } so callers never throw.
 */

const API_BASE = window.location.origin;

async function _get(path, params = {}) {
  const url = new URL(`${API_BASE}${path}`);
  Object.entries(params).forEach(([k, v]) => v !== undefined && url.searchParams.set(k, v));
  try {
    const res = await fetch(url.toString());
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      return { data: null, error: err.detail || `HTTP ${res.status}` };
    }
    return { data: await res.json(), error: null };
  } catch (e) {
    return { data: null, error: e.message };
  }
}

export const API = {
  // Internal helpers used by extension modules
  _fetch: (path, params) => _get(path, params),
  _post:  async (path) => {
    try {
      const res = await fetch(`${API_BASE}${path}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        return { data: null, error: err.detail || `HTTP ${res.status}` };
      }
      return { data: await res.json(), error: null };
    } catch (e) {
      return { data: null, error: e.message };
    }
  },

  // Market overview
  marketOverview: () => _get("/api/market/overview"),
  marketMovers: (limit = 10) => _get("/api/market/movers", { limit }),

  // Charts
  chartData: (symbol, period = "3mo", interval = "1d") =>
    _get(`/api/charts/${symbol}`, { period, interval }),
  quote: (symbol) => _get(`/api/quote/${symbol}`),

  // News
  marketNews: () => _get("/api/news/market"),
  symbolNews: (symbol) => _get(`/api/news/${symbol}`),

  // Correlation ("why is it moving")
  marketCorrelation: () => _get("/api/news/correlation/market"),
  symbolCorrelation: (symbol) => _get(`/api/news/correlation/${symbol}`),

  // Catalysts
  biotechCatalysts: (priority) => _get("/api/catalysts/biotech", priority ? { priority } : {}),
  upcomingCatalysts: (days = 90) => _get("/api/catalysts/upcoming", { days }),

  // Momentum
  momentumScan: (params = {}) => _get("/api/momentum/scan", params),
  squeezeScan: (top_n = 20) => _get("/api/momentum/squeeze", { top_n }),

  // Search
  search: (q) => _get("/api/search", { q }),

  // Macro
  macroIndicators: () => _get("/api/macro/indicators"),
};

/* ── WebSocket live price stream ──────────────────────────────────────────────
   Usage:
     const ws = createPriceStream(onQuoteUpdate, onConnect, onError);
     ws.subscribe(["SPY", "QQQ"]);
     ws.close();
─────────────────────────────────────────────────────────────────────────────── */

export function createPriceStream(onUpdate, onConnect, onError) {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${protocol}://${location.host}/ws/prices`;

  let ws = null;
  let retryDelay = 2000;
  let closed = false;

  function connect() {
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      retryDelay = 2000;
      if (typeof onConnect === "function") onConnect(ws);
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (typeof onUpdate === "function") onUpdate(msg);
      } catch (_) {}
    };

    ws.onerror = (e) => {
      if (typeof onError === "function") onError(e);
    };

    ws.onclose = () => {
      if (!closed) {
        setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 1.5, 30_000);
      }
    };
  }

  connect();

  return {
    subscribe(symbols) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: "subscribe", symbols }));
      }
    },
    close() {
      closed = true;
      ws && ws.close();
    },
    get readyState() {
      return ws ? ws.readyState : WebSocket.CLOSED;
    },
  };
}

/* ── Formatting helpers ───────────────────────────────────────────────────────*/

export const Fmt = {
  pct: (v, decimals = 2) => {
    if (v == null || isNaN(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${v.toFixed(decimals)}%`;
  },
  price: (v, decimals = 2) => {
    if (v == null || isNaN(v)) return "—";
    return `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;
  },
  bigNum: (v) => {
    if (v == null || isNaN(v)) return "—";
    if (v >= 1e12) return `$${(v / 1e12).toFixed(2)}T`;
    if (v >= 1e9)  return `$${(v / 1e9).toFixed(2)}B`;
    if (v >= 1e6)  return `$${(v / 1e6).toFixed(2)}M`;
    return `$${v.toLocaleString()}`;
  },
  timeAgo: (ts) => {
    const diff = (Date.now() / 1000) - ts;
    if (diff < 60)   return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  },
  date: (str) => {
    if (!str) return "—";
    try { return new Date(str).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }); }
    catch { return str; }
  },
  num: (v, decimals = 2) => {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString(undefined, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  },
  rvol: (v) => {
    if (v == null) return "—";
    return `${v.toFixed(1)}×`;
  },
  countdown: (days) => {
    if (days == null) return "—";
    if (days < 0) return `${Math.abs(days)}d ago`;
    if (days === 0) return "TODAY";
    if (days === 1) return "TOMORROW";
    return `${days}d`;
  },
};

export function changeClass(pct) {
  if (pct > 0.05) return "pos";
  if (pct < -0.05) return "neg";
  return "flat";
}

export function scoreColor(score) {
  if (score >= 70) return "#22c55e";
  if (score >= 45) return "#f59e0b";
  return "#3b82f6";
}

export function priorityBadge(priority) {
  const map = {
    HIGH:   "badge-green",
    MEDIUM: "badge-amber",
    LOW:    "badge-blue",
  };
  return `<span class="badge ${map[priority] || "badge-blue"}">${priority}</span>`;
}

export function eventTypeBadge(type) {
  const map = {
    FDA_PDUFA:        ["badge-green",  "FDA PDUFA"],
    FDA_ADCOM:        ["badge-cyan",   "ADCOM"],
    PHASE3_RESULT:    ["badge-purple", "Ph3 Data"],
    NDA_SUBMISSION:   ["badge-blue",   "NDA Sub"],
    SEC_8K_FDA:       ["badge-amber",  "8-K FDA"],
    SEC_8K_APPROVAL:  ["badge-green",  "Approved"],
    SEC_8K_CRL:       ["badge-red",    "CRL"],
    MOMENTUM:         ["badge-blue",   "Momentum"],
  };
  const [cls, label] = map[type] || ["badge-blue", type];
  return `<span class="badge ${cls}">${label}</span>`;
}

export function sentimentBadge(score) {
  if (score > 0.2)  return `<span class="badge badge-green">Bullish</span>`;
  if (score < -0.2) return `<span class="badge badge-red">Bearish</span>`;
  return `<span class="badge badge-blue">Neutral</span>`;
}

export function signalBadge(signal) {
  const map = {
    BREAKOUT: "badge-green",
    GAP_UP:   "badge-cyan",
    GAP_DOWN: "badge-red",
    SQUEEZE:  "badge-purple",
    HIGH_VOL: "badge-amber",
    MOMENTUM: "badge-blue",
  };
  return `<span class="badge ${map[signal] || "badge-blue"}">${signal.replace("_", " ")}</span>`;
}
