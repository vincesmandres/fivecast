# FiveCast M6.5 Operational Readiness

## Scope

M6.5 hardens the already frozen M6 prospective experiment. It does not change the
M5 model, coefficients, feature construction, scaler, quality policy, baseline,
cost assumptions, edge threshold, buckets, or sample gates. It remains paper-only:
no wallet, keys, signing, order placement, cancellation, capital, or execution code.

## Canonical Commands

```powershell
uv run python -m fivecast.m6 start --model-version 1 --experiment-id m6-20260906-001
uv run python -m fivecast.main collect
uv run python -m fivecast.m6 collect --experiment 1 --interval 5
uv run python -m fivecast.m6 settle
uv run python -m fivecast.m6 status --experiment 1
uv run python -m fivecast.m6 report --experiment 1 --detailed
```

The raw collector and M6 monitor are intentionally separate. The collector obtains
approved public read-only observations and official settlement metadata. The M6
monitor reads persisted snapshots, derives frozen features, stores paper evidence,
and creates no requests to any trading endpoint.

## Resume And Settlement

Starting again with an existing experiment ID and the same model ID returns the
original experiment. A different model binding is rejected. The manifest hash and
start timestamp stay unchanged. Snapshot periods missed during shutdown, sleep, or
network failure remain missing; the monitor never synthesizes a catch-up observation.

Prediction identity is unique by model, market, and timestamp. Paper action identity
is unique by model and market. M6 links are unique by experiment and prediction.
Existing feature records, asks, edge values, and entries are not recalculated during
resume. Settlement only attaches independently verified `markets.outcome`; it writes
the original-entry paper result and cannot replace an existing forward outcome.

`m6 settle` invokes the existing public settlement infrastructure once, then attaches
already verified official outcomes to pending isolated M6 forward records. It is safe
to rerun.

## Reporting Methodology

The detailed report is read-only and includes frozen calibration/edge buckets, paper
metrics, four fixed UTC blocks, daily metrics, data-quality counters, and robustness
diagnostics. Brier/log-loss and bootstrap inputs aggregate by market, not by snapshot.
The deterministic bootstrap uses the manifest's seed `20260906` and 1,000 replicates.
Fewer than two independent markets report `UNAVAILABLE` confidence intervals.

Concentration diagnostics report absolute best-trade, top-five, and best-day PnL plus
the result with best/top-five trades removed. Percentage shares are reported only for
positive total PnL. Fixed time blocks are 00:00-05:59, 06:00-11:59, 12:00-17:59, and
18:00-23:59 UTC; low-N blocks are labelled and never used to select policy.

Some data-quality values are explicitly `UNAVAILABLE` because the database does not
persist conflict attempts, rejected pre-feature snapshots, an M6 monitor schedule, or
a complete universe of publicly listed markets. They are not replaced with proxies.

## Status

The pre-registered status remains `INCONCLUSIVE` until at least 200 resolved
independent markets and 50 eligible records exist. Once both minimums are met, the
unchanged M5 gates determine `KILL` or `CONTINUE`. Collection never stops automatically
because of a status result.

## Live Resume Smoke Test

On 2026-09-07 UTC, after all offline gates passed, the existing experiment
`m6-20260906-001` was resumed twice with model ID 1. It retained database ID 1 and
manifest hash `82880e78007e71e0cc54b5325671410de2a592391a64bd481fa129a987567a3b`.
Two bounded public read-only collector runs saved seven new snapshots for market
`4274241`; the first M6 monitor pass correctly skipped incomplete 30-second feature
history, and the later pass persisted one rejected prediction with
`NET_EDGE_BELOW_MINIMUM`. Repeating the monitor for the exact same snapshot returned
the same prediction ID, creating no duplicate forward prediction or paper trade.

One settlement catch-up pass attached no new result. Final SQLite checks were
`integrity_check=ok` and an empty `foreign_key_check`. This smoke verifies operational
resume only. Its current prospective evidence remains under the pre-registered minimum
and is not interpreted as performance evidence.

## Known Limitations

- Coinbase remains a covariate, not the Polymarket settlement oracle.
- Public observations are sampled, non-atomic, and may miss short-lived book states.
- Paper costs are frozen research assumptions, not a full venue fill model.
- HF diagnostics are present only when the separate approved HF collector has run.
- Prospective evidence remains insufficient until the frozen M6 sample minimums are met.
