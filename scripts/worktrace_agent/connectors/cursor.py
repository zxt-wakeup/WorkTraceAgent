from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from worktrace_agent.connectors.sqlite_utils import copied_sqlite_connection
from worktrace_agent.schema import ConnectorResult, SourceCoverage, WorkSignal
from worktrace_agent.text import compact_text, is_low_signal
from worktrace_agent.window import TimeWindow, file_mtime_in_window, to_iso

CURSOR_KEYS = ["chat", "composer", "cursor", "ai", "aichat"]


class CursorConnector:
    key = "cursor"

    def __init__(self, roots: List[Path]) -> None:
        self.roots = roots

    def scan(self, window: TimeWindow) -> ConnectorResult:
        signals: List[WorkSignal] = []
        existing_roots = [root for root in self.roots if root.exists()]
        for root in existing_roots:
            if not root.exists():
                continue
            signals.extend(self._scan_workspace_storage(root, window))
            signals.extend(self._scan_file_history(root, window))
            signals.extend(self._scan_state_databases(root, window))
        if not existing_roots:
            status = "missing"
            detail = "Cursor application roots were not found"
        elif signals:
            status = "partial"
            detail = "Cursor workspace/history/cache signals are discovery-only; full transcript coverage is not claimed"
        else:
            status = "empty"
            detail = "Cursor roots exist, but no selected-period discovery signals were found"
        return ConnectorResult(
            signals=signals,
            coverage=[
                SourceCoverage(
                    source=self.key,
                    status=status,
                    detail=detail,
                )
            ],
        )

    def _scan_workspace_storage(
        self, root: Path, window: TimeWindow
    ) -> List[WorkSignal]:
        storage_root = root / "User" / "workspaceStorage"
        if not storage_root.exists():
            return []
        signals: List[WorkSignal] = []
        for workspace_file in storage_root.glob("*/workspace.json"):
            try:
                if not file_mtime_in_window(workspace_file, window, slack_days=0):
                    continue
            except OSError:
                continue
            data = _read_json(workspace_file)
            folder = str(data.get("folder") or data.get("workspace") or "")
            workspace = (
                Path(folder.replace("file://", "")).name
                if folder
                else workspace_file.parent.name
            )
            signals.append(
                WorkSignal(
                    origin=self.key,
                    workspace=workspace,
                    occurred_at=to_iso(workspace_file.stat().st_mtime, window.timezone),
                    note="Cursor workspace active: {}".format(folder or workspace),
                    paths=[str(workspace_file)],
                    confidence="medium",
                    extra={"folder": folder, "evidence": "cursor_workspace_storage"},
                )
            )
        return signals

    def _scan_file_history(self, root: Path, window: TimeWindow) -> List[WorkSignal]:
        history_root = root / "User" / "History"
        if not history_root.exists():
            return []
        signals: List[WorkSignal] = []
        for entries_file in history_root.glob("*/entries.json"):
            try:
                if not file_mtime_in_window(entries_file, window, slack_days=0):
                    continue
            except OSError:
                continue
            data = _read_json(entries_file)
            if not isinstance(data, dict):
                continue
            resource = str(data.get("resource") or data.get("source") or "")
            entries = data.get("entries") or []
            if not isinstance(entries, list):
                entries = []
            changed = [
                item for item in entries if _entry_in_window(item, entries_file, window)
            ]
            if not changed:
                continue
            file_path = resource.replace("file://", "")
            workspace = (
                Path(file_path).parent.name if file_path else entries_file.parent.name
            )
            signals.append(
                WorkSignal(
                    origin=self.key,
                    workspace=workspace,
                    occurred_at=_entry_time(changed[-1], entries_file, window),
                    note="Cursor file history: {} ({} entries)".format(
                        file_path or entries_file.parent.name, len(changed)
                    ),
                    paths=[file_path] if file_path else [str(entries_file)],
                    confidence="medium",
                    extra={
                        "entry_count": len(changed),
                        "evidence": "cursor_file_history",
                    },
                )
            )
        return signals

    def _scan_state_databases(self, root: Path, window: TimeWindow) -> List[WorkSignal]:
        signals: List[WorkSignal] = []
        for db_path in root.rglob("state.vscdb"):
            try:
                if not file_mtime_in_window(db_path, window, slack_days=0):
                    continue
            except OSError:
                continue
            signals.extend(self._scan_state_db(db_path, window))
        return signals

    def _scan_state_db(self, db_path: Path, window: TimeWindow) -> List[WorkSignal]:
        signals: List[WorkSignal] = []
        try:
            with copied_sqlite_connection(db_path) as connection:
                tables = _table_names(connection)
                if "ItemTable" in tables:
                    signals.extend(self._scan_item_table(connection, db_path, window))
                else:
                    signals.extend(
                        self._scan_generic_text_tables(connection, db_path, window)
                    )
        except sqlite3.Error:
            return []
        return signals[:20]

    def _scan_item_table(
        self, connection: sqlite3.Connection, db_path: Path, window: TimeWindow
    ) -> List[WorkSignal]:
        where = " OR ".join(["lower(key) LIKE ?" for _ in CURSOR_KEYS])
        params = ["%{}%".format(key) for key in CURSOR_KEYS]
        query = "SELECT key, value FROM ItemTable WHERE {} LIMIT 80".format(where)
        signals: List[WorkSignal] = []
        try:
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        for key, value in rows:
            snippet = _value_snippet(value)
            if is_low_signal(snippet):
                continue
            workspace = _workspace_from_state_db(db_path)
            signals.append(
                WorkSignal(
                    origin=self.key,
                    workspace=workspace,
                    occurred_at=to_iso(db_path.stat().st_mtime, window.timezone),
                    note="Cursor chat/cache key {}: {}".format(key, snippet),
                    paths=[str(db_path)],
                    confidence="medium",
                    extra={"key": key, "evidence": "cursor_state_vscdb"},
                )
            )
        return signals

    def _scan_generic_text_tables(
        self, connection: sqlite3.Connection, db_path: Path, window: TimeWindow
    ) -> List[WorkSignal]:
        signals: List[WorkSignal] = []
        for table in _table_names(connection):
            columns = _table_columns(connection, table)
            text_columns = [
                item
                for item in columns
                if item.lower() in {"key", "value", "data", "body"}
            ]
            if not text_columns:
                continue
            column_expr = ", ".join(text_columns[:3])
            try:
                rows = connection.execute(
                    "SELECT {} FROM {} LIMIT 80".format(column_expr, table)
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                snippet = _value_snippet(row)
                lowered = snippet.lower()
                if not any(key in lowered for key in CURSOR_KEYS) or is_low_signal(
                    snippet
                ):
                    continue
                signals.append(
                    WorkSignal(
                        origin=self.key,
                        workspace=_workspace_from_state_db(db_path),
                        occurred_at=to_iso(db_path.stat().st_mtime, window.timezone),
                        note="Cursor state snippet: {}".format(snippet),
                        paths=[str(db_path)],
                        confidence="low",
                        extra={
                            "table": table,
                            "evidence": "cursor_state_vscdb_generic",
                        },
                    )
                )
        return signals


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _entry_in_window(entry: Any, fallback_path: Path, window: TimeWindow) -> bool:
    if isinstance(entry, dict):
        timestamp = (
            entry.get("timestamp") or entry.get("mtime") or entry.get("lastModified")
        )
        if timestamp is not None:
            return window.contains(timestamp)
    try:
        return window.contains(fallback_path.stat().st_mtime)
    except OSError:
        return False


def _entry_time(entry: Any, fallback_path: Path, window: TimeWindow) -> str:
    if isinstance(entry, dict):
        timestamp = (
            entry.get("timestamp") or entry.get("mtime") or entry.get("lastModified")
        )
        if timestamp is not None:
            return to_iso(timestamp, window.timezone)
    try:
        return to_iso(fallback_path.stat().st_mtime, window.timezone)
    except OSError:
        return ""


def _table_names(connection: sqlite3.Connection) -> List[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [
        str(row[1]) for row in connection.execute("PRAGMA table_info({})".format(table))
    ]


def _value_snippet(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return compact_text(value, 900)


def _workspace_from_state_db(db_path: Path) -> str:
    workspace_file = db_path.parent / "workspace.json"
    data = _read_json(workspace_file) if workspace_file.exists() else {}
    folder = str(data.get("folder") or data.get("workspace") or "")
    if folder:
        return Path(folder.replace("file://", "")).name
    return db_path.parent.name
