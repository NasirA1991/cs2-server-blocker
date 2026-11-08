import json
import unittest

from cs2blocker.core.model import Protocol
from cs2blocker.core.sdr import (
    SdrError,
    diff_snapshots,
    parse_valve,
    snapshot_from_dict,
    snapshot_to_dict,
)
from cs2blocker.core.util import ValidationError
from tests.helpers import pop_doc, raw_doc, snapshot


class SdrParsingTests(unittest.TestCase):
    def test_minimal_document(self):
        snap = parse_valve(
            raw_doc(
                {
                    "aaa": pop_doc(
                        "Alpha (Testland)",
                        ipv4=["8.8.8.8"],
                        ipv6=["2606:4700:4700::1111"],
                        geo=[11.0, 22.0],
                        aliases=["a1"],
                    )
                }
            )
        )
        self.assertEqual(snap.appid, 730)
        self.assertEqual(snap.revision, 1)
        self.assertEqual(snap.source, "network")
        pop = snap.pops["aaa"]
        self.assertEqual(pop.name, "Alpha (Testland)")
        self.assertEqual(pop.relay_count, 2)
        self.assertEqual(pop.region.value, "Unknown")
        self.assertEqual(pop.geo, (11.0, 22.0))
        self.assertEqual(pop.aliases, ("a1",))
        self.assertEqual(pop.ipv4_relays[0].address, "8.8.8.8")
        self.assertIn(":", pop.ipv6_relays[0].address)

    def test_failure_success_false(self):
        with self.assertRaises(SdrError):
            parse_valve({"success": False, "message": "bad app", "pops": {}})

    def test_private_address_rejected(self):
        with self.assertRaises(SdrError):
            parse_valve(raw_doc({"aaa": pop_doc(ipv4=["10.0.0.5"])}))

    def test_loopback_rejected(self):
        with self.assertRaises(SdrError):
            parse_valve(raw_doc({"aaa": pop_doc(ipv4=["127.0.0.1"])}))

    def test_ula_ipv6_rejected(self):
        with self.assertRaises(SdrError):
            parse_valve(raw_doc({"aaa": pop_doc(ipv6=["fd00::1"])}))

    def test_bad_port_range(self):
        with self.assertRaises(SdrError):
            parse_valve(
                raw_doc({"aaa": {"desc": "x", "relays": [{"ipv4": "8.8.8.8", "port_range": [30000, 20000]}]}})
            )

    def test_duplicate_addresses_are_deduped(self):
        pop = pop_doc(ipv4=["8.8.8.8", "8.8.8.8"], port_range=(27015, 27060))
        snap = parse_valve(raw_doc({"aaa": pop}))
        self.assertEqual(snap.pops["aaa"].relay_count, 1)

    def test_empty_relays_key_is_allowed(self):
        snap = parse_valve(raw_doc({"aaa": {"desc": "No relays"}}))
        self.assertFalse(snap.pops["aaa"].has_relays)

    def test_too_many_relays(self):
        addresses = [f"1.1.{i // 250}.{i % 250 + 1}" for i in range(65)]
        with self.assertRaises(SdrError):
            parse_valve(raw_doc({"aaa": pop_doc(ipv4=addresses)}))

    def test_normalized_roundtrip(self):
        snap = snapshot({"aaa": pop_doc("Alpha", ipv4=["8.8.8.8"], ipv6=["2606:4700:4700::1111"])})
        document = snapshot_to_dict(snap)
        self.assertEqual(document["schema"], 1)
        again = snapshot_from_dict(document)
        self.assertEqual(again.pop_codes(), snap.pop_codes())
        self.assertEqual(
            [r.address for r in again.pops["aaa"].relays],
            [r.address for r in snap.pops["aaa"].relays],
        )

    def test_normalized_bad_appid(self):
        document = snapshot_to_dict(snapshot({"aaa": pop_doc(ipv4=["8.8.8.8"])}))
        document["appid"] = 570
        with self.assertRaises(SdrError):
            snapshot_from_dict(document, appid=730)

    def test_diff(self):
        old = snapshot({"aaa": pop_doc(ipv4=["8.8.8.8"])}, revision=1)
        new = snapshot(
            {"aaa": pop_doc(ipv4=["8.8.8.8", "9.9.9.9"]), "bbb": pop_doc(ipv4=["1.1.1.1"])},
            revision=2,
        )
        diff = diff_snapshots(old, new)
        self.assertTrue(diff.changed)
        self.assertTrue(diff.revision_changed)
        self.assertIn("9.9.9.9", diff.added_addresses)
        self.assertIn("bbb", diff.added_pops)

    def test_sanitizes_control_characters(self):
        snap = parse_valve(raw_doc({"aaa": {"desc": 'Evil\n"POP"', "relays": []}}))
        self.assertNotIn("\n", snap.pops["aaa"].name)
        self.assertIn("Evil", snap.pops["aaa"].name)


if __name__ == "__main__":
    unittest.main()
