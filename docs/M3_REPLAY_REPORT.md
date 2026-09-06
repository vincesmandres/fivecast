# FiveCast M3 Replay Engine + Shadow Strategy Lab Report

## Status

**PASS.** M3 adds only offline historical analysis to the approved M0-M2 data
collector. `SOUL.md` is unchanged. No feeds, wallet, keys, signing, capital,
credentials, orders, cancellation, or execution code was added. No holdout evaluation
was run automatically, no commit was made, and no push occurred.

## Architecture And Lookahead Protections

`ReplayStore` opens the existing database with SQLite `mode=ro` and `query_only`.
It validates schema version 2 or 3, official market state, each snapshot model,
source quality fields, chronology, and snapshot-market identity. It cannot contact
the internet. M3 persistence occurs only later through the existing local
`SnapshotStore`, into separate version-3 research tables.

For each eligible resolved market, snapshots are queried `ORDER BY timestamp_utc`.
Timestamps must be strictly increasing. `ReplayEngine` exposes `StrategyContext`
with only the current snapshot, an immutable tuple ending at current time, snapshot
quality, and features calculated from that same prefix. The context intentionally
has no outcome field. Official outcome stays inside `MarketReplay` and is passed to
the fill simulator only after the strategy loop stops. The engine stops on the first
non-`NO_SIGNAL` decision, enforcing at most one shadow trade per market.

There is no future interpolation. Historical velocities choose the latest recorded
BTC price at or before the trailing cutoff; unavailable lookback yields NULL-like
`None`. Volatility is population standard deviation of recorded BTC percentage
deltas in the inclusive 30/60-second historical window. Replaying the same SQLite
snapshot data and parameters is deterministic.

## Strategy And Fill Assumptions

`LateMomentumStrategy` v1 is the requested simple baseline, not a trading system.
At each historic point it tests signed BTC delta and timing/ask/spread/skew/stale
guards. Defaults are threshold 80 USD, remaining time at most 60 seconds, maximum
ask 0.85, maximum spread 0.05 and source skew at most 5,000 ms. It rejects unknown
legacy stale fields as well as true stale fields.

The shadow accounting uses the selected displayed ask, never a midpoint: UP uses
`up_ask`, DOWN uses `down_ask`. Slippage and fee inputs are basis points and default
to zero. The experiment used zero/zero. A binary official outcome supplies payout
one or zero only after the decision. Gross PnL excludes fees; net PnL subtracts them.
No liquidity/depth, partial fills, queue priority, fee schedule, latency-to-fill or
market impact is modeled.

## Database Additions

Schema version **3** adds only:

- `strategy_runs`: strategy/version, canonical parameters, split metadata, quality
  selection counts, snapshot count and chronological bounds.
- `shadow_trades`: one local shadow result per strategy run and market, linked by
  foreign keys and constrained to `BUY_UP`/`BUY_DOWN` and official `UP`/`DOWN`.

The M3 migration from version 2 is additive and transactional. It neither rebuilds
nor modifies `snapshots`, `markets`, `polls`, or `collection_runs`. The real database
contains 3,709 raw snapshots before and after the experiment; source tables are
unchanged by replay and M3 output persistence.

## Split And Grid

Default replay selected **57** eligible official-resolved markets and excluded 18:

```text
no_snapshots: 3
stale:        12
unresolved:    3
```

Chronological default split: **39 research** markets and **18 holdout** markets.
Research bounds were 2026-09-06 06:20:00 UTC through 10:00:00 UTC. Holdout bounds
were 10:00:00 UTC through 14:55:00 UTC. The endpoint at 10:00 is a boundary between
distinct market windows, not shared data. Holdout was never loaded by grid ranking.

The grid contains exactly 100 parameter configurations: 5 delta thresholds times 4
time limits times 5 entry limits. Ranking uses research split EV per trade, then
trade count and fixed parameter tie breaks. It does not use holdout results.

## Actual Offline Commands And Results

```text
uv run python -m fivecast.replay
Eligible resolved markets: 57
Excluded markets: 18

uv run python -m fivecast.experiment --top 10
Eligible markets=57 excluded=18 research=39 holdout=18 grid=100
```

The research grid persisted **100** `strategy_runs`, all split `research`, and only
**4** shadow trades across those configurations. The actual top-ten output was:

```text
D40 T90 P0.85   1  100.0%  0.830   +0.1700  +0.1700
D40 T90 P0.90   1  100.0%  0.830   +0.1700  +0.1700
D40 T120 P0.85  1  100.0%  0.830   +0.1700  +0.1700
D40 T120 P0.90  1  100.0%  0.830   +0.1700  +0.1700
D40 T30 P0.70   0  N/A       N/A      N/A      +0.0000
D40 T30 P0.75   0  N/A       N/A      N/A      +0.0000
D40 T30 P0.80   0  N/A       N/A      N/A      +0.0000
D40 T30 P0.85   0  N/A       N/A      N/A      +0.0000
D40 T30 P0.90   0  N/A       N/A      N/A      +0.0000
D40 T60 P0.70   0  N/A       N/A      N/A      +0.0000
```

This is preliminary and insufficient evidence. Four configurations each happened
to produce one research-split UP shadow action on the same market at an ask of 0.83.
This is not a profitability claim, a validated edge, a pricing model, or a basis to
select an implementation. The other displayed configurations had no eligible action.
No holdout command was executed; `shadow_trades` contains no holdout records.

## Tests And Quality Gates

Final M3 test run:

```text
uv run pytest -q
175 passed in 2.15s
```

Additional M3 coverage includes chronological replay, no-future context, hidden
outcomes, one-action limit, BTC velocity/volatility windows, UP/DOWN/no-signal
guards, ask-only fills, win/loss settlement, fees/slippage, grid cardinality,
drawdown/profit factor/naive edge, quality filtering, corrupt-data rejection,
research-only parameter persistence, and explicit holdout input restrictions.
M0-M2 tests remain offline with networking blocked.

Commands rerun after M3 integration:

```text
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
```

Final exact outputs are recorded in the build response; no internet command was
used for M3 replay or experiment verification.

Final post-documentation gate rerun:

```text
uv sync --locked                 PASS (19 packages checked)
uv run pytest -q                 175 passed in 2.15s
uv run ruff check .              All checks passed!
uv run ruff format --check .     42 files already formatted
git diff --check                 PASS (only existing CRLF advisories)
```

## Limitations

- Only 57 eligible resolved markets and four research shadow actions exist. The
  sample is far too small for any predictive or profitability conclusion.
- The reported top configuration is an observed research-split result only. It is
  not evaluated, ranked, or selected on holdout data.
- Standard collection snapshots do not model executable depth, matching, fees,
  slippage, partial fills, timing races or settlement mechanics beyond binary payout.
- Coinbase is a research covariate, not the Polymarket settlement benchmark.
- Quality filters are intentionally market-wide strict: one stale or over-threshold
  snapshot rejects that whole market. This reduces data and does not repair it.
- SQLite raw source observations remain immutable but source API collection quality
  limitations documented in M2 still apply.

## Next Recommended Milestone

Collect substantially more read-only data across independent days, pre-register a
small hypothesis and quality protocol, then explicitly evaluate one frozen research
configuration once on holdout. Do not add execution, capital, or automatic strategy
selection in the next step.
