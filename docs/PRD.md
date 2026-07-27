# Product Requirements - dbmask

## Problem

Every team that restores a production database into staging faces the same trap: the copy is full
of real customer data, and lower environments have weaker access controls, more connected tools,
and dumps that end up on laptops. Hand-written UPDATE scripts rot, miss new columns silently, and
destroy what makes a staging copy useful: joins break when the same email is scrambled two ways,
unique constraints blow up on collisions, and nobody can say afterwards what was actually masked.

dbmask is a CLI that turns a restored production copy into a safe staging database, driven by a
reviewed, committed config, with deterministic join-preserving output and a loud failure whenever
the schema grows a column the config does not cover.

## Goals

1. **Config-driven masking.** A `dbmask.toml` maps `table.column` to a strategy: `fake_email`,
   `fake_name`, `fake_phone`, `fake_address`, `redact`, `null`, `hash`, `keep`, or a derived
   `template`. The config is the single reviewed source of truth for what happens to every column.
2. **Deterministic output.** Every strategy is seeded from HMAC(project secret, input value): the
   same input produces the same fake output across columns, tables, and runs. A real email in
   `users.email` and `orders.customer_email` masks to one identical fake value, preserving
   logical joins and making tests reproducible.
3. **Two modes.** In-place mask of a restored copy (batched UPDATEs by primary-key range), and
   pump mode (read source, write masked rows to an empty target in foreign-key order).
4. **Schema drift guard.** Any live column not covered by the config fails the run loudly, listing
   the offenders. Columns whose names match PII patterns (email, phone, name, address, dob, ssn,
   and similar) are deny-by-default: never silently kept, always an explicit strategy.
5. **Verified referential consistency.** After masking, declared foreign keys, configured logical
   join pairs, unique indexes, and (in pump mode) row counts are re-verified; a failure fails the run.
6. **Safe by construction.** The secret lives only in an environment variable; real values never
   appear in logs, reports, or errors; in-place mode refuses databases whose name fails a
   configured safety pattern; an already-masked copy is detected and refused.
7. **Operator ergonomics.** `dbmask init` writes a starter config with PII columns pre-flagged;
   `--dry-run` shows the full plan without touching data; every run can emit a JSON audit report
   of counts per column, never sample values.

## Non-goals

- Automatic PII detection by sampling data. Detection is name-pattern heuristics only; reading
  values to guess sensitivity would put real data in the tool's path.
- NoSQL, columnar, or file targets. PostgreSQL and MySQL only in v1.
- Subsetting or shrinking. v1 produces full-size copies; row filtering is a later project.
- Reversible tokenization or format-preserving encryption; dbmask output is one-way by intent.
- Masking key columns; PK and FK columns must be `keep` in v1, key translation is backlogged.
- Typed fake strategies for date and numeric PII (for example date of birth); v1 offers `null` or
  an explicit `keep` there.
- A daemon, server, or GUI; dbmask is a CLI invoked by an operator or a pipeline.

## Target users

A backend developer or platform engineer who owns the "refresh staging from prod" job, keeps
infrastructure config in git, and wants the masking step reviewable in a pull request, repeatable
in CI, and impossible to run against production by accident. Secondary: a security reviewer who
needs to answer "what exactly is masked and how" from the config and the audit report alone.

## Core user stories

1. As an operator, I run `dbmask init` against a restored copy and get a starter `dbmask.toml`
   with every column listed and PII-named columns pre-flagged: a review, not a blank page.
2. As an operator, I run `dbmask check` in CI; when a migration adds a `phone_backup` column the
   check fails and names it, so no new PII ever slips into staging unmasked.
3. As an operator, I run `dbmask mask` on the restored copy and get a database where
   `users.email` and `orders.customer_email` still join, unique indexes still hold, and the
   report shows counts for every masked column.
4. As an operator, I run `dbmask pump` from a read-only source to an empty target when I cannot
   afford to restore first, and get the same masked result written in foreign-key order.
5. As an operator, when a run dies mid-table I re-run with `--resume` and it continues from the
   last committed batch instead of double-masking what was already done.
6. As a security reviewer, I can state from `dbmask.toml` and the JSON report what was masked,
   with which strategy, and how many rows, without ever seeing a real or fake sample value.

## Functional requirements

- **FR1 Config.** TOML with per-column strategy entries, strategy options (`value`, `template`,
  `unique`), verification join pairs, a safety database-name pattern, and batch settings.
  Unknown keys are rejected.
- **FR2 Determinism.** Strategy output is a pure function of (secret, strategy family, normalized
  input value). NULL passes through every strategy. The secret comes only from `DBMASK_SECRET`
  and must be at least 32 characters.
- **FR3 init.** Introspects tables, columns, types, nullability, PKs, FKs, and unique indexes,
  then emits a complete starter config with proposed strategies and PII flags as comments.
- **FR4 check.** Validates config against the live schema: existence both ways (drift guard),
  strategy compatibility (type, length, nullability), `keep`-only key columns, uniqueness
  coverage, FK cycles for pump, safety settings. A distinct exit code marks drift.
- **FR5 mask.** Batched UPDATE by PK range, one transaction per batch, progress persisted
  atomically with each batch, resumable, guarded by the safety pattern, an advisory lock, and
  already-masked detection.
- **FR6 pump.** Reads the source read-only; writes masked rows to an empty matching target in
  topological FK order using COPY on PostgreSQL and multi-row INSERT on MySQL.
- **FR7 verify.** FK orphan scan, configured join-pair scan, unique re-check, and pump row-count
  equality; runs automatically after mask/pump and standalone as `dbmask verify`.
- **FR8 Reporting and dry-run.** `--report` writes a JSON audit report (run metadata, per-column
  counts, verification results); human output and logs never contain cell values. `--dry-run`
  performs all checks, counts rows, and prints the full plan without reading or writing row data.

## Success criteria

- A fixture database masked twice from the same dump with the same secret produces byte-identical
  masked values both times; changing the secret changes every masked value.
- `users.email` and `orders.customer_email` sharing 500 real addresses share exactly 500 fake
  addresses after masking, and the join row count is unchanged.
- Adding an uncovered `ssn` column to the fixture schema makes `check` and `mask` exit non-zero
  with the column named; an uncovered non-PII column does the same with a distinct message.
- A unique index on a masked email column holds after masking a table containing deliberately
  colliding inputs; a forced collision fails verification loudly.
- Killing `mask` mid-table and re-running with `--resume` matches the final state of an
  uninterrupted run. Refused before any write: a fresh run over an incomplete one, a database
  name that fails the safety pattern, and a copy that was already masked.
- Grepping all integration-suite output (logs, reports, errors) for seeded real values finds none.
