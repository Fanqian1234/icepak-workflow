"""Area-conservative Gamma tests; no AEDT or Fluent solver is started."""
import unittest
import numpy as np
from sampling import _gamma_faces, _raster_gamma
from settings import AMBIENT_C
from types import SimpleNamespace


class GammaProjectionTests(unittest.TestCase):
    def setUp(self):
        self.square = np.array([(0., 0.), (.04, 0.), (.04, .04), (0., .04)])
        self.bbox = (0., 0., 0., 40., 40., 0.)
        self.temperature = AMBIENT_C + 273.15 + 50.

    def test_full_contact_is_conservative(self):
        _, _, _, _, proof = _raster_gamma(
            [self.square], np.array([90.]), np.array([self.temperature]),
            np.array([.04 * .04]), self.bbox, 4)
        self.assertLess(proof["heat_relative_error"], 1e-9)
        self.assertLess(proof["area_relative_error"], 1e-9)

    def test_outside_native_face_is_identified_not_silently_normalized(self):
        outer = np.array([(.041, 0.), (.042, 0.),
                          (.042, .001), (.041, .001)])
        with self.assertRaisesRegex(ValueError,
                                    "partially_unprojected_faces=1"):
            _raster_gamma(
                [self.square, outer],
                np.array([90., 5.]),
                np.array([self.temperature, self.temperature]),
                np.array([.04 * .04, .001 * .001]), self.bbox, 4)

    def test_gamma_uses_physical_contact_parent_not_oversized_parent(self):
        # The NCI exports both a 41.75-mm base overhang parent and the
        # real 40-mm contact parent. Only the latter represents Gamma.
        oversized = np.array([(0., 0., 0.), (.04175, 0., 0.),
                              (.04175, .04175, 0.), (0., .04175, 0.)])
        contact = np.array([(0., 0., 0.), (.04, 0., 0.),
                            (.04, .04, 0.), (0., .04, 0.)])
        native = SimpleNamespace(
            nodes=np.vstack([oversized, contact]),
            cells=np.array([0, 1, 2]),
            zones={1: {"name": "tim2_solid", "type": "solid"},
                   2: {"name": "heatsink_base_solid", "type": "solid"},
                   11: {"name": "large_parent", "type": "interface"},
                   12: {"name": "physical_parent", "type": "interface"},
                   21: {"name": "tim2_children", "type": "wall"}},
            faces=[(10, 11, [0, 1, 2, 3], 1, 0),
                   (20, 12, [4, 5, 6, 7], 2, 0),
                   (30, 21, [0, 1, 2, 3], 1, 0),
                   (40, 21, [4, 5, 6, 7], 1, 0)],
            parents_of={30: [10], 40: [20]},
            geometry={})
        plan = SimpleNamespace(gamma_bbox_mm=self.bbox)
        heat = 48.271064
        scalar = {30: {"T": self.temperature, "q": -heat},
                  40: {"T": self.temperature, "q": -heat}}
        polygons, Q, T, A = _gamma_faces(native, plan, scalar)
        self.assertEqual(len(polygons), 1)
        self.assertAlmostEqual(float(A.sum()), .04 * .04, places=12)
        self.assertAlmostEqual(float(Q.sum()), heat, places=8)
        for n in (80, 40):
            _, _, _, _, proof = _raster_gamma(
                polygons, Q, T, A, self.bbox, n)
            self.assertLess(proof["heat_relative_error"], 1e-10)
            self.assertLess(proof["area_relative_error"], 1e-10)


if __name__ == "__main__":
    unittest.main()

