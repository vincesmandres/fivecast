# FiveCast M4 Price Discovery & Lead-Lag Research

## Research Question

How quickly does the observed Polymarket selected-side ask change after a recorded
BTC movement? This is descriptive market-microstructure research, not strategy
selection. M4 does not evaluate holdout data, rank PnL, alter the approved replay
or strategy modules, or provide execution capability.

## Offline Method

The command `uv run python -m fivecast.leadlag` uses only the M3 default eligible
markets then the earliest chronological 70% subset: **39 research markets**. The
remaining holdout data is not passed to analysis functions.

BTC return/change uses the current BTC price minus the latest stored BTC price at or
before 5, 10, 15, 30, or 60 seconds earlier. The command emits the full lag matrix
for each of those five historical return windows. There is no interpolation. For a
descriptive future response only, an ask at `t+lag` is the first stored quote at or
after the target, accepted only up to 7.5 seconds late. Future values live solely in
`leadlag.py`, which is not imported by strategy code. They are never strategy
features. The report uses the UP ask for cross-correlation and the signed selected
side for events: UP for positive BTC movement, DOWN for negative.

Event buckets are pre-declared five-second BTC changes: $5-10, $10-20, $20-30,
$30-40, and $40+. Each market has a fixed 30-second cooldown after an accepted
event. This suppresses repeated snapshots of the same continuous move. An event
needs a valid selected-side current ask; it is not replaced by a later quote.

`time_to_reprice` is defined only for $40+ events with valid selected-side asks at
both event time and approximately +30 seconds. It is the first sampled response
time at which the absolute selected-side ask change reaches 80% of that observed
30-second change. It has sampling precision only, not sub-sample precision.

## Offline Findings

Cross-correlation pairs 5-second BTC returns at time `t` with UP-ask changes at
the labeled lag. Negative lags describe past probability changes and positive lags
are descriptive responses. Correlation does not establish causality, lead, or an
actionable relationship.

| Ask-change lag | Pairs | Pearson correlation |
| --- | ---: | ---: |
| -30 s | 1,719 | 0.303 |
| -15 s | 1,831 | 0.409 |
| -10 s | 1,869 | 0.477 |
| -5 s | 1,907 | 0.549 |
| 0 s | 1,911 | N/A, zero change by definition |
| +5 s | 1,850 | -0.029 |
| +10 s | 1,811 | -0.030 |
| +15 s | 1,778 | -0.044 |
| +30 s | 1,664 | -0.041 |

The positive values at negative lags are consistent with the two series already
having moved in the same window. The near-zero positive-lag correlations in this
small, irregular sample do not demonstrate delayed price discovery or disprove it.

### Event Study

Selected-side ask change after event, mean / median at +5, +10, +15, +30 seconds:

| BTC Event | Direction | N at event | +5 | +10 | +15 | +30 |
| --- | --- | ---: | --- | --- | --- | --- |
| $5-10 | positive | 40 | -.008 / -.010 | -.023 / -.020 | -.034 / -.005 | -.043 / -.030 |
| $5-10 | negative | 42 | -.003 / .000 | +.001 / .000 | -.012 / +.010 | -.011 / +.010 |
| $10-20 | positive | 29 | -.007 / .000 | -.026 / .000 | -.067 / -.020 | -.067 / -.019 |
| $10-20 | negative | 19 | +.024 / +.010 | -.005 / .000 | +.009 / +.010 | +.016 / .000 |
| $20-30 | positive | 4 | +.013 / .000 | -.025 / -.025 | +.005 / +.005 | +.200 / +.200 |
| $20-30 | negative | 1 | +.020 / +.020 | +.070 / +.070 | +.160 / +.160 | +.160 / +.160 |
| $30-40 | positive | 3 | +.013 / +.010 | .000 / .000 | -.005 / -.005 | +.045 / +.045 |
| $30-40 | negative | 2 | -.005 / -.005 | +.045 / +.045 | +.050 / +.050 | +.045 / +.045 |
| $40+ | positive | 1 | +.010 / +.010 | +.010 / +.010 | +.040 / +.040 | +.060 / +.060 |
| $40+ | negative | 2 | -.030 / -.030 | -.105 / -.105 | -.130 / -.130 | -.125 / -.125 |

The latter event cells have one to four events. They are not reliable estimates.

For three $40+ events meeting the +30-second response definition, sampled 80%
repricing time had median **30.001 seconds**, mean **25.053 seconds**, P25 **20.004**,
make this a coarse descriptive measure only.

## High-Frequency Architecture

M4 adds a separate public-WebSocket event collector, `fivecast.hf_collect`, and a
read-only `fivecast.hf_report`. It never attempts to synchronize events into M2
snapshots or send an order message. Raw append-only BTC events and Polymarket token
book events retain source timestamps, local receive timestamps, identifiers, prices,
sizes, sequence when supplied, and duplicate/out-of-order flags. Connection history
retains disconnect/reconnect observations. SQLite WAL supports sustained append and
read-only reporting.

Run:

```powershell
uv run python -m fivecast.hf_collect
uv run python -m fivecast.hf_report
```

Schema version 4 adds append-only `btc_events` and `market_events` fields:
`source_event_timestamp`, `local_receive_timestamp`, source, optional market/token
identity, optional sequence, event type, price/bid/ask/size, canonical raw payload,
duplicate/out-of-order flags, and signed source-to-receive latency. `hf_connections`
records source, connection/disconnection times, reconnect attempt, final status and
error. Timestamp, source/token and market/timestamp indexes support reporting. No
raw M0-M3 tables are changed by HF ingestion.

### Live HF Smoke

After mocked parser/reconnect/persistence tests, ran:

```powershell
uv run python -m fivecast.hf_collect --duration 45
```

The first short attempt surfaced Coinbase's valid initial `last_match` subscription
message. It was strictly parsed as a validated match event rather than treated as a
fatal unknown message; genuinely unsupported message types still fail/reconnect.
That initial attempt's failed connection rows and early market events remain as
append-only quality evidence.

The corrected 45-second public read-only run started at 16:28:49 UTC and recorded
**327 BTC events** and **8,382 market events**, approximately **7.3 BTC events/s**
and **186.4 market events/s** over its 44.97-second connection interval. Both source
streams arrived, with source and local receipt timestamps persisted. It created clean
disconnection records at duration shutdown. No credentials, wallet, orders, or
trading protocol messages were used.

The cumulative `hf_report` after both retained smoke attempts reported 17,593 events:
338 BTC and 17,255 market, 120.13 events/s over 146.45 seconds, 12 disconnects,
10 reconnects, 8,505 duplicate flags, zero sequence out-of-order flags, and a largest
receive gap of 81.52 seconds. All 17,593 stored events had a source timestamp in
this smoke. Duplicates are retained and flagged, not deleted. The
large gap includes connection/reconnect behavior, not an inferred market silence.

Median/P95 source-to-receive latency were **-1686.58/-1608.20 ms**. This negative
value means the vendor's source timestamp led this host's receipt clock; it is
evidence of clock/transport semantics, not negative network latency. It is retained
as a signed measurement, not clamped. SQLite integrity was `ok`.

At inspection the database was 24,592,384 bytes with 17,593 total HF events and mean
raw JSON payload size 631.2 bytes. At the corrected observed combined rate of about
194 events/s, raw JSON alone is roughly 10.3 GB/day; SQLite row/index/WAL overhead
makes this a lower-bound planning estimate, not a retention guarantee.

## Limitations

- Existing snapshots are approximately five-second polling observations, not event
  data. Irregular matching and source clocks limit lag precision.
- Cross-correlation is descriptive and does not control for shared time-to-expiry,
  market state, autocorrelation, latency, or other confounders.
- Event counts at $20+ are small and $40+ is extremely small.
- Selected-side asks are not executable fills and omit depth/queue/latency effects.
- HF streams can disconnect, have vendor schema changes, contain duplicates, or
  lack source timestamps. Anomalies are recorded, not silently removed.
- The high-frequency smoke is short. Sustained event rates, vendor throttling,
  websocket sequence semantics, disk growth and rollover behavior require longer
  read-only collection before making dataset-quality conclusions.

## Verification

```text
uv sync --locked                 PASS (20 packages checked)
uv run pytest -q                 189 passed in 10.58s
uv run ruff check .              All checks passed!
uv run ruff format --check .     51 files already formatted
git diff --check                 PASS (existing CRLF advisories only)
```

Offline tests include event bucket/cooldown de-duplication, positive and negative
selected-side mapping, future-response labeling, cross-correlation, coarse repricing
time, parser validation, duplicate/out-of-order flags, reconnect/subscription,
database persistence, duration shutdown, and read-only HF reporting.
