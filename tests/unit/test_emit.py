"""Starter-config proposals and the emitted TOML, which must round-trip through tomllib."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dbmask.config import build_plan
from dbmask.drift import find_findings
from dbmask.emit import DEFAULT_SAFETY_PATTERN, propose, render_config, write_config
from dbmask.errors import DbmaskError, ErrorCode
from dbmask.introspect import ForeignKeyInfo, TableInfo, UniqueIndexInfo
from tests.conftest import make_column, make_schema, make_table


def users_table() -> TableInfo:
    return make_table(
        "users",
        [
            make_column(
                "users", "id", family="integer", type_name="INTEGER", length=None, nullable=False
            ),
            make_column("users", "email", nullable=False),
            make_column("users", "full_name", nullable=False),
            make_column("users", "phone", type_name="VARCHAR(32)", length=32),
            make_column("users", "date_of_birth", family="date", type_name="DATE", length=None),
            make_column("users", "postal_code", type_name="VARCHAR(20)", length=20),
            make_column("users", "internal_note", type_name="TEXT", length=None),
        ],
        primary_key=("id",),
        unique_indexes=(UniqueIndexInfo("ix_users_email", ("email",)),),
    )


def orders_table() -> TableInfo:
    return make_table(
        "orders",
        [
            make_column(
                "orders", "id", family="integer", type_name="INTEGER", length=None, nullable=False
            ),
            make_column(
                "orders",
                "user_id",
                family="integer",
                type_name="INTEGER",
                length=None,
                nullable=False,
            ),
            make_column("orders", "customer_email", nullable=False),
        ],
        primary_key=("id",),
        foreign_keys=(ForeignKeyInfo("fk_orders_user", "orders", ("user_id",), "users", ("id",)),),
    )


def rendered_plan(text: str) -> object:
    return build_plan(tomllib.loads(text), Path("dbmask.toml"))


def test_generated_config_parses_and_passes_check() -> None:
    schema = make_schema(users_table(), orders_table())
    plan = rendered_plan(render_config(schema))
    assert find_findings(plan, schema) == []  # type: ignore[arg-type]


def test_every_column_is_covered_so_silence_is_impossible() -> None:
    schema = make_schema(users_table(), orders_table())
    plan = rendered_plan(render_config(schema))
    for table in schema.tables.values():
        assert set(plan.tables[table.name]) == set(table.columns)  # type: ignore[attr-defined]


def test_pii_columns_carry_the_pattern_comment() -> None:
    text = render_config(make_schema(users_table()))
    assert '# pii: name matches pattern "email"' in text
    assert '# pii: name matches pattern "phone"' in text


def test_unique_index_makes_the_proposal_unique() -> None:
    proposal = propose(users_table().columns["email"], users_table())
    assert proposal.rule.strategy == "fake_email"
    assert proposal.rule.unique is True


def test_key_columns_stay_keep_even_when_pii_named() -> None:
    orders = orders_table()
    # A foreign key that happens to carry a PII-flagged name still has to stay keep:
    # masking a key would break the joins the whole tool exists to preserve.
    orders.columns["user_id"] = make_column(
        "orders", "user_id", family="text", nullable=False, pii="email"
    )
    proposal = propose(orders.columns["user_id"], orders)
    assert proposal.rule.strategy == "keep"
    assert any("key column" in comment for comment in proposal.comments)


def test_nullable_non_text_pii_column_is_nulled() -> None:
    users = users_table()
    assert propose(users.columns["date_of_birth"], users).rule.strategy == "null"


def test_not_null_non_text_pii_column_is_kept_with_a_todo() -> None:
    users = users_table()
    users.columns["date_of_birth"] = make_column(
        "users", "date_of_birth", family="date", type_name="DATE", length=None, nullable=False
    )
    proposal = propose(users.columns["date_of_birth"], users)
    assert proposal.rule.strategy == "keep"
    assert any("TODO review" in comment for comment in proposal.comments)


def test_a_column_too_short_for_the_proposal_falls_back_to_redact() -> None:
    users = users_table()
    proposal = propose(users.columns["postal_code"], users)
    assert proposal.rule.strategy == "redact"
    assert proposal.rule.value == "MASKED"
    assert any("does not fit VARCHAR(20)" in comment for comment in proposal.comments)


def test_non_pii_columns_are_kept_without_comments() -> None:
    users = users_table()
    proposal = propose(users.columns["internal_note"], users)
    assert proposal.rule.strategy == "keep"
    assert proposal.comments == ()


def test_safety_pattern_is_anchored_when_the_default_would_not_match() -> None:
    default = render_config(make_schema(users_table(), database="app_staging"))
    assert f'database_name_pattern = "{DEFAULT_SAFETY_PATTERN}"' in default
    anchored = rendered_plan(render_config(make_schema(users_table(), database="copy1")))
    assert anchored.database_name_pattern == "^copy1$"  # type: ignore[attr-defined]


def test_column_names_that_are_not_bare_keys_are_quoted() -> None:
    table = make_table("t", [make_column("t", "odd name", nullable=False)], primary_key=())
    plan = rendered_plan(render_config(make_schema(table)))
    assert "odd name" in plan.tables["t"]  # type: ignore[attr-defined]


def test_write_config_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / "dbmask.toml"
    write_config(path, "x", force=False)
    with pytest.raises(DbmaskError) as caught:
        write_config(path, "y", force=False)
    assert caught.value.code is ErrorCode.E_CONFIG
    assert path.read_text(encoding="utf-8") == "x"
    write_config(path, "y", force=True)
    assert path.read_text(encoding="utf-8") == "y"
