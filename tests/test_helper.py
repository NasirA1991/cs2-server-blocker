import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from cs2blocker import helper
from cs2blocker.core.backends.base import ApplyResult
from cs2blocker.core.config import config_to_dict, load_config
from cs2blocker.core.model import Mode
from cs2blocker.core.sdr import load_bundled
from cs2blocker.core.state import Paths, read_json
from cs2blocker.core.util import ValidationError


def namespace(**kwargs):
    base = {
        "preset": None,
        "pops": None,
        "mode": None,
        "protocol": None,
        "forward": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class SelectionArgsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_bundled()
        cls.config = helper.default_config()

    def test_explicit_pops(self):
        selection, warnings = helper._selection_from_args(
            self.config, self.snapshot, namespace(pops="ams,fra")
        )
        self.assertEqual(selection.pops, frozenset({"ams", "fra"}))
        self.assertEqual(selection.mode, Mode.BLOCK)
        self.assertEqual(warnings, ())

    def test_raw_ip_is_rejected(self):
        with self.assertRaises(ValidationError):
            helper._selection_from_args(self.config, self.snapshot, namespace(pops="1.2.3.4"))

    def test_unknown_pop_is_rejected(self):
        with self.assertRaises(helper.HelperError):
            helper._selection_from_args(self.config, self.snapshot, namespace(pops="zzzz"))

    def test_preset_resolves(self):
        selection, warnings = helper._selection_from_args(
            self.config, self.snapshot, namespace(preset="eu_only")
        )
        self.assertEqual(selection.mode, Mode.ONLY)
        self.assertEqual(len(selection.pops), 12)
        self.assertEqual(warnings, ())

    def test_mode_conflict_with_preset(self):
        with self.assertRaises(helper.HelperError):
            helper._selection_from_args(
                self.config, self.snapshot, namespace(preset="eu_only", mode="block")
            )

    def test_preset_save_namespace_has_no_forward_attribute(self):
        selection, _ = helper._selection_from_args(
            self.config,
            self.snapshot,
            SimpleNamespace(preset=None, pops="ams", mode="only", protocol=None),
        )
        self.assertEqual(selection.mode, Mode.ONLY)

    def test_preset_and_pops_conflict(self):
        with self.assertRaises(helper.HelperError):
            helper._selection_from_args(
                self.config, self.snapshot, namespace(preset="eu_only", pops="ams")
            )


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.plan = None
        self.cleared = False

    def apply(self, plan):
        self.plan = plan
        return ApplyResult(
            backend="fake",
            active=True,
            blocked=plan.blocked_codes,
            relay_count=plan.relay_count,
            message="fake applied",
        )

    def clear(self):
        self.cleared = True
        return ApplyResult("fake", False, (), 0, "fake cleared")


@unittest.skipUnless(os.geteuid() == 0, "root-owned test paths need uid 0")
class ApplyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths.for_root(self.tmp.name)
        self.paths.ensure_dirs()
        self.start_patch = mock.patch.object(helper, "start_monitor")
        self.stop_patch = mock.patch.object(helper, "stop_monitor")
        self.start_patch.start()
        self.stop_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.addCleanup(self.stop_patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_writes_config_and_state(self):
        backend = FakeBackend()
        rc = helper.apply_command(self.paths, namespace(pops="ams"), backend=backend)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(backend.plan)
        config = load_config(self.paths, require_root=True)
        self.assertEqual(config.selection.pops, frozenset({"ams"}))
        state = read_json(self.paths.state_file, require_root=True)
        self.assertTrue(state["active"])
        self.assertEqual(state["blocked"], ["ams"])
        self.assertEqual(state["cache"]["origin"], "bundled")

    def test_clear_empties_selection(self):
        backend = FakeBackend()
        helper.apply_command(self.paths, namespace(pops="ams"), backend=backend)
        rc = helper.clear_command(self.paths, None)
        self.assertEqual(rc, 0)
        config = load_config(self.paths, require_root=True)
        self.assertEqual(config.selection.pops, frozenset())
        state = read_json(self.paths.state_file, require_root=True)
        self.assertFalse(state["active"])

    def test_failed_apply_restores_old_config(self):
        backend = FakeBackend()
        helper.apply_command(self.paths, namespace(pops="ams"), backend=backend)
        before = config_to_dict(load_config(self.paths, require_root=True))

        class FailingBackend(FakeBackend):
            def apply(self, plan):
                raise RuntimeError("boom")

        with self.assertRaises(helper.HelperError):
            helper.apply_command(self.paths, namespace(pops="fra"), backend=FailingBackend())
        after = config_to_dict(load_config(self.paths, require_root=True))
        self.assertEqual(before, after)
        state = read_json(self.paths.state_file, require_root=True)
        self.assertFalse(state["ok"])
        self.assertIn("boom", state["error"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.geteuid() == 0, "root-owned test paths need uid 0")
class RefreshCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths.for_root(self.tmp.name)
        self.paths.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_failure_keeps_old_data_and_records_error(self):
        from cs2blocker.core.sdr import SdrError

        args = SimpleNamespace(timer=True, no_apply=True)
        with mock.patch.object(helper, "fetch_raw", side_effect=SdrError("offline")):
            rc = helper.refresh_command(self.paths, args)
        self.assertEqual(rc, 0)
        state = read_json(self.paths.state_file, require_root=True)
        self.assertIn("offline", state["refresh_error"])
        self.assertFalse(self.paths.cache_file(730).exists())

    def test_successful_refresh_writes_cache(self):
        from pathlib import Path

        raw = Path("research/sdr-730-20261108.json").read_bytes()
        args = SimpleNamespace(timer=False, no_apply=True)
        with mock.patch.object(helper, "fetch_raw", return_value=raw):
            rc = helper.refresh_command(self.paths, args)
        self.assertEqual(rc, 0)
        self.assertTrue(self.paths.cache_file(730).exists())
        state = read_json(self.paths.state_file, require_root=True)
        self.assertIsNone(state["refresh_error"])
        self.assertIn("revision", state["cache"])
