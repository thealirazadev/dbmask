# Design - dbmask (CLI UX)

dbmask is an operator tool that will mostly run inside CI logs and terminal scrollback. The UX
priorities, in order: never surprise (especially about what will be written where), be readable in
a plain log file, and make failure output actionable without a manual.

## Command tree

```
dbmask [--config PATH] COMMAND
  init    --url URL [-o dbmask.toml] [--force]
  check   --url URL
  mask    --url URL [--dry-run] [--resume] [--report PATH] [--allow-remasked]
  pump    --source-url URL --target-url URL [--dry-run] [--report PATH]
  verify  --url URL [--source-url URL]
```

- `--config` defaults to `./dbmask.toml`. Every `--*url` flag falls back to its env var
  (`DBMASK_URL`, `DBMASK_SOURCE_URL`, `DBMASK_TARGET_URL`); the README recommends env vars so
  credentials stay out of shell history. The secret is env-only: `DBMASK_SECRET`, no flag.
- `--allow-remasked` exists solely for test environments and is called out as dangerous in
  `--help`; there is deliberately no `--force` on `mask` or `pump`.

## Output channels

- **stdout**: human-readable results (plans, summaries, findings tables). Stable enough to read,
  not a machine interface; scripts should consume exit codes and `--report` JSON.
- **stderr**: logfmt progress lines and the single final error line on failure.
- Color: bold/red/green via Click only when stdout is a TTY; `NO_COLOR` respected; content is
  identical with color off. No spinners, no progress bars, no cursor tricks: a batch commit logs
  one line, which is what you want in CI scrollback.

## States and example output

`check` success (exit 0):

```
dbmask check against postgresql://app_staging
  34 columns configured across 6 tables
  strategies: fake_email 3, fake_name 2, fake_phone 1, fake_address 4, hash 2, redact 1, null 2, keep 19
  drift: none. compatibility: ok. safety pattern: matched.
```

`check` with drift (exit 3), PII findings first, one line per finding, machine-greppable prefix:

```
finding[pii-drift]    users.ssn            not in config; name matches pattern "ssn"; strategy required
finding[drift]        orders.gift_note     not in config; add a strategy or explicit keep
finding[compat]       users.email          unique index ix_users_email but unique = true not set
error[E_DRIFT]: 3 findings (1 pii). fix dbmask.toml and re-run check.
```

`mask --dry-run` prints the plan and stops (exit 0): per table, row count, each masked column
with its strategy, batch count at the configured `batch_size`, then
`dry run: no data was read or written.`

`mask` progress (stderr, logfmt, one line per event):

```
run.started run_id=3f2a... mode=mask db=app_staging tables=6
mask.table_started table=users rows=182304 batches=37
mask.batch_committed table=users batch=1/37 rows=5000 elapsed_ms=210
mask.table_done table=users rows=182304 elapsed_s=8.1
verify.check_passed check=unique index=ix_users_email
run.completed run_id=3f2a... rows_masked=214887 elapsed_s=41.2
```

Failure ends with exactly one stderr line, after any findings:
`error[E_SAFETY]: database "app_prod" does not match safety.database_name_pattern "_(staging|masked)$"`.

## Prompts and confirmations

None. dbmask is non-interactive by design (CI-first); all confirmation is expressed in config
(safety pattern) and explicit flags (`--resume`, `--allow-remasked`, `--force` on init only).
A TTY prompt would create a mode that CI can never exercise.

## Exit codes (contract, tested)

| Code | Meaning |
|---|---|
| 0 | Success (including a clean dry-run and a clean check) |
| 1 | Runtime failure: connection, SQL error, write failure, verification failure after a run |
| 2 | Config or usage error (bad TOML, unknown strategy, missing secret, bad flags) |
| 3 | Drift or verification findings from `check`/`verify` (the "fix your config" code) |
| 4 | Safety refusal: pattern mismatch, already masked, incomplete run, lock busy, non-empty target |

The 3/4 split matters operationally: 3 means edit the config, 4 means stop and think about the
database you pointed at.

## Help text

Every command's `--help` states what it writes and where in the first line (`mask: UPDATE rows in
place in the database at --url`), because that is the single most important fact about each
command. Strategy names and the error-code list appear in `dbmask check --help` as reference.

## Accessibility and internationalization

Plain ASCII output (no box drawing, no glyphs) so screen readers and log parsers cope; column
alignment uses spaces. Messages are English-only in v1; Faker locale affects generated data, not
UI text.
