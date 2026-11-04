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
from tests.conftest import TEST_SECRET, USER_ROWS

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
