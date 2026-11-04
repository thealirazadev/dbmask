"""mask end to end against real PostgreSQL and MySQL, through the CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from dbmask.audit import AUDIT_TABLE, PROGRESS_TABLE
from dbmask.audit import metadata as audit_metadata
from dbmask.cli import main
from dbmask.errors import EXIT_OK
from tests.conftest import SENTINEL_NOTE, SENTINELS, TEST_SECRET, USER_ROWS

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
