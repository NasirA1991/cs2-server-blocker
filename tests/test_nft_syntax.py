import os
import shutil
import subprocess
import unittest

from cs2blocker.core.model import Mode, Selection
from cs2blocker.core.ruleset import build_plan, render_nft
from cs2blocker.core.sdr import load_bundled


@unittest.skipUnless(os.geteuid() == 0, "nft --check needs CAP_NET_ADMIN")
@unittest.skipUnless(shutil.which("nft"), "nft is not installed")
class NftSyntaxTests(unittest.TestCase):
    def test_generated_rulesets_pass_nft_check(self):
        snapshot = load_bundled()
        cases = [
            Selection(mode=Mode.BLOCK, pops=frozenset({"ams", "fra"})),
            Selection(mode=Mode.ONLY, pops=frozenset({"ams"})),
            Selection(mode=Mode.BLOCK, pops=frozenset({"ams"}), block_forward=True),
        ]
        for selection in cases:
            plan = build_plan(snapshot, selection)
            text = render_nft(plan, revision=plan.revision)
            result = subprocess.run(
                ["nft", "-c", "-f", "-"],
                input=text,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + "\n" + text[:500])


if __name__ == "__main__":
    unittest.main()
