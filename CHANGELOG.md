# Changelog

## Unreleased

### Added
- An in-place mask runner processes data in batch transactions.
- An advisory lock and batched update support were added for the database dialects.
- Audit and progress tables record each run.
- The `check` command reports problems with exit codes.
- The `init` command writes a starter config.
- The starter config emitter creates a masking config from the schema.
- Schema drift and compatibility findings are reported.
- Schema introspection flags columns whose names match PII patterns.
- A CLI skeleton handles the project secret.
- Strategy results are cached per unique value.
- A deterministic strategy engine derives masked values from an HMAC of the secret.
- Config loading and validation.
- Logging in logfmt format scrubs passwords from URLs.
- Error types map to exit codes.

### Fixed
- `init` no longer anchors the safety pattern to the introspected database.
- `mask --dry-run` stops before writing, and the report is filled in.
- Black was upgraded to patch security advisories.
- Verify join pairs are checked before a run writes anything.
- The starter config is written atomically.
- Expression unique indexes are tied to their columns.
- The decoded password is scrubbed from driver messages.
- Connection failures that do not come from SQLAlchemy map to `E_CONNECT`.
