"""JSON audit report assembly and writing.

One report per run, containing metadata and outcome. The report is written after
the run completes (or fails), whether the database write succeeded or verification
failed. It lives where the operator asked: `--report path.json`. Never contains a
cell value or a secret: only run IDs, counts, status, and the config hash.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import DbmaskError, ErrorCode
from .logging import log_event


@dataclass(frozen=True)
class ReportData:
    """Data for the JSON audit report. Never includes cell values or secrets."""

    run_id: str
    mode: str
    status: str
    tool_version: str
    config_hash: str
    secret_fingerprint: str
    locale: str
    rows_masked: int
    started_at: str
    finished_at: str | None
    verification_failed: bool | None = None


def write_report(path: Path, report_data: ReportData) -> None:
    """Write the JSON audit report to the given path.

    The report is written atomically via a temporary file to avoid partial
    writes on full disk or process kill.
    """
    try:
        data = asdict(report_data)
        json_str = json.dumps(data, indent=2)

        # Write atomically: temp file then rename
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json_str)
        temp_path.replace(path)

        log_event("report.written", path=str(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise DbmaskError(
            ErrorCode.E_INTERNAL,
            f"could not write report to {path} ({type(exc).__name__}); "
            "the report could not be written",
        ) from None
