# Testing - dbmask

## Strategy

- **Unit first, no database.** The determinism core, config validation, drift findings, the TOML
  emitter, topo sort, and error mapping are pure and get exhaustive unit tests that run in
  seconds with no docker and no network. This is the bulk of the suite.
- **Integration against real engines.** Everything that touches SQL runs against real PostgreSQL
  and MySQL in docker, both dialects for every schema-touching feature. SQLite is deliberately
  not used as a stand-in: COPY, advisory locks, row-value comparisons, and strict length/CHECK
  enforcement are exactly the things a stand-in would fake.
- **End-to-end via the CLI.** The highest-value tests invoke `dbmask` through Click's
  `CliRunner` (and a few via `subprocess` for exit-code truth) against the dockerized fixture:
  init -> check -> mask as an operator would run them (verify joins once it is implemented).
- **Determinism is tested structurally**: outputs compared across two separately constructed
  engines (never within one process's cache), across the two modes (mask vs pump), and across
  the two dialects.
- **Value hygiene is tested, not assumed**: the fixture seeds sentinel strings and the suite's
  final sweep greps all captured stdout, stderr, logs, and reports for them.

## What gets which coverage

Unit: strategy determinism/divergence matrix, normalization, NULL passthrough, unique suffix
placement and length budgets, cache-independence, config shape and option validation, config
hash stability, drift/compatibility findings, PII pattern matches (including deliberate false
positives like `file_name`), emitter output round-trips through `tomllib`, FK topo sort incl.
cycle detection, error-to-exit-code mapping, URL password scrubbing.

Integration: introspection fidelity per dialect (types, lengths, nullability, composite PKs,
unique indexes, FKs), init-then-check round trip, mask runner (batches, transactions, resume,
guards, lock), pump runner (read-only source, preflight, ordering, COPY/INSERT), audit and
progress rows, verification checks, report contents.

End-to-end: the operator flows (init, check, mask, resume after an interrupted run, a second
process refused by the lock, exit codes), automated wherever a process boundary matters.

## Docker compose test setup

`docker-compose.test.yml` at the repo root:

```yaml
services:
  postgres:
    image: postgres:16
    environment: { POSTGRES_USER: dbmask, POSTGRES_PASSWORD: dbmask, POSTGRES_DB: dbmask_staging }
    ports: ["54329:5432"]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U dbmask"], interval: 2s, retries: 30 }
  mysql:
    image: mysql:8.4
    environment: { MYSQL_USER: dbmask, MYSQL_PASSWORD: dbmask, MYSQL_DATABASE: dbmask_staging, MYSQL_ROOT_PASSWORD: root }
    ports: ["33069:3306"]
    healthcheck: { test: ["CMD", "mysqladmin", "ping", "-h", "localhost"], interval: 2s, retries: 60 }
```

Database names end in `_staging` so the fixture safety pattern matches. `conftest.py` reads
`DBMASK_TEST_PG_URL` / `DBMASK_TEST_MYSQL_URL` (defaults matching the compose ports), creates a
fresh schema per test via a fixture, and skips `integration`-marked tests with a clear message
when a database is unreachable.

## Exact commands

```bash
uv sync                                            # install pinned deps
uv run pytest -m "not integration"                 # unit suite, no docker needed
docker compose -f docker-compose.test.yml up -d --wait
uv run pytest                                      # full suite, both dialects
uv run pytest tests/integration/test_mask.py -k resume   # one area
uv run ruff check . && uv run black --check . && uv run mypy src
docker compose -f docker-compose.test.yml down -v  # clean up
```

## CI

`.github/workflows/ci.yml` runs one job on every push and pull request to `main`. PostgreSQL 16
and MySQL 8.4 run as service containers with the same images, credentials and ports as
`docker-compose.test.yml`, so the integration tests run rather than skip. The steps:

1. `uv sync --frozen` (Python 3.12, from the committed `uv.lock`)
2. `uv run ruff check .`
3. `uv run black --check .`
4. `uv run mypy src`
5. `uv run pytest` (unit and integration, both dialects)

No real credentials exist anywhere in CI; the test databases use throwaway passwords committed in
the compose file and the workflow.

## Before merging

1. `uv run ruff check .`, `uv run black --check .`, `uv run mypy src` clean.
2. `uv run pytest` green, including both dialects for schema-touching changes.
3. The sentinel assertions still find no real value in any captured output or report.
