"""The single error shape and the exit-code contract.

Every failure in dbmask raises DbmaskError; cli.py maps it to one stderr line and an
exit code. Exit codes are a public contract (docs/api-contracts.md): scripts depend on
them. Messages carry identifiers, constraint names and counts only, never cell values.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    E_CONFIG = "E_CONFIG"
    E_CONNECT = "E_CONNECT"
    E_DRIFT = "E_DRIFT"
    E_SAFETY = "E_SAFETY"
    E_MASK = "E_MASK"
    E_VERIFY = "E_VERIFY"
    E_INTERNAL = "E_INTERNAL"


EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_FINDINGS = 3
EXIT_SAFETY = 4

EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.E_CONFIG: EXIT_CONFIG,
    ErrorCode.E_CONNECT: EXIT_RUNTIME,
    ErrorCode.E_DRIFT: EXIT_FINDINGS,
    ErrorCode.E_SAFETY: EXIT_SAFETY,
    ErrorCode.E_MASK: EXIT_RUNTIME,
    ErrorCode.E_VERIFY: EXIT_FINDINGS,
    ErrorCode.E_INTERNAL: EXIT_RUNTIME,
}


class DbmaskError(Exception):
    """A failure with an error code, an actionable message, and an exit code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def format(self) -> str:
        """The one line written to stderr on failure."""
        return f"error[{self.code}]: {self.message}"
