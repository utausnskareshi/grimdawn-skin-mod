"""TRELLIS GLB post-processing without nvdiffrast, diff-gaussian-rasterization, pymeshfix or igraph.

The default GLB conversion since D-010 (generate_glb.py calls to_glb_alt); it replaces
trellis.utils.postprocessing_utils.to_glb, which is kept only in the dev profile for comparison
(D-007 study: docs/trellis_postprocess_alternatives.md). It follows the same steps as the original,
with these substitutions:

  step                          original                          here
  decimation                    pyvista (VTK)                     pyvista (unchanged)
  visibility for hole cleanup   nvdiffrast (via utils3d), 1000 views   rasterize() below (pure PyTorch)
  min cut of hidden faces       igraph                            scipy.sparse.csgraph.maximum_flow
  filling small holes           pymeshfix (MeshFix)               fill_small_holes() below (centroid fan)
  UV unwrap                     xatlas                            xatlas (unchanged)
  texture source                Gaussians rendered from 100 views  Gaussian colour field evaluated at each
                                (diff-gaussian-rasterization)     texel ("gaussian"), or the mesh decoder's
                                                                  vertex colours ("vertex")
  texture fitting               2500 Adam steps through nvdiffrast  direct per-texel assignment
  inpainting                    OpenCV Telea                      OpenCV Telea (unchanged)

Attribution: the procedure, and a few lines (pyvista/xatlas/trimesh calls and edge bookkeeping), follow
trellis/utils/postprocessing_utils.py of microsoft/TRELLIS (MIT License, Copyright (c) Microsoft Corporation;
full text: licenses/setup/TRELLIS-LICENSE.txt).

All dependencies are MIT/BSD/Apache-2.0 and ship as prebuilt wheels: torch, numpy, scipy, pyvista/vtk,
xatlas, trimesh, opencv-python-headless, open3d (vertex mode only) and utils3d (only its pure-PyTorch
mesh/camera helpers; its rasterizer, which needs nvdiffrast, is not used).

Usage (no TRELLIS import needed; reads a dump written by generate_glb.py --dump):
    python -I postprocess_alt.py --dump sample.pt --out <dir> --variants 0.95:1024 0.9:1024
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch

INT64_MAX = torch.iinfo(torch.int64).max
# Texels where no Gaussian contributes along the normal: "inpaint" fills them from neighbouring texels,
# "nearest" takes the colour of the nearest Gaussian centre.
FALLBACK = "inpaint"

# --------------------------------------------------------------------------------------------
# Rasterizer
# --------------------------------------------------------------------------------------------


def _edge(ax, ay, bx, by, px, py):
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


@torch.no_grad()
def rasterize(clip: torch.Tensor, faces: torch.Tensor, height: int, width: int,
              max_candidates: int = 1 << 23) -> tuple[torch.Tensor, torch.Tensor]:
    """Z-buffered triangle rasterization in pure PyTorch.

    clip: (V, 4) clip-space positions (OpenGL convention), faces: (F, 3) int64.
    Returns face_id (H, W) int64 (-1 where empty) and perspective-correct barycentrics (H, W, 3).
    Row 0 is clip y = -1 (bottom), the same layout nvdiffrast uses. No back-face culling.
    """
    device = clip.device
    w = clip[:, 3]
    ndc = clip[:, :3] / w[:, None]
    px = (ndc[:, 0] * 0.5 + 0.5) * width
    py = (ndc[:, 1] * 0.5 + 0.5) * height
    pz = ndc[:, 2]
    tx, ty, tz, tw = px[faces], py[faces], pz[faces], w[faces]
    area = _edge(tx[:, 0], ty[:, 0], tx[:, 1], ty[:, 1], tx[:, 2], ty[:, 2])
    i0 = torch.ceil(tx.min(1).values - 0.5).clamp(min=0).long()
    i1 = torch.floor(tx.max(1).values - 0.5).clamp(max=width - 1).long()
    j0 = torch.ceil(ty.min(1).values - 0.5).clamp(min=0).long()
    j1 = torch.floor(ty.max(1).values - 0.5).clamp(max=height - 1).long()
    cx = (i1 - i0 + 1).clamp(min=0)
    cy = (j1 - j0 + 1).clamp(min=0)
    ok = (tw > 1e-8).all(1) & (area.abs() > 1e-12)
    count = torch.where(ok, cx * cy, torch.zeros_like(cx))

    zbuf = torch.full((height * width,), INT64_MAX, dtype=torch.int64, device=device)
    live = torch.nonzero(count > 0).reshape(-1)
    if live.numel():
        csum = torch.cumsum(count[live], 0)
        bounds = torch.searchsorted(csum, torch.arange(1, int(csum[-1]) // max_candidates + 2, device=device) * max_candidates)
        start = 0
        for end in bounds.tolist() + [live.numel()]:
            end = max(end, start + 1)
            if start >= live.numel():
                break
            f = live[start:end]
            start = end
            c = count[f]
            rep = torch.repeat_interleave(torch.arange(f.numel(), device=device), c)
            first = torch.cumsum(c, 0) - c
            off = torch.arange(rep.numel(), device=device) - first[rep]
            fr = f[rep]
            ix = i0[fr] + off % cx[fr]
            iy = j0[fr] + torch.div(off, cx[fr], rounding_mode="floor")
            sx, sy = ix.float() + 0.5, iy.float() + 0.5
            x, y, z = tx[fr], ty[fr], tz[fr]
            a = area[fr]
            b0 = _edge(x[:, 1], y[:, 1], x[:, 2], y[:, 2], sx, sy) / a
            b1 = _edge(x[:, 2], y[:, 2], x[:, 0], y[:, 0], sx, sy) / a
            b2 = 1 - b0 - b1
            depth = b0 * z[:, 0] + b1 * z[:, 1] + b2 * z[:, 2]
            inside = (b0 >= 0) & (b1 >= 0) & (b2 >= 0) & (depth >= -1) & (depth <= 1)
            key = (((depth * 0.5 + 0.5) * (2**31 - 1)).long() << 32) | fr
            zbuf.scatter_reduce_(0, (iy * width + ix)[inside], key[inside], reduce="amin")

    hit = zbuf != INT64_MAX
    face_id = torch.where(hit, zbuf & 0xFFFFFFFF, torch.full_like(zbuf, -1))
    bary = torch.zeros((height * width, 3), device=device)
    pix = torch.nonzero(hit).reshape(-1)
    if pix.numel():
        fid = face_id[pix]
        sx = (pix % width).float() + 0.5
        sy = torch.div(pix, width, rounding_mode="floor").float() + 0.5
        x, y = tx[fid], ty[fid]
        a = area[fid]
        b0 = _edge(x[:, 1], y[:, 1], x[:, 2], y[:, 2], sx, sy) / a
        b1 = _edge(x[:, 2], y[:, 2], x[:, 0], y[:, 0], sx, sy) / a
        b = torch.stack([b0, b1, 1 - b0 - b1], -1) / tw[fid]  # perspective correction
        bary[pix] = b / b.sum(-1, keepdim=True)
    return face_id.view(height, width), bary.view(height, width, 3)


def interpolate(attr: torch.Tensor, faces: torch.Tensor, face_id: torch.Tensor, bary: torch.Tensor) -> torch.Tensor:
    """Barycentric interpolation of per-vertex attributes over a rasterized image (zeros where empty)."""
    out = torch.zeros((*face_id.shape, attr.shape[1]), device=attr.device, dtype=attr.dtype)
    m = face_id >= 0
    tri = attr[faces[face_id[m]]]  # (N, 3, C)
    out[m] = (tri * bary[m][..., None]).sum(1)
    return out


def sample_texture(tex: torch.Tensor, uv: torch.Tensor) -> torch.Tensor:
    """Bilinear lookup; tex (T, T, C) with row 0 at v = 0, uv (H, W, 2) in [0, 1]."""
    grid = uv[None] * 2 - 1
    return torch.nn.functional.grid_sample(tex.permute(2, 0, 1)[None], grid, mode="bilinear",
                                           padding_mode="border", align_corners=False)[0].permute(1, 2, 0)


# --------------------------------------------------------------------------------------------
# Hidden-face removal and hole filling (replaces _fill_holes: nvdiffrast + igraph + pymeshfix)
# --------------------------------------------------------------------------------------------

def _radical_inverse(base: int, n: int) -> float:
    val, inv = 0.0, 1.0 / base
    f = inv
    while n > 0:
        val += (n % base) * f
        n //= base
        f *= inv
    return val


def sphere_hammersley(n: int, num: int) -> tuple[float, float]:
    """Same camera distribution as trellis.utils.random_utils.sphere_hammersley_sequence."""
    u, v = n / num, _radical_inverse(2, n)
    return v * 2 * math.pi, math.acos(1 - 2 * u) - math.pi / 2


@torch.no_grad()
def face_visibility(verts: torch.Tensor, faces: torch.Tensor, num_views: int, resolution: int) -> torch.Tensor:
    """Fraction of views in which each face is visible (cameras as in TRELLIS _fill_holes)."""
    import utils3d.torch as u3
    fov = torch.deg2rad(torch.tensor(40.0)).cuda()
    proj = u3.perspective_from_fov_xy(fov, fov, 1, 3)
    vis = torch.zeros(faces.shape[0], dtype=torch.int32, device=verts.device)
    vh = torch.cat([verts, torch.ones_like(verts[:, :1])], -1)
    for i in range(num_views):
        yaw, pitch = sphere_hammersley(i, num_views)
        orig = torch.tensor([math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), math.sin(pitch)],
                            device="cuda") * 2.0
        view = u3.view_look_at(orig, torch.zeros(3, device="cuda"), torch.tensor([0.0, 0.0, 1.0], device="cuda"))
        clip = vh @ (proj @ view).T
        fid, _ = rasterize(clip, faces, resolution, resolution)
        seen = torch.unique(fid[fid >= 0])
        vis[seen] += 1
    return vis.float() / num_views


def _min_cut_max_source_side(num_nodes: int, edges: np.ndarray, caps: np.ndarray, s: int, t: int) -> np.ndarray:
    """Largest source side of an s-t minimum cut of an undirected capacitated graph (scipy max flow): every node
    that cannot reach t in the residual graph. This is igraph's mincut partition[0], which TRELLIS's _fill_holes
    uses; unlike the nodes reachable from s it also holds components connected to neither s nor t."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import breadth_first_order, maximum_flow
    rows = np.concatenate([edges[:, 0], edges[:, 1]])
    cols = np.concatenate([edges[:, 1], edges[:, 0]])
    data = np.concatenate([caps, caps]).astype(np.int32)
    g = csr_matrix((data, (rows, cols)), shape=(num_nodes, num_nodes))  # duplicates are summed
    g.sum_duplicates()
    res = maximum_flow(g, s, t, method="dinic")
    if res.flow_value >= np.iinfo(np.int32).max // 2:
        raise OverflowError("max-flow value close to the int32 limit; lower the capacity scale")
    residual = (g - res.flow.tocsr())
    residual.data[residual.data < 0] = 0
    residual.eliminate_zeros()
    reach_t = breadth_first_order(residual.T.tocsr(), t, directed=True, return_predecessors=False)
    return np.setdiff1d(np.arange(num_nodes), reach_t)


def fill_small_holes(verts: np.ndarray, faces: np.ndarray, max_edges: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Close boundary loops with at most `max_edges` edges by a fan around the loop centroid."""
    he = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    key = he[:, 0].astype(np.int64) * len(verts) + he[:, 1]
    rkey = he[:, 1].astype(np.int64) * len(verts) + he[:, 0]
    boundary = he[~np.isin(key, rkey)]  # half-edges without a twin
    out_count = np.bincount(boundary[:, 0], minlength=len(verts))
    nxt = {int(a): int(b) for a, b in boundary if out_count[a] == 1}
    new_v, new_f, used, filled = [], [], set(), 0
    for start in list(nxt):
        if start in used:
            continue
        loop, cur = [], start
        while cur in nxt and cur not in used and len(loop) <= max_edges:
            used.add(cur)
            loop.append(cur)
            cur = nxt[cur]
        if cur != start or len(loop) < 3 or len(loop) > max_edges:
            continue
        c = len(verts) + len(new_v)
        new_v.append(verts[loop].mean(0))
        for a, b in zip(loop, loop[1:] + loop[:1]):
            new_f.append((b, a, c))  # boundary half-edge a->b gets its twin b->a
        filled += 1
    if new_v:
        verts = np.concatenate([verts, np.asarray(new_v, dtype=verts.dtype)])
        faces = np.concatenate([faces, np.asarray(new_f, dtype=faces.dtype)])
    return verts, faces, filled


@torch.no_grad()
def remove_hidden_faces(verts: torch.Tensor, faces: torch.Tensor, max_hole_size=0.04, max_hole_nbe=32,
                        resolution=1024, num_views=1000) -> tuple[np.ndarray, np.ndarray, dict]:
    """Port of TRELLIS _fill_holes with the substitutions listed in the module docstring."""
    import utils3d.torch as u3
    info = {}
    t0 = time.perf_counter()
    vis = face_visibility(verts, faces, num_views, resolution)
    torch.cuda.synchronize()
    info["visibility_seconds"] = round(time.perf_counter() - t0, 3)

    edges, face2edge, edge_degrees = u3.compute_edges(faces)
    boundary_edge_indices = torch.nonzero(edge_degrees == 1).reshape(-1)
    components = u3.compute_connected_components(faces, edges, face2edge)
    outer = torch.zeros(faces.shape[0], dtype=torch.bool, device=faces.device)
    for cc in components:
        outer[cc] = vis[cc] > min(max(vis[cc].quantile(0.75).item(), 0.25), 0.5)
    outer_idx = outer.nonzero().reshape(-1)
    inner_idx = torch.nonzero(vis == 0).reshape(-1)
    info["invisible_faces"] = int(inner_idx.numel())
    if inner_idx.numel() == 0:
        return verts.cpu().numpy(), faces.cpu().numpy(), info

    t0 = time.perf_counter()
    dual_edges, dual_edge2edge = u3.compute_dual_graph(face2edge)
    dual_edge2edge = edges[dual_edge2edge]
    dual_w = torch.norm(verts[dual_edge2edge[:, 0]] - verts[dual_edge2edge[:, 1]], dim=1)
    nf = faces.shape[0]
    s, t = nf, nf + 1
    scale = 2e4  # integer capacities (scipy uses int32); same ratios as the original (edge length vs 1)
    graph_edges = np.concatenate([
        dual_edges.cpu().numpy(),
        np.stack([np.full(inner_idx.numel(), s), inner_idx.cpu().numpy()], 1),
        np.stack([outer_idx.cpu().numpy(), np.full(outer_idx.numel(), t)], 1),
    ])
    caps = np.concatenate([
        np.maximum(np.round(dual_w.cpu().numpy() * scale), 1),
        np.full(inner_idx.numel() + outer_idx.numel(), scale),
    ])
    side = _min_cut_max_source_side(nf + 2, graph_edges, caps, s, t)
    remove_idx = torch.tensor(np.sort(side[side < nf]), dtype=torch.long, device=faces.device)
    info["mincut_seconds"] = round(time.perf_counter() - t0, 3)

    valid = []
    if remove_idx.numel():
        for cc in u3.compute_connected_components(faces[remove_idx]):
            if vis[remove_idx[cc]].median() > 0.25:
                continue
            cc_e, cc_deg = torch.unique(face2edge[remove_idx[cc]], return_counts=True)
            cc_b = cc_e[cc_deg == 1]
            cc_new = cc_b[~torch.isin(cc_b, boundary_edge_indices)]
            if len(cc_new) > 0:
                areas = []
                for ecc in u3.compute_edge_connected_components(edges[cc_new]):
                    e = edges[cc_new[ecc]]
                    center = verts[e].mean(dim=1).mean(dim=0)
                    areas.append(torch.norm(torch.cross(verts[e[:, 0]] - center, verts[e[:, 1]] - center, dim=-1), dim=1).sum() * 0.5)
                if any(a > max_hole_size for a in areas):
                    continue
            valid.append(cc)
    if valid:
        drop = remove_idx[torch.cat(valid)]
        mask = torch.ones(nf, dtype=torch.bool, device=faces.device)
        mask[drop] = False
        faces, verts = u3.remove_unreferenced_vertices(faces[mask], verts)
        info["removed_faces"] = int((~mask).sum())
    else:
        info["removed_faces"] = 0

    v, f, filled = fill_small_holes(verts.cpu().numpy(), faces.cpu().numpy(), max_hole_nbe)
    info["filled_holes"] = filled
    return v, f, info


# --------------------------------------------------------------------------------------------
# Texture baking
# --------------------------------------------------------------------------------------------

def vertex_normals(verts: torch.Tensor, faces: torch.Tensor) -> torch.Tensor:
    fn = torch.cross(verts[faces[:, 1]] - verts[faces[:, 0]], verts[faces[:, 2]] - verts[faces[:, 0]], dim=-1)
    vn = torch.zeros_like(verts).index_add_(0, faces.reshape(-1), fn.repeat_interleave(3, 0))
    return torch.nn.functional.normalize(vn, dim=-1)


def gaussian_params(g: dict) -> dict:
    """Activated Gaussian parameters (same formulas as trellis.representations.Gaussian)."""
    p = g["init_params"]
    aabb = torch.tensor(p["aabb"], dtype=torch.float32, device="cuda")
    sb = torch.tensor(p["scaling_bias"])
    scale_bias = (sb + torch.log(-torch.expm1(-sb))).cuda() if p["scaling_activation"] == "softplus" else torch.log(sb).cuda()
    act = torch.nn.functional.softplus if p["scaling_activation"] == "softplus" else torch.exp
    scales = act(g["_scaling"].cuda() + scale_bias)
    scales = torch.sqrt(scales ** 2 + p["mininum_kernel_size"] ** 2)
    ob = torch.tensor(p["opacity_bias"])
    opacity = torch.sigmoid(g["_opacity"].cuda() + torch.log(ob / (1 - ob)).cuda())[:, 0]
    q = torch.nn.functional.normalize(g["_rotation"].cuda() + torch.tensor([1.0, 0, 0, 0], device="cuda"), dim=-1)
    r, x, y, z = q.unbind(-1)
    R = torch.stack([
        1 - 2 * (y * y + z * z), 2 * (x * y - r * z), 2 * (x * z + r * y),
        2 * (x * y + r * z), 1 - 2 * (x * x + z * z), 2 * (y * z - r * x),
        2 * (x * z - r * y), 2 * (y * z + r * x), 1 - 2 * (x * x + y * y)], -1).view(-1, 3, 3)
    xyz = g["_xyz"].cuda() * aabb[3:] + aabb[:3]
    rgb = (0.5 + 0.28209479177387814 * g["_features_dc"].cuda()[:, 0, :]).clamp(0, 1)
    return {"xyz": xyz, "scales": scales, "R": R, "opacity": opacity, "rgb": rgb}


@torch.no_grad()
def colors_from_gaussians(points: torch.Tensor, normals: torch.Tensor, gp: dict, k: int = 32,
                          behind: float = 0.01, chunk: int = 1 << 18) -> torch.Tensor:
    """Density-weighted colour of the Gaussian field at surface points.

    Gaussians whose centre lies more than `behind` below the surface (along the normal) are ignored
    so that thin parts do not take the colour of their back side.
    """
    from scipy.spatial import cKDTree
    tree = cKDTree(gp["xyz"].cpu().numpy())
    _, nn = tree.query(points.cpu().numpy(), k=k, workers=-1)
    nn = torch.as_tensor(nn, device="cuda")
    out = torch.empty((points.shape[0], 3), device="cuda")
    for a in range(0, points.shape[0], chunk):
        idx = nn[a:a + chunk]
        p = points[a:a + chunk, None, :]
        d = p - gp["xyz"][idx]                                   # (n, k, 3) point - centre
        local = torch.einsum("nkji,nkj->nki", gp["R"][idx], d)   # R^T d
        m = ((local / gp["scales"][idx]) ** 2).sum(-1)
        w = gp["opacity"][idx] * torch.exp(-0.5 * m)
        if behind is not None:
            depth = (-d * normals[a:a + chunk, None, :]).sum(-1)  # (centre - point) . n
            w = torch.where(depth < -behind, torch.zeros_like(w), w)
        ws = w.sum(-1, keepdim=True)
        col = (w[..., None] * gp["rgb"][idx]).sum(1) / ws.clamp(min=1e-12)
        nearest = gp["rgb"][idx[:, 0]]
        out[a:a + chunk] = torch.where(ws > 1e-4, col, nearest)
    return out


@torch.no_grad()
def colors_from_gaussians_composite(points: torch.Tensor, normals: torch.Tensor, gp: dict, k: int = 32,
                                    behind: float = 0.01, front: float | None = None,
                                    fallback: str = "nearest", chunk: int = 1 << 16) -> torch.Tensor:
    """Colour seen when looking at each surface point along its normal from outside.

    Each neighbouring Gaussian is projected onto the plane perpendicular to the normal (as a splatting
    renderer does for a view along the normal), and the splats are alpha-composited front to back.
    Gaussians more than `behind` below the surface, or more than `front` above it (they belong to
    another part of the model), are ignored.
    """
    from scipy.spatial import cKDTree
    tree = cKDTree(gp["xyz"].cpu().numpy())
    _, nn = tree.query(points.cpu().numpy(), k=k, workers=-1)
    nn = torch.as_tensor(nn, device="cuda")
    out = torch.empty((points.shape[0], 3), device="cuda")
    for a in range(0, points.shape[0], chunk):
        idx = nn[a:a + chunk]
        n = normals[a:a + chunk]
        helper = torch.where((n[:, :1].abs() < 0.9), torch.tensor([1.0, 0, 0], device="cuda"), torch.tensor([0, 1.0, 0], device="cuda"))
        u = torch.nn.functional.normalize(torch.cross(n, helper, dim=-1), dim=-1)
        v = torch.cross(n, u, dim=-1)
        B = torch.stack([u, v], 1)                                   # (c, 2, 3)
        d = gp["xyz"][idx] - points[a:a + chunk, None, :]            # centre - point
        depth = (d * n[:, None, :]).sum(-1)                          # > 0: in front of the surface
        d2 = torch.einsum("cij,ckj->cki", B, d)                      # (c, k, 2)
        A = torch.einsum("cij,ckjl->ckil", B, gp["R"][idx]) * gp["scales"][idx][:, :, None, :]  # (c, k, 2, 3)
        S = A @ A.transpose(-1, -2)                                   # projected 2D covariance
        det = (S[..., 0, 0] * S[..., 1, 1] - S[..., 0, 1] ** 2).clamp(min=1e-20)
        m = (S[..., 1, 1] * d2[..., 0] ** 2 - 2 * S[..., 0, 1] * d2[..., 0] * d2[..., 1] + S[..., 0, 0] * d2[..., 1] ** 2) / det
        alpha = (gp["opacity"][idx] * torch.exp(-0.5 * m)).clamp(max=0.99)
        alpha = torch.where(depth < -behind, torch.zeros_like(alpha), alpha)
        if front is not None:
            alpha = torch.where(depth > front, torch.zeros_like(alpha), alpha)
        order = torch.argsort(depth, dim=1, descending=True)         # front to back
        alpha = torch.gather(alpha, 1, order)
        rgb = gp["rgb"][torch.gather(idx, 1, order)]
        trans = torch.cumprod(torch.cat([torch.ones_like(alpha[:, :1]), 1 - alpha[:, :-1]], 1), 1)
        w = trans * alpha
        ws = w.sum(-1, keepdim=True)
        col = (w[..., None] * rgb).sum(1) / ws.clamp(min=1e-12)
        missing = rgb[:, 0] if fallback == "nearest" else torch.full_like(col, float("nan"))
        out[a:a + chunk] = torch.where(ws > 1e-3, col, missing)
    return out


@torch.no_grad()
def colors_from_vertex_attrs(points: torch.Tensor, raw_verts: np.ndarray, raw_faces: np.ndarray,
                             raw_colors: np.ndarray) -> torch.Tensor:
    """Colour of the closest point on the full-resolution decoder mesh (open3d / Embree, CPU)."""
    import open3d as o3d
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(raw_verts.astype(np.float32)), o3d.core.Tensor(raw_faces.astype(np.uint32)))
    ans = scene.compute_closest_points(o3d.core.Tensor(points.cpu().numpy().astype(np.float32)))
    pid = ans["primitive_ids"].numpy().astype(np.int64)
    uv = ans["primitive_uvs"].numpy()
    tri = raw_colors[raw_faces[pid]]  # (N, 3, 3)
    col = tri[:, 0] * (1 - uv[:, :1] - uv[:, 1:]) + tri[:, 1] * uv[:, :1] + tri[:, 2] * uv[:, 1:]
    return torch.as_tensor(np.clip(col, 0, 1), dtype=torch.float32, device="cuda")


@torch.no_grad()
def bake_texture(verts: np.ndarray, faces: np.ndarray, uvs: np.ndarray, texture_size: int, source: str,
                 raw: dict, gp: dict | None, behind: float = 0.01, front: float | None = None,
                 k: int = 32) -> np.ndarray:
    """Texture image (rows top-down, i.e. row 0 = v 1) for the UV layout."""
    import cv2
    v = torch.tensor(verts, dtype=torch.float32, device="cuda")
    f = torch.tensor(faces.astype(np.int64), device="cuda")
    uv = torch.tensor(uvs, dtype=torch.float32, device="cuda")
    clip = torch.cat([uv * 2 - 1, torch.zeros_like(uv[:, :1]), torch.ones_like(uv[:, :1])], -1)
    fid, bary = rasterize(clip, f, texture_size, texture_size)
    covered = fid >= 0
    pos = interpolate(v, f, fid, bary)[covered]
    nrm = torch.nn.functional.normalize(interpolate(vertex_normals(v, f), f, fid, bary)[covered], dim=-1)
    if source == "gaussian":
        col = colors_from_gaussians_composite(pos, nrm, gp, k=k, behind=behind, front=front, fallback=FALLBACK)
    elif source == "gaussian-density":
        col = colors_from_gaussians(pos, nrm, gp, behind=behind)
    elif source == "vertex":
        m = raw["mesh"]
        col = colors_from_vertex_attrs(pos, m["vertices"].numpy(), m["faces"].numpy(), m["vertex_attrs"][:, :3].numpy())
    else:
        raise ValueError(source)
    tex = torch.zeros((texture_size, texture_size, 3), device="cuda")
    tex[covered] = torch.nan_to_num(col)
    unresolved = torch.zeros_like(covered)
    unresolved[covered] = torch.isnan(col).any(-1)  # no Gaussian contributes: fill from neighbours
    tex = (tex.clamp(0, 1) * 255).round().byte().cpu().numpy()
    hole = (~covered | unresolved).cpu().numpy().astype(np.uint8)
    tex = cv2.inpaint(tex, hole, 3, cv2.INPAINT_TELEA)
    return tex[::-1].copy()  # row 0 = v 0 -> image row 0 = v 1


# --------------------------------------------------------------------------------------------
# Whole pipeline
# --------------------------------------------------------------------------------------------

def to_glb_alt(raw: dict, simplify: float = 0.95, texture_size: int = 1024, source: str = "gaussian",
               fill_holes: bool = True, fill_holes_views: int = 1000, fill_holes_resolution: int = 1024,
               behind: float = 0.01, front: float | None = None, k: int = 32,
               times: dict | None = None, counts: dict | None = None):
    """Same contract as trellis to_glb: returns a y-up trimesh.Trimesh with one baseColor texture."""
    import pyvista as pv
    import trimesh
    import xatlas
    from PIL import Image

    times = {} if times is None else times
    counts = {} if counts is None else counts

    def tick(name, t0):
        torch.cuda.synchronize()
        times[name] = round(time.perf_counter() - t0, 3)

    vertices = raw["mesh"]["vertices"].numpy()
    faces = raw["mesh"]["faces"].numpy()

    t0 = time.perf_counter()
    if simplify > 0:
        mesh = pv.PolyData(vertices, np.concatenate([np.full((faces.shape[0], 1), 3), faces], axis=1))
        mesh = mesh.decimate(simplify)
        vertices, faces = np.asarray(mesh.points, dtype=np.float32), mesh.faces.reshape(-1, 4)[:, 1:]
    tick("decimate", t0)
    counts["after_decimate"] = {"vertices": int(len(vertices)), "faces": int(len(faces))}

    if fill_holes:
        t0 = time.perf_counter()
        vertices, faces, info = remove_hidden_faces(
            torch.tensor(vertices, device="cuda"), torch.tensor(faces.astype(np.int64), device="cuda"),
            max_hole_size=0.04, max_hole_nbe=int(250 * np.sqrt(1 - simplify)),
            resolution=fill_holes_resolution, num_views=fill_holes_views)
        tick("remove_hidden_faces", t0)
        counts["hidden_face_cleanup"] = info
    counts["after_postprocess"] = {"vertices": int(len(vertices)), "faces": int(len(faces))}

    t0 = time.perf_counter()
    vmapping, indices, uvs = xatlas.parametrize(vertices, faces)
    vertices, faces = vertices[vmapping], indices
    tick("xatlas", t0)
    counts["after_xatlas"] = {"vertices": int(len(vertices)), "faces": int(len(faces))}

    t0 = time.perf_counter()
    gp = gaussian_params(raw["gaussian"]) if source.startswith("gaussian") else None
    texture = bake_texture(vertices, faces, uvs, texture_size, source, raw, gp, behind=behind, front=front, k=k)
    tick(f"bake_texture ({source})", t0)

    vertices = vertices @ np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])  # z-up -> y-up, as in TRELLIS
    # metallicFactor を書かないと glTF の既定（1 = 金属）になり、外部のビューアで暗く見える。ゲームは金属度を使わない
    # （TRELLIS 本体の to_glb は変えない。アプリの 3D プレビューは読み込み時に金属度を 0 にする）
    material = trimesh.visual.material.PBRMaterial(
        roughnessFactor=1.0, metallicFactor=0.0, baseColorTexture=Image.fromarray(texture),
        baseColorFactor=np.array([255, 255, 255, 255], dtype=np.uint8))
    return trimesh.Trimesh(vertices, faces, visual=trimesh.visual.TextureVisuals(uv=uvs, material=material))


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from measure import Stage, glb_stats, process_peak, render_views

    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--variants", nargs="+", default=["0.95:1024"])
    ap.add_argument("--sources", nargs="+", default=["gaussian", "vertex"], choices=["gaussian", "gaussian-density", "vertex"])
    ap.add_argument("--no-fill-holes", action="store_true")
    ap.add_argument("--fill-holes-views", type=int, default=1000)
    ap.add_argument("--fill-holes-resolution", type=int, default=1024)
    ap.add_argument("--behind", type=float, default=0.01, help="gaussian modes: back-side cutoff (<0: off)")
    ap.add_argument("--front", type=float, default=-1, help="gaussian mode: front-side cutoff (<0: off)")
    ap.add_argument("--k", type=int, default=32, help="gaussian modes: neighbours per texel")
    ap.add_argument("--tag", default="", help="suffix for output names")
    ap.add_argument("--no-views", action="store_true")
    ap.add_argument("--metrics", type=Path, default=None, help="metrics JSON path (default <out>/<name>_alt_metrics.json)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    name = args.name or args.dump.stem

    stages: list[dict] = []
    with Stage(stages, "load dump"):
        raw = torch.load(args.dump, map_location="cpu", weights_only=True)   # plain tensors only (no code)
    import importlib.util
    result = {"dump": str(args.dump), "dump_meta": raw.get("meta"), "variants": [],
              "absent_modules": [m for m in ("nvdiffrast", "diff_gaussian_rasterization", "pymeshfix", "igraph")
                                 if importlib.util.find_spec(m) is None]}
    for spec in args.variants:
        simplify, tex = spec.split(":")
        simplify, tex = float(simplify), int(tex)
        for source in args.sources:
            tag = f"alt-{source}_s{simplify:g}_t{tex}" + ("_nofill" if args.no_fill_holes else "") + args.tag
            times, counts = {}, {}
            with Stage(stages, f"to_glb_alt {tag}", simplify=simplify, texture_size=tex, source=source):
                glb = to_glb_alt(raw, simplify, tex, source, fill_holes=not args.no_fill_holes,
                                 fill_holes_views=args.fill_holes_views,
                                 fill_holes_resolution=args.fill_holes_resolution,
                                 behind=None if args.behind < 0 else args.behind,
                                 front=None if args.front < 0 else args.front, k=args.k,
                                 times=times, counts=counts)
            path = args.out / f"{name}_{tag}.glb"
            with Stage(stages, f"export {tag}"):
                glb.export(str(path))
            stats = glb_stats(path)
            stats.update({"simplify": simplify, "texture_size": tex, "source": source, "file": path.name,
                          "parts_seconds": times, "intermediate_counts": counts,
                          "stage": stages[-2]})
            result["variants"].append(stats)
            print(f"[glb] {path.name}: {stats['vertices']} verts, {stats['triangles']} tris, parts {times}, "
                  f"cleanup {counts.get('hidden_face_cleanup')}", flush=True)
            if not args.no_views:
                render_views(path, args.out / f"{name}_{tag}_views.png")
    result["stages"] = stages
    result["process_peak"] = process_peak(stages)
    (args.metrics or args.out / f"{name}_alt_metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["process_peak"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
