"""In-place masking: UPDATE rows in the database.

One run masks all tables in alphabetical order, all rows per table in primary key order,
all batches in the same transaction with their progress upsert (resume guarantee). Crash
mid-batch leaves the batch uncommitted and the progress unchanged, so the next `--resume`
re-masks the same batch identically. Crash between batches leaves the progress committed,
so resume skips to the next batch.

The advisory lock and all guards (completed run, incomplete run without --resume, config
hash and secret fingerprint match on resume) are checked before any data is read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .audit import (
    MODE_MASK,
    STATUS_COMPLETED,
    STATUS_FAILED,
    ProgressRow,
    completed_run,
    ensure_tables,
    finish_run,
    incomplete_run,
    insert_run,
    read_progress,
    secret_fingerprint,
    upsert_progress,
    utcnow,
)
from .config import MaskPlan
from .dialects import advisory_lock, batch_select, require_supported, update_batch
from .errors import DbmaskError, ErrorCode
from .introspect import SchemaModel
from .logging import log_event
from .strategies import StrategyEngine


@dataclass(frozen=True)
class MaskResult:
    """Summary of the run."""

    run_id: str
    rows_masked: int
    tables_done: int


def _preflight_guards(connection: Connection, plan: MaskPlan, allow_remasked: bool) -> None:
    """Check that the database is safe to mask: no completed run, no incomplete run without
    resume."""
    if completed_run(connection) is not None:
        if not allow_remasked:
            raise DbmaskError(
                ErrorCode.E_SAFETY,
                "this database has already been masked. restore a fresh copy to re-mask. "
                "to override (test environments only) use --allow-remasked",
            )
        log_event("mask.already_masked_override")
    if incomplete_run(connection) is not None:
        raise DbmaskError(
            ErrorCode.E_SAFETY,
            "an incomplete mask run exists; use --resume to continue it or restore a fresh copy",
        )


def _start_run(
    connection: Connection,
    plan: MaskPlan,
    secret: str,
    tool_version: str,
    mode: str = MODE_MASK,
) -> tuple[str, str]:
    """Create an audit row and return (run_id, config_hash).

    On resume, reuse the incomplete run's ID and verify config/secret match.
    Raises E_SAFETY on mismatch.
    """
    incomplete = incomplete_run(connection)
    if incomplete is not None:
        # This is a resume; verify the config and secret haven't changed.
        if incomplete.config_hash != plan.config_hash:
            raise DbmaskError(
                ErrorCode.E_SAFETY,
                "cannot resume: the config has changed since the incomplete run started. "
                "restore a fresh copy and run again",
            )
        if incomplete.secret_fingerprint != secret_fingerprint(secret):
            raise DbmaskError(
                ErrorCode.E_SAFETY,
                "cannot resume: the secret has changed since the incomplete run started. "
                "restore a fresh copy and run again",
            )
        log_event("mask.resumed", run_id=incomplete.run_id)
        return incomplete.run_id, plan.config_hash

    run_id = str(uuid4())
    insert_run(
        connection,
        run_id=run_id,
        mode=mode,
        tool_version=tool_version,
        config_hash=plan.config_hash,
        fingerprint=secret_fingerprint(secret),
        locale=plan.locale,
        started_at=utcnow(),
    )
    log_event("mask.started", run_id=run_id)
    return run_id, plan.config_hash


def _mask_table(
    connection: Connection,
    engine: StrategyEngine,
    plan: MaskPlan,
    schema: SchemaModel,
    table_name: str,
    run_id: str,
    batch_size: int,
    progress: dict[str, ProgressRow],
) -> int:
    """Mask one table in batches. Returns total rows masked for this table."""
    schema_table = schema.tables[table_name]
    rules = plan.tables[table_name]
    pk_names = schema_table.primary_key

    # Columns to select: PK columns (for batch ordering and update keys) plus all configured columns
    select_names = list(pk_names) + list(rules.keys())

    # Columns that need to be masked (exclude PK columns and 'keep' strategy)
    masked_names = [
        name for name in rules if name not in pk_names and rules[name].strategy != "keep"
    ]

    total_rows = 0
    last_pk: list[object] | None = None

    # Resume from progress if this is a resume run
    if table_name in progress:
        progress_row = progress[table_name]
        last_pk = json.loads(progress_row.last_pk)
        total_rows = progress_row.rows_done

    # Need to get the SQLAlchemy table object
    metadata = sa.MetaData()
    sa_table = sa.Table(table_name, metadata, autoload_with=connection)

    while True:
        # Load next batch
        select_stmt = batch_select(
            sa_table,
            pk_names,
            select_names,
            last_pk,
            batch_size,
        )
        rows = connection.execute(select_stmt).fetchall()
        if not rows:
            break

        # Convert rows to dicts for masking
        row_dicts = [dict(row._mapping) for row in rows]

        # Mask the values
        masked_rows = []
        for row_dict in row_dicts:
            masked_row = {}
            # Keep PK columns unchanged
            for name in pk_names:
                masked_row[name] = row_dict[name]
            # Mask other columns
            for name in masked_names:
                rule = rules[name]
                masked_row[name] = engine.mask(rule, row_dict.get(name), row_dict)
            masked_rows.append(masked_row)

        # Write this batch and update progress in one transaction
        update_batch(connection, sa_table, pk_names, masked_names, masked_rows)

        # Update progress for this batch
        last_row = row_dicts[-1]
        last_pk = [last_row[name] for name in pk_names]
        last_pk_json = json.dumps(last_pk)
        total_rows += len(rows)
        upsert_progress(connection, run_id, table_name, last_pk_json, total_rows)

        connection.commit()
        log_event(
            "mask.batch_done",
            table=table_name,
            rows_in_batch=len(rows),
            rows_total=total_rows,
        )

    return total_rows


def mask_plan(
    connection: Connection,
    strategy_engine: StrategyEngine,
    plan: MaskPlan,
    schema: SchemaModel,
    secret: str,
    *,
    resume: bool = False,
    dry_run: bool = False,
    batch_size: int | None = None,
) -> MaskResult:
    """Execute the mask plan. Returns the run ID and row count.

    Raises DbmaskError on safety violations or during execution.
    """
    # Validate
    require_supported(connection)
    if batch_size is None:
        batch_size = plan.settings.batch_size

    # Dry run: just show the plan, don't write anything
    if dry_run:
        log_event("mask.dry_run", tables=len(plan.tables))
        # Just return a dummy result; actual plan printing happens in CLI
        return MaskResult(run_id="", rows_masked=0, tables_done=0)

    # Create audit tables if needed
    ensure_tables(connection)
    connection.commit()

    # Preflight guards
    if not resume:
        _preflight_guards(connection, plan, allow_remasked=False)
    else:
        # Resume requires incomplete run to exist
        if incomplete_run(connection) is None:
            raise DbmaskError(
                ErrorCode.E_SAFETY,
                "no incomplete run to resume",
            )

    # Single-runner lock
    with advisory_lock(connection, schema.database):
        # Import __version__ here to get the tool version
        from . import __version__

        # Start the run (new or resume)
        run_id, config_hash = _start_run(connection, plan, secret, __version__)
        connection.commit()

        try:
            # Load progress if resuming
            progress_dict = read_progress(connection, run_id) if resume else {}

            total_rows = 0
            tables_done = 0

            # Mask tables in alphabetical order
            for table_name in sorted(plan.tables.keys()):
                rows = _mask_table(
                    connection,
                    strategy_engine,
                    plan,
                    schema,
                    table_name,
                    run_id,
                    batch_size,
                    progress_dict,
                )
                total_rows += rows
                tables_done += 1
                log_event("mask.table_done", table=table_name, rows=rows)

            # Mark the run complete
            finish_run(connection, run_id, STATUS_COMPLETED, total_rows)
            connection.commit()
            log_event("mask.completed", run_id=run_id, rows=total_rows)

            return MaskResult(
                run_id=run_id,
                rows_masked=total_rows,
                tables_done=tables_done,
            )
        except Exception as exc:
            # Mark the run as failed
            finish_run(connection, run_id, STATUS_FAILED, 0)
            connection.commit()
            log_event("mask.failed", run_id=run_id, error=type(exc).__name__)
            raise
