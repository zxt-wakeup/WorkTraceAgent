from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


SCHEDULE_LABEL = "com.worktrace-agent.daily"
DEFAULT_SCHEDULE_TIME = "19:00"
MODULE_ENTRY = "worktrace_agent.cli"
DEFAULT_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "{}.plist".format(SCHEDULE_LABEL)
)
DEFAULT_LOG_DIR = Path.home() / ".local" / "state" / "worktrace-agent"


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    loaded: bool
    time: Optional[str]
    plist_path: Path
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    stale: bool = False


def parse_daily_time(value: str) -> Tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        raise ValueError("time must use HH:MM, for example 19:00")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("time must be between 00:00 and 23:59")
    return hour, minute


def build_launch_agent(
    time_text: str,
    project_dir: Path,
    python_executable: Path,
    entry_script: Optional[Path] = None,
    log_dir: Path = DEFAULT_LOG_DIR,
    environment: Optional[Dict[str, str]] = None,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    hour, minute = parse_daily_time(time_text)
    project = project_dir.expanduser().resolve()
    python = python_executable.expanduser().absolute()
    script = entry_script.expanduser().resolve() if entry_script is not None else None
    logs = log_dir.expanduser().resolve()
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "WORKTRACE_SCHEDULED": "1",
    }
    if environment:
        env.update(environment)
    if script is not None:
        program_arguments = [str(python), str(script)]
    else:
        program_arguments = [str(python), "-m", MODULE_ENTRY]
    if config_path is not None:
        program_arguments.extend(["--config", str(config_path.expanduser().resolve())])
    program_arguments.extend(["run", "--day", "today"])
    return {
        "Label": SCHEDULE_LABEL,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(project),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": env,
        "StandardOutPath": str(logs / "daily.stdout.log"),
        "StandardErrorPath": str(logs / "daily.stderr.log"),
    }


def install_schedule(
    time_text: str = DEFAULT_SCHEDULE_TIME,
    project_dir: Optional[Path] = None,
    python_executable: Optional[Path] = None,
    plist_path: Path = DEFAULT_PLIST_PATH,
    log_dir: Path = DEFAULT_LOG_DIR,
    config_path: Optional[Path] = None,
) -> ScheduleStatus:
    _require_macos()
    project = (project_dir or _project_root()).expanduser().resolve()
    python = (python_executable or Path(sys.executable)).expanduser().absolute()
    source_entry = _source_entry_script(project)
    if not project.is_dir():
        raise RuntimeError("WorkTrace working directory not found: {}".format(project))
    if not python.is_file():
        raise RuntimeError("Python executable not found: {}".format(python))
    if _module_available(python):
        entry_script = None
    elif source_entry is not None:
        entry_script = source_entry
    else:
        raise RuntimeError(
            "WorkTrace Python package is not importable by {}".format(python)
        )

    plist = build_launch_agent(
        time_text,
        project,
        python,
        entry_script=entry_script,
        log_dir=log_dir,
        config_path=config_path,
    )
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)
    for log_path in (log_dir / "daily.stdout.log", log_dir / "daily.stderr.log"):
        log_path.touch(mode=0o600, exist_ok=True)
        log_path.chmod(0o600)
    _bootout(ignore_errors=True)
    plist_path.write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
    plist_path.chmod(0o644)
    result = subprocess.run(
        ["launchctl", "bootstrap", _launch_domain(), str(plist_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "launchctl bootstrap failed")
    return get_schedule_status(
        plist_path,
        project_dir=project,
        python_executable=python,
        config_path=config_path,
    )


def remove_schedule(plist_path: Path = DEFAULT_PLIST_PATH) -> bool:
    _require_macos()
    existed = plist_path.exists()
    _bootout(ignore_errors=True)
    if plist_path.exists():
        plist_path.unlink()
    return existed


def get_schedule_status(
    plist_path: Path = DEFAULT_PLIST_PATH,
    project_dir: Optional[Path] = None,
    python_executable: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> ScheduleStatus:
    installed = plist_path.exists()
    time_text: Optional[str] = None
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    stale = False
    if installed:
        try:
            value = plistlib.loads(plist_path.read_bytes())
            interval = value.get("StartCalendarInterval") or {}
            time_text = "{:02d}:{:02d}".format(
                int(interval.get("Hour", 0)), int(interval.get("Minute", 0))
            )
            if value.get("StandardOutPath"):
                stdout_path = Path(value["StandardOutPath"])
            if value.get("StandardErrorPath"):
                stderr_path = Path(value["StandardErrorPath"])
            expected_project = (project_dir or _project_root()).expanduser().resolve()
            expected_python = (
                (python_executable or Path(sys.executable)).expanduser().absolute()
            )
            expected_entry = _source_entry_script(expected_project)
            arguments = [str(item) for item in (value.get("ProgramArguments") or [])]
            stale = (
                value.get("Label") != SCHEDULE_LABEL
                or not _same_path(value.get("WorkingDirectory"), expected_project)
                or not _valid_program_arguments(
                    arguments,
                    expected_python=expected_python,
                    expected_entry=expected_entry,
                    expected_config=config_path,
                )
            )
        except (OSError, ValueError, plistlib.InvalidFileException):
            time_text = None
            stale = True
    loaded = False
    if sys.platform == "darwin":
        result = subprocess.run(
            ["launchctl", "print", "{}/{}".format(_launch_domain(), SCHEDULE_LABEL)],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        loaded = result.returncode == 0
    return ScheduleStatus(
        installed,
        loaded,
        time_text,
        plist_path,
        stdout_path,
        stderr_path,
        stale,
    )


def _valid_program_arguments(
    arguments,
    *,
    expected_python: Path,
    expected_entry: Optional[Path],
    expected_config: Optional[Path],
) -> bool:
    if len(arguments) < 5:
        return False
    python = Path(arguments[0]).expanduser()
    if not python.is_file() or not _same_path(python, expected_python):
        return False

    tail = arguments[1:]
    if tail[:2] == ["-m", MODULE_ENTRY]:
        tail = tail[2:]
    elif expected_entry is not None and tail:
        entry = Path(tail[0]).expanduser()
        if not entry.is_file() or not _same_path(entry, expected_entry):
            return False
        tail = tail[1:]
    else:
        return False

    configured_path: Optional[Path] = None
    if tail[:1] == ["--config"]:
        if len(tail) != 5:
            return False
        configured_path = Path(tail[1]).expanduser()
        if not configured_path.is_file():
            return False
        tail = tail[2:]
    if tail != ["run", "--day", "today"]:
        return False
    if expected_config is not None:
        expected = expected_config.expanduser().resolve()
        return configured_path is not None and _same_path(configured_path, expected)
    return True


def _same_path(value, expected: Path) -> bool:
    if value in (None, ""):
        return False
    return os.path.normcase(
        os.path.abspath(os.path.expanduser(str(value)))
    ) == os.path.normcase(os.path.abspath(os.path.expanduser(str(expected))))


def _bootout(ignore_errors: bool) -> None:
    result = subprocess.run(
        ["launchctl", "bootout", "{}/{}".format(_launch_domain(), SCHEDULE_LABEL)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return
    detail = "{}\n{}".format(result.stdout or "", result.stderr or "").strip()
    lowered = detail.lower()
    not_found = any(
        marker in lowered
        for marker in ("could not find service", "no such process", "service not found")
    )
    if ignore_errors and not_found:
        return
    raise RuntimeError(detail or "launchctl bootout failed")


def _launch_domain() -> str:
    return "gui/{}".format(os.getuid())


def _project_root() -> Path:
    source_root = Path(__file__).resolve().parents[2]
    if _source_entry_script(source_root) is not None:
        return source_root
    return Path.home().resolve()


def _source_entry_script(project: Path) -> Optional[Path]:
    entry = project.expanduser().resolve() / "scripts" / "worktrace.py"
    return entry if entry.is_file() else None


def _module_available(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import {}".format(MODULE_ENTRY)],
            cwd=str(Path.home()),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("daily scheduling currently supports macOS launchd only")
