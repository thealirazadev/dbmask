"""CLI skeleton: command surface, secret handling, and the exit-code contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbmask.cli import MIN_SECRET_LENGTH, SECRET_ENV, load_secret, main
from dbmask.errors import EXIT_CONFIG, EXIT_OK, EXIT_RUNTIME, DbmaskError, ErrorCode

SECRET = "unit-test-secret-of-at-least-32-chars"

CONFIG = (
    '[safety]\ndatabase_name_pattern = "_staging$"\n\n'
    '[tables.users.columns]\nid = "keep"\nemail = { strategy = "fake_email", unique = true }\n'
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DBMASK_SECRET", "DBMASK_URL", "DBMASK_SOURCE_URL", "DBMASK_TARGET_URL"):
        monkeypatch.delenv(name, raising=False)


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "dbmask.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_help_lists_the_five_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for command in ("init", "check", "mask", "pump", "verify"):
        assert command in out


def test_version_exits_zero() -> None:
    assert main(["--version"]) == EXIT_OK


def test_mask_without_config_is_a_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(tmp_path / "absent.toml"), "mask"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert err.startswith("error[E_CONFIG]: ")
    assert err.count("\n") == 1


def test_missing_secret_is_refused_without_echoing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(write_config(tmp_path)), "mask"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert err == (
        f"error[E_CONFIG]: {SECRET_ENV} is not set; export a project secret of at least "
        f"{MIN_SECRET_LENGTH} characters\n"
    )


def test_short_secret_is_refused_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SECRET_ENV, "too-short")
    assert main(["--config", str(write_config(tmp_path)), "mask"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "too-short" not in err
    assert f"{MIN_SECRET_LENGTH} characters" in err


def test_missing_url_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)
    assert main(["--config", str(write_config(tmp_path)), "check"]) == EXIT_CONFIG
    assert "DBMASK_URL" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["nope"]) == EXIT_CONFIG
    assert capsys.readouterr().err.startswith("error[E_CONFIG]: ")


def test_database_commands_report_not_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv("DBMASK_URL", "postgresql+psycopg://u:p@localhost:5432/app_staging")
    assert main(["--config", str(write_config(tmp_path)), "mask"]) == EXIT_RUNTIME
    err = capsys.readouterr().err
    assert err.startswith("error[E_INTERNAL]: mask is not implemented yet")
    assert "u:p" not in err


def test_load_secret_accepts_a_long_enough_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, SECRET)
    assert load_secret() == SECRET


def test_load_secret_raises_config_error_when_absent() -> None:
    with pytest.raises(DbmaskError) as caught:
        load_secret()
    assert caught.value.code is ErrorCode.E_CONFIG
    assert caught.value.exit_code == EXIT_CONFIG
