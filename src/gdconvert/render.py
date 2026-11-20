"""確認画像用の簡易ソフトウェアレンダラー（numpy のみ。正射影・Z バッファ・ランバート陰影）。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import geometry, texture

__all__ = ["Layer", "View", "VIEWS", "render_layers", "bone_palette", "weight_colors", "label", "grid_sheet"]


@dataclass
class Layer:
    positions: np.ndarray
    faces: np.ndarray                    # 反時計回り
    uvs: np.ndarray | None = None
    image: np.ndarray | None = None      # (h, w, 3|4) uint8
    vertex_colors: np.ndarray | None = None   # (n, 3) 0〜1
    color: tuple[float, float, float] = (0.75, 0.75, 0.75)
    overlay_alpha: float = 0.0           # >0 なら奥行きを無視して半透明で重ねる（X 線表示）
    outline: tuple[int, int, int] | None = None   # 輪郭線の色
    fill: bool = True                    # False なら輪郭線だけ描く


@dataclass(frozen=True)
class View:
    name: str
    yaw_deg: float          # カメラの方向。0 = +z 側から見る（正面）、90 = +x 側から見る
    pitch_deg: float = 0.0

    def basis(self):
        a, e = np.radians(self.yaw_deg), np.radians(self.pitch_deg)
        c = np.array([np.sin(a) * np.cos(e), np.sin(e), np.cos(a) * np.cos(e)])   # 原点 → カメラ
        up0 = np.array([0.0, 1.0, 0.0])
        r = np.cross(up0, c)
        r /= np.linalg.norm(r)
        u = np.cross(c, r)
        return r, u, c


VIEWS = {
    "front(+Z)": View("front(+Z)", 0), "back(-Z)": View("back(-Z)", 180), "left(+X)": View("left(+X)", 90),
    "right(-X)": View("right(-X)", -90), "3/4": View("3/4", 35, 12),
}


def _fragments(sx, sy, F, w, h, max_bbox=48):
    """スクリーン座標（画素中心 = 整数）の三角形を画素に分解する。戻り値 (tri, px, py, l0, l1, l2)。"""
    tx, ty = sx[F], sy[F]
    x0 = np.clip(np.floor(tx.min(1)).astype(int), 0, w - 1)
    x1 = np.clip(np.ceil(tx.max(1)).astype(int), 0, w - 1)
    y0 = np.clip(np.floor(ty.min(1)).astype(int), 0, h - 1)
    y1 = np.clip(np.ceil(ty.max(1)).astype(int), 0, h - 1)
    vis = (tx.max(1) >= 0) & (tx.min(1) <= w - 1) & (ty.max(1) >= 0) & (ty.min(1) <= h - 1)
    bw = x1 - x0 + 1
    bh = y1 - y0 + 1
    out = []

    def test(sel, px, py):
        ax, ay = tx[sel, 0:1], ty[sel, 0:1]
        bx, by = tx[sel, 1:2], ty[sel, 1:2]
        cx, cy = tx[sel, 2:3], ty[sel, 2:3]
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        with np.errstate(divide="ignore", invalid="ignore"):
            l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
            l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
            l2 = 1 - l0 - l1
        ins = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9) & np.isfinite(l0) & np.isfinite(l1)
        r, c = np.nonzero(ins)
        out.append((sel[r], px[r, c], py[r, c], l0[r, c], l1[r, c], l2[r, c]))

    small = vis & (bw <= max_bbox) & (bh <= max_bbox)
    ids = np.flatnonzero(small)
    if len(ids):
        keys = bw[ids] * 1000 + bh[ids]
        for key in np.unique(keys):
            sel = ids[keys == key]
            ww, hh = int(key // 1000), int(key % 1000)
            gx, gy = np.meshgrid(np.arange(ww), np.arange(hh))
            for s in range(0, len(sel), max(1, 400000 // (ww * hh))):
                ss = sel[s:s + max(1, 400000 // (ww * hh))]
                test(ss, x0[ss, None] + gx.reshape(1, -1), y0[ss, None] + gy.reshape(1, -1))
    for t in np.flatnonzero(vis & ~small):
        gx, gy = np.meshgrid(np.arange(x0[t], x1[t] + 1), np.arange(y0[t], y1[t] + 1))
        test(np.array([t]), gx.reshape(1, -1), gy.reshape(1, -1))
    if not out:
        z = np.zeros(0, np.int64)
        return z, z, z, np.zeros(0), np.zeros(0), np.zeros(0)
    return tuple(np.concatenate([o[i] for o in out]) for i in range(6))


def _render_one(layer: Layer, view: View, w: int, h: int, origin, scale):
    r, u, c = view.basis()
    P = np.asarray(layer.positions, dtype=np.float64)
    F = np.asarray(layer.faces, dtype=np.int64)
    sx = (P @ r - origin[0]) / scale
    sy = (origin[1] - P @ u) / scale
    depth = P @ c
    tri, px, py, l0, l1, l2 = _fragments(sx, sy, F, w, h)
    color = np.zeros((h, w, 3))
    mask = np.zeros((h, w), dtype=bool)
    if len(tri) == 0:
        return color, mask
    z = l0 * depth[F[tri, 0]] + l1 * depth[F[tri, 1]] + l2 * depth[F[tri, 2]]
    pix = py * w + px
    order = np.lexsort((-z, pix))
    first = np.ones(len(order), dtype=bool)
    first[1:] = pix[order][1:] != pix[order][:-1]
    sel = order[first]
    tri, px, py, l0, l1, l2 = tri[sel], px[sel], py[sel], l0[sel], l1[sel], l2[sel]
    fn = geometry.face_normals(P, F)
    shade = 0.35 + 0.65 * np.abs(fn[tri] @ c)
    lam = np.stack([l0, l1, l2], axis=1)
    if layer.image is not None and layer.uvs is not None:
        uv = np.einsum("nk,nkj->nj", lam, layer.uvs[F[tri]])
        base = texture.sample_bilinear(layer.image[..., :3], uv, wrap=False) / 255.0
    elif layer.vertex_colors is not None:
        base = np.einsum("nk,nkj->nj", lam, np.asarray(layer.vertex_colors)[F[tri]])
    else:
        base = np.repeat(np.array(layer.color, dtype=np.float64)[None], len(tri), axis=0)
    color[py, px] = np.clip(base * shade[:, None], 0, 1)
    mask[py, px] = True
    return color, mask


def render_layers(layers: list[Layer], view: View, size=(360, 520), bounds=None, background=(0.86, 0.86, 0.86)):
    """レイヤーを順に描く。``bounds`` = (xmin, xmax, ymin, ymax)（スクリーン座標系の世界単位）。"""
    w, h = size
    r, u, _ = view.basis()
    if bounds is None:
        pts = np.vstack([l.positions for l in layers])
        xs, ys = pts @ r, pts @ u
        bounds = (xs.min(), xs.max(), ys.min(), ys.max())
    xmin, xmax, ymin, ymax = bounds
    pad = 0.04 * max(xmax - xmin, ymax - ymin)
    scale = max((xmax - xmin + 2 * pad) / w, (ymax - ymin + 2 * pad) / h)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    origin = (cx - scale * w / 2, cy + scale * h / 2)
    img = np.ones((h, w, 3)) * np.array(background)
    zbuf_mask = np.zeros((h, w), dtype=bool)
    for layer in layers:
        col, m = _render_one(layer, view, w, h, origin, scale)
        if not layer.fill:
            pass
        elif layer.overlay_alpha > 0:
            a = layer.overlay_alpha
            img[m] = img[m] * (1 - a) + col[m] * a
        else:
            img[m] = col[m]
            zbuf_mask |= m
        if layer.outline is not None:
            from scipy import ndimage
            edge = m & ~ndimage.binary_erosion(m, iterations=1)
            edge = ndimage.binary_dilation(edge, iterations=1)
            img[edge] = np.array(layer.outline) / 255.0
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


def bone_palette(n: int) -> np.ndarray:
    """見分けやすい色を n 個（黄金角で色相を回す）。"""
    import colorsys
    return np.array([colorsys.hsv_to_rgb((i * 0.618034) % 1.0, 0.75 if i % 2 else 0.95, 0.95 if i % 3 else 0.7)
                     for i in range(n)])


def weight_colors(bone_idx: np.ndarray, weights: np.ndarray, nbones: int) -> np.ndarray:
    pal = bone_palette(nbones)
    return np.einsum("nk,nkj->nj", np.asarray(weights, dtype=np.float64), pal[np.asarray(bone_idx)])


def label(img: np.ndarray, text: str, pos=(4, 2), color=(0, 0, 0)) -> np.ndarray:
    from PIL import Image, ImageDraw
    im = Image.fromarray(img)
    d = ImageDraw.Draw(im)
    for line_no, line in enumerate(text.split("\n")):
        d.text((pos[0], pos[1] + 12 * line_no), line, fill=color)
    return np.asarray(im)


def grid_sheet(rows: list[list[np.ndarray]], gap: int = 4, background=255) -> np.ndarray:
    h = max(max(im.shape[0] for im in row) for row in rows)
    w = max(sum(im.shape[1] for im in row) + gap * (len(row) - 1) for row in rows)
    sheet = np.full((len(rows) * h + gap * (len(rows) - 1), w, 3), background, dtype=np.uint8)
    y = 0
    for row in rows:
        x = 0
        for im in row:
            sheet[y:y + im.shape[0], x:x + im.shape[1]] = im[..., :3]
            x += im.shape[1] + gap
        y += h + gap
    return sheet
