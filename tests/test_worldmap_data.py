import unittest

from cs2blocker.gui.map_data import WORLD_LAND_RINGS


class WorldMapDataTests(unittest.TestCase):
    def test_land_geometry_is_present_and_sane(self):
        self.assertGreater(len(WORLD_LAND_RINGS), 100)
        total = 0
        for ring in WORLD_LAND_RINGS:
            self.assertGreaterEqual(len(ring), 3)
            for lon, lat in ring:
                self.assertGreaterEqual(lon, -180.0)
                self.assertLessEqual(lon, 180.0)
                self.assertGreaterEqual(lat, -90.0)
                self.assertLessEqual(lat, 90.0)
            total += len(ring)
        self.assertGreater(total, 1000)


if __name__ == "__main__":
    unittest.main()
