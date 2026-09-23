"""Shared, secret-safe helpers for the MySQL backup/restore scripts."""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import URL, make_url


def mysql_url_from_env(name: str) -> URL:
    raw_url = os.environ.get(name)
    if not raw_url:
        raise ValueError(f"{name} is required")

    url = make_url(raw_url)
    if not url.get_backend_name().startswith("mysql"):
        raise ValueError(f"{name} must be a MySQL URL")
    if not url.database:
        raise ValueError(f"{name} must include a database name")
    if not url.username:
        raise ValueError(f"{name} must include a username")
    return url


def safe_filename_component(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not safe:
        raise ValueError("database name cannot be converted to a safe filename")
    return safe


def _option_file_value(value: object) -> str:
    text = str(value)
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError("MySQL connection values cannot contain control characters")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def mysql_defaults_file(url: URL) -> Iterator[Path]:
    """Create a mode-0600 client option file so credentials never enter argv."""
    query = dict(url.query)
    lines = [
        "[client]",
        f"user={_option_file_value(url.username)}",
        f"host={_option_file_value(url.host or 'localhost')}",
        f"port={int(url.port or 3306)}",
        "default-character-set=utf8mb4",
    ]
    if url.password is not None:
        lines.append(f"password={_option_file_value(url.password)}")
    if query.get("unix_socket"):
        lines.append(f"socket={_option_file_value(query['unix_socket'])}")

    fd, raw_path = tempfile.mkstemp(prefix="mysql-client-", suffix=".cnf")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
