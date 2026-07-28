"""Live schema introspection into a SchemaModel, plus the PII name-pattern list.

Detection is name-pattern heuristics only. dbmask never samples column values to guess
sensitivity: reading production values to classify them would put real data in the tool's
path, which is the one thing this tool exists to avoid.

The patterns err toward false positives on purpose (`file_name` matches `name`). The cost
of a false positive is one explicit `keep` in the config; the cost of a false negative is
unmasked PII in staging. Never narrow a pattern to reduce noise (docs/rules.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from .errors import DbmaskError, ErrorCode
from .logging import scrub_password, scrub_url

# Tables dbmask owns in the masked database. They are not part of the operator's schema,
# so they must not appear as drift the moment a run has created them.
OWNED_TABLES: Final = frozenset({"dbmask_audit", "dbmask_progress"})

TEXT_FAMILY: Final = "text"


@dataclass(frozen=True, slots=True)
class PiiPattern:
    """A column-name pattern, its operator-facing label, and the strategy init proposes."""

    label: str
    regex: re.Pattern[str]
    strategy: str


def _pattern(label: str, expression: str, strategy: str) -> PiiPattern:
    return PiiPattern(label, re.compile(expression, re.IGNORECASE), strategy)


# First match wins, so the specific patterns come before the broad ones.
PII_PATTERNS: Final = (
    _pattern("email", r"e_?mail", "fake_email"),
    _pattern("phone", r"phone|mobile|msisdn|fax", "fake_phone"),
    _pattern("password", r"password|passwd|pwd", "redact"),
    _pattern("secret", r"secret|token|api_?key|credential|private_?key|session_?id", "hash"),
    _pattern("ssn", r"ssn|social_security|national_id", "hash"),
    _pattern("government_id", r"passport|driver_?licen[cs]e|tax_?id|vat_?number", "hash"),
    _pattern("payment", r"card_?number|cardholder|iban|bic|account_?number|routing", "hash"),
    _pattern("dob", r"dob|date_?of_?birth|birth_?date|birthday", "null"),
    _pattern("address", r"address|street|postcode|postal|zip_?code|city|county", "fake_address"),
    _pattern("name", r"name|surname|given|first_?nm|last_?nm", "fake_name"),
    _pattern("ip", r"(^|_)ip(_|$)|ip_?addr", "hash"),
)


def match_pii(column_name: str) -> PiiPattern | None:
    """The first PII pattern the column name matches, or None."""
    for pattern in PII_PATTERNS:
        if pattern.regex.search(column_name):
            return pattern
    return None


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    table: str
    name: str
    family: str
    type_name: str
    length: int | None
    nullable: bool
    pii: str | None

    @property
    def ref(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def is_text(self) -> bool:
        return self.family == TEXT_FAMILY


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    name: str
    table: str
    columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UniqueIndexInfo:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    columns: dict[str, ColumnInfo]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    unique_indexes: tuple[UniqueIndexInfo, ...]

    @property
    def foreign_key_columns(self) -> frozenset[str]:
        return frozenset(name for key in self.foreign_keys for name in key.columns)

    def unique_indexes_covering(self, column: str) -> tuple[UniqueIndexInfo, ...]:
        return tuple(index for index in self.unique_indexes if column in index.columns)


@dataclass(frozen=True, slots=True)
class SchemaModel:
    database: str
    dialect: str
    tables: dict[str, TableInfo]

    def columns(self) -> list[ColumnInfo]:
        return [column for table in self.tables.values() for column in table.columns.values()]

    def pii_columns(self) -> list[ColumnInfo]:
        return [column for column in self.columns() if column.pii is not None]


def database_name(url: str) -> str:
    """The database name in a connection URL, without connecting to it."""
    try:
        parsed = sa.engine.make_url(url)
    except ArgumentError:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            "database URL is not a valid SQLAlchemy URL "
            "(expected postgresql+psycopg://... or mysql+pymysql://...)",
        ) from None
    if not parsed.database:
        raise DbmaskError(ErrorCode.E_CONFIG, "database URL does not name a database")
    return parsed.database


def connect(url: str) -> Engine:
    """An engine with a proven connection, or DbmaskError(E_CONNECT)."""
    try:
        engine = sa.create_engine(url, pool_pre_ping=True, future=True)
        with engine.connect():
            pass
    except (SQLAlchemyError, OSError) as exc:
        # The driver message can embed the URL, so the password is scrubbed out of it and
        # the whole thing is collapsed to the single line the error contract allows.
        detail = " ".join(scrub_password(str(exc), url).split())
        raise DbmaskError(
            ErrorCode.E_CONNECT, f"cannot connect to {scrub_url(url)}: {detail}"
        ) from None
    return engine


def introspect(engine: Engine) -> SchemaModel:
    """Read the live schema. Reads catalog metadata only, never a row of user data."""
    try:
        inspector = sa.inspect(engine)
        names = sorted(name for name in inspector.get_table_names() if name not in OWNED_TABLES)
        tables = {name: _table(inspector, name) for name in names}
    except SQLAlchemyError as exc:
        raise DbmaskError(
            ErrorCode.E_CONNECT,
            f"schema introspection failed on {scrub_url(str(engine.url))}: {type(exc).__name__}",
        ) from None
    return SchemaModel(
        database=engine.url.database or "",
        dialect=engine.dialect.name,
        tables=tables,
    )


def _table(inspector: sa.Inspector, name: str) -> TableInfo:
    columns = {
        raw["name"]: _column(name, raw) for raw in inspector.get_columns(name)  # type: ignore[arg-type]
    }
    primary_key = tuple(inspector.get_pk_constraint(name).get("constrained_columns") or ())
    foreign_keys = tuple(
        ForeignKeyInfo(
            name=raw.get("name") or f"{name}_fk_{index}",
            table=name,
            columns=tuple(raw.get("constrained_columns") or ()),
            referred_table=raw.get("referred_table") or "",
            referred_columns=tuple(raw.get("referred_columns") or ()),
        )
        for index, raw in enumerate(inspector.get_foreign_keys(name))
    )
    return TableInfo(
        name=name,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_indexes=_unique_indexes(inspector, name),
    )


def _unique_indexes(inspector: sa.Inspector, table: str) -> tuple[UniqueIndexInfo, ...]:
    """Unique indexes and unique constraints, merged by name; the PK is excluded by both."""
    found: dict[str, UniqueIndexInfo] = {}
    for index, raw in enumerate(inspector.get_indexes(table)):
        if not raw.get("unique"):
            continue
        label = raw.get("name") or f"{table}_uq_{index}"
        found[label] = UniqueIndexInfo(label, tuple(str(c) for c in raw.get("column_names") or ()))
    for index, constraint in enumerate(inspector.get_unique_constraints(table)):
        label = constraint.get("name") or f"{table}_uc_{index}"
        found.setdefault(label, UniqueIndexInfo(label, tuple(constraint.get("column_names") or ())))
    return tuple(found[label] for label in sorted(found))


def _column(table: str, raw: dict[str, Any]) -> ColumnInfo:
    column_type = raw["type"]
    pattern = match_pii(raw["name"])
    return ColumnInfo(
        table=table,
        name=raw["name"],
        family=type_family(column_type),
        type_name=str(column_type),
        length=getattr(column_type, "length", None),
        nullable=bool(raw.get("nullable", True)),
        pii=pattern.label if pattern is not None else None,
    )


def type_family(column_type: sa.types.TypeEngine[Any]) -> str:
    """Coarse type family used for strategy compatibility. Text is the only one that masks."""
    if isinstance(column_type, sa.String):
        return TEXT_FAMILY
    if isinstance(column_type, sa.Boolean):
        return "boolean"
    if isinstance(column_type, sa.Integer):
        return "integer"
    if isinstance(column_type, sa.Numeric):
        return "numeric"
    if isinstance(column_type, sa.DateTime):
        return "datetime"
    if isinstance(column_type, sa.Date):
        return "date"
    if isinstance(column_type, sa.Time):
        return "time"
    if isinstance(column_type, sa.LargeBinary):
        return "binary"
    return "other"
