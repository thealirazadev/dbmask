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
declarative config, a schema drift guard, deterministic output, post-run verification, and an
audit report.

## Planned features

All of the following is planned behavior; implementation follows `docs/phases.md`.

- Nine masking strategies per column: `fake_email`, `fake_name`, `fake_phone`, `fake_address`,
  `redact` (fixed string), `null`, `hash` (keyed), `keep`, and derived `template` values.
- Deterministic masking seeded per project secret via HMAC: preserves joins (the same email in
  `users.email` and `orders.customer_email` masks identically) and makes runs reproducible.
- Two modes: in-place mask of a restored copy (batched updates by primary-key range, resumable
  after a crash), and pump mode (read-only source to an empty target, in foreign-key order,
  COPY-fast on PostgreSQL).
- Schema drift guard: unconfigured columns fail the run by name; columns matching PII name
  patterns (email, phone, name, address, dob, ssn, ...) are deny-by-default.
- `dbmask init`: introspects the schema and writes a starter config with PII columns pre-flagged.
- Referential consistency verification after every run: foreign keys, configured logical join
  pairs, unique indexes, and pump row counts.
- Safety guards: database-name pattern check before any write, already-masked detection,
  single-runner lock, secret only via environment variable.
- JSON audit report of what was masked (counts per column, never sample values) and a `--dry-run`
  that prints the full plan without touching data.

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
| [docs/PRD.md](docs/PRD.md) | Problem, goals, non-goals, user stories, requirements, success criteria |
| [docs/architecture.md](docs/architecture.md) | Stack rationale, components, data model, flows, failure modes, invariants |
| [docs/rules.md](docs/rules.md) | Engineering rules, including the value-hygiene rule |
| [docs/phases.md](docs/phases.md) | Implementation phases with commit lists and verification checklists |
| [docs/design.md](docs/design.md) | CLI UX: commands, flags, output, exit codes |
| [docs/testing.md](docs/testing.md) | Test strategy, docker compose setup, CI plan |
| [docs/api-contracts.md](docs/api-contracts.md) | CLI contracts, error format, report JSON schema |
| [docs/launch-checklist.md](docs/launch-checklist.md) | Pre-release checks |
| [docs/memory.md](docs/memory.md) | Working log and decisions |

## Status

Planning stage: these documents are the complete spec, and no code exists yet. Implementation
proceeds phase by phase per `docs/phases.md`, starting with the deterministic strategy engine.
