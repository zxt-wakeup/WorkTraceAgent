from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ARTIFACT_MARKER_NAME = ".worktrace-period.json"
ARTIFACT_MARKER_SCHEMA = "worktrace-agent/artifact-period-v1"
_MARKER_MAX_BYTES = 4096


def ensure_private_directory(path: Path) -> Path:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if created:
        path.chmod(0o700)
    return path


def write_private_text(path: Path, content: str) -> Path:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary_path.chmod(0o600)
        os.replace(str(temporary_path), str(path))
        path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return path


def write_private_json(path: Path, value: Any) -> Path:
    return write_private_text(
        path, json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    )


def validate_artifact_root(root: Path) -> Path:
    """Resolve an artifact root and reject locations that are unsafe to prune."""

    expanded = Path(root).expanduser()
    if expanded.is_symlink():
        raise ValueError("artifacts root must not be a symbolic link")
    resolved = expanded.resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    home = Path.home().resolve()
    dangerous = {filesystem_root, home, home.parent.resolve()}
    # A direct child such as /tmp, /var, /Users, or C:\\Users is too broad a
    # boundary for an automated retention job, even with ownership markers.
    if resolved.parent == filesystem_root:
        dangerous.add(resolved)
    if resolved in dangerous:
        raise ValueError("artifacts root is too broad for automatic retention")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("artifacts root must be a directory")
    return resolved


def initialize_artifact_period(
    root: Path, period_directory: Path, period_type: str, period_key: str
) -> Path:
    """Create an atomic ownership marker for one canonical artifact period."""

    safe_root = validate_artifact_root(root)
    expected = _period_directory(safe_root, period_type, period_key)
    declared_root = Path(os.path.abspath(str(Path(root).expanduser())))
    declared_expected = _period_directory(declared_root, period_type, period_key)
    supplied = Path(os.path.abspath(str(Path(period_directory).expanduser())))
    if supplied != declared_expected:
        raise ValueError("artifact period directory does not match its root and key")

    ensure_private_directory(safe_root)
    if period_type == "weekly":
        weekly_root = safe_root / "weekly"
        if weekly_root.is_symlink():
            raise ValueError("weekly artifacts directory must not be a symbolic link")
        ensure_private_directory(weekly_root)
        if weekly_root.resolve() != weekly_root:
            raise ValueError("weekly artifacts directory escapes its root")

    if expected.is_symlink():
        raise ValueError("artifact period directory must not be a symbolic link")
    ensure_private_directory(expected)
    if expected.resolve() != expected:
        raise ValueError("artifact period directory escapes its root")

    marker = expected / ARTIFACT_MARKER_NAME
    payload = _marker_payload(period_type, period_key)
    if marker.is_symlink():
        raise ValueError("artifact ownership marker must not be a symbolic link")
    if marker.exists():
        if not _marker_matches(marker, payload):
            raise ValueError("artifact ownership marker is invalid or mismatched")
        return marker
    return write_private_json(marker, payload)


def prune_artifacts(root: Path, retention_days: int, today: date) -> int:
    safe_root = validate_artifact_root(root)
    if retention_days <= 0 or not safe_root.exists():
        return 0
    cutoff = today - timedelta(days=retention_days)
    removed = 0
    for child in safe_root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name == "weekly":
            for week_dir in child.iterdir():
                if week_dir.is_symlink() or not week_dir.is_dir():
                    continue
                try:
                    year_text, week_text = week_dir.name.upper().split("-W", 1)
                    week_start = date.fromisocalendar(int(year_text), int(week_text), 1)
                except (ValueError, TypeError):
                    continue
                if week_start < cutoff and _is_owned_period_directory(
                    safe_root, week_dir, "weekly", week_dir.name
                ):
                    shutil.rmtree(str(week_dir))
                    removed += 1
            continue
        try:
            folder_day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if folder_day < cutoff and _is_owned_period_directory(
            safe_root, child, "daily", child.name
        ):
            shutil.rmtree(str(child))
            removed += 1
    return removed


def _period_directory(root: Path, period_type: str, period_key: str) -> Path:
    if period_type == "daily":
        try:
            canonical = date.fromisoformat(period_key).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("daily artifact key must use YYYY-MM-DD") from exc
        if canonical != period_key:
            raise ValueError("daily artifact key must be canonical")
        return root / period_key
    if period_type == "weekly":
        try:
            year_text, week_text = period_key.split("-W", 1)
            week_start = date.fromisocalendar(int(year_text), int(week_text), 1)
            canonical = "{}-W{:02d}".format(
                week_start.isocalendar().year, week_start.isocalendar().week
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("weekly artifact key must use YYYY-Www") from exc
        if canonical != period_key:
            raise ValueError("weekly artifact key must be canonical")
        return root / "weekly" / period_key
    raise ValueError("period_type must be daily or weekly")


def _marker_payload(period_type: str, period_key: str) -> dict[str, Any]:
    return {
        "schema": ARTIFACT_MARKER_SCHEMA,
        "owner": "worktrace-agent",
        "period_type": period_type,
        "period_key": period_key,
    }


def _marker_matches(marker: Path, expected: dict[str, Any]) -> bool:
    try:
        if marker.is_symlink():
            return False
        metadata = os.lstat(marker)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MARKER_MAX_BYTES:
            return False
        with marker.open("r", encoding="utf-8") as file:
            content = file.read(_MARKER_MAX_BYTES + 1)
        if len(content.encode("utf-8")) > _MARKER_MAX_BYTES:
            return False
        return json.loads(content) == expected
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


def _is_owned_period_directory(
    root: Path, candidate: Path, period_type: str, period_key: str
) -> bool:
    try:
        expected = _period_directory(root, period_type, period_key)
        if candidate != expected or candidate.is_symlink():
            return False
        if candidate.resolve(strict=True) != expected:
            return False
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return _marker_matches(
        candidate / ARTIFACT_MARKER_NAME,
        _marker_payload(period_type, period_key),
    )
