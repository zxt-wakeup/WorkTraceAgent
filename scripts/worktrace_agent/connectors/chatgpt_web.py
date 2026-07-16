from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from worktrace_agent.connectors.browser import BrowserConversationConnector
from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
)
from worktrace_agent.text import sanitize_full_text, unique_preserving_order
from worktrace_agent.window import TimeWindow, to_iso


class ChatGptWebConnector:
    key = "chatgpt_web"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.browser = BrowserConversationConnector(
            key=self.key,
            label="ChatGPT Web",
            browser_profiles=config.get("browser_profiles") or [],
            url_patterns=["chatgpt.com", "chat.openai.com"],
            cache_origin_markers=["https_chatgpt.com", "https_chat.openai.com"],
            cache_keywords=[
                "conversation",
                "message",
                "title",
                "chatgpt",
                "prompt",
                "assistant",
            ],
        )
        self.include_browser_evidence = (
            config.get("include_browser_evidence", False) is True
        )
        self.auto_discover_exports = config.get("auto_discover_exports", True) is True
        self.export_paths = [
            Path(item).expanduser() for item in config.get("export_paths") or []
        ]

    def scan(self, window: TimeWindow) -> ConnectorResult:
        conversations: List[ConversationTrace] = []
        signals = self.browser.scan(window) if self.include_browser_evidence else []
        paths = self._export_candidates()
        readable_exports = 0
        for path in paths:
            found, parsed = self._scan_export_path(path, window)
            conversations.extend(found)
            readable_exports += int(parsed)

        message_count = sum(len(item.messages) for item in conversations)
        if readable_exports and conversations:
            status = "complete"
            detail = "ChatGPT data export parsed with full user and assistant messages"
        elif readable_exports:
            status = "empty"
            detail = "ChatGPT export parsed, but it contains no messages for the selected day"
        else:
            status = "missing"
            detail = "No readable ChatGPT conversations.json or export zip found"
        coverage = [
            SourceCoverage(
                source="chatgpt",
                status=status,
                conversations=len(conversations),
                messages=message_count,
                detail=detail,
            )
        ]
        if self.include_browser_evidence:
            coverage.append(
                SourceCoverage(
                    source="chatgpt_browser",
                    status="partial" if signals else "empty",
                    conversations=0,
                    messages=0,
                    detail="Browser history/cache is discovery-only and does not prove full transcript coverage",
                )
            )
        return ConnectorResult(
            conversations=conversations, signals=signals, coverage=coverage
        )

    def _export_candidates(self) -> List[Path]:
        candidates = list(self.export_paths)
        if self.auto_discover_exports:
            downloads = Path.home() / "Downloads"
            if downloads.exists():
                candidates.extend(downloads.rglob("conversations.json"))
                for pattern in ("*chatgpt*.zip", "*openai*.zip", "*data-export*.zip"):
                    candidates.extend(downloads.glob(pattern))
        return unique_preserving_order(
            [path.resolve() for path in candidates if path.exists()]
        )

    def _scan_export_path(
        self, path: Path, window: TimeWindow
    ) -> Tuple[List[ConversationTrace], bool]:
        if path.is_dir():
            conversations: List[ConversationTrace] = []
            parsed = False
            for candidate in path.rglob("conversations.json"):
                found, ok = self._scan_export_json(candidate, window)
                conversations.extend(found)
                parsed = parsed or ok
            return conversations, parsed
        if path.suffix.lower() == ".zip":
            return self._scan_export_zip(path, window)
        return self._scan_export_json(path, window)

    def _scan_export_zip(
        self, path: Path, window: TimeWindow
    ) -> Tuple[List[ConversationTrace], bool]:
        conversations: List[ConversationTrace] = []
        parsed = False
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.endswith("conversations.json"):
                        continue
                    data = json.loads(
                        archive.read(name).decode("utf-8", errors="ignore")
                    )
                    conversations.extend(
                        self._conversations_from_export(
                            data, window, "{}!{}".format(path, name)
                        )
                    )
                    parsed = True
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError):
            return [], False
        return conversations, parsed

    def _scan_export_json(
        self, path: Path, window: TimeWindow
    ) -> Tuple[List[ConversationTrace], bool]:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return [], False
        if not isinstance(data, list):
            return [], False
        return self._conversations_from_export(data, window, str(path)), True

    def _conversations_from_export(
        self, data: Any, window: TimeWindow, evidence_path: str
    ) -> List[ConversationTrace]:
        if not isinstance(data, list):
            return []
        conversations: List[ConversationTrace] = []
        for conversation in data:
            if not isinstance(conversation, dict):
                continue
            messages = _conversation_messages(conversation, window)
            if not messages:
                continue
            conversation_id = str(
                conversation.get("id") or conversation.get("conversation_id") or ""
            )
            if not conversation_id:
                continue
            title = sanitize_full_text(
                conversation.get("title") or "ChatGPT conversation"
            )
            conversations.append(
                ConversationTrace(
                    origin="chatgpt",
                    conversation_id=conversation_id,
                    workspace=title[:120],
                    title=title,
                    messages=messages,
                    paths=[evidence_path],
                    confidence="high",
                    extra={
                        "evidence": "chatgpt_data_export",
                        "current_node": conversation.get("current_node"),
                        "full_transcript": True,
                    },
                )
            )
        return conversations


def build_chatgpt_web_connector(config: Dict[str, Any]) -> ChatGptWebConnector:
    return ChatGptWebConnector(config)


def _conversation_messages(
    conversation: Dict[str, Any], window: TimeWindow
) -> List[TraceMessage]:
    mapping = conversation.get("mapping") or {}
    if not isinstance(mapping, dict):
        return []
    active_ids = _active_branch_ids(mapping, conversation.get("current_node"))
    fallback_time = conversation.get("update_time") or conversation.get("create_time")
    rows: List[Tuple[float, int, TraceMessage]] = []
    for order, (node_id, node) in enumerate(mapping.items()):
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author") or {}
        role = str(author.get("role") or "")
        if role not in {"user", "assistant", "tool"}:
            continue
        timestamp = (
            message.get("create_time") or message.get("update_time") or fallback_time
        )
        if not window.contains(timestamp):
            continue
        content = sanitize_full_text(_message_parts_text(message.get("content") or {}))
        if not content:
            continue
        message_id = str(message.get("id") or node_id)
        rows.append(
            (
                float(timestamp or 0),
                order,
                TraceMessage(
                    message_id=message_id,
                    role=role,
                    content=content,
                    occurred_at=to_iso(timestamp, window.timezone),
                    kind="message" if role in {"user", "assistant"} else "tool_output",
                    parent_id=node.get("parent"),
                    active_branch=not active_ids or node_id in active_ids,
                    extra={"node_id": node_id},
                ),
            )
        )
    rows.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in rows]


def _active_branch_ids(mapping: Dict[str, Any], current_node: Any) -> Set[str]:
    active: Set[str] = set()
    node_id = str(current_node or "")
    while node_id and node_id not in active:
        active.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            break
        node_id = str(node.get("parent") or "")
    return active


def _message_parts_text(content: Dict[str, Any]) -> str:
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if isinstance(parts, list):
        values = []
        for part in parts:
            if isinstance(part, str):
                values.append(part)
            elif isinstance(part, dict):
                values.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
        return "\n".join(values)
    return str(content.get("text") or content.get("result") or "")
