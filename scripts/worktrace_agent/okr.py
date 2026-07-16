from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from worktrace_agent.settings import SKILL_ROOT
from worktrace_agent.resource_paths import reference_path
from worktrace_agent.storage import write_private_text
from worktrace_agent.text import sanitize_for_model

TEMPLATE_MARKER = "<!-- worktrace:okr-template -->"
DEFAULT_OKR_PATH = Path.home() / ".config" / "worktrace-agent" / "okr.md"
OKR_EXAMPLE_PATH = reference_path("okr.example.md")
INACTIVE_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:状态|status)\s*[:：]\s*(?:停用|禁用|inactive|disabled)\s*$"
)
OKR_ID_PATTERN = re.compile(r"\bO\d+/KR\d+(?:\.\d+)?\b", re.IGNORECASE)
PERIOD_LINE_PATTERN = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:周期|period)\s*[:：]\s*(.+?)\s*$"
)
QUARTER_PATTERN = re.compile(
    r"^(\d{4})\s*[- ]?\s*Q([1-4])(?:\s*[（(].*[）)]\s*)?$", re.IGNORECASE
)
YEAR_PATTERN = re.compile(r"^(\d{4})(?:\s*[（(].*[）)]\s*)?$")
RANGE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*(?:至|到|to|~|～|—|–)\s*"
    r"(\d{4}-\d{2}-\d{2})(?:\s.*)?$",
    re.IGNORECASE,
)
LOCAL_INACTIVE_PATTERN = re.compile(
    r"(?:[（(\[]\s*(?:停用|禁用|inactive|disabled)\s*[）)\]]|"
    r"(?:状态|status)\s*[:：]\s*(?:停用|禁用|inactive|disabled))\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OkrContext:
    path: Path
    text: str = ""
    digest: str = ""
    refs: Tuple[str, ...] = ()
    status: str = "missing"

    @property
    def configured(self) -> bool:
        return self.status == "configured"


def resolve_okr_path(settings: Dict[str, Any], override: Optional[Path] = None) -> Path:
    if override is not None:
        path = override.expanduser()
    else:
        value = settings.get("okr", {}).get("path", str(DEFAULT_OKR_PATH))
        path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = SKILL_ROOT / path
    return path.resolve()


def initialize_okr(
    settings: Dict[str, Any], override: Optional[Path] = None
) -> Tuple[Path, bool]:
    path = resolve_okr_path(settings, override)
    if path.exists():
        if not path.is_file():
            raise ValueError("OKR reference is not a regular file: {}".format(path))
        path.chmod(0o600)
        return path, False
    template = OKR_EXAMPLE_PATH.read_text(encoding="utf-8")
    write_private_text(path, template)
    return path, True


def save_okr(settings: Dict[str, Any], raw: str) -> OkrContext:
    """Persist user-supplied OKR planning data with the normal private-file rules."""

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("OKR text must not be empty")
    max_chars = int(settings.get("okr", {}).get("max_chars", 20_000))
    if len(raw) > max_chars:
        raise ValueError("OKR reference exceeds okr.max_chars ({})".format(max_chars))
    path = resolve_okr_path(settings)
    write_private_text(path, raw)
    return load_okr(settings)


def load_okr(settings: Dict[str, Any], report_day: Optional[str] = None) -> OkrContext:
    path = resolve_okr_path(settings)
    required = bool(settings.get("okr", {}).get("required", False))
    if not path.exists():
        if required:
            raise ValueError("required OKR reference not found: {}".format(path))
        return OkrContext(path=path)
    if not path.is_file():
        raise ValueError("OKR reference is not a regular file: {}".format(path))

    max_chars = int(settings.get("okr", {}).get("max_chars", 20_000))
    if max_chars <= 0:
        raise ValueError("okr.max_chars must be greater than zero")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return _unconfigured(path, "empty", required)
    if TEMPLATE_MARKER in raw:
        return _unconfigured(path, "template", required)
    if len(raw) > max_chars:
        raise ValueError("OKR reference exceeds okr.max_chars ({})".format(max_chars))
    if INACTIVE_PATTERN.search(raw):
        return _unconfigured(path, "inactive", required)
    refs = _active_okr_refs(raw)
    if not refs:
        return _unconfigured(path, "invalid", required)
    if report_day is not None:
        try:
            selected_day = date.fromisoformat(report_day)
        except ValueError as exc:
            raise ValueError("report_day must use YYYY-MM-DD") from exc
        period_status = _period_status(raw, selected_day)
        if period_status != "configured":
            return _unconfigured(path, period_status, required)

    active_text = "\n".join(
        line
        for line in raw.splitlines()
        if not (OKR_ID_PATTERN.search(line) and LOCAL_INACTIVE_PATTERN.search(line))
    )
    text = sanitize_for_model(active_text)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return OkrContext(
        path=path,
        text=text,
        digest=digest,
        refs=refs,
        status="configured",
    )


def _active_okr_refs(raw: str) -> Tuple[str, ...]:
    refs = []
    for line in raw.splitlines():
        if LOCAL_INACTIVE_PATTERN.search(line):
            continue
        refs.extend(match.group(0).upper() for match in OKR_ID_PATTERN.finditer(line))
    return tuple(dict.fromkeys(refs))


def _period_status(raw: str, selected_day: date) -> str:
    period_lines = PERIOD_LINE_PATTERN.findall(raw)
    if len(period_lines) != 1:
        return "invalid_period"
    bounds = _parse_period(period_lines[0].strip())
    if bounds is None:
        return "invalid_period"
    start, end = bounds
    return "configured" if start <= selected_day <= end else "out_of_period"


def _parse_period(value: str) -> Optional[Tuple[date, date]]:
    quarter = QUARTER_PATTERN.fullmatch(value)
    if quarter:
        year = int(quarter.group(1))
        start_month = (int(quarter.group(2)) - 1) * 3 + 1
        end_month = start_month + 2
        return (
            date(year, start_month, 1),
            date(year, end_month, calendar.monthrange(year, end_month)[1]),
        )

    year_match = YEAR_PATTERN.fullmatch(value)
    if year_match:
        year = int(year_match.group(1))
        return date(year, 1, 1), date(year, 12, 31)

    date_range = RANGE_PATTERN.fullmatch(value)
    if date_range:
        try:
            start = date.fromisoformat(date_range.group(1))
            end = date.fromisoformat(date_range.group(2))
        except ValueError:
            return None
        return (start, end) if start <= end else None
    return None


def _unconfigured(path: Path, status: str, required: bool) -> OkrContext:
    if required:
        raise ValueError("required OKR reference is {}: {}".format(status, path))
    return OkrContext(path=path, status=status)
