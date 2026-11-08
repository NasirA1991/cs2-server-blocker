import unittest

from cs2blocker.core.sdr import load_bundled
from cs2blocker.gui.locations import normalize_geo, parse_location


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

    def test_partner_coordinates_use_longitude_latitude_order(self):
        # Valve's partner POPs sometimes publish (latitude, longitude).
        # Country references let the map pick the correct orientation.
        self.assertEqual(normalize_geo((51.51, 0.12), (-0.13, 51.51)), (0.12, 51.51))
        self.assertEqual(normalize_geo((50.11, 8.68), (8.68, 50.12)), (8.68, 50.11))
        self.assertEqual(normalize_geo((48.21, 16.36), (16.22, 48.12)), (16.36, 48.21))
        # Already-correct partner coordinates must not be flipped.
        self.assertEqual(
            normalize_geo((56.3414, 25.1164), (55.3, 25.25)),
            (56.3414, 25.1164),
        )


if __name__ == "__main__":
    unittest.main()
