# Project Memory - dbmask

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; docs await owner review before
  Phase 1 starts.
- 2026-07-28 - Phase 1 complete: config, strategy engine, CLI skeleton. Nine commits as listed in
  `docs/phases.md`. `pyproject.toml` pins every dependency exactly (click 8.4.2, sqlalchemy
  2.0.51, faker 40.36.0, psycopg[binary] 3.3.4, pymysql 1.2.0; dev pytest 9.1.1, ruff 0.16.0,
  black 26.5.1, mypy 2.3.0) with `uv.lock` committed. Modules landed: `errors.py`, `logging.py`,
  `config.py`, `strategies.py`, `cli.py`. 81 unit tests, no database and no network.

- 2026-07-29 - Phase 2 complete: introspection, drift and compatibility findings, the starter
  config emitter, `init`, `check`, and the dockerized two-dialect test bed. Seven commits as
  listed in `docs/phases.md`. New modules: `introspect.py`, `drift.py`, `emit.py`; `cli.py` now
  implements `init` and `check`; `logging.py` gained `scrub_password`. `docker-compose.test.yml`
  runs postgres:16 and mysql:8.4; `tests/conftest.py` owns the seeded fixture schema (users,
  addresses, orders, order_items with FKs, a unique email index, a composite PK, PII-named
  columns and sentinel rows) and the unit-test schema builders. 206 tests: 160 unit (no database,
  no network) and 46 integration, each running once per dialect.

- 2026-07-29 - Phase 2 functional review: five defects found and fixed, one commit each. Every
  fix has a test that was observed failing on the pre-fix source and passing after it. The suite
  went from 206 to 224 tests, all green with both containers up.
  1. `fix: map non sqlalchemy connect failures to e_connect`. `connect()` caught only
     `SQLAlchemyError` and `OSError`, but drivers fail before SQLAlchemy can wrap them: PyMySQL
     raises a bare `AttributeError` on an unknown charset (reproduced against the live MySQL
     container) and a missing driver raises `ModuleNotFoundError`. A connection problem was
     reported as `error[E_INTERNAL]: unexpected AttributeError`. In the same class of bug,
     `make_url` raises a bare `ValueError` on a non-numeric port, so a typo in a URL exited 1
     instead of the documented 2. Both now map to their contract codes.
  2. `fix: scrub the decoded password from driver messages`. `scrub_password` replaced only the
     percent-encoded spelling taken from the URL, but the driver is handed the decoded password
     and echoes that one, so any password containing URL-reserved characters (`@`, `/`, `:`)
     would have passed through into the `E_CONNECT` message. Both spellings are replaced now.
  3. `fix: tie expression unique indexes to their columns`. A unique index over an expression
     (`UNIQUE (lower(email))`) reports `column_names = [None]` on PostgreSQL, which the model
     turned into a column literally named "None". Two consequences: garbage in the schema model,
     and a masked column under such an index was never told it needs `unique = true`, so `check`
     passed and the run would have collided mid-write (Faker's address pool is small, so
     duplicate fake emails are near certain at scale). `UniqueIndexInfo` now keeps the expression
     text and matches column names inside it on a word boundary.
  4. `fix: write the starter config atomically`. `init --force` truncated the operator's reviewed
     config and wrote in place, so a full disk or a kill halfway through left a half-written file
     that still parses as TOML with the columns below the cut silently gone. The write now goes
     to a temporary file in the same directory and is renamed over the target.
  5. `fix: check verify join pairs before a run writes`. `[[verify.joins]]` was never diffed
     against the schema or against the plan, and the pair in the `docs/architecture.md` example
     is broken as written: `users.email` carries `unique = true` (it has a unique index) and
     `orders.customer_email` does not, so the unique suffix is appended to one side only and the
     two columns no longer share a fake address. Confirmed directly against the engine:
     `summersbenjamin-9c69da58c6df@example.com` versus `summersbenjamin@example.com`. That is the
     PRD's headline join-preservation promise failing, and it was only discoverable after a run
     had rewritten every row. `check` now reports a stale join reference as drift and a pair whose
     two sides do not carry an identical rule as a compatibility finding; setting `unique = true`
     on both sides is the fix and is asserted end to end on both dialects.

## Project status

- Phase 2 done and verified locally on 2026-07-29; awaiting owner approval before Phase 3
  (in-place mask). Observed on that date, on this machine, with both containers healthy:
  `ruff check .`, `black --check .`, `mypy --strict src` clean; `pytest -q` 206 passed;
  `pytest -m integration` 46 passed across postgresql and mysql; with unreachable URLs all 46
  skip naming the compose command. `dbmask init` against the fixture emits 25 columns over 4
  tables with 11 pii comments and that file passes `dbmask check` unedited (exit 0) on both
  dialects; re-running `init` without `--force` exits 2 and leaves the file untouched. Dropping
  the `users.email` entry exits 3 with `finding[pii-drift]`, dropping `users.age` exits 3 with
  the plain drift message, a stale `users.legacy_id` entry exits 3, a database name outside the
  safety pattern exits 4 before connecting, and a wrong password exits 1 with the password
  scrubbed to `***`. All six compatibility findings plus the no-primary-key and FK-cycle findings
  fire against live schemas. Nothing in Phase 2 is unverified.
- Phase 1 verification (2026-07-28) still holds: `dbmask --help` lists the five commands;
  `dbmask mask` without a config exits 2 with one `error[E_CONFIG]` line; a missing or short
  `DBMASK_SECRET` exits 2 without echoing the value.
- `MAX_OUTPUT_LENGTH` ceilings (email 64, name 64, phone 32, address 128) are now the live length
  budget `check` uses. Measured maxima over 3000 seeds on 2026-07-28 were 32/27/22/64, so the
  ceilings hold with headroom, and the unit test fails if a Faker bump widens output past them.
- Open for Phase 3: `mask`, `pump` and `verify` still exit with the "not implemented" message.
  `dialects.py`, `audit.py`, `runner_mask.py`, `report.py` and `verify.py` do not exist yet.
- Post-review state, observed on 2026-07-29 on this machine with both containers healthy:
  `ruff check .`, `black --check .`, `mypy --strict src` clean; `pytest -q` 224 passed (163 unit,
  61 integration, each integration test once per dialect). Manual CLI run against the live
  PostgreSQL fixture: `init` exit 0 (4 tables, 25 columns, 11 pii), `check` on that file exit 0,
  `init` again exit 2, a URL with a non-numeric port exit 2, and a MySQL URL with a bogus charset
  exit 1 with `error[E_CONNECT]` and the password shown as `***`. Nothing in this review is
  unverified.
- Known limitation recorded during the review: on MySQL, SQLAlchemy 2.0.51 reports a functional
  unique index with neither column names nor expression text, so dbmask cannot tie it to a
  column. PostgreSQL is covered by the expression match. The integration test asserts only what
  holds on both dialects, that no unique index reports a column the table does not have.

## Decisions log

- 2026-07-27 - The secret is env-only (`DBMASK_SECRET`), with no CLI flag equivalent. A flag
  would put the secret into shell history and `ps` output on shared build machines; env-only
  costs nothing in CI and closes the leak by construction.
- 2026-07-27 - Resume correctness rides on one rule: a batch's UPDATEs and its `dbmask_progress`
  upsert commit in the same transaction. Alternatives (progress in a separate commit, or a
  marker column on masked rows) either reopen the double-mask window or require schema changes
  to the operator's tables. Because `mask(mask(x)) != mask(x)`, a fresh run over a partial one is
  refused outright rather than warned about; the only paths forward are `--resume` with matching
  config-hash and secret-fingerprint, or restoring a fresh copy.
- 2026-07-27 - Uniqueness on masked columns uses a deterministic 12-hex-char HMAC suffix with the
  database unique constraint plus the verify pass as backstop, instead of an in-memory seen-set.
  A seen-set guarantees uniqueness but caps table size at available RAM and breaks resume (the
  set is lost on crash); 48 bits of suffix keeps collisions negligible below ~16M distinct
  values, and a residual collision fails loudly rather than silently.
- 2026-07-27 - `init` writes TOML via a purpose-built emitter rather than a TOML-writer
  dependency. The generated config's value is its comments (which pattern flagged each PII
  column, TODO markers for undecidable cases), and no generic writer emits comments; stdlib
  `tomllib` is read-only. The emitter's output round-trips through `tomllib` in unit tests.
- 2026-07-28 - `ColumnRule`, `STRATEGY_NAMES` and the template placeholder pattern live in
  `config.py`, not `strategies.py`, so the dependency runs one way (`strategies` imports
  `config`). They are the vocabulary the config file is written in; putting them in
  `strategies.py` would have made `config.py` import `strategies.py` and `strategies.py` import
  `ColumnRule` back, a cycle. Masking behaviour (normalization, HMAC seed, Faker calls, suffix,
  cache, output ceilings) stays entirely in `strategies.py` as the module boundary rule requires.
- 2026-07-28 - The engine never stores the secret. It keeps a keyed `hmac` object created in the
  constructor and copies it per value, so no attribute, `repr`, or `vars()` of a long-lived
  object can leak the secret. A unit test asserts this.
- 2026-07-28 - `unique = true` adds the 12-hex suffix to generated values (fake_*, hash, redact)
  but not to `template` output. A template renders from PK or `keep` columns, so it is already
  unique by construction; appending a suffix to `user_{id}` would only damage the readable
  derived value the operator asked for. On a template column the flag records that a unique index
  covers it, which is what `check` will assert in Phase 2.
- 2026-07-28 - Unexpected exceptions log only the exception class name, not a traceback. The
  value-hygiene rule outranks the "logged traceback" line in `docs/rules.md`: driver exception
  messages can embed row data, and stderr is the only sink dbmask has, so a traceback there could
  print real values. The stderr line still names the exception class so a bug stays diagnosable.
- 2026-07-28 - `config_hash` hashes the canonicalized plan (sorted JSON of locale, safety,
  settings, resolved column rules, sorted join pairs), not the file bytes. Reformatting or
  re-commenting `dbmask.toml` must not block a `--resume`, while any change to what happens to a
  column must. Join order is semantically irrelevant, so it is sorted out of the hash.
- 2026-07-29 - `connect()` and `database_name()` live in `introspect.py`, not in a new module.
  `introspect.py` is already the one Phase 2 module that talks to a live database, and the
  directory layout in `docs/architecture.md` has no slot for a connection helper; `dialects.py`
  is reserved for the dialect-specific SQL (COPY, advisory locks, read-only session) that lands
  in Phase 3 and 4. Adding a module would have meant changing the architecture doc.
- 2026-07-29 - `check` evaluates `safety.database_name_pattern` from the URL before it opens a
  connection, although `docs/architecture.md` lists safety as step 5 of check. If the operator
  pointed at production, dbmask must not authenticate to it, read its catalog, or print its
  column names. The check needs only the URL, so doing it first costs nothing. A missing pattern
  is `E_CONFIG` (exit 2: edit the config); a mismatch is `E_SAFETY` (exit 4: stop and think about
  the database you pointed at), which is the 3/4 split `docs/design.md` argues for.
- 2026-07-29 - `find_findings` takes a mode (`mask`, `pump`, `all`) and `check` passes `all`, so
  one `check` reports both the mask-only "no primary key" finding and the pump-only FK-cycle
  finding, as the Phase 2 checklist requires. The parameter exists so the Phase 3 and 4
  preflights can pass their own mode: an FK cycle is harmless for an in-place mask and must not
  block it, and a table without a primary key is fine for pump.
- 2026-07-29 - `introspect` skips `dbmask_audit` and `dbmask_progress` (`OWNED_TABLES`). They are
  dbmask's own bookkeeping in the masked database, not the operator's schema. Without the skip,
  the first `mask` run would make every later `check` fail as drift on tables the operator never
  wrote and cannot sensibly configure.
- 2026-07-29 - A live table missing from the config produces one table-level `drift` finding plus
  one `pii-drift` finding per PII-named column in it, rather than one line per column. A 60-column
  table would otherwise bury the output, and deny-by-default still has to stay visible per column
  for the PII ones.
- 2026-07-29 - `init` picks a strategy by walking a fallback chain: the pattern's proposal, then
  `redact = "MASKED"`, then `keep` with a TODO comment, taking the first whose worst-case output
  fits the column length. This is what makes "init output passes check unedited" true. The visible
  cost is that short address-ish columns (`city varchar(80)`, `postal_code varchar(20)`) come out
  as `redact`, with a comment naming the strategy that did not fit.
- 2026-07-29 - `init` proposes `fake_name` for `file_name`, where the excerpt in
  `docs/api-contracts.md` shows `keep`. init cannot tell a person's name from a file's, and PRD
  goal 4 makes PII-named columns deny-by-default, so a flagged column always gets a masking
  strategy and the operator downgrades it to `keep` during review. `docs/api-contracts.md` is left
  unchanged pending an owner call on that one illustrative line.
- 2026-07-29 - Cycle detection treats a self-referencing table as a cycle. Pump inserts a table's
  rows in primary key order, which does not guarantee a parent row precedes its child inside one
  table either, so refusing is honest rather than optimistic. Cyclic FK support is already a
  backlog item in `docs/phases.md`.
- 2026-07-29 - `tests/` gained `__init__.py` files. Without them pytest imports `conftest.py` as a
  top-level `conftest` module and a second time as `tests.conftest` when a test imports the shared
  sentinel constants and schema builders, which would give the fixture and the test two different
  `MetaData` objects.
- 2026-07-27 - PK and FK columns are restricted to `keep` in v1 and `check` enforces it.
  Deterministic key translation is feasible (the same HMAC machinery would preserve joins) but
  it drags in FK-aware update ordering, cascade interactions, and sequence/autoincrement
  reseeding; it is backlogged as its own phase rather than half-shipped. Logical joins on
  non-key columns (the users.email/orders.customer_email case) already work via determinism.
- 2026-07-29 - A join pair is validated on the whole rule, not just the strategy name. Two
  columns mask alike only when strategy, `value`, `template` and `unique` all agree, because the
  unique suffix is part of the output. Comparing strategy names alone would have passed the
  broken pair in the architecture example. Missing join references are `drift` (the schema moved)
  and mismatched rules are `compat` (the config is internally inconsistent), which keeps the
  existing severity meanings intact.
- 2026-07-29 - Coverage of an expression unique index is decided by looking for the column name
  inside the expression text, on a word boundary. It over-matches rather than under-matches by
  design: an unnecessary `unique = true` costs an extra 13 characters of output, while a missed
  one is a unique violation partway through a destructive run.
- 2026-07-29 - Two review findings are left for the owner rather than changed unilaterally, both
  logged in the `docs/phases.md` backlog. `docs/architecture.md` says `hash` truncates to the
  column length, but `strategies.py` returns the full 64-char digest and `check` reports a
  shorter column as incompatible; changing either side changes masked output or the architecture
  doc, so it needs a call. And `init` anchors `safety.database_name_pattern` to the database it
  introspected when the default staging pattern does not match, which means a config generated
  against production would permit masking production.
