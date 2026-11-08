import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cs2blocker.core.config import (
    config_from_dict,
    config_to_dict,
    default_config,
    load_config,
    save_config,
)
from cs2blocker.core.model import BackendName, Mode, Protocol, Selection
from cs2blocker.core.presets import load_default_presets, resolve_preset
from cs2blocker.core.sdr import load_bundled
from cs2blocker.core.state import Paths, lock, read_json, write_json
from cs2blocker.core.util import ConfigError


class ConfigTests(unittest.TestCase):
    def test_default_config_has_builtins(self):
        config = default_config()
        self.assertIn("eu_only", config.presets)
        self.assertIn("block_asia_oceania", config.presets)
        self.assertEqual(config.selection.mode, Mode.BLOCK)

    def test_roundtrip(self):
        config = default_config()
        config.selection = Selection(
            mode=Mode.ONLY,
            pops=frozenset({"ams", "fra"}),
            protocol=Protocol.ALL,
            block_forward=True,
        )
        config.backend = BackendName.NFT
        document = config_to_dict(config)
        again = config_from_dict(document)
        self.assertEqual(again.selection.pops, config.selection.pops)
        self.assertEqual(again.selection.mode, Mode.ONLY)
        self.assertEqual(again.selection.protocol, Protocol.ALL)
        self.assertTrue(again.selection.block_forward)
        self.assertEqual(again.backend, BackendName.NFT)
        self.assertEqual(set(again.presets), set(config.presets))

    def test_bad_preset_is_skipped_not_fatal(self):
        document = config_to_dict(default_config())
        document["presets"]["bad"] = {"mode": "nonsense"}
        config = config_from_dict(document)
        self.assertNotIn("bad", config.presets)
        self.assertIn("eu_only", config.presets)

    def test_invalid_selection_mode_raises(self):
        document = config_to_dict(default_config())
        document["selection"]["mode"] = "nonsense"
        with self.assertRaises(ConfigError):
            config_from_dict(document)

    def test_unknown_selected_pop_is_preserved(self):
        document = config_to_dict(default_config())
        document["selection"]["pops"] = ["ams", "zzzz"]
        config = config_from_dict(document)
        self.assertEqual(config.selection.pops, frozenset({"ams", "zzzz"}))

    def test_eu_preset_resolves(self):
        snapshot = load_bundled()
        presets = load_default_presets()
        codes, warnings = resolve_preset(presets["eu_only"], snapshot)
        self.assertEqual(len(codes), 12)
        self.assertEqual(warnings, ())
        self.assertIn("ams", codes)
        self.assertNotIn("atl", codes)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths.for_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_roundtrip_and_mode(self):
        self.paths.ensure_dirs()
        payload = {"hello": "world", "n": 1}
        write_json(self.paths.config_file, payload)
        self.assertEqual(read_json(self.paths.config_file, require_root=False), payload)
        mode = stat.S_IMODE(os.stat(self.paths.config_file).st_mode)
        self.assertEqual(mode, 0o644)

    def test_missing_ok(self):
        self.assertIsNone(read_json(self.paths.config_file, require_root=False, missing_ok=True))

    def test_group_writable_refused(self):
        self.paths.ensure_dirs()
        write_json(self.paths.config_file, {"x": 1})
        os.chmod(self.paths.config_file, 0o666)
        with self.assertRaises(ConfigError):
            read_json(self.paths.config_file, require_root=False)

    def test_symlink_refused(self):
        self.paths.ensure_dirs()
        target = self.paths.config_dir / "target.json"
        write_json(target, {"x": 1})
        self.paths.config_file.symlink_to(target)
        with self.assertRaises(ConfigError):
            read_json(self.paths.config_file, require_root=False)

    def test_config_save_load(self):
        self.paths.ensure_dirs()
        config = default_config()
        config.selection = Selection(mode=Mode.BLOCK, pops=frozenset({"ams"}))
        save_config(self.paths, config)
        loaded = load_config(self.paths, require_root=False)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.selection.pops, frozenset({"ams"}))

    def test_lock(self):
        self.paths.ensure_dirs()
        with lock(self.paths):
            self.assertTrue(self.paths.lock_file.exists())


if __name__ == "__main__":
    unittest.main()
