"""Deterministic Source-contact mesh controls with server-proven recovery profiles.

The AEDT CAD constructor imprints each Source footprint on its actual Bump/HB/
Die neighbors. This module then assigns small, geometry-derived local grids to
those frozen physical solids; no global shared-core mesh region, diagnostic
mesh variants, per-case donor strategy or silent change of source topology.
A native Icepak canary must still demonstrate the final Fluent zone encoding.
"""
from __future__ import annotations

import math
from native_cad import solid_names


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
    if source or feol:
        # Preserve the validated 10-um Source/FEOL four-cell Z contract.
        xy=[min(64,max(4,math.ceil(extents[i]/.20-1e-9))) for i in (0,1)]
        z=max(4,math.ceil(extents[2]/.0025-1e-9))
    elif name.startswith("Heatsink_Fin_"):
        # Resolve fin thickness across X. The old 0.5-mm Y/Z pitch
        # unnecessarily refined every fin and surrounding air channel.
        # Keep geometry unchanged and verify physical fin contact natively.
        # Old validated fin builder used exactly four X cells for a
        # uniform-thickness fin array. Its physical fin-contact native gate
        # remains the authority for whether those cells are sufficient.
        xy=[4,max(2,math.ceil(extents[1]/FIN_LENGTH_HEIGHT_PITCH_MM-1e-9))]
        z=max(2,math.ceil(extents[2]/FIN_LENGTH_HEIGHT_PITCH_MM-1e-9))
    elif name=="Heatsink_Base":
        xy=[max(2,round(extents[i]/.5)) for i in (0,1)]
        z=max(2,round(extents[2]/.5))
    elif name=="TIM2":
        # Frozen native mesh; this is independent of its 40/80 pixel samples.
        xy=[40,40]
        z=3
    elif name in ("Bump","HB","Die"):
        # The proved Source-adjacent grid from parametric_source_mesh.
        xy=[min(80,max(4,math.ceil(extents[i]/.25-1e-9))) for i in (0,1)]
        z=min(80,max(2,math.ceil(extents[2]/.005-1e-9)))
    elif name=="TIM1":
        xy=[min(72,max(6,math.ceil(extents[i]/.4))) for i in (0,1)]
        z=max(1,math.ceil(extents[2]/.06))
    else:
        xy=[min(72,max(6,math.ceil(extents[i]/.6))) for i in (0,1)]
        z=min(20,max(2,math.ceil(extents[2]/.3)))
    return [*xy,z]


def recovery_profile_for_failed_case(previous):
    """Select one existing mesh-only recovery policy from native failure evidence."""
    if previous.get("status")!="FAILED":
        return None
    reason=str(previous.get("reason",""))
    if ("NATIVE_FIN_MATERIAL_OCCUPANCY_FAILED" in reason
            or "SubModels" in reason):
        return "fin_endcaps"
    if ("NATIVE_SOLID_CELL_MISSING" in reason
            or "Source-temperature monitor has no native" in reason
            or reason.startswith((
                "NATIVE_SOURCE_INTERFACE_SIDE_AREA:",
                "NATIVE_SOURCE_INTERFACE_SIDE_COVERAGE:",
                "NATIVE_SOURCE_INTERFACE_SIDE_MISSING:"))):
        return "source_contact"
    return None


def profile_counts_for_bbox(name,box,mesh_profile=None):
    """Server recovery counts for native mesh only; physical CAD stays fixed."""
    counts=counts_for_bbox(name,box)
    dims=[float(box[i+3])-float(box[i]) for i in range(3)]
    if mesh_profile=="fin_endcaps" and name.startswith("Heatsink_Fin_"):
        return [max(8,counts[0]),
                max(counts[1],math.ceil(dims[1]/.5-1e-8)),
                max(counts[2],math.ceil(dims[2]/.5-1e-8))]
    if mesh_profile=="source_contact" and (name.startswith("Passive_FEOL_")
            or name.lower().startswith("active_") and "source" in name.lower()):
        return [max(counts[i],min(64,math.ceil(dims[i]/.12-1e-8)))
                for i in (0,1)]+[counts[2]]
    return counts


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


def apply_physical_source_mesh(app,object_names,package_conduction=False,*,plan,mesh_profile=None):
    """Apply legacy bulk global mesh plus selective native contact/fin boxes.

    Neither stage assumes that separate native meshes are conformal. Native
    contact area must be established by the same Fluent direct/NCI evidence.
    Only previously failed physics families select a common, mesh-only
    recovery profile. CAD, Source powers, faces, and sampling remain frozen.
    """
    if mesh_profile not in (None,"fin_endcaps","source_contact") or (package_conduction and mesh_profile):
        raise ValueError("Unsupported physical mesh recovery profile")
    # T0 never changes; the two server-proven recovery profiles refine CHT
    # globally by one level while preserving every physical model definition.
    bulk_resolution=(T0_GLOBAL_MESH_RESOLUTION if package_conduction else
                     GLOBAL_MESH_RESOLUTION+int(mesh_profile is not None))
    # A deliberate blank design has no default Region. The generic PyAEDT
    # updater probes global_region.object anyway and refreshes the model tree.
    # Submit its identical native arguments without that inapplicable probe.
    available = set(solid_names(app.modeler.oeditor))
    physical_boxes=plan.by_name
    if "Region" in available:
        raise RuntimeError("Default AEDT Region overlaps the physical CHT domain")
    region=app.mesh.global_mesh_region
    region.manual_settings=False
    region.settings["MeshRegionResolution"]=bulk_resolution
    args=["NAME:Settings",*region.settings.parse_settings_as_args(),
          "UserSpecifiedSettings:=",False]
    # Get the native MeshRegion module if the PyAEDT wrapper returned None.
    module=app.omeshmodule
    if module is None and app.odesign is not None:
        module=app.odesign.GetModule("MeshRegion")
    if module is None:
        raise RuntimeError("Native Icepak MeshRegion module unavailable")
    module.EditGlobalMeshRegion(args)
    if int(region.settings["MeshRegionResolution"])!=bulk_resolution:
        raise RuntimeError("Native global mesh resolution was not applied")
    required = {"Bump","HB","Die"}
    if not required.issubset(available):
        raise ValueError("Required Bump/HB/Die material solids unavailable")
    missing = set(object_names)-available
    if missing:
        raise ValueError("Mesh input references missing CAD solids: "+str(sorted(missing)))
    sources = {n for n in available if n.lower().startswith("active_") and "source" in n.lower()}
    feol = {n for n in available if n.startswith("Passive_FEOL_")}
    if not sources or not feol:
        raise ValueError("Physical Source and FEOL complement must exist before meshing")
    # Preserve the old bulk-vs-local assignment: package and sink-base solids
    # follow the single global setting, while only TIM2, Source/FEOL and
    # fins receive explicit local boxes. Never refine the whole Die to
    # 80x80x80 just to resolve its small Source-adjacent contact patches.
    all_solids = set(object_names)|required|sources|feol
    solids = sorted(name for name in all_solids if (
        name=="TIM2" or name.startswith("Heatsink_Fin_")
        or name in sources or name in feol))
    if not package_conduction and "Air_Domain" not in available:
        raise ValueError("CHT must contain the genuine heatsink air channels")
    planned = {}
    fins=sorted((n for n in solids if n.startswith("Heatsink_Fin_")),
                key=lambda n: float(physical_boxes[n].bbox_mm[0]))
    min_gap=None
    if not package_conduction:
        if len(fins)<2:
            raise ValueError("No genuine inter-fin air channels")
        gaps=[float(physical_boxes[b].bbox_mm[0])-
              float(physical_boxes[a].bbox_mm[3])
              for a,b in zip(fins,fins[1:])]
        min_gap=min(gaps)
        if min_gap<=0:
            raise ValueError("Physical heatsink fins overlap or close the air gap")
    for index,name in enumerate(solids,1):
        box = list(physical_boxes[name].bbox_mm)
        counts = (legacy_t0_source_tile_counts(name,box)
                  if package_conduction and (name in sources or name in feol)
                  else profile_counts_for_bbox(name,box,mesh_profile))
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
            # Every fin has the same CAD-derived counts. The single native
            # operation applies the same mesh policy to the full fin array.
            leader=planned.get(fins[0])
            if leader is None or leader["counts"]!=counts:
                raise RuntimeError("Fin mesh grouping would change a per-fin grid")
            planned[name]={**leader,"physical_bbox_mm":box}
            continue
        opname=f"PhysicalSourceMesh_{index:03d}"
        # Power-bearing Source/FEOL interfaces must use a conformal package
        # mesh in BOTH T0 and CHT.  Independent local meshes here can retain
        # Source cell zones while silently dropping their actual heat power.
        # Use the server's native conformal Source/FEOL and independent
        # TIM2/fin mesh policy; a prior failed fin may select fin_endcaps.
        separate=bool(not package_conduction and
                      (name=="TIM2" or name.startswith("Heatsink_Fin_")))
        max_level=0
        module.AssignMeshOperation([
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
    return {
        "status":"PASS",
        "method":"PHYSICAL_ASPECT_AND_SELECTIVE_NCI_MESH",
        "names":sorted({row["operation"] for row in planned.values()}),
        "assignment":solids,
        "physical_local_mesh":planned,
        "global_mesh_objects":sorted(all_solids-set(solids)),
        "one_operation_per_object":len(planned)==len({r["operation"] for r in planned.values()}),
        "mesh_object_separately":True,
        "source_geometry_unchanged":True,
        "requires_pre_solve_reopen":True,
        "manual_settings":False,
        "shared_core_mesh":False,
        "mesh_subregion_objects":[],
        "source_interface_target":"IMPRINTED_MATERIAL_CONTACT",
        "mesh_profile":mesh_profile or "default",
        "reason":f"Global-{bulk_resolution} bulk mesh; local Source/FEOL, TIM2 and fin occupancy",
    }

