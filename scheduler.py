"""One bounded, subprocess-isolated CHT queue; no runtime code patches.

Resource admissions are measured at launch, not a sum of double-counted
hypothetical memory peaks. Pending Cases hold no execution slot. A failed
Case is recorded once and never gets a Case-specific recovery branch.
"""
from __future__ import annotations

import csv
import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import psutil

from case_spec import load_cases
from settings import locally_reserved_case_ids
from publish import (cleanup_case_work,free_disk_bytes,read_state,
                     recover_terminal_workspaces,save_state)
from settings import (CASE_MASTER, GIB, RUNS, SAMPLES, STATE, WORK,
                      completed_rerun_ids, has_accepted_transfer,
                      active_case_workers, paired_interface_published,
                      legacy_reserved_status, policy_from_environment)

CODE=Path(__file__).resolve().parent


def _foreign_workers(active):
    """Count old and orphaned new workers against the SAME total slot limit."""
    owned={process.pid for process,_,_ in active.values()}
    return {pid:case_id for pid,case_id,_ in active_case_workers()
            if pid not in owned}



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


def _reclaim_terminal_fluent_orphans():
    """Reap detached native CFD engines ONLY for terminal Cases.

    Fluent can leave cx2320, fl2320, or -wait hosts after a worker exits.
    These keep substantial Windows commit, CPU, and license handles. Restrict
    cleanup to this queue's own physical-convergence workspaces, verify Case
    state and active ownership, and leave all running/unknown Cases alone.
    """
    if not WORK.is_dir():
        return 0
    workers=active_case_workers()
    live_cases={cid for _,cid,_ in workers}
    live_tree={pid for pid,_,_ in workers}
    for pid,_,_ in workers:
        try:
            live_tree.update(child.pid for child in
                             psutil.Process(pid).children(recursive=True))
        except psutil.Error:
            pass
    root=WORK.resolve()
    now=time.time()
    candidates=[]
    for proc in psutil.process_iter(["pid","name","create_time"]):
        try:
            if (proc.info["name"] or "").casefold() not in {
                    "fluent.exe","cx2320.exe","fl2320.exe"}:
                continue
            if proc.pid in live_tree or now-proc.create_time()<180:
                continue
            relative=Path(proc.cwd()).resolve().relative_to(root)
            parts=relative.parts
            if (len(parts)<2 or not re.fullmatch(r"case_\d+",parts[0])
                    or not parts[1].startswith("physical_convergence")):
                continue
            cid=parts[0]
            if (cid in live_cases or
                    read_state(cid).get("status") not in {"FAILED","PUBLISHED"}):
                continue
            candidates.append((proc.pid,proc.create_time(),cid))
        except (psutil.Error,OSError,ValueError):
            continue
    reclaimed=0
    for pid,birth,cid in candidates:
        try:
            proc=psutil.Process(pid)
            if (abs(proc.create_time()-birth)>0.01 or
                    cid in {c for _,c,_ in active_case_workers()} or
                    read_state(cid).get("status") not in {"FAILED","PUBLISHED"}):
                continue
            tree=proc.children(recursive=True)+[proc]
            for child in tree:
                try:
                    child.terminate()
                except (psutil.NoSuchProcess,psutil.AccessDenied):
                    pass
            _,alive=psutil.wait_procs(tree,timeout=3)
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess,psutil.AccessDenied):
                    pass
            _,alive=psutil.wait_procs(alive,timeout=2)
            if not alive:
                reclaimed+=1
        except (psutil.Error,OSError):
            continue
    if reclaimed:
        print("RECLAIM_TERMINAL_NATIVE",reclaimed,flush=True)
    return reclaimed


def _windows_free_commit_gib():
    """Actual Windows remaining commit, not another RAM reservation."""
    if os.name!="nt":
        return None
    class MemoryStatus(ctypes.Structure):
        _fields_=[("length",ctypes.c_uint32),
                  ("load",ctypes.c_uint32),
                  ("total_physical",ctypes.c_uint64),
                  ("free_physical",ctypes.c_uint64),
                  ("total_commit",ctypes.c_uint64),
                  ("free_commit",ctypes.c_uint64),
                  ("total_virtual",ctypes.c_uint64),
                  ("free_virtual",ctypes.c_uint64),
                  ("extended_virtual",ctypes.c_uint64)]
    status=MemoryStatus()
    status.length=ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)):
        raise OSError("Cannot read Windows available commit")
    return status.free_commit/GIB


def _readiness(policy,active_count=0):
    # RAM is already net of live processes: do not reserve it twice.
    # Windows may run out of COMMIT while RAM still appears plentiful;
    # commit is an independent measured admission signal.
    memory=psutil.virtual_memory().available/GIB
    commit=_windows_free_commit_gib()
    disk=free_disk_bytes()/GIB
    future_disk=policy.projected_workspace_gib*(active_count+1)
    ok=(memory>=policy.minimum_free_memory_gib and
        (commit is None or commit>=8.0) and
        disk>=policy.minimum_free_disk_gib+future_disk)
    return ok,{"free_memory_gib":round(memory,1),
               "free_commit_gib":round(commit,1) if commit is not None else None,
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


def _native_fatal_marker(worker_pid):
    """Detect terminal OOM/MPI failure from this live worker's own transcript.

    A child Fluent kernel can spin on a dead MPI communicator for hours.
    Inspect only the short tail of this worker's Icepak engine transcript.
    This is runtime evidence, not a scientific convergence threshold.
    """
    root=(RUNS/"native_solver_tmp").resolve()
    try:
        worker=psutil.Process(worker_pid)
        engines=[child.pid for child in worker.children(recursive=True)
                 if child.name().casefold()=="icepakcomengine.exe"]
        for pid in engines:
            for folder in root.glob(f"icepak_*_{pid}.pjt"):
                if folder.resolve().parent!=root:
                    continue
                for transcript in folder.glob("*.trn"):
                    with transcript.open("rb") as stream:
                        stream.seek(max(0,transcript.stat().st_size-8192))
                        tail=stream.read().lower()
                    if b"out of memory" in tail:
                        return "FLUENT_OUT_OF_MEMORY"
                    if b"fatal error in pmpi" in tail:
                        return "FLUENT_MPI_FATAL"
                    if b"bad termination of one of your application processes" in tail:
                        return "FLUENT_PROCESS_ABORT"
    except (psutil.Error,OSError):
        return None
    return None


_SOLVER_CPU_PREVIOUS={}
_NATIVE_IDLE_SINCE={}


def _recent_native_cpu_activity(worker_pid):
    """Use native solver CPU deltas when AEDT buffers its solver transcript."""
    if not worker_pid:
        return False
    try:
        worker=psutil.Process(worker_pid)
        native={}
        for proc in worker.children(recursive=True):
            if proc.name().casefold() not in {
                    "cx2320.exe","fl2320.exe","icepakmeshengine.exe",
                    "icepakcomengine.exe","fluent.exe"}:
                continue
            usage=proc.cpu_times()
            native[proc.pid]=usage.user+usage.system
        before=_SOLVER_CPU_PREVIOUS.get(worker_pid)
        _SOLVER_CPU_PREVIOUS[worker_pid]=native
        if before is None:
            return bool(native)  # First observation needs a second sample.
        return any(now-before.get(pid,now)>0.25
                   for pid,now in native.items())
    except (psutil.Error,OSError):
        return False


def _stalled_without_native_progress(
        cid,worker_age_seconds,policy,worker_pid=None):
    """Reclaim only sustained native inactivity, never active Fluent work.

    Fluent may compute for hours without updating its Python log or initial
    transcript. Require stale files and 15 continuous minutes without native
    CPU progress after the initial timeout; a wall-clock-only kill is unsafe.
    """
    key=(cid,worker_pid)
    if worker_age_seconds < max(55.0,policy.native_timeout_minutes+10.0)*60:
        return False
    now=time.time()
    try:
        log=STATE/f"{cid}.log"
        if log.is_file() and now-log.stat().st_mtime < 50*60:
            _NATIVE_IDLE_SINCE.pop(key,None)
            return False
        work=WORK/cid
        if work.is_dir():
            for child in work.iterdir():
                native=(child.name.startswith("physical_convergence")
                        or child.name.startswith("physical_verified")
                        or child.name in ("case.aedtresults","case.aedt","case.pyaedt"))
                if native and now-child.stat().st_mtime < 45*60:
                    _NATIVE_IDLE_SINCE.pop(key,None)
                    return False
    except OSError:
        # A transient lock is not evidence that a solver has stalled.
        _NATIVE_IDLE_SINCE.pop(key,None)
        return False
    if _recent_native_cpu_activity(worker_pid):
        _NATIVE_IDLE_SINCE.pop(key,None)
        return False
    idle_since=_NATIVE_IDLE_SINCE.setdefault(key,time.monotonic())
    return time.monotonic()-idle_since >= 15*60


def _reclaim_stale_inherited_workers(active,policy):
    """Recover NEW workers orphaned by an earlier dispatcher crash.

    Require BOTH a long-running worker and a long period without any
    worker-log progress. Never touch the old manager's workers or a
    published interface result. All checks are independent of Case ID.
    """
    owned={process.pid for process,_,_ in active.values()}
    reclaimed=0
    for pid,cid,kind in active_case_workers():
        if kind!="new" or pid in owned or paired_interface_published(cid):
            continue
        try:
            worker=psutil.Process(pid)
            if str(CODE/"main.py").casefold() not in {
                    str(item).casefold() for item in worker.cmdline()}:
                continue
            fatal=_native_fatal_marker(pid)
            if not fatal and not _stalled_without_native_progress(
                    cid,time.time()-worker.create_time(),policy,worker.pid):
                continue
            state=read_state(cid)
            if state.get("status") in {"FAILED","PUBLISHED"}:
                continue
            if state.get("owner_pid") not in (None,pid):
                continue
            print("RECLAIM_TERMINAL_INHERITED",cid,pid,
                  fatal or "STALE_NO_PROGRESS",flush=True)
            if not _stop_owned_tree(pid):
                print("RECLAIM_STILL_ALIVE",cid,pid,flush=True)
                continue
            save_state(cid,"FAILED",
                       reason=("Native Fluent terminal "+fatal if fatal else
                               "Inherited native worker stalled beyond deadline"),
                       owner_pid=None)
            try:
                cleanup_case_work(cid,failed=True)
            except OSError as error:
                print("RECLAIM_CLEANUP_DEFERRED",cid,str(error),flush=True)
            reclaimed+=1
        except (psutil.Error,OSError,ValueError) as error:
            print("RECLAIM_CHECK_DEFERRED",cid,str(error),flush=True)
    return reclaimed


def _physical_family(spec):
    return (spec.base_thickness_mm,spec.fin_number,
            spec.fin_height_mm,spec.fin_thickness_mm)


def _risky_physical_families(cases):
    """Identify recurring scientific failures once per queue admission.

    A difficult geometry family stays eligible but uses the small background
    lane; no Case ID whitelist, changing physics gate, or permanent pause.
    """
    patterns=(
        "Native Source temperature did not stabilize",
        "Source-temperature monitor has no native",
        "NATIVE_SOURCE_INTERFACE_SIDE_",
        "Gamma area/energy projection not conservative",
        "NATIVE_FIN_MATERIAL_OCCUPANCY_FAILED",
    )
    affected=defaultdict(set)
    for item in STATE.glob("case_*.json"):
        cid=item.stem
        if cid not in cases:
            continue
        state=read_state(cid)
        if state.get("status")!="FAILED":
            continue
        reason=str(state.get("reason",""))
        kind=next((pattern for pattern in patterns
                   if pattern in reason),None)
        if kind:
            affected[(_physical_family(cases[cid]),kind)].add(cid)
    return {family for (family,kind),ids in affected.items()
            if len(ids)>=3}


def _interleave_physical_families(selected,cases):
    """Fairly alternate heat-sink geometry families, independent of Case ID.

    This prevents one difficult geometric family from monopolizing native
    solver slots and leaves explicit --ids priority order unchanged.
    """
    families={}
    for cid in selected:
        spec=cases[cid]
        # A geometry remains one family across wind speed and TIM2 changes.
        family=_physical_family(spec)
        families.setdefault(family,deque()).append(cid)
    print("PHYSICAL_QUEUE_INTERLEAVE",
          "families",len(families),"cases",len(selected),flush=True)
    scheduled=[]
    while families:
        for family in tuple(families):
            group=families[family]
            scheduled.append(group.popleft())
            if not group:
                del families[family]
    return scheduled


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
    local_exclusive=locally_reserved_case_ids()
    requested=list(ids) if ids is not None else sorted(official)
    selected=[]
    seen=set()
    for cid in requested:
        if cid in seen:
            continue
        seen.add(cid)
        if cid in local_exclusive:
            continue
        if cid in completed or has_accepted_transfer(cid) or paired_interface_published(cid):
            continue
        # Live old workers are deferred at launch, not permanently dropped:
        # they may finish with wall sampling and still require interface rerun.
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
        if status=="PUBLISHED" and paired_interface_published(cid):
            continue
        if (WORK/cid).exists() and status not in {"FAILED","PUBLISHED"}:
            # Live or incomplete work is not safe to replace. Terminal work
            # is queued for targeted cleanup at dispatch, not skipped forever.
            print("CASE_SKIP_LIVE_WORKSPACE",cid,flush=True)
            continue
        # FAILED is not a completed rerun. A new queue invocation must
        # reconsider it after common code fixes; each selected ID is still
        # visited at most once during this queue invocation.
        if status in {"MODELED","SOLVED","SAMPLED","FOUR_T0_READY"}:
            owner=state.get("owner_pid")
            if isinstance(owner,int) and psutil.pid_exists(owner):
                continue
        selected.append(cid)
    ordered=(selected if ids is not None else
             _interleave_physical_families(selected,cases))
    return ordered,len(invalid)


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
    last_stale_check=0.0
    last_orphan_reap=0.0
    last_native_fatal_check={}
    last_launch=0.0
    max_tasks=min(policy.max_parallel,
                  max(1,(psutil.cpu_count(logical=True) or os.cpu_count() or 4)
                      //policy.cores_per_case))
    try:
        # Retire orphan ownership, but do not synchronously sweep every old
        # multi-GB native workspace before starting useful new work. Each
        # worker already cleans its own terminal files at publication.
        _reclaim_stale_inherited_workers(active,policy)
        _recover_orphaned_workers()
        _reclaim_terminal_fluent_orphans()
        last_orphan_reap=time.monotonic()
        selected,invalid_count=_admission_list(ids)
        if not selected:
            return {"passed":0,"failed":0,"pending":0,
                    "invalid_input_cases":invalid_count}
        pending=list(selected)
        # Historical failures form a bounded background retry lane; never
        # starve untried Cases merely because a native retry is slow.
        retry_ids={cid for cid in pending
                   if read_state(cid).get("status")=="FAILED"}
        physical_cases,_=load_cases()
        risky_families=_risky_physical_families(physical_cases)
        risk_ids={cid for cid in pending
                  if _physical_family(physical_cases[cid]) in risky_families}
        background_ids=retry_ids|risk_ids
        retry_slots=min(3,max(1,max_tasks//5))
        print(f"QUEUE_START pending={len(pending)} slots={max_tasks} "
              f"per_case_cores={policy.cores_per_case} "
              f"retry_backlog={len(retry_ids)} "
              f"family_risk_backlog={len(risk_ids)} "
              f"background_slots={retry_slots}",
              flush=True)
        while pending or active:
            completed_any=False
            for cid,(process,handle,start) in tuple(active.items()):
                code=process.poll()
                fatal=None
                if (code is None and
                        time.monotonic()-last_native_fatal_check.get(cid,0)>=30):
                    fatal=_native_fatal_marker(process.pid)
                    last_native_fatal_check[cid]=time.monotonic()
                stalled=(code is None and not fatal and
                         _stalled_without_native_progress(
                             cid,time.monotonic()-start,policy,process.pid))
                if code is None and (fatal or stalled):
                    print("CASE_NATIVE_TERMINAL",cid,
                          fatal or "STALE_NO_PROGRESS",flush=True)
                    if not _stop_owned_tree(process.pid):
                        # Retain ownership until the complete native tree dies.
                        print("CASE_RECLAIM_DEFERRED",cid,process.pid,
                              flush=True)
                        active[cid]=(process,handle,
                                     time.monotonic()
                                     -policy.native_timeout_minutes*60+90)
                        continue
                    save_state(cid,"FAILED",owner_pid=None,
                               reason=("Native Fluent terminal "+fatal if fatal
                                       else "Native worker stalled without output or CPU progress"))
                    try:
                        cleanup_case_work(cid,failed=True)
                    except OSError as error:
                        print("TERMINAL_CLEANUP_DEFERRED",cid,str(error),
                              flush=True)
                    code=125 if fatal else 124
                if code is None:
                    continue
                handle.close()
                active.pop(cid)
                last_native_fatal_check.pop(cid,None)
                _SOLVER_CPU_PREVIOUS.pop(process.pid,None)
                _NATIVE_IDLE_SINCE.pop((cid,process.pid),None)
                completed_any=True
                if code==0 and read_state(cid).get("status")=="PUBLISHED":
                    passed+=1
                    (STATE/f"{cid}.log").unlink(missing_ok=True)
                    print(f"CASE_PASS {cid} total_passed={passed}",flush=True)
                else:
                    failed+=1
                    print(f"CASE_FAIL {cid} exit={code} total_failed={failed}",
                          flush=True)
            if completed_any and (free_disk_bytes()/GIB <
                    policy.minimum_free_disk_gib +
                    policy.projected_workspace_gib*max_tasks + 5):
                # Workers clean terminal Cases immediately. Only sweep stale
                # work when space is actually tight; never put a full historical
                # cleanup ahead of the next productive solver launch.
                try:
                    cleaned=recover_terminal_workspaces(active_ids=active)
                    if cleaned:
                        print("CLEANUP_RELEASED",cleaned,flush=True)
                except OSError as error:
                    print("CLEANUP_DEFERRED",str(error),flush=True)
            if time.monotonic()-last_stale_check>=60:
                _reclaim_stale_inherited_workers(active,policy)
                last_stale_check=time.monotonic()
            if time.monotonic()-last_orphan_reap>=180:
                _reclaim_terminal_fluent_orphans()
                last_orphan_reap=time.monotonic()
            foreign=_foreign_workers(active)
            inspected=0
            # Slot capacity covers independent legacy and orphaned NEW workers
            # during takeover, not merely children of this scheduler process.
            while (pending and len(active)+len(foreign)<max_tasks
                   and inspected<len(pending)):
                inspected+=1
                # Ramp new AEDT launches while existing jobs continue.
                if len(active)+len(foreign)>=4 and (
                        time.monotonic()-last_launch <
                        policy.launch_spacing_seconds):
                    break
                ready,resources=_readiness(policy,len(active)+len(foreign))
                if not ready:
                    # A low commit balance is not a disk-cleanup request.
                    # Repeatedly scanning locked failed workspaces cannot
                    # release it; the terminal-Fluent reaper handles that.
                    if (resources["free_disk_gib"] <
                            policy.minimum_free_disk_gib+
                            resources["future_disk_budget_gib"] and
                            time.monotonic()-last_cleanup_attempt>=30):
                        try:
                            recover_terminal_workspaces(active_ids=active)
                        except OSError as error:
                            print("CLEANUP_DEFERRED",str(error),flush=True)
                        last_cleanup_attempt=time.monotonic()
                    ready,resources=_readiness(policy,len(active)+len(foreign))
                    if not ready:
                        if time.monotonic()-last_wait_note>=30:
                            print("WAIT_RESOURCES",resources,flush=True)
                            last_wait_note=time.monotonic()
                        break
                choice=0
                if background_ids:
                    # The background lane also applies to a scheduled queue
                    # with explicit --ids; otherwise failed Cases can consume
                    # every slot before any untried Case is attempted.
                    recovery_live=sum(cid in retry_ids for cid in active)
                    recovery_live+=sum(cid in retry_ids
                                       for cid in foreign.values())
                    recovery_pending=next(
                        (i for i,item in enumerate(pending) if item in retry_ids),
                        None)
                    if (recovery_pending is not None
                            and recovery_live<retry_slots):
                        choice=recovery_pending
                    else:
                        # Prefer untried safe families, then any untried Case;
                        # only fill remaining capacity with failed retries.
                        forward=next(
                            (i for i,item in enumerate(pending)
                             if item not in background_ids),None)
                        if forward is None:
                            forward=next(
                                (i for i,item in enumerate(pending)
                                 if item not in retry_ids),None)
                        if forward is not None:
                            choice=forward
                cid=pending.pop(choice)
                # Do not duplicate workers inherited from either manager.
                if cid in foreign.values() or legacy_reserved_status(cid):
                    pending.append(cid)
                    continue
                # An inherited worker may have published while we waited.
                if paired_interface_published(cid) or has_accepted_transfer(cid):
                    print("CASE_ALREADY_INTERFACE",cid,flush=True)
                    continue
                if (WORK/cid).exists():
                    terminal=read_state(cid).get("status")
                    if terminal in {"FAILED","PUBLISHED"}:
                        try:
                            cleanup_case_work(
                                cid,active_ids=set(active)|set(foreign.values()),
                                failed=(terminal=="FAILED"))
                        except OSError as error:
                            print("TARGETED_CLEANUP_DEFERRED",cid,str(error),flush=True)
                    if (WORK/cid).exists():
                        pending.append(cid)
                        continue
                # Replays may inherit a terminal sampled staging directory.
                stage=SAMPLES/cid
                if stage.exists():
                    exact=stage.resolve()
                    if exact.parent!=SAMPLES.resolve() or exact.name!=cid:
                        raise ValueError("Unsafe temporary Case stage target")
                    if read_state(cid).get("status") in {"FAILED","PUBLISHED"}:
                        try:
                            shutil.rmtree(exact)
                        except OSError as error:
                            print("TARGETED_STAGE_CLEANUP_DEFERRED",cid,str(error),flush=True)
                    if stage.exists():
                        pending.append(cid)
                        continue
                log=(STATE/f"{cid}.log").open("w",encoding="utf-8")
                cmd=[sys.executable,"-B",str(CODE/"main.py"),
                     "case",cid,"--worker",
                     "--cores",str(policy.cores_per_case)]
                try:
                    proc=subprocess.Popen(cmd,cwd=str(STATE),
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

