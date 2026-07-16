from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class TraceMessage:
    message_id: str
    role: str
    content: str
    occurred_at: Optional[str] = None
    kind: str = "message"
    parent_id: Optional[str] = None
    active_branch: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "occurred_at": self.occurred_at,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "active_branch": self.active_branch,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TraceMessage":
        return cls(
            message_id=str(value.get("message_id") or value.get("id") or ""),
            role=str(value.get("role") or "unknown"),
            content=str(value.get("content") or ""),
            occurred_at=value.get("occurred_at"),
            kind=str(value.get("kind") or "message"),
            parent_id=value.get("parent_id"),
            active_branch=bool(value.get("active_branch", True)),
            extra=dict(value.get("extra") or {}),
        )


@dataclass
class ConversationTrace:
    origin: str
    conversation_id: str
    workspace: str
    title: str = ""
    messages: List[TraceMessage] = field(default_factory=list)
    paths: List[str] = field(default_factory=list)
    confidence: str = "high"
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def occurred_at(self) -> str:
        times = [
            message.occurred_at for message in self.messages if message.occurred_at
        ]
        return max(times) if times else str(self.extra.get("updated_at") or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin,
            "conversation_id": self.conversation_id,
            "workspace": self.workspace,
            "title": self.title,
            "messages": [message.to_dict() for message in self.messages],
            "paths": list(self.paths),
            "confidence": self.confidence,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ConversationTrace":
        return cls(
            origin=str(value.get("origin") or "unknown"),
            conversation_id=str(value.get("conversation_id") or ""),
            workspace=str(value.get("workspace") or "unknown"),
            title=str(value.get("title") or ""),
            messages=[
                TraceMessage.from_dict(item)
                for item in value.get("messages", [])
                if isinstance(item, dict)
            ],
            paths=list(value.get("paths") or []),
            confidence=str(value.get("confidence") or "high"),
            extra=dict(value.get("extra") or {}),
        )


@dataclass
class SourceCoverage:
    source: str
    status: str
    conversations: int = 0
    messages: int = 0
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "conversations": self.conversations,
            "messages": self.messages,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "SourceCoverage":
        return cls(
            source=str(value.get("source") or "unknown"),
            status=str(value.get("status") or "unknown"),
            conversations=int(value.get("conversations") or 0),
            messages=int(value.get("messages") or 0),
            detail=str(value.get("detail") or ""),
        )


@dataclass
class WorkSignal:
    origin: str
    workspace: str
    note: str
    occurred_at: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    confidence: str = "medium"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin,
            "workspace": self.workspace,
            "note": self.note,
            "occurred_at": self.occurred_at,
            "paths": list(self.paths),
            "extra": dict(self.extra),
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "WorkSignal":
        return cls(
            origin=str(value.get("origin") or "unknown"),
            workspace=str(value.get("workspace") or "unknown"),
            note=str(value.get("note") or ""),
            occurred_at=value.get("occurred_at"),
            paths=list(value.get("paths") or []),
            extra=dict(value.get("extra") or {}),
            confidence=str(value.get("confidence") or "medium"),
        )


@dataclass
class ConnectorResult:
    conversations: List[ConversationTrace] = field(default_factory=list)
    signals: List[WorkSignal] = field(default_factory=list)
    coverage: List[SourceCoverage] = field(default_factory=list)


@dataclass
class TraceBundle:
    day: str
    generated_at: str
    timezone: str
    period_type: str = "daily"
    period_start: str = ""
    period_end: str = ""
    conversations: List[ConversationTrace] = field(default_factory=list)
    signals: List[WorkSignal] = field(default_factory=list)
    coverage: List[SourceCoverage] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        day: str,
        timezone: str,
        signals: Optional[List[WorkSignal]] = None,
        conversations: Optional[List[ConversationTrace]] = None,
        coverage: Optional[List[SourceCoverage]] = None,
        period_type: str = "daily",
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
    ) -> "TraceBundle":
        return cls(
            day=day,
            timezone=timezone,
            generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            period_type=period_type,
            period_start=period_start or day,
            period_end=period_end or day,
            conversations=list(conversations or []),
            signals=list(signals or []),
            coverage=list(coverage or []),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 3,
            "day": self.day,
            "generated_at": self.generated_at,
            "timezone": self.timezone,
            "period_type": self.period_type,
            "period_start": self.period_start or self.day,
            "period_end": self.period_end or self.day,
            "conversations": [
                conversation.to_dict() for conversation in self.conversations
            ],
            "signals": [signal.to_dict() for signal in self.signals],
            "coverage": [item.to_dict() for item in self.coverage],
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TraceBundle":
        return cls(
            day=str(value.get("day") or ""),
            generated_at=str(value.get("generated_at") or ""),
            timezone=str(value.get("timezone") or ""),
            period_type=str(value.get("period_type") or "daily"),
            period_start=str(value.get("period_start") or value.get("day") or ""),
            period_end=str(value.get("period_end") or value.get("day") or ""),
            conversations=[
                ConversationTrace.from_dict(item)
                for item in value.get("conversations", [])
                if isinstance(item, dict)
            ],
            signals=[
                WorkSignal.from_dict(item)
                for item in value.get("signals", [])
                if isinstance(item, dict)
            ],
            coverage=[
                SourceCoverage.from_dict(item)
                for item in value.get("coverage", [])
                if isinstance(item, dict)
            ],
        )


def validate_trace_bundle(bundle: TraceBundle) -> None:
    """Reject malformed or cross-period evidence before it can create paths/anchors."""

    from worktrace_agent.window import build_week_window, build_window

    try:
        generated_at = datetime.fromisoformat(
            bundle.generated_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("trace bundle generated_at must be ISO-8601") from exc
    if generated_at.tzinfo is None:
        raise ValueError("trace bundle generated_at must include a timezone")

    if bundle.period_type == "daily":
        window = build_window(bundle.day, bundle.timezone)
    elif bundle.period_type == "weekly":
        window = build_week_window(bundle.day, bundle.timezone)
    else:
        raise ValueError("trace bundle period_type must be daily or weekly")
    if bundle.day != window.day:
        raise ValueError("trace bundle period label is not canonical")
    if bundle.timezone != window.timezone:
        raise ValueError("trace bundle timezone is not canonical")
    if (bundle.period_start, bundle.period_end) != (
        window.period_start,
        window.period_end,
    ):
        raise ValueError("trace bundle period bounds do not match its label")
    for conversation in bundle.conversations:
        for message in conversation.messages:
            if not message.occurred_at or not window.contains(message.occurred_at):
                raise ValueError("trace bundle contains evidence outside its period")
    for signal in bundle.signals:
        if signal.occurred_at and not window.contains(signal.occurred_at):
            raise ValueError("trace bundle contains a signal outside its period")
