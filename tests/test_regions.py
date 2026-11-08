import unittest

from cs2blocker.core.regions import REGION_BY_POP, REGION_ORDER, region_for
from cs2blocker.core.sdr import load_bundled
from cs2blocker.core.model import Region


class RegionTests(unittest.TestCase):
    def test_every_bundled_pop_has_a_known_region(self):
        snapshot = load_bundled()
        self.assertEqual(len(REGION_BY_POP), 53)
        for code in snapshot.pops:
            self.assertNotEqual(region_for(code), Region.UNKNOWN, code)
            self.assertIn(region_for(code), REGION_ORDER)

    def test_unknown_code_is_not_guessed(self):
        self.assertEqual(region_for("zzzz"), Region.UNKNOWN)

    def test_counts(self):
        counts = {}
        for code in REGION_BY_POP:
            counts[region_for(code)] = counts.get(region_for(code), 0) + 1
        self.assertEqual(counts[Region.EUROPE], 15)
        self.assertEqual(counts[Region.NORTH_AMERICA], 7)
        self.assertEqual(counts[Region.ASIA], 22)
        self.assertEqual(counts[Region.SOUTH_AMERICA], 4)
        self.assertEqual(counts[Region.OCEANIA], 2)
        self.assertEqual(counts[Region.AFRICA], 1)
        self.assertEqual(counts[Region.MIDDLE_EAST], 2)


if __name__ == "__main__":
    unittest.main()
