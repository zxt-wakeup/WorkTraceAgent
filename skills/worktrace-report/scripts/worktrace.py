#!/usr/bin/env python3
"""Run the shared WorkTrace runtime from a linked or plugin skill."""

from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPOSITORY_ROOT / "scripts"
if not (RUNTIME_ROOT / "worktrace_agent" / "cli.py").is_file():
    raise SystemExit(
        "WorkTrace runtime not found. Reinstall this skill from the complete "
        "WorkTraceAgent clone instead of copying one skill directory."
    )

sys.path.insert(0, str(RUNTIME_ROOT))

from worktrace_agent.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
