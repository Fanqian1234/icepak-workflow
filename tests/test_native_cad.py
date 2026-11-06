"""Focused regression tests for the native CAD API boundary; no AEDT launch."""
import unittest
from types import SimpleNamespace
from native_cad import create_boxes, face_snapshot, native_bounds, native_volume


class Editor:
    def __init__(self, reply_missing=False, omit_box=False):
        self.names = []
        self.reply_missing = reply_missing
        self.omit_box = omit_box
        self.calls = {}

    def hit(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def CreateBox(self, parameters, attributes):
        self.hit("create")
        name = attributes[attributes.index("Name:=") + 1]
        if not self.omit_box:
            self.names.append(name)
        return None if self.reply_missing else name

    def GetObjectsInGroup(self, group):
        self.hit("inventory")
        return self.names

    def GetObjectBoundingBox(self, name):
        raise AssertionError("Do not reintroduce the unstable bounding-box API")

    def GetFaceIDs(self, name):
        self.hit("faces")
        return [11, 12]

    def GetFaceCenter(self, fid):
        self.hit("center")
        return [0.0, 0.0, float(fid)]

    def GetFaceArea(self, fid):
        self.hit("area")
        return 1.0

    def GetVertexIDsFromObject(self, name):
        return list(range(8))

    def GetVertexPosition(self, vid):
        return [float((vid >> axis) & 1) for axis in range(3)]

    def GetObjectVolume(self, name):
        return 1.0


class Modeler:
    def __init__(self, editor):
        self.oeditor = editor
        self.material_calls = 0

    def _pos_with_arg(self, values):
        return [str(v) + "mm" for v in values]

    def _default_object_attributes(self, *, name, material):
        self.material_calls += 1
        return ["NAME:Attributes", "Name:=", name, "MaterialValue:=", '"' + material + '"']


def part(name):
    return SimpleNamespace(name=name, material="Si", origin_mm=(0., 0., 0.), dimensions_mm=(1., 1., 1.))


class NativeCadTests(unittest.TestCase):
    def test_batch_does_not_invent_names_after_empty_reply(self):
        e = Editor(reply_missing=True)
        app = SimpleNamespace(modeler=Modeler(e))
        result = create_boxes(app, [part("box_" + str(i)) for i in range(40)])
        self.assertEqual(len(result), 40)
        self.assertEqual(e.calls, {"create": 40, "inventory": 1})
        self.assertEqual(app.modeler.material_calls, 1)

    def test_missing_real_object_fails(self):
        app = SimpleNamespace(modeler=Modeler(Editor(reply_missing=True, omit_box=True)))
        with self.assertRaises(RuntimeError):
            create_boxes(app, [part("missing")])

    def test_duplicate_requested_name_fails_before_creation(self):
        e = Editor()
        with self.assertRaises(ValueError):
            create_boxes(SimpleNamespace(modeler=Modeler(e)), [part("x"), part("x")])
        self.assertFalse(e.calls)

    def test_name_collision_fails_before_creation(self):
        e = Editor()
        with self.assertRaises(ValueError):
            create_boxes(SimpleNamespace(modeler=Modeler(e)), [part("x")], existing=["x"])
        self.assertFalse(e.calls)

    def test_material_fallback_is_rejected(self):
        modeler = Modeler(Editor())
        modeler._default_object_attributes = lambda **kw: ["Name:=", kw["name"], "MaterialValue:=", '"air"']
        with self.assertRaises(RuntimeError):
            create_boxes(SimpleNamespace(modeler=modeler), [part("x")])

    def test_face_snapshot_queries_each_value_once(self):
        e = Editor()
        self.assertEqual(len(face_snapshot(e, "body")), 2)
        self.assertEqual(e.calls, {"faces": 1, "center": 2, "area": 2})

    def test_missing_face_is_not_silently_replaced(self):
        e = Editor()
        e.GetFaceIDs = lambda name: None
        with self.assertRaises(RuntimeError):
            face_snapshot(e, "body")

    def test_nonfinite_face_area_is_rejected(self):
        e = Editor()
        e.GetFaceArea = lambda fid: float("nan")
        with self.assertRaises(RuntimeError):
            face_snapshot(e, "body")

    def test_native_bounds_are_measured_not_planned(self):
        self.assertEqual(native_bounds(Editor(), "body"), (0., 0., 0., 1., 1., 1.))

    def test_missing_native_vertex_is_rejected(self):
        e = Editor()
        e.GetVertexPosition = lambda vid: None
        with self.assertRaises(RuntimeError):
            native_bounds(e, "body")

    def test_transient_vertex_coordinate_retries_real_vertex_once(self):
        e = Editor()
        e.names = ["Air_Domain"]
        original = e.GetVertexPosition
        attempts = []

        def flaky(vid):
            attempts.append(vid)
            if vid == 0 and attempts.count(0) == 1:
                return None
            return original(vid)

        e.GetVertexPosition = flaky
        self.assertEqual(native_bounds(e, "Air_Domain"),
                         (0., 0., 0., 1., 1., 1.))
        self.assertEqual(attempts.count(0), 2)
        self.assertEqual(e.calls["inventory"], 1)

    def test_steady_setup_never_depends_on_implicit_solution_type(self):
        from icepak import _steady_setup
        required = ("Flow Regime", "Include Flow", "Include Temperature",
                    "Include Gravity", "Include Solar", "Radiation Model",
                    "Convergence Criteria - Energy",
                    "Convergence Criteria - Flow",
                    "Discretization Scheme - Temperature",
                    "Convergence Criteria - Max Iterations")
        setup = SimpleNamespace(name="Setup1", props=dict.fromkeys(required),
                                update=lambda: True)
        calls = []

        def create(name, setup_type=None):
            calls.append((name, setup_type))
            return setup

        app = SimpleNamespace(setup_names=[], create_setup=create)
        self.assertEqual(_steady_setup(app, 450), "Setup1")
        self.assertEqual(calls, [("Setup1", "IcepakSteadyState")])
        self.assertEqual(setup.props["Convergence Criteria - Max Iterations"], 450)

    def test_bad_native_volume_is_rejected(self):
        e = Editor()
        e.GetObjectVolume = lambda name: 0.
        with self.assertRaises(RuntimeError):
            native_volume(e, "body")

    def test_transient_null_volume_requeries_real_solid_once(self):
        e = Editor()
        e.names = ["body"]
        replies = iter((None, 1.25))
        e.GetObjectVolume = lambda name: next(replies)
        self.assertEqual(native_volume(e, "body"), 1.25)
        self.assertEqual(e.calls["inventory"], 1)

    def test_null_volume_rejects_missing_solid(self):
        e = Editor()
        e.GetObjectVolume = lambda name: None
        with self.assertRaisesRegex(RuntimeError, "Native solid missing"):
            native_volume(e, "body")

    def test_persistent_null_volume_is_rejected(self):
        e = Editor()
        e.names = ["body"]
        e.GetObjectVolume = lambda name: None
        with self.assertRaisesRegex(RuntimeError, "Native volume unavailable"):
            native_volume(e, "body")


if __name__ == "__main__":
    unittest.main(verbosity=2)

