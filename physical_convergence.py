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
import shutil

from native_io import find_native_pair
from native_source_temperature_monitor import prepare, validate_reports
import settings
# A worker launched before the 0.6-K update may hold the old settings module.
# Keep its pre-existing 0.5-K rule consistent with its loaded acceptance code.
SOURCE_TEMPERATURE_STABILITY_K = getattr(settings, "SOURCE_TEMPERATURE_STABILITY_K", 0.5)


def _native_source_zones(mesh,source_layers):
    with Path(mesh).open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as blob:
            names=[m[1].decode("ascii") for m in re.finditer(
                rb"(?m)^\(39 \(\d+ solid (\S+) 1\)\(",blob)]
    selected={}
    for layer in ("bottom","top"):
        short="bot" if layer=="bottom" else "top"
        zones=[name for name in names
               if name.lower().startswith("active_"+short+"_source")]
        if not zones:
            zones=[name for name in names
                   if name.lower().startswith("passive_feol_"+short+"_")]
        if not zones and layer not in source_layers:
            # Unpowered-layer passive FEOL can be merged into another solid.
            # Monitor the adjacent native volume, never an invented Source.
            adjacent="hb_solid" if layer=="bottom" else "die_solid"
            zones=[name for name in names if name.lower()==adjacent]
        if not zones:
            raise RuntimeError("Source-temperature monitor has no native "+layer+" volume zones")
        selected[layer]=zones
    if set(selected["bottom"]) & set(selected["top"]):
        raise RuntimeError("Source-temperature layers have overlapping native zones")
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
            "quantity":"powered Source or unpowered adjacent-solid cell temperature",
            "sample_interval_iterations":10,
            "sampled_window_iterations":40,
            "max_temperature_range_K":max(ranges.values()),
            "temperature_range_K":ranges,
            "last_window":readings,
            "monitored_zones":prepared["monitored_names"]}


def verify_and_continue(workspace,initial,cores,source_layers):
    """Continue the same native field until a measured 40-step window is stable."""
    while True:
        receipt,initial=_verify_next_window(workspace,initial,cores,source_layers)
        if receipt is not None:
            return receipt


def _verify_next_window(workspace,initial,cores,source_layers):
    """Advance exactly one native window and keep the final verified field."""
    # Icepak's temporary .trn is not always retained after analyze_setup.
    # The continuation journal will obtain the iteration counter directly
    # from the loaded same-field Fluent restart, before doing any new steps.
    measured_initial=(int(initial["iterations"])
                      if initial.get("status")=="MEASURED" else None)
    if measured_initial is not None and measured_initial<=0:
        raise RuntimeError("Measured native iteration counter must be positive")
    workspace=Path(workspace)
    mesh,data=find_native_pair(workspace)
    phase=int(initial.get("continuation_phase",0))+1
    if phase<1:
        raise RuntimeError("Native convergence continuation phase is invalid")
    tag="" if phase==1 else f"_{phase}"
    root=workspace/f"physical_convergence{tag}"
    root.mkdir(exist_ok=False)
    monitored=_native_source_zones(mesh,source_layers)
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
    # The 40-step native restart uses one compute rank to avoid
    # multi-rank MPI shared-memory allocation failures under concurrency.
    args=[str(fluent),"3ddp","-g","-t1","-i",str(journal)]
    with log.open("wb") as output:
        result=subprocess.run(args,cwd=str(root/"tmp"),
                              stdout=output,stderr=subprocess.STDOUT,
                              timeout=1200,check=False)
    body=log.read_text(encoding="utf-8",errors="replace")
    if result.returncode!=0:
        raise RuntimeError("Native thermal continuation failed: "+
                           body[-1500:])
    before=re.findall(r"(?m)^NCI_ITER_BEFORE=(\d+)\s*$",body)
    after=re.findall(r"(?m)^NCI_ITER_AFTER=(\d+)\s*$",body)
    if len(before)!=1 or len(after)!=1:
        raise RuntimeError("Fluent restart iteration count is not measured")
    iteration,final=int(before[0]),int(after[0])
    if (iteration<=0 or final-iteration!=40 or
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
    # The newly validated field supersedes the previous large native pair.
    # A failed next window still leaves its input pair intact for diagnosis.
    if phase>1:
        old_tag="" if phase==2 else f"_{phase-1}"
        old_verified=workspace/f"physical_verified{old_tag}.aedtresults"
        try:
            shutil.rmtree(old_verified)
        except OSError as error:
            print("PREVIOUS_NATIVE_PAIR_CLEANUP_DEFERRED",str(old_verified),
                  str(error),flush=True)
    print("SOURCE_TEMPERATURE_WINDOW",final,
          f"range_K={stability['max_temperature_range_K']:.6g}",
          f"limit_K={SOURCE_TEMPERATURE_STABILITY_K:.6g}",flush=True)
    if stability["max_temperature_range_K"]>SOURCE_TEMPERATURE_STABILITY_K:
        return None,{
            "status":"MEASURED","iterations":final,
            "continuation_phase":phase,
            "original_initial_iterations":initial.get("original_initial_iterations",iteration),
            "initial_diagnostics":initial.get("initial_diagnostics",initial)}
    receipt={"status":"MEASURED","iterations":final,
             "initial_iterations":initial.get("original_initial_iterations",iteration),
             "continuation_iterations":phase*40,
             "final_stability_window_iterations":40,
             "source_temperature_stability_limit_K":SOURCE_TEMPERATURE_STABILITY_K,
             "continuation_phases":phase,
             "maximum_iterations":None,"residuals_are_diagnostic_only":True,
             "initial_diagnostics":initial.get("initial_diagnostics",initial),
             "source_temperature_stability":stability,
             "native_verified_pair":str(verified),
             "field_origin":"SAME_FROZEN_NATIVE_CHT_CONTINUED"}
    (root/"convergence_receipt.json").write_text(
        json.dumps(receipt,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return receipt,None

