from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def reference_path(name: str) -> Path:
    """Resolve a contract in both source-skill and installed-wheel layouts."""

    source_path = Path(__file__).resolve().parents[2] / "references" / name
    if source_path.is_file():
        return source_path
    packaged = Path(str(files("worktrace_agent_resources").joinpath(name)))
    if not packaged.is_file():
        raise FileNotFoundError(
            "packaged WorkTrace reference not found: {}".format(name)
        )
    return packaged
