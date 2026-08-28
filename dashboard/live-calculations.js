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

  return Object.freeze({
    calculateLiveMetrics,
    calculatePriceChange,
    isCurrentAnchor,
    isNewerPrice,
    isPriceForTradingDate,
    sessionLabel,
  });
});
