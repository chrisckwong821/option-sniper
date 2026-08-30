const assert = require("node:assert/strict");
const SigmaMath = require("../dashboard/live-calculations.js");

const panw = SigmaMath.calculateLiveMetrics(382.815, 339.4, 3.536098487069497);
assert.ok(Math.abs(panw.gainPct - 12.791691219799661) < 1e-10);
assert.ok(Math.abs(panw.zScore - 3.40410459165968) < 1e-10);

const down = SigmaMath.calculateLiveMetrics(95, 100, 2.5);
assert.ok(down.gainPct < 0);
assert.ok(down.zScore < 0);
assert.equal(SigmaMath.calculateLiveMetrics(0, 100, 2.5), null);

const premarket = SigmaMath.calculatePriceChange(227.87, 227.96);
assert.ok(Math.abs(premarket.absolute - (-0.09)) < 1e-10);
assert.ok(Math.abs(premarket.percent - (-0.03948061063344932)) < 1e-10);
assert.equal(SigmaMath.calculatePriceChange(0, 227.96), null);

assert.equal(
  SigmaMath.isNewerPrice("2026-08-28T13:30:01Z", "2026-08-28T13:30:00Z"),
  true,
);
assert.equal(SigmaMath.sessionLabel("premarket"), "Pre-market");
assert.equal(
  SigmaMath.isPriceForTradingDate("2026-08-28", "2026-08-28"),
  true,
);
assert.equal(
  SigmaMath.isPriceForTradingDate("2026-08-27", "2026-08-28"),
  false,
);
assert.equal(
  SigmaMath.isCurrentAnchor("today", "2026-08-27", "2026-08-27"),
  true,
);
assert.equal(
  SigmaMath.isCurrentAnchor("today", "2026-08-26", "2026-08-27"),
  false,
);
assert.equal(
  SigmaMath.isCurrentAnchor("yesterday", "2026-08-26", "2026-08-27"),
  true,
);

const fridayPremarket = SigmaMath.calculateLiveMetrics(104, 100, 2);
assert.ok(Math.abs(fridayPremarket.zScore - (Math.log(1.04) / 0.02)) < 1e-12);

const rows = Array.from({ length: 45 }, (_, index) => index + 1);
assert.deepEqual(SigmaMath.paginateRows(rows, 2, 20), {
  end: 40,
  items: rows.slice(20, 40),
  offset: 20,
  page: 2,
  pageSize: 20,
  start: 21,
  total: 45,
  totalPages: 3,
});
assert.equal(SigmaMath.paginateRows(rows, 99, 20).page, 3);
assert.equal(SigmaMath.paginateRows([], 1, 20).start, 0);

console.log("live calculations: ok");
