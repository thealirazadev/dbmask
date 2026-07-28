"""Load and validate dbmask.toml into an immutable MaskPlan. No database access.

Validation is deliberately strict: unknown keys, unknown strategies and options that do
not belong to their strategy are rejected rather than ignored. The drift guard only works
if silence is impossible, and a typo in a strategy name must never mask nothing quietly.
All problems found in one pass are reported together.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .errors import DbmaskError, ErrorCode

DEFAULT_CONFIG_NAME: Final = "dbmask.toml"
DEFAULT_LOCALE: Final = "en_US"
DEFAULT_BATCH_SIZE: Final = 5000
DEFAULT_CACHE_SIZE: Final = 500_000

STRATEGY_NAMES: Final = (
    "fake_email",
    "fake_name",
    "fake_phone",
    "fake_address",
    "redact",
    "null",
    "hash",
    "keep",
    "template",
)

PLACEHOLDER_PATTERN: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_TOP_LEVEL_KEYS: Final = frozenset({"project", "safety", "settings", "tables", "verify"})
_PROJECT_KEYS: Final = frozenset({"locale"})
_SAFETY_KEYS: Final = frozenset({"database_name_pattern"})
_SETTINGS_KEYS: Final = frozenset({"batch_size", "cache_size"})
_VERIFY_KEYS: Final = frozenset({"joins"})
_TABLE_KEYS: Final = frozenset({"columns"})
_JOIN_KEYS: Final = frozenset({"left", "right"})

_COMMON_RULE_KEYS: Final = frozenset({"strategy", "unique"})
_RULE_KEYS: Final[dict[str, frozenset[str]]] = {
    "fake_email": _COMMON_RULE_KEYS,
    "fake_name": _COMMON_RULE_KEYS,
    "fake_phone": _COMMON_RULE_KEYS,
    "fake_address": _COMMON_RULE_KEYS,
    "hash": _COMMON_RULE_KEYS,
    "redact": _COMMON_RULE_KEYS | {"value"},
    "template": _COMMON_RULE_KEYS | {"template"},
    "null": frozenset({"strategy"}),
    "keep": frozenset({"strategy"}),
}


@dataclass(frozen=True, slots=True)
class ColumnRule:
    """One column's resolved strategy and its options."""

    table: str
    column: str
    strategy: str
    value: str | None = None
    template: str | None = None
    unique: bool = False

    @property
    def ref(self) -> str:
        return f"{self.table}.{self.column}"


def template_placeholders(template: str) -> tuple[str, ...]:
    """The column names a template refers to, in order of first appearance."""
    seen: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(template):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return tuple(seen)


def template_problems(template: str) -> list[str]:
    """Syntax problems in a template string; empty when the template is well formed."""
    problems: list[str] = []
    if not template_placeholders(template):
        problems.append('must contain at least one "{column}" placeholder')
    residue = PLACEHOLDER_PATTERN.sub("", template)
    if "{" in residue or "}" in residue:
        problems.append("has an unbalanced or malformed placeholder")
    return problems


@dataclass(frozen=True, slots=True)
class Settings:
    batch_size: int = DEFAULT_BATCH_SIZE
    cache_size: int = DEFAULT_CACHE_SIZE


@dataclass(frozen=True, slots=True)
class JoinPair:
    """A logical join with no foreign key behind it, verified after every run."""

    left_table: str
    left_column: str
    right_table: str
    right_column: str

    @property
    def label(self) -> str:
        return f"{self.left_table}.{self.left_column}~{self.right_table}.{self.right_column}"


@dataclass(frozen=True, slots=True)
class MaskPlan:
    path: Path
    locale: str
    database_name_pattern: str | None
    settings: Settings
    tables: dict[str, dict[str, ColumnRule]]
    joins: tuple[JoinPair, ...]
    config_hash: str

    def rules(self) -> list[ColumnRule]:
        return [rule for columns in self.tables.values() for rule in columns.values()]


def load_plan(path: Path) -> MaskPlan:
    """Read and validate a config file. Raises DbmaskError(E_CONFIG) on any problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            f'config file "{path}" not found; run `dbmask init` to generate a starter config',
        ) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise DbmaskError(
            ErrorCode.E_CONFIG, f'config file "{path}" could not be read: {type(exc).__name__}'
        ) from None
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DbmaskError(
            ErrorCode.E_CONFIG, f'config file "{path}" is not valid TOML: {exc}'
        ) from None
    return build_plan(data, path)


def build_plan(data: dict[str, Any], path: Path) -> MaskPlan:
    problems: list[str] = []
    _reject_unknown(problems, "", data, _TOP_LEVEL_KEYS)

    project = _section(problems, data, "project", _PROJECT_KEYS)
    locale = project.get("locale", DEFAULT_LOCALE)
    if not isinstance(locale, str) or not locale:
        problems.append("project.locale: must be a non-empty string")
        locale = DEFAULT_LOCALE

    safety = _section(problems, data, "safety", _SAFETY_KEYS)
    pattern = _parse_pattern(problems, safety.get("database_name_pattern"))

    settings_table = _section(problems, data, "settings", _SETTINGS_KEYS)
    settings = Settings(
        batch_size=_positive_int(problems, settings_table, "batch_size", DEFAULT_BATCH_SIZE),
        cache_size=_positive_int(problems, settings_table, "cache_size", DEFAULT_CACHE_SIZE),
    )

    tables = _parse_tables(problems, data.get("tables"))
    joins = _parse_joins(problems, _section(problems, data, "verify", _VERIFY_KEYS).get("joins"))

    if problems:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            f"{len(problems)} problem(s) in {path.name}: " + "; ".join(problems),
        )
    return MaskPlan(
        path=path,
        locale=locale,
        database_name_pattern=pattern,
        settings=settings,
        tables=tables,
        joins=joins,
        config_hash=config_hash(locale, pattern, settings, tables, joins),
    )


def config_hash(
    locale: str,
    pattern: str | None,
    settings: Settings,
    tables: dict[str, dict[str, ColumnRule]],
    joins: tuple[JoinPair, ...],
) -> str:
    """`sha256:<hex>` over the canonicalized plan.

    Hashing the meaning rather than the file text means comments and formatting can change
    without blocking a resume, while any change to what happens to a column blocks it.
    """
    payload = {
        "locale": locale,
        "safety": {"database_name_pattern": pattern},
        "settings": {"batch_size": settings.batch_size, "cache_size": settings.cache_size},
        "tables": {
            table: {
                column: {
                    "strategy": rule.strategy,
                    "value": rule.value,
                    "template": rule.template,
                    "unique": rule.unique,
                }
                for column, rule in columns.items()
            }
            for table, columns in tables.items()
        },
        "joins": sorted(
            [join.left_table, join.left_column, join.right_table, join.right_column]
            for join in joins
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reject_unknown(
    problems: list[str], prefix: str, table: dict[str, Any], allowed: frozenset[str]
) -> None:
    for key in sorted(set(table) - allowed):
        problems.append(f"{prefix}{key}: unknown key")


def _section(
    problems: list[str], data: dict[str, Any], name: str, allowed: frozenset[str]
) -> dict[str, Any]:
    section = data.get(name, {})
    if not isinstance(section, dict):
        problems.append(f"{name}: must be a table")
        return {}
    _reject_unknown(problems, f"{name}.", section, allowed)
    return section


def _parse_pattern(problems: list[str], raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        problems.append("safety.database_name_pattern: must be a non-empty string")
        return None
    try:
        re.compile(raw)
    except re.error as exc:
        problems.append(f"safety.database_name_pattern: not a valid regular expression ({exc.msg})")
        return None
    return raw


def _positive_int(problems: list[str], table: dict[str, Any], key: str, default: int) -> int:
    raw = table.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        problems.append(f"settings.{key}: must be an integer of 1 or more")
        return default
    return raw


def _parse_tables(problems: list[str], raw: object) -> dict[str, dict[str, ColumnRule]]:
    tables: dict[str, dict[str, ColumnRule]] = {}
    if raw is None:
        problems.append("tables: at least one table must be configured")
        return tables
    if not isinstance(raw, dict):
        problems.append("tables: must be a table")
        return tables
    for table in sorted(raw):
        entry = raw[table]
        if not isinstance(entry, dict):
            problems.append(f"tables.{table}: must be a table")
            continue
        _reject_unknown(problems, f"tables.{table}.", entry, _TABLE_KEYS)
        columns = entry.get("columns")
        if not isinstance(columns, dict) or not columns:
            problems.append(f"tables.{table}.columns: must be a non-empty table of columns")
            continue
        rules: dict[str, ColumnRule] = {}
        for column in sorted(columns):
            rule = _parse_rule(problems, table, column, columns[column])
            if rule is not None:
                rules[column] = rule
        tables[table] = rules
    return tables


def _parse_rule(problems: list[str], table: str, column: str, entry: object) -> ColumnRule | None:
    prefix = f"tables.{table}.columns.{column}"
    if isinstance(entry, str):
        entry = {"strategy": entry}
    if not isinstance(entry, dict):
        problems.append(f"{prefix}: must be a strategy name or a table of strategy options")
        return None

    strategy = entry.get("strategy")
    if not isinstance(strategy, str):
        problems.append(f'{prefix}: missing required key "strategy"')
        return None
    if strategy not in STRATEGY_NAMES:
        problems.append(
            f'{prefix}: unknown strategy "{strategy}"; valid strategies are '
            + ", ".join(STRATEGY_NAMES)
        )
        return None
    _reject_unknown(problems, f"{prefix}.", entry, _RULE_KEYS[strategy])

    unique = entry.get("unique", False)
    if not isinstance(unique, bool):
        problems.append(f"{prefix}.unique: must be true or false")
        unique = False

    value: str | None = None
    if strategy == "redact":
        raw_value = entry.get("value")
        if not isinstance(raw_value, str) or not raw_value:
            problems.append(f'{prefix}: strategy "redact" requires a non-empty "value"')
        else:
            value = raw_value

    template: str | None = None
    if strategy == "template":
        raw_template = entry.get("template")
        if not isinstance(raw_template, str) or not raw_template:
            problems.append(f'{prefix}: strategy "template" requires a non-empty "template"')
        else:
            problems.extend(f"{prefix}.template: {p}" for p in template_problems(raw_template))
            template = raw_template

    return ColumnRule(
        table=table,
        column=column,
        strategy=strategy,
        value=value,
        template=template,
        unique=unique,
    )


def _parse_joins(problems: list[str], raw: object) -> tuple[JoinPair, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append("verify.joins: must be an array of tables")
        return ()
    joins: list[JoinPair] = []
    for index, entry in enumerate(raw):
        prefix = f"verify.joins[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{prefix}: must be a table with left and right")
            continue
        _reject_unknown(problems, f"{prefix}.", entry, _JOIN_KEYS)
        left = _split_ref(problems, prefix, "left", entry.get("left"))
        right = _split_ref(problems, prefix, "right", entry.get("right"))
        if left is not None and right is not None:
            joins.append(JoinPair(left[0], left[1], right[0], right[1]))
    return tuple(joins)


def _split_ref(problems: list[str], prefix: str, key: str, raw: object) -> tuple[str, str] | None:
    if not isinstance(raw, str) or raw.count(".") != 1 or raw.startswith(".") or raw.endswith("."):
        problems.append(f'{prefix}.{key}: must be a "table.column" reference')
        return None
    table, column = raw.split(".")
    return table, column
