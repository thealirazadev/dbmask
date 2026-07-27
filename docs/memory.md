# Project Memory - dbmask

Running log of what is done, in progress, and decided. Update after every meaningful chunk of
work; log every non-obvious decision with its reason. Keep entries short and dated.

## Completed

- 2026-07-27 - Planning documentation created (README, PRD, architecture, rules, phases, design,
  testing, api-contracts, launch-checklist, memory). No code yet; docs await owner review before
  Phase 1 starts.

## Project status

- Planning stage. Implementation follows `docs/phases.md` starting with Phase 1 (config,
  strategy engine, CLI skeleton) once the docs are approved.

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
- 2026-07-27 - PK and FK columns are restricted to `keep` in v1 and `check` enforces it.
  Deterministic key translation is feasible (the same HMAC machinery would preserve joins) but
  it drags in FK-aware update ordering, cascade interactions, and sequence/autoincrement
  reseeding; it is backlogged as its own phase rather than half-shipped. Logical joins on
  non-key columns (the users.email/orders.customer_email case) already work via determinism.
