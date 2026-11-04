"""mask end to end against real PostgreSQL and MySQL, through the CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from dbmask import runner_mask
from dbmask.audit import AUDIT_TABLE, PROGRESS_TABLE
from dbmask.audit import metadata as audit_metadata
from dbmask.cli import main
from dbmask.errors import EXIT_OK, DbmaskError, ErrorCode
from dbmask.introspect import introspect
from dbmask.runner_mask import mask_plan
from dbmask.strategies import StrategyEngine
from tests.conftest import ADDRESS_ROWS, SENTINEL_NOTE, SENTINELS, TEST_SECRET, USER_ROWS, make_plan

pytestmark = pytest.mark.integration


@pytest.fixture
def url(fixture_schema: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("DBMASK_SECRET", TEST_SECRET)
    for name in ("DBMASK_URL", "DBMASK_SOURCE_URL", "DBMASK_TARGET_URL"):
        monkeypatch.delenv(name, raising=False)
    # A run leaves dbmask_audit and dbmask_progress behind, and the next test must start
    # from a fresh restore, not from an already masked copy.
    audit_metadata.drop_all(fixture_schema, checkfirst=True)
    yield fixture_schema.url.render_as_string(hide_password=False)
    audit_metadata.drop_all(fixture_schema, checkfirst=True)


@pytest.fixture
def config(url: str, tmp_path: Path) -> Path:
    path = tmp_path / "dbmask.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    return path


def user_emails(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return list(connection.execute(sa.text("SELECT email FROM users ORDER BY id")).scalars())


def test_dry_run_writes_nothing(fixture_schema: Engine, url: str, config: Path) -> None:
    assert main(["--config", str(config), "mask", "--url", url, "--dry-run"]) == EXIT_OK

    assert user_emails(fixture_schema) == [row["email"] for row in USER_ROWS]
    inspector = sa.inspect(fixture_schema)
    assert not inspector.has_table(AUDIT_TABLE)
    assert not inspector.has_table(PROGRESS_TABLE)


def test_mask_rewrites_pii_and_keeps_the_email_join(
    fixture_schema: Engine, url: str, config: Path
) -> None:
    # The operator's review step: declare the logical join so both sides mask alike.
    text = config.read_text(encoding="utf-8").replace(
        'customer_email = "fake_email"',
        'customer_email = { strategy = "fake_email", unique = true }',
    )
    text += '\n[[verify.joins]]\nleft = "users.email"\nright = "orders.customer_email"\n'
    config.write_text(text, encoding="utf-8")
    assert main(["--config", str(config), "check", "--url", url]) == EXIT_OK

    assert main(["--config", str(config), "mask", "--url", url]) == EXIT_OK

    emails = user_emails(fixture_schema)
    assert not set(emails) & {row["email"] for row in USER_ROWS}
    with fixture_schema.connect() as connection:
        joined = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM orders o JOIN users u "
                "ON u.id = o.user_id AND u.email = o.customer_email"
            )
        ).scalar()
        status = connection.execute(sa.text(f"SELECT status FROM {AUDIT_TABLE}")).scalars().all()
        # Notes are not PII-named, so init leaves them as keep; every other sentinel is masked.
        dumped = " ".join(
            str(value)
            for row in connection.execute(sa.text("SELECT * FROM users"))
            for value in row
        )
    assert joined == 4
    assert status == ["completed"]
    assert not [s for s in SENTINELS if s != SENTINEL_NOTE and s in dumped]


def test_a_failed_batch_is_rolled_back_not_committed(
    fixture_schema: Engine, url: str, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail after the batch UPDATE has run but before its transaction commits.
    def failing_upsert(*args: object, **kwargs: object) -> None:
        raise DbmaskError(ErrorCode.E_MASK, "writing dbmask_progress failed")

    monkeypatch.setattr(runner_mask, "upsert_progress", failing_upsert)
    assert main(["--config", str(config), "mask", "--url", url]) != EXIT_OK

    with fixture_schema.connect() as connection:
        streets = connection.execute(sa.text("SELECT street FROM addresses ORDER BY id")).scalars()
        status = connection.execute(sa.text(f"SELECT status FROM {AUDIT_TABLE}")).scalars()
        assert list(streets) == [row["street"] for row in ADDRESS_ROWS]
        assert list(status) == ["failed"]


def test_a_uuid_primary_key_masks_and_resumes(
    fixture_schema: Engine, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if fixture_schema.dialect.name != "postgresql":
        pytest.skip("MySQL has no native uuid column type")
    ids = [UUID(int=1), UUID(int=2), UUID(int=3)]
    tokens = sa.Table(
        "tokens",
        sa.MetaData(),
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("owner_name", sa.String(120), nullable=False),
    )
    tokens.create(fixture_schema)
    try:
        with fixture_schema.begin() as connection:
            connection.execute(
                sa.insert(tokens), [{"id": key, "owner_name": "Real Person"} for key in ids]
            )
        plan = make_plan({"tokens": {"id": "keep", "owner_name": "fake_name"}})
        schema = introspect(fixture_schema)

        calls = 0
        real_upsert = runner_mask.upsert_progress

        def upsert_then_crash(*args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DbmaskError(ErrorCode.E_MASK, "simulated crash")
            real_upsert(*args, **kwargs)

        monkeypatch.setattr(runner_mask, "upsert_progress", upsert_then_crash)
        with fixture_schema.connect() as connection, pytest.raises(DbmaskError):
            mask_plan(
                connection, StrategyEngine(TEST_SECRET), plan, schema, TEST_SECRET, batch_size=1
            )
        monkeypatch.setattr(runner_mask, "upsert_progress", real_upsert)
        with fixture_schema.connect() as connection:
            result = mask_plan(
                connection,
                StrategyEngine(TEST_SECRET),
                plan,
                schema,
                TEST_SECRET,
                resume=True,
                batch_size=1,
            )
            names = connection.execute(
                sa.select(tokens.c.owner_name).order_by(tokens.c.id)
            ).scalars()
            assert "Real Person" not in list(names)
        assert result.rows_masked == len(ids)
    finally:
        audit_metadata.drop_all(fixture_schema, checkfirst=True)
        tokens.drop(fixture_schema)
