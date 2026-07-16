from __future__ import annotations

from typing import Any, Dict

from worktrace_agent.connectors.browser import BrowserConversationConnector
from worktrace_agent.schema import ConnectorResult, SourceCoverage


class CodexWebConnector:
    key = "codex_web"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.browser = BrowserConversationConnector(
            key=self.key,
            label="Codex Web",
            browser_profiles=config.get("browser_profiles") or [],
            url_patterns=[
                "codex.openai.com",
                "chatgpt.com/codex",
                "chat.openai.com/codex",
            ],
            cache_origin_markers=[
                "https_codex.openai.com",
                "https_chatgpt.com",
                "https_chat.openai.com",
            ],
            cache_keywords=["codex", "task", "thread", "conversation", "prompt"],
        )

    def scan(self, window):
        signals = self.browser.scan(window)
        return ConnectorResult(
            signals=signals,
            coverage=[
                SourceCoverage(
                    source=self.key,
                    status="partial" if signals else "empty",
                    detail="Browser history/cache is discovery-only and never proves a full Codex transcript",
                )
            ],
        )


def build_codex_web_connector(config: Dict[str, Any]) -> CodexWebConnector:
    return CodexWebConnector(config)
