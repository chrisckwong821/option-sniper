# Sigma Tape

Sigma Tape is a dark, live Nasdaq mover dashboard. It ranks a saved list of the
latest completed session's gainers and a broader current-session universe using
option-implied one-day sigma and live Databento prices.

The project is standalone, and generated market snapshots are never committed.

## Architecture

- `scripts/dashboard_state.py` resolves the latest completed Nasdaq session,
  validates the private cache, and refreshes stale or missing state.
- `scripts/nasdaq_universe_movers.py` performs the bounded historical spot and
  option screen. The default pre-option candidate cap is 300.
- `scripts/live_price_adapter.py` serves the frontend and `/api/state`, then
  adapts Databento's Python live client to `/ws/prices` for the browser.
- `dashboard/app.js` applies incoming prices and computes gain and Z-score in
  the browser. A stale anchor is never used for the dynamic ranking.

The historical screen routes spot and live equity access through
`DATABENTO_LIVE_API_KEY`; historical OPRA access uses
`DATABENTO_OPTIONS_API_KEY`. The IV denominator is a prior-close option
snapshot, not historical volatility.

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

## Live data

Set credentials in the environment or an EC2 secret mechanism. Never put them
in Git or browser JavaScript.

```bash
export DATABENTO_LIVE_API_KEY='...'
export DATABENTO_OPTIONS_API_KEY='...'
python -m scripts.live_price_adapter --disable-overnight
```

At startup, `auto` mode accepts `.cache/sigma-tape/dashboard-state.json` only
when its anchor matches the latest completed Nasdaq session. Otherwise it
fetches the Nasdaq listing universe and runs one bounded Databento refresh. If
that refresh fails, an older valid cache is served with a visible stale warning;
without any valid cache, startup fails clearly.

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
