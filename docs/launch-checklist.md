# Launch Checklist - dbmask

Work top to bottom before tagging the first public release. Nothing is checked until verified on
a clean machine, not the development environment.

## Packaging & install

- [ ] `uv sync` then `uv run dbmask --help` works on a clean checkout with only Python 3.12 and
      uv installed.
- [ ] `pyproject.toml` metadata complete (name, version, description, license, entry point);
      every dependency pinned exactly; `uv.lock` committed and in sync.
- [ ] Version string in `pyproject.toml`, `dbmask --version`, and the report `tool_version`
      agree.

## Correctness gates

- [ ] Full suite green on both dialects via `docker-compose.test.yml`; CI green on the release
      commit.
- [ ] Determinism spot check on a real-sized dump: two fresh restores masked with the same
      secret diff as identical; a third with a rotated secret diffs everywhere expected.
- [ ] Kill-mid-run resume exercised once on a multi-million-row table on both dialects.
- [ ] Cross-mode check: pump output equals in-place output for the same dump and secret.
- [ ] Verification suite catches a hand-planted orphan, duplicate, and count mismatch (break it
      on purpose once before trusting it).

## Safety & privacy

- [ ] Sentinel value-hygiene sweep green over the entire integration suite's captured output.
- [ ] `mask` against a database named like production refused; already-masked refusal and lock
      refusal reproduced manually.
- [ ] `DBMASK_SECRET` absent from every log, report, audit row, and `ps` output during a run;
      URL passwords scrubbed in all log lines.
- [ ] README privacy statement reviewed: pseudonymization limits, dictionary-attack risk, secret
      handling guidance, "still personal data under GDPR-style regimes" wording present.
- [ ] `.env.example` contains dummies only; `.gitignore` covers `.env` and `out/`.

## Documentation

- [ ] README instructions executed verbatim on a clean machine produce a masked fixture
      database, including the docker compose test path.
- [ ] `docs/api-contracts.md` exit codes match the implementation (tested per code).
- [ ] Strategy list, config example, and PII pattern behavior in README match the code.
- [ ] Backlog in `docs/phases.md` reflects what actually shipped vs deferred.

## Repository

- [ ] LICENSE file present and matching `pyproject.toml`.
- [ ] No stray files: build artifacts, `out/`, scratch configs, dumps, or fixture data with
      anything resembling real PII.
- [ ] Git tag `v0.1.0` on the release commit; GitHub release notes list the strategy set, the
      two modes, and the honest privacy limits.
