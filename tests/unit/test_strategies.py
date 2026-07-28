"""Determinism, divergence, NULL passthrough, unique suffixing, and the value cache.

Every determinism assertion compares two separately constructed engines: a single engine
could fake stability through its cache.
"""

from __future__ import annotations

import pytest

from dbmask.config import STRATEGY_NAMES, ColumnRule
from dbmask.errors import DbmaskError
from dbmask.strategies import (
    FAKE_FAMILIES,
    MAX_OUTPUT_LENGTH,
    UNIQUE_SUFFIX_LENGTH,
    StrategyEngine,
    max_output_length,
    normalize,
)

SECRET = "unit-test-secret-of-at-least-32-chars"
OTHER_SECRET = "another-unit-test-secret-32-chars-min"

HEX_DIGITS = set("0123456789abcdef")


def rule(
    strategy: str,
    *,
    value: str | None = None,
    template: str | None = None,
    unique: bool = False,
) -> ColumnRule:
    return ColumnRule(
        table="users",
        column="c",
        strategy=strategy,
        value=value,
        template=template,
        unique=unique,
    )


def engine(secret: str = SECRET) -> StrategyEngine:
    return StrategyEngine(secret)


@pytest.mark.parametrize("family", FAKE_FAMILIES + ("hash",))
def test_same_input_masks_identically_across_engines(family: str) -> None:
    values = ["alice@example.com", "Bob Jones", "+1 555 010 0000"]
    left = [engine().mask(rule(family), value) for value in values]
    right = [engine().mask(rule(family), value) for value in values]
    assert left == right


@pytest.mark.parametrize("family", FAKE_FAMILIES + ("hash",))
def test_a_different_secret_diverges(family: str) -> None:
    value = "alice@example.com"
    assert engine().mask(rule(family), value) != engine(OTHER_SECRET).mask(rule(family), value)


def test_strategy_families_diverge_on_the_same_input() -> None:
    value = "alice@example.com"
    outputs = {family: engine().mask(rule(family), value) for family in FAKE_FAMILIES + ("hash",)}
    assert len(set(outputs.values())) == len(outputs)


def test_email_case_and_whitespace_variants_converge() -> None:
    email = rule("fake_email")
    canonical = engine().mask(email, "alice@example.com")
    assert engine().mask(email, "  Alice@Example.COM ") == canonical
    assert engine().mask(email, "ALICE@EXAMPLE.COM") == canonical


def test_non_email_families_are_case_sensitive() -> None:
    name = rule("fake_name")
    assert engine().mask(name, "alice") != engine().mask(name, "Alice")


def test_normalize_only_folds_emails() -> None:
    assert normalize("fake_email", " A@B.COM ") == "a@b.com"
    assert normalize("fake_name", " Alice ") == " Alice "


@pytest.mark.parametrize("strategy", STRATEGY_NAMES)
def test_null_passes_through_every_strategy(strategy: str) -> None:
    value = "MASKED" if strategy == "redact" else None
    template = "user_{id}" if strategy == "template" else None
    masked = engine().mask(rule(strategy, value=value, template=template), None, {"id": 1})
    assert masked is None


def test_keep_returns_the_input_unchanged() -> None:
    assert engine().mask(rule("keep"), "  Alice  ") == "  Alice  "


def test_null_strategy_discards_the_input() -> None:
    assert engine().mask(rule("null"), "secret note") is None


def test_redact_returns_the_configured_value() -> None:
    assert engine().mask(rule("redact", value="MASKED"), "hunter2") == "MASKED"


def test_hash_is_keyed_and_hex() -> None:
    digest = engine().mask(rule("hash"), "token-1")
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert set(digest) <= HEX_DIGITS
    assert digest != engine(OTHER_SECRET).mask(rule("hash"), "token-1")


def test_template_renders_from_row_values() -> None:
    template = rule("template", template="user_{id}")
    assert engine().mask(template, "alice", {"id": 42}) == "user_42"


def test_template_with_unknown_placeholder_names_the_column() -> None:
    template = rule("template", template="user_{missing}")
    with pytest.raises(DbmaskError) as caught:
        engine().mask(template, "alice", {"id": 42})
    assert "missing" in caught.value.message
    assert "alice" not in caught.value.message


@pytest.mark.parametrize("family", FAKE_FAMILIES)
def test_unique_suffix_is_twelve_hex_chars(family: str) -> None:
    plain = engine().mask(rule(family), "alice@example.com")
    suffixed = engine().mask(rule(family, unique=True), "alice@example.com")
    assert isinstance(plain, str) and isinstance(suffixed, str)
    assert len(suffixed) == len(plain) + UNIQUE_SUFFIX_LENGTH + 1
    suffix = suffixed.split("@")[0][-UNIQUE_SUFFIX_LENGTH:] if "@" in suffixed else suffixed[-12:]
    assert set(suffix) <= HEX_DIGITS


def test_unique_email_suffix_goes_before_the_at_sign() -> None:
    masked = engine().mask(rule("fake_email", unique=True), "alice@example.com")
    assert isinstance(masked, str)
    local, _, domain = masked.partition("@")
    assert "@" not in domain and "." in domain
    assert local[-13] == "-"


def test_unique_suffix_is_deterministic_and_distinguishes_inputs() -> None:
    unique_email = rule("fake_email", unique=True)
    first = engine().mask(unique_email, "alice@example.com")
    assert first == engine().mask(unique_email, "alice@example.com")
    assert first != engine().mask(unique_email, "bob@example.com")


def test_redact_with_unique_still_separates_rows() -> None:
    redacted = rule("redact", value="MASKED", unique=True)
    assert engine().mask(redacted, "a") != engine().mask(redacted, "b")


@pytest.mark.parametrize("family", FAKE_FAMILIES)
def test_generated_values_stay_within_the_declared_maximum(family: str) -> None:
    ceiling = MAX_OUTPUT_LENGTH[family]
    assert ceiling is not None
    unique_rule = rule(family, unique=True)
    budget = max_output_length(unique_rule)
    assert budget == ceiling + UNIQUE_SUFFIX_LENGTH + 1
    one = engine()
    for index in range(3000):
        masked = one.mask(unique_rule, f"value-{index}@example.com")
        assert isinstance(masked, str)
        assert len(masked) <= budget


def test_max_output_length_for_redact_follows_the_configured_value() -> None:
    assert max_output_length(rule("redact", value="MASKED")) == 6
    assert max_output_length(rule("redact", value="MASKED", unique=True)) == 19
    assert max_output_length(rule("keep")) is None
    assert max_output_length(rule("template", template="user_{id}")) is None


def test_cache_serves_repeated_values_without_changing_output() -> None:
    one = engine()
    email = rule("fake_email")
    first = one.mask(email, "alice@example.com")
    assert one.misses == 1 and one.hits == 0
    assert one.mask(email, "ALICE@example.com") == first
    assert one.hits == 1
    assert one.cache_hit_rate == pytest.approx(0.5)


def test_cache_is_bounded_and_evicts_oldest_first() -> None:
    small = StrategyEngine(SECRET, cache_size=2)
    email = rule("fake_email")
    outputs = [small.mask(email, f"user{index}@example.com") for index in range(3)]
    assert len(small._cache) == 2
    assert small.mask(email, "user0@example.com") == outputs[0]
    assert small.misses == 4


def test_engine_repr_does_not_leak_the_secret() -> None:
    one = engine()
    assert SECRET not in repr(one)
    assert SECRET not in repr(vars(one))
