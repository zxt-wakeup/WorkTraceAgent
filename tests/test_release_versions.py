from __future__ import annotations

import importlib
import io
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
version_check = importlib.import_module("check_release_versions")


class ReleaseVersionTests(unittest.TestCase):
    def test_release_version_declarations_are_consistent_semver(self):
        versions = version_check.declared_versions(ROOT)

        self.assertGreaterEqual(len(versions), 5)
        self.assertEqual(len(set(versions.values())), 1)
        self.assertTrue(
            all(version_check.SEMVER.match(value) for value in versions.values())
        )

    def test_built_wheel_and_sdist_metadata_must_match_release(self):
        version = version_check.consistent_declared_version(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            wheel = dist / "worktrace_agent-{}-py3-none-any.whl".format(version)
            with zipfile.ZipFile(wheel, mode="w") as archive:
                archive.writestr(
                    "worktrace_agent-{}.dist-info/METADATA".format(version),
                    (
                        "Metadata-Version: 2.4\nName: worktrace-agent\n"
                        "Version: {}\nLicense-Expression: MIT\n"
                        "License-File: LICENSE\n"
                    ).format(version),
                )
                archive.writestr(
                    "worktrace_agent-{}.dist-info/licenses/LICENSE".format(version),
                    "MIT License\n",
                )

            sdist = dist / "worktrace_agent-{}.tar.gz".format(version)
            metadata = (
                (
                    "Metadata-Version: 2.4\nName: worktrace-agent\n"
                    "Version: {}\nLicense-Expression: MIT\n"
                    "License-File: LICENSE\n"
                ).format(version).encode("utf-8")
            )
            with tarfile.open(sdist, mode="w:gz") as archive:
                info = tarfile.TarInfo(
                    "worktrace_agent-{}/PKG-INFO".format(version)
                )
                info.size = len(metadata)
                archive.addfile(info, io.BytesIO(metadata))
                license_text = b"MIT License\n"
                license_info = tarfile.TarInfo(
                    "worktrace_agent-{}/LICENSE".format(version)
                )
                license_info.size = len(license_text)
                archive.addfile(license_info, io.BytesIO(license_text))

            checked = version_check.validate_distribution_artifacts(dist, version)

        self.assertEqual(set(checked.values()), {version})

    def test_distribution_metadata_requires_mit_license(self):
        metadata = (
            b"Metadata-Version: 2.4\nName: worktrace-agent\nVersion: 1.4.0\n"
            b"License-Expression: Apache-2.0\nLicense-File: LICENSE\n"
        )
        with self.assertRaisesRegex(ValueError, "license expression"):
            version_check._metadata_version(metadata, Path("example.whl"))

    def test_old_or_unknown_distribution_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            (dist / "UNKNOWN-0.0.0-py3-none-any.whl").write_bytes(b"not a wheel")

            with self.assertRaises(ValueError):
                version_check.validate_distribution_artifacts(dist, "1.4.0")


if __name__ == "__main__":
    unittest.main()
