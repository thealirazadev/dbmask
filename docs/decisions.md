# Design decisions

The engineering choices behind dbmask, the reasons for them, and the bugs and edge cases that
shaped the current code. `docs/architecture.md` describes the components and flows; this page
explains why they look the way they do.

`init`, `check` and in-place `mask` are implemented. The `pump` and `verify` commands are defined
on the CLI but currently exit with a "not implemented" error.

## 1. Masked output is a pure function of the secret and the value

`StrategyEngine.mask` derives everything from an HMAC-SHA256 of the project secret over the
strategy family plus the normalized input. For the Faker strategies the first 8 bytes of that
digest seed a per-value `seed_instance` call, so the same real value always produces the same fake
value, across tables and across runs, with no wall clock, RNG state or per-run data involved. This
is what makes `users.email` and `orders.customer_email` mask to the same address and keeps
logical joins intact. The family is part of the HMAC message (with a separator byte) for domain
separation, so one string masked as a name and as an email gives uncorrelated results.
`fake_email` folds case and surrounding whitespace before hashing so case variants of one address
converge; other families hash the exact string. NULL passes through every strategy, and `hash`
is keyed rather than a bare SHA-256, because an unkeyed hash of low-entropy PII is a lookup table.
Faker is pinned exactly in `pyproject.toml` because its data changes alter masked output between
releases. The trade-off is stated plainly in the README: deterministic masking is
pseudonymization, and anyone holding the secret can confirm guessed values.

Production data repeats heavily, so generated values are kept in a bounded LRU cache keyed by
family and normalized input. CPU cost then scales with distinct values rather than rows, and the
cache never holds anything but generated output.

## 2. The secret is environment-only and never held as plain text

`DBMASK_SECRET` is read in `cli.py` only, must be at least 32 characters, and has no command-line
flag, because a flag would put it into shell history and `ps` output on shared build machines.
The engine keeps a keyed `hmac` object built in its constructor and copies it per value, so no
attribute, `repr` or `vars()` of a long-lived object can expose the secret; a unit test asserts
this. The audit tables record only a 12-character salted SHA-256 fingerprint, which is enough to
detect a changed secret on resume without revealing it.

## 3. Uniqueness comes from a deterministic suffix, not a seen-set

A masked column covered by a unique index needs `unique = true`, which appends 12 hex characters
(48 bits) taken from the same HMAC digest, inserted before the `@` for emails. An in-memory
seen-set would guarantee uniqueness but caps table size at available RAM and is lost on a crash,
which breaks resume. Below roughly 16M distinct values a suffix collision is negligible, and a
residual one fails loudly at the unique index instead of being hidden. The suffix is not added to
`template` output: a template renders from key or `keep` columns and is already unique by
construction, so a suffix would only damage the readable value. Because Faker's pools are small
(duplicate fake emails are near certain at scale), the suffix is what makes masking a unique
column safe at all.

## 4. Deny by default: every live column must be configured

`check` and the mask preflight compare the config with the live schema. Any column the config does
not mention is drift (exit 3), so a PII column added by a migration cannot slip into staging
unmasked. Column names matching the PII pattern list in `introspect.py` (email, phone, name,
address, dob, ssn and similar) are reported as `pii-drift`. The patterns deliberately over-match:
flagging `file_name` costs one explicit `keep` in review, while a missed match is real data in
staging. A live table missing from the config yields one table-level finding plus one `pii-drift`
per PII-named column, so a wide table does not bury the output. dbmask's own `dbmask_audit` and
`dbmask_progress` tables are skipped during introspection; otherwise the first mask run would make
every later `check` fail on tables the operator never wrote.

## 5. Compatibility checks run before anything is written

`check` also verifies that each rule can actually be applied. Primary and foreign key columns must
use `keep`: masking keys would break referential integrity, and deterministic key translation
would need FK-aware update ordering and sequence reseeding, so it is out of scope rather than half
done. `null` on a NOT NULL column, a generating strategy on a non-text column, and output that can
exceed the column length are all findings. Length budgets use declared per-strategy ceilings
(email 64, name 64, phone 32, address 128, hash 64, plus 13 characters when `unique` is set), and
a unit test samples many seeds and fails if a Faker upgrade widens output past them. `hash` keeps
the full 64-character digest instead of truncating to the column, because truncation weakens
uniqueness; a column too short for it is reported as incompatible instead.

A unique index over an expression (`UNIQUE (lower(email))`) reports `column_names = [None]` on
PostgreSQL. That was first turned into a column literally named "None", and a masked column under
such an index was never told it needed `unique = true`, so `check` passed and the run would have
collided mid-write. Unique indexes now keep their expression text, and a column counts as covered
when its name appears in the expression on a word boundary. This over-matches by design: an
unnecessary suffix costs 13 characters, a missed one is a unique violation partway through a
destructive run. On MySQL, SQLAlchemy reports a functional unique index with neither column names
nor expression text, so dbmask cannot tie it to a column there.

## 6. Join pairs are validated on the whole rule

`[[verify.joins]]` pairs declare columns that must mask identically. They were originally never
checked, and a natural example was broken: `users.email` had `unique = true` because of its
unique index while `orders.customer_email` did not, so the suffix landed on one side only and the
two columns stopped sharing a fake address. That was only discoverable after a run had rewritten
every row. `check` now reports a pair that references a missing column as drift and a pair whose
two sides differ in strategy, `value`, `template` or `unique` as a compatibility finding, since the
suffix is part of the output. Setting `unique = true` on both sides is the fix, and it is asserted
end to end on both dialects.

## 7. The safety pattern is checked from the URL before connecting

`safety.database_name_pattern` is evaluated against the database name parsed from the URL before
any connection is opened. If the operator pointed at production, dbmask must not authenticate to
it, read its catalog or print its column names, and the check needs only the URL. A missing
pattern is `E_CONFIG` (exit 2, fix the config); a mismatch is `E_SAFETY` (exit 4, stop and look at
the target). `init` always writes the default pattern, which matches names ending in `_staging` or
`_masked`. An earlier version anchored the pattern to the introspected database when the default
did not match, which meant a config generated against production would permit masking production;
now it writes a comment saying `mask` will refuse that database instead.

## 8. A batch and its progress commit together, so resume is exact

`mask` processes tables in alphabetical order and rows in primary key order, using keyset
pagination after the last committed key (a row-value comparison for composite keys, so the cursor
is one predicate instead of an OR-chain). Each batch's UPDATEs and its `dbmask_progress` upsert
commit in the same transaction. A crash mid-batch leaves both uncommitted, so `--resume` re-masks
that batch identically; a crash between batches leaves progress committed, so resume skips ahead.
Storing progress in a separate commit or marking masked rows would either reopen a double-mask
window or require changes to the operator's tables.

Because masking a masked value does not give the same result, a database with a completed run is
refused, and a fresh run over an incomplete or failed one is refused unless `--resume` is given.
Resume also requires the stored config hash and secret fingerprint to match. The config hash is
taken over the canonicalized plan (sorted JSON of locale, safety, settings, resolved rules and
sorted join pairs), not the file bytes, so reformatting or re-commenting `dbmask.toml` does not
block a resume while any change to what happens to a column does. On PostgreSQL a batch is one
`UPDATE ... FROM (VALUES ...)`; on MySQL it is an executemany UPDATE keyed by the primary key.
All SQL is SQLAlchemy Core with bound parameters and quoted identifiers, and the few raw `text()`
constructs live only in `dialects.py`.

## 9. One runner per database, enforced with a session lock

The whole run holds a session-scoped advisory lock (`pg_try_advisory_lock` on a key derived from
the database name, or MySQL `GET_LOCK` with a zero timeout). Session scope means the lock survives
the per-batch commits and dies with the connection if the process does. There is no waiting: a
second process is refused immediately with `E_SAFETY` and writes nothing, because two runners
interleaving batches would double-mask rows.

## 10. Dry run reads the schema only, and the runner owns its transactions

`mask --dry-run` runs the same preflight findings and prints the tables it would mask, then
returns without reading rows or creating the audit tables. The first real-database runs found
that the CLI fell through from the dry-run branch into a real run, so "no rows were read or
written" was printed just before the whole database was masked; the branch now returns. They also
found that wrapping the run in `engine.begin()` failed every real run with `InvalidRequestError`,
because the runner commits each batch itself. The CLI now opens a plain connection and lets the
runner manage commits. The JSON report is filled from the audit row (status, secret fingerprint,
start time) rather than placeholders.

## 11. No cell value or credential ever reaches output

Errors, log lines, reports and audit rows carry identifiers, counts and durations only. Every
failure is a `DbmaskError(code, message)` that `cli.py` prints as one `error[E_CODE]: message` line
with a fixed exit code (0 ok, 1 runtime, 2 config or usage, 3 drift or verification, 4 safety).
An unexpected exception is reported as `E_INTERNAL` with only its class name logged, not a
traceback, because driver exception messages can embed row data and stderr is the only sink.
`log_event` refuses any field that is not a string, number or boolean. The integration fixture
seeds sentinel values and the tests assert they never appear in captured output.

Two connection bugs fell under this rule. Drivers fail before SQLAlchemy can wrap them (PyMySQL
raises a bare `AttributeError` on an unknown charset, a missing driver raises
`ModuleNotFoundError`, and `make_url` raises a bare `ValueError` on a non-numeric port), so those
surfaced as `E_INTERNAL`; they now map to `E_CONNECT` and `E_CONFIG`. And `scrub_password` replaced
only the percent-encoded password from the URL, while the driver echoes the decoded form, so a
password with `@`, `/` or `:` would have leaked into an `E_CONNECT` message. Both spellings are
replaced now.

## 12. `init` writes a reviewable config with its own emitter

`init` introspects the schema and writes a starter `dbmask.toml` with a dedicated emitter rather
than a TOML-writer dependency, because the file's value is its comments: which pattern flagged
each column, and `TODO review` markers where no strategy fits. The standard library's `tomllib`
is read-only, and unit tests round-trip the emitter's output through it. Each column gets the first
strategy from a fallback chain (the pattern's proposal, then `redact = "MASKED"`, then `keep`
with a TODO) whose worst-case output fits the column, which is what lets `init` output pass
`check` unedited; the visible cost is that short address-like columns come out as `redact`. PII
columns always get a masking strategy, and the operator downgrades false positives to `keep`
during review. `init --force` originally truncated and rewrote the reviewed config in place, so a
full disk or a kill midway left a file that still parsed with the tail columns silently gone; the
write now goes to a temporary file in the same directory that is renamed over the target.
