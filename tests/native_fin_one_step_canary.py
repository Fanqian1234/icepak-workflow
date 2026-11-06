"""Isolated one-iteration native mesh canary; NEVER a production simulation.

Copy a previously saved *physical* AEDT project to a fresh test-only folder,
replace the shared fin-local operation with one operation per unchanged fin,
then run only one solver iteration to obtain the native Fluent cell mesh.
The existing production native-fin gate is the sole material-ray judge.
Run only when the formal scheduler and every formal worker have exited.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2] / "icepak_workflow"
sys.path.insert(0, str(ROOT))

from case_spec import case_by_id
from geometry import plan_case_geometry
from mesh import FIN_LOCAL_BUDGET_CELLS, counts_for_bbox
from native_cad import solid_names
from native_fin_gate import verify_native_fins
from settings import AEDT_VERSION, GIB, STATE, active_case_workers


def _preflight(workspace: Path):
    active = active_case_workers()
    if active:
        raise RuntimeError(f"REFUSE_ACTIVE_FORMAL_CASES: {active}")
    lock = STATE / "scheduler.lock"
    if lock.is_file():
        try:
            owner = int(lock.read_text(encoding="ascii").strip())
        except (ValueError, OSError):
            owner = 0
        if owner and psutil.pid_exists(owner):
            raise RuntimeError(f"REFUSE_LIVE_FORMAL_SCHEDULER: {owner}")
    free_ram = psutil.virtual_memory().available / GIB
    free_disk = psutil.disk_usage(workspace).free / GIB
    if free_ram < 18 or free_disk < 20:
        raise RuntimeError(
            f"REFUSE_RESOURCE_PRESSURE: free_RAM_GiB={free_ram:.2f} "
            f"free_disk_GiB={free_disk:.2f}")
    return {"free_ram_gib": free_ram, "free_disk_gib": free_disk}


def _assign_per_fin(app, plan):
    module = app.omeshmodule or app.odesign.GetModule("MeshRegion")
    available = set(solid_names(app.modeler.oeditor))
    required = set(plan.by_name) | {"Air_Domain"}
    if not required <= available:
        raise ValueError("Test AEDT project is missing frozen CAD solids")
    local = sorted(n for n in available if
                   n == "TIM2" or n.startswith("Heatsink_Fin_")
                   or n.startswith("Passive_FEOL_")
                   or (n.lower().startswith("active_") and "source" in n.lower()))
    fins = sorted((n for n in local if n.startswith("Heatsink_Fin_")),
                  key=lambda n: float(plan.by_name[n].bbox_mm[0]))
    if len(fins) < 2:
        raise ValueError("No complete original fin array")
    first_index = local.index(fins[0]) + 1
    group = f"PhysicalSourceMesh_{first_index:03d}"
    before = tuple(map(str, module.GetOperationNames()))
    if group not in before:
        raise ValueError(f"Original shared fin operation not found: {group}")
    counts = [counts_for_bbox(n, plan.by_name[n].bbox_mm) for n in fins]
    if any(row != counts[0] for row in counts):
        raise ValueError("The original fin array lacks uniform local mesh counts")
    if math.prod(counts[0]) * len(fins) > FIN_LOCAL_BUDGET_CELLS:
        raise ValueError("Test would not reproduce production coarsened fin counts")

    # Only the disposable copied project is ever changed.  Every other
    # mesh operation, global resolution, CAD part and boundary stays frozen.
    module.DeleteOp([group])
    if group in set(map(str, module.GetOperationNames())):
        raise RuntimeError("Shared fin operation was not deleted in test copy")
    names = []
    for i, (name, row) in enumerate(zip(fins, counts), 1):
        opname = f"FinCanary_{i:03d}"
        module.AssignMeshOperation([
            f"NAME:{opname}", "Enable:=", True,
            "MaxLevel:=", "0", "MinLevel:=", "0",
            "Local Mesh Parameters Enabled:=", True,
            "Mesh Reuse Enabled:=", False,
            "Local Mesh Parameters Type:=", "Box Local Mesh Parameters",
            "X count:=", str(row[0]), "Y count:=", str(row[1]),
            "Z count:=", str(row[2]), "Objects:=", [name],
            "Mesh Object(s) Separately Enabled:=", True,
        ])
        names.append(opname)
    after = set(map(str, module.GetOperationNames()))
    if not set(names) <= after or set(before) - {group} - after:
        raise RuntimeError("Candidate fin mesh operations are incomplete")
    return {"original_group_operation": group,
            "per_fin_operation_count": len(names),
            "local_cells_per_fin": counts[0],
            "unmodified_non_fin_operation_count": len(before) - 1}


def run(case_id: str, source_project: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "native_one_step_result.json"
    report = {"case_id": case_id,
              "mode": "TEST_ONE_ITERATION_MESH_ONLY_EVIDENCE",
              "not_formal_CHT_acceptance": True}
    app = None
    try:
        if not source_project.is_file() or source_project.suffix.lower() != ".aedt":
            raise FileNotFoundError("Missing frozen input AEDT project")
        report["resources"] = _preflight(output_dir)
        spec = case_by_id(case_id)
        plan = plan_case_geometry(spec)
        with tempfile.TemporaryDirectory(prefix="canary_run_", dir=output_dir) as temp:
            root = Path(temp)
            project = root / "case.aedt"
            shutil.copy2(source_project, project)
            from icepak import configure_aedt_runtime
            configure_aedt_runtime(root)
            from ansys.aedt.core import Icepak
            try:
                app = Icepak(project=str(project), version=AEDT_VERSION,
                             non_graphical=True, new_desktop=True,
                             close_on_exit=False)
                if not app:
                    raise RuntimeError("Canary AEDT startup failed")
                native_temp = root / "native_temp"
                native_temp.mkdir()
                app.odesktop.SetTempDirectory(str(native_temp))
                report["mesh_assignment"] = _assign_per_fin(app, plan)
                setup = app.get_setup("Setup1")
                if not setup or "Convergence Criteria - Max Iterations" not in setup.props:
                    raise RuntimeError("Frozen CHT setup is unavailable")
                report["original_max_iterations"] = int(
                    setup.props["Convergence Criteria - Max Iterations"])
                setup.props["Convergence Criteria - Max Iterations"] = 1
                if not setup.update() or not app.save_project():
                    raise RuntimeError("Disposable one-step setup not saved")
                report["one_step_solver_return"] = bool(app.analyze_setup(
                    "Setup1", cores=1, tasks=1,
                    use_auto_settings=False, blocking=True))
                meshes = list((root / "case.aedtresults").rglob("current.nc_cas"))
                if not meshes:
                    raise RuntimeError("No post-mesh native Fluent .nc_cas from one-step canary")
                mesh = max(meshes, key=lambda p: p.stat().st_mtime)
                report["native_mesh_bytes"] = mesh.stat().st_size
                report["native_fin_gate"] = verify_native_fins(mesh, plan)
                report["status"] = "CANDIDATE_NATIVE_FIN_PASS"
            finally:
                if app is not None:
                    app.release_desktop(close_projects=True, close_desktop=True)
                    app = None
    except Exception as exc:
        report["status"] = "CANDIDATE_NOT_VERIFIED"
        report["reason"] = f"{type(exc).__name__}: {exc}"
        report["trace_tail"] = traceback.format_exc().splitlines()[-6:]
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["status"] == "CANDIDATE_NATIVE_FIN_PASS" else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", required=True)
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    return run(args.case, args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())

