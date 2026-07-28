"""The error-to-exit-code contract and the logfmt writer's value hygiene."""

from __future__ import annotations

import pytest

from dbmask.errors import EXIT_CODES, DbmaskError, ErrorCode
from dbmask.logging import log_event, scrub_password, scrub_url

EXPECTED_EXIT_CODES = {
    ErrorCode.E_CONFIG: 2,
    ErrorCode.E_CONNECT: 1,
    ErrorCode.E_DRIFT: 3,
    ErrorCode.E_SAFETY: 4,
    ErrorCode.E_MASK: 1,
    ErrorCode.E_VERIFY: 3,
    ErrorCode.E_INTERNAL: 1,
}


def test_every_code_maps_to_its_documented_exit_code() -> None:
    assert EXIT_CODES == EXPECTED_EXIT_CODES
    for code, exit_code in EXPECTED_EXIT_CODES.items():
        assert DbmaskError(code, "boom").exit_code == exit_code


def test_error_formats_as_one_line() -> None:
    error = DbmaskError(ErrorCode.E_DRIFT, "4 findings (1 pii)")
    assert error.format() == "error[E_DRIFT]: 4 findings (1 pii)"
    assert "\n" not in error.format()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://app:s3cr3t@db.internal:5432/app_staging",
            "postgresql+psycopg://app:***@db.internal:5432/app_staging",
        ),
        (
            "mysql+pymysql://app:s3cr3t@localhost/app_staging",
            "mysql+pymysql://app:***@localhost/app_staging",
        ),
        ("postgresql+psycopg://localhost:5432/app_staging", None),
    ],
)
def test_scrub_url_removes_the_password(url: str, expected: str | None) -> None:
    scrubbed = scrub_url(url)
    assert scrubbed == (expected if expected is not None else url)
    assert "s3cr3t" not in scrubbed


def test_scrub_url_hides_everything_when_the_url_is_unparseable() -> None:
    assert scrub_url("postgresql://app:s3cr3t@[::1:5432/app") == "***"


def test_scrub_password_replaces_the_plain_spelling() -> None:
    url = "postgresql+psycopg://app:s3cr3t@db.internal:5432/app_staging"
    assert scrub_password('dsn "user=app password=s3cr3t"', url) == 'dsn "user=app password=***"'


def test_scrub_password_replaces_the_decoded_spelling_the_driver_receives() -> None:
    # A password with URL-reserved characters is percent-encoded in the URL, but the
    # driver is handed (and echoes) the decoded form.
    url = "mysql+pymysql://app:s3%40cr3t%2Fx@db.internal:3306/app_staging"
    scrubbed = scrub_password('Access denied: dsn "password=s3@cr3t/x"', url)
    assert "s3@cr3t/x" not in scrubbed
    assert scrubbed == 'Access denied: dsn "password=***"'


def test_scrub_password_leaves_text_alone_when_the_url_carries_no_password() -> None:
    assert scrub_password("nothing to hide", "postgresql+psycopg://localhost/app") == (
        "nothing to hide"
    )


def test_log_event_writes_one_logfmt_line(capsys: pytest.CaptureFixture[str]) -> None:
    log_event("mask.batch_committed", table="users", batch="1/37", rows=5000, elapsed_ms=210.5)
    err = capsys.readouterr().err
    assert err == "mask.batch_committed table=users batch=1/37 rows=5000 elapsed_ms=210.5\n"


def test_log_event_quotes_values_that_need_it(capsys: pytest.CaptureFixture[str]) -> None:
    log_event("run.failed", reason="two words", ok=False)
    assert capsys.readouterr().err == 'run.failed reason="two words" ok=false\n'


def test_log_event_rejects_non_scalar_fields() -> None:
    with pytest.raises(TypeError):
        log_event("run.started", rows=["real value"])
