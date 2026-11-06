"""Area-conservative 40x40 display temperatures from accepted native samples.

No Fluent restart, raw mesh, validation rerun, or Case-specific exceptions.
The original Gamma 80x80 and Source 128x128 values remain authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import numpy as np


TEMPERATURE_FIELDS = (
    ("T_gamma_80x80_area_C.tab", "T_gamma_40x40_area_C.tab", 80),
    ("T_source_bottom_128x128_area_C.tab", "T_source_bottom_40x40_area_C.tab", 128),
    ("T_source_top_128x128_area_C.tab", "T_source_top_40x40_area_C.tab", 128),
)
OUTPUT_FILES = tuple(out for _, out, _ in TEMPERATURE_FIELDS)


def _uniform_edges(centers: np.ndarray) -> np.ndarray:
    n = len(centers)
    if n < 2 or not np.isfinite(centers).all():
        raise ValueError("Invalid sample coordinates")
    step = float((centers[-1] - centers[0]) / (n - 1))
    if step <= 0 or not np.allclose(np.diff(centers), step, rtol=0, atol=1e-7):
        raise ValueError("Sample coordinates are not uniformly spaced")
    return np.linspace(centers[0] - step / 2, centers[-1] + step / 2, n + 1)


def _weights(edges: np.ndarray, count: int = 40) -> tuple[np.ndarray, np.ndarray]:
    target = np.linspace(float(edges[0]), float(edges[-1]), count + 1)
    overlap = np.maximum(
        0.0,
        np.minimum(target[1:, None], edges[None, 1:])
        - np.maximum(target[:-1, None], edges[None, :-1]),
    )
    weights = overlap / np.diff(target)[:, None]
    if not np.allclose(weights.sum(axis=1), 1.0, rtol=0, atol=1e-8):
        raise ValueError("Fine grid does not cover 40x40 temperature grid")
    return weights, target


def conservative_temperature_40(edges: np.ndarray, temperature: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Regrid a native square temperature field without reading it again."""
    edges = np.asarray(edges, dtype=np.float64)
    n = len(edges) - 1
    field = np.asarray(temperature, dtype=np.float64).reshape(n, n)
    if not np.isfinite(field).all():
        raise ValueError("Nonfinite native temperature field")
    weights, target = _weights(edges)
    reduced = weights @ field @ weights.T
    if not np.isclose(field.mean(), reduced.mean(), rtol=0, atol=1e-7):
        raise ValueError("Native temperature area mean changed")
    return target, reduced


def convert_temperature_file(source: Path) -> np.ndarray:
    """Return (1600, 4) x/y/z/T records, preserving the .tab y-major order."""
    match = next((n for name, _, n in TEMPERATURE_FIELDS if source.name == name), None)
    if match is None:
        raise ValueError("Unknown native temperature sample name")
    n = match
    values = np.loadtxt(source, dtype=np.float64, ndmin=2)
    if values.shape != (n * n, 4) or not np.isfinite(values).all():
        raise ValueError(f"Bad {n}x{n} temperature table: {source}")
    coords = values.reshape(n, n, 4)
    xs = coords[0, :, 0]
    ys = coords[:, 0, 1]
    if (not np.allclose(coords[:, :, 0], xs[None, :], rtol=0, atol=1e-7)
            or not np.allclose(coords[:, :, 1], ys[:, None], rtol=0, atol=1e-7)
            or not np.allclose(coords[:, :, 2], values[0, 2], rtol=0, atol=1e-7)):
        raise ValueError("Temperature coordinates are not an aligned XY grid")
    wx, target_x = _weights(_uniform_edges(xs))
    wy, target_y = _weights(_uniform_edges(ys))
    field = coords[:, :, 3]
    coarse = wy @ field @ wx.T
    if not np.isclose(field.mean(), coarse.mean(), rtol=0, atol=1e-7):
        raise ValueError("Area-conservative temperature mean is not preserved")
    if coarse.min() < field.min() - 1e-7 or coarse.max() > field.max() + 1e-7:
        raise ValueError("Coarse field is outside the original temperature range")
    x, y = np.meshgrid((target_x[:-1] + target_x[1:]) / 2,
                       (target_y[:-1] + target_y[1:]) / 2)
    return np.column_stack((x.ravel(), y.ravel(),
                            np.full(1600, values[0, 2]), coarse.ravel()))


def _write_atomic(destination: Path, records: np.ndarray) -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix="." + destination.name + ".", suffix=".tmp", delete=False,
        ) as output:
            temp = Path(output.name)
            np.savetxt(output, records,
                       fmt=["%.9f", "%.9f", "%.9f", "%.10g"], delimiter="\t")
        os.replace(temp, destination)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def ensure_case_40(folder: Path, *, apply: bool = False) -> tuple[str, ...]:
    """Create only missing derived temperatures for one already-ready Case."""
    if not re.fullmatch(r"case_\d+", folder.name):
        raise ValueError("Unexpected Case directory")
    marker = json.loads((folder / "READY.json").read_text(encoding="utf-8-sig"))
    if marker.get("case_id") != folder.name or marker.get("status") != "READY":
        raise ValueError("Case has no completed READY publication")
    generated = []
    for input_name, output_name, _ in TEMPERATURE_FIELDS:
        destination = folder / output_name
        if destination.exists():
            continue
        records = convert_temperature_file(folder / input_name)
        if apply:
            _write_atomic(destination, records)
        generated.append(output_name)
    return tuple(generated)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("published", type=Path, help="Exact published/ directory")
    parser.add_argument("--apply", action="store_true", help="Write derived files")
    parser.add_argument("--ids", nargs="*", help="Optional pilot Case IDs")
    args = parser.parse_args(argv)
    published = args.published.resolve(strict=True)
    if not published.is_dir():
        raise ValueError("Not a published directory")
    folders = ([published / cid for cid in args.ids] if args.ids
               else sorted(published.glob("case_[0-9]*")))
    totals = {"new_files": 0, "cases_updated": 0, "cases_skipped": 0, "errors": 0}
    for folder in folders:
        if not folder.is_dir() or folder.resolve().parent != published:
            totals["cases_skipped"] += 1
            continue
        try:
            result = ensure_case_40(folder, apply=args.apply)
        except (OSError, ValueError, TypeError, KeyError) as error:
            totals["errors"] += 1
            print("BACKFILL_ERROR", folder.name, type(error).__name__, str(error)[:160], flush=True)
            continue
        if result:
            totals["cases_updated"] += 1
            totals["new_files"] += len(result)
            print("BACKFILL", folder.name, ",".join(result), flush=True)
        else:
            totals["cases_skipped"] += 1
    print("SUMMARY", "APPLIED" if args.apply else "DRY_RUN", totals, flush=True)


