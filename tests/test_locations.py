import unittest

from cs2blocker.core.sdr import load_bundled
from cs2blocker.gui.locations import parse_location


class LocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_bundled()

    def test_stockholm_neighbourhood(self):
        loc = parse_location(self.snapshot.pops["sto"])
        self.assertEqual(loc.title, "Kista, Sweden")
        self.assertIn("Stockholm", loc.subtitle)
        self.assertEqual(loc.country, "Sweden")

    def test_stockholm_bromma(self):
        loc = parse_location(self.snapshot.pops["sto2"])
        self.assertEqual(loc.title, "Bromma, Sweden")
        self.assertIn("Stockholm", loc.subtitle)

    def test_vendor_prefix_is_not_the_city(self):
        loc = parse_location(self.snapshot.pops["dfra"])
        self.assertEqual(loc.title, "Frankfurt, Germany")
        self.assertIn("Datapacket", loc.subtitle)

    def test_us_state_becomes_country(self):
        loc = parse_location(self.snapshot.pops["sea"])
        self.assertEqual(loc.title, "Seattle, United States")
        self.assertIn("Washington", loc.subtitle)

    def test_partner_network_subtitle(self):
        loc = parse_location(self.snapshot.pops["pekm"])
        self.assertEqual(loc.title, "Beijing, China")
        self.assertIn("Mobile", loc.subtitle)


if __name__ == "__main__":
    unittest.main()
