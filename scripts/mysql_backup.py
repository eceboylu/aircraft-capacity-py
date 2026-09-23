#!/usr/bin/env python3
"""Create a transaction-safe, compressed MySQL backup from DATABASE_URL."""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _mysql_backup_common import (
    mysql_defaults_file,
    mysql_url_from_env,
    safe_filename_component,
)


def retention_days() -> int:
    raw = os.environ.get("MYSQL_BACKUP_RETENTION_DAYS", "0").strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError(
            "MYSQL_BACKUP_RETENTION_DAYS must be a non-negative integer"
        ) from exc
    if days < 0:
        raise ValueError("MYSQL_BACKUP_RETENTION_DAYS must be a non-negative integer")
    return days


def prune_old_backups(directory: Path, database_component: str, days: int) -> int:
    if days == 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pattern = re.compile(
        rf"^{re.escape(database_component)}_(\d{{8}}T\d{{6}}Z)\.sql\.gz$"
    )
    deleted = 0
    for candidate in directory.iterdir():
        if not candidate.is_file():
            continue
        match = pattern.fullmatch(candidate.name)
        if not match:
            continue
        created_at = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        if created_at < cutoff:
            candidate.unlink()
            deleted += 1
    return deleted


def main() -> int:
    try:
        url = mysql_url_from_env("DATABASE_URL")
        days = retention_days()
        database_component = safe_filename_component(url.database or "")
        output_dir = Path(
            os.environ.get("MYSQL_BACKUP_DIR", "backups/mysql")
        ).expanduser().resolve()
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = output_dir / f"{database_component}_{timestamp}.sql.gz"
        if destination.exists():
            raise FileExistsError(f"backup destination already exists: {destination}")

        mysqldump = os.environ.get("MYSQLDUMP_BIN", "mysqldump")
        with mysql_defaults_file(url) as defaults_file:
            command = [
                mysqldump,
                f"--defaults-extra-file={defaults_file}",
                "--single-transaction",
                "--quick",
                "--routines",
                "--triggers",
                "--events",
                "--hex-blob",
                "--no-tablespaces",
                "--set-gtid-purged=OFF",
                url.database or "",
            ]
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=output_dir
            )
            os.close(fd)
            temporary = Path(temporary_name)
            try:
                temporary.chmod(0o600)
                with tempfile.TemporaryFile() as stderr_file:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=stderr_file,
                    )
                    assert process.stdout is not None
                    with gzip.open(temporary, "wb", compresslevel=6) as compressed:
                        shutil.copyfileobj(process.stdout, compressed, length=1024 * 1024)
                    process.stdout.close()
                    return_code = process.wait()
                    stderr_file.seek(0)
                    error_text = stderr_file.read().decode("utf-8", errors="replace").strip()
                if return_code != 0:
                    raise RuntimeError(error_text or f"mysqldump exited with {return_code}")
                if temporary.stat().st_size == 0:
                    raise RuntimeError("mysqldump produced an empty backup")
                # Same-directory hard-link publication is atomic and, unlike
                # replace(), can never overwrite a same-second backup created
                # concurrently by another process.
                os.link(temporary, destination)
                temporary.unlink()
            finally:
                if temporary.exists():
                    temporary.unlink()

        deleted = prune_old_backups(output_dir, database_component, days)
        print(f"BACKUP FILE: {destination}")
        print("BACKUP METHOD: mysqldump --single-transaction | gzip")
        if days == 0:
            print("BACKUP RETENTION: disabled (MYSQL_BACKUP_RETENTION_DAYS=0)")
        else:
            print(f"BACKUP RETENTION: {days} days; deleted {deleted} expired backup(s)")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
