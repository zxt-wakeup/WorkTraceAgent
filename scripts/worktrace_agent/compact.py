from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Tuple

from worktrace_agent.schema import WorkSignal
from worktrace_agent.text import compact_text, is_low_signal, unique_preserving_order


def compact_signals(signals: List[WorkSignal]) -> List[WorkSignal]:
    filtered = [signal for signal in signals if not is_low_signal(signal.note)]
    deduped = _dedupe(filtered)
    return _aggregate(deduped)


def _dedupe(signals: List[WorkSignal]) -> List[WorkSignal]:
    seen: Dict[Tuple[Any, ...], WorkSignal] = {}
    for signal in signals:
        key = _fingerprint(signal)
        if key not in seen:
            seen[key] = signal
            continue
        existing = seen[key]
        extra = dict(existing.extra)
        extra["duplicate_count"] = int(extra.get("duplicate_count", 1)) + 1
        paths = sorted(set(existing.paths) | set(signal.paths))
        seen[key] = replace(existing, paths=paths, extra=extra)
    return list(seen.values())


def _aggregate(signals: List[WorkSignal]) -> List[WorkSignal]:
    groups: Dict[Tuple[Any, ...], List[WorkSignal]] = {}
    for signal in signals:
        groups.setdefault(_group_key(signal), []).append(signal)

    compacted: List[WorkSignal] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.occurred_at or "")
        if len(ordered) == 1:
            compacted.append(ordered[0])
            continue
        first = ordered[0]
        notes = unique_preserving_order([item.note for item in ordered if item.note])
        paths = sorted({path for item in ordered for path in item.paths})
        times = [item.occurred_at for item in ordered if item.occurred_at]
        extra = {
            "compact": True,
            "evidence_count": len(ordered),
            "time_start": times[0] if times else None,
            "time_end": times[-1] if times else None,
            "source_evidence": [item.extra.get("evidence") for item in ordered[:20]],
            "original_notes": notes[:20],
        }
        compacted.append(
            WorkSignal(
                origin=first.origin,
                workspace=first.workspace,
                occurred_at=_time_range(times),
                note=compact_text(
                    "{} evidence items: {}".format(len(ordered), "; ".join(notes[:6])),
                    1600,
                ),
                paths=paths[:30],
                extra=extra,
                confidence=_combined_confidence(ordered),
            )
        )
    return sorted(
        compacted,
        key=lambda item: (item.occurred_at or "", item.origin, item.workspace),
    )


def _fingerprint(signal: WorkSignal) -> Tuple[Any, ...]:
    stable_id = (
        signal.extra.get("thread_id")
        or signal.extra.get("session_id")
        or signal.extra.get("conversation_id")
        or signal.extra.get("key")
        or _first_url(signal)
    )
    if stable_id:
        return (signal.origin, stable_id, _normalize(signal.note))
    return (signal.origin, _normalize(signal.workspace), _normalize(signal.note))


def _group_key(signal: WorkSignal) -> Tuple[Any, ...]:
    stable_id = (
        signal.extra.get("thread_id")
        or signal.extra.get("session_id")
        or signal.extra.get("conversation_id")
        or signal.extra.get("key")
    )
    if stable_id:
        return (signal.origin, stable_id)
    return (signal.origin, _normalize(signal.workspace), "")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _first_url(signal: WorkSignal) -> str:
    for path in signal.paths:
        if path.startswith("http://") or path.startswith("https://"):
            return path
    return ""


def _time_range(times: List[str]) -> str:
    if not times:
        return ""
    if len(times) == 1 or times[0] == times[-1]:
        return times[0]
    return "{} ~ {}".format(times[0], times[-1])


def _combined_confidence(signals: List[WorkSignal]) -> str:
    scores = {"high": 3, "medium": 2, "low": 1}
    best = max(scores.get(signal.confidence, 2) for signal in signals)
    for name, score in scores.items():
        if score == best:
            return name
    return "medium"
