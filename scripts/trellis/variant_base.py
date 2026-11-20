"""SF10: the base mesh of a text-guided appearance variant (TrellisTextTo3DPipeline.run_variant) from a GLB.

run_variant(mesh, prompt) (trellis/pipelines/trellis_text_to_3d.py, example_variant.py) voxelizes an open3d TriangleMesh
in TRELLIS's own z-up space (pipeline.voxelize: centred, scaled by the longest bounding-box side, 64^3 grid) and samples
only the structured latent (the appearance) from the text, keeping those voxels (the shape and the pose).

The app keeps an equipment's generated GLB (source\\generated.glb). postprocess_alt.to_glb_alt and TRELLIS's to_glb
export it y-up with ``vertices @ GLB_FROM_TRELLIS``; :func:`load_glb` turns a GLB back into the z-up space with the
inverse (the transpose), so a variant of a TRELLIS result is voxelized in the frame it was generated in, and the new GLB
comes out in the same frame (same front, same conversion yaw). Other GLBs are read the same way (glTF: +Y up).

Only numpy and gen_helpers (standard library only; its tr() picks the display language) at import time (trimesh and
open3d, both in the runtime profile, are imported inside the functions), so the app's tests can import this module.
"""
from __future__ import annotations

import hashlib

import numpy as np

from gen_helpers import tr

# z-up (TRELLIS) -> y-up (GLB), as postprocess_alt.to_glb_alt and TRELLIS postprocessing_utils.to_glb
GLB_FROM_TRELLIS = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float64)
VOXEL_RESOLUTION = 64          # pipeline.voxelize: voxel_size 1/64 in [-0.5, 0.5]^3
BAD_BASE_MARKER = "ERROR: the variant base GLB cannot be used"


class VariantBaseError(ValueError):
    """The base GLB cannot be read or has no usable triangles (the message starts with BAD_BASE_MARKER)."""


def glb_to_trellis(vertices) -> np.ndarray:
    """GLB (y-up) vertices -> TRELLIS z-up vertices (the inverse of the export rotation)."""
    return np.asarray(vertices, dtype=np.float64) @ GLB_FROM_TRELLIS.T


def trellis_to_glb(vertices) -> np.ndarray:
    return np.asarray(vertices, dtype=np.float64) @ GLB_FROM_TRELLIS


def _bad(reason: str) -> VariantBaseError:
    return VariantBaseError(tr(f"{BAD_BASE_MARKER} ({reason}).\n"
                               "エラー: 見た目の作り直しの元になる GLB を使えません（読めない、または三角形がありません）。",
                               f"{BAD_BASE_MARKER} ({reason})."))


def check_mesh(vertices: np.ndarray, faces: np.ndarray) -> None:
    """Raise VariantBaseError unless there are triangles with finite vertices and a non-zero extent."""
    if vertices.ndim != 2 or vertices.shape[1] != 3 or len(vertices) == 0:
        raise _bad("no vertices")
    if faces.ndim != 2 or faces.shape[1] != 3 or len(faces) == 0:
        raise _bad("no triangles")
    if not np.all(np.isfinite(vertices)):
        raise _bad("vertices that are not finite numbers")
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise _bad("triangle indices out of range")
    if float((vertices.max(0) - vertices.min(0)).max()) <= 0.0:
        raise _bad("zero extent")


def load_glb(source) -> tuple[np.ndarray, np.ndarray]:
    """GLB (a path or a binary file object) -> (z-up vertices float64 [N, 3], triangles int64 [M, 3]). Every mesh of the
    scene is used with its node transform (trimesh force='mesh'); points and lines are ignored."""
    import trimesh
    try:
        m = trimesh.load(source if hasattr(source, "read") else str(source), file_type="glb", force="mesh",
                         process=False)
    except Exception as e:  # noqa: BLE001  any reader error is a bad base (the message says which)
        raise _bad(f"{type(e).__name__}: {e}"[:300]) from None
    if not isinstance(m, trimesh.Trimesh):
        raise _bad(f"no triangle mesh ({type(m).__name__})")
    vertices = np.asarray(m.vertices, dtype=np.float64)
    faces = np.asarray(m.faces, dtype=np.int64)
    check_mesh(vertices, faces)
    return glb_to_trellis(vertices), faces


def to_open3d(vertices: np.ndarray, faces: np.ndarray):
    """The open3d TriangleMesh that run_variant / pipeline.voxelize take."""
    import open3d as o3d
    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(np.ascontiguousarray(vertices, dtype=np.float64))
    mesh.triangles = o3d.utility.Vector3iVector(np.ascontiguousarray(faces, dtype=np.int32))
    return mesh


def describe(data: bytes, vertices: np.ndarray, faces: np.ndarray) -> dict:
    """What the report records about the base (no file name or path): SHA-256 of the file, counts, z-up extent."""
    ext = vertices.max(0) - vertices.min(0)
    return {"base_sha256": hashlib.sha256(data).hexdigest(), "base_bytes": len(data),
            "base_vertices": int(len(vertices)), "base_triangles": int(len(faces)),
            "base_extent_zup": [round(float(x), 4) for x in ext],
            # the longest side is scaled to the grid; a standing body is tallest along z
            "base_tallest_axis": "xyz"[int(np.argmax(ext))],
            "voxel_resolution": VOXEL_RESOLUTION}
