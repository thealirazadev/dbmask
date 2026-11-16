# Security policy

## Supported versions

Only the latest release on `main` receives security fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
(Security tab, "Report a vulnerability") or email thealirazadev@gmail.com with the
steps to reproduce. You should get a reply within 72 hours.

## How dbmask handles sensitive data

- The project secret is read only from the `DBMASK_SECRET` environment variable. There is deliberately no command-line flag for it (`src/dbmask/cli.py`).
- Database passwords are replaced with `***` in printed URLs and in driver error messages (`src/dbmask/logging.py`).
- The audit tables hold identifiers, counts and timestamps only, never cell values or the secret. The secret is recorded only as a 12-character salted SHA-256 fingerprint (`src/dbmask/audit.py`).
- Masking is deterministic (HMAC-SHA256 keyed by the secret), so anyone holding the secret can test guessed real values, and value frequencies survive masking. Treat masked copies as confidential (`src/dbmask/strategies.py`, "Privacy analysis" in `docs/architecture.md`).
