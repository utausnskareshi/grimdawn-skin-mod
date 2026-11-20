"""GLB → Grim Dawn 用の全身スキンメッシュ（男性用・女性用 .msh）とテクスチャ（.tex）。

:func:`convert` が全工程を順に呼ぶ。各工程の結果は ``<name>_report.json`` に記録する。
"""
from __future__ import annotations

import json
import os
import re
import time

import numpy as np

from . import exporter, geometry, gltf, meshproc, quality, render, rigging, texture
from ._i18n import tr
from .config import ConvertConfig
from .refs import GenderRefs, load_refs, player_bone_names
from .skeleton import PoseModel, lbs, test_pose_spec

__all__ = ["convert", "check_names", "default_refs_dir", "TexVerifyError"]

GENDER_LABEL = {"m": "male", "f": "female"}
# 出力名と名前空間の規則（ASCII の小文字・数字・_ . -。先頭は小文字・数字・_）
_NAME_RE = re.compile(r"[a-z0-9_][a-z0-9_.-]*")


class TexVerifyError(RuntimeError):
    """作った .tex の検証（texture.verify_tex）に失敗した。``reason`` は画面の文を選ぶ理由のコード（アプリの
    EXPORT_FAILED の tex_verify。インストール先の確認ではなくテクスチャの大きさの案内にする。点検 R04B-03）。"""

    reason = "tex_verify"


def default_refs_dir() -> str:
    """基準メッシュの既定の場所。modbuilder.paths.refs_dir（アプリのバックエンド）と同じ規則:
    ``GDCONVERT_REFS`` → ``GDSKIN_HOME\\refs`` → ``%LOCALAPPDATA%\\GrimDawnSkinMOD\\refs``（変換の経路は modbuilder を
    読み込まない作りなので、同じ数行を写している。点検 R03A-02）。"""
    env = os.environ.get("GDCONVERT_REFS")
    if env:
        return env
    home = os.environ.get("GDSKIN_HOME")
    if home:
        return os.path.join(os.path.abspath(home), "refs")
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "GrimDawnSkinMOD", "refs")


def _round(a, n=4):
    return np.round(np.asarray(a, dtype=np.float64), n).tolist()


def check_names(name, namespace) -> None:
    """出力名と名前空間を ASCII の規則で確かめる（誤りは ValueError）。

    .msh（latin-1）・.tex のパス（TextureCompiler）・Items.arc のエントリー名に入るので、重い処理の前に確かめる
    （漢字は .msh の書き出しで、é などは TextureCompiler で失敗するため）。:func:`convert` と CLI が呼ぶ。"""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(tr(f"名前は ASCII の小文字・数字・_ . - だけ（先頭は小文字・数字・_）: {name!r}",
                            f"The name may use only ASCII lowercase letters, digits and _ . - (the first character: "
                            f"a lowercase letter, a digit or _): {name!r}"))
    if not isinstance(namespace, str) or not _NAME_RE.fullmatch(namespace):
        raise ValueError(tr(f"名前空間（namespace）は ASCII の小文字・数字・_ . - だけ（先頭は小文字・数字・_）: "
                            f"{namespace!r}",
                            f"The namespace may use only ASCII lowercase letters, digits and _ . - (the first "
                            f"character: a lowercase letter, a digit or _): {namespace!r}"))


def convert(glb_path: str, out_dir: str, name: str, refs_dir: str | None = None, gd: str | None = None,
            cfg: ConvertConfig | None = None, *, preview: bool = True, make_tex: bool = True,
            export_glb: bool = True, log=print) -> dict:
    cfg = cfg or ConvertConfig()
    refs_dir = refs_dir or default_refs_dir()
    check_names(name, cfg.namespace)
    # 性別・列挙値・頂点数の誤りも重い処理の前に止める（性別の誤りはテクスチャの後の KeyError に、height_mode の
    # 綴りの誤りは黙って倍率 1 になっていた）
    cfg.validate()
    # 基準メッシュ・プレイヤー骨格も先に読む（壊れた・足りない基準ファイルで、TextureCompiler と工程 1〜4 の後に止まら
    # ないように。点検 R03A-01）
    refs = load_refs(refs_dir)
    players = player_bone_names(refs_dir)
    os.makedirs(out_dir, exist_ok=True)
    t_all = time.time()
    report: dict = {"input": os.path.abspath(glb_path), "name": name, "config": cfg.to_dict(), "steps": {}}
    timings: dict = {}

    # 各工程の始めに「[n] …を開始」を出す（作業プロセスはこの行で進捗と段階を切り替えるので、時間のかかる工程の
    # 間も、失敗したときも、その工程の段階になる）
    # --- 1. 読み込み ---
    t = time.time()
    log(tr("[1] 読み込みを開始", "[1] Loading"))
    model = gltf.load(glb_path)
    report["steps"]["load"] = {
        "vertices": model.vertex_count, "faces": model.face_count, "materials": len(model.materials),
        "textures": [list(m.image.shape) if m.has_texture else None for m in model.materials],
        "bbox_min": _round(model.positions.min(0)), "bbox_max": _round(model.positions.max(0)),
        "has_uv": model.uvs is not None, "has_color": model.colors is not None, "warnings": list(model.warnings),
        "load_flags": model.load_flags,
    }
    log(tr(f"[1] 読み込み: 頂点 {model.vertex_count}、三角形 {model.face_count}、マテリアル {len(model.materials)}",
           f"[1] Loaded: vertices {model.vertex_count}, triangles {model.face_count}, materials {len(model.materials)}"))
    shown = {msg for _k, msg in model.notices}
    for w in model.warnings:      # 利用者に見せる注意は「注意」付き（作業プロセスで warning になる）。他は記録だけ
        log((tr("    注意: 読み込み: ", "    Warning: load: ") if w in shown else tr("    読み込み: ", "    Load: ")) + w)
    timings["load"] = time.time() - t

    # --- 2. 向き・掃除 ---
    log(tr("[2] 向きの正規化を開始", "[2] Normalizing the orientation"))
    M = cfg.axes.matrix()
    model = meshproc.clean(meshproc.orient(model, M))
    if model.face_count == 0:
        raise gltf.GltfError(tr("有効な三角形がない（退化・面積 0・NaN の面だけ）",
                                "No valid triangles (only degenerate, zero-area or NaN faces)"), "no_triangles")
    if not np.isfinite(model.positions).all():      # 掃除の後に残った頂点の NaN・inf（点検 R03B-03）
        raise gltf.GltfError(tr("頂点の座標に数値でない値（NaN）か無限大（inf）がある",
                                "Some vertex coordinates are not numbers (NaN) or are infinite (inf)"),
                             "nonfinite_positions")
    # 体から極端に離れた頂点（範囲の高さの rigging.OUTLIER_STOP_MARGIN 倍より外）は、壊れた・余計な部品とみなして重い工程の
    # 前に止める（決定 #2 R03B-03）。それより近い外れは工程 5 の合わせ込みから除き、品質の注意 outlier_vertices にする
    n_far, far = rigging.extreme_outliers(model.positions, model.faces)
    if n_far:
        # 切り捨て（文は「〜倍以上」）。上限で切ってから整数にする（inf でも OverflowError にしない。点検 D2R-04）
        dist = int(min(far, rigging.OUTLIER_DISTANCE_CAP))
        raise gltf.GltfError(tr(f"体から極端に離れた頂点が {n_far} 個ある（最も遠い頂点は体の高さの {dist} 倍以上。"
                                f"止める目安は {rigging.OUTLIER_STOP_MARGIN:g} 倍）",
                                f"{n_far} {'vertex is' if n_far == 1 else 'vertices are'} extremely far away from the "
                                f"body (the farthest is at least {dist} times the body height away; the limit is "
                                f"{rigging.OUTLIER_STOP_MARGIN:g} times)"),
                             "outlier_vertices", count=n_far, distance=dist)
    h_in = float(model.positions[:, 1].max() - model.positions[:, 1].min())
    report["steps"]["orient"] = {"matrix": _round(M, 6), "vertices": model.vertex_count, "faces": model.face_count,
                                 "height_input_units": round(h_in, 5)}
    log(tr(f"[2] 向きの正規化: 行列 {np.round(M, 3).tolist()}、高さ {h_in:.4f}（入力単位）",
           f"[2] Orientation normalized: matrix {np.round(M, 3).tolist()}, height {h_in:.4f} (input units)"))

    # --- 3. 頂点数とテクスチャの統一 ---
    t = time.time()
    log(tr("[3] 頂点数の調整を開始", "[3] Adjusting the vertex count"))
    # 溶接の許容の高さは、大きく離れた頂点を除いて測る（決定 D-068。外れの分だけ許容が大きくなり、体の細かい部分の頂点まで
    # まとめないように）。判定の規則は合わせ込み（工程 5 の rigging.prealign）と同じ rigging._core_faces で、工程 2 の後
    # （間引き・UV の作り直しの前）のモデルに当てる。合わせ込みは工程 3 の後のモデルで判定し直すので、間引き・焼き直しの経路
    # では、モデルが変わる分だけ除く頂点（数）が違うことがある。離れた部品の頂点の割合が工程 3 で 0.5% をまたぐと、合わせ込みが
    # 何も除かないこともある（B-035。既存の限界）。外れの無い入力は今までどおり h_in（同じ値）で、報告・ログにも何も足さない。報告の
    # height_input_units は今までどおり外れを含めた高さ
    h_weld = h_in
    core = rigging.core_height(model.positions, model.faces)
    if core is not None:
        h_weld, n_core_out = core
        report["steps"]["orient"]["weld_height_input_units"] = round(h_weld, 5)
        report["steps"]["orient"]["weld_height_excluded_vertices"] = int(n_core_out)
        log(tr(f"    溶接の許容は、大きく離れた頂点 {n_core_out} 個を除いた高さ {h_weld:.4f}（入力単位）で測った",
               f"    The weld tolerance was measured without {n_core_out} far "
               f"{'vertex' if n_core_out == 1 else 'vertices'} (height {h_weld:.4f}, input units)"))
    weld_tol = cfg.weld_tolerance * h_weld
    reduce_kw = dict(weld_tol=weld_tol, bake_size=cfg.bake_size, max_size=cfg.texture_max_size,
                     padding=cfg.padding_px, log=log)
    model0 = model
    model, dec = meshproc.reduce_vertices(model0, cfg.target_vertices, cfg.max_vertices, **reduce_kw)
    if not dec.get("decimated"):
        model, uni = meshproc.unify_texture(model0, **reduce_kw)
        if uni.get("rebaked") and model.vertex_count > cfg.target_vertices:
            # 焼き直しの UV の継ぎ目で目標を超えた → 間引きの経路（間引き → UV → 焼き直し）に回す
            log(tr(f"    焼き直しの後の頂点 {model.vertex_count} が目標 {cfg.target_vertices} を超えたので、間引く",
                   f"    {model.vertex_count} vertices after rebaking exceed the target {cfg.target_vertices}; "
                   "decimating"))
            model, dec = meshproc.reduce_vertices(model0, cfg.target_vertices, cfg.max_vertices, force=True,
                                                  **reduce_kw)
            uni["over_target"] = True
        dec["unify"] = uni
    if model.vertex_count > cfg.max_vertices:
        raise RuntimeError(tr(f"頂点数 {model.vertex_count} が上限 {cfg.max_vertices} を超えた",
                              f"The vertex count {model.vertex_count} exceeds the limit {cfg.max_vertices}"))
    report["steps"]["vertices"] = dec
    log(tr(f"[3] 頂点数: {model.vertex_count}（目標 {cfg.target_vertices}、上限 {cfg.max_vertices}）",
           f"[3] Vertex count: {model.vertex_count} (target {cfg.target_vertices}, limit {cfg.max_vertices})")
        + (tr("、間引きと UV の作り直しを実施", ", decimated and the UV rebuilt") if dec.get("decimated")
           else tr("、間引きなし", ", no decimation")))
    timings["reduce"] = time.time() - t

    # --- 4. テクスチャ ---
    t = time.time()
    log(tr("[4] テクスチャの作成を開始", "[4] Creating the texture"))
    img = texture.single_texture(model)
    if img is None:
        raise RuntimeError(tr("テクスチャを 1 枚にまとめられなかった", "Could not combine the textures into one"))
    tw, th = texture.choose_size(img.shape[1], img.shape[0], cfg.texture_max_size, cfg.texture_min_size)
    img2 = texture.resize(img, tw, th)
    cov = texture.coverage_mask(model.uvs, model.faces, tw, th, dilate=2)
    cut = texture.cutout_mask(model, img2)        # MASK の切り抜き部分は近くの不透明な色で塗る
    if cut is not None:
        cov &= ~cut
    img2 = texture.pad_charts(img2, cov)
    tex_name = f"{name}_dif"
    png_path = os.path.join(out_dir, tex_name + ".png")
    texture.write_png(png_path, img2)
    tex_game_path = f"Items/{cfg.namespace}/{tex_name}.tex"
    tex_info = {"source_size": [int(img.shape[1]), int(img.shape[0])], "size": [tw, th], "png": png_path,
                "coverage": round(float(cov.mean()), 4), "game_path": tex_game_path}
    if make_tex:
        if gd and os.path.isfile(os.path.join(gd, "TextureCompiler.exe")):
            tex_path = os.path.join(out_dir, tex_name + ".tex")
            texture.compile_tex(gd, png_path, tex_path, cfg.texture_format)
            tex_info["tex"] = tex_path
            if cfg.texture_format.lower() == "dxt1":       # compile_tex と同じく大文字も同じ形式
                tex_info["verify"] = texture.verify_tex(tex_path, img2[..., :3])
                if not tex_info["verify"]["ok"]:
                    raise TexVerifyError(tr(f".tex の検証に失敗: {tex_info['verify']}",
                                            f"Verifying the .tex failed: {tex_info['verify']}"))
                if tex_info["verify"].get("warning"):      # 細かい模様で圧縮誤差が大きい（点検 R04B-01）
                    v = tex_info["verify"]
                    log(tr(f"[4] 注意: {v['warning']}（平均誤差 {v['mean_abs_error']}、4x4 ブロック平均の誤差 "
                           f"{v['block_mean_abs_error']}）",
                           f"[4] Warning: {v['warning']} (mean error {v['mean_abs_error']}, mean error of the 4x4 "
                           f"block averages {v['block_mean_abs_error']})"))
            log(tr(f"[4] テクスチャ: {tw}x{th} → {tex_path}（平均誤差 {tex_info.get('verify', {}).get('mean_abs_error', '-')}）",
                   f"[4] Texture: {tw}x{th} → {tex_path} (mean error "
                   f"{tex_info.get('verify', {}).get('mean_abs_error', '-')})"))
        else:
            tex_info["tex"] = None
            tex_info["note"] = tr("TextureCompiler.exe が無いので .tex を作っていない（--gd を指定）",
                                  "No .tex was made because TextureCompiler.exe was not found (specify --gd)")
            log(tr("[4] テクスチャ: PNG のみ（TextureCompiler.exe が見つからない）",
                   "[4] Texture: PNG only (TextureCompiler.exe was not found)"))
    report["steps"]["texture"] = tex_info
    timings["texture"] = time.time() - t

    # --- 5. 骨格ごと ---
    remap, reps = geometry.weld(model.positions, weld_tol)
    report["steps"]["weld"] = {"vertices": model.vertex_count, "welded": int(len(reps)),
                               "seam_ratio": round(model.vertex_count / max(1, len(reps)), 4)}
    missing = [g for g in cfg.genders if g not in players]
    if missing:
        log(tr(f"    注意: プレイヤー骨格 {missing} が無いので、出力のボーン名の照合を省く（refs コマンドで取り出せる）",
               f"    Warning: the player skeleton {missing} is missing, so the bone names of the output are not checked "
               "(the refs command extracts it)"))
    report["steps"]["player_skeleton_check"] = {g: (g in players) for g in cfg.genders}
    results = {}
    for g in cfg.genders:
        t = time.time()
        r = _convert_gender(model, remap, reps, refs[g], cfg, name, tex_game_path, out_dir, players.get(g),
                            export_glb, img2, log)
        r["seconds"] = round(time.time() - t, 2)
        results[g] = r
        report["steps"][f"gender_{g}"] = r["report"]
    timings["genders"] = {g: results[g]["seconds"] for g in results}

    # --- 6. 確認画像 ---
    if preview:
        t = time.time()
        log(tr("[6] 確認画像を作成", "[6] Creating the check image"))
        sheet_path = os.path.join(out_dir, f"{name}_preview.png")
        _preview(model, img2, results, refs, sheet_path, name)
        report["preview"] = sheet_path
        log(tr(f"[6] 確認画像: {sheet_path}", f"[6] Check image: {sheet_path}"))
        timings["preview"] = time.time() - t
    timings["total"] = time.time() - t_all
    report["timings_seconds"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in timings.items()}
    rp = os.path.join(out_dir, f"{name}_report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    report["report_path"] = rp
    log(tr(f"完了: {rp}（{timings['total']:.1f} 秒）", f"Done: {rp} ({timings['total']:.1f} s)"))
    return report


def _convert_gender(model, remap, reps, refs: GenderRefs, cfg: ConvertConfig, name, tex_game_path, out_dir,
                    player_bones, export_glb, tex_img, log) -> dict:
    g = refs.gender
    body = refs.body
    log(tr(f"[5{g}] {GENDER_LABEL[g]}: 骨格 {refs.template_path}（{len(refs.skeleton)} 本）",
           f"[5{g}] {GENDER_LABEL[g]}: skeleton {refs.template_path} ({len(refs.skeleton)} bones)"))
    scale = cfg.scale if cfg.scale > 0 else (0.0 if cfg.height_mode == "bbox" else 1.0)   # 0 = 高さから自動
    pre = rigging.prealign(model.positions, model.faces, body, scale)
    if pre.outlier_vertices:        # 点検 R03B-03。外れた部品は出力に残る
        log(tr(f"    注意: 体から大きく離れた頂点 {pre.outlier_vertices} 個を、大きさと位置の合わせ込みから除いた（GLB を確認）",
               f"    Warning: {pre.outlier_vertices} {'vertex' if pre.outlier_vertices == 1 else 'vertices'} far away "
               f"from the body {'was' if pre.outlier_vertices == 1 else 'were'} left out of the size and position "
               "fitting (check the GLB)"))
    Pg = pre.apply(model.positions)
    if cfg.fit == "pose":
        fit = rigging.fit_pose(body, Pg, model.faces, samples_body=cfg.fit_samples_body,
                               samples_model=cfg.fit_samples_model, max_nfev=cfg.fit_max_nfev,
                               leg_separation=cfg.fit_leg_separation, seed=cfg.fit_seed, log=log)
    elif cfg.fit == "none":
        fit = rigging.identity_fit(body)
    else:
        raise ValueError(tr(f"fit は pose / none: {cfg.fit}", f"fit must be pose / none: {cfg.fit}"))
    height = body.height
    wt = rigging.transfer_weights(body, fit.transforms, Pg, model.faces, remap, reps,
                                  max_distance=cfg.transfer_max_distance * height,
                                  max_angle_deg=cfg.transfer_max_angle, smooth_iterations=cfg.smooth_iterations,
                                  max_influences=cfg.max_influences, island_min=cfg.island_min_vertices,
                                  island_fraction=cfg.island_min_fraction,
                                  leg_to_pelvis=((cfg.leg_to_pelvis_start * height, cfg.leg_to_pelvis_end * height)
                                                 if cfg.leg_to_pelvis else None),
                                  head_rigid=cfg.head_rigid, limb_gating=cfg.limb_gating, height=height)
    Pb = rigging.unpose(Pg, wt.bone_idx, wt.weights, fit.transforms) if cfg.fit == "pose" else Pg.copy()
    lift, dy = rigging.ground_snap(Pb, wt.bone_idx, wt.weights, refs.skeleton,
                                   mode=cfg.ground_snap if cfg.ground_snap else "none")
    Pb[:, 1] += lift
    normals = geometry.vertex_normals(Pb, model.faces, remap)
    # 変形の度合い（辺の長さの比）
    e0 = geometry.edge_lengths(Pg, model.faces)
    e1 = geometry.edge_lengths(Pb, model.faces)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = (e1 / e0)[e0 > 1e-9]
    mat = exporter.material_for(refs, tex_game_path, cfg.bump_texture)
    m = exporter.build_mesh(refs, Pb, normals, model.uvs, model.faces, wt.bone_idx, wt.weights, mat)
    msh_path = os.path.join(out_dir, f"{name}_{g}.msh")
    info = exporter.write_and_verify(m, msh_path, player_bones)
    log(tr(f"    → {msh_path}: 頂点 {info['vertices']}、三角形 {info['triangles']}、セクション {len(info['sections'])}、"
           f"y {info['bbox_min'][1]:.3f}〜{info['bbox_max'][1]:.3f}",
           f"    → {msh_path}: vertices {info['vertices']}, triangles {info['triangles']}, "
           f"sections {len(info['sections'])}, y {info['bbox_min'][1]:.3f} to {info['bbox_max'][1]:.3f}"))
    if export_glb:
        gltf.save_glb(os.path.join(out_dir, f"{name}_{g}_bind.glb"), Pb, model.faces, model.uvs, tex_img[..., :3], normals)
    edge_ratio = {"p01": round(float(np.percentile(ratio, 1)), 3), "median": round(float(np.median(ratio)), 3),
                  "p99": round(float(np.percentile(ratio, 99)), 3), "max": round(float(ratio.max()), 3),
                  "frac_over_2": round(float((ratio > 2.0).mean()), 5)}
    sk = refs.skeleton
    q = {
        "limb_angles_bind_deg": quality.limb_angles(sk),
        "limb_angles_input_deg": quality.limb_angles(sk, fit.transforms) if cfg.fit == "pose" else None,
        "hand_to_weapon": quality.hand_to_weapon(Pb, wt.bone_idx, wt.weights, sk),
        "hand_to_weapon_body": quality.hand_to_weapon(body.positions, body.bone_idx, body.weights, sk),
        "test_pose_stretch": quality.test_pose_stretch(Pb, model.faces, wt.bone_idx, wt.weights, sk),
        "far_parts": quality.far_part_shares(Pb[reps], wt.bone_idx[reps], wt.weights[reps], body,
                                             cfg.transfer_max_distance * height),
    }
    q["flags"] = quality.flags(fit.rms_after if cfg.fit == "pose" else None, float(Pb[:, 1].min()),
                               q["hand_to_weapon"], q["test_pose_stretch"], q["far_parts"], edge_ratio,
                               wt.info.get("zero_weight_vertices", 0), pre.outlier_vertices)
    if q["flags"]:
        log(tr(f"    注意: {', '.join(q['flags'])}（確認画像で見直す）",
               f"    Warning: {', '.join(q['flags'])} (review the check image)"))
    rep = {
        "skeleton": refs.template_path, "body": body.name,
        "prealign": {"scale": round(pre.scale, 5), "translation": _round(pre.translation), "input_height": round(pre.source_height, 5),
                     "body_height": round(pre.body_height, 4), "outlier_vertices": int(pre.outlier_vertices)},
        "fit": fit.summary(), "transfer": wt.info, "ground_snap_dy": round(dy, 4), "ground_snap_mode": cfg.ground_snap,
        "edge_ratio": edge_ratio,
        "quality": q,
        "msh": info,
    }
    return {"report": rep, "Pg": Pg, "Pb": Pb, "fit": fit, "transfer": wt, "msh_path": msh_path}


def _preview(model, img, results, refs, path, name):
    from PIL import Image
    rows = []
    allp = np.vstack([r["Pb"] for r in results.values()] + [r["Pg"] for r in results.values()])
    rad = float(max(np.abs(allp[:, 0]).max(), np.abs(allp[:, 2]).max(), 0.9)) + 0.05
    top = float(allp[:, 1].max()) + 0.05
    bounds = (-rad, rad, min(-0.1, float(allp[:, 1].min()) - 0.05), top)
    size = (340, 500)
    spec, vals = test_pose_spec()
    blue = (0.25, 0.45, 0.95)
    for g, r in results.items():
        ref = refs[g]
        body = ref.body
        bodyP = body.positions
        posed_body = lbs(body.positions, body.bone_idx, body.weights, r["fit"].transforms)
        res = render.Layer(r["Pb"], model.faces, model.uvs, img)
        ovl = render.Layer(bodyP, body.faces, color=blue, overlay_alpha=0.35, outline=(20, 40, 200))
        inp = render.Layer(r["Pg"], model.faces, model.uvs, img)
        ovl_fit = render.Layer(posed_body, body.faces, color=blue, overlay_alpha=0.35, outline=(20, 40, 200))
        ovl_bind = render.Layer(bodyP, body.faces, outline=(220, 30, 30), fill=False)
        tag = f"{name} {GENDER_LABEL[g]}"
        row1 = [
            render.label(render.render_layers([inp, ovl_bind], render.VIEWS["front(+Z)"], size, bounds),
                         f"{tag}\ninput (scaled) + body\nbind outline (red)"),
            render.label(render.render_layers([inp, ovl_fit], render.VIEWS["front(+Z)"], size, bounds),
                         "input + fitted body (blue)"),
            render.label(render.render_layers([res, ovl], render.VIEWS["front(+Z)"], size, bounds),
                         "RESULT bind pose + body\nfront (+Z)"),
            render.label(render.render_layers([res, ovl], render.VIEWS["back(-Z)"], size, bounds), "result back (-Z)"),
            render.label(render.render_layers([res, ovl], render.VIEWS["left(+X)"], size, bounds), "result side (+X)"),
        ]
        wc = render.weight_colors(r["transfer"].bone_idx, r["transfer"].weights, len(ref.skeleton))
        wl = render.Layer(r["Pb"], model.faces, vertex_colors=wc)
        pm = PoseModel(ref.skeleton, spec)
        x = np.array([vals[p.label] for p in pm.spec])
        A = pm.transforms(x)
        posed = lbs(r["Pb"], r["transfer"].bone_idx, r["transfer"].weights, A)
        pb = lbs(body.positions, body.bone_idx, body.weights, A)
        res_p = render.Layer(posed, model.faces, model.uvs, img)
        ovl_p = render.Layer(pb, body.faces, color=blue, overlay_alpha=0.3, outline=(20, 40, 200))
        row2 = [
            render.label(render.render_layers([wl], render.VIEWS["front(+Z)"], size, bounds), "weights (dominant bone color)"),
            render.label(render.render_layers([wl], render.VIEWS["back(-Z)"], size, bounds), "weights back"),
            render.label(render.render_layers([res_p, ovl_p], render.VIEWS["front(+Z)"], size, bounds),
                         "TEST POSE (LBS) + body\narms down, elbows, step"),
            render.label(render.render_layers([res_p, ovl_p], render.VIEWS["left(+X)"], size, bounds), "test pose side"),
            render.label(render.render_layers([res], render.VIEWS["3/4"], size, bounds), "result 3/4"),
        ]
        rows += [row1, row2]
    sheet = render.grid_sheet(rows)
    Image.fromarray(sheet).save(path)
