from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from worktrace_agent.connectors.sqlite_utils import readonly_sqlite_connection
from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
)
from worktrace_agent.text import sanitize_full_text, unique_preserving_order
from worktrace_agent.window import TimeWindow, parse_timestamp, to_iso


class CodexCliConnector:
    key = "codex_cli"

    def __init__(
        self,
        root: Path,
        include_session_jsonl: bool = True,
        excluded_conversation_ids: Optional[Iterable[str]] = None,
        max_jsonl_files: int = 2000,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_messages: int = 50_000,
        max_thread_rows: int = 10_000,
    ) -> None:
        self.root = root
        self.include_session_jsonl = include_session_jsonl
        self.excluded_conversation_ids: Set[str] = {
            str(value) for value in (excluded_conversation_ids or []) if str(value)
        }
        self.max_jsonl_files = max(0, max_jsonl_files)
        self.max_file_bytes = max(0, max_file_bytes)
        self.max_messages = max(0, max_messages)
        self.max_thread_rows = max(0, max_thread_rows)

    def scan(self, window: TimeWindow) -> ConnectorResult:
        if not self.root.exists():
            return ConnectorResult(
                coverage=[
                    SourceCoverage(
                        "codex", "missing", detail="Codex home directory not found"
                    )
                ]
            )

        conversations: List[ConversationTrace] = []
        database_errors = 0
        database_row_cap_hit = False
        for db_path in self._state_db_candidates():
            database_stats = {"errors": 0, "row_cap_hit": 0}
            conversations.extend(
                self._scan_threads_db(db_path, window, stats=database_stats)
            )
            database_errors += database_stats["errors"]
            database_row_cap_hit = bool(
                database_row_cap_hit or database_stats["row_cap_hit"]
            )

        jsonl_files_seen = 0
        jsonl_files_parsed = 0
        skipped_large = 0
        file_errors = 0
        malformed_records = 0
        omitted_untimestamped = 0
        file_cap_hit = False
        message_cap_hit = False
        messages_kept = 0
        for source_kind, path in self._jsonl_candidates(window):
            if jsonl_files_seen >= self.max_jsonl_files:
                file_cap_hit = True
                break
            jsonl_files_seen += 1
            try:
                if path.stat().st_size > self.max_file_bytes:
                    skipped_large += 1
                    continue
            except OSError:
                file_errors += 1
                continue

            file_stats = {
                "malformed_records": 0,
                "omitted_untimestamped": 0,
                "message_cap_hit": 0,
            }
            remaining_messages = max(0, self.max_messages - messages_kept)
            try:
                if source_kind == "history":
                    found = self._scan_history_jsonl(
                        path,
                        window,
                        stats=file_stats,
                        max_messages=remaining_messages,
                    )
                elif source_kind == "index":
                    found = self._scan_session_index(path, window, stats=file_stats)
                else:
                    conversation = self._scan_session_jsonl(
                        path,
                        window,
                        stats=file_stats,
                        max_messages=remaining_messages,
                    )
                    found = [conversation] if conversation is not None else []
            except (OSError, ValueError):
                malformed_records += file_stats["malformed_records"]
                omitted_untimestamped += file_stats["omitted_untimestamped"]
                file_errors += 1
                continue

            jsonl_files_parsed += 1
            malformed_records += file_stats["malformed_records"]
            omitted_untimestamped += file_stats["omitted_untimestamped"]
            conversations.extend(found)
            messages_kept += sum(len(item.messages) for item in found)
            if file_stats["message_cap_hit"]:
                message_cap_hit = True
                break

        before_exclusion = len(conversations)
        conversations = [
            item
            for item in conversations
            if item.conversation_id not in self.excluded_conversation_ids
        ]
        excluded_count = before_exclusion - len(conversations)
        transcript_count = sum(
            1 for item in conversations if item.extra.get("full_transcript")
        )
        session_transcript_count = sum(
            1
            for item in conversations
            if item.extra.get("evidence") == "codex_session_jsonl"
        )

        message_count = sum(len(item.messages) for item in conversations)
        degraded = bool(
            database_errors
            or file_errors
            or skipped_large
            or malformed_records
            or omitted_untimestamped
            or file_cap_hit
            or message_cap_hit
            or database_row_cap_hit
        )
        if transcript_count and not degraded:
            status = "complete"
            detail = "Full Codex JSONL transcripts found; SQLite and indexes supply metadata only"
        elif conversations:
            status = "partial"
            if session_transcript_count:
                detail = "Codex JSONL transcripts were read with incomplete coverage"
            else:
                detail = "Only Codex metadata/history was found; full JSONL transcripts are unavailable"
        elif file_errors or database_errors or malformed_records:
            status = "error"
            detail = "Codex sources were found but no usable records could be read"
        elif (
            skipped_large
            or omitted_untimestamped
            or file_cap_hit
            or message_cap_hit
            or database_row_cap_hit
        ):
            status = "partial"
            detail = "Codex sources were found but configured limits or missing timestamps omitted all records"
        else:
            status = "empty"
            detail = "No Codex messages were found in the selected day"
        detail += "; JSONL candidates={}, parsed={}".format(
            jsonl_files_seen, jsonl_files_parsed
        )
        if database_errors:
            detail += "; {} state databases failed parsing".format(database_errors)
        if database_row_cap_hit:
            detail += "; threads database row scan capped at {}".format(
                self.max_thread_rows
            )
        if file_errors:
            detail += "; {} JSONL files could not be read".format(file_errors)
        if skipped_large:
            detail += "; skipped {} oversized JSONL files".format(skipped_large)
        if malformed_records:
            detail += "; dropped {} malformed or undecodable JSONL records".format(
                malformed_records
            )
        if omitted_untimestamped:
            detail += (
                "; omitted {} message or index records with missing or "
                "unparseable timestamps"
            ).format(omitted_untimestamped)
        if file_cap_hit:
            detail += "; JSONL file scan capped at {}".format(self.max_jsonl_files)
        if message_cap_hit:
            detail += "; message scan capped at {}; excess messages omitted".format(
                self.max_messages
            )
        if excluded_count:
            detail += "; excluded {} active WorkTrace conversation records".format(
                excluded_count
            )
        return ConnectorResult(
            conversations=conversations,
            coverage=[
                SourceCoverage(
                    source="codex",
                    status=status,
                    conversations=len(conversations),
                    messages=message_count,
                    detail=detail,
                )
            ],
        )

    def _state_db_candidates(self) -> List[Path]:
        candidates = [
            self.root / "state_5.sqlite",
            self.root / "sqlite" / "state_5.sqlite",
        ]
        return unique_preserving_order([path for path in candidates if path.exists()])

    def _scan_threads_db(
        self,
        db_path: Path,
        window: TimeWindow,
        stats: Optional[Dict[str, int]] = None,
    ) -> List[ConversationTrace]:
        try:
            with readonly_sqlite_connection(db_path) as connection:
                columns = self._table_columns(connection, "threads")
                if not columns:
                    return []
                time_col = (
                    "updated_at_ms" if "updated_at_ms" in columns else "updated_at"
                )
                params = (
                    window.start_epoch_ms
                    if time_col.endswith("_ms")
                    else window.start_epoch,
                    window.end_epoch_ms
                    if time_col.endswith("_ms")
                    else window.end_epoch,
                )
                wanted = [
                    "id",
                    "cwd",
                    "title",
                    "first_user_message",
                    "preview",
                    "updated_at",
                    "updated_at_ms",
                    "model",
                    "reasoning_effort",
                    "source",
                    "thread_source",
                    "git_branch",
                ]
                select_cols = [item for item in wanted if item in columns]
                query = """
                    SELECT {}
                    FROM threads
                    WHERE {} >= ? AND {} < ?
                    ORDER BY {} DESC
                    LIMIT ?
                """.format(", ".join(select_cols), time_col, time_col, time_col)
                rows = connection.execute(
                    query, params + (self.max_thread_rows + 1,)
                ).fetchall()
                if len(rows) > self.max_thread_rows:
                    if stats is not None:
                        stats["row_cap_hit"] = 1
                    rows = rows[: self.max_thread_rows]
        except (OSError, sqlite3.Error):
            if stats is not None:
                stats["errors"] = stats.get("errors", 0) + 1
            return []

        conversations: List[ConversationTrace] = []
        for row in rows:
            data = dict(zip(select_cols, row))
            conversation_id = str(data.get("id") or "")
            if not conversation_id:
                continue
            cwd = str(data.get("cwd") or "")
            timestamp = data.get("updated_at_ms") or data.get("updated_at")
            conversations.append(
                ConversationTrace(
                    origin="codex",
                    conversation_id=conversation_id,
                    workspace=Path(cwd).name if cwd else "codex",
                    title=sanitize_full_text(data.get("title") or ""),
                    messages=[],
                    paths=[str(db_path)],
                    confidence="medium",
                    extra={
                        "cwd": cwd,
                        "model": data.get("model"),
                        "reasoning_effort": data.get("reasoning_effort"),
                        "source": data.get("source"),
                        "thread_source": data.get("thread_source"),
                        "git_branch": data.get("git_branch"),
                        "updated_at": to_iso(timestamp, window.timezone),
                        "evidence": "codex_threads_db",
                        "metadata_only": True,
                        "discovery_note": sanitize_full_text(
                            data.get("first_user_message") or data.get("preview") or ""
                        ),
                    },
                )
            )
        return conversations

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> List[str]:
        return [
            row[1] for row in connection.execute("PRAGMA table_info({})".format(table))
        ]

    def _scan_history_jsonl(
        self,
        path: Path,
        window: TimeWindow,
        stats: Optional[Dict[str, int]] = None,
        max_messages: Optional[int] = None,
    ) -> List[ConversationTrace]:
        grouped: Dict[str, ConversationTrace] = {}
        messages_kept = 0
        limit = self.max_messages if max_messages is None else max(0, max_messages)
        for index, event in enumerate(_read_jsonl(path, stats=stats)):
            timestamp = event.get("ts") or event.get("timestamp")
            content = sanitize_full_text(event.get("text") or event.get("message"))
            if not content:
                continue
            parsed_timestamp = parse_timestamp(timestamp, window.timezone)
            if parsed_timestamp is None:
                if stats is not None:
                    stats["omitted_untimestamped"] = (
                        stats.get("omitted_untimestamped", 0) + 1
                    )
                continue
            if not window.start <= parsed_timestamp < window.end:
                continue
            if messages_kept >= limit:
                if stats is not None:
                    stats["message_cap_hit"] = 1
                continue
            conversation_id = str(event.get("session_id") or "history-{}".format(index))
            cwd = str(event.get("cwd") or "")
            conversation = grouped.setdefault(
                conversation_id,
                ConversationTrace(
                    origin="codex",
                    conversation_id=conversation_id,
                    workspace=Path(cwd).name if cwd else "codex-history",
                    paths=[str(path)],
                    confidence="medium",
                    extra={
                        "cwd": cwd,
                        "evidence": "codex_history_jsonl",
                        "metadata_only": True,
                    },
                ),
            )
            conversation.messages.append(
                TraceMessage(
                    message_id=str(
                        event.get("id")
                        or "{}:history:{}".format(conversation_id, index)
                    ),
                    role="user",
                    content=content,
                    occurred_at=to_iso(timestamp, window.timezone),
                    extra={"metadata_fallback": True},
                )
            )
            messages_kept += 1
        return list(grouped.values())

    def _scan_session_index(
        self,
        path: Path,
        window: TimeWindow,
        stats: Optional[Dict[str, int]] = None,
    ) -> List[ConversationTrace]:
        conversations: List[ConversationTrace] = []
        for event in _read_jsonl(path, stats=stats):
            timestamp = event.get("updated_at") or event.get("created_at")
            conversation_id = str(event.get("id") or event.get("session_id") or "")
            if not conversation_id:
                continue
            parsed_timestamp = parse_timestamp(timestamp, window.timezone)
            if parsed_timestamp is None:
                if stats is not None:
                    stats["omitted_untimestamped"] = (
                        stats.get("omitted_untimestamped", 0) + 1
                    )
                continue
            if not window.start <= parsed_timestamp < window.end:
                continue
            conversations.append(
                ConversationTrace(
                    origin="codex",
                    conversation_id=conversation_id,
                    workspace="codex",
                    title=sanitize_full_text(
                        event.get("thread_name") or event.get("title") or ""
                    ),
                    paths=[str(path)],
                    confidence="medium",
                    extra={
                        "updated_at": to_iso(timestamp, window.timezone),
                        "evidence": "codex_session_index",
                        "metadata_only": True,
                    },
                )
            )
        return conversations

    def _jsonl_candidates(self, window: TimeWindow) -> Iterable[Tuple[str, Path]]:
        history = self.root / "history.jsonl"
        if history.exists():
            yield "history", history
        session_index = self.root / "session_index.jsonl"
        if session_index.exists():
            yield "index", session_index
        if self.include_session_jsonl:
            for path in self._session_jsonl_candidates(window):
                yield "session", path

    def _session_jsonl_candidates(self, window: TimeWindow) -> Iterable[Path]:
        sessions_root = self.root / "sessions"
        archived_root = self.root / "archived_sessions"
        roots = [sessions_root, archived_root]
        seen: Set[Path] = set()

        # Modern Codex stores sessions in YYYY/MM/DD. Prefer the selected period
        # so a bounded scan cannot be consumed by older history first.
        selected_dates = []
        cursor = window.start
        while cursor < window.end:
            selected_dates.append(cursor.date())
            cursor += timedelta(days=1)
        if sessions_root.exists():
            for selected_date in selected_dates:
                day_root = sessions_root.joinpath(
                    "{:04d}".format(selected_date.year),
                    "{:02d}".format(selected_date.month),
                    "{:02d}".format(selected_date.day),
                )
                if not day_root.exists():
                    continue
                for path in day_root.rglob("*.jsonl"):
                    if path not in seen:
                        seen.add(path)
                        yield path
        if archived_root.exists():
            for selected_date in selected_dates:
                for path in archived_root.glob(
                    "*{}*.jsonl".format(selected_date.isoformat())
                ):
                    if path not in seen:
                        seen.add(path)
                        yield path

        # Bounded caller consumption makes this legacy-layout fallback safe.
        # Transcript timestamps, rather than mutable file mtimes, decide whether
        # records belong to the requested period.
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if path in seen:
                    continue
                seen.add(path)
                yield path

    def _scan_session_jsonl(
        self,
        path: Path,
        window: TimeWindow,
        stats: Optional[Dict[str, int]] = None,
        max_messages: Optional[int] = None,
    ) -> Optional[ConversationTrace]:
        meta: Dict[str, Any] = {}
        response_messages: List[TraceMessage] = []
        fallback_messages: List[TraceMessage] = []
        tool_messages: List[TraceMessage] = []

        response_overflow = False
        fallback_overflow = False
        tool_overflow = False
        limit = self.max_messages if max_messages is None else max(0, max_messages)
        channel_limit = limit + 1

        for index, event in enumerate(_read_jsonl(path, stats=stats)):
            payload = (
                event.get("payload") if isinstance(event.get("payload"), dict) else {}
            )
            timestamp = event.get("timestamp") or payload.get("timestamp")
            if event.get("type") == "session_meta" and payload:
                meta.update(payload)
                for key in (
                    "base_instructions",
                    "developer_instructions",
                    "instructions",
                ):
                    meta.pop(key, None)
                continue
            parsed_timestamp = parse_timestamp(timestamp, window.timezone)
            if parsed_timestamp is None:
                if _event_has_reportable_message(payload, window, index, path):
                    if stats is not None:
                        stats["omitted_untimestamped"] = (
                            stats.get("omitted_untimestamped", 0) + 1
                        )
                continue
            if not window.start <= parsed_timestamp < window.end:
                continue

            message = _response_item_message(payload, timestamp, window, index, path)
            if message is not None:
                if message.kind == "message":
                    if len(response_messages) < channel_limit:
                        response_messages.append(message)
                    else:
                        response_overflow = True
                else:
                    if len(tool_messages) < channel_limit:
                        tool_messages.append(message)
                    else:
                        tool_overflow = True
                continue
            fallback = _event_message(payload, timestamp, window, index, path)
            if fallback is not None:
                if len(fallback_messages) < channel_limit:
                    fallback_messages.append(fallback)
                else:
                    fallback_overflow = True

        chat_messages = response_messages or fallback_messages
        messages = _dedupe_messages(chat_messages + tool_messages)
        chosen_chat_overflow = (
            response_overflow if response_messages else fallback_overflow
        )
        if chosen_chat_overflow or tool_overflow or len(messages) > limit:
            if stats is not None:
                stats["message_cap_hit"] = 1
            messages = messages[:limit]
        if not messages:
            return None

        conversation_id = str(
            meta.get("session_id") or meta.get("id") or _session_id_from_name(path)
        )
        cwd = str(meta.get("cwd") or "")
        full_transcript = not bool(
            (stats or {}).get("malformed_records")
            or (stats or {}).get("omitted_untimestamped")
            or (stats or {}).get("message_cap_hit")
        )
        extra = {
            "cwd": cwd,
            "model": meta.get("model"),
            "source": meta.get("source"),
            "thread_source": meta.get("thread_source"),
            "evidence": "codex_session_jsonl",
            "full_transcript": full_transcript,
        }
        if stats is not None and stats.get("malformed_records"):
            extra["malformed_records"] = stats["malformed_records"]
        if stats is not None and stats.get("omitted_untimestamped"):
            extra["omitted_untimestamped"] = stats["omitted_untimestamped"]
        if stats is not None and stats.get("message_cap_hit"):
            extra["message_cap_hit"] = True
        return ConversationTrace(
            origin="codex",
            conversation_id=conversation_id,
            workspace=Path(cwd).name if cwd else "codex",
            title=sanitize_full_text(meta.get("title") or ""),
            messages=messages,
            paths=[str(path)],
            confidence="high",
            extra=extra,
        )


def _read_jsonl(
    path: Path, stats: Optional[Dict[str, int]] = None
) -> Iterable[Dict[str, Any]]:
    with path.open("rb") as file:
        for raw_line in file:
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                if stats is not None:
                    stats["malformed_records"] = stats.get("malformed_records", 0) + 1
                continue
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                if stats is not None:
                    stats["malformed_records"] = stats.get("malformed_records", 0) + 1
                continue
            if isinstance(value, dict):
                yield value
            elif stats is not None:
                stats["malformed_records"] = stats.get("malformed_records", 0) + 1


def _event_has_reportable_message(
    payload: Dict[str, Any], window: TimeWindow, index: int, path: Path
) -> bool:
    probe_timestamp = window.start.isoformat()
    return bool(
        _response_item_message(payload, probe_timestamp, window, index, path)
        or _event_message(payload, probe_timestamp, window, index, path)
    )


def _response_item_message(
    payload: Dict[str, Any],
    timestamp: Any,
    window: TimeWindow,
    index: int,
    path: Path,
) -> Optional[TraceMessage]:
    payload_type = payload.get("type")
    role = str(payload.get("role") or "")
    if payload_type == "message" and role in {"user", "assistant"}:
        content = _content_to_text(payload.get("content"))
        if role == "user":
            content = _clean_codex_user_text(content)
        else:
            content = sanitize_full_text(content)
        if not content:
            return None
        return _make_message(
            payload, role, content, timestamp, window, index, path, "message"
        )
    if payload_type == "function_call":
        name = sanitize_full_text(payload.get("name") or "tool")
        arguments = sanitize_full_text(payload.get("arguments") or "")
        content = "{}({})".format(name, arguments)
        return _make_message(
            payload, "tool", content, timestamp, window, index, path, "tool_call"
        )
    if payload_type == "function_call_output":
        content = sanitize_full_text(payload.get("output") or "")
        if not content:
            return None
        return _make_message(
            payload, "tool", content, timestamp, window, index, path, "tool_output"
        )
    return None


def _event_message(
    payload: Dict[str, Any],
    timestamp: Any,
    window: TimeWindow,
    index: int,
    path: Path,
) -> Optional[TraceMessage]:
    payload_type = payload.get("type")
    if payload_type not in {"user_message", "agent_message"}:
        return None
    role = "user" if payload_type == "user_message" else "assistant"
    content = sanitize_full_text(payload.get("message") or "")
    if role == "user":
        content = _clean_codex_user_text(content)
    if not content:
        return None
    return _make_message(
        payload, role, content, timestamp, window, index, path, "message"
    )


def _make_message(
    payload: Dict[str, Any],
    role: str,
    content: str,
    timestamp: Any,
    window: TimeWindow,
    index: int,
    path: Path,
    kind: str,
) -> TraceMessage:
    message_id = payload.get("id") or payload.get("call_id")
    if not message_id:
        seed = "{}:{}:{}:{}".format(path, index, role, content)
        message_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return TraceMessage(
        message_id=str(message_id),
        role=role,
        content=content,
        occurred_at=to_iso(timestamp, window.timezone),
        kind=kind,
        extra={"call_id": payload.get("call_id")} if payload.get("call_id") else {},
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return sanitize_full_text(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("message")
                if text:
                    parts.append(str(text))
        return sanitize_full_text("\n".join(parts))
    if isinstance(content, dict):
        return sanitize_full_text(content.get("text") or content.get("message") or "")
    return ""


def _clean_codex_user_text(text: str) -> str:
    markers = [
        "## My request for Codex:",
        "My request for Codex:",
        "# My request for Codex:",
    ]
    cleaned = sanitize_full_text(text)
    found_request_marker = False
    for marker in markers:
        index = cleaned.rfind(marker)
        if index >= 0:
            cleaned = cleaned[index + len(marker) :]
            found_request_marker = True
            break
    if not found_request_marker and _looks_like_codex_context(cleaned):
        return ""
    for tag in (
        "environment_context",
        "codex_internal_context",
        "INSTRUCTIONS",
        "image",
        "recommended_plugins",
        "apps_instructions",
        "skills_instructions",
        "plugins_instructions",
        "permissions instructions",
        "app-context",
    ):
        cleaned = _strip_xml_block(cleaned, tag)
    return cleaned.replace("# AGENTS.md instructions", "").strip()


def _looks_like_codex_context(text: str) -> bool:
    markers = [
        "# AGENTS.md instructions",
        "<recommended_plugins>",
        "<environment_context>",
        "<codex_internal_context",
        "<permissions instructions>",
        "<app-context>",
        "<skills_instructions>",
        "# Context from my IDE setup:",
    ]
    return any(marker in text for marker in markers)


def _strip_xml_block(text: str, tag: str) -> str:
    start = "<{}".format(tag)
    end = "</{}>".format(tag)
    result = text
    while True:
        start_index = result.find(start)
        if start_index < 0:
            return result
        end_index = result.find(end, start_index)
        if end_index < 0:
            return result[:start_index].strip()
        result = result[:start_index] + result[end_index + len(end) :]


def _dedupe_messages(messages: List[TraceMessage]) -> List[TraceMessage]:
    seen: set = set()
    result: List[TraceMessage] = []
    for message in sorted(messages, key=lambda item: item.occurred_at or ""):
        key: Tuple[str, str] = (message.message_id, message.kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


def _session_id_from_name(path: Path) -> str:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
        path.stem,
    )
    return match.group(1) if match else path.stem
