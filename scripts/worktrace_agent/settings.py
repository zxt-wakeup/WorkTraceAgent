from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from worktrace_agent.window import DEFAULT_TIMEZONE, get_zone
from worktrace_agent.storage import (
    ensure_private_directory,
    validate_artifact_root,
    write_private_json,
)


def _is_real_source_skill_root(candidate: Path) -> bool:
    source_module = candidate / "scripts" / "worktrace_agent" / "settings.py"
    return (
        (candidate / "skills" / "worktrace-report" / "SKILL.md").is_file()
        and (candidate / "scripts" / "worktrace.py").is_file()
        and source_module.is_file()
        and source_module.resolve() == Path(__file__).resolve()
    )


_SOURCE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "worktrace-agent" / "settings.json"
_SOURCE_SKILL_ROOT = _SOURCE_ROOT if _is_real_source_skill_root(_SOURCE_ROOT) else None
SKILL_ROOT = (
    _SOURCE_SKILL_ROOT
    if _SOURCE_SKILL_ROOT is not None
    else DEFAULT_CONFIG_PATH.parent.resolve()
)
PROJECT_CONFIG_PATH = (
    _SOURCE_SKILL_ROOT / "worktrace.settings.json"
    if _SOURCE_SKILL_ROOT is not None
    else None
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "connectors": {
        "codex_cli": {
            "enabled": True,
            "root": "~/.codex",
            "include_session_jsonl": True,
            "include_current_session": False,
            "exclude_conversation_ids": [],
            "max_jsonl_files": 2000,
            "max_file_mb": 50,
            "max_messages": 50000,
            "max_thread_rows": 10000,
        },
        "claude_code": {
            "enabled": True,
            "root": "~/.claude",
            "include_subagents": True,
            "exclude_session_ids": [],
        },
        "codex_web": {
            "enabled": False,
            "browser_profiles": [],
        },
        "cursor": {
            "enabled": "auto",
            "roots": [
                "~/Library/Application Support/Cursor",
                "~/Library/Application Support/Cursor - Insiders",
                "~/Library/Application Support/Cursor Nightly",
            ],
        },
        "chatgpt_web": {
            "enabled": False,
            "browser_profiles": [],
            "export_paths": [],
            "auto_discover_exports": True,
            "include_browser_evidence": False,
        },
        "agent_sessions": {
            "enabled": True,
            "max_files_per_profile": 500,
            "max_file_mb": 50,
            "max_messages_per_profile": 10000,
            "profiles": {
                "zcode": {"enabled": "auto"},
                "trae": {"enabled": "auto"},
                "qoder": {"enabled": "auto"},
                "codebuddy": {"enabled": "auto"},
                "tongyi_lingma": {"enabled": "auto"},
                "comate": {"enabled": "auto"},
                "kimi_cli": {"enabled": "auto"},
                "kimi_code": {"enabled": "auto"},
                "qwen_code": {"enabled": "auto"},
                "gemini_cli": {"enabled": "auto"},
                "opencode": {"enabled": "auto"},
                "github_copilot": {"enabled": "auto"},
                "cline": {"enabled": "auto"},
                "roo_code": {"enabled": "auto"},
                "windsurf": {"enabled": "auto"},
                "factory_droid": {"enabled": "auto"},
            },
        },
    },
    "artifacts": {
        "directory": "~/.local/state/worktrace-agent/artifacts",
        "timezone": DEFAULT_TIMEZONE,
        "retention_days": 30,
    },
    "okr": {
        "path": "~/.config/worktrace-agent/okr.md",
        "required": False,
        "max_chars": 20_000,
    },
    "weekly_report_reference": {
        "path": "~/.config/worktrace-agent/weekly-report-reference.md",
        "max_chars": 200_000,
    },
    "context": {
        "max_chars": 0,
    },
    "generation": {
        "agent": "auto",
        "model": "",
        "timeout_seconds": 900,
        "chunk_chars": 250000,
        "max_parallel_chunks": 4,
        "runners": {
            "codex_cli": {
                "enabled": True,
                "command": "codex",
                "adapter": "codex",
                "model": "gpt-5.4-mini",
                "cost_rank": 10,
                "origins": ["codex", "codex_cli"],
            },
            "claude_code": {
                "enabled": True,
                "command": "claude",
                "adapter": "claude",
                "model": "haiku",
                "response_field": "result",
                "cost_rank": 20,
                "origins": ["claude", "claude_code"],
            },
            "gemini_cli": {
                "enabled": True,
                "command": "gemini",
                "adapter": "gemini",
                "model": "gemini-2.5-flash",
                "response_field": "response",
                "cost_rank": 10,
                "origins": ["gemini", "gemini_cli"],
            },
            "opencode": {
                "enabled": False,
                "command": "opencode",
                "adapter": "generic",
                "args": ["run"],
                "response_field": "",
                "model": "",
                "cost_rank": 50,
                "origins": ["opencode"],
            },
        },
    },
    "research": {
        "enabled": True,
        "mode": "auto",
        "max_suggestions": 4,
        "privacy_mode": "strict",
        "web_search": "live",
        "private_terms": [],
        "aihot": {"enabled": True},
    },
    "schedule": {
        "default_time": "19:00",
    },
    "codex": {
        "command": "codex",
        "model": "",
        "reasoning_effort": "medium",
        "timeout_seconds": 900,
    },
}


def load_settings(path: Optional[Path] = None) -> Dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    explicit_path = path.expanduser().resolve() if path is not None else None
    if explicit_path is not None and not explicit_path.is_file():
        raise FileNotFoundError("settings file not found: {}".format(explicit_path))
    if explicit_path is not None:
        paths = [explicit_path]
    else:
        paths = []
        if PROJECT_CONFIG_PATH is not None:
            paths.append(PROJECT_CONFIG_PATH)
        paths.append(DEFAULT_CONFIG_PATH)
    for config_path in paths:
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict):
            raise ValueError(
                "settings file must contain a JSON object: {}".format(config_path)
            )
        loaded = _migrate_legacy_context_settings(loaded)
        loaded = _resolve_loaded_relative_paths(loaded, config_path.resolve().parent)
        settings = merge_dicts(settings, loaded)
    _validate_connector_settings(settings.get("connectors"))
    settings["research"] = _normalize_research_settings(settings.get("research"))
    _validate_runtime_settings(settings)
    return settings


def resolve_config_path() -> Path:
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    if PROJECT_CONFIG_PATH is not None and PROJECT_CONFIG_PATH.exists():
        return PROJECT_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def write_default_settings(path: Optional[Path] = None) -> Path:
    config_path = path or DEFAULT_CONFIG_PATH
    ensure_private_directory(config_path.parent)
    if not config_path.exists():
        write_private_json(config_path, DEFAULT_SETTINGS)
    return config_path


def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _migrate_legacy_context_settings(value: Dict[str, Any]) -> Dict[str, Any]:
    """Map the former truncation defaults to the new lossless/unlimited mode."""

    migrated = copy.deepcopy(value)
    context = migrated.get("context")
    if not isinstance(context, dict):
        return migrated
    if (
        context.get("max_chars") == 400_000
        and context.get("per_message_chars") == 12_000
    ):
        context["max_chars"] = 0
        context.pop("per_message_chars", None)
    return migrated


def expand_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = SKILL_ROOT / path
    return path.resolve()


def _resolve_loaded_relative_paths(
    loaded: Dict[str, Any], base_directory: Path
) -> Dict[str, Any]:
    """Bind configured filesystem paths to the file that declared them."""

    value = copy.deepcopy(loaded)
    _resolve_mapping_path(value.get("artifacts"), "directory", base_directory)
    _resolve_mapping_path(value.get("okr"), "path", base_directory)
    _resolve_mapping_path(value.get("weekly_report_reference"), "path", base_directory)

    connectors = value.get("connectors")
    if not isinstance(connectors, dict):
        return value
    _resolve_mapping_path(connectors.get("codex_cli"), "root", base_directory)
    _resolve_mapping_path(connectors.get("claude_code"), "root", base_directory)
    _resolve_mapping_paths(connectors.get("cursor"), "roots", base_directory)
    _resolve_mapping_paths(
        connectors.get("codex_web"), "browser_profiles", base_directory
    )
    chatgpt = connectors.get("chatgpt_web")
    _resolve_mapping_paths(chatgpt, "browser_profiles", base_directory)
    _resolve_mapping_paths(chatgpt, "export_paths", base_directory)

    portable = connectors.get("agent_sessions")
    profiles = portable.get("profiles") if isinstance(portable, dict) else None
    if isinstance(profiles, dict):
        for profile in profiles.values():
            _resolve_mapping_paths(profile, "roots", base_directory)
    return value


def _resolve_mapping_path(value: Any, key: str, base_directory: Path) -> None:
    if isinstance(value, dict) and isinstance(value.get(key), str) and value[key]:
        value[key] = str(_resolve_declared_path(value[key], base_directory))


def _resolve_mapping_paths(value: Any, key: str, base_directory: Path) -> None:
    if not isinstance(value, dict) or not isinstance(value.get(key), list):
        return
    value[key] = [
        str(_resolve_declared_path(item, base_directory))
        if isinstance(item, str)
        else item
        for item in value[key]
    ]


def _resolve_declared_path(value: str, base_directory: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_directory / path
    return path.resolve()


def artifact_dir(settings: Dict[str, Any]) -> Path:
    value = settings.get("artifacts", {}).get(
        "directory", "~/.local/state/worktrace-agent/artifacts"
    )
    return expand_path(str(value))


def _normalize_research_settings(value: Any) -> Dict[str, Any]:
    """Validate active research settings and discard unknown/deprecated fields."""

    if not isinstance(value, dict):
        raise ValueError("research settings must be a JSON object")
    defaults = DEFAULT_SETTINGS["research"]

    enabled = value.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("research.enabled must be a boolean")

    mode = value.get("mode", defaults["mode"])
    if not isinstance(mode, str) or mode not in {"auto", "off", "required"}:
        raise ValueError("research.mode must be auto, off, or required")

    max_suggestions = value.get("max_suggestions", defaults["max_suggestions"])
    if (
        isinstance(max_suggestions, bool)
        or not isinstance(max_suggestions, int)
        or not 0 <= max_suggestions <= 4
    ):
        raise ValueError("research.max_suggestions must be an integer from 0 to 4")

    privacy_mode = value.get("privacy_mode", defaults["privacy_mode"])
    if privacy_mode != "strict":
        raise ValueError("research.privacy_mode must be strict")

    web_search = value.get("web_search", defaults["web_search"])
    if not isinstance(web_search, str) or web_search not in {
        "cached",
        "indexed",
        "live",
    }:
        raise ValueError("research.web_search must be cached, indexed, or live")

    private_terms = value.get("private_terms", defaults["private_terms"])
    if not isinstance(private_terms, list) or any(
        not isinstance(item, str) for item in private_terms
    ):
        raise ValueError("research.private_terms must be an array of strings")
    if len(private_terms) > 100 or any(len(item) > 256 for item in private_terms):
        raise ValueError("research.private_terms exceeds the safety limit")

    aihot = value.get("aihot", defaults["aihot"])
    if not isinstance(aihot, dict):
        raise ValueError("research.aihot must be a JSON object")
    aihot_enabled = aihot.get("enabled", defaults["aihot"]["enabled"])
    if not isinstance(aihot_enabled, bool):
        raise ValueError("research.aihot.enabled must be a boolean")

    return {
        "enabled": enabled,
        "mode": mode,
        "max_suggestions": max_suggestions,
        "privacy_mode": privacy_mode,
        "web_search": web_search,
        "private_terms": private_terms,
        "aihot": {"enabled": aihot_enabled},
    }


def _validate_connector_settings(value: Any) -> None:
    """Fail closed when JSON strings could otherwise become truthy privacy flags."""

    if not isinstance(value, dict):
        raise ValueError("connectors settings must be a JSON object")
    boolean_fields = {
        "codex_cli": ("enabled", "include_session_jsonl", "include_current_session"),
        "claude_code": ("enabled", "include_subagents"),
        "codex_web": ("enabled",),
        "chatgpt_web": (
            "enabled",
            "include_browser_evidence",
            "auto_discover_exports",
        ),
    }
    for name, fields in boolean_fields.items():
        config = value.get(name, {})
        if not isinstance(config, dict):
            raise ValueError("connectors.{} must be a JSON object".format(name))
        for field in fields:
            if field in config and not isinstance(config[field], bool):
                raise ValueError(
                    "connectors.{}.{} must be a boolean".format(name, field)
                )
        for list_field in (
            "browser_profiles",
            "export_paths",
            "roots",
            "exclude_conversation_ids",
            "exclude_session_ids",
        ):
            if list_field in config and (
                not isinstance(config[list_field], list)
                or any(not isinstance(item, str) for item in config[list_field])
            ):
                raise ValueError(
                    "connectors.{}.{} must be an array of strings".format(
                        name, list_field
                    )
                )
        if "root" in config and not isinstance(config["root"], str):
            raise ValueError("connectors.{}.root must be a string".format(name))

    cursor = value.get("cursor", {})
    if not isinstance(cursor, dict):
        raise ValueError("connectors.cursor must be a JSON object")
    cursor_enabled = cursor.get("enabled", "auto")
    if not isinstance(cursor_enabled, bool) and cursor_enabled != "auto":
        raise ValueError("connectors.cursor.enabled must be true, false, or auto")
    cursor_roots = cursor.get("roots", [])
    if not isinstance(cursor_roots, list) or any(
        not isinstance(item, str) for item in cursor_roots
    ):
        raise ValueError("connectors.cursor.roots must be an array of strings")

    codex_cli = value.get("codex_cli", {})
    codex_limits = {
        "max_jsonl_files": 100_000,
        "max_file_mb": 1_024,
        "max_messages": 1_000_000,
        "max_thread_rows": 1_000_000,
    }
    for field, maximum in codex_limits.items():
        if field not in codex_cli:
            continue
        item = codex_cli[field]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= maximum
        ):
            raise ValueError(
                "connectors.codex_cli.{} must be an integer from 0 to {}".format(
                    field, maximum
                )
            )

    portable = value.get("agent_sessions", {})
    if not isinstance(portable, dict):
        raise ValueError("connectors.agent_sessions must be a JSON object")
    if "enabled" in portable and not isinstance(portable["enabled"], bool):
        raise ValueError("connectors.agent_sessions.enabled must be a boolean")
    numeric_limits = {
        "max_files_per_profile": 100_000,
        "max_file_mb": 1_024,
        "max_messages_per_profile": 1_000_000,
    }
    for field, maximum in numeric_limits.items():
        if field not in portable:
            continue
        item = portable[field]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= maximum
        ):
            raise ValueError(
                "connectors.agent_sessions.{} must be an integer from 0 to {}".format(
                    field, maximum
                )
            )
    profiles = portable.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("connectors.agent_sessions.profiles must be a JSON object")
    for key, raw_config in profiles.items():
        if isinstance(raw_config, (bool, str)):
            enabled = raw_config
            config = {}
        elif isinstance(raw_config, dict):
            config = raw_config
            enabled = config.get("enabled", "auto")
        else:
            raise ValueError("agent profile {} must be an object".format(key))
        if enabled not in (True, False, "auto"):
            raise ValueError(
                "agent profile {}.enabled must be true, false, or auto".format(key)
            )
        for list_field in ("roots", "patterns"):
            if list_field in config and (
                not isinstance(config[list_field], list)
                or any(not isinstance(item, str) for item in config[list_field])
            ):
                raise ValueError(
                    "agent profile {}.{} must be an array of strings".format(
                        key, list_field
                    )
                )
        if "label" in config and not isinstance(config["label"], str):
            raise ValueError("agent profile {}.label must be a string".format(key))
        if "support_note" in config and not isinstance(config["support_note"], str):
            raise ValueError(
                "agent profile {}.support_note must be a string".format(key)
            )


def _validate_runtime_settings(settings: Dict[str, Any]) -> None:
    artifacts = _object_section(settings, "artifacts")
    if not isinstance(artifacts.get("directory"), str) or not artifacts["directory"]:
        raise ValueError("artifacts.directory must be a non-empty string")
    validate_artifact_root(expand_path(artifacts["directory"]))
    timezone = artifacts.get("timezone")
    if not isinstance(timezone, str):
        raise ValueError("artifacts.timezone must be a timezone string")
    get_zone(timezone)
    _bounded_integer(artifacts, "retention_days", 0, 3650, "artifacts")

    okr = _object_section(settings, "okr")
    if not isinstance(okr.get("path"), str) or not okr["path"]:
        raise ValueError("okr.path must be a non-empty string")
    if not isinstance(okr.get("required"), bool):
        raise ValueError("okr.required must be a boolean")
    _bounded_integer(okr, "max_chars", 1, 1_000_000, "okr")

    weekly_reference = _object_section(settings, "weekly_report_reference")
    if (
        not isinstance(weekly_reference.get("path"), str)
        or not weekly_reference["path"]
    ):
        raise ValueError("weekly_report_reference.path must be a non-empty string")
    _bounded_integer(
        weekly_reference,
        "max_chars",
        1,
        2_000_000,
        "weekly_report_reference",
    )

    context = _object_section(settings, "context")
    _bounded_integer(context, "max_chars", 0, 100_000_000, "context")

    generation = _object_section(settings, "generation")
    if not isinstance(generation.get("agent"), str) or not generation["agent"]:
        raise ValueError("generation.agent must be a non-empty string")
    if not isinstance(generation.get("model"), str):
        raise ValueError("generation.model must be a string")
    _bounded_integer(generation, "timeout_seconds", 1, 7200, "generation")
    _bounded_integer(generation, "chunk_chars", 50_000, 2_000_000, "generation")
    _bounded_integer(generation, "max_parallel_chunks", 1, 8, "generation")
    runners = generation.get("runners")
    if not isinstance(runners, dict) or not runners:
        raise ValueError("generation.runners must be a non-empty JSON object")
    for key, runner in runners.items():
        if not isinstance(key, str) or not key or not isinstance(runner, dict):
            raise ValueError("generation runner entries must be named JSON objects")
        if not isinstance(runner.get("enabled"), bool):
            raise ValueError("generation runner {}.enabled must be boolean".format(key))
        if not isinstance(runner.get("command"), str) or not runner["command"]:
            raise ValueError(
                "generation runner {}.command must be non-empty".format(key)
            )
        if runner.get("adapter") not in {"codex", "claude", "gemini", "generic"}:
            raise ValueError("generation runner {}.adapter is invalid".format(key))
        if not isinstance(runner.get("model", ""), str):
            raise ValueError("generation runner {}.model must be a string".format(key))
        _bounded_integer(
            runner, "cost_rank", 0, 1000, "generation runner {}".format(key)
        )
        origins = runner.get("origins")
        if (
            not isinstance(origins, list)
            or not origins
            or any(not isinstance(item, str) or not item for item in origins)
        ):
            raise ValueError("generation runner {}.origins must be strings".format(key))
        if "args" in runner and (
            not isinstance(runner["args"], list)
            or any(not isinstance(item, str) for item in runner["args"])
        ):
            raise ValueError("generation runner {}.args must be strings".format(key))
        if "env_allowlist" in runner and (
            not isinstance(runner["env_allowlist"], list)
            or any(
                not isinstance(item, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item)
                for item in runner["env_allowlist"]
            )
        ):
            raise ValueError(
                "generation runner {}.env_allowlist must be environment variable names".format(
                    key
                )
            )
        if not isinstance(runner.get("response_field", ""), str):
            raise ValueError(
                "generation runner {}.response_field must be a string".format(key)
            )

    schedule = _object_section(settings, "schedule")
    schedule_time = schedule.get("default_time")
    if not isinstance(schedule_time, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", schedule_time
    ):
        raise ValueError("schedule.default_time must use HH:MM")

    codex = _object_section(settings, "codex")
    if not isinstance(codex.get("command"), str) or not codex["command"]:
        raise ValueError("codex.command must be a non-empty string")
    if not isinstance(codex.get("model"), str):
        raise ValueError("codex.model must be a string")
    effort = codex.get("reasoning_effort")
    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("codex.reasoning_effort is invalid")
    _bounded_integer(codex, "timeout_seconds", 1, 7200, "codex")


def _object_section(settings: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = settings.get(key)
    if not isinstance(value, dict):
        raise ValueError("{} settings must be a JSON object".format(key))
    return value


def _bounded_integer(
    value: Dict[str, Any], key: str, minimum: int, maximum: int, section: str
) -> None:
    item = value.get(key)
    if (
        isinstance(item, bool)
        or not isinstance(item, int)
        or not minimum <= item <= maximum
    ):
        raise ValueError(
            "{}.{} must be an integer from {} to {}".format(
                section, key, minimum, maximum
            )
        )
