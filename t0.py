"""Package-only four-h0 reference fields, calculated from fresh native CAD.

T0 is NOT reused from CHT temperature. Physically remove sink/air, retain
Source and all package layers, solve TemperatureOnly with uniform Robin h0
on the TIM2 top face, and export the established 40x40 temperature contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import psutil

from geometry import create_native_cad
from mesh import apply_physical_source_mesh
from settings import AEDT_VERSION, AMBIENT_C, T0_LEVELS, RUNS

CACHE=RUNS/"t0_cache"


def _identity(spec,plan):
    """An internal short key for tiny physical metadata, never file hashing."""
    values={"package":spec.package_bboxes_mm,
            "sources":[(s.layer,s.origin_mm,s.dimensions_mm,s.power_W)
                       for s in spec.sources],
            "gamma_contact_bbox_mm":plan.gamma_bbox_mm,
            "ambient_C":AMBIENT_C,
            "physical_boundary_model":"PACKAGE_SIDE_0_BOTTOM_HTC50_EXACT_GAMMA_V4",
            "materials":"VERIFIED_STEADY_THERMAL_V1"}
    payload=json.dumps(values,sort_keys=True,separators=(",",":"))
    return hashlib.blake2b(payload.encode(),digest_size=12).hexdigest()


def filenames():
    return [f"T0_h0_{h0}_40x40_C.tab" for h0 in T0_LEVELS]


def _points_file(path,plan):
    """Use the verified historical X-major points ON Gamma, never inward."""
    x0,y0,_,x1,y1,z=plan.gamma_bbox_mm
    if abs((x1-x0)-(y1-y0))>1e-8:
        raise ValueError("T0 Gamma source grid must be physically square")
    step=(x1-x0)/40
    with Path(path).open("w",encoding="utf-8",newline="\n") as handle:
        handle.write("Unit=mm\n")
        # Historical accepted T0 files are X-major; Gamma is Y-major.
        for i in range(40):
            x=x0+(i+.5)*step
            for j in range(40):
                y=y0+(j+.5)*step
                handle.write(f"{x:.9f} {y:.9f} {z:.9f}\n")


def _field_to_tab(raw,output,plan):
    rows=[]
    # Small 40x40 export only; never reopen large Fluent fields.
    with Path(raw).open(encoding="utf-8",errors="ignore") as stream:
        for line in stream:
            parts=line.split()
            if len(parts)<4:
                continue
            try:
                row=tuple(float(x) for x in parts[:4])
            except ValueError:
                continue
            rows.append(row)
    data=np.asarray(rows,dtype=float)
    if data.shape!=(1600,4) or not np.isfinite(data).all():
        raise RuntimeError("Native T0 40x40 field invalid")
    x0,y0,_,x1,y1,z=plan.gamma_bbox_mm
    expected=x0+(np.arange(40)+.5)*(x1-x0)/40
    # Match the formally accepted X-major T0 layout, not Gamma ordering.
    ordered=data[np.lexsort((data[:,1],data[:,0]))]
    if not (np.allclose(ordered[:,0],np.repeat(expected,40),atol=1e-6,rtol=0)
            and np.allclose(ordered[:,1],np.tile(expected,40),atol=1e-6,rtol=0)
            and np.allclose(ordered[:,2],z,atol=1e-7,rtol=0)):
        raise RuntimeError("Native T0 must be measured at the exact Gamma plane")
    # Heated Robin fields can touch ambient within solver roundoff; spatial
    # uniformity alone is not evidence of bad physics. Reject cooling below
    # ambient or a field with no measurable positive thermal response.
    minimum=float(ordered[:,3].min())
    maximum=float(ordered[:,3].max())
    if abs(maximum-4726.85)<0.2 and maximum-minimum<0.2:
        raise RuntimeError("Native T0 probe returned 5000 K invalid-field sentinel")
    if minimum<AMBIENT_C-0.05 or maximum<=AMBIENT_C+0.01:
        raise RuntimeError(
            f"Native T0 thermal response invalid ({Path(output).name}): "
            f"min={minimum:.6g} C, max={maximum:.6g} C, "
            f"ambient={AMBIENT_C:.6g} C")
    np.savetxt(output,ordered,delimiter="\t",
               fmt=["%.9f","%.9f","%.9f","%.12g"])
    return {"min_C":minimum,
            "mean_C":float(ordered[:,3].mean()),
            "max_C":maximum}


def _validate_top_robin_power(spec,plan,h0,stats):
    """A Robin top cannot reject more heat than all Source power combined."""
    x0,y0,_,x1,y1,_=plan.gamma_bbox_mm
    sampled_area_m2=(x1-x0)*(y1-y0)*1.0e-6
    mean_C=float(stats["mean_C"])
    if not np.isfinite(mean_C) or mean_C<=AMBIENT_C:
        raise RuntimeError("T0 Gamma mean is not above ambient")
    # Gamma is area-uniform, so the 40x40 mean estimates the integrated
    # Robin flux; 10% allows conservative room for native sampling error.
    implied_W=float(h0)*sampled_area_m2*(mean_C-AMBIENT_C)
    if implied_W>spec.total_power_W*1.10:
        raise RuntimeError(
            f"Inactive/misbound top HTC h0={h0}: implied Robin heat "
            f"{implied_W:.3f} W exceeds Source input "
            f"{spec.total_power_W:.3f} W")
    return implied_W


def _validate_four_h0_response(spec,plan,statistics):
    """Reject stale or newly solved h0 fields with an inactive top boundary."""
    means=[]
    for h0 in T0_LEVELS:
        row=statistics[str(h0)]
        _validate_top_robin_power(spec,plan,h0,row)
        means.append(float(row["mean_C"]))
    tolerance_C=.03
    if any(means[i+1]>means[i]+tolerance_C for i in range(len(means)-1)):
        raise RuntimeError("T0 temperature rises as top HTC increases")
    minimum_drop=max(.05,.0005*(means[0]-AMBIENT_C))
    if means[0]-means[-1]<minimum_drop:
        raise RuntimeError("T0 top Robin wall has no measurable cooling response")
    return True


def _produce_native(spec,plan,workspace,target,cores):
    from icepak import (_materials, set_native_ambient,
                        assign_package_exterior_thermal, assign_package_top_htc,
                        remove_automatic_region, configure_aedt_runtime,
                        configure_native_solver_directory, release_native_aedt)
    configure_aedt_runtime(Path(workspace).parent)
    from ansys.aedt.core import Icepak
    project=Path(workspace)/"package_t0.aedt"
    if project.exists():
        raise FileExistsError("T0 project will never reuse a previous CAD")
    app=None
    try:
        app=Icepak(project=str(project),version=AEDT_VERSION,
                   non_graphical=True,new_desktop=True,close_on_exit=False)
        configure_native_solver_directory(app,workspace)
        app.modeler.model_units="mm"
        app.problem_type="TemperatureOnly"
        # Icepak may insert a default ambient-air Region at design creation.
        # Remove it explicitly; a wrapped TIM2 surface loses its Robin wall.
        remove_automatic_region(app)
        set_native_ambient(app)
        _materials(app,plan)
        solids=create_native_cad(app,plan,package_only=True)
        remove_automatic_region(app)
        assign_package_exterior_thermal(app,plan)
        # The top is an exposed solid face, not an air-solid interface. Use
        # the same native wall API and 20 C reference as the bottom HTC=50.
        wall=assign_package_top_htc(app,plan,T0_LEVELS[0])
        apply_physical_source_mesh(app,solids,package_conduction=True)
        # One project and one mesh for all four h0 values. Changing the wall
        # boundary invalidates each previous solve, so export immediately.
        for name in list(app.setup_names):
            app.delete_setup(name)
        setup=app.create_setup(
            "BoundT0",
            props={"Include Flow":False,"Include Temperature":True,
                   "Include Gravity":False,"Include Solar":False,
                   "Radiation Model":"Off",
                   # The historical accepted T0 criterion is 5e-8;
                   # area-integrated Robin power and all four h0 responses
                   # are validated independently after the native solve.
                   "Convergence Criteria - Energy":"5e-08",
                   "Convergence Criteria - Max Iterations":450})
        if not setup:
            raise RuntimeError("Native T0 steady conduction setup failed")
        points=Path(workspace)/"contact_40x40.pts"
        _points_file(points,plan)
        metadata={}
        for h0 in T0_LEVELS:
            wall.props["Heat Transfer Coefficient"]=f"{h0}w_per_m2kel"
            if not wall.update():
                raise RuntimeError("Native T0 Robin coefficient update failed")
            if not app.save_project():
                raise RuntimeError("Native T0 project save failed")
            if not app.analyze_setup("BoundT0",cores=int(cores),tasks=int(cores),
                                     use_auto_settings=False,blocking=True):
                messages=list(app.logger.error_messages[-5:])
                try:
                    native=app.odesktop.GetMessages("", "", 0)
                    messages.extend(str(row) for row in native if any(
                        term in str(row).lower() for term in
                        ("error", "fatal", "failed", "mesh")))
                except Exception:
                    pass
                raise RuntimeError(
                    f"Native T0 h0={h0} solve failed: {messages[-10:]}")
            raw=Path(workspace)/f"temp_h0_{h0}.fld"
            if not app.post.export_field_file(
                    "Temp",solution="BoundT0 : SteadyState",
                    output_file=str(raw),
                    sample_points_file=str(points),
                    export_in_si_system=False) or not raw.is_file():
                raise RuntimeError(f"Native T0 h0={h0} field export failed")
            output=Path(target)/f"T0_h0_{h0}_40x40_C.tab"
            metadata[str(h0)]=_field_to_tab(raw,output,plan)
            _validate_top_robin_power(spec,plan,h0,metadata[str(h0)])
            raw.unlink()
        _validate_four_h0_response(spec,plan,metadata)
        return metadata
    finally:
        if app is not None:
            release_native_aedt(app)


def provide_four_t0(spec,plan,workspace,sampled,cores=4):
    """Share only complete 4-field references for identical package physics."""
    cache=CACHE/_identity(spec,plan)
    cache.mkdir(parents=True,exist_ok=True)
    ready=cache/"READY.json"
    output=Path(sampled)
    output.mkdir(parents=True,exist_ok=True)
    def complete():
        if not ready.is_file() or not all(
                (cache/name).is_file() and (cache/name).stat().st_size
                for name in filenames()):
            return False
        # A previous solver PASS is insufficient if its Robin wall was
        # inactive. Recompute in the same physical cache, without versions.
        try:
            receipt=json.loads(ready.read_text(encoding="utf-8"))
            if receipt.get("status")!="PASS":
                return False
            _validate_four_h0_response(
                spec,plan,receipt["statistics"])
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):
            return False
        return True
    if not complete():
        lock=cache/".building.lock"
        deadline=time.monotonic()+3600
        while not complete():
            try:
                fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            except FileExistsError:
                # A killed T0 worker must not leave a permanent shared-cache
                # deadlock. Probe only the one owner PID, not a giant directory.
                try:
                    owner=int(lock.read_text(encoding="ascii").strip())
                except (OSError,ValueError):
                    owner=0
                if owner>0 and not psutil.pid_exists(owner):
                    lock.unlink(missing_ok=True)
                    continue
                if owner==0 and lock.exists() and (
                        time.time()-lock.stat().st_mtime>60):
                    lock.unlink(missing_ok=True)
                    continue
                if time.monotonic()>deadline:
                    raise TimeoutError("Shared T0 owner is not publishing")
                time.sleep(3)
                continue
            else:
                os.write(fd,str(os.getpid()).encode("ascii"))
                os.close(fd)
                try:
                    if not complete():
                        scratch=Path(workspace)/"t0"
                        scratch.mkdir(parents=True,exist_ok=True)
                        meta=_produce_native(spec,plan,scratch,cache,cores)
                        ready.write_text(json.dumps({
                            "status":"PASS","source":"PACKAGE_ONLY_NATIVE_HTC",
                            "h0_values_W_m2K":T0_LEVELS,
                            "statistics":meta},indent=2)+"\n",encoding="utf-8")
                finally:
                    lock.unlink(missing_ok=True)
                break
    for name in filenames():
        # Accept either historical raw ordering, but publish the established
        # X-major T0 contract used by the training files.
        _field_to_tab(cache/name,output/name,plan)
    return {"status":"PASS","cached_package":str(cache),
            "t0_levels":list(T0_LEVELS)}
