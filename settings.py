"""Single production configuration for the independent NeuralCHT CHT queue.

Nothing in this module references the retired simulation experiment.  The only
external inputs are the authoritative case master and the installed AEDT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parent.parent
PROJECT = EXPERIMENT.parent.parent
CASE_ROOT = PROJECT / "case"
CASE_MASTER = PROJECT / "CASE_MASTER.csv"
CASE_FEATURES = CASE_ROOT / "CASE_FEATURES.csv"
RUNS = EXPERIMENT / "runs"
WORK = RUNS / "work"
STATE = RUNS / "state"
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
    max_parallel: int = 20
    cores_per_case: int = 4
    minimum_free_disk_gib: float = 5.0
    # Conservative future disk growth per live Case, measured workspaces
    # are already small and their bytes are included in actual free space.
    projected_workspace_gib: float = 1.5
    minimum_free_memory_gib: float = 5.0
    poll_seconds: float = 3.0
    launch_spacing_seconds: float = 5.0
    native_timeout_minutes: int = 45

    def __post_init__(self):
        if (not 1 <= self.max_parallel <= 20 or self.cores_per_case < 1
                or self.minimum_free_disk_gib < 0
                or self.projected_workspace_gib <= 0
                or self.minimum_free_memory_gib < 0
                or self.poll_seconds < 0.25 or self.launch_spacing_seconds < 0):
            raise ValueError("Invalid production resource policy")


def policy_from_environment() -> RuntimePolicy:
    """Read resource policy once at scheduler startup; no dynamic code edits."""
    return RuntimePolicy(
        max_parallel=int(os.environ.get("NEURALCHT_PARALLEL", "20")),
        cores_per_case=int(os.environ.get("NEURALCHT_CORES_PER_CASE", "4")),
        minimum_free_disk_gib=float(os.environ.get("NEURALCHT_FREE_DISK_GIB", "5")),
        projected_workspace_gib=float(os.environ.get("NEURALCHT_WORKSPACE_GIB", "1.5")),
        minimum_free_memory_gib=float(os.environ.get("NEURALCHT_FREE_RAM_GIB", "5")),
        launch_spacing_seconds=float(os.environ.get("NEURALCHT_LAUNCH_SPACING_SEC", "5")),
    )


def has_accepted_transfer(case_id: str) -> bool:
    """Skip a verified CHT export without reopening any native mesh/field data."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        return False
    output = PROJECT / "transfer_ready" / case_id
    ready = output / "READY.json"
    receipt = output / "sampling_receipt.json"
    if not (ready.is_file() and receipt.is_file()):
        return False
    # Only two small metadata files; a directory alone is not evidence of success.
    import json
    try:
        a = json.loads(ready.read_text(encoding="utf-8-sig"))
        b = json.loads(receipt.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    return (a.get("case_id") == case_id and a.get("status") == "READY"
            and b.get("case_id") == case_id and b.get("status") == "PASS"
            and b.get("lineage") == "RERUN_NEW_NATIVE")


def legacy_reserved_status(case_id: str) -> str:
    """Single-Case read: never duplicate the old manager's in-flight work.

    Only old terminal FAILED cases are eligible for a fresh attempt here.
    No bulk scans, hashing or changes to the legacy manager.
    """
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    old = (PROJECT/"experiments"/"20261023_base40_CFD_independent_validation"
           /"runs"/"server_scheduler_20261028_v5"/"scheduler"/"cases"
           /f"{case_id}.json")
    if not old.is_file():
        return ""
    import json
    try:
        record=json.loads(old.read_text(encoding="utf-8-sig"))
    except (OSError,ValueError):
        return "UNKNOWN"  # Fail closed for this one ID; other Cases proceed.
    status=record.get("job_status")
    return status if status in {"RUNNING","PENDING","DEFERRED","DONE"} else ""


def completed_rerun_ids() -> frozenset[str]:
    """Read one verified Case-ID list; never infer reruns from old PASS labels."""
    path = STATE / "completed_reruns.txt"
    if not path.is_file():
        raise RuntimeError("Verified completed_reruns.txt is missing; refusing queue admission")
    rows = [x.strip() for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]
    if not rows or any(not x.startswith("case_") or not x[5:].isdigit() for x in rows):
        raise RuntimeError("Invalid verified completed_reruns.txt")
    return frozenset(rows)


def ensure_private_directories() -> None:
    for path in (RUNS, WORK, STATE, SAMPLES, PUBLISHED, ASSETS):
        path.mkdir(parents=True, exist_ok=True)


def workdir_for(case_id: str) -> Path:
    """A dedicated work directory, never the experiment or case root."""
    if not case_id.startswith("case_") or not case_id[5:].isdigit():
        raise ValueError("Invalid physical Case ID")
    return WORK / case_id
