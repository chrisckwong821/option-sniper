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
`DATABENTO_LIVE_API_KEY`; live and historical OPRA access use the independent
`DATABENTO_LIVE_OPTIONS_API_KEY` and
`DATABENTO_HISTORICAL_OPTIONS_API_KEY` credentials. The legacy
`DATABENTO_OPTIONS_API_KEY` remains a shared fallback. The IV denominator is an
option snapshot, not historical volatility. Option Sniper infers the forward
from paired call-put mids and applies discounted Black-76 with a hard-coded 4%
continuously compounded annual risk-free rate.

Expected sigma scales annualized IV by the actual elapsed close-to-close time
using `IV * sqrt(elapsed calendar time / 365)`. A Friday-to-Monday trading
session therefore uses three calendar days, while an ordinary weekday session
uses one. The legacy cache field remains `sigma_1d_pct` for compatibility, but
the dashboard labels the measure `Expected sigma` and exposes the exact horizon
and formula in its metadata and tooltips.

Both dashboard views now use a shared expected-sigma snapshot. It activates at
04:00 America/New_York on exchange trading days and remains active through the
following overnight period. The reference is the previous trading session's
closing IV; the horizon ends at the active session's close, including holidays
and early closes. Yesterday's saved gain and ticker list are preserved, while
its Z-score uses this same current denominator.

The backend prepares the next snapshot after the close and retries pending work
every minute. With S3 configured, it first restores the session publication from
`expected-sigma/YYYY-MM-DD.json` under the configured S3 cache prefix. The
publication contains validated sigma, closing quotes, and verified stock closes;
it contains no live prices or Z-scores. Conditional S3 writes merge ticker
coverage safely across instances. Access/checksum errors stop the refresh instead
of triggering a paid fallback. Only missing inputs after S3/local reuse request a bounded
OPRA `cbbo-1m` live replay within the last 24 hours, with cost-guarded historical
fallback when necessary. It accepts quotes only in the final minute through the
exchange close and computes IV with the existing Black-76 estimator. Live replay
must complete before its timeout; an incomplete response is never published.
Live-feed closing stock prices or completed daily snapshots supply the spot
input, with a bounded historical daily-bar fallback.

Inputs and snapshots live in `.cache/sigma-tape/expected-sigma/`, independently
of daily list caches and IV-expansion caches. A prepared snapshot cannot activate
early, including after a restart. A late snapshot activates on the next API
request; open dashboards discover it with their existing one-minute state poll.
Until a symbol has valid previous-close IV, both views keep its prior shared
sigma and display `Awaiting previous-close IV`. Legacy list values are only a
visibly stale migration fallback. A local process must be restarted once to load
this code; subsequent sigma rollovers do not require restarting it.

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

### OCC weekly-options universe

The server restricts both dashboard lists to OCC weekly-program members before
building equity subscriptions, option subscriptions, or IV tracking. Valid IV
Coverage and reliability counts describe the filtered universe. Membership uses
the OCC option root, with a normalized ticker fallback, independently of the
selected contract expiry. There is no browser availability filter.

Import the latest public OCC list, then restart the local server to apply it:

```bash
python -m scripts.weekly_options
```

The command validates the current (left-hand) list before atomically replacing
`.cache/sigma-tape/occ-weekly-options.json`. For a custom `--state-cache`, put
this file in the same directory using `--output`. Membership is fixed for the
server lifetime so displayed coverage and subscriptions stay consistent. Missing,
invalid, or nonmatching lists stop non-mock startup rather than subscribing to
an unrestricted universe. The report date is displayed in the dashboard.
Production and staging deployment scripts import the latest OCC list before
activation and abort if the import fails. Stored source snapshots are preserved.
Mock mode can run without the OCC file.

Set credentials in the environment or an EC2 secret mechanism. Never put them
in Git or browser JavaScript.

```bash
export DATABENTO_LIVE_API_KEY='...'
export DATABENTO_LIVE_OPTIONS_API_KEY='...'
export DATABENTO_HISTORICAL_OPTIONS_API_KEY='...'
python -m scripts.live_price_adapter --disable-overnight
```

In local `auto` mode, startup accepts `.cache/sigma-tape/dashboard-state.json` only
when its anchor matches the latest completed Nasdaq session and its policy
fingerprint matches the current eligibility rules. Otherwise it fetches the
Nasdaq listing universe and runs one bounded Databento refresh. If that refresh
fails, an older valid cache is served with a visible stale warning; without any
valid cache, startup fails clearly.

The production container starts in `never` mode so a deployment cannot block on
historical-data availability. Its mounted cache remains visible until the
dedicated pre-market refresh job atomically replaces it.

Production also runs `option-sniper-refresh.timer` at 03:15 America/New_York on
Tuesday through Saturday, with a no-op retry at 03:45. The completed session's
close, gain, and Z-score remain active overnight; the one-shot job rolls the
live table to its new anchor before pre-market only after a valid cache is written.
It performs no Databento download when the cache already covers the latest
completed session and current screening policy.

Yesterday's Gainers has a separate `option-sniper-yesterday-refresh.timer` at
20:20 America/New_York Monday through Friday, with a retry at 20:40. That is
08:20/08:40 UTC+8 the following morning during U.S. daylight saving time and
09:20/09:40 in standard time, plus processing time. This follows the final
EQUS.SUMMARY daily bar published around 20:15 ET. The job writes
`yesterday-state.json`; the adapter uses only its saved gainer list, preserving
today's universe, anchors, and alert cycle in `dashboard-state.json`. It restarts
only the adapter to subscribe to the updated ticker list; live clients reconnect
automatically. The existing pre-market refresh and Discord logic are unchanged.
Open dashboards check for new lists every minute, on reconnect, and when returning
to the tab. An unchanged Today view retains its existing live rows and filters.

## Discord +1.2 sigma alerts

Production includes a small alert worker that consumes the backend WebSocket;
it does not open a second Databento connection. Its alert cycle starts at 04:00
America/New_York and covers pre-market, regular, after-hours, and the following
overnight session. The first fresh snapshot sends every tracked ticker already
at or above +1.2 expected sigma. Later alerts require an upward crossing, and a
SQLite ledger ensures that each ticker is sent at most once per alert cycle.

Set these values only in the host-owned `/etc/sigma-tape/sigma-tape.env` file:

```bash
DISCORD_BOT_TOKEN=ROTATED_TOKEN
DISCORD_CHANNEL_ID=1544929277009465374
ZSCORE_ALERT_THRESHOLD=1.2
```

The channel shown above is the built-in default for this deployment. The
production Compose manifest pins the alert threshold to `1.2`.

The bot needs View Channel and Send Messages permission in the configured
channel. Never commit its token. If a token is pasted into chat, logs, or source
control, reset it in the Discord Developer Portal before deployment.

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

## Historical IV-expansion calibration

`scripts/iv_expansion_history.py` builds per-ticker, 30-minute robust IV-change
distributions. It keeps at least two calendar days to expiry: the front weekly
is used at 2-4 DTE and the next weekly is used after the Thursday roll. Holiday
weeks follow the exchange calendar. A ticker without the target weekly uses its
nearest eligible listed fallback and is calibrated in a separate tenor class.

Contract definitions are captured once per target-expiry cycle and reused for
that cycle. The prior close and every intraday bucket still use the historical
stock price to select moneyness-matched call/put strikes. Raw Databento responses
are content-addressed in the local cache, so exact reruns do not repurchase data.
Low-reliability current marks receive a provisional score against the clean
reliable baseline; low-reliability history is excluded unless explicitly enabled.

Batch calibration is session-centric: each stock session and each expiry-cycle
contract catalogue is loaded once for the full universe. Exact option symbols are
then split into 1,500-symbol requests and downloaded through three concurrent
lanes by default. The cost ledger reserves concurrent requests before download,
so `--max-cost` remains a hard aggregate cap. Computation and SQLite writes stay
ordered and single-threaded; reruns resume from the content-addressed cache.

## Security

Only the three frontend assets, `/api/state`, `/api/health`, and `/ws/prices`
are routed. Private cache and refresh artifacts stay under `.cache/`. For an
internet deployment, place the service behind Cloudflare Access and restrict
the EC2 security group to Cloudflare-origin traffic. `SIGMA_ALLOWED_ORIGINS`
can additionally contain a comma-separated WebSocket origin allowlist.

This dashboard is a research screen, not investment advice. IV Reliability
describes quote quality for the sigma denominator; it does not measure forecast
accuracy or trade execution quality.

## Reopening after weekends and holidays

`option-sniper-reopening-refresh.timer` checks daily at 19:45 ET, with retries
at 20:00, 20:15, and 20:45 ET. The refresh command uses the exchange calendar:
it only proceeds on the evening immediately before a trading session whose
previous trading session is more than one calendar day earlier. Ordinary
weekday evenings and evenings before holidays are no-ops.

The first check prepares the latest completed-session cache before Blue Ocean
opens at 20:00 ET (09:00 JST in daylight saving time, 10:00 JST in winter).
A valid current cache avoids historical downloads and backend restarts. Failed
refreshes retain the published cache. The existing pre-market timer remains
unchanged and provides a fallback. Reopening refreshes recreate only the backend;
the Discord worker and its pre-market alert-cycle policy remain unchanged.

During that reopening overnight session, Today uses fresh trades for live Z-score
ranking against the last completed-session anchor. Stale trades and trades for
other session dates do not activate the exception. Yesterday's saved display and
ordinary weekday overnight carry behavior are preserved. Live activation depends
on receiving a fresh overnight trade; preparing a cache alone does not create
live prices. The new timer is installed by the deployment script.
