from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

from worktrace_agent.storage import ensure_private_directory


def run_codex_draft(
    prompt: str,
    output_path: Path,
    settings: Dict,
    cwd: Optional[Path] = None,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    output_schema: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    """Run model-only synthesis in an isolated, offline Codex invocation."""

    codex_settings = settings.get("codex", {})
    command = codex_settings.get("command", "codex")
    selected_model = model or codex_settings.get("model")
    effort = reasoning_effort or codex_settings.get("reasoning_effort", "medium")
    timeout_seconds = int(codex_settings.get("timeout_seconds", 900))
    ensure_private_directory(output_path.parent)
    output_path.touch(mode=0o600, exist_ok=True)
    output_path.chmod(0o600)

    # `cwd` is retained for backwards-compatible callers but intentionally not
    # exposed to the model-only child.
    _ = cwd
    with tempfile.TemporaryDirectory(prefix="worktrace-synthesis-") as temp_name:
        isolation_root = Path(temp_name)
        workspace, codex_home = _prepare_isolation(isolation_root)
        cmd = _base_isolated_command(
            command=command,
            workspace=workspace,
            effort=effort,
            web_search="disabled",
            selected_model=selected_model,
        )
        if output_schema is not None:
            cmd.extend(["--output-schema", str(output_schema)])
        cmd.extend(["-o", str(output_path), "-"])
        return subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            env=_minimal_codex_environment(isolation_root, codex_home),
        )


def run_codex_research(
    prompt: str,
    output_path: Path,
    settings: Dict,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    output_schema: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    """Run a web-only Codex turn with no local agent tools or user extensions.

    The model receives only the already-redacted prompt. Its native web-search
    tool is enabled explicitly, while shell, apps, hooks, subagents, memories,
    plugins, and the user's Codex configuration are disabled. An empty working
    directory and isolated CODEX_HOME prevent automatic project/skill context
    from crossing into the networked stage.
    """

    codex_settings = settings.get("codex", {})
    research_settings = settings.get("research", {})
    command = codex_settings.get("command", "codex")
    selected_model = model or codex_settings.get("model")
    effort = reasoning_effort or codex_settings.get("reasoning_effort", "medium")
    timeout_seconds = int(codex_settings.get("timeout_seconds", 900))
    search_mode = str(research_settings.get("web_search", "live"))
    if search_mode not in {"cached", "indexed", "live"}:
        search_mode = "live"

    ensure_private_directory(output_path.parent)
    output_path.touch(mode=0o600, exist_ok=True)
    output_path.chmod(0o600)

    with tempfile.TemporaryDirectory(prefix="worktrace-research-") as temp_name:
        isolation_root = Path(temp_name)
        workspace, codex_home = _prepare_isolation(isolation_root)
        cmd = [command]
        if search_mode == "live":
            # --search is a global Codex flag and must precede the `exec`
            # subcommand. The explicit web_search config covers cached/indexed.
            cmd.append("--search")
        cmd.extend(
            _base_isolated_command(
                command=command,
                workspace=workspace,
                effort=effort,
                web_search=search_mode,
                selected_model=selected_model,
            )[1:]
        )
        if output_schema is not None:
            cmd.extend(["--output-schema", str(output_schema)])
        cmd.extend(["-o", str(output_path), "-"])
        return subprocess.run(
            cmd,
            input=prompt,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            env=_minimal_codex_environment(isolation_root, codex_home),
        )


def _base_isolated_command(
    command: str,
    workspace: Path,
    effort: str,
    web_search: str,
    selected_model: Optional[str],
) -> list[str]:
    command_parts = [command, "exec"]
    if selected_model:
        command_parts.extend(["-m", str(selected_model)])
    command_parts.extend(
        [
            "-c",
            'model_reasoning_effort="{}"'.format(effort),
            "-c",
            'web_search="{}"'.format(web_search),
        ]
    )
    for feature in (
        "shell_tool",
        "unified_exec",
        "shell_snapshot",
        "apps",
        "hooks",
        "multi_agent",
        "goals",
        "memories",
        "plugins",
        "plugin_sharing",
        "remote_plugin",
        "computer_use",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "in_app_browser",
        "image_generation",
        "artifact",
        "workspace_dependencies",
        "code_mode",
        "code_mode_host",
        "enable_mcp_apps",
        "skill_mcp_dependency_install",
        "tool_call_mcp_elicitation",
        "tool_suggest",
        "request_permissions_tool",
        "auth_elicitation",
        "network_proxy",
    ):
        command_parts.extend(["-c", "features.{}=false".format(feature)])
    command_parts.extend(
        [
            "-c",
            'approval_policy="never"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "-s",
            "read-only",
            "-C",
            str(workspace),
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    )
    return command_parts


def _prepare_isolation(isolation_root: Path) -> tuple[Path, Path]:
    workspace = isolation_root / "workspace"
    codex_home = isolation_root / "codex-home"
    home = isolation_root / "home"
    temp = isolation_root / "tmp"
    for path in (workspace, codex_home, home, temp):
        path.mkdir(mode=0o700)
    _copy_codex_auth_only(codex_home)
    return workspace, codex_home


def _minimal_codex_environment(
    isolation_root: Path, codex_home: Path
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
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    }
    environment = {
        key: value for key, value in os.environ.items() if key in allowed and value
    }
    environment["HOME"] = str(isolation_root / "home")
    environment["TMPDIR"] = str(isolation_root / "tmp")
    environment["TMP"] = str(isolation_root / "tmp")
    environment["TEMP"] = str(isolation_root / "tmp")
    environment["CODEX_HOME"] = str(codex_home)
    environment["WORKTRACE_INNER_GENERATION"] = "1"
    environment["WORKTRACE_RESEARCH_ONLY"] = "1"
    environment["NO_COLOR"] = "1"
    return environment


def _copy_codex_auth_only(destination: Path) -> None:
    source_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    source = source_home / "auth.json"
    if not source.is_file():
        return
    target = destination / "auth.json"
    shutil.copyfile(source, target)
    target.chmod(0o600)
