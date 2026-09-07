# FiveCast M6 True Forward Validation

## Frozen Experiment

Experiment `m6-20260906-001` was created after M5 model freeze, with database ID 1,
start time recorded in the immutable manifest, and manifest SHA-256:
`82880e78007e71e0cc54b5325671410de2a592391a64bd481fa129a987567a3b`.

The manifest references M5 model `m5-logistic-v1` / ID 1, its artifact/dataset/config
fingerprints, cutoff, feature names, quality policy, friction policy, M5 pre-registration,
one-paper-record rule, fixed edge buckets, bootstrap seed 20260906 and 1,000 replicates.
The database rejects a resumed experiment ID with a changed manifest hash.

Only snapshot times strictly after both the model cutoff and manifest start may be linked.
M5 records created before this manifest are not M6 evidence. Links are unique, so restarts
cannot count a prior prediction twice or recompute entry at a new quote. Forward data is
not used to retrain, calibrate, change thresholds, or change frictions.

## Policy And Evidence Requirements

M5's frozen values apply unchanged: selected executable ask, 0.01 fee, 0.01 slippage,
zero latency penalty, net edge >=0.02, spread <=0.05, skew <=5,000ms, fresh data, and one
paper record per model/market. Edge buckets are `<=0`, `0-0.02`, `0.02-0.05`, `0.05-0.08`,
and `>0.08`.

Pre-registered gates are at least 200 resolved independent markets and 50 eligible records;
probability scores not worse than market by >0.01; positive net EV; drawdown no worse than
10 units; and no concentration in fewer than five markets. Before minimum counts, status is
normally `INCONCLUSIVE`. M6 does not automatically stop collection on any result.

## Commands

```powershell
uv run python -m fivecast.m6 start --model-version 1 --experiment-id m6-20260906-001
uv run python -m fivecast.main collect
uv run python -m fivecast.m6 collect --experiment 1 --interval 5
uv run python -m fivecast.m6_report --experiment 1 --detailed
```

The collector and forward monitor run separately. Ctrl+C stops either process safely;
existing manifest/prediction uniqueness enables resume. Missing periods remain missing.

## Initial Smoke

After manifest creation, an initial 30-second collection did not create a prediction because
the frozen 30-second velocity feature lacked enough in-market history. This was recorded as
no prospective prediction, not fabricated. A later 40-second read-only collection supplied
history, then one M6 prediction was linked:

```text
market=4264975
P(UP)=0.922268
selected side=UP
estimated net edge=0.3522684832540404
eligible=True
```

No official outcome was awaited or inspected. Progress is 0/200 resolved markets and 1/50
eligible records, so status is **INCONCLUSIVE**. This is pipeline verification, not evidence.

## Limitations

The frozen M5 historical logistic model was already worse than the market baseline on its
eight-market historical validation. One prospective record cannot support calibration,
edge, PnL, bootstrap uncertainty, concentration, daily, or time-of-day conclusions. These
fields remain unavailable until independently settled prospective data accumulates.

## Verification

```text
uv sync --locked                 PASS
uv run pytest -q                 203 passed in 13.40s
uv run ruff check .              All checks passed!
uv run ruff format --check .     65 files already formatted
git diff --check                 PASS (existing CRLF advisories only)
```

## M6.5 Hardening And Operations

The M6.5 hardening pass keeps the M5 artifact and policy unchanged. Resume is
idempotent for a fixed experiment/model pair; a different model binding is rejected.
Evidence at or before either frozen temporal boundary is rejected, while periods before
the manifest start remain unlinked. Prediction, paper-trade, link, and settlement writes
use database uniqueness constraints so restart and settlement catch-up do not duplicate
records. Existing official outcomes are never overwritten.

The read-only detailed report additionally exposes market-level bootstrap intervals for
win rate, EV/trade, mean net PnL/trade, and Brier difference; frozen calibration and edge
buckets; paper performance; calendar-day and UTC time blocks; concentration diagnostics;
and snapshot/HF data-quality counters. Bootstrap uses seed `20260906`, 1,000 replicates,
and markets rather than snapshots as resampling units. Missing evidence is reported as
missing and is never synthesized.

Operational commands:

```powershell
uv run python -m fivecast.m6 start --model-version 1 --experiment-id m6-20260906-001
uv run python -m fivecast.main collect
uv run python -m fivecast.m6 collect --experiment 1 --interval 5
uv run python -m fivecast.m6_report --experiment 1 --detailed
```

The M6 monitor handles Ctrl+C cleanly and can be restarted with the same experiment ID.
It remains paper-only, does not retrain, does not evaluate holdout data, and never sends
wallet, signing, order, cancellation, or trading messages.
