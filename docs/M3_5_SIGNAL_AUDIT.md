# FiveCast M3.5 Signal Density & Hypothesis Audit

## Scope

**PASS.** This diagnostic uses local SQLite only and evaluates no holdout result.
It adds `fivecast.audit`, a read-only module that does not import feed clients,
write a database row, create shadow trades, rank strategies, calculate PnL, or make
any profitability claim. The approved M0-M3 collector/replay behavior is unchanged.

The audit applies M3's existing default quality selection, then uses the earliest
chronological 70% only. It selected **39 research markets** from 57 eligible resolved
markets; **19** markets were excluded before splitting. Holdout has 18 markets and
was not passed into any audit calculation or command. The counts below describe this
single local dataset and are not estimates of a general market process.

## Semantics

For each absolute threshold and remaining-time bucket, each market counts once when
the first stored snapshot satisfying both `abs(delta) >= threshold` and
`seconds_remaining <= bucket` is found. That exact earliest eligible snapshot fixes
side and quote: nonnegative delta uses UP; negative delta uses DOWN. A missing quote
remains missing and is not replaced by a later quote. Thus crossing counts may exceed
available price observations.

Price buckets are cumulative threshold counts from the first crossing in the broad
T120 bucket: `<=0.60`, `<=0.70`, ..., `<=0.95`, and separately `>0.95`. Percentiles
use deterministic linear interpolation over sorted observed values. Momentum-at-T
values are absolute delta from the latest snapshot at or before the specified
remaining-time cutoff; no forward observation or interpolation is used.

Blocker counts cover 3,900 configuration-market evaluations (39 markets times the
unchanged 100 configuration grid). A market can have several all-applicable blockers.
The first blocker is prioritized as delta, time, quote, entry price, spread, skew,
stale. A later valid snapshot can produce a signal; an earlier bad quote is not used
to hide it. Funnels use fixed diagnostic limits rather than selected parameters.

## Threshold Matrix

Markets reaching absolute delta; percent of 39 research markets.

| Delta USD | T<=120 | T<=90 | T<=60 | T<=30 | T<=15 |
| --- | --- | --- | --- | --- | --- |
| 20 | 29 (74.4%) | 27 (69.2%) | 27 (69.2%) | 23 (59.0%) | 20 (51.3%) |
| 30 | 18 (46.2%) | 17 (43.6%) | 16 (41.0%) | 15 (38.5%) | 14 (35.9%) |
| 40 | 13 (33.3%) | 11 (28.2%) | 11 (28.2%) | 9 (23.1%) | 9 (23.1%) |
| 60 | 6 (15.4%) | 6 (15.4%) | 6 (15.4%) | 5 (12.8%) | 4 (10.3%) |
| 80 | 3 (7.7%) | 3 (7.7%) | 3 (7.7%) | 2 (5.1%) | 2 (5.1%) |
| 100 | 3 (7.7%) | 3 (7.7%) | 2 (5.1%) | 2 (5.1%) | 2 (5.1%) |
| 120 | 2 (5.1%) | 2 (5.1%) | 2 (5.1%) | 2 (5.1%) | 1 (2.6%) |

## Price Response

At first broad T120 eligibility, observed selected-ask summaries:

| Delta USD | Quoted Markets | Mean | Median | P25 | P75 | Min-Max |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 20 | 22 | 0.919 | 0.950 | 0.903 | 0.970 | 0.73-0.99 |
| 30 | 11 | 0.952 | 0.970 | 0.945 | 0.985 | 0.80-0.99 |
| 40 | 7 | 0.966 | 0.970 | 0.950 | 0.985 | 0.93-0.99 |
| 60 | 1 | 0.970 | 0.970 | 0.970 | 0.970 | 0.97-0.97 |
| 80+ | 0 | N/A | N/A | N/A | N/A | N/A |

Later buckets are sparse: D40/T90 had four quotes, mean 0.985; D60/T60 had one
quote at 0.99; D80/T30 had none. This is descriptive of stored observations only.

Full price-response matrix as `quoted markets / mean selected ask`; `-` means no
valid selected-side ask at the first eligible crossing:

| Delta USD | T<=120 | T<=90 | T<=60 | T<=30 | T<=15 |
| --- | --- | --- | --- | --- | --- |
| 20 | 22 / .919 | 19 / .948 | 10 / .938 | 2 / .415 | 1 / .010 |
| 30 | 11 / .952 | 10 / .983 | 2 / .990 | 1 / .010 | 1 / .010 |
| 40 | 7 / .966 | 4 / .985 | 1 / .990 | - | - |
| 60 | 1 / .970 | 1 / .980 | 1 / .990 | - | - |
| 80 | - | - | - | - | - |
| 100 | - | - | - | - | - |
| 120 | - | - | - | - | - |

## Price Buckets

First broad T120 crossing asks, cumulative counts:

| Threshold | <=.60 | <=.70 | <=.75 | <=.80 | <=.85 | <=.90 | <=.95 | >.95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| D20 | 0 | 0 | 1 | 3 | 4 | 6 | 14 | 8 |
| D30 | 0 | 0 | 0 | 1 | 1 | 1 | 3 | 8 |
| D40 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 5 |
| D60 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| D80+ | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Signal Blockers

First blocker across 3,900 grid evaluations:

| Blocker | Count | Percent |
| --- | ---: | ---: |
| DELTA_NOT_REACHED | 3,320 | 85.1% |
| NO_VALID_QUOTE | 466 | 11.9% |
| TIME_NOT_ELIGIBLE | 100 | 2.6% |
| ENTRY_PRICE_TOO_HIGH | 10 | 0.3% |
| SIGNAL | 4 | 0.1% |

All-applicable counts retain overlapping reasons: delta 3,320, no valid quote 466,
time 100, entry price 76, signal 4. No selected research candidate was finally
blocked first by spread, skew, or stale under the baseline limits. The dominant
constraint is threshold frequency, followed by missing valid selected-side quotes;
this does not establish why those quotes were missing.

## Representative Funnels

Fixed diagnostic limits: selected ask <=0.90, spread <=0.05, skew <=5,000 ms, fresh.

| Stage | D20/T120 | D40/T90 | D60/T60 | D80/T30 |
| --- | ---: | ---: | ---: | ---: |
| Research markets | 39 | 39 | 39 | 39 |
| Delta reached anywhere | 31 | 15 | 6 | 3 |
| Within time bucket | 29 | 11 | 6 | 2 |
| Valid quote | 22 | 4 | 1 | 0 |
| Ask limit | 6 | 0 | 0 | 0 |
| Spread limit | 6 | 0 | 0 | 0 |
| Skew limit | 6 | 0 | 0 | 0 |
| Fresh | 6 | 0 | 0 | 0 |
| Diagnostic signals | 6 | 0 | 0 | 0 |

## Momentum Distribution

Absolute USD delta distribution by market, using maxima or the specified cutoff.

| Measure | Median | P75 | P90 | P95 | Max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Max positive | 8.73 | 29.07 | 48.84 | 55.25 | 139.89 |
| Max negative magnitude | 24.57 | 37.76 | 57.42 | 80.54 | 147.39 |
| Max absolute | 33.94 | 49.84 | 74.02 | 117.45 | 147.39 |
| T-120 absolute | 22.02 | 28.91 | 43.93 | 73.56 | 139.89 |
| T-90 absolute | 24.39 | 32.92 | 47.74 | 73.35 | 131.23 |
| T-60 absolute | 21.19 | 30.23 | 50.85 | 100.48 | 118.92 |
| T-30 absolute | 21.80 | 34.15 | 60.91 | 79.72 | 137.08 |
| T-15 absolute | 23.59 | 37.80 | 60.91 | 79.79 | 115.89 |

## Repricing Relationship

All selected-side quote observations, non-overlapping absolute-delta bins:

| Abs Delta USD | Observations | Mean Ask | Median | P25 | P75 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0-20 | 1,154 | 0.635 | 0.660 | 0.500 | 0.808 |
| 20-40 | 485 | 0.851 | 0.900 | 0.780 | 0.960 |
| 40-60 | 93 | 0.942 | 0.970 | 0.900 | 0.980 |
| 60-80 | 27 | 0.963 | 0.970 | 0.950 | 0.980 |
| 80-100 | 5 | 0.982 | 0.990 | 0.980 | 0.990 |
| 100+ | 2 | 0.999 | 0.999 | 0.999 | 0.999 |

This describes association in the stored data, not causal repricing or an implied
fair probability. Samples in the high-delta bins are extremely small.

## Tests And Quality Gates

Offline audit tests cover first crossings, UP/DOWN mapping, time buckets, cumulative
price buckets, blocker behavior including a later valid candidate, funnels,
distribution, repricing, and the research/holdout boundary. Full repository gates
were rerun after M3.5 implementation.

```text
uv sync --locked                 PASS (19 packages checked)
uv run pytest -q                 180 passed in 2.30s
uv run ruff check .              All checks passed!
uv run ruff format --check .     45 files already formatted
git diff --check                 PASS (only existing CRLF advisories)
```

## Interpretation And Limitations

The existing late-momentum threshold range is often rare in this short sample:
only 33.3% of research markets reached D40 by T120 and 7.7% reached D80. When D40+
was first observed, quoted asks were commonly already above 0.95, while selected-side
quotes were often unavailable in later windows. The original late-momentum hypothesis
is not frequent enough in this dataset to justify further implementation or claims.

This is not evidence that any lower threshold is predictive, economical, executable,
No settlement outcomes were used by this audit beyond M3's pre-existing resolved-market
eligibility requirement, and none were inspected or reported. Holdout remains untouched.

## Next Recommended Action

Continue read-only collection across more independent days. Before any new strategy
work, pre-register minimum sample size, quality criteria, a frozen diagnostic plan,
and a rule for whether a hypothesis is frequent enough to study. Do not optimize
thresholds from this audit and do not evaluate holdout data yet.
