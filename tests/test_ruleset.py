import unittest
from pathlib import Path

from cs2blocker.core.model import Mode, Protocol, Selection
from cs2blocker.core.ruleset import (
    build_plan,
    render_clear,
    render_nft,
    summarize,
)
from cs2blocker.core.sdr import load_bundled
from cs2blocker.core.util import nft_comment
from tests.helpers import pop_doc, snapshot

EXPECTED = Path(__file__).parent / "expected" / "block_ams_fra.nft"


class RulesetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real = load_bundled()

    def test_block_ams_fra_snapshot(self):
        plan = build_plan(self.real, Selection(mode=Mode.BLOCK, pops=frozenset({"ams", "fra"})))
        text = render_nft(
            plan,
            revision=plan.revision,
            generated_at="2026-11-08T00:00:00+00:00",
        )
        self.assertEqual(text, EXPECTED.read_text(encoding="utf-8"))
        self.assertIn("destroy table inet cs2blocker", text)
        self.assertNotIn("flush ruleset", text)
        self.assertNotIn("/etc/nftables.conf", text)
        self.assertNotIn("table inet filter", text)

    def test_only_mode_blocks_non_selected(self):
        plan = build_plan(self.real, Selection(mode=Mode.ONLY, pops=frozenset({"ams", "fra"})))
        self.assertNotIn("ams", plan.blocked_codes)
        self.assertNotIn("fra", plan.blocked_codes)
        text = render_nft(plan, revision=plan.revision)
        self.assertNotIn("set pop_ams", text)
        self.assertNotIn("set pop_fra", text)
        self.assertIn("set pop_bom2", text)
        self.assertIn("select", text)

    def test_empty_block_clears(self):
        plan = build_plan(self.real, Selection(mode=Mode.BLOCK, pops=frozenset()))
        self.assertFalse(plan.has_rules)
        text = render_nft(plan)
        self.assertTrue(text.startswith("# Generated"))
        self.assertIn("destroy table inet cs2blocker", text)

    def test_empty_only_blocks_everything_and_warns(self):
        plan = build_plan(self.real, Selection(mode=Mode.ONLY, pops=frozenset()))
        self.assertTrue(any("every known POP" in w for w in plan.warnings))
        self.assertEqual(len(plan.blocked), 45)

    def test_protocol_all_removes_udp_match(self):
        plan = build_plan(
            self.real,
            Selection(mode=Mode.BLOCK, pops=frozenset({"ams"}), protocol=Protocol.ALL),
        )
        text = render_nft(plan, revision=plan.revision)
        self.assertNotIn("udp dport", text)
        self.assertNotIn("set sdr_ports", text)
        self.assertIn("ip daddr @pop_ams", text)

    def test_forward_chain(self):
        plan = build_plan(
            self.real,
            Selection(mode=Mode.BLOCK, pops=frozenset({"ams"}), block_forward=True),
        )
        text = render_nft(plan, revision=plan.revision)
        self.assertIn("chain forward", text)
        self.assertIn("(forward)", text)

    def test_ipv6_rules_and_sets(self):
        snap = snapshot(
            {"aaa": pop_doc("Alpha", ipv4=["8.8.8.8"], ipv6=["2606:4700:4700::1111"])}
        )
        plan = build_plan(snap, Selection(mode=Mode.BLOCK, pops=frozenset({"aaa"})))
        text = render_nft(plan, revision=snap.revision)
        self.assertIn("set pop_aaa {", text)
        self.assertIn("set pop_aaa_v6 {", text)
        self.assertIn("type ipv6_addr", text)
        self.assertIn("ip6 daddr @pop_aaa_v6", text)

    def test_overlap_is_excluded_in_only_mode(self):
        snap = snapshot(
            {
                "aaa": pop_doc("Alpha", ipv4=["8.8.8.8", "1.1.1.1"]),
                "bbb": pop_doc("Beta", ipv4=["8.8.8.8", "9.9.9.9"]),
            }
        )
        plan = build_plan(snap, Selection(mode=Mode.ONLY, pops=frozenset({"aaa"})))
        self.assertEqual(plan.blocked_codes, ("bbb",))
        self.assertEqual([r.address for r in plan.blocked[0].relays], ["9.9.9.9"])
        self.assertTrue(any("shared" in w for w in plan.warnings))
        text = render_nft(plan, revision=snap.revision)
        self.assertNotIn("8.8.8.8", text)
        self.assertIn("9.9.9.9", text)

    def test_unknown_selected_warns(self):
        plan = build_plan(self.real, Selection(mode=Mode.BLOCK, pops=frozenset({"zzzz"})))
        self.assertTrue(any("unknown" in w for w in plan.warnings))
        self.assertFalse(plan.has_rules)

    def test_comment_sanitization(self):
        self.assertEqual(nft_comment('a"b\\c\nd'), "a'b/c d")
        self.assertNotIn("\n", nft_comment("x\ny"))

    def test_clear_text(self):
        self.assertIn("destroy table inet cs2blocker", render_clear())
        self.assertEqual(render_clear(replace_strategy="none"), "")

    def test_summarize(self):
        plan = build_plan(self.real, Selection(mode=Mode.BLOCK, pops=frozenset({"ams"})))
        self.assertIn("blocked=1", summarize(plan))


if __name__ == "__main__":
    unittest.main()
