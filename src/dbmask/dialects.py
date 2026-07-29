"""The dialect seam: everything PostgreSQL and MySQL do differently lives here.

Two real dialects justify this module and nothing else in dbmask gets an abstraction
layer. Raw `text()` is allowed here alone, always with bound parameters; identifiers are
never interpolated, they come from reflected `sa.Table` objects and go through
SQLAlchemy's quoting.

The batched UPDATE has two shapes because the databases do: PostgreSQL rewrites a whole
batch in one statement with `UPDATE ... FROM (VALUES ...)`, while MySQL has no portable
VALUES-in-UPDATE form and takes an executemany of UPDATE-by-primary-key instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Final

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Select

from .errors import DbmaskError, ErrorCode
from .logging import log_event

POSTGRESQL: Final = "postgresql"
MYSQL: Final = "mysql"
SUPPORTED_DIALECTS: Final = (POSTGRESQL, MYSQL)

_LOCK_PREFIX: Final = "dbmask:"
_MYSQL_LOCK_NAME_LIMIT: Final = 64


def require_supported(connection: Connection) -> str:
    """The dialect name, or E_CONFIG naming the two dbmask supports."""
    name = connection.dialect.name
    if name not in SUPPORTED_DIALECTS:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            f'dialect "{name}" is not supported; dbmask supports '
            + " and ".join(SUPPORTED_DIALECTS),
        )
    return name


def lock_key(database: str) -> int:
    """A stable signed 64-bit key for pg_try_advisory_lock, derived from the database name."""
    digest = hashlib.sha256((_LOCK_PREFIX + database).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_name(database: str) -> str:
    """MySQL lock names are limited to 64 characters."""
    return (_LOCK_PREFIX + database)[:_MYSQL_LOCK_NAME_LIMIT]


@contextmanager
def advisory_lock(connection: Connection, database: str) -> Iterator[None]:
    """Hold the single-runner lock for the whole run, or refuse with E_SAFETY.

    Both locks are session scoped, so they survive the per-batch commits and are released
    when this block exits or the connection dies. Zero wait by design: a second dbmask
    process must fail fast rather than queue behind a long destructive run.
    """
    dialect = require_supported(connection)
    if dialect == POSTGRESQL:
        acquire = sa.text("SELECT pg_try_advisory_lock(:key)")
        release = sa.text("SELECT pg_advisory_unlock(:key)")
        params: dict[str, object] = {"key": lock_key(database)}
    else:
        acquire = sa.text("SELECT GET_LOCK(:name, 0)")
        release = sa.text("SELECT RELEASE_LOCK(:name)")
        params = {"name": lock_name(database)}

    try:
        acquired = connection.execute(acquire, params).scalar()
        connection.rollback()
    except SQLAlchemyError as exc:
        raise DbmaskError(
            ErrorCode.E_MASK, f"could not take the dbmask lock ({type(exc).__name__})"
        ) from None
    if not acquired:
        raise DbmaskError(
            ErrorCode.E_SAFETY, "another dbmask process holds the lock on this database"
        )
    try:
        yield
    finally:
        try:
            connection.rollback()
            connection.execute(release, params)
            connection.rollback()
        except SQLAlchemyError as exc:
            # Losing the lock release is not worth masking the error that got us here:
            # the lock dies with the session anyway.
            log_event("mask.lock_release_failed", error=type(exc).__name__)


def batch_select(
    table: sa.Table,
    pk_names: Sequence[str],
    column_names: Sequence[str],
    last_pk: Sequence[object] | None,
    limit: int,
) -> Select[tuple[object, ...]]:
    """One batch of rows in primary key order, after `last_pk`.

    Composite primary keys use a row-value comparison, which both dialects support and
    which keeps the resume cursor to one comparison instead of an OR-chain per key column.
    """
    pk_columns = [table.c[name] for name in pk_names]
    statement = (
        sa.select(*(table.c[name] for name in column_names)).order_by(*pk_columns).limit(limit)
    )
    if last_pk is None:
        return statement
    bounds = [
        sa.literal(value, type_=column.type)
        for column, value in zip(pk_columns, last_pk, strict=True)
    ]
    if len(pk_columns) == 1:
        return statement.where(pk_columns[0] > bounds[0])
    return statement.where(sa.tuple_(*pk_columns) > sa.tuple_(*bounds))


def update_batch(
    connection: Connection,
    table: sa.Table,
    pk_names: Sequence[str],
    masked_names: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> int:
    """Write one batch of masked rows, keyed by primary key. Returns rows written."""
    if not rows or not masked_names:
        return 0
    if connection.dialect.name == POSTGRESQL:
        return _update_from_values(connection, table, pk_names, masked_names, rows)
    return _update_executemany(connection, table, pk_names, masked_names, rows)


def _update_from_values(
    connection: Connection,
    table: sa.Table,
    pk_names: Sequence[str],
    masked_names: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> int:
    names = [*pk_names, *masked_names]
    columns = [sa.column(f"c{index}", table.c[name].type) for index, name in enumerate(names)]
    batch = sa.values(*columns, name="dbmask_batch").data(
        [tuple(row[name] for name in names) for row in rows]
    )
    statement = (
        sa.update(table)
        .values(
            {
                name: batch.c[f"c{index}"]
                for index, name in enumerate(names)
                if index >= len(pk_names)
            }
        )
        .where(
            sa.and_(*(table.c[name] == batch.c[f"c{index}"] for index, name in enumerate(pk_names)))
        )
    )
    return connection.execute(statement).rowcount


def _update_executemany(
    connection: Connection,
    table: sa.Table,
    pk_names: Sequence[str],
    masked_names: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> int:
    statement = (
        sa.update(table)
        .where(
            sa.and_(
                *(
                    table.c[name] == sa.bindparam(f"dbmask_pk_{index}")
                    for index, name in enumerate(pk_names)
                )
            )
        )
        .values(
            {name: sa.bindparam(f"dbmask_v_{index}") for index, name in enumerate(masked_names)}
        )
    )
    parameters = [
        {
            **{f"dbmask_pk_{index}": row[name] for index, name in enumerate(pk_names)},
            **{f"dbmask_v_{index}": row[name] for index, name in enumerate(masked_names)},
        }
        for row in rows
    ]
    return connection.execute(statement, parameters).rowcount
