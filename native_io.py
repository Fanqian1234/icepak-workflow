"""Read-only Fluent binary parsing and conservative polygon integration.

Production primitives relocated from the historical Icepak review tools.
The default retains every face. Boundary-only reads still validate all binary
records and indices, without materializing unused interior faces.
"""
from __future__ import annotations
from pathlib import Path
import re
import struct
import mmap
import numpy as np
from scipy.sparse import csr_matrix


def _mapped(path):
    """Map one giant native file without copying it into Python heap."""
    with Path(path).open('rb') as stream:
        return mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)


def parse_mesh(path, *, include_interior=True, omit_same_zone_interior=()):
    data=_mapped(path)
    totals={int(k):int(last,16) for k,last in re.findall(rb'(?m)^\((10|12|13) \(0 1 ([0-9a-f]+) ',data)}
    nodes=np.full((totals[10]+1,3),np.nan)
    cell_zone=np.zeros(totals[12]+1,dtype=np.int32)
    zones={}
    zone_matches=list(re.finditer(rb'(?m)^\(39 \((\d+) (\S+) (\S+) 1\)\(',data))
    for i,m in enumerate(zone_matches):
        block=data[m.end():zone_matches[i+1].start() if i+1<len(zone_matches) else len(data)]
        material=re.search(rb'\(material \. ([^)]+)\)',block)
        zones[int(m[1])]={'type':m[2].decode(),'name':m[3].decode(),'material':material[1].decode() if material else None}
    for m in re.finditer(rb'(?m)^\(3010 \(([^)]+)\)\s*\(',data):
        zone,first,last,kind,dim=[int(x,16) for x in m[1].split()]
        assert dim==3
        count=(last-first+1)*dim
        nodes[first:last+1]=np.frombuffer(data,dtype='<f8',count=count,offset=m.end()).reshape(-1,3)
        assert data[m.end()+count*8:m.end()+count*8+1]==b')'
    for m in re.finditer(rb'(?m)^\(3012 \(([^)]+)\)',data):
        zone,first,last,kind,element=[int(x,16) for x in m[1].split()]
        cell_zone[first:last+1]=zone
        if element==0:
            start=m.end()
            while data[start:start+1] in (b' ',b'\r',b'\n',b'\t'):start+=1
            assert data[start:start+1]==b'('
            stop=start+1+(last-first+1)*4
            assert data[stop:stop+1]==b')'
    # Optional audit view: validate every record, but do not allocate faces
    # wholly inside named bodies. Cross-body faces and all boundary/NCI zones
    # remain available; the default solver reader still retains every face.
    omitted_names=frozenset(omit_same_zone_interior)
    omitted={zid for zid,zone in zones.items() if zone['name'] in omitted_names}
    protected_zones=set()
    if omitted:
        protected_zones.update(int(m[1].split()[0],16)
            for m in re.finditer(rb'(?m)^\(3067 \(([^)]+)\)',data))
        for match in re.finditer(rb'(?m)^\(3018 \(([^)]+)\)',data):
            protected_zones.update(int(value,16) for value in match[1].split()[2:4])
    faces=[];seen=0
    for m in re.finditer(rb'(?m)^\(3013 \(([^)]+)\)\s*\(',data):
        zone,first,last,kind,element=[int(x,16) for x in m[1].split()]
        pos=m.end()
        keep=include_interior or zones[zone]['type']!='interior'
        prune=bool(omitted) and zones[zone]['type']=='interior' and zone not in protected_zones
        if element not in (0,5):
            # Batch validation of uniform faces preserves all indices and order.
            assert 0 < element <= 64, (zone,first,element)
            count=last-first+1
            rows=np.frombuffer(data,dtype='<i4',count=count*(element+2),offset=pos).reshape(count,element+2)
            vertices=rows[:,:element]
            assert ((vertices>0)&(vertices<=totals[10])).all(), ('face-node-range',zone,first,last,element)
            owners=rows[:,-2:]
            assert ((owners>=0)&(owners<=totals[12])).all(), ('face-cell-range',zone,first,last)
            if keep:
                retained=np.arange(count)
                if prune:
                    left,right=cell_zone[owners[:,0]],cell_zone[owners[:,1]]
                    retained=np.flatnonzero((owners[:,0]==0)|(owners[:,1]==0)
                        |(left!=right)|~np.isin(left,list(omitted)))
                faces.extend((first+int(i),zone,rows[i,:element].copy(),
                              int(rows[i,-2]),int(rows[i,-1])) for i in retained)
            seen+=count;pos+=count*(element+2)*4
            assert data[pos:pos+1]==b')',(zone,pos,data[pos:pos+20])
            continue
        for idx in range(first,last+1):
            n=element
            # Mixed (0) and polygonal (5) sections store a node count per face.
            if n in (0,5):n=struct.unpack_from('<i',data,pos)[0];pos+=4
            assert 0<=n<=64,(zone,idx,n)
            values=struct.unpack_from('<'+'i'*(n+2),data,pos);pos+=(n+2)*4
            vertex_ids=values[:n]
            c0,c1=values[-2:]
            assert 0<=c0<=totals[12] and 0<=c1<=totals[12]
            assert not n or (min(vertex_ids)>0 and max(vertex_ids)<=totals[10]),('face-node-range',idx,zone,n,list(vertex_ids),totals[10],pos)
            if keep and not (prune and c0 and c1
                    and cell_zone[c0]==cell_zone[c1] and int(cell_zone[c0]) in omitted):
                vertices=np.array(vertex_ids,dtype=np.int32)
                faces.append((idx,zone,vertices,c0,c1))
            seen+=1
        assert data[pos:pos+1]==b')',(zone,pos,data[pos:pos+20])
    assert seen==totals[13],(seen,totals)
    assert np.isfinite(nodes[1:]).all() and np.all(cell_zone[1:]>0),(np.flatnonzero(~np.isfinite(nodes[:,0]))[:8],np.flatnonzero(cell_zone==0)[:8],re.findall(rb'(?m)^\((?:3012|12) \([^)]+\)',data))
    return nodes,cell_zone,zones,faces,totals


def data_arrays(path):
    blob=_mapped(path);arrays={}
    for m in re.finditer(rb'(?m)^\(3300 \(([^)]+)\)\s*\(',blob):
        var,zid,dim,_,_,first,last=map(int,m[1].split())
        if var not in (3,20,111,112,113):continue
        count=(last-first+1)*dim;stop=m.end()+count*8
        assert blob[stop:stop+35].startswith(b'\nEnd of Binary Section   3300))')
        label=blob[max(0,m.start()-140):m.start()]
        if var==3:assert b'SV_T,' in label
        if var==20:assert b'SV_HEAT_FLUX,' in label
        arrays[(var,zid)]=np.frombuffer(blob,dtype='<f8',count=count,offset=m.end()).copy()
    return arrays


def _clip(poly,axis,edge,keep_greater):
    out=[]
    for p,q in zip(poly,poly[1:]+poly[:1]):
        pi=(p[axis]>=edge) if keep_greater else (p[axis]<=edge)
        qi=(q[axis]>=edge) if keep_greater else (q[axis]<=edge)
        if pi: out.append(p)
        if pi!=qi:
            den=q[axis]-p[axis]
            if abs(den)<1e-30: continue
            t=(edge-p[axis])/den
            out.append(p+t*(q-p))
    return out


def _polygon_area(poly):
    if len(poly)<3:return 0.0
    a=np.asarray(poly,float)
    return abs(np.sum(a[:,0]*np.roll(a[:,1],-1)-a[:,1]*np.roll(a[:,0],-1)))/2


def _overlaps(polygons,edges):
    N=len(edges)-1; rr=[]; cc=[]; dd=[]
    for fi,p0 in enumerate(polygons):
        p=np.asarray(p0,float)[:,:2]
        lo=np.maximum(np.searchsorted(edges,p.min(0),side="right")-1,0)
        hi=np.minimum(np.searchsorted(edges,p.max(0),side="left"),N)
        for iy in range(int(lo[1]),int(hi[1])):
            for ix in range(int(lo[0]),int(hi[0])):
                cut=list(p)
                for axis,e,g in [(0,edges[ix],True),(0,edges[ix+1],False),
                                 (1,edges[iy],True),(1,edges[iy+1],False)]:
                    cut=_clip(cut,axis,e,g)
                    if not cut: break
                a=_polygon_area(cut)
                if a>1e-20:
                    rr.append(fi);cc.append(iy*N+ix);dd.append(a)
    return csr_matrix((dd,(rr,cc)),shape=(len(polygons),N*N))

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass
class SurfaceContext:
    nodes: Any
    cells: Any
    zones: dict
    faces: list
    values: dict
    by_zone: dict
    parents_of: dict
    geometry: dict
    nci_pairs: list


def find_native_pair(workspace):
    """One shallow discovery of a just-completed AEDT solve."""
    workspace=Path(workspace)
    pairs=[]
    for folder in workspace.glob("*.aedtresults"):
        for mesh in folder.rglob("current.nc_cas"):
            data=mesh.with_name("current.fdat")
            if data.is_file():
                pairs.append((mesh,data))
    if not pairs:
        raise FileNotFoundError("Native current.nc_cas/current.fdat pair missing")
    return max(pairs,key=lambda pair:pair[1].stat().st_mtime)


def read_surface_context(mesh_path,data_path):
    """Read native mesh/data only once for all Gamma and Source samples.

    Non-surface interior faces are parsed but not materialized. One auxiliary
    metadata pass resolves parent/child NCI face areas, without extra CFD
    field rereads or diagnostic backups.
    """
    # Preserve cross-material conformal faces for the proven sink/air and
    # package contact audit, omitting voluminous same-body fluid interiors.
    nodes,cells,zones,faces,_=parse_mesh(
        Path(mesh_path),include_interior=True,
        omit_same_zone_interior=("box1_fluid","air_domain_fluid",
                                 "heatsink_base_solid"))
    fields=data_arrays(Path(data_path))
    by_zone=defaultdict(list)
    for face in faces:
        by_zone[face[1]].append(face)
    parents_of={}
    geometry={}
    with Path(mesh_path).open("rb") as stream:
        blob=mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ)
        try:
            for m in re.finditer(rb'(?m)^\(3061 \(([^)]+)\)\s*\(',blob):
                first,last=[int(x,16) for x in m[1].split()]
                rows=np.frombuffer(blob,dtype='<i4',count=(last-first+1)*2,
                                   offset=m.end()).copy().reshape(-1,2)
                for idx,row in enumerate(rows,first):
                    parents_of[idx]=tuple(int(x) for x in row if x)
            for m in re.finditer(rb'(?m)^\(3067 \(([^)]+)\)\s*\(',blob):
                zid,count=[int(x,16) for x in m[1].split()]
                rows=by_zone.get(zid,())
                if len(rows)!=count:
                    continue
                data=np.frombuffer(blob,dtype='<f8',count=count*6,
                                   offset=m.end()).copy().reshape(-1,6)
                for face,row in zip(rows,data):
                    geometry[face[0]]=(row[:3].copy(),row[3:].copy())
            # Fluent 3018 pairs are the authoritative native fluid/solid
            # coupling proof, including faces omitted from simple wall lists.
            nci_pairs=[]
            for m in re.finditer(rb'(?m)^\(3018 \(([^)]+)\)\s*\(',blob):
                first,last,_,_=[int(x,16) for x in m[1].split()]
                pair_data=np.frombuffer(blob,dtype='<i4',
                    count=(last-first+1)*2,offset=m.end()).copy().reshape(-1,2)
                nci_pairs.extend((int(a),int(b)) for a,b in pair_data
                                 if a and b)
        finally:
            blob.close()
    return SurfaceContext(nodes,cells,zones,faces,fields,by_zone,
                          parents_of,geometry,nci_pairs)


def opening_mass_balance(native, data_path):
    """Measure actual Fluent SV_FLUX on every external flow opening.

    The native field's sign convention is positive out of the fluid domain.
    This is an open-boundary mass balance, not a continuity-residual proxy.
    """
    openings={z:v for z,v in native.zones.items() if v["type"] in
              ("pressure-outlet","velocity-inlet","pressure-inlet",
               "mass-flow-inlet","outflow")}
    if not openings:
        raise ValueError("Native inlet/outlet flow zones are missing")
    counts={z:0 for z in openings}
    for face in native.faces:
        zid=face[1]
        if zid in counts:
            if bool(face[3])==bool(face[4]):
                raise ValueError("Flow opening face has invalid cell ownership")
            counts[zid]+=1
    fluxes={}
    with Path(data_path).open("rb") as stream:
        with mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as blob:
            for match in re.finditer(rb'(?m)^\(3300 \(([^)]+)\)\s*\(',blob):
                var,zid,dim,_,_,first,last=map(int,match[1].split())
                if var!=18 or zid not in openings:
                    continue
                label=blob[max(0,match.start()-180):match.start()]
                if not re.search(rb'\(0 "SV_FLUX,[^"\r\n]*"\)\s*\Z',label):
                    raise ValueError("Mass flow array has incorrect native label")
                if (dim!=1 or zid in fluxes or counts[zid]==0 or
                        last-first+1!=counts[zid]):
                    raise ValueError("Native mass flow face-count mismatch")
                values=np.frombuffer(blob,dtype="<f8",count=counts[zid],
                                     offset=match.end()).copy()
                stop=match.end()+len(values)*8
                if (not blob[stop:stop+35].startswith(
                        b"\nEnd of Binary Section   3300))")
                        or not np.isfinite(values).all()):
                    raise ValueError("Native mass flow values are invalid")
                fluxes[zid]={"name":openings[zid]["name"],
                             "signed_kg_s":float(values.sum()),
                             "inflow_kg_s":float(-values[values<0].sum()),
                             "outflow_kg_s":float(values[values>0].sum())}
    if fluxes.keys()!=openings.keys():
        raise ValueError("One or more native opening mass fluxes are absent")
    incoming=sum(row["inflow_kg_s"] for row in fluxes.values())
    outgoing=sum(row["outflow_kg_s"] for row in fluxes.values())
    if not (np.isfinite(incoming) and np.isfinite(outgoing)
            and incoming>0 and outgoing>0):
        raise ValueError("Native flow opening directions are invalid")
    return {"status":"MEASURED","inflow_kg_s":incoming,
            "outflow_kg_s":outgoing,
            "relative_imbalance":abs(incoming-outgoing)/incoming,
            "openings":fluxes,"method":"NATIVE_SV_FLUX_BOUNDARY_SUM_V1"}

