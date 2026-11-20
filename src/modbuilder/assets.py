""".msh / .tex の検査と、メッシュ内蔵マテリアルのテクスチャパスの付け替え。

検査の内容は試作MOD 2・3 の check_refs.py（9・10 項）と同じ:

* .msh: gdmesh の厳密モードで読めて書き戻しがバイト一致、``validate()`` が問題なし、本ツールが書くチャンクだけ、
  頂点の値が有限、頂点数 ≤ 65,535、
  マテリアルのテクスチャが実在、ボーン名がプレイヤー骨格（男性欄 = hero_00.msh / 女性欄 = heroine_00.msh）に含まれる
* .tex: TEX + DDSR ヘッダー、大きさが 2 の累乗、DXT1/3/5、ミップ列の長さが一致
"""
from __future__ import annotations

import struct

from ._i18n import tr
from .arz import norm_path

__all__ = ["TEXTURE_PARAMS", "read_tex_info", "check_tex", "check_msh", "material_textures", "retarget_textures",
           "MAX_VERTICES", "TARGET_VERTICES"]

TEXTURE_PARAMS = ("baseTexture", "bumpTexture", "specTexture", "glowTexture")
MAX_VERTICES = 65_535        # D-009 絶対上限（16 ビットの頂点番号）
TARGET_VERTICES = 25_000     # D-009 目標（公式装備の最大級）。超えたら警告
# gdmesh.build が書くチャンク（0 元のパス・13・10・4・5・6・8・7・3 アタッチポイント）。check_msh はこれ以外を拒否する
_TOOL_CHUNKS = frozenset({0, 13, 10, 4, 5, 6, 8, 7, 3})


def read_tex_info(data: bytes) -> dict:
    """.tex のヘッダー（gdconvert.texture.read_tex と同じ読み方）。"""
    if data[:3] != b"TEX":
        raise ValueError(tr("TEX ヘッダーではない", "not a TEX header"))
    _zero, size = struct.unpack_from("<II", data, 4)
    dds = data[12:]
    if dds[:4] != b"DDSR" or size != len(dds):
        raise ValueError(tr("DDSR ヘッダー/長さが不正", "invalid DDSR header or length"))
    h, w = struct.unpack_from("<II", dds, 12)
    mips = struct.unpack_from("<I", dds, 28)[0]
    return {"version": data[3], "width": w, "height": h, "mips": mips, "fourcc": bytes(dds[84:88]),
            "payload": len(dds) - 128, "size": len(data)}


def _mip_bytes(w: int, h: int, mips: int, block: int) -> int:
    return sum(max(1, (max(1, w >> i) + 3) // 4) * max(1, (max(1, h >> i) + 3) // 4) * block for i in range(mips))


def check_tex(key: str, data: bytes, errors: list[str], info: list[str]) -> dict | None:
    try:
        t = read_tex_info(data)
    except (ValueError, struct.error) as e:
        errors.append(tr(f"{key}: .tex として読めません: {e}", f"{key}: cannot be read as a .tex file: {e}"))
        return None
    w, h = t["width"], t["height"]
    if w <= 0 or h <= 0 or w & (w - 1) or h & (h - 1):
        errors.append(tr(f"{key}: テクスチャの大きさが 2 の累乗ではありません（{w}x{h}）",
                         f"{key}: the texture size is not a power of 2 ({w}x{h})"))
    block = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16}.get(t["fourcc"])
    # ミップ数はヘッダーの u32 をそのまま使うので、範囲（1〜最大の辺のビット数。1024 なら 11）を先に確かめる。細工した
    # .tex（ミップ数 0xFFFFFFFF）で _mip_bytes の繰り返しが数十分止まらないようにする（2026-10-20 レビュー R05B-02）
    max_mips = max(w, h).bit_length() if w > 0 and h > 0 else 1
    if block is None:
        errors.append(tr(f"{key}: 圧縮形式が DXT1/3/5 ではありません（{t['fourcc']!r}）",
                         f"{key}: the compression format is not DXT1/3/5 ({t['fourcc']!r})"))
    elif not 1 <= t["mips"] <= max_mips:
        errors.append(tr(f"{key}: ミップ数 {t['mips']} が不正です（1〜{max_mips}）",
                         f"{key}: invalid mip count {t['mips']} (1 to {max_mips})"))
    else:
        expected = _mip_bytes(w, h, t["mips"], block)
        if expected != t["payload"]:
            errors.append(tr(f"{key}: ミップ列の長さ {t['payload']} が期待値 {expected} と一致しません",
                             f"{key}: the mip chain length {t['payload']} does not match the expected value {expected}"))
    info.append(f"{key}: TEX v{t['version']} {w}x{h} {t['fourcc'].decode('ascii', 'replace')} mips={t['mips']} {t['size']} B")
    return t


def material_textures(m) -> list[tuple[int, str, str]]:
    """[(マテリアル番号, パラメーター名, パス)]。"""
    out = []
    for mi, mat in enumerate(m.materials.materials if m.materials else []):
        for pname in TEXTURE_PARAMS:
            v = mat.get(pname)
            if v:
                out.append((mi, pname, str(v)))
    return out


def check_msh(key: str, data: bytes, genders: set[str], player_bones: dict[str, set[str]], texture_exists,
              errors: list[str], warnings: list[str], info: list[str]) -> dict | None:
    """.msh の検査。genders: このメッシュを参照する欄の性別（'m' 男性欄 / 'f' 女性欄）。"""
    from gdmesh import msh
    import numpy as np
    try:
        m = msh.read_bytes(data, strict=True)
    except Exception as e:  # noqa: BLE001  gdmesh の例外は種類が多い
        errors.append(tr(f"{key}: .msh として読めません: {e}", f"{key}: cannot be read as a .msh file: {e}"))
        return None
    if msh.to_bytes(m) != data:
        errors.append(tr(f"{key}: 読み込み→書き出しがバイト一致しません",
                         f"{key}: reading and writing it back does not give the same bytes"))
    try:
        problems = m.validate()
    except Exception as e:  # noqa: BLE001  想定外の形でも検査の結果として返す（例外で止めない）
        problems = [tr(f"validate() で例外: {type(e).__name__}: {e}", f"exception in validate(): {type(e).__name__}: {e}")]
    for p in problems:
        errors.append(f"{key}: validate: {p}")
    # 本ツール（gdmesh.build）が書かないチャンク。validate() は公式ファイルにある 2・9・11・12 も通すので、ここで
    # 拒否する（受け取った装備・MOD の .msh に検査されない中身を持ち込ませない。点検 R01B-03）
    unknown = sorted({c.id for c in m.chunks} - _TOOL_CHUNKS)
    if unknown:
        errors.append(tr(f"{key}: このアプリが作らないチャンクがあります: {unknown}",
                         f"{key}: it has chunks that this app does not write: {unknown}"))
    v = m.vertices
    nv = v.count if v is not None else 0
    # 頂点の値の有限性。validate() は公式メッシュ（NaN の法線・ウェイトを持つ扉など）を通すため、NaN の UV・法線・
    # ウェイトを報告しない。未知のレイアウト（v.data が None）は validate が報告済みなので飛ばす（点検 R01A-02）
    if nv and v.data is not None:
        for arr, what in ((v.positions, tr("位置", "positions")), (v.normals, tr("法線", "normals")),
                          (v.uvs, tr("UV", "UVs")), (v.tangents, tr("接線", "tangents")),
                          (v.binormals, tr("従法線", "binormals")), (v.weights, tr("ウェイト", "weights"))):
            if arr is None:
                continue
            bad = int((~np.isfinite(arr.reshape(nv, -1))).any(axis=1).sum())
            if bad:
                errors.append(tr(f"{key}: 頂点の{what}に NaN/inf の値があります（{bad} 頂点）",
                                 f"{key}: the vertex {what} contain NaN or inf (vertices: {bad})"))
    if nv > MAX_VERTICES:
        errors.append(tr(f"{key}: 頂点数 {nv} が上限 {MAX_VERTICES} を超えています",
                         f"{key}: the vertex count {nv} exceeds the limit {MAX_VERTICES}"))
    elif nv > TARGET_VERTICES:
        warnings.append(tr(f"{key}: 頂点数 {nv} が目標 {TARGET_VERTICES} を超えています（表示はできる見込み）",
                           f"{key}: the vertex count {nv} exceeds the target {TARGET_VERTICES} (it is expected "
                           "to display)"))
    textures = []
    for mi, pname, path in material_textures(m):
        textures.append(norm_path(path))
        if not texture_exists(path):
            errors.append(tr(f"{key}: マテリアル {mi} の {pname} が見つかりません: {path}",
                             f"{key}: {pname} of material {mi} was not found: {path}"))
    names = [b.name for b in m.bones.bones] if m.bones else []
    for g in sorted(genders):
        if not names:
            continue
        sex = tr("男性", "male") if g == "m" else tr("女性", "female")
        if g not in player_bones:
            errors.append(tr(f"{key}: {sex}のプレイヤー骨格が読めないため、ボーン名を照合できません",
                             f"{key}: the bone names cannot be checked because the {sex} player skeleton cannot be read"))
            continue
        extra = [n for n in names if n not in player_bones[g]]
        if extra:
            errors.append(tr(f"{key}: {sex}骨格に無いボーン: {extra}", f"{key}: bones not in the {sex} skeleton: {extra}")
                          + (tr("（女性欄に男性用メッシュを入れていないか）", " (is a male mesh in the female mesh field?)")
                             if g == "f" else ""))
    if v is not None and v.is_skinned and not names:
        errors.append(tr(f"{key}: スキンメッシュなのにボーン表がありません", f"{key}: the mesh is skinned but has no bone table"))
    tri = m.triangles.count if m.triangles else 0
    # 未知の頂点レイアウト（validate が報告済み）では位置を読めない
    ys = (float(v.positions[:, 1].min()), float(v.positions[:, 1].max())) if nv and v.positions is not None else (0.0, 0.0)
    nmat = len(m.materials.materials) if m.materials else 0
    info.append(tr(f"{key}: 頂点 {nv}, 三角形 {tri}, ボーン {len(names)}, マテリアル {nmat}, "
                   f"y {ys[0]:.3f}〜{ys[1]:.3f}, テクスチャ {textures}",
                   f"{key}: vertices {nv}, triangles {tri}, bones {len(names)}, materials {nmat}, "
                   f"y {ys[0]:.3f} to {ys[1]:.3f}, textures {textures}"))
    return {"vertices": nv, "triangles": tri, "bones": len(names), "textures": textures}


def retarget_textures(data: bytes, mapping: dict[str, str]) -> tuple[bytes, int]:
    """マテリアルのテクスチャパスを付け替える。mapping: 旧パス（小文字・'/'）→ 新パス。戻り値 (新しい中身, 変更数)。
    付け替え後に厳密モードで読み戻してバイト一致を確かめる。"""
    from gdmesh import msh
    m = msh.read_bytes(data, strict=True)
    n = 0
    for mat in (m.materials.materials if m.materials else []):
        for p in mat.params:
            if p.type == msh.PARAM_STRING and p.name in TEXTURE_PARAMS:
                new = mapping.get(norm_path(str(p.value)))
                if new is not None and new != p.value:
                    p.value = new
                    n += 1
    out = msh.to_bytes(m)
    if msh.to_bytes(msh.read_bytes(out, strict=True)) != out:
        raise RuntimeError(tr("テクスチャパスの付け替え後の .msh が読み戻しで一致しません",
                              "The .msh with the retargeted texture paths does not match when read back"))
    return out, n
