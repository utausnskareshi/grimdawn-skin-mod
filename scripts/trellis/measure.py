"""Timing / VRAM measurement and GLB inspection helpers shared by the TRELLIS scripts."""
from __future__ import annotations

import json
import struct
import threading
import time
from io import BytesIO
from pathlib import Path

import numpy as np
import torch
from PIL import Image

def _nvml_handle(pynvml):
    """NVML handle of the GPU CUDA uses as device 0. NVML ignores CUDA_VISIBLE_DEVICES, so follow it here: the app
    passes the UUID of the GPU it checked (GPU-...) on PCs with several NVIDIA GPUs; a number is taken as an NVML
    index (PCI bus order). Without it, NVML index 0."""
    import os
    first = (os.environ.get("CUDA_VISIBLE_DEVICES") or "").split(",")[0].strip()
    if first.upper().startswith("GPU-"):
        try:
            return pynvml.nvmlDeviceGetHandleByUUID(first)
        except Exception:  # pragma: no cover - older bindings want bytes
            return pynvml.nvmlDeviceGetHandleByUUID(first.encode("ascii"))
    if first.isdigit():
        return pynvml.nvmlDeviceGetHandleByIndex(int(first))
    return pynvml.nvmlDeviceGetHandleByIndex(0)


try:
    import pynvml
    pynvml.nvmlInit()
    NVML = _nvml_handle(pynvml)
except Exception:  # pragma: no cover - NVML is optional
    NVML = None


def nvml_used_mib() -> float:
    if NVML is None:
        return float("nan")
    return pynvml.nvmlDeviceGetMemoryInfo(NVML).used / 2**20


class DevicePeak:
    """Samples device-wide used VRAM (NVML) in a thread while a stage runs."""

    def __init__(self, interval: float = 0.02):
        self.interval = interval
        self.peak = 0.0
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def __enter__(self):
        self.peak = nvml_used_mib()
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, nvml_used_mib())
            time.sleep(self.interval)

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, nvml_used_mib())


class Stage:
    """Context manager that appends {stage, seconds, VRAM peaks, **extra} to `records`."""

    def __init__(self, records: list, name: str, **extra):
        self.records = records
        self.name = name
        self.extra = extra

    def __enter__(self):
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        self.dev = DevicePeak().__enter__()
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        torch.cuda.synchronize()
        dt = time.perf_counter() - self.t0
        self.dev.__exit__(*exc)
        rec = {
            "stage": self.name,
            "seconds": round(dt, 3),
            "torch_peak_allocated_mib": round(torch.cuda.max_memory_allocated() / 2**20, 1),
            "torch_peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20, 1),
            "nvml_peak_used_mib": round(self.dev.peak, 1),
            **self.extra,
        }
        self.records.append(rec)
        print(f"[stage] {self.name:<34s} {dt:8.2f}s  alloc {rec['torch_peak_allocated_mib']:8.0f} MiB  "
              f"reserved {rec['torch_peak_reserved_mib']:8.0f} MiB  device {rec['nvml_peak_used_mib']:8.0f} MiB",
              flush=True)


def process_peak(records: list) -> dict:
    return {
        "nvml_peak_used_mib": max(s["nvml_peak_used_mib"] for s in records),
        "torch_peak_allocated_mib": max(s["torch_peak_allocated_mib"] for s in records),
        "torch_peak_reserved_mib": max(s["torch_peak_reserved_mib"] for s in records),
    }


def glb_stats(path: Path) -> dict:
    """Read counts, texture size and bounds straight from the GLB container."""
    data = Path(path).read_bytes()
    magic, version, _length = struct.unpack_from("<4sII", data, 0)
    assert magic == b"glTF" and version == 2
    jlen, _jtype = struct.unpack_from("<I4s", data, 12)
    gltf = json.loads(data[20:20 + jlen])
    bin_off = 20 + jlen + 8
    acc = gltf["accessors"]
    verts = tris = 0
    attrs = set()
    index_type = None
    pos_min = np.full(3, np.inf)
    pos_max = np.full(3, -np.inf)
    for mesh in gltf["meshes"]:
        for prim in mesh["primitives"]:
            a = acc[prim["attributes"]["POSITION"]]
            verts += a["count"]
            pos_min = np.minimum(pos_min, a["min"])
            pos_max = np.maximum(pos_max, a["max"])
            tris += acc[prim["indices"]]["count"] // 3 if "indices" in prim else a["count"] // 3
            attrs.update(prim["attributes"].keys())
            index_type = acc[prim["indices"]]["componentType"] if "indices" in prim else None
    textures = []
    for img in gltf.get("images", []):
        bv = gltf["bufferViews"][img["bufferView"]]
        start = bin_off + bv.get("byteOffset", 0)
        im = Image.open(BytesIO(data[start:start + bv["byteLength"]]))
        textures.append({"mime": img.get("mimeType"), "size": list(im.size), "mode": im.mode,
                         "bytes": bv["byteLength"]})
    nodes = gltf.get("nodes", [])
    return {
        "file_bytes": len(data),
        "vertices": verts,
        "triangles": tris,
        "attributes": sorted(attrs),
        "index_component_type": index_type,  # 5125 = uint32, 5123 = uint16
        "textures": textures,
        "materials": gltf.get("materials", []),
        "node_transforms": [{k: n[k] for k in ("matrix", "rotation", "scale", "translation") if k in n} for n in nodes],
        "bounds_min": pos_min.round(4).tolist(),
        "bounds_max": pos_max.round(4).tolist(),
        "extent": (pos_max - pos_min).round(4).tolist(),
    }


def render_views(glb_path: Path, out_png: Path, res: int = 512) -> None:
    """Orthographic textured renders of the GLB from +Z, -Z, +X, -X, +Y (y-up) for orientation checks.

    Uses nvdiffrast when it is installed, otherwise the pure-PyTorch rasterizer in postprocess_alt.
    """
    import trimesh
    from PIL import ImageDraw

    scene = trimesh.load(glb_path, force="scene")
    mesh = next(iter(scene.geometry.values()))
    v = torch.tensor(np.asarray(mesh.vertices), dtype=torch.float32, device="cuda")
    f = torch.tensor(np.asarray(mesh.faces), dtype=torch.int32, device="cuda")
    uv = np.asarray(mesh.visual.uv, dtype=np.float32).copy()
    uv[:, 1] = 1.0 - uv[:, 1]  # trimesh keeps OpenGL-style v; texture rows start at the top
    uv = torch.tensor(uv, device="cuda")
    tex_img = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"), dtype=np.float32) / 255.0
    tex = torch.tensor(tex_img, device="cuda")[None]
    center = (v.max(0).values + v.min(0).values) / 2
    half = (v.max(0).values - v.min(0).values).max() / 2 * 1.05
    p = (v - center) / half
    X, Y, Z = p[:, 0], p[:, 1], p[:, 2]
    views = {
        "from +Z": (X, Y, -Z),
        "from -Z": (-X, Y, Z),
        "from +X": (-Z, Y, -X),
        "from -X": (Z, Y, X),
        "from +Y (top, -Z up)": (X, -Z, -Y),
    }
    try:
        import nvdiffrast.torch as dr
        ctx = dr.RasterizeCudaContext()
    except ImportError:
        dr = None
    tiles = []
    for label, (ix, iy, iz) in views.items():
        clip = torch.stack([ix, iy, iz * 0.5, torch.ones_like(ix)], -1)
        if dr is not None:
            rast, _ = dr.rasterize(ctx, clip[None], f, resolution=[res, res])
            texc, _ = dr.interpolate(uv[None], rast, f)
            col = dr.texture(tex, texc, filter_mode="linear")[0]
            covered = rast[0, ..., 3:] > 0
        else:
            from postprocess_alt import rasterize, interpolate, sample_texture
            face_id, bary = rasterize(clip, f.long(), res, res)
            texc = interpolate(uv, f.long(), face_id, bary)
            col = sample_texture(tex[0], texc)
            covered = (face_id >= 0)[..., None]
        col = torch.where(covered, col, torch.full_like(col, 0.85))
        img = (col.flip(0).clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
        tile = Image.fromarray(img)
        ImageDraw.Draw(tile).text((6, 6), label, fill=(0, 0, 0))
        tiles.append(tile)
    sheet = Image.new("RGB", (res * len(tiles), res), (255, 255, 255))
    for i, t in enumerate(tiles):
        sheet.paste(t, (i * res, 0))
    sheet.save(out_png)
