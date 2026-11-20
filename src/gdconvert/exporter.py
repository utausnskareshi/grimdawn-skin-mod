"""GrimDawnExporter: スキンメッシュ .msh の組み立て・書き出し・検証。"""
from __future__ import annotations

import os

import numpy as np

from gdmesh import build, msh

from ._i18n import tr
from .refs import GenderRefs

__all__ = ["material_for", "build_mesh", "write_and_verify"]


def material_for(refs: GenderRefs, base_texture: str, bump_texture: str) -> build.MaterialSpec:
    """standardskinned のマテリアル。鏡面反射などの値は template（公式メッシュ）の最初のマテリアルから写す。"""
    tm = refs.template.materials.materials[0]
    return build.MaterialSpec(
        base_texture=base_texture,
        bump_texture=bump_texture,
        spec_texture="",
        shader=build.SKINNED_SHADER,
        specular_color=tuple(float(v) for v in tm.get("specularColor", (1.0, 1.0, 1.0))),
        specular_power=float(tm.get("specularPower", 10.0)),
        outline_thickness=float(tm.get("outlineThickness", 1.0)),
    )


def build_mesh(refs: GenderRefs, positions, normals, uvs, faces_ccw, bone_idx, weights,
               material: build.MaterialSpec) -> msh.MshFile:
    """ボーン表とヒットボックスは template から写す（名前と並びをそのまま使う）。"""
    m = build.build_skinned_mesh(
        np.asarray(positions, dtype=np.float32), np.asarray(normals, dtype=np.float32),
        np.asarray(uvs, dtype=np.float32), np.asarray(faces_ccw, dtype=np.int64),
        np.asarray(bone_idx, dtype=np.int64), np.asarray(weights, dtype=np.float32),
        refs.template.bones, [material], hitboxes=refs.template.hitboxes, winding="ccw")
    return m


def set_base_texture(src: str, dst: str, base_texture: str) -> dict:
    """変換済み .msh のマテリアルの ``baseTexture`` だけを書き換える（再変換せずに Items.arc 内の置き場所を変える用）。

    それ以外のチャンクは読み込んだまま書き戻す（gdmesh の往復はバイト一致）。全マテリアルの baseTexture を同じ値にする。
    """
    m = msh.read(src, strict=True)
    for mat in m.materials.materials:
        mat.set("baseTexture", msh.PARAM_STRING, base_texture)
    return write_and_verify(m, dst)


def _check_finite(m: msh.MshFile, path: str) -> None:
    """位置・法線・ウェイト・外接箱・セクションの中心と半径が（書き出す float32 で）有限かを確かめる。validate() は
    外接箱・中心・半径が inf 同士で一致すると通すので、ここで止める（点検 R03B-03）。"""
    v, bb, t = m.vertices, m.bbox, m.triangles
    arrs = [v.positions, v.normals, v.weights] if v is not None else []
    if bb is not None:
        arrs += [bb.min, bb.max]
    for s in (t.sections if t is not None else []):
        arrs += [s.center, s.extent]
    with np.errstate(over="ignore", invalid="ignore"):
        ok = all(a is None or np.isfinite(np.asarray(a, dtype=np.float32)).all() for a in arrs)
    if not ok:
        raise RuntimeError(tr(f".msh の座標・ウェイトに非有限の値がある（入力に体から大きく離れた頂点がある可能性）: {path}",
                              f"The .msh has non-finite coordinates or weights (the input may have vertices far "
                              f"away from the body): {path}"))


def write_and_verify(m: msh.MshFile, path: str, player_bones: set[str] | None = None) -> dict:
    """書き出して読み戻し（バイト一致）・validate()・ボーン名の照合を行う。問題があれば例外。

    同じフォルダーの一時ファイルに書いて検証し、全部通ったら置き換える（retex の上書きで書き込みや検証に失敗しても
    元の .msh が残り、検証に通らない .msh が出力先に残らない）。"""
    _check_finite(m, path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        msh.write(m, tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        again = msh.read_bytes(data, strict=True)
        if msh.to_bytes(again) != data:
            raise RuntimeError(tr(f".msh の読み戻しがバイト一致しない: {path}",
                                  f"The .msh read back does not match byte for byte: {path}"))
        problems = again.validate()
        if problems:
            raise RuntimeError(tr(f".msh の validate() で問題: {path} {problems}",
                                  f".msh validate() found problems: {path} {problems}"))
        names = [b.name for b in again.bones.bones]
        if player_bones is not None:
            extra = sorted(set(names) - player_bones)
            if extra:
                raise RuntimeError(tr(f"プレイヤー骨格に無いボーン: {extra}", f"Bones that are not in the player skeleton: {extra}"))
        v = again.vertices
        w = v.weights
        if not np.allclose(w.sum(axis=1), 1.0, atol=1e-4):
            raise RuntimeError(tr("ウェイトの合計が 1 でない頂点がある", "Some vertices have weights that do not sum to 1"))
        if v.count > 65535:
            raise RuntimeError(tr(f"頂点数 {v.count} > 65535", f"The vertex count {v.count} exceeds 65535"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {
        "path": os.path.abspath(path), "bytes": len(data), "vertices": int(v.count), "triangles": int(again.triangles.count),
        "sections": [{"material": s.material, "triangles": s.count, "palette": len(s.bones)} for s in again.triangles.sections],
        "bones": len(names), "bbox_min": np.round(v.positions.min(axis=0), 4).tolist(),
        "bbox_max": np.round(v.positions.max(axis=0), 4).tolist(), "validate": "ok", "roundtrip": "byte-identical",
        "max_influences": int((w > 0).sum(axis=1).max()),
    }
