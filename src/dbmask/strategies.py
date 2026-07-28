"""The determinism core: nine strategies as pure functions of (secret, options, value).

Masked output is a pure function of (secret, strategy family, normalized value, locale,
Faker version). No wall clock, no RNG outside the per-value seeded Faker instance, no
per-run state. That is what makes `users.email` and `orders.customer_email` mask to the
same fake address, in either execution mode, on every run.

The strategy family is part of the HMAC message (domain separation): one string masked
as a name and the same string masked as an email produce uncorrelated outputs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import OrderedDict
from collections.abc import Mapping
from typing import Final

from faker import Faker

from .config import (
    DEFAULT_CACHE_SIZE,
    DEFAULT_LOCALE,
    PLACEHOLDER_PATTERN,
    ColumnRule,
    template_placeholders,
)
from .errors import DbmaskError, ErrorCode

FAKE_FAMILIES: Final = ("fake_email", "fake_name", "fake_phone", "fake_address")

HASH_LENGTH: Final = 64

# 12 hex chars = 48 bits from the same HMAC digest, so the suffix is deterministic and
# needs no in-memory seen-set (tables larger than RAM must work). Below roughly 16M
# distinct values the birthday probability is negligible, and a residual collision is not
# tolerated silently: the unique index rejects it and the verify pass re-checks.
UNIQUE_SUFFIX_LENGTH: Final = 12
UNIQUE_SEPARATOR: Final = "-"
UNIQUE_SUFFIX_COST: Final = UNIQUE_SUFFIX_LENGTH + len(UNIQUE_SEPARATOR)

_SUFFIX_OFFSET: Final = 8
_SUFFIX_BYTES: Final = UNIQUE_SUFFIX_LENGTH // 2

# Declared per-strategy output ceilings, used by the length budget in `check`. None means
# the ceiling depends on config (redact) or on row data (template), or that the strategy
# never generates a value (keep, null). The bounds are asserted by unit tests over a large
# sample of seeds, so a Faker bump that widens output fails the suite instead of the schema.
MAX_OUTPUT_LENGTH: Final[Mapping[str, int | None]] = {
    "fake_email": 64,
    "fake_name": 64,
    "fake_phone": 32,
    "fake_address": 128,
    "hash": HASH_LENGTH,
    "redact": None,
    "template": None,
    "null": None,
    "keep": None,
}

_FAMILY_SEPARATOR: Final = b"\x1f"
_SEED_BYTES: Final = 8


def max_output_length(rule: ColumnRule) -> int | None:
    """Longest value the rule can produce, or None when it cannot be bounded statically."""
    if rule.strategy == "redact":
        base = len(rule.value) if rule.value is not None else None
    else:
        base = MAX_OUTPUT_LENGTH[rule.strategy]
    if base is None:
        return None
    return base + UNIQUE_SUFFIX_COST if rule.unique else base


def apply_unique_suffix(value: str, digest: bytes) -> str:
    """Insert the deterministic suffix before `@` for emails, append it otherwise."""
    suffix = digest[_SUFFIX_OFFSET : _SUFFIX_OFFSET + _SUFFIX_BYTES].hex()
    local, at, domain = value.rpartition("@")
    if at:
        return f"{local}{UNIQUE_SEPARATOR}{suffix}@{domain}"
    return f"{value}{UNIQUE_SEPARATOR}{suffix}"


def normalize(family: str, value: str) -> str:
    """Input form the HMAC is taken over.

    `fake_email` folds case and surrounding whitespace so that case variants of one real
    address converge on a single fake address; every other family uses the exact string.
    """
    if family == "fake_email":
        return value.strip().lower()
    return value


class StrategyEngine:
    """Applies column rules to values. Constructed once per run, used by both runners."""

    def __init__(
        self, secret: str, locale: str = DEFAULT_LOCALE, cache_size: int = DEFAULT_CACHE_SIZE
    ) -> None:
        # The secret is never held as an attribute and never interpolated: only a keyed
        # HMAC object, copied per value, so no repr of this engine can leak it.
        self._mac = hmac.new(secret.encode("utf-8"), digestmod=hashlib.sha256)
        self._faker = Faker(locale)
        self.locale = locale
        # Production data repeats heavily (one address appears in many rows), so the cache
        # makes CPU cost proportional to distinct values rather than to rows. It only ever
        # holds generated values, keyed by family and normalized input.
        self._cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._cache_size = max(cache_size, 0)
        self.hits = 0
        self.misses = 0

    @property
    def cache_hit_rate(self) -> float:
        lookups = self.hits + self.misses
        return self.hits / lookups if lookups else 0.0

    def _digest(self, family: str, normalized: str) -> bytes:
        mac = self._mac.copy()
        mac.update(family.encode("utf-8") + _FAMILY_SEPARATOR + normalized.encode("utf-8"))
        return mac.digest()

    def _generate(self, family: str, normalized: str, digest: bytes) -> str:
        key = (family, normalized)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            self._cache.move_to_end(key)
            return cached
        self.misses += 1
        generated = self._call_provider(family, int.from_bytes(digest[:_SEED_BYTES], "big"))
        if self._cache_size:
            self._cache[key] = generated
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return generated

    def _call_provider(self, family: str, seed: int) -> str:
        self._faker.seed_instance(seed)
        if family == "fake_email":
            return self._faker.email()
        if family == "fake_name":
            return self._faker.name()
        if family == "fake_phone":
            return self._faker.phone_number()
        return self._faker.address()

    def _render_template(self, rule: ColumnRule, row: Mapping[str, object] | None) -> str:
        if rule.template is None or row is None:
            raise DbmaskError(
                ErrorCode.E_INTERNAL,
                f"template strategy for {rule.ref} was applied without row values",
            )
        missing = [name for name in template_placeholders(rule.template) if name not in row]
        if missing:
            raise DbmaskError(
                ErrorCode.E_CONFIG,
                f"template for {rule.ref} references unknown columns: {', '.join(missing)}",
            )
        return PLACEHOLDER_PATTERN.sub(
            lambda match: "" if row[match.group(1)] is None else str(row[match.group(1)]),
            rule.template,
        )

    def mask(
        self, rule: ColumnRule, value: object, row: Mapping[str, object] | None = None
    ) -> object:
        """Masked value for one cell. NULL passes through every strategy untouched."""
        if rule.strategy == "keep":
            return value
        if value is None:
            return None
        if rule.strategy == "null":
            return None
        if rule.strategy == "template":
            return self._render_template(rule, row)

        text = value if isinstance(value, str) else str(value)
        normalized = normalize(rule.strategy, text)
        digest = self._digest(rule.strategy, normalized)
        if rule.strategy == "redact":
            masked = rule.value if rule.value is not None else ""
        elif rule.strategy == "hash":
            masked = digest.hex()
        else:
            masked = self._generate(rule.strategy, normalized, digest)
        return apply_unique_suffix(masked, digest) if rule.unique else masked
