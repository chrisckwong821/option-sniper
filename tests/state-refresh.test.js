const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createLoader } = require("../dashboard/state-refresh.js");

function bundle(date = "2026-08-26", ticker = "OLD") {
  const payload = (dataDate, symbol) => ({
    data_date: dataDate,
    anchor_date: "2026-08-25",
    last_update_utc: `${dataDate}T20:00:00+00:00`,
    rows: [{ ticker: symbol, sector: "Technology", last_price: 102, anchor_price: 100,
      gain_pct: 2, z_score: 1, sigma_1d_pct: 2 }],
  });
  return {
    snapshot: payload(date, ticker),
    today: payload("2026-08-26", "LIVE"),
    cache: { target_completed_session: "2026-08-26" },
  };
}

function response(data, etag = '"v1"', status = 200) {
  return { status, ok: status === 200, headers: { get: () => etag },
    json: async () => structuredClone(data) };
}

test("conditional requests preserve today's live state when only yesterday changes", async () => {
  const responses = [response(bundle()), response(null, '"v1"', 304),
    response(bundle("2026-08-27", "NEW"), '"v2"')];
  const requests = [];
  const changes = [];
  const load = createLoader(async (url, options) => {
    requests.push({ url, ...options });
    return responses.shift();
  }, (state, changed) => {
    changes.push(changed);
    state.today.rows[0].last_price = 500; // UI mutations must not change the saved version.
  });
  assert.equal(await load(), true);
  assert.equal(await load(), false);
  assert.equal(await load(), true);
  assert.deepEqual(changes, [{ yesterday: true, today: true }, { yesterday: true, today: false }]);
  assert.equal(requests[1].headers["If-None-Match"], '"v1"');
  assert.equal(requests[2].cache, "no-store");
});

test("failed or malformed responses keep the last version and allow retries", async () => {
  const responses = [response(bundle()), response(null, '"bad"', 503),
    response({ snapshot: { rows: [] }, today: { rows: [] } }, '"bad"'),
    response(bundle("2026-08-27", "NEW"), '"v2"')];
  let applied = 0;
  const requests = [];
  const load = createLoader(async (_, options) => {
    requests.push(options);
    return responses.shift();
  }, () => { applied += 1; });
  await load();
  await assert.rejects(load(), /HTTP 503/);
  await assert.rejects(load(), /Invalid dashboard state/);
  assert.equal(applied, 1);
  await load();
  assert.equal(applied, 2);
  assert.equal(requests[3].headers["If-None-Match"], '"v1"');
});

test("overlapping checks share a request", async () => {
  let resolve;
  let requests = 0;
  const load = createLoader(() => {
    requests += 1;
    return new Promise((done) => { resolve = done; });
  }, () => {});
  const first = load();
  assert.equal(load(), first);
  resolve(response(bundle()));
  await first;
  assert.equal(requests, 1);
});

test("the open dashboard replaces yesterday rows while preserving today's objects and controls", async () => {
  const intervals = [];
  const events = {};
  const responses = [response(bundle())];
  const element = () => ({ dataset: {}, classList: { toggle() {} },
    addEventListener() {}, querySelectorAll: () => [], setAttribute() {} });
  const document = { visibilityState: "visible", querySelector: element,
    querySelectorAll: () => [], addEventListener: (name, fn) => { events[name] = fn; } };
  const context = vm.createContext({
    document,
    fetch: async () => responses.shift() || response(null, '"v2"', 304),
    WebSocket: class { addEventListener() {} close() {} },
    requestAnimationFrame: (fn) => fn(),
    window: {
      SigmaMath: require("../dashboard/live-calculations.js"),
      SigmaStateRefresh: { createLoader }, location: { protocol: "http:", host: "localhost" },
      setInterval: (fn, ms) => intervals.push({ fn, ms }),
      setTimeout() {}, clearTimeout() {}, addEventListener() {},
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8"), context);
  // Layout is outside this regression; run the actual state and price update functions.
  vm.runInContext("render = () => {};", context);
  await vm.runInContext("refreshDashboardState()", context);
  vm.runInContext(`
    state.payload.rows = [
      { ticker: "SECOND", company_name: "Second", sector: "Technology",
        iv_reliability: "high", original_rank: 2, gain_pct: 50, z_score: 9 },
      { ticker: "FIRST", company_name: "First", sector: "Technology",
        iv_reliability: "high", original_rank: 1, gain_pct: -5, z_score: -2 },
    ];
    state.view = "yesterday";
    setSort("gain_pct");
  `, context);
  assert.equal(
    vm.runInContext("state.viewSettings.yesterday.sortKey", context),
    "original_rank",
  );
  assert.equal(
    vm.runInContext("visibleRows('yesterday').map((row) => row.ticker).join(',')", context),
    "FIRST,SECOND",
  );
  vm.runInContext("state.view = 'today'; setSort('gain_pct');", context);
  assert.equal(
    vm.runInContext("state.viewSettings.today.sortKey", context),
    "gain_pct",
  );
  vm.runInContext(`
    state.view = "today";
    state.search = "live";
    state.sector = "Technology";
    state.viewSettings.today.page = 2;
    state.todayPayload.rows[0].last_price = 123;
    state.todayPayload.rows[0].z_score = 4.2;
  `, context);
  const today = vm.runInContext("state.todayPayload", context);
  const todayRow = today.rows[0];
  const before = JSON.stringify(today);
  const controls = vm.runInContext("JSON.stringify([state.view, state.search, state.sector, state.viewSettings])", context);

  responses.push(response(bundle("2026-08-27", "NEW"), '"v2"'));
  intervals.find(({ ms }) => ms === 60000).fn();
  await vm.runInContext("refreshDashboardState()", context);
  assert.equal(vm.runInContext("state.payload.rows[0].ticker", context), "NEW");
  assert.equal(vm.runInContext("state.todayPayload", context), today);
  assert.equal(vm.runInContext("state.todayPayload.rows[0]", context), todayRow);
  assert.equal(JSON.stringify(today), before);
  assert.equal(vm.runInContext("JSON.stringify([state.view, state.search, state.sector, state.viewSettings])", context), controls);
  assert.equal(vm.runInContext("state.rowsByTicker.has('OLD')", context), false);
  assert.equal(vm.runInContext("state.rowsByTicker.get('LIVE')[0]", context), todayRow);
  assert.equal(typeof events.visibilitychange, "function");

  // The existing pre-market job can still roll today's list normally.
  const premarket = bundle("2026-08-27", "NEW");
  premarket.today.anchor_date = "2026-08-27";
  premarket.today.rows[0].anchor_price = 110;
  responses.push(response(premarket, '"v3"'));
  await vm.runInContext("refreshDashboardState()", context);
  assert.equal(vm.runInContext("state.todayPayload.anchor_date", context), "2026-08-27");
  assert.equal(vm.runInContext("state.todayPayload.rows[0].anchor_price", context), 110);
});

test("reopening prices rank Today overnight without changing saved gainers or weekday carry", async () => {
  const element = () => ({ dataset: {}, classList: { toggle() {} },
    addEventListener() {}, querySelectorAll: () => [], setAttribute() {} });
  const initial = bundle("2026-09-04", "LIVE");
  initial.today.anchor_date = "2026-09-04";
  initial.cache.target_completed_session = "2026-09-04";
  initial.refresh_plan = { next_session: "2026-09-08" };
  const context = vm.createContext({
    document: { visibilityState: "visible", querySelector: element,
      querySelectorAll: () => [], addEventListener() {} },
    fetch: async () => response(initial),
    WebSocket: class { addEventListener() {} close() {} },
    requestAnimationFrame: () => {},
    window: { SigmaMath: require("../dashboard/live-calculations.js"),
      SigmaStateRefresh: { createLoader }, location: { protocol: "http:", host: "localhost" },
      setInterval() {}, setTimeout() {}, clearTimeout() {}, addEventListener() {} },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8"), context);
  vm.runInContext("render = () => {};", context);
  await vm.runInContext("refreshDashboardState()", context);
  vm.runInContext(`
    state.live.marketSession = "overnight";
    state.live.dataDate = "2026-09-08";
    globalThis.tick = { ticker: "LIVE", price: 105, session: "overnight",
      trading_date: "2026-09-08", ts_event: "2026-09-08T00:01:00Z", is_stale: false,
      regular_close: { price: 100, trading_date: "2026-09-04", ts_event: "2026-09-04T20:00:00Z" } };
    applyLivePrice(tick, "2026-09-08");
  `, context);
  assert.ok(Math.abs(vm.runInContext("state.todayPayload.rows[0].z_score", context) - Math.log(1.05) / .02) < 1e-10);
  assert.equal(vm.runInContext("state.todayPayload.rows[0].holding_completed_close", context), false);
  assert.equal(vm.runInContext("state.payload.rows[0].last_price", context), 100);
  vm.runInContext(`applyLivePrice({ ...tick, price: 120, is_stale: true, ts_event: "2026-09-08T00:02:00Z" }, "2026-09-08")`, context);
  assert.equal(vm.runInContext("state.todayPayload.rows[0].last_price", context), 105);
  vm.runInContext(`
    state.todayPayload.rows[0].reopening_overnight = false;
    state.todayPayload.rows[0].payload_anchor_date = "2026-09-07";
    state.cache.target_completed_session = "2026-09-07";
    applyLivePrice({ ...tick, ts_event: "2026-09-08T00:03:00Z" }, "2026-09-08");
  `, context);
  assert.equal(vm.runInContext("state.todayPayload.rows[0].last_price", context), 100);
});

test("late shared sigma updates both views and reapplies the latest live price", async () => {
  const element = () => ({ dataset: {}, classList: { toggle() {} },
    addEventListener() {}, querySelectorAll: () => [], setAttribute() {} });
  const initial = bundle('2026-09-08', 'LIVE');
  for (const view of ['snapshot', 'today']) {
    initial[view].data_date = '2026-09-08';
    initial[view].anchor_date = '2026-09-08';
    initial[view].last_update_utc = '2026-09-08T20:00:00Z';
  }
  initial.cache.target_completed_session = '2026-09-08';
  const responses = [response(initial)];
  const context = vm.createContext({
    document: { visibilityState: 'visible', querySelector: element,
      querySelectorAll: () => [], addEventListener() {} },
    fetch: async () => responses.shift(),
    WebSocket: class { addEventListener() {} close() {} },
    requestAnimationFrame: () => {},
    window: { SigmaMath: require('../dashboard/live-calculations.js'),
      SigmaStateRefresh: { createLoader }, location: { protocol: 'http:', host: 'localhost' },
      setInterval() {}, setTimeout() {}, clearTimeout() {}, addEventListener() {} },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../dashboard/app.js'), 'utf8'), context);
  vm.runInContext('render = () => {};', context);
  await vm.runInContext('refreshDashboardState()', context);
  vm.runInContext(`
    state.live.marketSession = 'premarket';
    state.live.dataDate = '2026-09-09';
    const point = { ticker: 'LIVE', price: 105, session: 'premarket',
      trading_date: '2026-09-09', ts_event: '2026-09-09T08:12:00Z', is_stale: false };
    state.live.points.set('LIVE', point);
    applyLivePrice(point, '2026-09-09');
  `, context);
  const updated = structuredClone(initial);
  for (const view of ['snapshot', 'today']) updated[view].rows[0].sigma_1d_pct = 4;
  responses.push(response(updated, '"sigma-ready"'));
  await vm.runInContext('refreshDashboardState()', context);
  for (const payload of ['state.payload', 'state.todayPayload']) {
    assert.equal(vm.runInContext(`${payload}.rows[0].sigma_1d_pct`, context), 4);
    assert.equal(vm.runInContext(`${payload}.rows[0].last_price`, context), 105);
    assert.ok(Math.abs(vm.runInContext(`${payload}.rows[0].z_score`, context) - Math.log(1.05)/.04) < 1e-10);
  }
  // A stale Friday list must use the verified Tuesday anchor in both views,
  // even when the feed carries an approximate or older regular close.
  const anchored = structuredClone(updated);
  for (const view of ['snapshot', 'today']) {
    anchored[view].anchor_date = '2026-09-04';
    Object.assign(anchored[view].rows[0], { return_anchor_price: 110,
      return_anchor_date: '2026-09-08', return_anchor_timestamp: '2026-09-08T20:00:00Z' });
  }
  responses.push(response(anchored, '"anchor-ready"'));
  await vm.runInContext('refreshDashboardState()', context);
  vm.runInContext(`
    applyLivePrice({ ticker: 'LIVE', price: 106, session: 'premarket',
      trading_date: '2026-09-09', ts_event: '2026-09-09T08:13:00Z', is_stale: false,
      regular_close: { price: 99, trading_date: '2026-09-04', ts_event: '2026-09-04T19:58:00Z' }
    }, '2026-09-09');
  `, context);
  for (const payload of ['state.payload', 'state.todayPayload']) {
    assert.equal(vm.runInContext(`${payload}.rows[0].last_price`, context), 106);
    assert.equal(vm.runInContext(`${payload}.rows[0].regular_price`, context), 110);
    assert.ok(Math.abs(vm.runInContext(`${payload}.rows[0].z_score`, context) - Math.log(106/110)/.04) < 1e-10);
    assert.ok(Math.abs(vm.runInContext(`extendedPriceState(${payload}.rows[0]).change.percent`, context) - (106/110-1)*100) < 1e-10);
  }

});
