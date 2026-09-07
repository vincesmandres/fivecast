# FiveCast M5 Fair Value Model + Forward Validation

## Status And Freeze

M5 is non-executing research. The frozen artifact was created before the first
forward prediction. No holdout evaluation occurred. Dataset cutoff is
`2026-09-06T09:59:55.620000+00:00`; artifact dataset fingerprint is
`8fa0f4ce5fade22c4ea87ccc75bdc51257e71bd325014c4804fa7bf5f3d3ac70`.

The artifact contains the exact eligible/rejected IDs, reasons, quality filter,
feature list, provenance, train cutoff, coefficients, scaler means/scales, fixed
training configuration, and SHA-256 artifact/config fingerprints. It is stored in
`model_versions` as immutable `m5-logistic-v1` / ID 1. Forward code only deserializes
this record; it has no training function or automatic retraining path.

## Sampling And Model

The source uses approved M3 quality filtering and takes the chronological research
segment only. Its 39 research markets are split at market level into 21 train and
8 historical-validation markets. Each market contributes exactly one predeclared
anchor: the first complete historical feature row with 60 seconds or less remaining.
This avoids treating five-second observations from the same outcome as independent
samples. Ten research markets had no complete anchor; 463 earlier partial rows were
rejected for missing historical lookback/quote values.

Model 0 is contemporaneous UP executable ask when predicting UP, and own-side ask
when comparing an executable side. Bid, ask, midpoint and normalized complements
are not conflated. Ask is an executable buy reference, not assumed a calibrated
probability.

Model 1 is deterministic regularized logistic regression: learning rate 0.1,
1,000 full-batch iterations, L2=0.01. Numeric scaling is fitted on train rows only.
No tuning, neural network, gradient boosting, calibration transformation, holdout,
or M4 future-response/HF feature was used.

Frozen standardized coefficients, in feature-list order, are:

```text
0.512392, 0.512338, 0.132722, 0.121493, 0.210319, 0.128030,
-0.338474, 0.022828, 1.081994, 1.081994, -1.055221, -1.055221,
0.000000, 0.000000, 0.507148, 0.000000
intercept = -0.782208
```

Features are BTC delta USD/pct; historical 5/15/30-second BTC velocities; historical
30/60-second volatility; remaining time; UP/DOWN bid/ask/spread; source skew; and
the stale flag. Each is reconstructed from the current snapshot and earlier rows
only. Official outcome is appended only after a feature row exists, solely for
historical fitting/evaluation.

## Historical Validation

Eight independent market anchors are available for historical validation:

| Model | Brier | Log loss | ROC-AUC | Accuracy |
| --- | ---: | ---: | ---: | ---: |
| Market UP ask baseline | 0.054413 | 0.210785 | N/A | N/A |
| Logistic v1 | 0.138096 | 0.517270 | 0.9375 | 0.8750 |

The logistic model is worse than the market baseline on Brier and log loss. The
apparent AUC/accuracy are based on eight markets and are secondary diagnostics; they
do not override probability-score failure. Historical model calibration buckets:

| Probability bucket | N | Mean prediction | Observed UP rate |
| --- | ---: | ---: | ---: |
| 0.00-0.10 | 2 | 0.0343 | 0.0000 |
| 0.30-0.40 | 1 | 0.3976 | 0.0000 |
| 0.90-1.00 | 5 | 0.9856 | 0.8000 |

This is preliminary, underpowered, and not evidence of useful probability estimates.

## Fair Value And Frozen Policy

For UP, `raw_edge = model_probability_up - up_ask`. For DOWN,
`model_probability_down = 1 - model_probability_up` and
`raw_edge = model_probability_down - down_ask`. The selected side is the greater
estimated net edge; its best ask is always used, never midpoint.

Frozen friction assumptions are 0.01 estimated fees, 0.01 estimated slippage and
0 latency penalty, all probability units. `net_edge = raw_edge - fees - slippage -
latency`. A record is eligible only with net edge >=0.02, selected spread <=0.05,
source skew <=5,000ms and `is_stale=false`. One `forward_paper_trades` row per model
and market is enforced. This is a research record, not an order or capital action.
The complete pre-registration is in [M5_PREREGISTRATION.md](M5_PREREGISTRATION.md).

## Forward Smoke

Collected ten fresh public read-only snapshots, then ran:

```powershell
uv run python -m fivecast.forward --model-version 1 --once
uv run python -m fivecast.forward_report
```

Recorded output:

```text
FORWARD model=m5-logistic-v1 market=4260133 probability_up=0.999245
side=UP net_edge=0.0492450963045464 eligible=True reason=None
```

This is one pipeline-operational research record, not evidence. Its market was
unresolved at reporting time: predictions=1, eligible paper records=1, resolved
predictions=0, resolved paper signals=0, and all forward Brier/log-loss values are
N/A. No future outcome was waited for or inspected.

## Database

Schema version 5 adds additive-only `model_versions`, `forward_predictions`, and
`forward_paper_trades`. Raw snapshots, markets, polls, runs and HF event tables are
not altered by M5. Forward prediction rows retain model ID, feature JSON/fingerprint,
asks, selected side/ask, edges, frozen frictions, eligibility/reason, and nullable
official outcome/paper PnL. Settlement attachment is restricted to these isolated
forward tables once an independently persisted official market resolution exists.
Each forward loop first attaches any such existing official resolution and computes
stored net paper accounting from that record's frozen entry and friction fields; it
does not query outcomes directly or use them in a new prediction.

## Commands

```powershell
uv run python -m fivecast.train
uv run python -m fivecast.forward --model-version 1 --once
uv run python -m fivecast.forward --model-version 1 --interval 5
uv run python -m fivecast.forward_report
```

`forward` reads only existing collected snapshots. Run the approved read-only
collector separately for new observations. It never starts a collector, retrains,
or connects a wallet.

## Limitations

- Eight validation markets are far below the pre-registered 200-market minimum.
- The initial logistic probability scores are worse than the executable market
  baseline. This is a negative preliminary result.
- Forward records cannot establish performance until independently resolved markets
  accumulate. The legacy M3 holdout remains excluded.
- Fixed frictions are simple research assumptions, not a venue fee or fill model.
- Snapshot timing, source clock, settlement delay, and data-quality limitations from
  M2-M4 continue to apply.

## Verification

```text
uv sync --locked                 PASS (20 packages checked)
uv run pytest -q                 201 passed in 13.81s
uv run ruff check .              All checks passed!
uv run ruff format --check .     61 files already formatted
git diff --check                 PASS (existing CRLF advisories only)
```

Tests include point-in-time feature construction, unavailable-history rejection,
market-level split isolation, train-only scaling, deterministic coefficients and
fingerprints, executable ask baselines, UP/DOWN fair values, cost deduction,
policy checks, artifact identity, no automatic training, forward persistence,
settlement attachment, and read-only reporting.
