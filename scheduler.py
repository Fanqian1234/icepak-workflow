"""The single local queue: live request refresh, bounded failure isolation and safe drain."""
from __future__ import annotations
import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
import psutil
from case_spec import load_cases
from publish import (read_state,save_state,cleanup_case_work,recover_terminal_workspaces,
                     free_disk_bytes,owner_alive,live_native_for_case)
from settings import (CASE_MASTER,STATE,WORK,SAMPLES,GIB,QUEUE_FILE,PAUSE_FILE,
                      CODE_REVISION,MESH_REVISION,completed_rerun_ids,has_accepted_transfer,
                      paired_interface_published,active_case_workers,legacy_reserved_status,
                      policy_from_environment,production_signature)
CODE=Path(__file__).resolve().parent

def _foreign_workers(active):
    """Count distinct physical Cases, never double-count Python launch wrappers."""
    seen=set(active)
    owned={entry[0].pid for entry in active.values()}
    foreign={}
    for pid,case_id,_ in active_case_workers():
        if pid in owned or case_id in seen:
            continue
        seen.add(case_id)
        foreign[pid]=case_id
    # Native children may outlive a crashed Python worker. They still
    # consume the same Case slot, but idle license-only AEDT windows do not.
    prefix=str(WORK.resolve()).lower().replace(chr(92),"/")+"/"
    for proc in psutil.process_iter(["name","cmdline"]):
        try:
            if (proc.info.get("name") or "").lower()!="ansysedt.exe":continue
            command=" ".join(proc.info.get("cmdline") or []).lower().replace(chr(92),"/")
            match=re.search(re.escape(prefix)+r"(case_\d+)(?:/|\s|$)",command)
            if not match or match[1] in seen:continue
            if any(child.name().lower() in ("fluent.exe","hdm.exe","icepakcomengine.exe")
                   or child.name().lower().startswith("fl_mpi")
                   for child in proc.children(recursive=True)):
                foreign[proc.pid]=match[1];seen.add(match[1])
        except (psutil.Error,OSError):continue
    return foreign

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

def _queue_lock():
    """An OS lock, automatically released on death; PID reuse cannot wedge it."""
    import msvcrt
    STATE.mkdir(parents=True,exist_ok=True)
    handle=(STATE/"scheduler.lock").open("a+b")
    try:
        handle.seek(0,2)
        if handle.tell()==0:handle.write(b"0\n");handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        handle.seek(0);handle.write((str(os.getpid())+"\n").encode("ascii"));handle.truncate();handle.flush()
        return handle
    except Exception:
        handle.close();raise RuntimeError("Another production manager holds scheduler.lock")


def _unlock(handle):
    import msvcrt
    try:
        handle.seek(0);handle.write(b"0\n");handle.truncate();handle.flush()
        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
    finally:handle.close()


def read_requests(path):
    raw=Path(path).read_text(encoding="utf-8-sig")
    ids=[x.strip() for x in raw.splitlines() if x.strip() and not x.lstrip().startswith("#")]
    if len(ids)!=len(set(ids)) or any(not re.fullmatch(r"case_\d+",x) for x in ids):
        raise ValueError("Invalid or duplicate physical Case IDs in persistent queue")
    return ids


def append_requests(path,ids):
    path=Path(path);previous=path.read_bytes() if path.is_file() else None
    original=read_requests(path) if previous is not None else []
    cases,invalid=load_cases()
    for cid in ids:
        if not re.fullmatch(r"case_\d+",cid) or cid not in cases or cid in invalid:
            raise ValueError("Invalid requested Case "+str(cid))
        if not cases[cid].eligible:raise ValueError("Excluded physical Case "+cid)
    merged=original+[cid for cid in dict.fromkeys(ids) if cid not in original]
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".pending")
    if tmp.exists():raise RuntimeError("Another request-list update is in progress")
    try:
        with tmp.open("x",encoding="utf-8",newline="") as stream:
            stream.write("\n".join(merged)+"\n");stream.flush();os.fsync(stream.fileno())
        if (path.read_bytes() if path.is_file() else None)!=previous:
            raise RuntimeError("Request list changed concurrently")
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)
    return merged


def _failure(cid,code,error):
    record=read_state(cid)
    if record.get("status")=="STATE_CORRUPT":return
    try:
        save_state(cid,"FAILED",failure_code=code,reason=str(error),
                   error_type=type(error).__name__,code_revision=CODE_REVISION)
    except OSError as exc:
        print("CASE_STATE_WRITE_DEFERRED",cid,str(exc),flush=True)


def _recover_orphaned_workers():
    if not WORK.is_dir():return 0
    recovered=0
    for folder in WORK.iterdir():
        cid=folder.name
        if not folder.is_dir() or not re.fullmatch(r"case_\d+",cid):continue
        record=read_state(cid)
        if record.get("status") in ("PUBLISHED","FAILED","STATE_CORRUPT"):continue
        if not record or owner_alive(record) or live_native_for_case(cid,record):continue
        _failure(cid,"WORKER_EXITED_BEFORE_PUBLICATION",RuntimeError("Worker exited; native evidence retained"))
        recovered+=1
    return recovered


def _admission_list(ids=None):
    cases,invalid=load_cases()
    with CASE_MASTER.open(encoding="utf-8-sig",newline="") as stream:
        official=[row["case_id"] for row in csv.DictReader(stream)]
    if len(official)!=len(set(official)) or set(official)!=(set(cases)|set(invalid)):
        raise RuntimeError("CASE_MASTER and CASE_FEATURES identities disagree; global admission paused")
    requested=list(dict.fromkeys(ids)) if ids is not None else sorted(official)
    old_done=completed_rerun_ids();fresh=[];repair=[]
    for cid in requested:
        if cid in old_done or paired_interface_published(cid) or has_accepted_transfer(cid):continue
        record=read_state(cid);status=record.get("status")
        if status=="STATE_CORRUPT":continue
        if cid not in cases or cid in invalid:
            why=invalid.get(cid,"Case ID is absent from the physical catalog")
            if status!="INPUT_INVALID" or record.get("reason")!=why:
                try:save_state(cid,"INPUT_INVALID",reason=why,code_revision=CODE_REVISION)
                except OSError:pass
            continue
        if not cases[cid].eligible:continue
        if owner_alive(record) or live_native_for_case(cid,record):continue
        if status=="PUBLISHED":
            print("INCOMPLETE_PUBLISHED_BUNDLE_REQUIRES_REPAIR",cid,flush=True);continue
        if status=="FAILED":
            physical=str(record.get("reason","")).startswith(("NATIVE_","Source","Native"))
            same=(record.get("mesh_revision")==MESH_REVISION if physical
                  else record.get("code_revision")==CODE_REVISION)
            if same and not record.get("retry_requested",False):continue
            repair.append(cid)
        else:fresh.append(cid)
    return fresh+repair,len(invalid)


def _progress(cid,record):
    cpu=0.;io=0;birth=None
    pid=record.get("owner_pid")
    if owner_alive(record):
        try:
            proc=psutil.Process(pid);birth=proc.create_time()
            for child in [proc,*proc.children(recursive=True)]:
                try:
                    ct=child.cpu_times();cpu+=ct.user+ct.system
                    ct=child.io_counters();io+=ct.read_bytes+ct.write_bytes
                except psutil.Error:pass
        except psutil.Error:pass
    return {"case_id":cid,"owner_pid":pid,"owner_birth":birth,"stage":record.get("status"),
            "cpu_seconds":round(cpu,3),"io_bytes":io,"state_updated":record.get("updated_epoch")}


def _write_snapshot(status,requested,active,foreign,pending,deferred,counters,resources,progress,error=None):
    body={"status":status,"updated_epoch":time.time(),"pid":os.getpid(),
          "birth":psutil.Process().create_time(),"code_revision":CODE_REVISION,
          "requested_count":len(requested),"active_cases":list(active),
          "foreign_cases":sorted(set(foreign.values())),"pending":list(pending),
          "deferred":dict(deferred),"counts":dict(counters),"resources":resources,
          "progress":progress,"error":error}
    target=STATE/"QUEUE_STATE.json";tmp=STATE/".QUEUE_STATE.pending"
    try:
        tmp.write_text(json.dumps(body,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        os.replace(tmp,target)
    except OSError as exc:print("QUEUE_STATUS_WRITE_DEFERRED",str(exc),flush=True)


def run_queue(ids=None,queue_file=None,*,_max_cycles=None):
    """One Case failure cannot terminate dispatch; pause drains without killing."""
    policy=policy_from_environment();handle=_queue_lock()
    active={};pending=[];attempted=set();deferred={};foreign={};resources={}
    counts={"launched":0,"passed":0,"failed":0,"invalid_input_cases":0}
    loaded_signature=production_signature();last_refresh=-1e30;last_status=-1e30
    requested=[];last_request_stamp=None;progress={};last_progress={};error=None;cycles=0
    max_tasks=min(policy.max_parallel,max(1,(psutil.cpu_count() or 4)//policy.cores_per_case))
    try:
        _recover_orphaned_workers()
        while True:
            cycles+=1;completed=False;now=time.monotonic()
            # Reap first, then reclaim only after the corresponding worker died.
            for cid,(proc,log,started,birth) in tuple(active.items()):
                code=proc.poll()
                if code is None:continue
                try:log.close()
                except OSError:pass
                active.pop(cid);completed=True
                if paired_interface_published(cid):
                    counts["passed"]+=1
                    try:
                        save_state(cid,"PUBLISHED",output=str(__import__('settings').PUBLISHED/cid),cleanup_pending=True)
                        cleaned=cleanup_case_work(cid,active_ids=active)
                        if cleaned:save_state(cid,"PUBLISHED",cleanup_pending=False)
                        stage=SAMPLES/cid
                        if cleaned and stage.is_dir():shutil.rmtree(stage)
                    except (OSError,ValueError,RuntimeError) as exc:
                        print("ACCEPTED_CASE_CLEANUP_DEFERRED",cid,str(exc),flush=True)
                    print("CASE_PASS",cid,"passed",counts["passed"],flush=True)
                else:
                    counts["failed"]+=1
                    if read_state(cid).get("status") not in ("FAILED","STATE_CORRUPT","INPUT_INVALID"):
                        _failure(cid,"WORKER_EXITED_WITHOUT_COMPLETE_BUNDLE",RuntimeError(f"Worker exit={code}"))
                    print("CASE_FAIL_CONTINUE",cid,"exit",code,flush=True)
            foreign=_foreign_workers(active)
            changed=production_signature()!=loaded_signature
            draining=PAUSE_FILE.exists() or changed
            if changed:error="CODE_CHANGED: drain required before adopting a different release"
            if draining:
                if not active and not foreign:
                    _write_snapshot("DRAINED",requested,active,foreign,[],deferred,counts,resources,progress,error)
                    return dict(counts,status="DRAINED",pending=0)
                pending=[]
            elif completed or now-last_refresh>=30 or not requested:
                try:
                    if queue_file is not None:
                        requested=read_requests(queue_file)
                    else:requested=list(ids) if ids is not None else read_requests(QUEUE_FILE)
                    selected,invalid=_admission_list(requested)
                    counts["invalid_input_cases"]=invalid
                    pending=[cid for cid in selected if cid not in attempted and cid not in active and cid not in foreign.values()]
                    error=None
                except (OSError,ValueError,KeyError,RuntimeError) as exc:
                    pending=[];error=f"ADMISSION_CONFIG_ERROR: {exc}"
                last_refresh=now
            while not draining and not error and pending and len(active)+len(foreign)<max_tasks:
                ready,resources=_readiness(policy,len(active)+len(foreign))
                if not ready:break
                cid=pending.pop(0)
                if paired_interface_published(cid) or has_accepted_transfer(cid):continue
                if legacy_reserved_status(cid) or live_native_for_case(cid):
                    deferred[cid]="LIVE_NATIVE_OWNER";continue
                attempted.add(cid)
                # Failed/unaccepted work is kept. Its exact reason is visible,
                # and it does not rotate endlessly through the runnable queue.
                if (WORK/cid).exists() or (SAMPLES/cid).exists():
                    deferred[cid]="REPAIR_WORKSPACE_RETAINED";continue
                log=None
                try:
                    log=(STATE/f"{cid}.log").open("w",encoding="utf-8")
                    cmd=[sys.executable,"-B",str(CODE/"main.py"),"case",cid,"--worker","--cores",str(policy.cores_per_case)]
                    options={"creationflags":subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=="nt" else {"start_new_session":True}
                    proc=subprocess.Popen(cmd,cwd=str(CODE),stdout=log,stderr=subprocess.STDOUT,close_fds=True,**options)
                    birth=psutil.Process(proc.pid).create_time()
                    active[cid]=(proc,log,time.monotonic(),birth);counts["launched"]+=1
                    print("CASE_LAUNCH",cid,"pid",proc.pid,"birth",birth,flush=True)
                except (OSError,psutil.Error,RuntimeError) as exc:
                    if log is not None:log.close()
                    _failure(cid,"WORKER_LAUNCH_FAILED",exc);deferred[cid]=str(exc)
                    counts["failed"]+=1
                    print("CASE_LAUNCH_FAILED_CONTINUE",cid,str(exc),flush=True)
            if now-last_status>=15 or completed:
                for cid in set(active)|set(foreign.values()):
                    p=_progress(cid,read_state(cid));old=progress.get(cid)
                    if old is None or any(p.get(k)!=old.get(k) for k in ("cpu_seconds","io_bytes","stage","state_updated")):
                        last_progress[cid]=time.time()
                    p['seconds_without_progress']=round(time.time()-last_progress.get(cid,time.time()),1)
                    p['stalled_suspect']=p['seconds_without_progress']>=600
                    progress[cid]=p
                status=("DRAINING" if draining else "ADMISSION_BLOCKED" if error else
                        "RUNNING" if active or foreign else "WAIT_RESOURCES" if pending else "IDLE_REPAIR_BACKLOG")
                _write_snapshot(status,requested,active,foreign,pending,deferred,counts,resources,progress,error)
                last_status=now
            if _max_cycles is not None and cycles>=_max_cycles:return dict(counts,pending=len(pending))
            time.sleep(policy.poll_seconds)
    finally:
        for proc,log,started,birth in active.values():
            try:log.close()
            except OSError:pass
        _unlock(handle)
