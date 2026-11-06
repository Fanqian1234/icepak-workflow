"""One deterministic Source/FEOL CAD plan, computed before modifying AEDT.

All coordinates are physical mm in the same global frame. Source XY rectangles
are never scaled to match a target case. The substrate/HB/Die Z boundaries
derive both source-layer thickness and the physical sample-interface plane.
"""
from __future__ import annotations

import math
from native_cad import solid_names
from collections import defaultdict


class SourceGeometryError(ValueError):
    pass


def _bbox(values, name):
    box = tuple(float(x) for x in values)
    if (len(box) != 6 or not all(map(math.isfinite, box)) or
            any(box[i + 3] <= box[i] for i in range(3))):
        raise SourceGeometryError("Invalid physical CAD bbox for " + name)
    return box


def _near(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _partitions(bbox, rectangles, layer):
    x0, y0, _, x1, y1, _ = bbox
    xs = sorted({x0, x1, *(v for r in rectangles for v in (r[0], r[2]))})
    ys = sorted({y0, y1, *(v for r in rectangles for v in (r[1], r[3]))})
    tiles = []
    for low_y, high_y in zip(ys[:-1], ys[1:]):
        run = None
        for low_x, high_x in zip(xs[:-1], xs[1:]):
            if high_x-low_x <= 1e-10 or high_y-low_y <= 1e-10:
                raise SourceGeometryError("Degenerate FEOL partition")
            cx, cy = (low_x+high_x)/2, (low_y+high_y)/2
            active = any(ax0 < cx < ax1 and ay0 < cy < ay1
                         for ax0,ay0,ax1,ay1 in rectangles)
            if active:
                if run is not None:
                    tiles.append((*run,low_y,high_y))
                    run = None
            elif run is None:
                run = [low_x,high_x]
            else:
                run[1] = high_x
        if run is not None:
            tiles.append((*run,low_y,high_y))
    area = sum((r[2]-r[0])*(r[3]-r[1]) for r in rectangles)
    passive = sum((t[1]-t[0])*(t[3]-t[2]) for t in tiles)
    core = (x1-x0)*(y1-y0)
    if not _near(area + passive, core, 1e-7*max(1, core)):
        raise SourceGeometryError("Source/FEOL partition area differs from HB")
    return [{"name": f"Passive_FEOL_{layer.capitalize()}_Tile_{i:03d}",
             "origin_mm": (a,c), "size_mm": (b-a,d-c)}
            for i,(a,b,c,d) in enumerate(tiles,1)]


def compile_source_model(specs, stack_bboxes):
    """Physical Source positions, contact planes and complement FEOL tiles.

    Runs on numerical inputs before deleting or creating *any* AEDT objects.
    Caller supplies existing Bump/HB/Die CAD bboxes in global millimetres.
    """
    stack = {name:_bbox(stack_bboxes[name], name)
             for name in ("Bump","HB","Die")}
    bump,hb,die = (stack[name] for name in ("Bump","HB","Die"))
    if (not bump[2] < bump[5] < hb[2] < hb[5] < die[2] < die[5]
            or not _near(hb[2]-bump[5], .01)
            or not _near(die[2]-hb[5], .01)):
        raise SourceGeometryError("Expected 10-um FEOL gaps between Bump/HB/Die")
    if not isinstance(specs,(list,tuple)) or not specs:
        raise SourceGeometryError("No physical source descriptors")
    zfaces = {"bot":(bump[5],hb[2]), "top":(hb[5],die[2])}
    by_layer = defaultdict(list)
    sources, names = [],set()
    for spec in specs:
        name = str(spec["name"])
        layer = str(spec["layer"]).lower()
        if layer not in zfaces or name in names or not name.startswith("active_"):
            raise SourceGeometryError("Invalid/duplicate Source identity: " + name)
        names.add(name)
        origin = tuple(float(v) for v in spec["origin_mm"])
        dimensions = tuple(float(v) for v in spec["dimensions_mm"])
        if (len(origin) != 3 or len(dimensions) != 3 or
                not all(map(math.isfinite, (*origin,*dimensions))) or
                min(dimensions) <= 0):
            raise SourceGeometryError("Invalid Source coordinates: " + name)
        lower,upper = zfaces[layer]
        if not _near(origin[2],lower) or not _near(origin[2]+dimensions[2],upper):
            raise SourceGeometryError("Source Z must equal the package-layer gap: " + name)
        x0,y0 = origin[:2]
        x1,y1 = x0+dimensions[0], y0+dimensions[1]
        if not (hb[0]-1e-7 <= x0 < x1 <= hb[3]+1e-7 and
                hb[1]-1e-7 <= y0 < y1 <= hb[4]+1e-7):
            raise SourceGeometryError("Source outside HB lateral envelope: " + name)
        if spec.get("center_xy_mm") is not None:
            cx,cy = [float(v) for v in spec["center_xy_mm"]]
            if not _near((x0+x1)/2,cx) or not _near((y0+y1)/2,cy):
                raise SourceGeometryError("Source origin/center convention mismatch: " + name)
        power = float(spec["power_W"])
        if not math.isfinite(power) or power <= 0:
            raise SourceGeometryError("Invalid Source total power: " + name)
        rect = (x0,y0,x1,y1)
        for previous in by_layer[layer]:
            a,b,c,d = previous
            if min(x1,c)-max(x0,a)>1e-9 and min(y1,d)-max(y0,b)>1e-9:
                raise SourceGeometryError("Overlapping Source rectangles in " + layer)
        by_layer[layer].append(rect)
        sources.append({**spec,"name":name,"layer":layer,
                        "origin_mm":list(origin),
                        "dimensions_mm":list(dimensions),"power_W":power})
    tiles={}
    for layer in ("bot","top"):
        rows = _partitions(hb,by_layer[layer],layer)
        lower,upper = zfaces[layer]
        tiles[layer]=[{**r,"origin_mm":[*r["origin_mm"],lower],
                       "dimensions_mm":[*r["size_mm"],upper-lower]}
                      for r in rows]
    return {"method":"PHYSICAL_SOURCE_CAD_BY_CONSTRUCTION_V1",
            "sources":sources,"passive_feol_tiles":tiles,
            "source_contact_z_mm":{"bottom":hb[2],"top":die[2]},
            "source_lower_contact_z_mm":{"bottom":bump[5],"top":hb[5]},
            "stack_bboxes_mm":{n:list(b) for n,b in stack.items()}}


# The complete package and heatsink use the same global millimetre frame as
# the Source planner above. No CAD carrier, rescaling or per-Case branches.
from dataclasses import dataclass


@dataclass(frozen=True)
class Solid:
    name: str
    role: str
    material: str
    origin_mm: tuple[float, float, float]
    dimensions_mm: tuple[float, float, float]

    @property
    def bbox_mm(self):
        return self.origin_mm + tuple(self.origin_mm[i]+self.dimensions_mm[i]
                                      for i in range(3))


@dataclass(frozen=True)
class CADPlan:
    solids: tuple[Solid, ...]
    source_plan: dict
    gamma_bbox_mm: tuple[float, float, float, float, float, float]
    inlet_velocity_m_per_s: float

    @property
    def by_name(self):
        return {part.name: part for part in self.solids}


def plan_case_geometry(spec):
    """Compile every physical box before touching the AEDT project.

    Geometry is the invariant; later mesh/field consumers cannot resize it.
    The plane Gamma is the TIM2/base intersection, never a carrier face ID.
    """
    role_material = {
        "Substrate":"Substrate", "Bump":"Bump", "HB":"HB",
        "Die":"Si", "TIM1":"TIM1", "IHS":"Cu", "TIM2":"TIM2",
    }
    stack = spec.package_bboxes_mm
    source_plan = compile_source_model(spec.source_cad_specs(), stack)
    solids = []
    for name,material in role_material.items():
        b = _bbox(stack[name], name)
        solids.append(Solid(name, name, material, b[:3],
                            tuple(b[i+3]-b[i] for i in range(3))))
    for layer in ("bot","top"):
        for p in source_plan["passive_feol_tiles"][layer]:
            solids.append(Solid(p["name"], "FEOL", "FEOL",
                                tuple(p["origin_mm"]),
                                tuple(p["dimensions_mm"])))
    for p in source_plan["sources"]:
        solids.append(Solid(p["name"], "Source", "FEOL",
                            tuple(p["origin_mm"]), tuple(p["dimensions_mm"])))
    base_z = _bbox(stack["TIM2"],"TIM2")[5]
    # Frozen production geometry: X=base_length is the fin-array direction;
    # Y=base_width is the fin extrusion direction, also the inlet flow axis.
    x0=10.0-spec.base_length_mm/2.0
    y0=10.0-spec.base_width_mm/2.0
    solids.append(Solid("Heatsink_Base","Heatsink_Base",spec.sink_material,
                        (x0,y0,base_z),
                        (spec.base_length_mm,spec.base_width_mm,spec.base_thickness_mm)))
    # The CSV pitch is decimal-rounded (up to 5e-7 mm per interval).
    # Reconstruct the same equally-spaced CAD endpoints exactly from the
    # authoritative base length and thickness, rather than leaving 435
    # otherwise valid cases with a nanometre-scale last-fin overshoot.
    pitch=(spec.base_length_mm-spec.fin_thickness_mm)/(spec.fin_number-1)
    if abs(pitch-spec.fin_pitch_mm)>0.005:
        raise SourceGeometryError("Fin pitch differs from physical design")
    for i in range(spec.fin_number):
        left=x0+i*pitch
        right=left+spec.fin_thickness_mm
        if (left < x0-1e-7 or right > x0+spec.base_length_mm+1e-6
                or i and pitch < spec.fin_thickness_mm-1e-8):
            raise SourceGeometryError("Fin pitch/thickness outside heatsink base")
        solids.append(Solid(
            f"Heatsink_Fin_{i+1:02d}","Fin",spec.sink_material,
            (left,y0,base_z+spec.base_thickness_mm),
            (spec.fin_thickness_mm,spec.base_width_mm,spec.fin_height_mm)))
    tim2 = stack["TIM2"]
    base = solids[-spec.fin_number-1].bbox_mm
    gx0=max(tim2[0],base[0]); gx1=min(tim2[3],base[3])
    gy0=max(tim2[1],base[1]); gy1=min(tim2[4],base[4])
    if gx0>=gx1 or gy0>=gy1:
        raise SourceGeometryError("TIM2/heatsink contact has zero area")
    # Non-overlap is physically exposed TIM2, NOT a fictitious contact.
    return CADPlan(tuple(solids),source_plan,
                   (gx0,gy0,base_z,gx1,gy1,base_z),
                   spec.air_velocity_m_per_s)


def create_native_cad(app, plan: CADPlan, package_only=False):
    """Build from an actually blank Icepak design with frozen physical solids.

    Both CHT and T0 must remove AEDT's automatically created, air-filled
    Region BEFORE adding anything. The old accepted builder did this; leaving
    Region would overlap the explicit Air_Domain and package.
    """
    editor=app.modeler.oeditor
    existing=solid_names(editor)
    if existing==("Region",):
        if not app.modeler.delete("Region"):
            raise RuntimeError("Cannot remove overlapping default Icepak Region")
    elif existing:
        raise RuntimeError("Fresh native CAD design is not blank: "+repr(existing[:12]))
    if solid_names(editor):
        raise RuntimeError("Default Icepak Region was not completely removed")
    parts=[part for part in plan.solids
           if not package_only or part.role not in ("Heatsink_Base","Fin")]
    created=[]
    for part in parts:
        obj=app.modeler.create_box(list(part.origin_mm),
                                   list(part.dimensions_mm),
                                   name=part.name,material=part.material)
        if not obj:
            raise RuntimeError("AEDT CAD box creation failed: "+part.name)
        actual=str(getattr(obj,"name",""))
        if actual!=part.name:
            raise RuntimeError("Unexpected AEDT solid identity: "+actual+
                               " expected "+part.name)
        created.append(actual)
    # Create explicit matching subfaces at all Source and TIM2 contacts.
    for p in plan.source_plan["sources"]:
        neighbors=("Bump","HB") if p["layer"]=="bot" else ("HB","Die")
        if not app.modeler.imprint(list(neighbors),[p["name"]],keep_originals=True):
            raise RuntimeError("Source CAD imprint failed: "+p["name"])
    # The IHS top is split into TIM2-contact and exposed regions even in T0.
    # Preserve the verified blank/tool imprint order to expose both regions.
    if not app.modeler.imprint(["IHS"],["TIM2"],keep_originals=True):
        raise RuntimeError("IHS/TIM2 native contact imprint failed")
    if not package_only:
        if not app.modeler.imprint(["Heatsink_Base"],["TIM2"],keep_originals=True):
            raise RuntimeError("TIM2/base native contact imprint failed")
        fins=[p.name for p in plan.solids if p.role=="Fin"]
        if not app.modeler.imprint(["Heatsink_Base"],fins,keep_originals=True):
            raise RuntimeError("Fin/base native contact imprint failed")
    for idx,p in enumerate(plan.source_plan["sources"],1):
        boundary=app.assign_source(
            p["name"],"Total Power",f"{p['power_W']:.12g}W",
            boundary_name=f"P_source_{idx:03d}",radiate=False)
        if not boundary:
            raise RuntimeError("Native Source power assignment failed: "+p["name"])
    return created

