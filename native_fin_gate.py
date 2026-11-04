"""Native Fluent mesh admission for every physical fin and air channel.

Science contract ported from the previously validated finite material-ray
audit; no imports, hashes, recovery branches or runtime dependence on legacy
production files. Finite rays reject missing/replaced fins but do not by
themselves certify convergence, energy closure or the full flow volume.
"""
from __future__ import annotations

import collections
import math
import mmap
import re
import struct
from pathlib import Path

def fin_rays(fin_boxes, selected=None, fractions=(0.2113248654, 0.51789, 0.7886751346), channel_fraction=0.371234567):
    fins = sorted(fin_boxes.items(), key=lambda item: item[1][0])
    selected = set(fin_boxes if selected is None else selected)
    if not selected or not selected <= set(fin_boxes):
        raise ValueError('Missing actual fin geometry')
    if not all((math.isfinite(f) and 0 < f < 1 for f in fractions)) or not (math.isfinite(channel_fraction) and 0 < channel_fraction < 1):
        raise ValueError('Section fractions must be strictly inside real fin/channel')
    rays = []
    for name, b in fins:
        if len(b) != 6 or not all((b[j] < b[j + 3] for j in range(3))):
            raise ValueError('Invalid actual fin box')
        if name in selected:
            for f in fractions:
                rays.append((name + '_solid', b[0] + 0.5234567 * (b[3] - b[0]), b[2] + f * (b[5] - b[2]), b[1], b[4]))
    for (left, a), (right, b) in zip(fins, fins[1:]):
        if not {left, right} & selected:
            continue
        if a[3] >= b[0]:
            raise ValueError('Frozen fin gap is not positive')
        ymin, ymax = (max(a[1], b[1]), min(a[4], b[4]))
        zmin, zmax = (max(a[2], b[2]), min(a[5], b[5]))
        if ymin >= ymax or zmin >= zmax:
            continue
        for f in fractions:
            # A channel ray at the exact midpoint crossed a native HDM
            # face/vertex in otherwise valid case_4017 fluid, yielding odd
            # parity on three heights despite complete fluid coverage and no
            # solid overlap. Use an interior, off-symmetry CAD-derived X for
            # EVERY channel; never skip a ray or ignore odd intersections.
            x = a[3] + channel_fraction * (b[0] - a[3])
            rays.append((left + '__' + right + '_channel', x, zmin + f * (zmax - zmin), ymin, ymax))
    return rays

def _scan(raw, rays):
    import numpy as np  # Load the array runtime only after reader admission.
    zones = {int(m[1]): (m[2].decode(), m[3].decode()) for m in re.finditer(b'(?m)^\\(39 \\((\\d+) (\\S+) (\\S+) 1\\)\\(', raw)}
    solid_ids = {k for k, (_, name) in zones.items()
                 if name.lower().startswith('heatsink_')
                 and name.lower().endswith('_solid')}
    fluid_ids = {k for k, (_, name) in zones.items()
                 if name.lower() in ('box1_fluid', 'air_domain_fluid')
                 or (name.lower().startswith('air_domain')
                     and name.lower().endswith('_fluid'))}
    if not solid_ids or not fluid_ids:
        raise ValueError('Fluent mesh lacks actual heatsink solid or air fluid zones')
    total = int(re.search(b'(?m)^\\(12 \\(0 1 ([0-9a-f]+) ', raw)[1], 16)
    cells = np.zeros(total + 1, dtype=np.int32)
    for m in re.finditer(b'(?m)^\\(3012 \\(([^)]+)\\)', raw):
        zid, lo, hi, t, el = [int(x, 16) for x in m[1].split()]
        cells[lo:hi + 1] = zid
    nodes = []
    for m in re.finditer(b'(?m)^\\(3010 \\(([^)]+)\\)\\s*\\(', raw):
        zid, lo, hi, t, d = [int(x, 16) for x in m[1].split()]
        nodes.append((lo, hi, np.frombuffer(raw, dtype='<f8', count=(hi - lo + 1) * 3, offset=m.end()).reshape(-1, 3)))
    metric = {int(m[1].split()[0], 16) for m in re.finditer(b'(?m)^\\(3067 \\(([^)]+)\\)', raw)}
    children = []
    for m in re.finditer(b'(?m)^\\(3061 \\(([^)]+)\\)\\s*\\(', raw):
        lo, hi = [int(x, 16) for x in m[1].split()]
        children.append((lo, hi, np.frombuffer(raw, dtype='<i4', count=(hi - lo + 1) * 2, offset=m.end()).reshape(-1, 2)))
    hits = [collections.defaultdict(list) for ray in rays]
    processed = 0

    def process(ids, c0, c1, fids):
        nonlocal processed
        if len(ids) == 0:
            return
        physical = np.ones(len(ids), dtype=bool)
        for lo, hi, links in children:
            selected = (fids >= lo) & (fids <= hi)
            if selected.any():
                physical[selected] &= ~np.any(links[fids[selected] - lo], axis=1)
        keep = physical & (np.isin(cells[c0],list(solid_ids|fluid_ids))
                           | np.isin(cells[c1],list(solid_ids|fluid_ids)))
        if not keep.any():
            return
        ids = ids[keep]
        c0 = c0[keep]
        c1 = c1[keep]
        xyz = np.full((*ids.shape, 3), np.nan)
        for lo, hi, points in nodes:
            mask = (ids >= lo) & (ids <= hi)
            xyz[mask] = points[ids[mask] - lo] * 1000
        assert np.isfinite(xyz).all()
        processed += len(ids)
        lo_xyz = xyz.min(1)
        hi_xyz = xyz.max(1)
        for rayid, (_, px, pz, _, _) in enumerate(rays):
            candidate = (lo_xyz[:, 0] <= px) & (hi_xyz[:, 0] >= px) & (lo_xyz[:, 2] <= pz) & (hi_xyz[:, 2] >= pz)
            if not candidate.any():
                continue
            pts = xyz[candidate]
            left = c0[candidate]
            right = c1[candidate]
            a = pts[:, 0]
            for j in range(1, pts.shape[1] - 1):
                v = pts[:, j] - a
                w = pts[:, j + 1] - a
                den = v[:, 0] * w[:, 2] - v[:, 2] * w[:, 0]
                valid = np.abs(den) > 1e-14
                alpha = np.zeros(len(pts))
                beta = alpha.copy()
                alpha[valid] = ((px - a[valid, 0]) * w[valid, 2] - (pz - a[valid, 2]) * w[valid, 0]) / den[valid]
                beta[valid] = (v[valid, 0] * (pz - a[valid, 2]) - v[valid, 2] * (px - a[valid, 0])) / den[valid]
                valid &= (alpha >= -1e-10) & (beta >= -1e-10) & (alpha + beta <= 1 + 1e-10)
                for row in np.flatnonzero(valid):
                    y = float(a[row, 1] + alpha[row] * v[row, 1] + beta[row] * w[row, 1])
                    for cell in {int(left[row]), int(right[row])} - {0}:
                        if cells[cell] in solid_ids or cells[cell] in fluid_ids:
                            hits[rayid][cell].append(y)
    for m in re.finditer(b'(?m)^\\(3013 \\(([^)]+)\\)\\s*\\(', raw):
        zid, first, last, t, el = [int(x, 16) for x in m[1].split()]
        if zid in metric:
            continue
        off = m.end()
        count = last - first + 1
        if el not in (0, 5):
            rows = np.frombuffer(raw, dtype='<i4', count=count * (el + 2), offset=off).reshape(-1, el + 2)
            for p in range(0, count, 65536):
                part = rows[p:p + 65536]
                process(part[:, :el], part[:, -2], part[:, -1], np.arange(first + p, first + p + len(part)))
        else:
            batches = collections.defaultdict(list)
            for fid in range(first, last + 1):
                n = struct.unpack_from('<i', raw, off)[0]
                off += 4
                values = struct.unpack_from('<' + 'i' * (n + 2), raw, off)
                off += (n + 2) * 4
                if n >= 3:
                    batches[n].append((*values, fid))
                if (fid - first + 1) % 65536 == 0 or fid == last:
                    for n, rows in batches.items():
                        a = np.asarray(rows, dtype=np.int32)
                        process(a[:, :n], a[:, n], a[:, n + 1], a[:, -1])
                    batches.clear()



    def merge(intervals):
        result = []
        for lo, hi in sorted(intervals):
            if result and lo <= result[-1][1] + 1e-08:
                result[-1][1] = max(result[-1][1], hi)
            else:
                result.append([lo, hi])
        return result
    reports = []
    for ray, values in zip(rays, hits):
        intervals = collections.defaultdict(list)
        odd = []
        for cell, ys in values.items():
            ys = sorted(set((round(y, 8) for y in ys)))
            zone_id=int(cells[cell])
            label = 'sink_solid' if zone_id in solid_ids else 'air_fluid'
            if len(ys) % 2:
                odd.append(dict(cell=cell, zone=label, hits=ys))
                continue
            intervals[label].extend(zip(ys[::2], ys[1::2]))
        merged = {label: merge(v) for label, v in intervals.items()}
        inside = {label: sum((max(0, min(hi, ray[4]) - max(lo, ray[3])) for lo, hi in rows)) for label, rows in merged.items()}
        overlap = sum((max(0, min(a1, c1, ray[4]) - max(a0, c0, ray[3])) for a0, a1 in merged.get('sink_solid', []) for c0, c1 in merged.get('air_fluid', [])))
        reports.append(dict(name=ray[0], x_mm=ray[1], z_mm=ray[2], cells_with_intersections=len(values), odd_intersection_cells=odd, merged_material_intervals_y_mm=merged, material_length_inside_fin_y_span_mm=inside, solid_fluid_overlap_length_mm=overlap))
    return (reports, processed)


def verify_native_fins(mesh_path, plan):
    """Reject missing native solid fin cells or blocked/missing air passages.

    Requires THREE distinct internal Z rays through every fin and every
    adjacent airflow gap. Numerical tolerances reproduce the validated native
    material check; evidence is read-only and measured in the Fluent cells,
    not inferred from CAD objects or solver convergence.
    """
    fin_boxes = {
        obj.name: list(obj.bbox_mm) for obj in plan.solids if obj.role == 'Fin'
    }
    if len(fin_boxes) < 2:
        raise ValueError('Native fin gate needs multiple real fins')
    rays = fin_rays(fin_boxes)
    mesh_path = Path(mesh_path)
    with mesh_path.open('rb') as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            observations, examined = _scan(raw, rays)
    if len(observations) != len(rays):
        raise ValueError('Incomplete native fin/channel ray sampling')
    failures = []
    nfin = 0
    nchannel = 0
    for expected, row in zip(rays, observations):
        name, x, z, ymin, ymax = expected
        if (row['name'] != name or abs(row['x_mm'] - x) > 1e-6
                or abs(row['z_mm'] - z) > 1e-6):
            failures.append((name, 'wrong material-ray coordinates'))
            continue
        span = ymax - ymin
        materials = row['material_length_inside_fin_y_span_mm']
        solid = float(materials.get('sink_solid', 0))
        fluid = float(materials.get('air_fluid', 0))
        overlap = float(row['solid_fluid_overlap_length_mm'])
        if name.endswith('_solid'):
            nfin += 1
            valid = abs(solid-span) <= 1e-6 and fluid <= 1e-7
        else:
            nchannel += 1
            valid = abs(fluid-span) <= 1e-6 and solid <= 1e-7
        valid = (valid and all(map(math.isfinite, (span,solid,fluid,overlap)))
                 and span > 0 and row['cells_with_intersections'] > 0
                 and abs(overlap) <= 1e-7
                 and not row['odd_intersection_cells'])
        if not valid:
            failures.append((name, 'native solid/fluid occupancy mismatch'))
    if failures:
        raise ValueError(
            'NATIVE_FIN_MATERIAL_OCCUPANCY_FAILED: '
            + repr(failures[:16])
            + (' (more omitted)' if len(failures)>16 else '')
        )
    return {
        'status': 'PASS_NATIVE_FIN_MATERIAL_RAYS',
        'fin_count': len(fin_boxes),
        'fin_rays': nfin,
        'channel_rays': nchannel,
        'native_faces_examined': examined,
        'read_only': True,
        'finite_rays_not_global_CHT_acceptance': True,
    }
