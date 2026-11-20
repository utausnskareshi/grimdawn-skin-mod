"""幾何計算の部品（numpy + scipy）。

* :func:`weld` — 同じ位置の頂点（UV の継ぎ目で分かれた頂点）を 1 つにまとめる対応表
* :func:`vertex_normals` — 面積重みの頂点法線（溶接した位相で計算し、分割頂点へ配る）
* :func:`closest_point_on_triangles` — 点と三角形の最近点（Ericson, Real-Time Collision Detection 5.1.5）
* :class:`SurfaceQuery` — メッシュ表面への最近点問い合わせ（三角形番号と重心座標を返す）
* :func:`sample_surface` — 面積に比例した一様サンプリング
* :func:`uniform_laplacian` — グラフラプラシアン（重みの補間・平滑化用）
* :func:`rasterize_uv` — UV 空間で三角形を塗る（テクスチャの焼き込み用）
* :func:`rasterize_uv_coverage` — UV 三角形が覆う画素だけを求める（余白の判定用。重なった大きい三角形でも速い）
"""
from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.spatial import cKDTree

from ._i18n import tr

__all__ = [
    "weld", "vertex_normals", "face_normals", "face_areas", "closest_point_on_triangles",
    "SurfaceQuery", "sample_surface", "uniform_laplacian", "connected_components",
    "rasterize_uv", "rasterize_uv_coverage", "edge_lengths",
]


def weld(positions: np.ndarray, tol: float = 1e-6) -> tuple[np.ndarray, np.ndarray]:
    """``tol`` 以内の頂点をまとめる。

    戻り値 ``(remap, representatives)``: ``remap[i]`` は溶接後の番号、``representatives[j]`` は
    溶接後の頂点 j の代表（元の番号の最小値）。
    """
    p = np.asarray(positions, dtype=np.float64)
    if len(p) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    if tol <= 0:
        _, first, inv = np.unique(p, axis=0, return_index=True, return_inverse=True)
        order = np.argsort(first)
        rank = np.empty_like(order)
        rank[order] = np.arange(len(order))
        return rank[inv.reshape(-1)], first[order]
    q = np.round(p / tol).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    return rank[inv.reshape(-1)], first[order]


def face_normals(positions: np.ndarray, faces: np.ndarray, normalize: bool = True) -> np.ndarray:
    """反時計回り（右手系）で外向きになる面法線。``normalize=False`` なら長さ = 面積の 2 倍。"""
    p = np.asarray(positions, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    n = np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]])
    if normalize:
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = n / np.where(ln == 0, 1.0, ln)
    return n


def face_areas(positions: np.ndarray, faces: np.ndarray) -> np.ndarray:
    return 0.5 * np.linalg.norm(face_normals(positions, faces, normalize=False), axis=1)


def vertex_normals(positions: np.ndarray, faces: np.ndarray, remap: np.ndarray | None = None) -> np.ndarray:
    """面積重みの頂点法線。``remap``（:func:`weld` の結果）を渡すと溶接した位相で平均し、
    継ぎ目で分かれた頂点に同じ法線を与える（継ぎ目で陰影が切れない）。"""
    p = np.asarray(positions, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    fn = face_normals(p, f, normalize=False)
    key = np.arange(len(p)) if remap is None else np.asarray(remap)
    nk = int(key.max()) + 1 if len(key) else 0
    acc = np.zeros((nk, 3))
    for k in range(3):
        np.add.at(acc, key[f[:, k]], fn)
    ln = np.linalg.norm(acc, axis=1, keepdims=True)
    bad = ln[:, 0] < 1e-20
    acc[bad] = (0.0, 1.0, 0.0)
    ln[bad] = 1.0
    out = (acc / ln)[key]
    if remap is not None and len(f):
        # 溶接で同じ 3 頂点の組になった面（表と裏を別の頂点・逆の巻き順で作った両面の板: マント・髪の板・葉など）は、
        # 溶接した位相で平均すると表と裏の法線が打ち消し合い、(0,1,0) やでたらめな向きになる。その面の頂点だけ、
        # 溶接しない法線にする（重なった面の無いメッシュの結果は変わらない。2026-10-27 点検 R03B-02）
        kf = np.sort(key[f], axis=1)
        _, inv, cnt = np.unique(kf, axis=0, return_inverse=True, return_counts=True)
        stacked = (cnt[inv.reshape(-1)] > 1) & (kf[:, 0] != kf[:, 1]) & (kf[:, 1] != kf[:, 2])
        if stacked.any():
            vs = np.unique(f[stacked])
            out[vs] = vertex_normals(p, f)[vs]
    return out


def edge_lengths(positions: np.ndarray, faces: np.ndarray) -> np.ndarray:
    p = np.asarray(positions, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    return np.stack([np.linalg.norm(p[f[:, (k + 1) % 3]] - p[f[:, k]], axis=1) for k in range(3)], axis=1)


# ----------------------------------------------------------------------
# 最近点
# ----------------------------------------------------------------------
def closest_point_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray):
    """点 ``p[i]`` に最も近い三角形 ``(a[i], b[i], c[i])`` 上の点。

    戻り値 ``(point, bary)``。``bary`` は (u, v, w)（point = u·a + v·b + w·c）。退化三角形も扱う。
    """
    p = np.asarray(p, dtype=np.float64)
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    n = len(p)
    ab = b - a
    ac = c - a
    ap = p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4

    bary = np.zeros((n, 3))
    done = np.zeros(n, dtype=bool)

    def put(mask, u, v, w):
        m = mask & ~done
        if m.any():
            bary[m, 0] = u[m] if np.ndim(u) else u
            bary[m, 1] = v[m] if np.ndim(v) else v
            bary[m, 2] = w[m] if np.ndim(w) else w
            done[m] = True

    one = np.ones(n)
    zero = np.zeros(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        # 頂点 A
        put((d1 <= 0) & (d2 <= 0), one, zero, zero)
        # 頂点 B
        put((d3 >= 0) & (d4 <= d3), zero, one, zero)
        # 辺 AB
        t = d1 / (d1 - d3)
        put((vc <= 0) & (d1 >= 0) & (d3 <= 0), 1 - t, t, zero)
        # 頂点 C
        put((d6 >= 0) & (d5 <= d6), zero, zero, one)
        # 辺 AC
        t = d2 / (d2 - d6)
        put((vb <= 0) & (d2 >= 0) & (d6 <= 0), 1 - t, zero, t)
        # 辺 BC
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        put((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), zero, 1 - t, t)
        # 内部
        denom = va + vb + vc
        v = vb / denom
        w = vc / denom
        put(np.ones(n, dtype=bool), 1 - v - w, v, w)
    bad = ~np.isfinite(bary).all(axis=1)
    if bad.any():   # 退化三角形: 3 頂点のうち最も近いもの
        dd = np.stack([np.linalg.norm(p[bad] - x[bad], axis=1) for x in (a, b, c)], axis=1)
        k = np.argmin(dd, axis=1)
        bary[bad] = 0.0
        bary[np.flatnonzero(bad), k] = 1.0
    point = bary[:, 0:1] * a + bary[:, 1:2] * b + bary[:, 2:3] * c
    return point, bary


class SurfaceQuery:
    """三角形メッシュ表面への最近点問い合わせ。

    三角形ごとに代表点（3 頂点・3 辺の中点・重心。大きい三角形は細分した点も）を KD 木に入れ、
    近い代表点を ``k`` 個持つ三角形を候補にして厳密な最近点を求める。
    """

    def __init__(self, positions: np.ndarray, faces: np.ndarray, max_spacing: float | None = None):
        self.V = np.asarray(positions, dtype=np.float64)
        self.F = np.asarray(faces, dtype=np.int64)
        if len(self.F) == 0:
            raise ValueError(tr("三角形がない", "No triangles"))
        a, b, c = self.V[self.F[:, 0]], self.V[self.F[:, 1]], self.V[self.F[:, 2]]
        bc = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (.5, .5, 0), (0, .5, .5), (.5, 0, .5), (1 / 3, 1 / 3, 1 / 3)]
        pts = [w[0] * a + w[1] * b + w[2] * c for w in bc]
        ids = [np.arange(len(self.F))] * len(bc)
        el = edge_lengths(self.V, self.F).max(axis=1)
        if max_spacing is None:
            max_spacing = float(np.median(el)) * 1.5 if len(el) else 1.0
        big = np.flatnonzero(el > max_spacing)
        if len(big):
            # 大きい三角形は辺の分割数 s で格子状に代表点を足す
            s_all = np.minimum(np.ceil(el[big] / max_spacing).astype(int), 32)
            for s in np.unique(s_all):
                sel = big[s_all == s]
                for i in range(s + 1):
                    for j in range(s + 1 - i):
                        u, v = i / s, j / s
                        w = 1 - u - v
                        pts.append(u * a[sel] + v * b[sel] + w * c[sel])
                        ids.append(sel)
        self._pts = np.vstack(pts)
        self._ids = np.concatenate(ids)
        self._tree = cKDTree(self._pts)
        self.face_normals = face_normals(self.V, self.F)

    def query(self, points: np.ndarray, k: int = 16, normals: np.ndarray | None = None,
              normal_penalty: float = 0.0, chunk: int = 200_000):
        """最近点を返す: ``(face, bary, point, distance)``。

        ``normals`` と ``normal_penalty`` を渡すと、法線が逆向き（内積 < 0）の三角形の距離に
        ``normal_penalty`` を足して選ぶ（薄い板の裏面を拾わないため）。``distance`` は実距離。
        """
        pts = np.asarray(points, dtype=np.float64)
        n = len(pts)
        out_f = np.zeros(n, dtype=np.int64)
        out_b = np.zeros((n, 3))
        out_p = np.zeros((n, 3))
        out_d = np.zeros(n)
        k = int(min(k, len(self._pts)))
        step = max(1, chunk // max(k, 1))
        for s in range(0, n, step):
            q = pts[s:s + step]
            _, idx = self._tree.query(q, k=k)
            idx = np.asarray(idx).reshape(len(q), k)
            cand = self._ids[idx]                                    # (m, k)
            m = len(q)
            qq = np.repeat(q, k, axis=0)
            fc = cand.reshape(-1)
            tri = self.F[fc]
            cp, bary = closest_point_on_triangles(qq, self.V[tri[:, 0]], self.V[tri[:, 1]], self.V[tri[:, 2]])
            d = np.linalg.norm(cp - qq, axis=1).reshape(m, k)
            score = d
            if normals is not None and normal_penalty > 0:
                nq = np.repeat(np.asarray(normals[s:s + step], dtype=np.float64), k, axis=0)
                dot = np.einsum("ij,ij->i", nq, self.face_normals[fc]).reshape(m, k)
                score = d + normal_penalty * (dot < 0)
            best = np.argmin(score, axis=1)
            rows = np.arange(m) * k + best
            out_f[s:s + m] = fc[rows]
            out_b[s:s + m] = bary[rows]
            out_p[s:s + m] = cp[rows]
            out_d[s:s + m] = d[np.arange(m), best]
        return out_f, out_b, out_p, out_d


def sample_surface(positions: np.ndarray, faces: np.ndarray, count: int, rng: np.random.Generator | None = None,
                   face_weights: np.ndarray | None = None):
    """面積に比例した一様サンプリング。戻り値 ``(points, face, bary)``。"""
    rng = np.random.default_rng(0) if rng is None else rng
    p = np.asarray(positions, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    area = face_areas(p, f)
    if face_weights is not None:
        area = area * face_weights
    tot = area.sum()
    if tot <= 0:
        raise ValueError(tr("面積 0 のメッシュ", "The mesh has zero area"))
    fi = rng.choice(len(f), size=count, p=area / tot)
    r1 = np.sqrt(rng.random(count))
    r2 = rng.random(count)
    bary = np.stack([1 - r1, r1 * (1 - r2), r1 * r2], axis=1)
    tri = f[fi]
    pts = bary[:, 0:1] * p[tri[:, 0]] + bary[:, 1:2] * p[tri[:, 1]] + bary[:, 2:3] * p[tri[:, 2]]
    return pts, fi, bary


# ----------------------------------------------------------------------
# 位相
# ----------------------------------------------------------------------
def uniform_laplacian(faces: np.ndarray, n: int) -> sparse.csr_matrix:
    """``L = D - A``（A は辺の隣接、重み 1）。"""
    f = np.asarray(faces, dtype=np.int64)
    i = np.concatenate([f[:, 0], f[:, 1], f[:, 2], f[:, 1], f[:, 2], f[:, 0]])
    j = np.concatenate([f[:, 1], f[:, 2], f[:, 0], f[:, 0], f[:, 1], f[:, 2]])
    A = sparse.coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)).tocsr()
    A.data[:] = 1.0      # 重複した辺を 1 にする
    D = sparse.diags(np.asarray(A.sum(axis=1)).reshape(-1))
    return (D - A).tocsr()


def connected_components(faces: np.ndarray, n: int) -> np.ndarray:
    from scipy.sparse.csgraph import connected_components as cc
    f = np.asarray(faces, dtype=np.int64)
    i = np.concatenate([f[:, 0], f[:, 1]])
    j = np.concatenate([f[:, 1], f[:, 2]])
    A = sparse.coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n))
    _, labels = cc(A, directed=False)
    return labels


# ----------------------------------------------------------------------
# UV 空間のラスタライズ
# ----------------------------------------------------------------------
def _uv_edge_test(px, py, ax, ay, bx, by, cx, cy):
    """画素 (px, py) が三角形 (a, b, c) の中か（辺の上を含む）と重心座標。:func:`rasterize_uv` と
    :func:`rasterize_uv_coverage` で同じ式を使う（同じ画素を塗る）。"""
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    with np.errstate(divide="ignore", invalid="ignore"):
        l0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
        l1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
        l2 = 1 - l0 - l1
    eps = -1e-9
    inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps) & np.isfinite(l0) & np.isfinite(l1)
    return inside, l0, l1, l2


def _uv_bboxes(uvs, faces, width: int, height: int):
    """三角形の画素座標 (tx, ty) と、画像に切り詰めた外接矩形 (x0, x1, y0, y1)。"""
    uv = np.asarray(uvs, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64)
    X = uv[:, 0] * width - 0.5
    Y = uv[:, 1] * height - 0.5
    tx, ty = X[f], Y[f]                                      # (m, 3)
    x0 = np.clip(np.floor(tx.min(1)).astype(int), 0, width - 1)
    x1 = np.clip(np.ceil(tx.max(1)).astype(int), 0, width - 1)
    y0 = np.clip(np.floor(ty.min(1)).astype(int), 0, height - 1)
    y1 = np.clip(np.ceil(ty.max(1)).astype(int), 0, height - 1)
    return tx, ty, x0, x1, y0, y1


def rasterize_uv_coverage(uvs: np.ndarray, faces: np.ndarray, width: int, height: int,
                          max_bbox: int = 64) -> np.ndarray:
    """UV 三角形が覆う画素 (h, w) bool。:func:`rasterize_uv` の ``face_id >= 0`` とビット単位で同じ。

    塗りの有無だけを求めるので、重心座標を作らず、全く同じ UV 三角形（同じ頂点の順）は 1 回だけ塗り、大きい三角形は
    外接矩形のまだ塗られていない画素だけを判定する（被覆は和集合なので結果は同じ）。面ごとに UV の 0〜1 全体を使う
    モデル（箱・板・カード）で、テクスチャの工程が 1 三角形 0.3〜0.5 秒・合計で数十分〜数時間止まっていた
    （2026-10-27 点検 R02A-02・R02B-03）。"""
    mask = np.zeros((height, width), dtype=bool)
    f = np.asarray(faces, dtype=np.int64)
    if len(f) == 0:
        return mask
    tx, ty, x0, x1, y0, y1 = _uv_bboxes(uvs, f, width, height)
    _, keep = np.unique(np.concatenate([tx, ty], axis=1), axis=0, return_index=True)
    keep = np.sort(keep)
    tx, ty, x0, x1, y0, y1 = tx[keep], ty[keep], x0[keep], x1[keep], y0[keep], y1[keep]
    bw = x1 - x0 + 1
    bh = y1 - y0 + 1
    small = (bw <= max_bbox) & (bh <= max_bbox)
    # 小さい三角形: rasterize_uv と同じく、バウンディングボックスの大きさごとにまとめてベクトル化
    idx_small = np.flatnonzero(small)
    if len(idx_small):
        keys = bw[idx_small] * 1000 + bh[idx_small]
        for key in np.unique(keys):
            sel = idx_small[keys == key]
            w_, h_ = int(key // 1000), int(key % 1000)
            gx, gy = np.meshgrid(np.arange(w_), np.arange(h_))
            px = x0[sel, None] + gx.reshape(1, -1)
            py = y0[sel, None] + gy.reshape(1, -1)
            inside = _uv_edge_test(px, py, tx[sel, 0:1], ty[sel, 0:1], tx[sel, 1:2], ty[sel, 1:2],
                                   tx[sel, 2:3], ty[sel, 2:3])[0]
            mask[py[inside], px[inside]] = True
    # 大きい三角形: 外接矩形がすべて塗られていれば飛ばし、そうでなければまだ塗られていない画素だけを判定する
    for t in np.flatnonzero(~small):
        sub = mask[y0[t]:y1[t] + 1, x0[t]:x1[t] + 1]
        if sub.all():
            continue
        yy, xx = np.nonzero(~sub)
        px = xx + x0[t]
        py = yy + y0[t]
        inside = _uv_edge_test(px, py, tx[t, 0], ty[t, 0], tx[t, 1], ty[t, 1], tx[t, 2], ty[t, 2])[0]
        mask[py[inside], px[inside]] = True
    return mask


def rasterize_uv(uvs: np.ndarray, faces: np.ndarray, width: int, height: int, max_bbox: int = 64):
    """UV 三角形を画素中心で塗る。戻り値 ``(face_id, bary)``: (h, w) int64（-1 = 空き）と (h, w, 3)。

    画素 (x, y) の中心は UV ``((x + 0.5)/w, (y + 0.5)/h)``（v 0 = 画像の上端。glTF / DirectX と同じ）。
    重なった三角形は後のものが勝つ。
    """
    fid = np.full((height, width), -1, dtype=np.int64)
    bary = np.zeros((height, width, 3))
    tx, ty, x0, x1, y0, y1 = _uv_bboxes(uvs, faces, width, height)
    bw = x1 - x0 + 1
    bh = y1 - y0 + 1
    small = (bw <= max_bbox) & (bh <= max_bbox)
    edge_test = _uv_edge_test

    # 小さい三角形: バウンディングボックスの大きさごとにまとめてベクトル化
    idx_small = np.flatnonzero(small)
    if len(idx_small):
        keys = bw[idx_small] * 1000 + bh[idx_small]
        for key in np.unique(keys):
            sel = idx_small[keys == key]
            w_, h_ = int(key // 1000), int(key % 1000)
            gx, gy = np.meshgrid(np.arange(w_), np.arange(h_))
            px = x0[sel, None] + gx.reshape(1, -1)
            py = y0[sel, None] + gy.reshape(1, -1)
            inside, l0, l1, l2 = edge_test(px, py, tx[sel, 0:1], ty[sel, 0:1], tx[sel, 1:2], ty[sel, 1:2],
                                           tx[sel, 2:3], ty[sel, 2:3])
            r, cidx = np.nonzero(inside)
            fid[py[r, cidx], px[r, cidx]] = sel[r]
            bary[py[r, cidx], px[r, cidx]] = np.stack([l0[r, cidx], l1[r, cidx], l2[r, cidx]], axis=1)
    for t in np.flatnonzero(~small):
        gx, gy = np.meshgrid(np.arange(x0[t], x1[t] + 1), np.arange(y0[t], y1[t] + 1))
        inside, l0, l1, l2 = edge_test(gx, gy, tx[t, 0], ty[t, 0], tx[t, 1], ty[t, 1], tx[t, 2], ty[t, 2])
        fid[gy[inside], gx[inside]] = t
        bary[gy[inside], gx[inside]] = np.stack([l0[inside], l1[inside], l2[inside]], axis=1)
    return fid, bary
