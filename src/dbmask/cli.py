"""The `dbmask` command group. Callbacks stay thin: no SQL, ever, lives here.

The secret is read here and here only, from DBMASK_SECRET. There is deliberately no flag
for it: a flag would put the secret into shell history and `ps` output on shared build
machines. Its value is never echoed, logged, or included in an error message.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click

from . import __version__
from .config import DEFAULT_CONFIG_NAME, MaskPlan, load_plan
from .errors import EXIT_CONFIG, EXIT_OK, EXIT_RUNTIME, DbmaskError, ErrorCode
from .logging import log_event

SECRET_ENV: Final = "DBMASK_SECRET"
MIN_SECRET_LENGTH: Final = 32

_CONTEXT_SETTINGS: Final = {"help_option_names": ["-h", "--help"]}


def load_secret() -> str:
    """The project secret, from the environment only. Never echoed, never logged."""
    raw = os.environ.get(SECRET_ENV, "")
    if not raw:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            f"{SECRET_ENV} is not set; export a project secret of at least "
            f"{MIN_SECRET_LENGTH} characters",
        )
    if len(raw) < MIN_SECRET_LENGTH:
        raise DbmaskError(
            ErrorCode.E_CONFIG,
            f"{SECRET_ENV} is shorter than the required {MIN_SECRET_LENGTH} characters",
        )
    return raw


def require_url(url: str | None, env_var: str, flag: str) -> str:
    if not url:
        raise DbmaskError(ErrorCode.E_CONFIG, f"no database URL: pass {flag} or set {env_var}")
    return url


def _plan(ctx: click.Context) -> MaskPlan:
    return load_plan(ctx.obj)


def _not_implemented(command: str, phase: int) -> None:
    raise DbmaskError(
        ErrorCode.E_INTERNAL, f"{command} is not implemented yet; it lands in phase {phase}"
    )


@click.group(context_settings=_CONTEXT_SETTINGS)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(DEFAULT_CONFIG_NAME),
    show_default=True,
    help="Path to the dbmask config file.",
)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def cli(ctx: click.Context, config_path: Path) -> None:
    """Deterministically mask a restored production database copy.

    Strategies: fake_email, fake_name, fake_phone, fake_address, redact, null, hash, keep,
    template. Exit codes: 0 ok, 1 runtime, 2 config, 3 drift or verification, 4 safety.
    """
    ctx.obj = config_path


@cli.command()
@click.option("--url", envvar="DBMASK_URL", default=None, help="Database URL (env DBMASK_URL).")
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(DEFAULT_CONFIG_NAME),
    show_default=True,
    help="Where to write the starter config.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing output file.")
def init(url: str | None, output: Path, force: bool) -> None:
    """Write a starter dbmask.toml from the live schema at --url. Writes no database."""
    require_url(url, "DBMASK_URL", "--url")
    _not_implemented("init", 2)


@cli.command()
@click.option("--url", envvar="DBMASK_URL", default=None, help="Database URL (env DBMASK_URL).")
@click.pass_context
def check(ctx: click.Context, url: str | None) -> None:
    """Validate the config against the live schema at --url. Reads only, writes nothing."""
    _plan(ctx)
    load_secret()
    require_url(url, "DBMASK_URL", "--url")
    _not_implemented("check", 2)


@cli.command()
@click.option("--url", envvar="DBMASK_URL", default=None, help="Database URL (env DBMASK_URL).")
@click.option("--dry-run", is_flag=True, help="Preflight and plan only; no row reads or writes.")
@click.option("--resume", is_flag=True, help="Continue the newest incomplete run.")
@click.option(
    "--report",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the JSON audit report to this path.",
)
@click.option(
    "--allow-remasked",
    is_flag=True,
    help="DANGEROUS: skip the already-masked refusal. Test environments only.",
)
@click.pass_context
def mask(
    ctx: click.Context,
    url: str | None,
    dry_run: bool,
    resume: bool,
    report: Path | None,
    allow_remasked: bool,
) -> None:
    """UPDATE rows in place in the database at --url. Destructive by design."""
    _plan(ctx)
    load_secret()
    require_url(url, "DBMASK_URL", "--url")
    _not_implemented("mask", 3)


@cli.command()
@click.option(
    "--source-url", envvar="DBMASK_SOURCE_URL", default=None, help="Source URL (read-only session)."
)
@click.option(
    "--target-url", envvar="DBMASK_TARGET_URL", default=None, help="Target URL (must be empty)."
)
@click.option("--dry-run", is_flag=True, help="Preflight and plan only; no writes.")
@click.option(
    "--report",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the JSON audit report to this path.",
)
@click.pass_context
def pump(
    ctx: click.Context,
    source_url: str | None,
    target_url: str | None,
    dry_run: bool,
    report: Path | None,
) -> None:
    """INSERT masked rows read from --source-url into the empty database at --target-url."""
    _plan(ctx)
    load_secret()
    require_url(source_url, "DBMASK_SOURCE_URL", "--source-url")
    require_url(target_url, "DBMASK_TARGET_URL", "--target-url")
    _not_implemented("pump", 4)


@cli.command()
@click.option("--url", envvar="DBMASK_URL", default=None, help="Database URL (env DBMASK_URL).")
@click.option(
    "--source-url",
    envvar="DBMASK_SOURCE_URL",
    default=None,
    help="Optional source URL for the pump row-count comparison.",
)
@click.pass_context
def verify(ctx: click.Context, url: str | None, source_url: str | None) -> None:
    """Re-check consistency of the masked database at --url. Read-only and repeatable."""
    _plan(ctx)
    load_secret()
    require_url(url, "DBMASK_URL", "--url")
    _not_implemented("verify", 5)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point: one stderr line per failure, exit code per docs/api-contracts.md."""
    try:
        result = cli.main(args=argv, prog_name="dbmask", standalone_mode=False)
    except DbmaskError as exc:
        click.echo(exc.format(), err=True)
        return exc.exit_code
    except click.ClickException as exc:
        click.echo(f"error[{ErrorCode.E_CONFIG}]: {exc.format_message()}", err=True)
        return EXIT_CONFIG
    except click.Abort:
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001 - last resort; nothing may escape uncoded
        # Only the exception class is logged: driver exception messages can embed row data.
        log_event("run.failed", error=type(exc).__name__)
        click.echo(
            f"error[{ErrorCode.E_INTERNAL}]: unexpected {type(exc).__name__}; see the log line "
            "above and report it",
            err=True,
        )
        return EXIT_RUNTIME
    return result if isinstance(result, int) else EXIT_OK
