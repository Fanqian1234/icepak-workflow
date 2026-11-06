"""Read-only material-ray probe for a Fluent *text* volume mesh.

This is a diagnostic adapter for mesh-only AEDT trials, not a production
acceptance gate.  It reports the same geometric ray quantities as the
existing binary `native_fin_gate` without launching Fluent or a CHT solve.
Unsupported or incomplete mesh records fail closed.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import closing
from pathlib import Path
import argparse
import json
import math
import mmap
import re
import sys

import numpy as np


_SECTION = re.compile(rb"(?m)^\([ \t]*(\d+)[ \t]+\(([^)]*)\)")
_ZONE = re.compile(rb"(?m)^\(39 \((\d+) (\S+) (\S+) 1\)\(")
_HEX_FIELD = re.compile(rb"[0-9a-fA-F]+")


def _merge(intervals):
    result = []
    for lo, hi in sorted(intervals):
        if result and lo <= result[-1][1] + 1e-8:
            result[-1][1] = max(result[-1][1], hi)
        else:
            result.append([lo, hi])
    return result


def _rays_from_hits(rays, hits, cell_zone, solid_ids, fluid_ids):
    reports = []
    for ray, values in zip(rays, hits):
        intervals = defaultdict(list)
        odd = []
        for cell, ys in values.items():
            ys = sorted(set(round(float(y), 8) for y in ys))
            zone_id = int(cell_zone[cell])
            label = "sink_solid" if zone_id in solid_ids else "air_fluid"
            if len(ys) % 2:
                odd.append({"cell": int(cell), "zone": label, "hits": ys})
                continue
            intervals[label].extend(zip(ys[::2], ys[1::2]))
        merged = {name: _merge(intervals[name]) for name in ("sink_solid", "air_fluid")}
        inside = {
            name: sum(max(0, min(hi, ray[4]) - max(lo, ray[3])) for lo, hi in rows)
            for name, rows in merged.items()
        }
        overlap = sum(
            max(0, min(a1, b1, ray[4]) - max(a0, b0, ray[3]))
            for a0, a1 in merged["sink_solid"]
            for b0, b1 in merged["air_fluid"]
        )
        reports.append({
            "name": ray[0], "x_mm": ray[1], "z_mm": ray[2],
            "cells_with_intersections": len(values),
            "odd_intersection_cells": odd,
            "merged_material_intervals_y_mm": merged,
            "material_length_inside_fin_y_span_mm": inside,
            "solid_fluid_overlap_length_mm": overlap,
        })
    return reports


def scan_ascii_volume_mesh(mesh_path, rays):
    """Return ``(ray_reports, examined_faces)``; never assume an ASCII mesh is valid.

    ``rays`` are the exact ``fin_rays`` tuples used by the native binary gate:
    (label, x_mm, z_mm, y_min_mm, y_max_mm).  Every required physical cell,
    face and node must be present; a surface-only mesh cannot pass.
    """
    if not rays:
        raise ValueError("No frozen-geometry fin/channel rays")
    path = Path(mesh_path)
    with path.open("rb") as file, closing(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)) as raw:
        if b"\0" in raw[: min(65536, len(raw))]:
            raise ValueError("Expected legacy ASCII Fluent .msh, got binary data")
        headers = list(_SECTION.finditer(raw))
        if not headers:
            raise ValueError("No standard ASCII Fluent sections")
        sections = []
        for i, match in enumerate(headers):
            end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
            sections.append((int(match[1]), match[2].split(), match.end(), end))
        zones = {
            int(match[1]): (match[2].decode("ascii"), match[3].decode("ascii"))
            for match in _ZONE.finditer(raw)
        }
        solid_ids = {
            zid for zid, (_, name) in zones.items()
            if name.lower().startswith("heatsink_") and name.lower().endswith("_solid")
        }
        fluid_ids = {
            zid for zid, (_, name) in zones.items()
            if name.lower() in ("box1_fluid", "air_domain_fluid")
            or name.lower().startswith("air_domain") and name.lower().endswith("_fluid")
        }
        if not solid_ids or not fluid_ids:
            raise ValueError("ASCII mesh lacks named heatsink and air volume zones")

        def header_values(tokens):
            return [int(value, 16) for value in tokens]

        node_total = next((header_values(a)[2] for idx, a, _, _ in sections
                           if idx == 10 and header_values(a)[0] == 0), None)
        cell_total = next((header_values(a)[2] for idx, a, _, _ in sections
                           if idx == 12 and header_values(a)[0] == 0), None)
        if not node_total or not cell_total:
            raise ValueError("ASCII volume mesh has no complete node/cell declarations")
        nodes = np.full((node_total + 1, 3), np.nan, dtype=np.float64)
        cell_zone = np.zeros(cell_total + 1, dtype=np.int32)
        for idx, tokens, start, end in sections:
            if idx not in (10, 12):
                continue
            info = header_values(tokens)
            if idx == 12 and info[0] != 0:
                if len(info) < 4 or info[2] > cell_total or info[1] < 1:
                    raise ValueError("Cell zone exceeds ASCII declaration")
                if np.any(cell_zone[info[1]:info[2]+1]):
                    raise ValueError("Duplicate or overlapping cell zone IDs")
                cell_zone[info[1]:info[2]+1] = info[0]
            elif idx == 10 and info[0] != 0:
                if len(info) < 4 or info[2] > node_total or info[1] < 1:
                    raise ValueError("Node zone exceeds ASCII declaration")
                # Only ordinary real 3D node records can prove material rays.
                if len(info) >= 5 and info[4] != 3:
                    raise ValueError("Non-3D node coordinates")
                data = raw[start:end].replace(b"(", b" ").replace(b")", b" ")
                xyz = np.fromstring(data.decode("ascii"), sep=" ", dtype=np.float64)
                expected = (info[2]-info[1]+1)*3
                if len(xyz) != expected:
                    raise ValueError(f"Incomplete node coordinates: {len(xyz)} != {expected}")
                nodes[info[1]:info[2]+1] = xyz.reshape(-1, 3)*1000.0
        if not np.isfinite(nodes[1:]).all() or not np.all(cell_zone[1:]):
            raise ValueError("Incomplete physical volume node or cell encoding")

        # Remove NCI intersection-child faces and hanging-node parent faces,
        # matching the binary reader's exclusion of nonphysical duplicates.
        # Section 61 and face-tree section 59 are defined in Fluent's legacy
        # mesh format. Unknown cell-tree topology still fails closed.
        if any(idx == 58 for idx, _, _, _ in sections):
            raise ValueError("Unresolved adapted cell tree in ASCII mesh")
        nonphysical_face_ids = set()
        metric_zone_ids = set()
        for idx, tokens, start, end in sections:
            if idx not in (59, 61, 67):
                continue
            info = header_values(tokens)
            if idx == 67:
                if len(info) < 1:
                    raise ValueError("Malformed ASCII face-metric section")
                metric_zone_ids.add(info[0])
            elif idx == 61:
                if len(info) != 2:
                    raise ValueError("Malformed NCI parent-face index range")
                data = raw[start:end].replace(b"(", b" ").replace(b")", b" ")
                links = [int(item, 16) for item in data.split()]
                if len(links) != (info[1]-info[0]+1)*2:
                    raise ValueError("Incomplete NCI parent links")
                nonphysical_face_ids.update(
                    info[0]+i for i in range(info[1]-info[0]+1)
                    if links[2*i] or links[2*i+1]
                )
            else:
                if len(info) != 4:
                    raise ValueError("Malformed hanging-node face tree")
                nonphysical_face_ids.update(range(info[0],info[1]+1))
        x_groups = defaultdict(list)
        for index, ray in enumerate(rays):
            x_groups[float(ray[1])].append(index)
        x_positions = sorted(x_groups)
        hits = [defaultdict(list) for _ in rays]
        examined = 0
        present_face_zones = set()
        for idx, tokens, start, end in sections:
            if idx != 13:
                continue
            info = header_values(tokens)
            if info[0] == 0:
                continue
            if len(info) != 5:
                raise ValueError("Unsupported ASCII face-zone header")
            zone, first, last, _, face_type = info
            if face_type not in (0, 3, 4, 5):
                raise ValueError("Unsupported 3D face type")
            present_face_zones.add(zone)
            # ASCII Fluent records are whitespace-delimited; line breaks
            # are not reliable boundaries for individual face records.
            # Match directly against the mmap range to avoid a giant copy.
            fields = (int(m.group(), 16) for m in
                      _HEX_FIELD.finditer(raw, start, end))
            for face_id in range(first, last+1):
                try:
                    n = next(fields) if face_type in (0, 5) else face_type
                    if n < 3 or n > 64:
                        raise ValueError("Invalid ASCII face vertex count")
                    vertices = [next(fields) for _ in range(n)]
                    c0, c1 = next(fields), next(fields)
                except StopIteration as err:
                    raise ValueError("Incomplete ASCII face connectivity") from err
                if any(v < 1 or v > node_total for v in vertices) or any(
                    c < 0 or c > cell_total for c in (c0, c1)
                ):
                    raise ValueError("Face references missing physical node/cell")
                if face_id in nonphysical_face_ids or zone in metric_zone_ids:
                    continue
                if not any(c and int(cell_zone[c]) in solid_ids | fluid_ids for c in (c0, c1)):
                    continue
                pts = nodes[np.asarray(vertices, dtype=np.int64)]
                xmin, xmax = float(pts[:,0].min()), float(pts[:,0].max())
                zmin, zmax = float(pts[:,2].min()), float(pts[:,2].max())
                startx = bisect_left(x_positions, xmin-1e-10)
                endx = bisect_right(x_positions, xmax+1e-10)
                if startx == endx:
                    continue
                examined += 1
                a = pts[0]
                for x in x_positions[startx:endx]:
                    for rayid in x_groups[x]:
                        z = float(rays[rayid][2])
                        if not (zmin-1e-10 <= z <= zmax+1e-10):
                            continue
                        for j in range(1, len(pts)-1):
                            v, w = pts[j]-a, pts[j+1]-a
                            den = v[0]*w[2]-v[2]*w[0]
                            if abs(den) <= 1e-14:
                                continue
                            alpha = ((x-a[0])*w[2]-(z-a[2])*w[0])/den
                            beta = (v[0]*(z-a[2])-v[2]*(x-a[0]))/den
                            if not (alpha >= -1e-10 and beta >= -1e-10
                                    and alpha+beta <= 1+1e-10):
                                continue
                            y = float(a[1]+alpha*v[1]+beta*w[1])
                            for c in set((c0, c1)) - {0}:
                                if int(cell_zone[c]) in solid_ids | fluid_ids:
                                    hits[rayid][c].append(y)
            if next(fields, None) is not None:
                raise ValueError("Unexpected trailing ASCII face connectivity")
        if not present_face_zones or not examined:
            raise ValueError("No physical Fluent volume faces intersect the selected rays")
        return _rays_from_hits(rays, hits, cell_zone, solid_ids, fluid_ids), examined


def summarize_rays(rays, reports, *, atol_mm=1e-7):
    """Mesh-only diagnostics; not the formal CHT native-science receipt."""
    failures = []
    for ray, row in zip(rays, reports):
        name, _, _, ymin, ymax = ray
        span = ymax-ymin
        lengths = row["material_length_inside_fin_y_span_mm"]
        solid = lengths["sink_solid"]
        fluid = lengths["air_fluid"]
        if name.endswith("_solid"):
            good = abs(solid-span) <= 1e-6 and fluid <= atol_mm
        else:
            good = abs(fluid-span) <= 1e-6 and solid <= atol_mm
        if not (good and row["cells_with_intersections"]
                and row["solid_fluid_overlap_length_mm"] <= atol_mm
                and not row["odd_intersection_cells"]
                and all(math.isfinite(x) for x in (span, solid, fluid))):
            failures.append({"ray": name, "solid_mm": solid,
                             "fluid_mm": fluid,
                             "overlap_mm": row["solid_fluid_overlap_length_mm"],
                             "odd_cells": len(row["odd_intersection_cells"])})
    return {"status": "MESH_ONLY_RAY_CANDIDATE_PASS" if not failures else "MESH_ONLY_RAY_FAILED",
            "ray_count": len(rays), "failed_count": len(failures),
            "failures": failures[:30], "not_formal_CHT_acceptance": True}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only material-ray evidence from an AEDT-generated ASCII Fluent volume mesh"
    )
    parser.add_argument("--case", required=True, dest="case_id")
    parser.add_argument("--mesh", required=True, type=Path)
    args = parser.parse_args(argv)
    # Reuse frozen formal geometry and ray definition, never a local geometry copy.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "icepak_workflow"))
    from case_spec import case_by_id
    from geometry import plan_case_geometry
    from native_fin_gate import fin_rays

    plan = plan_case_geometry(case_by_id(args.case_id))
    boxes = {p.name: p.bbox_mm for p in plan.solids if p.role == "Fin"}
    rays = fin_rays(boxes)
    reports, examined = scan_ascii_volume_mesh(args.mesh, rays)
    summary = summarize_rays(rays, reports)
    print(json.dumps({"case_id": args.case_id, "mesh": str(args.mesh),
                      "examined_faces": examined, **summary}, indent=2))
    return 0 if summary["failed_count"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

