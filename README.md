# Option Sniper

Option Sniper is a dark, live sigma-adjusted mover monitor. It ranks a saved
list of the latest completed Nasdaq session's gainers and a broader
current-session universe using option-implied close-to-close expected sigma and
live Databento prices.

The project is standalone, and generated market snapshots are never committed.
Legacy `sigma-tape` runtime paths and environment-variable names are retained so
existing caches, secrets, and EC2 releases remain compatible.

## Architecture

- `scripts/dashboard_state.py` resolves the latest completed Nasdaq session,
  validates the private cache and screening policy, and refreshes stale state.
- `scripts/nasdaq_universe_movers.py` performs the bounded historical spot and
  option screen.
- `scripts/live_price_adapter.py` serves the frontend and `/api/state`, then
  adapts Databento's Python live client to `/ws/prices` for the browser.
- `dashboard/app.js` applies incoming prices and computes gain and Z-score in
  the browser. A stale anchor is never used for the dynamic ranking.

The production universe includes comparable stocks above `$5 billion` in market
capitalization. There is no daily-notional gate and no pre-option candidate cap.
A stock is retained only when the nearest weekly expiration is 1 through 7
calendar days after the completed session and its paired option quotes produce a
mathematically valid IV. The upper bound is inclusive, so a following-Friday
contract is eligible from the prior Friday close.

The historical screen uses consolidated `EQUS.SUMMARY` daily bars for closing
prices. Live equity prices remain on `EQUS.MINI`. Both route through
`DATABENTO_LIVE_API_KEY`; historical OPRA access uses
`DATABENTO_OPTIONS_API_KEY`. The IV denominator is a prior-close option snapshot,
not historical volatility. Option Sniper infers the forward from paired call-put
mids and applies discounted Black-76 with a hard-coded 4% continuously
compounded annual risk-free rate.

Expected sigma scales annualized IV by the actual elapsed close-to-close time
using `IV * sqrt(elapsed calendar time / 365)`. A Friday-to-Monday trading
session therefore uses three calendar days, while an ordinary weekday session
uses one. The legacy cache field remains `sigma_1d_pct` for compatibility, but
the dashboard labels the measure `Expected sigma` and exposes the exact horizon
and formula in its metadata and tooltips.

## Local development

Python 3.11+ and Node 22+ are required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
make test
make coverage
python -m scripts.live_price_adapter --mock
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Mock mode generates
deterministic in-memory state and does not call Databento.

The automated suite covers option math and IV Reliability, exchange calendars,
liquidity and cost guards, state refresh/fallback, HTTP and WebSocket boundaries,
frontend live calculations, and repository hygiene. CI intentionally has no
Databento credentials: provider calls are mocked so pushes cannot spend credits
or fail because an external feed is unavailable. Python branch coverage is
measured across the complete backend and must remain at or above 60%.

Successful `main` pushes deploy automatically after CI passes. The production
workflow packages the exact tested commit, verifies its checksum on EC2, builds
a versioned image, waits for both API and frontend health checks, and rolls back
to the prior release on failure. Databento credentials remain only on EC2.

## Live data

Set credentials in the environment or an EC2 secret mechanism. Never put them
in Git or browser JavaScript.

```bash
export DATABENTO_LIVE_API_KEY='...'
export DATABENTO_OPTIONS_API_KEY='...'
python -m scripts.live_price_adapter --disable-overnight
```

At startup, `auto` mode accepts `.cache/sigma-tape/dashboard-state.json` only
when its anchor matches the latest completed Nasdaq session and its policy
fingerprint matches the current eligibility rules. Otherwise it fetches the
Nasdaq listing universe and runs one bounded Databento refresh. If that refresh
fails, an older valid cache is served with a visible stale warning; without any
valid cache, startup fails clearly.

Production also runs `option-sniper-refresh.timer` at 04:05 America/New_York on
Tuesday through Saturday, with a no-op retry at 05:00. The completed session's
close, gain, and Z-score remain active overnight; the one-shot job rolls the
live table to its new anchor for pre-market only after a valid cache is written.
It performs no Databento download when the cache already covers the latest
completed session and current screening policy.

Useful controls:

```bash
# Rebuild even if the cache is current
python -m scripts.live_price_adapter --refresh-mode force --disable-overnight

# Never spend historical credits; require an existing cache
python -m scripts.live_price_adapter --refresh-mode never --disable-overnight
```

`OCEA.MEMOIR` overnight data requires the corresponding Databento entitlement.
Use `--disable-overnight` when the live key only covers pre-market, regular, and
after-hours `EQUS.MINI` data.

## Security

Only the three frontend assets, `/api/state`, `/api/health`, and `/ws/prices`
are routed. Private cache and refresh artifacts stay under `.cache/`. For an
internet deployment, place the service behind Cloudflare Access and restrict
the EC2 security group to Cloudflare-origin traffic. `SIGMA_ALLOWED_ORIGINS`
can additionally contain a comma-separated WebSocket origin allowlist.

This dashboard is a research screen, not investment advice. IV Reliability
describes quote quality for the sigma denominator; it does not measure forecast
accuracy or trade execution quality.
