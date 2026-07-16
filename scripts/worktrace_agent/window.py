from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def detect_local_timezone() -> str:
    candidates = []
    if os.environ.get("TZ"):
        candidates.append(os.environ["TZ"])
    try:
        localtime = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in localtime:
            candidates.append(localtime.split("/zoneinfo/", 1)[1])
    except OSError:
        pass
    key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    if key:
        candidates.append(str(key))
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
            return candidate
        except ZoneInfoNotFoundError:
            continue
    return "UTC"


DEFAULT_TIMEZONE = detect_local_timezone()
CHROME_EPOCH_OFFSET_SECONDS = 11644473600
MAX_TIMESTAMP_SECONDS = 10_000_000_000
MAX_TIMESTAMP_UNIT_DIVISIONS = 3


@dataclass(frozen=True)
class TimeWindow:
    day: str
    timezone: str
    start: datetime
    end: datetime
    period_type: str = "daily"

    @property
    def period_start(self) -> str:
        return self.start.date().isoformat()

    @property
    def period_end(self) -> str:
        return (self.end - timedelta(microseconds=1)).date().isoformat()

    @property
    def label(self) -> str:
        if self.period_type == "weekly":
            return "{}..{}".format(self.period_start, self.period_end)
        return self.day

    @property
    def start_epoch(self) -> int:
        return int(self.start.timestamp())

    @property
    def end_epoch(self) -> int:
        return int(self.end.timestamp())

    @property
    def start_epoch_ms(self) -> int:
        return int(self.start.timestamp() * 1000)

    @property
    def end_epoch_ms(self) -> int:
        return int(self.end.timestamp() * 1000)

    def contains(self, value: Union[str, int, float, None]) -> bool:
        parsed = parse_timestamp(value, self.timezone)
        return parsed is not None and self.start <= parsed < self.end


def get_zone(timezone_name: Optional[str]) -> ZoneInfo:
    name = timezone_name or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone: {}".format(name)) from exc


def normalize_day(value: str, timezone_name: Optional[str] = None) -> str:
    zone = get_zone(timezone_name)
    today = datetime.now(zone).date()
    if value == "today":
        return today.isoformat()
    if value == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    return date.fromisoformat(value).isoformat()


def build_window(value: str, timezone_name: Optional[str] = None) -> TimeWindow:
    zone = get_zone(timezone_name)
    day_text = normalize_day(value, timezone_name)
    day = date.fromisoformat(day_text)
    start = datetime.combine(day, time.min, tzinfo=zone)
    return TimeWindow(
        day=day_text,
        timezone=zone.key,
        start=start,
        end=start + timedelta(days=1),
    )


def normalize_week(value: str, timezone_name: Optional[str] = None) -> date:
    """Return the Monday represented by this-week, last-week or YYYY-Www."""
    zone = get_zone(timezone_name)
    today = datetime.now(zone).date()
    current_monday = today - timedelta(days=today.weekday())
    normalized = (value or "this-week").strip().lower()
    if normalized in {"this-week", "current-week", "week", "today"}:
        return current_monday
    if normalized in {"last-week", "previous-week"}:
        return current_monday - timedelta(days=7)
    match = re.fullmatch(r"(\d{4})-?w(\d{1,2})", normalized, re.IGNORECASE)
    if not match:
        raise ValueError("week must be this-week, last-week, or YYYY-Www")
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise ValueError("invalid ISO week: {}".format(value)) from exc


def build_week_window(value: str, timezone_name: Optional[str] = None) -> TimeWindow:
    zone = get_zone(timezone_name)
    monday = normalize_week(value, timezone_name)
    start = datetime.combine(monday, time.min, tzinfo=zone)
    iso_year, iso_week, _ = monday.isocalendar()
    return TimeWindow(
        day="{:04d}-W{:02d}".format(iso_year, iso_week),
        timezone=zone.key,
        start=start,
        end=start + timedelta(days=7),
        period_type="weekly",
    )


def parse_timestamp(
    value: Union[str, int, float, None], timezone_name: Optional[str] = None
) -> Optional[datetime]:
    if value is None:
        return None
    zone = get_zone(timezone_name)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if not math.isfinite(timestamp):
            return None
        for _ in range(MAX_TIMESTAMP_UNIT_DIVISIONS):
            if abs(timestamp) <= MAX_TIMESTAMP_SECONDS:
                break
            timestamp = timestamp / 1000
        if abs(timestamp) > MAX_TIMESTAMP_SECONDS:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=zone)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        try:
            return parse_timestamp(float(text), timezone_name)
        except (OverflowError, OSError, ValueError):
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def to_iso(
    value: Union[str, int, float, None], timezone_name: Optional[str] = None
) -> str:
    parsed = parse_timestamp(value, timezone_name)
    if parsed is None:
        return ""
    return parsed.isoformat(timespec="seconds")


def file_mtime_in_window(path, window: TimeWindow, slack_days: int = 1) -> bool:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=get_zone(window.timezone))
    return (
        window.start - timedelta(days=slack_days)
        <= mtime
        < window.end + timedelta(days=slack_days)
    )


def chrome_time_to_iso(value: int, timezone_name: Optional[str] = None) -> str:
    unix_seconds = (int(value) / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
    return to_iso(unix_seconds, timezone_name)


def chrome_time_in_window(value: int, window: TimeWindow) -> bool:
    unix_seconds = (int(value) / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
    return window.contains(unix_seconds)
