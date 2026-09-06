# FiveCast M2 Dataset Integrity & Settlement Report

## Scope

**BUILD STATUS: PASS.** Final offline gates and a seven-minute live run completed;
the final process crossed two market boundaries and exited naturally.

M2 extends the approved M0/M1 observer without replacing its feed sources, opening
price definition, Decimal calculations or hard snapshot validity rules. The work
was performed in the existing local repository. `SOUL.md` and the historical
M0/M1 report are unchanged. No strategy, paper engine, execution system or M3 work
was started. Nothing was committed or pushed.

## Architecture Changes

- Preserve the original observer and CLI. Add `collect` mode for unattended sampling,
  compact logs, persisted polling evidence and independent settlement.
- Use one synchronous foreground collector and one standard-library settlement
  thread. Each thread owns its HTTP client and SQLite connection. No network
  request holds a SQLite transaction.
- Use bounded public GET retries for transport failures, HTTP 429 and HTTP 5xx.
  Defaults are three attempts and exponential waits of one and two seconds.
  `Retry-After` is honored up to the configured maximum of 30 seconds.
- Poll the current five-minute market on a fixed schedule. Do not burst requests
  to catch up after slow responses. Record failed cycles and let the report expose
  missing schedule slots.
- Poll expired unresolved markets separately, including markets retained from
  earlier runs. Handle at most ten due markets per pass, oldest-checked first.
  Default settlement interval is 30 seconds.
- Invalid external data is rejected and logged at ERROR in collector mode. Only
  valid normalized observations and independently valid metadata are persisted.
  The failure ledger stores categories, not invalid external payloads.
- Unexpected worker failures retain their exception and traceback and propagate to
  the owning thread. They cannot silently turn a bounded run into a false success.
- Graceful shutdown commits or rolls back the current transaction, closes each
  client/connection, interrupts retry waits, joins the worker and logs a summary.
- Reports use a read-only SQLite URI, `query_only`, and one consistent read
  transaction. They do not import an HTTP client or contact any API.

No new third-party dependency was necessary. Runtime remains `httpx`, `pydantic`
and the standard library. Pytest and Ruff remain the development tools.

## Database Changes

Schema version is tracked with `PRAGMA user_version`, currently **2**.

For a nonempty version-0 database, migration first creates
`<database>.v0.bak` using SQLite's backup API. Inside one transaction it creates
the new tables, validates and copies every old snapshot, adds foreign keys and
quality fields, and advances the version. A failed validation rolls the migration
back. Existing backups are not overwritten; unknown schema versions are rejected.

Actual preservation check against `data/fivecast.db.v0.bak`:

```text
LEGACY_ROWS_PRESERVED 3 True
SCHEMA_VERSION 2
```

Every original column value in all three legacy rows was compared with the
migrated database, including IDs, Decimal strings and creation timestamps.

### Markets

`markets` stores:

```text
market_id, slug, condition_id, question
start_time_utc, end_time_utc, up_token_id, down_token_id
closed, resolved, outcome
resolution_time_utc, resolution_time_source
resolution_observed_at_utc, resolution_source
metadata_source
settlement_checked_at_utc, next_settlement_check_utc, settlement_error_count
created_at_utc, updated_at_utc
```

Window/token metadata can be reconstructed from legacy snapshots, with provenance
`snapshot_backfill`. Unknown question/condition fields remain NULL until Gamma
hydrates them. Market window, slug, token identity and condition identity cannot
silently change. The `closed` flag denotes measurement-window expiry; `resolved`
requires a validated official label.

### Snapshots

All original columns remain. New columns are:

```text
btc_source_timestamp, market_source_timestamp, source_skew_ms
poll_latency_ms, is_stale, run_id
```

`market_id` is an enforced foreign key to `markets`; `run_id` references
`collection_runs`. Decimal values remain TEXT. Repeated identical snapshots remain
idempotent; conflicting core snapshot data is never overwritten. Unknown legacy
latency, stale flags and run identity remain NULL, not guessed.

### Collection Evidence

`collection_runs` records start, heartbeat, scheduling-stop time and polling
interval. `polls` records scheduled/start/finish time, target window slug, optional
market ID, success/failure/interruption state, failure category and cycle latency.
Per-market quality aggregates are computed from this evidence rather than maintained
as mutable counters. SQLite WAL allows the two writers and read-only reporting to
coexist with short transactions.

## Endpoints And Settlement

All remote calls are unauthenticated public GETs:

| Endpoint | Use |
| --- | --- |
| `https://api.exchange.coinbase.com/products/BTC-USD/ticker` | BTC spot research covariate and exchange timestamp |
| `https://api.exchange.coinbase.com/products/BTC-USD/candles` | Exact starting-minute open, `granularity=60` |
| `https://gamma-api.polymarket.com/markets/slug/{slug}` | Identity, window metadata and final lifecycle state |
| `https://clob.polymarket.com/book?token_id=...` | Validated UP and DOWN books |
| `https://clob.polymarket.com/markets/{condition_id}` | Explicit official outcome-token winner flags |

The canonical label comes from CLOB `tokens[].winner`, never Coinbase direction
or a probabilistic quote. Acceptance requires exactly one winner, exactly two
correctly mapped tokens, matching market/condition identities, CLOB `closed=true`,
Gamma `closed=true`, Gamma `umaResolutionStatus="resolved"`, and corroborating
Gamma final `outcomePrices` of exactly 1 and 0.

Publication lag leaves the market unresolved. Contradictory or malformed final
data is logged and rejected. Previously stored final outcomes cannot be replaced
by a conflicting winner.

`resolution_time_utc` comes from Gamma `closedTime` when present, accompanied by
`resolution_time_source="gamma_closedTime"`. This is the platform lifecycle close
time, not a claim about precise on-chain finalization. The separate
`resolution_observed_at_utc` is the first locally confirmed final agreement. Missing
official time stays NULL; neither the scheduled end nor local observation time is
substituted. Naive or out-of-window official timestamps are rejected.

Official references are linked in README. The verified public CLOB `/markets`
winner route is distinct from the current `/clob-markets` parameter-reference
route. Availability and schema are validated, not assumed interchangeable.

## Rollover Bug Found Live

The first diagnostic run started at **2026-09-06 06:17:21 UTC**. It discovered the
06:15-06:20 market and collected **32 snapshots**. At 06:20 it marked that window
closed and discovered the 06:20-06:25 market without restarting.

However, the exact starting candle was initially absent. Direct inspection of the
public Coinbase response at 06:21:27 showed:

```text
HTTP 200
body: []
cache-control: public, max-age=300, must-revalidate
cf-cache-status: HIT
age: 85
```

The approved fixed `start`/`end` candle query had cached an initial miss for the
entire five-minute window. This was a real rollover bug, not a reason to invent
the opening price or relax validation.

Correction: keep `start` fixed at the market start and advance the legitimate
query `end` with current collection time, capped at the market end. Continue to
select only the one-minute candle whose bucket is exactly the market start. Cache
only a validated opening result for the current window. No alternative candle,
startup quote or inferred open is used.

Added an offline cache regression simulating URL-keyed 300-second cached misses.
The original diagnostic run was stopped after **26 recorded candle-unavailable
failures**. Its observations and failure ledger were retained; its unfinished run
is bounded by its last heartbeat at **06:22:06.811010 UTC**. Those failures are
deliberately included in the overall data-quality report.

Two additional reviewed edge cases were fixed before the final live run: fatal
worker exceptions now propagate even in bounded runs, and a bounded duration caps
the recorded scheduling interval so sleep overshoot cannot manufacture an extra
expected sample at the deadline. Both have regression tests.

## Quality Metric Definitions

| Metric | Definition |
| --- | --- |
| BTC source time | Coinbase trade timestamp, unchanged |
| Market source time | Earlier of the UP/DOWN book timestamps |
| Source skew | Maximum minus minimum of all three source timestamps, in milliseconds |
| Snapshot poll latency | Monotonic acquisition/validation duration including retries and clock waits, excluding final insertion |
| Stale snapshot | Oldest source age exceeds the configured polling interval, while still satisfying the approved hard age/skew/window guards |
| Sample count | Actual stored snapshots for the market |
| Expected samples | Configured schedule-grid points in the persisted observable run interval, intersected with the market window |
| Coverage | 100 times run-associated stored samples divided by expected samples; N/A for zero expectation |
| Mean/max gap | Consecutive successful sample gaps within the same run and market |
| Mean/max skew | Aggregates over actual stored source skews |
| Failed poll count | Foreground poll cycles ending in rejection or exhausted failure, not individual recovered HTTP attempts |

For run start `S`, configured interval `I`, and scheduling end `E`, expected points
are `S + n*I`, `n >= 0`, inside `[S, E)`. Graceful duration-limited runs cap `E` at
the explicit deadline. Unfinished runs use their last persisted heartbeat, never
the report wall clock. Per-market expectations also intersect `[market_start,
market_end)`. Different run intervals are retained; downtime between runs is not
bridged. Slow in-process work and retry waits remain part of observable time.

The global expectation includes periods when market discovery failed entirely.
Failures are attributed by target slug when metadata is subsequently available.
Unmatched failures, pending/interrupted polls and settlement errors are separately
visible. Coverage is not capped to conceal anomalies. No samples are fabricated.

Legacy or standalone observer rows without a run ID are excluded from coverage
because their observation schedule is unknown. They remain in sample/skew totals.
Their stale classification and latency are unknown unless explicitly measured.
The three original M0/M1 rows therefore contribute three unknown stale flags.

## Offline Verification

Environment: CPython **3.12.13**, uv **0.11.14**, pytest **9.1.1**, Ruff **0.16.6**.
All external API tests use mocks; the global fixture blocks outbound connections
and DNS. Database tests use temporary files.

Quality gates immediately before the final live run:

```text
uv sync --locked
Resolved 19 packages; checked 19 installed packages

uv run pytest -q
158 passed in 2.36s

uv run ruff check .
All checks passed!

uv run ruff format --check .
31 files already formatted

git diff --check
No whitespace errors (Git emitted its existing LF/CRLF advisory for README.md)
```

Source and wheel builds also succeeded. M2 coverage includes market hydration and
immutable identity, official UP/DOWN outcomes, unresolved/publication-lag states,
malformed final payloads, cached empty candles, retry/backoff/cancellation, delayed
settlement, independent worker progress, fatal-worker propagation, UTC bounds,
stale/skew metrics, grid/gap/coverage calculations, crash-truncated run reporting,
duration overshoot, graceful shutdown, read-only reports, foreign keys, migration
rollback and legacy preservation. Approved M0/M1 regression coverage remains.

Final post-smoke/documentation quality-gate rerun:

```text
uv sync --locked                 PASS (19 packages)
uv run pytest -q                 158 passed in 1.36s
uv run ruff check .              All checks passed!
uv run ruff format --check .     32 files already formatted
git diff --check                 PASS (same README line-ending advisory only)
```

## Final Multi-Window Live Test

After the candle-cache correction and all offline gates passed, executed:

```powershell
uv run python -m fivecast.main collect --duration 420
```

Final run ID **2** started at **2026-09-06 06:23:18.865146 UTC** and stopped
naturally at approximately **06:30:18 UTC**. Its persisted scheduling end is
`2026-09-06T06:30:18.863012+00:00`.

```text
COLLECTION SUMMARY run=2 attempted=84 saved=80 failed=4 interrupted=0
```

The same process collected across three market IDs and two transitions:

| Market | UTC Window | Final-Run Samples / Expected |
| --- | --- | --- |
| 4240502 | 06:20-06:25 | 21 / 21 |
| 4240512 | 06:25-06:30 | 58 / 60 |
| 4240532 | 06:30-06:35 | 1 / 3 before bounded shutdown |

Key log evidence:

```text
06:24:59.468 SNAPSHOT market=4240502 remaining=0.5s
06:25:03.861 WINDOW CLOSED market=4240502
06:25:03.881 DISCOVERED market=4240512
06:25:15.205 SNAPSHOT market=4240512 remaining=284.8s
06:29:59.408 SNAPSHOT market=4240512 remaining=0.6s
06:30:03.862 WINDOW CLOSED market=4240512
06:30:03.882 DISCOVERED market=4240532
06:30:15.143 SNAPSHOT market=4240532 remaining=284.9s
06:30:18.864 COLLECTION SUMMARY run=2 attempted=84 saved=80 failed=4 interrupted=0
```

At each boundary, two genuine candle-unavailable polls were recorded. The opening
candle became available by the third attempt, and collection resumed without a
restart or an invented opening price. The final run achieved **95.2381% coverage**.
It had no stale accepted snapshots and no interrupted/pending poll at shutdown.

## Settlement Verification

The retained M0/M1 market was successfully hydrated and resolved through the new
worker, independently of snapshot collection:

```text
market_id                    4240043
slug                         btc-updown-5m-1788672900
resolved                     true
outcome                      UP
resolution_time_utc          2026-09-06T05:41:25.000000+00:00
resolution_time_source       gamma_closedTime
resolution_observed_at_utc   2026-09-06T06:17:22.492973+00:00
resolution_source            polymarket_clob_winner+gamma_final
```

Recent expired markets **4240488** and **4240502** reached Gamma-final state but
CLOB still reported `closed=false` with both winner flags false during the live
run. Warnings continued while new snapshots were saved. A direct post-run check
confirmed these CLOB responses were `cf-cache-status: DYNAMIC`, not the Coinbase
cached-miss issue. They remain unresolved rather than receiving guessed labels.

Therefore **one official live settlement is persisted**; settlement of a newly
expired smoke-test market was not confirmed within the bounded run. Pending
markets remain in SQLite and are retried on the next collection run. This is an
explicit publication-lag limitation, not a claimed successful resolution of every
closed market. Offline tests cover both UP and DOWN final labels and delayed agreement.

## Data Quality Results

Actual output of `uv run python -m fivecast.report --per-market --json` after shutdown:

| Aggregate | Result |
| --- | --- |
| Markets observed / resolved | 5 / 1 |
| Total snapshots | 115 |
| Preserved M0/M1 snapshots | 3 |
| M2 run-associated snapshots | 112 |
| Expected M2 samples across both runs | 142 |
| Cumulative coverage | 78.8732% |
| Mean / maximum source skew | 1540.767 ms / 5225.250 ms |
| Mean / maximum interior sample gap | 4.977393 s / 5.455100 s |
| Stale / unknown-staleness snapshots | 1 / 3 |
| Failed foreground polls | 30 |
| Interrupted / pending / unattributed-failure polls | 0 / 0 / 0 |

The cumulative result intentionally includes the initial diagnostic run's **26
failures** from the cached empty response. It was not deleted to improve coverage.
The final corrected run contributed **80 samples from 84 expected slots**, with
only **four genuine candle-publication misses**. The complete 06:25-06:30 market
had **58/60 samples (96.6667%)**.

Gap statistics describe interior consecutive observations only; they do not bridge
separate runs or market boundaries. Leading/trailing gaps and completely failed
periods remain visible through coverage and the failed-poll ledger, even when the
mean interior gap is close to five seconds.

## SQLite Verification

Opened the final database using a read-only URI and revalidated every snapshot
through `MarketSnapshot`. Compared every snapshot's window and token IDs with its
persisted market. Recomputed stored source skew and configured stale classification.

```text
VALIDATED_SNAPSHOTS 115
NEGATIVE_OR_ZERO_REMAINING 0
FOREIGN_KEY_CHECK []
INTEGRITY ok
MARKET_METADATA_MATCH True
QUALITY_METRICS_MATCH True

Snapshot groups:
legacy run_id=NULL: 3
diagnostic run_id=1: 32
final run_id=2: 80

Poll outcomes:
run 1: 32 success, 26 failed
run 2: 80 success, 4 failed
```

All five observed market IDs have metadata. All source/snapshot times satisfy the
validated UTC and active-window rules. The final run has a graceful stop record;
the explicitly stopped diagnostic run retains its last heartbeat. Both tracked
smoke processes have stopped; no collector is left running automatically.

## Security Review

Repeated searches covered source, tests, configuration, dependency lockfile and
documents for `private_key`, `PRIVATE_KEY`, `seed`, `mnemonic`, `wallet`, `sign`,
`signature`, `order`, `place_order`, `cancel_order`, `api_secret`, and `LiveExecutor`.
Hits are safety statements (including the pre-existing SOUL prohibition), public
book-readiness metadata, SQL ordering and ordinary research wording.

There is no wallet, private-key handling, signing, order creation/cancellation,
trading credential or live executor implementation. Public outcome-token IDs are
metadata, not credentials. No authenticated trading SDK or questionable GitHub
dependency is present. `uv tree --locked` confirms the unchanged PyPI dependency
tree. This is an execution-capability/dependency-origin audit, not a comprehensive
third-party vulnerability assessment.

## Known Limitations

- A multi-window smoke test is not evidence of full overnight or multi-day uptime.
- Coinbase prices remain independent research covariates, not the Chainlink TWAP
  settlement benchmark. No experimental labels are inferred from spot returns.
- Genuine opening-candle delays, API outages and invalid/stale data cause visible
  gaps. The fixed cached-miss issue does not justify accepting unavailable data.
- Official API publication may lag or disagree. Markets remain unresolved until
  corroborated final data appears; no deadline forces a label.
- Gamma `closedTime` is a lifecycle timestamp, not guaranteed exact finalization
  time. Missing official times remain unknown and observation time is separate.
- Sequential polling remains bounded-skew rather than atomic across venues.
- Host UTC synchronization, machine wakefulness, disk capacity and internet access
  are operational requirements. An in-flight worker request can delay shutdown up
  to its configured timeout.
- Use one collector process per database. Multi-process orchestration, retention,
  raw payload/depth archives and price backfill are outside this milestone.
- The retained stopped diagnostic run intentionally lowers cumulative coverage.
  Its heartbeat bounds observation time; no historical rows were removed to improve
  the result.

## Overnight Command

From the repository root:

```powershell
uv sync --locked
uv run python -m fivecast.main collect
```

Inspect data from another terminal without network access:

```powershell
uv run python -m fivecast.report --per-market
```

No M3 work has been started.
