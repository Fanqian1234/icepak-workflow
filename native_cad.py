"""Small AEDT editor boundary for immutable box plans and native geometry reads.

No repeated CAD mutations, invented names, global monkey patches, or topology cache.
Physical mesh/convergence/field admission stays in its existing modules.
"""
from dataclasses import dataclass
import math


def solid_names(editor):
    names = editor.GetObjectsInGroup("Solids")
    if names is None:
        # AEDT may transiently omit a non-mutating gRPC read under load.
        # Repeat only the inventory read; never recreate any geometry.
        names = editor.GetObjectsInGroup("Solids")
        if names is None:
            raise RuntimeError("AEDT did not return the native solid inventory")
    result = tuple(str(name) for name in names)
    if len(result) != len(set(result)):
        raise RuntimeError("Duplicate native CAD object identity")
    return result


def create_boxes(app, parts, existing=()):
    """Create explicit native names; validate once after the mutation batch.

    Avoid Object3d(None), which manufactures NewObject_* after an empty RPC
    return, and avoid refreshing the whole model tree after every primitive.
    A missing CreateBox reply is accepted ONLY if the final native inventory
    proves that every requested solid, and no unexpected solid, exists.
    """
    modeler = app.modeler
    editor = modeler.oeditor
    parts = tuple(parts)
    expected = [part.name for part in parts]
    if len(expected) != len(set(expected)) or set(expected) & set(existing):
        raise ValueError("Box names must be unique and not already present")
    attributes = {}
    for part in parts:
        if (len(part.origin_mm) != 3 or len(part.dimensions_mm) != 3
                or not all(math.isfinite(v) for v in (*part.origin_mm, *part.dimensions_mm))
                or min(part.dimensions_mm) <= 0):
            raise ValueError("Invalid planned native box: " + part.name)
        if part.material not in attributes:
            args = modeler._default_object_attributes(name="planned_box", material=part.material)
            actual = str(args[args.index("MaterialValue:=") + 1]).strip('"')
            if actual.casefold() != part.material.casefold():
                raise RuntimeError("AEDT material substitution rejected: " + part.material)
            attributes[part.material] = args
        args = list(attributes[part.material])
        args[args.index("Name:=") + 1] = part.name
        xyz = modeler._pos_with_arg(list(part.origin_mm))
        sizes = modeler._pos_with_arg(list(part.dimensions_mm))
        shape = ["NAME:BoxParameters", "XPosition:=", xyz[0], "YPosition:=", xyz[1],
                 "ZPosition:=", xyz[2], "XSize:=", sizes[0], "YSize:=", sizes[1], "ZSize:=", sizes[2]]
        reply = editor.CreateBox(shape, args)
        if reply not in (None, "", part.name):
            raise RuntimeError("Native CreateBox returned an unexpected name: " + str(reply))
    if set(solid_names(editor)) != set(existing) | set(expected):
        raise RuntimeError("Native CAD inventory differs from the complete planned box batch")
    return expected


@dataclass(frozen=True)
class NativeFace:
    id: int
    center: tuple
    area: float | None


def native_face_area(editor, face_id):
    """Read actual native area only when a physical area integral needs it."""
    raw=editor.GetFaceArea(int(face_id))
    if raw is None:
        raise RuntimeError("AEDT returned no native face area for face "+str(face_id))
    result=float(raw)
    if not math.isfinite(result) or result<=0:
        raise RuntimeError("Invalid native face area for face "+str(face_id))
    return result


def face_snapshot(editor, name, *, areas=True):
    """Read each native planar face once, after topology-changing operations."""
    ids = editor.GetFaceIDs(name)
    if ids is None or not ids:
        raise RuntimeError("AEDT returned no native faces for " + name)
    result = []
    for fid in ids:
        fid = int(fid)
        values = editor.GetFaceCenter(fid)
        if values is None or len(values) != 3:
            raise RuntimeError("AEDT returned no planar-face center for " + name)
        center = tuple(float(v) for v in values)
        if not all(math.isfinite(v) for v in center):
            raise RuntimeError("Invalid native face center for " + name)
        area = native_face_area(editor, fid) if areas else None
        result.append(NativeFace(fid, center, area))
    if len({face.id for face in result}) != len(result):
        raise RuntimeError("Duplicate native face IDs for " + name)
    return tuple(result)


def native_bounds(editor, name):
    """Native vertex envelope, independent of CADPlan and GetObjectBoundingBox.

    Used for Boolean geometry verification, not for repeated mesh sizing.
    """
    ids = editor.GetVertexIDsFromObject(name)
    if ids is None or not ids:
        raise RuntimeError("Native vertices unavailable for " + name)
    points = []
    for vid in ids:
        position = editor.GetVertexPosition(int(vid))
        if position is None or len(position) != 3:
            # AEDT 2023 R2 can transiently return an empty vertex reply.
            # Retry the SAME native vertex once, only after proving the
            # solid and full vertex identity are still unchanged.
            if name not in solid_names(editor):
                raise RuntimeError("Native solid missing for " + name)
            refreshed = editor.GetVertexIDsFromObject(name)
            if (refreshed is None
                    or {int(v) for v in refreshed} != {int(v) for v in ids}):
                raise RuntimeError("Native vertices changed during inspection for " + name)
            position = editor.GetVertexPosition(int(vid))
        if position is None or len(position) != 3:
            raise RuntimeError("Native vertex coordinates unavailable for " + name)
        p = tuple(float(v) for v in position)
        if not all(math.isfinite(v) for v in p):
            raise RuntimeError("Nonfinite native vertex coordinates for " + name)
        points.append(p)
    bounds = tuple(min(p[k] for p in points) for k in range(3)) + tuple(max(p[k] for p in points) for k in range(3))
    if any(bounds[k+3] <= bounds[k] for k in range(3)):
        raise RuntimeError("Degenerate native volume envelope for " + name)
    return bounds


def native_volume(editor, name):
    value = editor.GetObjectVolume(name)
    if value is None:
        # AEDT 2023 R2 occasionally returns a transient empty COM reply.
        # Retry once only after independently confirming the solid exists.
        if name not in solid_names(editor):
            raise RuntimeError("Native solid missing for " + name)
        value = editor.GetObjectVolume(name)
    if value is None:
        raise RuntimeError("Native volume unavailable for " + name)
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("Invalid native solid volume for " + name)
    return value

