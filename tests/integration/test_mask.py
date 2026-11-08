"""In-place mask end to end against real PostgreSQL and MySQL, through the CLI."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from dbmask.cli import main
from dbmask.errors import EXIT_OK
from tests.conftest import SENTINEL_EMAIL, SENTINELS, TEST_SECRET

pytestmark = pytest.mark.integration


@pytest.fixture
def url(fixture_schema: Engine, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DBMASK_SECRET", TEST_SECRET)
    for name in ("DBMASK_URL", "DBMASK_SOURCE_URL", "DBMASK_TARGET_URL"):
        monkeypatch.delenv(name, raising=False)
    return fixture_schema.url.render_as_string(hide_password=False)


@pytest.fixture
def config(url: str, tmp_path: Path) -> Path:
    """init's output, plus what an operator does next: declare the logical join and give both
    sides the same rule (users.email carries `unique` because of its unique index)."""
    path = tmp_path / "dbmask.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    text = path.read_text().replace(
        'customer_email = "fake_email"',
        'customer_email = { strategy = "fake_email", unique = true }',
    )
    text += '\n[[verify.joins]]\nleft = "users.email"\nright = "orders.customer_email"\n'
    path.write_text(text)
    assert main(["--config", str(path), "check", "--url", url]) == EXIT_OK
    return path


def rows(engine: Engine, table: str) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(r._mapping) for r in conn.execute(sa.text(f"SELECT * FROM {table} ORDER BY id"))
        ]


def table_names(engine: Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_dry_run_writes_nothing(fixture_schema: Engine, url: str, config: Path, capsys) -> None:
    before = rows(fixture_schema, "users")

    assert main(["--config", str(config), "mask", "--url", url, "--dry-run"]) == EXIT_OK

    assert rows(fixture_schema, "users") == before
    assert not any(name.startswith("dbmask_") for name in table_names(fixture_schema))
    out = capsys.readouterr().out
    assert "no rows were read or written" in out
    assert not any(sentinel in out for sentinel in SENTINELS)


def test_full_mask_preserves_joins_nulls_and_keep_columns(
    fixture_schema: Engine, url: str, config: Path, tmp_path: Path, capsys
) -> None:
    before = {row["id"]: row for row in rows(fixture_schema, "users")}
    report = tmp_path / "report.json"

    assert main(["--config", str(config), "mask", "--url", url, "--report", str(report)]) == EXIT_OK

    users = {row["id"]: row for row in rows(fixture_schema, "users")}
    orders = rows(fixture_schema, "orders")
    assert users[1]["email"] != SENTINEL_EMAIL
    # orders.customer_email repeats users.email; determinism keeps the logical join.
    assert {o["customer_email"] for o in orders if o["user_id"] == 1} == {users[1]["email"]}
    assert len({u["email"] for u in users.values()}) == len(users)  # unique index holds
    for user_id, row in users.items():
        assert row["age"] == before[user_id]["age"]  # keep column untouched
        if before[user_id]["phone"] is None:
            assert row["phone"] is None  # NULLs pass through
    # internal_note is `keep` in init's output (its name is not a PII pattern), so it is the
    # one column whose seeded text survives; every masked column must not.
    masked = [{k: v for k, v in u.items() if k != "internal_note"} for u in users.values()]
    dumped = json.dumps([*masked, *orders], default=str)
    assert not any(sentinel in dumped for sentinel in SENTINELS)

    data = json.loads(report.read_text())
    assert data["status"] == "completed"
    assert data["secret_fingerprint"]
    assert data["started_at"] and data["finished_at"]
    assert not any(sentinel in capsys.readouterr().out for sentinel in SENTINELS)


def test_masking_is_deterministic_and_secret_dependent(
    fixture_schema: Engine, url: str, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import _reset

    assert main(["--config", str(config), "mask", "--url", url]) == EXIT_OK
    first = rows(fixture_schema, "users")
    _reset(fixture_schema)
    assert main(["--config", str(config), "mask", "--url", url]) == EXIT_OK
    assert rows(fixture_schema, "users") == first

    _reset(fixture_schema)
    monkeypatch.setenv("DBMASK_SECRET", "a-different-secret-of-at-least-32-chars")
    assert main(["--config", str(config), "mask", "--url", url]) == EXIT_OK
    assert rows(fixture_schema, "users")[0]["email"] != first[0]["email"]


def test_a_second_mask_is_refused(fixture_schema: Engine, url: str, config: Path) -> None:
    assert main(["--config", str(config), "mask", "--url", url]) == EXIT_OK
    assert main(["--config", str(config), "mask", "--url", url]) != EXIT_OK


def small_batches(config: Path) -> Path:
    config.write_text(config.read_text().replace("batch_size = 5000", "batch_size = 1"))
    return config


def crash_after(monkeypatch: pytest.MonkeyPatch, batches: int) -> None:
    """Fail the run after `batches` committed batches, as a killed process would stop."""
    from dbmask import runner_mask

    real = runner_mask.update_batch
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > batches:
            raise RuntimeError("simulated crash between batches")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_mask, "update_batch", flaky)


def test_resume_finishes_an_interrupted_run_exactly_like_an_uninterrupted_one(
    fixture_schema: Engine, url: str, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dbmask import runner_mask
    from tests.conftest import _reset

    small_batches(config)
    args = ["--config", str(config), "mask", "--url", url]
    assert main(args) == EXIT_OK
    expected = {t: rows(fixture_schema, t) for t in ("users", "orders", "addresses")}

    _reset(fixture_schema)
    real = runner_mask.update_batch
    crash_after(monkeypatch, 2)
    assert main(args) != EXIT_OK
    monkeypatch.setattr(runner_mask, "update_batch", real)

    assert main(args) == 4  # an incomplete run exists: refused without --resume
    assert main([*args, "--resume"]) == EXIT_OK
    assert {t: rows(fixture_schema, t) for t in expected} == expected


def test_resume_refuses_a_changed_secret(
    fixture_schema: Engine, url: str, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(config)
    crash_after(monkeypatch, 1)
    args = ["--config", str(config), "mask", "--url", url]
    assert main(args) != EXIT_OK
    monkeypatch.undo()
    monkeypatch.setenv("DBMASK_SECRET", "another-secret-that-is-at-least-32-chars")
    for name in ("DBMASK_URL", "DBMASK_SOURCE_URL", "DBMASK_TARGET_URL"):
        monkeypatch.delenv(name, raising=False)

    assert main([*args, "--resume"]) != EXIT_OK


def test_a_second_process_is_refused_while_the_lock_is_held(
    fixture_schema: Engine, url: str, config: Path
) -> None:
    from dbmask.dialects import advisory_lock

    before = rows(fixture_schema, "users")
    with fixture_schema.connect() as other, advisory_lock(other, fixture_schema.url.database):
        assert main(["--config", str(config), "mask", "--url", url]) == 4
    assert rows(fixture_schema, "users") == before
