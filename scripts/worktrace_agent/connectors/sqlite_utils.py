from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def copied_sqlite_connection(path: Path):
    """Open a consistent snapshot, including committed WAL pages when available."""
    tmp = tempfile.NamedTemporaryFile(
        prefix="worktrace-", suffix=".sqlite", delete=False
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        try:
            source = sqlite3.connect(
                "file:{}?mode=ro".format(path), uri=True, timeout=2
            )
            destination = sqlite3.connect(str(tmp_path))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        except sqlite3.Error:
            # Some application databases refuse backup while migrating. A plain
            # copy is still preferable to opening the live file read/write.
            shutil.copy2(str(path), str(tmp_path))
        connection = sqlite3.connect(str(tmp_path))
        try:
            yield connection
        finally:
            connection.close()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def readonly_sqlite_connection(path: Path):
    return sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
