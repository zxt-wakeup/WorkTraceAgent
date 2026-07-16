from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Dict, Iterable, List, Tuple, Union

from worktrace_agent.schema import (
    ConnectorResult,
    ConversationTrace,
    SourceCoverage,
    TraceMessage,
    WorkSignal,
)


def coerce_connector_result(
    value: Union[ConnectorResult, List[WorkSignal]],
) -> ConnectorResult:
    if isinstance(value, ConnectorResult):
        return value
    return ConnectorResult(signals=list(value))


def merge_conversations(
    conversations: Iterable[ConversationTrace],
) -> List[ConversationTrace]:
    grouped: Dict[Tuple[str, str], ConversationTrace] = {}
    for conversation in conversations:
        key = (conversation.origin, conversation.conversation_id)
        if key not in grouped:
            grouped[key] = conversation
            continue
        grouped[key] = _merge_conversation(grouped[key], conversation)
    return sorted(
        grouped.values(),
        key=lambda item: (item.occurred_at, item.origin, item.conversation_id),
    )


def merge_coverage(items: Iterable[SourceCoverage]) -> List[SourceCoverage]:
    grouped: Dict[str, SourceCoverage] = {}
    for item in items:
        existing = grouped.get(item.source)
        if existing is None:
            grouped[item.source] = item
            continue
        status = _merge_coverage_status(existing.status, item.status)
        details = [value for value in (existing.detail, item.detail) if value]
        grouped[item.source] = SourceCoverage(
            source=item.source,
            status=status,
            conversations=max(existing.conversations, item.conversations),
            messages=max(existing.messages, item.messages),
            detail="; ".join(dict.fromkeys(details)),
        )
    return sorted(grouped.values(), key=lambda item: item.source)


def _merge_coverage_status(left: str, right: str) -> str:
    statuses = {left, right}
    if statuses == {"complete"}:
        return "complete"
    if "complete" in statuses or "partial" in statuses:
        return "partial"
    if "error" in statuses:
        return "error"
    if "empty" in statuses:
        return "empty"
    if "missing" in statuses:
        return "missing"
    return "unknown"


def _merge_conversation(
    left: ConversationTrace, right: ConversationTrace
) -> ConversationTrace:
    left_full = bool(left.extra.get("full_transcript"))
    right_full = bool(right.extra.get("full_transcript"))
    if left_full and not right_full:
        messages = left.messages
    elif right_full and not left_full:
        messages = right.messages
    else:
        messages = _merge_messages(left.messages + right.messages)

    extra = dict(left.extra)
    for key, value in right.extra.items():
        if value not in (None, "", [], {}):
            extra[key] = value
    if left_full or right_full:
        extra["full_transcript"] = True
        extra.pop("metadata_only", None)

    title = _prefer_text(
        left.title, right.title, generic={"codex", "chatgpt conversation"}
    )
    workspace = _prefer_text(
        left.workspace,
        right.workspace,
        generic={"codex", "codex-history", "codex-session", "unknown"},
    )
    confidence = (
        "high" if "high" in {left.confidence, right.confidence} else left.confidence
    )
    return replace(
        left,
        workspace=workspace,
        title=title,
        messages=messages,
        paths=sorted(set(left.paths) | set(right.paths)),
        confidence=confidence,
        extra=extra,
    )


def _merge_messages(messages: List[TraceMessage]) -> List[TraceMessage]:
    seen = set()
    result = []
    for message in sorted(messages, key=lambda item: item.occurred_at or ""):
        key = (message.message_id, message.kind)
        if not message.message_id:
            normalized = re.sub(r"\s+", " ", message.content).strip()
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            key = (
                "{}:{}:{}".format(message.role, message.occurred_at, digest),
                message.kind,
            )
        if key in seen:
            continue
        seen.add(key)
        result.append(message)
    return result


def _prefer_text(left: str, right: str, generic: set) -> str:
    if left and left.strip().lower() not in generic:
        return left
    return right or left
