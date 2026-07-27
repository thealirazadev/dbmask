# Engineering Rules - dbmask

These rules extend the workspace CLAUDE.md and are binding for every change in this repository.

## Conventions

- **Module boundaries**: strategy logic only in `strategies.py`; dialect-specific SQL (COPY,
  advisory locks, read-only session, row-value comparisons) only in `dialects.py`; DDL for the
  audit tables only in `audit.py`; Click callbacks in `cli.py` stay thin and delegate to the
  runner/verify modules. No SQL strings inside `cli.py`, ever.
- **SQLAlchemy Core only**: no ORM models, no `Session`. Build statements with the expression
  language; raw `text()` is allowed only in `dialects.py` for constructs Core cannot express,
  and always with bound parameters.
- **Strategies are pure**: a strategy receives `(secret, options, value)` and returns a value.
  No I/O, no clocks, no global mutable state except the bounded LRU cache. New strategies get a
  determinism unit test (same input twice, across two process simulations) before anything else.
- **Naming**: modules and functions snake_case; dataclasses `PascalCase` (`MaskPlan`,
  `SchemaModel`, `ColumnRule`, `Finding`); Click commands are verbs (`init`, `check`, `mask`,
  `pump`, `verify`); error codes are `E_` + SCREAMING_SNAKE (`E_DRIFT`).
- **Commit format**: Conventional Commits, short imperative subject, lowercase after the prefix,
  e.g. `feat: add hmac seeded strategy engine`, `fix: bound unique suffix by column length`.
  One commit per feature or task, in the order listed in `docs/phases.md`.
- **Pinned dependencies**: exact versions in `pyproject.toml`, `uv.lock` committed. Faker in
  particular is pinned exactly (its data changes alter masked output between releases); any bump
  is its own commit, flagged, with the determinism impact stated. No new dependency without
  owner approval.
- **Typing**: full type hints; `mypy --strict` on `src/` must pass from Phase 1 onward.

## The value-hygiene rule (project-critical)

- **No cell value ever leaves the data path.** Not in log lines, exception messages, report
  fields, audit rows, assertion messages, or CLI output. Errors reference tables, columns,
  constraints, counts, and PK-free positions ("batch 41") only.
- Exception wrapping is mandatory at the batch boundary: driver exceptions can embed row data in
  their message, so `runner_mask`/`runner_pump` catch driver errors and re-raise `DbmaskError`
  with table/constraint context and the original message dropped, logged only as its class name.
- Database URLs are logged with the password replaced by `***` (one helper in `logging.py`; no
  ad hoc URL printing).
- The secret: read once from `DBMASK_SECRET` in `cli.py`, passed as an argument, never stored on
  a long-lived object, never in an f-string. `repr` of config/plan objects must not include it.
- The integration suite seeds sentinel values (e.g. `sentinel-real-value-x7`) and asserts they
  appear in no captured stdout/stderr/log/report output. Breaking this test blocks merge.

## Error handling & logging

- **Every external call handles failure**: connections, each batch SELECT/UPDATE, COPY streams,
  lock acquisition, report file writes. No bare calls that assume success.
- **One error shape**: all failures raise `DbmaskError(code, message)`; `cli.py` maps it to a
  single stderr line `error[E_CODE]: message` and the documented exit code (0 ok, 1 runtime,
  2 config/usage, 3 drift/verification, 4 safety refusal). Unexpected exceptions become
  `E_INTERNAL` with exit 1 and a logged traceback (stderr gets one line, not the trace).
- **Structured logging**: logfmt lines to stderr with dotted event keys: `run.started`,
  `check.finding`, `mask.table_started`, `mask.batch_committed`, `mask.table_done`,
  `pump.table_done`, `verify.check_passed`, `verify.check_failed`, `run.completed`,
  `run.failed`. Fields are identifiers, counts, and durations only.
- **Friendly stderr, detailed log**: the one-line error is actionable ("add the column to
  dbmask.toml or mark it keep"); the log line carries the finding list.
- **Exit codes are contract**: tested per code in the CLI test suite; scripts depend on them.

## Security

- No hardcoded secrets or URLs; `.env.example` carries dummies only and is kept current.
- Connection URLs come from flags or `DBMASK_URL`/`DBMASK_SOURCE_URL`/`DBMASK_TARGET_URL` env
  vars. The README recommends env vars over flags (flags leak into shell history and `ps`).
  `DBMASK_SECRET` has no flag equivalent at all, by design.
- All identifiers (table/column names) used in SQL go through SQLAlchemy's quoting; never
  interpolate an identifier into a string by hand. All values are bound parameters.
- `hash` strategy is keyed (HMAC with the project secret), never a bare `sha256(value)`: an
  unkeyed hash of low-entropy PII is a lookup table waiting to happen.
- The PII name-pattern list in `introspect.py` errs toward false positives (matching
  `file_name` is fine; the cost is one explicit `keep`). Never narrow a pattern to reduce noise
  without owner approval.

## Simplicity / YAGNI-KISS

- Build only what the current phase requires. No plugin system for strategies, no parallel
  workers, no config templating, no progress bars beyond plain log lines in v1.
- Two real dialects (PostgreSQL, MySQL) justify the `dialects.py` seam; nothing else in the
  codebase warrants an abstraction layer until a third concrete case exists.
- No new wrapper classes, managers, or utility modules without approval. If a change exceeds
  roughly 150 lines, pause and justify it before continuing.

## Testing discipline

- Unit tests must not require a database or docker; everything under `tests/unit` runs in CI on
  every push in seconds.
- Integration tests are marked `integration`, read connection URLs from env, and skip with a
  clear message when the docker databases are absent. Every schema-touching feature ships with
  tests against both PostgreSQL and MySQL, not just one.
- Determinism tests compare full masked outputs across two separate engine constructions, not
  within one (a shared cache can fake stability).
- After creating or editing files, run `ruff check`, `black --check`, `mypy`, and `pytest` and
  fix all errors before reporting done.

## Boundaries - never do without asking the owner first

- Never modify `docs/PRD.md` or `docs/architecture.md` without flagging the change and getting
  sign-off; they are the source of truth.
- No wholesale file rewrites; targeted edits, destructive changes flagged first.
- Stop after two failed fix attempts on the same problem and report instead of churning.
- Any mid-phase request not in the PRD is classified with the owner as current phase, new phase,
  or Backlog in `docs/phases.md`. Never silently absorb scope.
