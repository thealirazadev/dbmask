"""Structured logfmt logging to stderr.

One line per event: the dotted event name, then key=value fields. Fields are
identifiers, counts and durations only. No cell value, masked or real, and no secret
ever reaches this module (docs/rules.md, value-hygiene rule). Database URLs are only
ever logged through scrub_url, which replaces the password with ***.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit, urlunsplit

_NEEDS_QUOTING = set(' "=\n\t')

_PASSWORD_MASK = "***"


def scrub_url(url: str) -> str:
    """Return url with the password replaced by ***; the only way a URL may be printed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        # Unparseable URLs may still embed credentials, so nothing of them is echoed.
        return _PASSWORD_MASK
    if parts.password is None or parts.hostname is None:
        return url
    userinfo = f"{parts.username or ''}:{_PASSWORD_MASK}@"
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return urlunsplit((parts.scheme, userinfo + host, parts.path, parts.query, parts.fragment))


def scrub_password(text: str, url: str) -> str:
    """Return text with the password from url replaced by ***.

    Driver messages often echo the connection string, so any message derived from a
    driver goes through here before it can reach stderr.
    """
    try:
        password = urlsplit(url).password
    except ValueError:
        return text
    if not password:
        return text
    return text.replace(password, _PASSWORD_MASK)


def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str | int | float):
        raise TypeError(f"log field must be an identifier, count or duration, got {type(value)}")
    text = str(value)
    if text == "" or any(char in _NEEDS_QUOTING for char in text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def log_event(event: str, **fields: object) -> None:
    """Write one logfmt line to stderr, e.g. `mask.table_done table=users rows=182304`."""
    parts = [event]
    parts.extend(f"{key}={_render(value)}" for key, value in fields.items())
    print(" ".join(parts), file=sys.stderr, flush=True)
