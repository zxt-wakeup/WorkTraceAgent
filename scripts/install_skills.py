#!/usr/bin/env python3
"""Safely link WorkTrace skills into supported user-level skill directories."""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPOSITORY_ROOT / "skills"
TARGET_LABELS = {
    "universal": "Codex / Gemini CLI / OpenCode",
    "claude": "Claude Code",
}


@dataclass(frozen=True)
class PlannedAction:
    operation: str
    source: Path
    destination: Path
    detail: str = ""


class InstallConflict(RuntimeError):
    """Raised before writes when an existing destination is not ours."""


def discover_skills(skills_root: Path = SKILLS_ROOT) -> Dict[str, Path]:
    if not skills_root.is_dir():
        raise ValueError("skills directory not found: {}".format(skills_root))
    found = {
        path.name: path.resolve()
        for path in sorted(skills_root.iterdir())
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    if not found:
        raise ValueError("no skills containing SKILL.md were found")
    return found


def target_directories(home: Path) -> Dict[str, Path]:
    home = home.expanduser().resolve()
    return {
        "universal": home / ".agents" / "skills",
        "claude": home / ".claude" / "skills",
    }


def detected_targets(home: Path) -> List[str]:
    """Choose install locations, not a report-generation model."""

    targets = target_directories(home)
    result: List[str] = []
    universal_commands = ("codex", "gemini", "opencode")
    if targets["universal"].exists() or any(
        shutil.which(command) for command in universal_commands
    ):
        result.append("universal")
    if targets["claude"].exists() or shutil.which("claude"):
        result.append("claude")
    return result or ["universal"]


def normalize_targets(requested: Sequence[str], home: Path) -> List[str]:
    if not requested:
        return detected_targets(home)
    expanded: List[str] = []
    for value in requested:
        names = list(TARGET_LABELS) if value == "all" else [value]
        for name in names:
            if name not in expanded:
                expanded.append(name)
    return expanded


def _destination_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _same_link(destination: Path, source: Path) -> bool:
    if not destination.is_symlink():
        return False
    try:
        return destination.resolve(strict=False) == source.resolve()
    except (OSError, RuntimeError):
        return False


def plan_actions(
    skills: Mapping[str, Path],
    selected_skills: Iterable[str],
    targets: Iterable[Path],
    uninstall: bool = False,
) -> List[PlannedAction]:
    actions: List[PlannedAction] = []
    for target in targets:
        for name in selected_skills:
            source = skills[name].resolve()
            destination = target / name
            if uninstall:
                if not _destination_exists(destination):
                    actions.append(
                        PlannedAction("unchanged", source, destination, "not installed")
                    )
                elif _same_link(destination, source):
                    actions.append(PlannedAction("remove", source, destination))
                else:
                    raise InstallConflict(
                        "refusing to remove unrelated path: {}".format(destination)
                    )
            elif not _destination_exists(destination):
                actions.append(PlannedAction("link", source, destination))
            elif _same_link(destination, source):
                actions.append(
                    PlannedAction("unchanged", source, destination, "already linked")
                )
            else:
                raise InstallConflict(
                    "refusing to overwrite existing path: {}".format(destination)
                )
    return actions


def apply_actions(actions: Sequence[PlannedAction], dry_run: bool = False) -> None:
    if dry_run:
        return
    for action in actions:
        if action.operation == "link":
            action.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                action.destination.symlink_to(action.source, target_is_directory=True)
            except OSError as exc:
                raise OSError(
                    "could not create skill link {} -> {}: {}".format(
                        action.destination, action.source, exc
                    )
                ) from exc
        elif action.operation == "remove":
            action.destination.unlink()


def _print_plan(actions: Sequence[PlannedAction], dry_run: bool) -> None:
    print("Planned skill changes{}:".format(" (dry run)" if dry_run else ""))
    for action in actions:
        if action.operation == "link":
            print("- link {} -> {}".format(action.destination, action.source))
        elif action.operation == "remove":
            print("- remove {}".format(action.destination))
        else:
            print("- keep {} ({})".format(action.destination, action.detail))


def _print_next_steps() -> None:
    print("Open a new Agent conversation and enter: 生成日报 or 生成周报")
    print("The first report run initializes WorkTrace automatically.")
    print("The first weekly report will ask for your OKR and previous report samples.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Link WorkTrace skills into user-level directories without sudo or overwrite."
        )
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=("universal", "claude", "all"),
        default=[],
        help=(
            "Install target; repeatable. Default detects local products. "
            "universal means ~/.agents/skills."
        ),
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        help="Install one named skill; repeatable. Default installs every skill.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without writing.")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove only links that point back to this clone.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path.home()
    try:
        skills = discover_skills()
        selected_skills = args.skill or list(skills)
        unknown = sorted(set(selected_skills) - set(skills))
        if unknown:
            raise ValueError("unknown skill(s): {}".format(", ".join(unknown)))
        target_names = normalize_targets(args.target, home)
        directories = target_directories(home)
        print("Repository: {}".format(REPOSITORY_ROOT))
        for target_name in target_names:
            print(
                "Target {}: {} ({})".format(
                    target_name,
                    directories[target_name],
                    TARGET_LABELS[target_name],
                )
            )
        actions = plan_actions(
            skills,
            selected_skills,
            [directories[name] for name in target_names],
            uninstall=args.uninstall,
        )
        _print_plan(actions, args.dry_run)
        apply_actions(actions, dry_run=args.dry_run)
    except (InstallConflict, OSError, ValueError) as exc:
        print("install error: {}".format(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print("No files were changed.")
    elif args.uninstall:
        print("WorkTrace skill links removed safely.")
    else:
        print(
            "WorkTrace skills installed. No sudo was used and no path was overwritten."
        )
        _print_next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
