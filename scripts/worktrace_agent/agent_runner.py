from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from worktrace_agent.codex_runner import run_codex_draft
from worktrace_agent.schema import TraceBundle
from worktrace_agent.storage import ensure_private_directory, write_private_text


AGENT_COMMANDS = {
    "codex_cli": "codex",
    "claude_code": "claude",
    "gemini_cli": "gemini",
    "opencode": "opencode",
    "zcode": "zcode",
    "qwen_code": "qwen",
    "kimi_cli": "kimi",
    "codebuddy": "codebuddy",
    "qoder": "qoder",
}
AGENT_ALIASES = {
    "codex": "codex_cli",
    "claude": "claude_code",
    "gemini": "gemini_cli",
    "qwen": "qwen_code",
    "kimi": "kimi_cli",
}


@dataclass(frozen=True)
class GenerationSelection:
    agent: str
    adapter: str
    command: str
    model: str
    usage_messages: int
    cost_rank: int
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_local_agents(settings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return executable presence separately from automatic runner support."""

    generation = settings.get("generation", {})
    runners = generation.get("runners", {})
    names = set(AGENT_COMMANDS) | set(runners)
    detected: Dict[str, Dict[str, Any]] = {}
    for name in sorted(names):
        runner = runners.get(name, {}) if isinstance(runners, dict) else {}
        command = str(runner.get("command") or AGENT_COMMANDS.get(name) or name)
        executable = shutil.which(command)
        detected[name] = {
            "command": command,
            "path": executable or "",
            "installed": executable is not None,
            "generation_enabled": bool(runner.get("enabled", False)),
            "adapter": str(runner.get("adapter", "")),
        }
    return detected


def usage_by_agent(bundle: TraceBundle, settings: Dict[str, Any]) -> Dict[str, int]:
    runners = settings.get("generation", {}).get("runners", {})
    result: Dict[str, int] = {str(key): 0 for key in runners}
    for conversation in bundle.conversations:
        origin = str(conversation.origin or "").strip().lower()
        count = len(conversation.messages)
        for key, runner in runners.items():
            origins = {str(item).strip().lower() for item in runner.get("origins", [])}
            if origin in origins:
                result[str(key)] += count
    return result


def select_generation_agent(
    bundle: TraceBundle,
    settings: Dict[str, Any],
    requested_agent: Optional[str] = None,
    requested_model: Optional[str] = None,
) -> GenerationSelection:
    generation = settings.get("generation", {})
    runners = generation.get("runners", {})
    detected = detect_local_agents(settings)
    usage = usage_by_agent(bundle, settings)
    requested = str(requested_agent or generation.get("agent") or "auto").strip()
    requested = AGENT_ALIASES.get(requested, requested)

    available = []
    for key, runner in runners.items():
        if not runner.get("enabled", False):
            continue
        if not detected.get(key, {}).get("installed", False):
            continue
        available.append((str(key), runner))
    if requested != "auto":
        if requested not in runners:
            raise ValueError("unknown generation agent: {}".format(requested))
        runner = runners[requested]
        if not runner.get("enabled", False):
            raise ValueError("generation agent is disabled: {}".format(requested))
        if not detected.get(requested, {}).get("installed", False):
            raise OSError(
                "generation agent executable is not installed: {} ({})".format(
                    requested, runner.get("command")
                )
            )
        key = requested
        reason = "user override"
    else:
        if not available:
            raise OSError(
                "no enabled coding-agent generator is installed; use --no-model or configure generation.runners"
            )
        # Primary key: actual accepted transcript messages in this period.
        # Tie-breakers: cheaper configured model, then stable runner name.
        key, runner = min(
            available,
            key=lambda item: (
                -usage.get(item[0], 0),
                int(item[1].get("cost_rank", 1000)),
                item[0],
            ),
        )
        reason = "highest period usage; cost rank breaks ties"

    selected_model = str(
        requested_model or generation.get("model") or runner.get("model") or ""
    )
    return GenerationSelection(
        agent=key,
        adapter=str(runner.get("adapter")),
        command=str(runner.get("command")),
        model=selected_model,
        usage_messages=usage.get(key, 0),
        cost_rank=int(runner.get("cost_rank", 1000)),
        reason=reason,
    )


def run_generation_draft(
    selection: GenerationSelection,
    prompt: str,
    output_path: Path,
    settings: Dict[str, Any],
    output_schema: Optional[Path] = None,
    reasoning_effort: Optional[str] = None,
) -> subprocess.CompletedProcess:
    runner = settings["generation"]["runners"][selection.agent]
    if selection.adapter == "codex":
        codex_settings = dict(settings)
        codex_settings["codex"] = dict(settings.get("codex", {}))
        codex_settings["codex"]["command"] = selection.command
        return run_codex_draft(
            prompt=prompt,
            output_path=output_path,
            output_schema=output_schema,
            settings=codex_settings,
            model=selection.model or None,
            reasoning_effort=reasoning_effort,
        )

    timeout = int(settings.get("generation", {}).get("timeout_seconds", 900))
    ensure_private_directory(output_path.parent)
    output_path.touch(mode=0o600, exist_ok=True)
    output_path.chmod(0o600)
    with tempfile.TemporaryDirectory(prefix="worktrace-agent-generation-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        home = root / "home"
        workspace.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        _copy_runner_auth_only(selection.adapter, home)
        environment = _minimal_environment(home, runner.get("env_allowlist", []))
        command = _runner_command(selection, runner, output_schema)
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            cwd=workspace,
            env=environment,
        )
        if completed.returncode == 0:
            response = _extract_response(
                completed.stdout, str(runner.get("response_field", ""))
            )
            write_private_text(output_path, response)
        return completed


def _runner_command(
    selection: GenerationSelection,
    runner: Dict[str, Any],
    output_schema: Optional[Path],
) -> list[str]:
    if selection.adapter == "claude":
        command = [
            selection.command,
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--permission-mode",
            "plan",
            "--disallowedTools",
            "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task",
        ]
        if selection.model:
            command.extend(["--model", selection.model])
        return command
    if selection.adapter == "gemini":
        command = [
            selection.command,
            "--output-format",
            "json",
            "--allowed-tools",
            "",
        ]
        if selection.model:
            command.extend(["--model", selection.model])
        return command

    values = {
        "model": selection.model,
        "schema": str(output_schema or ""),
    }
    return [selection.command] + [
        str(item).format_map(values) for item in runner.get("args", [])
    ]


def _extract_response(stdout: str, response_field: str) -> str:
    if not response_field:
        return stdout.strip()
    value: Any = json.loads(stdout)
    for part in response_field.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(
                "coding-agent response is missing configured field {}".format(
                    response_field
                )
            )
        value = value[part]
    if not isinstance(value, str):
        raise ValueError("configured coding-agent response field is not text")
    return value.strip()


def _minimal_environment(
    home: Path, extra_allowed: Iterable[str] = ()
) -> Dict[str, str]:
    allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    }
    allowed.update(str(item) for item in extra_allowed)
    result = {
        key: value for key, value in os.environ.items() if key in allowed and value
    }
    result.update(
        {
            "HOME": str(home),
            "TMPDIR": str(home),
            "NO_COLOR": "1",
            "WORKTRACE_INNER_GENERATION": "1",
        }
    )
    return result


def _copy_runner_auth_only(adapter: str, home: Path) -> None:
    candidates = {
        "claude": [Path.home() / ".claude" / ".credentials.json"],
        "gemini": [
            Path.home() / ".gemini" / "oauth_creds.json",
            Path.home() / ".gemini" / "google_accounts.json",
        ],
    }.get(adapter, [])
    for source in candidates:
        if not source.is_file():
            continue
        relative = source.relative_to(Path.home())
        target = home / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)


def installed_agent_names(detected: Dict[str, Dict[str, Any]]) -> Iterable[str]:
    return (name for name, item in detected.items() if item.get("installed"))
