"""Config shape, option validation, and config-hash canonicalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dbmask.config import build_plan, load_plan
from dbmask.errors import EXIT_CONFIG, DbmaskError, ErrorCode

VALID: dict[str, Any] = {
    "project": {"locale": "en_US"},
    "safety": {"database_name_pattern": "_(staging|masked)$"},
    "settings": {"batch_size": 100, "cache_size": 1000},
    "tables": {
        "users": {
            "columns": {
                "id": "keep",
                "email": {"strategy": "fake_email", "unique": True},
                "full_name": "fake_name",
                "password_hash": {"strategy": "redact", "value": "MASKED"},
                "internal_note": "null",
                "username": {"strategy": "template", "template": "user_{id}", "unique": True},
                "api_token": "hash",
            }
        }
    },
    "verify": {"joins": [{"left": "users.email", "right": "orders.customer_email"}]},
}


def _plan(overrides: dict[str, Any] | None = None) -> Any:
    data = {key: dict(value) for key, value in VALID.items()}
    if overrides:
        data.update(overrides)
    return build_plan(data, Path("dbmask.toml"))


def _problems(overrides: dict[str, Any]) -> str:
    with pytest.raises(DbmaskError) as caught:
        _plan(overrides)
    assert caught.value.code is ErrorCode.E_CONFIG
    assert caught.value.exit_code == EXIT_CONFIG
    return caught.value.message


def test_valid_config_resolves_every_column() -> None:
    plan = _plan()
    assert plan.locale == "en_US"
    assert plan.settings.batch_size == 100
    assert set(plan.tables["users"]) == set(VALID["tables"]["users"]["columns"])
    assert plan.tables["users"]["email"].unique is True
    assert plan.tables["users"]["full_name"].strategy == "fake_name"
    assert plan.joins[0].label == "users.email~orders.customer_email"


def test_shorthand_string_is_a_strategy() -> None:
    assert _plan().tables["users"]["id"].strategy == "keep"


def test_unknown_top_level_key_is_named() -> None:
    assert "surprise: unknown key" in _problems({"surprise": {"a": 1}})


def test_unknown_section_key_is_named() -> None:
    assert "project.author: unknown key" in _problems({"project": {"author": "x"}})


def test_unknown_strategy_is_named() -> None:
    message = _problems({"tables": {"users": {"columns": {"email": "fake_mail"}}}})
    assert 'tables.users.columns.email: unknown strategy "fake_mail"' in message


def test_redact_without_value_is_named() -> None:
    message = _problems({"tables": {"users": {"columns": {"pw": {"strategy": "redact"}}}}})
    assert 'tables.users.columns.pw: strategy "redact" requires a non-empty "value"' in message


def test_option_not_valid_for_strategy_is_named() -> None:
    message = _problems(
        {"tables": {"users": {"columns": {"email": {"strategy": "fake_email", "value": "x"}}}}}
    )
    assert "tables.users.columns.email.value: unknown key" in message


def test_unique_must_be_boolean() -> None:
    message = _problems(
        {"tables": {"users": {"columns": {"email": {"strategy": "fake_email", "unique": "yes"}}}}}
    )
    assert "tables.users.columns.email.unique: must be true or false" in message


def test_template_placeholder_syntax_is_validated() -> None:
    message = _problems(
        {"tables": {"users": {"columns": {"u": {"strategy": "template", "template": "user_{id"}}}}}
    )
    assert "tables.users.columns.u.template:" in message


def test_all_problems_are_reported_together() -> None:
    message = _problems(
        {"tables": {"users": {"columns": {"a": "nope", "b": {"strategy": "redact"}}}}, "extra": {}}
    )
    assert message.startswith("3 problem(s)")


def test_invalid_safety_pattern_is_rejected() -> None:
    message = _problems({"safety": {"database_name_pattern": "_(staging"}})
    assert "safety.database_name_pattern:" in message


def test_batch_size_must_be_positive() -> None:
    assert "settings.batch_size:" in _problems({"settings": {"batch_size": 0}})


def test_join_reference_must_be_table_dot_column() -> None:
    message = _problems({"verify": {"joins": [{"left": "users", "right": "orders.email"}]}})
    assert 'verify.joins[0].left: must be a "table.column" reference' in message


def test_config_hash_is_stable_and_semantic() -> None:
    first = _plan().config_hash
    assert first.startswith("sha256:")
    assert len(first) == 71
    assert first == _plan().config_hash
    changed = _plan({"tables": {"users": {"columns": dict(VALID["tables"]["users"]["columns"])}}})
    assert changed.config_hash == first


def test_config_hash_changes_when_a_strategy_changes() -> None:
    columns = dict(VALID["tables"]["users"]["columns"])
    columns["full_name"] = "keep"
    other = _plan({"tables": {"users": {"columns": columns}}})
    assert other.config_hash != _plan().config_hash


def test_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(DbmaskError) as caught:
        load_plan(tmp_path / "dbmask.toml")
    assert caught.value.code is ErrorCode.E_CONFIG


def test_invalid_toml_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "dbmask.toml"
    path.write_text("[tables\n", encoding="utf-8")
    with pytest.raises(DbmaskError) as caught:
        load_plan(path)
    assert caught.value.code is ErrorCode.E_CONFIG


def test_load_plan_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "dbmask.toml"
    path.write_text(
        '[safety]\ndatabase_name_pattern = "_staging$"\n\n'
        '[tables.users.columns]\nid = "keep"\n'
        'email = { strategy = "fake_email", unique = true }\n',
        encoding="utf-8",
    )
    plan = load_plan(path)
    assert plan.tables["users"]["email"].strategy == "fake_email"
    assert plan.database_name_pattern == "_staging$"
    assert [rule.ref for rule in plan.rules()] == ["users.email", "users.id"]
