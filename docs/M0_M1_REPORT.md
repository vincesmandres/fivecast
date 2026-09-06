# FiveCast M0–M1 Build Report

## Status And Scope

**PASS.** Built the first functional read-only research milestone in the existing
local repository. Despite the requested report filename, this report covers the
full sprint M0 through M8: foundation, BTC and Polymarket feeds, synchronized
snapshots, SQLite, CLI, offline tests, live smoke verification and documentation.

`SOUL.md` and the original README were read before implementation. `SOUL.md` is
unchanged. No second repository, nested project, commit or push was created.
No paper or live execution capability was implemented.

## What Was Built

- Python 3.12+ `src` package, `pyproject.toml`, uv lockfile and reproducible
  runtime/development installation from PyPI.
- Validated TOML/environment configuration and UTC logging.
- Public Coinbase BTC-USD ticker and exact starting-minute opening candle.
- Gamma discovery of the current BTC five-minute UP/DOWN market.
- Public CLOB books, validated token identity and best bid/ask extraction.
- Immutable Pydantic models with Decimal prices, UTC timestamps and invariants.
- Bounded-age/skew snapshots, explicit opening-price provenance and derived values.
- Local SQLite schema creation, exact decimal text, safe insertion, count and latest
  retrieval, idempotent repeats and loud conflict detection.
- Five-second observer with continuous and bounded CLI modes and Ctrl+C cleanup.
- An entirely offline automated suite and a credential-free live smoke run.

## Architecture Decisions

Use synchronous `httpx` polling because the initial five-second research cadence
does not justify stream reconnection, subscriptions and book reconstruction.
Coinbase recommends WebSocket updates for real-time workloads; those are a future
data-engineering option, not a requirement for this low-frequency milestone.

Use Pydantic for consumed external fields and normalized models. Ignore unrelated
external metadata, reject malformed fields and cross-field inconsistencies.
Models are immutable and reject unexpected internal fields. Monetary values are
Decimal, not runtime pandas or binary-float prices. Default Decimal precision is
28 digits; snapshot display rounding does not change persisted values.

Use `sqlite3`, not an ORM or service. Persist enough source timing, token identity
and opening-method provenance to interpret each normalized snapshot. No heavy
framework, asynchronous runtime, cloud database or container is needed.

The snapshot timestamp follows source acquisition. Each quote must be in-window,
non-future and within configured age/skew bounds. A bounded real wait handles a
source clock lead of at most one second without changing any validation rule.
Malformed payloads intentionally terminate the collector to make failure visible;
only explicitly classified availability/transport failures retry in continuous mode.

The open is the Coinbase one-minute candle's first trade for the exact market-start
bucket. It is never a startup quote or a fallback candle. Persist acquisition time
and method; refresh at rollover. Missing exact buckets prevent snapshot creation.

## External APIs And Rationale

| Interface | Purpose And Rationale |
| --- | --- |
| Coinbase `GET /products/BTC-USD/ticker` | Reputable public USD spot price with exchange trade time; no authentication |
| Coinbase `GET /products/BTC-USD/candles` | Deterministic opening observation for mid-window startup; `granularity=60` |
| Gamma `GET /markets/slug/{slug}` | Direct current-window discovery without scanning unrelated markets |
| CLOB `GET /book?token_id=...` | Public outcome books with timestamps and explicit asset/condition identity |

Bases are `https://api.exchange.coinbase.com`,
`https://gamma-api.polymarket.com`, and `https://clob.polymarket.com`.
Official reference links are in the README. API research used only official
documentation and public read-only responses, not trading repositories.

Important verified contract details:

- Slug convention: `btc-updown-5m-{aligned_window_epoch}`.
- `eventStartTime` is the five-minute start; `startDate` is the earlier listing time.
- `outcomes` and `clobTokenIds` are JSON-encoded arrays mapped by position.
- Book timestamps are Unix milliseconds. Book sorting is not assumed.
- Gamma's `ready`/`funded` flags were not useful readiness gates in the observed
  market. Active/unclosed/unarchived/book-enabled metadata and validated CLOB
  responses are used instead, with `acceptingOrders` read only as a flag.
- Current market metadata referenced Chainlink BTC/USD 60-second TWAP. Coinbase
  spot returns are independent covariates, not settlement-price estimates.

## Files Created And Modified

Modified: `README.md`.

Created foundation files: `pyproject.toml`, `uv.lock`, `.gitignore`, `.env.example`,
`config/settings.example.toml`, `data/.gitkeep`.

Created package files:

```text
src/fivecast/__init__.py
src/fivecast/config.py
src/fivecast/models.py
src/fivecast/main.py
src/fivecast/feeds/__init__.py
src/fivecast/feeds/btc.py
src/fivecast/feeds/polymarket.py
src/fivecast/market/__init__.py
src/fivecast/market/snapshot.py
src/fivecast/storage/__init__.py
src/fivecast/storage/sqlite.py
```

Created test files:

```text
tests/conftest.py
tests/test_models.py
tests/test_btc_feed.py
tests/test_polymarket_feed.py
tests/test_snapshot.py
tests/test_storage.py
tests/test_config.py
tests/test_main.py
```

Created documentation: `docs/M0_M1_REPORT.md`.

Generated locally and ignored: `.venv`, caches, `dist` distributions and
`data/fivecast.db`. The SQLite database is local evidence, not a committed fixture.

## Tests And Checks

Environment: Windows, CPython **3.12.13**, uv **0.11.14**.
Runtime versions: httpx **0.28.1**, Pydantic **2.13.5**.
Test/lint versions: pytest **9.1.1**, Ruff **0.16.6**.

Executed:

```text
uv sync --python 3.12
uv run pytest -q
100 passed in 0.61s

uv run ruff check .
All checks passed!

uv run ruff format --check .
22 files already formatted

uv pip check
Checked 19 packages in 2ms
All installed packages are compatible

uv build
Successfully built dist\fivecast-0.1.0.tar.gz
Successfully built dist\fivecast-0.1.0-py3-none-any.whl
```

The 100 cases cover model invariants, finite positive prices, malformed BTC and
Polymarket data, incorrect probability ranges, crossed books, empty sides, missing
and reversed outcome mappings, token/condition mismatches, hex casing, timestamps,
exact candle selection and precision, missing/duplicate candles, UTC conversion,
snapshot deltas and remaining time, age/skew bounds, opening-window mismatch,
rollover, one-second clock handling, SQLite roundtrips/latest ordering/duplicates,
safe SQL parameters, configuration precedence/errors, end-to-end observation,
bounded failures, retry logging and Ctrl+C.

Tests do not use live internet. `httpx.MockTransport` or injected feed methods
replace requests; a global fixture blocks socket connections and DNS. Databases
use pytest temporary directories. The live smoke is a separate manual CLI run.

## Live Smoke-Test Result

**PASS**, 2026-09-06, no credentials and public GET requests only.

Initial run at approximately 05:36:28 UTC exited without storing a row because
strict synchronization detected a future-dated source. A diagnostic read showed
the DOWN book timestamp `05:36:40.287Z` while the host was at
`05:36:40.176822Z`, a roughly 110 ms clock lead. This was not a schema mismatch.

The implementation now waits up to one second for small source clock leads before
taking the final snapshot time, then applies the original strict checks. It still
rejects larger leads and never rewrites source times. Added tests for a 200 ms lead
and a rejected two-second lead. Reran the complete suite and lint checks before
retrying. No validation was bypassed.

Successful command:

```powershell
uv run python -m fivecast.main --iterations 3
```

Three snapshots were saved at approximately **05:37:40**, **05:37:45** and
**05:37:49 UTC** for Gamma market **4240043**, slug
`btc-updown-5m-1788672900`, window **05:35:00 to 05:40:00 UTC**.

Sanitized final snapshot:

```text
timestamp_utc              2026-09-06T05:37:49.937794Z
market_id                  4240043
seconds_remaining          130.062206
btc_window_open_price      79930.78
btc_price                  80000
btc_delta_usd              69.22
btc_delta_pct              0.0008659993058994294813587456547
up_bid / up_ask             0.98 / 0.99
down_bid / down_ask         0.01 / 0.02
up_spread / down_spread     0.01 / 0.01
source_btc                 coinbase_exchange
source_prediction_market   polymarket_clob
btc_open_method            coinbase_1m_candle_open
btc_open_observed_at_utc    2026-09-06T05:37:39.486136Z
```

The BTC price was plausible for the live source, and every probability was within
`[0,1]`, with uncrossed books. Exact source times in the stored final snapshot:

- BTC: `05:37:48.212775Z`, age **1.725019 seconds**.
- UP: `05:37:49.699000Z`, age **0.238794 seconds**.
- DOWN: `05:37:49.937000Z`, age **0.000794 seconds**.

All source times and snapshot times aligned with the active window. The opening
candle was selected from the exact 05:35:00 bucket, not the collector start.

## SQLite Verification

Opened `file:data/fivecast.db?mode=ro` using `sqlite3`, counted the rows, ran
`PRAGMA integrity_check`, retrieved the latest row and revalidated it through
`MarketSnapshot`:

```text
SNAPSHOT_COUNT 3
INTEGRITY ok
CREATED_AT 2026-09-06T05:37:49.937794+00:00
```

The database retains Decimal strings, source timestamps and opening provenance.
Duplicate behavior and timestamp-based latest selection were also verified offline.

## Security Review

Searched project source, tests, configuration, lockfile and documents for:

```text
private_key PRIVATE_KEY seed mnemonic wallet sign signature order
place_order cancel_order api_secret
```

Matches were reviewed rather than interpreted as execution capability. Relevant
hits are safety statements in documents, read-only Gamma `enableOrderBook` and
`acceptingOrders` fields and fixtures, SQL `ORDER BY`, and ordinary words such as
signal/ordering. There is no wallet, key handling, seed phrase input, signing,
authenticated credential creation, order placement or cancellation implementation.

Dependency inspection using `uv tree --locked` and `uv.lock` confirmed all 18
third-party runtime/development packages come from PyPI; FiveCast itself is the
editable current directory. Build backend is PyPI `hatchling`. There are no GitHub
or trading-SDK dependencies. No prohibited external repository was used. This is
a capability/dependency-origin audit, not a comprehensive vulnerability assessment.

## Known Limitations And Risks

- Coinbase spot and opening trades differ from Chainlink TWAP market resolution.
  Do not treat positive spot delta as a known winning outcome.
- Public candles may be delayed, missing or revised. Acquisition provenance is
  recorded; no raw payload archive or historical backfill is implemented.
- Polling misses intrainterval events. Sequential requests and independent clocks
  mean observations are bounded-skew, not simultaneous.
- Public API availability, region restrictions, slug conventions and payload
  contracts can change. No access restrictions are bypassed.
- Stale/missing observations create visible collection gaps. Malformed payloads
  deliberately stop the process for investigation rather than silently dropping data.
- Exact first-trade timestamps inside opening candles are unavailable through the
  candle endpoint. Host UTC clock synchronization remains an operational requirement.
- Short live smoke coverage is not evidence of long-run reliability; rollover is
  tested offline but was not part of this three-row live run.
- No raw depth, settled outcomes, settlement metadata archive, schema migrations
  or multi-process collector coordination exists in this milestone.
- No strategy performance or predictive edge has been established by this work.

## Next Recommended Milestone

Improve research dataset integrity before any execution work: longer read-only
collection across many windows, explicit gap/latency reporting, persisted market
resolution metadata and settled outcomes, and an offline replay/quality report.
Add settlement-aligned Chainlink data only through a verified public interface.
Then define a pre-registered baseline with out-of-sample evaluation.

Do not implement paper execution, capital deployment or live execution as part of
this milestone. The recommended next milestone has not been started.
