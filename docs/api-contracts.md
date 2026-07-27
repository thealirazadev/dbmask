# CLI Contracts - dbmask

dbmask has no HTTP surface. Its public contracts are: the CLI commands (flags, output, exit
codes), the environment variables, the `dbmask.toml` config format, the JSON audit report, and
the two tables it creates in the target database (`dbmask_audit`, `dbmask_progress`, specified in
`docs/architecture.md`). All are agreed here before any code is written. Scripts must depend on
exit codes and the report JSON, not on prose output.

## Environment variables

| Variable | Required by | Purpose |
|---|---|---|
| `DBMASK_SECRET` | check, mask, pump, verify | Project secret, >= 32 chars. Env-only; no flag exists. |
| `DBMASK_URL` | fallback for `--url` | Database URL for init/check/mask/verify. |
| `DBMASK_SOURCE_URL` | fallback for `--source-url` | Pump source; also verify's count reference. |
| `DBMASK_TARGET_URL` | fallback for `--target-url` | Pump target. |

URL form: `postgresql+psycopg://user:pass@host:port/dbname` or
`mysql+pymysql://user:pass@host:port/dbname`. Passwords are scrubbed to `***` in every log line.

## Error format (single, consistent)

Every failure ends with exactly one line on stderr:

```
error[E_CODE]: human-readable, actionable message
```

Findings (from `check`/`verify`) precede it, one per line, machine-greppable:

```
finding[SEVERITY]  <table.column or check name>  <explanation>
```

Severities: `pii-drift`, `drift`, `compat`, `verify`. Messages contain identifiers and counts
only, never cell values, never secrets, never full URLs with passwords.

### Error codes and exit codes

| Exit | Code | When |
|---|---|---|
| 2 | `E_CONFIG` | TOML unparseable, unknown key/strategy/option, secret missing or under 32 chars, bad flag combination. |
| 1 | `E_CONNECT` | Connection or authentication failure to any database. |
| 3 | `E_DRIFT` | Config/schema mismatch in either direction, or compatibility findings, from check or a run preflight. |
| 4 | `E_SAFETY` | Safety-pattern mismatch, already-masked copy, incomplete run without `--resume`, fingerprint mismatch on resume, advisory lock busy, non-empty pump target. |
| 1 | `E_MASK` | Write-time failure during a run (unique/CHECK/NOT NULL violation, SQL error); batch rolled back. |
| 3 | `E_VERIFY` | Standalone `verify` found orphans/duplicates/count mismatches. Post-run verification failure exits 1 (the run itself failed). |
| 1 | `E_INTERNAL` | Unexpected exception; traceback in the log, one line on stderr. |

Exit 0 is success, including clean `--dry-run` and clean `check`.

---

## dbmask init

Introspect the schema and write a starter config. Requires `--url`/`DBMASK_URL`. Does not
require the secret (it generates no values).

```
$ dbmask init --url "$DBMASK_URL" -o dbmask.toml
introspected 6 tables, 47 columns
flagged 11 pii columns by name pattern
wrote dbmask.toml (review every entry before first mask)
```

Generated file excerpt (comments are part of the contract; operators review by reading them):

```toml
[tables.users.columns]
id         = "keep"
# pii: name matches "email"
email      = { strategy = "fake_email", unique = true }
# pii: name matches "name"
full_name  = "fake_name"
# pii: name matches "dob"; date column, no fake strategy in v1
# TODO review: null chosen because column is nullable
dob        = "null"
file_name  = "keep"   # pattern "name" matched; kept: review
```

| Flag | Meaning |
|---|---|
| `-o, --output PATH` | Default `./dbmask.toml`. |
| `--force` | Overwrite an existing output file; refused otherwise (exit 2). |

Failures: `E_CONNECT` (exit 1); existing file without `--force` (`E_CONFIG`, exit 2).

---

## dbmask check

Validate config against the live schema. This is also every run's preflight, so `check` passing
means `mask`/`pump` will get past preflight on the same schema.

```
$ dbmask check --url "$DBMASK_URL"
dbmask check against postgresql://app_staging
  47 columns configured across 6 tables
  drift: none. compatibility: ok. safety pattern: matched.
$ echo $?
0
```

Drift example (exit 3):

```
finding[pii-drift]  users.ssn         not in config; name matches pattern "ssn"; strategy required
finding[drift]      orders.gift_note  not in config; add a strategy or explicit keep
finding[drift]      users.legacy_id   configured but not in schema; remove from dbmask.toml
finding[compat]     users.email       unique index ix_users_email but unique = true not set
error[E_DRIFT]: 4 findings (1 pii). fix dbmask.toml and re-run check.
```

Checks performed: unknown config keys/strategies; secret present and >= 32 chars; column
existence both ways; PII-pattern severity split; `null` vs NOT NULL; strategy/type fit; length
budget (strategy max + unique suffix vs column length); `unique = true` required when a unique
index covers a masked column; PK/FK columns must be `keep`; masked tables must have a PK; FK
cycle detection; `safety.database_name_pattern` present and matching for mask.

---

## dbmask mask

In-place mask of the database at `--url`. Destructive by design; all guards run before the first
write.

```
$ dbmask mask --url "$DBMASK_URL" --report out/report.json
run.started run_id=3f2a9c mode=mask db=app_staging tables=6        # stderr, logfmt
mask.table_started table=users rows=182304 batches=37
mask.batch_committed table=users batch=1/37 rows=5000 elapsed_ms=210
...
verify.check_passed check=fk fk=orders_user_id_fkey
verify.check_passed check=join pair=users.email~orders.customer_email
verify.check_passed check=unique index=ix_users_email
run.completed run_id=3f2a9c rows_masked=214887 elapsed_s=41.2
masked 214887 rows across 6 tables in 41.2s; report: out/report.json   # stdout summary
```

| Flag | Meaning |
|---|---|
| `--dry-run` | Preflight + plan only; no reads or writes of row data; exit 0 on clean plan. |
| `--resume` | Continue the newest incomplete run; requires matching config hash and secret fingerprint. |
| `--report PATH` | Write the JSON audit report. |
| `--allow-remasked` | Skip the already-masked refusal. Test environments only. |

Guard failures (all exit 4, all before any write):

```
error[E_SAFETY]: database "app_prod" does not match safety.database_name_pattern "_(staging|masked)$"
error[E_SAFETY]: this database was already masked by run 3f2a9c on 2026-07-27; re-masking corrupts data
error[E_SAFETY]: incomplete run 9b1d44 exists; re-run with --resume, or restore a fresh copy
error[E_SAFETY]: resume refused: config hash changed since run 9b1d44 started
error[E_SAFETY]: another dbmask process holds the lock on this database
```

Write failure (exit 1): `error[E_MASK]: unique constraint ix_users_email violated writing batch
41 of users; batch rolled back`.

---

## dbmask pump

Read `--source-url` (read-only session), write masked rows to `--target-url` (must match the
source schema and be empty) in FK topological order.

```
$ dbmask pump --source-url "$DBMASK_SOURCE_URL" --target-url "$DBMASK_TARGET_URL" --report out/report.json
run.started run_id=77c1e0 mode=pump source=app_prod_replica target=app_staging tables=6
pump.table_done table=users rows=182304 elapsed_s=12.4
pump.table_done table=addresses rows=88110 elapsed_s=5.0
...
verify.check_passed check=rowcount table=users source=182304 target=182304
run.completed run_id=77c1e0 rows_masked=214887 elapsed_s=63.8
pumped 214887 rows across 6 tables in 63.8s; report: out/report.json
```

| Flag | Meaning |
|---|---|
| `--dry-run` | Preflight + plan (table order, row counts); no writes. |
| `--report PATH` | Write the JSON audit report (audit row lives in the target). |

Preflight failures: schema mismatch between source and target (`E_DRIFT`, exit 3); non-empty
target, target name outside the safety pattern (`E_SAFETY`, exit 4). Recovery from an
interrupted pump: truncate the target and re-run; the source is untouched by construction.

---

## dbmask verify

Standalone consistency verification of an already-masked database. Read-only, repeatable.

```
$ dbmask verify --url "$DBMASK_URL"
verify.check_passed check=fk fk=orders_user_id_fkey orphans=0
verify.check_passed check=join pair=users.email~orders.customer_email orphans=0
verify.check_passed check=unique index=ix_users_email duplicates=0
verification passed: 9 checks
```

Failure (exit 3):

```
verify.check_failed check=join pair=users.email~orders.customer_email orphans=17
error[E_VERIFY]: 1 of 9 checks failed; this copy must not be used until re-created
```

| Flag | Meaning |
|---|---|
| `--source-url URL` | Optional; enables the pump row-count comparison against a source. |

Checks: every declared FK (orphan scan), every `[[verify.joins]]` pair (orphan scan), every
unique index covering a masked column (duplicate scan), per-table row counts when
`--source-url` is given. Output contains counts only, never offending values or PKs.

---

## JSON audit report (`--report`)

Written atomically (temp file + rename) after the run and its verification. Schema (stable; new
optional fields may be added, existing fields never change meaning):

```json
{
  "dbmask_report_version": 1,
  "run": {
    "id": "3f2a9c1e-8d5b-4f6a-9c2d-1e8f7a6b5c4d",
    "mode": "mask",
    "tool_version": "0.1.0",
    "config_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "locale": "en_US",
    "database": "app_staging",
    "dialect": "postgresql",
    "started_at": "2026-07-27T09:12:03Z",
    "finished_at": "2026-07-27T09:12:44Z",
    "status": "completed"
  },
  "tables": [
    {
      "name": "users",
      "rows": 182304,
      "columns": [
        { "name": "email", "strategy": "fake_email", "unique": true,
          "masked": 182290, "nulls_passed": 14, "cache_hit_rate": 0.31 },
        { "name": "full_name", "strategy": "fake_name",
          "masked": 182304, "nulls_passed": 0, "cache_hit_rate": 0.08 }
      ]
    }
  ],
  "verification": {
    "status": "passed",
    "checks": [
      { "check": "fk", "name": "orders_user_id_fkey", "orphans": 0, "passed": true },
      { "check": "join", "name": "users.email~orders.customer_email", "orphans": 0, "passed": true },
      { "check": "unique", "name": "ix_users_email", "duplicates": 0, "passed": true }
    ]
  },
  "warnings": []
}
```

Guarantees: counts and metadata only; no sample values, no PK values, no secret material beyond
`config_hash` (which hashes the config file, not the secret). `keep` columns are listed with
`"strategy": "keep"` and no count fields, so the report enumerates every column the config
covered: the report plus the config answers "what happened to column X" completely.

## Config format

The full `dbmask.toml` contract (keys, strategy options, shorthand, join pairs, safety and
settings blocks) is specified in `docs/architecture.md` under "Data model" and is not repeated
here. `check` is the executable validator of that contract.
