"""Fresh AEDT 2023 R2 Icepak CHT simulation, independent of the old carrier.

The only CAD input is a physical CADPlan generated from the authoritative
Case master. Old .aedt files and old queue modules are never opened/imported.
"""
from __future__ import annotations

import json
import os
import time
import shutil
import psutil
from contextlib import contextmanager
from pathlib import Path

from geometry import create_native_cad
from native_cad import solid_names, face_snapshot, native_face_area
from mesh import apply_physical_source_mesh
from settings import AEDT_VERSION, AMBIENT_C, RUNS

# Steady-state thermal conductivity from the verified reference material
# definitions, W/(m K). Three-vectors are native anisotropic tensors.
THERMAL_K = {
    "Substrate":1.9, "Bump":[10.0,10.0,30.0],
    "HB":[5.0,5.0,15.0], "Si":140.0,
    "TIM1":10.0, "TIM2":5.0, "FEOL":20.0,
    "Cu":400.0,"aluminum":200.0,"Al-Extruded":205.0,
    "air":0.0261,
}
# Literal project-material values recovered from the accepted native .aedt
# material library. Steady-state thermal properties are never left to
# version-dependent AEDT system defaults.
THERMAL_DENSITY_CP = {
    "Substrate":(1.0,1_000_000.0), "Bump":(1.0,3_000_000.0),
    "HB":(1.0,3_000_000.0), "Si":(1.0,1_630_000.0),
    "TIM1":(1.0,3_000_000.0), "TIM2":(1.0,3_000_000.0),
    "FEOL":(1.0,1_630_000.0), "Cu":(1.0,3_450_000.0),
    "aluminum":(1.0,2_400_000.0),
    "Al-Extruded":(2800.0,900.0), "air":(1.1614,1005.0),
}


def _materials(app,plan):
    # Do not change the global user/project material library.
    materials=app.materials
    for name in {part.material for part in plan.solids} | {"air"}:
        conductivity=THERMAL_K.get(name)
        if conductivity is None:
            raise ValueError("Unknown physical material: "+name)
        if name.lower() not in {str(k).lower() for k in materials.material_keys}:
            material=materials.add_material(name)
        else:
            material=materials[name]
        if not material:
            raise RuntimeError("Cannot create native thermal material "+name)
        material.thermal_conductivity=conductivity
        density,heat_capacity=THERMAL_DENSITY_CP[name]
        material.mass_density=density
        material.specific_heat=heat_capacity
        if name=="air":
            # Exact constant air viscosity of the accepted AEDT carrier.
            material.viscosity=1.84e-5


def set_native_ambient(app):
    """Apply the old 20 C design ambient in both CHT and package-only T0."""
    design_settings=app.odesign.GetChildObject("Design Settings")
    if "AmbTemp" not in design_settings.GetPropNames():
        raise RuntimeError("Native AEDT ambient setting is unavailable")
    design_settings.SetPropValue("AmbTemp",f"{AMBIENT_C:.12g}cel")
    reported=str(design_settings.GetPropValue("AmbTemp")).strip().lower()
    if (not reported.endswith("cel") or
            abs(float(reported[:-3].strip())-AMBIENT_C)>1e-6):
        raise RuntimeError("Native ambient temperature differs from contract")


def assign_package_exterior_thermal(app,plan,include_heatsink=False):
    """Restore verified package and exposed-contact walls in CHT and T0.

    Match the accepted direct-native carrier: seven package layer sides
    are adiabatic and the Substrate bottom has HTC=50. All internal
    TIM2/IHS/heatsink and Source contacts remain native solid contacts;
    no added wall boundary is assigned to those interfaces.
    """
    layers=("Substrate","Bump","HB","Die","TIM1","IHS","TIM2")
    side=[]
    for name in layers:
        box=plan.by_name[name].bbox_mm
        native_faces=face_snapshot(app.modeler.oeditor,name,areas=False)
        for axis in (0,1):
            for offset in (0,3):
                plane=[int(face.id) for face in native_faces
                       if abs(float(face.center[axis])-box[axis+offset])<1e-6]
                if not plane:
                    raise RuntimeError(name+" missing an insulated lateral plane")
                side.extend(plane)
    # Match the accepted carrier: only seven package lateral surfaces
    # and the substrate bottom have explicit thermal-wall boundaries.
    # IHS/TIM2/base interfaces stay native conducting contacts.
    side=sorted(set(side))
    bottom_z=float(plan.by_name["Substrate"].bbox_mm[2])
    bottom_faces=[f for f in face_snapshot(app.modeler.oeditor,"Substrate",areas=False)
                  if abs(float(f.center[2])-bottom_z)<1e-7]
    bottom=sorted(int(f.id) for f in bottom_faces)
    substrate_size=plan.by_name["Substrate"].dimensions_mm
    expected_bottom=substrate_size[0]*substrate_size[1]
    if abs(sum(native_face_area(app.modeler.oeditor,f.id) for f in bottom_faces)-expected_bottom)>max(.05,expected_bottom*.005):
        raise RuntimeError("Package bottom HTC=50 would miss physical area")
    if not side or not bottom or set(side)&set(bottom):
        raise RuntimeError("Package side/bottom physical face assignment invalid")
    common={"Radiation":{"Radiate":False},"Shell Conduction":False,
            "External Surface Radiation":False,
            "Heat Transfer Data":{"Heat Transfer Correlation":False}}
    insulated={"Faces":side,"External Condition":"Heat Flux",
               "Heat Flux":"0irrad_W_per_m2",**common}
    cooled={"Faces":bottom,"External Condition":"Heat Transfer Coefficient",
            "Heat Transfer Coefficient":"50w_per_m2kel",
            "Reference Temperature":f"{AMBIENT_C:.12g}cel",**common}
    if not app._create_boundary("pkg_side_adiabatic",insulated,"Stationary Wall"):
        raise RuntimeError("Cannot assign zero-heat-flux package lateral wall")
    if not app._create_boundary("pkg_bottom_htc50",cooled,"Stationary Wall"):
        raise RuntimeError("Cannot assign package substrate bottom HTC=50")
    return {"side_wall_faces":len(side),"bottom_wall_faces":len(bottom),
            "bottom_htc_W_m2K":50.0,"ambient_C":AMBIENT_C}


def remove_automatic_region(app):
    """Delete Icepak's implicit air Region before assigning package walls.

    This is a built-in CAD object, distinct from the global mesh settings.
    It must not wrap a package-only Robin wall.
    """
    regions=[n for n in app.modeler.object_names
             if str(n).casefold()=="region"]
    if len(regions)>1:
        raise RuntimeError("Multiple implicit Icepak air regions")
    if regions:
        if not app.modeler.delete(regions[0]):
            raise RuntimeError("Unable to remove automatic Icepak Region")
        if any(str(n).casefold()=="region"
               for n in app.modeler.object_names):
            raise RuntimeError("Automatic Icepak Region still exists")
    return bool(regions)


def assign_package_top_htc(app,plan,htc_W_m2K,name="Gamma_bound_h0"):
    """Direct Robin wall on exposed TIM2 top, in a package-only solid model.

    Use the same native Stationary Wall representation as the verified
    substrate-bottom HTC=50 boundary. Never build an enclosing air Region.
    """
    htc=float(htc_W_m2K)
    if not 0.0<htc<1.0e7:
        raise ValueError("Invalid positive package-top HTC")
    if {"Air_Domain","Region"} & set(solid_names(app.modeler.oeditor)):
        raise RuntimeError("Package-only Robin model must have no air box")
    top_z=float(plan.gamma_bbox_mm[2])
    faces=[f for f in face_snapshot(app.modeler.oeditor,"TIM2",areas=False)
           if abs(float(f.center[2])-top_z)<1e-7]
    area=sum(native_face_area(app.modeler.oeditor,f.id) for f in faces)
    dimensions=plan.by_name["TIM2"].dimensions_mm
    expected_area=float(dimensions[0])*float(dimensions[1])
    if (not faces or
            abs(area-expected_area)>max(.05,.005*expected_area)):
        raise RuntimeError("TIM2 top HTC misses physical surface area")
    props={"Faces":sorted(int(f.id) for f in faces),
           "External Condition":"Heat Transfer Coefficient",
           "Heat Transfer Coefficient":f"{htc:.12g}w_per_m2kel",
           "Reference Temperature":f"{AMBIENT_C:.12g}cel",
           "Heat Transfer Data":{"Heat Transfer Correlation":False},
           "Radiation":{"Radiate":False},
           "External Surface Radiation":False,
           "Shell Conduction":False}
    wall=app._create_boundary(name,props,"Stationary Wall")
    if not wall:
        raise RuntimeError("Direct TIM2 top Robin boundary creation failed")
    if set(map(int,wall.props.get("Faces",[])))!=set(props["Faces"]):
        raise RuntimeError("Direct TIM2 top Robin face ownership mismatch")
    return wall


def _fluid_domain(app,plan,solid_names):
    """Flow region starts at the heatsink underside, never below the package.

    The package's lateral adiabatic walls and bottom HTC=50 are not air-solid
    interfaces. Subtract only heatsink solids: the flow domain must not
    accidentally expose the insulated package to forced convection.
    """
    sink=[s for s in plan.solids if s.role in ("Heatsink_Base","Fin")]
    sink_names=[s.name for s in sink]
    if not sink or not set(sink_names).issubset(set(solid_names)):
        raise RuntimeError("Heatsink solids missing from native air model")
    # The accepted original Box1 is a FIXED 100 x 200 x 70.78 mm box.
    # Its exact bbox, recovered from multiple previously accepted native
    # preexecution_audit.json records, must not follow the heatsink size.
    origin=[-40.0,-90.0,4.22]
    far=[60.0,110.0,75.0]
    extent=[far[i]-origin[i] for i in range(3)]
    base_bottom=plan.by_name["Heatsink_Base"].bbox_mm[2]
    package_top=plan.by_name["TIM2"].bbox_mm[5]
    if abs(base_bottom-package_top)>1e-7 or abs(base_bottom-origin[2])>1e-7:
        raise RuntimeError("Frozen air bottom must coincide with TIM2/base Gamma")
    for part in sink:
        box=part.bbox_mm
        if any(box[i]<origin[i]-1e-7 or box[i+3]>far[i]+1e-7
               for i in range(3)):
            raise RuntimeError("Physical heatsink exceeds proven original Box1: "+part.name)
    air=app.modeler.create_box(origin,extent,name="Air_Domain",material="air")
    if not air or air.name!="Air_Domain":
        raise RuntimeError("Physical air domain not constructed")
    # Historical frozen-CAD contract: the boolean MUST remove exactly the
    # full fin+base volumes from air, preserving every original sink solid.
    frozen={name:(tuple(float(v) for v in app.modeler[name].bounding_box),
                  float(app.modeler[name].volume)) for name in sink_names}
    expected_volume=extent[0]*extent[1]*extent[2]-sum(
        volume for _,volume in frozen.values())
    volume_tol=max(.01,extent[0]*extent[1]*extent[2]*1e-6)
    if expected_volume<=0 or any(volume<=0 for _,volume in frozen.values()):
        raise RuntimeError("Invalid sink CAD volume before air subtraction")
    if not app.modeler.subtract(["Air_Domain"],sink_names,keep_originals=True):
        raise RuntimeError("Heatsink/air native Boolean subtraction failed")
    if abs(float(app.modeler["Air_Domain"].volume)-expected_volume)>volume_tol:
        raise RuntimeError("Air Boolean did not remove all native fins and base")
    outer=tuple(float(v) for v in app.modeler["Air_Domain"].bounding_box)
    expected_bbox=tuple(origin+[origin[i]+extent[i] for i in range(3)])
    if any(abs(v-w)>1e-5 for v,w in zip(outer,expected_bbox)):
        raise RuntimeError("Air Boolean altered the external physical envelope")
    for name,(bbox,volume) in frozen.items():
        part=app.modeler[name]
        after=tuple(float(v) for v in part.bounding_box)
        if (any(abs(v-w)>1e-6 for v,w in zip(after,bbox)) or
                abs(float(part.volume)-volume)>max(1e-7,abs(volume)*1e-8)):
            raise RuntimeError("Air Boolean altered frozen heatsink solid "+name)
    # The accepted 110-Case flow model used ONE complete Box1 Y-inlet
    # and ONE complete Y-outlet. Verify their actual native face area and
    # ownership after the sink/air Boolean, not merely a point lookup.
    candidates={"inlet":[],"outlet":[]}
    expected_opening_area=extent[0]*extent[2]
    for face in app.modeler["Air_Domain"].faces:
        y=float(face.center[1])
        for key,target in (("inlet",origin[1]),("outlet",origin[1]+extent[1])):
            if (abs(y-target)<1e-6 and
                    abs(float(face.area)-expected_opening_area)<1e-3):
                candidates[key].append(int(face.id))
    if any(len(rows)!=1 for rows in candidates.values()):
        raise RuntimeError("Physical air inlet/outlet faces are not complete: "+str(candidates))
    inlet_id=candidates["inlet"][0]
    outlet_id=candidates["outlet"][0]
    if inlet_id==outlet_id:
        raise RuntimeError("Air inlet/outlet face identity collapsed")
    # Use the same 2023 R2 velocity/pressure opening APIs as the
    # accepted 110-Case run, with immutable explicit inlet temperature.
    inlet=app.assign_velocity_free_opening(
        inlet_id,boundary_name="Opening1",
        temperature=f"{AMBIENT_C:.12g}cel",
        velocity=["0m_per_sec",f"{plan.inlet_velocity_m_per_s:.12g}m_per_sec",
                  "0m_per_sec"])
    outlet=app.assign_pressure_free_opening(
        outlet_id,boundary_name="Opening2",
        temperature=f"{AMBIENT_C:.12g}cel")
    if not inlet or not outlet:
        raise RuntimeError("Cannot assign native velocity-inlet/pressure-outlet")
    if (inlet.props.get("Inlet Type")!="Velocity"
            or outlet.props.get("Inlet Type")!="Pressure"
            or set(map(int,inlet.props.get("Faces",[])))!={inlet_id}
            or set(map(int,outlet.props.get("Faces",[])))!={outlet_id}):
        raise RuntimeError("Physical air opening type/face ownership mismatch")
    return {"air_bbox_mm":origin+[
        origin[i]+extent[i] for i in range(3)],
        "inlet_face_id":inlet_id,"outlet_face_id":outlet_id}


def _steady_setup(app,max_iterations):
    names=list(app.setup_names)
    setup=(app.get_setup("Setup1") if "Setup1" in names
           else app.create_setup("Setup1",setup_type="IcepakSteadyState"))
    if not setup:
        raise RuntimeError("Cannot create native steady-state setup")
    # Restore the physically accepted Icepak steady-state CHT options.
    # The verified carrier used laminar forced flow, zero gravity and no
    # radiation. AEDT fresh-design defaults are not a physical contract.
    validated={
        "Flow Regime":"Laminar",
        "Include Flow":True,
        "Include Temperature":True,
        "Include Gravity":False,
        "Include Solar":False,
        "Radiation Model":"Off",
        # The old production queue uses physical-balance early stopping,
        # not Icepak's stringent default residual recommendations.  Relaxed
        # native residuals let a stable Case stop early.  Native fin/contact,
        # interface conservation and Source evidence are checked afterwards;
        # the old queue's 40-iteration temperature-stability monitor is a
        # separate criterion and must not be silently claimed as reproduced.
        # The initial AEDT solve uses a finite native step budget; verified
        # 40-step Fluent continuations have no overall iteration cap.
        "Convergence Criteria - Energy":"1e-06",
        "Convergence Criteria - Flow":"0.003",
        "Discretization Scheme - Temperature":"Second",
        "Convergence Criteria - Max Iterations":int(max_iterations),
    }
    missing=set(validated)-set(setup.props)
    # In a fresh AEDT 2023 R2 design the solar checkbox can be absent
    # entirely. With Radiation Model=Off it has no physical contribution.
    # Preserve every other established field as a mandatory native setting.
    if missing=={"Include Solar"}:
        validated.pop("Include Solar")
        missing=set()
    if missing:
        raise RuntimeError("Native Icepak setup missing original physics: "+str(sorted(missing)))
    setup.props.update(validated)
    if not setup.update():
        raise RuntimeError("Cannot configure native solver setup")
    return setup.name


def configure_aedt_runtime(case_workspace):
    """One isolated AEDT profile for both native T0 and CHT sessions."""
    profile=Path(case_workspace)/"runtime_profile"
    for part in ("AppData/Roaming","AppData/Local","Temp"):
        (profile/part).mkdir(parents=True,exist_ok=True)
    os.environ["APPDATA"]=str(profile/"AppData/Roaming")
    os.environ["LOCALAPPDATA"]=str(profile/"AppData/Local")
    os.environ["TEMP"]=str(profile/"Temp")
    os.environ["TMP"]=str(profile/"Temp")
    os.environ["PYAEDT_USE_PRE_GRPC_ARGS"]="True"
    from ansys.aedt.core.generic.settings import settings
    settings.use_grpc_api=True


def configure_native_solver_directory(app, case_workspace):
    """Use one queue-owned scratch root; AEDT temp settings are shared globally."""
    if not Path(case_workspace).is_dir():
        raise ValueError("Missing private Case workspace for native solve")
    target=(RUNS/"native_solver_tmp").resolve()
    target.mkdir(parents=True,exist_ok=True)
    try:
        app.odesktop.SetTempDirectory(str(target))
        actual=Path(str(app.odesktop.GetTempDirectory())).resolve()
    except Exception as exc:
        raise RuntimeError("Cannot set AEDT per-Case solver temp directory") from exc
    if os.path.normcase(str(actual))!=os.path.normcase(str(target)):
        raise RuntimeError(f"AEDT solver temp directory mismatch: {actual} != {target}")
    return target


def release_native_aedt(app):
    """Close AEDT and clean only stopped Fluent scratch owned by this session."""
    engine_pids=set()
    try:
        desktop=psutil.Process(int(app.odesktop.GetProcessID()))
        engine_pids={child.pid for child in desktop.children(recursive=True)
                     if child.name().casefold()=="icepakcomengine.exe"}
    except (OSError, ValueError, psutil.Error):
        pass
    try:
        app.release_desktop(close_projects=True,close_desktop=True)
    finally:
        shared=(RUNS/"native_solver_tmp").resolve()
        for pid in engine_pids:
            if psutil.pid_exists(pid):
                continue
            for folder in shared.glob(f"icepak_*_{pid}.pjt"):
                if folder.is_dir() and folder.resolve().parent==shared:
                    try:
                        shutil.rmtree(folder)
                    except OSError:
                        print("DEFERRED_NATIVE_TEMP_CLEANUP",folder,flush=True)


def _native_convergence_receipt(app,max_iterations):
    """Read only the finished Case's short Fluent transcript, if available.

    This measures actual iterations without extending the solve, exporting
    heavy native fields, or pretending the old thermal monitor is present.
    """
    result={"status":"UNAVAILABLE","maximum_iterations":int(max_iterations)}
    try:
        parent=psutil.Process(int(app.odesktop.GetProcessID()))
        engines=[child.pid for child in parent.children(recursive=True)
                 if child.name().casefold()=="icepakcomengine.exe"]
    except (OSError,ValueError,psutil.Error):
        return result
    root=(RUNS/"native_solver_tmp").resolve()
    for pid in engines:
        for folder in root.glob(f"icepak_*_{pid}.pjt"):
            if folder.resolve().parent!=root:
                continue
            for transcript in folder.glob("*.trn"):
                try:
                    with transcript.open(encoding="utf-8",errors="ignore") as stream:
                        for line in stream:
                            values=line.split()
                            if (len(values)<6 or not values[0].isdigit() or
                                    not all("e" in item.lower() for item in values[1:6])):
                                continue
                            iteration=int(values[0])
                            try:
                                flow=float(values[1])
                                energy=float(values[5])
                            except ValueError:
                                continue
                            if 0<iteration<=max_iterations and (
                                    result["status"]!="MEASURED" or
                                    iteration>result["iterations"]):
                                result={"status":"MEASURED",
                                        "iterations":iteration,
                                        "maximum_iterations":int(max_iterations),
                                        "stopped_before_cap":iteration<max_iterations,
                                        "continuity_residual":flow,
                                        "energy_residual":energy}
                except OSError:
                    continue
    return result


@contextmanager
def run_native_cht(spec,plan,workspace,cores=4,max_iterations=450,mesh_profile=None):
    """Yield an open native CHT solve for sampling, then release AEDT.

    Fluent restart files are ephemeral: closing AEDT may remove them.
    Sampling therefore runs inside this context before desktop release.
    """
    workspace=Path(workspace)
    workspace.mkdir(parents=True,exist_ok=True)
    project_path=workspace/"case.aedt"
    if project_path.exists():
        raise FileExistsError("Refusing implicit reuse of a previous CAD project")
    # Limit simultaneous native Fluent solves independently of lightweight
    # CAD/T0 workers. The OS releases this lock on unexpected process exit.
    from native_gate import NativeSolveLease
    native_slot=NativeSolveLease(spec.case_id)
    native_slot.acquire()
    app=None
    try:
        configure_aedt_runtime(workspace)
        from ansys.aedt.core import Icepak
        app=Icepak(project=str(project_path),version=AEDT_VERSION,
                   non_graphical=True,new_desktop=True,close_on_exit=False)
        if not app:
            raise RuntimeError("Native AEDT Icepak startup failed")
        configure_native_solver_directory(app,workspace)
        app.modeler.model_units="mm"
        app.problem_type="TemperatureAndFlow"
        if app.problem_type!="TemperatureAndFlow":
            raise RuntimeError("Native CHT model is not TemperatureAndFlow")
        remove_automatic_region(app)
        set_native_ambient(app)
        _materials(app,plan)
        solids=create_native_cad(app,plan)
        remove_automatic_region(app)
        # The explicit fluid domain is for heatsink CHT only; an implicit
        # Region must never enclose the package's assigned thermal walls.
        air=_fluid_domain(app,plan,solids)
        package_walls=assign_package_exterior_thermal(
            app,plan,include_heatsink=True)
        mesh=apply_physical_source_mesh(app,solids,plan=plan,mesh_profile=mesh_profile)
        setup_name=_steady_setup(app,max_iterations)
        if not app.save_project():
            raise RuntimeError("AEDT project cannot be saved")
        ok=app.analyze_setup(setup_name,cores=int(cores),tasks=int(cores),
                             use_auto_settings=False,blocking=True)
        if not ok:
            message=list(app.logger.error_messages[-6:])
            # Keep actionable native solver messages in the failure record.
            # Some Fluent failures never appear in the PyAEDT logger.
            try:
                native=app.odesktop.GetMessages("", "", 0)
                details=[str(m) for m in native if any(
                    word in str(m).lower() for word in
                    ("error", "fatal", "mesh", "failed", "invalid"))]
                message.extend(details[-12:])
            except Exception:
                pass
            raise RuntimeError("Icepak native CHT failed: "+str(message[-16:]))
        # Icepak may flush Fluent restart data asynchronously after analyze.
        deadline=time.monotonic()+180
        from native_io import find_native_pair
        while time.monotonic()<deadline:
            try:
                mesh_file,data_file=find_native_pair(workspace)
                if mesh_file.stat().st_size and data_file.stat().st_size:
                    break
            except FileNotFoundError:
                pass
            time.sleep(3)
        else:
            raise RuntimeError("Icepak completed but native restart pair missing")
        convergence=_native_convergence_receipt(app,max_iterations)
        print("NATIVE_CONVERGENCE_INITIAL",spec.case_id,
              json.dumps(convergence),flush=True)
        # The production gate continues this exact Fluent restart field for
        # five measured Source-cell temperature snapshots spanning 40 steps.
        # Residual magnitudes remain diagnostics; only the final verified field
        # can enter the ordinary interface sampler and publication path.
        from physical_convergence import verify_and_continue
        convergence=verify_and_continue(
            workspace,convergence,int(cores),
            frozenset(source.layer for source in spec.sources))
        mesh_file,data_file=find_native_pair(workspace)
        print("NATIVE_CONVERGENCE_PHYSICAL",spec.case_id,
              json.dumps(convergence),flush=True)
        yield {"status":"SOLVED","case_id":spec.case_id,
                "project":str(project_path),"setup":setup_name,
                "native_convergence":convergence,
                "source_count":len(spec.sources),
                "fin_count":spec.fin_number,
                "air":air,
                "package_walls":package_walls,
                "mesh_operation_count":len(mesh["names"]),
                "native_mesh_bytes":mesh_file.stat().st_size,
                "native_fields_bytes":data_file.stat().st_size}
    finally:
        try:
            if app is not None:
                release_native_aedt(app)
        finally:
            native_slot.release()

