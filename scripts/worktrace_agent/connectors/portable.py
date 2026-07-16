from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from worktrace_agent.connectors.sqlite_utils import copied_sqlite_connection
from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
)
from worktrace_agent.text import sanitize_full_text
from worktrace_agent.window import TimeWindow, parse_timestamp, to_iso


@dataclass(frozen=True)
class AgentProfile:
    key: str
    label: str
    roots: Tuple[str, ...]
    patterns: Tuple[str, ...] = (
        "**/*.jsonl",
        "**/sessions/**/*.json",
        "**/session*.json",
        "**/conversations/**/*.json",
        "**/conversation*.json",
        "**/chats/**/*.json",
        "**/chat*.json",
        "**/tasks/**/*.json",
        "**/history/**/*.json",
        "**/state*.sqlite",
        "**/state*.db",
        "**/opencode.db",
        "**/db.sqlite",
        "**/state.vscdb",
    )
    support_note: str = ""


@dataclass(frozen=True)
class _ParseOutcome:
    conversations: List[ConversationTrace]
    omitted_untimestamped: int = 0
    malformed_records: int = 0
    limit_details: Tuple[str, ...] = ()


_SENSITIVE_SQLITE_TABLE_TOKENS = {
    "account",
    "accounts",
    "apikey",
    "apikeys",
    "auth",
    "authentication",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "key",
    "keys",
    "oauth",
    "password",
    "passwords",
    "passwd",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_SQLITE_CONTENT_COLUMNS = {
    "body",
    "content",
    "input",
    "output",
    "parts",
    "prompt",
    "result",
    "text",
}
_SQLITE_ROLE_COLUMNS = {
    "actor",
    "author",
    "eventtype",
    "messagetype",
    "role",
    "sender",
    "speaker",
    "type",
}
_SQLITE_TIMESTAMP_COLUMNS = {
    "createdat",
    "date",
    "occurredat",
    "time",
    "timecreated",
    "timestamp",
    "ts",
    "updatedat",
}
_SQLITE_JSON_COLUMNS = {
    "data",
    "eventjson",
    "eventpayload",
    "json",
    "message",
    "messagejson",
    "messagepayload",
    "payload",
}
_SQLITE_CONVERSATION_COLUMNS = {
    "chatid",
    "conversationid",
    "sessionid",
    "taskid",
    "threadid",
}


# Paths are deliberately product-scoped. WorkTrace never crawls the whole home directory.
KNOWN_AGENT_PROFILES: Dict[str, AgentProfile] = {
    "zcode": AgentProfile(
        "zcode",
        "ZCode",
        (
            "~/.zcode",
            "~/Library/Application Support/ZCode",
            "~/.config/zcode",
            "~/.local/share/zcode",
        ),
        support_note="ZCode's current SQLite layout is version-probed and may drift; coverage remains best effort",
    ),
    "trae": AgentProfile(
        "trae",
        "Trae",
        (
            "~/.trae",
            "~/Library/Application Support/Trae",
            "~/Library/Application Support/Trae CN",
            "~/.trae-server/ai-agent",
            "~/.trae-cn-server/ai-agent",
            "~/.config/trae",
        ),
        support_note="Trae full chat databases may be encrypted; only readable exports or recognized plaintext sessions are ingested",
    ),
    "qoder": AgentProfile(
        "qoder",
        "Qoder",
        (
            "~/.qoder",
            "~/Library/Application Support/Qoder",
            "~/.config/qoder",
        ),
    ),
    "codebuddy": AgentProfile(
        "codebuddy",
        "CodeBuddy",
        (
            "~/.codebuddy",
            "~/Library/Application Support/CodeBuddy",
            "~/.config/codebuddy",
        ),
    ),
    "tongyi_lingma": AgentProfile(
        "tongyi_lingma",
        "通义灵码",
        (
            "~/.lingma",
            "~/Library/Application Support/Visual Studio Code/User/globalStorage/alibaba-cloud.tongyi-lingma",
            "~/.config/lingma",
        ),
        support_note="Lingma/Qoder CN extension storage has no stable public transcript schema; coverage is best effort",
    ),
    "comate": AgentProfile(
        "comate",
        "Baidu Comate",
        (
            "~/.comate",
            "~/Library/Application Support/Visual Studio Code/User/globalStorage/baidu.comate",
            "~/.config/comate",
        ),
        support_note="Comate has no stable public local transcript schema; coverage is best effort",
    ),
    "kimi_cli": AgentProfile(
        "kimi_cli",
        "Kimi Code",
        ("~/.kimi", "~/.config/kimi", "~/.local/share/kimi"),
        support_note="Kimi wire envelopes are decoded conservatively; unknown protocol events are skipped",
    ),
    "kimi_code": AgentProfile(
        "kimi_code",
        "Kimi Code",
        ("~/.kimi-code",),
        support_note="Kimi Code wire envelopes are decoded conservatively; unknown protocol events are skipped",
    ),
    "qwen_code": AgentProfile(
        "qwen_code",
        "Qwen Code",
        ("~/.qwen",),
        support_note="Qwen Code branch/checkpoint replay is not fully reconstructed; coverage is best effort",
    ),
    "gemini_cli": AgentProfile(
        "gemini_cli",
        "Gemini CLI",
        ("~/.gemini/tmp", "~/.gemini/chats"),
        support_note="Gemini rewind/mutation records are not fully replayed; coverage is best effort",
    ),
    "opencode": AgentProfile(
        "opencode",
        "OpenCode",
        ("~/.local/share/opencode", "~/.config/opencode"),
    ),
    "github_copilot": AgentProfile(
        "github_copilot",
        "GitHub Copilot CLI",
        ("~/.copilot/session-state",),
        support_note="Copilot event logs are decoded conservatively; coverage is best effort",
    ),
    "cline": AgentProfile(
        "cline",
        "Cline",
        (
            "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev",
            "~/Library/Application Support/Visual Studio Code/User/globalStorage/saoudrizwan.claude-dev",
        ),
        support_note="Cline task exports are recognized conservatively across VS Code hosts",
    ),
    "roo_code": AgentProfile(
        "roo_code",
        "Roo Code",
        (
            "~/Library/Application Support/Code/User/globalStorage/rooveterinaryinc.roo-cline",
            "~/Library/Application Support/Visual Studio Code/User/globalStorage/rooveterinaryinc.roo-cline",
        ),
        support_note="Roo Code task exports are recognized conservatively across VS Code hosts",
    ),
    "windsurf": AgentProfile(
        "windsurf",
        "Windsurf",
        ("~/.codeium/windsurf", "~/Library/Application Support/Windsurf"),
        support_note="Windsurf has no stable public transcript schema; coverage is best effort",
    ),
    "factory_droid": AgentProfile(
        "factory_droid",
        "Factory Droid",
        ("~/.factory/sessions", "~/.factory/projects"),
        support_note="Factory session formats are not treated as a stable public API",
    ),
}


class PortableAgentConnector:
    """Conservative adapter for product-scoped JSON/JSONL/SQLite session stores."""

    def __init__(
        self,
        key: str,
        label: str,
        roots: Sequence[Path],
        patterns: Sequence[str],
        max_files: int = 500,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_messages: int = 10_000,
        support_note: str = "",
        max_sqlite_tables: int = 12,
        max_sqlite_rows: Optional[int] = None,
    ) -> None:
        self.key = key
        self.label = label
        self.roots = list(roots)
        self.patterns = list(patterns)
        self.max_files = max(0, max_files)
        self.max_file_bytes = max_file_bytes
        self.max_messages = max(0, max_messages)
        self.support_note = support_note
        self.max_sqlite_tables = max(0, max_sqlite_tables)
        self.max_sqlite_rows = (
            max(0, max_sqlite_rows) if max_sqlite_rows is not None else None
        )

    def scan(self, window: TimeWindow) -> ConnectorResult:
        roots = [root for root in self.roots if root.exists()]
        if not roots:
            return ConnectorResult(
                coverage=[
                    SourceCoverage(
                        self.key,
                        "missing",
                        detail="{} configured roots were not found".format(self.label),
                    )
                ]
            )

        grouped: Dict[str, ConversationTrace] = {}
        files_seen = 0
        files_parsed = 0
        skipped_large = 0
        errors = 0
        omitted_untimestamped = 0
        malformed_records = 0
        limit_details: Set[str] = set()
        file_cap_hit = False
        message_cap_hit = False
        for path in self._candidate_files(roots, window):
            if files_seen >= self.max_files:
                file_cap_hit = True
                break
            files_seen += 1
            try:
                if path.stat().st_size > self.max_file_bytes:
                    skipped_large += 1
                    continue
                if path.suffix.lower() == ".jsonl":
                    outcome = _parse_jsonl_file(path, self.key, window)
                elif path.suffix.lower() == ".json":
                    outcome = _parse_json_file(path, self.key, window)
                elif path.suffix.lower() in {".sqlite", ".db", ".vscdb"}:
                    outcome = _parse_sqlite_file(
                        path,
                        self.key,
                        window,
                        max_tables=self.max_sqlite_tables,
                        max_rows=self.max_sqlite_rows,
                    )
                else:
                    continue
                files_parsed += 1
            except (OSError, ValueError, sqlite3.Error):
                errors += 1
                continue
            omitted_untimestamped += outcome.omitted_untimestamped
            malformed_records += outcome.malformed_records
            limit_details.update(outcome.limit_details)
            for conversation in outcome.conversations:
                existing = grouped.get(conversation.conversation_id)
                if existing is None:
                    grouped[conversation.conversation_id] = conversation
                else:
                    grouped[conversation.conversation_id] = _merge_same_source(
                        existing, conversation
                    )
            if sum(len(item.messages) for item in grouped.values()) > self.max_messages:
                grouped = _limit_grouped_messages(grouped, self.max_messages)
                message_cap_hit = True
                break

        conversations = list(grouped.values())
        messages = sum(len(item.messages) for item in conversations)
        degraded = bool(
            errors
            or skipped_large
            or omitted_untimestamped
            or malformed_records
            or limit_details
            or file_cap_hit
            or message_cap_hit
            or self.support_note
        )
        if (
            file_cap_hit
            or message_cap_hit
            or limit_details
            or omitted_untimestamped
            or malformed_records
            or skipped_large
        ):
            status = "partial"
        elif conversations and not degraded:
            status = "complete"
        elif conversations:
            status = "partial"
        elif errors:
            status = "error"
        else:
            status = "empty"
        detail = "{} read-only portable adapter: roots={}, candidate_files={}, parsed_files={}".format(
            self.label, len(roots), files_seen, files_parsed
        )
        if omitted_untimestamped:
            detail += (
                "; omitted {} message events with missing or unparseable timestamps"
            ).format(omitted_untimestamped)
        if malformed_records:
            detail += "; dropped {} malformed or undecodable JSONL records".format(
                malformed_records
            )
        if skipped_large:
            detail += "; skipped {} oversized files".format(skipped_large)
        if errors:
            detail += "; {} candidate files failed parsing".format(errors)
        if file_cap_hit:
            detail += "; file scan capped at {}".format(self.max_files)
        if message_cap_hit:
            detail += "; message scan capped at {}; excess messages omitted".format(
                self.max_messages
            )
        for limit_detail in sorted(limit_details):
            detail += "; {}".format(limit_detail)
        if self.support_note:
            detail += "; {}".format(self.support_note)
        return ConnectorResult(
            conversations=conversations,
            coverage=[
                SourceCoverage(
                    self.key,
                    status,
                    conversations=len(conversations),
                    messages=messages,
                    detail=detail,
                )
            ],
        )

    def _candidate_files(
        self, roots: Sequence[Path], window: TimeWindow
    ) -> Iterable[Path]:
        seen: Set[Path] = set()
        blocked_names = {
            "settings.json",
            "config.json",
            "auth.json",
            "credentials.json",
            "package.json",
            "manifest.json",
        }
        blocked_parts = {"cache", "cacheddata", "logs", "backups", "extensions"}
        for root in roots:
            for pattern in self.patterns:
                for path in root.glob(pattern):
                    if not path.is_file() or path in seen:
                        continue
                    seen.add(path)
                    if path.name.lower() in blocked_names:
                        continue
                    if any(part.lower() in blocked_parts for part in path.parts):
                        continue
                    # Transcript contents, not file mtime, define report membership.
                    # This preserves historical sessions that were resumed later.
                    yield path


def _parse_jsonl_file(path: Path, origin: str, window: TimeWindow) -> _ParseOutcome:
    grouped: Dict[str, List[Tuple[TraceMessage, Dict[str, Any]]]] = {}
    stats = {"untimestamped": 0}
    malformed_records = 0
    with path.open("rb") as file:
        for index, raw_line in enumerate(file):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                malformed_records += 1
                continue
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed_records += 1
                continue
            if not isinstance(value, dict):
                continue
            parsed = _parse_event(value, path, index, window, stats=stats)
            if parsed is None:
                continue
            message, metadata = parsed
            conversation_id = _conversation_id(value, metadata, path)
            grouped.setdefault(conversation_id, []).append((message, metadata))
    conversations = [
        _build_conversation(origin, conversation_id, entries, path)
        for conversation_id, entries in grouped.items()
        if entries
    ]
    if stats["untimestamped"] or malformed_records:
        for conversation in conversations:
            conversation.extra["full_transcript"] = False
            if stats["untimestamped"]:
                conversation.extra["omitted_untimestamped"] = stats["untimestamped"]
            if malformed_records:
                conversation.extra["malformed_records"] = malformed_records
    return _ParseOutcome(
        conversations=conversations,
        omitted_untimestamped=stats["untimestamped"],
        malformed_records=malformed_records,
    )


def _parse_json_file(path: Path, origin: str, window: TimeWindow) -> _ParseOutcome:
    value = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    documents = list(_conversation_documents(value))
    if not documents and isinstance(value, (dict, list)):
        documents = [value]
    result: List[ConversationTrace] = []
    omitted_untimestamped = 0
    for doc_index, document in enumerate(documents):
        if isinstance(document, dict):
            events = (
                document.get("messages")
                or document.get("events")
                or document.get("history")
            )
            metadata = document
        else:
            events = document
            metadata = {}
        if not isinstance(events, list):
            continue
        entries: List[Tuple[TraceMessage, Dict[str, Any]]] = []
        stats = {"untimestamped": 0}
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            parsed = _parse_event(
                event,
                path,
                index,
                window,
                stats=stats,
            )
            if parsed is not None:
                entries.append(parsed)
        omitted_untimestamped += stats["untimestamped"]
        if not entries:
            continue
        conversation_id = _conversation_id(
            metadata, metadata, path, suffix=str(doc_index)
        )
        conversation = _build_conversation(
            origin, conversation_id, entries, path, metadata
        )
        if stats["untimestamped"]:
            conversation.extra["full_transcript"] = False
            conversation.extra["omitted_untimestamped"] = stats["untimestamped"]
        result.append(conversation)
    return _ParseOutcome(
        conversations=result,
        omitted_untimestamped=omitted_untimestamped,
    )


def _parse_sqlite_file(
    path: Path,
    origin: str,
    window: TimeWindow,
    max_tables: int = 12,
    max_rows: Optional[int] = None,
) -> _ParseOutcome:
    grouped: Dict[str, List[Tuple[TraceMessage, Dict[str, Any]]]] = {}
    stats = {"untimestamped": 0}
    limit_details: List[str] = []
    with copied_sqlite_connection(path) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        safe_tables = [table for table in tables if not _is_sensitive_table(table)]
        table_map = {table.lower(): table for table in safe_tables}
        if {"session", "message", "part"}.issubset(table_map):
            return _parse_opencode_tables(
                connection,
                table_map,
                path,
                origin,
                window,
                max_rows=max_rows,
            )
        message_tables = []
        for table in safe_tables:
            table_name = table.lower()
            if not any(
                marker in table_name for marker in ("message", "event", "part", "chat")
            ) or any(marker in table_name for marker in ("cache", "log", "metric")):
                continue
            columns = _sqlite_table_columns(connection, table)
            if _is_generic_message_schema(columns):
                message_tables.append(table)
        selected_tables = message_tables[:max_tables]
        if len(message_tables) > len(selected_tables):
            limit_details.append(
                "SQLite table scan capped at {}; omitted {} message tables".format(
                    max_tables, len(message_tables) - len(selected_tables)
                )
            )
        row_limit = 5000 if max_rows is None else max_rows
        for table in selected_tables:
            rows, row_cap_hit = _sqlite_rows(connection, table, limit=row_limit)
            if row_cap_hit:
                limit_details.append(
                    "SQLite row scan capped at {} for table {}".format(row_limit, table)
                )
            for index, raw in enumerate(rows):
                expanded = _expand_json_columns(raw)
                parsed = _parse_event(
                    expanded,
                    path,
                    index,
                    window,
                    stats=stats,
                )
                if parsed is None:
                    continue
                message, metadata = parsed
                conversation_id = _conversation_id(
                    expanded, metadata, path, suffix=table
                )
                grouped.setdefault(conversation_id, []).append((message, metadata))
    conversations = [
        _build_conversation(origin, conversation_id, entries, path)
        for conversation_id, entries in grouped.items()
        if entries
    ]
    if limit_details or stats["untimestamped"]:
        for conversation in conversations:
            conversation.extra["full_transcript"] = False
            if limit_details:
                conversation.extra["connector_limit"] = True
            if stats["untimestamped"]:
                conversation.extra["omitted_untimestamped"] = stats["untimestamped"]
    return _ParseOutcome(
        conversations=conversations,
        omitted_untimestamped=stats["untimestamped"],
        limit_details=tuple(limit_details),
    )


def _parse_opencode_tables(
    connection: sqlite3.Connection,
    table_map: Dict[str, str],
    path: Path,
    origin: str,
    window: TimeWindow,
    max_rows: Optional[int] = None,
) -> _ParseOutcome:
    row_limits = {
        "session": 5000 if max_rows is None else max_rows,
        "message": 20000 if max_rows is None else max_rows,
        "part": 50000 if max_rows is None else max_rows,
    }
    session_rows, session_cap_hit = _sqlite_rows(
        connection, table_map["session"], limit=row_limits["session"]
    )
    message_rows, message_cap_hit = _sqlite_rows(
        connection, table_map["message"], limit=row_limits["message"]
    )
    part_rows, part_cap_hit = _sqlite_rows(
        connection, table_map["part"], limit=row_limits["part"]
    )
    limit_details = [
        "SQLite row scan capped at {} for table {}".format(
            row_limits[name], table_map[name]
        )
        for name, cap_hit in (
            ("session", session_cap_hit),
            ("message", message_cap_hit),
            ("part", part_cap_hit),
        )
        if cap_hit
    ]

    sessions: Dict[str, Dict[str, Any]] = {}
    for row in session_rows:
        expanded = _expand_json_columns(row)
        session_id = str(expanded.get("id") or expanded.get("session_id") or "")
        if session_id:
            sessions[session_id] = expanded

    messages: Dict[str, Dict[str, Any]] = {}
    for row in message_rows:
        expanded = _expand_json_columns(row)
        message_id = str(expanded.get("id") or expanded.get("message_id") or "")
        if message_id:
            messages[message_id] = expanded

    grouped: Dict[str, List[Tuple[TraceMessage, Dict[str, Any]]]] = {}
    omitted_untimestamped = 0
    for index, row in enumerate(part_rows):
        part = _expand_json_columns(row)
        data = part.get("data") if isinstance(part.get("data"), dict) else part
        message_id = str(
            part.get("message_id")
            or part.get("messageID")
            or data.get("messageID")
            or ""
        )
        message_meta = messages.get(message_id, {})
        session_id = str(
            part.get("session_id")
            or part.get("sessionID")
            or data.get("sessionID")
            or message_meta.get("session_id")
            or message_meta.get("sessionID")
            or ""
        )
        if not session_id:
            continue
        role = _normalize_role(
            message_meta.get("role")
            or (
                message_meta.get("data", {}).get("role")
                if isinstance(message_meta.get("data"), dict)
                else None
            )
        )
        part_type = str(data.get("type") or part.get("type") or "text").lower()
        if part_type in {
            "reasoning",
            "thinking",
            "snapshot",
            "step-start",
            "step-finish",
        }:
            continue
        kind = "message"
        content = ""
        if part_type == "text":
            content = _content_to_text(data.get("text") or data.get("content"))
        elif part_type == "tool":
            role = "tool"
            state = data.get("state") if isinstance(data.get("state"), dict) else {}
            tool_name = sanitize_full_text(
                data.get("tool") or data.get("name") or "tool"
            )
            output = state.get("output") or state.get("result")
            if output not in (None, "", [], {}):
                kind = "tool_output"
                content = "{}: {}".format(tool_name, _content_to_text(output))
            else:
                kind = "tool_call"
                content = "{}({})".format(
                    tool_name, _content_to_text(state.get("input") or data.get("input"))
                )
        elif part_type in {"subtask", "patch"}:
            role = role if role in {"user", "assistant"} else "assistant"
            content = _content_to_text(data)
        else:
            continue
        content = sanitize_full_text(content)
        if role not in {"user", "assistant", "tool"} or not content:
            continue
        timestamp = (
            part.get("time_created")
            or part.get("created_at")
            or data.get("time")
            or message_meta.get("time_created")
            or message_meta.get("created_at")
        )
        if isinstance(timestamp, dict):
            timestamp = (
                timestamp.get("start")
                or timestamp.get("created")
                or timestamp.get("end")
            )
        if timestamp is None:
            omitted_untimestamped += 1
            continue
        parsed_timestamp = parse_timestamp(timestamp, window.timezone)
        if parsed_timestamp is None:
            omitted_untimestamped += 1
            continue
        if not window.start <= parsed_timestamp < window.end:
            continue
        part_id = (
            part.get("id") or data.get("id") or "{}:part:{}".format(message_id, index)
        )
        trace_message = TraceMessage(
            message_id=str(part_id),
            role=role,
            content=content,
            occurred_at=to_iso(timestamp, window.timezone),
            kind=kind,
            parent_id=message_id or None,
        )
        metadata = dict(sessions.get(session_id, {}))
        metadata.update(message_meta)
        grouped.setdefault(session_id, []).append((trace_message, metadata))

    result = []
    for session_id, entries in grouped.items():
        session_meta = sessions.get(session_id, {})
        conversation = _build_conversation(
            origin, session_id, entries, path, document_metadata=session_meta
        )
        conversation.extra["evidence"] = "opencode_sqlite"
        conversation.extra["adapter"] = "opencode-sqlite-v1"
        if limit_details or omitted_untimestamped:
            conversation.extra["full_transcript"] = False
        if limit_details:
            conversation.extra["connector_limit"] = True
        if omitted_untimestamped:
            conversation.extra["omitted_untimestamped"] = omitted_untimestamped
        result.append(conversation)
    return _ParseOutcome(
        conversations=result,
        omitted_untimestamped=omitted_untimestamped,
        limit_details=tuple(limit_details),
    )


def _sqlite_rows(
    connection: sqlite3.Connection, table: str, limit: int
) -> Tuple[List[Dict[str, Any]], bool]:
    columns = _sqlite_table_columns(connection, table)
    if not columns:
        return [], False
    rows = connection.execute(
        "SELECT * FROM {} LIMIT {}".format(_quote_identifier(table), int(limit) + 1)
    ).fetchall()
    cap_hit = len(rows) > limit
    return [dict(zip(columns, row)) for row in rows[:limit]], cap_hit


def _sqlite_table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    return [
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info({})".format(_quote_identifier(table))
        ).fetchall()
    ]


def _is_sensitive_table(table: str) -> bool:
    """Reject credential-bearing tables before any row-level SELECT is issued."""
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", table)
    tokens = set(re.findall(r"[a-z0-9]+", snake_case.lower()))
    return bool(tokens & _SENSITIVE_SQLITE_TABLE_TOKENS)


def _is_generic_message_schema(columns: Sequence[str]) -> bool:
    """Require a recognizable transcript schema before reading a generic table."""
    normalized = {re.sub(r"[^a-z0-9]", "", column.lower()) for column in columns}
    has_timestamp = bool(normalized & _SQLITE_TIMESTAMP_COLUMNS)
    direct_message = bool(normalized & _SQLITE_CONTENT_COLUMNS) and bool(
        normalized & _SQLITE_ROLE_COLUMNS
    )
    json_message = bool(normalized & _SQLITE_JSON_COLUMNS) and bool(
        normalized & _SQLITE_CONVERSATION_COLUMNS
    )
    return has_timestamp and (direct_message or json_message)


def _conversation_documents(value: Any, depth: int = 0) -> Iterable[Any]:
    if depth > 4:
        return
    if isinstance(value, dict):
        if any(
            isinstance(value.get(key), list)
            for key in ("messages", "events", "history")
        ):
            yield value
            return
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _conversation_documents(child, depth + 1)
    elif isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            roles = sum(bool(_event_role(item)) for item in value)
            if roles:
                yield value
                return
        for child in value:
            if isinstance(child, (dict, list)):
                yield from _conversation_documents(child, depth + 1)


def _parse_event(
    event: Dict[str, Any],
    path: Path,
    index: int,
    window: TimeWindow,
    stats: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[TraceMessage, Dict[str, Any]]]:
    expanded = _expand_json_columns(event)
    nested = (
        expanded.get("message") if isinstance(expanded.get("message"), dict) else {}
    )
    wire_type = str(nested.get("type") or "").lower().replace("_", "").replace("-", "")
    wire_payload = (
        nested.get("payload") if isinstance(nested.get("payload"), dict) else {}
    )
    if wire_type in {
        "thinkpart",
        "thinkingpart",
        "statusupdate",
        "stepbegin",
        "stepend",
        "compaction",
    }:
        return None
    if wire_type in {"textpart", "assistantmessage"}:
        nested = {
            **wire_payload,
            "role": "assistant",
            "type": "message",
            "content": wire_payload.get("text") or wire_payload.get("content"),
        }
    elif wire_type in {"toolcall", "toolcallpart"}:
        nested = {**wire_payload, "role": "tool", "type": "tool_call"}
    elif wire_type in {"toolresult", "tooloutput"}:
        nested = {**wire_payload, "role": "tool", "type": "tool_result"}
    elif wire_type in {"turnbegin", "userinput", "usermessage"}:
        nested = {
            **wire_payload,
            "role": "user",
            "type": "message",
            "content": wire_payload.get("prompt")
            or wire_payload.get("text")
            or wire_payload.get("message"),
        }
    role = _normalize_role(
        nested.get("role")
        or expanded.get("role")
        or expanded.get("author")
        or expanded.get("type")
    )
    event_type = str(nested.get("type") or expanded.get("type") or "").lower()
    if (
        event_type in {"thinking", "reasoning", "system", "developer"}
        or role == "system"
    ):
        return None

    kind = "message"
    if event_type in {"tool_use", "tool_call", "function_call", "command"}:
        role = "tool"
        kind = "tool_call"
    elif event_type in {
        "tool_result",
        "tool_output",
        "function_call_output",
        "command_result",
    }:
        role = "tool"
        kind = "tool_output"
    if role not in {"user", "assistant", "tool"}:
        return None

    content_value = _first_value(
        nested,
        ("content", "text", "body", "output", "result", "parts"),
    )
    if content_value is None:
        content_value = _first_value(
            expanded,
            ("content", "text", "body", "output", "result", "parts", "input"),
        )
    content = _content_to_text(content_value)
    if kind == "tool_call":
        name = sanitize_full_text(
            expanded.get("name") or nested.get("name") or expanded.get("tool") or "tool"
        )
        content = "{}({})".format(name, content)
    content = sanitize_full_text(content)
    if not content:
        return None

    timestamp = _event_timestamp(expanded) or _event_timestamp(nested)
    if timestamp is None:
        if stats is not None:
            stats["untimestamped"] = stats.get("untimestamped", 0) + 1
        return None
    parsed_timestamp = parse_timestamp(timestamp, window.timezone)
    if parsed_timestamp is None:
        if stats is not None:
            stats["untimestamped"] = stats.get("untimestamped", 0) + 1
        return None
    if not window.start <= parsed_timestamp < window.end:
        return None
    message_id = _first_value(
        expanded,
        ("id", "uuid", "message_id", "messageId", "call_id", "tool_use_id"),
    )
    if not message_id:
        seed = "{}:{}:{}:{}".format(path, index, role, content)
        message_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    parent_id = _first_value(
        expanded, ("parent_id", "parentId", "parentUuid", "parent_message_id")
    )
    message = TraceMessage(
        message_id=str(message_id),
        role=role,
        content=content,
        occurred_at=to_iso(timestamp, window.timezone),
        kind=kind,
        parent_id=str(parent_id) if parent_id else None,
        active_branch=not bool(expanded.get("inactive") or expanded.get("isSidechain")),
    )
    return message, expanded


def _build_conversation(
    origin: str,
    conversation_id: str,
    entries: Sequence[Tuple[TraceMessage, Dict[str, Any]]],
    path: Path,
    document_metadata: Optional[Dict[str, Any]] = None,
) -> ConversationTrace:
    metadata: Dict[str, Any] = dict(document_metadata or {})
    for _, item in entries:
        for key in (
            "cwd",
            "workspace",
            "project_path",
            "projectPath",
            "title",
            "name",
            "model",
            "gitBranch",
        ):
            if item.get(key) not in (None, "", [], {}):
                metadata[key] = item[key]
    cwd = str(
        metadata.get("cwd")
        or metadata.get("workspace")
        or metadata.get("project_path")
        or metadata.get("projectPath")
        or ""
    )
    title = sanitize_full_text(metadata.get("title") or metadata.get("name") or "")
    messages = _dedupe_messages([message for message, _ in entries])
    return ConversationTrace(
        origin=origin,
        conversation_id=conversation_id,
        workspace=Path(cwd.replace("file://", "")).name if cwd else path.parent.name,
        title=title,
        messages=messages,
        paths=[str(path)],
        confidence="high",
        extra={
            "cwd": cwd,
            "model": metadata.get("model"),
            "git_branch": metadata.get("gitBranch"),
            "evidence": "portable_agent_session",
            "full_transcript": True,
            "adapter": "portable-v1",
        },
    )


def _conversation_id(
    value: Dict[str, Any],
    metadata: Dict[str, Any],
    path: Path,
    suffix: str = "",
) -> str:
    candidate = _first_value(
        value,
        (
            "session_id",
            "sessionId",
            "conversation_id",
            "conversationId",
            "thread_id",
            "threadId",
            "chat_id",
            "chatId",
            "task_id",
            "taskId",
        ),
    ) or _first_value(
        metadata,
        (
            "session_id",
            "sessionId",
            "conversation_id",
            "conversationId",
            "thread_id",
            "threadId",
            "chat_id",
            "chatId",
            "task_id",
            "taskId",
        ),
    )
    if candidate:
        return str(candidate)
    seed = "{}:{}".format(path, suffix)
    return "file-{}".format(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16])


def _event_role(value: Dict[str, Any]) -> str:
    nested = value.get("message") if isinstance(value.get("message"), dict) else {}
    return _normalize_role(
        nested.get("role")
        or value.get("role")
        or value.get("author")
        or value.get("type")
    )


def _normalize_role(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("role") or value.get("type") or value.get("name")
    role = str(value or "").lower()
    if role in {"user", "human", "prompt", "request"}:
        return "user"
    if role in {"assistant", "agent", "model", "ai", "response", "gemini"}:
        return "assistant"
    if role.startswith("tool") or role in {"function", "command"}:
        return "tool"
    return role


def _event_timestamp(value: Dict[str, Any]) -> Any:
    return _first_value(
        value,
        (
            "timestamp",
            "created_at",
            "createdAt",
            "updated_at",
            "updatedAt",
            "time",
            "ts",
            "date",
        ),
    )


def _content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and str(item.get("type") or "").lower() in {
                "thinking",
                "reasoning",
                "image",
            }:
                continue
            parts.append(_content_to_text(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "body", "output", "result"):
            if key in value:
                return _content_to_text(value[key])
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _expand_json_columns(value: Dict[str, Any]) -> Dict[str, Any]:
    expanded = dict(value)
    for key, item in list(value.items()):
        if not isinstance(item, (str, bytes)):
            continue
        text = (
            item.decode("utf-8", errors="ignore") if isinstance(item, bytes) else item
        )
        if key.lower() not in {"data", "json", "payload", "metadata", "message"}:
            continue
        if not text.lstrip().startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            for parsed_key, parsed_value in parsed.items():
                expanded.setdefault(parsed_key, parsed_value)
        expanded[key] = parsed
    return expanded


def _first_value(value: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if value.get(key) not in (None, "", [], {}):
            return value[key]
    return None


def _quote_identifier(value: str) -> str:
    return '"{}"'.format(value.replace('"', '""'))


def _dedupe_messages(messages: Sequence[TraceMessage]) -> List[TraceMessage]:
    seen: Set[Tuple[str, str]] = set()
    result = []
    for message in sorted(messages, key=lambda item: item.occurred_at or ""):
        key = (message.message_id, message.kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


def _merge_same_source(
    left: ConversationTrace, right: ConversationTrace
) -> ConversationTrace:
    left.messages = _dedupe_messages(left.messages + right.messages)
    left.paths = sorted(set(left.paths) | set(right.paths))
    if not left.title:
        left.title = right.title
    if left.workspace in {"", "unknown"}:
        left.workspace = right.workspace
    left.extra.update({key: value for key, value in right.extra.items() if value})
    return left


def _limit_grouped_messages(
    grouped: Dict[str, ConversationTrace], limit: int
) -> Dict[str, ConversationTrace]:
    """Preserve encounter order while enforcing a strict connector-wide cap."""
    remaining = max(0, limit)
    result: Dict[str, ConversationTrace] = {}
    for conversation_id, conversation in grouped.items():
        if remaining <= 0:
            break
        messages = conversation.messages[:remaining]
        if not messages:
            continue
        extra = dict(conversation.extra)
        if len(messages) < len(conversation.messages):
            extra["full_transcript"] = False
            extra["message_limit_truncated"] = True
        result[conversation_id] = replace(
            conversation,
            messages=messages,
            extra=extra,
        )
        remaining -= len(messages)
    return result


def profile_from_config(key: str, value: Dict[str, Any]) -> AgentProfile:
    base = KNOWN_AGENT_PROFILES.get(key)
    label = str(value.get("label") or (base.label if base else key))
    roots = tuple(
        str(item)
        for item in value.get("roots", base.roots if base else [])
        if str(item)
    )
    env_candidates = {
        "qwen_code": ("QWEN_RUNTIME_DIR", "QWEN_HOME"),
        "kimi_code": ("KIMI_CODE_HOME",),
        "kimi_cli": ("KIMI_SHARE_DIR",),
    }.get(key, ())
    env_roots = tuple(
        os.environ[name] for name in env_candidates if os.environ.get(name)
    )
    if env_roots:
        roots = tuple(dict.fromkeys(env_roots + roots))
    patterns = tuple(
        str(item)
        for item in value.get(
            "patterns", base.patterns if base else AgentProfile("", "", ()).patterns
        )
        if str(item)
    )
    support_note = str(
        value.get("support_note")
        or (
            base.support_note
            if base
            else "Generic profile uses conservative schema inference"
        )
    )
    return AgentProfile(
        key=key,
        label=label,
        roots=roots,
        patterns=patterns,
        support_note=support_note,
    )
