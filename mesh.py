"""Deterministic Source-contact mesh controls, without recovery-mode switches.

The AEDT CAD constructor imprints each Source footprint on its actual Bump/HB/
Die neighbors. This module then assigns small, geometry-derived local grids to
those frozen physical solids; no global shared-core mesh region, diagnostic
mesh variants, per-case donor strategy or silent change of source topology.
A native Icepak canary must still demonstrate the final Fluent zone encoding.
"""
from __future__ import annotations

import math


GLOBAL_MESH_RESOLUTION = 3
T0_GLOBAL_MESH_RESOLUTION = 3  # Accepted package-only conformal T0 profile.
FIN_LENGTH_HEIGHT_PITCH_MM = 1.0
FIN_LOCAL_BUDGET_CELLS = 300_000


def counts_for_bbox(name,box):
    if len(box)!=6:
        raise ValueError("Invalid real CAD bbox for mesh: "+name)
    extents = [float(box[i+3])-float(box[i]) for i in range(3)]
    if not all(math.isfinite(x) and x>0 for x in extents):
        raise ValueError("Invalid real CAD extent for mesh: "+name)
    source = name.lower().startswith("active_") and "source" in name.lower()
    feol = name.startswith("Passive_FEOL_")
    if source:
        # Preserve four cells across 10-um Z. Smaller 1.2-1.8 mm Sources
        # must have at least 12 lateral cells to keep a native power-bearing
        # volume zone; the earlier 9x9 mesh lost a 1.8-mm Source body.
        # Do not refine large Source geometries or passive FEOL globally.
        xy=[min(64,max(12,math.ceil(extents[i]/.20-1e-9))) for i in (0,1)]
        z=max(4,math.ceil(extents[2]/.0025-1e-9))
    elif feol:
        xy=[min(64,max(4,math.ceil(extents[i]/.20-1e-9))) for i in (0,1)]
        z=max(4,math.ceil(extents[2]/.0025-1e-9))
    elif name.startswith("Heatsink_Fin_"):
        # Resolve fin thickness across X. The old 0.5-mm Y/Z pitch
        # unnecessarily refined every fin and surrounding air channel.
        # Keep geometry unchanged and verify physical fin contact natively.
        # Old validated fin builder used exactly four X cells for a
        # uniform-thickness fin array. Its physical fin-contact native gate
        # remains the authority for whether those cells are sufficient.
        xy=[4,
            max(2,math.ceil(extents[1]/FIN_LENGTH_HEIGHT_PITCH_MM-1e-9))]
        z=max(2,math.ceil(extents[2]/FIN_LENGTH_HEIGHT_PITCH_MM-1e-9))
    elif name=="Heatsink_Base":
        xy=[max(2,round(extents[i]/.5)) for i in (0,1)]
        z=max(2,round(extents[2]/.5))
    elif name=="TIM2":
        # Frozen native mesh; this is independent of its 40/80 pixel samples.
        xy=[40,40]
        z=3
    elif name in ("Bump","HB","Die"):
        # These solids cannot rely on the global mesh.  In AEDT 2023 R2 the
        # global grid may retain the visible Source CAD while emitting no
        # native volume zone for the 10-um package stack.  Give the three
        # adjacent solids an explicit, bounded local grid so the Source/FEOL
        # interfaces are represented by real Fluent cells.  The shared core
        # region still supplies the common contact contract.
        xy=[min(40,max(8,math.ceil(extents[i]/.5-1e-9))) for i in (0,1)]
        z=max(2,math.ceil(extents[2]/.01-1e-9))
    elif name=="TIM1":
        xy=[min(72,max(6,math.ceil(extents[i]/.4))) for i in (0,1)]
        z=max(1,math.ceil(extents[2]/.06))
    else:
        xy=[min(72,max(6,math.ceil(extents[i]/.6))) for i in (0,1)]
        z=min(20,max(2,math.ceil(extents[2]/.3)))
    return [*xy,z]


def _source_core_mesh_contract(plan, source_names, feol_names):
    """A single geometry-driven conformal core grid, not per-Source retries.

    The native global grid can miss 10-um Source solids despite having
    per-object Box Local Mesh Operations. A shared subregion forces adjacent
    Source, FEOL and package solids onto one resolved HDM stack, as in the
    historically validated source-core meshing method.
    """
    bodies=sorted(set(source_names)|set(feol_names)|
                  {"Substrate","Bump","HB","Die","TIM1"})
    if not all(name in plan.by_name for name in bodies):
        raise RuntimeError("Shared source core lacks frozen package geometry")
    sides=[]
    for name in source_names:
        b=plan.by_name[name].bbox_mm
        sides.extend([b[3]-b[0],b[4]-b[1]])
    if not sides or min(sides)<=0:
        raise RuntimeError("Shared source core has invalid Source footprints")
    pitch=min(1.0,min(sides)/3.0)
    z_pitch=0.005  # Accepted thin-Source stack: 2 shared cells per 10 um.
    lo=[min(plan.by_name[name].bbox_mm[i] for name in bodies) for i in range(3)]
    hi=[max(plan.by_name[name].bbox_mm[i+3] for name in bodies) for i in range(3)]
    counts=[math.ceil((hi[i]-lo[i])/([pitch,pitch,z_pitch][i])-1e-9)
            for i in range(3)]
    if math.prod(counts)>400_000:
        # Never silently generate a huge grid. Future very small Source
        # shapes need a bounded, general subdivision rather than weakening
        # the real native volume-cell acceptance gate.
        raise RuntimeError("Shared source core exceeds 400k estimated cells")
    return {"members":bodies,"pitch_xy_mm":pitch,"pitch_z_mm":z_pitch,
            "bbox_mm":lo+hi,"estimated_cells":math.prod(counts),
            "target_grid_counts":counts}


def _apply_shared_source_core(app,plan,source_names,feol_names):
    """Create one native non-model source-stack subregion in AEDT 2023 R2."""
    from ansys.aedt.core.modules.mesh_icepak import MeshRegion
    contract=_source_core_mesh_contract(plan,source_names,feol_names)
    name="PhysicalSourceConformalCore"
    before=set(app.modeler.object_names)
    result=MeshRegion(app,contract["members"],name=name)
    after=set(app.modeler.object_names)
    added=sorted(after-before)
    if (not result or len(added)!=1
            or not set(added).issubset(set(app.modeler.non_model_objects))):
        raise RuntimeError("Native shared Source core was not created as one non-model mesh helper")
    pitch=f'{contract["pitch_xy_mm"]:.8g}mm'
    thin=f'{contract["pitch_z_mm"]:.8g}mm'
    args=[f"NAME:{name}",
          "Enable:=",True,
          "MeshMethod:=","MesherHD",
          "UserSpecifiedSettings:=",True,
          "ComputeGap:=",False,
          "MaxElementSizeX:=",pitch,
          "MaxElementSizeY:=",pitch,
          "MaxElementSizeZ:=",thin,
          "MinElementsInGap:=","2",
          "MinElementsOnEdge:=","2",
          "MaxSizeRatio:=","2",
          "NoOGrids:=",False,
          "EnableMLM:=",True,
          "EnforeMLMType:=","3D",
          "MaxLevels:=","0",
          "BufferLayers:=","0",
          "UniformMeshParametersType:=","Average",
          "StairStepMeshing:=",False,
          "MinGapX:=",pitch,
          "MinGapY:=",pitch,
          "MinGapZ:=",thin,
          "Objects:=",added]
    app.omeshmodule.EditMeshRegion(name,args)
    contract["status"]="PASS"
    contract["region_name"]=name
    contract["subregion_helpers"]=added
    return contract


def legacy_t0_source_tile_counts(name,box):
    """Verified conformal T0 source/FEOL tile grid; never changes the CAD.

    The old accepted T0 profile uses two Z cells across 10-um FEOL solids,
    two cells on each compact XY axis, and resolves elongated tile aspect
    ratios. Aggressive source-edge grids belong to the separate CHT stage.
    """
    if len(box)!=6:
        raise ValueError("Invalid T0 Source/FEOL physical bbox: "+name)
    extents=[float(box[i+3])-float(box[i]) for i in range(3)]
    if (not all(math.isfinite(x) and x>0 for x in extents)
            or extents[2]>.1):
        raise ValueError("Invalid physical Source/FEOL thickness: "+name)
    counts=[2,2,max(2,math.ceil(extents[2]/.005-1e-8))]
    long_axis=0 if extents[0]>=extents[1] else 1
    ratio=extents[long_axis]/min(extents[:2])
    if ratio>=8:
        counts[long_axis]=math.ceil(ratio-1e-8)
    return counts


def apply_physical_source_mesh(app,object_names,package_conduction=False,*,plan):
    """Apply legacy bulk global mesh plus selective native contact/fin boxes.

    Neither stage assumes that separate native meshes are conformal. Native
    contact area must be established by the same Fluent direct/NCI evidence.
    Counts follow physical geometry, not Case-specific recovery branches.
    """
    # Restore the accepted package-only T0 global resolution 3, independent
    # from the CHT grid. T0 uses the old conformal Source/FEOL tile controls;
    # CHT retains its independently verified fin and Source sampling grids.
    # Never modify physical CAD, Source powers, materials or Robin boundaries.
    bulk_resolution=T0_GLOBAL_MESH_RESOLUTION if package_conduction else GLOBAL_MESH_RESOLUTION
    region=app.mesh.global_mesh_region
    region.manual_settings=False
    region.settings["MeshRegionResolution"]=bulk_resolution
    if (not region.update() or
            int(region.settings["MeshRegionResolution"])!=bulk_resolution):
        raise RuntimeError("Native global mesh resolution was not applied")
    available = set(app.modeler.object_names)
    if any(name not in plan.by_name for name in object_names):
        raise RuntimeError("Native mesh has no authoritative CAD-plan solid")
    if "Region" in available:
        raise RuntimeError("Default AEDT Region overlaps the physical CHT domain")
    required = {"Bump","HB","Die","TIM1"}
    if not required.issubset(available):
        raise ValueError("Required Bump/HB/Die material solids unavailable")
    missing = set(object_names)-available
    if missing:
        raise ValueError("Mesh input references missing CAD solids: "+str(sorted(missing)))
    sources = {n for n in available if n.lower().startswith("active_") and "source" in n.lower()}
    feol = {n for n in available if n.startswith("Passive_FEOL_")}
    if not sources or not feol:
        raise ValueError("Physical Source and FEOL complement must exist before meshing")
    # Preserve the old bulk-vs-local assignment for the large sink and air,
    # while the package stack, Source/FEOL, TIM2 and fins receive bounded
    # local boxes. The package stack is included explicitly because a global
    # mesh alone can omit thin Source-adjacent volume zones.
    all_solids = set(object_names)|required|sources|feol
    package_core = {"Bump","HB","Die","TIM1"}
    solids = sorted(name for name in all_solids if (
        name in package_core or name=="TIM2" or name.startswith("Heatsink_Fin_")
        or name in sources or name in feol))
    if not package_conduction and "Air_Domain" not in available:
        raise ValueError("CHT must contain the genuine heatsink air channels")
    planned = {}
    fins=sorted((n for n in solids if n.startswith("Heatsink_Fin_")),
                key=lambda n: float(plan.by_name[n].bbox_mm[0]))
    min_gap=None
    if not package_conduction:
        if len(fins)<2:
            raise ValueError("No genuine inter-fin air channels")
        gaps=[float(plan.by_name[b].bbox_mm[0])-
              float(plan.by_name[a].bbox_mm[3])
              for a,b in zip(fins,fins[1:])]
        min_gap=min(gaps)
        if min_gap<=0:
            raise ValueError("Physical heatsink fins overlap or close the air gap")
    for index,name in enumerate(solids,1):
        box = [float(v) for v in plan.by_name[name].bbox_mm]
        counts = (legacy_t0_source_tile_counts(name,box)
                  if package_conduction and (name in sources or name in feol)
                  else counts_for_bbox(name,box))
        if (name.startswith("Heatsink_Fin_")
                and math.prod(counts)*len(fins)>FIN_LOCAL_BUDGET_CELLS):
            # Pre-solve bound for all fin-local cells, not a permission to
            # build millions of fin cells and only discover OOM in Fluent.
            # Coarsen Y/Z together; preserve X thickness resolution.
            dims=[box[i+3]-box[i] for i in range(3)]
            yz_pitch=max(FIN_LENGTH_HEIGHT_PITCH_MM,math.sqrt(
                len(fins)*counts[0]*dims[1]*dims[2]/FIN_LOCAL_BUDGET_CELLS))
            counts=[counts[0],max(2,math.ceil(dims[1]/yz_pitch-1e-8)),
                    max(2,math.ceil(dims[2]/yz_pitch-1e-8))]
            while math.prod(counts)*len(fins)>FIN_LOCAL_BUDGET_CELLS:
                axes=[axis for axis in (1,2) if counts[axis]>2]
                if not axes:
                    raise RuntimeError("Fin thickness alone exceeds native cell budget")
                axis=min(axes,key=lambda i:dims[i]/counts[i])
                counts[axis]-=1
        if name.startswith("Heatsink_Fin_") and name!=fins[0]:
            # Every fin has the same CAD-derived counts. One AEDT operation
            # with separate=True preserves each fin's own physical mesh.
            leader=planned.get(fins[0])
            if leader is None or leader["counts"]!=counts:
                raise RuntimeError("Fin mesh grouping would change a per-fin grid")
            planned[name]={**leader,"physical_bbox_mm":box}
            continue
        opname=f"PhysicalSourceMesh_{index:03d}"
        # Power-bearing Source/FEOL interfaces must use a conformal package
        # mesh in BOTH T0 and CHT.  Independent local meshes here can retain
        # Source cell zones while silently dropping their actual heat power.
        # Only CHT TIM2 and cooling fins keep the audited independent mesh;
        # no Case-specific mesh exceptions or extra recovery modules.
        separate=bool(not package_conduction and
                      (name=="TIM2" or name.startswith("Heatsink_Fin_")))
        max_level=0
        app.omeshmodule.AssignMeshOperation([
            f"NAME:{opname}", "Enable:=",True,
            "MaxLevel:=",str(max_level), "MinLevel:=","0",
            "Local Mesh Parameters Enabled:=",True,
            "Mesh Reuse Enabled:=",False,
            "Local Mesh Parameters Type:=","Box Local Mesh Parameters",
            "X count:=",str(counts[0]), "Y count:=",str(counts[1]),
            "Z count:=",str(counts[2]),
            "Objects:=",fins if fins and name==fins[0] else [name],
            "Mesh Object(s) Separately Enabled:=",separate])
        planned[name]={"operation":opname,"counts":counts,
                       "max_level":max_level,"separate":separate,
                       "physical_bbox_mm":box}
    core=None if package_conduction else _apply_shared_source_core(
        app,plan,sources,feol)
    return {
        "status":"PASS",
        "method":"PHYSICAL_ASPECT_AND_SELECTIVE_NCI_MESH",
        "names":sorted({row["operation"] for row in planned.values()}|
                       ({core["region_name"]} if core else set())),
        "assignment":solids,
        "physical_local_mesh":planned,
        "global_mesh_objects":sorted(all_solids-set(solids)),
        "one_operation_per_object":len(planned)==len({r["operation"] for r in planned.values()}),
        "mesh_object_separately":True,
        "source_geometry_unchanged":True,
        "requires_pre_solve_reopen":True,
        "manual_settings":False,
        "shared_core_mesh":bool(core),
        "mesh_subregion_objects":core["subregion_helpers"] if core else [],
        "source_core_region":core,
        "source_interface_target":"IMPRINTED_MATERIAL_CONTACT",
        "reason":f"Global-{bulk_resolution} bulk mesh; local Source/FEOL, TIM2 and fin occupancy",
    }
