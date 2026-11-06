"""Permanent directory-boundary regression; no native solver is launched."""
import ast
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
CODE = PROJECT / "icepak_workflow"
sys.path.insert(0, str(CODE))
import settings


class ProductionLayoutTests(unittest.TestCase):
    def test_runtime_roots_are_outside_code(self):
        self.assertEqual(settings.CODE_ROOT, CODE)
        self.assertEqual(settings.RUNS, PROJECT / "icepak_runs")
        for name in ("RUNS", "WORK", "STATE", "SAMPLES", "PUBLISHED"):
            self.assertFalse(getattr(settings, name).is_relative_to(CODE), name)

    def test_case_data_root_is_new_reruns_only(self):
        self.assertEqual(settings.CASE_ROOT, PROJECT / "case_new")
        self.assertEqual(settings.CASE_MASTER, PROJECT / "case_new" / "CASE_MASTER.csv")
        self.assertEqual(settings.CASE_FEATURES, PROJECT / "case_new" / "CASE_FEATURES.csv")

    def test_runtime_creation_does_not_create_code_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            mapping = {name: base / name.lower() for name in
                       ("RUNS", "WORK", "STATE", "SAMPLES", "PUBLISHED")}
            with patch.multiple(settings, **mapping):
                settings.ensure_private_directories()
            self.assertTrue(all(path.is_dir() for path in mapping.values()))
            self.assertEqual(set(base.iterdir()), set(mapping.values()))

    def test_no_retired_code_or_runtime_directories(self):
        for name in ("runs", "data", "assets", ".repair_stage", "candidate",
                     "batch.log", "test_reliability.py", "OPERATIONS.md"):
            self.assertFalse((CODE / name).exists(), name)

    def test_source_tree_has_no_legacy_runtime_imports(self):
        for file in CODE.glob("*.py"):
            tree = ast.parse(file.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0],
                                     ("data", "experiments", "test_reliability"), file.name)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0],
                                         ("data", "experiments", "test_reliability"), file.name)

    def test_source_contact_recovery_profile_is_geometry_derived(self):
        from mesh import counts_for_bbox, profile_counts_for_bbox
        box=(0.,0.,0.,1.25,1.25,.01)
        source="active_top_source1"
        normal=counts_for_bbox(source,box)
        recovered=profile_counts_for_bbox(source,box,"source_contact")
        self.assertEqual(normal,[7,7,4])
        self.assertEqual(recovered,[11,11,4])
        self.assertEqual(profile_counts_for_bbox(source,box,None),normal)
        self.assertEqual(profile_counts_for_bbox(
            "Passive_FEOL_Top_Tile_001",box,"source_contact"),recovered)
        self.assertEqual(profile_counts_for_bbox(
            "Heatsink_Fin_01",(0,0,0,.8,60,18),"source_contact"),[4,60,18])

    def test_source_interface_area_failures_trigger_existing_contact_profile(self):
        from mesh import recovery_profile_for_failed_case
        sample={"status":"FAILED",
                "reason":"NATIVE_SOURCE_INTERFACE_SIDE_AREA: top source 0.0348138"}
        self.assertEqual(recovery_profile_for_failed_case(sample),"source_contact")
        for category in ("NATIVE_SOURCE_INTERFACE_SIDE_COVERAGE",
                         "NATIVE_SOURCE_INTERFACE_SIDE_MISSING",
                         "NATIVE_SOLID_CELL_MISSING"):
            self.assertEqual(recovery_profile_for_failed_case({
                "status":"FAILED","reason":category+": top source"}),"source_contact")
        self.assertEqual(recovery_profile_for_failed_case({
            "status":"FAILED",
            "reason":"NATIVE_FIN_MATERIAL_OCCUPANCY_FAILED"}),"fin_endcaps")
        self.assertIsNone(recovery_profile_for_failed_case({
            "status":"PUBLISHED","reason":sample["reason"]}))
        self.assertIsNone(recovery_profile_for_failed_case({
            "status":"FAILED","reason":"PACKAGE_ENERGY_NOT_CLOSED"}))

    def test_native_watchdog_requires_continuous_inactivity(self):
        from types import SimpleNamespace
        import scheduler
        policy=SimpleNamespace(native_timeout_minutes=40)
        key=("case_test",71234)
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            with (patch.object(scheduler,"STATE",base/"state"),
                  patch.object(scheduler,"WORK",base/"work")):
                scheduler._NATIVE_IDLE_SINCE.clear()
                # Even after three hours, live Fluent CPU work is not stale.
                with patch.object(scheduler,"_recent_native_cpu_activity",return_value=True):
                    self.assertFalse(scheduler._stalled_without_native_progress(
                        "case_test",3*3600,policy,71234))
                with patch.object(scheduler,"_recent_native_cpu_activity",return_value=False):
                    with patch.object(scheduler.time,"monotonic",return_value=1000):
                        self.assertFalse(scheduler._stalled_without_native_progress(
                            "case_test",3*3600,policy,71234))
                    with patch.object(scheduler.time,"monotonic",return_value=1899):
                        self.assertFalse(scheduler._stalled_without_native_progress(
                            "case_test",3*3600,policy,71234))
                    with patch.object(scheduler.time,"monotonic",return_value=1901):
                        self.assertTrue(scheduler._stalled_without_native_progress(
                            "case_test",3*3600,policy,71234))
                scheduler._NATIVE_IDLE_SINCE.clear()

    def test_fin_endcap_recovery_profile_refines_mesh_not_geometry(self):
        from mesh import counts_for_bbox,profile_counts_for_bbox
        fin=(0,0,0,.8,60,18)
        name="Heatsink_Fin_01"
        self.assertEqual(counts_for_bbox(name,fin),[4,60,18])
        self.assertEqual(profile_counts_for_bbox(name,fin,None),[4,60,18])
        self.assertEqual(profile_counts_for_bbox(
            name,fin,"fin_endcaps"),[8,120,36])
        source=(0,0,0,1.25,1.25,.01)
        self.assertEqual(profile_counts_for_bbox(
            "active_top_source1",source,"fin_endcaps"),[7,7,4])

    def test_native_inventory_rechecks_only_a_transient_empty_reply(self):
        from unittest.mock import Mock
        from native_cad import solid_names
        editor=Mock()
        editor.GetObjectsInGroup.side_effect=[None,["A","B"]]
        self.assertEqual(solid_names(editor),("A","B"))
        self.assertEqual(editor.GetObjectsInGroup.call_count,2)
        editor.GetObjectsInGroup.side_effect=[None,None]
        with self.assertRaisesRegex(RuntimeError,"native solid inventory"):
            solid_names(editor)

    def test_source_power_uses_canonical_native_api(self):
        from pathlib import Path
        source=(CODE / "geometry.py").read_text(encoding="utf-8")
        tree=ast.parse(source)
        calls=(node for node in ast.walk(tree) if isinstance(node,ast.Call))
        self.assertTrue(any(isinstance(node.func,ast.Attribute)
                            and node.func.attr=="assign_source" for node in calls))
        self.assertNotIn("_assign_source_power",source)

    def test_local_resource_policy_preserved(self):
        policy = settings.policy_from_environment()
        self.assertEqual(policy.max_parallel, 3)
        self.assertEqual(policy.cores_per_case, 1)
        self.assertEqual(policy.native_solver_slots, 2)
        self.assertEqual(policy.minimum_native_commit_gib, 8.0)
        self.assertEqual(policy.minimum_free_disk_gib, 5.0)
        self.assertEqual(policy.projected_workspace_gib, 1.5)
        self.assertEqual(settings.T0_LEVELS, (250, 500, 1000, 2000))
        self.assertEqual((settings.GAMMA_GRID, settings.SOURCE_GRID, settings.HTC_GRID),
                         (80, 128, 40))

    def test_source_temperature_window_is_0p6_without_relaxing_interface(self):
        from types import SimpleNamespace
        from acceptance import accept_sample
        import physical_convergence
        self.assertEqual(settings.SOURCE_TEMPERATURE_STABILITY_K, 0.6)
        self.assertEqual(physical_convergence.SOURCE_TEMPERATURE_STABILITY_K, 0.6)
        layer = {"family": "interface",
                 "method": "PAIRED_NATIVE_SOURCE_INTERFACE_AREA_V1",
                 "side_temperature_delta_max_K": 0.2,
                 "source_side_area_relative_error": 0.,
                 "upper_side_area_relative_error": 0.,
                 "native_area_relative_error": 0.,
                 "area_relative_error": 0.,
                 "mean_pixel_coverage": 1.,
                 "core_min_pixel_coverage": 1.,
                 "max_pixel_coverage": 1.}
        proof = {
            "status": "PASS", "case_id": "case_test",
            "surface_contract": "PAIRED_NATIVE_SOURCE_INTERFACE_V1",
            "native_fin_gate": {"status": "PASS_NATIVE_FIN_MATERIAL_RAYS"},
            "native_solid_cells": {"status": "PASS_NATIVE_SOLID_CELLS"},
            "native_convergence": {"status": "MEASURED", "iterations": 160},
            "mass_balance": {"status": "MEASURED", "relative_imbalance": 0.0005},
            "source_temperature_stability": {"status": "MEASURED",
                "sampled_window_iterations": 40, "max_temperature_range_K": 0.6},
            "package_energy": {"status": "PASS", "relative_error": 0.002},
            "sink_contact": {"status": "PASS", "area_ratio": 1.,
                             "min_fin_coverage": 1.},
            "gamma": {key: {"area_relative_error": 0.,
                            "heat_relative_error": 0.} for key in ("80", "40")},
            "source": {key: dict(layer) for key in
                       ("bot", "top", "bot_48", "top_48")},
            "source_power_W": {"bot": 1., "top": 2.}}
        spec = SimpleNamespace(case_id="case_test", total_power_W=3.)
        self.assertEqual(accept_sample(spec, proof)["status"], "PASS")
        proof["native_convergence"]["iterations"] = 1200
        self.assertEqual(accept_sample(spec, proof)["status"], "PASS")
        proof["source_temperature_stability"]["max_temperature_range_K"] = 0.600001
        with self.assertRaisesRegex(ValueError, "not stabilized"):
            accept_sample(spec, proof)
        proof["source_temperature_stability"]["max_temperature_range_K"] = 0.6
        proof["source"]["top"]["side_temperature_delta_max_K"] = 0.51
        with self.assertRaisesRegex(ValueError, "interface sides disagree"):
            accept_sample(spec, proof)

    def test_native_stability_has_no_total_iteration_limit(self):
        import physical_convergence
        history = []
        def next_window(workspace, initial, cores, source_layers):
            history.append(initial)
            count = len(history)
            if count <= 15:
                return None, {"status": "MEASURED",
                              "iterations": 400 + 40 * count,
                              "continuation_phase": count}
            return {"status": "MEASURED", "iterations": 1040}, None
        with patch.object(physical_convergence, "_verify_next_window",
                          side_effect=next_window):
            receipt = physical_convergence.verify_and_continue(
                "unused", {"status": "MEASURED", "iterations": 400},
                1, frozenset())
        self.assertEqual(len(history), 16)
        self.assertEqual(receipt["iterations"], 1040)
        source = (CODE / "physical_convergence.py").read_text(encoding="utf-8")
        for old_cap in ("final+40>450", "final>450", "phase<=12",
                        "iteration<=410"):
            self.assertNotIn(old_cap, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)

