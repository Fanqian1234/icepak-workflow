"""Parse the authoritative CASE_FEATURES.csv once, into physical units.

The source '@(x,y)' values are CENTRES, not box origins.  There is no
carrier-based scaling, implicit 40-mm normalization, or case-ID geometry branch.
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from settings import CASE_FEATURES, EXCLUDED_SIZES_MM, EXCLUDED_TOTAL_POWERS_W, SINK_MATERIAL

NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_SOURCE = re.compile(
    rf"(?P<name>[A-Za-z][A-Za-z0-9_]*):(?P<layer>bottom|top)"
    rf"@\((?P<x>{NUM}),(?P<y>{NUM})\),"
    rf"(?P<w>{NUM})x(?P<h>{NUM})x(?P<t>{NUM})mm,"
    rf"z=(?P<z>{NUM})mm,P=(?P<p>{NUM})W", re.IGNORECASE
)
PACKAGE_PARTS = ("Substrate", "Bump", "HB", "Die", "TIM1", "IHS", "TIM2")


@dataclass(frozen=True)
class Source:
    name: str
    layer: str
    center_xy_mm: tuple[float, float]
    origin_mm: tuple[float, float, float]
    dimensions_mm: tuple[float, float, float]
    power_W: float


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    tim2_size_mm: float
    base_length_mm: float
    base_width_mm: float
    base_thickness_mm: float
    fin_height_mm: float
    fin_number: int
    fin_thickness_mm: float
    fin_pitch_mm: float
    air_velocity_m_per_s: float
    sink_material: str
    sources: tuple[Source, ...]
    package_bboxes_mm: dict[str, tuple[float, float, float, float, float, float]]

    @property
    def total_power_W(self) -> float:
        return sum(x.power_W for x in self.sources)

    @property
    def eligible(self) -> bool:
        return (self.tim2_size_mm not in EXCLUDED_SIZES_MM and
                not any(math.isclose(self.total_power_W, p, abs_tol=1e-7)
                        for p in EXCLUDED_TOTAL_POWERS_W))

    def source_cad_specs(self) -> list[dict]:
        return [
            dict(name=x.name, layer="bot" if x.layer == "bottom" else "top",
                 center_xy_mm=x.center_xy_mm, origin_mm=x.origin_mm,
                 dimensions_mm=x.dimensions_mm, power_W=x.power_W)
            for x in self.sources
        ]


def _positive(row: dict, key: str) -> float:
    try:
        result = float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Missing physical field {key}") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"Invalid physical field {key}={result!r}")
    return result


def _sources(row: dict) -> tuple[Source, ...]:
    raw = row.get("source_layout", "")
    sources = []
    for token in raw.split(";"):
        token = re.sub(r"\s+", "", token)
        if not token:
            continue
        found = _SOURCE.fullmatch(token)
        if not found:
            raise ValueError("Invalid source descriptor: " + token[:160])
        v = found.groupdict()
        cx, cy, w, h, t, z, p = (
            float(v[key]) for key in ("x", "y", "w", "h", "t", "z", "p")
        )
        if any(not math.isfinite(x) or x <= 0 for x in (w,h,t,p)):
            raise ValueError("Invalid physical Source dimensions or power")
        if any(not math.isfinite(x) for x in (cx,cy,z)):
            raise ValueError("Nonfinite Source coordinate")
        layer = v["layer"].lower()
        original_name = v["name"].lower()
        prefix = "bot" if layer == "bottom" else "top"
        if original_name.startswith("active_"):
            if not original_name.startswith(f"active_{prefix}_source"):
                raise ValueError("Source name and layer disagree: " + original_name)
            name = original_name
        else:
            # Historical descriptions sometimes use 'source1'.  Canonicalize
            # names exactly once at physical input parsing, never inside CAD.
            index = 1 + sum(x.layer == layer for x in sources)
            name = f"active_{prefix}_source{index}"
        sources.append(Source(
            name=name, layer=layer, center_xy_mm=(cx,cy),
            origin_mm=(cx-w/2,cy-h/2,z), dimensions_mm=(w,h,t), power_W=p))
    if not sources or len({x.name for x in sources}) != len(sources):
        raise ValueError("Missing/duplicate Source identities")
    if int(row["source_count"]) != len(sources):
        raise ValueError("Declared Source count differs from physical layout")
    for layer, key in (("top", "source_top_count"),
                       ("bottom", "source_bottom_count")):
        if int(row[key]) != sum(x.layer == layer for x in sources):
            raise ValueError("Declared Source-layer count differs from layout")
    declared = _positive(row, "source_power_W")
    if abs(sum(x.power_W for x in sources)-declared) > max(1e-5, declared*1e-5):
        raise ValueError("Declared source power differs from individual sources")
    return tuple(sources)


def _package_bboxes(row: dict) -> dict:
    """Construct the package from the physical stack, even for legacy blank JSON.

    The optional package_geometry contains a carrier-era description in many
    older rows; TIM2 always follows the explicit target 'size_mm' parameter.
    """
    size = _positive(row, "size_mm")
    # Package lateral dimensions scale with physical TIM2 size, while the
    # explicitly specified FEOL/HB/Die Z planes remain unchanged.  This
    # matches the authoritative Source centres and sizes for multi-size Cases.
    scale = size / 40.0
    def lateral(box):
        return (10+(box[0]-10)*scale, 10+(box[1]-10)*scale,box[2],
                10+(box[3]-10)*scale, 10+(box[4]-10)*scale,box[5])
    parts = {
        "Substrate": lateral((0,0,0,20,20,.3)),
        "Bump": lateral((0,0,.3,20,20,.4)),
        "HB": lateral((0,0,.41,20,20,.46)),
        "Die": lateral((0,0,.47,20,20,.97)),
        "TIM1": lateral((0,0,.97,20,20,1.02)),
        "IHS": lateral((-10,-10,1.02,30,30,4.02)),
    }
    raw = row.get("package_geometry", "").strip()
    if raw:
        try:
            records = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Malformed optional package geometry") from exc
        if not isinstance(records, list):
            raise ValueError("Package geometry must be a list")
        seen = set()
        for item in records:
            role = item.get("role")
            if role not in PACKAGE_PARTS or role == "TIM2":
                continue
            if role in seen:
                raise ValueError("Duplicate package solid: " + role)
            seen.add(role)
            bbox = tuple(float(v) for v in item["bbox_mm"])
            if (len(bbox)!=6 or any(not math.isfinite(x) for x in bbox) or
                    any(bbox[i+3]<=bbox[i] for i in range(3))):
                raise ValueError("Invalid physical CAD box: " + role)
            parts[role] = bbox
    ihs = parts["IHS"]
    cx,cy=(ihs[0]+ihs[3])/2,(ihs[1]+ihs[4])/2
    parts["TIM2"]=(cx-size/2,cy-size/2,ihs[5],
                   cx+size/2,cy+size/2,ihs[5]+.2)
    return parts


def from_row(row: dict) -> CaseSpec:
    cid = row.get("case_id", "")
    if not re.fullmatch(r"case_\d+", cid):
        raise ValueError("Invalid case identity: " + cid)
    n = int(_positive(row, "fin_number"))
    if n < 2 or n > 200:
        raise ValueError("Invalid fin count")
    spec = CaseSpec(
        case_id=cid,
        tim2_size_mm=_positive(row, "size_mm"),
        base_length_mm=_positive(row, "base_length_mm"),
        base_width_mm=_positive(row, "base_width_mm"),
        base_thickness_mm=_positive(row, "base_thickness_mm"),
        fin_height_mm=_positive(row, "fin_height_mm"),
        fin_number=n,
        fin_thickness_mm=_positive(row, "fin_thickness_mm"),
        fin_pitch_mm=_positive(row, "fin_pitch_mm"),
        air_velocity_m_per_s=_positive(row, "air_velocity_m_per_s"),
        sink_material=SINK_MATERIAL,
        sources=_sources(row),
        package_bboxes_mm=_package_bboxes(row),
    )
    physical_pitch=(spec.base_length_mm-spec.fin_thickness_mm)/(n-1)
    if abs(physical_pitch-spec.fin_pitch_mm) > 0.005:
        raise ValueError("Fin pitch does not fit the physical base length")
    return spec


def load_cases(path: Path = CASE_FEATURES) -> tuple[dict[str, CaseSpec], dict[str,str]]:
    """Return usable specs AND explicit invalid cases. Never silently skip data."""
    cases, errors = {}, {}
    with Path(path).open(encoding="utf-8-sig", newline="") as src:
        for row in csv.DictReader(src):
            cid = row.get("case_id", "")
            if not cid or cid in cases or cid in errors:
                raise ValueError("Duplicate/missing Case ID in authority: "+repr(cid))
            try:
                cases[cid] = from_row(row)
            except (ValueError, KeyError, TypeError) as exc:
                errors[cid] = str(exc)
    if not cases and not errors:
        raise ValueError("Empty official Case feature table")
    return cases, errors


def case_by_id(case_id: str, path: Path = CASE_FEATURES) -> CaseSpec:
    cases, errors = load_cases(path)
    if case_id in errors:
        raise ValueError(case_id + ": " + errors[case_id])
    return cases[case_id]
