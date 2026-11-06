"""Offline tests for the server's area-conservative Source-temperature export."""
from pathlib import Path
import tempfile
import unittest
import numpy as np
from temperature40_backfill import conservative_temperature_40, convert_temperature_file


class Temperature40Tests(unittest.TestCase):
    def test_constant_temperature(self):
        edges = np.linspace(2., 18., 129)
        target, result = conservative_temperature_40(edges, np.full((128, 128), 85.))
        self.assertEqual(result.shape, (40, 40))
        np.testing.assert_allclose(result, 85., rtol=0, atol=1e-10)
        np.testing.assert_allclose(target[[0, -1]], edges[[0, -1]])

    def test_noninteger_ratio_preserves_mean_and_range(self):
        fine = np.arange(128 * 128, dtype=float).reshape(128, 128)
        _, coarse = conservative_temperature_40(np.linspace(0., 20., 129), fine)
        self.assertLess(abs(coarse.mean() - fine.mean()), 1e-7)
        self.assertGreaterEqual(coarse.min(), fine.min() - 1e-7)
        self.assertLessEqual(coarse.max(), fine.max() + 1e-7)

    def test_nonfinite_temperature_is_rejected(self):
        with self.assertRaises(ValueError):
            conservative_temperature_40(np.linspace(0., 20., 129), np.full((128, 128), np.nan))

    def test_file_coordinates_plane_and_temperature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'T_source_bottom_128x128_area_C.tab'
            edges = np.linspace(2., 18., 129)
            centers = (edges[:-1] + edges[1:]) / 2
            x, y = np.meshgrid(centers, centers)
            values = np.column_stack((x.ravel(), y.ravel(), np.full(128 * 128, .41), np.full(128 * 128, 90.)))
            np.savetxt(path, values)
            original = path.read_bytes()
            result = convert_temperature_file(path)
            self.assertEqual(result.shape, (1600, 4))
            np.testing.assert_allclose(result[:, 2], .41)
            np.testing.assert_allclose(result[:, 3], 90.)
            np.testing.assert_allclose(result[0, :2], [2.2, 2.2])
            np.testing.assert_allclose(result[-1, :2], [17.8, 17.8])
            self.assertEqual(path.read_bytes(), original)
            values[0, 0] += .1
            np.savetxt(path, values)
            with self.assertRaises(ValueError):
                convert_temperature_file(path)

    def test_wrong_native_resolution_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'T_source_top_128x128_area_C.tab'
            np.savetxt(path, np.ones((1600, 4)))
            with self.assertRaises(ValueError):
                convert_temperature_file(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)

