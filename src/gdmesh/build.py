"""頂点配列から新規 Grim Dawn ``.msh`` を組み立てる。

入力はすべて numpy 配列（glTF などから直接埋められる形）::

    positions (n, 3) float   normals (n, 3) float   uvs (n, 2) float
    triangles (m, 3) int     [bone_indices (n, 4) int, bone_weights (n, 4) float]

典型的な使い方::

    from gdmesh import build, msh
    mat = build.MaterialSpec(base_texture="System/textures/testtexturegray.tex",
                             bump_texture="System/textures/flatnml.tex")
    m = build.build_static_mesh(positions, normals, uvs, triangles, materials=[mat])
    msh.write(m, "out.msh")

    ref = msh.read("items/gearhead/head_004a_01.msh")        # ボーン表を流用
    m = build.build_skinned_mesh(positions, normals, uvs, triangles,
                                 bone_indices, bone_weights, ref.bones, materials=[mat],
                                 hitboxes=ref.hitboxes)

バウンディングボックス・セクションの中心/半径・接線/従法線は自動計算する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from . import msh

__all__ = [
    "MaterialSpec", "STATIC_SHADER", "SKINNED_SHADER", "MAX_PALETTE_DEFAULT",
    "compute_normals", "compute_tangents", "compute_bbox", "section_box",
    "build_static_mesh", "build_skinned_mesh", "make_box", "default_hitbox",
]

STATIC_SHADER = "Shaders/standardstatic.ssh"
SKINNED_SHADER = "Shaders/standardskinned.ssh"
# ゲーム同梱メッシュのセクションが参照するボーン数の最大値（観測値 27）。
# これを超える場合はセクションを分割する。シェーダーの boneMatrix[27] が上限なので、これより大きくはできない。
MAX_PALETTE_DEFAULT = msh.MAX_PALETTE


# ======================================================================
# マテリアル
# ======================================================================
@dataclass
class MaterialSpec:
    """マテリアル 1 個の指定。``to_material()`` でチャンク 7 の :class:`msh.Material` になる。

    パラメータの並びはゲーム同梱メッシュと同じ
    (specularColor, specularPower, outlineThickness, baseTexture, bumpTexture, specTexture)。
    ``extra`` で ``glowTexture`` など追加パラメータを足せる（``(名前, 型, 値)``）。
    """

    base_texture: str
    bump_texture: str = "System/textures/flatnml.tex"
    spec_texture: str = ""
    shader: str | None = None                      # None なら静的/スキンで自動選択
    specular_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    specular_power: float = 1.0
    outline_thickness: float = 1.0
    extra: list[tuple[str, int, object]] = field(default_factory=list)

    def to_material(self, skinned: bool) -> msh.Material:
        shader = self.shader or (SKINNED_SHADER if skinned else STATIC_SHADER)
        params = [
            msh.MaterialParam("specularColor", msh.PARAM_FLOAT3, tuple(float(x) for x in self.specular_color)),
            msh.MaterialParam("specularPower", msh.PARAM_FLOAT, float(self.specular_power)),
            msh.MaterialParam("outlineThickness", msh.PARAM_FLOAT, float(self.outline_thickness)),
            msh.MaterialParam("baseTexture", msh.PARAM_STRING, self.base_texture),
            msh.MaterialParam("bumpTexture", msh.PARAM_STRING, self.bump_texture),
            msh.MaterialParam("specTexture", msh.PARAM_STRING, self.spec_texture),
        ]
        for name, typ, value in self.extra:
            params.append(msh.MaterialParam(name, typ, value))
        return msh.Material(shader, params)


# ======================================================================
# 幾何計算
# ======================================================================
def compute_normals(positions: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """面法線を面積重みで頂点に集めて正規化する（法線が無い入力用）。

    面法線は ``cross(p1-p0, p2-p0)`` なので、三角形が **外から見て反時計回り**（glTF/OBJ など）のとき外向きになる。
    .msh の規約（外から見て時計回り）の三角形に使うと内向きになるので、その場合は符号を反転して使う。
    """
    p = np.asarray(positions, dtype=np.float64)
    t = np.asarray(triangles, dtype=np.int64)
    fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
    n = np.zeros_like(p)
    for k in range(3):
        np.add.at(n, t[:, k], fn)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    ln[ln == 0] = 1.0
    return (n / ln).astype(np.float32)


def compute_tangents(positions: np.ndarray, normals: np.ndarray, uvs: np.ndarray,
                     triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """UV から接線と従法線を求め ``(tangent, binormal)`` を返す。

    ゲーム同梱メッシュを調べた結果に合わせた規則（``tests/test_build.py`` で検証）:

    * 接線  = 位置の u 方向勾配 ``dP/du`` を頂点に集め、法線に直交化して正規化
    * 従法線 = 位置の v 方向勾配 ``dP/dv`` の **逆向き** を同様に処理したもの
      （v が下向きの DirectX 流テクスチャ座標のため）
    * 接線と従法線は互いに直交化しない（同梱メッシュでも直交していない）
    """
    p = np.asarray(positions, dtype=np.float64)
    n = np.asarray(normals, dtype=np.float64)
    uv = np.asarray(uvs, dtype=np.float64)
    t = np.asarray(triangles, dtype=np.int64)
    i0, i1, i2 = t[:, 0], t[:, 1], t[:, 2]
    e1 = p[i1] - p[i0]
    e2 = p[i2] - p[i0]
    d1 = uv[i1] - uv[i0]
    d2 = uv[i2] - uv[i0]
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    safe = np.where(det == 0, 1.0, det)
    r = np.where(np.abs(det) < 1e-20, 0.0, 1.0 / safe)
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * r[:, None]    # dP/du
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * r[:, None]    # dP/dv
    tan1 = np.zeros_like(p)
    tan2 = np.zeros_like(p)
    for k in range(3):
        np.add.at(tan1, t[:, k], sdir)
        np.add.at(tan2, t[:, k], tdir)
    tangent = _orthonormalize(tan1, n, fallback_axis=0)
    binormal = _orthonormalize(-tan2, n, fallback_axis=2)
    return tangent.astype(np.float32), binormal.astype(np.float32)


def _orthonormalize(v: np.ndarray, n: np.ndarray, fallback_axis: int) -> np.ndarray:
    """v を n に直交化して正規化する。退化した頂点には n に直交する適当な軸を与える。"""
    out = v - n * np.sum(n * v, axis=1, keepdims=True)
    ln = np.linalg.norm(out, axis=1, keepdims=True)
    bad = ln[:, 0] < 1e-12
    if bad.any():
        axis = np.zeros(3)
        axis[fallback_axis] = 1.0
        alt = np.cross(n[bad], axis)
        alt2 = np.cross(n[bad], np.roll(axis, 1))
        use2 = np.linalg.norm(alt, axis=1) < 1e-6
        alt[use2] = alt2[use2]
        out[bad] = alt
        ln = np.linalg.norm(out, axis=1, keepdims=True)
        ln[ln == 0] = 1.0
    return out / ln


def compute_bbox(positions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(positions, dtype=np.float32)
    if len(p) == 0:
        z = np.zeros(3, dtype=np.float32)
        return z, z.copy()
    return p.min(axis=0), p.max(axis=0)


def section_box(positions: np.ndarray, indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """セクションの中心と半径。ゲーム同梱メッシュと同じ float32 演算で求める。"""
    p = np.asarray(positions, dtype=np.float32)
    used = np.unique(np.asarray(indices).reshape(-1))
    if len(used) == 0:
        z = np.zeros(3, dtype=np.float32)
        return z, z.copy()
    q = p[used]
    mn, mx = q.min(axis=0), q.max(axis=0)
    return (mn + mx) * np.float32(0.5), (mx - mn) * np.float32(0.5)


def default_hitbox(bbox_min: np.ndarray, bbox_max: np.ndarray, name: str = "hitbox_Box001_1") -> msh.Hitbox:
    """バウンディングボックスから静的メッシュ用のヒットボックスを 1 個作る。

    ボーン番号 -1（ワールド空間）、回転は単位行列、平行移動は AABB の中心、半径は AABB の半分。
    ``creatures/pc/hero_00.msh`` のヒットボックス（単位回転＋中心平行移動、ボーン -1）と同じ形式。
    """
    mn = np.asarray(bbox_min, dtype=np.float32)
    mx = np.asarray(bbox_max, dtype=np.float32)
    extent = (mx - mn) * np.float32(0.5)
    center = (mn + mx) * np.float32(0.5)
    return msh.Hitbox(name, extent, np.eye(3, dtype=np.float32), center, -1)


# ======================================================================
# 組み立て
# ======================================================================
def _check_inputs(positions, normals, uvs, triangles, winding="cw"):
    if winding not in ("cw", "ccw"):
        raise ValueError("winding must be 'cw' or 'ccw'")
    p = np.ascontiguousarray(positions, dtype=np.float32)
    if p.ndim != 2 or p.shape[1] != 3:
        raise ValueError(f"positions must be (n,3), got {p.shape}")
    n = len(p)
    if n > 65535:
        raise ValueError(f"too many vertices for uint16 indices: {n} > 65535")
    t = np.ascontiguousarray(triangles, dtype=np.int64)
    if t.ndim != 2 or t.shape[1] != 3:
        raise ValueError(f"triangles must be (m,3), got {t.shape}")
    if len(t) and (t.min() < 0 or t.max() >= n):
        raise ValueError("triangle index out of range")
    if winding == "ccw":
        t = t[:, [0, 2, 1]].copy()
    uv = np.ascontiguousarray(uvs, dtype=np.float32)
    if uv.shape != (n, 2):
        raise ValueError(f"uvs must be ({n},2), got {uv.shape}")
    if normals is None:
        # t は時計回り（.msh の規約）にそろえ済みなので、反時計回り前提の compute_normals の符号を反転する
        nr = -compute_normals(p, t)
    else:
        nr = np.ascontiguousarray(normals, dtype=np.float32)
        if nr.shape != (n, 3):
            raise ValueError(f"normals must be ({n},3), got {nr.shape}")
    # float32 への変換の後に確かめる（float64 の 1e39 などは変換で inf になる）。NaN・inf の位置は外接箱・中心を、
    # UV は接線を NaN・inf にし、validate() でも原因の分からない形になるため
    for what, arr in (("positions", p), ("uvs", uv), ("normals", nr)):
        _check_finite(what, arr)
    return p, nr, uv, t


def _check_finite(what: str, arr: np.ndarray) -> None:
    """NaN・inf（float32 の範囲を超えた値を含む）の要素がある行があれば ValueError。"""
    bad = ~np.isfinite(arr).reshape(len(arr), -1).all(axis=1)
    if bad.any():
        raise ValueError(f"{what} contain NaN or inf (or values beyond the float32 range) "
                         f"in {int(bad.sum())} vertices (first: {int(np.argmax(bad))})")


def _tangent_frame(p, nr, uv, t, tangents, binormals):
    """省略された接線・従法線を UV から計算し、与えられたものは float32 にして形と有限性を確かめる。"""
    if tangents is None or binormals is None:
        tg, bn = compute_tangents(p, nr, uv, t)
        tangents = tg if tangents is None else tangents
        binormals = bn if binormals is None else binormals
    tangents = np.ascontiguousarray(tangents, dtype=np.float32)
    binormals = np.ascontiguousarray(binormals, dtype=np.float32)
    for what, arr in (("tangents", tangents), ("binormals", binormals)):
        if arr.shape != (len(p), 3):
            raise ValueError(f"{what} must be ({len(p)},3), got {arr.shape}")
        _check_finite(what, arr)
    return tangents, binormals


def _material_ids(material_ids, ntri, nmat):
    if material_ids is None:
        return np.zeros(ntri, dtype=np.int64)
    m = np.ascontiguousarray(material_ids, dtype=np.int64)
    if m.shape != (ntri,):
        raise ValueError(f"material_ids must be ({ntri},), got {m.shape}")
    if len(m) and (m.min() < 0 or m.max() >= nmat):
        raise ValueError("material id out of range")
    return m


def _common_chunks(source_path, bbox_min, bbox_max):
    chunks: list[msh.Chunk] = []
    if source_path:
        chunks.append(msh.SourceChunk(source_path))
    chunks.append(msh.FlagsChunk(1, 0))
    chunks.append(msh.BBoxChunk(bbox_min, bbox_max))
    return chunks


def build_static_mesh(positions, normals, uvs, triangles, materials: Sequence[MaterialSpec | msh.Material],
                      material_ids=None, *, tangents=None, binormals=None, source_path: str | None = None,
                      hitboxes: msh.HitboxChunk | Sequence[msh.Hitbox] | None | bool = True,
                      attach_text: str | None = None, winding: str = "cw") -> msh.MshFile:
    """ボーンなしの静的メッシュ（レイアウト 0,4,1,2,3 / ストライド 56）を作る。

    :param normals: ``None`` なら面法線から計算
    :param materials: :class:`MaterialSpec` または :class:`msh.Material` の列
    :param material_ids: 三角形ごとのマテリアル番号（省略時は全て 0）
    :param tangents, binormals: 省略時は UV から計算
    :param hitboxes: ``True`` = AABB から 1 個自動生成、``None``/``False`` = 空、
        または :class:`msh.HitboxChunk` / :class:`msh.Hitbox` の列
    :param winding: 入力三角形の巻き順。``"cw"`` = 外から見て時計回り（.msh の規約、そのまま）、
        ``"ccw"`` = 反時計回り（glTF/OBJ など。頂点順を入れ替える）

    位置・法線・UV・接線・従法線に NaN・inf（float32 の範囲を超えた値を含む）があれば ``ValueError``。
    """
    p, nr, uv, t = _check_inputs(positions, normals, uvs, triangles, winding)
    mats = [m.to_material(False) if isinstance(m, MaterialSpec) else m for m in materials]
    if not mats:
        raise ValueError("at least one material is required")
    mid = _material_ids(material_ids, len(t), len(mats))
    tangents, binormals = _tangent_frame(p, nr, uv, t, tangents, binormals)
    verts = msh.VertexChunk.from_arrays(msh.STATIC_LAYOUT, position=p, uv=uv, normal=nr,
                                        tangent=tangents, binormal=binormals)
    # マテリアル順に三角形を並べ替えてセクションを作る
    order = np.argsort(mid, kind="stable")
    t_sorted = t[order]
    mid_sorted = mid[order]
    sections = []
    start = 0
    for m in range(len(mats)):
        cnt = int((mid_sorted == m).sum())
        if cnt == 0:
            continue
        c, e = section_box(p, t_sorted[start:start + cnt])
        sections.append(msh.Section(m, start, cnt, c, e, []))
        start += cnt
    tri = msh.TriangleChunk(t_sorted.astype("<u2"), sections)
    bmin, bmax = compute_bbox(p)
    chunks = _common_chunks(source_path, bmin, bmax)
    chunks += [verts, tri, msh.BoneChunk([]), _hitbox_chunk(hitboxes, bmin, bmax), msh.MaterialChunk(mats)]
    if attach_text:
        chunks.append(msh.AttachChunk(attach_text))
    return msh.MshFile(chunks)


def _hitbox_chunk(hitboxes, bmin, bmax) -> msh.HitboxChunk:
    if hitboxes is True:
        return msh.HitboxChunk([default_hitbox(bmin, bmax)])
    if not hitboxes:
        return msh.HitboxChunk([])
    if isinstance(hitboxes, msh.HitboxChunk):
        return msh.HitboxChunk(list(hitboxes.hitboxes))
    return msh.HitboxChunk(list(hitboxes))


def _split_sections(t_sorted, mid_sorted, gbi, w, nmat, max_palette):
    """マテリアルごとに三角形をパレットサイズ上限で区切り、
    (material, start, count, palette) のリストを返す。パレットはグローバルボーン番号。"""
    out = []
    start = 0
    ntri = len(t_sorted)
    while start < ntri:
        m = mid_sorted[start]
        palette: list[int] = []
        pset: set[int] = set()
        end = start
        while end < ntri and mid_sorted[end] == m:
            tri = t_sorted[end]
            need = set()
            for v in tri:
                for k in range(4):
                    if w[v, k] > 0:
                        need.add(int(gbi[v, k]))
            new = need - pset
            if len(palette) + len(new) > max_palette:
                if end == start:
                    raise ValueError(f"triangle {end} alone needs {len(need)} bones > max_palette {max_palette}")
                break
            for b in sorted(new):
                palette.append(b)
                pset.add(b)
            end += 1
        out.append((int(m), start, end - start, palette))
        start = end
    return out


def build_skinned_mesh(positions, normals, uvs, triangles, bone_indices, bone_weights,
                       bones: msh.BoneChunk | Sequence[msh.Bone],
                       materials: Sequence[MaterialSpec | msh.Material], material_ids=None, *,
                       tangents=None, binormals=None, source_path: str | None = None,
                       hitboxes: msh.HitboxChunk | Sequence[msh.Hitbox] | None | bool = None,
                       attach_text: str | None = None, max_palette: int = MAX_PALETTE_DEFAULT,
                       winding: str = "cw") -> msh.MshFile:
    """スキンメッシュ（レイアウト 0,4,1,6,5,2,3 / ストライド 76）を作る。

    :param bone_indices: (n, 4) **ボーン表のグローバル番号**。セクションごとのパレット番号には
        自動で変換する
    :param bone_weights: (n, 4) 合計 1 に正規化する。ウェイト 0 のスロットは無視される。負・NaN・inf の値、
        合計が 0 の頂点、合計が float32 であふれる頂点は ``ValueError``。
        スロットは頂点ごとにウェイトの降順へ並べ替えて書き出す（スロット 0 = 最大。法線・接線はゲームの
        シェーダーがスロット 0 のボーンだけで変形するため）
    :param bones: 流用する既存メッシュのボーン表（``msh.read(...).bones``）など
    :param hitboxes: ``None`` = 空、``True`` = AABB から自動、またはチャンク/列
    :param max_palette: 1 セクションが参照できるボーン数の上限（既定 27 = 同梱メッシュの最大値）。
        :data:`msh.MAX_PALETTE`（シェーダーの boneMatrix[27]）を超える値は ``ValueError``
    :param winding: 入力三角形の巻き順（``build_static_mesh`` と同じ）
    """
    if not 1 <= max_palette <= msh.MAX_PALETTE:
        raise ValueError(f"max_palette must be 1..{msh.MAX_PALETTE} (shader boneMatrix[{msh.MAX_PALETTE}]), "
                         f"got {max_palette}")
    p, nr, uv, t = _check_inputs(positions, normals, uvs, triangles, winding)
    n = len(p)
    bone_list = list(bones.bones) if isinstance(bones, msh.BoneChunk) else list(bones)
    nb = len(bone_list)
    if nb == 0:
        raise ValueError("skinned mesh needs at least one bone")
    gbi = np.ascontiguousarray(bone_indices, dtype=np.int64)
    w = np.ascontiguousarray(bone_weights, dtype=np.float32)
    if gbi.shape != (n, 4) or w.shape != (n, 4):
        raise ValueError(f"bone_indices and bone_weights must be ({n},4)")
    # NaN は大小の比較をすべてすり抜け、inf は正規化で 0・NaN になるので、比較の前に拒否する
    _check_finite("bone_weights", w)
    if (w < 0).any():
        raise ValueError("negative bone weight")
    s = w.sum(axis=1)
    if (s <= 0).any():
        raise ValueError("vertex with zero total weight")
    # 有限のウェイトでも合計が float32 であふれる（3e38 + 3e38 など）と、正規化で全スロットが 0 の頂点になる。
    # 正規化は今の float32 のまま（float64 にすると正しい入力でも出力のバイトが変わるため）
    if not np.isfinite(s).all():
        raise ValueError("total bone weight overflows float32 "
                         f"in {int((~np.isfinite(s)).sum())} vertices (first: {int(np.argmax(~np.isfinite(s)))})")
    w = (w / s[:, None]).astype(np.float32)
    # 影響スロットを頂点ごとにウェイトの降順へ並べ替える（同じウェイトは元の順のまま）。ゲームのスキンの頂点
    # シェーダー（standardskinned / standardglowskinned）は、位置は 4 スロットを混ぜるが、法線・接線・従法線は
    # スロット 0 のボーンだけで変形する。公式メッシュもウェイトが NaN の頂点を除けば全頂点で降順。
    # ウェイト 0 のスロットは後ろに回る（パレット番号 255 の扱いは変わらない）。
    order = np.argsort(-w, axis=1, kind="stable")
    w = np.take_along_axis(w, order, axis=1)
    gbi = np.take_along_axis(gbi, order, axis=1)
    active = w > 0
    if (gbi[active] < 0).any() or (gbi[active] >= nb).any():
        raise ValueError("bone index out of range")
    mats = [m.to_material(True) if isinstance(m, MaterialSpec) else m for m in materials]
    if not mats:
        raise ValueError("at least one material is required")
    mid = _material_ids(material_ids, len(t), len(mats))
    tangents, binormals = _tangent_frame(p, nr, uv, t, tangents, binormals)

    # マテリアル順、その中では三角形の主ボーン（最大ウェイト）順に並べ替える。
    # 近いボーンの三角形がまとまり、パレット分割で生じるセクション数が減る。
    dominant = gbi[np.arange(n), np.argmax(w, axis=1)]
    tri_bone = np.min(dominant[t], axis=1)
    order = np.lexsort((tri_bone, mid))
    t_sorted = t[order]
    mid_sorted = mid[order]
    specs = _split_sections(t_sorted, mid_sorted, gbi, w, len(mats), max_palette)

    # 頂点が複数セクションから参照され、パレット番号が食い違う場合は頂点を複製する
    local = np.zeros((n, 4), dtype=np.uint8)
    assigned = np.full(n, -1, dtype=np.int64)          # 頂点がどのセクションで番号付けされたか
    extra_p, extra_n, extra_uv, extra_t, extra_b, extra_w, extra_l = [], [], [], [], [], [], []
    remap_cache: dict[tuple[int, int], int] = {}
    t_out = t_sorted.copy()
    next_index = n
    for si, (m, start, cnt, palette) in enumerate(specs):
        pal_index = {b: i for i, b in enumerate(palette)}
        for ti in range(start, start + cnt):
            for k in range(3):
                v = int(t_out[ti, k])
                if v >= n:
                    continue
                loc = np.full(4, 255, dtype=np.uint8)   # 未使用スロットは同梱メッシュに倣い 255
                for j in range(4):
                    if w[v, j] > 0:
                        loc[j] = pal_index[int(gbi[v, j])]
                if assigned[v] == -1:
                    assigned[v] = si
                    local[v] = loc
                elif assigned[v] != si and not np.array_equal(local[v], loc):
                    key = (v, si)
                    if key not in remap_cache:
                        remap_cache[key] = next_index
                        extra_p.append(p[v]); extra_n.append(nr[v]); extra_uv.append(uv[v])
                        extra_t.append(tangents[v]); extra_b.append(binormals[v]); extra_w.append(w[v]); extra_l.append(loc)
                        next_index += 1
                    t_out[ti, k] = remap_cache[key]
    if next_index > 65535:
        raise ValueError(f"vertex count after section split {next_index} exceeds 65535")
    if extra_p:
        p = np.vstack([p, np.array(extra_p, dtype=np.float32)])
        nr = np.vstack([nr, np.array(extra_n, dtype=np.float32)])
        uv = np.vstack([uv, np.array(extra_uv, dtype=np.float32)])
        tangents = np.vstack([tangents, np.array(extra_t, dtype=np.float32)])
        binormals = np.vstack([binormals, np.array(extra_b, dtype=np.float32)])
        w = np.vstack([w, np.array(extra_w, dtype=np.float32)])
        local = np.vstack([local, np.array(extra_l, dtype=np.uint8)])
    verts = msh.VertexChunk.from_arrays(msh.SKINNED_LAYOUT, position=p, uv=uv, normal=nr,
                                        bone_indices=local, weights=w, tangent=tangents, binormal=binormals)
    sections = []
    for (m, start, cnt, palette) in specs:
        c, e = section_box(p, t_out[start:start + cnt])
        sections.append(msh.Section(m, start, cnt, c, e, list(palette)))
    tri = msh.TriangleChunk(t_out.astype("<u2"), sections)
    bmin, bmax = compute_bbox(p)
    chunks = _common_chunks(source_path, bmin, bmax)
    chunks += [verts, tri, msh.BoneChunk([_copy_bone(b) for b in bone_list]),
               _hitbox_chunk(hitboxes, bmin, bmax), msh.MaterialChunk(mats)]
    if attach_text:
        chunks.append(msh.AttachChunk(attach_text))
    return msh.MshFile(chunks)


def _copy_bone(b: msh.Bone) -> msh.Bone:
    return msh.Bone(b.name, b.first_child, b.child_count, np.array(b.matrix, dtype=np.float32))


# ======================================================================
# テスト用プリミティブ
# ======================================================================
def make_box(size=(1.0, 1.0, 1.0), center=(0.0, 0.0, 0.0)):
    """各面が独立した頂点を持つ直方体（24 頂点、12 三角形）。

    戻り値 ``(positions, normals, uvs, triangles)``。面ごとに UV は (0,0)-(1,1)。
    三角形の巻き順はゲーム同梱メッシュと同じ **外から見て時計回り**
    （``cross(p1-p0, p2-p0)`` が法線と逆向き）。
    """
    sx, sy, sz = (float(s) * 0.5 for s in size)
    cx, cy, cz = (float(c) for c in center)
    faces = [
        # (normal, u axis, v axis)
        ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
        ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
        ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
        ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
        ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
    ]
    half = np.array([sx, sy, sz])
    P, N, UV, T = [], [], [], []
    for n, u, v in faces:
        n = np.array(n, dtype=np.float64)
        u = np.array(u, dtype=np.float64)
        v = np.array(v, dtype=np.float64)
        base = len(P)
        for (a, b) in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
            P.append((n + a * u + b * v) * half + np.array([cx, cy, cz]))
            N.append(n)
            UV.append(((a + 1) * 0.5, 1.0 - (b + 1) * 0.5))   # v は上が 0
        T.append((base, base + 2, base + 1))
        T.append((base, base + 3, base + 2))
    return (np.array(P, dtype=np.float32), np.array(N, dtype=np.float32),
            np.array(UV, dtype=np.float32), np.array(T, dtype=np.int64))
