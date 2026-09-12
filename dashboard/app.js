const state = {
  payload: null,
  todayPayload: null,
  cache: null,
  refreshPlan: null,
  rowsByTicker: new Map(),
  view: "today",
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
    sequence: null,
    points: new Map(),
    ivExpansions: new Map(),
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
      horizonLine: document.querySelector("#sigma-horizon-yesterday"),
    },
    today: {
      panel: document.querySelector("#panel-today"),
      rows: document.querySelector("#mover-rows-today"),
      empty: document.querySelector("#empty-state-today"),
      resultCount: document.querySelector("#result-count-today"),
      methodLine: document.querySelector("#method-line-today"),
      horizonLine: document.querySelector("#sigma-horizon-today"),
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

function formatOptionTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

function formatMarketMoment(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
    timeZone: "America/New_York",
  }).format(new Date(value));
}

function formatHorizonDays(value) {
  const days = Number(value);
  if (!Number.isFinite(days)) return "unknown horizon";
  const rounded = Math.round(days * 100) / 100;
  const digits = Number.isInteger(rounded) ? 0 : 2;
  return `${formatNumber(rounded, digits)} calendar ${rounded === 1 ? "day" : "days"}`;
}

function calendarDateDifference(startValue, endValue) {
  const startDate = String(startValue || "").slice(0, 10);
  const endDate = String(endValue || "").slice(0, 10);
  const start = Date.parse(`${startDate}T00:00:00Z`);
  const end = Date.parse(`${endDate}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return (end - start) / (24 * 60 * 60 * 1000);
}

function sigmaContext(row) {
  let annualizedIvPct = Number(row.annualized_implied_vol_pct);
  let horizon = window.SigmaMath.calculateSigmaHorizon(
    row.sigma_1d_pct,
    annualizedIvPct,
  );
  if (!horizon) {
    const calendarDays = calendarDateDifference(
      row.iv_snapshot_time,
      row.payload_data_date,
    );
    if (calendarDays) {
      annualizedIvPct = Number(row.sigma_1d_pct) / Math.sqrt(calendarDays / 365);
      horizon = window.SigmaMath.calculateSigmaHorizon(
        row.sigma_1d_pct,
        annualizedIvPct,
      );
    }
  }
  const end = window.SigmaMath.sigmaHorizonEnd(row.iv_snapshot_time, horizon);
  return horizon && end ? { horizon, end, annualizedIvPct } : null;
}

function expectedSigmaTooltip(row) {
  const pending = row.expected_sigma_status === "awaiting_previous_close"
    ? `Awaiting ${row.expected_sigma_target_iv_session} closing IV. Showing the previous available sigma. ` : "";
  const context = sigmaContext(row);
  if (!context) {
    return pending + "Option-implied expected move for the stored close-to-close horizon.";
  }
  const days = Math.round(context.horizon.calendarDays * 100) / 100;
  return pending + [
    `Option IV: ${formatNumber(context.annualizedIvPct)}% annualized at ${formatMarketMoment(row.iv_snapshot_time)}.`,
    `Expected sigma: ${formatNumber(context.annualizedIvPct)}% x sqrt(${formatNumber(days, Number.isInteger(days) ? 0 : 2)}/365) = ${formatNumber(row.sigma_1d_pct)}%.`,
    `Horizon: ${formatMarketMoment(row.iv_snapshot_time)} to ${formatMarketMoment(context.end)}.`,
    `Expiry: ${formatDate(row.option_expiry)}. IV Reliability: ${row.iv_reliability}.`,
  ].join(" ");
}

function expectedSigmaNote(payload) {
  const row = payload?.rows?.find((candidate) => sigmaContext(candidate));
  const context = row ? sigmaContext(row) : null;
  if (!row || !context) {
    return `IV as of ${formatUpdate(payload?.iv_snapshot_time)}`;
  }
  return `IV as of ${formatMarketMoment(row.iv_snapshot_time)} · Expected-move horizon to ${formatMarketMoment(context.end)} (${formatHorizonDays(context.horizon.calendarDays)})`;
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
  return window.SigmaMath.primaryGainPct(view, row, state.live.marketSession);
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
      if (view === "yesterday") {
        return Number(a.original_rank) - Number(b.original_rank);
      }
      if (view === "today") {
        const premarketPriority = window.SigmaMath.premarketTradePriority(
          b,
          state.live.marketSession,
          state.live.dataDate,
        ) - window.SigmaMath.premarketTradePriority(
          a,
          state.live.marketSession,
          state.live.dataDate,
        );
        if (premarketPriority) return premarketPriority;
      }
      const sortValue = (row) => {
        if (settings.sortKey === "gain_pct") {
          return displayGainPct(row, view);
        }
        if (settings.sortKey === "abs_z_score") return row.z_score == null ? Number.NaN : Math.abs(Number(row.z_score));
        if (settings.sortKey === "z_score" && row.z_score == null) return Number.NaN;
        if (settings.sortKey === "iv_expansion_pct") {
          if (row.iv_expansion_pct == null) return Number.NaN;
          return Number(row.iv_expansion_pct);
        }
        return Number(row[settings.sortKey]);
      };
      const aValue = sortValue(a);
      const bValue = sortValue(b);
      const aMissing = !Number.isFinite(aValue);
      const bMissing = !Number.isFinite(bValue);
      if (aMissing !== bMissing) return aMissing ? 1 : -1;
      const primary = (aValue - bValue) * direction;
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
  const displaysCompletedSession = !row.reopening_overnight && window.SigmaMath.displaysCompletedSession(
    state.live.marketSession,
    row.holding_completed_close,
  );
  const regularLabel = `${
    displaysCompletedSession ? "Last close" : row.regular_is_live ? "Regular" : "Prior close"
  }${regularTime ? ` · ${regularTime}` : ""}`;

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
  const regularGain = window.SigmaMath.primaryGainPct(
    row.dashboard_view,
    row,
    state.live.marketSession,
  );
  const hasRegularGain = Number.isFinite(regularGain);
  const regularDirection = hasRegularGain && regularGain >= 0 ? "positive" : "negative";
  const displaysCompletedSession = !row.reopening_overnight && window.SigmaMath.displaysCompletedSession(
    state.live.marketSession,
    row.holding_completed_close,
  );
  const regularLabel = isSavedList
    ? window.SigmaMath.isLiveGainSession(state.live.marketSession)
      ? row.regular_is_live ? "Today's move" : "Awaiting trade"
      : "Saved gain"
    : displaysCompletedSession || row.regular_is_live ? "Today's move" : "Prior day";
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
        <span class="number movement ${hasRegularGain ? regularDirection : ""}">${hasRegularGain ? formatSigned(regularGain, 2, "%") : "--"}</span>
        <span class="gain-session regular">${regularLabel}</span>
      </span>
      ${extendedMarkup}
    </span>`;
}

function ivExpansionMarkup(row) {
  if (row.dashboard_view !== "today") return "";
  const isPremarket = state.live.marketSession === "premarket";
  const rawValue = isPremarket ? null : row.iv_expansion_pct;
  const value = Number(rawValue);
  const hasValue = rawValue !== null && rawValue !== undefined && Number.isFinite(value);
  const bucketScore = isPremarket ? null : row.iv_expansion_bucket_score;
  const z = bucketScore?.z;
  const scored = hasValue && typeof z === "number" && Number.isFinite(z);
  const scoreLabel = scored ? formatSigned(z, 1, "σ") : "—";
  const availabilityTip = "IV Expansion updates only during options trading hours. Readings are hidden during pre-market. The last available reading is shown after the market closes.";
  const scoreTip = bucketScore
    ? `${bucketScore.bucket || "No matched"} ET bucket · ${bucketScore.n || 0} historical observations. ${bucketScore.reason || ""} Calibration: ${bucketScore.calibration_date || "unknown"}. ${bucketScore.indicative ? "Indicative: within-bucket quote compared with historical bucket-end samples." : ""} ${bucketScore.is_stale ? "Saved/stale quote; bucket follows the observation timestamp, not the current clock." : ""} ${bucketScore.provisional ? "Provisional: low quote reliability." : ""}`
    : hasValue
      ? "Bucket σ is not yet available for this reading. It requires matching historical observations."
      : availabilityTip + " Bucket σ appears once an IV reading and matching history are available.";
  let bucketLabel = "";
  if (bucketScore?.bucket && /^\d{2}:\d{2}$/.test(bucketScore.bucket)) {
    const [hour, minute] = bucketScore.bucket.split(":").map(Number);
    const end = hour * 60 + minute + 30;
    bucketLabel = `${bucketScore.bucket}–${String(Math.floor(end / 60)).padStart(2, "0")}:${String(end % 60).padStart(2, "0")} ET`;
  }
  const status = {
    live: "Live",
    last_quote: "Last quote",
    close: "Close",
  }[row.iv_expansion_status] || "Close";
  const timestamp = row.iv_expansion_timestamp ? new Intl.DateTimeFormat("en-GB", {hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "America/New_York"}).format(new Date(row.iv_expansion_timestamp)) : "--";
  const referenceStrikes = (row.iv_expansion_reference_strikes || [])
    .map((strike) => formatNumber(strike, Number.isInteger(Number(strike)) ? 0 : 2))
    .join(", ");
  const liveStrikes = (row.iv_expansion_live_strikes || [])
    .map((strike) => formatNumber(strike, Number.isInteger(Number(strike)) ? 0 : 2))
    .join(", ");
  const fallbackSessions = Number(row.iv_expansion_fallback_sessions) || 0;
  const referenceLabel = row.iv_expansion_reference_status === "fallback"
    ? "Fallback " + (row.iv_expansion_reference_session || "snapshot")
    : "Yesterday";
  const provenance = fallbackSessions > 0
    ? "Fallback " + fallbackSessions + (fallbackSessions === 1 ? " session" : " sessions")
    : status;
  const methodology = hasValue
    ? referenceLabel + " " + formatNumber(row.iv_expansion_reference_iv_pct, 2)
      + "% IV at strikes " + (referenceStrikes || "--") + ". " + status + " "
      + formatNumber(row.iv_expansion_live_iv_pct, 2)
      + "% IV at moneyness-matched strikes " + (liveStrikes || "--") + "."
      + (row.iv_expansion_observation_session
        ? " Observation session " + row.iv_expansion_observation_session + "."
        : "")
    : availabilityTip + " Waiting for fresh same-expiry call and put quotes.";

  return '<td class="iv-expansion-cell ' + (hasValue ? "" : "waiting")
    + '" title="' + escapeHtml(methodology) + '">'
    + '<span class="iv-expansion-primary"><span class="iv-expansion-value">'
    + (hasValue ? formatSigned(value, 1, "%") : "--") + '</span>'
    + '<span class="iv-bucket-badge term-tip ' + (scored ? "scored" : "")
    + '" tabindex="0" aria-label="' + (scored ? "Bucket sigma " + scoreLabel : "Bucket sigma unavailable") + '" data-tooltip="' + escapeHtml(scoreTip) + '">'
    + (scored && bucketScore.indicative ? "≈" : "") + scoreLabel + (scored && bucketScore.provisional ? "*" : "") + '</span>'
    + '</span>'
    + '<span class="iv-expansion-meta">'
    + (hasValue ? escapeHtml([provenance, bucketScore?.is_stale ? "STALE" : "", bucketScore?.observation_date || "", bucketLabel, "mark " + timestamp].filter(Boolean).join(" · ")) : state.live.marketSession === "regular" ? "Awaiting options quotes" : "Options trading hours only") + "</span></td>";
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
      <td><span class="number term-tip sigma-value" tabindex="0" data-tooltip="${escapeHtml(expectedSigmaTooltip(row))}">${formatNumber(row.sigma_1d_pct)}%</span>${row.expected_sigma_status === "awaiting_previous_close" ? '<small class="sigma-pending">Awaiting previous-close IV</small>' : ''}</td>
      <td>
        <div class="z-cell">
          <span class="z-value ${direction}">${row.z_score == null ? "—" : formatNumber(row.z_score, 3)}</span>
          <span class="z-track"><span class="z-bar ${direction}" style="width:${width}%"></span></span>
        </div>
      </td>
      ${ivExpansionMarkup(row)}
      <td><span class="reliability ${reliability}">${reliability}</span></td>
    </tr>`;
}

function sortLabel(view = state.view) {
  if (view === "yesterday") return "saved previous-session rank";
  const settings = state.viewSettings[view];
  const labels = {
    original_rank: "saved rank",
    abs_z_score: "absolute Z-score",
    z_score: "Z-score",
    gain_pct: "gain",
    sigma_1d_pct: "expected sigma",
    market_cap_usd: "market cap",
    iv_expansion_pct: "IV expansion",
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
  const premarketTradeCount = view === "today"
    ? payload.rows.filter((row) => window.SigmaMath.premarketTradePriority(
      row,
      state.live.marketSession,
      state.live.dataDate,
    )).length
    : 0;

  table.rows.innerHTML = rows.map((row, index) => rowMarkup(
    row,
    view === "yesterday"
      ? row.original_rank
      : pagination.offset + index + 1,
  )).join("");
  table.empty.hidden = rows.length > 0;
  table.resultCount.textContent = view === "today"
    ? `${pagination.start}-${pagination.end} of ${pagination.total}`
    : `${matchingRows.length} of ${payload.rows.length}`;
  table.methodLine.textContent = view === "today"
    ? premarketTradeCount > 0
      ? `${premarketTradeCount} pre-market trades first · ranked within each group by ${sortLabel(view)}`
      : observedRows > 0
      ? `${observedRows} of ${payload.rows.length} current-session prices · ranked by ${sortLabel(view)}`
      : `No current-session prices yet · showing ${formatDate(payload.data_date)} closing snapshot · ranked by ${sortLabel(view)}`
    : `Fixed 20-name list · fixed in ${sortLabel(view)} order`;
  table.horizonLine.textContent = expectedSigmaNote(payload);
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

  elements.topSignal.textContent = top && top.z_score != null ? `${formatNumber(top.z_score, 3)} z` : "--";
  elements.topSignal.dataset.direction = top && top.z_score < 0 ? "negative" : "positive";
  elements.topTicker.textContent = top?.ticker || "No matching movers";
  elements.outsideCount.textContent = outsideCount;
  elements.coverageCount.textContent = payload.rows.length;
  elements.reliabilityMix.textContent = `${mix.high || 0} high / ${mix.medium || 0} medium / ${mix.low || 0} low`;
  elements.sessionDate.textContent = formatDate(state.live.dataDate || payload.data_date);
  elements.lastUpdate.textContent = formatUpdate(liveTimestamp || payload.last_update_utc);
  elements.anchorNote.textContent = state.view === "today"
    ? `Today's return anchor: ${formatDate(payload.rows.find(row => row.return_anchor_date)?.return_anchor_date || payload.anchor_date)} close`
    : `Saved screen anchor: ${formatDate(payload.anchor_date)} close`;
  elements.ivNote.textContent = `${expectedSigmaNote(payload)} · Expiry ${formatDate(payload.option_expiry)}`;
  document.querySelectorAll(".sort-button").forEach((button) => {
    const buttonView = button.closest(".table-panel")?.id === "panel-today" ? "today" : "yesterday";
    const settings = state.viewSettings[buttonView];
    button.classList.toggle("active", button.dataset.sort === settings.sortKey);
    button.dataset.direction = button.dataset.sort === settings.sortKey ? settings.sortDirection : "";
  });
  document.querySelectorAll(".rank-button").forEach((button) => {
    button.hidden = button.dataset.sort === "original_rank"
      ? state.view !== "yesterday"
      : state.view !== "today";
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
  if (state.view === "yesterday") return;
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
  const canCarryCompletedClose = window.SigmaMath.isPostCloseSession(
    state.live.marketSession,
  ) && Number(point.regular_close?.price) > 0;
  if (!window.SigmaMath.isPriceForTradingDate(point.trading_date, tradingDate)
    && !canCarryCompletedClose) return false;
  const trackedRows = state.rowsByTicker.get(point.ticker) || [];
  if (!trackedRows.length) return false;
  return trackedRows.reduce(
    (changed, row) => applyLivePriceToRow(row, point) || changed,
    false,
  );
}

function applyIvExpansion(point) {
  if (!point || !point.ticker) return false;
  state.live.ivExpansions.set(point.ticker, point);
  const row = (state.rowsByTicker.get(point.ticker) || [])
    .find((candidate) => candidate.dashboard_view === "today");
  if (!row) return false;
  row.iv_expansion_pct = Number(point.iv_expansion_pct);
  row.iv_expansion_level = Number(point.iv_expansion_level);
  row.iv_expansion_bucket_score = point.iv_expansion_bucket_score || null;
  row.iv_expansion_status = point.status;
  row.iv_expansion_timestamp = point.last_valid_at;
  row.iv_expansion_reference_iv_pct = Number(point.reference_annualized_iv_pct);
  row.iv_expansion_live_iv_pct = Number(point.live_annualized_iv_pct);
  row.iv_expansion_reference_strikes = point.reference_strikes || [];
  row.iv_expansion_live_strikes = point.live_strikes || [];
  row.iv_expansion_moneyness_match_error = Number(point.moneyness_match_max_error);
  row.iv_expansion_reference_session = point.iv_reference_session || null;
  row.iv_expansion_observation_session = point.iv_observation_session || null;
  row.iv_expansion_reference_status = point.iv_reference_status || "yesterday";
  row.iv_expansion_fallback_sessions = Number(point.fallback_sessions) || 0;
  row.iv_expansion_baseline_revision = Number(point.baseline_revision) || 1;
  return true;
}

function applyLivePriceToRow(row, point) {
  const regularClose = point.regular_close;
  const carriesCompletedClose = window.SigmaMath.isCompletedSessionCarry(
    row.dashboard_view,
    row.payload_anchor_date,
    state.cache?.target_completed_session,
    state.live.marketSession,
    regularClose?.trading_date,
  );
  const stalePremarketAnchor = state.live.marketSession === "premarket"
    && !row.return_anchor_price
    && regularClose?.trading_date
    && regularClose.trading_date !== row.payload_anchor_date;
  const reopeningOvernight = window.SigmaMath.isReopeningOvernight(
    row.dashboard_view, row.payload_anchor_date, state.refreshPlan?.next_session,
    state.live.marketSession, point,
  );
  // Ignore stale or previous-session ticks once this reopening is live.
  if (row.reopening_overnight && state.live.marketSession === "overnight"
      && !reopeningOvernight) return false;
  const usesTodayCompletedClose = row.dashboard_view === "today" && (
      (window.SigmaMath.isPostCloseSession(state.live.marketSession) && !reopeningOvernight)
      || stalePremarketAnchor
  );
  const usesSavedListCompletedClose = row.dashboard_view === "yesterday"
    && window.SigmaMath.isPostCloseSession(state.live.marketSession);
  const usesCompletedClose = Number(regularClose?.price) > 0
    && (usesTodayCompletedClose || usesSavedListCompletedClose);
  if (!window.SigmaMath.isCurrentAnchor(
    row.dashboard_view,
    row.payload_anchor_date,
    state.cache?.target_completed_session,
  ) && !carriesCompletedClose) return false;
  if (point.ts_event === row.price_timestamp) {
    row.price_stale = Boolean(point.is_stale);
    if (point.session === row.extended_session) {
      row.extended_stale = Boolean(point.is_stale);
    }
    return false;
  }
  if (!window.SigmaMath.isNewerPrice(point.ts_event, row.price_timestamp)) return false;
  const anchor = row.live_anchor_price || row.anchor_price || row.snapshot_last_price || row.last_price;
  const metricPrice = usesCompletedClose ? regularClose.price : point.price;
  const metrics = window.SigmaMath.calculateLiveMetrics(metricPrice, anchor, row.sigma_1d_pct);
  if (!metrics) return false;

  row.reopening_overnight = reopeningOvernight;
  row.last_price = Number(metricPrice);
  row.gain_pct = metrics.gainPct;
  row.log_return_pct = metrics.logReturnPct;
  row.z_score = metrics.zScore;
  row.outside_one_sigma = metrics.zScore > 1;
  row.price_timestamp = point.ts_event;
  row.price_source = point.source_dataset;
  row.price_session = point.session;
  row.price_trading_date = point.trading_date;
  row.price_stale = Boolean(point.is_stale);
  row.holding_completed_close = usesCompletedClose;
  if (point.session === "regular" || usesCompletedClose) {
    row.regular_price = Number(usesCompletedClose ? regularClose.price : point.price);
    row.regular_timestamp = usesCompletedClose ? regularClose.ts_event : point.ts_event;
    row.regular_source = usesCompletedClose
      ? regularClose.source_dataset
      : point.source_dataset;
    row.regular_trading_date = usesCompletedClose
      ? regularClose.trading_date
      : point.trading_date;
    row.regular_is_live = true;
    row.regular_gain_pct = metrics.gainPct;
    if (point.session === "regular") {
      row.extended_price = null;
      row.extended_timestamp = null;
      row.extended_session = null;
      row.extended_stale = false;
    }
  }
  if (["premarket", "afterhours", "overnight"].includes(point.session)) {
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
  const decision = window.SigmaMath.streamSequenceDecision(
    state.live.sequence,
    message.type,
    message.sequence,
  );
  if (decision.action === "ignore") return;
  if (decision.action === "resync") {
    setConnectionState("stale", "Resyncing");
    state.live.socket?.close(4001, "Price sequence gap");
    return;
  }
  state.live.sequence = decision.sequence;
  state.live.lastMessageAt = Date.now();
  const previousMarketSession = state.live.marketSession;
  state.live.marketSession = message.market_session || state.live.marketSession;
  if (previousMarketSession !== "regular" && state.live.marketSession === "regular") {
    allRows().forEach((row) => {
      if (row.regular_trading_date !== message.trading_date) row.regular_is_live = false;
    });
  }
  if (previousMarketSession !== "premarket" && state.live.marketSession === "premarket") {
    state.viewSettings.today.page = 1;
  }
  state.live.dataDate = message.trading_date || state.live.dataDate;
  const priceChanges = message.prices.reduce((count, point) => {
    state.live.points.set(point.ticker, point);
    return count + Number(applyLivePrice(point, message.trading_date));
  }, 0);
  const ivChanges = (message.iv_expansions || []).reduce(
    (count, point) => count + Number(applyIvExpansion(point)),
    0,
  );
  const changed = priceChanges + ivChanges;
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
    state.live.sequence = null;
    setConnectionState("connecting", "Syncing");
    refreshDashboardState().catch(() => {});
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
    state.live.socket = null;
    state.live.connected = false;
    setConnectionState(state.live.pricesReceived ? "stale" : "snapshot", state.live.pricesReceived ? "Stale prices" : "Snapshot");
    if (document.visibilityState === "visible") scheduleReconnect();
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
  if (document.visibilityState === "visible") {
    refreshDashboardState().catch(() => {});
    if (!state.live.connected) connectLivePrices();
  } else {
    window.clearTimeout(state.live.retryTimer);
    state.live.retryTimer = null;
    state.live.socket?.close(1000, "Page hidden");
  }
});

window.setInterval(() => {
  if (document.visibilityState === "visible") refreshDashboardState().catch(() => {});
}, 60000);

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
      row.payload_data_date = payload.data_date;
      row.iv_snapshot_time = row.iv_snapshot_time || payload.iv_snapshot_time;
      row.option_expiry = row.option_expiry || payload.option_expiry;
      row.snapshot_last_price = row.last_price;
      row.snapshot_gain_pct = row.gain_pct;
      row.saved_gain_pct = row.gain_pct;
      row.regular_price = row.last_price;
      row.regular_timestamp = payload.last_update_utc;
      row.regular_source = payload.price_source;
      row.regular_trading_date = payload.data_date;
      row.regular_is_live = false;
      if (Number(row.return_anchor_price) > 0) {
        row.payload_anchor_date = row.return_anchor_date;
        row.live_anchor_price = Number(row.return_anchor_price);
        row.regular_price = Number(row.return_anchor_price);
        row.regular_timestamp = row.return_anchor_timestamp;
        row.regular_trading_date = row.return_anchor_date;
        row.regular_source = "Verified session close";
        row.last_price = Number(row.return_anchor_price);
        row.z_score = null;
      }
      row.holding_completed_close = false;
      row.reopening_overnight = false;
      row.regular_gain_pct = window.SigmaMath.primaryGainPct(
        view,
        row,
        state.live.marketSession,
      );
      row.extended_price = null;
      row.extended_timestamp = null;
      row.extended_session = null;
      row.extended_trading_date = null;
      row.extended_stale = false;
      row.price_timestamp = row.return_anchor_timestamp || payload.last_update_utc;
      row.price_session = "snapshot";
      row.price_trading_date = payload.data_date;
      row.price_stale = false;
      row.iv_expansion_pct = null;
      row.iv_expansion_level = 0;
      row.iv_expansion_bucket_score = null;
      row.iv_expansion_status = "waiting";
      row.iv_expansion_timestamp = null;
      row.iv_expansion_reference_iv_pct = null;
      row.iv_expansion_live_iv_pct = null;
      row.iv_expansion_reference_strikes = [];
      row.iv_expansion_live_strikes = [];
      row.iv_expansion_reference_session = null;
      row.iv_expansion_observation_session = null;
      row.iv_expansion_reference_status = null;
      row.iv_expansion_fallback_sessions = 0;
      row.iv_expansion_baseline_revision = null;
  });
}

function applyDashboardState(bundle, changed) {
  const initial = !state.payload;
  state.cache = bundle.cache || null;
  state.refreshPlan = bundle.refresh_plan;
  if (changed.yesterday) {
    state.payload = bundle.snapshot;
    initializeRows(state.payload, "yesterday");
  }
  if (changed.today) {
    state.live.ivExpansions.clear();
    state.todayPayload = bundle.today;
    initializeRows(state.todayPayload, "today");
  }
  state.rowsByTicker.clear();
  allRows().forEach((row) => {
    const trackedRows = state.rowsByTicker.get(row.ticker) || [];
    trackedRows.push(row);
    state.rowsByTicker.set(row.ticker, trackedRows);
  });
  // A WebSocket snapshot may arrive before the new saved list finishes loading.
  for (const row of allRows()) {
    if (!changed[row.dashboard_view]) continue;
    const point = state.live.points.get(row.ticker);
    if (point && window.SigmaMath.isPriceForTradingDate(point.trading_date, state.live.dataDate)) {
      applyLivePriceToRow(row, point);
    }
    if (row.dashboard_view === "today") {
      const ivExpansion = state.live.ivExpansions.get(row.ticker);
      if (ivExpansion) applyIvExpansion(ivExpansion);
    }
  }
  if (changed[state.view]) {
    populateSectors(activePayload().rows);
    if (state.sector !== "all" && !activePayload().rows.some((row) => row.sector === state.sector)) {
      state.sector = "all";
    }
    elements.sector.value = state.sector;
  }
  render();
  if (initial && state.cache?.status === "stale") {
    setConnectionState("stale", "Stale snapshot");
  }
  if (initial) connectLivePrices();
}

const refreshDashboardState = window.SigmaStateRefresh.createLoader(
  (...args) => fetch(...args), applyDashboardState,
);

refreshDashboardState()
  .catch(() => {
    Object.values(elements.tables).forEach((table) => {
      table.empty.hidden = false;
      table.empty.textContent = "Dashboard state is unavailable.";
    });
    setConnectionState("stale", "Snapshot error");
  });
