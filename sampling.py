"""One-pass, area-conservative native Fluent CHT sampling.

Gamma: 80x80 temperature / outward flux / HTC and 40x40 HTC.
Source: 128x128 and 48x48 native surface-area temperature, top/bot.
Power: exact rectangle-area integration, without point sampling or resizing.
The file names form the user-approved NeuralCHT training contract.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from native_io import _overlaps, find_native_pair, read_surface_context
from settings import AMBIENT_C


def _native_solid_presence(native,plan):
    """Check actual Fluent CELLS, not just CAD or zone-name declarations.

    Fins have their stronger three-height material-ray test; package and
    active Source solids require occupied native zones. Passive FEOL may be
    merged by Fluent, so require occupied FEOL material zones as a group.
    """
    counts=np.bincount(native.cells[1:])
    occupied={
        z["name"].lower() for zid,z in native.zones.items()
        if zid<len(counts) and counts[zid]>0
    }
    # Fluent can legitimately merge adjacent same-material passive FEOL
    # CAD tiles into one native zone. Requiring one cell-zone *name* per
    # passive tile would reject correct meshes (old native monitor contract).
    required=[
        p.name.lower()+"_solid" for p in plan.solids
        if p.role not in ("Fin","FEOL")
    ]
    missing=sorted(name for name in required if name not in occupied)
    if missing:
        raise RuntimeError("NATIVE_SOLID_CELL_MISSING: "+repr(missing[:16]))
    feol_tiles=sum(p.role=="FEOL" for p in plan.solids)
    feol_native=[name for name in occupied
                 if name.startswith("passive_feol_") and name.endswith("_solid")]
    if feol_tiles and not feol_native:
        raise RuntimeError("NATIVE_PASSIVE_FEOL_VOLUME_MISSING")
    return {"status":"PASS_NATIVE_SOLID_CELLS",
            "verified_solid_count":len(required),
            "passive_feol_tiles_in_cad":feol_tiles,
            "occupied_passive_feol_zones":len(feol_native),
            "native_occupied_zone_count":len(occupied)}


def _projection(nodes,face):
    p=nodes[face[2]][:,:2]
    return abs(float(np.dot(p[:,0],np.roll(p[:,1],-1))-
                     np.dot(p[:,1],np.roll(p[:,0],-1))))/2


def _face_area(nodes,face):
    p=nodes[face[2]]
    return float(np.linalg.norm(np.cross(p[1:-1]-p[0],
                                          p[2:]-p[0]).sum(axis=0))/2)


def _sink_air_native_contact(native,plan):
    """Preserve the old native per-fin and whole-sink coupled-area gate.

    Count actual fluid/solid direct faces and 3018 NCI shadow pairs.
    CAD volumes, fin mesh operations and sampled heat alone are not proof
    that the cooling fins survived into the native heat-flow network.
    """
    fins={part.name:part.bbox_mm for part in plan.solids if part.role=="Fin"}
    if not fins:
        raise RuntimeError("Native sink audit has no physical fins")
    by_id={f[0]:f for f in native.faces}
    def zone(c):
        return native.zones[int(native.cells[c])]["name"].lower()
    def owner(f):
        return zone(f[3] or f[4])
    def sink(name):
        return name=="heatsink_base_solid" or (
            name.startswith("heatsink_fin_") and name.endswith("_solid"))
    def air(name):
        return name in ("box1_fluid","air_domain_fluid")
    def metric(f):
        if f[0] in native.geometry:
            return native.geometry[f[0]]
        p=native.nodes[f[2]]
        return p.mean(axis=0),np.cross(p[1:-1]-p[0],p[2:]-p[0]).sum(axis=0)/2
    total=0.0
    connected={name:0.0 for name in fins}
    pairs=0
    def accumulate(sink_face):
        nonlocal total,pairs
        center,vector=metric(sink_face)
        area=float(np.linalg.norm(vector))*1e6
        if not np.isfinite(area) or area<=0:
            return
        center=np.asarray(center)*1000
        total+=area
        pairs+=1
        for name,box in fins.items():
            if (center[2]>box[2]+1e-6 and
                    all(box[i]-1e-6<=center[i]<=box[i+3]+1e-6
                        for i in range(3))):
                connected[name]+=area
                break
    for f in native.faces:
        if not f[3] or not f[4] or len(f[2])<3:
            continue
        left,right=zone(f[3]),zone(f[4])
        if (sink(left) and air(right)) or (sink(right) and air(left)):
            accumulate(f)
    for a,b in native.nci_pairs:
        fa,fb=by_id.get(a),by_id.get(b)
        if fa is None or fb is None:
            raise RuntimeError("Fluent native NCI paired face missing")
        na,nb=owner(fa),owner(fb)
        if sink(na) and air(nb):
            accumulate(fa)
        elif sink(nb) and air(na):
            accumulate(fb)
    base=plan.by_name["Heatsink_Base"].dimensions_mm
    fin0=plan.by_name[next(iter(fins))].dimensions_mm
    length,width,thickness=base
    thin,_,height=fin0
    n=len(fins)
    expected=(2*thickness*(length+width)+length*width-n*thin*width+
              n*(2*width*height+2*thin*height+thin*width))
    def expected_fin(box):
        dx,dy,dz=[box[i+3]-box[i] for i in range(3)]
        return dx*dy+2*dz*(dx+dy)
    ratios={name:connected[name]/expected_fin(box)
            for name,box in fins.items()}
    ratio=total/expected
    if (not np.isfinite(ratio) or ratio<.85 or ratio>1.05 or
            any(not np.isfinite(v) or v<.80 or v>1.05
                for v in ratios.values())):
        raise RuntimeError(
            f"NATIVE_SINK_AIR_CONTACT_FAIL: total={ratio:.4f}, "
            f"min_fin={min(ratios.values()):.4f}, native_pairs={pairs}")
    return {"status":"PASS","expected_area_mm2":expected,
            "native_connected_area_mm2":total,"area_ratio":ratio,
            "per_fin_area_ratios":ratios,"min_fin_coverage":min(ratios.values()),
            "native_contact_face_count":pairs}


def _scalar_faces(native):
    face_scalar={}
    for zid,rows in native.by_zone.items():
        t=native.values.get((3,zid))
        q=native.values.get((20,zid))
        if t is not None and len(t)==len(rows):
            for f,v in zip(rows,t):
                face_scalar.setdefault(f[0],{})["T"]=float(v)
        if q is not None and len(q)==len(rows):
            for f,v in zip(rows,q):
                face_scalar.setdefault(f[0],{})["q"]=float(v)
    return face_scalar


def _gamma_faces(native,plan,scalar):
    tim2={zid for zid,z in native.zones.items()
          if z["name"].lower()=="tim2_solid"}
    if not tim2:
        raise RuntimeError("Native TIM2 material zone not found")
    ztop=plan.gamma_bbox_mm[2]*1e-3
    parents=[]
    for f in native.faces:
        zid=f[1];z=native.zones[zid]
        if z["type"] not in ("interface","wall") or len(f[2])<3:
            continue
        a,b=int(native.cells[f[3]]),int(native.cells[f[4]])
        if a not in tim2 and b not in tim2:
            continue
        p=native.nodes[f[2]]
        if np.ptp(p[:,2])>1e-10 or abs(float(p[:,2].mean())-ztop)>1e-8:
            continue
        if _projection(native.nodes,f)>1e-20:
            parents.append(f)
    if not parents:
        raise RuntimeError("Native TIM2 top interface missing")
    parent_ids={f[0] for f in parents}
    by_face_id={f[0]:f for f in native.faces}
    children=defaultdict(list)
    for child,pids in native.parents_of.items():
        for pid in pids:
            if pid in parent_ids:
                children[pid].append(child)
    polys=[]; Q=[]; T=[]; A=[]
    for f in parents:
        pid,zid=f[0],f[1]
        area=_projection(native.nodes,f)
        values=scalar.get(pid,{})
        # The established native Gamma rule ALWAYS aggregates actual NCI
        # child wall scalars for an interface; a parent interface scalar is
        # not interchangeable with the physical contact heat/temperature.
        if native.zones[zid]["type"]=="interface":
            # Old 3061 contract: ONLY the TIM2-side native WALL children
            # carry the authoritative temperature and integrated heat.
            kids=[]
            for k in children[pid]:
                child=by_face_id.get(k)
                if (child is not None
                        and native.zones[child[1]]["type"]=="wall"
                        and any(int(native.cells[c]) in tim2
                                for c in (child[3],child[4]) if c)
                        and {"T","q"}<=scalar.get(k,{}).keys()):
                    kids.append(k)
            if not kids:
                raise RuntimeError("Native Gamma TIM2-side NCI child scalars missing")
            ka=np.asarray([float(np.linalg.norm(native.geometry[k][1]))
                           if k in native.geometry
                           else _face_area(native.nodes,by_face_id[k])
                           for k in kids])
            if not np.isclose(ka.sum(),_face_area(native.nodes,f),rtol=5e-3,atol=1e-15):
                raise RuntimeError("Native Gamma NCI child area mismatch")
            t=float(np.average([scalar[k]["T"] for k in kids],weights=ka))
            q=-sum(scalar[k]["q"] for k in kids)
        else:
            if not {"T","q"}<=values.keys():
                raise RuntimeError("Native Gamma surface scalar missing")
            t=values["T"];q=-values["q"]
        polys.append(native.nodes[f[2]][:,:2].copy())
        T.append(t);Q.append(q);A.append(area)
    A=np.asarray(A);Q=np.asarray(Q);T=np.asarray(T)
    if not (np.isfinite(A).all() and np.isfinite(Q).all()
            and np.isfinite(T).all() and Q.sum()>0):
        raise RuntimeError("Invalid native Gamma face heat or temperature")
    return polys,Q,T,A


def _package_energy_closure(native,plan,scalar,gamma_heat_W,input_power_W):
    """Check native package steady-state heat in = top + bottom Robin loss.

    This is the physical check missing from native residual-only stopping.
    Side walls are adiabatic; 50 W/(m2 K) is the only bottom sink.
    Use native bottom WALL temperatures, not Source temperatures or a
    guessed cooling split.  An incomplete native wall is not a PASS.
    """
    zones={int(zid) for zid,z in native.zones.items()
           if z["name"].lower()=="substrate_solid"}
    if not zones:
        raise RuntimeError("PACKAGE_ENERGY_SUBSTRATE_ZONE_MISSING")
    substrate=plan.by_name["Substrate"]
    z_bottom=substrate.bbox_mm[2]*1e-3
    expected=substrate.dimensions_mm[0]*substrate.dimensions_mm[1]*1e-6
    area=0.0
    bottom_heat=0.0
    for f in native.faces:
        if len(f[2])<3 or native.zones[f[1]]["type"]!="wall":
            continue
        adjacent=[int(native.cells[c]) for c in (f[3],f[4]) if c]
        if not any(z in zones for z in adjacent):
            continue
        positions=native.nodes[f[2]]
        if (np.ptp(positions[:,2])>1e-9 or
                abs(float(positions[:,2].mean())-z_bottom)>1e-8):
            continue
        temperature=scalar.get(f[0],{}).get("T")
        if temperature is None or not np.isfinite(temperature):
            raise RuntimeError("PACKAGE_ENERGY_BOTTOM_NATIVE_TEMPERATURE_MISSING")
        face_area=_face_area(native.nodes,f)
        area+=face_area
        bottom_heat+=50.0*face_area*(temperature-(AMBIENT_C+273.15))
    if area<=0 or abs(area-expected)>0.02*expected:
        raise RuntimeError(
            f"PACKAGE_ENERGY_BOTTOM_AREA_INCOMPLETE: {area/expected:.4f}")
    rejected=float(gamma_heat_W)+bottom_heat
    discrepancy=abs(rejected-float(input_power_W))/float(input_power_W)
    if not np.isfinite(discrepancy) or discrepancy>0.05:
        raise RuntimeError(
            f"PACKAGE_ENERGY_NOT_CLOSED: source={input_power_W:.4g}W "
            f"gamma={gamma_heat_W:.4g}W bottom={bottom_heat:.4g}W "
            f"relative_error={discrepancy:.4f}")
    return {"status":"PASS","gamma_heat_W":float(gamma_heat_W),
            "bottom_htc50_heat_W":float(bottom_heat),
            "native_bottom_area_mm2":float(area*1e6),
            "source_total_power_W":float(input_power_W),
            "relative_error":float(discrepancy)}


def _raster_gamma(polys,Q,T,A,bbox,n):
    x0,y0,_,x1,y1,_=bbox
    if abs((x1-x0)-(y1-y0))>1e-6:
        raise ValueError("Native Robin raster requires square physical contact")
    edges=np.linspace(x0*1e-3,x1*1e-3,n+1)
    M=_overlaps(polys,edges)
    overlap=np.asarray(M.sum(axis=0)).ravel()
    if np.any(overlap<=0):
        raise ValueError("Gamma pixel has no physical native area")
    q_heat=np.asarray(M.T@(Q/A)).ravel()
    t_int=np.asarray(M.T@T).ravel()
    temp=t_int/overlap
    flux=q_heat/overlap
    denom=t_int-(AMBIENT_C+273.15)*overlap
    with np.errstate(divide="ignore",invalid="ignore"):
        h=q_heat/denom
    expected=(x1-x0)*(y1-y0)*1e-6
    area_err=abs(float(overlap.sum())-expected)/expected
    heat_err=abs(float(q_heat.sum())-float(Q.sum()))/max(abs(float(Q.sum())),1e-300)
    if (area_err>0.01 or heat_err>5e-3
            or not (np.isfinite(temp).all() and np.isfinite(flux).all()
                    and np.isfinite(h).all())):
        raise ValueError("Gamma area/energy projection not conservative")
    proof={"area_relative_error":area_err,"heat_relative_error":heat_err,
           "native_heat_W":float(Q.sum()),"pixel_heat_W":float(q_heat.sum())}
    return edges,temp-273.15,flux,h,proof


def _source_direct_native(native,plan,layer,scalar,n=128):
    # Preserve both original area-conservative Source grids (128 and 48).
    hb=plan.by_name["HB"].bbox_mm
    edges=np.linspace(hb[0]*1e-3,hb[3]*1e-3,n+1)
    if abs((hb[3]-hb[0])-(hb[4]-hb[1]))>1e-7:
        raise ValueError("Native Source domain must be square")
    z_mm=plan.source_plan["source_contact_z_mm"][
        "bottom" if layer=="bot" else "top"]
    # Match the frozen legacy source_surface() contract: consider both
    # native wall and interface source-side faces.  A physically complete
    # family is selected by real area, so NCI child walls win over a
    # duplicate parent without discarding valid conformal parent faces.
    families={"wall":[],"interface":[]}
    for f in native.faces:
        zid=f[1]
        family=native.zones[zid]["type"]
        if family not in families:
            continue
        value=scalar.get(f[0],{}).get("T")
        if value is None or len(f[2])<3:
            continue
        pts=native.nodes[f[2]]
        if np.ptp(pts[:,2])>1e-10 or abs(float(pts[:,2].mean())-z_mm*1e-3)>1e-10:
            continue
        neighbor_names=[
            native.zones[int(native.cells[c])]["name"].lower()
            for c in (f[3],f[4]) if c]
        if not any(("active_"+layer+"_source" in name or
                    "passive_feol" in name) for name in neighbor_names):
            continue
        a=_projection(native.nodes,f)
        if a>1e-20:
            families[family].append((pts[:,:2].copy(),float(value),a))
    target=(hb[3]-hb[0])*(hb[4]-hb[1])*1e-6
    choices=[]
    for family,rows in families.items():
        if rows:
            area=sum(row[2] for row in rows)
            choices.append((abs(area-target)/target,family,rows))
    if families["wall"] and families["interface"]:
        combined=families["wall"]+families["interface"]
        area=sum(row[2] for row in combined)
        choices.append((abs(area-target)/target,"wall+interface",combined))
    if not choices:
        raise RuntimeError("NATIVE_SOURCE_LEAF_MISSING: "+layer)
    native_area_error,selected_family,surface=min(choices,key=lambda x:x[0])
    if native_area_error>0.01:
        raise RuntimeError("NATIVE_SOURCE_LEAF_AREA_FAILED: "+layer+
                           " "+str(native_area_error))
    polygons=[r[0] for r in surface]
    M=_overlaps(polygons,edges)
    coverage=np.asarray(M.sum(axis=0)).ravel()
    pixel_area=(edges[1]-edges[0])**2
    ratio=coverage/pixel_area
    target=(hb[3]-hb[0])*(hb[4]-hb[1])*1e-6
    area_err=abs(float(coverage.sum())-target)/target
    core=ratio.reshape(n,n)[3:-3,3:-3]
    if (area_err>0.01 or float(ratio.mean())<0.99
            or float(core.min())<0.90 or float(ratio.max())>1.10):
        raise RuntimeError("NATIVE_SOURCE_LEAF_COVERAGE_FAILED: "+layer)
    if np.any(coverage<=0):
        raise RuntimeError("NATIVE_SOURCE_LEAF_MISSING_PIXELS: "+layer)
    temp=np.asarray(M.T@np.asarray([r[1] for r in surface])).ravel()/coverage
    if not np.isfinite(temp).all():
        raise ValueError("Nonfinite native Source leaf temperature")
    proof={"family":selected_family,
           "method":"SOURCE_SIDE_NATIVE_PROJECTED_AREA_V3",
           "face_count":len(surface),
           "native_area_relative_error":native_area_error,
           "area_relative_error":area_err,
           "mean_pixel_coverage":float(ratio.mean()),
           "min_pixel_coverage":float(ratio.min()),
           "core_min_pixel_coverage":float(core.min()),
           "max_pixel_coverage":float(ratio.max())}
    return edges,temp-273.15,z_mm,proof



def _source_parent_native(native,plan,layer,scalar,n):
    """Reconstruct the legacy Source plane from upper parent geometry and
    physical Source-side NCI child temperatures (not parent-only scalars).

    The native parent defines pixel coverage; 3061 descendants define the
    physical temperature. Small incomplete child partitions follow the
    original audited 0.5-K parent-agreement / 2%-closure contract.
    """
    hb=plan.by_name["HB"].bbox_mm
    target=(hb[3]-hb[0])*(hb[4]-hb[1])*1e-6
    edges=np.linspace(hb[0]*1e-3,hb[3]*1e-3,n+1)
    z_mm=plan.source_plan["source_contact_z_mm"][
        "bottom" if layer=="bot" else "top"]
    upper="hb_solid" if layer=="bot" else "die_solid"
    parents=[]; source_t={}; source_area={}
    for f in native.faces:
        if native.zones[f[1]]["type"] not in ("wall","interface"):
            continue
        val=scalar.get(f[0],{}).get("T")
        if len(f[2])<3:
            continue
        nodes=native.nodes[f[2]]
        if np.ptp(nodes[:,2])>1e-10 or abs(float(nodes[:,2].mean())-z_mm*1e-3)>1e-10:
            continue
        names=[native.zones[int(native.cells[c])]["name"].lower()
               for c in (f[3],f[4]) if c]
        if upper in names:
            parents.append(f)
        if val is not None and any(
                "active_"+layer+"_source" in name or "passive_feol" in name
                for name in names):
            area=(float(np.linalg.norm(native.geometry[f[0]][1]))
                  if f[0] in native.geometry else _face_area(native.nodes,f))
            if area>1e-20:
                source_t[f[0]]=float(val)
                source_area[f[0]]=area
    if not parents:
        raise RuntimeError("NATIVE_SOURCE_PARENT_MISSING: "+layer)
    reverse=defaultdict(list)
    for child,pids in native.parents_of.items():
        for parent in pids:
            reverse[parent].append(child)
    def leaves(root):
        seen=set()
        def visit(fid):
            if fid in seen:
                return []
            seen.add(fid)
            result=[leaf for child in reverse.get(fid,())
                    for leaf in visit(child)]
            return result if result else ([fid] if fid in source_t else [])
        return list(dict.fromkeys(visit(root)))
    polygons=[]; temperatures=[]; areas=[]; modes=defaultdict(int)
    for face in parents:
        fid=face[0]
        parea=_face_area(native.nodes,face)
        kids=leaves(fid)
        if not kids:
            continue
        c_area=np.asarray([source_area[k] for k in kids])
        c_temp=np.asarray([source_t[k] for k in kids])
        child_sum=float(c_area.sum())
        if child_sum<=0 or parea<=0:
            continue
        coverage=child_sum/parea
        child_temperature=float(np.dot(c_area,c_temp)/child_sum)
        parent_temperature=scalar.get(fid,{}).get("T")
        if 0.90<=coverage<=1.10:
            temperature=child_temperature
            modes["source_child"]=modes["source_child"]+1
        elif (parent_temperature is not None and
              abs(child_temperature-float(parent_temperature))<=0.5):
            temperature=float(parent_temperature)
            modes["parent_agreed"]=modes["parent_agreed"]+1
        else:
            temperature=child_temperature
            modes["child_closure"]=modes["child_closure"]+1
        polygons.append(native.nodes[face[2]][:,:2].copy())
        temperatures.append(temperature)
        areas.append(parea)
    if not polygons:
        raise RuntimeError("NATIVE_SOURCE_PARENT_CHILD_MISSING: "+layer)
    projected_area=sum(areas)
    parent_error=abs(projected_area-target)/target
    if parent_error>0.02:
        raise RuntimeError("NATIVE_SOURCE_PARENT_AREA_FAILED: "+layer+
                           " "+str(parent_error))
    raster=_overlaps(polygons,edges)
    pixel_areas=np.asarray(raster.sum(axis=0)).ravel()
    numerator=np.asarray(raster.T@np.asarray(temperatures)).ravel()
    pixel_area=target/(n*n)
    missing=np.flatnonzero(pixel_areas<=1e-20)
    if len(missing):
        covered=np.flatnonzero(pixel_areas>1e-20)
        if not len(covered):
            raise RuntimeError("NATIVE_SOURCE_PARENT_PIXELS_MISSING: "+layer)
        from scipy.spatial import cKDTree
        xy=lambda v:np.column_stack(np.unravel_index(v,(n,n)))
        nearest=cKDTree(xy(covered)).query(xy(missing))[1]
        fill=numerator[covered]/pixel_areas[covered]
        pixel_areas[missing]=pixel_area
        numerator[missing]=pixel_area*fill[nearest]
    coverage=pixel_areas/pixel_area
    mean=float(coverage.mean())
    area_error=abs(float(pixel_areas.sum())-target)/target
    if (mean<0.98 or area_error>0.01 or float(coverage.max())>1.10
            or np.any(pixel_areas<=0)):
        raise RuntimeError("NATIVE_SOURCE_PARENT_RASTER_FAILED: "+layer+
                           " "+str((mean,area_error,float(coverage.max()))))
    temp=numerator/pixel_areas
    if not np.isfinite(temp).all():
        raise RuntimeError("NATIVE_SOURCE_PARENT_TEMP_NONFINITE: "+layer)
    core=coverage.reshape(n,n)[3:-3,3:-3]
    proof={"method":"PARENT_GEOMETRY_NATIVE_SOURCE_CHILD_AREA_CLOSURE",
           "family":"parent_source_child",
           "native_area_relative_error":parent_error,
           "area_relative_error":area_error,
           "mean_pixel_coverage":mean,
           "min_pixel_coverage":float(coverage.min()),
           "core_min_pixel_coverage":float(core.min()) if core.size else float(coverage.min()),
           "max_pixel_coverage":float(coverage.max()),
           "filled_uncovered_pixels":int(len(missing)),
           "parent_faces":len(parents),
           "selected_parent_faces":len(polygons),
           "source_child_method_counts":dict(modes)}
    return edges,temp-273.15,z_mm,proof



def _source_two_side_native(native,plan,layer,scalar,n):
    """Legacy last-resort union of measured Source/upper native polygons.

    This handles Fluent mixed wall/interface partitions only when the
    two physically adjacent sides agree to 0.5 K; no parent scalar silently
    replaces an inconsistent measured Source temperature.
    """
    hb=plan.by_name["HB"].bbox_mm
    target=(hb[3]-hb[0])*(hb[4]-hb[1])*1e-6
    edges=np.linspace(hb[0]*1e-3,hb[3]*1e-3,n+1)
    z_mm=plan.source_plan["source_contact_z_mm"][
        "bottom" if layer=="bot" else "top"]
    upper="hb_solid" if layer=="bot" else "die_solid"
    rows={"source":[],"upper":[]}
    for f in native.faces:
        if native.zones[f[1]]["type"] not in ("wall","interface"):
            continue
        t=scalar.get(f[0],{}).get("T")
        if t is None or len(f[2])<3:
            continue
        p=native.nodes[f[2]]
        if np.ptp(p[:,2])>1e-10 or abs(float(p[:,2].mean())-z_mm*1e-3)>1e-10:
            continue
        names=[native.zones[int(native.cells[c])]["name"].lower()
               for c in (f[3],f[4]) if c]
        if any("active_"+layer+"_source" in name or "passive_feol" in name
               for name in names):
            rows["source"].append((p[:,:2].copy(),float(t)))
        if upper in names:
            rows["upper"].append((p[:,:2].copy(),float(t)))
    if not rows["source"] and not rows["upper"]:
        raise RuntimeError("NATIVE_SOURCE_TWO_SIDE_EMPTY: "+layer)
    side_fields={}
    for side in ("source","upper"):
        observations=rows[side]
        if not observations:
            side_fields[side]=(np.zeros(n*n),np.full(n*n,np.nan))
            continue
        M=_overlaps([x[0] for x in observations],edges)
        A=np.asarray(M.sum(axis=0)).ravel()
        numerator=np.asarray(M.T@np.asarray([x[1] for x in observations])).ravel()
        with np.errstate(divide="ignore",invalid="ignore"):
            T=numerator/A
        side_fields[side]=(A,T)
    As,Ts=side_fields["source"]; Au,Tu=side_fields["upper"]
    pixel_area=target/(n*n)
    ss=np.minimum(As,pixel_area); su=np.minimum(Au,pixel_area)
    both=(ss>0.1*pixel_area)&(su>0.1*pixel_area)&np.isfinite(Ts)&np.isfinite(Tu)
    delta=float(np.max(np.abs(Ts[both]-Tu[both]))) if np.any(both) else 0.0
    if delta>0.5:
        raise RuntimeError("NATIVE_SOURCE_TWO_SIDE_DISAGREEMENT: "+layer+
                           " "+str(delta))
    use_source=(ss>=su)&(ss>0)
    selected=np.where(use_source,ss,su)
    temp=np.where(use_source,Ts,Tu)
    if np.any(selected<=0) or not np.isfinite(temp).all():
        raise RuntimeError("NATIVE_SOURCE_TWO_SIDE_PIXELS_MISSING: "+layer)
    ratio=selected/pixel_area
    area_error=abs(float(selected.sum())-target)/target
    if float(ratio.mean())<0.99 or area_error>0.01:
        raise RuntimeError("NATIVE_SOURCE_TWO_SIDE_AREA_FAILED: "+layer)
    core=ratio.reshape(n,n)[3:-3,3:-3]
    proof={"method":"ADAPTIVE_TWO_SIDE_NATIVE_AREA_UNION_V1",
           "family":"two_side_union",
           "native_area_relative_error":area_error,
           "area_relative_error":area_error,
           "mean_pixel_coverage":float(ratio.mean()),
           "min_pixel_coverage":float(ratio.min()),
           "core_min_pixel_coverage":float(core.min()) if core.size else float(ratio.min()),
           "max_pixel_coverage":float(ratio.max()),
           "two_side_max_delta_K":delta,
           "source_face_records":len(rows["source"]),
           "upper_face_records":len(rows["upper"]),
           "pixels_using_source_side":int(use_source.sum()),
           "pixels_using_upper_side":int((~use_source).sum())}
    return edges,temp-273.15,z_mm,proof


def _source_interface(native,plan,layer,scalar,n=128):
    """Sample the *physical* Source contact from both native Fluent sides.

    Fluent 2023 R2 exports a coupled solid/solid contact as matching wall
    and wall-shadow faces, even when its CAD surface is an interface.  A wall
    label alone does not establish an interface: require full native surface
    area, independent two-sided pixel coverage, and pointwise temperature
    agreement before accepting the reconstructed interface temperature.
    Native interface and cross-material interior faces follow the same test.
    """
    hb=plan.by_name["HB"].bbox_mm
    if abs((hb[3]-hb[0])-(hb[4]-hb[1]))>1e-7:
        raise ValueError("Native Source interface domain must be square")
    target=(hb[3]-hb[0])*(hb[4]-hb[1])*1e-6
    edges=np.linspace(hb[0]*1e-3,hb[3]*1e-3,n+1)
    z_mm=plan.source_plan["source_contact_z_mm"][
        "bottom" if layer=="bot" else "top"]
    upper="hb_solid" if layer=="bot" else "die_solid"
    sides={"source":[],"upper":[]}
    encodings={"source":defaultdict(int),"upper":defaultdict(int)}
    for f in native.faces:
        zone_type=native.zones[f[1]]["type"]
        if zone_type not in ("wall","interface","interior") or len(f[2])<3:
            continue
        value=scalar.get(f[0],{}).get("T")
        if value is None or not np.isfinite(value):
            continue
        pts=native.nodes[f[2]]
        if (np.ptp(pts[:,2])>1e-10 or
                abs(float(pts[:,2].mean())-z_mm*1e-3)>1e-10):
            continue
        names=[native.zones[int(native.cells[c])]["name"].lower()
               for c in (f[3],f[4]) if c]
        area=_projection(native.nodes,f)
        if area<=1e-20:
            continue
        row=(pts[:,:2].copy(),float(value),area)
        if any(("active_"+layer+"_source" in name or
                "passive_feol" in name) for name in names):
            sides["source"].append(row)
            encodings["source"][zone_type]+=1
        if upper in names:
            sides["upper"].append(row)
            encodings["upper"][zone_type]+=1

    def project(side):
        rows=sides[side]
        if not rows:
            raise RuntimeError("NATIVE_SOURCE_INTERFACE_SIDE_MISSING: "+
                               layer+" "+side)
        native_area=sum(row[2] for row in rows)
        native_error=abs(native_area-target)/target
        if native_error>0.01:
            raise RuntimeError("NATIVE_SOURCE_INTERFACE_SIDE_AREA: "+
                               layer+" "+side+" "+str(native_error))
        overlap=_overlaps([row[0] for row in rows],edges)
        coverage=np.asarray(overlap.sum(axis=0)).ravel()
        pixel_area=target/(n*n)
        ratio=coverage/pixel_area
        area_error=abs(float(coverage.sum())-target)/target
        core=ratio.reshape(n,n)[3:-3,3:-3]
        if (area_error>0.01 or float(ratio.mean())<0.99 or
                float(core.min())<0.90 or float(ratio.max())>1.10 or
                np.any(coverage<=0)):
            raise RuntimeError(
                f"NATIVE_SOURCE_INTERFACE_SIDE_COVERAGE: {layer} {side} "
                f"native_area_error={native_error:.6g} "
                f"sampled_area_error={area_error:.6g} "
                f"mean={float(ratio.mean()):.6g} "
                f"core_min={float(core.min()):.6g} "
                f"max={float(ratio.max()):.6g} "
                f"zero_pixels={int(np.count_nonzero(coverage<=0))}")
        temperature=(np.asarray(
            overlap.T@np.asarray([row[1] for row in rows])).ravel()/coverage)
        if not np.isfinite(temperature).all():
            raise RuntimeError("NATIVE_SOURCE_INTERFACE_NONFINITE: "+
                               layer+" "+side)
        return temperature,ratio,native_error,area_error

    source,source_ratio,source_native_error,source_area_error=project("source")
    upper_temp,upper_ratio,upper_native_error,upper_area_error=project("upper")
    delta=np.abs(source-upper_temp)
    delta_max=float(delta.max())
    if delta_max>0.5:
        raise RuntimeError("NATIVE_SOURCE_INTERFACE_SIDE_DISAGREEMENT: "+
                           layer+" "+str(delta_max))
    # Each side denotes the same geometric interface.  The average removes
    # harmless two-sided roundoff without substituting a boundary-wall field.
    temperature=(source+upper_temp)/2
    proof={
        "family":"interface",
        "method":"PAIRED_NATIVE_SOURCE_INTERFACE_AREA_V1",
        "physical_contact":"Source_to_"+upper,
        "fluent_face_encoding":{key:dict(value)
                                for key,value in encodings.items()},
        "source_side_faces":len(sides["source"]),
        "upper_side_faces":len(sides["upper"]),
        "native_area_relative_error":max(source_native_error,
                                         upper_native_error),
        "area_relative_error":max(source_area_error,upper_area_error),
        "source_side_area_relative_error":source_area_error,
        "upper_side_area_relative_error":upper_area_error,
        "mean_pixel_coverage":float(min(source_ratio.mean(),
                                          upper_ratio.mean())),
        "min_pixel_coverage":float(min(source_ratio.min(),
                                         upper_ratio.min())),
        "core_min_pixel_coverage":float(min(
            source_ratio.reshape(n,n)[3:-3,3:-3].min(),
            upper_ratio.reshape(n,n)[3:-3,3:-3].min())),
        "max_pixel_coverage":float(max(source_ratio.max(),
                                         upper_ratio.max())),
        "side_temperature_delta_max_K":delta_max,
        "side_temperature_delta_p95_K":float(np.percentile(delta,95)),
        "side_temperature_delta_mean_K":float(delta.mean()),
    }
    return edges,temperature-273.15,z_mm,proof


def _power_maps(spec,bbox,n):
    # One conservative physical-rectangle integrator for both Source 128x128
    # and package-aligned 80x80 maps, with no interpolation or rescaling.
    x0,y0,_,x1,y1,_=bbox
    if abs(x0-y0)>1e-7 or abs(x1-y1)>1e-7:
        raise ValueError("Power map requires the true square physical grid")
    edges=np.linspace(x0,x1,n+1)
    m2=((edges[1]-edges[0])*1e-3)**2
    maps={"bot":np.zeros((n,n)),"top":np.zeros((n,n))}
    totals={"bot":0.0,"top":0.0}
    for src in spec.sources:
        layer="bot" if src.layer=="bottom" else "top"
        cx,cy=src.center_xy_mm;w,h=src.dimensions_mm[:2]
        dx=np.maximum(0,np.minimum(edges[1:],cx+w/2)-
                      np.maximum(edges[:-1],cx-w/2))
        dy=np.maximum(0,np.minimum(edges[1:],cy+h/2)-
                      np.maximum(edges[:-1],cy-h/2))
        maps[layer]+=src.power_W*dy[:,None]*dx[None,:]/(w*h*m2)
        totals[layer]+=src.power_W
    for layer in maps:
        if not np.isclose(float(maps[layer].sum()*m2),
                          totals[layer],rtol=1e-8,atol=1e-7):
            raise ValueError("Physical Source raster power did not close")
    return edges*1e-3,maps,totals


def _tab(path,edges,z_mm,data):
    n=len(edges)-1
    xy=(edges[:-1]+edges[1:])/2*1000
    positions=np.asarray([(x,y,z_mm) for y in xy for x in xy])
    matrix=np.column_stack((positions,np.asarray(data).reshape(n*n)))
    # Exact original .tab contract: 9 decimal places for physical XYZ,
    # 10 significant digits for sampled thermal/power values.
    np.savetxt(path,matrix,fmt=["%.9f","%.9f","%.9f","%.10g"],delimiter="\t")


def sample_native_case(spec,plan,workspace,output,*,fin_gate=None,
                       native_convergence=None):
    """Sample only native solves whose fin/air material cells were verified."""
    if not isinstance(fin_gate,dict) or fin_gate.get("status")!="PASS_NATIVE_FIN_MATERIAL_RAYS":
        raise ValueError("Unverified native fin/air mesh cannot produce accepted labels")
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    mesh,data=find_native_pair(workspace)
    native=read_surface_context(mesh,data)
    from native_io import opening_mass_balance
    mass_balance=opening_mass_balance(native,data)
    # Every package and Source solid must occupy real Fluent volume cells.
    native_solids=_native_solid_presence(native,plan)
    # Native face-area proof is mandatory, not a diagnostic-only routine.
    sink_contact=_sink_air_native_contact(native,plan)
    scalar=_scalar_faces(native)
    polys,Q,T,A=_gamma_faces(native,plan,scalar)
    energy=_package_energy_closure(
        native,plan,scalar,float(Q.sum()),spec.total_power_W)
    proof={"case_id":spec.case_id,"status":"PASS",
           "surface_contract":"PAIRED_NATIVE_SOURCE_INTERFACE_V1",
           "native_fin_gate":fin_gate,
           "native_solid_cells":native_solids,
           "sink_contact":sink_contact,
           "native_convergence":native_convergence,
           "mass_balance":mass_balance,
           "source_temperature_stability":(native_convergence or {}).get(
               "source_temperature_stability"),
           "package_energy":energy,
           "gamma":{},"source":{}}
    for n in (80,40):
        edges,temp,q,h,g=_raster_gamma(polys,Q,T,A,plan.gamma_bbox_mm,n)
        proof["gamma"][str(n)]=g
        if n==80:
            _tab(output/"T_gamma_80x80_area_C.tab",edges,plan.gamma_bbox_mm[2],temp)
            _tab(output/"q_gamma_80x80_area_W_m2.tab",edges,plan.gamma_bbox_mm[2],q)
            _tab(output/"h_gamma_80x80_area_W_m2K.tab",edges,plan.gamma_bbox_mm[2],h)
        else:
            _tab(output/"h_gamma_40x40_area_W_m2K.tab",edges,plan.gamma_bbox_mm[2],h)
    for layer in ("bot","top"):
        formal_layer="bottom" if layer=="bot" else "top"
        for n in (128,48):
            edges,temp,z,gate=_source_interface(native,plan,layer,scalar,n=n)
            _tab(output/f"T_source_{formal_layer}_{n}x{n}_area_C.tab",
                 edges,z,temp)
            proof["source"][layer if n==128 else f"{layer}_48"]=gate
    power_edges,maps,totals=_power_maps(
        spec,plan.by_name["HB"].bbox_mm,128)
    for layer in ("bot","top"):
        formal_layer="bottom" if layer=="bot" else "top"
        z=plan.source_plan["source_contact_z_mm"][formal_layer]
        _tab(output/f"power_map_{formal_layer}_128x128_W_m2.tab",
             power_edges,z,maps[layer])
    legacy_edges,legacy_maps,legacy_totals=_power_maps(
        spec,plan.by_name["HB"].bbox_mm,48)
    if legacy_totals!=totals:
        raise ValueError("Legacy and 128 Source power totals disagree")
    for layer in ("bot","top"):
        formal_layer="bottom" if layer=="bot" else "top"
        z=plan.source_plan["source_contact_z_mm"][formal_layer]
        _tab(output/f"power_map_{formal_layer}_48x48_W_m2.tab",
             legacy_edges,z,legacy_maps[layer])
    package_edges,package_maps,package_totals=_power_maps(
        spec,plan.gamma_bbox_mm,80)
    if package_totals!=totals:
        raise ValueError("Package/Source power totals disagree")
    for layer in ("bot","top"):
        formal_layer="bottom" if layer=="bot" else "top"
        # Historical contract: power-map coordinates stay on the
        # corresponding Source plane, even for package-wide XY coverage.
        source_z=plan.source_plan["source_contact_z_mm"][formal_layer]
        _tab(output/f"power_map_{formal_layer}_80x80_package_W_m2.tab",
             package_edges,source_z,package_maps[layer])
    proof["source_power_W"]=totals
    (output/"validation.json").write_text(
        json.dumps(proof,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (output/"sampling_receipt.json").write_text(
        json.dumps({"case_id":spec.case_id,"status":"PASS",
                    "lineage":"RERUN_NEW_NATIVE",
                    "native_source":"native_fluent_CHT",
                    "native_convergence":native_convergence,
                    "grid":{"gamma":80,"htc":40,"source":128,
                            "source_legacy":48},
                    "source_surface_family":"paired_native_source_interface_area_v1",
                    "source_surface_families":{
                        layer:proof["source"][layer]["family"]
                        for layer in ("bot","top")}},
                   indent=2)+"\n",encoding="utf-8")
    return proof
