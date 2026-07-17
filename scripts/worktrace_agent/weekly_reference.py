from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from worktrace_agent.settings import SKILL_ROOT
from worktrace_agent.storage import write_private_text
from worktrace_agent.text import sanitize_for_model


DEFAULT_WEEKLY_REFERENCE_PATH = (
    Path.home() / ".config" / "worktrace-agent" / "weekly-report-reference.md"
)


@dataclass(frozen=True)
class WeeklyReportReference:
    path: Path
    text: str = ""
    status: str = "missing"

    @property
    def configured(self) -> bool:
        return self.status == "configured"


def resolve_weekly_reference_path(settings: Dict[str, Any]) -> Path:
    value = settings.get("weekly_report_reference", {}).get(
        "path", str(DEFAULT_WEEKLY_REFERENCE_PATH)
    )
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = SKILL_ROOT / path
    return path.resolve()


def load_weekly_reference(settings: Dict[str, Any]) -> WeeklyReportReference:
    path = resolve_weekly_reference_path(settings)
    if not path.exists():
        return WeeklyReportReference(path=path)
    if not path.is_file():
        raise ValueError("weekly report reference is not a regular file: {}".format(path))
    max_chars = int(
        settings.get("weekly_report_reference", {}).get("max_chars", 200_000)
    )
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return WeeklyReportReference(path=path, status="empty")
    if len(raw) > max_chars:
        raise ValueError(
            "weekly report reference exceeds weekly_report_reference.max_chars ({})".format(
                max_chars
            )
        )
    text = sanitize_for_model(raw)
    if not text.strip():
        return WeeklyReportReference(path=path, status="invalid")
    return WeeklyReportReference(path=path, text=text, status="configured")


def save_weekly_reference(
    settings: Dict[str, Any], raw: str
) -> WeeklyReportReference:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("weekly report reference must not be empty")
    max_chars = int(
        settings.get("weekly_report_reference", {}).get("max_chars", 200_000)
    )
    if len(raw) > max_chars:
        raise ValueError(
            "weekly report reference exceeds weekly_report_reference.max_chars ({})".format(
                max_chars
            )
        )
    path = resolve_weekly_reference_path(settings)
    write_private_text(path, raw)
    return load_weekly_reference(settings)
