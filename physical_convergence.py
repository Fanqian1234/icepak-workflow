"""Mandatory, measured 40-iteration Source temperature stability check.

Continue the newly solved native Fluent field only; never initialize again.
All sampled data comes from the final, post-verification native field.
"""
from __future__ import annotations
import json
import math
import mmap
import os
from pathlib import Path
import re
import subprocess

from native_io import find_native_pair
from native_source_temperature_monitor import prepare, validate_reports


def _native_source_zones(mesh,expected_source_zones):
    """Monitor actual Source layers only; reject missing required Source zones."""
    if set(expected_source_zones)!={"bottom","top"} or not any(expected_source_zones.values()):
        raise RuntimeError("No physical Source layers")
    with Path(mesh).open("rb") as stream:
        with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as blob:
            names={m[1].decode("ascii").lower() for m in re.finditer(
                rb"(?m)^\(39 \(\d+ solid (\S+) 1\)\(",blob)}
    selected={}
    for layer in ("bottom","top"):
        expected=[str(x).lower() for x in expected_source_zones[layer]]
        if len(expected)!=len(set(expected)):
            raise RuntimeError("Duplicate native Source names")
        missing=[name for name in expected if name not in names]
        if missing:raise RuntimeError("NATIVE_SOLID_CELL_MISSING: "+repr(missing[:16]))
        selected[layer]=expected
    if set(selected["bottom"]) & set(selected["top"]):
        raise RuntimeError("Overlapping physical Source layers")
    return selected


def _parse_stability(log,prepared,initial_iter):
    count=validate_reports(log,prepared)
    chunks=re.findall(
        r"(?ms)^THERMAL_MONITOR_BEGIN\s*$(.*?)^THERMAL_MONITOR_END\s*$",log)
    if count!=5 or len(chunks)!=5:
        raise RuntimeError("Native thermal monitor must report at 0,10,20,30,40 steps")
    readings=[]
    for k,chunk in enumerate(chunks):
        row={"completed_iteration":initial_iter+k*10}
        for layer in ("bottom","top"):
            if not prepared["monitored_names"][layer]:continue
            for metric in ("volume-avg","maximum"):
                marker="THERMAL_MONITOR_"+layer+"_"+metric
                match=re.search(
                    r"(?m)^"+re.escape(marker)+r"\s*\n\s*Net\s+([\d.eE+-]+)\s*$",
                    chunk)
                if not match:
                    raise RuntimeError("Native thermal monitor measurement missing: "+marker)
                value=float(match[1])
                if not math.isfinite(value):
                    raise RuntimeError("Native Source temperature is nonfinite")
                row[layer+"_"+metric+"_K"]=value
        readings.append(row)
    fields=tuple(k for k in readings[0] if k!="completed_iteration")
    ranges={key:max(row[key] for row in readings)-min(row[key] for row in readings)
            for key in fields}
    return {"status":"MEASURED","method":"RESIDENT_CELL_REDUCTION_V1",
            "quantity":"native Source-cell volume mean and maximum",
            "sample_interval_iterations":10,
            "sampled_window_iterations":40,
            "max_temperature_range_K":max(ranges.values()),
            "temperature_range_K":ranges,
            "last_window":readings,
            "monitored_zones":prepared["monitored_names"]}


def verify_and_continue(workspace,initial,cores,expected_source_zones=None):
    """Return evidence; leave a later-mtime verified pair for normal sampling."""
    if expected_source_zones is None:
        # In-flight workers may have imported the former 3-argument Icepak
        # entry. Resolve the same strict Source identity from the authoritative
        # physical Case, not from whichever zones happen to survive meshing.
        from case_spec import case_by_id
        native_case=case_by_id(Path(workspace).name)
        expected_source_zones={
            "bottom":[s.name+"_solid" for s in native_case.sources
                      if s.layer=="bottom"],
            "top":[s.name+"_solid" for s in native_case.sources
                   if s.layer=="top"]}
    # Icepak's temporary .trn is not always retained after analyze_setup.
    # The continuation journal will obtain the iteration counter directly
    # from the loaded same-field Fluent restart, before doing any new steps.
    measured_initial=(int(initial["iterations"])
                      if initial.get("status")=="MEASURED" else None)
    if measured_initial is not None and not 0<measured_initial<=410:
        raise RuntimeError("No 40-iteration window remains within 450 total iterations")
    workspace=Path(workspace)
    mesh,data=find_native_pair(workspace)
    phase=int(initial.get("continuation_phase",0))+1
    if not 1<=phase<=12:
        raise RuntimeError("Native convergence continuation phase is invalid")
    tag="" if phase==1 else f"_{phase}"
    root=workspace/f"physical_convergence{tag}"
    root.mkdir(exist_ok=False)
    monitored=_native_source_zones(mesh,expected_source_zones)
    prepared=prepare(mesh,root,monitored)
    verified=(workspace/f"physical_verified{tag}.aedtresults"/"verified")
    verified.mkdir(parents=True,exist_ok=False)
    commands=[f'/file/read-case "{mesh.as_posix()}"',
              f'/file/read-data "{data.as_posix()}"',
              *prepared["compile_commands"],
              "/solve/monitors/residual/convergence-criteria",
              *["1e-12"]*5,
              '(format #t "NCI_ITER_BEFORE=~a~%" (client-inquire-iteration))',
              *prepared["report_commands"]]
    for _ in range(4):
        commands.extend(["/solve/iterate","10",*prepared["report_commands"]])
    commands.append('(format #t "NCI_ITER_AFTER=~a~%" (client-inquire-iteration))')
    commands.extend(["/file/cff-files?","no",
                     f'/file/write-case "{(verified/"current.cas").as_posix()}"',
                     f'/file/write-data "{(verified/"current.dat").as_posix()}"',
                     "/exit","yes"])
    journal=root/"verify.jou"
    journal.write_text("\n".join(commands)+"\n",encoding="ascii")
    fluent=Path(os.environ.get(
        "ANSYSEM_ROOT232",r"C:\Program Files\AnsysEM\v232\Win64"))/"fluent/ntbin/win64/fluent.exe"
    if not fluent.is_file():
        raise FileNotFoundError("Installed Fluent executable is missing")
    log=root/"verify.log"
    args=[str(fluent),"3ddp","-g",f"-t{int(cores)}","-i",str(journal)]
    with log.open("wb") as output:
        result=subprocess.run(args,cwd=str(root/"tmp"),
                              stdout=output,stderr=subprocess.STDOUT,
                              check=False)
    body=log.read_text(encoding="utf-8",errors="replace")
    if result.returncode!=0:
        raise RuntimeError("Native thermal continuation failed: "+
                           body[-1500:])
    before=re.findall(r"(?m)^NCI_ITER_BEFORE=(\d+)\s*$",body)
    after=re.findall(r"(?m)^NCI_ITER_AFTER=(\d+)\s*$",body)
    if len(before)!=1 or len(after)!=1:
        raise RuntimeError("Fluent restart iteration count is not measured")
    iteration,final=int(before[0]),int(after[0])
    if (not 0<iteration<=410 or final-iteration!=40 or final>450 or
            measured_initial is not None and measured_initial!=iteration):
        raise RuntimeError("Fluent did not continue the same native field for exactly 40 iterations")
    stability=_parse_stability(body,prepared,iteration)
    # Only the written, continued native field can enter normal sampling.
    raw_case=verified/"current.cas"
    raw_data=verified/"current.dat"
    if not raw_case.is_file() or not raw_data.is_file():
        raise RuntimeError("Continuation did not write the final native field")
    if min(raw_case.stat().st_size,raw_data.stat().st_size)<=0:
        raise RuntimeError("Continuation wrote empty native files")
    raw_case.rename(verified/"current.nc_cas")
    raw_data.rename(verified/"current.fdat")
    from native_io import bind_native_pair
    bind_native_pair(workspace,verified/"current.nc_cas",verified/"current.fdat",
                     stage=f"CHT_CONTINUED_{phase}")
    if stability["max_temperature_range_K"]>0.5:
        if final+40>450:
            raise RuntimeError("Native Source temperature did not stabilize by iteration 450")
        return verify_and_continue(workspace,{
            "status":"MEASURED","iterations":final,
            "continuation_phase":phase,
            "original_initial_iterations":initial.get("original_initial_iterations",iteration),
            "initial_diagnostics":initial.get("initial_diagnostics",initial)},cores,expected_source_zones)
    receipt={"status":"MEASURED","iterations":final,
             "initial_iterations":initial.get("original_initial_iterations",iteration),
             "continuation_iterations":phase*40,
             "final_stability_window_iterations":40,
             "continuation_phases":phase,
             "maximum_iterations":450,"residuals_are_diagnostic_only":True,
             "initial_diagnostics":initial.get("initial_diagnostics",initial),
             "source_temperature_stability":stability,
             "native_verified_pair":str(verified),
             "field_origin":"SAME_FROZEN_NATIVE_CHT_CONTINUED"}
    (root/"convergence_receipt.json").write_text(
        json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return receipt
