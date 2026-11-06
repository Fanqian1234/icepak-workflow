"""Physical admission for a single CHT run, never giant-file auditing.

Prevent wrong geometry at the parameterized build stage. For a completed
solve, consume the one native sampling receipt; never rescan CFD artifacts.
"""
from __future__ import annotations

import math

from geometry import CADPlan
from settings import SOURCE_TEMPERATURE_STABILITY_K


def preflight_case(spec, plan: CADPlan):
    by=plan.by_name
    if len(by)!=len(plan.solids):
        raise ValueError("Duplicate physical CAD object names")
    fins=[p for p in plan.solids if p.role=="Fin"]
    if len(fins)!=spec.fin_number:
        raise ValueError("Fin population differs from authoritative parameters")
    base=by["Heatsink_Base"].bbox_mm
    for fin in fins:
        b=fin.bbox_mm
        if (b[0]<base[0]-1e-8 or b[3]>base[3]+1e-8
                or b[1]<base[1]-1e-8 or b[4]>base[4]+1e-8
                or abs(b[2]-base[5])>1e-8):
            raise ValueError("Fin cannot touch its base by physical construction")
    gamma=plan.gamma_bbox_mm
    if min(gamma[3]-gamma[0],gamma[4]-gamma[1])<=0:
        raise ValueError("No TIM2/heatsink physical contact")
    sources=[p for p in plan.solids if p.role=="Source"]
    if len(sources)!=len(spec.sources):
        raise ValueError("Source population differs from authority")
    if not math.isclose(
            sum(s.power_W for s in spec.sources),spec.total_power_W,
            rel_tol=1e-12,abs_tol=1e-9):
        raise ValueError("Source power is not closed")
    return {"status":"PASS", "source_count":len(sources),
            "fin_count":len(fins),
            "gamma_area_mm2":(gamma[3]-gamma[0])*(gamma[4]-gamma[1]),
            "total_power_W":spec.total_power_W}


def accept_sample(spec, proof: dict):
    if proof.get("status")!="PASS" or proof.get("case_id")!=spec.case_id:
        raise ValueError("Sampling evidence not admitted for this Case")
    if proof.get("surface_contract")!="PAIRED_NATIVE_SOURCE_INTERFACE_V1":
        raise ValueError("Source samples must be verified paired native interface fields")
    if proof.get("native_fin_gate",{}).get("status")!="PASS_NATIVE_FIN_MATERIAL_RAYS":
        raise ValueError("Native fin and channel volume cells were not verified")
    if proof.get("native_solid_cells",{}).get("status")!="PASS_NATIVE_SOLID_CELLS":
        raise ValueError("Native package, Source and FEOL volume cells were not verified")
    # Measured native Source temperature convergence is mandatory.
    # Residuals are diagnostic; neither a normal Icepak exit nor an
    # iteration count establishes conservation or temperature stability.
    convergence=proof.get("native_convergence") or {}
    if (convergence.get("status")!="MEASURED" or
            int(convergence.get("iterations") or 0)<=0):
        raise ValueError("Measured native iteration evidence is missing")
    mass=proof.get("mass_balance") or {}
    if (mass.get("status")!="MEASURED" or
            not math.isfinite(float(mass.get("relative_imbalance",float("inf")))) or
            float(mass["relative_imbalance"])>0.001):
        raise ValueError("Native inlet/outlet mass imbalance exceeds 0.1% or lacks proof")
    stability=proof.get("source_temperature_stability") or {}
    if (stability.get("status")!="MEASURED" or
            int(stability.get("sampled_window_iterations") or 0)<40 or
            not math.isfinite(float(stability.get("max_temperature_range_K",float("inf")))) or
            float(stability["max_temperature_range_K"])>SOURCE_TEMPERATURE_STABILITY_K):
        raise ValueError("Native Source temperature has not stabilized for 40 iterations")
    energy=proof.get("package_energy",{})
    if (energy.get("status")!="PASS" or
            not math.isfinite(float(energy.get("relative_error",float("inf")))) or
            float(energy["relative_error"])>0.005):
        raise ValueError("Native package energy imbalance exceeds 0.5% or lacks proof")
    sink_contact=proof.get("sink_contact",{})
    if sink_contact.get("status")!="PASS":
        raise ValueError("Native fin/air direct and NCI contact area not verified")
    if (sink_contact.get("area_ratio",0)<0.85 or
            sink_contact.get("area_ratio",2)>1.05 or
            sink_contact.get("min_fin_coverage",0)<0.80):
        raise ValueError("Native sink-air exposed area below proven contract")
    gamma=proof.get("gamma",{})
    for grid in ("80","40"):
        item=gamma.get(grid,{})
        if item.get("area_relative_error",1)>0.01:
            raise ValueError("Gamma projected area is incomplete")
        if item.get("heat_relative_error",1)>5e-3:
            raise ValueError("Gamma energy raster is not conservative")
    for layer in ("bot","top","bot_48","top_48"):
        source=proof.get("source",{}).get(layer,{})
        if (source.get("family")!="interface" or
                source.get("method")!="PAIRED_NATIVE_SOURCE_INTERFACE_AREA_V1"):
            raise ValueError("Native "+layer+" must sample the paired physical interface")
        if source.get("side_temperature_delta_max_K",float("inf"))>0.5:
            raise ValueError("Native "+layer+" interface sides disagree in temperature")
        if (source.get("source_side_area_relative_error",1)>0.01 or
                source.get("upper_side_area_relative_error",1)>0.01):
            raise ValueError("Native "+layer+" interface side area is incomplete")
        if source.get("native_area_relative_error",1)>0.01:
            raise ValueError("Native "+layer+" interface native area is incomplete")
        if source.get("area_relative_error",1)>0.01:
            raise ValueError("Native "+layer+" interface projected area is incomplete")
        if source.get("mean_pixel_coverage",0)<0.99:
            raise ValueError("Native "+layer+" interface pixel coverage is too low")
        if source.get("core_min_pixel_coverage",0)<0.90:
            raise ValueError("Native "+layer+" interface core coverage is too low")
        if source.get("max_pixel_coverage",float("inf"))>1.10:
            raise ValueError("Native "+layer+" interface double coverage")
    power=proof.get("source_power_W",{})
    total=float(power.get("bot",0))+float(power.get("top",0))
    if not math.isclose(total,spec.total_power_W,abs_tol=1e-5,rel_tol=1e-5):
        raise ValueError("Source raster power does not close")
    return {"status":"PASS","case_id":spec.case_id,
            "source_total_power_W":total}

