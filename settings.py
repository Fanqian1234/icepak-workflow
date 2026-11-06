"""Single production configuration for the independent NeuralCHT CHT queue.

Nothing in this module references the retired simulation experiment.  The only
external inputs are the authoritative case master and the installed AEDT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Site-specific AEDT installation; production physics modules remain unchanged.
os.environ.setdefault("ANSYSEM_ROOT232", r"D:\Program Files\AnsysEM\v232\Win64")

CODE_ROOT = Path(__file__).resolve().parent
PROJECT = CODE_ROOT.parent
CASE_ROOT = PROJECT / "case_new"
CASE_MASTER = CASE_ROOT / "CASE_MASTER.csv"
CASE_FEATURES = CASE_ROOT / "CASE_FEATURES.csv"
# Runtime data never lives in the production source directory.
RUNS = PROJECT / "icepak_runs"
WORK = RUNS / "work"
STATE = RUNS / "state"
SAMPLES = RUNS / "samples"
PUBLISHED = CASE_ROOT
AEDT_VERSION = "2023.2"
# The heatsink is uniformly aluminum across all physical Cases. This is a
# fixed material property, not a Case variable or a per-row admission gate.
SINK_MATERIAL = "aluminum"

SOURCE_GRID = 128
GAMMA_GRID = 80
HTC_GRID = 40
T0_LEVELS = (250, 500, 1000, 2000)
AMBIENT_C = 20.0
# Maximum permitted Source-temperature spread over the final 40 native iterations.
SOURCE_TEMPERATURE_STABILITY_K = 0.6

# Exclusion is scheduling policy, not a deletion of the physical case master.
EXCLUDED_SIZES_MM = frozenset((30, 38, 39, 52))
EXCLUDED_TOTAL_POWERS_W = frozenset((161,))

GIB = 1024 ** 3


@dataclass(frozen=True)
class RuntimePolicy:
    max_parallel: int = 3
    cores_per_case: int = 1
    minimum_free_disk_gib: float = 5.0
    # Actual free disk already includes live workspace bytes. Leave a
    # measured headroom for additional Case growth without double reserving.
    projected_workspace_gib: float = 1.5
    minimum_free_memory_gib: float = 8.0
    # Native Fluent is the peak-memory phase; two slots suit this 32-GiB PC.
    native_solver_slots: int = 2
    minimum_native_commit_gib: float = 8.0
    poll_seconds: float = 3.0
    launch_spacing_seconds: float = 5.0
    native_timeout_minutes: int = 180

    def __post_init__(self):
        if (not 1 <= self.max_parallel <= 24 or self.cores_per_case < 1
                or self.minimum_free_disk_gib < 0
                or self.projected_workspace_gib <= 0
                or self.minimum_free_memory_gib < 0
                or not 1 <= self.native_solver_slots <= 24
                or self.minimum_native_commit_gib < 0
                or self.poll_seconds < 0.25 or self.launch_spacing_seconds < 0):
            raise ValueError("Invalid production resource policy")


def policy_from_environment() -> RuntimePolicy:
    """Read resource policy once at scheduler startup; no dynamic code edits."""
    return RuntimePolicy(
        max_parallel=int(os.environ.get("NEURALCHT_PARALLEL", "3")),
        cores_per_case=int(os.environ.get("NEURALCHT_CORES_PER_CASE", "1")),
        minimum_free_disk_gib=float(os.environ.get("NEURALCHT_FREE_DISK_GIB", "5")),
        projected_workspace_gib=float(os.environ.get("NEURALCHT_WORKSPACE_GIB", "1.5")),
        minimum_free_memory_gib=float(os.environ.get("NEURALCHT_FREE_RAM_GIB", "8")),
        native_solver_slots=int(os.environ.get("NEURALCHT_NATIVE_SOLVER_SLOTS", "2")),
        minimum_native_commit_gib=float(os.environ.get("NEURALCHT_NATIVE_COMMIT_GIB", "8")),
        launch_spacing_seconds=float(os.environ.get("NEURALCHT_LAUNCH_SPACING_SEC", "5")),
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
    """Only a complete two-sided interface and physics PASS is terminal."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        return False
    path = PUBLISHED / case_id / "validation.json"
    if not path.is_file():
        return False
    import json
    try:
        proof = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    return proof.get("case_id")==case_id and complete_new_physics_proof(proof)


def has_accepted_transfer(case_id: str) -> bool:
    """Skip a verified CHT export without reopening any native mesh/field data."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        return False
    output = CASE_ROOT / case_id
    ready = output / "READY.json"
    receipt = output / "sampling_receipt.json"
    validation = output / "validation.json"
    if not (ready.is_file() and receipt.is_file() and validation.is_file()):
        return False
    # Official reruns must include all declared sampling files and the
    # complete NEW two-sided physical-interface acceptance proof.
    import json
    try:
        a = json.loads(ready.read_text(encoding="utf-8-sig"))
        b = json.loads(receipt.read_text(encoding="utf-8-sig"))
        c = json.loads(validation.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    files = a.get("files")
    if (not isinstance(files, list) or not files
            or any(not isinstance(name, str) or Path(name).name != name
                   or not (output / name).is_file() for name in files)):
        return False
    return (a.get("case_id") == case_id and a.get("status") == "READY"
            and b.get("case_id") == case_id and b.get("status") == "PASS"
            and b.get("lineage") == "RERUN_NEW_NATIVE"
            and c.get("case_id") == case_id
            and complete_new_physics_proof(c))


def active_case_workers() -> tuple[tuple[int, str, str], ...]:
    """Live Python workers from both queues; never trust stale queue labels."""
    import psutil
    workers = []
    parents = {}
    for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
        try:
            command = list(proc.info.get("cmdline") or ())
            if "--worker" not in command:
                continue
            scripts = [str(arg).lower().replace("/", "\\") for arg in command]
            old = any(s.endswith("\\sustainable_queue.py") for s in scripts)
            new = any(s.endswith("\\icepak_workflow\\main.py")
                      for s in scripts)
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
                parents[proc.pid] = proc.info.get("ppid")
        except (psutil.Error, OSError, ValueError):
            continue
    # The local venv launches a Python child with the same Case command.
    # Count its outer worker once, but keep independent process trees distinct.
    identities = {pid: (cid, kind) for pid, cid, kind in workers}
    return tuple(row for row in workers
                 if identities.get(parents.get(row[0])) != (row[1], row[2]))


LOCAL_PC_RESERVED_CASES = STATE / "SERVER_RESERVED_CASES.txt"


def locally_reserved_case_ids() -> frozenset[str]:
    """Human-readable Case ownership, not a new physics or validation gate."""
    if not LOCAL_PC_RESERVED_CASES.is_file():
        raise RuntimeError("Local-PC reservation list missing: prevent duplicate compute")
    rows=[x.strip() for x in LOCAL_PC_RESERVED_CASES.read_text(
        encoding="utf-8-sig").splitlines() if x.strip()]
    if (len(rows)!=len(set(rows)) or
            any(not r.startswith("case_") or not r[5:].isdigit() for r in rows)):
        raise RuntimeError("Invalid local exclusive Case list")
    return frozenset(rows)


def legacy_reserved_status(case_id: str) -> str:
    """Only a still-running old worker reserves this Case during handover."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    if case_id in locally_reserved_case_ids():
        return "RESERVED_FOR_LOCAL_PC"
    return ("RUNNING" if any(cid == case_id and kind == "old"
                             for _, cid, kind in active_case_workers()) else "")


def completed_rerun_ids() -> frozenset[str]:
    """Read one verified Case-ID list; never infer reruns from old PASS labels."""
    path = STATE / "completed_reruns.txt"
    if not path.is_file():
        raise RuntimeError("Verified completed_reruns.txt is missing; refusing queue admission")
    rows = [x.strip() for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    if not rows or any(not x.startswith("case_") or not x[5:].isdigit() for x in rows):
        raise RuntimeError("Invalid verified completed_reruns.txt")
    # Historical completed-rerun labels may describe wall or mixed sources.
    # A Case becomes terminal only with explicit two-sided interface proof.
    return frozenset(cid for cid in rows if (paired_interface_published(cid)
                                            or has_accepted_transfer(cid)))


def ensure_private_directories() -> None:
    for path in (RUNS, WORK, STATE, SAMPLES, PUBLISHED):
        path.mkdir(parents=True, exist_ok=True)


def workdir_for(case_id: str) -> Path:
    """A dedicated work directory, never the experiment or case root."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    return WORK / case_id

