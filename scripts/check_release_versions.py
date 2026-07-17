#!/usr/bin/env python3
"""Fail when release-facing WorkTrace version declarations drift apart."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Dict


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
EXPECTED_LICENSE_EXPRESSION = "MIT"
EXPECTED_LICENSE_FILE = "LICENSE"


def _assignment(path: Path, name: str, section: str = "") -> str:
    active_section = ""
    pattern = re.compile(
        r'^{}\s*=\s*["\']([^"\']+)["\']\s*$'.format(re.escape(name))
    )
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            active_section = line[1:-1]
            continue
        if section and active_section != section:
            continue
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise ValueError("{} does not declare {}".format(path, name))


def declared_versions(root: Path = ROOT) -> Dict[str, str]:
    marketplace = json.loads((root / "marketplace.json").read_text(encoding="utf-8"))
    codex_plugin = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    zcode_plugin = json.loads(
        (root / ".zcode-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    versions = {
        "pyproject.toml": _assignment(root / "pyproject.toml", "version", "project"),
        "worktrace_agent.__version__": _assignment(
            root / "scripts" / "worktrace_agent" / "__init__.py", "__version__"
        ),
        ".codex-plugin/plugin.json": str(codex_plugin.get("version") or ""),
        ".zcode-plugin/plugin.json": str(zcode_plugin.get("version") or ""),
    }
    for index, plugin in enumerate(marketplace.get("plugins") or []):
        versions["marketplace.json:plugins[{}]".format(index)] = str(
            plugin.get("version") or ""
        )
    return versions


def consistent_declared_version(root: Path = ROOT) -> str:
    versions = declared_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1 or not SEMVER.match(next(iter(distinct), "")):
        details = ", ".join(
            "{}={}".format(location, version or "<missing>")
            for location, version in versions.items()
        )
        raise ValueError("release versions are inconsistent: {}".format(details))
    return next(iter(distinct))


def _metadata_version(raw: bytes, artifact: Path) -> str:
    if len(raw) > 1_000_000:
        raise ValueError("distribution metadata is unexpectedly large: {}".format(artifact))
    text = raw.decode("utf-8")
    fields = {}
    for line in text.splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            if key in {"Name", "Version", "License-Expression", "License-File"}:
                fields.setdefault(key, []).append(value.strip())
    name = next(iter(fields.get("Name", [])), "")
    if name.lower().replace("_", "-") != "worktrace-agent":
        raise ValueError("distribution metadata has the wrong project name: {}".format(artifact))
    version = next(iter(fields.get("Version", [])), "")
    if not SEMVER.match(version):
        raise ValueError("distribution metadata has an invalid version: {}".format(artifact))
    if fields.get("License-Expression") != [EXPECTED_LICENSE_EXPRESSION]:
        raise ValueError(
            "distribution metadata has the wrong license expression: {}".format(
                artifact
            )
        )
    if EXPECTED_LICENSE_FILE not in fields.get("License-File", []):
        raise ValueError(
            "distribution metadata does not declare the license file: {}".format(
                artifact
            )
        )
    return version


def distribution_artifact_versions(dist_dir: Path) -> Dict[str, str]:
    """Read versions from one wheel and one sdist produced by a clean build."""

    directory = dist_dir.expanduser().resolve()
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            "expected exactly one wheel and one sdist in {}; found {} wheel(s) and {} sdist(s)".format(
                directory, len(wheels), len(sdists)
            )
        )

    result: Dict[str, str] = {}
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA file: {}".format(wheel))
        result[wheel.name] = _metadata_version(archive.read(metadata_names[0]), wheel)
        license_names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/licenses/{}".format(EXPECTED_LICENSE_FILE))
        ]
        if len(license_names) != 1:
            raise ValueError("wheel must contain the MIT license file: {}".format(wheel))

    sdist = sdists[0]
    with tarfile.open(sdist, mode="r:gz") as archive:
        metadata_members = [
            member
            for member in archive.getmembers()
            if member.isfile()
            and member.name.endswith("/PKG-INFO")
            and member.name.count("/") == 1
        ]
        if len(metadata_members) != 1:
            raise ValueError("sdist must contain exactly one PKG-INFO file: {}".format(sdist))
        extracted = archive.extractfile(metadata_members[0])
        if extracted is None:
            raise ValueError("could not read sdist metadata: {}".format(sdist))
        result[sdist.name] = _metadata_version(extracted.read(1_000_001), sdist)
        license_members = [
            member
            for member in archive.getmembers()
            if member.isfile()
            and member.name.count("/") == 1
            and member.name.endswith("/{}".format(EXPECTED_LICENSE_FILE))
        ]
        if len(license_members) != 1:
            raise ValueError("sdist must contain the MIT license file: {}".format(sdist))
    return result


def validate_distribution_artifacts(dist_dir: Path, expected_version: str) -> Dict[str, str]:
    versions = distribution_artifact_versions(dist_dir)
    expected_names = {
        "worktrace_agent-{}.tar.gz".format(expected_version),
    }
    names = set(versions)
    if not expected_names.issubset(names) or not any(
        name.startswith("worktrace_agent-{}-".format(expected_version))
        and name.endswith(".whl")
        for name in names
    ):
        raise ValueError(
            "distribution filenames do not match release version {}: {}".format(
                expected_version, ", ".join(sorted(names))
            )
        )
    if set(versions.values()) != {expected_version}:
        raise ValueError(
            "distribution metadata versions do not match {}: {}".format(
                expected_version,
                ", ".join(
                    "{}={}".format(name, version)
                    for name, version in sorted(versions.items())
                ),
            )
        )
    return versions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check WorkTrace release declarations and built artifacts."
    )
    parser.add_argument(
        "--dist",
        type=Path,
        help="Also require and validate exactly one wheel and one sdist in this directory.",
    )
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print only the consistent release version for scripts.",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        versions = declared_versions()
        version = consistent_declared_version()
        artifacts = (
            validate_distribution_artifacts(args.dist, version) if args.dist else {}
        )
    except (
        OSError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print("version check failed: {}".format(exc), file=sys.stderr)
        return 2
    if args.print_version:
        print(version)
    else:
        message = "Release version {} is consistent across {} declarations".format(
            version, len(versions)
        )
        if artifacts:
            message += " and {} built artifacts".format(len(artifacts))
        print(message + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
