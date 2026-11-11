import json
import unittest
from dataclasses import replace

from cs2blocker.core.backends.base import CommandResult
from cs2blocker.core.backends.iptables import CHAIN, NEW_CHAIN, IptablesBackend
from cs2blocker.core.backends.nft import NftBackend
from cs2blocker.core.model import Mode, Selection
from cs2blocker.core.ruleset import build_plan
from cs2blocker.core.util import BackendError
from tests.helpers import pop_doc, snapshot


class FakeRunner:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, argv, *, input_text=None, timeout=60.0):
        self.calls.append((list(argv), input_text))
        return self.handler(argv, input_text)


def ok(stdout="", stderr=""):
    return CommandResult(0, stdout, stderr)


def fail(stderr="failed", code=1):
    return CommandResult(code, "", stderr)


class NftBackendTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot({"aaa": pop_doc("Alpha", ipv4=["8.8.8.8"])})
        self.plan = build_plan(self.snap, Selection(mode=Mode.BLOCK, pops=frozenset({"aaa"})))

    def test_version_and_destroy_detection(self):
        runner = FakeRunner(lambda argv, text: ok("nftables v1.1.7 (Commodore Bullmoose #8)\n"))
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        self.assertEqual(backend.version_tuple(), (1, 1, 7))
        self.assertTrue(backend.supports_destroy())

    def test_old_version_does_not_support_destroy(self):
        runner = FakeRunner(lambda argv, text: ok("nftables v1.0.6 (Old Doc Yak)\n"))
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        self.assertFalse(backend.supports_destroy())

    def test_apply_uses_check_then_single_apply_with_destroy(self):
        def handler(argv, text):
            if "--version" in argv:
                return ok("nftables v1.1.7\n")
            if "-c" in argv or "-f" in argv:
                return ok()
            raise AssertionError(argv)

        runner = FakeRunner(handler)
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        result = backend.apply(self.plan)
        self.assertTrue(result.active)
        self.assertEqual(result.blocked, ("aaa",))
        inputs = [text for argv, text in runner.calls if text]
        self.assertEqual(len(inputs), 2)
        self.assertIn("destroy table inet cs2blocker", inputs[0])
        self.assertEqual(inputs[0], inputs[1])
        self.assertNotIn("flush ruleset", inputs[0])

    def test_apply_old_nft_uses_delete_when_table_exists(self):
        def handler(argv, text):
            if "--version" in argv:
                return ok("nftables v1.0.6\n")
            if argv[-4:] == ["list", "table", "inet", "cs2blocker"]:
                return ok("table inet cs2blocker { }")
            if "-c" in argv or "-f" in argv:
                return ok()
            raise AssertionError(argv)

        runner = FakeRunner(handler)
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        backend.apply(self.plan)
        inputs = [text for argv, text in runner.calls if text]
        self.assertTrue(inputs[0].startswith("# Generated"))
        self.assertIn("delete table inet cs2blocker", inputs[0])

    def test_check_failure_does_not_apply(self):
        def handler(argv, text):
            if "--version" in argv:
                return ok("nftables v1.1.7\n")
            if "-c" in argv:
                return fail("syntax error")
            if "-f" in argv:
                return ok()
            raise AssertionError(argv)

        runner = FakeRunner(handler)
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        with self.assertRaises(BackendError):
            backend.apply(self.plan)
        self.assertFalse(any("-f" in argv for argv, _ in runner.calls if "-c" not in argv))

    def test_destroy_rejected_by_kernel_retries_with_delete(self):
        state = {"checks": 0, "applies": 0}

        def handler(argv, text):
            if "--version" in argv:
                return ok("nftables v1.1.7\n")
            if "list" in argv and "table" in argv:
                return ok("table inet cs2blocker { }")
            if "-c" in argv:
                state["checks"] += 1
                if state["checks"] == 1:
                    return fail("Error: Operation not supported")
                return ok()
            if "-f" in argv:
                state["applies"] += 1
                return ok()
            raise AssertionError(argv)

        backend = NftBackend(FakeRunner(handler), nft_path="/usr/bin/nft")
        result = backend.apply(self.plan)
        self.assertTrue(result.active)
        texts = [text for argv, text in backend._runner.calls if text]
        self.assertIn("destroy table inet cs2blocker", texts[0])
        self.assertIn("delete table inet cs2blocker", texts[1])
        self.assertFalse(backend.supports_destroy())

        # The capability is cached: the next apply must not try destroy again.
        backend.apply(self.plan)
        newer = [text for argv, text in backend._runner.calls if text][-2:]
        self.assertTrue(all("destroy table" not in text for text in newer))
        self.assertTrue(any("delete table inet cs2blocker" in text for text in newer))

    def test_clear_destroy_rejected_by_kernel_retries_with_delete(self):
        calls = []

        def handler(argv, text):
            if "list" in argv and "table" in argv:
                return ok("table inet cs2blocker { }")
            if "-f" in argv:
                calls.append(text)
                if len(calls) == 1:
                    return fail("Error: Operation not supported")
                return ok()
            raise AssertionError(argv)

        backend = NftBackend(FakeRunner(handler), nft_path="/usr/bin/nft")
        # Seed the version cache so clear() picks destroy first.
        backend._version = (1, 1, 7)
        result = backend.clear()
        self.assertFalse(result.active)
        self.assertIn("destroy table inet cs2blocker", calls[0])
        self.assertIn("delete table inet cs2blocker", calls[1])
        self.assertFalse(backend.supports_destroy())

    def test_status_parses_counters(self):
        payload = {
            "nftables": [
                {"table": {"family": "inet", "name": "cs2blocker"}},
                {
                    "counter": {
                        "name": "cnt_aaa",
                        "packets": 4,
                        "bytes": 200,
                    }
                },
                {"set": {"name": "pop_aaa"}},
            ]
        }
        runner = FakeRunner(lambda argv, text: ok(json.dumps(payload)))
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        status = backend.status()
        self.assertTrue(status.active)
        self.assertEqual(status.blocked, ("aaa",))
        self.assertEqual(status.counters["aaa"].packets, 4)

    def test_status_absent_table(self):
        runner = FakeRunner(lambda argv, text: fail("No such file or directory"))
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        self.assertFalse(backend.status().active)

    def test_clear(self):
        runner = FakeRunner(
            lambda argv, text: ok("nftables v1.1.7\n") if "--version" in argv else ok()
        )
        backend = NftBackend(runner, nft_path="/usr/bin/nft")
        result = backend.clear()
        self.assertFalse(result.active)
        text = [t for _, t in runner.calls if t][-1]
        self.assertIn("destroy table inet cs2blocker", text)


class IptablesBackendTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot({"aaa": pop_doc("Alpha", ipv4=["8.8.8.8"], ipv6=["2606:4700:4700::1111"])})
        self.plan = build_plan(self.snap, Selection(mode=Mode.BLOCK, pops=frozenset({"aaa"})))

    def make_backend(self):
        def handler(argv, text):
            binary = argv[0].rsplit("/", 1)[-1]
            if binary == "ipset" and "list" in argv:
                return ok("cs2b_stale\n")
            if binary.startswith("iptables") and "-S" in argv:
                # Simulate no existing chain; -S CS2BLOCKER is used for existence.
                return fail("No chain")
            if binary.startswith("iptables") and "-C" in argv:
                # Pretend only the real chain jump exists; the new chain has none.
                return ok() if argv[-1] == CHAIN else fail("no rule")
            return ok()

        runner = FakeRunner(handler)
        backend = IptablesBackend(runner)
        backend._iptables = "/usr/bin/iptables"
        backend._ip6tables = "/usr/bin/ip6tables"
        backend._ipset = "/usr/bin/ipset"
        return backend, runner

    def test_apply_builds_sets_and_swaps_chain(self):
        backend, runner = self.make_backend()
        result = backend.apply(self.plan)
        self.assertTrue(result.active)
        flat = [" ".join(argv) for argv, _ in runner.calls]
        joined = "\n".join(flat)
        self.assertIn("ipset create cs2b_aaa hash:net family inet", joined)
        payloads = [text for _, text in runner.calls if text]
        self.assertTrue(any("add cs2b_aaa_tmp 8.8.8.8 -exist" in text for text in payloads))
        self.assertIn(f"{NEW_CHAIN}", joined)
        self.assertIn("iptables -w 5 -I OUTPUT 1 -j CS2BLOCKER_NEW", joined)
        self.assertIn("iptables -w 5 -E CS2BLOCKER_NEW CS2BLOCKER", joined)
        self.assertIn("--comment cs2blocker:aaa", joined)

    def test_large_port_set_is_split_into_multiport_rules(self):
        backend, runner = self.make_backend()
        plan = replace(
            self.plan,
            port_ranges=tuple((27015 + i * 10, 27015 + i * 10 + 1) for i in range(10)),
        )
        backend.apply(plan)
        rule_lines = [
            " ".join(argv)
            for argv, _ in runner.calls
            if "iptables" in argv[0] and "-A" in argv
        ]
        matching = [line for line in rule_lines if "cs2blocker:aaa" in line]
        self.assertGreaterEqual(len(matching), 2)
        self.assertTrue(all("--dports" in line for line in matching))

    def test_clear_removes_chain_and_sets(self):
        backend, runner = self.make_backend()
        backend.clear()
        joined = "\n".join(" ".join(argv) for argv, _ in runner.calls)
        self.assertIn("iptables -w 5 -D OUTPUT -j CS2BLOCKER", joined)
        self.assertIn("ipset destroy cs2b_stale", joined)

    def test_status_parses_rule_counters(self):
        def handler(argv, text):
            binary = argv[0].rsplit("/", 1)[-1]
            if binary == "iptables" and "-S" in argv:
                return ok("")
            if binary == "iptables" and "-L" in argv:
                return ok(
                    "Chain CS2BLOCKER (1 references)\n"
                    "    pkts      bytes target     prot opt in     out     source               destination\n"
                    "      12      720 DROP       udp  --  *      *       0.0.0.0/0            0.0.0.0/0            /* cs2blocker:aaa */ match-set cs2b_aaa dst\n"
                )
            if binary == "ip6tables" and "-S" in argv:
                return ok("")
            if binary == "ip6tables" and "-L" in argv:
                return ok("")
            return ok()

        runner = FakeRunner(handler)
        backend = IptablesBackend(runner)
        backend._iptables = "/usr/bin/iptables"
        backend._ip6tables = "/usr/bin/ip6tables"
        backend._ipset = "/usr/bin/ipset"
        status = backend.status()
        self.assertTrue(status.active)
        self.assertEqual(status.counters["aaa"].packets, 12)
        self.assertEqual(status.counters["aaa"].bytes, 720)

    def test_available_requires_ipset(self):
        backend = IptablesBackend(lambda *args, **kwargs: ok())
        backend._iptables = "/usr/bin/iptables"
        backend._ip6tables = "/usr/bin/ip6tables"
        backend._ipset = None
        self.assertFalse(backend.available())


if __name__ == "__main__":
    unittest.main()
