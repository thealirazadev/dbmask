"""PII name patterns, type families, and URL parsing. No database is touched here."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from dbmask.errors import DbmaskError, ErrorCode
from dbmask.introspect import (
    OWNED_TABLES,
    PII_PATTERNS,
    TEXT_FAMILY,
    UniqueIndexInfo,
    connect,
    database_name,
    match_pii,
    type_family,
)

PII_CASES = [
    ("email", "email"),
    ("customer_email", "email"),
    ("e_mail", "email"),
    ("phone", "phone"),
    ("mobile_number", "phone"),
    ("password_hash", "password"),
    ("api_token", "secret"),
    ("session_id", "secret"),
    ("ssn", "ssn"),
    ("passport_no", "government_id"),
    ("card_number", "payment"),
    ("date_of_birth", "dob"),
    ("birthday", "dob"),
    ("street", "address"),
    ("city", "address"),
    ("postal_code", "address"),
    ("full_name", "name"),
    ("surname", "name"),
    # Deliberate false positive: the tool cannot tell a person's name from a file's, so
    # it flags both. The cost is one explicit keep in review (docs/rules.md).
    ("file_name", "name"),
]

NOT_PII = ["id", "user_id", "order_id", "line_no", "sku", "age", "total_cents", "created_at"]


@pytest.mark.parametrize(("column", "label"), PII_CASES)
def test_pii_pattern_matches_with_its_label(column: str, label: str) -> None:
    matched = match_pii(column)
    assert matched is not None
    assert matched.label == label


@pytest.mark.parametrize("column", NOT_PII)
def test_non_pii_column_names_do_not_match(column: str) -> None:
    assert match_pii(column) is None


def test_every_pattern_proposes_a_known_strategy() -> None:
    from dbmask.config import STRATEGY_NAMES

    assert {pattern.strategy for pattern in PII_PATTERNS} <= set(STRATEGY_NAMES)


def test_pattern_labels_are_unique() -> None:
    labels = [pattern.label for pattern in PII_PATTERNS]
    assert len(labels) == len(set(labels))


@pytest.mark.parametrize(
    ("column_type", "family"),
    [
        (sa.String(20), TEXT_FAMILY),
        (sa.Text(), TEXT_FAMILY),
        (sa.Enum("a", "b", name="e"), TEXT_FAMILY),
        (sa.Integer(), "integer"),
        (sa.BigInteger(), "integer"),
        (sa.Boolean(), "boolean"),
        (sa.Numeric(10, 2), "numeric"),
        (sa.Float(), "numeric"),
        (sa.DateTime(), "datetime"),
        (sa.Date(), "date"),
        (sa.Time(), "time"),
        (sa.LargeBinary(), "binary"),
        (sa.JSON(), "other"),
    ],
)
def test_type_family_classification(column_type: sa.types.TypeEngine[object], family: str) -> None:
    assert type_family(column_type) == family


def test_database_name_reads_the_url_without_connecting() -> None:
    assert database_name("postgresql+psycopg://u:p@localhost:5432/app_staging") == "app_staging"
    assert database_name("mysql+pymysql://u:p@localhost:3306/app_staging") == "app_staging"


def test_database_name_rejects_a_url_without_a_database() -> None:
    with pytest.raises(DbmaskError) as caught:
        database_name("postgresql+psycopg://u:p@localhost:5432/")
    assert caught.value.code is ErrorCode.E_CONFIG


def test_database_name_rejects_an_unparseable_url() -> None:
    with pytest.raises(DbmaskError) as caught:
        database_name("not a url at all")
    assert caught.value.code is ErrorCode.E_CONFIG


def test_database_name_rejects_a_non_numeric_port_as_a_config_error() -> None:
    # make_url raises a bare ValueError here, which used to escape as E_INTERNAL, exit 1.
    with pytest.raises(DbmaskError) as caught:
        database_name("postgresql+psycopg://app:s3cr3t@localhost:notaport/app_staging")
    assert caught.value.code is ErrorCode.E_CONFIG
    assert "s3cr3t" not in caught.value.message


def test_connect_maps_a_driver_failure_that_is_not_a_sqlalchemy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyMySQL raises AttributeError on an unknown charset; a missing driver raises
    ModuleNotFoundError. Neither is a SQLAlchemyError, and both must still be E_CONNECT."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("'NoneType' object has no attribute 'encoding'")

    monkeypatch.setattr(sa, "create_engine", boom)
    with pytest.raises(DbmaskError) as caught:
        connect("mysql+pymysql://app:s3cr3t@localhost:3306/app_staging?charset=bogus")
    assert caught.value.code is ErrorCode.E_CONNECT
    assert "cannot connect to mysql+pymysql://app:***@localhost:3306/app_staging" in (
        caught.value.message
    )
    assert "s3cr3t" not in caught.value.message


def test_unique_index_covers_the_columns_it_names() -> None:
    index = UniqueIndexInfo("uq_users_tenant_email", ("tenant_id", "email"))
    assert index.covers("email")
    assert not index.covers("phone")


def test_expression_unique_index_covers_the_column_inside_its_expression() -> None:
    # UNIQUE (lower(email)) reports no column name at all, so a masked email column would
    # otherwise never be told it needs unique = true, and the run would collide mid-write.
    index = UniqueIndexInfo("ix_users_lower_email", (), ("lower(email::text)",))
    assert index.covers("email")


def test_expression_match_is_whole_word_so_a_substring_is_not_coverage() -> None:
    index = UniqueIndexInfo("ix_users_lower_nickname", (), ("lower(nickname)",))
    assert not index.covers("name")


def test_dbmask_owned_tables_are_named_so_they_never_read_as_drift() -> None:
    assert set(OWNED_TABLES) == {"dbmask_audit", "dbmask_progress"}
