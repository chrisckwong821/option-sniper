const state = {
  payload: null,
  todayPayload: null,
  cache: null,
  rowsByTicker: new Map(),
  view: "yesterday",
  search: "",
  sector: "all",
  reliability: "all",
  threshold: "all",
  viewSettings: {
    yesterday: { sortKey: "original_rank", sortDirection: "asc" },
    today: {
      page: 1,
      pageSize: 20,
      sortKey: "abs_z_score",
      sortDirection: "desc",
    },
  },
  live: {
    socket: null,
    connected: false,
    retryAttempt: 0,
    retryTimer: null,
    lastMessageAt: 0,
    dataDate: null,
    marketSession: "snapshot",
    pricesReceived: 0,
  },
  renderQueued: false,
};

const elements = {
  tables: {
    yesterday: {
      panel: document.querySelector("#panel-yesterday"),
      rows: document.querySelector("#mover-rows-yesterday"),
      empty: document.querySelector("#empty-state-yesterday"),
      resultCount: document.querySelector("#result-count-yesterday"),
      methodLine: document.querySelector("#method-line-yesterday"),
    },
    today: {
      panel: document.querySelector("#panel-today"),
      rows: document.querySelector("#mover-rows-today"),
      empty: document.querySelector("#empty-state-today"),
      resultCount: document.querySelector("#result-count-today"),
      methodLine: document.querySelector("#method-line-today"),
    },
  },
  search: document.querySelector("#search-input"),
  sector: document.querySelector("#sector-filter"),
  reliability: document.querySelector("#reliability-filter"),
  sessionDate: document.querySelector("#session-date"),
  lastUpdate: document.querySelector("#last-update"),
  statusBadge: document.querySelector("#status-badge"),
  connectionLabel: document.querySelector("#connection-label"),
  topSignal: document.querySelector("#top-signal"),
  topTicker: document.querySelector("#top-ticker"),
  outsideCount: document.querySelector("#outside-count"),
  coverageCount: document.querySelector("#coverage-count"),
  reliabilityMix: document.querySelector("#reliability-mix"),
  anchorNote: document.querySelector("#anchor-note"),
  ivNote: document.querySelector("#iv-note"),
  announcer: document.querySelector("#live-announcer"),
  absoluteRank: document.querySelector('[data-sort="abs_z_score"]'),
  savedRank: document.querySelector('[data-sort="original_rank"]'),
  todayPagination: document.querySelector("#pagination-today"),
  todayPageLabel: document.querySelector("#page-label-today"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value, digits = 2) {
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatSigned(value, digits = 2, suffix = "") {
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${formatNumber(number, digits)}${suffix}`;
}

function formatMarketCap(value) {
  const number = Number(value);
  if (number >= 1e12) return `$${formatNumber(number / 1e12, 2)}T`;
  if (number >= 1e9) return `$${formatNumber(number / 1e9, 1)}B`;
  return `$${formatNumber(number / 1e6, 0)}M`;
}

function shortCompanyName(value) {
  return String(value ?? "")
    .replace(/ Class [A-Z] Common Stock$/i, "")
    .replace(/ Common Stock Class [A-Z]$/i, "")
    .replace(/ Common Stock$/i, "")
    .replace(/ ADS$/i, "")
    .trim();
}

function formatDate(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  }).format(new Date(`${value}T12:00:00-04:00`));
}

function formatUpdate(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

function formatPriceTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

function populateSectors(rows) {
  const sectors = [...new Set(rows.map((row) => row.sector).filter(Boolean))].sort();
  elements.sector.innerHTML = '<option value="all">All sectors</option>' + sectors
    .map((sector) => `<option value="${escapeHtml(sector)}">${escapeHtml(sector)}</option>`)
    .join("");
}

function activePayload() {
  return state.view === "today" ? state.todayPayload : state.payload;
}

function activeSettings() {
  return state.viewSettings[state.view];
}

function allRows() {
  return [state.payload, state.todayPayload]
    .filter(Boolean)
    .flatMap((payload) => payload.rows);
}

function displayGainPct(row, view) {
  if (view === "yesterday") {
    return Number(row.saved_gain_pct ?? row.snapshot_gain_pct ?? row.gain_pct);
  }
  return Number(row.gain_pct);
}

function visibleRows(view = state.view) {
  const payload = view === "today" ? state.todayPayload : state.payload;
  if (!payload) return [];
  const settings = state.viewSettings[view];
  const qualityRank = { low: 1, medium: 2, high: 3 };
  const direction = settings.sortDirection === "desc" ? -1 : 1;
  return payload.rows
    .filter((row) => {
      const haystack = `${row.ticker} ${row.company_name}`.toLowerCase();
      if (state.search && !haystack.includes(state.search)) return false;
      if (state.sector !== "all" && row.sector !== state.sector) return false;
      if (state.reliability !== "all" && qualityRank[row.iv_reliability] < qualityRank[state.reliability]) return false;
      const thresholdZ = view === "today" ? Math.abs(row.z_score) : row.z_score;
      if (state.threshold === "one" && thresholdZ <= 1) return false;
      if (state.threshold === "two" && thresholdZ <= 2) return false;
      return true;
    })
    .sort((a, b) => {
      const sortValue = (row) => {
        if (settings.sortKey === "gain_pct") {
          return displayGainPct(row, view);
        }
        if (settings.sortKey === "abs_z_score") return Math.abs(Number(row.z_score));
        return Number(row[settings.sortKey]);
      };
      const primary = (sortValue(a) - sortValue(b)) * direction;
      return primary || (b.z_score - a.z_score);
    });
}

function extendedPriceState(row) {
  const regularPrice = Number(row.regular_price || row.snapshot_last_price || row.last_price);
  const activeExtendedSession = ["premarket", "afterhours", "overnight"].includes(state.live.marketSession)
    ? state.live.marketSession
    : null;
  const extendedSession = row.extended_session;
  const hasExtendedPrice = ["premarket", "afterhours", "overnight"].includes(extendedSession)
    && Number(row.extended_price) > 0;
  return {
    activeExtendedSession,
    change: hasExtendedPrice
      ? window.SigmaMath.calculatePriceChange(row.extended_price, regularPrice)
      : null,
    extendedSession,
    hasExtendedPrice,
    regularPrice,
  };
}

function priceStackMarkup(row, includeExtendedChange = false) {
  const priceState = extendedPriceState(row);
  const regularTime = formatPriceTime(row.regular_timestamp);
  const regularLabel = `${row.regular_is_live ? "Regular" : "Prior close"}${regularTime ? ` · ${regularTime}` : ""}`;

  let extendedMarkup = "";
  if (priceState.hasExtendedPrice) {
    const direction = priceState.change && priceState.change.percent >= 0 ? "positive" : "negative";
    const staleLabel = row.extended_stale ? " · stale" : "";
    const extendedTime = formatPriceTime(row.extended_timestamp);
    const sessionLabel = window.SigmaMath.sessionLabel(priceState.extendedSession);
    extendedMarkup = `
      <span class="price-secondary ${escapeHtml(priceState.extendedSession)} ${row.extended_stale ? "stale" : ""}">
        <span class="price-secondary-values">
          <span class="session-marker">${escapeHtml(sessionLabel)}</span>
          <span class="secondary-price">$${formatNumber(row.extended_price)}</span>
          ${includeExtendedChange && priceState.change ? `<span class="secondary-move ${direction}">${formatSigned(priceState.change.percent, 2, "%")}</span>` : ""}
        </span>
        <span class="price-session ${escapeHtml(priceState.extendedSession)} ${row.extended_stale ? "stale" : ""}">${escapeHtml(`${extendedTime || "Awaiting timestamp"}${staleLabel}`)}</span>
      </span>`;
  } else if (priceState.activeExtendedSession) {
    const sessionLabel = window.SigmaMath.sessionLabel(priceState.activeExtendedSession);
    extendedMarkup = `
      <span class="price-secondary empty ${escapeHtml(priceState.activeExtendedSession)}">
        <span class="price-secondary-values">
          <span class="session-marker">${escapeHtml(sessionLabel)}</span>
          <span class="no-session-trade">No trades</span>
        </span>
      </span>`;
  }

  return `
    <span class="price-stack">
      <span class="price-primary">
        <span class="number">$${formatNumber(priceState.regularPrice)}</span>
        <span class="price-session regular">${escapeHtml(regularLabel)}</span>
      </span>
      ${extendedMarkup}
    </span>`;
}

function gainStackMarkup(row) {
  const isSavedList = row.dashboard_view === "yesterday";
  const regularGain = isSavedList
    ? Number(row.saved_gain_pct ?? row.snapshot_gain_pct ?? row.gain_pct)
    : Number(row.regular_gain_pct ?? row.snapshot_gain_pct ?? row.gain_pct);
  const regularDirection = regularGain >= 0 ? "positive" : "negative";
  const regularLabel = isSavedList
    ? "Saved gain"
    : row.regular_is_live ? "Today's move" : "Prior day";
  const priceState = extendedPriceState(row);
  let extendedMarkup = "";

  if (priceState.hasExtendedPrice && priceState.change) {
    const extendedDirection = priceState.change.percent >= 0 ? "positive" : "negative";
    extendedMarkup = `
      <span class="gain-secondary">
        <span class="number movement ${extendedDirection}">${formatSigned(priceState.change.percent, 2, "%")}</span>
        <span class="gain-session ${escapeHtml(priceState.extendedSession)}">${escapeHtml(window.SigmaMath.sessionLabel(priceState.extendedSession))}</span>
      </span>`;
  }

  return `
    <span class="gain-stack">
      <span class="gain-primary">
        <span class="number movement ${regularDirection}">${formatSigned(regularGain, 2, "%")}</span>
        <span class="gain-session regular">${regularLabel}</span>
      </span>
      ${extendedMarkup}
    </span>`;
}

function rowMarkup(row, viewRank) {
  const width = Math.min(100, Math.max(2, (Math.abs(row.z_score) / 4) * 100));
  const reliability = escapeHtml(row.iv_reliability);
  const direction = row.gain_pct >= 0 ? "positive" : "negative";
  const priceTitle = row.price_timestamp
    ? `Updated ${formatUpdate(row.price_timestamp)}`
    : "Saved closing snapshot";
  return `
    <tr class="${row.z_score > 1 ? "signal-row" : ""}">
      <td><span class="rank-number">${viewRank}</span></td>
      <td class="company-cell">
        <strong>${escapeHtml(row.ticker)}</strong><span class="company-name">${escapeHtml(shortCompanyName(row.company_name))}</span>
        <span class="company-description" title="${escapeHtml(row.company_description)}">${escapeHtml(row.company_description)}</span>
        <span class="mobile-price-line">${priceStackMarkup(row, true)}</span>
      </td>
      <td>${escapeHtml(row.sector)}</td>
      <td><span class="number">${formatMarketCap(row.market_cap_usd)}</span></td>
      <td class="price-cell" title="${escapeHtml(priceTitle)}">
        ${priceStackMarkup(row)}
      </td>
      <td>${gainStackMarkup(row)}</td>
      <td><span class="number">${formatNumber(row.sigma_1d_pct)}%</span></td>
      <td>
        <div class="z-cell">
          <span class="z-value ${direction}">${formatNumber(row.z_score, 3)}</span>
          <span class="z-track"><span class="z-bar ${direction}" style="width:${width}%"></span></span>
        </div>
      </td>
      <td><span class="reliability ${reliability}">${reliability}</span></td>
    </tr>`;
}

function sortLabel(view = state.view) {
  const settings = state.viewSettings[view];
  const labels = {
    original_rank: "saved rank",
    abs_z_score: "absolute Z-score",
    z_score: "Z-score",
    gain_pct: "gain",
    sigma_1d_pct: "one-day sigma",
    market_cap_usd: "market cap",
  };
  return `${labels[settings.sortKey]}, ${settings.sortDirection === "desc" ? "high to low" : "low to high"}`;
}

function latestVisibleTimestamp(rows) {
  const timestamps = rows.map((row) => Date.parse(row.price_timestamp || "")).filter(Number.isFinite);
  return timestamps.length ? new Date(Math.max(...timestamps)).toISOString() : null;
}

function setConnectionState(status, label) {
  elements.statusBadge.dataset.state = status;
  elements.connectionLabel.textContent = label;
}

function updateConnectionPresentation(message) {
  const feeds = Object.values(message.feeds || {});
  const errors = feeds.map((feed) => feed.last_error || feed.detail).filter(Boolean);
  const cacheError = state.cache?.last_refresh_error;
  elements.statusBadge.title = [...errors, cacheError].filter(Boolean).join("\n");

  if (state.live.pricesReceived > 0) {
    const liveRows = [...state.rowsByTicker.values()]
      .map((rows) => rows.find((row) => row.price_source))
      .filter(Boolean);
    const freshPrices = liveRows.filter((row) => !row.price_stale).length;
    const phase = window.SigmaMath.sessionLabel(state.live.marketSession);
    setConnectionState(freshPrices ? "live" : "stale", freshPrices ? `Live · ${phase}` : `Stale · ${phase}`);
    return;
  }

  const activeStates = new Set(["connecting", "subscribed", "retrying"]);
  if (feeds.some((feed) => activeStates.has(feed.state))) {
    setConnectionState("connecting", "Feed connecting");
  } else {
    setConnectionState("snapshot", errors.length ? "Snapshot · Feed offline" : "Snapshot");
  }
}

function renderTable(view) {
  const payload = view === "today" ? state.todayPayload : state.payload;
  if (!payload) return [];
  const table = elements.tables[view];
  const settings = state.viewSettings[view];
  const matchingRows = visibleRows(view);
  const pagination = view === "today"
    ? window.SigmaMath.paginateRows(matchingRows, settings.page, settings.pageSize)
    : window.SigmaMath.paginateRows(matchingRows, 1, Math.max(1, matchingRows.length));
  const rows = pagination.items;
  if (view === "today") settings.page = pagination.page;
  const observedRows = payload.rows.filter((row) => row.price_source).length;

  table.rows.innerHTML = rows.map((row, index) => rowMarkup(
    row,
    view === "yesterday" && settings.sortKey === "original_rank"
      ? row.original_rank
      : pagination.offset + index + 1,
  )).join("");
  table.empty.hidden = rows.length > 0;
  table.resultCount.textContent = view === "today"
    ? `${pagination.start}-${pagination.end} of ${pagination.total}`
    : `${matchingRows.length} of ${payload.rows.length}`;
  table.methodLine.textContent = view === "today"
    ? observedRows > 0
      ? `${observedRows} of ${payload.rows.length} current-session prices · ranked by ${sortLabel(view)}`
      : `No current-session prices yet · showing ${formatDate(payload.data_date)} closing snapshot · ranked by ${sortLabel(view)}`
    : `Fixed 20-name list · ranked by ${sortLabel(view)}`;
  if (view === "today") {
    elements.todayPagination.hidden = pagination.totalPages <= 1;
    elements.todayPageLabel.textContent = `Page ${pagination.page} of ${pagination.totalPages}`;
    elements.todayPagination.querySelectorAll("button").forEach((button) => {
      const backward = ["first", "previous"].includes(button.dataset.pageAction);
      button.disabled = backward
        ? pagination.page === 1
        : pagination.page === pagination.totalPages;
    });
  }
  return rows;
}

function render() {
  const payload = activePayload();
  if (!payload) return;
  renderTable(state.view === "yesterday" ? "today" : "yesterday");
  renderTable(state.view);
  const matchingRows = visibleRows(state.view);
  const top = matchingRows[0];
  const mix = payload.reliability_mix || {};
  const liveTimestamp = latestVisibleTimestamp(payload.rows);
  const outsideCount = matchingRows.filter((row) => (
    state.view === "today" ? Math.abs(row.z_score) > 1 : row.z_score > 1
  )).length;

  elements.topSignal.textContent = top ? `${formatNumber(top.z_score, 3)} z` : "--";
  elements.topSignal.dataset.direction = top && top.z_score < 0 ? "negative" : "positive";
  elements.topTicker.textContent = top?.ticker || "No matching movers";
  elements.outsideCount.textContent = outsideCount;
  elements.coverageCount.textContent = payload.rows.length;
  elements.reliabilityMix.textContent = `${mix.high || 0} high / ${mix.medium || 0} medium / ${mix.low || 0} low`;
  elements.sessionDate.textContent = formatDate(state.live.dataDate || payload.data_date);
  elements.lastUpdate.textContent = formatUpdate(liveTimestamp || payload.last_update_utc);
  elements.anchorNote.textContent = state.view === "today"
    ? `Today's return anchor: ${formatDate(payload.anchor_date)} close`
    : `Saved screen anchor: ${formatDate(payload.anchor_date)} close`;
  elements.ivNote.textContent = `IV snapshot: ${formatUpdate(payload.iv_snapshot_time)} / expiry ${payload.option_expiry}`;
  elements.absoluteRank.hidden = state.view !== "today";
  elements.savedRank.hidden = state.view !== "yesterday";

  document.querySelectorAll(".sort-button").forEach((button) => {
    const buttonView = button.closest(".table-panel")?.id === "panel-today" ? "today" : "yesterday";
    const settings = state.viewSettings[buttonView];
    button.classList.toggle("active", button.dataset.sort === settings.sortKey);
    button.dataset.direction = button.dataset.sort === settings.sortKey ? settings.sortDirection : "";
  });
  document.querySelectorAll(".rank-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.sort === activeSettings().sortKey);
  });
  document.querySelectorAll(".view-button").forEach((button) => {
    const selected = button.dataset.view === state.view;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.setAttribute("tabindex", selected ? "0" : "-1");
  });
  Object.entries(elements.tables).forEach(([view, table]) => {
    const selected = view === state.view;
    table.panel.hidden = !selected;
    table.panel.classList.toggle("active", selected);
  });
}

function queueRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
}

function setSort(sortKey) {
  const settings = activeSettings();
  if (settings.sortKey === sortKey) {
    settings.sortDirection = settings.sortDirection === "desc" ? "asc" : "desc";
  } else {
    settings.sortKey = sortKey;
    settings.sortDirection = "desc";
  }
  if (state.view === "today") settings.page = 1;
  render();
}

function setView(view) {
  if (view === state.view || !["yesterday", "today"].includes(view)) return;
  state.view = view;
  state.sector = "all";
  populateSectors(activePayload().rows);
  elements.sector.value = "all";
  render();
}

function applyLivePrice(point, tradingDate) {
  if (!window.SigmaMath.isPriceForTradingDate(point.trading_date, tradingDate)) return false;
  const trackedRows = state.rowsByTicker.get(point.ticker) || [];
  if (!trackedRows.length) return false;
  return trackedRows.reduce(
    (changed, row) => applyLivePriceToRow(row, point) || changed,
    false,
  );
}

function applyLivePriceToRow(row, point) {
  if (!window.SigmaMath.isCurrentAnchor(
    row.dashboard_view,
    row.payload_anchor_date,
    state.cache?.target_completed_session,
  )) return false;
  if (point.ts_event === row.price_timestamp) {
    row.price_stale = Boolean(point.is_stale);
    if (point.session === row.extended_session) {
      row.extended_stale = Boolean(point.is_stale);
    }
    return false;
  }
  if (!window.SigmaMath.isNewerPrice(point.ts_event, row.price_timestamp)) return false;
  const anchor = row.live_anchor_price || row.anchor_price || row.snapshot_last_price || row.last_price;
  const metrics = window.SigmaMath.calculateLiveMetrics(point.price, anchor, row.sigma_1d_pct);
  if (!metrics) return false;

  row.last_price = Number(point.price);
  row.gain_pct = metrics.gainPct;
  row.log_return_pct = metrics.logReturnPct;
  row.z_score = metrics.zScore;
  row.outside_one_sigma = metrics.zScore > 1;
  row.price_timestamp = point.ts_event;
  row.price_source = point.source_dataset;
  row.price_session = point.session;
  row.price_trading_date = point.trading_date;
  row.price_stale = Boolean(point.is_stale);
  if (point.session === "regular") {
    row.regular_price = Number(point.price);
    row.regular_timestamp = point.ts_event;
    row.regular_source = point.source_dataset;
    row.regular_is_live = true;
    row.regular_gain_pct = row.dashboard_view === "yesterday"
      ? row.saved_gain_pct
      : metrics.gainPct;
    row.extended_price = null;
    row.extended_timestamp = null;
    row.extended_session = null;
    row.extended_stale = false;
  } else if (["premarket", "afterhours", "overnight"].includes(point.session)) {
    row.extended_price = Number(point.price);
    row.extended_timestamp = point.ts_event;
    row.extended_session = point.session;
    row.extended_trading_date = point.trading_date;
    row.extended_source = point.source_dataset;
    row.extended_stale = Boolean(point.is_stale);
  }
  return true;
}

function handlePriceMessage(message) {
  if (!message || !Array.isArray(message.prices)) return;
  state.live.lastMessageAt = Date.now();
  state.live.marketSession = message.market_session || state.live.marketSession;
  state.live.dataDate = message.trading_date || state.live.dataDate;
  const changed = message.prices.reduce(
    (count, point) => count + Number(applyLivePrice(point, message.trading_date)),
    0,
  );
  state.live.pricesReceived = [...state.rowsByTicker.values()]
    .filter((rows) => rows.some((row) => row.price_source))
    .length;
  updateConnectionPresentation(message);
  if (changed) {
    elements.announcer.textContent = `${changed} live prices updated`;
    queueRender();
  } else {
    render();
  }
}

function scheduleReconnect() {
  window.clearTimeout(state.live.retryTimer);
  const delay = Math.min(30000, 1000 * (2 ** state.live.retryAttempt));
  state.live.retryAttempt += 1;
  state.live.retryTimer = window.setTimeout(connectLivePrices, delay);
}

function connectLivePrices() {
  if (!state.payload || !state.todayPayload || document.visibilityState === "hidden") return;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/ws/prices`;
  setConnectionState("connecting", "Connecting");
  const socket = new WebSocket(url);
  state.live.socket = socket;

  socket.addEventListener("open", () => {
    state.live.connected = true;
    state.live.retryAttempt = 0;
    state.live.lastMessageAt = Date.now();
    setConnectionState("connecting", "Syncing");
  });
  socket.addEventListener("message", (event) => {
    try {
      handlePriceMessage(JSON.parse(event.data));
    } catch (_) {
      setConnectionState("stale", "Stream error");
    }
  });
  socket.addEventListener("close", () => {
    if (state.live.socket !== socket) return;
    state.live.connected = false;
    setConnectionState(state.live.pricesReceived ? "stale" : "snapshot", state.live.pricesReceived ? "Stale prices" : "Snapshot");
    scheduleReconnect();
  });
  socket.addEventListener("error", () => socket.close());
}

document.querySelectorAll(".rank-button, .sort-button").forEach((button) => {
  button.addEventListener("click", () => setSort(button.dataset.sort));
});

document.querySelectorAll(".view-button").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
  button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const nextView = button.dataset.view === "yesterday" ? "today" : "yesterday";
    setView(nextView);
    document.querySelector(`[data-view="${nextView}"]`).focus();
  });
});

document.querySelectorAll(".threshold-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.threshold = button.dataset.threshold;
    state.viewSettings.today.page = 1;
    document.querySelectorAll(".threshold-button").forEach((item) => item.classList.toggle("active", item === button));
    render();
  });
});

elements.search.addEventListener("input", (event) => {
  state.search = event.target.value.trim().toLowerCase();
  state.viewSettings.today.page = 1;
  render();
});
elements.search.addEventListener("change", (event) => {
  state.search = event.target.value.trim().toLowerCase();
  state.viewSettings.today.page = 1;
  render();
});
elements.sector.addEventListener("change", (event) => {
  state.sector = event.target.value;
  state.viewSettings.today.page = 1;
  render();
});
elements.reliability.addEventListener("change", (event) => {
  state.reliability = event.target.value;
  state.viewSettings.today.page = 1;
  render();
});

elements.todayPagination.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-page-action]");
  if (!button || button.disabled) return;
  const settings = state.viewSettings.today;
  const totalPages = Math.max(
    1,
    Math.ceil(visibleRows("today").length / settings.pageSize),
  );
  const nextPage = {
    first: 1,
    previous: settings.page - 1,
    next: settings.page + 1,
    last: totalPages,
  }[button.dataset.pageAction];
  settings.page = Math.min(totalPages, Math.max(1, nextPage));
  render();
  elements.tables.today.panel.scrollIntoView({ behavior: "smooth", block: "start" });
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !state.live.connected) connectLivePrices();
});

window.setInterval(() => {
  if (state.live.connected && Date.now() - state.live.lastMessageAt > 15000) {
    setConnectionState("stale", "Stream stale");
  }
}, 5000);

window.addEventListener("beforeunload", () => {
  window.clearTimeout(state.live.retryTimer);
  state.live.socket?.close();
});

function initializeRows(payload, view) {
  payload.rows.forEach((row) => {
      row.dashboard_view = view;
      row.payload_anchor_date = payload.anchor_date;
      row.snapshot_last_price = row.last_price;
      row.snapshot_gain_pct = row.gain_pct;
      row.saved_gain_pct = row.gain_pct;
      row.regular_price = row.last_price;
      row.regular_timestamp = payload.last_update_utc;
      row.regular_source = payload.price_source;
      row.regular_is_live = false;
      row.regular_gain_pct = row.gain_pct;
      row.extended_price = null;
      row.extended_timestamp = null;
      row.extended_session = null;
      row.extended_trading_date = null;
      row.extended_stale = false;
      row.price_timestamp = payload.last_update_utc;
      row.price_session = "snapshot";
      row.price_trading_date = payload.data_date;
      row.price_stale = false;
      const trackedRows = state.rowsByTicker.get(row.ticker) || [];
      trackedRows.push(row);
      state.rowsByTicker.set(row.ticker, trackedRows);
  });
}

fetch(`/api/state?t=${Date.now()}`, { cache: "no-store" })
  .then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  })
  .then((bundle) => {
    const payload = bundle.snapshot;
    const todayPayload = bundle.today;
    if (!payload?.rows || !todayPayload?.rows) throw new Error("Invalid state payload");
    state.payload = payload;
    state.todayPayload = todayPayload;
    state.cache = bundle.cache || null;
    initializeRows(payload, "yesterday");
    initializeRows(todayPayload, "today");
    populateSectors(activePayload().rows);
    render();
    if (state.cache?.status === "stale") {
      setConnectionState("stale", "Stale snapshot");
    }
    connectLivePrices();
  })
  .catch(() => {
    Object.values(elements.tables).forEach((table) => {
      table.empty.hidden = false;
      table.empty.textContent = "Dashboard state is unavailable.";
    });
    setConnectionState("stale", "Snapshot error");
  });
