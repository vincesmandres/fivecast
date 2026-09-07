# FiveCast M5 Pre-Registration

Created before forward predictions or forward outcomes are evaluated.

## Frozen Dataset And Labels

- Dataset cutoff: **2026-09-06T09:59:55.620000+00:00**.
- Dataset fingerprint: `8fa0f4ce5fade22c4ea87ccc75bdc51257e71bd325014c4804fa7bf5f3d3ac70`.
- Source: validated M2 SQLite snapshots and official Polymarket `markets.outcome` only.
- Eligible freeze market IDs and exclusions are stored in immutable artifact JSON.
- Quality filter: M3 default `ReplayQuality`: no stale/unknown stale snapshot, no max skew override, no coverage override.
- Label: final official Polymarket UP=1, DOWN=0. Coinbase is never a label.

## Model

- Model: deterministic L2-regularized logistic regression, pure Python.
- Fixed hyperparameters: learning rate 0.1, 1,000 batch iterations, L2 0.01.
- Scaling: mean and population standard deviation fit on training markets only.
- No calibration transformation and no hyperparameter search.
- Features: `btc_delta_usd`, `btc_delta_pct`, `btc_velocity_5s`, `btc_velocity_15s`,
  `btc_velocity_30s`, `btc_volatility_30s`, `btc_volatility_60s`, `seconds_remaining`,
  UP/DOWN bid/ask, UP/DOWN spread, source skew and stale flag.
- No M4 future response, future quote, future BTC observation, outcome, or HF event is a feature.

## Sampling And Split

- One anchor observation per market: first complete point-in-time feature row at or after 60 seconds remaining.
- Markets, not snapshots, are independent primary units. All rows from a market stay in one split.
- The approved M3 chronological research region is split again: earliest 70% train, latest 30% historical validation.
- Legacy M3 holdout remains excluded from model training, historical validation, and model selection.

## Frozen Forward Policy

- Artifact: `m5-logistic-v1`, model ID 1, persisted before forward collection.
- Executable price: selected side best ask, never midpoint.
- `raw_edge = model_probability_side - side_ask`.
- Estimated fees: 0.01 probability units; estimated slippage: 0.01; latency penalty: 0.
- `net_edge = raw_edge - fees - slippage - latency`.
- Eligible only if net edge >= 0.02, selected spread <= 0.05, source skew <= 5,000 ms and `is_stale=false`.
- At most one paper research record per model and market. No order is sent.

## Forward Evaluation And Gates

Forward report metrics: independent markets observed, predictions, eligible records,
resolved signals, Brier/log loss/calibration versus UP ask baseline, mean edge,
realized paper accounting and drawdown where available.

No continuation toward execution research unless all are met on a new true-forward
period: at least 200 resolved independent markets, at least 50 eligible records,
model Brier and log loss not worse than market baseline by more than 0.01, positive
net realized EV after frozen costs, maximum drawdown no worse than 10 units, and no
result dominated by fewer than five markets. Failure of any condition is a kill result.
These thresholds cannot be loosened after seeing forward outcomes; a new experiment
would require a new version and pre-registration.
