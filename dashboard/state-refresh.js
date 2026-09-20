(function attachStateRefresh(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.SigmaStateRefresh = api;
})(typeof window !== "undefined" ? window : globalThis, function createStateRefresh() {
  "use strict";

  function createLoader(fetchState, applyState) {
    let etag = null;
    let snapshotVersion = null;
    let todayVersion = null;
    let pending = null;
    return function load() {
      if (pending) return pending;
      pending = (async () => {
        const headers = etag ? { "If-None-Match": etag } : {};
        const response = await fetchState("/api/state", { cache: "no-store", headers });
        if (response.status === 304) return false;
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const bundle = await response.json();
        if (!Array.isArray(bundle.snapshot?.rows) || !bundle.today?.rows?.length) {
          throw new Error("Invalid dashboard state");
        }
        // Capture versions before the UI enriches the rows with live prices.
        const nextSnapshot = JSON.stringify(bundle.snapshot);
        const nextToday = JSON.stringify(bundle.today);
        await applyState(bundle, {
          yesterday: nextSnapshot !== snapshotVersion,
          today: nextToday !== todayVersion,
        });
        snapshotVersion = nextSnapshot;
        todayVersion = nextToday;
        etag = response.headers.get("ETag");
        return true;
      })().finally(() => { pending = null; });
      return pending;
    };
  }

  return { createLoader };
});
