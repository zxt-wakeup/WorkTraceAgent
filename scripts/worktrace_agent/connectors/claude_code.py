from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
)
from worktrace_agent.text import sanitize_full_text
from worktrace_agent.window import TimeWindow, parse_timestamp, to_iso


class ClaudeCodeConnector:
    """Read Claude Code's local JSONL transcripts without mutating its state."""

    key = "claude_code"

    def __init__(
        self,
        root: Path,
        key: str = "claude_code",
        label: str = "Claude Code",
        include_subagents: bool = True,
        excluded_session_ids: Optional[Iterable[str]] = None,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_files: int = 2000,
    ) -> None:
        self.root = root
        self.key = key
        self.label = label
        self.include_subagents = include_subagents
        self.excluded_session_ids: Set[str] = {
            str(value) for value in (excluded_session_ids or []) if str(value)
        }
        self.max_file_bytes = max_file_bytes
        self.max_files = max(0, max_files)

    def scan(self, window: TimeWindow) -> ConnectorResult:
        if not self.root.exists():
            return ConnectorResult(
                coverage=[
                    SourceCoverage(
                        self.key,
                        "missing",
                        detail="{} home directory not found".format(self.label),
                    )
                ]
            )

        conversations: List[ConversationTrace] = []
        skipped_large = 0
        parse_errors = 0
        malformed_records = 0
        omitted_untimestamped = 0
        files_seen = 0
        file_cap_hit = False
        for path in self._session_candidates(window):
            if files_seen >= self.max_files:
                file_cap_hit = True
                break
            files_seen += 1
            try:
                if path.stat().st_size > self.max_file_bytes:
                    skipped_large += 1
                    continue
            except OSError:
                parse_errors += 1
                continue
            try:
                file_stats = {"malformed_records": 0, "untimestamped": 0}
                conversation = self._scan_session(path, window, stats=file_stats)
                malformed_records += file_stats["malformed_records"]
                omitted_untimestamped += file_stats["untimestamped"]
            except (OSError, ValueError):
                parse_errors += 1
                continue
            if conversation is None:
                continue
            raw_session_id = str(conversation.extra.get("raw_session_id") or "")
            if raw_session_id in self.excluded_session_ids:
                continue
            conversation.extra.pop("raw_session_id", None)
            if file_stats["malformed_records"]:
                conversation.extra["full_transcript"] = False
                conversation.extra["malformed_records"] = file_stats[
                    "malformed_records"
                ]
            if file_stats["untimestamped"]:
                conversation.extra["full_transcript"] = False
                conversation.extra["omitted_untimestamped"] = file_stats[
                    "untimestamped"
                ]
            conversations.append(conversation)

        messages = sum(len(item.messages) for item in conversations)
        if file_cap_hit or skipped_large or malformed_records or omitted_untimestamped:
            status = "partial"
        elif conversations and not (skipped_large or parse_errors):
            status = "complete"
        elif conversations:
            status = "partial"
        elif parse_errors:
            status = "error"
        else:
            status = "empty"
        detail = "Read-only {} project JSONL transcripts".format(self.label)
        if skipped_large:
            detail += "; skipped {} oversized files".format(skipped_large)
        if parse_errors:
            detail += "; {} files could not be parsed".format(parse_errors)
        if malformed_records:
            detail += "; dropped {} malformed or undecodable JSONL records".format(
                malformed_records
            )
        if omitted_untimestamped:
            detail += (
                "; omitted {} message events with missing or unparseable timestamps"
            ).format(omitted_untimestamped)
        if file_cap_hit:
            detail += "; file scan capped at {}".format(self.max_files)
        if not conversations:
            detail += "; no messages found in the selected period"
        return ConnectorResult(
            conversations=conversations,
            coverage=[
                SourceCoverage(
                    source=self.key,
                    status=status,
                    conversations=len(conversations),
                    messages=messages,
                    detail=detail,
                )
            ],
        )

    def _session_candidates(self, window: TimeWindow) -> Iterable[Path]:
        roots = [self.root / "projects", self.root / "sessions"]
        seen: Set[Path] = set()
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if path in seen:
                    continue
                seen.add(path)
                if not self.include_subagents and "subagents" in path.parts:
                    continue
                if any(
                    part in {"backups", "file-history", "debug"} for part in path.parts
                ):
                    continue
                yield path

    def _scan_session(
        self,
        path: Path,
        window: TimeWindow,
        stats: Optional[Dict[str, int]] = None,
    ) -> Optional[ConversationTrace]:
        messages: List[TraceMessage] = []
        session_id = ""
        cwd = ""
        title = ""
        model = ""
        git_branch = ""
        is_subagent = "subagents" in path.parts

        for index, event in enumerate(_read_jsonl(path, stats=stats)):
            event_type = str(event.get("type") or "").lower()
            session_id = str(
                event.get("sessionId") or event.get("session_id") or session_id
            )
            cwd = str(event.get("cwd") or cwd)
            git_branch = str(
                event.get("gitBranch") or event.get("git_branch") or git_branch
            )
            if event_type in {"ai-title", "custom-title"}:
                title = sanitize_full_text(
                    event.get("aiTitle") or event.get("title") or title
                )
                continue
            timestamp = event.get("timestamp") or event.get("createdAt")
            parsed_timestamp = parse_timestamp(timestamp, window.timezone)
            if parsed_timestamp is None:
                if _event_messages(
                    event,
                    timestamp=window.start.isoformat(),
                    timezone=window.timezone,
                    index=index,
                    path=path,
                    force_active=is_subagent,
                ):
                    if stats is not None:
                        stats["untimestamped"] = stats.get("untimestamped", 0) + 1
                continue
            if not window.start <= parsed_timestamp < window.end:
                continue
            message_value = event.get("message")
            if isinstance(message_value, dict):
                model = str(message_value.get("model") or model)
            messages.extend(
                _event_messages(
                    event,
                    timestamp=timestamp,
                    timezone=window.timezone,
                    index=index,
                    path=path,
                    force_active=is_subagent,
                )
            )

        messages = _dedupe_messages(messages)
        if not messages:
            return None
        raw_session_id = session_id or path.stem
        conversation_id = raw_session_id
        if is_subagent:
            conversation_id = "{}:{}".format(raw_session_id, path.stem)
        return ConversationTrace(
            origin=self.key,
            conversation_id=conversation_id,
            workspace=Path(cwd).name if cwd else _workspace_from_project_path(path),
            title=title,
            messages=messages,
            paths=[str(path)],
            confidence="high",
            extra={
                "cwd": cwd,
                "model": model,
                "git_branch": git_branch,
                "raw_session_id": raw_session_id,
                "is_subagent": is_subagent,
                "evidence": "{}_jsonl".format(self.key),
                "full_transcript": True,
            },
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


def _event_messages(
    event: Dict[str, Any],
    timestamp: Any,
    timezone: str,
    index: int,
    path: Path,
    force_active: bool,
) -> List[TraceMessage]:
    event_type = str(event.get("type") or "").lower()
    if event_type not in {"user", "assistant"}:
        return []
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    default_role = _normalize_role(message.get("role") or event_type)
    if default_role not in {"user", "assistant"}:
        return []
    content = message.get("content")
    blocks: Sequence[Any] = content if isinstance(content, list) else [content]
    result: List[TraceMessage] = []
    active = force_active or not bool(event.get("isSidechain", False))
    parent_id = event.get("parentUuid") or event.get("parent_id")
    event_id = event.get("uuid") or message.get("id")

    for block_index, block in enumerate(blocks):
        role = default_role
        kind = "message"
        block_id: Any = None
        text = ""
        extra: Dict[str, Any] = {}
        if isinstance(block, str):
            text = block
        elif isinstance(block, dict):
            block_type = str(block.get("type") or "text").lower()
            if block_type in {"thinking", "redacted_thinking", "image", "document"}:
                continue
            if block_type == "text":
                text = block.get("text") or ""
            elif block_type in {"tool_use", "server_tool_use"}:
                role = "tool"
                kind = "tool_call"
                block_id = block.get("id")
                name = sanitize_full_text(block.get("name") or "tool")
                tool_input = sanitize_full_text(block.get("input") or {})
                text = "{}({})".format(name, tool_input)
                extra["tool_name"] = name
            elif block_type in {"tool_result", "server_tool_result"}:
                role = "tool"
                kind = "tool_output"
                block_id = block.get("tool_use_id") or block.get("id")
                text = _content_to_text(block.get("content"))
                extra["is_error"] = bool(block.get("is_error", False))
            else:
                continue
        else:
            continue

        text = sanitize_full_text(text)
        if role == "user":
            text = _clean_user_text(text)
        if not text:
            continue
        message_id = block_id or event_id
        if not message_id or len(blocks) > 1:
            seed = "{}:{}:{}:{}:{}".format(path, index, block_index, role, text)
            message_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        result.append(
            TraceMessage(
                message_id=str(message_id),
                role=role,
                content=text,
                occurred_at=to_iso(timestamp, timezone),
                kind=kind,
                parent_id=str(parent_id) if parent_id else None,
                active_branch=active,
                extra=extra,
            )
        )
    return result


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").lower()
                if item_type in {"thinking", "redacted_thinking"}:
                    continue
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return str(value or "")


def _clean_user_text(text: str) -> str:
    cleaned = re.sub(
        r"<system-reminder>.*?</system-reminder>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<(?:command-name|command-message|local-command-caveat)>.*?</(?:command-name|command-message|local-command-caveat)>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return cleaned.strip()


def _normalize_role(value: Any) -> str:
    role = str(value or "").lower()
    if role in {"user", "human"}:
        return "user"
    if role in {"assistant", "agent", "model"}:
        return "assistant"
    return role


def _dedupe_messages(messages: List[TraceMessage]) -> List[TraceMessage]:
    seen: Set[Tuple[str, str]] = set()
    result: List[TraceMessage] = []
    for message in sorted(messages, key=lambda item: item.occurred_at or ""):
        key = (message.message_id, message.kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


def _workspace_from_project_path(path: Path) -> str:
    project_name = (
        path.parents[2].name if "subagents" in path.parts else path.parent.name
    )
    return project_name.strip("-") or "claude-code"
