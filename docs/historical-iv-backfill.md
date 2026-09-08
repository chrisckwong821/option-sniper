# Historical IV Backfill Exploration

Date: 2026-08-29

This note evaluates whether Databento can support a two-year option IV history
for the current coverage universe and sketches a toolchain that can backfill new
tickers safely.

## Databento Fit

Databento's `OPRA.PILLAR` dataset is the right source for US equity option
quotes. Their docs describe OPRA as consolidated last sale and national BBO for
single-name, ETF, and index options, and they show parent symbology requests such
as `AAPL.OPT` with `stype_in="parent"`.

Use `cbbo-1m` as the default schema for IV history. It gives consolidated best
bid/offer snapshots at a one-minute cadence, which is enough for a daily IV mark
without storing full event-level OPRA updates.

Databento's documented schemas do not currently expose a precomputed IV/Greeks
schema. Treat IV as a derived metric: fetch quote snapshots plus a spot price,
pair calls and puts by strike/expiry, infer forward from put-call parity, and
invert an option model. The existing `nasdaq_universe_movers.py` code already
does this for a single snapshot.

## Recommended Scope

Backfill a daily IV snapshot, not all historical option data.

- Snapshot time: default to `15:59:00 America/New_York` through `16:00:01`.
- Quote schema: `OPRA.PILLAR` / `cbbo-1m`.
- Input symbology: `parent`, e.g. `AAPL.OPT`.
- Spot schema: `EQUS.MINI` / `ohlcv-1m` for same-minute spot close.
- Tenor policy: choose the paired expiry nearest 30 DTE inside a 7-60 DTE band.
- IV model: reuse the current forward-ATM paired call/put method and store
  reliability metadata.

For research, storing one derived row per ticker/session is the useful object:
`annualized_implied_vol_pct`, `forward_price`, `atm_strike`, bid/ask quality,
expiry, DTE, source schema, and request/run IDs.

## Cost Model

Databento's pricing page says historical data is usage-based by GB, and their
API exposes exact preflight pricing through `metadata.get_cost`. Their example
unit prices show `OPRA.PILLAR` historical `cbbo-1m` at `$2.00/GB`; the final cost
still depends on how many records the request returns.

Existing local authenticated runs give a useful calibration:

| Local run | Parent tickers | Window | OPRA schema | Preflight cost | Records |
| --- | ---: | --- | --- | ---: | ---: |
| `finance-skills/research/nasdaq-movers/2026-08-21/screen.json` | 299 | 61s | `cbbo-1m` | `$0.4149` | 343,261 |
| `finance-skills/research/nasdaq-movers/2026-08-24/screen.json` | 300 | 61s | `cbbo-1m` | `$0.4251` | 344,223 |
| `finance-skills/research/nasdaq-movers/2026-08-26/screen-tue-close-wed-close.json` | 300 | 61s | `cbbo-1m` | `$0.5011` | 606,646 |

Approximate scaling:

- One 300-ticker daily snapshot: about `$0.42-$0.50`.
- One average ticker daily snapshot: about `$0.0014-$0.0017`.
- AAPL for roughly 504 trading sessions: about `$0.70-$0.85` on linear average,
  but use `$1-$5` as a realistic planning range because AAPL has a larger option
  chain than a random ticker.
- Current IV universe has 135 tickers. Linear scaling gives about `$95-$115` for
  two years; a practical budget cap should allow `$100-$180` until exact
  `metadata.get_cost` sampling confirms the chain mix.

Spot costs are tiny relative to OPRA in the existing runs. Still, spot requests
should be preflighted and included in the run ledger.

The expensive path is full intraday or tick-level option history. Avoid that for
this objective unless there is a separate microstructure question.

## Probe Command

The reusable probe lives at `scripts/historical_iv_backfill_probe.py`.

Dry-run request shape without credentials:

```bash
sigma-tape/.venv/bin/python sigma-tape/scripts/historical_iv_backfill_probe.py \
  --tickers AAPL \
  --end-date 2026-08-28 \
  --cost-sample-sessions 8 \
  --dry-run-plan
```

After exporting Databento credentials, run the preflight:

```bash
export DATABENTO_LIVE_API_KEY='...'
export DATABENTO_HISTORICAL_OPTIONS_API_KEY='...'

sigma-tape/.venv/bin/python sigma-tape/scripts/historical_iv_backfill_probe.py \
  --tickers AAPL \
  --end-date 2026-08-28 \
  --cost-sample-sessions 8 \
  --output /private/tmp/aapl-iv-preflight.json
```

To download one guarded sample session and compute IV:

```bash
sigma-tape/.venv/bin/python sigma-tape/scripts/historical_iv_backfill_probe.py \
  --tickers AAPL \
  --end-date 2026-08-28 \
  --cost-sample-sessions 8 \
  --download-sample-sessions 1 \
  --max-download-cost 1.00 \
  --output /private/tmp/aapl-iv-sample.json
```

For current Option Sniper coverage:

```bash
sigma-tape/.venv/bin/python sigma-tape/scripts/historical_iv_backfill_probe.py \
  --tickers '' \
  --ticker-source finance-skills/dashboard/data/iv-universe-state.json \
  --end-date 2026-08-28 \
  --cost-sample-sessions 8 \
  --output /private/tmp/current-coverage-iv-preflight.json
```

## Architecture

Keep the system as a small Python data pipeline before introducing a scheduler
framework.

1. Coverage adapters
   - Option Sniper coverage: read `dashboard/data/iv-universe-state.json`.
   - Portfolio coverage: read sanitized holdings from the portfolio writer or a
     local IBKR read-only snapshot.
   - Manual coverage: accept a checked-in or local JSON/CSV watchlist.

2. Planner
   - Build XNYS sessions for the target lookback.
   - Generate one snapshot window per session.
   - Apply the target DTE policy.
   - Detect new tickers and plan only missing `(ticker, session, tenor_policy)`
     rows.

3. Cost gate
   - Call `metadata.get_cost` before downloads.
   - Store request payload, sampled sessions, unit price, estimated cost, exact
     cost if available, and approval status.
   - Enforce per-run and monthly budget caps.

4. Downloader
   - Use Databento Historical streaming for small requests.
   - Use Databento batch for larger requests, especially once estimated request
     size crosses Databento's large-request guidance.
   - Cache raw DBN/Zstd files partitioned by dataset, schema, date, and ticker
     batch.

5. IV engine
   - Normalize OPRA symbols with the existing OCC parser.
   - Pair calls and puts by expiry/strike.
   - Compute forward-ATM IV with the existing Black-76 inversion.
   - Emit quality fields so noisy/stale quotes can be filtered later.

6. Storage
   - Use local Parquet files plus DuckDB for exploration.
   - Suggested derived table: `iv_snapshots(ticker, session_date, snapshot_ts,
     expiry, dte, annualized_iv, forward_price, atm_strike, reliability,
     quote_records, request_id)`.
   - Suggested ledger tables: `coverage_tickers`, `backfill_runs`,
     `request_costs`, `raw_artifacts`.

7. Operations
   - Add Make targets for `historical-iv-preflight`, `historical-iv-sample`, and
     `historical-iv-backfill`.
   - Run backfills locally or on the existing EC2-style secret boundary, never in
     browser code.
   - On each coverage update, diff tickers against stored history and queue only
     missing work.

## Open Items

- Confirm the true coverage source: current IV universe, portfolio holdings, or
  a separate analyst watchlist.
- Run the AAPL authenticated preflight once a Databento key is available in the
  shell.
- Decide whether the research mark should be close, open, or multiple intraday
  snapshots around events.
