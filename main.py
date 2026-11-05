"""ONLY entry point of the independent NeuralCHT Icepak production system.

Examples (run from this code directory):
    python -B main.py plan case_1254
    python -B main.py case case_1254 --cores 4
    python -B main.py queue --ids case_1254,case_1256
    python -B main.py queue
    python -B main.py status
No command references or modifies any legacy simulation code.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from collections import Counter

from pathlib import Path as _ReleasePath
_RELEASE_ROOT=_ReleasePath(__file__).resolve().parent
_IMPORT_SIGNATURE=tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size)
    for p in sorted(_RELEASE_ROOT.iterdir()) if p.is_file()
    and p.suffix in (".py",".c") and not p.name.startswith("test_"))

from acceptance import accept_sample, preflight_case
from case_spec import case_by_id, load_cases
from geometry import plan_case_geometry
from icepak import run_native_cht
from native_fin_gate import verify_native_fins
from native_io import find_native_pair
from sampling import sample_native_case
from t0 import provide_four_t0
from publish import (native_cht_admission, cleanup_case_work, publish_samples,
                     read_state, recover_terminal_workspaces, save_state)
from settings import (PUBLISHED,SAMPLES,STATE,WORK,completed_rerun_ids,
                      ensure_private_directories,has_accepted_transfer,
                      paired_interface_published,legacy_reserved_status,workdir_for)


from settings import (CODE_REVISION,MESH_REVISION,QUEUE_FILE,PAUSE_FILE,
                      production_signature,assert_release_unchanged)
from physical_convergence import verify_and_continue as _frozen_continuation
import native_source_temperature_monitor as _frozen_monitor
import psutil
import time

if _IMPORT_SIGNATURE != tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size)
    for p in sorted(_RELEASE_ROOT.iterdir()) if p.is_file()
    and p.suffix in (".py",".c") and not p.name.startswith("test_")):
    raise RuntimeError("CODE_CHANGED_DURING_IMPORT: do not start a mixed release")

def _private_samples(case_id):
    folder=(SAMPLES/case_id).resolve()
    if folder.parent!=SAMPLES.resolve() or folder.name!=case_id:
        raise ValueError("Refusing non-Case temporary cleanup target")
    return folder


def execute_case(case_id,cores):
    """One lifecycle, terminal success or terminal failure; no recovery modes."""
    ensure_private_directories()
    if (case_id in completed_rerun_ids() or has_accepted_transfer(case_id) or
            paired_interface_published(case_id)):
        print("CASE_SKIPPED_ALREADY_RERUN",case_id,flush=True)
        return 0
    legacy=legacy_reserved_status(case_id)
    if legacy:
        print("CASE_SKIPPED_LEGACY_RESERVED",case_id,legacy,flush=True)
        return 0
    signature=production_signature()
    try:
        spec=case_by_id(case_id)
        if not spec.eligible:raise ValueError("Excluded physical Case")
        plan=plan_case_geometry(spec)
        preflight=preflight_case(spec,plan)
    except (ValueError,KeyError,RuntimeError) as error:
        save_state(case_id,"INPUT_INVALID",reason=str(error),code_revision=CODE_REVISION)
        return 1
    workspace=workdir_for(case_id)
    stage=_private_samples(case_id)
    published=False
    owns_workspace=False
    owns_stage=False
    try:
        # Exclusive mkdir is the per-Case ownership gate. A second worker
        # must never change the first worker's state or delete its files.
        workspace.mkdir(exist_ok=False)
        owns_workspace=True
        (workspace/"OWNER.pid").write_text(str(os.getpid()),encoding="ascii")
        stage.mkdir(exist_ok=False)
        owns_stage=True
        save_state(case_id,"MODELED",owner_pid=os.getpid(),
                   owner_birth=psutil.Process().create_time(),preflight=preflight,
                   code_revision=CODE_REVISION,mesh_revision=MESH_REVISION,
                   stage_start_epoch=time.time(),cleanup_pending=False)
        # Package-only T0 is independent of the expensive airflow solve.
        # Produce or reuse its four validated fields first, so a T0 failure
        # never discards a successfully completed multi-hour CHT run.
        t0=provide_four_t0(spec,plan,workspace,stage,cores=cores)
        assert_release_unchanged(signature)
        save_state(case_id,"FOUR_T0_READY",t0=t0,stage_start_epoch=time.time())
        # Fluent restart fields are available only while AEDT owns this
        # project. Read and sample them BEFORE the desktop is released.
        with run_native_cht(spec,plan,workspace,cores=cores,
                            max_iterations=410) as solved:
            # The native CHT context has already continued and verified its
            # final Fluent field. Never repeat the 40-step native solve here.
            verified=solved["native_convergence"]
            if (verified.get("status")!="MEASURED" or
                    verified.get("field_origin")!="SAME_FROZEN_NATIVE_CHT_CONTINUED" or
                    (verified.get("source_temperature_stability") or {}).get("status")!="MEASURED"):
                raise RuntimeError("Native physical continuation has not passed")
            # CAD fin counts are insufficient: prove actual Fluent cell material
            # for all fins and inter-fin air passages BEFORE sampling/publish.
            native_mesh,_=find_native_pair(workspace)
            from pathlib import Path
            if native_mesh.parent.resolve()!=Path(verified["native_verified_pair"]).resolve():
                raise RuntimeError("Sampling selected a different native Fluent solve")
            assert_release_unchanged(signature)
            fin_gate=verify_native_fins(native_mesh,plan)
            save_state(case_id,"SOLVED",
                       native_convergence=verified,
                       native_mesh_bytes=solved["native_mesh_bytes"],
                       native_fields_bytes=solved["native_fields_bytes"],
                       native_fin_gate=fin_gate)
            sampled=sample_native_case(
                spec,plan,workspace,stage,fin_gate=fin_gate,
                native_convergence=verified)
            from dataclasses import asdict
            sampled["physical_input"]=json.loads(json.dumps(asdict(spec)))
            sampled["binding_contract"]="FROZEN_PHYSICAL_INPUT_V1"
            sampled["code_revision"]=CODE_REVISION
            (stage/"validation.json").write_text(
                json.dumps(sampled,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            acceptance=accept_sample(spec,sampled)
            (stage/"CHT_ADMISSION.json").write_text(
                json.dumps(native_cht_admission(sampled),ensure_ascii=False,indent=2)+"\n",
                encoding="utf-8")
            save_state(case_id,"SAMPLED",acceptance=acceptance)
        assert_release_unchanged(signature)
        output=publish_samples(case_id,stage)
        published=True
        # Parent manager reclaims only after this worker has exited. Native
        # cleanup cannot reject its own still-alive owner and wedge the queue.
        print(json.dumps({"case_id":case_id,"status":"PUBLISHED",
                          "output":str(output)},ensure_ascii=False),flush=True)
        return 0
    except Exception as error:
        if owns_workspace and not published:
            diagnostic=getattr(error,"diagnostic",None)
            save_state(case_id,"FAILED",reason=str(error),
                       failure_code=getattr(error,"code",type(error).__name__),
                       error_type=type(error).__name__,diagnostic=diagnostic,
                       code_revision=CODE_REVISION,mesh_revision=MESH_REVISION,
                       cleanup_pending=True)
        # Preserve unaccepted data for concentrated repair; never remove a
        # native field or stage merely because its producer reported failure.
        raise


def explain_plan(case_id):
    spec=case_by_id(case_id)
    plan=plan_case_geometry(spec)
    physical=preflight_case(spec,plan)
    return {"case_id":case_id,
            "eligible":spec.eligible,
            "tim2_size_mm":spec.tim2_size_mm,
            "sink_material":spec.sink_material,
            "source_count":len(spec.sources),
            "source_total_power_W":spec.total_power_W,
            "fin_count":spec.fin_number,
            "solid_count":len(plan.solids),
            "gamma_bbox_mm":plan.gamma_bbox_mm,
            "preflight":physical,
            "native_solver_started":False}


def queue_status():
    ensure_private_directories()
    counts=Counter()
    accepted=0
    for path in STATE.glob("case_*.json"):
        try:
            data=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            counts["BAD_STATE_RECORD"]+=1
            continue
        counts[data.get("status","UNKNOWN")]+=1
        if data.get("status")=="PUBLISHED" and paired_interface_published(path.stem):
            accepted+=1
    return {"states":dict(counts),
            "accepted_sampled_cases":accepted,
            "new_queue_published_count":accepted,
            "historical_published_awaiting_physics_recheck":max(0,counts["PUBLISHED"]-accepted),
            "new_work_dir":str(WORK)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    cmd=parser.add_subparsers(dest="action",required=True)
    plan=cmd.add_parser("plan")
    plan.add_argument("case_id")
    case=cmd.add_parser("case")
    case.add_argument("case_id")
    case.add_argument("--cores",type=int,default=4)
    case.add_argument("--worker",action="store_true",help=argparse.SUPPRESS)
    batch=cmd.add_parser("queue")
    batch.add_argument("--ids",default="",
                       help="Comma-separated physical Case IDs; blank=all eligible")
    batch.add_argument("--queue-file",default=str(QUEUE_FILE))
    add=cmd.add_parser("enqueue")
    add.add_argument("--ids",required=True)
    add.add_argument("--queue-file",default=str(QUEUE_FILE))
    cmd.add_parser("drain")
    cmd.add_parser("resume")
    cmd.add_parser("status")
    cmd.add_parser("cleanup")
    args=parser.parse_args(argv)
    if args.action=="plan":
        print(json.dumps(explain_plan(args.case_id),indent=2,
                         ensure_ascii=False))
    elif args.action=="case":
        try:
            return execute_case(args.case_id,args.cores)
        except Exception:
            traceback.print_exc()
            return 1
    elif args.action=="queue":
        from scheduler import run_queue
        requested=[x.strip() for x in args.ids.split(",") if x.strip()]
        try:
            from scheduler import append_requests
            if requested:
                append_requests(args.queue_file,requested)
            result=run_queue(queue_file=args.queue_file)
        except Exception:
            # Preserve exactly one human-readable failure explanation when a
            # scheduled manager exits; do not preserve giant solver traces.
            STATE.mkdir(parents=True,exist_ok=True)
            (STATE/"last_queue_error.txt").write_text(
                traceback.format_exc()[-8000:],encoding="utf-8")
            raise
        try:(STATE/"last_queue_error.txt").unlink(missing_ok=True)
        except OSError:pass
        print(json.dumps(result,indent=2,ensure_ascii=False))
    elif args.action=="enqueue":
        from scheduler import append_requests
        ids=append_requests(args.queue_file,[x.strip() for x in args.ids.split(",") if x.strip()])
        print(json.dumps({"queued":len(ids),"file":args.queue_file}))
    elif args.action=="drain":
        STATE.mkdir(parents=True,exist_ok=True)
        PAUSE_FILE.write_text("Drain active native jobs before code deployment.\n",encoding="utf-8")
        print("ADMISSION_PAUSED_ACTIVE_CASES_PRESERVED")
    elif args.action=="resume":
        PAUSE_FILE.unlink(missing_ok=True)
        print("ADMISSION_RESUMED")
    elif args.action=="status":
        print(json.dumps(queue_status(),indent=2,ensure_ascii=False))
    elif args.action=="cleanup":
        from scheduler import _recover_orphaned_workers
        orphaned=_recover_orphaned_workers()
        cleaned=recover_terminal_workspaces()
        print(json.dumps({"orphaned_workspaces_reclaimed":orphaned,
                          "terminal_or_native_dirs_removed":cleaned}))
    return 0


if __name__=="__main__":
    sys.exit(main())
