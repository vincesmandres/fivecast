# FiveCast

An open research lab for studying Bitcoin 5-minute prediction markets, market
probabilities, and short-horizon price dynamics.

FiveCast is **not a trading bot**. The M2 collector observes public data, validates
point-in-time research snapshots, stores metadata and official settlement labels,
and measures dataset integrity locally. It does
not evaluate a strategy or claim an edge. [SOUL.md](SOUL.md) defines the project's
research principles and safety boundary. Paper execution is not implemented.

## Architecture

```text
Coinbase public ticker + exact starting-minute candle
                         |
                         v
Gamma market discovery -> validated models <- CLOB UP/DOWN public books
                         |
                 freshness/skew checks
                         |
                  MarketSnapshot
                         |
                 SQLite + terminal
```

- `config.py`: validated defaults, optional TOML, and environment overrides.
- `models.py`: immutable Pydantic models, UTC timestamps, Decimal values and invariants.
- `feeds/btc.py`: Coinbase BTC-USD last-trade quote and deterministic window open.
- `feeds/polymarket.py`: current BTC five-minute market discovery and best book quotes.
- `market/snapshot.py`: active-window, identity, freshness and synchronization checks.
- `storage/sqlite.py`: local schema creation, parameterized insertion and retrieval.
- `main.py`: synchronous observer, UTC logging, bounded runs and graceful Ctrl+C.
- `collector.py`: resilient scheduled collection and an independent settlement thread.
- `feeds/http.py`: bounded, cancellable retry/backoff for public GET requests.
- `storage/migrations.py`: backed-up, transactional migration of existing observations.
- `report.py`: read-only SQLite quality reports, with no internet access.

This is a small Python package in `src/fivecast`, not a nested project. Runtime
dependencies are only `httpx` and `pydantic` plus their PyPI dependencies. SQLite,
TOML parsing, CLI handling and logging use the standard library. HTTP polling keeps
the five-second collector explicit and avoids WebSocket reconnect/book-rebuild
state; it is not intended to capture every tick.

## Setup And Installation

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run all commands
from this repository's root. The verified environment is Windows with CPython
3.12.13. `uv` can provision Python if needed.

```powershell
uv sync --locked --python 3.12
```

This creates `.venv` locally and installs the package and development tools using
`uv.lock`. No credentials, accounts, wallet setup or services are required.

## Run

**Overnight collection**, with a five-second default interval and independent
settlement polling:

```powershell
uv run python -m fivecast.main collect
```

Leave the terminal running and prevent the machine from sleeping. No external
service is required. Ctrl+C requests graceful shutdown. Bounded M2 runs count
attempted poll cycles, including failures:

```powershell
uv run python -m fivecast.main collect --duration 420
uv run python -m fivecast.main collect --iterations 12
```

The original M0/M1 observer commands remain available, including its fail-fast
bounded mode and full human-readable snapshots:

```powershell
uv run python -m fivecast.main
```

One snapshot or a bounded smoke run:

```powershell
uv run python -m fivecast.main --once
uv run python -m fivecast.main --iterations 3
```

Optional settings and a different local database:

```powershell
uv run python -m fivecast.main --config config/settings.example.toml --db-path data/research.db
```

The installed `fivecast` console entry point is equivalent. Normal operation
rediscovers the active window on every iteration and caches only the current
window's validated opening candle. Metadata is saved even if a subsequent quote
request fails. At rollover, expired windows are marked closed and the next window
is discovered without restarting.

The original observer's continuous mode logs and skips unavailable markets, unpublished opening candles,
stale/skewed quotes, transport failures, HTTP 429 and HTTP 5xx. It retries on the
configured cadence. Malformed payloads, identity mismatches, crossed books and
other HTTP errors terminate with an error rather than concealing bad research
data. Bounded runs fail nonzero on any unsuccessful observation. No row is stored
for a failed observation.

In **collect mode**, each public GET has at most three attempts by default.
Timeouts, connection failures, HTTP 429 and HTTP 5xx trigger exponential backoff
(1, 2 seconds by default); `Retry-After` is honored up to the configured cap. Other
HTTP responses are not retried within that request. Exhausted polls are logged and
recorded as failures, then collection continues on its schedule. Invalid payloads
and inconsistent observations are logged at ERROR, recorded as failed polls, and
never stored as snapshots. Unexpected programming/storage failures are fatal,
including failures in the settlement thread; they are not silently discarded.

There are no catch-up request bursts. Slow requests and backoffs leave missed grid
slots visible as reduced coverage. A duration limit stops scheduling new polls;
an in-flight poll may finish after the limit. Ctrl+C closes each thread's HTTP and
SQLite resources, interrupts retry waits, and logs a collection summary. A request
already in progress in the settlement thread may take its configured timeout to
finish. SQLite transactions either commit completely or roll back.

Normal M2 logs are compact:

```text
DISCOVERED market=... slug=btc-updown-5m-...
SNAPSHOT market=... remaining=... delta=... skew_ms=... latency_ms=... stale=False
WINDOW CLOSED market=...
RESOLVED market=... outcome=UP
COLLECTION SUMMARY run=... attempted=... saved=... failed=... interrupted=...
```

Offline reports, also usable while collection is running:

```powershell
uv run python -m fivecast.report
uv run python -m fivecast.report --per-market
uv run python -m fivecast.report --per-market --json
```

Reports open SQLite with `mode=ro` and `query_only`, within one consistent read
transaction. They never create a database, run a migration, or access the internet.
Use `--db-path` or `--config` when collecting/reporting a nondefault database.

## Configuration

Precedence is defaults, explicitly supplied TOML, `FIVECAST_*` environment
variables, then `--db-path` for the database path only. Unknown settings, invalid
values and missing explicitly requested files are errors. Paths are relative to
the working directory. `.env.example` documents environment variables; dotenv
files are **not** loaded automatically.

| Setting | Default | Environment Variable |
| --- | --- | --- |
| `db_path` | `data/fivecast.db` | `FIVECAST_DB_PATH` |
| `interval_seconds` | `5` | `FIVECAST_INTERVAL_SECONDS` |
| `request_timeout_seconds` | `10` | `FIVECAST_REQUEST_TIMEOUT_SECONDS` |
| `max_quote_age_seconds` | `30` | `FIVECAST_MAX_QUOTE_AGE_SECONDS` |
| `max_quote_skew_seconds` | `10` | `FIVECAST_MAX_QUOTE_SKEW_SECONDS` |
| `retry_attempts` | `3` | `FIVECAST_RETRY_ATTEMPTS` |
| `retry_backoff_seconds` | `1` | `FIVECAST_RETRY_BACKOFF_SECONDS` |
| `retry_max_backoff_seconds` | `30` | `FIVECAST_RETRY_MAX_BACKOFF_SECONDS` |
| `settlement_poll_interval_seconds` | `30` | `FIVECAST_SETTLEMENT_POLL_INTERVAL_SECONDS` |
| `log_level` | `INFO` | `FIVECAST_LOG_LEVEL` |

`interval_seconds` and maximum quote age must be in `(0, 300]`; timeout and maximum
quote skew must be in `(0, 60]`. Numeric settings must be finite. Logging levels
are `DEBUG`, `INFO`, `WARNING` or `ERROR`.
Retry attempts are in `[1, 5]`; initial backoff in `(0, 30]` seconds, capped backoff
in `(0, 60]` seconds, and settlement interval in `[5, 3600]` seconds.

## Public Data Sources

**Coinbase Exchange**, chosen as a reputable, unauthenticated BTC-USD spot source:

- `GET https://api.exchange.coinbase.com/products/BTC-USD/ticker`: last trade price
  and the exchange's trade timestamp, not the local response-arrival timestamp.
- `GET https://api.exchange.coinbase.com/products/BTC-USD/candles`: `granularity=60`,
  `start=<market start>`, `end=<current collection time, capped at market end>`.
- Official references: [ticker](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductticker)
  and [candles](https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles).

**Polymarket**, using direct HTTP, without a trading SDK:

- `GET https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-{epoch}`:
  discovery using `epoch = floor(current UTC epoch / 300) * 300`.
- `GET https://clob.polymarket.com/book?token_id=<token>`: one public book per outcome.
- `GET https://clob.polymarket.com/markets/{condition_id}`: public explicit outcome
  winner flags, corroborated against Gamma's final state before recording settlement.
- Official references: [discovery](https://docs.polymarket.com/market-data/discover-markets.md),
  [market details](https://docs.polymarket.com/market-data/market-details.md),
  [books](https://docs.polymarket.com/market-data/prices-order-books.md),
  [Gamma contract](https://docs.polymarket.com/api-spec/gamma-openapi.yaml), and
  [CLOB contract](https://docs.polymarket.com/api-spec/clob-openapi.yaml).

Gamma's `outcomes` and `clobTokenIds` are JSON-encoded arrays, mapped positionally
and checked for exactly one UP and one DOWN. `eventStartTime` is the actual window
start; `startDate` is listing time and is not used. The slug epoch, start, end and
five-minute duration must agree. Markets must be active, unclosed, unarchived and
book-enabled, with `acceptingOrders=true`; this flag is read as metadata only.

CLOB responses must match the requested token and condition ID. Hex condition
identity is compared case-insensitively. Every book level's price and size are
validated. Best bid is the maximum bid; best ask is the minimum ask, independent
of response sorting. Empty sides become `None`/SQL `NULL`, never fabricated prices.
Book timestamps are interpreted as Unix milliseconds. Unknown external metadata
is ignored, but every consumed field is validated before use.

## Official Settlement

A dedicated thread with its own HTTP client and SQLite connection polls expired,
unresolved markets. It never delays the foreground collector with network work.
Pending markets survive restarts in SQLite. Each pass handles at most ten due
markets, oldest-checked first, and schedules subsequent checks on the configured
settlement interval. Network backoff is bounded and cancellable.

A label is accepted only when all of the following agree:

- Gamma market ID, slug, condition, window and token mapping match persisted metadata.
- Gamma reports `closed=true` and `umaResolutionStatus="resolved"`.
- CLOB reports the same condition and slug, `closed=true`, exactly two correctly
  mapped tokens, and exactly one `tokens[].winner=true`.
- Gamma's final `outcomePrices` are exactly `1` for that explicit winning token and
  `0` for the other outcome. These prices corroborate the winner; they do not create it.

Window expiry, `active`, `acceptingOrders`, Coinbase direction, and near-1.0 prices
are not settlement labels. Gamma/CLOB publication lag leaves a market unresolved
and is logged. Malformed or contradictory final data is rejected. A conflicting
later winner cannot overwrite an existing resolved label.

When available, `resolution_time_utc` stores Gamma's `closedTime`, with
`resolution_time_source="gamma_closedTime"`. This is the platform lifecycle close
timestamp, not a claimed exact on-chain finalization time. It must be aware and no
earlier than the market end. `resolution_observed_at_utc` separately records when
the collector first confirmed agreement. If `closedTime` is absent, the official
time stays NULL; the scheduled market end or local observation time is not substituted.

The live public CLOB `/markets/{condition_id}` route supplies winner flags. The
current [CLOB market-info reference](https://docs.polymarket.com/api-reference/markets/get-clob-market-info.md)
also describes a different `/clob-markets` route for market parameters; these are
not treated as interchangeable. A schema change on the verified winner route must
be investigated, not bypassed. See the official [resolution lifecycle](https://docs.polymarket.com/concepts/resolution.md).

## Snapshot Semantics

**Deterministic BTC opening price:** use the `open` field from the Coinbase
one-minute candle whose bucket timestamp equals the market start exactly. Coinbase
defines this as the first trade in that minute. Select by timestamp, never by array
position or by nearest fallback. Reject malformed or duplicate matching candles;
if the exact candle is absent, skip until it is published. Ignore valid extra
candles outside the requested bucket. Cache the result for that window only.

Coinbase caches even empty candle responses for 300 seconds. The M2 live test
exposed this at rollover: a fixed query range cached an initial miss for the whole
window. Requests now advance their real `end` bound with collection time, capped
at the five-minute market end, so newly available observations can be retrieved.
The exact selected one-minute bucket and opening-price method have not changed.
If the candle genuinely remains unavailable, collection still records failures
rather than inventing an open.

This works when starting mid-window without labeling a startup quote as the open.
The candle bucket is known, but the precise first-trade time inside the minute is
not supplied by the candles endpoint. Persist `btc_open_method` and
`btc_open_observed_at_utc` to identify how and when this historical observation
became available to the collector. Only the candle's open is used for the signal;
its high, low and close are not used as features.

**This is not Polymarket's price to beat.** Live BTC market metadata inspected on
2026-09-06 referenced Chainlink BTC/USD 60-second TWAP. Coinbase is an independent
research covariate. These snapshots cannot alone reconstruct settlement, and no
outcome is inferred from the Coinbase price difference.

Snapshot time is the local UTC time after acquisition. Each source timestamp must
be within the active market window, no later than the snapshot, and at most 30
seconds old by default. Maximum source-to-source skew is 10 seconds by default.
If a source clock leads the host by at most one second, the observer waits for that
timestamp, reads the clock again, and applies all the same checks. Larger leads
are rejected. It never rewrites source timestamps or backdates a snapshot. Keep
the host clock synchronized. Polling is bounded-skew sampling, not an atomic
cross-venue observation.

```text
btc_delta_usd = btc_price - btc_window_open_price
btc_delta_pct = btc_delta_usd / btc_window_open_price
seconds_remaining = (market_end_utc - timestamp_utc).total_seconds()
spread = ask - bid, or NULL when either side is absent
```

`btc_delta_pct` is a fractional return: `0.001` means `0.1%`. Values are not rounded
before storage; Decimal arithmetic uses Python's default 28-digit precision.
Active snapshots require `start <= timestamp < end`, so remaining seconds are
strictly positive. Elapsed-window observations are not clamped or relabeled.

## Data Quality Definitions

The approved hard limits remain unchanged: out-of-window, future, excessively old
or excessively skewed quotes cannot produce stored snapshots. Within those limits,
M2 records the following quality measurements:

| Field / Metric | Definition |
| --- | --- |
| `btc_source_timestamp` | Original Coinbase trade timestamp, equal to `btc_timestamp_utc` |
| `market_source_timestamp` | Earlier of UP and DOWN book timestamps, a conservative market-data time |
| `source_skew_ms` | `(max(BTC, UP, DOWN source times) - min(...)) * 1000` |
| `poll_latency_ms` | Monotonic elapsed acquisition/validation time, including retries, candles and small clock waits; excludes the final SQLite insertion |
| `is_stale` | Oldest source age at snapshot time exceeds the run's configured polling interval; this is a softer flag than the hard rejection limits |
| `sample_count` | Actual stored snapshots for that market, including legacy observations |
| `coverage_sample_count` | Stored snapshots associated with an M2 collection run |
| `expected_sample_count` | Count of configured schedule grid points within the actual recorded collection interval, clipped to the market window for per-market reporting |
| `coverage_pct` | `100 * coverage_sample_count / expected_sample_count`; N/A when the denominator is zero |
| `mean_gap_seconds`, `max_gap_seconds` | Gaps between consecutive successful snapshots within the same run and market; gaps never bridge separate runs or market boundaries |
| `mean_skew_ms`, `max_skew_ms` | Aggregate measured source skew over stored snapshots |
| `stale_snapshot_count` | Number of stored snapshots explicitly flagged stale |
| `failed_poll_count` | Foreground poll cycles that ended in rejection or failure; successfully recovered HTTP retries do not count as failed polls |

For a run starting at `S` with interval `I`, expected samples are grid points
`S + n*I`, `n >= 0`, in `[S, E)`. `E` is graceful scheduling-stop time (capped at an
explicit duration deadline), or the last persisted heartbeat for an unfinished run.
Per-market expectations additionally intersect
`[market_start, market_end)`. Counts are summed across runs, retaining each run's
own interval. Process downtime before startup, between runs, or after an abandoned
heartbeat is excluded. Slow in-process requests and retry/backoff time are included.
The global denominator includes scheduled windows where discovery never succeeded.
No missing samples are synthesized. Coverage is not capped to hide anomalies.

Pending/interrupted polls are reported separately. Failures during discovery are
attributed by requested window slug if metadata is later discovered; unmatched
failures remain visible globally. Settlement errors have their own per-market
counter and do not inflate foreground failed-poll counts. Markets observed means
metadata rows, including markets with zero successful samples.

Legacy snapshots lack measured acquisition latency, stale-threshold configuration
and collection-run intervals. Their latency/stale flags remain NULL and they are
excluded from cadence coverage, but are included in sample/skew counts and any
available within-market legacy gaps. The report names rows without `run_id`
`legacy_snapshot_count`; this also covers standalone M0/M1 observer runs outside
`collect` mode. Unknown stale flags are reported separately, not assumed fresh.

## Example Live Output

Actual read-only observation from the successful three-snapshot smoke run:

```text
[2026-09-06 05:37:49 UTC]
Market      BTC 5m UP/DOWN (4240043)
Window      05:35:00 -> 05:40:00 UTC
Remaining   130.1s

BTC open    $79,930.78 (Coinbase 1m candle open)
BTC now     $80,000.00
Delta       $+69.22 (+0.0866%)

UP          bid 0.98  ask 0.99  spread 0.01
DOWN        bid 0.01  ask 0.02  spread 0.01
Sources     coinbase_exchange / polymarket_clob
Snapshot saved.
```

## SQLite Schema

The schema is created automatically in `data/fivecast.db`. Schema version 2 adds
`markets`, `collection_runs`, and `polls` alongside the original `snapshots` table.

Opening a version-0 database first creates `<database>.v0.bak` using SQLite's backup
API when it contains snapshots, then migrates within one transaction. Original
snapshot IDs, Decimal strings, creation times and observations are preserved.
Market window/token metadata is backfilled from those validated rows, with
`metadata_source="snapshot_backfill"`; unavailable question/condition fields stay
NULL until hydrated from Gamma. Unknown schema versions are rejected. The original
backup is not overwritten on subsequent starts.

The rebuilt snapshots table has an enforced `market_id` foreign key. SQLite WAL
allows collection, settlement and consistent offline report reads to coexist.
The legacy snapshot columns remain:

| Columns | SQLite Type / Meaning |
| --- | --- |
| `id` | INTEGER primary key |
| `timestamp_utc`, `market_start_utc`, `market_end_utc` | TEXT, fixed-width ISO UTC |
| `market_id` | TEXT, Gamma identifier |
| `seconds_remaining` | REAL, positive fractional seconds |
| `btc_price`, `btc_window_open_price`, `btc_delta_usd`, `btc_delta_pct` | TEXT, exact Decimal representation |
| `up_bid`, `up_ask`, `down_bid`, `down_ask`, `up_spread`, `down_spread` | Nullable TEXT, Decimal representation |
| `source_btc`, `source_prediction_market` | TEXT, explicit source names |
| `btc_timestamp_utc`, `up_timestamp_utc`, `down_timestamp_utc` | TEXT, source observation times |
| `up_token_id`, `down_token_id` | TEXT, outcome identity provenance |
| `btc_open_observed_at_utc`, `btc_open_method` | TEXT, opening-price provenance |
| `created_at_utc` | TEXT, local insertion time |

New snapshot columns: `btc_source_timestamp`, `market_source_timestamp` (UTC TEXT),
`source_skew_ms` (REAL), `poll_latency_ms` (nullable REAL), `is_stale` (nullable
boolean INTEGER), and `run_id` (nullable foreign key to `collection_runs`).

`markets` stores `market_id`, unique `slug`, nullable `condition_id`/`question`,
window bounds, UP/DOWN token IDs, `closed`, `resolved`, nullable `outcome`,
resolution timestamps/source provenance, metadata provenance, settlement check
scheduling/error count, and `created_at_utc`/`updated_at_utc`. Here `closed` means the
measurement window has elapsed; `resolved` requires the corroborated official label.

`collection_runs` stores start, heartbeat, graceful stop and configured interval.
`polls` stores scheduled/start/finish times, target slug and optional market ID,
status, failure category and full poll-cycle latency. No invalid external payload
is archived in the failure ledger. Per-market aggregates are computed by the
offline report, avoiding mutable summary counters that can drift from the evidence.

Core snapshot fields except absent quotes/spreads are non-null. A unique constraint covers
`(market_id, timestamp_utc)` and an index supports timestamp retrieval.
`save_snapshot()` returns `True` for a new row and `False` for an identical repeat.
A conflicting repeat raises an error instead of overwriting evidence. Identical
prices at a later timestamp are separate observations. `get_latest_snapshot()`
sorts by observation time, not insertion order, and revalidates the stored model.

Verify the count and SQLite integrity without modifying the database:

```powershell
uv run python -c "import sqlite3; c = sqlite3.connect('file:data/fivecast.db?mode=ro', uri=True); print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

The original M0/M1 smoke result was **3 rows**, integrity **ok**; M2 preserves these
rows and appends new observations. See [the M2 report](docs/M2_DATASET_REPORT.md)
for the multi-window verification. Databases, local
settings, virtual environments and generated build artifacts are git-ignored.

## Tests And Checks

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
uv build
```

Tests use `httpx.MockTransport`, injected feed methods and temporary databases.
A global test fixture blocks outbound socket connections and DNS, and isolates
`FIVECAST_*` environment configuration. No test requires live internet access.
Coverage includes malformed data, mapping, timestamp normalization, stale/future
quotes, clock leads, rollover, deltas, storage ordering, duplicate conflicts,
configuration, bounded failures, recovery, Ctrl+C and mocked end-to-end collection.

The approved M0/M1 build passed 100 offline tests. M2 adds migration, settlement,
retry, worker/shutdown, scheduling and report regressions. See
[the M2 report](docs/M2_DATASET_REPORT.md) for current exact test and live results,
and [the original report](docs/M0_M1_REPORT.md) for historical M0/M1 results.

M2 verification on 2026-09-06: **158 offline tests passed**. The final seven-minute
live run crossed two window boundaries, stored **80/84 expected samples (95.24%)**,
and exited naturally. SQLite holds **115 snapshots across five markets**, including
the three unchanged legacy rows, and one confirmed official UP settlement. Recent
expired markets remain unresolved while Gamma/CLOB final-state publication disagrees.
The full report retains and explains the initial diagnostic run's candle-cache failures.

## Known Limitations And Safety

- Coinbase prices are not the market's Chainlink TWAP settlement benchmark.
- Exact starting-minute candles can be delayed or absent; no open is invented.
- Five-second sequential polling misses intrainterval dynamics and is not atomic.
- Slug conventions, schemas, rate limits, access restrictions and public feed
  availability can change; validation failures require investigation.
- Only normalized top-of-book snapshots, market/settlement metadata and quality
  evidence are stored, not raw payload archives or full depth. There is no price backfill.
- Multi-window smoke testing does not establish unattended multi-hour reliability.
  Use one foreground collector per database; multi-process coordination and retention
  are not implemented. SQLite data grows with the observation/poll history.
- Final settlement publication can be delayed or disagree across public APIs. Such
  markets remain unresolved until validated agreement; missing official times stay NULL.
- Run heartbeats conservatively bound crash-truncated coverage; unobserved downtime
  is not included. Keep the system clock synchronized and inspect interrupted polls.
- There are no credentials, private keys, seed phrases, wallet connections,
  transaction signing, order placement/cancellation or live execution components.
- All implemented remote calls are public GETs to the documented endpoints. No
  exchange credentials or authenticated trading SDKs are imported.
- Implementation is independent; no external trading-bot code or GitHub dependency
  was reused, including the prohibited repository named in the sprint constraints.
- No capital, paper engine, optimization, ML, frontend or execution milestone has
  been added. Observe and validate data before testing a hypothesis.
