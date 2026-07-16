from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from worktrace_agent.connectors.chatgpt_web import build_chatgpt_web_connector
from worktrace_agent.connectors.claude_code import ClaudeCodeConnector
from worktrace_agent.connectors.codex_cli import CodexCliConnector
from worktrace_agent.connectors.codex_web import build_codex_web_connector
from worktrace_agent.connectors.cursor import CursorConnector
from worktrace_agent.connectors.portable import (
    KNOWN_AGENT_PROFILES,
    PortableAgentConnector,
    profile_from_config,
)
from worktrace_agent.settings import expand_path


def build_connectors(
    settings: Dict[str, Any], requested: Optional[Iterable[str]] = None
):
    requested_set = _expand_requested(requested)
    connector_settings = settings.get("connectors", {})
    connectors = []

    codex_cli = connector_settings.get("codex_cli", {})
    if _enabled("codex_cli", codex_cli, requested_set):
        excluded_ids = {
            str(value)
            for value in codex_cli.get("exclude_conversation_ids", [])
            if str(value)
        }
        if codex_cli.get("include_current_session", False) is not True:
            current_thread_id = os.environ.get("CODEX_THREAD_ID")
            if current_thread_id:
                excluded_ids.add(current_thread_id)
        connectors.append(
            CodexCliConnector(
                root=expand_path(codex_cli.get("root", "~/.codex")),
                include_session_jsonl=(
                    codex_cli.get("include_session_jsonl", True) is True
                ),
                excluded_conversation_ids=excluded_ids,
                max_jsonl_files=int(codex_cli.get("max_jsonl_files", 2000)),
                max_file_bytes=int(codex_cli.get("max_file_mb", 50)) * 1024 * 1024,
                max_messages=int(codex_cli.get("max_messages", 50_000)),
                max_thread_rows=int(codex_cli.get("max_thread_rows", 10_000)),
            )
        )

    codex_web = connector_settings.get("codex_web", {})
    if _enabled("codex_web", codex_web, requested_set):
        connectors.append(build_codex_web_connector(codex_web))

    claude_code = connector_settings.get("claude_code", {})
    if _enabled("claude_code", claude_code, requested_set):
        claude_root_value = claude_code.get("root", "~/.claude")
        if claude_root_value == "~/.claude" and os.environ.get("CLAUDE_CONFIG_DIR"):
            claude_root_value = os.environ["CLAUDE_CONFIG_DIR"]
        excluded_claude_ids = {
            str(value)
            for value in claude_code.get("exclude_session_ids", [])
            if str(value)
        }
        for variable in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            if os.environ.get(variable):
                excluded_claude_ids.add(str(os.environ[variable]))
        connectors.append(
            ClaudeCodeConnector(
                root=expand_path(claude_root_value),
                include_subagents=(claude_code.get("include_subagents", True) is True),
                excluded_session_ids=excluded_claude_ids,
            )
        )

    cursor = connector_settings.get("cursor", {})
    if _enabled("cursor", cursor, requested_set):
        roots = [expand_path(item) for item in cursor.get("roots", [])]
        connectors.append(CursorConnector(roots=roots))

    chatgpt_web = connector_settings.get("chatgpt_web", {})
    if _enabled("chatgpt_web", chatgpt_web, requested_set):
        connectors.append(build_chatgpt_web_connector(chatgpt_web))

    portable = connector_settings.get("agent_sessions", {})
    if portable.get("enabled", True) is True:
        profile_configs = portable.get("profiles", {})
        if not isinstance(profile_configs, dict):
            profile_configs = {}
        for key in sorted(set(KNOWN_AGENT_PROFILES) | set(profile_configs)):
            raw_config = profile_configs.get(key, {})
            config = (
                raw_config if isinstance(raw_config, dict) else {"enabled": raw_config}
            )
            enabled = config.get("enabled", "auto")
            if enabled not in (True, False, "auto"):
                continue
            explicitly_requested = requested_set is not None and key in requested_set
            if requested_set is not None and not explicitly_requested:
                continue
            if enabled is False:
                continue
            profile = profile_from_config(key, config)
            roots = [expand_path(value) for value in profile.roots]
            if (
                enabled == "auto"
                and not explicitly_requested
                and not any(root.exists() for root in roots)
            ):
                continue
            if key in {"qoder", "codebuddy"}:
                selected_roots = [root for root in roots if root.exists()]
                if not selected_roots and roots:
                    selected_roots = [roots[0]]
                for root in selected_roots:
                    connectors.append(
                        ClaudeCodeConnector(
                            root=root,
                            key=profile.key,
                            label=profile.label,
                            include_subagents=True,
                        )
                    )
                continue
            connectors.append(
                PortableAgentConnector(
                    key=profile.key,
                    label=profile.label,
                    roots=roots,
                    patterns=profile.patterns,
                    max_files=int(portable.get("max_files_per_profile", 500)),
                    max_file_bytes=int(portable.get("max_file_mb", 50)) * 1024 * 1024,
                    max_messages=int(portable.get("max_messages_per_profile", 10_000)),
                    support_note=profile.support_note,
                )
            )

    return connectors


def _enabled(key: str, config: Dict[str, Any], requested: Optional[Set[str]]) -> bool:
    if requested is not None and key not in requested:
        return False
    return config.get("enabled", True) is True


def _expand_requested(requested: Optional[Iterable[str]]) -> Optional[Set[str]]:
    if requested is None:
        return None
    expanded: Set[str] = set()
    for item in requested:
        normalized = item.strip()
        if not normalized or normalized == "all":
            return None
        if normalized == "codex":
            expanded.update({"codex_cli", "codex_web"})
        elif normalized in {"claude", "claudecode", "claude-code"}:
            expanded.add("claude_code")
        elif normalized == "web":
            expanded.update({"codex_web", "chatgpt_web"})
        elif normalized in {"domestic", "china", "cn"}:
            expanded.update(
                {
                    "zcode",
                    "trae",
                    "qoder",
                    "codebuddy",
                    "tongyi_lingma",
                    "comate",
                    "kimi_cli",
                    "kimi_code",
                    "qwen_code",
                }
            )
        elif normalized in {"agents", "coding-agents"}:
            expanded.update(KNOWN_AGENT_PROFILES)
            expanded.update({"codex_cli", "claude_code", "cursor"})
        else:
            expanded.add(normalized)
    return expanded


def configured_root_summary(settings: Dict[str, Any]) -> List[str]:
    connector_settings = settings.get("connectors", {})
    rows: List[str] = []
    codex_root = connector_settings.get("codex_cli", {}).get("root", "~/.codex")
    rows.append("codex_cli: {}".format(Path(codex_root).expanduser()))
    claude_root = connector_settings.get("claude_code", {}).get("root", "~/.claude")
    rows.append("claude_code: {}".format(Path(claude_root).expanduser()))
    portable = connector_settings.get("agent_sessions", {})
    profile_configs = portable.get("profiles", {})
    if not isinstance(profile_configs, dict):
        profile_configs = {}
    detected = []
    available = sorted(set(KNOWN_AGENT_PROFILES) | set(profile_configs))
    for key in available:
        raw_config = profile_configs.get(key, {})
        config = raw_config if isinstance(raw_config, dict) else {"enabled": raw_config}
        if config.get("enabled", "auto") is False:
            continue
        profile = profile_from_config(key, config)
        if any(expand_path(root).exists() for root in profile.roots):
            detected.append(key)
    rows.append(
        "portable agent profiles: {} (detected: {})".format(
            ", ".join(available), ", ".join(detected) or "none"
        )
    )
    return rows
