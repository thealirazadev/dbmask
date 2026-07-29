"""The two tables dbmask owns in the masked database: dbmask_audit and dbmask_progress.

Names and columns are the contract in `docs/architecture.md`; do not rename them. The
tables hold identifiers, counts and timestamps only: no cell value, no secret. What
records the secret is `secret_fingerprint`, 12 hex chars of a salted digest, which is
enough to refuse a resume attempted with a different secret and useless for recovering it.

The progress upsert is written as UPDATE-then-INSERT rather than a dialect-specific
ON CONFLICT / ON DUPLICATE KEY form: it is portable Core, and the advisory lock already
guarantees dbmask is the only writer of these rows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from .errors import DbmaskError, ErrorCode

AUDIT_TABLE: Final = "dbmask_audit"
PROGRESS_TABLE: Final = "dbmask_progress"

STATUS_RUNNING: Final = "running"
STATUS_COMPLETED: Final = "completed"
STATUS_FAILED: Final = "failed"

MODE_MASK: Final = "mask"
MODE_PUMP: Final = "pump"

_FINGERPRINT_SALT: Final = "dbmask-fp:"
_FINGERPRINT_LENGTH: Final = 12

metadata = sa.MetaData()

audit_table = sa.Table(
    AUDIT_TABLE,
    metadata,
    sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
    sa.Column("run_id", sa.String(36), nullable=False, unique=True),
    sa.Column("mode", sa.String(8), nullable=False),
    sa.Column("tool_version", sa.String(20), nullable=False),
    sa.Column("config_hash", sa.String(71), nullable=False),
    sa.Column("secret_fingerprint", sa.String(12), nullable=False),
    sa.Column("locale", sa.String(16), nullable=False),
    sa.Column("status", sa.String(12), nullable=False),
    sa.Column("rows_masked", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("started_at", sa.DateTime, nullable=False),
    sa.Column("finished_at", sa.DateTime, nullable=True),
    sa.Index("ix_dbmask_audit_status", "status"),
)

progress_table = sa.Table(
    PROGRESS_TABLE,
    metadata,
    sa.Column("run_id", sa.String(36), primary_key=True),
    sa.Column("table_name", sa.String(128), primary_key=True),
    sa.Column("last_pk", sa.Text, nullable=False),
    sa.Column("rows_done", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.ForeignKeyConstraint(["run_id"], [f"{AUDIT_TABLE}.run_id"], name="fk_dbmask_progress_run"),
)


@dataclass(frozen=True, slots=True)
class RunRow:
    """One row of dbmask_audit, as much of it as the guards and resume need."""

    run_id: str
    mode: str
    status: str
    config_hash: str
    secret_fingerprint: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class ProgressRow:
    table_name: str
    last_pk: str
    rows_done: int


def utcnow() -> datetime:
    """Naive UTC: MySQL DATETIME carries no zone, so the zone is dropped consistently."""
    return datetime.now(UTC).replace(tzinfo=None)


def secret_fingerprint(secret: str) -> str:
    """12 hex chars of sha256("dbmask-fp:" + secret): detects a changed secret, leaks none."""
    digest = hashlib.sha256((_FINGERPRINT_SALT + secret).encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_LENGTH]


@contextmanager
def _guard(action: str) -> Iterator[None]:
    """Map a bookkeeping failure to E_MASK without letting the driver message through."""
    try:
        yield
    except SQLAlchemyError as exc:
        raise DbmaskError(
            ErrorCode.E_MASK, f"{action} failed ({type(exc).__name__}); see the log line above"
        ) from None


def tables_exist(connection: Connection) -> bool:
    """Whether a previous run left its bookkeeping behind. A fresh restore has neither."""
    with _guard("looking for the dbmask audit tables"):
        inspector = sa.inspect(connection)
        return inspector.has_table(AUDIT_TABLE)


def ensure_tables(connection: Connection) -> None:
    with _guard(f"creating {AUDIT_TABLE} and {PROGRESS_TABLE}"):
        metadata.create_all(connection, checkfirst=True)


def insert_run(
    connection: Connection,
    *,
    run_id: str,
    mode: str,
    tool_version: str,
    config_hash: str,
    fingerprint: str,
    locale: str,
    started_at: datetime,
) -> None:
    with _guard(f"recording the run in {AUDIT_TABLE}"):
        connection.execute(
            sa.insert(audit_table).values(
                run_id=run_id,
                mode=mode,
                tool_version=tool_version,
                config_hash=config_hash,
                secret_fingerprint=fingerprint,
                locale=locale,
                status=STATUS_RUNNING,
                rows_masked=0,
                started_at=started_at,
            )
        )


def finish_run(connection: Connection, run_id: str, status: str, rows_masked: int) -> None:
    with _guard(f"closing the run row in {AUDIT_TABLE}"):
        connection.execute(
            sa.update(audit_table)
            .where(audit_table.c.run_id == run_id)
            .values(status=status, rows_masked=rows_masked, finished_at=utcnow())
        )


def _runs(connection: Connection, statuses: tuple[str, ...]) -> list[RunRow]:
    with _guard(f"reading {AUDIT_TABLE}"):
        rows = connection.execute(
            sa.select(
                audit_table.c.run_id,
                audit_table.c.mode,
                audit_table.c.status,
                audit_table.c.config_hash,
                audit_table.c.secret_fingerprint,
                audit_table.c.started_at,
            )
            .where(audit_table.c.status.in_(statuses))
            .order_by(audit_table.c.id.desc())
        ).all()
    return [RunRow(*row) for row in rows]


def completed_run(connection: Connection) -> RunRow | None:
    """The newest finished run, the evidence that this copy was already masked."""
    runs = _runs(connection, (STATUS_COMPLETED,))
    return runs[0] if runs else None


def incomplete_run(connection: Connection) -> RunRow | None:
    """The newest run that never finished; the only run `--resume` may continue."""
    runs = _runs(connection, (STATUS_RUNNING, STATUS_FAILED))
    return runs[0] if runs else None


def upsert_progress(
    connection: Connection, run_id: str, table_name: str, last_pk: str, rows_done: int
) -> None:
    """Record the last committed batch. Must run inside that batch's own transaction."""
    with _guard(f"writing {PROGRESS_TABLE}"):
        updated = connection.execute(
            sa.update(progress_table)
            .where(
                progress_table.c.run_id == run_id,
                progress_table.c.table_name == table_name,
            )
            .values(last_pk=last_pk, rows_done=rows_done, updated_at=utcnow())
        )
        if updated.rowcount:
            return
        connection.execute(
            sa.insert(progress_table).values(
                run_id=run_id,
                table_name=table_name,
                last_pk=last_pk,
                rows_done=rows_done,
                updated_at=utcnow(),
            )
        )


def read_progress(connection: Connection, run_id: str) -> dict[str, ProgressRow]:
    with _guard(f"reading {PROGRESS_TABLE}"):
        rows = connection.execute(
            sa.select(
                progress_table.c.table_name,
                progress_table.c.last_pk,
                progress_table.c.rows_done,
            ).where(progress_table.c.run_id == run_id)
        ).all()
    return {row[0]: ProgressRow(*row) for row in rows}
