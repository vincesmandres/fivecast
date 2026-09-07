# FiveCast Cloud Deployment

## Scope

`fivecast.cloud` is a single-process, paper-only supervisor. It starts the existing
read-only standard collector and existing M6 monitor against the same SQLite file. It
does not change M5/M6 policy, retrain a model, or add wallets, credentials, signing,
orders, cancellations, capital, or live execution.

## Environment

| Variable | Default | Railway value |
| --- | --- | --- |
| `FIVECAST_DB_PATH` | `data/fivecast.db` | `/data/fivecast.db` |
| `M6_EXPERIMENT_ID` | `1` | `1` |
| `COLLECT_INTERVAL_SECONDS` | `5` | `5` |

All cloud tasks derive their SQLite path from `FIVECAST_DB_PATH`. M6 `status`,
`report`, and `settle` use the same variable unless `--db-path` explicitly overrides
it. The M6 experiment is resumed; it is not recreated.

## Railway

1. Deploy this repository as a Dockerfile service.
2. Mount a Railway persistent volume at `/data`.
3. Set `FIVECAST_DB_PATH=/data/fivecast.db`.
4. Set `M6_EXPERIMENT_ID=1`.
5. Set `COLLECT_INTERVAL_SECONDS=5`.
6. Configure exactly one replica.
7. Use the container command `uv run --no-sync python -m fivecast.cloud`.

The included `Dockerfile` uses Python 3.12, locked `uv` dependencies, and an
unprivileged application user. It contains no credentials.

## SQLite Safety

The store enables WAL and a 5,000 ms busy timeout for the collector, settlement
worker, and M6 monitor sharing one local database file. This supports concurrent
threads inside one container only. SQLite is not configured for multi-host writers,
distributed locks, or horizontal scaling. Keep the service at one container replica
while SQLite is the persistence layer.

## Operations

The supervisor logs task startup and handles `SIGINT`/`SIGTERM` by signalling both
loops to stop, finish SQLite work, and close clients. It retries bounded transient
I/O/SQLite failures (maximum three attempts with increasing delay). Model, manifest,
and other invariant failures terminate the container visibly rather than being hidden.

For an operational report in a one-off Railway shell:

```powershell
uv run --no-sync python -m fivecast.m6 status --experiment 1
uv run --no-sync python -m fivecast.m6 report --experiment 1 --detailed
uv run --no-sync python -m fivecast.m6 settle
```
