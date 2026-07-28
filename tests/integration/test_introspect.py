"""Introspection fidelity against real PostgreSQL and MySQL.

Both dialects must produce the same SchemaModel for the same DDL: everything downstream
(drift, length budgets, batching) assumes the model is dialect-independent.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from dbmask.introspect import introspect

pytestmark = pytest.mark.integration

# Both dialects support a unique index over an expression; neither reports a column name
# for it, and PostgreSQL reports a None placeholder that must not become a column called
# "None". The index goes away with the fixture tables.
EXPRESSION_INDEX_DDL = {
    "postgresql": "CREATE UNIQUE INDEX ix_users_lower_email ON users (lower(email))",
    "mysql": "CREATE UNIQUE INDEX ix_users_lower_email ON users ((lower(email)))",
}


@pytest.fixture
def expression_unique_index(fixture_schema: Engine, dialect: str) -> Iterator[Engine]:
    with fixture_schema.begin() as connection:
        connection.execute(sa.text(EXPRESSION_INDEX_DDL[dialect]))
    yield fixture_schema


def test_all_fixture_tables_are_found(fixture_schema: Engine) -> None:
    schema = introspect(fixture_schema)
    assert sorted(schema.tables) == ["addresses", "order_items", "orders", "users"]
    assert schema.database == "dbmask_staging"
    assert len(schema.columns()) == 25


def test_column_types_lengths_and_nullability_survive_both_dialects(
    fixture_schema: Engine,
) -> None:
    users = introspect(fixture_schema).tables["users"]
    assert users.columns["email"].family == "text"
    assert users.columns["email"].length == 120
    assert users.columns["email"].nullable is False
    assert users.columns["phone"].nullable is True
    assert users.columns["internal_note"].length is None
    assert users.columns["age"].family == "integer"
    assert users.columns["created_at"].family == "datetime"


def test_primary_keys_including_a_composite_one(fixture_schema: Engine) -> None:
    schema = introspect(fixture_schema)
    assert schema.tables["users"].primary_key == ("id",)
    assert schema.tables["order_items"].primary_key == ("order_id", "line_no")


def test_unique_indexes_are_reported_and_the_primary_key_is_not(fixture_schema: Engine) -> None:
    users = introspect(fixture_schema).tables["users"]
    assert [index.name for index in users.unique_indexes] == ["ix_users_email"]
    assert users.unique_indexes_covering("email")[0].columns == ("email",)
    assert users.unique_indexes_covering("id") == ()


def test_an_expression_unique_index_never_invents_a_column(
    expression_unique_index: Engine,
) -> None:
    users = introspect(expression_unique_index).tables["users"]
    indexes = {index.name: index for index in users.unique_indexes}
    assert "ix_users_lower_email" in indexes
    for index in users.unique_indexes:
        assert set(index.columns) <= set(users.columns), index


def test_foreign_keys_carry_their_names_and_both_ends(fixture_schema: Engine) -> None:
    schema = introspect(fixture_schema)
    keys = {key.name: key for key in schema.tables["orders"].foreign_keys}
    assert keys["fk_orders_user"].columns == ("user_id",)
    assert keys["fk_orders_user"].referred_table == "users"
    assert keys["fk_orders_user"].referred_columns == ("id",)
    assert schema.tables["orders"].foreign_key_columns == frozenset({"user_id"})
    assert schema.tables["order_items"].foreign_key_columns == frozenset({"order_id"})


def test_pii_flags_are_applied_to_the_live_column_names(fixture_schema: Engine) -> None:
    schema = introspect(fixture_schema)
    flagged = {column.ref: column.pii for column in schema.pii_columns()}
    assert flagged["users.email"] == "email"
    assert flagged["users.phone"] == "phone"
    assert flagged["users.password_hash"] == "password"
    assert flagged["users.api_token"] == "secret"
    assert flagged["orders.customer_email"] == "email"
    assert flagged["users.file_name"] == "name"
    assert "users.age" not in flagged
    assert "order_items.sku" not in flagged
