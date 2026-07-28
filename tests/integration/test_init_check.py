"""init and check end to end against real PostgreSQL and MySQL, through the CLI.

The operator's first two commands are `init` then `check`, so the round trip is the test
that matters most: whatever init writes must pass check without a single edit.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from dbmask.cli import main
from dbmask.config import MaskPlan, build_plan
from dbmask.drift import MODE_MASK, MODE_PUMP, SEVERITY_COMPAT, find_findings
from dbmask.errors import EXIT_CONFIG, EXIT_FINDINGS, EXIT_OK, EXIT_SAFETY
from dbmask.introspect import introspect
from tests.conftest import SENTINELS, TEST_SECRET

pytestmark = pytest.mark.integration


@pytest.fixture
def url(fixture_schema: Engine, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("DBMASK_SECRET", TEST_SECRET)
    for name in ("DBMASK_URL", "DBMASK_SOURCE_URL", "DBMASK_TARGET_URL"):
        monkeypatch.delenv(name, raising=False)
    return fixture_schema.url.render_as_string(hide_password=False)


@pytest.fixture
def config(url: str, tmp_path: Path) -> Path:
    path = tmp_path / "dbmask.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    return path


def drop_entry(text: str, column: str) -> str:
    return "\n".join(line for line in text.splitlines() if not re.match(rf"^{column}\s*=", line))


def tweaked(text: str, table: str, column: str, rule: Any) -> MaskPlan:
    data = tomllib.loads(text)
    data["tables"][table]["columns"][column] = rule
    return build_plan(data, Path("dbmask.toml"))


def compat_messages(plan: MaskPlan, engine: Engine, mode: str) -> list[str]:
    findings = find_findings(plan, introspect(engine), mode)
    return [f"{f.subject} {f.message}" for f in findings if f.severity == SEVERITY_COMPAT]


def test_init_writes_a_config_that_passes_check(
    url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "dbmask.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    written = capsys.readouterr().out
    assert "introspected 4 tables, 25 columns" in written
    assert "flagged 11 pii columns by name pattern" in written

    text = path.read_text(encoding="utf-8")
    assert '# pii: name matches pattern "email"' in text
    assert '# pii: name matches pattern "name"' in text

    assert main(["--config", str(path), "check", "--url", url]) == EXIT_OK
    assert "drift: none. compatibility: ok. safety pattern: matched." in capsys.readouterr().out


def test_init_refuses_to_overwrite_a_reviewed_config_without_force(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text("# reviewed by hand\n", encoding="utf-8")
    assert main(["init", "--url", url, "-o", str(config)]) == EXIT_CONFIG
    assert "pass --force" in capsys.readouterr().err
    assert config.read_text(encoding="utf-8") == "# reviewed by hand\n"

    assert main(["init", "--url", url, "-o", str(config), "--force"]) == EXIT_OK
    assert "[tables.users.columns]" in config.read_text(encoding="utf-8")


def test_missing_pii_column_fails_check_with_the_pii_severity(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text(drop_entry(config.read_text(encoding="utf-8"), "email"), encoding="utf-8")
    assert main(["--config", str(config), "check", "--url", url]) == EXIT_FINDINGS
    captured = capsys.readouterr()
    assert "finding[pii-drift]" in captured.out
    assert "users.email" in captured.out
    # The final stderr line is the one error line; the logfmt event precedes it.
    assert captured.err.splitlines()[-1].startswith("error[E_DRIFT]: ")


def test_missing_non_pii_column_fails_check_with_the_plain_drift_message(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text(drop_entry(config.read_text(encoding="utf-8"), "age"), encoding="utf-8")
    assert main(["--config", str(config), "check", "--url", url]) == EXIT_FINDINGS
    captured = capsys.readouterr()
    assert "finding[drift]" in captured.out
    assert "users.age" in captured.out
    assert "add a strategy or explicit keep" in captured.out


def test_stale_config_entry_fails_check(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = config.read_text(encoding="utf-8").replace(
        "[tables.users.columns]", '[tables.users.columns]\nlegacy_id = "keep"'
    )
    config.write_text(text, encoding="utf-8")
    assert main(["--config", str(config), "check", "--url", url]) == EXIT_FINDINGS
    assert "users.legacy_id" in capsys.readouterr().out


def test_database_outside_the_safety_pattern_is_refused_with_exit_four(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = config.read_text(encoding="utf-8").replace(
        'database_name_pattern = "_(staging|masked)$"', 'database_name_pattern = "_prod$"'
    )
    config.write_text(text, encoding="utf-8")
    assert main(["--config", str(config), "check", "--url", url]) == EXIT_SAFETY
    assert "does not match safety.database_name_pattern" in capsys.readouterr().err


def test_check_output_never_contains_a_seeded_value(
    url: str, config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text(drop_entry(config.read_text(encoding="utf-8"), "email"), encoding="utf-8")
    main(["--config", str(config), "check", "--url", url])
    captured = capsys.readouterr()
    for sentinel in SENTINELS:
        assert sentinel not in captured.out
        assert sentinel not in captured.err


@pytest.mark.parametrize(
    ("table", "column", "rule", "expected"),
    [
        ("users", "full_name", "null", "NOT NULL"),
        ("users", "age", {"strategy": "redact", "value": "X"}, "generates text"),
        ("users", "short_code", "fake_address", "may generate up to"),
        ("users", "email", "fake_email", "unique index ix_users_email"),
        ("users", "id", "fake_name", 'primary key column must use "keep"'),
        ("orders", "user_id", "hash", 'foreign key column must use "keep"'),
    ],
)
def test_compatibility_findings_fire_against_the_live_schema(
    fixture_schema: Engine, config: Path, table: str, column: str, rule: Any, expected: str
) -> None:
    plan = tweaked(config.read_text(encoding="utf-8"), table, column, rule)
    messages = compat_messages(plan, fixture_schema, MODE_MASK)
    assert len(messages) == 1, messages
    assert expected in messages[0]
    assert f"{table}.{column}" in messages[0]


@pytest.fixture
def awkward_tables(fixture_schema: Engine) -> Iterator[Engine]:
    """A table with no primary key and a deliberate two-table foreign key cycle."""
    extra = sa.MetaData()
    sa.Table("notes", extra, sa.Column("body", sa.String(100), nullable=False))
    sa.Table(
        "node_a",
        extra,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("b_id", sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(["b_id"], ["node_b.id"], name="fk_node_a_b", use_alter=True),
    )
    sa.Table(
        "node_b",
        extra,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("a_id", sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(["a_id"], ["node_a.id"], name="fk_node_b_a", use_alter=True),
    )
    extra.create_all(fixture_schema)
    try:
        yield fixture_schema
    finally:
        extra.drop_all(fixture_schema)


def test_masked_table_without_a_primary_key_is_reported(
    awkward_tables: Engine, url: str, tmp_path: Path
) -> None:
    path = tmp_path / "awkward.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    plan = tweaked(path.read_text(encoding="utf-8"), "notes", "body", "fake_name")
    messages = compat_messages(plan, awkward_tables, MODE_MASK)
    assert any("no primary key" in message for message in messages)


def test_foreign_key_cycle_is_reported_for_pump(
    awkward_tables: Engine, url: str, tmp_path: Path
) -> None:
    path = tmp_path / "awkward.toml"
    assert main(["init", "--url", url, "-o", str(path)]) == EXIT_OK
    plan = build_plan(tomllib.loads(path.read_text(encoding="utf-8")), Path("dbmask.toml"))
    messages = compat_messages(plan, awkward_tables, MODE_PUMP)
    assert any("foreign key cycle" in message for message in messages)
