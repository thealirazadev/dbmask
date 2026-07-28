"""Drift both ways, compatibility findings, ordering, and the safety-pattern refusal."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from dbmask.drift import (
    MODE_ALL,
    MODE_MASK,
    MODE_PUMP,
    SEVERITY_COMPAT,
    SEVERITY_DRIFT,
    SEVERITY_PII,
    Finding,
    check_database_name,
    find_findings,
    format_findings,
    pii_count,
)
from dbmask.errors import EXIT_SAFETY, DbmaskError, ErrorCode
from dbmask.introspect import ForeignKeyInfo, TableInfo, UniqueIndexInfo
from tests.conftest import make_column, make_plan, make_schema, make_table

UNIQUE_EMAIL = UniqueIndexInfo("ix_users_email", ("email",))


def users_table(primary_key: tuple[str, ...] = ("id",)) -> TableInfo:
    return make_table(
        "users",
        [
            make_column(
                "users", "id", family="integer", type_name="INTEGER", length=None, nullable=False
            ),
            make_column("users", "email", nullable=False),
            make_column("users", "full_name", nullable=False),
        ],
        primary_key=primary_key,
        unique_indexes=(UNIQUE_EMAIL,),
    )


CLEAN_CONFIG = {
    "users": {
        "id": "keep",
        "email": {"strategy": "fake_email", "unique": True},
        "full_name": "fake_name",
    }
}


def subjects(findings: Sequence[Finding], severity: str) -> list[str]:
    return [finding.subject for finding in findings if finding.severity == severity]


def only(findings: Sequence[Finding]) -> Finding:
    assert len(findings) == 1, [f.format() for f in findings]
    return findings[0]


def test_matching_config_and_schema_produce_no_findings() -> None:
    assert find_findings(make_plan(CLEAN_CONFIG), make_schema(users_table())) == []


def test_unconfigured_pii_column_is_a_pii_finding_naming_the_pattern() -> None:
    without_email = {k: v for k, v in CLEAN_CONFIG["users"].items() if k != "email"}
    config = {"users": without_email}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.severity == SEVERITY_PII
    assert finding.subject == "users.email"
    assert 'name matches pattern "email"' in finding.message


def test_unconfigured_non_pii_column_is_plain_drift() -> None:
    table = users_table()
    table.columns["gift_note"] = make_column("users", "gift_note")
    finding = only(find_findings(make_plan(CLEAN_CONFIG), make_schema(table)))
    assert finding.severity == SEVERITY_DRIFT
    assert finding.subject == "users.gift_note"
    assert "add a strategy or explicit keep" in finding.message


def test_configured_column_missing_from_schema_is_stale_config() -> None:
    config = {"users": {**CLEAN_CONFIG["users"], "legacy_id": "keep"}}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.severity == SEVERITY_DRIFT
    assert finding.subject == "users.legacy_id"
    assert "configured but not in schema" in finding.message


def test_configured_table_missing_from_schema_is_stale_config() -> None:
    config = {**CLEAN_CONFIG, "legacy": {"id": "keep"}}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.subject == "legacy"


def test_unconfigured_table_reports_the_table_and_still_names_its_pii_columns() -> None:
    schema = make_schema(users_table())
    findings = find_findings(make_plan({"other": {"id": "keep"}}), schema)
    assert subjects(findings, SEVERITY_PII) == ["users.email", "users.full_name"]
    assert "users" in subjects(findings, SEVERITY_DRIFT)


def test_null_on_a_not_null_column_is_a_compatibility_finding() -> None:
    config = {"users": {**CLEAN_CONFIG["users"], "full_name": "null"}}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.severity == SEVERITY_COMPAT
    assert "NOT NULL" in finding.message


def test_null_on_a_nullable_column_is_accepted() -> None:
    table = users_table()
    table.columns["full_name"] = make_column("users", "full_name", nullable=True)
    config = {"users": {**CLEAN_CONFIG["users"], "full_name": "null"}}
    assert find_findings(make_plan(config), make_schema(table)) == []


def test_text_strategy_on_a_non_text_column_is_a_compatibility_finding() -> None:
    table = users_table()
    table.columns["age"] = make_column(
        "users", "age", family="integer", type_name="INTEGER", length=None, nullable=False
    )
    config = {"users": {**CLEAN_CONFIG["users"], "age": {"strategy": "redact", "value": "X"}}}
    finding = only(find_findings(make_plan(config), make_schema(table)))
    assert finding.subject == "users.age"
    assert "generates text but the column type is INTEGER" in finding.message


def test_generated_value_longer_than_the_column_is_a_compatibility_finding() -> None:
    table = users_table()
    table.columns["short_code"] = make_column(
        "users", "short_code", type_name="VARCHAR(20)", length=20, nullable=False
    )
    config = {"users": {**CLEAN_CONFIG["users"], "short_code": "fake_address"}}
    finding = only(find_findings(make_plan(config), make_schema(table)))
    assert finding.subject == "users.short_code"
    assert "may generate up to 128 characters but the column holds 20" in finding.message


def test_masked_unique_column_without_unique_true_is_named_with_its_index() -> None:
    config = {"users": {**CLEAN_CONFIG["users"], "email": "fake_email"}}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.subject == "users.email"
    assert "unique index ix_users_email" in finding.message


def test_masked_column_under_an_expression_unique_index_still_needs_unique_true() -> None:
    table = make_table(
        "users",
        list(users_table().columns.values()),
        primary_key=("id",),
        unique_indexes=(UniqueIndexInfo("ix_users_lower_email", (), ("lower(email::text)",)),),
    )
    config = {"users": {**CLEAN_CONFIG["users"], "email": "fake_email"}}
    finding = only(find_findings(make_plan(config), make_schema(table)))
    assert finding.subject == "users.email"
    assert "unique index ix_users_lower_email" in finding.message


def test_masking_a_primary_key_column_is_refused() -> None:
    config = {"users": {**CLEAN_CONFIG["users"], "id": "fake_name"}}
    finding = only(find_findings(make_plan(config), make_schema(users_table())))
    assert finding.subject == "users.id"
    assert 'primary key column must use "keep"' in finding.message


def test_masking_a_foreign_key_column_is_refused() -> None:
    table = make_table(
        "orders",
        [
            make_column("orders", "id", family="integer", length=None, nullable=False),
            make_column("orders", "user_id", family="integer", length=None, nullable=False),
        ],
        primary_key=("id",),
        foreign_keys=(ForeignKeyInfo("fk_orders_user", "orders", ("user_id",), "users", ("id",)),),
    )
    config = {"orders": {"id": "keep", "user_id": "hash"}}
    finding = only(find_findings(make_plan(config), make_schema(table)))
    assert finding.subject == "orders.user_id"
    assert 'foreign key column must use "keep"' in finding.message


def test_masked_table_without_a_primary_key_is_a_mask_mode_finding() -> None:
    schema = make_schema(users_table(primary_key=()))
    finding = only(find_findings(make_plan(CLEAN_CONFIG), schema, MODE_MASK))
    assert finding.subject == "users"
    assert "no primary key" in finding.message
    assert find_findings(make_plan(CLEAN_CONFIG), schema, MODE_PUMP) == []


def test_foreign_key_cycle_is_a_pump_mode_finding() -> None:
    parent = make_table(
        "parent",
        [make_column("parent", "id", family="integer", length=None, nullable=False)],
        primary_key=("id",),
        foreign_keys=(ForeignKeyInfo("fk_parent_child", "parent", ("id",), "child", ("id",)),),
    )
    child = make_table(
        "child",
        [make_column("child", "id", family="integer", length=None, nullable=False)],
        primary_key=("id",),
        foreign_keys=(ForeignKeyInfo("fk_child_parent", "child", ("id",), "parent", ("id",)),),
    )
    config = {"parent": {"id": "keep"}, "child": {"id": "keep"}}
    schema = make_schema(parent, child)
    finding = only(find_findings(make_plan(config), schema, MODE_PUMP))
    assert "foreign key cycle" in finding.message
    assert find_findings(make_plan(config), schema, MODE_MASK) == []


def test_template_may_only_read_primary_key_or_keep_columns() -> None:
    table = users_table()
    table.columns["username"] = make_column("users", "username", nullable=False)
    masked = {
        "users": {
            **CLEAN_CONFIG["users"],
            "username": {"strategy": "template", "template": "u_{full_name}"},
        }
    }
    finding = only(find_findings(make_plan(masked), make_schema(table)))
    assert finding.subject == "users.username"
    assert "not a primary key or keep column" in finding.message

    allowed = {
        "users": {
            **CLEAN_CONFIG["users"],
            "username": {"strategy": "template", "template": "u_{id}", "unique": True},
        }
    }
    assert find_findings(make_plan(allowed), make_schema(table)) == []


def test_findings_are_ordered_pii_then_drift_then_compat() -> None:
    table = users_table()
    table.columns["ssn"] = make_column("users", "ssn", nullable=False)
    table.columns["gift_note"] = make_column("users", "gift_note")
    config = {"users": {**CLEAN_CONFIG["users"], "email": "fake_email"}}
    findings = find_findings(make_plan(config), make_schema(table), MODE_ALL)
    assert [finding.severity for finding in findings] == [
        SEVERITY_PII,
        SEVERITY_DRIFT,
        SEVERITY_COMPAT,
    ]
    assert pii_count(findings) == 1


def test_format_findings_is_greppable_and_aligned() -> None:
    lines = format_findings(
        [Finding(SEVERITY_PII, "users.ssn", "explain"), Finding(SEVERITY_DRIFT, "a.b", "explain")]
    )
    assert lines[0].startswith("finding[pii-drift]")
    assert lines[1].startswith("finding[drift]")
    assert all(line.endswith("explain") for line in lines)
    assert len({line.index("explain") for line in lines}) == 1


def test_check_database_name_accepts_a_matching_name() -> None:
    check_database_name(make_plan(CLEAN_CONFIG), "app_staging")


def test_check_database_name_refuses_a_mismatch_with_exit_four() -> None:
    with pytest.raises(DbmaskError) as caught:
        check_database_name(make_plan(CLEAN_CONFIG), "app_prod")
    assert caught.value.code is ErrorCode.E_SAFETY
    assert caught.value.exit_code == EXIT_SAFETY
    assert 'database "app_prod" does not match' in caught.value.message


def test_check_database_name_requires_the_pattern_to_be_configured() -> None:
    with pytest.raises(DbmaskError) as caught:
        check_database_name(make_plan(CLEAN_CONFIG, pattern=None), "app_staging")
    assert caught.value.code is ErrorCode.E_CONFIG
