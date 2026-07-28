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

## Project status

- Phase 1 done and verified locally; awaiting owner approval before Phase 2 (introspection,
  init, check, docker test bed). Verified on 2026-07-28: `ruff check .`, `black --check .`,
  `mypy --strict src`, and `pytest -q` (81 passed) all clean; `dbmask --help` lists the five
  commands; `dbmask mask` without a config exits 2 with one `error[E_CONFIG]` line; a missing or
  short `DBMASK_SECRET` exits 2 without echoing the value. Nothing is unverified in Phase 1: no
  database container was needed, since no Phase 1 code touches a database.
- Known follow-up for Phase 2: `MAX_OUTPUT_LENGTH` ceilings (email 64, name 64, phone 32,
  address 128) are the length budget `check` will use. Measured maxima over 3000 seeds today were
  32/27/22/64, so the ceilings hold with headroom, and the unit test fails if a Faker bump widens
  output past them.

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
- 2026-07-27 - PK and FK columns are restricted to `keep` in v1 and `check` enforces it.
  Deterministic key translation is feasible (the same HMAC machinery would preserve joins) but
  it drags in FK-aware update ordering, cascade interactions, and sequence/autoincrement
  reseeding; it is backlogged as its own phase rather than half-shipped. Logical joins on
  non-key columns (the users.email/orders.customer_email case) already work via determinism.
