"""コマンドライン: ``python -m gdconvert <コマンド> ...``（``src`` を PYTHONPATH に入れて実行）。

  refs     ゲームから基準メッシュを取り出す（ゲームの .arc を読み取り専用で直接読む。ArchiveTool は使わない）
  convert  GLB → 男性用・女性用の .msh と .tex（確認画像と報告 JSON も）
  retex    変換済み .msh のテクスチャのパス（Items.arc 内の置き場所）だけを書き換える
  inspect  基準ボディのバインド姿勢・骨の位置・ウェイトと、GLB の寸法を表示する（調査用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

from .config import AxisConfig, ConvertConfig
from .refs import DEFAULT_GD, prepare_refs

__all__ = ["main"]


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def cmd_refs(a) -> int:
    from .pipeline import default_refs_dir
    refs = a.refs or default_refs_dir()
    done = prepare_refs(refs, a.gd)
    print(f"基準メッシュ: {refs}（取り出し {len(done)} 件）")
    return 0


def cmd_convert(a) -> int:
    from .pipeline import check_names, convert
    try:
        cfg = ConvertConfig.load(a.config) if a.config else ConvertConfig()
        if a.source_up or a.source_front or a.front or a.yaw is not None or a.mirror_x:
            ax = cfg.axes
            cfg.axes = AxisConfig(a.source_up or ax.source_up, a.source_front or ax.source_front,
                                  a.front or ax.target_front, ax.yaw_deg if a.yaw is None else a.yaw,
                                  a.mirror_x or ax.mirror_x)
        if a.fit:
            cfg.fit = a.fit
        if a.ground_snap:
            cfg.ground_snap = a.ground_snap
        # 0 を「指定なし」として黙って既定値にしない（範囲は cfg.validate() で確かめる）
        if a.target_vertices is not None:
            cfg.target_vertices = a.target_vertices
        if a.texture_max is not None:
            cfg.texture_max_size = a.texture_max
        if a.namespace:
            cfg.namespace = a.namespace
        if a.genders is not None:
            cfg.genders = tuple(x.strip().lower() for x in a.genders.split(",") if x.strip())
        cfg.validate()
        check_names(a.name, cfg.namespace)      # convert の中でも確かめるが、ここで止めて終了コード 2 にする
    # 引数・設定 JSON の誤りは重い処理の前に、トレースバックなしで止める（終了コード 2）。存在しない・読めない
    # --config（OSError）も同じ（点検 R02A-07・X3X-02）
    except (ValueError, OSError) as e:
        print(f"gdconvert convert: エラー: {e}", file=sys.stderr)
        return 2
    gd = None if a.no_tex else a.gd
    if a.prepare_refs:
        from .pipeline import default_refs_dir
        prepare_refs(a.refs or default_refs_dir(), a.gd)
    rep = convert(a.input, a.out, a.name, a.refs, gd, cfg, preview=not a.no_preview, make_tex=not a.no_tex,
                  export_glb=not a.no_glb)
    print(json.dumps({g: rep["steps"][f"gender_{g}"]["msh"] for g in cfg.genders}, ensure_ascii=False, indent=1))
    return 0


def cmd_retex(a) -> int:
    from .exporter import set_base_texture
    # .msh のパスは latin-1 で書く。空や ASCII 以外はトレースバックや空のパスの .msh にせず止める（点検 R02A-07）
    if not a.base_texture or not a.base_texture.isascii():
        print(f"gdconvert retex: エラー: --base-texture は ASCII の空でないパス: {a.base_texture!r}", file=sys.stderr)
        return 2
    info = set_base_texture(a.input, a.output, a.base_texture)
    print(json.dumps({"path": info["path"], "base_texture": a.base_texture, "validate": info["validate"]}, ensure_ascii=False))
    return 0


def cmd_inspect(a) -> int:
    from . import gltf
    from .refs import load_refs, ref_path
    from .pipeline import default_refs_dir
    out = {}
    if a.glb:
        for p in a.glb:
            m = gltf.load(p)
            P = m.positions
            out[os.path.basename(p)] = {
                "vertices": m.vertex_count, "faces": m.face_count, "bbox_min": np.round(P.min(0), 4).tolist(),
                "bbox_max": np.round(P.max(0), 4).tolist(), "height": round(float(P[:, 1].max() - P[:, 1].min()), 4),
                "textures": [list(x.image.shape) if x.has_texture else None for x in m.materials],
                "warnings": m.warnings,
            }
    refs_dir = a.refs or default_refs_dir()
    if os.path.isdir(refs_dir):
        refs = load_refs(refs_dir)
        for g, r in refs.items():
            sk = r.skeleton
            j = {n: np.round(sk.joints[i], 3).tolist() for i, n in enumerate(sk.names) if n.startswith("Bip01")}
            def ang(a_, b_):
                d = sk.joints[sk.index(b_)] - sk.joints[sk.index(a_)]
                return round(float(np.degrees(np.arctan2(abs(d[0]), -d[1]))), 1)
            out[f"skeleton_{g}"] = {
                "file": r.template_path, "bones": len(sk), "body_height": round(r.body.height, 4),
                "body_bbox_min": np.round(r.body.positions.min(0), 3).tolist(),
                "body_bbox_max": np.round(r.body.positions.max(0), 3).tolist(),
                "upper_arm_from_vertical_deg": {s: ang(f"Bip01 {s} UpperArm", f"Bip01 {s} Forearm") for s in "LR"},
                "forearm_from_vertical_deg": {s: ang(f"Bip01 {s} Forearm", f"Bip01 {s} Hand") for s in "LR"},
                "thigh_from_vertical_deg": {s: ang(f"Bip01 {s} Thigh", f"Bip01 {s} Calf") for s in "LR"},
                "joints": j,
            }
        out["refs_dir"] = refs_dir
        out["body_m_file"] = ref_path(refs_dir, "body_m")
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


def main(argv=None) -> int:
    _utf8()
    ap = argparse.ArgumentParser(prog="gdconvert", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refs", help="ゲームから基準メッシュを取り出す（.arc を読み取り専用で直接読む）")
    p.add_argument("--refs", help="保存先（既定: GDCONVERT_REFS → GDSKIN_HOME/refs → "
                                  "%%LOCALAPPDATA%%/GrimDawnSkinMOD/refs）")
    p.add_argument("--gd", default=DEFAULT_GD, help="Grim Dawn のインストール先（resources/Creatures.arc・Items.arc を読む）")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("convert", help="GLB → .msh / .tex")
    p.add_argument("input", help="入力 .glb / .gltf")
    p.add_argument("--out", required=True, help="出力フォルダー")
    p.add_argument("--name", required=True,
                   help="出力名（ASCII の小文字・数字・_ . - だけ。先頭は小文字・数字・_）。"
                        "<name>_m.msh, <name>_f.msh, <name>_dif.tex")
    p.add_argument("--refs", help="基準メッシュのフォルダー（refs コマンドの保存先）")
    p.add_argument("--prepare-refs", action="store_true", help="基準メッシュが無ければゲームから取り出す")
    p.add_argument("--gd", default=DEFAULT_GD, help="Grim Dawn のインストール先（TextureCompiler.exe を使う）")
    p.add_argument("--config", help="設定 JSON（ConvertConfig のキー）")
    p.add_argument("--source-up", help="入力の上方向（既定 +y）")
    p.add_argument("--source-front", help="入力の正面方向（既定 +z。TRELLIS の GLB は +z）")
    p.add_argument("--front", help="Grim Dawn の正面方向（既定 +z。試作MOD 2 の確認結果で変える）")
    p.add_argument("--yaw", type=float, help="追加の y 軸回転（度）")
    p.add_argument("--mirror-x", action="store_true", help="左右を反転する")
    p.add_argument("--fit", choices=("pose", "none"), help="姿勢・比率の合わせ込み（既定 pose）")
    p.add_argument("--ground-snap", choices=("legs", "all", "none"),
                   help="足元を y=0 に合わせる方法（既定 legs = 膝から下だけ動かす）")
    p.add_argument("--target-vertices", type=int, help="目標頂点数（既定 25000）")
    p.add_argument("--texture-max", type=int, help="テクスチャの最大辺（既定 2048）")
    p.add_argument("--namespace", help="Items.arc 内のフォルダー名（既定 aiskin3。テクスチャのパスに入る。"
                                       "--name と同じく ASCII の小文字・数字・_ . - だけ）")
    p.add_argument("--genders", help="m,f（既定 両方）")
    p.add_argument("--no-preview", action="store_true")
    p.add_argument("--no-tex", action="store_true", help=".tex を作らない（PNG のみ）")
    p.add_argument("--no-glb", action="store_true", help="確認用のバインド姿勢 GLB を書かない")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("retex", help="変換済み .msh の baseTexture のパスだけを書き換える")
    p.add_argument("input", help="変換済みの .msh")
    p.add_argument("output", help="書き出す .msh（input と同じでもよい）")
    p.add_argument("--base-texture", required=True, help="例: Items/mymod/knight_dif.tex")
    p.set_defaults(func=cmd_retex)

    p = sub.add_parser("inspect", help="基準ボディと GLB の寸法を表示")
    p.add_argument("glb", nargs="*")
    p.add_argument("--refs")
    p.add_argument("--json", help="結果を JSON で保存")
    p.set_defaults(func=cmd_inspect)

    a = ap.parse_args(argv)
    return a.func(a)
