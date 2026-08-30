(function attachSigmaMath(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SigmaMath = api;
})(typeof window !== "undefined" ? window : globalThis, function createSigmaMath() {
  "use strict";

  function calculateLiveMetrics(price, anchorPrice, sigmaOneDayPct) {
    const current = Number(price);
    const anchor = Number(anchorPrice);
    const sigma = Number(sigmaOneDayPct) / 100;
    if (!(current > 0) || !(anchor > 0) || !(sigma > 0)) return null;

    const logReturn = Math.log(current / anchor);
    return {
      gainPct: ((current / anchor) - 1) * 100,
      logReturnPct: logReturn * 100,
      zScore: logReturn / sigma,
    };
  }

  function calculatePriceChange(price, referencePrice) {
    const current = Number(price);
    const reference = Number(referencePrice);
    if (!(current > 0) || !(reference > 0)) return null;
    return {
      absolute: current - reference,
      percent: ((current / reference) - 1) * 100,
    };
  }

  function sessionLabel(session) {
    return ({
      regular: "Regular",
      premarket: "Pre-market",
      afterhours: "After-hours",
      overnight: "Overnight",
      closed: "Closed",
      snapshot: "Snapshot",
    })[session] || "Unknown";
  }

  function isNewerPrice(incomingTimestamp, currentTimestamp) {
    const incoming = Date.parse(incomingTimestamp || "");
    const current = Date.parse(currentTimestamp || "");
    if (!Number.isFinite(incoming)) return false;
    return !Number.isFinite(current) || incoming > current;
  }

  function isPriceForTradingDate(priceTradingDate, activeTradingDate) {
    return Boolean(
      priceTradingDate
      && activeTradingDate
      && priceTradingDate === activeTradingDate
    );
  }

  function isCurrentAnchor(view, payloadAnchorDate, targetCompletedSession) {
    return view !== "today"
      || !targetCompletedSession
      || payloadAnchorDate === targetCompletedSession;
  }

  function streamSequenceDecision(lastSequence, messageType, incomingSequence) {
    const parsedCurrent = Number(lastSequence);
    const current = lastSequence !== null
      && lastSequence !== undefined
      && Number.isInteger(parsedCurrent)
      ? parsedCurrent
      : null;
    const incoming = Number(incomingSequence);
    if (!Number.isInteger(incoming) || incoming < 0) {
      return { action: "resync", sequence: current };
    }
    if (messageType === "snapshot") {
      return { action: "accept", sequence: incoming };
    }
    if (messageType === "status") {
      if (current === null || incoming > current) {
        return { action: "resync", sequence: current };
      }
      return {
        action: incoming === current ? "accept" : "ignore",
        sequence: current,
      };
    }
    if (messageType !== "delta" || current === null || incoming > current + 1) {
      return { action: "resync", sequence: current };
    }
    return {
      action: incoming === current + 1 ? "accept" : "ignore",
      sequence: incoming === current + 1 ? incoming : current,
    };
  }

  function paginateRows(rows, requestedPage, requestedPageSize) {
    const values = Array.isArray(rows) ? rows : [];
    const pageSize = Math.max(1, Math.floor(Number(requestedPageSize)) || 20);
    const totalPages = Math.max(1, Math.ceil(values.length / pageSize));
    const page = Math.min(totalPages, Math.max(1, Math.floor(Number(requestedPage)) || 1));
    const offset = (page - 1) * pageSize;
    const items = values.slice(offset, offset + pageSize);
    return {
      end: offset + items.length,
      items,
      offset,
      page,
      pageSize,
      start: items.length ? offset + 1 : 0,
      total: values.length,
      totalPages,
    };
  }

  return Object.freeze({
    calculateLiveMetrics,
    calculatePriceChange,
    isCurrentAnchor,
    isNewerPrice,
    isPriceForTradingDate,
    paginateRows,
    sessionLabel,
    streamSequenceDecision,
  });
});
