# Phases - dbmask

**Rule: phase N+1 does not start until the owner approves phase N.** Each phase ends green:
`ruff check`, `black --check`, `mypy --strict src`, and `pytest` all clean. One commit per
feature/task, Conventional Commits, in the listed order.

Ordering rationale: the determinism core is the product, so it lands first as a pure, exhaustively
unit-tested engine before any database is touched. The drift guard and `check` land before either
execution mode, because every run's preflight is `check`. In-place mask precedes pump (it
exercises batching, progress, and safety, which pump reuses conceptually), and the verification
suite hardens both at the end.

---

## Phase 1 - Config, strategy engine, CLI skeleton

**Scope**: everything that needs no database. Parse and validate `dbmask.toml` into a `MaskPlan`,
implement all nine strategies with HMAC-seeded determinism, uniqueness suffixing, the value
cache, the error/exit-code contract, logfmt logging, and a Click skeleton where `init`, `check`,
`mask`, `pump`, and `verify` exist but the database-touching ones exit with "not implemented".

### Tasks

- Project scaffold: `pyproject.toml` (pinned click, sqlalchemy, faker, psycopg, pymysql; dev:
  pytest, ruff, black, mypy), `uv.lock`, `src/dbmask` package, `.env.example`, `.gitignore`.
- `errors.py` (codes E_CONFIG, E_CONNECT, E_DRIFT, E_SAFETY, E_MASK, E_VERIFY, E_INTERNAL and
  exit-code mapping) and `logging.py` (logfmt to stderr, URL password scrubber).
- `config.py`: tomllib load, shape validation, unknown-key rejection, strategy option
  validation (`redact.value` required, `template` placeholder syntax, `unique` boolean),
  `[[verify.joins]]` parsing, config hash canonicalization.
- `strategies.py`: normalization, HMAC->seed, seeded Faker calls, `hash`, `redact`, `null`,
  `keep`, `template`, unique suffix insertion, LRU cache, per-strategy max output lengths.
- `cli.py`: group, global `--config`, secret loading and length check, exit-code mapping.

### Expected commits

1. `build: scaffold package with pinned dependencies and uv lock`
2. `chore: add env example and gitignore`
3. `feat: add error types with exit code mapping`
4. `feat: add logfmt logging with url password scrubbing`
5. `feat: add config loading and validation`
6. `feat: add hmac seeded strategy engine`
7. `feat: add unique suffix and value cache to strategies`
8. `feat: add cli skeleton with secret handling`
9. `test: cover config validation and strategy determinism`

### Verification checklist

- [ ] `uv run dbmask --help` lists the five commands; `dbmask mask` without config errors as
      `E_CONFIG`, exit 2; missing/short `DBMASK_SECRET` errors without echoing anything.
- [ ] Determinism: identical inputs produce identical outputs across two freshly constructed
      engines; different secrets diverge; different strategy families diverge on the same input;
      `fake_email` case-variants of one address converge.
- [ ] NULL passes through every strategy; `redact` without `value`, unknown strategy, unknown
      key each produce a named `E_CONFIG` finding.
- [ ] Unique suffix: 12 hex chars, inserted before `@` for emails, deterministic, output length
      within the declared per-strategy maximum.
- [ ] `ruff check`, `black --check`, `mypy --strict src`, `pytest` all clean; no test touches a
      database or the network.

---

## Phase 2 - Introspection, init, check, and the docker test bed

**Scope**: the schema-facing half. `SchemaModel` introspection, PII name patterns, the TOML
emitter, `dbmask init`, the full `check` command (drift both ways, compatibility findings, safety
validation, FK cycle detection), and the dockerized PostgreSQL + MySQL integration test setup
with the seeded fixture schema used by every later phase.

### Tasks

- `docker-compose.test.yml` (postgres:16, mysql:8.4, healthchecks) and `tests/conftest.py`
  fixtures: engine per dialect from env URLs, fixture schema (users/orders/addresses with FKs, a
  unique email index, composite-PK table, PII-named columns, sentinel data), `integration` marker
  with skip-when-absent.
- `introspect.py` with the PII pattern list; `drift.py` findings; `emit.py`; `init` and `check`
  wired into the CLI.

### Expected commits

1. `build: add docker compose test databases and integration fixtures`
2. `feat: add schema introspection with pii name patterns`
3. `feat: add drift and compatibility findings`
4. `feat: add starter config emitter`
5. `feat: wire init command`
6. `feat: wire check command with exit codes`
7. `test: cover introspection drift and init against both dialects`

### Verification checklist

- [ ] `docker compose -f docker-compose.test.yml up -d` then `uv run pytest -m integration`
      passes on both dialects; without docker, integration tests skip with a clear message.
- [ ] `dbmask init` on the fixture DB emits a config that immediately passes `dbmask check`;
      PII columns carry the pattern comment; rerun without `--force` refuses.
- [ ] Drop a config entry for `users.email` -> `check` exits 3 listing it under the `pii`
      severity; drop a non-PII entry -> exits 3 with the plain drift message; add a config entry
      for a nonexistent column -> exits 3 (stale config).
- [ ] Compatibility findings fire: `null` on NOT NULL, `redact` on an integer column, generated
      length over a `varchar(20)`, masked unique column without `unique = true`, `fake_name` on
      a PK, masked table without a PK, and an artificial FK cycle (pump mode finding).
- [ ] All lint/type/test gates clean.

---

## Phase 3 - In-place mask

**Scope**: the first execution mode, end to end: safety guards, advisory lock, audit and progress
tables, batched PK-range updates, resume, dry-run, the JSON report, and post-run verification of
unique indexes (full verification suite arrives in Phase 5; unique re-check cannot wait because
Phase 3 writes could violate it).

### Tasks

- `audit.py` (DDL, run rows, progress upsert, fingerprints); `dialects.py` (advisory locks,
  row-value comparison SELECTs, batched UPDATE forms); `runner_mask.py`; `report.py`;
  `mask` command with `--dry-run`, `--resume`, `--report`.

### Expected commits

1. `feat: add audit and progress tables`
2. `feat: add advisory lock and batched update dialect support`
3. `feat: add in place mask runner with batch transactions`
4. `feat: add resume from progress with fingerprint checks`
5. `feat: add safety guards for mask`
6. `feat: add dry run plan output`
7. `feat: add json audit report`
8. `test: cover mask runner resume and guards on both dialects`

### Verification checklist

- [ ] Full mask of the fixture DB: joins between `users.email` and `orders.customer_email`
      preserved, unique index holds, NULLs untouched, `keep` columns byte-identical, audit row
      `completed`, report counts match row counts.
- [ ] Same dump masked twice (fresh restores) with one secret -> identical masked values; a
      different secret -> different values.
- [ ] Kill the process mid-table (test hook between batches); re-run without `--resume` ->
      refused exit 4; with `--resume` -> final state equals an uninterrupted run's dump.
- [ ] Resume with a modified config or different secret -> refused with the fingerprint message.
- [ ] Mask against a DB named outside the safety pattern -> refused before any write; second
      mask after completion -> refused (already masked); concurrent second process -> lock
      refusal.
- [ ] Forced unique collision (two rows crafted to collide after suffixing) -> batch rolls
      back, `E_MASK` names table and constraint, no partial batch visible.
- [ ] `--dry-run` performs no writes (checked via row counts and absence of audit tables) and
      prints the per-column plan; sentinel grep over all captured output finds no real value.

---

## Phase 4 - Pump mode

**Scope**: source-to-target pumping: source read-only session, target emptiness and schema-match
preflight, topological ordering, PostgreSQL COPY and MySQL multi-row INSERT writers, per-table
row counts in the report, dry-run.

### Tasks

- Topo sort over the FK graph (deterministic order for equal ranks); `runner_pump.py`; COPY
  writer in `dialects.py`; `pump` command flags (`--source-url`, `--target-url`, `--dry-run`,
  `--report`).

### Expected commits

1. `feat: add fk topological sort`
2. `feat: add read only source session and target preflight`
3. `feat: add pump runner with copy and batched inserts`
4. `feat: wire pump command with dry run and report`
5. `test: cover pump ordering writers and preflight on both dialects`

### Verification checklist

- [ ] Pump fixture source -> empty target: identical row counts per table, masked values equal
      what in-place mask produces for the same secret (cross-mode determinism), FKs valid.
- [ ] Non-empty target -> refused exit 4; target schema missing a column -> exit 3; target name
      outside safety pattern -> exit 4.
- [ ] Source session verified read-only (attempted write in a test hook fails); source data
      byte-identical after the run.
- [ ] Child-before-parent fixture ordering handled by topo sort; artificial cycle still refused
      at preflight.
- [ ] Interrupted pump: truncate target, re-run, clean result (documented recovery path works).
- [ ] All gates clean on both dialects.

---

## Phase 5 - Verification suite, hardening, release polish

**Scope**: the full `verify` command (FK orphans, join pairs, unique re-check, pump row counts)
wired standalone and into both runners; the value-hygiene sentinel test over the whole suite;
CI workflow (lint + unit always; integration job with docker services); README finalized with
real install/run examples and the privacy statement.

### Tasks

- `verify.py` complete; runners call it and set `failed` on verification failure; `verify`
  command with `--source-url` for pump count checks; sentinel-grep harness; GitHub Actions
  workflow; README and `.env.example` final pass.

### Expected commits

1. `feat: add verification suite`
2. `feat: run verification after mask and pump`
3. `feat: wire standalone verify command`
4. `test: add value hygiene sentinel sweep`
5. `build: add ci workflow with dockerized integration job`
6. `docs: finalize readme with usage and privacy statement`

### Verification checklist

- [ ] Manually orphan a row in the masked fixture -> `verify` exits 3 naming the FK and count;
      break a join pair -> named; inject a duplicate into a unique-masked column -> named;
      pump with a deleted target row -> row-count check fails.
- [ ] A verification failure after a successful mask sets the audit row `failed` and exits 1.
- [ ] Sentinel sweep passes over the entire integration suite's captured output.
- [ ] CI green: unit job without docker, integration job with both services.
- [ ] README instructions executed verbatim on a clean checkout produce a masked fixture DB.

---

## Backlog

- Deterministic key translation (masking PK/FK values consistently on both sides); large design,
  needs its own phase and PRD amendment.
- Typed fake strategies for dates (`fake_date` with year preservation) and numerics (jitter);
  blocked on strategy-set approval since it widens the config surface.
- `--truncate-target` convenience for pump; deferred because destructive DDL/DML belongs to the
  operator in v1.
- Cyclic FK support via deferred constraints on PostgreSQL; rejected at check in v1.
- Parallel table workers; single-writer simplicity wins until throughput data says otherwise.
- Optional index drop/recreate around large in-place runs; destructive, operator-owned for now.
- Decide whether `hash` truncates to the column length. `docs/architecture.md` says it does;
  `strategies.py` returns the full 64-char digest and `check` reports a shorter column as
  incompatible instead. Truncating changes masked output for existing configs, so it is an owner
  call between amending the architecture doc and changing the strategy.
- Decide what `init` should emit for `safety.database_name_pattern` when the introspected
  database name does not match the default `_(staging|masked)$`. Today it anchors the pattern to
  that database name, so a config generated against production would let `mask` run against
  production. Options: emit the anchored pattern with an explicit warning comment, emit no
  pattern and let `check` demand one, or refuse. Each changes the "init output passes check
  unedited" property, so it needs a call rather than a quiet fix.
- MySQL functional unique indexes cannot be tied to a column: SQLAlchemy reports neither column
  names nor expression text for them, so `check` cannot demand `unique = true` there. PostgreSQL
  is covered.
