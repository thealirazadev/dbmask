# Architecture - dbmask

## Tech stack with rationale

- **Python 3.12** - stdlib `tomllib` for config parsing, `hmac`/`hashlib` for the determinism
  core, mature database drivers. Exact interpreter pinned in `pyproject.toml`; `uv` manages the
  environment and `uv.lock` is committed.
- **Click** - command group with subcommands, tested via `CliRunner`. Declarative options and
  env-var fallbacks (`DBMASK_URL` and friends) without hand-rolled argv parsing.
- **SQLAlchemy Core (2.x)** - one introspection API (`Inspector`) and one SQL expression layer
  covering PostgreSQL and MySQL. No ORM: dbmask moves rows, it does not model them. Dialect
  escapes are confined to `dialects.py` (COPY, advisory locks, read-only session).
- **psycopg 3 / PyMySQL** - drivers. psycopg 3 is required for PostgreSQL COPY streaming;
  PyMySQL is pure Python, which keeps installation painless.
- **Faker** - realistic fake values. dbmask controls determinism itself (HMAC-derived seed per
  value, see below); Faker is only the value generator. The Faker version is pinned exactly
  because provider data changes between releases would silently change masked output.
- **pytest** - unit tests need no database; integration tests run against dockerized
  PostgreSQL 16 and MySQL 8.4 via `docker-compose.test.yml` (see `docs/testing.md`).
- **ruff + black** - lint and formatting, zero configuration debates.

No other runtime dependencies. TOML writing for `init` is a small purpose-built emitter
(`emit.py`) because the stdlib cannot write TOML and generated configs need explanatory comments,
which generic TOML writers cannot produce.

## System components

```
dbmask.toml ──► config.py ──► MaskPlan (validated, immutable)
                                 │
   DBMASK_SECRET (env) ──────────┤
                                 ▼
        ┌──────────── cli.py (Click group) ────────────┐
        │            │           │          │          │
        ▼            ▼           ▼          ▼          ▼
      init         check       mask       pump       verify
        │            │           │          │          │
  introspect.py  drift.py   runner_mask  runner_pump verify.py
        │            │           │          │          │
        ▼            ▼           ▼          ▼          ▼
     emit.py     findings    strategies.py (HMAC seed -> Faker)
                                 │          │
                            audit.py   dialects.py (COPY / locks / read-only)
                                 │
                             report.py (JSON audit report)
```

- **config.py** loads `dbmask.toml`, rejects unknown keys/strategies, and produces a `MaskPlan`:
  per-table, per-column resolved strategy objects plus settings. Pure, no database access.
- **introspect.py** reads the live schema via `Inspector` into a `SchemaModel` (tables, columns
  with type/length/nullability, PKs, FKs, unique indexes) and flags PII-pattern column names.
- **drift.py** diffs `MaskPlan` against `SchemaModel` both ways and validates strategy
  compatibility; returns a findings list consumed by `check` and by every run's preflight.
- **strategies.py** implements the nine strategies as pure functions of
  `(secret, strategy_family, value)`. The only stateful piece is a bounded value cache.
- **runner_mask.py / runner_pump.py** are the two execution engines; both consume the same
  `MaskPlan` and `strategies` so a value masks identically in either mode.
- **audit.py** owns the `dbmask_audit` and `dbmask_progress` tables in the target database.
- **verify.py** runs the post-run consistency checks. **report.py** assembles the JSON report.
- **errors.py** defines `DbmaskError(code, message, exit_code)`; **logging.py** emits logfmt
  lines to stderr. Neither ever receives a cell value.

## Determinism core

The heart of the tool. For a non-NULL input value:

1. **Normalize**: convert to `str`; `fake_email` additionally lowercases and strips whitespace
   (case-variant duplicates of one address must mask identically). All other strategies use the
   exact string.
2. **Seed**: `seed = int.from_bytes(hmac_sha256(secret, family + b"\x1f" + normalized)[:8])`.
   The strategy family is part of the message (domain separation): a string masked as a name and
   the same string masked as an email produce uncorrelated outputs.
3. **Generate**: `faker.seed_instance(seed)` then call the family's provider (`email()`,
   `name()`, `phone_number()`, `address()`). `hash` skips Faker: output is the hex HMAC digest
   truncated to the column length. `redact` returns the fixed configured string; `null` returns
   NULL; `keep` returns the input; `template` renders `"{placeholder}"` substitutions from
   same-row column values.
4. **Cache**: results are memoized per `(family, normalized)` in an LRU capped at
   `settings.cache_size` (default 500k entries). Production data repeats heavily (one user's
   email appears across many rows), so the cache converts most lookups into dict hits.

Rules that make this safe and reproducible:

- NULL always passes through; no strategy fabricates a value for NULL.
- The Faker locale is fixed in config (`project.locale`, default `en_US`) and recorded in the
  audit report; changing locale or the pinned Faker version changes outputs across runs, and the
  docs say so.
- Uniqueness (`unique = true`): the generated value gets a deterministic suffix of 12 hex chars
  from the same HMAC (inserted before `@` for emails, appended with `-` otherwise). 48 bits keeps
  birthday-collision probability negligible below ~16M distinct values; a residual collision is
  not silently tolerated: the database unique constraint rejects it at write time and the verify
  pass re-checks, either way failing the run loudly. No in-memory seen-set is kept (tables larger
  than RAM must work).
- `template` placeholders may reference only the table's PK columns and columns whose strategy is
  `keep` (enforced by `check`), so a template can never re-embed a masked or PII value.

### Privacy analysis (honest limits)

Deterministic masking is pseudonymization, not anonymization:

- **Dictionary attack**: anyone holding the secret can compute `mask(guess)` for a candidate real
  value and compare against the masked database, confirming membership. The whole pseudonym
  universe re-derives from the secret. Mitigations: the secret is required to be at least 32
  characters of real entropy, lives only in `DBMASK_SECRET` (never in config, argv, logs, or the
  audit tables), and should be stored in a secret manager and rotated per project.
- **Linkage is the feature**: identical inputs produce identical outputs, so an attacker with the
  masked copy learns that two rows referred to the same person. That is exactly the join
  preservation operators want; treat masked copies as internal-confidential, not public.
- **Frequency analysis**: value frequencies survive masking (the most common fake city was the
  most common real city). Out of scope to defeat in v1; documented so nobody assumes otherwise.
- Under GDPR-style regimes, pseudonymized data is still personal data. dbmask reduces exposure;
  it does not remove obligations. The README repeats this.

The `secret_fingerprint` stored in `dbmask_audit` is the first 12 hex chars of
`sha256("dbmask-fp:" + secret)`: enough to detect a resume attempted with a different secret,
useless for recovering a high-entropy secret.

## Data model

dbmask owns two tables, created on demand in the masked/target database (not the source). All
DDL is dialect-portable through SQLAlchemy Core. Names are the contract; do not rename.

### dbmask_audit

One row per run, written by `mask` and `pump`.

| Column | Type | Notes |
|---|---|---|
| id | bigint PK, auto-increment | |
| run_id | char(36), unique | UUID4, also in logs and the report |
| mode | varchar(8) | `mask` or `pump` |
| tool_version | varchar(20) | |
| config_hash | char(71) | `sha256:<hex>` of the canonicalized config |
| secret_fingerprint | char(12) | see privacy analysis; never the secret |
| locale | varchar(16) | Faker locale used |
| status | varchar(12) | `running`, `completed`, `failed` |
| rows_masked | bigint, default 0 | total across tables, updated at finish |
| started_at | timestamp (UTC) | |
| finished_at | timestamp (UTC), nullable | null while `running` |

Indexes: unique `run_id`; `(status)` for the incomplete-run and already-masked probes.

### dbmask_progress

Resume bookkeeping for in-place mask. One row per (run, table).

| Column | Type | Notes |
|---|---|---|
| run_id | char(36) | FK to dbmask_audit.run_id |
| table_name | varchar(128) | |
| last_pk | text | JSON array of the last committed batch's final PK tuple |
| rows_done | bigint | |
| updated_at | timestamp (UTC) | |

Primary key `(run_id, table_name)`. The row is upserted inside the same transaction as its
batch's UPDATEs; that atomicity is the resume guarantee.

### dbmask.toml (the config contract)

```toml
[project]
locale = "en_US"                       # optional, default en_US

[safety]
database_name_pattern = "_(staging|masked)$"   # required for mask; target must match for pump

[settings]
batch_size = 5000                      # rows per batch/transaction
cache_size = 500000                    # value-cache entries

[tables.users.columns]
id            = "keep"
email         = { strategy = "fake_email", unique = true }
full_name     = "fake_name"
phone         = "fake_phone"
street        = "fake_address"
password_hash = { strategy = "redact", value = "MASKED" }
internal_note = "null"
username      = { strategy = "template", template = "user_{id}", unique = true }
api_token     = "hash"
created_at    = "keep"

[[verify.joins]]                       # logical joins with no FK behind them
left  = "users.email"
right = "orders.customer_email"
```

A bare string is shorthand for `{ strategy = "..." }`. Every live column must appear; there is no
wildcard and no per-table default. That is deliberate: the drift guard only works if silence is
impossible.

## Key flows

### init

1. Connect, introspect into `SchemaModel`.
2. For each column, propose: PII-pattern name -> matching fake strategy (`email`->`fake_email`,
   `name`->`fake_name`, `phone`->`fake_phone`, address-ish -> `fake_address`, `token`/`secret`/
   `password` -> `hash` or `redact`); non-text PII (dates, numerics) -> `null` when nullable,
   otherwise `keep` plus a `# TODO no compatible strategy` comment; everything else -> `keep`.
3. Emit `dbmask.toml` with a comment on every PII flag naming the matched pattern. Refuse to
   overwrite an existing file without `--force`.

### check (also the preflight of every run)

1. Parse config; unknown key/strategy -> `E_CONFIG`, exit 2.
2. Validate secret presence and length (>= 32 chars) -> `E_CONFIG`.
3. Introspect; diff both ways: configured-but-missing columns and unconfigured live columns are
   both `E_DRIFT` (exit 3); unconfigured PII-pattern columns are listed first with severity
   `pii`.
4. Compatibility: `null` on NOT NULL columns, text strategies on non-text columns, generated
   max length (strategy max + unique suffix) exceeding the column length, `unique = true` missing
   on a masked column covered by a unique index, non-`keep` on any PK/FK column, masked table
   without a PK (mask mode), FK cycle in the graph (pump mode) -> each a named finding.
5. `mask` additionally requires `safety.database_name_pattern` and that the database name matches.

### mask (in-place)

1. Preflight = the full check above; any finding aborts before any write.
2. Acquire the single-runner lock: `pg_advisory_lock(hash)` / MySQL `GET_LOCK` with zero wait;
   held for the whole run. A second dbmask process fails fast with `E_SAFETY`.
3. Guards: any `dbmask_audit` row with status `completed` -> refuse (`E_SAFETY`, already masked;
   a fresh restore never contains the table). A `running`/`failed` row -> refuse unless
   `--resume`, because re-masking already-masked rows produces garbage.
4. Insert the audit row (`running`). On `--resume`: reuse the newest incomplete run after
   verifying `config_hash` and `secret_fingerprint` match; mismatch -> `E_SAFETY` (a resumed run
   with a different config or secret would mask the remainder inconsistently).
5. Per table (alphabetical), per batch:
   a. `SELECT pk_cols, masked_cols FROM t WHERE (pk) > (last_pk) ORDER BY pk LIMIT batch_size`
      (row-value comparison covers composite PKs; on resume, `last_pk` comes from
      `dbmask_progress`).
   b. Compute masked values in memory via the strategy cache.
   c. Write the batch: PostgreSQL `UPDATE ... FROM (VALUES ...)`; MySQL `executemany` UPDATE by
      PK. Same transaction: upsert `dbmask_progress`. Commit.
6. Finish: set audit row `completed` with `rows_masked` and `finished_at`; run verification
   (below); write the report if requested. A verification failure sets status `failed` and exits
   1 even though writes committed, because the copy must not be trusted.

### pump (source -> target)

1. Preflight on both ends: source schema is the truth for drift; target schema must match the
   source (same tables/columns) or `E_DRIFT`; target tables must all be empty or `E_SAFETY`
   (partial targets are unrecoverable; the operator truncates deliberately, not dbmask); target
   database name must match the safety pattern.
2. Source session is set read-only (`SET default_transaction_read_only = on` / MySQL
   `SET SESSION TRANSACTION READ ONLY`): pump can never write to the source, by construction.
3. Topologically sort tables by FK dependencies (cycles were rejected at check).
4. Per table: stream source rows in PK order in `batch_size` chunks, mask in memory, write:
   PostgreSQL binary COPY via psycopg 3 `cursor.copy()`, one transaction per table; MySQL
   multi-row INSERT per chunk, commit per chunk. FK order guarantees parents exist before
   children; masked non-key values cannot break FKs because key columns are `keep`.
5. Finish: audit row in the **target**, verification (including per-table row-count equality
   source vs target), report.
6. Crash recovery for pump is re-run from scratch: source is untouched, so the operator truncates
   the target and pumps again. No progress table needed.

### verify (standalone and post-run)

1. Declared FKs: `SELECT count(*) FROM child LEFT JOIN parent ... WHERE parent.pk IS NULL AND
   child.fk IS NOT NULL` per FK; any orphan count > 0 fails.
2. Configured `[[verify.joins]]` pairs: same orphan scan over the logical pair.
3. Unique indexes covering masked columns: `GROUP BY ... HAVING count(*) > 1` re-check.
4. Pump only: `count(*)` equality per table between source and target.
5. Output: counts only; never an offending value, never a PK value.

## Failure modes and handling

| Failure | Handling |
|---|---|
| Config unparseable, unknown key/strategy | `E_CONFIG`, exit 2, nothing touched |
| Secret missing/short | `E_CONFIG`, exit 2; message names the env var, never echoes a value |
| Cannot connect / auth failure | `E_CONNECT`, exit 1; driver message passed through minus the URL password |
| Schema drift found | `E_DRIFT`, exit 3, offending columns listed by name (names only) |
| Safety pattern mismatch, already masked, incomplete run without `--resume`, lock busy, non-empty pump target | `E_SAFETY`, exit 4, before any write |
| Unique violation during a batch write | Batch transaction rolls back; `E_MASK`, exit 1, names table + constraint; run is resumable after the operator fixes the config (same-hash rule then forces a documented restore-and-restart, see invariants) |
| CHECK/NOT NULL violation on a generated value | Same path as unique violation; the database is the final validator of values dbmask cannot predict |
| Crash mid-run (OOM, kill, network drop) | Committed batches stay; audit row stays `running`; next invocation refuses without `--resume`; `--resume` continues from `dbmask_progress` exactly |
| Verification failure after a completed run | Exit 1, audit `failed`; report records which check failed with counts |
| Report path unwritable | Exit 1 after the run completes; masking success is logged, report failure is the error |

## Correctness invariants

1. **Determinism**: masked output is a pure function of (secret, strategy family, normalized
   value, locale, Faker version). No wall clock, no RNG outside the seeded Faker instance, no
   per-run state influences a value.
2. **Batch atomicity**: a batch's UPDATEs and its `dbmask_progress` upsert commit in one
   transaction. Therefore `last_pk` never points into an uncommitted batch, resume never
   re-masks a committed row, and never skips an unmasked one.
3. **No double mask**: `mask(mask(x)) != mask(x)`, so double application is corruption. Three
   guards enforce single application: completed-run refusal, incomplete-run refusal without
   `--resume`, and the config/secret fingerprint match on resume.
4. **Single runner**: the advisory lock makes concurrent dbmask runs against one database
   impossible; dbmask additionally assumes no other writers during a run (staging is quiescent;
   documented, not enforced).
5. **Source immutability (pump)**: the source connection is read-only at the session level; no
   code path issues DDL or DML against it.
6. **Key stability**: PK/FK columns are `keep` by validation, so referential integrity cannot be
   broken by masking; logical (non-FK) joins survive via determinism and are verified.
7. **Value hygiene**: no cell value, masked or real, is ever passed to the logger, an exception
   message, the report, or the audit tables. Enforced by review rule (`docs/rules.md`) and by an
   integration test that greps all captured output for seeded sentinel values.
8. **Idempotent verify**: verification is read-only and can run any number of times.

## Directory layout

```
dbmask/
├── pyproject.toml              # pinned deps; console entry point `dbmask`
├── uv.lock
├── README.md
├── .env.example                # DBMASK_SECRET, DBMASK_URL, DBMASK_SOURCE_URL, DBMASK_TARGET_URL (dummies)
├── docker-compose.test.yml     # postgres:16 + mysql:8.4 with healthchecks (integration tests)
├── docs/
├── src/dbmask/
│   ├── __init__.py             # version
│   ├── cli.py                  # Click group: init, check, mask, pump, verify
│   ├── config.py               # tomllib load -> MaskPlan; validation of shape and options
│   ├── emit.py                 # starter-config TOML emitter with comments (init)
│   ├── introspect.py           # Inspector -> SchemaModel; PII name patterns live here
│   ├── drift.py                # plan vs schema diff + compatibility findings
│   ├── strategies.py           # nine strategies; HMAC->seed; unique suffix; value cache
│   ├── runner_mask.py          # in-place engine: batches, progress, resume
│   ├── runner_pump.py          # pump engine: topo sort, COPY/INSERT streams
│   ├── dialects.py             # COPY, advisory locks, read-only session, row-value SQL
│   ├── audit.py                # dbmask_audit / dbmask_progress DDL and access
│   ├── verify.py               # FK, join-pair, unique, row-count checks
│   ├── report.py               # JSON report assembly and writing
│   ├── errors.py               # DbmaskError with code + exit code mapping
│   └── logging.py              # logfmt to stderr; forbids non-count value fields
└── tests/
    ├── unit/                   # config, strategies, drift, emit, topo sort, errors
    ├── integration/            # against dockerized PG + MySQL; marker `integration`
    └── conftest.py             # fixture DBs, seeded sentinel data, URL env plumbing
```

## Performance notes

- In-place: one SELECT + one batched UPDATE round trip per `batch_size` rows; the strategy cache
  makes CPU cost proportional to distinct values, not rows. Batches keep transactions short so
  the database's undo/redo stays bounded; `batch_size` is the only tuning knob.
- Pump on PostgreSQL uses binary COPY, the fastest bulk path available without superuser; MySQL
  uses multi-row INSERTs sized to stay under `max_allowed_packet`.
- Indexes on masked columns slow in-place UPDATEs (every index entry rewrites); the README
  recommends dropping and re-creating heavy secondary indexes around a large in-place run. dbmask
  does not do this itself in v1 (destructive DDL stays in operator hands).
- Realistic expectation, documented so nobody is surprised: masking is bound by UPDATE/COPY
  write throughput, not by Faker; benchmarks land with the implementation, not in this plan.
