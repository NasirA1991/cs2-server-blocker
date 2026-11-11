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
            "usr/share/applications/org.cs2blocker.Gui.desktop",
            "usr/share/icons/hicolor/scalable/apps/cs2blocker.svg",
            "usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml",
            "usr/share/bash-completion/completions/cs2blockctl",
            "usr/share/zsh/site-functions/_cs2blockctl",
            "usr/share/fish/vendor_completions.d/cs2blockctl.fish",
            "usr/share/licenses/cs2blocker/LICENSE",
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

    def test_installed_code_is_not_user_writable(self):
        code_root = self.root / "usr/lib/cs2blocker"
        for path in code_root.rglob("*"):
            st = os.lstat(path)
            self.assertFalse(st.st_mode & 0o022, f"group/world-writable: {path}")
            if os.geteuid() == 0:
                self.assertEqual(st.st_uid, 0, f"not root-owned: {path}")
        self.assertIn(
            "--no-preserve=ownership",
            (ROOT / "packaging/install.sh").read_text(),
        )

    def test_preremove_clears_rules(self):
        script = ROOT / "packaging/scripts/preremove.sh"
        self.assertTrue(script.is_file())
        text = script.read_text()
        self.assertIn("cs2blocker-helper", text)
        self.assertIn("clear", text)
        self.assertIn("nft delete table inet cs2blocker", text)
        aur = (ROOT / "packaging/aur/cs2blocker.install").read_text()
        self.assertIn("pre_remove()", aur)
        self.assertIn("nft delete table inet cs2blocker", aur)

    def test_policy_xml(self):
        policy = ET.parse(self.root / "usr/share/polkit-1/actions/org.cs2blocker.policy").getroot()
        self.assertEqual(policy.find("action").get("id"), "org.cs2blocker.helper")
        defaults = policy.find("action/defaults")
        self.assertEqual(defaults.findtext("allow_inactive"), "no")

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
        detail = result.stderr + result.stdout
        if result.returncode != 0 and "sysinit.target not found" in detail:
            self.skipTest("systemd-analyze lacks distro unit files in this environment")
        self.assertEqual(result.returncode, 0, detail)

    def test_version_is_single_sourced(self):
        from cs2blocker import __version__

        self.assertIn(f"pkgver={__version__}", (ROOT / "packaging/PKGBUILD").read_text())
        self.assertIn(f"pkgver={__version__}", (ROOT / "packaging/aur/PKGBUILD").read_text())
        self.assertIn(f"version: {__version__}", (ROOT / "packaging/nfpm.yaml").read_text())

    @unittest.skipUnless(shutil.which("appstreamcli"), "appstreamcli absent")
    def test_metainfo_validates(self):
        result = subprocess.run(
            ["appstreamcli", "validate", "--no-net", str(self.root / "usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate absent")
    def test_desktop_file(self):
        result = subprocess.run(
            ["desktop-file-validate", str(self.root / "usr/share/applications/org.cs2blocker.Gui.desktop")],
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
        self.assertFalse((self.root / "usr/share/metainfo/org.cs2blocker.Gui.metainfo.xml").exists())
        self.assertFalse((self.root / "usr/share/bash-completion/completions/cs2blockctl").exists())


if __name__ == "__main__":
    unittest.main()
