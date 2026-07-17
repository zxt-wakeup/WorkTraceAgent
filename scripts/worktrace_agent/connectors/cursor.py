from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from worktrace_agent.connectors.sqlite_utils import copied_sqlite_connection
from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
    WorkSignal,
)
from worktrace_agent.text import compact_text, is_low_signal, sanitize_full_text
from worktrace_agent.window import (
    TimeWindow,
    file_mtime_in_window,
    parse_timestamp,
    to_iso,
)

CURSOR_KEYS = ["chat", "composer", "cursor", "ai", "aichat", "bubble"]
MESSAGE_LIST_KEYS = {"messages", "bubbles", "turns", "chatMessages", "conversation"}
ROLE_KEYS = ("role", "sender", "author", "type", "messageType")
CONTENT_KEYS = ("content", "text", "message", "body")
TIMESTAMP_KEYS = (
    "createdAt",
    "created_at",
    "timestamp",
    "time",
    "date",
    "updatedAt",
    "updated_at",
)
ROLE_MAP = {
    "user": "user",
    "human": "user",
    "user_message": "user",
    "usermessage": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "bot": "assistant",
    "model": "assistant",
    "assistant_message": "assistant",
    "assistantmessage": "assistant",
    "tool": "tool",
    "tool_result": "tool",
    "toolresult": "tool",
}


class CursorConnector:
    key = "cursor"

    def __init__(self, roots: List[Path]) -> None:
        self.roots = roots

    def scan(self, window: TimeWindow) -> ConnectorResult:
        signals: List[WorkSignal] = []
        conversations: List[ConversationTrace] = []
        existing_roots = [root for root in self.roots if root.exists()]
        for root in existing_roots:
            if not root.exists():
                continue
            signals.extend(self._scan_workspace_storage(root, window))
            signals.extend(self._scan_file_history(root, window))
            state_signals, state_conversations = self._scan_state_databases(
                root, window
            )
            signals.extend(state_signals)
            conversations.extend(state_conversations)
        conversations = _merge_cursor_conversations(conversations)
        if not existing_roots:
            status = "missing"
            detail = "Cursor application roots were not found"
        elif signals or conversations:
            status = "partial"
            detail = (
                "Recognized plaintext Cursor chat messages are evidence; workspace/history/"
                "cache signals remain discovery-only, and full transcript coverage is not claimed"
            )
        else:
            status = "empty"
            detail = "Cursor roots exist, but no selected-period discovery signals were found"
        return ConnectorResult(
            conversations=conversations,
            signals=signals,
            coverage=[
                SourceCoverage(
                    source=self.key,
                    status=status,
                    conversations=len(conversations),
                    messages=sum(len(item.messages) for item in conversations),
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

    def _scan_state_databases(
        self, root: Path, window: TimeWindow
    ) -> Tuple[List[WorkSignal], List[ConversationTrace]]:
        signals: List[WorkSignal] = []
        conversations: List[ConversationTrace] = []
        for db_path in root.rglob("state.vscdb"):
            try:
                if not file_mtime_in_window(db_path, window, slack_days=0):
                    continue
            except OSError:
                continue
            db_signals, db_conversations = self._scan_state_db(db_path, window)
            signals.extend(db_signals)
            conversations.extend(db_conversations)
        return signals, conversations

    def _scan_state_db(
        self, db_path: Path, window: TimeWindow
    ) -> Tuple[List[WorkSignal], List[ConversationTrace]]:
        signals: List[WorkSignal] = []
        conversations: List[ConversationTrace] = []
        try:
            with copied_sqlite_connection(db_path) as connection:
                tables = _table_names(connection)
                if "ItemTable" in tables:
                    item_signals, item_conversations = self._scan_item_table(
                        connection, db_path, window
                    )
                    signals.extend(item_signals)
                    conversations.extend(item_conversations)
                if "ItemTable" not in tables:
                    signals.extend(
                        self._scan_generic_text_tables(connection, db_path, window)
                    )
                for table in tables:
                    if table.lower() == "cursordiskkv":
                        kv_signals, kv_conversations = self._scan_cursor_disk_kv(
                            connection, db_path, window
                        )
                        signals.extend(kv_signals)
                        conversations.extend(kv_conversations)
        except sqlite3.Error:
            return [], []
        return signals[:20], conversations

    def _scan_item_table(
        self, connection: sqlite3.Connection, db_path: Path, window: TimeWindow
    ) -> Tuple[List[WorkSignal], List[ConversationTrace]]:
        where = " OR ".join(["lower(key) LIKE ?" for _ in CURSOR_KEYS])
        params = ["%{}%".format(key) for key in CURSOR_KEYS]
        query = "SELECT key, value FROM ItemTable WHERE {} LIMIT 80".format(where)
        signals: List[WorkSignal] = []
        conversations: List[ConversationTrace] = []
        try:
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return [], []
        for key, value in rows:
            decoded = _conversations_from_cursor_value(key, value, db_path, window)
            conversations.extend(decoded)
            if decoded:
                continue
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
        return signals, conversations

    def _scan_cursor_disk_kv(
        self, connection: sqlite3.Connection, db_path: Path, window: TimeWindow
    ) -> Tuple[List[WorkSignal], List[ConversationTrace]]:
        where = " OR ".join(["lower(key) LIKE ?" for _ in CURSOR_KEYS])
        params = ["%{}%".format(key) for key in CURSOR_KEYS]
        try:
            rows = connection.execute(
                "SELECT key, value FROM cursorDiskKV WHERE {} LIMIT 500".format(
                    where
                ),
                params,
            ).fetchall()
        except sqlite3.Error:
            return [], []
        signals: List[WorkSignal] = []
        conversations: List[ConversationTrace] = []
        for key, value in rows:
            decoded = _conversations_from_cursor_value(key, value, db_path, window)
            conversations.extend(decoded)
            if decoded:
                continue
            snippet = _value_snippet(value)
            if is_low_signal(snippet):
                continue
            signals.append(
                WorkSignal(
                    origin=self.key,
                    workspace=_workspace_from_state_db(db_path),
                    occurred_at=to_iso(db_path.stat().st_mtime, window.timezone),
                    note="Cursor disk cache key {}: {}".format(key, snippet),
                    paths=[str(db_path)],
                    confidence="medium",
                    extra={"key": key, "evidence": "cursor_disk_kv"},
                )
            )
        return signals, conversations

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
    if isinstance(value, str):
        try:
            value = _without_reasoning(json.loads(value))
        except json.JSONDecodeError:
            pass
    return compact_text(value, 900)


def _without_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_reasoning(item) for item in value]
    if not isinstance(value, dict):
        return value
    block_type = str(value.get("type") or "").lower()
    if block_type in {"thinking", "reasoning", "analysis"}:
        return {}
    cleaned: Dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower().replace("_", "").replace("-", "")
        if normalized in {"thinking", "reasoning", "analysis"}:
            continue
        cleaned[str(key)] = _without_reasoning(item)
    return cleaned


def _workspace_from_state_db(db_path: Path) -> str:
    workspace_file = db_path.parent / "workspace.json"
    data = _read_json(workspace_file) if workspace_file.exists() else {}
    folder = str(data.get("folder") or data.get("workspace") or "")
    if folder:
        return Path(folder.replace("file://", "")).name
    return db_path.parent.name


def _conversations_from_cursor_value(
    key: Any, value: Any, db_path: Path, window: TimeWindow
) -> List[ConversationTrace]:
    parsed = _parse_cursor_json(value)
    if parsed is None:
        return []
    conversations: List[ConversationTrace] = []
    for group_index, (metadata, entries, route) in enumerate(
        _find_message_groups(parsed)
    ):
        messages: List[TraceMessage] = []
        for entry_index, entry in enumerate(entries):
            messages.extend(
                _decode_cursor_entry(
                    entry,
                    window,
                    identity_seed="{}:{}:{}".format(route, group_index, entry_index),
                )
            )
        if not messages:
            continue
        raw_conversation_id = ""
        if isinstance(metadata, dict):
            raw_conversation_id = str(
                metadata.get("conversationId")
                or metadata.get("chatId")
                or metadata.get("composerId")
                or metadata.get("tabId")
                or ""
            )
        identity = raw_conversation_id or "{}:{}:{}".format(
            str(key), route, group_index
        )
        conversation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        title = ""
        if isinstance(metadata, dict):
            title = sanitize_full_text(
                metadata.get("title")
                or metadata.get("name")
                or metadata.get("chatTitle")
                or metadata.get("composerName")
                or ""
            )
        conversations.append(
            ConversationTrace(
                origin="cursor",
                conversation_id=conversation_id,
                workspace=_workspace_from_state_db(db_path),
                title=title,
                messages=messages,
                paths=[str(db_path)],
                confidence="medium",
                extra={"evidence": "cursor_plaintext_chat_state"},
            )
        )
    return conversations


def _parse_cursor_json(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _find_message_groups(value: Any) -> List[Tuple[Dict[str, Any], List[Any], str]]:
    groups: List[Tuple[Dict[str, Any], List[Any], str]] = []

    def visit(node: Any, route: str, depth: int) -> None:
        if depth > 10:
            return
        if isinstance(node, list):
            if node and any(_looks_like_cursor_message(item) for item in node):
                groups.append(({}, node, route or "root"))
                return
            for index, item in enumerate(node):
                visit(item, "{}/{}".format(route, index), depth + 1)
            return
        if not isinstance(node, dict):
            return
        if _looks_like_cursor_message(node):
            groups.append((node, [node], route or "root"))
            return
        consumed = set()
        for key, child in node.items():
            if key in MESSAGE_LIST_KEYS and isinstance(child, list) and child:
                if any(_looks_like_cursor_message(item) for item in child):
                    groups.append((node, child, "{}/{}".format(route, key)))
                    consumed.add(key)
        for key, child in node.items():
            if key not in consumed and isinstance(child, (dict, list)):
                visit(child, "{}/{}".format(route, key), depth + 1)

    visit(value, "", 0)
    return groups


def _looks_like_cursor_message(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if _cursor_role(value) and any(key in value for key in CONTENT_KEYS):
        return True
    return isinstance(value.get("prompt"), str) and isinstance(
        value.get("response"), str
    )


def _decode_cursor_entry(
    value: Any, window: TimeWindow, identity_seed: str
) -> List[TraceMessage]:
    if not isinstance(value, dict):
        return []
    occurred_at = _cursor_timestamp(value, window)
    if not occurred_at:
        return []
    role = _cursor_role(value)
    if role:
        content = _cursor_content(value)
        if not content:
            return []
        return [
            _cursor_trace_message(
                value,
                role,
                content,
                occurred_at,
                identity_seed,
            )
        ]
    prompt = sanitize_full_text(value.get("prompt"))
    response = sanitize_full_text(value.get("response"))
    result: List[TraceMessage] = []
    if prompt:
        result.append(
            _cursor_trace_message(
                value, "user", prompt, occurred_at, identity_seed + ":prompt"
            )
        )
    if response:
        result.append(
            _cursor_trace_message(
                value,
                "assistant",
                response,
                occurred_at,
                identity_seed + ":response",
            )
        )
    return result


def _cursor_trace_message(
    raw: Dict[str, Any],
    role: str,
    content: str,
    occurred_at: str,
    identity_seed: str,
) -> TraceMessage:
    raw_identity = raw.get("id") or raw.get("messageId") or raw.get("bubbleId") or ""
    identity = "{}:{}:{}".format(identity_seed, raw_identity, content)
    message_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return TraceMessage(
        message_id=message_id,
        role=role,
        content=content,
        occurred_at=occurred_at,
        kind="tool_output" if role == "tool" else "message",
        extra={"evidence": "cursor_plaintext_chat_state"},
    )


def _cursor_role(value: Dict[str, Any]) -> str:
    for key in ROLE_KEYS:
        raw = value.get(key)
        if isinstance(raw, dict):
            raw = raw.get("role") or raw.get("type") or raw.get("name")
        if not isinstance(raw, str):
            continue
        normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
        role = ROLE_MAP.get(normalized)
        if role:
            return role
    return ""


def _cursor_content(value: Dict[str, Any]) -> str:
    for key in CONTENT_KEYS:
        if key not in value:
            continue
        content = _cursor_plain_text(value[key])
        if content:
            return sanitize_full_text(content)
    return ""


def _cursor_plain_text(value: Any, depth: int = 0) -> str:
    if depth > 6 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_cursor_plain_text(item, depth + 1) for item in value]
        return "\n".join(item for item in parts if item)
    if not isinstance(value, dict):
        return ""
    block_type = str(value.get("type") or "").lower()
    if block_type in {"thinking", "reasoning", "analysis"}:
        return ""
    for key in ("text", "content", "body", "value"):
        if key in value:
            content = _cursor_plain_text(value[key], depth + 1)
            if content:
                return content
    return ""


def _cursor_timestamp(value: Dict[str, Any], window: TimeWindow) -> str:
    for key in TIMESTAMP_KEYS:
        if key not in value:
            continue
        parsed = parse_timestamp(value[key], window.timezone)
        if parsed is not None and window.start <= parsed < window.end:
            return parsed.isoformat(timespec="seconds")
    return ""


def _merge_cursor_conversations(
    conversations: List[ConversationTrace],
) -> List[ConversationTrace]:
    merged: Dict[str, ConversationTrace] = {}
    seen_messages: Dict[str, set] = {}
    for conversation in conversations:
        existing = merged.get(conversation.conversation_id)
        if existing is None:
            existing = ConversationTrace(
                origin=conversation.origin,
                conversation_id=conversation.conversation_id,
                workspace=conversation.workspace,
                title=conversation.title,
                messages=[],
                paths=list(conversation.paths),
                confidence=conversation.confidence,
                extra=dict(conversation.extra),
            )
            merged[conversation.conversation_id] = existing
            seen_messages[conversation.conversation_id] = set()
        elif not existing.title and conversation.title:
            existing.title = conversation.title
        existing.paths = list(dict.fromkeys(existing.paths + conversation.paths))
        seen = seen_messages[conversation.conversation_id]
        for message in conversation.messages:
            fingerprint = (message.role, message.content, message.occurred_at)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            existing.messages.append(message)
    for conversation in merged.values():
        conversation.messages.sort(
            key=lambda item: (item.occurred_at or "", item.message_id)
        )
    return list(merged.values())
