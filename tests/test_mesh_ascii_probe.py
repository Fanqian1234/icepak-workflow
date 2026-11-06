"""Small synthetic format checks; never substitute for a native Icepak trial."""
from pathlib import Path
import tempfile
import unittest

from mesh_ascii_probe import scan_ascii_volume_mesh, summarize_rays


RAYS = [
    (f"Heatsink_Fin_01_solid", 0.5234567, z, 0.0, 10.0)
    for z in (0.42, 1.03578, 1.57735)
] + [
    (f"Heatsink_Fin_02_solid", 2.5234567, z, 0.0, 10.0)
    for z in (0.42, 1.03578, 1.57735)
] + [
    (f"Heatsink_Fin_01__Heatsink_Fin_02_channel", 1.371234567, z, 0.0, 10.0)
    for z in (0.42, 1.03578, 1.57735)
]


def fixture_text(intrusion=False):
    cells = [
        (0.0, 1.0, 0.0, 10.0, 0.0, 2.0, 1),
        (2.0, 3.0, 0.0, 10.0, 0.0, 2.0, 2),
        (1.0, 2.0, 0.0, 10.0, 0.0, 2.0, 3),
    ]
    if intrusion:
        cells.append((0.0, 1.0, 4.0, 5.0, 0.0, 2.0, 4))
    nodes = []
    faces = []
    for x0, x1, y0, y1, z0, z1, cid in cells:
        first = len(nodes) + 1
        nodes.extend([
            (x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
            (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1),
        ])
        for v in ((1,2,3,4),(5,8,7,6),(1,5,6,2),
                  (2,6,7,3),(3,7,8,4),(4,8,5,1)):
            faces.append((*(first+k-1 for k in v),cid,0))
    text = ['(0 "synthetic test volume mesh")', '(2 3)',
            f'(10 (0 1 {len(nodes):x} 0))',
            f'(10 (1 1 {len(nodes):x} 1 3)(']
    text.extend(' '.join(f'{v/1000:.9g}' for v in node) for node in nodes)
    text.append('))')
    text.extend(['(12 (0 1 '+f'{len(cells):x}'+' 0))',
                 '(12 (a 1 1 11 4))', '(12 (b 2 2 11 4))',
                 f'(12 (c 3 {len(cells):x} 1 4))',
                 f'(13 (0 1 {len(faces):x} 0))',
                 f'(13 (5 1 {len(faces):x} 3 4)('])
    text.extend(' '.join(f'{v:x}' for v in face) for face in faces)
    text.extend(['))', '(39 (10 solid Heatsink_Fin_01_solid 1)(material . aluminum))',
                 '(39 (11 solid Heatsink_Fin_02_solid 1)(material . aluminum))',
                 '(39 (12 fluid Air_Domain_fluid 1)(material . air))'])
    return '\n'.join(text)+'\n'


class MeshASCIIProbeTests(unittest.TestCase):
    def _run(self, intrusive):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'mesh.msh'
            path.write_text(fixture_text(intrusive), encoding='ascii')
            reports, examined = scan_ascii_volume_mesh(path, RAYS)
            self.assertGreater(examined, 0)
            return summarize_rays(RAYS, reports)

    def test_disjoint_fins_pass_mesh_only(self):
        result = self._run(False)
        self.assertEqual(result['status'], 'MESH_ONLY_RAY_CANDIDATE_PASS')
        self.assertEqual(result['failed_count'], 0)

    def test_fluid_intrusion_is_rejected(self):
        result = self._run(True)
        self.assertEqual(result['status'], 'MESH_ONLY_RAY_FAILED')
        self.assertEqual(result['failed_count'], 3)
        self.assertGreater(result['failures'][0]['overlap_mm'], 0.9)

    def test_surface_mesh_cannot_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'surface_only.msh'
            path.write_text('(10 (0 1 2 0))\n(12 (0 1 0 0))\n', encoding='ascii')
            with self.assertRaises(ValueError):
                scan_ascii_volume_mesh(path, RAYS)

    def test_multiple_faces_on_same_line(self):
        text = fixture_text(False)
        start = text.index('(13 (5 ')
        end = text.index('))\n(39 ', start)
        text = text[:start] + text[start:end].replace('\n', ' ') + text[end:]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'packed_faces.msh'
            path.write_text(text, encoding='ascii')
            reports, _ = scan_ascii_volume_mesh(path, RAYS)
            self.assertEqual(summarize_rays(RAYS, reports)['failed_count'], 0)

    def test_truncated_face_data_cannot_pass(self):
        text = fixture_text(False)
        start = text.index('(13 (5 ')
        end = text.index('))\n(39 ', start)
        cut = text[start:end].rsplit(' ', 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'truncated_faces.msh'
            path.write_text(text[:start]+cut+text[end:], encoding='ascii')
            with self.assertRaises(ValueError):
                scan_ascii_volume_mesh(path, RAYS)


if __name__ == '__main__':
    unittest.main()

