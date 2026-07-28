"""Integration fixtures: the dockerized databases and the seeded fixture schema.

Every schema-touching feature is tested against both PostgreSQL and MySQL, so the
`dialect` fixture is parametrized and each integration test runs twice. When a database
is unreachable the tests skip with a message naming the compose file, so a checkout
without docker still runs the whole unit suite.

The seeded values include sentinel strings. Nothing in dbmask's output may ever contain
them; the sweep that asserts this lands with the verification suite in phase 5.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from dbmask.config import MaskPlan, build_plan
from dbmask.introspect import (
    ColumnInfo,
    ForeignKeyInfo,
    SchemaModel,
    TableInfo,
    UniqueIndexInfo,
    match_pii,
)

DIALECTS: Final = ("postgresql", "mysql")

URL_ENV: Final = {
    "postgresql": "DBMASK_TEST_PG_URL",
    "mysql": "DBMASK_TEST_MYSQL_URL",
}
DEFAULT_URL: Final = {
    "postgresql": "postgresql+psycopg://dbmask:dbmask@localhost:54329/dbmask_staging",
    "mysql": "mysql+pymysql://dbmask:dbmask@localhost:33069/dbmask_staging",
}

# Real-looking values seeded into the fixture. They must never appear in any dbmask
# output; the fixed suffix makes a grep over captured output unambiguous.
SENTINEL_EMAIL: Final = "sentinel-real-value-x7@example.com"
SENTINEL_NAME: Final = "Sentinel Realvalue X7"
SENTINEL_PHONE: Final = "+1-555-0177-x7"
SENTINEL_STREET: Final = "77 Sentinel Real Street"
SENTINEL_NOTE: Final = "sentinel-real-note-x7"
SENTINELS: Final = (
    SENTINEL_EMAIL,
    SENTINEL_NAME,
    SENTINEL_PHONE,
    SENTINEL_STREET,
    SENTINEL_NOTE,
)

TEST_SECRET: Final = "integration-secret-of-at-least-32-characters"

metadata = sa.MetaData()

# users carries the awkward cases on purpose: a unique index on a masked column, a
# deliberate PII false positive (file_name), a column too short for a generated value
# (short_code), and a non-text column with a PII-ish name (age).
users_table = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("email", sa.String(120), nullable=False),
    sa.Column("full_name", sa.String(120), nullable=False),
    sa.Column("phone", sa.String(32), nullable=True),
    sa.Column("street", sa.String(160), nullable=False),
    sa.Column("password_hash", sa.String(255), nullable=False),
    sa.Column("api_token", sa.String(64), nullable=False),
    sa.Column("internal_note", sa.Text, nullable=True),
    sa.Column("short_code", sa.String(20), nullable=False),
    sa.Column("age", sa.Integer, nullable=False),
    sa.Column("file_name", sa.String(120), nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Index("ix_users_email", "email", unique=True),
)

addresses_table = sa.Table(
    "addresses",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("user_id", sa.Integer, nullable=False),
    sa.Column("street", sa.String(160), nullable=False),
    sa.Column("city", sa.String(80), nullable=False),
    sa.Column("postal_code", sa.String(20), nullable=False),
    sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_addresses_user"),
)

orders_table = sa.Table(
    "orders",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("user_id", sa.Integer, nullable=False),
    sa.Column("customer_email", sa.String(120), nullable=False),
    sa.Column("total_cents", sa.Integer, nullable=False),
    sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_orders_user"),
)

# Composite primary key, so the batched row-value comparison has something to exercise.
order_items_table = sa.Table(
    "order_items",
    metadata,
    sa.Column("order_id", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("line_no", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("sku", sa.String(40), nullable=False),
    sa.Column("note", sa.Text, nullable=True),
    sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_items_order"),
)

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC).replace(tzinfo=None)

USER_ROWS: Final = [
    {
        "id": 1,
        "email": SENTINEL_EMAIL,
        "full_name": SENTINEL_NAME,
        "phone": SENTINEL_PHONE,
        "street": SENTINEL_STREET,
        "password_hash": "argon2-real-hash-1",
        "api_token": "token-real-1",
        "internal_note": SENTINEL_NOTE,
        "short_code": "SC1",
        "age": 41,
        "file_name": "invoice-2026-01.pdf",
        "created_at": _CREATED,
    },
    {
        "id": 2,
        "email": "second.person@example.com",
        "full_name": "Second Person",
        "phone": None,
        "street": "12 Second Street",
        "password_hash": "argon2-real-hash-2",
        "api_token": "token-real-2",
        "internal_note": None,
        "short_code": "SC2",
        "age": 29,
        "file_name": "receipt.pdf",
        "created_at": _CREATED,
    },
    {
        "id": 3,
        "email": "third.person@example.com",
        "full_name": "Third Person",
        "phone": "+1-555-0103",
        "street": "13 Third Street",
        "password_hash": "argon2-real-hash-3",
        "api_token": "token-real-3",
        "internal_note": "note three",
        "short_code": "SC3",
        "age": 55,
        "file_name": "export.csv",
        "created_at": _CREATED,
    },
]

ADDRESS_ROWS: Final = [
    {
        "id": 1,
        "user_id": 1,
        "street": SENTINEL_STREET,
        "city": "Sentinelton",
        "postal_code": "77007",
    },
    {
        "id": 2,
        "user_id": 2,
        "street": "12 Second Street",
        "city": "Secondville",
        "postal_code": "20002",
    },
    {
        "id": 3,
        "user_id": 3,
        "street": "13 Third Street",
        "city": "Thirdville",
        "postal_code": "30003",
    },
]

# customer_email repeats users.email so the logical join pair has something to preserve.
ORDER_ROWS: Final = [
    {"id": 1, "user_id": 1, "customer_email": SENTINEL_EMAIL, "total_cents": 1999},
    {"id": 2, "user_id": 1, "customer_email": SENTINEL_EMAIL, "total_cents": 2999},
    {"id": 3, "user_id": 2, "customer_email": "second.person@example.com", "total_cents": 500},
    {"id": 4, "user_id": 3, "customer_email": "third.person@example.com", "total_cents": 12500},
]

ORDER_ITEM_ROWS: Final = [
    {"order_id": 1, "line_no": 1, "sku": "SKU-1", "note": SENTINEL_NOTE},
    {"order_id": 1, "line_no": 2, "sku": "SKU-2", "note": None},
    {"order_id": 2, "line_no": 1, "sku": "SKU-3", "note": "gift"},
    {"order_id": 3, "line_no": 1, "sku": "SKU-4", "note": None},
    {"order_id": 4, "line_no": 1, "sku": "SKU-5", "note": None},
]


@pytest.fixture(scope="session", params=DIALECTS)
def dialect(request: pytest.FixtureRequest) -> str:
    """Each integration test runs once per dialect; a stand-in database is never used."""
    value: str = request.param
    return value


@pytest.fixture(scope="session")
def engine(dialect: str) -> Iterator[Engine]:
    url = os.environ.get(URL_ENV[dialect], DEFAULT_URL[dialect])
    created = sa.create_engine(url, pool_pre_ping=True, future=True)
    try:
        with created.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except SQLAlchemyError as exc:
        created.dispose()
        pytest.skip(
            f"{dialect} test database unreachable ({type(exc).__name__}); start it with "
            f"`docker compose -f docker-compose.test.yml up -d --wait` or set {URL_ENV[dialect]}"
        )
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def fixture_schema(engine: Engine) -> Iterator[Engine]:
    """A freshly created and seeded fixture schema, dropped again after the test."""
    _reset(engine)
    try:
        yield engine
    finally:
        _reset(engine, seed=False)


def make_column(
    table: str,
    name: str,
    *,
    family: str = "text",
    type_name: str = "VARCHAR(120)",
    length: int | None = 120,
    nullable: bool = True,
    pii: str | None = "auto",
) -> ColumnInfo:
    """A ColumnInfo for the unit tests; the PII label defaults to the real pattern match."""
    if pii == "auto":
        matched = match_pii(name)
        pii = matched.label if matched is not None else None
    return ColumnInfo(
        table=table,
        name=name,
        family=family,
        type_name=type_name,
        length=length,
        nullable=nullable,
        pii=pii,
    )


def make_table(
    name: str,
    columns: Sequence[ColumnInfo],
    *,
    primary_key: tuple[str, ...] = (),
    foreign_keys: tuple[ForeignKeyInfo, ...] = (),
    unique_indexes: tuple[UniqueIndexInfo, ...] = (),
) -> TableInfo:
    return TableInfo(
        name=name,
        columns={column.name: column for column in columns},
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_indexes=unique_indexes,
    )


def make_schema(
    *tables: TableInfo, database: str = "app_staging", dialect: str = "postgresql"
) -> SchemaModel:
    return SchemaModel(
        database=database,
        dialect=dialect,
        tables={table.name: table for table in tables},
    )


def make_plan(
    tables: dict[str, dict[str, Any]],
    *,
    pattern: str | None = "_staging$",
    joins: list[dict[str, str]] | None = None,
) -> MaskPlan:
    data: dict[str, Any] = {
        "tables": {name: {"columns": columns} for name, columns in tables.items()}
    }
    if pattern is not None:
        data["safety"] = {"database_name_pattern": pattern}
    if joins is not None:
        data["verify"] = {"joins": joins}
    return build_plan(data, Path("dbmask.toml"))


def _reset(engine: Engine, seed: bool = True) -> None:
    with engine.begin() as connection:
        metadata.drop_all(connection, checkfirst=True)
        if not seed:
            return
        metadata.create_all(connection)
        connection.execute(sa.insert(users_table), USER_ROWS)
        connection.execute(sa.insert(addresses_table), ADDRESS_ROWS)
        connection.execute(sa.insert(orders_table), ORDER_ROWS)
        connection.execute(sa.insert(order_items_table), ORDER_ITEM_ROWS)
