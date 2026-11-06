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
)


def _case_id(case_id: str) -> str:
    workdir_for(case_id)
    return case_id


def save_state(case_id: str, status: str, **fields) -> None:
    _case_id(case_id)
    STATE.mkdir(parents=True, exist_ok=True)
    target = STATE / f"{case_id}.json"
    temporary = STATE / f".{case_id}.{uuid.uuid4().hex}.tmp"
    payload = {"case_id":case_id, "status":status, **fields}
    # Keep the worker owner visible across stage transitions so a restarted
    # scheduler can recognize an independently progressing native solve.
    if "owner_pid" not in payload and target.is_file():
        try:
            previous=json.loads(target.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            previous={}
        if isinstance(previous.get("owner_pid"),int):
            payload["owner_pid"]=previous["owner_pid"]
    temporary.write_text(
        json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    os.replace(temporary,target)


def read_state(case_id: str) -> dict:
    path = STATE / (_case_id(case_id)+".json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def validate_bundle(folder: Path) -> tuple[str,...]:
    """Check just the small sampled outputs, never the giant CFD files."""
    missing=[]
    for name in SAMPLE_FILES:
        file=folder/name
        if not file.is_file() or file.stat().st_size==0:
            missing.append(name)
    if missing:
        raise ValueError("Incomplete sampled result: "+", ".join(missing))
    result=json.loads((folder/"validation.json").read_text(encoding="utf-8"))
    if result.get("status")!="PASS":
        raise ValueError("Sample validation has not passed")
    if not complete_new_physics_proof(result):
        raise ValueError("New CHT publish requires paired interface and measured physical convergence")
    if result.get("native_fin_gate",{}).get("status")!="PASS_NATIVE_FIN_MATERIAL_RAYS":
        raise ValueError("Cannot publish CHT without actual Fluent fin/air occupancy")
    if result.get("native_solid_cells",{}).get("status")!="PASS_NATIVE_SOLID_CELLS":
        raise ValueError("Cannot publish CHT without package and Source volume cells")
    if result.get("sink_contact",{}).get("status")!="PASS":
        raise ValueError("Cannot publish CHT without native sink/air contact")
    return SAMPLE_FILES


def publish_samples(case_id: str, source: Path) -> Path:
    """Commit an already accepted bundle to the new queue's private output.

    Production export to another machine is intentionally a separate step.
    No old Case directory or legacy queue state is modified here.
    """
    _case_id(case_id)
    source=Path(source).resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    files=validate_bundle(source)
    PUBLISHED.mkdir(parents=True,exist_ok=True)
    destination=PUBLISHED/case_id
    stage=PUBLISHED/f".{case_id}.{uuid.uuid4().hex}.incoming"
    stage.mkdir()
    try:
        for name in files:
            shutil.copy2(source/name,stage/name)
        # Same area-overlap reduction as the server, applied only to accepted
        # 128x128 paired-interface fields. This is not a new Fluent solve or
        # an additional physical acceptance gate; keep native samples intact.
        from temperature40_backfill import TEMPERATURE_FIELDS, convert_temperature_file, _write_atomic
        for input_name,output_name,_ in TEMPERATURE_FIELDS:
            if input_name.startswith("T_source_"):
                _write_atomic(stage/output_name,
                              convert_temperature_file(stage/input_name))
        marker={"case_id":case_id,"status":"READY",
                "files":list(files),"sample_source":"independent_native_CHT"}
        (stage/"READY.json").write_text(
            json.dumps(marker,indent=2)+"\n",encoding="utf-8")
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
        save_state(case_id,"PUBLISHED",output=str(destination))
        return destination
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _private_workdir(case_id: str) -> Path:
    """Validate an exact path inside the private WORK root before deletion."""
    path=workdir_for(_case_id(case_id)).resolve()
    root=WORK.resolve()
    if path.parent!=root or path==root or path.name!=case_id:
        raise ValueError("Unsafe disk-cleanup target")
    return path


def cleanup_case_work(case_id: str, active_ids=(), *, failed=False) -> bool:
    """Immediately release native CAD/mesh/field for terminal Cases only."""
    if case_id in set(active_ids):
        return False
    state=read_state(case_id)
    terminal = (state.get("status")=="FAILED" if failed else
                state.get("status")=="PUBLISHED" and
                (PUBLISHED/case_id/"READY.json").is_file())
    if not terminal:
        return False
    work=_private_workdir(case_id)
    if not work.is_dir():
        return False
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
    """Restore interrupted directory commits and reclaim terminal native work."""
    active=set(active_ids)
    count=0
    if PUBLISHED.is_dir():
        # Only transient directories from this module are recoverable targets.
        # Never remove a committed Case or a directory owned by a live worker.
        for child in PUBLISHED.iterdir():
            match=re.fullmatch(
                r"\.(case_\d+)\.[0-9a-f]{32}\.(previous|incoming)",
                child.name)
            if not match or not child.is_dir():
                continue
            cid,kind=match.groups()
            if cid in active:
                continue
            owner=read_state(cid).get("owner_pid")
            if isinstance(owner,int) and owner>0 and psutil.pid_exists(owner):
                continue
            destination=PUBLISHED/cid
            committed=(destination/"READY.json").is_file()
            if kind=="previous":
                if not destination.exists() and (child/"READY.json").is_file():
                    os.replace(child,destination)
                    save_state(cid,"PUBLISHED",output=str(destination))
                    count+=1
                elif committed:
                    shutil.rmtree(child)
                    count+=1
            elif committed or not (child/"READY.json").is_file():
                shutil.rmtree(child)
                count+=1
            elif not destination.exists():
                os.replace(child,destination)
                save_state(cid,"PUBLISHED",output=str(destination))
                count+=1
    count+=cleanup_stopped_native_temp()
    if not WORK.is_dir():
        return count
    for child in WORK.iterdir():
        if not child.is_dir() or not child.name.startswith("case_"):
            continue
        try:
            _case_id(child.name)
        except ValueError:
            continue
        if child.name in active:
            continue
        status=read_state(child.name).get("status")
        if status in {"FAILED","PUBLISHED"}:
            try:
                count+=bool(cleanup_case_work(child.name,active_ids=active,
                                              failed=(status=="FAILED")))
            except OSError as error:
                print("CLEANUP_DEFERRED",child.name,str(error),flush=True)
    # Finished or failed workers may leave a small sampled staging folder
    # behind if Windows kept a file handle open. Reclaim only private stages.
    if SAMPLES.is_dir():
        for child in SAMPLES.iterdir():
            if not child.is_dir() or child.name in active:
                continue
            try:
                _case_id(child.name)
            except ValueError:
                continue
            if child.resolve().parent!=SAMPLES.resolve():
                continue
            state=read_state(child.name).get("status")
            terminal=(state=="FAILED" or state=="PUBLISHED" and
                      (PUBLISHED/child.name/"READY.json").is_file())
            if terminal:
                try:
                    shutil.rmtree(child)
                    count+=1
                except OSError as error:
                    print("STAGE_CLEANUP_DEFERRED",child.name,str(error),flush=True)
    return count


def free_disk_bytes() -> int:
    WORK.mkdir(parents=True,exist_ok=True)
    return shutil.disk_usage(WORK).free

