import json
import subprocess
import sys
import unittest
from pathlib import Path

from cs2blocker.cli import _requested_region, build_parser
from cs2blocker.core.model import Mode, Region, Selection
from cs2blocker.core.selection import allow_codes, block_codes


class CliParsingTests(unittest.TestCase):
    def test_region_lookup(self):
        self.assertEqual(_requested_region("europe"), Region.EUROPE)
        self.assertEqual(_requested_region("EUROPE"), Region.EUROPE)
        with self.assertRaises(Exception):
            _requested_region("nope")

    def test_parser_accepts_commands(self):
        parser = build_parser()
        args = parser.parse_args(["block", "ams"])
        self.assertEqual(args.command, "block")
        args = parser.parse_args(["--sudo", "clear"])
        self.assertTrue(args.sudo)
        self.assertEqual(parser.parse_args(["pause"]).command, "pause")
        self.assertEqual(parser.parse_args(["resume"]).command, "resume")


class SelectionSemanticsTests(unittest.TestCase):
    def test_block_in_block_mode_adds_to_block_list(self):
        selection = Selection(mode=Mode.BLOCK, pops=frozenset({"sto"}))
        self.assertEqual(
            block_codes(selection, frozenset({"ams"})),
            Selection(mode=Mode.BLOCK, pops=frozenset({"sto", "ams"})),
        )

    def test_unblock_in_block_mode_removes_from_block_list(self):
        selection = Selection(mode=Mode.BLOCK, pops=frozenset({"sto", "ams"}))
        self.assertEqual(
            allow_codes(selection, frozenset({"ams"})),
            Selection(mode=Mode.BLOCK, pops=frozenset({"sto"})),
        )

    def test_block_in_allow_only_mode_removes_from_allow_list(self):
        selection = Selection(mode=Mode.ONLY, pops=frozenset({"sto", "ams"}))
        self.assertEqual(
            block_codes(selection, frozenset({"ams"})),
            Selection(mode=Mode.ONLY, pops=frozenset({"sto"})),
        )

    def test_unblock_in_allow_only_mode_adds_to_allow_list(self):
        selection = Selection(mode=Mode.ONLY, pops=frozenset({"sto"}))
        self.assertEqual(
            allow_codes(selection, frozenset({"ams"})),
            Selection(mode=Mode.ONLY, pops=frozenset({"sto", "ams"})),
        )


class CliSubprocessTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "cs2blocker.cli", *args],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_list_json(self):
        result = self.run_cli("list", "--region", "Europe", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["appid"], 730)
        self.assertEqual(len(payload["pops"]), 15)

    def test_status_json_without_install(self):
        result = self.run_cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("persistence", payload)

    def test_counters_without_monitor(self):
        result = self.run_cli("counters", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)

    def test_doctor_json(self):
        result = self.run_cli("doctor", "--json")
        self.assertIn(result.returncode, (0, 1))
        payload = json.loads(result.stdout)
        self.assertIn("checks", payload)


if __name__ == "__main__":
    unittest.main()
