"""MeshProcessor: 向きの正規化、掃除、頂点数の自動調整（D-009）。

頂点数の数え方: .msh の頂点は位置・UV・法線などの組で 1 個なので、UV の継ぎ目で分かれた頂点は別に数える
（GLB の頂点数とほぼ同じ）。D-009 の目標（約 25,000）を超える入力は、

1. 継ぎ目の頂点を溶接した形状だけを fast-simplification（MIT。二次誤差による辺の縮約）で間引き、
2. xatlas（MIT）で UV を作り直し、
3. 元のモデルの表面色を新しい UV に焼き直す（:func:`texture.bake_from_model`）。

UV を作り直すと継ぎ目の分だけ頂点が増えるので、結果が目標以下になるまで間引き率を下げて繰り返す。

目標以下でも 1 枚のテクスチャで表せない入力（複数マテリアル・頂点色など）は :func:`unify_texture` で、同じく溶接した
形状に UV を作って焼き直す（溶接で重なった両面の板の表と裏は頂点を分けたままにし、それでも溶接しない方が頂点が
少なければ溶接しない）。それでも継ぎ目の分だけ目標を超えたら、pipeline が間引きの経路
（``reduce_vertices(force=True)``）に回す。
"""
from __future__ import annotations

import time

import numpy as np

from . import geometry, texture
from ._i18n import tr
from .gltf import Material, Model

__all__ = ["orient", "clean", "reduce_vertices", "unify_texture", "uv_seam_ratio"]


def orient(model: Model, M: np.ndarray) -> Model:
    """3×3 行列 M（列ベクトル規約）で座標を変換する。行列式が負なら巻き順も入れ替える。"""
    out = model.copy()
    out.positions = model.positions @ np.asarray(M, dtype=np.float64).T
    if np.linalg.det(M) < 0:
        out.faces = model.faces[:, [0, 2, 1]].copy()
    return out


def clean(model: Model, area_eps: float = 0.0) -> Model:
    """使われていない頂点と面積 0 の三角形を除く。"""
    out = model.copy()
    if area_eps >= 0:
        a = geometry.face_areas(out.positions, out.faces)
        keep = a > area_eps
        out.faces = out.faces[keep]
        out.face_material = out.face_material[keep]
    used = np.unique(out.faces)
    remap = np.full(len(out.positions), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    out.positions = out.positions[used]
    if out.uvs is not None:
        out.uvs = out.uvs[used]
    if out.colors is not None:
        out.colors = out.colors[used]
    out.faces = remap[out.faces]
    return out


def uv_seam_ratio(model: Model, tol: float) -> float:
    remap, reps = geometry.weld(model.positions, tol)
    return len(model.positions) / max(1, len(reps))


def _xatlas(P: np.ndarray, F: np.ndarray, resolution: int, padding: int):
    import xatlas
    atlas = xatlas.Atlas()
    atlas.add_mesh(np.ascontiguousarray(P, dtype=np.float32), np.ascontiguousarray(F, dtype=np.uint32))
    co = xatlas.ChartOptions()
    po = xatlas.PackOptions()
    po.resolution = int(resolution)
    po.padding = int(padding)
    po.bilinear = True
    po.bruteForce = False
    atlas.generate(co, po)
    vmap, idx, uv = atlas[0]
    return np.asarray(vmap, dtype=np.int64), np.asarray(idx, dtype=np.int64), np.asarray(uv, dtype=np.float64)


def _bake_size(model: Model, bake_size: int, max_size: int) -> int:
    if bake_size > 0:
        return bake_size
    sizes = [max(m.image.shape[:2]) for m in model.materials if m.has_texture]
    s = max(sizes) if sizes else 1024
    return int(min(max(texture.pow2_floor(s), 512), max_size))


def _welded(model: Model, weld_tol: float) -> tuple[np.ndarray, np.ndarray]:
    """UV の継ぎ目で分かれた頂点を溶接した形状 (Pw, Fw)。溶接で縮退した面は除く。"""
    remap, reps = geometry.weld(model.positions, weld_tol)
    Pw = model.positions[reps]
    Fw = remap[model.faces]
    ok = (Fw[:, 0] != Fw[:, 1]) & (Fw[:, 1] != Fw[:, 2]) & (Fw[:, 0] != Fw[:, 2])
    return Pw, Fw[ok]


def _split_stacked_faces(P: np.ndarray, F: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """溶接で同じ 3 頂点の組になった面（両面の板の表と裏など）を、頂点の写しで別の面に戻す。

    表と裏を別の頂点・逆の巻き順で作った両面の部分（マント・スカート・髪の板・葉など）は、溶接すると 1 辺に 4 面が
    付く非多様体になり、xatlas がチャートを細かく割る。組の 2 枚目の面には 1 組目の写し、3 枚目には 2 組目の写し…
    を使い、重なる面どうしは頂点を共有しないようにする（溶接しない入力と同じ形）。戻り値 (P, F, 分けた面の数)。"""
    if len(F) == 0:
        return P, F, 0
    key = np.sort(F, axis=1)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    order = np.argsort(inv, kind="stable")
    grp = inv[order]
    starts = np.r_[0, np.flatnonzero(np.diff(grp)) + 1]
    run_start = np.repeat(starts, np.diff(np.r_[starts, len(grp)]))
    layer = np.empty(len(F), dtype=np.int64)
    layer[order] = np.arange(len(grp)) - run_start          # 同じ組の中で何枚目か（0 が 1 枚目）
    if not (layer > 0).any():
        return P, F, 0
    P_out = [P]
    F_out = F.copy()
    n = len(P)
    for k in range(1, int(layer.max()) + 1):
        sel = layer == k
        used = np.unique(F[sel])
        new_idx = np.full(len(P), -1, dtype=np.int64)
        new_idx[used] = n + np.arange(len(used))
        F_out[sel] = new_idx[F[sel]]
        P_out.append(P[used])
        n += len(used)
    return np.vstack(P_out), F_out, int((layer > 0).sum())


def unify_texture(model: Model, *, weld_tol: float | None = None, bake_size: int = 0, max_size: int = 2048,
                  padding: int = 8, log=print) -> tuple[Model, dict]:
    """1 枚のテクスチャで表せない入力（複数マテリアル等）を、xatlas の UV と焼き直しで 1 枚にまとめる。

    ``weld_tol`` を渡すと、:func:`reduce_vertices` と同じく継ぎ目の頂点を溶接した形状を xatlas に渡す（元の継ぎ目の
    分だけ頂点が増えないように。焼き直しは元のモデルから行うので色は失わない）。溶接で重なった面（両面の板の表と裏）
    は頂点を分けたままにする（:func:`_split_stacked_faces`）。それでも溶接した形状の UV の頂点数が入力の頂点数を
    超えたときは、溶接しない入力でも UV を作り、頂点の少ない方を使う（溶接しない方が増えないことの保証。
    記録の ``welded`` は実際に使った方、``weld_fallback`` は比べた結果）。結果が目標の頂点数を超えたかは
    呼び出し側（pipeline）が確かめ、超えたら間引きの経路に回す。"""
    if not texture.needs_bake(model):
        return model, {"rebaked": False}
    t0 = time.time()
    size = _bake_size(model, bake_size, max_size)
    info: dict = {"rebaked": True, "reason": "materials/uv", "size": size, "welded": False}
    if weld_tol is None:
        P, F = model.positions, model.faces
        vmap, F2, uv2 = _xatlas(P, F, size, padding)
    else:
        P, F = _welded(model, weld_tol)
        P, F, n_split = _split_stacked_faces(P, F)
        vmap, F2, uv2 = _xatlas(P, F, size, padding)
        info["welded"] = True
        if n_split:
            info["stacked_faces_split"] = n_split
        if len(vmap) > model.vertex_count:
            # 溶接しない入力の UV は入力の頂点数以上なので、ここを超えたときだけ比べる価値がある
            vmap0, F20, uv20 = _xatlas(model.positions, model.faces, size, padding)
            info["weld_fallback"] = {"welded_vertices": int(len(vmap)), "unwelded_vertices": int(len(vmap0))}
            if len(vmap0) < len(vmap):
                log(tr(f"    溶接した形状の UV の頂点 {len(vmap)} が溶接しない場合の {len(vmap0)} より多いので、溶接しない",
                       f"    The UV of the welded shape has {len(vmap)} vertices, more than {len(vmap0)} without "
                       "welding; not welding"))
                P, vmap, F2, uv2 = model.positions, vmap0, F20, uv20
                info["welded"] = False
                info["weld_fallback"]["reason"] = "more_vertices_when_welded"
    P2 = P[vmap]
    img = texture.bake_from_model(model, P2, F2, uv2, size, size, padding, log=log)
    out = Model(P2, F2, uv2, None, None, [Material("baked", image=img)], list(model.warnings), model.source,
                list(model.notices))
    info.update({"vertices": int(len(P2)), "seconds": round(time.time() - t0, 2)})
    return out, info


def reduce_vertices(model: Model, target: int, hard_max: int = 65535, *, weld_tol: float, bake_size: int = 0,
                    max_size: int = 2048, padding: int = 8, max_tries: int = 5, force: bool = False,
                    log=print) -> tuple[Model, dict]:
    """頂点数（分割頂点）が ``target`` を超えるなら間引いて UV を作り直す。戻り値 (モデル, 記録)。

    ``force`` は入力が目標以下でも間引きの経路を通す（焼き直しだけでは継ぎ目の分だけ目標を超えたとき）。"""
    n0 = model.vertex_count
    if n0 <= target and not force:
        return model, {"decimated": False, "vertices_in": n0}
    import fast_simplification
    t0 = time.time()
    Pw, Fw = _welded(model, weld_tol)
    size = _bake_size(model, bake_size, max_size)
    # 形状の頂点数 ≈ 面数 / 2。xatlas の継ぎ目で 1.2 倍前後に増えるのを見込む
    want_faces = target / 1.25 * 2
    tries = []
    best = None
    for attempt in range(max_tries):
        # 縮小は実際に使った面数に掛ける（面数で切り詰めた値のままだと、同じ試行を繰り返して間引かれない）
        want_faces = min(len(Fw) - 1, max(100, want_faces))
        tf = int(want_faces)
        P2, F2 = fast_simplification.simplify(Pw, Fw, target_count=tf)
        P2 = np.asarray(P2, dtype=np.float64)
        F2 = np.asarray(F2, dtype=np.int64)
        vmap, F3, uv3 = _xatlas(P2, F2, size, padding)
        nv = len(vmap)
        tries.append({"target_faces": tf, "faces": int(len(F2)), "welded_vertices": int(len(P2)), "vertices": int(nv)})
        log(tr(f"  間引き {attempt + 1}: 面 {len(Fw)} → {len(F2)}、UV 作成後の頂点 {nv}",
               f"  Decimation {attempt + 1}: faces {len(Fw)} → {len(F2)}, vertices after creating the UV {nv}"))
        if nv <= target or (nv <= hard_max and attempt == max_tries - 1):
            best = (P2, F3, uv3, vmap)
            break
        want_faces *= (target / nv) * 0.97
    if best is None:
        raise RuntimeError(tr(f"頂点数を {hard_max} 以下にできなかった: {tries}",
                              f"Could not reduce the vertex count to {hard_max} or less: {tries}"))
    P2, F3, uv3, vmap = best
    P3 = P2[vmap]
    img = texture.bake_from_model(model, P3, F3, uv3, size, size, padding, log=log)
    out = Model(P3, F3, uv3, None, None, [Material("baked", image=img)], list(model.warnings), model.source,
                list(model.notices))
    info = {"decimated": True, "vertices_in": n0, "welded_in": int(len(Pw)), "faces_in": int(len(Fw)),
            "vertices_out": int(len(P3)), "faces_out": int(len(F3)), "texture_size": size, "tries": tries,
            "seconds": round(time.time() - t0, 2)}
    return out, info
