import os
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "stage"
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "packaging/install.sh"),
                "--root",
                str(self.root),
                "--no-daemon-reload",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expected_files(self):
        expected = [
            "usr/bin/cs2blockctl",
            "usr/bin/cs2blocker-gui",
            "usr/lib/cs2blocker/cs2blocker-helper",
            "usr/lib/cs2blocker/cs2blocker/core/ruleset.py",
            "usr/lib/cs2blocker/cs2blocker/data/sdr-730.json",
            "usr/lib/systemd/system/cs2blocker.service",
            "usr/lib/systemd/system/cs2blocker-refresh.timer",
            "usr/share/polkit-1/actions/org.cs2blocker.policy",
            "usr/share/applications/cs2blocker.desktop",
            "usr/share/icons/hicolor/scalable/apps/cs2blocker.svg",
        ]
        for relative in expected:
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_installed_cli_lists(self):
        result = subprocess.run(
            [str(self.root / "usr/bin/cs2blockctl"), "list", "--region", "Europe", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"appid": 730', result.stdout)

    def test_policy_xml(self):
        policy = ET.parse(self.root / "usr/share/polkit-1/actions/org.cs2blocker.policy").getroot()
        self.assertEqual(policy.find("action").get("id"), "org.cs2blocker.helper")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze absent")
    def test_units_verify(self):
        result = subprocess.run(
            [
                "systemd-analyze",
                "verify",
                f"--root={self.root}",
                "cs2blocker.service",
                "cs2blocker-refresh.service",
                "cs2blocker-refresh.timer",
                "cs2blocker-monitor.service",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate absent")
    def test_desktop_file(self):
        result = subprocess.run(
            ["desktop-file-validate", str(self.root / "usr/share/applications/cs2blocker.desktop")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_files(self):
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "packaging/uninstall.sh"),
                "--root",
                str(self.root),
                "--yes",
                "--no-daemon-reload",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "usr/bin/cs2blockctl").exists())
        self.assertFalse((self.root / "usr/lib/cs2blocker").exists())


if __name__ == "__main__":
    unittest.main()
