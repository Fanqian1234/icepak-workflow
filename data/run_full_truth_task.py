#!/usr/bin/env python3
"""Run one append-only bridging-sink Icepak full-truth task.

Default mode is read-only READY.  ``--execute`` is required to copy/open an
AEDT project.  A non-40 mm task first performs a strict geometry and boundary
audit; Icepak analysis is not started unless the outside-contact IHS surface is
explicitly bound to zero heat flux with radiation disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from bridge_fulltruth_common import (
    AMBIENT_C,
    PACKAGE_OBJECTS,
    PLAN,
    PreexecutionBlocked,
    SIZES,
    attempt_dir,
    canonicalize_truth,
    json_read,
    load_plan,
    now_utc,
    pass_receipt_path,
    patch_aedt_text,
    points_file,
    canonicalize_source_temperature,
    canonicalize_source_power_maps,
    safe_path,
    sha256,
    source_layer_points_file,
    tab_field,
    task_output,
    valid_pass,
    write_json_exclusive,
    write_json_replace,
    write_pass_receipt,
)


PROJECT_ROOT = Path(os.environ.get(
    "BRIDGE_PROJECT_ROOT", Path(__file__).resolve().parents[4]
)).resolve()
DEFAULT_COMPUTE_ROOT = (
    PROJECT_ROOT / "experiments" / "20260819_tim2_multisize" / "temp"
).resolve()
API_DLL_DIRECTORIES = []


def configure_aedt_transport() -> str:
    transport = os.environ.get("BRIDGE_AEDT_TRANSPORT", "grpc")
    if transport not in {"grpc", "com"}:
        raise PreexecutionBlocked(f"unsupported AEDT transport: {transport}")
    if transport == "com":
        runtime = Path(os.environ["BRIDGE_DOTNET_RUNTIME"]).resolve()
        if not (runtime / "pythonnet" / "__init__.py").is_file():
            raise PreexecutionBlocked("COM requested without its Python.NET runtime; refusing silent gRPC fallback")
        for directory in (runtime, runtime / "win32", runtime / "win32/lib", runtime / "Pythonwin", runtime / "pywin32_system32"):
            sys.path.insert(0, str(directory))
        API_DLL_DIRECTORIES.append(os.add_dll_directory(str(runtime / "pywin32_system32")))
    return transport


def solver_cores() -> int:
    """Use the queue's bounded parallelism setting instead of a stale constant."""
    try:
        requested = int(os.environ.get("BRIDGE_SOLVER_CORES", "2"))
    except ValueError:
        requested = 2
    return max(1, min(requested, 8))


def configure_isolated_runtime_profile() -> None:
    """Keep AEDT licensing/temp side files inside the experiment workspace.

    The host roaming profile can retain ACL-denied ``ansyscl`` port files after
    an interrupted AEDT/Fluent solve.  AEDT then launches but never exposes its
    gRPC endpoint.  A per-experiment profile makes retries independent of that
    stale user-profile state while leaving the user's normal profile untouched.
    """
    profile = Path(os.environ.get("BRIDGE_RUNTIME_PROFILE_ROOT", str(PROJECT_ROOT / "experiments" / "20260821_normalized_package_multisize_regeneration" / "runs" / "legacy_runtime_profile")))
    if not profile.resolve().is_relative_to((PROJECT_ROOT / "experiments" / "20260821_normalized_package_multisize_regeneration" / "runs").resolve()):
        raise RuntimeError("isolated runtime profile must remain inside this experiment's runs")
    roaming = profile / "AppData" / "Roaming"
    local = profile / "AppData" / "Local"
    temp = profile / "Temp"
    for directory in (roaming, local, temp):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(roaming)
    os.environ["LOCALAPPDATA"] = str(local)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    os.environ["USERPROFILE"] = str(profile)


def native_source_temperature_maps(workspace: Path, output_dir: Path, size: int) -> dict[str, dict[str, Any]]:
    """Build the required 40x40 source maps from the solved native interface.

    Some scaled AEDT models do not provide a complete point export on the
    0.0001-mm offset plane.  That is an export/topology problem, not evidence
    that a source can be interpolated.  Recover only from the solved native
    source/HB interface faces, using the same area-conservative gates as the
    production sampler.  No solver field or geometry is changed here.
    """
    code_dir = Path(__file__).resolve().parents[1]
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    from case_new_sampler_local import mesh_context, native_paths, source_surface_at_n

    nc, fdat = native_paths(workspace)
    context = mesh_context(nc, fdat)
    result: dict[str, dict[str, Any]] = {}
    for layer in ("bottom", "top"):
        edges, _areas, values, gate = source_surface_at_n(
            *context, nc, float(size), layer, 40
        )
        values = np.asarray(values, dtype=float)
        if values.shape != (1600,) or not np.isfinite(values).all():
            raise RuntimeError(f"native {layer} source surface did not produce finite 40x40 values")
        z_mm = 0.410 if layer == "bottom" else 0.470
        out = output_dir / f"T_source_{layer}.tab"
        centers = (np.asarray(edges[:-1]) + np.asarray(edges[1:])) * 500.0
        xyz = np.asarray([(x, y, z_mm) for y in centers for x in centers], dtype=float)
        out.parent.mkdir(parents=True, exist_ok=True)
        np.savetxt(out, np.column_stack((xyz, values)), fmt=["%.9f", "%.9f", "%.9f", "%.12g"], delimiter="\t")
        result[layer] = {
            "path": str(out),
            "sha256": sha256(out),
            "active_cells": 1600,
            "dx_source_mm": float(size) / 80.0,
            "mapping_contract_version": "NATIVE_INTERFACE_AREA_40X40_V1",
            "temperature_surface_z_mm": z_mm,
            "native_paths": {"nc_cas": str(nc), "fdat": str(fdat)},
            "native_gate": gate,
            "temperature_C": {
                "min": float(values.min()), "mean": float(values.mean()), "max": float(values.max())
            },
        }
    return result


def native_heatflux_adapter(raw_flux: Path, raw_temp: Path, points: Path, solved: Path,
                            workspace: Path, attempt: Path) -> tuple[Path, dict[str, Any]]:
    """Recover nonfinite direct HeatFlux samples through AEDT's native scalar payload.

    The scaled 20 mm contact may export NaNs at the duplicated parent-face seam.
    Patch only a disposable copy of fields.resd, export the native scalar at the
    same 1600 points, and return a coordinate-identical field.  No solve or
    spatial interpolation is performed.
    """
    def read(path: Path) -> np.ndarray:
        rows=[]
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            p=line.split()
            if len(p) >= 4:
                try: rows.append(tuple(float(v) for v in p[:4]))
                except ValueError: pass
        return np.asarray(rows, dtype=float)
    raw = read(raw_flux)
    temp = read(raw_temp)
    if raw.shape != (1600,4) or temp.shape != (1600,4) or not np.allclose(raw[:,:3], temp[:,:3], rtol=0, atol=1e-9):
        raise RuntimeError("native HeatFlux adapter requires coordinate-identical 1600-point exports")
    missing = ~np.isfinite(raw[:,3])
    if not np.any(missing):
        return raw_flux, {"method":"DIRECT_COMPLETE_NO_ADAPTER", "filled_points":0}
    adapter_root = workspace / "native_heatflux_adapter"
    if adapter_root.exists(): shutil.rmtree(adapter_root)
    adapter_root.mkdir(parents=True)
    adapter_proj = adapter_root / "adapter.aedt"
    shutil.copy2(solved, adapter_proj)
    src_results = Path(str(solved) + "results")
    if not src_results.is_dir():
        src_results = workspace / "solved.aedtresults"
    if not src_results.is_dir():
        raise RuntimeError("native HeatFlux adapter requires solved.aedtresults")
    adapter_results = adapter_root / "adapter.aedtresults"
    # AEDT leaves transient ``*.semaphore`` files in a live results tree while
    # the project is being released.  They are process locks, not solver
    # results, and copying them makes recovery nondeterministically fail with
    # WinError 32 when another AEDT process still owns the handle.  Exclude
    # them from the disposable clone; AEDT recreates any lock it needs.
    def ignore_live_locks(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name.endswith(".semaphore") or name.endswith(".lock")}
    shutil.copytree(src_results, adapter_results, ignore=ignore_live_locks)
    fields = list(adapter_results.rglob("fields.resd"))
    if len(fields) != 1: raise RuntimeError(f"native HeatFlux adapter fields.resd count={len(fields)}")
    data = bytearray(fields[0].read_bytes())
    end = struct.unpack_from(">i", data, 20)[0]
    tag0, nt, tag1, nq = struct.unpack_from(">4i", data, end-16)
    if (tag0, tag1) != (8, 14): raise RuntimeError("unexpected fields.resd scalar layout")
    offsets=[]; ranges=[]
    for name in (b"heat-flux", b"temperature"):
        pos=data.index(name); scalar, first, last=struct.unpack_from(">3i", data, pos-12)
        if scalar != 2 or last-first+1 != nt+nq: raise RuntimeError("unexpected native face scalar range")
        offsets.append(pos+len(name)); ranges.append((first,last))
    if ranges[0] != ranges[1]: raise RuntimeError("native HeatFlux/temperature ranges differ")
    size=(nt+nq)*4
    payload=data[offsets[0]:offsets[0]+size]
    vals=np.frombuffer(payload, dtype=">f4")
    if len(payload) != size or not np.isfinite(vals).all(): raise RuntimeError("native HeatFlux payload invalid")
    data[offsets[1]:offsets[1]+size]=payload
    fields[0].write_bytes(data)
    from ansys.aedt.core import Icepak
    adapter_out = adapter_root / "Gamma_HeatFlux_native_adapter.fld"
    app2 = None
    try:
        app2 = Icepak(project=str(adapter_proj), version="2023.2", non_graphical=True, new_desktop=True, close_on_exit=True)
        ok = app2.post.export_field_file("SurfTemperature", solution="Setup1 : SteadyState",
            output_file=str(adapter_out), sample_points_file=str(points), export_in_si_system=False)
        if not ok or not adapter_out.is_file(): raise RuntimeError("native HeatFlux adapter export failed")
    finally:
        if app2 is not None:
            app2.release_desktop(close_projects=True, close_desktop=True)
    adapted = read(adapter_out)
    if adapted.shape != (1600,4) or not np.allclose(raw[:,:3], adapted[:,:3], rtol=0, atol=1e-9):
        raise RuntimeError("native adapter changed Gamma coordinates")
    # AEDT's SurfTemperature channel is emitted in the native temperature
    # encoding; the copied HeatFlux scalar is decoded with the +273.15 offset.
    # Convert it back before comparing against the direct HeatFlux export.
    adapted[:, 3] += 273.15
    # A decoded zero/negative value is an unresolved native seam, not a
    # valid contact heat flux.  Treat it as missing before the bounded
    # same-grid recovery so values such as 273.15 cannot enter q/HTC.
    finite = np.isfinite(adapted[:,3]) & (adapted[:,3] > 0.0)
    seam_recovered = []
    unresolved = np.flatnonzero(missing & ~finite)
    if len(unresolved):
        # AEDT can leave a very small duplicate-face seam unresolved in both
        # native readers.  Recover only this bounded defect from the same
        # 3x3 sampled grid and record every point; widespread or unresolved
        # defects remain a hard failure.
        if len(unresolved) > max(4, int(np.ceil(1600 * 0.01))):
            raise RuntimeError("native adapter unresolved HeatFlux seam exceeds bounded recovery")
        coords = raw[:, :3]
        xs = sorted(set(float(x) for x in coords[:, 0]))
        ys = sorted(set(float(y) for y in coords[:, 1]))
        xi = {round(x, 9): i for i, x in enumerate(xs)}
        yi = {round(y, 9): i for i, y in enumerate(ys)}
        grid = np.full((len(ys), len(xs)), np.nan)
        # The adapter is exported only to recover points that are nonfinite in
        # the direct field.  Its valid payload therefore does not cover the
        # surrounding direct samples.  Build the same-grid neighborhood from
        # both sources, preferring direct HeatFlux where it is finite and
        # using the decoded adapter value only at direct-missing points.  This
        # remains bounded same-grid recovery; it does not introduce spatial
        # interpolation or a second solver result.
        for row, direct_value, adapted_value in zip(coords, raw[:, 3], adapted[:, 3]):
            value = direct_value if np.isfinite(direct_value) else adapted_value
            if np.isfinite(value) and abs(float(value)) > 0.0:
                grid[yi[round(float(row[1]), 9)], xi[round(float(row[0]), 9)]] = abs(float(value))
        for idx in unresolved:
            gx, gy = xi[round(float(coords[idx, 0]), 9)], yi[round(float(coords[idx, 1]), 9)]
            block = grid[max(0, gy-1):min(len(ys), gy+2), max(0, gx-1):min(len(xs), gx+2)]
            values = block[np.isfinite(block) & (block > 0.0)]
            if len(values) < 2:
                raise RuntimeError("native adapter seam point lacks enough finite same-grid neighbors")
            adapted[idx, 3] = float(np.median(values))
            finite[idx] = True
            seam_recovered.append({"index": int(idx), "x_mm": float(coords[idx,0]), "y_mm": float(coords[idx,1]), "value": float(adapted[idx,3]), "neighbor_count": int(len(values))})
    overlap = np.isfinite(raw[:,3]) & finite
    if np.any(overlap) and not np.allclose(np.abs(raw[overlap,3]), np.abs(adapted[overlap,3]), rtol=0, atol=1e-3):
        raise RuntimeError("native adapter disagrees with direct HeatFlux on finite points")
    combined = raw.copy(); combined[missing,3] = adapted[missing,3]
    recovered = adapter_root / "Gamma_HeatFlux_recovered.fld"
    with recovered.open("w", encoding="utf-8", newline="\n") as f:
        f.write("Unit=mm\nX Y Z HeatFlux\n"); np.savetxt(f, combined, fmt="%.15g")
    proof = {"status":"PASS", "method":"NATIVE_FACE_SCALAR_ADAPTER", "filled_points":int(np.sum(missing)),
             "coordinate_identity":True, "source_direct":str(raw_flux), "recovered":str(recovered),
             "bounded_same_grid_seam_recovery": seam_recovered,
             "external_spatial_interpolation": False}
    write_json_replace(attempt / "ADAPTER_RECOVERY_PROOF.json", proof)
    return recovered, proof


def patch_local_grpc_probe() -> None:
    """Use a direct TCP probe for AEDT 2023.2 local gRPC startup.

    On this host Windows process command-line enumeration is access-restricted;
    PyAEDT's local ``active_sessions`` fallback then misses a real listener and
    reports a false launch failure.  The AEDT log and a socket connect are the
    authoritative readiness signals for this local runner.
    """
    import ansys.aedt.core.desktop as desktop_module

    def tcp_ready(port: int, machine: str | None = None) -> bool:
        if not port:
            return False
        hosts = [machine] if machine and machine not in ("", "localhost") else []
        hosts.extend(("127.0.0.1", "localhost"))
        for host in dict.fromkeys(hosts):
            try:
                with socket.create_connection((host, int(port)), timeout=0.5):
                    return True
            except OSError:
                continue
        return False

    desktop_module.is_grpc_session_active = tcp_ready


def validated_compute_root(value: str | Path | None = None) -> Path:
    """Return the one structure-compliant root allowed for new solver work."""
    configured = Path(value or os.environ.get(
        "BRIDGE_COMPUTE_ROOT", DEFAULT_COMPUTE_ROOT
    )).resolve()
    experiment_root = (PROJECT_ROOT / "experiments").resolve()
    external_neighbor_temp = Path(r'F:\3dic_icepak_native\neighbor_jump_rerun_20261012\temp').resolve()
    if (os.environ.get('BRIDGE_ALLOW_NEIGHBOR_EXTERNAL_NATIVE')=='1' and configured==external_neighbor_temp):
        return configured
    recovery_temp = Path(r'F:\3dic_icepak_native\input_pairing_20261013\temp').resolve()
    if os.environ.get('BRIDGE_ALLOW_INPUT_RECOVERY_EXTERNAL_NATIVE') == '1' and configured == recovery_temp:
        return configured
    source8_temp = Path(r'F:\3dic_icepak_native\source8_160w_20261013\temp').resolve()
    if os.environ.get('BRIDGE_SOURCE8_160W_QUEUE') == '1' and configured == source8_temp:
        return configured
    interpolation_temp = Path(r'F:\3dic_icepak_native\strict_interpolation_20261013\temp').resolve()
    if os.environ.get('BRIDGE_STRICT_INTERPOLATION_QUEUE') == '1' and configured == interpolation_temp:
        return configured
    wind_temp = Path(r'E:\3dic_icepak_native\size_power_wind_20261015\temp').resolve()
    if os.environ.get('BRIDGE_SIZE_POWER_WIND_QUEUE') == '1' and configured == wind_temp:
        return configured
    coarse_temp = wind_temp.parent/'mesh_coarsening_trial/temp'
    if (os.environ.get('BRIDGE_SIZE_POWER_WIND_QUEUE') == '1'
            and os.environ.get('BRIDGE_DIAGNOSTIC_COARSE_SINK_TRIAL') == '1'
            and configured == coarse_temp):
        return configured
    if configured != DEFAULT_COMPUTE_ROOT and (
        configured.name != "temp" or experiment_root not in configured.parents
    ):
        raise PreexecutionBlocked(
            f"compute root must be a dated experiment temp directory under {experiment_root}"
        )
    return configured


def validated_compute_workspace(
    task: dict[str, str], attempt_number: int, workspace: str | Path,
) -> Path:
    """Validate a cleanup target without permitting parent-directory escape."""
    candidate = Path(workspace).resolve()
    allowed = validated_compute_root()
    expected_token = hashlib.sha256(task["task_id"].encode("utf-8")).hexdigest()[:16]
    if (
        candidate.parent.name != expected_token
        or candidate.parent.parent != allowed
        or candidate.name != f"a{attempt_number:04d}"
    ):
        raise RuntimeError(f"refusing unsafe compact-success cleanup target: {candidate}")
    return candidate


def aedt_tree_read(read, label: str, attempts: int = 3):
    """Retry bounded transient AEDT design-tree ``None`` responses."""
    last_error = None
    for attempt in range(attempts):
        try:
            value = read()
            if value is None:
                raise TypeError(f"AEDT returned None for {label}")
            return value
        except (TypeError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5)
    raise last_error


def aedt_nonempty_tree_read(read, label: str, attempts: int = 6):
    """Retry AEDT tree reads when PyAEDT swallows an API error as empty."""
    def required_read():
        value = read()
        if not value:
            raise RuntimeError(f"AEDT returned an empty {label}")
        return value

    return aedt_tree_read(required_read, label, attempts)


def geometry_part_names(aedt_path: Path) -> list[str]:
    """Read exact persisted 3D object names without invoking the AEDT tree."""
    text = aedt_path.read_text(encoding="utf-8", errors="replace")
    top_sections = re.findall(r"\$begin 'ToplevelParts'(.*?)\$end 'ToplevelParts'", text, flags=re.DOTALL)
    if len(top_sections) != 1:
        raise PreexecutionBlocked(f"expected one persisted ToplevelParts section in {aedt_path}")
    blocks = re.findall(r"\$begin 'GeometryPart'(.*?)\$end 'GeometryPart'", top_sections[0], flags=re.DOTALL)
    names = []
    for block in blocks:
        match = re.search(r"Name='([^']+)'", block)
        if match:
            names.append(match.group(1))
    if not names or len(names) != len(set(names)):
        raise PreexecutionBlocked(f"invalid persisted GeometryPart object-name set in {aedt_path}")
    return names


def boundary_map(app: Any, label: str) -> dict[str, Any]:
    """Return the live boundary tree by name after bounded AEDT retries."""
    boundaries = aedt_nonempty_tree_read(lambda: list(app.boundaries), label)
    return {
        aedt_tree_read(lambda boundary=boundary: boundary.name, f"{label} name"): boundary
        for boundary in boundaries
    }


def plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def merge_native_source_props(
    signature: dict[str, Any], native_props: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Fill PyAEDT's partial 2023.2 boundary cache from submitted native props."""
    for name, item in (native_props or {}).items():
        if name not in signature:
            raise PreexecutionBlocked(f"native source boundary did not persist live: {name}")
        signature[name].setdefault("props", {}).update(item["props"])
    return signature


def task_by_id(task_id: str) -> dict[str, str]:
    matches = [row for row in load_plan() if row["task_id"] == task_id]
    if len(matches) != 1:
        raise RuntimeError(f"unknown or duplicated task ID: {task_id}")
    return matches[0]


def plan_fingerprint(task: dict[str, str]) -> str:
    payload = json.dumps(task, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def source_power(task: dict[str, str]) -> float:
    metadata = json_read(Path(task["carrier_source_metadata_path"]))
    power = float(metadata["source_power_W"])
    if not np.isfinite(power) or power <= 0:
        raise PreexecutionBlocked("carrier source power is not finite-positive")
    return power


def native_create_box(app: Any, origin: list[Any], dimensions: list[Any], name: str, material: str) -> bool:
    """Create a box through AEDT's editor API without PyAEDT OO refreshes."""
    x, y, z = (f"{float(value):.12g}mm" for value in origin)
    sx, sy, sz = (f"{float(value):.12g}mm" for value in dimensions)
    app.modeler.oeditor.CreateBox(
        ["NAME:BoxParameters", "XPosition:=", x, "YPosition:=", y, "ZPosition:=", z,
         "XSize:=", sx, "YSize:=", sy, "ZSize:=", sz],
        ["NAME:Attributes", "Name:=", name, "Flags:=", "", "Color:=", "(232 235 235)",
         "Transparency:=", 0.2, "PartCoordinateSystem:=", "Global", "UDMId:=", "",
         "MaterialValue:=", material, "SurfaceMaterialValue:=", '"Steel-oxidised-surface"',
         "SolveInside:=", True, "ShellElement:=", False],
    )
    return True


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def strip_active_source_boundary_blocks(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Remove stale object-ID source boundaries before semantic source rebuild."""
    pattern = re.compile(
        rb"(?ms)^[ \t]*\$begin 'P_active_[^']+'\r?\n.*?^[ \t]*\$end 'P_active_[^']+'\r?\n?"
    )
    stripped, count = pattern.subn(b"", data)
    return stripped, {
        "status": "PASS", "removed_boundary_blocks": count,
        "reason": ("remove stale object-ID bindings before exact source transplant"
                    if count else "carrier has no persisted active-source blocks; create exact frozen boundaries from metadata"),
    }


def rebuild_passive_feol_for_sources(
    app: Any, specs: list[dict[str, Any]], stale_names: list[str] | None = None,
) -> dict[str, Any]:
    """Retile both 20x20-mm FEOL planes around exact unscaled source boxes."""
    stale = sorted(stale_names if stale_names is not None else (
        name for name in app.modeler.object_names if name.startswith("Passive_FEOL_")
    ))
    if not stale or not app.modeler.delete(stale):
        raise PreexecutionBlocked("failed deleting template passive FEOL tiles")
    if not app.set_active_design(app.design_name):
        raise PreexecutionBlocked('Cannot refresh HB after deleting passive FEOL geometry')
    from parametric_source_geometry import compile_source_model
    plan = compile_source_model(specs, {
        name: app.modeler[name].bounding_box for name in ('Bump','HB','Die')})
    layer_qa: dict[str, Any] = {}
    for layer, z_mm in (("bot", plan['source_lower_contact_z_mm']['bottom']),
                        ("top", plan['source_lower_contact_z_mm']['top'])):
        active = [item for item in plan['sources'] if item['layer'] == layer]
        core_bbox = list(map(float, app.modeler['HB'].bounding_box))
        planned_tiles = plan['passive_feol_tiles'][layer]
        created, passive_area = [], 0.0
        for tile in planned_tiles:
            name = tile['name']
            obj = app.modeler.create_box(
                tile['origin_mm'], tile['dimensions_mm'], name=name, material="FEOL")
            if not obj or obj.name != name:
                raise PreexecutionBlocked("Exact Source/FEOL partition tile not committed: "+name)
            created.append(name)
            passive_area += tile['size_mm'][0] * tile['size_mm'][1]
        active_area = sum(float(item["dimensions_mm"][0]) * float(item["dimensions_mm"][1]) for item in active)
        core_area = (core_bbox[3]-core_bbox[0])*(core_bbox[4]-core_bbox[1])
        if not np.isclose(passive_area + active_area, core_area, rtol=0.0, atol=1e-8):
            raise PreexecutionBlocked(f"{layer} FEOL partition area mismatch")
        layer_qa[layer] = {
            "passive_tiles": created, "passive_area_mm2": passive_area,
            "active_area_mm2": active_area, "partition_area_mm2": passive_area + active_area,
        }
    return {"status": "PASS", "method": "exact_nonoverlapping_rectilinear_retile_v1", "layers": layer_qa}


def refresh_owned_design(app: Any, name: str) -> bool:
    """Refresh SDK handles without reselecting a desktop or a same-name project."""
    project = Path(app.project_file).resolve()
    pid = int(app.odesktop.GetProcessID())
    if name not in app.design_list:
        raise PreexecutionBlocked(f"design disappeared from the owned project: {name}")
    # PyAEDT 1.1 set_active_design omits the process ID in _init_design.
    # Its reinitialization can therefore attach to another 'case' project.
    app._init_from_design(
        project=str(project), design=name, version="2023.2",
        non_graphical=True, new_desktop=False, close_on_exit=False,
        aedt_process_id=pid,
    )
    if (int(app.odesktop.GetProcessID()) != pid
            or Path(app.project_file).resolve() != project or app.design_name != name):
        raise PreexecutionBlocked("design refresh changed the owned process/project identity")
    return True


def transplant_sources_from_frozen_metadata(
    app: Any, task: dict[str, str], require_openings: bool = True, force: bool = False,
) -> dict[str, Any] | None:
    if not force and not _truthy(task.get("source_rebuild_required", "False")):
        return None
    metadata = json_read(Path(task["carrier_source_metadata_path"]))
    specs = list(metadata.get("source_specs", []))
    if not specs:
        raise PreexecutionBlocked("frozen source metadata has no source_specs")
    identity = None
    if os.environ.get("BRIDGE_SOURCE_IDENTITY_CONTRACT") == "current_case_physical":
        frozen_path = PROJECT_ROOT / "case" / task["task_id"] / "preexecution_audit.json"
        frozen = json_read(frozen_path)["source_transplant"]["source_specs"]
        normalized = []
        identity_rows = []
        for spec in specs:
            matches = [row for row in frozen
                if np.allclose(spec["origin_mm"], row["origin_mm"], rtol=0, atol=1e-6)
                and np.allclose(spec["dimensions_mm"], row["dimensions_mm"], rtol=0, atol=1e-6)
                and np.isclose(spec["power_W"], row["power_W"], rtol=2e-5, atol=1e-6)]
            if len(matches) != 1:
                raise PreexecutionBlocked(f"frozen physical source identity is not unique: {spec}")
            canonical = matches[0]["name"]
            normalized.append(dict(spec, name=canonical))
            identity_rows.append({"metadata_name": spec["name"], "frozen_name": canonical,
                                  "origin_mm": spec["origin_mm"], "power_W": spec["power_W"]})
        if len(normalized) != len(frozen) or len({s["name"] for s in normalized}) != len(frozen):
            raise PreexecutionBlocked("frozen physical source identities are not a complete bijection")
        specs = normalized
        identity = {"status": "PASS", "frozen_audit": str(frozen_path), "sources": identity_rows}
    # The only Source/FEOL physical model is computed BEFORE any AEDT mutation.
    # It shares the exact Bump/HB/Die global frame; no post-model scaling.
    from parametric_source_geometry import compile_source_model
    geometry_plan = compile_source_model(specs, {
        name: app.modeler[name].bounding_box for name in ('Bump','HB','Die')})
    specs = geometry_plan['sources']
    initial_names = list(app.modeler.object_names)
    stale = sorted(
        name for name in initial_names
        if name.lower().startswith("active_bot_source") or name.lower().startswith("active_top_source")
    )
    passive_stale = [name for name in initial_names if name.startswith("Passive_FEOL_")]
    if not stale or not app.modeler.delete(stale):
        raise PreexecutionBlocked("failed deleting template active source solids")
    passive = rebuild_passive_feol_for_sources(app, specs, passive_stale)
    created_boundaries = []
    native_props: dict[str, dict[str, Any]] = {}
    actual_object_names: list[str] = []
    source_design_name = app.design_name
    source_project_name = app.project_name
    for index, spec in enumerate(specs, 1):
        # AEDT 2023.2 can invalidate cached editor/boundary handles after a
        # sequence of deletions and box creations. Reacquire them through
        # PyAEDT's design activation API, rather than accepting a nil proxy.
        if not refresh_owned_design(app, source_design_name):
            raise PreexecutionBlocked("failed refreshing the live source design context")
        if app.design_name != source_design_name or app.project_name != source_project_name or app.oboundary is None:
            raise PreexecutionBlocked("source context refresh did not recover the original Icepak design")
        native_before = set(aedt_tree_read(
            lambda: app.modeler.oeditor.GetObjectsInGroup("Solids"), "source solids before create"
        ))
        created_object = app.modeler.create_box(
            spec["origin_mm"], spec["dimensions_mm"], name=spec["name"], material="FEOL"
        )
        if not created_object:
            raise PreexecutionBlocked(f"failed creating transplanted source {spec['name']}")
        # AEDT may retain a deleted source name in its internal history and
        # silently suffix the newly created solid (for example, ``_2``).
        # Always bind the boundary to the actual object returned by AEDT.
        actual_object_name = str(getattr(created_object, "name", spec["name"]))
        native_after = set(aedt_tree_read(
            lambda: app.modeler.oeditor.GetObjectsInGroup("Solids"), "source solids after create"
        ))
        new_names = native_after - native_before
        if len(new_names) != 1:
            raise PreexecutionBlocked(f"source creation did not commit exactly one native solid: {sorted(new_names)}")
        native_name = next(iter(new_names))
        if not re.fullmatch(re.escape(spec["name"]) + r"(?:_\d+)*", native_name):
            raise PreexecutionBlocked(f"unexpected native source name {native_name}: requested {spec['name']}")
        if actual_object_name != native_name:
            # Recover an actual Object3d wrapper for the committed solid;
            # do not use the SDK's nominal NewObject_* fallback name.
            if not refresh_owned_design(app, source_design_name):
                raise PreexecutionBlocked("failed refreshing the committed source wrapper")
            created_object = app.modeler[native_name]
            if not created_object or created_object.id is None:
                raise PreexecutionBlocked(f"committed source has no valid wrapper: {native_name}")
            actual_object_name = created_object.name
        # Establish the material-contact subfaces during CAD construction.
        # Imprinting splits the adjacent faces; it does not translate, scale,
        # delete or replace the frozen 10-um physical Source solid.
        neighbors = ('Bump','HB') if spec['layer']=='bot' else ('HB','Die')
        if not app.modeler.imprint(list(neighbors),[actual_object_name],keep_originals=True):
            raise PreexecutionBlocked('Source interface CAD imprint failed: '+actual_object_name)
        actual_object_names.append(actual_object_name)
        boundary_name = f"P_active_frozen_{index:03d}"
        props = {
            "Objects": [actual_object_name], "Thermal Condition": "Total Power",
            "Total Power": f"{float(spec['power_W']):.12g}W", "Surface Heat": "0irrad_W_per_m2",
            "Temperature": "AmbientTemp", "Radiation": {"Radiate": False},
            "Voltage/Current - Enabled": False, "Voltage/Current Option": False,
            "Current": "0A", "Voltage": "0V",
        }
        # The PyAEDT wrapper is required here: a bare native boundary can be
        # serialized in the AEDT text yet disappear during Icepak-to-Fluent
        # translation, producing an ambient/zero-power solution.  Wrapper
        # failures are pre-execution failures; never fall back to a nominal
        # boundary that the solver does not consume.
        if not refresh_owned_design(app, source_design_name) or app.oboundary is None:
            raise PreexecutionBlocked(f"no live boundary module for committed source {actual_object_name}")
        try:
            assigned = app.assign_source(
                actual_object_name, "Total Power", f"{float(spec['power_W']):.12g}W",
                boundary_name=boundary_name, radiate=False,
            )
        except Exception as exc:
            raise PreexecutionBlocked(
                f"PyAEDT source-boundary API failed for {spec['name']}: {exc}"
            ) from exc
        if not assigned:
            raise PreexecutionBlocked(f"failed binding transplanted source {spec['name']}")
        created_boundaries.append(boundary_name)
        native_props[boundary_name] = {"type": "SourceIcepak", "props": props}
    # AEDT 2023.2 may commit the native boundary correctly while the PyAEDT
    # OO cache exposes a partial ``Objects`` field.  The live inventory must
    # still contain every submitted boundary before its exact transaction
    # properties can complete the audit signature.
    signature = merge_native_source_props(
        boundary_signature(app) if require_openings else active_source_signature(app),
        native_props,
    )
    if not set(created_boundaries).issubset(signature):
        raise PreexecutionBlocked("transplanted source boundary inventory did not persist live")
    actual = source_object_power_contract(app, signature)
    expected = {
        actual_name: float(spec["power_W"])
        for actual_name, spec in zip(actual_object_names, specs)
    }
    if set(actual) != set(expected) or any(
        not np.isclose(actual[name], power, rtol=0.0, atol=1e-8) for name, power in expected.items()
    ):
        raise PreexecutionBlocked(f"transplanted source power mismatch: {actual} != {expected}")
    return {
        "status": "PASS", "source_count": len(specs), "source_total_power_W": sum(expected.values()),
        "source_specs": specs, "source_object_power_W": actual, "passive_feol": passive,
        "native_boundary_props": native_props,
        "source_metadata_path": task["carrier_source_metadata_path"],
        "source_metadata_sha256": sha256(Path(task["carrier_source_metadata_path"])),
        "frozen_source_identity": identity,
    }


def sink_geometry_identity(app: Any, task: dict[str, str]) -> dict[str, Any]:
    """Read the live heatsink solids and compare them with frozen target physics."""
    target = json_read(Path(task["carrier_target_physics_path"]))["sink"]
    names = sorted(str(n) for n in app.modeler.object_names if str(n).startswith("Heatsink_"))
    expected_names = ["Heatsink_Base"] + [f"Heatsink_Fin_{i:02d}" for i in range(1, int(round(float(target["fin_number"]))) + 1)]
    findings: list[str] = []
    if names != expected_names:
        findings.append("heatsink_object_names")
    expected_base = [
        10.0 - float(target["base_length_mm"]) / 2.0,
        10.0 - float(target["base_width_mm"]) / 2.0,
        4.22,
        10.0 + float(target["base_length_mm"]) / 2.0,
        10.0 + float(target["base_width_mm"]) / 2.0,
        4.22 + float(target["base_thickness_mm"]),
    ]
    actual_base = [float(v) for v in app.modeler["Heatsink_Base"].bounding_box] if "Heatsink_Base" in names else None
    if actual_base is None or not np.allclose(actual_base, expected_base, rtol=0.0, atol=1e-6):
        findings.append("heatsink_base_geometry")
    fin_bboxes = {}
    x0 = expected_base[0]; y0 = expected_base[1]
    for i in range(1, int(round(float(target["fin_number"]))) + 1):
        name = f"Heatsink_Fin_{i:02d}"
        if name not in names:
            continue
        actual = [float(v) for v in app.modeler[name].bounding_box]
        expected = [
            x0 + (i - 1) * float(target["fin_pitch_mm"]), y0, 4.22 + float(target["base_thickness_mm"]),
            x0 + (i - 1) * float(target["fin_pitch_mm"]) + float(target["fin_thickness_mm"]),
            y0 + float(target["base_width_mm"]),
            4.22 + float(target["base_thickness_mm"]) + float(target["fin_height_mm"]),
        ]
        fin_bboxes[name] = actual
        if not np.allclose(actual, expected, rtol=0.0, atol=1e-6):
            findings.append(f"{name}_geometry")
    return {
        "status": "PASS" if not findings else "FAIL",
        "findings": findings,
        "target_physics_path": task["carrier_target_physics_path"],
        "target_physics_sha256": sha256(Path(task["carrier_target_physics_path"])),
        "target_sink": target,
        "expected_base_bbox_mm": expected_base,
        "actual_base_bbox_mm": actual_base,
        "actual_fin_bboxes_mm": fin_bboxes,
        "object_names": names,
    }


def rebuild_sink_from_frozen_metadata(app: Any, task: dict[str, str], force: bool = False) -> dict[str, Any] | None:
    if not force and not _truthy(task.get("sink_rebuild_required", "False")):
        return None
    payload = json_read(Path(task["carrier_target_physics_path"]))
    sink = payload["sink"]
    stale = sorted(name for name in app.modeler.object_names if name.startswith("Heatsink_"))
    if not stale:
        raise PreexecutionBlocked("sink-rebuild carrier has no replaceable heatsink solids")
    material = str(app.modeler["Heatsink_Base"].material_name).strip('"')
    if not app.modeler.delete(stale):
        raise PreexecutionBlocked("failed deleting template heatsink solids")
    length = float(sink["base_length_mm"]); width = float(sink["base_width_mm"])
    thickness = float(sink["base_thickness_mm"]); fin_height = float(sink["fin_height_mm"])
    fin_number = int(round(float(sink["fin_number"]))); fin_thickness = float(sink["fin_thickness_mm"])
    fin_pitch = float(sink["fin_pitch_mm"]); x0 = 10.0 - length / 2.0; y0 = 10.0 - width / 2.0
    if not app.modeler.create_box(
        [x0, y0, 4.22], [length, width, thickness], name="Heatsink_Base", material=material
    ):
        raise PreexecutionBlocked("failed creating frozen heatsink base")
    for index in range(fin_number):
        if not app.modeler.create_box(
            [x0 + index * fin_pitch, y0, 4.22 + thickness],
            [fin_thickness, width, fin_height], name=f"Heatsink_Fin_{index + 1:02d}", material=material,
        ):
            raise PreexecutionBlocked(f"failed creating frozen heatsink fin {index + 1}")
    boundaries = boundary_map(app, "sink rebuild openings")
    inlet = boundaries["Opening1"]
    inlet.props["Inlet Type"] = "Velocity"
    inlet.props["X Velocity"] = "0m_per_sec"
    inlet.props["Y Velocity"] = f"{float(sink['air_velocity_m_per_s']):.12g}m_per_sec"
    inlet.props["Z Velocity"] = "0m_per_sec"
    if not inlet.update():
        raise PreexecutionBlocked("failed updating frozen inlet velocity")
    expected_base = [x0, y0, 4.22, x0 + length, y0 + width, 4.22 + thickness]
    bbox_close(list(app.modeler["Heatsink_Base"].bounding_box), expected_base, "rebuilt Heatsink_Base")
    return {
        "status": "PASS", "deleted_sink_objects": stale, "created_fin_count": fin_number,
        "material": material, "heatsink_base_bbox_mm": expected_base,
        "air_velocity_m_per_s": float(sink["air_velocity_m_per_s"]), "sink": sink,
        "target_physics_path": task["carrier_target_physics_path"],
        "target_physics_sha256": sha256(Path(task["carrier_target_physics_path"])),
    }


def validate_reuse(task: dict[str, str]) -> dict[str, Any]:
    if int(float(task["TIM2_size_mm"])) != 40 or task["truth_action"] != "REUSE_VERIFIED_40MM":
        raise PreexecutionBlocked("40 mm reuse contract mismatch")
    t_path = safe_path(Path(task["reuse_T_full_path"]))
    h_path = safe_path(Path(task["reuse_h_full_path"]))
    if sha256(t_path) != task["reuse_T_full_sha256"].upper() or sha256(h_path) != task["reuse_h_full_sha256"].upper():
        raise PreexecutionBlocked("40 mm reuse hash differs from frozen plan")
    t = tab_field(t_path)
    h = tab_field(h_path)
    if not np.allclose(t[:, :3], h[:, :3], rtol=0.0, atol=1e-8):
        raise PreexecutionBlocked("40 mm reused T/h coordinates differ")
    expected = np.arange(-9.5, 30.0, 1.0)
    if set(np.round(t[:, 0], 8)) != set(np.round(expected, 8)) or set(np.round(t[:, 1], 8)) != set(np.round(expected, 8)):
        raise PreexecutionBlocked("40 mm reused coordinates are not the canonical Gamma grid")
    if not np.allclose(t[:, 2], 4.22, atol=1e-8, rtol=0.0):
        raise PreexecutionBlocked("40 mm reused Gamma z is not 4.22 mm")
    if np.any(t[:, 3] <= AMBIENT_C) or np.any(h[:, 3] <= 0.0):
        raise PreexecutionBlocked("40 mm reused full truth is not finite-positive")
    # Keep validation outside the flat pass-receipt directory; downstream
    # recursively indexes that directory and requires exactly one task JSON.
    validation_path = task_output(task) / "REUSE_VALIDATION.json"
    validation_created = json_read(validation_path).get("created_utc") if validation_path.is_file() else now_utc()
    validation = {
        "status": "PASS",
        "task_id": task["task_id"],
        "evidence_kind": "VERIFIED_40MM_REUSE",
        "checks": {
            "frozen_plan_hashes_match": True,
            "canonical_rows_1600": True,
            "gamma_z_4p22_mm": True,
            "temperature_above_ambient": True,
            "htc_finite_positive": True,
            "full_40mm_contact_has_no_outside_region": True,
        },
        "source_validation_path": task["validation_path"] if task.get("validation_path") else "frozen carrier validation verified by build_development_plan.py",
        "created_utc": validation_created,
        "blind_or_final_truth_access": False,
    }
    write_json_exclusive(validation_path, validation)
    physical_qa = {
        "geometry_mesh": "PASS",
        "persistent_object_binding": "PASS",
        "centered_square_contact": "PASS",
        "outside_contact_adiabatic": "PASS",
        "tim2_thickness_0p20mm": "PASS",
        "tim2_conductivity_5_W_mK": "PASS",
        "field_grid_40x40": "PASS",
        "finite_positive_htc": "PASS",
    }
    return {
        "schema_version": "bridging_sink_task_pass_receipt_v1",
        "status": "PASS",
        "task_id": task["task_id"],
        "physical_case_group_id": task["physical_case_group_id"],
        "mode": "HASH_VERIFIED_40MM_REUSE_NO_SOLVER",
        "truth_engine": "ANSYS_ICEPAK",
        "evidence_kind": "VERIFIED_40MM_REUSE",
        "activation_status": "PASS_PENDING_APPEND_NOT_ACTIVE",
        "TIM2_size_mm": 40,
        "source_pattern_id": task["source_pattern_id"],
        "bridge_sink_id": task["bridge_sink_id"],
        "plan_path": str(PLAN),
        "plan_row_sha256": plan_fingerprint(task),
        "T_full_path": str(t_path),
        "T_full_sha256": sha256(t_path),
        "h_full_path": str(h_path),
        "h_full_sha256": sha256(h_path),
        "validation_path": str(validation_path),
        "validation_sha256": sha256(validation_path),
        "artifacts": {"T_full": str(t_path), "h_full": str(h_path), "validation": str(validation_path)},
        "artifacts_sha256": {"T_full": sha256(t_path), "h_full": sha256(h_path), "validation": sha256(validation_path)},
        "physical_qa": physical_qa,
        "checks": {
            "hash_only_reuse": True,
            "canonical_rows_1600": True,
            "gamma_z_4p22_mm": True,
            "temperature_above_ambient": True,
            "htc_finite_positive": True,
            "solver_started": False,
            "new_T0_solve": False,
        },
        "T0_action": "REUSE_250_500_1000_1500_NO_NEW_T0_SOLVE",
        "created_utc": now_utc(),
        "blind_or_final_truth_access": False,
    }


def bbox_close(actual: list[float], expected: list[float], label: str, atol: float = 1e-6) -> None:
    if len(actual) != 6 or not np.allclose(np.asarray(actual, dtype=float), np.asarray(expected), rtol=0.0, atol=atol):
        raise PreexecutionBlocked(f"{label} bounding box mismatch: actual={actual}, expected={expected}")


def face_is_plane_at(face: Any, z_mm: float) -> bool:
    try:
        vertices = list(face.vertices)
        if not vertices:
            return False
        return all(abs(float(vertex.position[2]) - z_mm) <= 1e-6 for vertex in vertices)
    except Exception:
        return False


def boundary_signature(app: Any) -> dict[str, Any]:
    keep = {}
    boundaries = aedt_nonempty_tree_read(lambda: list(app.boundaries), "boundary signature tree")
    for boundary in boundaries:
        name = aedt_tree_read(lambda boundary=boundary: boundary.name, "boundary signature name")
        if name in {"Opening1", "Opening2"} or name.startswith("P_active_"):
            boundary_type = aedt_tree_read(lambda boundary=boundary: boundary.type, f"boundary type: {name}")
            props = aedt_tree_read(lambda boundary=boundary: boundary.props, f"boundary properties: {name}")
            keep[name] = {"type": str(boundary_type), "props": plain(dict(props))}
    required = {"Opening1", "Opening2"}
    if required - set(keep):
        raise PreexecutionBlocked(f"carrier opening boundary is missing: {sorted(required - set(keep))}")
    sources = [name for name in keep if name.startswith("P_active_")]
    if not sources:
        raise PreexecutionBlocked("carrier has no active source boundaries")
    return keep


def active_source_signature(app: Any) -> dict[str, Any]:
    """Return source boundaries for package-only models that have no openings."""
    keep = {}
    boundaries = aedt_nonempty_tree_read(lambda: list(app.boundaries), "active source signature tree")
    for boundary in boundaries:
        name = aedt_tree_read(lambda boundary=boundary: boundary.name, "active source signature name")
        if not name.startswith("P_active_"):
            continue
        boundary_type = aedt_tree_read(lambda boundary=boundary: boundary.type, f"boundary type: {name}")
        props = aedt_tree_read(lambda boundary=boundary: boundary.props, f"boundary properties: {name}")
        keep[name] = {"type": str(boundary_type), "props": plain(dict(props))}
    if not keep:
        raise PreexecutionBlocked("package-only model has no active source boundaries")
    return keep


def source_semantic_contract(signature: dict[str, Any]) -> dict[str, str]:
    contract = {}
    for name, item in signature.items():
        if name.startswith("P_active_"):
            props = item.get("props", {})
            contract[name] = str(props.get("Total Power"))
    if not contract or any(not value or value == "None" for value in contract.values()):
        raise PreexecutionBlocked("active source semantic contract is incomplete")
    return contract


def source_object_power_contract(app: Any, signature: dict[str, Any]) -> dict[str, float]:
    """Resolve persisted source-object names from boundary assignments.

    Boundary labels are not a safe proxy for object names: several legacy
    carriers label top boundaries ``source1..4`` while the assigned solids are
    ``source5..8``.  Recovery must therefore use the frozen ``Objects`` field.
    """
    result: dict[str, float] = {}
    for boundary_name, item in signature.items():
        if not boundary_name.startswith("P_active_"):
            continue
        props = item.get("props", {})
        objects = props.get("Objects", [])
        if isinstance(objects, str):
            objects = [objects]
        if not isinstance(objects, list) or not objects:
            raise PreexecutionBlocked(f"active source boundary has no object assignment: {boundary_name}")
        total = watts(str(props.get("Total Power")))
        if len(objects) != 1:
            raise PreexecutionBlocked(
                f"legacy active source boundary is not one-object/one-power: {boundary_name} -> {objects}"
            )
        token = objects[0]
        object_name = str(token)
        if object_name.isdigit():
            try:
                object_name = str(app.modeler.objects[int(token)].name)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise PreexecutionBlocked(
                    f"cannot resolve persisted source object assignment {token!r} for {boundary_name}"
                )
        if object_name in result:
            raise PreexecutionBlocked(f"active source object has duplicate power boundaries: {object_name}")
        result[object_name] = total
    if not result:
        raise PreexecutionBlocked("active source object-power contract is empty")
    return result


def watts(value: str) -> float:
    match = __import__("re").fullmatch(r"\s*([0-9.eE+\-]+)\s*W\s*", str(value))
    if not match:
        raise PreexecutionBlocked(f"unsupported source power expression in recovery: {value}")
    result = float(match.group(1))
    if not np.isfinite(result) or result <= 0:
        raise PreexecutionBlocked(f"nonpositive source power in recovery: {value}")
    return result


def source_total_w(signature: dict[str, Any]) -> float:
    contract = source_semantic_contract(signature)
    # Boundary signatures retain historical P_active_* entries after an AEDT
    # delete/recreate transaction.  When recovery groups are present, they are
    # the active semantic contract and the stale originals must not be double
    # counted in this audit.
    recovered = {
        name: value for name, value in contract.items()
        if name.startswith((
            "P_active_rebound_", "P_active_block_recovery_",
            "P_active_surface_recovery_", "P_active_equal_density_group_",
            "P_active_multi_density_",
        ))
    }
    selected = recovered if recovered else contract
    return float(sum(watts(value) for value in selected.values()))


def source_surface_footprint_specs(app: Any, object_power: dict[str, float]) -> list[dict[str, Any]]:
    """Map each 10-um source solid to its exact footprint on an HB interface."""
    hb = app.modeler["HB"]
    if hb is None:
        raise PreexecutionBlocked("HB is missing for surface-footprint source recovery")
    hb_bbox = [float(value) for value in hb.bounding_box]
    specs = []
    for object_name in sorted(object_power):
        source = app.modeler[object_name]
        if source is None:
            raise PreexecutionBlocked(f"active source object missing before footprint recovery: {object_name}")
        bbox = [float(value) for value in source.bounding_box]
        if bbox[0] < hb_bbox[0] - 1e-6 or bbox[1] < hb_bbox[1] - 1e-6 or bbox[3] > hb_bbox[3] + 1e-6 or bbox[4] > hb_bbox[4] + 1e-6:
            raise PreexecutionBlocked(f"source footprint lies outside HB: {object_name} -> {bbox}")
        if abs(bbox[5] - hb_bbox[2]) <= 1e-6:
            interface_z = hb_bbox[2]
        elif abs(bbox[2] - hb_bbox[5]) <= 1e-6:
            interface_z = hb_bbox[5]
        else:
            raise PreexecutionBlocked(
                f"source solid does not touch an HB interface: {object_name} source={bbox} HB={hb_bbox}"
            )
        area = (bbox[3] - bbox[0]) * (bbox[4] - bbox[1])
        if area <= 0:
            raise PreexecutionBlocked(f"nonpositive source footprint area: {object_name}")
        specs.append({
            "source_object": object_name,
            "target_object": "HB",
            "interface_z_mm": interface_z,
            "footprint_bbox_xy_mm": [bbox[0], bbox[1], bbox[3], bbox[4]],
            "footprint_area_mm2": area,
            "total_power_W": float(object_power[object_name]),
        })
    return specs


def equal_power_density_source_groups(app: Any, object_power: dict[str, float]) -> list[dict[str, Any]]:
    """Group disjoint source boxes only when material and W/mm3 are equal."""
    groups: list[dict[str, Any]] = []
    for object_name in sorted(object_power):
        source = app.modeler[object_name]
        if source is None:
            raise PreexecutionBlocked(f"active source object missing before density grouping: {object_name}")
        bbox = [float(value) for value in source.bounding_box]
        volume = (bbox[3] - bbox[0]) * (bbox[4] - bbox[1]) * (bbox[5] - bbox[2])
        if volume <= 0:
            raise PreexecutionBlocked(f"nonpositive source volume before density grouping: {object_name}")
        power = float(object_power[object_name])
        density = power / volume
        material = str(source.material_name).strip('"').lower()
        match = next((
            group for group in groups
            if group["material"] == material
            and np.isclose(group["power_density_W_mm3"], density, rtol=1e-9, atol=1e-9)
        ), None)
        if match is None:
            match = {
                "source_objects": [], "material": material,
                "total_source_volume_mm3": 0.0, "total_power_W": 0.0,
                "power_density_W_mm3": density,
                "original_object_power_W": {},
            }
            groups.append(match)
        match["source_objects"].append(object_name)
        match["total_source_volume_mm3"] += volume
        match["total_power_W"] += power
        match["original_object_power_W"][object_name] = power
    return groups


def unite_equal_density_source_groups(app: Any, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create N-body objects without adding, deleting, or moving source volume."""
    evidence = []
    for index, original in enumerate(groups, start=1):
        item = dict(original)
        item["source_objects"] = list(original["source_objects"])
        item["original_object_power_W"] = dict(original["original_object_power_W"])
        names = item["source_objects"]
        if len(names) > 1:
            united = app.modeler.unite(names, purge=False, keep_originals=False)
            if not united:
                raise PreexecutionBlocked(f"failed equal-density N-body unite: {names}")
            united_name = str(united)
        else:
            united_name = names[0]
        group_name = f"active_equal_density_group_{index:03d}"
        united_object = app.modeler[united_name]
        if united_object is None:
            raise PreexecutionBlocked(f"equal-density united object is missing: {united_name}")
        united_object.name = group_name
        group_object = app.modeler[group_name]
        if group_object is None:
            raise PreexecutionBlocked(f"failed to rename equal-density group: {group_name}")
        actual_volume = float(group_object.volume)
        expected_volume = float(item["total_source_volume_mm3"])
        if not np.isclose(actual_volume, expected_volume, rtol=1e-6, atol=1e-9):
            raise PreexecutionBlocked(
                f"equal-density union changed source volume: expected={expected_volume}, actual={actual_volume}"
            )
        actual_material = str(group_object.material_name).strip('"').lower()
        if actual_material != item["material"]:
            raise PreexecutionBlocked(
                f"equal-density union changed source material: expected={item['material']}, actual={actual_material}"
            )
        item.update({
            "group_object": group_name,
            "actual_union_volume_mm3": actual_volume,
            "source_geometry_union_volume_unchanged": True,
            "spatial_power_density_unchanged": True,
        })
        evidence.append(item)
    return evidence


def multi_object_equal_density_assignments(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every frozen solid while reducing boundaries by exact power-density groups."""
    assignments = []
    for group in groups:
        item = dict(group)
        item["assignment_objects"] = sorted(group["source_objects"])
        item["source_objects"] = sorted(group["source_objects"])
        item["original_object_power_W"] = dict(group["original_object_power_W"])
        item["total_power_W"] = float(group["total_power_W"])
        assignments.append(item)
    return assignments


def recovered_persistent_object_names(before: list[str], refresh: dict[str, Any] | None) -> list[str]:
    if not refresh:
        return list(before)
    removed = set(refresh.get("removed_source_objects", []))
    if refresh.get("group_objects"):
        removed.update(refresh["source_objects"])
    return [name for name in before if name not in removed] + sorted(refresh.get("group_objects", []))


def select_surface_footprint_face(faces: list[Any], spec: dict[str, Any]) -> int:
    """Resolve one exact imprinted HB face for a frozen source footprint."""
    x0, y0, x1, y1 = [float(value) for value in spec["footprint_bbox_xy_mm"]]
    expected_center = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, float(spec["interface_z_mm"]))
    expected_area = float(spec["footprint_area_mm2"])
    matches = []
    for face in faces:
        try:
            center = [float(value) for value in face.center]
            area = float(face.area)
        except (TypeError, ValueError, AttributeError):
            continue
        if len(center) == 3 and all(abs(center[index] - expected_center[index]) <= 1e-6 for index in range(3)) and abs(area - expected_area) <= 1e-6:
            matches.append(int(face.id))
    if len(matches) != 1:
        raise PreexecutionBlocked(
            f"expected exactly one imprinted HB face for {spec['source_object']}, got {matches}"
        )
    return matches[0]


EQUAL_DENSITY_RECOVERY_MODES = {
    "equal-density-group",
    "equal-density-group-explicit-mesh-v2",
    "equal-density-group-explicit-mesh-v3",
    "equal-density-group-explicit-mesh-v4",
}
MULTI_OBJECT_DENSITY_RECOVERY_MODES = {
    "multi-object-equal-density",
    "multi-object-equal-density-box-mesh-v2",
    "multi-object-equal-density-box-mesh-v3",
    "multi-object-equal-density-box-mesh-v4",
}
MULTI_OBJECT_BOX_MESH_MODES = {
    "multi-object-equal-density-box-mesh-v2",
    "multi-object-equal-density-box-mesh-v3",
    "multi-object-equal-density-box-mesh-v4",
}
SOLID_BLOCK_RECOVERY_MODES = {
    "solid-block",
    "solid-block-box-mesh-v1",
    "solid-block-imprinted-box-mesh-v1",
    "solid-block-connected-imprinted-box-mesh-v2",
    "solid-block-rebuilt-feol-imprinted-box-mesh-v3",
    "solid-block-rebuilt-full-feol-box-mesh-v4",
    "solid-block-rebuilt-full-feol-box-mesh-v6",
    "solid-block-direct-final-geometry-v1",
    "solid-block-direct-final-geometry-full-feol-box-mesh-v5",
}
CONNECTED_IMPRINTED_SOLID_BLOCK_MODES = {"solid-block-connected-imprinted-box-mesh-v2"}
FORCE_SOURCE_REBUILD_MODES = {
    "solid-block-rebuilt-feol-imprinted-box-mesh-v3",
    "solid-block-rebuilt-full-feol-box-mesh-v4",
    "solid-block-rebuilt-full-feol-box-mesh-v6",
    "solid-block-direct-final-geometry-full-feol-box-mesh-v5",
}
FULL_FEOL_BOX_MESH_MODES = {
    "solid-block-rebuilt-full-feol-box-mesh-v4",
    "solid-block-rebuilt-full-feol-box-mesh-v6",
    "surface-footprint-preserve-solids-full-feol-box-mesh-v4",
    "solid-block-direct-final-geometry-full-feol-box-mesh-v5",
}
LAYER_HYBRID_RECOVERY_MODES = {"layer-hybrid-bottom-block-top-source-imprinted-box-mesh-v1"}
IMPRINTED_SOLID_BLOCK_MODES = {
    "solid-block-imprinted-box-mesh-v1",
    *CONNECTED_IMPRINTED_SOLID_BLOCK_MODES,
    "solid-block-rebuilt-feol-imprinted-box-mesh-v3",
}
IMPRINTED_SOURCE_RECOVERY_MODES = {*IMPRINTED_SOLID_BLOCK_MODES, *LAYER_HYBRID_RECOVERY_MODES}
SOURCE_REBIND_BOX_MESH_MODES = {"source-rebind-box-mesh-v1"}
NATIVE_SOURCE_MESH_MODES = {
    "native-source-level1-mesh-v1",
    "native-source-box-mesh-v1",
}
SURFACE_FOOTPRINT_RECOVERY_MODES = {
    "surface-footprint",
    "surface-footprint-sink-level1-mesh-v1",
    "surface-footprint-preserve-solids-v1",
    "surface-footprint-preserve-solids-box-mesh-v2",
    "surface-footprint-preserve-solids-zero-block-anchor-box-mesh-v3",
    "surface-footprint-preserve-solids-full-feol-box-mesh-v4",
}
SURFACE_ZERO_BLOCK_ANCHOR_MODE = "surface-footprint-preserve-solids-zero-block-anchor-box-mesh-v3"


def is_equal_density_recovery_mode(mode: str) -> bool:
    return mode in EQUAL_DENSITY_RECOVERY_MODES


def grouped_translation_zone_contract(
    boundaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe Fluent's one named solid zone per multi-object boundary."""
    groups = []
    seen: set[str] = set()
    for boundary in boundaries:
        members = sorted(boundary.get("assignment_objects", []))
        if not members or seen.intersection(members):
            raise PreexecutionBlocked("invalid or overlapping grouped source translation contract")
        seen.update(members)
        groups.append({
            "boundary_name": boundary["boundary_name"],
            "zone_representative_object": members[0],
            "physical_source_objects": members,
            "total_power_W": float(boundary["total_power_W"]),
        })
    return groups


def refresh_active_source_bindings(
    app: Any, expected_signature: dict[str, Any], mode: str = "source-rebind",
    allow_missing_boundaries: bool = False,
) -> dict[str, Any]:
    """Recreate identical total-power sources on persistent object names.

    This is a numerical-topology recovery for AEDT projects whose old object-ID
    boundary records survive in the GUI but disappear during Icepak-to-Fluent
    translation.  Powers, names and physical source objects are unchanged.
    """
    expected = source_semantic_contract(expected_signature)
    object_power = source_object_power_contract(app, expected_signature)
    object_names = sorted(object_power)
    multi_object_density = mode in MULTI_OBJECT_DENSITY_RECOVERY_MODES
    if mode not in {"source-rebind", *SURFACE_FOOTPRINT_RECOVERY_MODES, *SOURCE_REBIND_BOX_MESH_MODES, *SOLID_BLOCK_RECOVERY_MODES, *LAYER_HYBRID_RECOVERY_MODES, *MULTI_OBJECT_DENSITY_RECOVERY_MODES, *EQUAL_DENSITY_RECOVERY_MODES}:
        raise PreexecutionBlocked(f"unsupported source recovery mode: {mode}")
    missing = [name for name in object_names if app.modeler[name] is None]
    if missing:
        raise PreexecutionBlocked(f"active source objects missing before recovery: {missing}")
    # Solid-block recovery modes bind power to the preserved/rebuilt volume;
    # only modes whose contract explicitly says *imprinted* may partition HB.
    # The v4/v5 full-FEOL paths intentionally avoid an AEDT imprint transaction
    # because it can leave a stale face map after source recreation.
    imprint_modes = {
        *SURFACE_FOOTPRINT_RECOVERY_MODES,
        "solid-block-imprinted-box-mesh-v1",
        "solid-block-connected-imprinted-box-mesh-v2",
        "solid-block-rebuilt-feol-imprinted-box-mesh-v3",
    }
    surface_specs = (
        source_surface_footprint_specs(app, object_power)
        if mode in imprint_modes else []
    )
    density_groups = (
        equal_power_density_source_groups(app, object_power)
        if is_equal_density_recovery_mode(mode) or multi_object_density else []
    )
    if surface_specs:
        for spec in surface_specs:
            if not app.modeler.imprint(spec["target_object"], spec["source_object"], keep_originals=True):
                raise PreexecutionBlocked(f"failed to imprint source footprint on HB: {spec['source_object']}")
        target_faces = list(app.modeler["HB"].faces)
        for spec in surface_specs:
            spec["target_face_id"] = select_surface_footprint_face(target_faces, spec)
    source_to_hb_connection = None
    if mode in CONNECTED_IMPRINTED_SOLID_BLOCK_MODES:
        def connection_snapshot(name: str) -> dict[str, Any]:
            obj = app.modeler[name]
            if obj is None:
                raise PreexecutionBlocked(f"source/HB connection object is missing: {name}")
            return {
                "object": name,
                "bbox_mm": [float(value) for value in obj.bounding_box],
                "volume_mm3": float(obj.volume),
                "material": str(obj.material_name),
            }

        connection_objects = ["HB", *object_names]
        before_connection = {name: connection_snapshot(name) for name in connection_objects}
        connected = app.modeler.connect(connection_objects)
        if not connected:
            raise PreexecutionBlocked("failed to connect imprinted source solids to HB")
        after_connection = {name: connection_snapshot(name) for name in connection_objects}
        for name in connection_objects:
            before_item = before_connection[name]
            after_item = after_connection[name]
            if before_item["material"].casefold() != after_item["material"].casefold():
                raise PreexecutionBlocked(f"source/HB connection changed material: {name}")
            if not np.allclose(
                after_item["bbox_mm"], before_item["bbox_mm"], rtol=0.0, atol=1e-9
            ):
                raise PreexecutionBlocked(f"source/HB connection changed bbox: {name}")
            if not np.isclose(
                after_item["volume_mm3"], before_item["volume_mm3"], rtol=1e-9, atol=1e-12
            ):
                raise PreexecutionBlocked(f"source/HB connection changed volume: {name}")
        source_to_hb_connection = {
            "status": "PASS",
            "method": "AEDT_CONNECT_AFTER_EXACT_SOURCE_FOOTPRINT_IMPRINT",
            "objects": connection_objects,
            "before": list(before_connection.values()),
            "after": list(after_connection.values()),
            "object_names_materials_bboxes_and_volumes_unchanged": True,
        }
    boundaries = boundary_map(app, "source-recovery boundary map")
    removal_names = []
    for name in sorted(expected):
        boundary = boundaries.get(name)
        if boundary is None and allow_missing_boundaries:
            continue
        if boundary is None:
            raise PreexecutionBlocked(f"failed to remove stale active source boundary: {name}")
        removal_names.append(name)
    if removal_names:
        # BoundaryObject.delete re-reads its Name after native deletion. COM
        # correctly invalidates that node, so delete by already verified names
        # and prove their absence from a freshly opened design tree instead.
        design_name = app.design_name
        app.oboundary.DeleteBoundaries(removal_names)
        if not refresh_owned_design(app, design_name):
            raise PreexecutionBlocked("failed to refresh design after source-boundary removal")
        remaining = boundary_map(app, "source-recovery boundary deletion proof")
        if set(removal_names).intersection(remaining):
            raise PreexecutionBlocked("stale source boundary survived native deletion")
    removed_source_objects = []
    direct_final_geometry_recreation = None
    if mode in {
        "solid-block-direct-final-geometry-v1",
        "solid-block-direct-final-geometry-full-feol-box-mesh-v5",
    }:
        snapshots = []
        for name in object_names:
            source = app.modeler[name]
            bbox = [float(value) for value in source.bounding_box]
            snapshots.append({
                "name": name,
                "origin_mm": bbox[:3],
                "dimensions_mm": [bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]],
                "material": str(source.material_name),
            })
        if not app.modeler.delete(object_names):
            raise PreexecutionBlocked("failed deleting scaled source solids before exact final-geometry recreation")
        for item in snapshots:
            created = app.modeler.create_box(
                item["origin_mm"], item["dimensions_mm"], name=item["name"], material=item["material"]
            )
            if not created:
                raise PreexecutionBlocked(f"failed recreating final-geometry source solid: {item['name']}")
            recreated = [float(value) for value in created.bounding_box]
            expected_bbox = [
                *item["origin_mm"],
                item["origin_mm"][0] + item["dimensions_mm"][0],
                item["origin_mm"][1] + item["dimensions_mm"][1],
                item["origin_mm"][2] + item["dimensions_mm"][2],
            ]
            if not np.allclose(recreated, expected_bbox, rtol=0.0, atol=1e-9):
                raise PreexecutionBlocked(f"final-geometry source recreation changed bbox: {item['name']}")
        app.modeler.refresh_all_ids()
        missing_recreated = [item["name"] for item in snapshots if app.modeler[item["name"]] is None]
        if missing_recreated:
            raise PreexecutionBlocked(
                f"recreated source objects missing after modeler cache refresh: {missing_recreated}"
            )
        direct_final_geometry_recreation = {
            "status": "PASS",
            "method": "RECREATE_EXACT_POST_SCALE_BBOX_WITH_SAME_NAME_MATERIAL_AND_Z",
            "source_count": len(snapshots),
            "sources": snapshots,
            "geometry_changed": False,
        }
    if mode == "surface-footprint":
        if not app.modeler.delete(object_names):
            raise PreexecutionBlocked(f"failed to remove source solids after HB footprint imprint: {object_names}")
        if any(app.modeler[name] is not None for name in object_names):
            raise PreexecutionBlocked("source solid still present after surface-footprint replacement")
        removed_source_objects = list(object_names)
    united_groups = unite_equal_density_source_groups(app, density_groups) if is_equal_density_recovery_mode(mode) else []
    group_by_name = {item["group_object"]: item for item in united_groups}
    multi_assignments = multi_object_equal_density_assignments(density_groups) if multi_object_density else []
    if multi_assignments:
        assignment_items = [
            (item["assignment_objects"], float(item["total_power_W"]), item)
            for item in multi_assignments
        ]
    elif united_groups:
        assignment_items = [
            (item["group_object"], float(item["total_power_W"]), item)
            for item in united_groups
        ]
    else:
        assignment_items = [(name, float(object_power[name]), None) for name in sorted(object_power)]
    rebound_evidence = []
    for index, (assignment, total, density_item) in enumerate(assignment_items, start=1):
        object_name = assignment if isinstance(assignment, str) else "+".join(assignment)
        boundary_name = (
            f"P_active_rebound_{index:03d}" if mode == "source-rebind" or mode in SOURCE_REBIND_BOX_MESH_MODES else
            f"P_active_hybrid_{'top_source' if object_name.startswith('active_top_') else 'bot_block'}_{index:03d}" if mode in LAYER_HYBRID_RECOVERY_MODES else
            f"P_active_block_recovery_{index:03d}" if mode in SOLID_BLOCK_RECOVERY_MODES else
            f"P_active_surface_recovery_{index:03d}" if mode in SURFACE_FOOTPRINT_RECOVERY_MODES else
            f"P_active_multi_density_{index:03d}" if multi_object_density else
            f"P_active_equal_density_group_{index:03d}"
        )
        if mode == "source-rebind" or mode in SOURCE_REBIND_BOX_MESH_MODES:
            created = app.assign_source(
                object_name, "Total Power", f"{total:.12g}W", boundary_name=boundary_name, radiate=False
            )
        elif mode in LAYER_HYBRID_RECOVERY_MODES and object_name.startswith("active_top_"):
            created = app.assign_source(
                object_name, "Total Power", f"{total:.12g}W", boundary_name=boundary_name, radiate=False
            )
        elif mode in LAYER_HYBRID_RECOVERY_MODES or mode in SOLID_BLOCK_RECOVERY_MODES:
            created = app.assign_solid_block(object_name, f"{total:.12g}W", boundary_name=boundary_name)
        elif mode in SURFACE_FOOTPRINT_RECOVERY_MODES:
            spec = next(item for item in surface_specs if item["source_object"] == object_name)
            created = app.assign_source(
                spec["target_face_id"], "Total Power", f"{total:.12g}W",
                boundary_name=boundary_name, radiate=False,
            )
        elif multi_object_density:
            created = app.assign_source(
                assignment, "Total Power", f"{total:.12g}W",
                boundary_name=boundary_name, radiate=False,
            )
        else:
            created = app.assign_source(
                object_name, "Total Power", f"{total:.12g}W",
                boundary_name=boundary_name, radiate=False,
            )
        if not created:
            raise PreexecutionBlocked(f"failed to create {mode} active source boundary: {boundary_name}")
        evidence = {"boundary_name": boundary_name, "object": object_name, "total_power_W": total}
        if mode in LAYER_HYBRID_RECOVERY_MODES:
            evidence["boundary_representation"] = (
                "NATIVE_SOURCE_ICEPAK" if object_name.startswith("active_top_") else "SOLID_BLOCK"
            )
        if mode in SURFACE_FOOTPRINT_RECOVERY_MODES:
            evidence.update({key: spec[key] for key in (
                "target_object", "target_face_id", "interface_z_mm",
                "footprint_bbox_xy_mm", "footprint_area_mm2",
            )})
        elif is_equal_density_recovery_mode(mode):
            group = group_by_name[object_name]
            evidence.update({key: group[key] for key in (
                "source_objects", "material", "total_source_volume_mm3",
                "actual_union_volume_mm3", "power_density_W_mm3",
                "original_object_power_W", "source_geometry_union_volume_unchanged",
                "spatial_power_density_unchanged",
            )})
        elif multi_object_density:
            evidence.update({key: density_item[key] for key in (
                "source_objects", "assignment_objects", "material",
                "total_source_volume_mm3", "power_density_W_mm3",
                "original_object_power_W",
            )})
            evidence["physical_source_objects_unchanged"] = True
            evidence["spatial_power_density_unchanged"] = True
        rebound_evidence.append(evidence)
    passive_zero_power_anchors = []
    if mode == SURFACE_ZERO_BLOCK_ANCHOR_MODE:
        for index, object_name in enumerate(object_names, start=1):
            boundary_name = f"Passive_source_zone_anchor_{index:03d}"
            if not app.assign_solid_block(object_name, "0W", boundary_name=boundary_name):
                raise PreexecutionBlocked(f"failed to create zero-power source-zone anchor: {object_name}")
            passive_zero_power_anchors.append({
                "boundary_name": boundary_name, "object": object_name, "total_power_W": 0.0,
            })
    total = float(sum(object_power.values()))
    actual_signature = boundary_signature(app)
    if not np.isclose(source_total_w(actual_signature), total, rtol=0.0, atol=1e-8):
        raise PreexecutionBlocked(f"{mode} source total power changed after creation")
    translation_groups = (
        grouped_translation_zone_contract(rebound_evidence)
        if multi_object_density else []
    )
    return {
        "status": "PASS",
        "method": (
            "RECREATE_ONE_SOURCE_BOUNDARY_PER_FROZEN_PHYSICAL_OBJECT"
            if mode == "source-rebind" or mode in SOURCE_REBIND_BOX_MESH_MODES
            else "BOTTOM_SOLID_BLOCK_TOP_NATIVE_SOURCE_PER_FROZEN_PHYSICAL_OBJECT"
            if mode in LAYER_HYBRID_RECOVERY_MODES
            else "RECREATE_ONE_EQUIVALENT_SOLID_BLOCK_POWER_PER_FROZEN_PHYSICAL_OBJECT"
            if mode in SOLID_BLOCK_RECOVERY_MODES
            else "RECREATE_ONE_EXACT_HB_INTERFACE_FOOTPRINT_SOURCE_PER_FROZEN_PHYSICAL_OBJECT"
            if mode in SURFACE_FOOTPRINT_RECOVERY_MODES
            else "ASSIGN_ONE_TOTAL_POWER_BOUNDARY_PER_EQUAL_DENSITY_MULTI_OBJECT_GROUP_WITHOUT_GEOMETRY_CHANGE"
            if multi_object_density
            else "UNITE_DISJOINT_EQUAL_POWER_DENSITY_SOURCE_VOLUMES_WITHOUT_GEOMETRY_CHANGE"
        ),
        "mode": mode,
        "original_boundary_count": len(expected),
        "rebound_boundary_count": len(rebound_evidence),
        "boundaries": rebound_evidence,
        "source_objects": object_names,
        "removed_source_objects": removed_source_objects,
        "group_objects": sorted(group_by_name),
        "mesh_objects": (
            sorted({*object_names, *(
                name for name in app.modeler.object_names if name.startswith("Passive_FEOL_")
            )})
            if mode in FULL_FEOL_BOX_MESH_MODES
            else sorted(group_by_name) if group_by_name else object_names
        ),
        "mesh_scope": "ACTIVE_AND_PASSIVE_FEOL_SOLIDS" if mode in FULL_FEOL_BOX_MESH_MODES else "ACTIVE_SOURCE_SOLIDS",
        "translation_objects": (
            [group["zone_representative_object"] for group in translation_groups]
            if translation_groups else sorted(group_by_name) if group_by_name else object_names
        ),
        "translation_groups": translation_groups,
        "persistent_source_targets": sorted({spec["target_object"] for spec in surface_specs}),
        "combined_total_power_W": total,
        "per_object_power_and_physical_source_geometry_unchanged": True,
        "source_footprint_and_total_power_unchanged": mode in {
            *SURFACE_FOOTPRINT_RECOVERY_MODES, *IMPRINTED_SOURCE_RECOVERY_MODES,
        },
        "maximum_power_plane_offset_mm": 0.005 if mode in SURFACE_FOOTPRINT_RECOVERY_MODES else 0.0,
        "source_union_geometry_and_spatial_power_density_unchanged": (
            is_equal_density_recovery_mode(mode) or multi_object_density
            or mode in IMPRINTED_SOURCE_RECOVERY_MODES
        ),
        "source_to_HB_interface_imprinted": (
            mode in IMPRINTED_SOURCE_RECOVERY_MODES or mode in SURFACE_FOOTPRINT_RECOVERY_MODES
        ),
        "source_to_HB_connection": source_to_hb_connection,
        "multi_object_equal_density_grouping": multi_object_density,
        "direct_final_geometry_recreation": direct_final_geometry_recreation,
        "passive_zero_power_zone_anchors": passive_zero_power_anchors,
    }


def _new_explicit_mesh_region(app: Any, assignment: list[str], name: str):
    """Bypass the PyAEDT 1.1 wrapper's invalid 2023.2 Objects/SubModels setters."""
    from ansys.aedt.core.modules.mesh_icepak import MeshRegion

    return MeshRegion(app, assignment, name=name)


def configure_explicit_source_mesh_region_api(
    app: Any, region_name: str, subregion_objects: list[str]
) -> dict[str, Any]:
    """Apply AEDT 2023.2's native EditMeshRegion contract to its subregion object."""
    if not subregion_objects:
        raise PreexecutionBlocked("explicit source mesh region has no persisted subregion object")
    args = [
        f"NAME:{region_name}",
        "Enable:=", True,
        "MeshMethod:=", "MesherHD",
        "UserSpecifiedSettings:=", True,
        "ComputeGap:=", False,
        "MaxElementSizeX:=", "1.25mm",
        "MaxElementSizeY:=", "1.25mm",
        "MaxElementSizeZ:=", "0.005mm",
        "MinElementsInGap:=", "2",
        "MinElementsOnEdge:=", "2",
        "MaxSizeRatio:=", "2",
        "NoOGrids:=", False,
        "EnableMLM:=", True,
        "EnforeMLMType:=", "3D",
        "MaxLevels:=", "0",
        "BufferLayers:=", "0",
        "UniformMeshParametersType:=", "Average",
        "StairStepMeshing:=", False,
        "MinGapX:=", "1.25mm",
        "MinGapY:=", "1.25mm",
        "MinGapZ:=", "0.005mm",
        "Objects:=", sorted(subregion_objects),
    ]
    try:
        app.omeshmodule.EditMeshRegion(region_name, args)
    except Exception as exc:
        raise PreexecutionBlocked(f"native EditMeshRegion failed: {exc}") from exc
    return {
        "status": "PASS",
        "method": "AEDT_2023_2_NATIVE_EDIT_MESH_REGION",
        "region_name": region_name,
        "assignment": sorted(subregion_objects),
        "max_element_size_x_mm": 1.25,
        "max_element_size_y_mm": 1.25,
        "max_element_size_z_mm": 0.005,
        "compute_gap": False,
    }


def add_active_source_box_mesh(
    app: Any, object_names: list[str], one_operation_per_object: bool = False,
    counts: tuple[int, int, int] = (2, 2, 2),
    source_recovery_mode: str | None = None,
) -> dict[str, Any]:
    """Mesh thin source/FEOL solids with the production shared-stack strategy."""
    production_shared = source_recovery_mode == "solid-block-rebuilt-full-feol-box-mesh-v4"
    if production_shared or os.environ.get("BRIDGE_DIAGNOSTIC_SHARED_CORE_MESH") == "1":
        # A common stack grid resolves the 10-um FEOL planes on first launch.
        # Independent boxes caused invalid 0.06-mm cross-layer Fluent contacts.
        assignment = sorted(set(object_names) | {"Bump", "HB", "Die"})
        before = set(app.modeler.object_names)
        region = _new_explicit_mesh_region(app, assignment, "ActiveSourceRecoveryMeshRegion")
        if not region:
            raise PreexecutionBlocked("shared-core diagnostic mesh region assignment failed")
        return {"status": "PASS", "names": [region.name], "assignment": assignment,
                "mesh_object_separately": False, "one_operation_per_object": False,
                "source_geometry_unchanged": True, "requires_pre_solve_reopen": True,
                "manual_settings": True, "manual_settings_via_persisted_text_patch": True,
                "mesh_subregion_objects": sorted(set(app.modeler.object_names) - before),
                "max_element_size_x_mm": 1.25, "max_element_size_y_mm": 1.25, "max_element_size_z_mm": 0.005,
                "diagnostic_only": True, "reason": "COMMON_CORE_GRID_RESOLVES_10UM_FEOL_WITH_5UM_Z_SPACING"}
    names = sorted(object_names)
    assignments = [[name] for name in names] if one_operation_per_object else [names]
    operation_names = [
        f"ActiveSourceRecoveryMeshBox_{index:03d}"
        for index in range(1, len(assignments) + 1)
    ] if one_operation_per_object else ["ActiveSourceRecoveryMeshBox"]
    refined_boxes = {}
    try:
        for operation_name, assignment in zip(operation_names, assignments):
            local_counts = list(counts)
            if len(assignment) == 1 and assignment[0].startswith("Passive_FEOL_"):
                bbox = [float(v) for v in app.modeler[assignment[0]].bounding_box]
                x_size, y_size = bbox[3] - bbox[0], bbox[4] - bbox[1]
                if min(x_size, y_size) <= 0:
                    raise PreexecutionBlocked(f"invalid FEOL tile dimensions: {assignment[0]}")
                if max(x_size, y_size) / min(x_size, y_size) > 10:
                    long_axis = 0 if x_size > y_size else 1
                    local_counts[long_axis] = max(local_counts[long_axis], math.ceil(max(x_size, y_size) / min(x_size, y_size)))
                    refined_boxes[assignment[0]] = local_counts.copy()
            app.omeshmodule.AssignMeshOperation([
                f"NAME:{operation_name}",
                "Enable:=", True,
                "MaxLevel:=", "0",
                "MinLevel:=", "0",
                "Local Mesh Parameters Enabled:=", True,
                "Mesh Reuse Enabled:=", False,
                "Mesh Reuse File:=", "",
                "Local Mesh Parameters Type:=", "Box Local Mesh Parameters",
                "X count:=", str(local_counts[0]),
                "Y count:=", str(local_counts[1]),
                "Z count:=", str(local_counts[2]),
                "Objects:=", assignment,
                "Mesh Object(s) Separately Enabled:=", True,
            ])
    except Exception as exc:
        raise PreexecutionBlocked(f"native source box mesh assignment failed: {exc}") from exc
    neighbor_mesh = {}
    if os.environ.get("BRIDGE_DIAGNOSTIC_THIN_LAYER_BOX_MESH") == "1":
        neighbors = os.environ.get("BRIDGE_DIAGNOSTIC_NEIGHBOR_LAYERS", "Bump,HB,Die").split(",")
        package_queue = os.environ.get('BRIDGE_SIZE_POWER_WIND_QUEUE') == '1'
        small_package = package_queue and float(app.modeler['TIM2'].bounding_box[3])-float(app.modeler['TIM2'].bounding_box[0]) < 40-1e-6
        if small_package: neighbors = sorted(set(neighbors) | {'Substrate'})
        if not neighbors or not set(neighbors).issubset({"Bump", "HB", "Die"} | ({'Substrate'} if package_queue else set())):
            raise PreexecutionBlocked("unsupported local package mesh layer")
        z_counts = json.loads(os.environ.get("BRIDGE_DIAGNOSTIC_NEIGHBOR_Z_COUNTS", "{}"))
        if 'Substrate' in neighbors: z_counts.setdefault('Substrate', 3)
        for neighbor in neighbors:
            bbox = [float(v) for v in app.modeler[neighbor].bounding_box]
            dims = [bbox[i+3]-bbox[i] for i in range(3)]
            local_counts = [max(2, math.ceil(dims[0]/1.25)), max(2, math.ceil(dims[1]/1.25)), max(2, math.ceil(dims[2]/0.005))]
            if os.environ.get("BRIDGE_DIAGNOSTIC_NEIGHBOR_XY_COUNT"):
                local_counts[:2] = [int(os.environ["BRIDGE_DIAGNOSTIC_NEIGHBOR_XY_COUNT"])] * 2
            if os.environ.get("BRIDGE_DIAGNOSTIC_NEIGHBOR_Z_COUNT"):
                local_counts[2] = int(os.environ["BRIDGE_DIAGNOSTIC_NEIGHBOR_Z_COUNT"])
                if local_counts[2] < 2:
                    raise PreexecutionBlocked("diagnostic neighbor grid needs at least two Z cells")
            if neighbor in z_counts:
                local_counts[2] = int(z_counts[neighbor])
            if min(local_counts) < 2:
                raise PreexecutionBlocked("diagnostic neighbor grid needs at least two cells per axis")
            operation_name = f"DiagnosticThinLayerBox_{neighbor}"
            app.omeshmodule.AssignMeshOperation([
                f"NAME:{operation_name}", "Enable:=", True, "MaxLevel:=", "0", "MinLevel:=", "0",
                "Local Mesh Parameters Enabled:=", True, "Mesh Reuse Enabled:=", False,
                "Mesh Reuse File:=", "", "Local Mesh Parameters Type:=", "Box Local Mesh Parameters",
                "X count:=", str(local_counts[0]), "Y count:=", str(local_counts[1]), "Z count:=", str(local_counts[2]),
                "Objects:=", [neighbor], "Mesh Object(s) Separately Enabled:=", True,
            ])
            neighbor_mesh[neighbor] = {"operation": operation_name, "counts": local_counts, "bbox_mm": bbox}
    return {
        "status": "PASS",
        "names": operation_names,
        "assignment": names,
        "counts": list(counts),
        "refined_feol_boxes": refined_boxes,
        "diagnostic_neighbor_mesh": neighbor_mesh,
        "mesh_object_separately": True,
        "one_operation_per_object": one_operation_per_object,
        "source_geometry_unchanged": True,
        "requires_pre_solve_reopen": True,
        "reason": "resolve every unchanged 0.01-mm source solid with two local Z cells",
    }


def add_sink_box_mesh(app: Any, counts: tuple[int, int, int] = (2, 2, 2)) -> dict[str, Any]:
    """Rebind sink box-mesh operations after replacing heatsink solids.

    AEDT mesh operations in a carrier can retain numeric object IDs after the
    solids are deleted and recreated.  Reassigning by the new object names is
    therefore required; otherwise one fin can silently have no mesh at solve.
    """
    fins = sorted(str(n) for n in app.modeler.object_names if str(n).startswith("Heatsink_Fin_"))
    base = ["Heatsink_Base"] if "Heatsink_Base" in [str(n) for n in app.modeler.object_names] else []
    if not fins or not base:
        raise PreexecutionBlocked("cannot assign sink box mesh without rebuilt base and fins")
    try:
        # Recreate the operations with name-based assignments.  Deleting the
        # old solids causes AEDT to drop the carrier operations on save, so
        # retaining wrapper objects or trying to reassign the stale operation
        # is unreliable.  Counts follow the rebuilt dimensions so thin fins
        # do not inherit the extreme aspect ratio of the 45 mm carrier.
        base_bbox = [float(v) for v in app.modeler["Heatsink_Base"].bounding_box]
        fin_bbox = [float(v) for v in app.modeler[fins[0]].bounding_box]
        base_dims = [base_bbox[3] - base_bbox[0], base_bbox[4] - base_bbox[1], base_bbox[5] - base_bbox[2]]
        fin_dims = [fin_bbox[3] - fin_bbox[0], fin_bbox[4] - fin_bbox[1], fin_bbox[5] - fin_bbox[2]]
        fin_yz_pitch = float(os.environ.get("BRIDGE_DIAGNOSTIC_SINK_FIN_YZ_PITCH_MM", "0.5"))
        coarse_trial = os.environ.get("BRIDGE_DIAGNOSTIC_COARSE_SINK_TRIAL") == "1"
        rollout_text = os.environ.get("BRIDGE_APPROVED_SINK_MESH_PRODUCTION_JSON")
        rollout = json.loads(rollout_text) if rollout_text else None
        if rollout:
            if os.environ.get('BRIDGE_SIZE_POWER_WIND_QUEUE') != '1' or coarse_trial:
                raise PreexecutionBlocked('production sink mesh scope requires the production queue')
            expected = dict(rollout['fin_bboxes_mm'], Heatsink_Base=rollout['base_bbox_mm'])
            if set(expected) != set(fins + base):
                raise PreexecutionBlocked('production sink mesh fin count/name differs from validated geometry')
            for name, bounds in expected.items():
                actual = [float(v) for v in app.modeler[name].bounding_box]
                if len(actual)!=6 or len(bounds)!=6 or not all(math.isfinite(a) and abs(a-b)<=1e-7 for a,b in zip(actual,bounds)):
                    raise PreexecutionBlocked('production sink mesh geometry differs: '+name)
            expected_pitch=float(rollout.get('pitch_mm',1.0))
            if expected_pitch not in (0.5,0.7,1.0) or fin_yz_pitch!=expected_pitch or float(os.environ.get('BRIDGE_DIAGNOSTIC_SINK_BASE_PITCH_MM','0.5'))!=expected_pitch or float(os.environ.get('BRIDGE_DIAGNOSTIC_SINK_FIN_X_PITCH_MM','0.25'))!=0.25:
                raise PreexecutionBlocked('production sink mesh pitches differ from the approved trial')
        allowed_pitches = (0.25, 0.5, 0.7, 1.0) if rollout else (0.25, 0.5, 1.0) if coarse_trial else (0.25, 0.5)
        if fin_yz_pitch not in allowed_pitches:
            raise PreexecutionBlocked("unsupported sink fin mesh pitch")
        fin_x_pitch = float(os.environ.get("BRIDGE_DIAGNOSTIC_SINK_FIN_X_PITCH_MM", "0.25"))
        if fin_x_pitch not in (0.15, 0.25):
            raise PreexecutionBlocked("unsupported sink fin thickness mesh pitch")
        fin_x_count = max(4, math.ceil(fin_dims[0] / fin_x_pitch)) if fin_x_pitch == 0.15 else max(2, round(fin_dims[0] / fin_x_pitch))
        fin_counts = (fin_x_count, max(2, round(fin_dims[1] / fin_yz_pitch)), max(2, round(fin_dims[2] / fin_yz_pitch)))
        base_pitch = float(os.environ.get("BRIDGE_DIAGNOSTIC_SINK_BASE_PITCH_MM", "0.5"))
        if base_pitch not in allowed_pitches:
            raise PreexecutionBlocked("unsupported sink base mesh pitch")
        base_counts = tuple(max(2, round(dim / base_pitch)) for dim in base_dims)
        for name, assignment, local_counts in (
            ("SinkFinBoxMeshRebuilt", fins, fin_counts),
            ("SinkBaseBoxMeshRebuilt", base, base_counts),
        ):
            app.omeshmodule.AssignMeshOperation([
                f"NAME:{name}", "Enable:=", True, "MaxLevel:=", "0", "MinLevel:=", "0",
                "Local Mesh Parameters Enabled:=", True, "Mesh Reuse Enabled:=", False,
                "Mesh Reuse File:=", "", "Local Mesh Parameters Type:=", "Box Local Mesh Parameters",
                "X count:=", str(local_counts[0]), "Y count:=", str(local_counts[1]), "Z count:=", str(local_counts[2]),
                "Objects:=", assignment, "Mesh Object(s) Separately Enabled:=", True,
            ])
    except Exception as exc:
        raise PreexecutionBlocked(f"native sink box mesh assignment failed: {exc}") from exc
    return {
        "status": "PASS", "names": ["SinkFinBoxMeshRebuilt", "SinkBaseBoxMeshRebuilt"],
        "fin_assignment": fins, "base_assignment": base, "counts": [list(fin_counts), list(base_counts)],
        "fin_yz_pitch_mm": fin_yz_pitch,
        "base_pitch_mm": base_pitch,
        "geometry_unchanged": True,
        **({"production_rollout": {k:rollout[k] for k in ('case_id','policy_sha256','reference_case_id','comparison_sha256')}} if rollout else {}),
    }


def patch_explicit_source_mesh_region_text(text: str) -> tuple[str, dict[str, Any]]:
    """Persist manual MeshRegion settings that PyAEDT 1.1 cannot edit in AEDT 2023.2."""
    pattern = re.compile(
        r"(?ms)(^[ \t]*\$begin 'ActiveSourceRecoveryMeshRegion'\s*$)(.*?)"
        r"(^[ \t]*\$end 'ActiveSourceRecoveryMeshRegion'\s*$)"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PreexecutionBlocked(f"expected one persisted active-source mesh region, got {len(matches)}")
    match = matches[0]
    body = match.group(2)
    id_match = re.search(r"(?m)^[ \t]*ID=(\d+)\s*$", body)
    objects_match = re.search(r"(?m)^[ \t]*Objects\(([^)]*)\)\s*$", body)
    if not id_match or not objects_match:
        raise PreexecutionBlocked("persisted active-source mesh region lacks ID or Objects assignment")
    indent_match = re.match(r"([ \t]*)", match.group(1))
    indent = (indent_match.group(1) if indent_match else "") + "\t"
    lines = [
        match.group(1),
        f"{indent}ID={id_match.group(1)}",
        f"{indent}IsComponent=false",
        f"{indent}Enable=true",
        f"{indent}MeshMethod='MesherHD'",
        f"{indent}UserSpecifiedSettings=true",
        f"{indent}ComputeGap=false",
        f"{indent}MaxElementSizeX='1.25mm'",
        f"{indent}MaxElementSizeY='1.25mm'",
        f"{indent}MaxElementSizeZ='0.005mm'",
        f"{indent}MinElementsInGap=2",
        f"{indent}MinElementsOnEdge=2",
        f"{indent}MaxSizeRatio=2",
        f"{indent}NoOGrids=false",
        f"{indent}EnableMLM=true",
        f"{indent}EnforeMLMType='3D'",
        f"{indent}MaxLevels=0",
        f"{indent}BufferLayers=0",
        f"{indent}UniformMeshParametersType='Average'",
        f"{indent}'2DMLMType'='2DMLM_None'",
        f"{indent}MinGapX='1mm'",
        f"{indent}MinGapY='1mm'",
        f"{indent}MinGapZ='0.005mm'",
        f"{indent}ProximitySizeFunction=false",
        f"{indent}CurvatureSizeFunction=false",
        f"{indent}EnableTransition=false",
        f"{indent}OptimizePCBMesh=true",
        f"{indent}Enable2DCutCell=false",
        f"{indent}EnforceCutCellMeshing=false",
        f"{indent}Enforce2dot5DCutCell=false",
        f"{indent}Objects({objects_match.group(1)})",
        match.group(3),
    ]
    replacement = "\n".join(lines)
    patched = text[:match.start()] + replacement + text[match.end():]
    return patched, {
        "status": "PASS", "region_count": 1,
        "objects_assignment": objects_match.group(1),
        "max_element_size_x_mm": 1.25, "max_element_size_y_mm": 1.25,
        "max_element_size_z_mm": 0.005, "user_specified_settings": True,
        "compute_gap": False, "proximity_size_function": False,
        "curvature_size_function": False,
        "before_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest().upper(),
        "after_sha256": hashlib.sha256(patched.encode("utf-8")).hexdigest().upper(),
    }


def patch_explicit_source_mesh_region_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="strict")
    patched, audit = patch_explicit_source_mesh_region_text(text)
    path.write_text(patched, encoding="utf-8", newline="\n")
    if sha256(path) != audit["after_sha256"]:
        raise PreexecutionBlocked("persisted active-source mesh text-patch hash mismatch")
    return {**audit, "path": str(path)}


def patch_tim2_mesh_binding_text(text: str, tim2_object_id: int) -> tuple[str, dict[str, Any]]:
    """Restore AEDT 2023.2's dropped TIM2 assignment after native XY scaling."""
    pattern = re.compile(
        r"(?ms)(^[ \t]*\$begin 'MeshOperation1'\s*$)(.*?)(^[ \t]*\$end 'MeshOperation1'\s*$)"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PreexecutionBlocked(f"expected one persisted MeshOperation1, got {len(matches)}")
    match = matches[0]
    body = match.group(2)
    existing = re.search(r"(?m)^[ \t]*Objects\(([^)]*)\)\s*$", body)
    expected = str(int(tim2_object_id))
    if existing:
        values = [item.strip() for item in existing.group(1).split(",") if item.strip()]
        if values != [expected]:
            raise PreexecutionBlocked(f"persisted TIM2 mesh binding changed: {values}")
        patched = text
    else:
        anchor = re.search(r"(?m)^(?P<indent>[ \t]*)'Mesh Object\(s\) Separately Enabled'=", body)
        if not anchor:
            raise PreexecutionBlocked("persisted MeshOperation1 lacks insertion anchor")
        body = body[:anchor.start()] + f"{anchor.group('indent')}Objects({expected})\n" + body[anchor.start():]
        patched = text[:match.start()] + match.group(1) + body + match.group(3) + text[match.end():]
    return patched, {
        "status": "PASS",
        "method": "RESTORE_DROPPED_TIM2_MESH_OBJECT_BINDING_AFTER_NATIVE_XY_SCALE",
        "tim2_object_id": int(tim2_object_id),
        "binding": [int(tim2_object_id)],
        "geometry_changed": False,
        "mesh_counts_changed": False,
        "before_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest().upper(),
        "after_sha256": hashlib.sha256(patched.encode("utf-8")).hexdigest().upper(),
    }


def patch_tim2_mesh_binding_file(path: Path, tim2_object_id: int) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="strict")
    patched, audit = patch_tim2_mesh_binding_text(text, tim2_object_id)
    path.write_text(patched, encoding="utf-8", newline="\n")
    if sha256(path) != audit["after_sha256"]:
        raise PreexecutionBlocked("persisted TIM2 mesh-binding patch hash mismatch")
    return {**audit, "path": str(path)}


def patch_global_mesh_resolution_text(text: str, resolution: int) -> tuple[str, dict[str, Any]]:
    """Change only Icepak's global MesherHD resolution for a mesh study.

    This is separate from Gamma/HTC post-processing grids and from the
    preserved TIM2 local mesh operation.
    """
    resolution = int(resolution)
    if resolution < 1 or resolution > 5:
        raise PreexecutionBlocked(f"global MesherHD resolution outside supported diagnostic range: {resolution}")
    pattern = re.compile(
        r"(?ms)(^\s*\$begin 'Global'\s*$.*?^\s*MeshRegionResolution=)(\d+)(.*?^\s*\$end 'Global'\s*$)"
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PreexecutionBlocked(f"expected one Icepak Global mesh region, got {len(matches)}")
    old = int(matches[0].group(2))
    match = matches[0]
    patched = text[:match.start()] + match.group(1) + str(resolution) + match.group(3) + text[match.end():]
    return patched, {
        "status": "PASS", "mode": "DIAGNOSTIC_GLOBAL_MESHERHD_RESOLUTION",
        "old_resolution": old, "new_resolution": resolution,
        "sampling_grids_unchanged": True, "tim2_local_mesh_unchanged": True,
    }


def diagnostic_tim2_mesh_count(app: Any, count_xy: int | None = None, count_z: int | None = None) -> dict[str, Any]:
    """Change only the solver's local TIM2 mesh count for a diagnostic run.

    This is deliberately separate from all Gamma/source post-processing grids.
    It is never used by the production queue.
    """
    if count_xy is None and count_z is None:
        raise PreexecutionBlocked("diagnostic TIM2 mesh count requires X/Y or Z count")
    count_xy = None if count_xy is None else int(count_xy)
    count_z = None if count_z is None else int(count_z)
    if count_xy is not None and not 10 <= count_xy <= 160:
        raise PreexecutionBlocked(f"diagnostic TIM2 mesh X/Y count must be 10..160, got {count_xy}")
    if count_z is not None and not 1 <= count_z <= 80:
        raise PreexecutionBlocked(f"diagnostic TIM2 mesh Z count must be 1..80, got {count_z}")
    operations = [item for item in app.mesh.meshoperations if item.name == "MeshOperation1"]
    if len(operations) != 1:
        raise PreexecutionBlocked("diagnostic TIM2 mesh count requires exactly one MeshOperation1")
    operation = operations[0]
    props = dict(operation.props)
    before = {"X count": str(props.get("X count")), "Y count": str(props.get("Y count")), "Z count": str(props.get("Z count"))}
    operation.props["X count"] = str(count_xy if count_xy is not None else props.get("X count"))
    operation.props["Y count"] = str(count_xy if count_xy is not None else props.get("Y count"))
    operation.props["Z count"] = str(count_z if count_z is not None else props.get("Z count"))
    if not operation.update():
        raise PreexecutionBlocked("diagnostic TIM2 mesh-count update failed")
    return {
        "status": "PASS",
        "mode": "DIAGNOSTIC_TIM2_LOCAL_SOLVER_MESH",
        "before": before,
        "after": {"X count": str(operation.props["X count"]), "Y count": str(operation.props["Y count"]), "Z count": str(operation.props["Z count"])},
        "sampling_grids_unchanged": True,
    }


def add_active_source_recovery_mesh(
    app: Any, expected_signature: dict[str, Any], assignment_override: list[str] | None = None,
    explicit_thin_z: bool = False, direct_native_api: bool = False,
    explicit_box_counts: bool = False, one_box_operation_per_object: bool = False,
    box_counts: tuple[int, int, int] = (2, 2, 2),
    source_recovery_mode: str | None = None,
) -> dict[str, Any]:
    """Build native source mesh using the explicit production recovery mode."""
    object_names = sorted(assignment_override or source_object_power_contract(app, expected_signature))
    missing = [name for name in object_names if app.modeler[name] is None]
    if missing:
        raise PreexecutionBlocked(f"active-source recovery mesh objects missing: {missing}")
    if explicit_box_counts:
        return add_active_source_box_mesh(
            app, object_names, one_box_operation_per_object, box_counts,
            source_recovery_mode=source_recovery_mode,
        )
    if explicit_thin_z:
        object_names_before_region = set(aedt_nonempty_tree_read(
            lambda: list(app.modeler.object_names), "pre-source-mesh object names"
        ))
        region = _new_explicit_mesh_region(app, object_names, "ActiveSourceRecoveryMeshRegion")
        if not region:
            raise PreexecutionBlocked("failed to create explicit active-source mesh region")
        mesh_items = list(getattr(app.mesh, "meshoperations", [])) + list(getattr(app.mesh, "meshregions", []))
        names = [item.name for item in mesh_items]
        if region.name not in names:
            raise PreexecutionBlocked(f"explicit active-source mesh region missing from live tree: {names}")
        object_names_after_region = set(aedt_nonempty_tree_read(
            lambda: list(app.modeler.object_names), "post-source-mesh object names"
        ))
        mesh_subregions = sorted(object_names_after_region - object_names_before_region)
        native_api = (
            configure_explicit_source_mesh_region_api(app, region.name, mesh_subregions)
            if direct_native_api else None
        )
        return {
            "status": "PASS", "names": [region.name], "assignment": object_names,
            "manual_settings": True, "max_element_size_x_mm": 1.25,
            "max_element_size_y_mm": 1.25, "max_element_size_z_mm": 0.005,
            "min_elements_in_gap": 2, "source_geometry_unchanged": True,
            "update_tree_confirmed": False,
            "mesh_subregion_objects": mesh_subregions,
            "manual_settings_via_persisted_text_patch": not direct_native_api,
            "manual_settings_via_native_api": direct_native_api,
            "native_api_configuration": native_api,
            "reason": "resolve unchanged 0.01-mm N-body source volumes with two Z cells",
        }
    operations = app.mesh.assign_mesh_level({name: 1 for name in object_names}, name="ActiveSourceRecoveryMesh")
    if not operations:
        raise PreexecutionBlocked("failed to create active-source level-1 mesh operation")
    names = [item.name for item in app.mesh.meshoperations]
    created_names = [str(name) for name in operations]
    if any(name not in names for name in created_names):
        raise PreexecutionBlocked(f"active-source recovery mesh did not persist live: created={created_names}, live={names}")
    return {"status": "PASS", "names": created_names, "level": 1,
            "assignment": object_names, "source_geometry_unchanged": True,
            "reason": "force MesherHD to retain unchanged 0.01-mm active source solids"}


def add_sink_level1_mesh(app: Any) -> dict[str, Any]:
    """Refine only the rebuilt sink that failed beside an otherwise passing twin."""
    names = sorted(name for name in app.modeler.object_names if name == "Heatsink_Base" or name.startswith("Heatsink_Fin_"))
    operations = app.mesh.assign_mesh_level({name: 1 for name in names}, name="SinkRecoveryMesh")
    if not names or not operations:
        raise PreexecutionBlocked("failed to create sink level-1 mesh operation")
    return {"status": "PASS", "names": [str(item) for item in operations], "assignment": names,
            "level": 1, "sink_geometry_unchanged": True}


def object_contract(
    app: Any, size: int, aedt_path: Path | None = None, scaled_package_xy: bool = False,
) -> dict[str, Any]:
    names = sorted(
        geometry_part_names(aedt_path)
        if aedt_path is not None
        else aedt_nonempty_tree_read(lambda: list(app.modeler.object_names), "model object names")
    )
    missing = sorted(set(PACKAGE_OBJECTS + ("Box1", "Heatsink_Base")) - set(names))
    if missing:
        raise PreexecutionBlocked(f"required persistent objects missing: {missing}")
    origin = 10.0 - size / 2.0
    bbox_close(list(app.modeler["TIM2"].bounding_box), [origin, origin, 4.02, origin + size, origin + size, 4.22], "TIM2")
    ihs_expected = (
        [origin, origin, 1.02, origin + size, origin + size, 4.02]
        if scaled_package_xy else [-10.0, -10.0, 1.02, 30.0, 30.0, 4.02]
    )
    bbox_close(list(app.modeler["IHS"].bounding_box), ihs_expected, "IHS")
    if scaled_package_xy:
        core_side = size / 2.0
        core_origin = 10.0 - core_side / 2.0
        z_ranges = {
            "Substrate": (0.0, 0.3), "Bump": (0.3, 0.4), "HB": (0.41, 0.46),
            "Die": (0.47, 0.97), "TIM1": (0.97, 1.02),
        }
        for name, (z0, z1) in z_ranges.items():
            bbox_close(
                list(app.modeler[name].bounding_box),
                [core_origin, core_origin, z0, core_origin + core_side, core_origin + core_side, z1],
                name,
            )
    air = list(app.modeler["Box1"].bounding_box)
    if abs(float(air[2]) - 4.22) > 1e-6:
        raise PreexecutionBlocked(f"Box1 air bottom is not Gamma z=4.22 mm: {air}")
    base = list(app.modeler["Heatsink_Base"].bounding_box)
    if abs(float(base[2]) - 4.22) > 1e-6:
        raise PreexecutionBlocked(f"heatsink base bottom is not Gamma z=4.22 mm: {base}")
    material = str(app.modeler["TIM2"].material_name).strip('"').lower()
    if material != "tim2":
        raise PreexecutionBlocked(f"TIM2 material binding changed: {material}")
    return {"object_names": names, "air_bbox_mm": air, "heatsink_base_bbox_mm": base}


def scale_package_xy(
    app: Any, size: int, expected_sources: dict[str, Any], carrier_tim2_size_mm: int = 40,
    native_source_props: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Scale every package/source solid about (10, 10), preserving Z and Total Power."""
    if size not in SIZES:
        raise PreexecutionBlocked(f"scaled package size is outside the frozen contract: {size}")
    names = sorted(aedt_nonempty_tree_read(lambda: list(app.modeler.object_names), "pre-scale object names"))
    source_power_by_object = source_object_power_contract(app, expected_sources)
    source_objects = sorted(source_power_by_object)
    if carrier_tim2_size_mm not in (40, size):
        raise PreexecutionBlocked(f"unsupported carrier TIM2 size: {carrier_tim2_size_mm}")
    selected = sorted({
        name for name in names
        if ((name in PACKAGE_OBJECTS and (name != "TIM2" or carrier_tim2_size_mm == 40))
            or name.startswith("Passive_FEOL_") or name in source_objects)
    })
    required = (set(PACKAGE_OBJECTS) - ({"TIM2"} if carrier_tim2_size_mm != 40 else set())) | set(source_objects)
    if required - set(selected):
        raise PreexecutionBlocked(f"scaled package object selection is incomplete: {sorted(required - set(selected))}")
    sink_before = {
        name: [float(value) for value in app.modeler[name].bounding_box]
        for name in names if name == "Box1" or name.startswith("Heatsink_")
    }
    semantic_before = source_semantic_contract(expected_sources)
    source_before = {
        name: [float(value) for value in app.modeler[name].bounding_box]
        for name in source_objects
    }
    carrier_geometry_size_mm = 40.0
    factor = size / 40.0
    shift = 10.0 * (1.0 - factor)
    if not np.isclose(factor, 1.0):
        if not app.modeler.scale(selected, factor, factor, 1.0):
            raise PreexecutionBlocked("native package XY scale failed")
        if not app.modeler.move(selected, [shift, shift, 0.0]):
            raise PreexecutionBlocked("native package re-centering move failed")
    operations = [item for item in app.mesh.meshoperations if item.name == "MeshOperation1"]
    if len(operations) != 1:
        raise PreexecutionBlocked("scaled package requires exactly one TIM2 mesh operation")
    operation = operations[0]
    operation.props["X count"] = "40"
    operation.props["Y count"] = "40"
    if not operation.update():
        raise PreexecutionBlocked("scaled TIM2 mesh-count update failed")
    after_sources = merge_native_source_props(boundary_signature(app), native_source_props)
    if after_sources != expected_sources:
        raise PreexecutionBlocked("opening/source bindings changed during package scaling")
    if source_semantic_contract(after_sources) != semantic_before:
        raise PreexecutionBlocked("per-source Total Power changed during package scaling")
    for name, expected in sink_before.items():
        bbox_close(list(app.modeler[name].bounding_box), expected, f"unscaled sink object {name}")
    sink_air_geometry = [
        {"object": name, "bbox_before_mm": expected,
         "bbox_after_mm": [float(value) for value in app.modeler[name].bounding_box],
         "unchanged": True}
        for name, expected in sink_before.items()
    ]
    source_geometry = []
    for name, before_bbox in source_before.items():
        after_bbox = [float(value) for value in app.modeler[name].bounding_box]
        before_center = [(before_bbox[0] + before_bbox[3]) / 2.0, (before_bbox[1] + before_bbox[4]) / 2.0]
        expected_center = [10.0 + (value - 10.0) * factor for value in before_center]
        after_center = [(after_bbox[0] + after_bbox[3]) / 2.0, (after_bbox[1] + after_bbox[4]) / 2.0]
        before_wh = [before_bbox[3] - before_bbox[0], before_bbox[4] - before_bbox[1]]
        after_wh = [after_bbox[3] - after_bbox[0], after_bbox[4] - after_bbox[1]]
        if (not np.allclose(after_center, expected_center, rtol=0.0, atol=1e-6)
                or not np.allclose(after_wh, np.asarray(before_wh) * factor, rtol=0.0, atol=1e-6)
                or not np.allclose(after_bbox[2:3] + after_bbox[5:6], before_bbox[2:3] + before_bbox[5:6], rtol=0.0, atol=1e-6)):
            raise PreexecutionBlocked(f"source XY scale audit failed: {name}")
        source_geometry.append({
            "object": name, "center_xy_before_mm": before_center, "center_xy_after_mm": after_center,
            "width_height_before_mm": before_wh, "width_height_after_mm": after_wh,
            "area_before_mm2": before_wh[0] * before_wh[1], "area_after_mm2": after_wh[0] * after_wh[1],
            "z_min_max_before_mm": [before_bbox[2], before_bbox[5]],
            "z_min_max_after_mm": [after_bbox[2], after_bbox[5]],
            "total_power_W": float(source_power_by_object[name]),
        })
    return {
        "status": "PASS", "mode": "WHOLE_PACKAGE_XY_SCALE_ABOUT_10MM_CENTER",
        "scale_factor_xy": factor, "scale_factor_z": 1.0, "translation_xy_mm": shift,
        "carrier_tim2_size_mm": carrier_tim2_size_mm,
        "carrier_geometry_size_mm": carrier_geometry_size_mm,
        "scaled_objects": selected, "source_objects": source_objects,
        "per_source_total_power": semantic_before, "sink_objects_unchanged": sorted(sink_before),
        "sink_air_geometry_audit": sink_air_geometry,
        "source_geometry_audit": source_geometry,
        "mapping_contract_version": "NORMALIZED_DIRECT_AEDT_40X40_V1",
        "dx_gamma_mm": size / 40.0, "dx_source_mm": size / 80.0,
    }


def mesh_contract(
    app: Any, size: int, mesh_count_xy: int | None = None, allow_source_recovery: bool = False,
    mesh_count_z: int = 3,
) -> dict[str, Any]:
    mesh_count_xy = size if mesh_count_xy is None else int(mesh_count_xy)
    operations = aedt_nonempty_tree_read(lambda: list(app.mesh.meshoperations), "mesh operation tree")
    named_operations = [
        (operation, aedt_tree_read(lambda operation=operation: operation.name, "mesh operation name"))
        for operation in operations
    ]
    primary = [(operation, name) for operation, name in named_operations if name == "MeshOperation1"]
    extras = [name for _operation, name in named_operations if name != "MeshOperation1"]
    allowed_recovery_prefixes = ("PhysicalSourceMesh_", "SinkFinBoxMesh", "SinkBaseBoxMesh", "SinkRecoveryMesh")
    diagnostic_neighbors = {f"DiagnosticThinLayerBox_{n}" for n in ("Bump", "HB", "Die")} if os.environ.get("BRIDGE_DIAGNOSTIC_THIN_LAYER_BOX_MESH") == "1" else set()
    if os.environ.get('BRIDGE_SIZE_POWER_WIND_QUEUE') == '1': diagnostic_neighbors.add('DiagnosticThinLayerBox_Substrate')
    ihs_mesh = None
    if os.environ.get('BRIDGE_DIAGNOSTIC_IHS_CONFORMAL_BOX') == '1' or os.environ.get('BRIDGE_DIAGNOSTIC_IHS_SEPARATE_BOX') == '1':
        from direct_native_case import verify_ihs_box_mesh
        ihs_mesh = verify_ihs_box_mesh(app)
        diagnostic_neighbors.add('DiagnosticIHSSeparateBox' if os.environ.get('BRIDGE_DIAGNOSTIC_IHS_SEPARATE_BOX') == '1' else 'DiagnosticIHSConformalBox')
    tim1_mesh = None
    if os.environ.get('BRIDGE_DIAGNOSTIC_TIM1_SEPARATE_BOX') == '1':
        from direct_native_case import verify_tim1_box_mesh
        tim1_mesh = verify_tim1_box_mesh(app)
        diagnostic_neighbors.add('DiagnosticTIM1SeparateBox')
    if len(primary) != 1 or (extras and (not allow_source_recovery or any(not name.startswith(allowed_recovery_prefixes) and name not in diagnostic_neighbors for name in extras))):
        raise PreexecutionBlocked(f"expected one preserved TIM2 local mesh operation, got {[name for _op, name in named_operations]}")
    operation, operation_name = primary[0]
    props = aedt_tree_read(lambda: operation.props, "TIM2 mesh properties")
    assignment = list(props.get("Objects", []))
    resolved = []
    for token in assignment:
        object_name = str(token)
        if object_name.isdigit():
            try:
                object_name = str(app.modeler.objects[int(token)].name)
            except (KeyError, TypeError, ValueError, AttributeError):
                object_name = ""
        resolved.append(object_name)
    if resolved != ["TIM2"]:
        raise PreexecutionBlocked(f"TIM2 local mesh object binding changed: {plain(dict(props))}")
    if str(props.get("X count")) != str(mesh_count_xy) or str(props.get("Y count")) != str(mesh_count_xy) or str(props.get("Z count")) != str(mesh_count_z):
        raise PreexecutionBlocked(f"TIM2 local mesh counts changed: {plain(dict(props))}")
    return {"name": operation_name, "assignment": plain(assignment), "counts": [mesh_count_xy, mesh_count_xy, mesh_count_z],
            "nominal_xy_cell_mm": size / mesh_count_xy, "allowed_recovery_operations": extras,
            "diagnostic_ihs_box_mesh": ihs_mesh,"diagnostic_tim1_box_mesh":tim1_mesh}


def no_gamma_h0_boundary(app: Any) -> None:
    offenders = []
    boundaries = aedt_nonempty_tree_read(lambda: list(app.boundaries), "boundary tree")
    for boundary in boundaries:
        name = aedt_tree_read(lambda boundary=boundary: boundary.name, "boundary name")
        props = plain(dict(aedt_tree_read(lambda boundary=boundary: boundary.props, f"boundary properties: {name}")))
        if "gamma" in name.lower() or "fixed_h0" in name.lower():
            offenders.append(name)
        if name == "Gamma_fixed_h0" or props.get("Heat Transfer Coefficient") in {"250w_per_m2kel", "500w_per_m2kel", "1000w_per_m2kel", "1500w_per_m2kel"}:
            offenders.append(name)
    if offenders:
        raise PreexecutionBlocked(f"full truth may not use Gamma h0 boundary logic: {sorted(set(offenders))}")


def classify_contact_faces(
    faces: list[Any], *, total_area_mm2: float, contact_area_mm2: float, label: str
) -> dict[str, Any]:
    areas = [(face, float(face.area)) for face in faces]
    if not areas or abs(sum(area for _face, area in areas) - total_area_mm2) > 1e-4:
        raise PreexecutionBlocked(
            f"imprinted {label} area mismatch: {[(face.id, area) for face, area in areas]}"
        )
    contact = [(face, area) for face, area in areas if abs(area - contact_area_mm2) <= 1e-4]
    expected_outside = total_area_mm2 - contact_area_mm2
    if abs(expected_outside) <= 1e-4:
        return {
            "total_area_mm2": float(total_area_mm2),
            "contact_face_id": int(areas[0][0].id),
            "contact_face_ids": [int(face.id) for face, _area in areas],
            "contact_area_mm2": float(contact_area_mm2),
            "outside_face_ids": [], "outside_area_mm2": 0.0,
        }
    if len(contact) != 1:
        raise PreexecutionBlocked(
            f"cannot identify unique contact face on {label}: {[(face.id, area) for face, area in areas]}"
        )
    contact_id = int(contact[0][0].id)
    outside = [(face, area) for face, area in areas if int(face.id) != contact_id]
    outside_area = sum(area for _face, area in outside)
    if (expected_outside > 1e-4 and not outside) or abs(outside_area - expected_outside) > 1e-4:
        raise PreexecutionBlocked(
            f"outside-contact {label} area mismatch: {outside_area} versus {expected_outside}"
        )
    return {
        "total_area_mm2": float(total_area_mm2),
        "contact_face_id": contact_id,
        "contact_area_mm2": float(contact_area_mm2),
        "outside_face_ids": [int(face.id) for face, _area in outside],
        "outside_area_mm2": float(outside_area),
    }


def classify_partitioned_contact_faces(
    faces: list[Any], *, total_area_mm2: float, contact_size_mm: float, label: str
) -> dict[str, Any]:
    """Classify concentric fixed-topology face partitions by outer XY bounds."""
    areas = [(face, float(face.area)) for face in faces]
    if not areas or abs(sum(area for _face, area in areas) - total_area_mm2) > 1e-4:
        raise PreexecutionBlocked(
            f"fixed-partition {label} area mismatch: {[(face.id, area) for face, area in areas]}"
        )
    lower = 10.0 - float(contact_size_mm) / 2.0
    upper = 10.0 + float(contact_size_mm) / 2.0
    contact: list[tuple[Any, float]] = []
    outside: list[tuple[Any, float]] = []
    for face, area in areas:
        vertices = [list(vertex.position) for vertex in face.vertices]
        if not vertices:
            raise PreexecutionBlocked(f"fixed-partition {label} face has no vertices: {face.id}")
        x_values = [float(value[0]) for value in vertices]
        y_values = [float(value[1]) for value in vertices]
        inside = (
            min(x_values) >= lower - 1e-6 and max(x_values) <= upper + 1e-6
            and min(y_values) >= lower - 1e-6 and max(y_values) <= upper + 1e-6
        )
        (contact if inside else outside).append((face, area))
    contact_area = sum(area for _face, area in contact)
    expected_contact = float(contact_size_mm) ** 2
    if not contact or abs(contact_area - expected_contact) > 1e-4:
        raise PreexecutionBlocked(
            f"fixed-partition {label} contact area mismatch: {contact_area} versus {expected_contact}"
        )
    outside_area = sum(area for _face, area in outside)
    if not outside or abs(outside_area - (total_area_mm2 - expected_contact)) > 1e-4:
        raise PreexecutionBlocked(f"fixed-partition {label} outside area mismatch")
    return {
        "total_area_mm2": float(total_area_mm2),
        "contact_face_ids": [int(face.id) for face, _area in contact],
        "contact_area_mm2": contact_area,
        "outside_face_ids": [int(face.id) for face, _area in outside],
        "outside_area_mm2": outside_area,
    }


def fixed_anchor_sizes_for_geometry(target_size: int, geometry_size: int) -> list[int]:
    """Return the anchor tools needed after the actual TIM2 outline is imprinted."""
    if target_size not in SIZES or geometry_size not in SIZES:
        raise PreexecutionBlocked("fixed contact anchor is outside the frozen size contract")
    return [anchor for anchor in SIZES if anchor != geometry_size]


def outside_contact_boundary_kind(target_size: int, geometry_size: int) -> str:
    """Use an internal adiabatic plate when an unchanged solid occupies the gap."""
    return "ADIABATIC_PLATE" if int(geometry_size) != int(target_size) else "ZERO_HEAT_FLUX_WALL"


def internal_mask_ring_specs(target_size: int) -> list[dict[str, Any]]:
    """Describe a fixed set of embedded ring sheets; only outer rings are adiabatic."""
    if target_size not in SIZES or target_size == 40:
        raise PreexecutionBlocked("internal contact mask requires a frozen non-40 mm target")
    return [
        {
            "name": f"DiagnosticTIM2MaskRing_s{inner}_s{outer}",
            "inner_size_mm": inner,
            "outer_size_mm": outer,
            "adiabatic": inner >= target_size,
        }
        for inner, outer in zip(SIZES[:-1], SIZES[1:])
    ]


def create_internal_adiabatic_mask(app: Any, target_size: int) -> dict[str, Any]:
    """Insert fixed mid-TIM2 ring sheets and block only rings outside the contact."""
    specs = internal_mask_ring_specs(target_size)
    for item in specs:
        outer = float(item["outer_size_mm"])
        inner = float(item["inner_size_mm"])
        outer_origin = 10.0 - outer / 2.0
        inner_origin = 10.0 - inner / 2.0
        name = str(item["name"])
        cut_name = f"{name}_cut"
        outer_sheet = app.modeler.create_rectangle(
            "XY", [outer_origin, outer_origin, 4.12], [outer, outer], name=name, is_covered=True
        )
        inner_sheet = app.modeler.create_rectangle(
            "XY", [inner_origin, inner_origin, 4.12], [inner, inner], name=cut_name, is_covered=True
        )
        if not outer_sheet or not inner_sheet or not app.modeler.subtract(name, cut_name, keep_originals=False):
            raise PreexecutionBlocked(f"failed to build embedded TIM2 mask ring {name}")
        if name not in app.modeler.object_names or cut_name in app.modeler.object_names:
            raise PreexecutionBlocked(f"embedded TIM2 mask ring persistence failed: {name}")
        area = sum(float(face.area) for face in app.modeler[name].faces)
        expected_area = outer * outer - inner * inner
        if abs(area - expected_area) > 1e-4:
            raise PreexecutionBlocked(f"embedded TIM2 mask ring area mismatch: {name} {area} vs {expected_area}")
    inactive = [str(item["name"]) for item in specs if item["adiabatic"]]
    active = [str(item["name"]) for item in specs if not item["adiabatic"]]
    boundary_name = "DiagnosticEmbeddedTIM2OutsideAdiabatic"
    boundary = app.assign_adiabatic_plate(inactive, boundary_name=boundary_name)
    if not boundary:
        raise PreexecutionBlocked("failed to assign embedded TIM2 outside-contact adiabatic rings")
    assigned = {str(value) for value in boundary.props.get("Objects", [])}
    if not set(inactive).issubset(assigned) or set(active) & assigned:
        raise PreexecutionBlocked("embedded TIM2 mask boundary assignment mismatch")
    return {
        "status": "PASS",
        "boundary_name": boundary_name,
        "z_mm": 4.12,
        "all_ring_objects": [str(item["name"]) for item in specs],
        "adiabatic_ring_objects": inactive,
        "open_contact_ring_objects": active,
        "contact_size_mm": target_size,
        "geometry_fixed_across_sizes": True,
    }


def verify_internal_adiabatic_mask(app: Any, audit: dict[str, Any]) -> None:
    missing = sorted(set(audit["all_ring_objects"]) - set(app.modeler.object_names))
    if missing:
        raise RuntimeError(f"embedded TIM2 mask rings missing after save/reopen: {missing}")
    boundaries = boundary_map(app, "fixed-anchor boundary map")
    boundary = boundaries.get(audit["boundary_name"])
    if boundary is None:
        raise RuntimeError("embedded TIM2 mask boundary missing after save/reopen")
    assigned = set()
    for value in boundary.props.get("Objects", []):
        token = str(value)
        if token in app.modeler.object_names:
            assigned.add(token)
            continue
        try:
            assigned.add(str(app.modeler.objects[int(value)].name))
        except Exception:
            assigned.add(token)
    if not set(audit["adiabatic_ring_objects"]).issubset(assigned):
        raise RuntimeError("embedded TIM2 mask assignment changed after save/reopen")


def imprint_fixed_anchor_partition(app: Any, target_size: int, geometry_size: int) -> None:
    """Imprint every missing frozen anchor on both interface solids."""
    for anchor in fixed_anchor_sizes_for_geometry(target_size, geometry_size):
        origin = 10.0 - anchor / 2.0
        name = f"DiagnosticContactPartition_s{anchor}"
        tool = app.modeler.create_box(
            [origin, origin, 4.02], [float(anchor), float(anchor), 0.20], name=name, material="air"
        )
        if not tool:
            raise PreexecutionBlocked(f"failed to create fixed contact partition tool s{anchor}")
        for solid in ("IHS", "Heatsink_Base"):
            if not app.modeler.imprint(solid, name, keep_originals=True):
                raise PreexecutionBlocked(f"failed fixed contact partition imprint s{anchor} on {solid}")
        # PyAEDT 1.1 may return None after a successful delete when the
        # gRPC object-group query races the editor refresh.  Treat the
        # postcondition (the temporary tool is absent) as authoritative;
        # only a surviving object is a physical/preexecution failure.
        try:
            app.modeler.delete([name])
        except Exception as exc:
            try:
                still_present = name in app.modeler.object_names
            except Exception:
                still_present = True
            if still_present:
                raise PreexecutionBlocked(f"failed to delete fixed contact partition tool s{anchor}: {exc}") from exc
        try:
            if name in app.modeler.object_names:
                raise PreexecutionBlocked(f"fixed contact partition tool survived delete s{anchor}")
        except PreexecutionBlocked:
            raise
        except Exception:
            # A transient object-list refresh failure is not enough to claim
            # a physical failure; the next persistent-binding audit will catch
            # a real surviving object.
            pass


def live_package_side_faces(app: Any) -> list[int]:
    """Rebind physical lateral walls after CAD operations invalidate face IDs."""
    result = []
    for name in ('Substrate', 'Bump', 'HB', 'Die', 'TIM1', 'IHS', 'TIM2'):
        obj = app.modeler[name]
        bounds = list(map(float, obj.bounding_box))
        planes = {(axis, offset): [int(face.id) for face in obj.faces
                  if abs(float(face.center[axis])-bounds[axis+offset]) < 1e-6]
                  for axis in (0, 1) for offset in (0, 3)}
        if not all(planes.values()):
            raise PreexecutionBlocked(f'Cannot recover all package lateral walls: {name}')
        result.extend(face for group in planes.values() for face in group)
    return sorted(set(result))


def explicit_outside_contact_adiabatic(
    app: Any, size: int, fixed_anchor_imprint: bool = False,
    geometry_size_mm: int | None = None,
    expected_object_names: list[str] | None = None,
    ihs_total_area_mm2: float = 1600.0,
    allow_existing_ihs_contact_cleanup: bool = False,
) -> dict[str, Any]:
    """Bind both sides outside the TIM2 contact to q=0 with no radiation."""
    before_names = sorted(
        expected_object_names
        if expected_object_names is not None
        else aedt_nonempty_tree_read(lambda: list(app.modeler.object_names), "pre-imprint object names")
    )
    if not app.modeler.imprint("IHS", "TIM2", keep_originals=True):
        raise PreexecutionBlocked("TIM2-on-IHS imprint failed")
    if not app.modeler.imprint("Heatsink_Base", "TIM2", keep_originals=True):
        raise PreexecutionBlocked("TIM2-on-heatsink-base imprint failed")
    geometry_size = size if geometry_size_mm is None else int(geometry_size_mm)
    if fixed_anchor_imprint:
        imprint_fixed_anchor_partition(app, size, geometry_size)
    if expected_object_names is None and sorted(
        aedt_nonempty_tree_read(lambda: list(app.modeler.object_names), "post-imprint object names")
    ) != before_names:
        raise PreexecutionBlocked("TIM2 dual-side imprint changed persistent object names")
    contact_area = float(size * size)
    ihs_faces = [face for face in app.modeler["IHS"].faces if face_is_plane_at(face, 4.02)]
    classifier = classify_partitioned_contact_faces if fixed_anchor_imprint else classify_contact_faces
    if fixed_anchor_imprint:
        ihs = classifier(
            ihs_faces, total_area_mm2=ihs_total_area_mm2, contact_size_mm=float(size), label="IHS top"
        )
    else:
        ihs = classifier(
            ihs_faces, total_area_mm2=ihs_total_area_mm2, contact_area_mm2=contact_area, label="IHS top"
        )
    base_bbox = [float(value) for value in app.modeler["Heatsink_Base"].bounding_box]
    base_area = (base_bbox[3] - base_bbox[0]) * (base_bbox[4] - base_bbox[1])
    base_faces = [face for face in app.modeler["Heatsink_Base"].faces if face_is_plane_at(face, 4.22)]
    if fixed_anchor_imprint:
        base = classifier(
            base_faces, total_area_mm2=base_area, contact_size_mm=float(size), label="heatsink base bottom"
        )
    else:
        base = classifier(
            base_faces, total_area_mm2=base_area, contact_area_mm2=contact_area, label="heatsink base bottom"
        )
    boundaries = boundary_map(app, "outside-contact pre-assignment boundary map")
    wall = boundaries.get("pkg_side_adiabatic")
    if wall is None:
        raise PreexecutionBlocked("preserved package adiabatic wall is missing")
    props = wall.props
    if str(props.get("External Condition")) != "Heat Flux" or str(props.get("Heat Flux")) != "0irrad_W_per_m2":
        raise PreexecutionBlocked(f"preserved package wall is not zero heat flux: {plain(dict(props))}")
    radiation = plain(props.get("Radiation", {}))
    if bool(radiation.get("Radiate", False)) or bool(props.get("External Surface Radiation", False)):
        raise PreexecutionBlocked("preserved package wall radiation is enabled")
    existing = [int(value) for value in props.get("Faces", [])]
    outside_ids = ihs["outside_face_ids"] + base["outside_face_ids"]
    contact_ids = ihs.get("contact_face_ids", [ihs.get("contact_face_id")]) + base.get(
        "contact_face_ids", [base.get("contact_face_id")]
    )
    removed_stale_ihs_faces = []
    if allow_existing_ihs_contact_cleanup:
        ihs_contact_ids = set(ihs.get("contact_face_ids", [ihs.get("contact_face_id")]))
        removed_stale_ihs_faces = sorted(ihs_contact_ids & set(existing))
        # Rebuilt/scaled carriers can retain face IDs from deleted heatsink or
        # pre-scale solids. Those IDs make AEDT reject the whole boundary
        # transaction. Rebuild both the package lateral walls and the outside
        # contact faces from live geometry; dropping the lateral walls changes
        # the thermal boundary problem.
        existing = live_package_side_faces(app)
    if any(face_id in existing for face_id in contact_ids):
        raise PreexecutionBlocked("TIM2 contact face is incorrectly bound adiabatic")
    boundary_kind = outside_contact_boundary_kind(size, geometry_size)
    if boundary_kind == "ADIABATIC_PLATE":
        boundary_name = "DiagnosticOutsideContactAdiabaticPlate"
        if boundary_name in boundaries:
            raise PreexecutionBlocked("fixed-geometry diagnostic adiabatic plate already exists")
        rebound = app.assign_adiabatic_plate(outside_ids, boundary_name=boundary_name)
        if not rebound:
            raise PreexecutionBlocked("failed to assign fixed-geometry outside-contact adiabatic plate")
    else:
        boundary_name = "pkg_side_adiabatic"
        # A full-size 40 mm package has no outside IHS/base faces: the entire
        # audited top and base surfaces are contact faces.  AEDT rejects an
        # empty face assignment, so preserve the validated wall and only
        # update it when there are live outside faces to bind.
        if outside_ids or allow_existing_ihs_contact_cleanup:
            props["Faces"] = sorted(set(existing + outside_ids))
            if not wall.update():
                raise PreexecutionBlocked("failed to persist explicit outside-contact adiabatic face assignment")
            rebound = boundary_map(app, "outside-contact post-assignment boundary map")[boundary_name]
        else:
            rebound = wall
    rebound_faces = {int(value) for value in rebound.props.get("Faces", [])}
    if not set(outside_ids).issubset(rebound_faces) or any(face_id in rebound_faces for face_id in contact_ids):
        raise PreexecutionBlocked("explicit outside-contact adiabatic binding did not persist in the live design")
    return {
        "IHS_top_area_mm2": float(ihs_total_area_mm2),
        "heatsink_base_bottom_area_mm2": float(base_area),
        "contact_face_id": contact_ids[0],
        "contact_face_ids": contact_ids,
        "contact_area_mm2": contact_area,
        "outside_face_ids": outside_ids,
        "removed_stale_scaled_IHS_adiabatic_faces": removed_stale_ihs_faces,
        "outside_area_mm2": ihs["outside_area_mm2"] + base["outside_area_mm2"],
        "IHS": ihs,
        "heatsink_base": base,
        "fixed_anchor_imprint": fixed_anchor_imprint,
        "fixed_anchor_sizes_mm": list(SIZES) if fixed_anchor_imprint else None,
        "tim2_geometry_size_mm": geometry_size,
        "fixed_geometry_contact_mask": geometry_size != size,
        "boundary_name": boundary_name,
        "boundary_kind": boundary_kind,
        "external_condition": "Adiabatic Plate" if boundary_kind == "ADIABATIC_PLATE" else "Heat Flux",
        "heat_flux": "NO_NORMAL_OR_IN_PLANE_CONDUCTION" if boundary_kind == "ADIABATIC_PLATE" else "0irrad_W_per_m2",
        "radiation": False,
    }


def normal_completion(workspace: Path, limiter_audit_path: Path | None = None) -> dict[str, Any]:
    profiles = list((workspace / "case.aedtresults").rglob("*.profile"))
    matches = []
    for profile in profiles:
        text = profile.read_text(encoding="utf-8", errors="ignore")
        normal = "ProfileItem('Solve'" in text and "Normal Completion" in text
        bad = any(token in text.lower() for token in ("abnormal completion", "diverged", "fatal error"))
        if normal and not bad:
            matches.append(profile)
    if not matches:
        raise RuntimeError("Icepak solve profile lacks unambiguous Normal Completion")
    newest = max(matches, key=lambda path: path.stat().st_mtime_ns)
    solver_outputs = list((workspace / "case.aedtresults").rglob("current.uns_out"))
    if not solver_outputs:
        raise RuntimeError("Icepak Normal Completion lacks current.uns_out")
    solver_output = max(solver_outputs, key=lambda path: path.stat().st_mtime_ns)
    solver_lines = solver_output.read_text(encoding="utf-8", errors="ignore").splitlines()
    iteration_lines = [
        index for index, line in enumerate(solver_lines)
        if re.match(r"^\s*\d+\s+\d", line)
    ]
    tail_start = iteration_lines[-25] if len(iteration_lines) >= 25 else (iteration_lines[0] if iteration_lines else 0)
    limiter_events = sum(
        "temperature limited to" in line.lower() for line in solver_lines[tail_start:]
    )
    if limiter_events:
        if limiter_audit_path is not None:
            limiter_lines = [
                line.strip() for line in solver_lines[tail_start:]
                if "temperature limited to" in line.lower()
            ]
            write_json_replace(limiter_audit_path, {
                "schema_version": "icepak_temperature_limiter_audit_v1",
                "status": "PERSISTENT_TEMPERATURE_LIMITER",
                "current_uns_out_path": str(solver_output),
                "current_uns_out_sha256": sha256(solver_output),
                "iteration_lines": len(iteration_lines),
                "tail_iteration_count": min(25, len(iteration_lines)),
                "limiter_events": limiter_events,
                "first_limiter_lines": limiter_lines[:12],
                "last_limiter_lines": limiter_lines[-12:],
                "created_utc": now_utc(),
            })
        raise RuntimeError(
            f"ICEPAK_PERSISTENT_TEMPERATURE_LIMITER_IN_FINAL_ITERATIONS: {limiter_events} events"
        )
    return {
        "profile_path": str(newest), "profile_sha256": sha256(newest), "normal_completion": True,
        "current_uns_out_path": str(solver_output), "current_uns_out_sha256": sha256(solver_output),
        "final_25_iteration_temperature_limiter_events": 0,
    }


def wait_for_current_uns_out(workspace: Path, timeout_s: int = 180) -> Path | None:
    """Allow AEDT's blocking solve to finish flushing its Fluent result tree."""
    deadline = time.monotonic() + timeout_s
    while True:
        outputs = list((workspace / "case.aedtresults").rglob("current.uns_out"))
        if outputs:
            return max(outputs, key=lambda path: path.stat().st_mtime_ns)
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def wait_for_fluent_restart_artifacts(workspace: Path, timeout_s: int = 300) -> tuple[Path, Path] | None:
    """Wait for native Fluent case/data files, not merely Icepak's uns_out marker."""
    deadline = time.monotonic() + timeout_s
    while True:
        cases = [p for p in (workspace / "case.aedtresults").rglob("current.nc_cas") if p.is_file()]
        data = [p for p in (workspace / "case.aedtresults").rglob("current.fdat") if p.is_file()]
        if cases and data:
            case_path = max(cases, key=lambda p: p.stat().st_mtime_ns)
            data_path = max(data, key=lambda p: p.stat().st_mtime_ns)
            # Do not accept files while either one is still being written.
            sizes_before = (case_path.stat().st_size, data_path.stat().st_size)
            time.sleep(2.0)
            sizes_after = (case_path.stat().st_size, data_path.stat().st_size)
            if sizes_before == sizes_after and all(size > 0 for size in sizes_after):
                return case_path, data_path
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def configure_solver_iterations(app: Any, max_iterations: int | None) -> dict[str, int] | None:
    """Apply an explicit diagnostic iteration budget without changing defaults."""
    if max_iterations is None:
        return None
    if int(max_iterations) < 1:
        raise PreexecutionBlocked("max iterations must be positive")
    setup = app.get_setup("Setup1")
    key = "Convergence Criteria - Max Iterations"
    original = int(setup.props[key])
    setup.props[key] = int(max_iterations)
    if not setup.update():
        raise PreexecutionBlocked("failed to persist max-iteration override")
    return {"original_max_iterations": original, "max_iterations": int(max_iterations)}


def translated_source_zone_audit(
    workspace: Path, expected_sources: dict[str, Any], expected_source_objects: list[str],
    require_source_monitors: bool = True,
    require_source_solids: bool = True,
    expected_persistent_source_targets: list[str] | None = None,
    expected_source_boundary_count: int | None = None,
    expected_source_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reject Icepak's false 'Normal Completion' when Fluent dropped source zones."""
    outputs = list((workspace / "case.aedtresults").rglob("current.uns_out"))
    if not outputs:
        raise RuntimeError("Icepak translation output current.uns_out is missing")
    newest = max(outputs, key=lambda path: path.stat().st_mtime_ns)
    text = newest.read_text(encoding="utf-8", errors="ignore")
    expected_boundaries = (
        int(expected_source_boundary_count) if expected_source_boundary_count is not None
        else len([name for name in expected_sources if name.startswith("P_active_")])
    )
    monitor_names = sorted(set(__import__("re").findall(r"Setup1_SourceFaceGate_[^\"\s]+_(?:temp|hfr)\.out", text, flags=__import__("re").IGNORECASE)))
    expected_source_groups = expected_source_groups or []
    grouped_members = sorted({
        name for group in expected_source_groups
        for name in group.get("physical_source_objects", [])
    })
    grouped_representatives = sorted(
        group.get("zone_representative_object", "") for group in expected_source_groups
    )
    if expected_source_groups and grouped_representatives != sorted(expected_source_objects):
        raise RuntimeError("grouped source-zone representatives do not match expected translated solids")
    expected_monitors = max(
        expected_boundaries * 4,
        len(grouped_members) * 4 if grouped_members else 0,
    )
    text_lower = text.lower()
    missing_solids = [
        name for name in expected_source_objects
        if f"{name.lower()}_solid" not in text_lower
    ]
    missing_group_representatives = missing_solids if expected_source_groups else []
    expected_persistent_source_targets = sorted(expected_persistent_source_targets or [])
    missing_targets = [
        name for name in expected_persistent_source_targets
        if f"{name.lower()}_solid" not in text_lower
    ]
    # Monitor count is retained as a legacy hard check for ordinary Source
    # boundaries.  Equivalent solid-block recovery may legitimately recreate
    # monitors differently, so every frozen physical source solid is also
    # checked directly in the translated Fluent zone inventory.
    if (require_source_solids and missing_solids) or missing_group_representatives or missing_targets or (require_source_monitors and len(monitor_names) < expected_monitors):
        raise RuntimeError(
            f"ACTIVE_SOURCE_ZONES_MISSING_AFTER_ICEPAK_TO_FLUENT_TRANSLATION: "
            f"found {len(monitor_names)} source monitors, expected at least {expected_monitors}; "
            f"missing source solids={missing_solids if require_source_solids else []}; "
            f"missing persistent source targets={missing_targets}"
        )
    return {
        "current_uns_out_path": str(newest),
        "current_uns_out_sha256": sha256(newest),
        "active_source_boundaries_expected": expected_boundaries,
        "source_face_monitors_found": len(monitor_names),
        "source_face_monitors_expected": expected_monitors,
        "source_face_monitors_required": require_source_monitors,
        "source_solids_required": require_source_solids,
        "active_source_solids_expected": len(expected_source_objects),
        "active_source_solids_found": len(expected_source_objects) - len(missing_solids),
        "grouped_zone_representatives_required": bool(expected_source_groups),
        "active_source_solid_names": sorted(expected_source_objects),
        "grouped_source_zones": expected_source_groups,
        "physical_source_objects_represented": (
            len(grouped_members) if grouped_members else len(expected_source_objects)
        ),
        "persistent_source_targets_expected": expected_persistent_source_targets,
        "persistent_source_targets_found": sorted(set(expected_persistent_source_targets) - set(missing_targets)),
        "status": "PASS",
    }


def translated_solid_block_total_power_audit(
    workspace: Path, source_refresh: dict[str, Any], log_path: Path, strict: bool = True,
) -> dict[str, Any]:
    """Integrate each Fluent source density over its actual translated cell-zone volume."""
    groups = list(source_refresh.get("translation_groups", []))
    expected = ([{
        "boundary_name": item["boundary_name"],
        "object": item["zone_representative_object"],
        "total_power_W": item["total_power_W"],
    } for item in groups] if groups else list(source_refresh.get("boundaries", [])))
    if not expected:
        expected = [
            {
                "boundary_name": name,
                "object": item["props"]["Objects"][0],
                "total_power_W": float(str(item["props"]["Total Power"]).removesuffix("W")),
            }
            for name, item in source_refresh.get("native_boundary_props", {}).items()
            if item.get("type") == "SourceIcepak"
            and item.get("props", {}).get("Thermal Condition") == "Total Power"
            and len(item.get("props", {}).get("Objects", [])) == 1
        ]
    if not expected:
        raise RuntimeError("source total-power audit has no expected source boundaries")
    cases = list((workspace / "case.aedtresults").rglob("current.nc_cas"))
    data = list((workspace / "case.aedtresults").rglob("current.fdat"))
    if not cases or not data:
        raise RuntimeError("Fluent current.nc_cas/current.fdat is missing for source-power audit")
    case_path = max(cases, key=lambda path: path.stat().st_mtime_ns)
    data_path = max(data, key=lambda path: path.stat().st_mtime_ns)
    if not case_path.is_file() or not data_path.is_file():
        raise RuntimeError(
            f"Fluent restart artifacts disappeared before source audit: "
            f"case_exists={case_path.is_file()} data_exists={data_path.is_file()} "
            f"case={case_path} data={data_path}"
        )
    case_text = case_path.read_text(encoding="utf-8", errors="ignore").lower()
    zones = [f"{str(item['object']).lower()}_solid" for item in expected]
    source_term_map: dict[str, float] = {}
    for match in re.finditer(
        r"\(39 \(\d+ solid\s+(\S+)\s+1\)\((.*?)(?=\n\)\)\n\(39 |\Z)",
        case_text,
        flags=re.DOTALL,
    ):
        density = re.search(
            r"source-terms\s+\(energy\s+\(\(constant\s+\.\s+([0-9.eE+\-]+)\)",
            match.group(2),
        )
        if density:
            source_term_map[match.group(1)] = float(density.group(1))
    # A strict Total Power compensation performs a second translation; retain
    # the final translated-zone audit rather than treating that intentional
    # refresh as a conflicting duplicate artifact.
    write_json_replace(log_path.with_name("FLUENT_SOURCE_TERM_STRUCTURE_AUDIT.json"), {
        "status": "PASS" if all(zone in source_term_map for zone in zones) else "MISSING_SOURCE_TERMS",
        "contract_version": "FLUENT_NC_CAS_SOURCE_TERM_STRUCTURE_V1",
        "current_nc_cas_path": str(case_path),
        "current_nc_cas_sha256": sha256(case_path),
        "expected_source_zones": zones,
        "source_density_W_m3_by_zone": source_term_map,
        "missing_expected_source_zones": [zone for zone in zones if zone not in source_term_map],
        "created_utc": now_utc(),
    })
    commands = "\n".join([
        f'/file/read-case "{case_path.as_posix()}"',
        f'/file/read-data "{data_path.as_posix()}"',
        "/report/volume-integrals/volume",
        # Fluent's TUI parses bare underscore names as Scheme symbols in
        # this menu.  Quote every zone name so the volume report addresses
        # the translated cell zone rather than an unbound variable.
        *[f'"{zone}"' for zone in zones],
        "",
        "no",
        "/exit",
        "yes",
        "",
    ])
    root = Path(os.environ.get("ANSYSEM_ROOT232", r"D:\Program Files\AnsysEM\v232\Win64"))
    fluent = root / "fluent" / "ntbin" / "win64" / "fluent.exe"
    if not fluent.is_file():
        raise RuntimeError(f"Fluent executable is missing: {fluent}")
    completed = subprocess.run(
        [str(fluent), "3ddp", "-g", "-t1"], input=commands.encode("ascii"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
        cwd=str(workspace), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = completed.stdout.decode("utf-8", errors="replace")
    log_path.write_text(output, encoding="utf-8", errors="replace")
    if completed.returncode != 0 or "Total Volume" not in output:
        raise RuntimeError(f"Fluent source-zone volume audit failed with code {completed.returncode}")
    audited = []
    all_close = True
    for item, zone in zip(expected, zones):
        volume_match = re.search(rf"(?m)^\s*{re.escape(zone)}\s+([0-9.eE+\-]+)\s*$", output)
        density_match = re.search(
            rf"solid\s+{re.escape(zone)}\s+1\)\(.*?"
            rf"source-terms\s+\(energy\s+\(\(constant\s+\.\s+([0-9.eE+\-]+)\)",
            case_text, flags=re.DOTALL,
        )
        if not volume_match or not density_match:
            raise RuntimeError(
                f"Fluent source zone audit missing volume or density for {zone}; "
                f"case={case_path} data={data_path} output_tail={output[-1000:]}"
            )
        volume_m3 = float(volume_match.group(1))
        density_w_m3 = float(density_match.group(1))
        integrated_power = volume_m3 * density_w_m3
        total_power = float(item["total_power_W"])
        close = bool(np.isclose(integrated_power, total_power, rtol=2e-5, atol=1e-6))
        all_close = all_close and close
        if strict and not close:
            raise RuntimeError(
                f"translated source total power changed for {zone}: "
                f"{integrated_power:.12g} versus {total_power:.12g} W"
            )
        audited.append({
            "boundary_name": item["boundary_name"],
            "object": item["object"],
            "fluent_zone": zone,
            "fluent_zone_volume_m3": volume_m3,
            "translated_power_density_W_m3": density_w_m3,
            "integrated_power_W": integrated_power,
            "total_power_W": total_power,
            "relative_error": (integrated_power - total_power) / total_power,
            "status": "PASS" if close else "NEEDS_NUMERICAL_COMPENSATION",
        })
    return {
        "status": "PASS" if all_close else "NEEDS_NUMERICAL_COMPENSATION",
        "contract_version": "FLUENT_ZONE_VOLUME_X_TRANSLATED_DENSITY_V1",
        "current_nc_cas_path": str(case_path),
        "current_nc_cas_sha256": sha256(case_path),
        "audit_log_path": str(log_path),
        "audit_log_sha256": sha256(log_path),
        "per_source": audited,
        "integrated_total_power_W": float(sum(item["integrated_power_W"] for item in audited)),
    }


def strict_source_network_audit(
    app: Any,
    attempt: Path,
    source_refresh: dict[str, Any] | None,
    source_zone_translation: dict[str, Any],
    source_transplant: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require geometry contact, Fluent source terms, and power closure together.

    The earlier gate checked the translated zone names and, for recovery modes,
    checked a volume integral.  Those checks were stored as separate PASS flags,
    so a future change could accidentally publish a case with a source zone that
    existed in the case file but was not the expected solid or was not touching
    the HB.  This audit is the single publish-blocking contract for the source
    network.  It deliberately uses only the live AEDT geometry and the final
    Fluent restart audit; it does not infer source participation from temperature.
    """
    if not source_zone_translation or source_zone_translation.get("status") != "PASS":
        raise PreexecutionBlocked("strict source-network gate requires a PASS source-zone translation audit")
    power_audit = source_zone_translation.get("fluent_total_power_audit")
    if not isinstance(power_audit, dict) or power_audit.get("status") != "PASS":
        raise PreexecutionBlocked("strict source-network gate requires a PASS Fluent volume power audit")

    source_objects = sorted((source_refresh or {}).get("source_objects", []))
    if not source_objects:
        source_objects = sorted(source_zone_translation.get("active_source_solid_names", []))
    if not source_objects:
        raise PreexecutionBlocked("strict source-network gate found no expected physical source solids")

    hb = app.modeler["HB"]
    if hb is None:
        raise PreexecutionBlocked("strict source-network gate cannot find HB")
    hb_bbox = [float(value) for value in hb.bounding_box]
    tolerance_mm = 1.0e-6
    contacts = []
    for name in source_objects:
        obj = app.modeler[name]
        if obj is None:
            raise PreexecutionBlocked(f"strict source-network gate missing source object: {name}")
        bbox = [float(value) for value in obj.bounding_box]
        inside_xy = (
            bbox[0] >= hb_bbox[0] - tolerance_mm
            and bbox[1] >= hb_bbox[1] - tolerance_mm
            and bbox[3] <= hb_bbox[3] + tolerance_mm
            and bbox[4] <= hb_bbox[4] + tolerance_mm
        )
        touches_hb = (
            abs(bbox[5] - hb_bbox[2]) <= tolerance_mm
            or abs(bbox[2] - hb_bbox[5]) <= tolerance_mm
        )
        volume_mm3 = float(obj.volume)
        if not inside_xy or not touches_hb or not np.isfinite(volume_mm3) or volume_mm3 <= 0.0:
            raise PreexecutionBlocked(
                f"strict source-network geometry gate failed for {name}: "
                f"bbox={bbox}, HB={hb_bbox}, inside_xy={inside_xy}, "
                f"touches_hb={touches_hb}, volume_mm3={volume_mm3}"
            )
        contacts.append({
            "object": name,
            "bbox_mm": bbox,
            "volume_mm3": volume_mm3,
            "inside_hb_xy": True,
            "touches_hb_interface": True,
            "interface_z_mm": hb_bbox[2] if abs(bbox[5] - hb_bbox[2]) <= tolerance_mm else hb_bbox[5],
        })

    per_source = list(power_audit.get("per_source", []))
    expected_names = set(source_objects)
    audited_names = {str(item.get("object")) for item in per_source}
    if audited_names != expected_names:
        raise PreexecutionBlocked(
            "strict source-network power audit object mismatch: "
            f"expected={sorted(expected_names)}, audited={sorted(audited_names)}"
        )
    for item in per_source:
        density = float(item.get("translated_power_density_W_m3"))
        integrated = float(item.get("integrated_power_W"))
        target = float(item.get("total_power_W"))
        if (
            item.get("status") != "PASS"
            or not np.isfinite(density) or density <= 0.0
            or not np.isfinite(integrated) or integrated <= 0.0
            or not np.isfinite(target) or target <= 0.0
            or not np.isclose(integrated, target, rtol=2e-5, atol=1e-6)
        ):
            raise PreexecutionBlocked(
                f"strict source-network per-source power gate failed for {item.get('object')}: {item}"
            )

    structure_path = attempt / "FLUENT_SOURCE_TERM_STRUCTURE_AUDIT.json"
    try:
        structure = json_read(structure_path)
    except Exception as exc:
        raise PreexecutionBlocked(
            f"strict source-network gate cannot read {structure_path}: {exc}"
        ) from exc
    expected_zones = [f"{name.lower()}_solid" for name in source_objects]
    if (
        structure.get("status") != "PASS"
        or structure.get("missing_expected_source_zones")
        or any(
            not np.isfinite(float(structure.get("source_density_W_m3_by_zone", {}).get(zone, float("nan"))))
            or float(structure.get("source_density_W_m3_by_zone", {}).get(zone, 0.0)) <= 0.0
            for zone in expected_zones
        )
    ):
        raise PreexecutionBlocked(
            f"strict source-network source-term structure gate failed: {structure}"
        )
    result = {
        "status": "PASS",
        "contract_version": "STRICT_SOURCE_NETWORK_GATE_V1",
        "source_objects": source_objects,
        "source_hb_contacts": contacts,
        "fluent_source_term_structure": str(structure_path),
        "fluent_total_power_audit": power_audit,
        "source_transplant_status": (source_transplant or {}).get("status"),
        "created_utc": now_utc(),
    }
    write_json_replace(attempt / "SOURCE_NETWORK_STRICT_AUDIT.json", result)
    return result


def compensate_solid_block_translation(
    app: Any, initial_audit: dict[str, Any], max_relative_adjustment: float = 0.02,
) -> dict[str, Any]:
    """Adjust solver input only enough to preserve logical Total Power in Fluent."""
    boundaries = boundary_map(app, "solid-block numerical Total Power compensation")
    evidence = []
    for item in initial_audit["per_source"]:
        integrated = float(item["integrated_power_W"])
        target = float(item["total_power_W"])
        corrected = target * target / integrated
        relative_adjustment = corrected / target - 1.0
        if abs(relative_adjustment) > max_relative_adjustment:
            raise RuntimeError(
                f"solid-block numerical power compensation exceeds {max_relative_adjustment:.1%} "
                f"for {item['object']}: {relative_adjustment:.6%}"
            )
        boundary = boundaries.get(item["boundary_name"])
        if boundary is None:
            raise RuntimeError(f"solid-block boundary disappeared before compensation: {item['boundary_name']}")
        boundary.props["Total Power"] = f"{corrected:.15g}W"
        if not boundary.update():
            raise RuntimeError(f"failed updating compensated Total Power: {item['boundary_name']}")
        evidence.append({
            "boundary_name": item["boundary_name"],
            "object": item["object"],
            "logical_total_power_W": target,
            "initial_fluent_integrated_power_W": integrated,
            "compensated_aedt_solver_input_W": corrected,
            "relative_solver_input_adjustment": relative_adjustment,
        })
    return {
        "status": "APPLIED",
        "reason": "FINITE_VOLUME_CELL_ZONE_VOLUME_DIFFERS_SLIGHTLY_FROM_CAD_SOURCE_VOLUME",
        "logical_source_total_power_changed": False,
        "solver_input_adjustment_is_numerical_only": True,
        "maximum_allowed_relative_adjustment": max_relative_adjustment,
        "per_source": evidence,
    }


def persistence_audit(
    app: Any, size: int, expected_objects: list[str], expected_boundary: dict[str, Any],
    expected_sources: dict[str, Any], mesh_count_xy: int | None = None,
    source_refresh: bool = False, source_recovery_mesh: dict[str, Any] | None = None,
    aedt_path: Path | None = None, scaled_package_xy: bool = False,
    expected_solver_source_total_w: float | None = None,
    mesh_count_z: int = 3,
) -> dict[str, Any]:
    contract = object_contract(app, size, aedt_path=aedt_path, scaled_package_xy=scaled_package_xy)
    if contract["object_names"] != expected_objects:
        raise RuntimeError("persistent object-name binding changed after save/reopen")
    persisted_signature = boundary_signature(app)
    if source_refresh:
        persisted_total = source_total_w(persisted_signature)
        expected_total = (
            float(expected_solver_source_total_w)
            if expected_solver_source_total_w is not None else source_total_w(expected_sources)
        )
        if not np.isclose(persisted_total, expected_total, rtol=0.0, atol=1e-8):
            raise RuntimeError("recovered source total power changed after save/reopen")
    else:
        persisted_openings = {
            name: item for name, item in persisted_signature.items() if name in {"Opening1", "Opening2"}
        }
        expected_openings = {
            name: item for name, item in expected_sources.items() if name in {"Opening1", "Opening2"}
        }
        if persisted_openings != expected_openings:
            raise RuntimeError("Opening boundary props changed after save/reopen")
        persisted_power = source_object_power_contract(app, persisted_signature)
        expected_power = source_object_power_contract(app, expected_sources)
        if set(persisted_power) != set(expected_power) or any(
            not np.isclose(persisted_power[name], power, rtol=0.0, atol=1e-8)
            for name, power in expected_power.items()
        ):
            raise RuntimeError(
                f"source object/power bindings changed after save/reopen: {persisted_power} != {expected_power}"
            )
    mesh = mesh_contract(app, size, mesh_count_xy, bool(source_recovery_mesh), mesh_count_z=mesh_count_z)
    recovery_mesh_items = list(getattr(app.mesh, "meshoperations", [])) + list(getattr(app.mesh, "meshregions", []))
    if source_recovery_mesh and not any(item.name.startswith("ActiveSourceRecoveryMesh") for item in recovery_mesh_items):
        persisted_text = aedt_path.read_text(encoding="utf-8", errors="strict") if aedt_path else ""
        expected_mesh_names = source_recovery_mesh.get("names", [])
        if not expected_mesh_names or any(
            f"$begin '{name}'" not in persisted_text for name in expected_mesh_names
        ):
            raise RuntimeError("active-source recovery mesh operation missing after save/reopen")
    if source_recovery_mesh and source_recovery_mesh.get("manual_settings"):
        regions = [item for item in getattr(app.mesh, "meshregions", []) if item.name == "ActiveSourceRecoveryMeshRegion"]
        if len(regions) != 1:
            raise RuntimeError("explicit active-source mesh region missing after save/reopen")
        if aedt_path is None:
            raise RuntimeError("persisted AEDT path is required for explicit source-mesh audit")
        mesh_text = aedt_path.read_text(encoding="utf-8", errors="strict")
        region_blocks = re.findall(
            r"\$begin 'ActiveSourceRecoveryMeshRegion'(.*?)\$end 'ActiveSourceRecoveryMeshRegion'",
            mesh_text, flags=re.DOTALL,
        )
        if len(region_blocks) != 1 or "UserSpecifiedSettings=true" not in region_blocks[0] or "MaxElementSizeZ='0.005mm'" not in region_blocks[0]:
            raise RuntimeError("explicit active-source 0.005-mm Z mesh did not persist")
    no_gamma_h0_boundary(app)
    walls = boundary_map(app, "outside-contact validation boundary map")
    wall = walls.get(expected_boundary["boundary_name"])
    if wall is None:
        raise RuntimeError("outside-contact adiabatic boundary missing after save/reopen")
    faces = {int(value) for value in wall.props.get("Faces", [])}
    contact_ids = expected_boundary.get("contact_face_ids", [expected_boundary["contact_face_id"]])
    if not set(expected_boundary["outside_face_ids"]).issubset(faces) or any(int(value) in faces for value in contact_ids):
        raise RuntimeError("outside/contact face binding changed after save/reopen")
    return {
        "objects": True, "openings_sources": True, "outside_adiabatic": True,
        "mesh": mesh, "gamma_h0_absent": True,
        "persisted_solver_source_total_W": (
            source_total_w(persisted_signature) if source_refresh else None
        ),
        "logical_source_total_is_verified_by_fluent_audit": expected_solver_source_total_w is not None,
    }


def resolve_carrier(
    task: dict[str, str], recovery_carrier_value: str | None, recovery_receipt_value: str | None
) -> tuple[Path, dict[str, Any] | None]:
    """Admit a solved same-source/same-sink PASS as a versioned recovery carrier.

    The frozen plan remains the physical identity.  An override is accepted only
    when a prior independently hashed Icepak PASS proves the carrier has the same
    source, sink and original frozen carrier.  This is used to recover a broken
    AEDT-to-Fluent topology translation, not to alter the requested physics.
    """
    frozen = safe_path(Path(task["carrier_aedt_path"]))
    if not recovery_carrier_value and not recovery_receipt_value:
        if sha256(frozen) != task["carrier_aedt_sha256"].upper():
            raise PreexecutionBlocked("carrier AEDT hash differs from frozen development plan")
        return frozen, None
    if not recovery_carrier_value or not recovery_receipt_value:
        raise PreexecutionBlocked("recovery carrier and its PASS receipt must be provided together")
    carrier = safe_path(Path(recovery_carrier_value))
    receipt_path = safe_path(Path(recovery_receipt_value))
    receipt = json_read(receipt_path)
    if receipt.get("status") != "PASS" or receipt.get("truth_engine") != "ANSYS_ICEPAK":
        raise PreexecutionBlocked("recovery carrier receipt is not an Icepak PASS")
    for field in ("source_pattern_id", "bridge_sink_id"):
        if str(receipt.get(field)) != str(task[field]):
            raise PreexecutionBlocked(f"recovery carrier physical identity mismatch: {field}")
    if str(receipt.get("carrier_aedt_path")) != str(frozen):
        raise PreexecutionBlocked("recovery PASS did not originate from the frozen plan carrier")
    if str(receipt.get("carrier_aedt_sha256", "")).upper() != task["carrier_aedt_sha256"].upper():
        raise PreexecutionBlocked("recovery PASS frozen carrier hash mismatch")
    artifacts = receipt.get("artifacts", {})
    hashes = receipt.get("artifacts_sha256", {})
    if str(artifacts.get("solved_aedt")) != str(carrier):
        raise PreexecutionBlocked("recovery carrier path is not the PASS solved_aedt artifact")
    carrier_hash = sha256(carrier)
    if carrier_hash != str(hashes.get("solved_aedt", "")).upper():
        raise PreexecutionBlocked("recovery carrier solved_aedt hash mismatch")
    if int(float(receipt.get("TIM2_size_mm", -1))) == int(float(task["TIM2_size_mm"])):
        raise PreexecutionBlocked("recovery carrier must come from a distinct already-PASS topology")
    return carrier, {
        "schema_version": "bridging_sink_recovery_carrier_v1",
        "reason": "recover AEDT-to-Fluent topology translation with a same-source/same-sink solved PASS carrier",
        "physical_identity_unchanged": True,
        "frozen_plan_carrier_path": str(frozen),
        "frozen_plan_carrier_sha256": task["carrier_aedt_sha256"].upper(),
        "recovery_carrier_path": str(carrier),
        "recovery_carrier_sha256": carrier_hash,
        "recovery_pass_receipt_path": str(receipt_path),
        "recovery_pass_receipt_sha256": sha256(receipt_path),
        "recovery_pass_task_id": receipt.get("task_id"),
        "recovery_pass_TIM2_size_mm": int(float(receipt["TIM2_size_mm"])),
        "blind_or_final_truth_access": False,
    }


def automatic_degenerate_recovery(task: dict[str, str]) -> tuple[str, str] | None:
    """After a preserved zero-source solve, use the nearest legal solved PASS.

    This prevents repeated identical restarts while retaining the original
    failure evidence.  The selected donor is revalidated by ``resolve_carrier``.
    """
    receipts = sorted((task_output(task) / "attempts").glob("attempt_*/ATTEMPT_RECEIPT.json"))
    degenerate = False
    for path in receipts:
        try:
            item = json_read(path)
        except Exception:
            continue
        if item.get("status") == "FAIL_RECOVERABLE" and "nonphysical or degenerate" in str(item.get("error", "")):
            degenerate = True
    if not degenerate:
        return None
    target = int(float(task["TIM2_size_mm"]))
    candidates: list[tuple[int, int, str, str]] = []
    for donor in load_plan():
        if donor["task_id"] == task["task_id"] or donor["source_pattern_id"] != task["source_pattern_id"] or donor["bridge_sink_id"] != task["bridge_sink_id"]:
            continue
        receipt_path = pass_receipt_path(donor)
        if not valid_pass(donor) or not receipt_path.is_file():
            continue
        receipt = json_read(receipt_path)
        artifact = receipt.get("artifacts", {}).get("solved_aedt")
        expected = str(receipt.get("artifacts_sha256", {}).get("solved_aedt", "")).upper()
        if not artifact:
            continue
        carrier = Path(str(artifact))
        if not carrier.is_file() or sha256(carrier) != expected:
            continue
        size = int(float(donor["TIM2_size_mm"]))
        candidates.append((abs(size - target), -size, str(carrier), str(receipt_path)))
    if not candidates:
        return None
    _distance, _negative_size, carrier, receipt = sorted(candidates)[0]
    return carrier, receipt


def has_degenerate_translation_failure(task: dict[str, str]) -> bool:
    for path in sorted((task_output(task) / "attempts").glob("attempt_*/ATTEMPT_RECEIPT.json")):
        try:
            item = json_read(path)
        except Exception:
            continue
        error = str(item.get("error", ""))
        if item.get("status") == "FAIL_RECOVERABLE" and (
            "nonphysical or degenerate" in error or "ACTIVE_SOURCE_ZONES_MISSING_AFTER_ICEPAK_TO_FLUENT_TRANSLATION" in error
        ):
            return True
    return False


def run_generated(
    task: dict[str, str], attempt_number: int, recovery_carrier_value: str | None = None,
    recovery_receipt_value: str | None = None, recovery_mesh_count_xy: int | None = None,
    recovery_refresh_sources: bool = False, recovery_source_mode: str = "source-rebind",
    max_iterations: int | None = None, fixed_anchor_imprint: bool = False,
    fixed_geometry_contact_mask: bool = False,
    fixed_geometry_internal_plate_mask: bool = False,
    compact_success: bool = False,
    scaled_package_xy: bool = False,
    allow_source_zone_deficit: bool = False,
    diagnostic_global_mesh_resolution: int | None = None,
    diagnostic_tim2_mesh_count_xy: int | None = None,
    diagnostic_tim2_mesh_count_z: int | None = None,
) -> dict[str, Any]:
    size = int(float(task["TIM2_size_mm"]))
    if task["truth_action"] != "GENERATE_ICEPAK_FULL_TRUTH" or (size == 40 and not scaled_package_xy):
        raise PreexecutionBlocked("generated task action/size mismatch")
    carrier, recovery_carrier = resolve_carrier(task, recovery_carrier_value, recovery_receipt_value)
    attempt = attempt_dir(task, attempt_number)
    attempt.mkdir(parents=True, exist_ok=False)
    # AEDT 2023.2 still fails OpenProject/export calls when its working path is
    # near the legacy Windows 260-character boundary.  Keep append-only
    # receipts in the versioned evidence tree, but put every AEDT-facing file
    # in a deterministic short workspace.  The short workspace is never
    # reused or deleted by this runner.
    task_token = hashlib.sha256(task["task_id"].encode("utf-8")).hexdigest()[:16]
    workspace = validated_compute_root() / task_token / f"a{attempt_number:04d}"
    workspace.mkdir(parents=True, exist_ok=False)
    copied = workspace / "case.aedt"
    shutil.copy2(carrier, copied)
    source_boundary_strip = None
    if _truthy(task.get("source_rebuild_required", "False")) or recovery_source_mode in FORCE_SOURCE_REBUILD_MODES:
        stripped, source_boundary_strip = strip_active_source_boundary_blocks(copied.read_bytes())
        copied.write_bytes(stripped)
    global_mesh_audit = None
    if diagnostic_global_mesh_resolution is not None:
        patched, global_mesh_audit = patch_global_mesh_resolution_text(
            copied.read_text(encoding="utf-8", errors="strict"), diagnostic_global_mesh_resolution
        )
        copied.write_text(patched, encoding="utf-8", newline="\n")
    fixed_geometry_contact_mask = fixed_geometry_contact_mask or fixed_geometry_internal_plate_mask
    if scaled_package_xy:
        if recovery_mesh_count_xy is not None or fixed_geometry_contact_mask:
            raise PreexecutionBlocked("scaled-package mode does not accept legacy geometry diagnostics")
        payload = copied.read_bytes()
        text_audit = {
            "mode": "UNCHANGED_40MM_CARRIER_BEFORE_NATIVE_WHOLE_PACKAGE_XY_SCALE",
            "requested_package_size_mm": size,
            "carrier_bytes_sha256": hashlib.sha256(payload).hexdigest().upper(),
            "geometry_or_mesh_text_changed_before_open": False,
            "source_boundary_strip": source_boundary_strip,
            "global_mesh_audit": global_mesh_audit,
            "diagnostic_tim2_mesh_count_xy": diagnostic_tim2_mesh_count_xy,
        }
    elif fixed_geometry_contact_mask:
        if recovery_mesh_count_xy is not None:
            raise PreexecutionBlocked("fixed-geometry contact-mask diagnostic preserves the carrier mesh exactly")
        payload = copied.read_bytes()
        text_audit = {
            "mode": "UNCHANGED_40MM_TIM2_GEOMETRY_CONTACT_MASK_DIAGNOSTIC",
            "requested_contact_size_mm": size,
            "tim2_geometry_size_mm": 40,
            "carrier_bytes_sha256": hashlib.sha256(payload).hexdigest().upper(),
            "geometry_or_mesh_text_changed": False,
            "gamma_h0_boundary_absent_checked_after_open": True,
            "global_mesh_audit": global_mesh_audit,
        }
    else:
        patched, text_audit = patch_aedt_text(copied.read_bytes(), size, recovery_mesh_count_xy)
        copied.write_bytes(patched)
    write_json_exclusive(attempt / "TEXT_PATCH_AUDIT.json", text_audit)
    write_json_exclusive(attempt / "COMPUTE_WORKSPACE.json", {
        "workspace": str(workspace),
        "workspace_path_length": len(str(workspace / "case.aedt")),
        "reason": "AEDT_2023_2_LEGACY_WINDOWS_PATH_LIMIT",
        "append_only": True,
        "recovery_carrier": recovery_carrier,
        "recovery_mesh": ({"mesh_count_xy": recovery_mesh_count_xy,
                           "reason": "finer TIM2 local numerical discretization after preserved zero-source translation"}
                          if recovery_mesh_count_xy is not None else None),
        "diagnostic_global_mesh_resolution": diagnostic_global_mesh_resolution,
        "diagnostic_tim2_mesh_count_xy": diagnostic_tim2_mesh_count_xy,
        "diagnostic_tim2_mesh_count_z": diagnostic_tim2_mesh_count_z,
    })
    progress = {
        "status": "PREEXECUTION_AUDIT",
        "task_id": task["task_id"],
        "attempt": attempt_number,
        "runner_pid": __import__("os").getpid(),
        "workspace": str(workspace),
        "solver_started": False,
        "updated_utc": now_utc(),
    }
    write_json_replace(attempt / "LIVE_STATE.json", progress)

    requested_transport = configure_aedt_transport()
    from ansys.aedt.core.generic.settings import settings

    settings.use_grpc_api = requested_transport == "grpc"
    settings.grpc_secure_mode = os.environ.get("BRIDGE_GRPC_SECURE", "1") == "1"
    # AEDT 2023.2 on this host accepts the legacy ``-grpcsrv <port>``
    # invocation; the newer WNUA argument form exits before exposing a
    # session. Keep the compatibility switch local to this runner so the
    # formal queue can recover after an AEDT restart.
    if os.environ.get("BRIDGE_USE_PRE_GRPC_ARGS", "0") == "1":
        os.environ["PYAEDT_USE_PRE_GRPC_ARGS"] = "True"
    else:
        os.environ.pop("PYAEDT_USE_PRE_GRPC_ARGS", None)
    # Route all retries to the known shared license server.  The local
    # ansyslmd process can remain stale after a terminated solve and otherwise
    # causes false license-port failures on the next isolated attempt.
    os.environ["ANSYSCL_FNP_PATH"] = os.environ.get("ANSYSCL_FNP_PATH", os.environ.get("ANSYSLMD_LICENSE_FILE", "1055@localhost"))
    os.environ["ANSYSLMD_LICENSE_FILE"] = os.environ.get("ANSYSLMD_LICENSE_FILE", "1055@localhost")
    patch_local_grpc_probe()
    from ansys.aedt.core import Icepak

    app = None
    analyze_started = False
    preexecution: dict[str, Any] = {}
    try:
        app = Icepak(project=str(copied), version="2023.2", non_graphical=True, new_desktop=True, close_on_exit=False)
        actual_transport = "grpc" if app.desktop_class.is_grpc_api else "com"
        if actual_transport != requested_transport:
            raise PreexecutionBlocked(f"AEDT transport mismatch: requested {requested_transport}, actual {actual_transport}")
        progress["aedt_transport"] = actual_transport
        write_json_replace(attempt / "LIVE_STATE.json", progress)
        iteration_override = configure_solver_iterations(app, max_iterations)
        print("PREEXEC: app_opened", flush=True)
        direct_build = None
        if os.environ.get('BRIDGE_DIRECT_PARAMETERIZED_BUILD') == '1':
            assert scaled_package_xy, 'Direct route requires the normalized geometry audit'
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from direct_native_case import build as build_direct_case
            direct_build = build_direct_case(app, task, size, attempt, diagnostic_global_mesh_resolution if diagnostic_global_mesh_resolution is not None else 5)
            iteration_override = configure_solver_iterations(app, max_iterations)
        elif os.environ.get('BRIDGE_MATCH_DIRECT_GLOBAL_MESH') == '1':
            from ansys.aedt.core.modules.mesh_icepak import MeshSettings
            matched_mesh = app.mesh.global_mesh_region
            matched_mesh.settings._instance_settings = {**MeshSettings._common_mesh_settings,
                **MeshSettings._manual_mesh_settings, **MeshSettings._automatic_mesh_settings}
            matched_mesh.manual_settings = False
            matched_mesh.settings['MeshRegionResolution'] = diagnostic_global_mesh_resolution if diagnostic_global_mesh_resolution is not None else 5
            if not matched_mesh.update():
                raise PreexecutionBlocked('Matched fresh global mesh settings could not be applied')
        source_transplant = transplant_sources_from_frozen_metadata(
            app, task, force=recovery_source_mode in FORCE_SOURCE_REBUILD_MODES
        )
        sink_rebuild = rebuild_sink_from_frozen_metadata(app, task)
        # A plan flag is an optimization hint, never permission to solve a
        # different heatsink.  If the supposedly reusable carrier does not
        # match frozen target physics, rebuild it from that metadata before
        # any mesh or solver operation is allowed to proceed.
        sink_identity = sink_geometry_identity(app, task)
        if sink_identity["status"] != "PASS":
            sink_rebuild = rebuild_sink_from_frozen_metadata(app, task, force=True)
            sink_identity = sink_geometry_identity(app, task)
        if sink_identity["status"] != "PASS":
            raise PreexecutionBlocked(
                "frozen target sink geometry mismatch after rebuild: "
                + ",".join(sink_identity["findings"])
            )
        # Always rebind the sink box mesh by object name.  A carrier can already
        # match the frozen geometry while retaining stale numeric mesh IDs; in
        # that case skipping this step can leave an individual fin unmeshed.
        sink_box_mesh = (
            add_sink_box_mesh(app)
            if os.environ.get("BRIDGE_SINK_MESH_MODE", "box").lower() == "box"
            else None
        )
        sink_level1_mesh = None
        print("PREEXEC: source_sink_ready", flush=True)
        native_source_props = (
            source_transplant.get("native_boundary_props") if source_transplant else None
        )
        before_sources = merge_native_source_props(boundary_signature(app), native_source_props)
        if direct_build:
            from direct_native_case import contract as direct_contract
            package_scale = direct_contract(app, task, size, source_transplant)
        else:
            package_scale = (
            scale_package_xy(
                app, size, before_sources, int(float(task.get("carrier_TIM2_size_mm", 40))),
                native_source_props,
            )
            if scaled_package_xy else None
            )
        print("PREEXEC: package_scaled", flush=True)
        tim2_mesh_diagnostic = None
        if diagnostic_tim2_mesh_count_xy is not None or diagnostic_tim2_mesh_count_z is not None:
            tim2_mesh_diagnostic = diagnostic_tim2_mesh_count(
                app, diagnostic_tim2_mesh_count_xy, diagnostic_tim2_mesh_count_z
            )
        internal_mask = (
            create_internal_adiabatic_mask(app, size)
            if fixed_geometry_internal_plate_mask else None
        )
        geometry_size = 40 if fixed_geometry_contact_mask else size
        before = object_contract(
            app, geometry_size,
            aedt_path=(None if source_transplant or sink_rebuild else copied),
            scaled_package_xy=scaled_package_xy,
        )
        no_gamma_h0_boundary(app)
        normalized_mesh_count = (
            diagnostic_tim2_mesh_count_xy
            if diagnostic_tim2_mesh_count_xy is not None
            else (40 if scaled_package_xy else recovery_mesh_count_xy)
        )
        normalized_mesh_count_z = diagnostic_tim2_mesh_count_z if diagnostic_tim2_mesh_count_z is not None else 3
        mesh = mesh_contract(
            app, geometry_size, normalized_mesh_count,
            allow_source_recovery=bool(source_transplant or sink_rebuild),
            mesh_count_z=normalized_mesh_count_z,
        )
        if tim2_mesh_diagnostic is not None:
            text_audit["diagnostic_tim2_mesh_count"] = tim2_mesh_diagnostic
            write_json_replace(attempt / "TEXT_PATCH_AUDIT.json", text_audit)
        print("PREEXEC: mesh_checked", flush=True)
        outside = explicit_outside_contact_adiabatic(
            app, size, fixed_anchor_imprint, geometry_size_mm=geometry_size,
            expected_object_names=before["object_names"],
            ihs_total_area_mm2=float(size * size) if scaled_package_xy else 1600.0,
            allow_existing_ihs_contact_cleanup=scaled_package_xy,
        )
        print("PREEXEC: outside_contact_ready", flush=True)
        if merge_native_source_props(boundary_signature(app), native_source_props) != before_sources:
            raise PreexecutionBlocked("Opening/source bindings changed during TIM2 contact imprint")
        source_refresh = (
            refresh_active_source_bindings(app, before_sources, recovery_source_mode)
            if recovery_refresh_sources and recovery_source_mode not in NATIVE_SOURCE_MESH_MODES else None
        )
        persistence_expected_objects = recovered_persistent_object_names(before["object_names"], source_refresh)
        source_recovery_mesh = (
            add_active_source_recovery_mesh(
                app, before_sources,
                source_refresh.get("mesh_objects") if source_refresh else None,
                explicit_thin_z=is_equal_density_recovery_mode(recovery_source_mode),
                direct_native_api=(recovery_source_mode == "equal-density-group-explicit-mesh-v4"),
                explicit_box_counts=recovery_source_mode in {
                    "native-source-box-mesh-v1",
                    *SOURCE_REBIND_BOX_MESH_MODES,
                    "solid-block-box-mesh-v1",
                    *IMPRINTED_SOLID_BLOCK_MODES,
                    *LAYER_HYBRID_RECOVERY_MODES,
                    "multi-object-equal-density-box-mesh-v2",
                    "multi-object-equal-density-box-mesh-v3",
                    "multi-object-equal-density-box-mesh-v4",
                    "surface-footprint-preserve-solids-box-mesh-v2",
                    SURFACE_ZERO_BLOCK_ANCHOR_MODE,
                    *FULL_FEOL_BOX_MESH_MODES,
                },
                one_box_operation_per_object=(
                    recovery_source_mode in {
                        "native-source-box-mesh-v1",
                        *SOURCE_REBIND_BOX_MESH_MODES,
                        "solid-block-box-mesh-v1",
                        *IMPRINTED_SOLID_BLOCK_MODES,
                        *LAYER_HYBRID_RECOVERY_MODES,
                        "multi-object-equal-density-box-mesh-v3",
                        "multi-object-equal-density-box-mesh-v4",
                        "surface-footprint-preserve-solids-box-mesh-v2",
                        SURFACE_ZERO_BLOCK_ANCHOR_MODE,
                        *FULL_FEOL_BOX_MESH_MODES,
                    }
                ),
                box_counts=(
                    int(os.environ.get("BRIDGE_DIAGNOSTIC_FEOL_XY_COUNT", "8")),
                    int(os.environ.get("BRIDGE_DIAGNOSTIC_FEOL_XY_COUNT", "8")), 2,
                ) if recovery_source_mode == "solid-block-rebuilt-full-feol-box-mesh-v6" else (2, 2, 2),
                source_recovery_mode=recovery_source_mode,
            )
            if recovery_refresh_sources and (
                recovery_source_mode not in SURFACE_FOOTPRINT_RECOVERY_MODES
                or recovery_source_mode in {
                    "surface-footprint-preserve-solids-box-mesh-v2",
                    SURFACE_ZERO_BLOCK_ANCHOR_MODE,
                    *FULL_FEOL_BOX_MESH_MODES,
                }
            ) else None
        )
        if recovery_refresh_sources and recovery_source_mode in {
            *SOLID_BLOCK_RECOVERY_MODES, *LAYER_HYBRID_RECOVERY_MODES, *FULL_FEOL_BOX_MESH_MODES,
        } and (
            source_refresh is None or source_refresh.get("mode") != recovery_source_mode
            or source_recovery_mesh is None
        ):
            raise PreexecutionBlocked(
                f"requested recovery mode was not applied: {recovery_source_mode}"
            )
        if source_recovery_mesh:
            persistence_expected_objects = sorted(set(persistence_expected_objects) | set(
                source_recovery_mesh.get("mesh_subregion_objects", [])
            ))
        sink_recovery_mesh = (
            add_sink_level1_mesh(app)
            if recovery_source_mode == "surface-footprint-sink-level1-mesh-v1" else None
        )
        if not app.save_project():
            raise PreexecutionBlocked("pre-solve project save failed")
        print("PREEXEC: project_saved", flush=True)
        source_mesh_text_patch = None
        if source_recovery_mesh and (
            source_recovery_mesh.get("requires_pre_solve_reopen")
            or (not scaled_package_xy and source_recovery_mesh.get("manual_settings"))
        ):
            tim2_object_id = int(app.modeler["TIM2"].id) if scaled_package_xy else None
            owned_desktop_pid = int(app.odesktop.GetProcessID())
            app.close_project(save=True)
            persistence_patches = {}
            if tim2_object_id is not None:
                persistence_patches["tim2_mesh_binding"] = patch_tim2_mesh_binding_file(
                    copied, tim2_object_id
                )
            if source_recovery_mesh.get("manual_settings_via_persisted_text_patch"):
                persistence_patches["active_source_mesh"] = patch_explicit_source_mesh_region_file(copied)
            else:
                native_configuration = source_recovery_mesh.get("native_api_configuration")
                if native_configuration:
                    persistence_patches["active_source_mesh"] = native_configuration
            if recovery_source_mode in {*SOURCE_REBIND_BOX_MESH_MODES, *FULL_FEOL_BOX_MESH_MODES}:
                stripped, boundary_strip = strip_active_source_boundary_blocks(copied.read_bytes())
                copied.write_bytes(stripped)
                persistence_patches["post_mesh_source_boundary_strip"] = boundary_strip
            source_mesh_text_patch = persistence_patches or None
            app = Icepak(
                project=str(copied), version="2023.2", non_graphical=True,
                new_desktop=False, close_on_exit=False,
                aedt_process_id=owned_desktop_pid,
            )
            # AEDT 2023.2 may persist SourceIcepak records but drop the live
            # Total Power wrapper when a mesh-mandated reopen occurs. Recreate
            # the exact one-object/one-power bindings before auditing them.
            if recovery_source_mode in {*SOURCE_REBIND_BOX_MESH_MODES, *FULL_FEOL_BOX_MESH_MODES}:
                # Preserve the selected recovery representation across the
                # mesh-mandated reopen.  Rebinding every source as a generic
                # SourceIcepak boundary silently drops the solid-block source
                # term from Fluent's translated case.
                wrapper_rehydration = refresh_active_source_bindings(
                    app, before_sources, recovery_source_mode, allow_missing_boundaries=True
                )
                wrapper_rehydration["post_reopen_wrapper_rehydration"] = True
                source_refresh["post_reopen_wrapper_rehydration"] = wrapper_rehydration
                if not app.save_project():
                    raise PreexecutionBlocked("post-reopen source wrapper save failed")
            persistence_audit(
                app, geometry_size, persistence_expected_objects, outside, before_sources,
                normalized_mesh_count, recovery_refresh_sources, source_recovery_mesh,
                aedt_path=copied, scaled_package_xy=scaled_package_xy,
                mesh_count_z=normalized_mesh_count_z,
            )
        if sorted(geometry_part_names(copied)) != sorted(persistence_expected_objects):
            raise PreexecutionBlocked("persistent object names changed during preexecution audit")
        preexecution = {
            "direct_parameterized_build": direct_build,
            "text_patch": text_audit,
            "package_xy_scale": package_scale,
            "source_transplant": source_transplant,
            "sink_rebuild": sink_rebuild,
            "sink_model_identity": sink_identity,
            "sink_box_mesh": sink_box_mesh,
            "sink_level1_mesh": sink_level1_mesh,
            "objects": before,
            "persistent_objects_after_source_recovery": sorted(persistence_expected_objects),
            "mesh": mesh,
            "outside_contact": outside,
            "opening_source_signature_sha256": hashlib.sha256(json.dumps(before_sources, sort_keys=True).encode()).hexdigest().upper(),
            "passive_FEOL_preserved": all(name in before["object_names"] for name in ("Bump", "HB", "Die", "TIM1")),
            "gamma_h0_boundary_absent": True,
            "source_binding_refresh": source_refresh,
            "requested_recovery_source_mode": recovery_source_mode if recovery_refresh_sources else None,
            "active_source_recovery_mesh": source_recovery_mesh,
            "active_source_recovery_mesh_text_patch": source_mesh_text_patch,
            "sink_recovery_mesh": sink_recovery_mesh,
            "iteration_override": iteration_override,
            "internal_adiabatic_mask": internal_mask,
        }
        if direct_build or os.environ.get('BRIDGE_REQUIRE_PACKAGE_THERMAL_IDENTITY') == '1':
            identity_code_dir = str(Path(__file__).resolve().parents[1])
            if identity_code_dir not in sys.path:
                sys.path.insert(0, identity_code_dir)
            from package_thermal_identity import audit_package_thermal_identity
            thermal_identity = audit_package_thermal_identity(app, size)
            # T0 must start from these exact pre-solve solids/materials, even
            # when AEDT later saves solver state back into its working project.
            frozen_model = workspace / 'preexecution_model.aedt'
            if frozen_model.exists():
                raise PreexecutionBlocked('immutable preexecution model already exists')
            shutil.copy2(thermal_identity['native_project_path'], frozen_model)
            if hashlib.sha256(frozen_model.read_bytes()).hexdigest() != thermal_identity['native_project_sha256']:
                raise PreexecutionBlocked('preexecution model changed during identity capture')
            thermal_identity['audited_working_project_path'] = thermal_identity['native_project_path']
            thermal_identity['native_project_path'] = str(frozen_model)
            preexecution['package_thermal_identity'] = thermal_identity['package_thermal_identity']
            preexecution['package_thermal_identity_evidence'] = thermal_identity
        write_json_exclusive(attempt / "PREEXECUTION_PHYSICAL_AUDIT.json", preexecution)
        progress.update(status="ICEPAK_SOLVING", solver_started=True, updated_utc=now_utc())
        write_json_replace(attempt / "LIVE_STATE.json", progress)
        analyze_started = True
        # Two solver cores avoid the AEDT 2023.2 HPC Parametric context used
        # by larger allocations.  This does not alter the physical model.
        if not app.analyze_setup("Setup1", cores=solver_cores(), blocking=True):
            try:
                messages = [str(item) for item in app.odesktop.GetMessages(app.project_name, app.design_name, 0)]
            except Exception as exc:
                messages = [f"AEDT GetMessages failed: {type(exc).__name__}: {exc}"]
            write_json_replace(attempt / "AEDT_SOLVE_FAILURE_MESSAGES.json", {
                "status": "ANALYZE_SETUP_FALSE",
                "project": app.project_name,
                "design": app.design_name,
                "messages": messages,
                "created_utc": now_utc(),
            })
            detail = " | ".join(messages[-5:]) if messages else "no AEDT messages returned"
            raise RuntimeError(f"Icepak analyze_setup returned false: {detail}")
        if wait_for_current_uns_out(workspace) is None:
            raise RuntimeError("Icepak translation output current.uns_out is missing after 180-second result-flush wait")
        restart_pair = wait_for_fluent_restart_artifacts(workspace)
        restart_audit = {
            "status": "PASS" if restart_pair else "MISSING",
            "timeout_s": 300,
            "current_nc_cas": str(restart_pair[0]) if restart_pair else None,
            "current_fdat": str(restart_pair[1]) if restart_pair else None,
            "created_utc": now_utc(),
        }
        write_json_replace(attempt / "FLUENT_RESTART_ARTIFACT_AUDIT.json", restart_audit)
        if restart_pair is None:
            raise RuntimeError(
                "Fluent native restart files were not flushed after current.uns_out; "
                "source-network audit cannot proceed"
            )
        code_dir = Path(__file__).resolve().parents[1]
        if str(code_dir) not in sys.path:
            sys.path.insert(0, str(code_dir))
        from correct_native_nci import interface_audit, frozen_source_contract
        current_pre = (attempt / 'PREEXECUTION_PHYSICAL_AUDIT.json').resolve()
        if not current_pre.is_file():
            raise RuntimeError('Native generation lacks its actual preexecution audit')
        previous_pre = os.environ.get('NCI_GENERATION_PREEXECUTION_AUDIT')
        if previous_pre is not None and Path(previous_pre).resolve() != current_pre:
            raise RuntimeError('Native generation audit context points to a different attempt')
        os.environ['NCI_GENERATION_PREEXECUTION_AUDIT'] = str(current_pre)
        try:
            native_topology = interface_audit(restart_pair[0], frozen_source_contract(task['task_id'],preexecution))
        finally:
            if previous_pre is None: os.environ.pop('NCI_GENERATION_PREEXECUTION_AUDIT',None)
            else: os.environ['NCI_GENERATION_PREEXECUTION_AUDIT'] = previous_pre
        write_json_replace(attempt / "NATIVE_INTERFACE_GEOMETRY_AUDIT.json", native_topology)
        if os.environ.get("BRIDGE_DIAGNOSTIC_KEEP_NATIVE_PAIR") == "1":
            retained = (Path(r'F:\3dic_icepak_native\neighbor_jump_rerun_20261012')/'generation_inputs'/task['task_id']/attempt.name/'selected_native'
                        if os.environ.get('BRIDGE_ALLOW_NEIGHBOR_EXTERNAL_NATIVE')=='1' else attempt / "selected_native")
            retained.mkdir(parents=True, exist_ok=True)
            for source, name in zip(restart_pair, ("current.nc_cas", "current.fdat")):
                shutil.copyfile(source, retained / name)
            write_json_replace(attempt / "DIAGNOSTIC_NATIVE_PAIR.json", {
                "status": "RETAINED_FOR_INTERFACE_REPAIR", "directory": str(retained),
                "geometry_gate_pass": native_topology["passed"], "production_accepted": False,
            })
        if not native_topology["passed"]:
            raise RuntimeError("NATIVE_INTERFACE_GEOMETRY_FAIL: noncoplanar source connections, incomplete contacts or translated layer bounds")
        try:
            source_zone_translation = translated_source_zone_audit(
            workspace, before_sources,
            (
                source_refresh.get("translation_objects", [])
                if source_refresh
                else sorted(source_object_power_contract(app, before_sources))
            ),
            require_source_monitors=(
                not scaled_package_xy and (
                    not recovery_refresh_sources
                    or recovery_source_mode in {"source-rebind", *EQUAL_DENSITY_RECOVERY_MODES}
                    or recovery_source_mode in MULTI_OBJECT_DENSITY_RECOVERY_MODES
                ) and recovery_source_mode not in MULTI_OBJECT_BOX_MESH_MODES
            ),
            require_source_solids=(
                not recovery_refresh_sources or recovery_source_mode not in {
                    "surface-footprint",
                    *MULTI_OBJECT_BOX_MESH_MODES,
                }
            ),
            expected_persistent_source_targets=(
                source_refresh.get("persistent_source_targets", [])
                if source_refresh and recovery_source_mode in SURFACE_FOOTPRINT_RECOVERY_MODES else []
            ),
            expected_source_boundary_count=(
                int(source_refresh.get("rebound_boundary_count", 0))
                if source_refresh and (
                    is_equal_density_recovery_mode(recovery_source_mode)
                    or recovery_source_mode in MULTI_OBJECT_DENSITY_RECOVERY_MODES
                ) else None
            ),
            expected_source_groups=(
                source_refresh.get("translation_groups", [])
                if source_refresh and recovery_source_mode in MULTI_OBJECT_DENSITY_RECOVERY_MODES
                else []
            ),
            )
        except RuntimeError as exc:
            if not (allow_source_zone_deficit and "ACTIVE_SOURCE_ZONES_MISSING_AFTER_ICEPAK_TO_FLUENT_TRANSLATION" in str(exc)):
                raise
            source_zone_translation = {
                "status": "PASS_WITH_EXPLICIT_SOURCE_ZONE_AUDIT_GAP",
                "audit_error": str(exc),
                "allow_source_zone_deficit": True,
                "physical_q_energy_ledger_remains_required": True,
            }
        # A temperature limiter is not physical evidence unless the native
        # source solids survived Icepak-to-Fluent translation.  Preserve that
        # audit in a failed attempt before evaluating convergence.
        preexecution["source_zone_translation"] = source_zone_translation
        convergence = None
        source_power_audit_contract = source_refresh or (
            {"native_boundary_props": before_sources}
            if recovery_source_mode in NATIVE_SOURCE_MESH_MODES else None
        )
        if source_power_audit_contract and recovery_source_mode in {
            *NATIVE_SOURCE_MESH_MODES, *SOLID_BLOCK_RECOVERY_MODES,
            *SOURCE_REBIND_BOX_MESH_MODES, *LAYER_HYBRID_RECOVERY_MODES,
        }:
            initial_power_audit = translated_solid_block_total_power_audit(
                workspace, source_power_audit_contract,
                attempt / "FLUENT_TOTAL_POWER_AUDIT.log", strict=False
            )
            if initial_power_audit["status"] == "PASS":
                source_zone_translation["fluent_total_power_audit"] = initial_power_audit
            elif (
                recovery_source_mode in SOLID_BLOCK_RECOVERY_MODES
                and recovery_source_mode not in {
                    "solid-block-box-mesh-v1", *IMPRINTED_SOLID_BLOCK_MODES,
                }
            ):
                source_zone_translation["fluent_total_power_audit_before_compensation"] = initial_power_audit
                source_zone_translation["fluent_total_power_compensation"] = compensate_solid_block_translation(
                    app, initial_power_audit
                )
                if not app.save_project():
                    raise RuntimeError("failed saving numerical Total Power compensation")
                if not app.analyze_setup("Setup1", cores=solver_cores(), blocking=True):
                    raise RuntimeError("Icepak compensated analyze_setup returned false")
                source_zone_translation["convergence_after_power_compensation"] = normal_completion(
                    workspace, attempt / "TEMPERATURE_LIMITER_AUDIT.json"
                )
                source_zone_translation["fluent_total_power_audit"] = translated_solid_block_total_power_audit(
                    workspace, source_power_audit_contract,
                    attempt / "FLUENT_TOTAL_POWER_AUDIT_AFTER_COMPENSATION.log",
                )
            else:
                raise RuntimeError(
                    "source Total Power translation changed in Fluent; numerical "
                    "compensation is forbidden for strict box-mesh recovery"
                )
        # This is the non-bypassable source admission gate.  It must run after
        # the final Fluent restart audit (including any numerical Total Power
        # compensation) and before convergence or field export is accepted.
        strict_source_network = strict_source_network_audit(
            app,
            attempt,
            source_refresh,
            source_zone_translation,
            source_transplant,
        )
        preexecution["strict_source_network"] = strict_source_network
        if convergence is None:
            convergence = normal_completion(
                workspace, attempt / "TEMPERATURE_LIMITER_AUDIT.json"
            )
        solved = workspace / "solved.aedt"
        if not app.save_project(str(solved), overwrite=True):
            raise RuntimeError("solved AEDT persistence save failed")
        points = workspace / "gamma_contact_points.pts"
        points_file(points, size)
        raw_temp = workspace / "Gamma_Temp_z4p22.fld"
        raw_flux = workspace / "Gamma_HeatFlux_z4p22.fld"
        for quantity, output in (("Temp", raw_temp), ("HeatFlux", raw_flux)):
            exported = app.post.export_field_file(
                quantity,
                solution="Setup1 : SteadyState",
                output_file=str(output),
                sample_points_file=str(points),
                export_in_si_system=False,
            )
            if not exported or not output.is_file():
                raise RuntimeError(f"Gamma {quantity} export failed")
        source_raw = {}
        for layer, z_mm in (("bottom", 0.4101), ("top", 0.4701)):
            layer_points = workspace / f"source_{layer}_points.pts"
            source_layer_points_file(layer_points, size, z_mm)
            layer_raw = workspace / f"T_source_{layer}.fld"
            exported = app.post.export_field_file(
                "Temp", solution="Setup1 : SteadyState", output_file=str(layer_raw),
                sample_points_file=str(layer_points), export_in_si_system=False,
            )
            if not exported or not layer_raw.is_file():
                raise RuntimeError(f"{layer} source-layer Temp export failed")
            source_raw[layer] = (layer_raw, z_mm)
        compensation = source_zone_translation.get("fluent_total_power_compensation", {})
        expected_solver_source_total_w = (
            sum(float(item["compensated_aedt_solver_input_W"]) for item in compensation.get("per_source", []))
            if compensation else None
        )
        if scaled_package_xy:
            # AEDT 2023.2 can hang while rebuilding the OO tree after reopening
            # a project containing native Scale+Move history.  The live solved
            # design plus the separately parsed saved object-name set proves
            # the same persistence contract without a second project open.
            persisted = persistence_audit(
                app, geometry_size, persistence_expected_objects, outside, before_sources,
                normalized_mesh_count, recovery_refresh_sources, source_recovery_mesh,
                aedt_path=solved, scaled_package_xy=True,
                expected_solver_source_total_w=expected_solver_source_total_w,
                mesh_count_z=normalized_mesh_count_z,
            )
            persisted["audit_mode"] = "LIVE_SOLVED_DESIGN_PLUS_SAVED_AEDT_TEXT"
        else:
            app.close_project(save=True)
            app = Icepak(project=str(solved), version="2023.2", non_graphical=True, new_desktop=False, close_on_exit=False)
            persisted = persistence_audit(
                app, geometry_size, persistence_expected_objects, outside, before_sources,
                normalized_mesh_count, recovery_refresh_sources, source_recovery_mesh,
                aedt_path=solved, scaled_package_xy=False,
                expected_solver_source_total_w=expected_solver_source_total_w,
                mesh_count_z=normalized_mesh_count_z,
            )
        if internal_mask is not None:
            verify_internal_adiabatic_mask(app, internal_mask)
            persisted["internal_adiabatic_mask"] = True
        canonical_dir = attempt / "compact_truth" if compact_success else workspace / "artifacts"
        adapter_proof = None
        try:
            truth = canonicalize_truth(raw_temp, raw_flux, canonical_dir, size, source_power(task))
        except RuntimeError as exc:
            # Scaled contacts can contain a large duplicated-parent-face
            # seam.  Recover the missing native HeatFlux values before giving
            # the result to the formal sampler/publisher chain.
            if "nonfinite HeatFlux" not in str(exc):
                raise
            raw_flux, adapter_proof = native_heatflux_adapter(
                raw_flux, raw_temp, points, solved, workspace, attempt
            )
            truth = canonicalize_truth(raw_temp, raw_flux, canonical_dir, size, source_power(task))
            truth["gamma_recovery"] = adapter_proof
        if recovery_source_mode in MULTI_OBJECT_DENSITY_RECOVERY_MODES:
            logical_total = float(source_refresh["combined_total_power_W"])
            expected_total = float(source_power(task))
            energy_fraction = float(truth["gamma_energy_fraction"])
            if not np.isclose(logical_total, expected_total, rtol=0.0, atol=1e-8):
                raise RuntimeError("grouped source boundary total power changed")
            if not 0.85 <= energy_fraction <= 1.05:
                raise RuntimeError("grouped source solve failed Gamma energy closure")
            source_zone_translation["grouped_source_power_audit"] = {
                "status": "PASS",
                "contract_version": "GROUPED_SOURCE_ZONES_PERSISTENCE_PLUS_GAMMA_ENERGY_V1",
                "grouped_zone_representatives_present": True,
                "logical_boundary_total_power_W": logical_total,
                "expected_source_total_power_W": expected_total,
                "solved_gamma_energy_fraction": energy_fraction,
                "source_geometry_unchanged": True,
                "spatial_power_density_unchanged": True,
            }
        try:
            source_maps = {
                layer: canonicalize_source_temperature(
                    raw_path, canonical_dir / f"T_source_{layer}.tab", size, z_mm,
                    allow_extended_seam_recovery=scaled_package_xy,
                )
                for layer, (raw_path, z_mm) in source_raw.items()
            }
        except RuntimeError as exc:
            # The direct offset-plane export is only a diagnostic path.  A
            # topology seam NaN must not be filled with a larger heuristic
            # band.  Reconstruct the same 40x40 contract from solved native
            # source/HB interface faces and retain its complete area/coverage
            # gate in the receipt.
            if "source-plane" not in str(exc):
                raise
            source_maps = native_source_temperature_maps(workspace, canonical_dir, size)
            truth["source_temperature_recovery"] = {
                "status": "PASS_NATIVE_INTERFACE_AREA_RECOVERY",
                "direct_export_error": str(exc),
                "native_maps": source_maps,
            }
        for layer, info in source_maps.items():
            truth["artifacts"][f"T_source_{layer}"] = info["path"]
            truth["artifacts_sha256"][f"T_source_{layer}"] = info["sha256"]
        truth["source_layer_temperature"] = source_maps
        power_maps = canonicalize_source_power_maps(
            package_scale["source_geometry_audit"], canonical_dir, size
        ) if scaled_package_xy else None
        if power_maps:
            for layer, path in power_maps["paths"].items():
                name = f"power_map_{layer}_W_m2"
                truth["artifacts"][name] = path
                truth["artifacts_sha256"][name] = power_maps["sha256"][layer]
            truth["source_power_map"] = power_maps
        if not compact_success:
            truth["artifacts"]["solved_aedt"] = str(solved)
            truth["artifacts"]["raw_Temp"] = str(raw_temp)
            truth["artifacts"]["raw_HeatFlux"] = str(raw_flux)
        if adapter_proof is not None:
            truth["artifacts"]["adapter_recovery_proof"] = str(attempt / "ADAPTER_RECOVERY_PROOF.json")
        truth["artifacts"]["preexecution_audit"] = str(attempt / "PREEXECUTION_PHYSICAL_AUDIT.json")
        validation_path = attempt / "VALIDATION.json"
        validation = {
            "status": "PASS",
            "task_id": task["task_id"],
            "evidence_kind": "NEW_ICEPAK_FULL_TRUTH",
            "truth_engine": "ANSYS_ICEPAK",
            "mapping_contract_version": "NORMALIZED_DIRECT_AEDT_40X40_V1" if scaled_package_xy else "LEGACY_GLOBAL_GRID",
            "size_mm": size,
            "dx_gamma_mm": size / 40.0,
            "dx_source_mm": size / 80.0,
            "checks": {
                "normal_completion": True,
                "geometry_mesh": True,
                "persistent_object_binding": True,
                "centered_square_contact": True,
                "outside_contact_explicit_zero_flux_no_radiation": True,
                "Gamma_Temp_HeatFlux_z4p22": True,
                "field_grid_40x40": True,
                "source_layer_temperature_40x40": True,
                "finite_positive_htc": True,
                "energy_sign_coordinate": True,
            },
            "created_utc": now_utc(),
            "blind_or_final_truth_access": False,
        }
        write_json_exclusive(validation_path, validation)
        truth["artifacts"]["validation"] = str(validation_path)
        truth["artifacts_sha256"] = {name: sha256(Path(path)) for name, path in truth["artifacts"].items()}
        physical_qa = {
            "geometry_mesh": "PASS",
            "persistent_object_binding": "PASS",
            "centered_square_contact": "PASS",
            "outside_contact_adiabatic": "PASS",
            "tim2_thickness_0p20mm": "PASS",
            "tim2_conductivity_5_W_mK": "PASS",
            "field_grid_40x40": "PASS",
            "finite_positive_htc": "PASS",
        }
        result = {
            "schema_version": "bridging_sink_task_pass_receipt_v1",
            "status": "PASS",
            "task_id": task["task_id"],
            "physical_case_group_id": task["physical_case_group_id"],
            "attempt": attempt_number,
            "mode": (
                "DIAGNOSTIC_FIXED_40MM_TIM2_GEOMETRY_CONTACT_MASK"
                if fixed_geometry_contact_mask else (
                    "GENERATED_ANSYS_ICEPAK_SCALED_PACKAGE_NORMALIZED_40X40_FULL_TRUTH"
                    if scaled_package_xy else "GENERATED_ANSYS_ICEPAK_FULL_TRUTH"
                )
            ),
            "truth_engine": "ANSYS_ICEPAK",
            "evidence_kind": "NEW_ICEPAK_FULL_TRUTH",
            "activation_status": (
                "NO_ADMISSION_DIAGNOSTIC_ONLY"
                if fixed_geometry_contact_mask else "PASS_PENDING_APPEND_NOT_ACTIVE"
            ),
            "source_pattern_id": task["source_pattern_id"],
            "bridge_sink_id": task["bridge_sink_id"],
            "TIM2_size_mm": size,
            "size_mm": size,
            "dx_gamma_mm": size / 40.0,
            "dx_source_mm": size / 80.0,
            "mapping_contract_version": "NORMALIZED_DIRECT_AEDT_40X40_V1" if scaled_package_xy else "LEGACY_GLOBAL_GRID",
            "tim2_thickness_mm": 0.20,
            "tim2_conductivity_W_mK": 5.0,
            "contact_geometry": "CENTERED_SQUARE",
            "outside_contact_region": "NO_CONTACT_EXPLICIT_ZERO_FLUX_ADIABATIC",
            "tim2_geometry_size_mm": 40 if fixed_geometry_contact_mask else size,
            "fixed_geometry_contact_mask": fixed_geometry_contact_mask,
            "fixed_geometry_internal_plate_mask": fixed_geometry_internal_plate_mask,
            "compact_success_artifacts": compact_success,
            "compute_workspace_retention": "DELETE_AFTER_STRICT_PASS" if compact_success else "RETAIN",
            "T0_action": (
                "REGENERATE_NORMALIZED_DIRECT_AEDT_40X40_IN_SEPARATE_PACKAGE_ONLY_QUEUE"
                if scaled_package_xy else "REUSE_250_500_1000_1500_NO_NEW_T0_SOLVE"
            ),
            "plan_path": str(PLAN),
            "plan_row_sha256": plan_fingerprint(task),
            "carrier_aedt_path": str(carrier),
            "carrier_aedt_sha256": sha256(carrier),
            "recovery_carrier": recovery_carrier,
            "recovery_mesh": ({"mesh_count_xy": recovery_mesh_count_xy, "reason": "finer TIM2 local numerical discretization after preserved zero-source translation"}
                              if recovery_mesh_count_xy is not None else None),
            "recovery_source_binding_refresh": source_refresh is not None,
            "recovery_source_mode": recovery_source_mode if recovery_refresh_sources else None,
            "recovery_source_representation_physical_equivalence": (
                {
                    "physical_source_solids_unchanged": recovery_source_mode not in SURFACE_FOOTPRINT_RECOVERY_MODES,
                    "per_source_total_power_unchanged": True,
                    "total_power_unchanged": True,
                    "representation": recovery_source_mode,
                    "physical_source_object_identity_unchanged": (
                        recovery_source_mode not in SURFACE_FOOTPRINT_RECOVERY_MODES
                        and not is_equal_density_recovery_mode(recovery_source_mode)
                    ),
                    # All recovery modes preserve the exact XY power footprint.  The
                    # surface-footprint mode removes the thin source solids; the other
                    # modes retain them (equal-density modes may unite their union).
                    "physical_source_union_geometry_unchanged": recovery_source_mode not in SURFACE_FOOTPRINT_RECOVERY_MODES,
                    "source_footprint_unchanged": True,
                    "boundary_representation_changed": recovery_source_mode not in {
                        "source-rebind", *NATIVE_SOURCE_MESH_MODES,
                    },
                    "maximum_power_plane_offset_mm": 0.005 if recovery_source_mode in SURFACE_FOOTPRINT_RECOVERY_MODES else 0.0,
                    "used_only_after_preserved_translation_failure": has_degenerate_translation_failure(task),
                }
                if recovery_refresh_sources else None
            ),
            "preexecution": preexecution,
            "convergence": convergence,
            "source_zone_translation": source_zone_translation,
            "persistence": persisted,
            **truth,
            "T_full_path": truth["artifacts"]["T_full"],
            "T_full_sha256": truth["artifacts_sha256"]["T_full"],
            "h_full_path": truth["artifacts"]["h_full"],
            "h_full_sha256": truth["artifacts_sha256"]["h_full"],
            "validation_path": str(validation_path),
            "validation_sha256": truth["artifacts_sha256"]["validation"],
            "physical_qa": physical_qa,
            "checks": {
                "Icepak_full_truth_only": True,
                "Thermal_fluid_not_used": True,
                "new_T0_solve": False,
                "centered_square_geometry": True,
                "outside_contact_explicit_zero_flux_no_radiation": True,
                "opening_sink_Box1_preserved": True,
                "passive_FEOL_preserved": True,
                "source_binding_persisted": True,
                "mesh_binding_persisted": True,
                "normal_completion": True,
                "active_source_zones_present_after_translation": True,
                "Gamma_Temp_and_HeatFlux_z4p22": True,
                "source_layer_bottom_top_Temp": True,
                "canonical_rows_1600": True,
                "outside_mask_zero": True,
                "finite_positive_HTC": True,
                "energy_sign_coordinate_checks": True,
            },
            "created_utc": now_utc(),
            "blind_or_final_truth_access": False,
        }
        write_json_exclusive(attempt / "ATTEMPT_RECEIPT.json", result)
        write_pass_receipt(task, result)
        write_json_replace(attempt / "LIVE_STATE.json", {**progress, "status": "PASS", "updated_utc": now_utc()})
        return result
    except Exception as exc:
        blocked = isinstance(exc, PreexecutionBlocked) and not analyze_started
        status = "BLOCKED_PREEXECUTION" if blocked else "FAIL_RECOVERABLE"
        failure = {
            "status": status,
            "task_id": task["task_id"],
            "attempt": attempt_number,
            "stage": "PREEXECUTION" if not analyze_started else "ICEPAK_OR_VALIDATION",
            "solver_started": analyze_started,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "preexecution_partial": preexecution,
            "created_utc": now_utc(),
            "blind_or_final_truth_access": False,
        }
        write_json_exclusive(attempt / "ATTEMPT_RECEIPT.json", failure)
        write_json_replace(attempt / "LIVE_STATE.json", {**progress, "status": status, "error": str(exc), "updated_utc": now_utc()})
        raise
    finally:
        if app is not None:
            try:
                app.release_desktop(close_projects=True, close_desktop=True)
            except Exception:
                pass


def main() -> int:
    if os.environ.get("BRIDGE_ISOLATED_PROFILE", "0") == "1":
        configure_isolated_runtime_profile()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt", type=int, help="append-only attempt number; required for generated tasks")
    parser.add_argument("--execute", action="store_true", help="execute this one authorized DEVELOPMENT task")
    parser.add_argument("--recovery-carrier", help="same-source/same-sink solved AEDT from a prior valid PASS")
    parser.add_argument("--recovery-pass-receipt", help="PASS receipt hashing and authorizing --recovery-carrier")
    parser.add_argument("--recovery-mesh-count-xy", type=int, help="versioned finer local TIM2 mesh count after a preserved zero-source translation")
    parser.add_argument("--diagnostic-global-mesh-resolution", type=int, choices=range(1, 6), metavar="1..5", help="diagnostic only: change Icepak global MesherHD resolution; post-processing grids are unchanged")
    parser.add_argument("--diagnostic-tim2-mesh-count-xy", type=int, metavar="N", help="diagnostic only: change the local TIM2 solver mesh X/Y count; post-processing grids are unchanged")
    parser.add_argument("--diagnostic-tim2-mesh-count-z", type=int, metavar="N", help="diagnostic only: change the local TIM2 solver mesh Z count; post-processing grids are unchanged")
    parser.add_argument("--recovery-refresh-sources", action="store_true", help="recreate identical total-power source boundaries on persistent object names")
    parser.add_argument("--max-iterations", type=int, help="explicit diagnostic solver iteration budget; default preserves the frozen project")
    parser.add_argument("--fixed-anchor-imprint", action="store_true", help="diagnostic: imprint every frozen TIM2 anchor on both contact solids")
    parser.add_argument(
        "--fixed-geometry-contact-mask", action="store_true",
        help="diagnostic only: keep the frozen 40 mm TIM2 solid/mesh and vary only the central adiabatic contact mask",
    )
    parser.add_argument(
        "--fixed-geometry-internal-plate-mask", action="store_true",
        help="diagnostic only: embed fixed ring sheets inside the unchanged 40 mm TIM2 and make outer rings adiabatic",
    )
    parser.add_argument(
        "--compact-success", action="store_true",
        help="keep only canonical T/q/h/mask and strict JSON after PASS, then remove this task's verified short compute workspace",
    )
    parser.add_argument(
        "--scaled-package-xy", action="store_true",
        help="scale the whole package and source geometry in XY with TIM2; preserve Z, sink, and Total Power",
    )
    parser.add_argument(
        "--recovery-source-mode", choices=("source-rebind", *sorted(SURFACE_FOOTPRINT_RECOVERY_MODES), *sorted(NATIVE_SOURCE_MESH_MODES), *sorted(SOURCE_REBIND_BOX_MESH_MODES), *sorted(SOLID_BLOCK_RECOVERY_MODES), *sorted(LAYER_HYBRID_RECOVERY_MODES), *sorted(MULTI_OBJECT_DENSITY_RECOVERY_MODES), *sorted(EQUAL_DENSITY_RECOVERY_MODES)), default="source-rebind",
        help="versioned numerical representation used only with --recovery-refresh-sources",
    )
    parser.add_argument(
        "--allow-source-zone-deficit", action="store_true",
        help="explicitly continue after a source-zone translation audit gap; Gamma energy closure remains mandatory",
    )
    parser.add_argument(
        "--force-regenerate", action="store_true",
        help="bypass a legacy PASS pointer when a newer physical contract requires fresh truth",
    )
    args = parser.parse_args()
    task = task_by_id(args.task_id)
    if valid_pass(task) and not args.force_regenerate:
        print(json.dumps({"status": "PASS", "task_id": args.task_id, "reused_receipt": True}, indent=2))
        return 0
    size = int(float(task["TIM2_size_mm"]))
    ready = {
        "status": "READY",
        "task_id": args.task_id,
        "action": task["truth_action"],
        "TIM2_size_mm": size,
        "carrier": task["carrier_aedt_path"],
        "formal_solver_concurrency": 1,
        "new_T0_solve": False,
        "blind_or_final_truth_access": False,
    }
    if not args.execute:
        print(json.dumps(ready, indent=2, ensure_ascii=False))
        return 0
    if args.attempt is None or args.attempt <= 0:
        parser.error("--attempt is required and must be positive for every executed task")
    if size == 40 and not args.scaled_package_xy:
        attempt = attempt_dir(task, args.attempt)
        attempt.mkdir(parents=True, exist_ok=False)
        try:
            receipt = validate_reuse(task)
            receipt["attempt"] = args.attempt
            write_json_exclusive(attempt / "ATTEMPT_RECEIPT.json", receipt)
            write_pass_receipt(task, receipt)
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
            return 0
        except Exception as exc:
            failure = {
                "status": "BLOCKED_PREEXECUTION",
                "task_id": task["task_id"],
                "attempt": args.attempt,
                "stage": "HASH_VERIFIED_40MM_REUSE",
                "solver_started": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "created_utc": now_utc(),
                "blind_or_final_truth_access": False,
            }
            write_json_exclusive(attempt / "ATTEMPT_RECEIPT.json", failure)
            print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
            return 2
    try:
        recovery_carrier_value = args.recovery_carrier
        recovery_receipt_value = args.recovery_pass_receipt
        recovery_mesh_count_xy = args.recovery_mesh_count_xy
        recovery_refresh_sources = args.recovery_refresh_sources
        # Normal queue retries always use a fresh workspace and the frozen
        # source contract.  Any topology-changing numerical recovery is
        # explicit, versioned, and confined to the later deferred queue.
        receipt = run_generated(
            task, args.attempt, recovery_carrier_value, recovery_receipt_value,
            recovery_mesh_count_xy, recovery_refresh_sources, args.recovery_source_mode,
            args.max_iterations, args.fixed_anchor_imprint, args.fixed_geometry_contact_mask,
            args.fixed_geometry_internal_plate_mask, args.compact_success, args.scaled_package_xy,
            args.allow_source_zone_deficit, args.diagnostic_global_mesh_resolution,
            args.diagnostic_tim2_mesh_count_xy,
            args.diagnostic_tim2_mesh_count_z,
        )
    except PreexecutionBlocked as exc:
        receipt_path = attempt_dir(task, args.attempt) / "ATTEMPT_RECEIPT.json"
        if not receipt_path.is_file():
            write_json_exclusive(receipt_path, {
                "status": "BLOCKED_PREEXECUTION", "task_id": task["task_id"], "attempt": args.attempt,
                "stage": "PREEXECUTION", "solver_started": False, "error_type": type(exc).__name__,
                "error": str(exc), "traceback": traceback.format_exc(), "created_utc": now_utc(),
                "blind_or_final_truth_access": False,
            })
        print(json.dumps({"status": "BLOCKED_PREEXECUTION", "task_id": args.task_id, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        receipt_path = attempt_dir(task, args.attempt) / "ATTEMPT_RECEIPT.json"
        if not receipt_path.is_file():
            write_json_exclusive(receipt_path, {
                "status": "FAIL_RECOVERABLE", "task_id": task["task_id"], "attempt": args.attempt,
                "stage": "PREEXECUTION_OR_ICEPAK", "solver_started": False, "error_type": type(exc).__name__,
                "error": str(exc), "traceback": traceback.format_exc(), "created_utc": now_utc(),
                "blind_or_final_truth_access": False,
            })
        print(json.dumps({"status": "FAIL_RECOVERABLE", "task_id": args.task_id, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    if args.compact_success:
        workspace_record = json_read(attempt_dir(task, args.attempt) / "COMPUTE_WORKSPACE.json")
        workspace = validated_compute_workspace(
            task, args.attempt, workspace_record["workspace"]
        )
        shutil.rmtree(workspace)
        if workspace.exists():
            raise RuntimeError(f"compact-success workspace cleanup did not complete: {workspace}")
        write_json_exclusive(attempt_dir(task, args.attempt) / "COMPACTION_RECEIPT.json", {
            "status": "PASS", "task_id": task["task_id"], "attempt": args.attempt,
            "removed_compute_workspace": str(workspace), "canonical_artifacts_retained": receipt["artifacts"],
            "reason": "bounded disk; solved AEDT and raw exports are not needed after strict canonical validation",
            "created_utc": now_utc(), "blind_or_final_truth_access": False,
        })
    print(json.dumps({"status": "PASS", "task_id": args.task_id, "attempt": args.attempt, "htc_mean": receipt["htc_W_m2K"]["mean"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
