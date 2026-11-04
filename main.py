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

from acceptance import accept_sample, preflight_case
from case_spec import case_by_id, load_cases
from geometry import plan_case_geometry
from icepak import run_native_cht
from native_fin_gate import verify_native_fins
from native_io import find_native_pair
from sampling import sample_native_case
from t0 import provide_four_t0
from publish import (cleanup_case_work, publish_samples,
                     read_state, recover_terminal_workspaces, save_state)
from settings import (PUBLISHED,SAMPLES,STATE,WORK,completed_rerun_ids,
                      ensure_private_directories,has_accepted_transfer,
                      legacy_reserved_status,workdir_for)


def _private_samples(case_id):
    folder=(SAMPLES/case_id).resolve()
    if folder.parent!=SAMPLES.resolve() or folder.name!=case_id:
        raise ValueError("Refusing non-Case temporary cleanup target")
    return folder


def execute_case(case_id,cores):
    """One lifecycle, terminal success or terminal failure; no recovery modes."""
    ensure_private_directories()
    if (case_id in completed_rerun_ids() or has_accepted_transfer(case_id) or
            read_state(case_id).get("status")=="PUBLISHED"):
        print("CASE_SKIPPED_ALREADY_RERUN",case_id,flush=True)
        return 0
    legacy=legacy_reserved_status(case_id)
    if legacy:
        print("CASE_SKIPPED_LEGACY_RESERVED",case_id,legacy,flush=True)
        return 0
    spec=case_by_id(case_id)
    if not spec.eligible:
        raise ValueError("Excluded physical Case is not in the production queue")
    plan=plan_case_geometry(spec)
    preflight=preflight_case(spec,plan)
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
        save_state(case_id,"MODELED",owner_pid=os.getpid(),preflight=preflight)
        # Package-only T0 is independent of the expensive airflow solve.
        # Produce or reuse its four validated fields first, so a T0 failure
        # never discards a successfully completed multi-hour CHT run.
        t0=provide_four_t0(spec,plan,workspace,stage,cores=cores)
        save_state(case_id,"FOUR_T0_READY",t0=t0)
        # Fluent restart fields are available only while AEDT owns this
        # project. Read and sample them BEFORE the desktop is released.
        with run_native_cht(spec,plan,workspace,cores=cores) as solved:
            # CAD fin counts are insufficient: prove actual Fluent cell material
            # for all fins and inter-fin air passages BEFORE sampling/publish.
            native_mesh,_=find_native_pair(workspace)
            fin_gate=verify_native_fins(native_mesh,plan)
            save_state(case_id,"SOLVED",
                       native_convergence=solved["native_convergence"],
                       native_mesh_bytes=solved["native_mesh_bytes"],
                       native_fields_bytes=solved["native_fields_bytes"],
                       native_fin_gate=fin_gate)
            sampled=sample_native_case(
                spec,plan,workspace,stage,fin_gate=fin_gate,
                native_convergence=solved["native_convergence"])
            acceptance=accept_sample(spec,sampled)
            save_state(case_id,"SAMPLED",acceptance=acceptance)
        output=publish_samples(case_id,stage)
        published=True
        # Published samples are authoritative: a temporary cleanup error
        # must never roll a good Case back to FAILED.
        try:
            cleanup_case_work(case_id)
            shutil.rmtree(stage)
        except OSError as cleanup_error:
            print("CLEANUP_DEFERRED",case_id,str(cleanup_error),flush=True)
        print(json.dumps({"case_id":case_id,"status":"PUBLISHED",
                          "output":str(output)},ensure_ascii=False),flush=True)
        return 0
    except Exception as error:
        if owns_workspace:
            save_state(case_id,"FAILED",reason=str(error),
                       error_type=type(error).__name__)
            # A Windows logging handle may outlive the AEDT project.
            # Preserve the original physics error; the scheduler reclaims
            # the private workdir once this worker process has exited.
            try:
                cleanup_case_work(case_id,failed=True)
            except OSError as cleanup_error:
                print("CLEANUP_DEFERRED",case_id,str(cleanup_error),flush=True)
        if owns_stage and stage.is_dir() and not published:
            try:
                shutil.rmtree(stage)
            except OSError as cleanup_error:
                print("STAGE_CLEANUP_DEFERRED",case_id,str(cleanup_error),flush=True)
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
        if data.get("status")=="PUBLISHED":
            receipt=PUBLISHED/path.stem/"READY.json"
            try:
                published=json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError,ValueError):
                continue
            if (published.get("status")=="READY"
                    and published.get("case_id")==path.stem):
                accepted+=1
    return {"states":dict(counts),
            "accepted_sampled_cases":accepted,
            "new_queue_published_count":accepted,
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
        result=run_queue(requested if requested else None)
        print(json.dumps(result,indent=2,ensure_ascii=False))
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
