"""Single production configuration for the independent NeuralCHT CHT queue.

Nothing in this module references the retired simulation experiment.  The only
external inputs are the authoritative case master and the installed AEDT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The authorized local AEDT installation is on D:.
os.environ.setdefault("ANSYSEM_ROOT232", r"D:\Program Files\AnsysEM\v232\Win64")

EXPERIMENT = Path(__file__).resolve().parent
PROJECT = EXPERIMENT.parent
CASE_ROOT = PROJECT / "case"
CASE_MASTER = CASE_ROOT / "CASE_MASTER.csv"
CASE_FEATURES = CASE_ROOT / "CASE_FEATURES.csv"
RUNS = EXPERIMENT / "runs"
WORK = RUNS / "work"
STATE = RUNS / "state"
QUEUE_FILE = STATE / "REQUESTED_CASES.txt"
PAUSE_FILE = STATE / "ADMISSION_PAUSED"
CODE_REVISION = "local-reliability-20261105-r2"
MESH_REVISION = "analytic-source-contact-20261105-r2"
SAMPLES = RUNS / "samples"
PUBLISHED = RUNS / "published"
ASSETS = EXPERIMENT / "assets"
AEDT_VERSION = "2023.2"
# The heatsink is uniformly aluminum across all physical Cases. This is a
# fixed material property, not a Case variable or a per-row admission gate.
SINK_MATERIAL = "aluminum"

SOURCE_GRID = 128
GAMMA_GRID = 80
HTC_GRID = 40
T0_LEVELS = (250, 500, 1000, 2000)
AMBIENT_C = 20.0

# Exclusion is scheduling policy, not a deletion of the physical case master.
EXCLUDED_SIZES_MM = frozenset((30, 38, 39, 52))
EXCLUDED_TOTAL_POWERS_W = frozenset((161,))

GIB = 1024 ** 3


@dataclass(frozen=True)
class RuntimePolicy:
    max_parallel: int = 1
    cores_per_case: int = 4
    minimum_free_disk_gib: float = 10.0
    # Conservative future disk growth per live Case, measured workspaces
    # are already small and their bytes are included in actual free space.
    projected_workspace_gib: float = 8.0
    minimum_free_memory_gib: float = 8.0
    poll_seconds: float = 3.0
    launch_spacing_seconds: float = 10.0
    native_timeout_minutes: int = 180

    def __post_init__(self):
        if (not 1 <= self.max_parallel <= 24 or self.cores_per_case < 1
                or self.minimum_free_disk_gib < 0
                or self.projected_workspace_gib <= 0
                or self.minimum_free_memory_gib < 0
                or self.poll_seconds < 0.25 or self.launch_spacing_seconds < 0):
            raise ValueError("Invalid production resource policy")


def policy_from_environment() -> RuntimePolicy:
    """Read resource policy once at scheduler startup; no dynamic code edits."""
    return RuntimePolicy(
        max_parallel=int(os.environ.get("NEURALCHT_PARALLEL", "1")),
        cores_per_case=int(os.environ.get("NEURALCHT_CORES_PER_CASE", "4")),
        minimum_free_disk_gib=float(os.environ.get("NEURALCHT_FREE_DISK_GIB", "10")),
        projected_workspace_gib=float(os.environ.get("NEURALCHT_WORKSPACE_GIB", "8")),
        minimum_free_memory_gib=float(os.environ.get("NEURALCHT_FREE_RAM_GIB", "8")),
        launch_spacing_seconds=float(os.environ.get("NEURALCHT_LAUNCH_SPACING_SEC", "10")),
    )


def complete_new_physics_proof(proof: dict) -> bool:
    """Only a NEW interface PASS with all measured physical gates is terminal."""
    if not isinstance(proof,dict):
        return False
    return (proof.get("status")=="PASS"
            and proof.get("surface_contract")=="PAIRED_NATIVE_SOURCE_INTERFACE_V1"
            and (proof.get("native_convergence") or {}).get("status")=="MEASURED"
            and (proof.get("mass_balance") or {}).get("status")=="MEASURED"
            and (proof.get("source_temperature_stability") or {}).get("status")=="MEASURED"
            and (proof.get("package_energy") or {}).get("status")=="PASS")


def paired_interface_published(case_id: str) -> bool:
    """A complete admitted bundle, not merely a validation status string."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        return False
    from publish import bundle_complete
    return bundle_complete(PUBLISHED/case_id,case_id)



def _legacy_paired_interface(proof: dict) -> bool:
    """Preserve legacy PASS only when BOTH native Source sides are pure interface."""
    if proof.get("status") != "PASS":
        return False
    fields = proof.get("source_128")
    if not isinstance(fields, dict):
        return False
    return all(
        isinstance(fields.get(side), dict)
        and fields[side].get("method") in (
            "PAIRED_NATIVE_SOURCE_INTERFACE_AREA_V1",
            "SOURCE_SIDE_NATIVE_PROJECTED_AREA_V3")
        and fields[side].get("surface_family") == "interface"
        and float(fields[side].get("min_pixel_coverage",0)) >= 0.99
        and float(fields[side].get("native_area_relative_error",1)) <= 0.01
        and float(fields[side].get("pixel_area_relative_error",1)) <= 0.01
        for side in ("bottom", "top")
    )


def has_accepted_transfer(case_id: str) -> bool:
    """Skip a verified CHT export without reopening any native mesh/field data."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        return False
    output = PROJECT / "transfer_ready" / case_id
    ready = output / "READY.json"
    receipt = output / "sampling_receipt.json"
    validation = output / "validation.json"
    if not (ready.is_file() and receipt.is_file() and validation.is_file()):
        return False
    # A legacy PASS is not complete unless BOTH native Source layers
    # explicitly prove paired physical interface sampling.
    import json
    try:
        a = json.loads(ready.read_text(encoding="utf-8-sig"))
        b = json.loads(receipt.read_text(encoding="utf-8-sig"))
        c = json.loads(validation.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    return (a.get("case_id") == case_id and a.get("status") == "READY"
            and b.get("case_id") == case_id and b.get("status") == "PASS"
            and b.get("lineage") == "RERUN_NEW_NATIVE"
            and c.get("case_id") == case_id and _legacy_paired_interface(c))


def active_case_workers() -> tuple[tuple[int, str, str], ...]:
    """Live Python workers from both queues; never trust stale queue labels."""
    import psutil
    workers = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = list(proc.info.get("cmdline") or ())
            if "--worker" not in command:
                continue
            scripts = [str(arg).lower().replace("/", "\\") for arg in command]
            old = any(s.endswith("\\sustainable_queue.py") for s in scripts)
            new = any(s.endswith("\\20261103_queue_interface_refactor\\code\\main.py")
                      or s.endswith("\\icepak_workflow\\main.py") for s in scripts)
            if old:
                pos = command.index("--worker")
                cid = command[pos + 1] if pos + 1 < len(command) else ""
                kind = "old"
            elif new and "case" in command:
                pos = command.index("case")
                cid = command[pos + 1] if pos + 1 < len(command) else ""
                kind = "new"
            else:
                continue
            if cid.startswith("case_") and cid[5:].isdigit():
                workers.append((proc.pid, cid, kind))
        except (psutil.Error, OSError, ValueError):
            continue
    return tuple(workers)


def legacy_reserved_status(case_id: str) -> str:
    """Only a still-running old worker reserves this Case during handover."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    return ("RUNNING" if any(cid == case_id and kind == "old"
                             for _, cid, kind in active_case_workers()) else "")


LEGACY_STATE = (PROJECT / "experiments" /
                "20261023_base40_CFD_independent_validation" /
                "runs" / "targeted_sparse_refinement_20261024" /
                "scheduler" / "STATE.json")


def completed_rerun_ids() -> frozenset[str]:
    """Retain the local authority's actual fully admitted CHT+T0 identities."""
    import json
    if not LEGACY_STATE.is_file():
        raise RuntimeError("Local legacy Case authority state missing")
    state = json.loads(LEGACY_STATE.read_text(encoding="utf-8-sig"))
    ids = state.get("full_accepted")
    if (not isinstance(ids,list) or len(ids)!=len(set(ids))
            or any(not isinstance(cid,str) or not cid.startswith("case_")
                   or not cid[5:].isdigit() for cid in ids)):
        raise RuntimeError("Local completed re-runs are not reliably identified")
    return frozenset(ids)


def ensure_private_directories() -> None:
    for path in (RUNS, WORK, STATE, SAMPLES, PUBLISHED, ASSETS):
        path.mkdir(parents=True, exist_ok=True)


def workdir_for(case_id: str) -> Path:
    """A dedicated work directory, never the experiment or case root."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    return WORK / case_id


def production_signature():
    """Small local code metadata snapshot; no content hashes or native scans."""
    return tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size)
                 for p in sorted(EXPERIMENT.iterdir())
                 if p.is_file() and p.suffix in (".py",".c") and not p.name.startswith("test_"))


def assert_release_unchanged(signature):
    if production_signature()!=signature:
        raise RuntimeError("CODE_CHANGED_DURING_CASE: stop admission and deploy only after drain")
