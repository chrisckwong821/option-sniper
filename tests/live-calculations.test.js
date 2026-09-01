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

const weekendHorizon = SigmaMath.calculateSigmaHorizon(
  3.008704734264028,
  33.18679970994348,
);
assert.ok(Math.abs(weekendHorizon.calendarDays - 3) < 1e-12);
assert.equal(
  SigmaMath.sigmaHorizonEnd("2026-08-28T16:00:00-04:00", weekendHorizon),
  "2026-08-31T20:00:00.000Z",
);
const weekdayHorizon = SigmaMath.calculateSigmaHorizon(
  33.18679970994348 / Math.sqrt(365),
  33.18679970994348,
);
assert.ok(Math.abs(weekdayHorizon.calendarDays - 1) < 1e-12);
assert.equal(SigmaMath.calculateSigmaHorizon(0, 33), null);
assert.equal(SigmaMath.sigmaHorizonEnd("invalid", weekendHorizon), null);

const premarketRow = {
  gain_pct: -0.8,
  prior_session_gain_pct: 3.25,
  regular_gain_pct: 3.25,
};
const premarketLiveMetrics = SigmaMath.calculateLiveMetrics(99.2, 100, 2);
premarketRow.gain_pct = premarketLiveMetrics.gainPct;
premarketRow.z_score = premarketLiveMetrics.zScore;
assert.equal(SigmaMath.primaryGainPct("today", premarketRow), 3.25);
assert.ok(premarketRow.z_score < 0);
premarketRow.regular_gain_pct = 0.4;
assert.equal(SigmaMath.primaryGainPct("today", premarketRow), 0.4);
assert.equal(SigmaMath.primaryGainPct("yesterday", {
  gain_pct: -0.8,
  saved_gain_pct: 5.5,
}), 5.5);

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
assert.equal(
  SigmaMath.isCompletedSessionCarry(
    "today", "2026-08-27", "2026-08-28", "afterhours", "2026-08-28",
  ),
  true,
);
assert.equal(
  SigmaMath.isCompletedSessionCarry(
    "today", "2026-08-28", "2026-08-28", "premarket", "2026-08-28",
  ),
  false,
);
assert.equal(SigmaMath.isPostCloseSession("overnight"), true);
assert.equal(SigmaMath.isPostCloseSession("closed"), true);
assert.equal(SigmaMath.isPostCloseSession("premarket"), false);
assert.equal(SigmaMath.displaysCompletedSession("overnight", false), true);
assert.equal(SigmaMath.displaysCompletedSession("premarket", true), true);
assert.equal(SigmaMath.displaysCompletedSession("premarket", false), false);
assert.equal(
  SigmaMath.isCompletedSessionCarry(
    "today", "2026-08-28", "2026-08-31", "overnight", "2026-08-31",
  ),
  true,
);
assert.equal(
  SigmaMath.isCompletedSessionCarry(
    "today", "2026-08-28", "2026-08-31", "premarket", "2026-08-31",
  ),
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

assert.deepEqual(SigmaMath.streamSequenceDecision(null, "snapshot", 7), {
  action: "accept",
  sequence: 7,
});
assert.deepEqual(SigmaMath.streamSequenceDecision(7, "delta", 8), {
  action: "accept",
  sequence: 8,
});
assert.equal(SigmaMath.streamSequenceDecision(8, "delta", 8).action, "ignore");
assert.equal(SigmaMath.streamSequenceDecision(8, "delta", 10).action, "resync");
assert.equal(SigmaMath.streamSequenceDecision(8, "status", 8).action, "accept");
assert.equal(SigmaMath.streamSequenceDecision(null, "delta", 1).action, "resync");

console.log("live calculations: ok");
