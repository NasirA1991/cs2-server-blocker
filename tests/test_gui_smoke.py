import os
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"),
    "no graphical session",
)
class GuiSmokeTests(unittest.TestCase):
    def test_smoke_launch(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(root / "cs2blocker-gui"), "--smoke-test"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GUI smoke test", result.stdout)


if __name__ == "__main__":
    unittest.main()
