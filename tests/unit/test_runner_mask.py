"""Run-level ordering in `mask_plan`, with every database call replaced by a recorder.

The database behavior itself is covered by the integration suite. These tests pin the
order of operations that the integration suite cannot easily force: guards run inside the
single-runner lock, and a failed run rolls back its open batch before recording the failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest

from dbmask import runner_mask
from dbmask.audit import STATUS_COMPLETED, STATUS_FAILED, RunRow
from dbmask.errors import EXIT_SAFETY, DbmaskError, ErrorCode
from tests.conftest import make_column, make_plan, make_schema, make_table

SECRET = "unit-test-secret-of-at-least-32-chars"

COMPLETED = RunRow(
    run_id="earlier-run",
    mode="mask",
    status=STATUS_COMPLETED,
    config_hash="sha256:x",
    secret_fingerprint="abc",
    started_at=datetime(2026, 1, 1),
)


class RecordingConnection:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> dict[str, Any]:
    """Replace every database touchpoint in runner_mask; returns knobs for the test."""
    state: dict[str, Any] = {"completed": None, "mask_error": None}

    @contextmanager
    def lock(connection: object, database: str) -> Iterator[None]:
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    def completed_run(connection: object) -> RunRow | None:
        events.append("guard:completed")
        return state["completed"]

    def incomplete_run(connection: object) -> RunRow | None:
        events.append("guard:incomplete")
        return None

    def mask_table(*args: object, **kwargs: object) -> int:
        events.append("mask_table")
        if state["mask_error"] is not None:
            raise state["mask_error"]
        return 3

    def finish_run(connection: object, run_id: str, status: str, rows: int) -> None:
        events.append(f"finish:{status}")

    monkeypatch.setattr(runner_mask, "require_supported", lambda connection: "postgresql")
    monkeypatch.setattr(runner_mask, "advisory_lock", lock)
    monkeypatch.setattr(runner_mask, "ensure_tables", lambda c: events.append("ensure_tables"))
    monkeypatch.setattr(runner_mask, "completed_run", completed_run)
    monkeypatch.setattr(runner_mask, "incomplete_run", incomplete_run)
    monkeypatch.setattr(runner_mask, "insert_run", lambda c, **kw: events.append("insert_run"))
    monkeypatch.setattr(runner_mask, "_mask_table", mask_table)
    monkeypatch.setattr(runner_mask, "finish_run", finish_run)
    return state


def run(events: list[str], **kwargs: Any) -> runner_mask.MaskResult:
    plan = make_plan({"users": {"id": "keep", "email": "fake_email"}})
    schema = make_schema(
        make_table("users", [make_column("users", "id"), make_column("users", "email")])
    )
    return runner_mask.mask_plan(
        RecordingConnection(events),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        plan,
        schema,
        SECRET,
        **kwargs,
    )


def test_guards_and_bookkeeping_run_inside_the_lock(
    events: list[str], fake_db: dict[str, Any]
) -> None:
    run(events)
    assert events[0] == "lock"
    assert events[-1] == "unlock"
    for step in ("ensure_tables", "guard:completed", "insert_run", "finish:completed"):
        assert events.index("lock") < events.index(step) < events.index("unlock")


def test_an_already_masked_database_is_refused_inside_the_lock(
    events: list[str], fake_db: dict[str, Any]
) -> None:
    fake_db["completed"] = COMPLETED
    with pytest.raises(DbmaskError) as excinfo:
        run(events)
    assert excinfo.value.code is ErrorCode.E_SAFETY
    assert excinfo.value.exit_code == EXIT_SAFETY
    assert "--allow-remasked" in excinfo.value.message
    assert "insert_run" not in events
    assert events.index("lock") < events.index("guard:completed") < events.index("unlock")


def test_allow_remasked_masks_an_already_masked_database(
    events: list[str], fake_db: dict[str, Any]
) -> None:
    fake_db["completed"] = COMPLETED
    result = run(events, allow_remasked=True)
    assert result.rows_masked == 3
    assert "insert_run" in events
    assert "finish:completed" in events


def test_a_failed_run_rolls_back_the_open_batch_before_recording_failure(
    events: list[str], fake_db: dict[str, Any]
) -> None:
    fake_db["mask_error"] = RuntimeError("unique violation mid-batch")
    with pytest.raises(RuntimeError, match="unique violation"):
        run(events)
    failed = events.index(f"finish:{STATUS_FAILED}")
    assert events[failed - 1] == "rollback"
    assert events[failed + 1] == "commit"
