"""The fixture bed itself: both dialects reachable, schema created, rows seeded."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from tests.conftest import ORDER_ITEM_ROWS, USER_ROWS

pytestmark = pytest.mark.integration


def test_fixture_schema_is_created_and_seeded(fixture_schema: Engine) -> None:
    with fixture_schema.connect() as connection:
        users = connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
        items = connection.execute(sa.text("SELECT count(*) FROM order_items")).scalar_one()
    assert users == len(USER_ROWS)
    assert items == len(ORDER_ITEM_ROWS)


def test_fixture_database_name_ends_in_staging(fixture_schema: Engine) -> None:
    assert (fixture_schema.url.database or "").endswith("_staging")
