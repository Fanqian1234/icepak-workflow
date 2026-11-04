"""One bounded, subprocess-isolated CHT queue; no runtime code patches.

Resource admissions are measured at launch, not a sum of double-counted
hypothetical memory peaks. Pending Cases hold no execution slot. A failed
Case is recorded once and never gets a Case-specific recovery branch.
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

from case_spec import load_cases
from publish import (cleanup_case_work,free_disk_bytes,read_state,
                     recover_terminal_workspaces,save_state)
from settings import (CASE_MASTER, GIB, SAMPLES, STATE, WORK,
                      completed_rerun_ids, has_accepted_transfer,
                      legacy_reserved_status, policy_from_environment)

CODE=Path(__file__).resolve().parent


def _queue_lock():
    """Prohibit concurrent schedulers in this new production code directory."""
    STATE.mkdir(parents=True,exist_ok=True)
    lock=STATE/"scheduler.lock"
    try:
        fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
    except FileExistsError:
        try:
            pid=int(lock.read_text().strip())
        except (OSError,ValueError):
            pid=0
        if pid and psutil.pid_exists(pid):
            raise RuntimeError(f"New CHT scheduler already running, pid {pid}")
        lock.unlink(missing_ok=True)
        fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
    os.write(fd,str(os.getpid()).encode())
    os.close(fd)
    return lock


def _recover_orphaned_workers():
    """Reclaim only private workspaces whose owning worker has died."""
    if not WORK.is_dir():
        return 0
    reclaimed=0
    for folder in WORK.iterdir():
        if not folder.is_dir() or not re.fullmatch(r"case_\d+",folder.name):
            continue
        record=read_state(folder.name)
        if record.get("status") in {"FAILED","PUBLISHED"}:
            continue
        marker=folder/"OWNER.pid"
        if not marker.is_file() and not record:
            continue
        try:
            owner=int(marker.read_text(encoding="ascii").strip())
        except (OSError,ValueError):
            owner=record.get("owner_pid") or 0
        if isinstance(owner,int) and owner>0 and psutil.pid_exists(owner):
            continue
        save_state(folder.name,"FAILED",reason="Worker process ended before publication")
        try:
            reclaimed+=bool(cleanup_case_work(folder.name,failed=True))
        except OSError as error:
            # Windows may keep an AEDT log handle after its worker has died.
            # One locked file must never prevent admission of other Cases.
            print("CLEANUP_DEFERRED",folder.name,str(error),flush=True)
    return reclaimed


def _readiness(policy,active_count=0):
    # Never double-charge running tasks' observed RAM. It is already excluded
    # from psutil.available. Disk admission also budgets for future growth
    # across all live workspaces before launching the next worker.
    memory=psutil.virtual_memory().available/GIB
    disk=free_disk_bytes()/GIB
    future_disk=policy.projected_workspace_gib*(active_count+1)
    ok=(memory>=policy.minimum_free_memory_gib and
        disk>=policy.minimum_free_disk_gib+future_disk)
    return ok,{"free_memory_gib":round(memory,1),
               "free_disk_gib":round(disk,1),
               "future_disk_budget_gib":round(future_disk,1)}


def _stop_owned_tree(pid):
    """Timeout applies only to the worker and subprocesses it launched."""
    try:
        parent=psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    tree=parent.children(recursive=True)
    for proc in tree+[parent]:
        try: proc.terminate()
        except psutil.NoSuchProcess: pass
    _,alive=psutil.wait_procs(tree+[parent],timeout=10)
    for proc in alive:
        try: proc.kill()
        except psutil.NoSuchProcess: pass
    _,still_alive=psutil.wait_procs(alive,timeout=5)
    return not still_alive


def _admission_list(ids=None):
    cases,invalid=load_cases()
    # The formal master owns Case identity; the feature table owns physics.
    # Verify this small CSV contract once per scheduler admission.
    with CASE_MASTER.open(encoding="utf-8-sig",newline="") as source:
        official=[row["case_id"] for row in csv.DictReader(source)]
    feature_ids=set(cases)|set(invalid)
    if len(official)!=len(set(official)) or set(official)!=feature_ids:
        raise RuntimeError("CASE_MASTER and CASE_FEATURES differ in Case IDs")
    completed=completed_rerun_ids()
    requested=list(ids) if ids is not None else sorted(official)
    selected=[]
    seen=set()
    for cid in requested:
        if cid in seen:
            continue
        seen.add(cid)
        if cid in completed or has_accepted_transfer(cid):
            continue
        if legacy_reserved_status(cid):
            continue
        if cid in invalid:
            if ids is None:
                continue  # Incomplete legacy geometry never blocks other Cases.
            raise ValueError(cid+": "+invalid[cid])
        if cid not in cases:
            raise KeyError("Unknown Case ID "+cid)
        if not cases[cid].eligible:
            continue
        state=read_state(cid)
        status=state.get("status")
        if status=="PUBLISHED":
            continue
        if (WORK/cid).exists():
            # A dead AEDT engine can leave a locked file in terminal work.
            # Retry cleanup on the next queue pass; never launch into it.
            print("CASE_SKIP_WORKSPACE_CLEANUP_PENDING",cid,flush=True)
            continue
        # FAILED is not a completed rerun. A new queue invocation must
        # reconsider it after common code fixes; each selected ID is still
        # visited at most once during this queue invocation.
        if status in {"MODELED","SOLVED","SAMPLED","FOUR_T0_READY"}:
            owner=state.get("owner_pid")
            if isinstance(owner,int) and psutil.pid_exists(owner):
                continue
        selected.append(cid)
    return selected,len(invalid)


def run_queue(ids=None):
    policy=policy_from_environment()
    lock=_queue_lock()
    active={}
    launched=0
    passed=0
    failed=0
    pending=[]
    last_wait_note=0
    last_cleanup_attempt=0.0
    last_launch=0.0
    max_tasks=min(policy.max_parallel,
                  max(1,(psutil.cpu_count(logical=True) or os.cpu_count() or 4)
                      //policy.cores_per_case))
    try:
        _recover_orphaned_workers()
        recover_terminal_workspaces()
        selected,invalid_count=_admission_list(ids)
        if not selected:
            return {"passed":0,"failed":0,"pending":0,
                    "invalid_input_cases":invalid_count}
        pending=list(selected)
        print(f"QUEUE_START pending={len(pending)} slots={max_tasks} "
              f"per_case_cores={policy.cores_per_case}",flush=True)
        while pending or active:
            completed_any=False
            for cid,(process,handle,start) in tuple(active.items()):
                code=process.poll()
                if code is None and time.monotonic()-start > (
                        policy.native_timeout_minutes*60):
                    print(f"CASE_TIMEOUT {cid}",flush=True)
                    if not _stop_owned_tree(process.pid):
                        raise RuntimeError("Timed-out worker tree is still alive; "
                                           "refusing unsafe cleanup")
                    save_state(cid,"FAILED",reason="Native worker timeout")
                    cleanup_case_work(cid,failed=True)
                    code=124
                if code is None:
                    continue
                handle.close()
                active.pop(cid)
                completed_any=True
                if code==0 and read_state(cid).get("status")=="PUBLISHED":
                    passed+=1
                    (STATE/f"{cid}.log").unlink(missing_ok=True)
                    print(f"CASE_PASS {cid} total_passed={passed}",flush=True)
                else:
                    failed+=1
                    print(f"CASE_FAIL {cid} exit={code} total_failed={failed}",
                          flush=True)
            if completed_any:
                # Reclaim native projects only AFTER the worker has exited.
                # A locked file is deferred without terminating the queue.
                try:
                    cleaned=recover_terminal_workspaces(active_ids=active)
                    if cleaned:
                        print("CLEANUP_RELEASED",cleaned,flush=True)
                except OSError as error:
                    print("CLEANUP_DEFERRED",str(error),flush=True)
            while pending and len(active)<max_tasks:
                # Four starters are immediate; remaining workers ramp up to
                # prevent 20 simultaneous AEDT launches and memory spikes.
                if len(active)>=4 and (
                        time.monotonic()-last_launch <
                        policy.launch_spacing_seconds):
                    break
                ready,resources=_readiness(policy,len(active))
                if not ready:
                    if time.monotonic()-last_cleanup_attempt>=30:
                        try:
                            recover_terminal_workspaces(active_ids=active)
                        except OSError as error:
                            print("CLEANUP_DEFERRED",str(error),flush=True)
                        last_cleanup_attempt=time.monotonic()
                    ready,resources=_readiness(policy,len(active))
                    if not ready:
                        if time.monotonic()-last_wait_note>=30:
                            print("WAIT_RESOURCES",resources,flush=True)
                            last_wait_note=time.monotonic()
                        break
                cid=pending.pop(0)
                # Recheck immediately before launch: the old manager may have
                # claimed this Case after our initial queue admission.
                legacy=legacy_reserved_status(cid)
                if legacy:
                    print("CASE_SKIP_LEGACY_RESERVED",cid,legacy,flush=True)
                    continue
                log=(STATE/f"{cid}.log").open("w",encoding="utf-8")
                cmd=[sys.executable,"-B",str(CODE/"main.py"),
                     "case",cid,"--worker",
                     "--cores",str(policy.cores_per_case)]
                try:
                    proc=subprocess.Popen(cmd,cwd=str(CODE),
                                          stdout=log,stderr=subprocess.STDOUT,
                                          close_fds=True)
                except Exception:
                    log.close()
                    raise
                active[cid]=(proc,log,time.monotonic())
                last_launch=time.monotonic()
                launched+=1
                print("CASE_LAUNCH",cid,"pid",proc.pid,"running",len(active),
                      flush=True)
            # Pending work holds no parallel slot. Keep the scheduler alive
            # when temporarily blocked on disk/RAM, and retry automatically.
            # The existing WAIT_RESOURCES line is throttled to one per 30 s.
            if pending or active:
                time.sleep(policy.poll_seconds)
        return {"passed":passed,"failed":failed,"pending":len(pending),
                "launched":launched,"invalid_input_cases":invalid_count}
    finally:
        # A normal exception must not silently orphan a controller that owns
        # live workers. Leave their Python children running to finish; their
        # PID and Case state remain explicit rather than killing external AEDT.
        for process,handle,_ in active.values():
            handle.close()
        lock.unlink(missing_ok=True)
