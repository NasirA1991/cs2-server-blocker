import unittest
from unittest.mock import patch

from cs2blocker.core.ping import PingResult, ping_median


class PingMedianTests(unittest.TestCase):
    def test_median_of_successful_samples(self):
        samples = [
            PingResult("192.0.2.1", 30.0, "icmp"),
            PingResult("192.0.2.1", 70.0, "icmp"),
            PingResult("192.0.2.1", 50.0, "icmp"),
        ]
        with patch("cs2blocker.core.ping.ping", side_effect=samples):
            result = ping_median("192.0.2.1", samples=3)
        self.assertEqual(result.rtt_ms, 50.0)
        self.assertEqual(result.method, "icmp")

    def test_failed_samples_are_ignored(self):
        samples = [
            PingResult("192.0.2.1", None, "icmp", "timeout"),
            PingResult("192.0.2.1", 44.0, "icmp"),
            PingResult("192.0.2.1", None, "icmp", "timeout"),
        ]
        with patch("cs2blocker.core.ping.ping", side_effect=samples):
            result = ping_median("192.0.2.1", samples=3)
        self.assertEqual(result.rtt_ms, 44.0)

    def test_all_failed_is_none(self):
        samples = [PingResult("192.0.2.1", None, "ping", "no reply") for _ in range(3)]
        with patch("cs2blocker.core.ping.ping", side_effect=samples):
            result = ping_median("192.0.2.1", samples=3)
        self.assertIsNone(result.rtt_ms)
        self.assertEqual(result.method, "ping")
        self.assertEqual(result.error, "no reply")


if __name__ == "__main__":
    unittest.main()
