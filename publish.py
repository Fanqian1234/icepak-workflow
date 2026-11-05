"""Atomic sampled-output publication and bounded automatic disk reclamation.

Only explicit per-Case private workspace directories may be deleted. Source
Case records, in-flight solver workspaces and other experiment folders are
never cleanup targets. Native mesh/field/CAD are discarded after publication.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
import time
from pathlib import Path

import psutil

from settings import (PUBLISHED, RUNS, SAMPLES, STATE, WORK,
                      complete_new_physics_proof, workdir_for)

SAMPLE_FILES = (
    "T_gamma_80x80_area_C.tab",
    "q_gamma_80x80_area_W_m2.tab",
    "h_gamma_80x80_area_W_m2K.tab",
    "h_gamma_40x40_area_W_m2K.tab",
    "T_source_bottom_128x128_area_C.tab",
    "T_source_top_128x128_area_C.tab",
    "T_source_bottom_48x48_area_C.tab",
    "T_source_top_48x48_area_C.tab",
    "power_map_bottom_128x128_W_m2.tab",
    "power_map_top_128x128_W_m2.tab",
    "power_map_bottom_48x48_W_m2.tab",
    "power_map_top_48x48_W_m2.tab",
    "power_map_bottom_80x80_package_W_m2.tab",
    "power_map_top_80x80_package_W_m2.tab",
    "T0_h0_250_40x40_C.tab",
    "T0_h0_500_40x40_C.tab",
    "T0_h0_1000_40x40_C.tab",
    "T0_h0_2000_40x40_C.tab",
    "validation.json",
    "sampling_receipt.json",
    "CHT_ADMISSION.json",
)


def _case_id(case_id: str) -> str:
    workdir_for(case_id)
    return case_id


def save_state(case_id: str, status: str, **fields) -> None:
    _case_id(case_id)
    STATE.mkdir(parents=True,exist_ok=True)
    target=STATE/f"{case_id}.json"
    try:
        previous=json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        if not isinstance(previous,dict):previous={}
    except (OSError,ValueError):previous={}
    payload={} if status=="MODELED" else previous
    payload.update(case_id=case_id,status=status,updated_epoch=time.time(),**fields)
    if status=="MODELED":
        payload.setdefault("started_epoch",time.time())
    if status not in ("FAILED","INPUT_INVALID","STATE_CORRUPT"):
        payload.pop("reason",None);payload.pop("error_type",None)
    temporary=target.with_name(f".{case_id}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x",encoding="utf-8",newline="") as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2)
            stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)



def read_state(case_id: str) -> dict:
    path=STATE/(_case_id(case_id)+".json")
    if not path.is_file():return {}
    try:
        data=json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data,dict) or data.get("case_id")!=case_id:
            raise ValueError("Case identity in state is inconsistent")
        return data
    except (OSError,ValueError,TypeError) as error:
        # Preserve the damaged file; only this Case is withheld for repair.
        return {"case_id":case_id,"status":"STATE_CORRUPT",
                "reason":str(error),"state_path":str(path)}



def native_cht_admission(proof):
    """Small formal Case receipt made solely from accepted native evidence.

    Keep a comprehensible physical identity contract, not file-content hashes.
    All referenced values must match the actual sampled validation.json.
    """
    if not complete_new_physics_proof(proof):
        raise ValueError("Native CHT field lacks measured physical gates")
    keys=("surface_contract","native_convergence","source_temperature_stability",
          "mass_balance","package_energy","native_fin_gate",
          "native_solid_cells","sink_contact","source_power_W")
    receipt={"schema_version":"NATIVE_CHT_ADMISSION_V1",
             "case_id":proof["case_id"],"status":"PASS_CHT_VERIFIED"}
    for key in keys:
        if key not in proof:
            raise ValueError("Missing native admission field "+key)
        receipt[key]=proof[key]
    if proof.get("binding_contract")=="FROZEN_PHYSICAL_INPUT_V1":
        receipt["binding_contract"]=proof["binding_contract"]
        receipt["physical_input"]=proof["physical_input"]
        receipt["code_revision"]=proof.get("code_revision")
    return receipt


def validate_bundle(folder: Path,case_id=None,*,require_ready=False,deep=True) -> tuple[str,...]:
    """The sole sampled-output completion contract used at commit and replay."""
    folder=Path(folder)
    missing=[n for n in SAMPLE_FILES if not (folder/n).is_file() or not (folder/n).stat().st_size]
    if missing:raise ValueError("Incomplete sampled result: "+", ".join(missing))
    result=json.loads((folder/"validation.json").read_text(encoding="utf-8-sig"))
    def finite_values(value):
        import math
        if isinstance(value,float) and not math.isfinite(value):
            raise ValueError("Nonfinite number in native acceptance evidence")
        if isinstance(value,dict):
            for child in value.values():finite_values(child)
        elif isinstance(value,list):
            for child in value:finite_values(child)
    finite_values(result)
    cid=case_id or result.get("case_id")
    _case_id(cid)
    if result.get("case_id")!=cid:raise ValueError("Validation Case identity mismatch")
    from case_spec import case_by_id
    from acceptance import accept_sample
    spec=case_by_id(cid)
    accept_sample(spec,result)
    if result.get("binding_contract")=="FROZEN_PHYSICAL_INPUT_V1":
        from dataclasses import asdict
        if result.get("physical_input")!=json.loads(json.dumps(asdict(spec))):
            raise ValueError("Published native field does not match this Case's current physical input")
    elif result.get("code_revision"):
        raise ValueError("New production result lacks frozen physical-input binding")
    admission=json.loads((folder/"CHT_ADMISSION.json").read_text(encoding="utf-8-sig"))
    if admission!=native_cht_admission(result):
        raise ValueError("Admission is inconsistent with actual sampled native evidence")
    sampling=json.loads((folder/"sampling_receipt.json").read_text(encoding="utf-8-sig"))
    if (sampling.get("case_id")!=cid or sampling.get("status")!="PASS"
            or sampling.get("lineage")!="RERUN_NEW_NATIVE"):
        raise ValueError("Sampling receipt identity/lineage is invalid")
    if require_ready:
        marker=json.loads((folder/"READY.json").read_text(encoding="utf-8-sig"))
        if (marker.get("case_id")!=cid or marker.get("status")!="READY"
                or marker.get("files")!=list(SAMPLE_FILES)):
            raise ValueError("READY does not commit the full sampled bundle")
    if deep:
        import numpy as np
        for name in SAMPLE_FILES:
            if not name.endswith(".tab"):continue
            match=re.search(r"_(\d+)x(\d+)_",name)
            if not match:raise ValueError("Unspecified sample shape "+name)
            nx,ny=map(int,match.groups())
            values=np.loadtxt(folder/name,dtype=float,ndmin=2)
            if values.shape!=(nx*ny,4) or not np.isfinite(values).all():
                raise ValueError("Invalid shape/nonfinite sampled field "+name)
            if (len(np.unique(values[:,0]))!=nx or len(np.unique(values[:,1]))!=ny
                    or len(np.unique(values[:,:2],axis=0))!=nx*ny):
                raise ValueError("Missing/duplicate sampling coordinates "+name)
        from t0 import _validate_four_h0_response
        from geometry import plan_case_geometry
        spec=case_by_id(cid);stats={}
        for h in (250,500,1000,2000):
            v=np.loadtxt(folder/f"T0_h0_{h}_40x40_C.tab")[:,3]
            stats[str(h)]={"mean_C":float(v.mean()),"min_C":float(v.min()),"max_C":float(v.max())}
        _validate_four_h0_response(spec,plan_case_geometry(spec),stats)
    return SAMPLE_FILES



_BUNDLE_CACHE={}


def bundle_complete(folder: Path,case_id: str) -> bool:
    """Cache only validated small files; missing files invalidate completion."""
    folder=Path(folder)
    try:
        signature=tuple((n,(folder/n).stat().st_mtime_ns,(folder/n).stat().st_size)
                        for n in (*SAMPLE_FILES,"READY.json"))
        key=(str(folder.resolve()),case_id)
        if _BUNDLE_CACHE.get(key)==signature:return True
        validate_bundle(folder,case_id,require_ready=True,deep=True)
        _BUNDLE_CACHE[key]=signature
        return True
    except (OSError,ValueError,TypeError,KeyError,RuntimeError,IndexError):
        return False


def owner_alive(record: dict) -> bool:
    """Birth time + process role prevent recycled PIDs from blocking cleanup."""
    pid=record.get("owner_pid")
    if not isinstance(pid,int) or pid<=0:return False
    try:
        proc=psutil.Process(pid)
        birth=record.get("owner_birth")
        if birth is not None and abs(proc.create_time()-float(birth))>1:return False
        args=proc.cmdline()
        return ("--worker" in args and record.get("case_id") in args
                and any(str(x).lower().replace(chr(92),"/").endswith("/icepak_workflow/main.py") for x in args))
    except psutil.NoSuchProcess:return False
    except (psutil.Error,ValueError,TypeError):return True


def live_native_for_case(case_id: str,record=None) -> bool:
    """Protect a complete native tree by exact worker identity or Case path."""
    record=record or read_state(case_id)
    if owner_alive(record):return True
    expected=str((WORK/case_id).resolve()).lower().replace(chr(92),"/")+"/"
    try:
        for proc in psutil.process_iter(["name","cmdline","create_time","pid"]):
            name=(proc.info.get("name") or "").lower()
            if name not in ("ansysedt.exe","icepakcomengine.exe","fluent.exe","hdm.exe") and not name.startswith("fl_mpi"):
                continue
            command=" ".join(proc.info.get("cmdline") or []).lower().replace(chr(92),"/")
            if expected in command:return True
            for item in record.get("native_processes",[]):
                if proc.pid==item.get("pid") and abs(proc.create_time()-float(item["birth"]))<1:
                    return True
        return False
    except (psutil.Error,ValueError,KeyError):return True


def publish_samples(case_id: str, source: Path) -> Path:
    """Commit an already accepted bundle to the new queue's private output.

    Production export to another machine is intentionally a separate step.
    No old Case directory or legacy queue state is modified here.
    """
    _case_id(case_id)
    source=Path(source).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    files=validate_bundle(source,case_id,deep=True)
    PUBLISHED.mkdir(parents=True,exist_ok=True)
    destination=PUBLISHED/case_id
    stage=PUBLISHED/f".{case_id}.{uuid.uuid4().hex}.incoming"
    stage.mkdir()
    try:
        for name in files:
            shutil.copy2(source/name,stage/name)
        marker={"case_id":case_id,"status":"READY",
                "files":list(files),"sample_source":"independent_native_CHT"}
        (stage/"READY.json").write_text(
            json.dumps(marker,indent=2)+"\n",encoding="utf-8")
        validate_bundle(stage,case_id,require_ready=True,deep=True)
        if destination.exists():
            # On Windows a populated directory cannot be replaced in place.
            # Rename complete directories only: never expose a mixture of
            # old/new sampled files or destroy the accepted old bundle.
            if not (destination/"READY.json").is_file():
                raise RuntimeError("Existing published Case has no READY marker")
            previous=PUBLISHED/f".{case_id}.{uuid.uuid4().hex}.previous"
            os.replace(destination,previous)
            try:
                os.replace(stage,destination)
            except Exception:
                os.replace(previous,destination)
                raise
            try:
                shutil.rmtree(previous)
            except OSError:
                # Keep the complete previous bundle until safe restart cleanup.
                pass
        else:
            os.replace(stage,destination)
        try:
            save_state(case_id,"PUBLISHED",output=str(destination),cleanup_pending=True)
        except OSError as error:
            print("COMMITTED_BUNDLE_STATE_WRITE_DEFERRED",case_id,str(error),flush=True)
        return destination
    finally:
        if stage.exists():
            try:shutil.rmtree(stage)
            except OSError:print("PUBLISH_STAGE_CLEANUP_DEFERRED",case_id,flush=True)


def _private_workdir(case_id: str) -> Path:
    """Validate an exact path inside the private WORK root before deletion."""
    path=workdir_for(_case_id(case_id)).resolve()
    root=WORK.resolve()
    if path.parent!=root or path==root or path.name!=case_id:
        raise ValueError("Unsafe disk-cleanup target")
    return path


def cleanup_case_work(case_id: str,active_ids=(),*,failed=False) -> bool:
    """Parent-only reclamation after a complete accepted publication."""
    if case_id in set(active_ids):return False
    record=read_state(case_id)
    # Failed/unaccepted native evidence is not destructively reclaimed merely
    # because a state label says FAILED. It remains in the repair backlog.
    if failed or record.get("status")!="PUBLISHED":return False
    if not bundle_complete(PUBLISHED/case_id,case_id):return False
    if owner_alive(record) or live_native_for_case(case_id,record):return False
    work=_private_workdir(case_id)
    if not work.is_dir():return False
    shutil.rmtree(work)
    return True



def cleanup_stopped_native_temp() -> int:
    """Reclaim abandoned AEDT scratch from this queue, never live engines.

    The native .pjt names encode their engine PID.  Anonymous AEDT scratch
    folders and other experiments are outside this cleanup contract.
    """
    root=(RUNS/"native_solver_tmp").resolve()
    if not root.is_dir():
        return 0
    removed=0
    for child in root.iterdir():
        match=re.fullmatch(r"icepak_[A-Za-z0-9_-]+_(\d+)\.pjt",
                           child.name,re.IGNORECASE)
        if not match or not child.is_dir() or child.is_symlink():
            continue
        if psutil.pid_exists(int(match.group(1))):
            continue
        if child.resolve().parent!=root:
            continue
        try:
            shutil.rmtree(child)
            removed+=1
        except OSError as error:
            print("DEFERRED_NATIVE_TEMP_CLEANUP",str(error),flush=True)
    return removed


def recover_terminal_workspaces(active_ids=()) -> int:
    active=set(active_ids);count=0
    if not PUBLISHED.is_dir():return 0
    for child in PUBLISHED.iterdir():
        match=re.fullmatch(r"\.(case_\d+)\.[0-9a-f]{32}\.(previous|incoming)",child.name)
        if not match or not child.is_dir():continue
        cid,kind=match.groups()
        if cid in active or live_native_for_case(cid):continue
        destination=PUBLISHED/cid
        try:
            if not destination.exists() and bundle_complete(child,cid):
                os.replace(child,destination)
                save_state(cid,"PUBLISHED",output=str(destination),cleanup_pending=True)
                count+=1
            elif bundle_complete(destination,cid) and bundle_complete(child,cid):
                shutil.rmtree(child);count+=1
        except OSError as error:print("COMMIT_RECOVERY_DEFERRED",cid,str(error),flush=True)
    # At most two admitted workspaces per pass. Historical failed grids are
    # not swept; their scientific diagnostics remain available for repair.
    if WORK.is_dir():
        handled=0
        for folder in WORK.iterdir():
            cid=folder.name
            if not folder.is_dir() or not re.fullmatch(r"case_\d+",cid) or cid in active:continue
            if read_state(cid).get("status")!="PUBLISHED":continue
            try:
                count+=bool(cleanup_case_work(cid,active_ids=active))
                if not live_native_for_case(cid) and bundle_complete(PUBLISHED/cid,cid):
                    stage=SAMPLES/cid
                    if stage.is_dir() and stage.resolve().parent==SAMPLES.resolve():shutil.rmtree(stage)
            except OSError as error:print("CLEANUP_DEFERRED",cid,str(error),flush=True)
            handled+=1
            if handled>=2:break
    return count



def free_disk_bytes() -> int:
    WORK.mkdir(parents=True,exist_ok=True)
    return shutil.disk_usage(WORK).free
