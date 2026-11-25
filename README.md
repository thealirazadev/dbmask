# dbmask

A production database anonymizer for creating safe staging copies. dbmask reads a reviewed,
committed masking config (`dbmask.toml`) that maps every column to a strategy, then rewrites a
restored production copy in place or pumps it into a fresh target, producing a database with fake
but realistic data. Masking is deterministic: the same real value always becomes the same fake
value, across tables and across runs, so joins, unique constraints, and test fixtures keep
working. Anything the config does not cover fails the run loudly, so a new PII column added by a
migration can never slip into staging unmasked.

## The problem it solves

Restoring production into staging hands real customer emails, names, phones, and addresses to
the least protected environment you run. Hand-rolled UPDATE scripts miss new columns silently,
scramble the same email differently in different tables (breaking joins), collide on unique
indexes, and leave no record of what was actually masked. dbmask replaces that script with a
declarative config, a schema drift guard, deterministic output, an audit report, and (planned) post-run
verification.

## Features

- Nine masking strategies per column: `fake_email`, `fake_name`, `fake_phone`, `fake_address`,
  `redact` (fixed string), `null`, `hash` (keyed), `keep`, and derived `template` values.
- Deterministic masking seeded per project secret via HMAC: preserves joins (the same email in
  `users.email` and `orders.customer_email` masks identically) and makes runs reproducible.
- In-place mask of a restored copy: batched updates in primary-key order, each batch committed
  with its progress record, resumable after a crash with `--resume`.
- Schema drift guard: unconfigured columns fail the run by name; columns matching PII name
  patterns (email, phone, name, address, dob, ssn, ...) are deny-by-default.
- `dbmask init`: introspects the schema and writes a starter config with PII columns pre-flagged.
- `dbmask check`: drift and compatibility findings (key columns, length budgets, unique indexes,
  declared join pairs) before anything is written.
- Safety guards: database-name pattern check before connecting, already-masked detection
  (`--allow-remasked` overrides it, test environments only), single-runner lock, secret only via
  environment variable.
- JSON audit report of the run (counts, config hash, secret fingerprint, never sample values) and
  a `--dry-run` that checks the plan without touching data.

Not implemented yet: `pump` (read-only source into an empty target, in foreign-key order) and
`verify` (post-run checks of foreign keys, join pairs, unique indexes and row counts). Both
commands exist on the CLI and currently exit with a "not implemented" error.

An honest limit, stated up front: deterministic masking is pseudonymization, not anonymization.
Anyone holding the project secret can confirm guessed real values by dictionary attack, and value
frequencies survive masking. Treat masked copies as confidential and the secret as a credential;
details in `docs/architecture.md`.

## Stack

- Python 3.12, Click CLI
- SQLAlchemy Core for introspection and data movement; PostgreSQL (psycopg 3) and MySQL (PyMySQL)
- Faker for realistic values, seeded per value from HMAC(project secret, input)
- pytest; integration tests against dockerized PostgreSQL 16 and MySQL 8.4
- uv, ruff, black, mypy

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Stack rationale, components, data model, flows, failure modes, invariants |
| [docs/decisions.md](docs/decisions.md) | Design decisions and the bugs and edge cases behind them |
| [docs/design.md](docs/design.md) | CLI UX: commands, flags, output, exit codes |
| [docs/testing.md](docs/testing.md) | Test strategy, docker compose setup, CI |
| [docs/api-contracts.md](docs/api-contracts.md) | CLI contracts, error format, report JSON schema |

## Status

In development. `init`, `check` and `mask` work against PostgreSQL and MySQL and are covered by
unit and integration tests; `pump` and `verify` are not implemented yet.
