from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple
from urllib.parse import urlparse

from worktrace_agent.connectors.sqlite_utils import copied_sqlite_connection
from worktrace_agent.schema import WorkSignal
from worktrace_agent.text import compact_text, is_low_signal
from worktrace_agent.window import (
    TimeWindow,
    chrome_time_in_window,
    chrome_time_to_iso,
    file_mtime_in_window,
    to_iso,
)

ProfileRef = Tuple[str, Path]


def discover_chromium_profiles(explicit_profiles: Sequence[str]) -> List[ProfileRef]:
    if explicit_profiles:
        return [("custom", Path(item).expanduser()) for item in explicit_profiles]

    home = Path.home()
    roots = [
        ("chrome", home / "Library/Application Support/Google/Chrome"),
        ("chrome-beta", home / "Library/Application Support/Google/Chrome Beta"),
        ("brave", home / "Library/Application Support/BraveSoftware/Brave-Browser"),
        ("edge", home / "Library/Application Support/Microsoft Edge"),
        ("arc", home / "Library/Application Support/Arc/User Data"),
    ]
    profiles: List[ProfileRef] = []
    for browser_name, root in roots:
        if not root.exists():
            continue
        for profile in root.iterdir():
            if not profile.is_dir():
                continue
            if (profile / "History").exists() or (profile / "IndexedDB").exists():
                profiles.append((browser_name, profile))
    return profiles


class BrowserConversationConnector:
    def __init__(
        self,
        key: str,
        label: str,
        browser_profiles: Sequence[str],
        url_patterns: Sequence[str],
        cache_origin_markers: Sequence[str],
        cache_keywords: Sequence[str],
    ) -> None:
        self.key = key
        self.label = label
        self.browser_profiles = browser_profiles
        self.url_patterns = list(url_patterns)
        self.cache_origin_markers = [item.lower() for item in cache_origin_markers]
        self.cache_keywords = [item.lower() for item in cache_keywords]

    def scan(self, window: TimeWindow) -> List[WorkSignal]:
        signals: List[WorkSignal] = []
        for browser_name, profile in discover_chromium_profiles(self.browser_profiles):
            if not profile.exists():
                continue
            signals.extend(self._scan_history(browser_name, profile, window))
            signals.extend(self._scan_cache(browser_name, profile, window))
        return signals

    def _scan_history(
        self, browser_name: str, profile: Path, window: TimeWindow
    ) -> List[WorkSignal]:
        history_path = profile / "History"
        if not history_path.exists():
            return []

        where = " OR ".join(["urls.url LIKE ?" for _ in self.url_patterns])
        params = ["%{}%".format(pattern) for pattern in self.url_patterns]
        query = """
            SELECT visits.visit_time, urls.title, urls.url
            FROM visits
            JOIN urls ON urls.id = visits.url
            WHERE {}
            ORDER BY visits.visit_time DESC
        """.format(where)

        signals: List[WorkSignal] = []
        try:
            with copied_sqlite_connection(history_path) as connection:
                for visit_time, title, url in connection.execute(query, params):
                    if not chrome_time_in_window(int(visit_time), window):
                        continue
                    note = compact_text(title or url, 500)
                    if is_low_signal(note):
                        continue
                    parsed = urlparse(url)
                    safe_url = _sanitize_url(url)
                    signals.append(
                        WorkSignal(
                            origin=self.key,
                            workspace=_workspace_from_url(url),
                            occurred_at=chrome_time_to_iso(
                                int(visit_time), window.timezone
                            ),
                            note="{} page session: {}".format(self.label, note),
                            paths=[safe_url],
                            confidence="low",
                            extra={
                                "browser": browser_name,
                                "profile": str(profile),
                                "host": parsed.netloc,
                                "evidence": "browser_history",
                            },
                        )
                    )
        except Exception:
            return []
        return signals

    def _scan_cache(
        self, browser_name: str, profile: Path, window: TimeWindow
    ) -> List[WorkSignal]:
        snippets: List[WorkSignal] = []
        for path in self._cache_files(profile):
            try:
                if path.stat().st_size > 50 * 1024 * 1024:
                    continue
                if not file_mtime_in_window(path, window, slack_days=0):
                    continue
            except OSError:
                continue

            for snippet in self._extract_snippets(path):
                snippets.append(
                    WorkSignal(
                        origin=self.key,
                        workspace=path.parent.name.replace(".indexeddb.leveldb", ""),
                        occurred_at=to_iso(path.stat().st_mtime, window.timezone),
                        note="{} local cache snippet: {}".format(self.label, snippet),
                        paths=[str(path)],
                        confidence="low",
                        extra={
                            "browser": browser_name,
                            "profile": str(profile),
                            "evidence": "browser_leveldb_strings",
                        },
                    )
                )
                if len(snippets) >= 30:
                    return snippets
        return snippets

    def _cache_files(self, profile: Path) -> Iterable[Path]:
        roots = []
        indexed = profile / "IndexedDB"
        if indexed.exists():
            for child in indexed.iterdir():
                marker = child.name.lower()
                if any(item in marker for item in self.cache_origin_markers):
                    roots.append(child)
        local_storage = profile / "Local Storage" / "leveldb"
        if local_storage.exists():
            roots.append(local_storage)

        for root in roots:
            for pattern in ("*.ldb", "*.log"):
                for path in root.glob(pattern):
                    yield path

    def _extract_snippets(self, path: Path) -> List[str]:
        try:
            data = path.read_bytes()
        except OSError:
            return []
        text = data.decode("utf-8", errors="ignore")
        parts = re.split(r"[\x00-\x1f]+", text)
        snippets: List[str] = []
        for part in parts:
            cleaned = compact_text(part, 420)
            if len(cleaned) < 24:
                continue
            lowered = cleaned.lower()
            if not any(keyword in lowered for keyword in self.cache_keywords):
                continue
            if is_low_signal(cleaned):
                continue
            snippets.append(cleaned)
            if len(snippets) >= 8:
                break
        return snippets


def _workspace_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path:
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"c", "g", "codex"}:
            return "{}/{}".format(parts[0], parts[1][:12])
        return parts[0][:60]
    return parsed.netloc or "web"


def _sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl()
