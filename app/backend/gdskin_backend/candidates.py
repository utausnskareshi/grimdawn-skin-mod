"""候補を利用者が選ぶ（SF06「この候補で作る」）と、保存した生成結果からの GLB の作り直し（SF14）。2026-10-17 追加機能 U-A2。

* 設定「候補を保存する」（``generation.keep_candidates``。既定オフ）がオンのとき、生成スクリプトが候補ごとの生成結果
  （raw sample。1 体約 50 MB）を下書きの ``generate\\samples\\model_seed<N>.pt`` に残す（generate_glb.py --sample-dir）。
  下書きにだけ置き、装備（library_ops.PREVIEW_FILES）・共有ファイル（.gdskin の許可リスト）には入れない。下書きを保存
  （keep_drafts がオフ）・破棄・再生成で置き換えると、下書きのフォルダーごと消える。
* ジョブ ``rebuild_glb``（列 gpu）: 下書きの候補のシード（省くと今のシード）の体で GLB を作り直し、変換し直す。

  - 生成結果が保存してあれば、それを ``--from-dump`` で GLB にする（3D 生成のモデルは読み込まない。torch.load は
    weights_only=True。C24）。候補の図と同じ体になる。今のシードなら、細かさ（simplify）・テクスチャの大きさ
    （texture_size）を変えて GLB だけを作り直せる。
  - 保存していない候補は、そのシードで候補の選別なしに 3D 生成し直す。同じシードでも GPU の計算の細かな違いで、
    候補の図とまったく同じ体にはならないことがある（docs の U-A1 の計測: 頂点数が数十違う程度）。

  既定の生成（自動の選別。D-012）は変わらない。利用者の選択・作り直しは下書きの ``generation.info.picked`` に記録し、
  装備に保存すると ``generation.candidate_choice`` に残る（library_ops._generation_info）。3D 生成・GLB 化に失敗・取消の
  ときは下書きを変えない（作業フォルダーを消して元の状態に戻す）。変換で失敗・取消のときは、変換し直し（run_reconvert）と
  同じく下書きを failed にする（生成結果は新しい GLB のまま。向きの変換し直しでやり直せる）。想定外の例外（OSError
  など）も同じ扱いにし、INTERNAL（下書きの ID つき）にする（下書きを処理の途中の状態のまま残さない。点検 UA2-R1）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import traceback

from . import candidate_colors, providers, redact
from .errors import BackendError, JobCancelled, failure_text
from .i18n import tr
from .providers.base import GenerationRequest
from .providers.trellis import SAMPLE_DIR, sample_file

NAME = "model"                  # 下書きの生成の出力の名前（GenerationRequest.name の既定）
_SAMPLE_RE = re.compile(re.escape(NAME) + r"_seed(\d+)\.pt")
WORK_NEW = "rebuild_new"        # 作り直しの作業フォルダー（成功したら rebuild_<k> に移す）
# 自動の選別（D-012・D-013）にかかわる生成の注意。別の候補を選んだときは、選んだ候補の判定で付け直す
POSE_WARNINGS = ("weapon_suspected", "pose_orientation_sensitive")
POSE_KEYS = ("arm_angle_deg", "arm_angle_error_deg", "pose_class", "weapon_suspected", "weapon_reasons")
# 作り直しの要求ごとに変えられる設定（ほかは下書きの生成の設定のまま）。細かさ・テクスチャの大きさは GLB の作り方、
# 低 VRAM・通信の遮断は PC 側の選択（結果を変えない）
OVERRIDABLE = ("simplify", "texture_size", "low_vram", "strict_offline")


def selection(draft) -> dict:
    """生成の候補の記録（generate_glb.py の <名前>_selection.json）。無い・読めないときは {}。"""
    gen = draft.data.get("generation")
    files = gen.get("files") if isinstance(gen, dict) else None
    rel = files.get("selection") if isinstance(files, dict) else None      # 形の崩れた draft.json は記録なし（R10B-05）
    if not rel:
        return {}
    try:
        with open(draft.path(*str(rel).split("/")), encoding="utf-8") as f:
            sel = json.load(f)
    except (OSError, ValueError):
        return {}
    return sel if isinstance(sel, dict) else {}


def sample_path(draft, seed) -> str | None:
    """シード seed の保存した生成結果（あれば）。"""
    if not isinstance(seed, int) or isinstance(seed, bool):
        return None
    p = sample_file(draft.generate_dir, NAME, seed)
    return p if os.path.isfile(p) else None


def saved_samples(draft) -> dict[int, int]:
    """保存した生成結果 → {シード: バイト数}。"""
    d = os.path.join(draft.generate_dir, SAMPLE_DIR)
    out: dict[int, int] = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        m = _SAMPLE_RE.fullmatch(n)
        if m:
            try:
                out[int(m.group(1))] = os.path.getsize(os.path.join(d, n))
            except OSError:
                pass
    return out


def _candidates(sel: dict) -> list[dict]:
    return [c for c in sel.get("candidates") or [] if isinstance(c, dict) and isinstance(c.get("seed"), int)
            and not isinstance(c.get("seed"), bool)]


def view(draft, colors: bool = True) -> dict | None:
    """下書きの要約（drafts.Draft.summary）の ``candidates``: 候補の一覧と、保存した生成結果の有無。

    生成 AI の結果の無い下書き（GLB の入力・装備の変換し直しの作業用）と、候補の記録も保存した生成結果も無い下書きは None。
    ``items`` の ``auto`` は自動の選別（D-012）が選んだ候補、``current`` は今の GLB の候補、``sample`` は生成結果が保存
    してあるか（あれば 3D 生成なしで作れる）、``selectable`` は選べるか（空の候補は選べない）。``rebuild_available`` は
    今の候補の生成結果が保存してあり、GLB だけを作り直せるか。``color`` は候補の図から測った色の付き方（``colored``・
    ``colorless``・None）、``color_notice`` は今の体がほぼ無彩色のときの注意（candidate_colors.notice。B-040）。
    colors が False なら候補の図を測らない（``color``・``color_notice`` は None。下書きの一覧で、画面が復元しない下書きの分。
    2026-11-18 点検 BC-02）。"""
    d = draft.data
    # 形の崩れた draft.json（generation・input が辞書でない）でも下書きの一覧を止めない（点検 R10B-05）
    gen = d.get("generation") if isinstance(d.get("generation"), dict) else {}
    inp = d.get("input") if isinstance(d.get("input"), dict) else {}
    if gen.get("status") != "done" or inp.get("kind") not in ("image", "text"):
        return None
    sel = selection(draft)
    cands = _candidates(sel)
    samples = saved_samples(draft)
    if not cands and not samples:
        return None
    cur = d.get("seed")
    measured = candidate_colors.for_draft(draft, sel) if colors else {}
    items = [{"seed": c["seed"], "round": c.get("round"), "auto": bool(c.get("chosen")), "current": c["seed"] == cur,
              "excluded": [str(x) for x in c.get("excluded") or []], "empty": c.get("empty"),
              **{k: c.get(k) for k in ("arm_angle_deg", "pose_class", "weapon_suspected")},
              "sample": c["seed"] in samples, "selectable": not c.get("empty"), "color": measured.get(c["seed"])}
             for c in cands]
    return {"auto_seed": sel.get("chosen_seed"), "current_seed": cur,
            "picked": gen["info"].get("picked") if isinstance(gen.get("info"), dict) else None, "items": items,
            "samples": len(samples), "samples_bytes": sum(samples.values()),
            "rebuild_available": isinstance(cur, int) and cur in samples,
            "color_notice": candidate_colors.notice(items, cur, inp.get("kind"))}


# ---------------------------------------------------------------------------
# ジョブ rebuild_glb
# ---------------------------------------------------------------------------
def summarize(p: dict) -> dict:
    return {"draft_id": p.get("draft_id"), "seed": p.get("seed")}


def _bad(message: str, reason: str, **params) -> BackendError:
    return BackendError("INVALID_INPUT", message, params={"reason": reason, **params})


def _pose_of(c: dict) -> dict:
    pose = {k: c.get(k) for k in POSE_KEYS}
    if c.get("orientation_differs"):
        pose["orientation_differs"] = c["orientation_differs"]
    return pose


def _pose_warnings(pose: dict) -> list[str]:
    return (["weapon_suspected"] if pose.get("weapon_suspected") else []) + \
           (["pose_orientation_sensitive"] if pose.get("orientation_differs") else [])


def run_rebuild(svc, ctx, params: dict) -> dict:
    """ジョブ ``rebuild_glb``: params = {"draft_id", "seed"（省くと今のシード）, "settings"（OVERRIDABLE だけ）,
    "require_sample"（true: 保存した生成結果が無ければ生成し直さずに止める。画面が「保存済み」と表示した候補を確認なしで
    選んだとき。点検 UA2-R2）}。"""
    from . import pipeline
    from .settings import validate_generation

    draft = svc.drafts.get(str(params.get("draft_id") or ""))
    d = draft.data
    kind = (d.get("input") or {}).get("kind")
    gen0 = d.get("generation") or {}
    if kind not in ("image", "text") or gen0.get("status") != "done" or not d.get("provider") or not gen0.get("glb"):
        raise _bad(tr("この下書きには生成 AI の生成結果がありません", "This draft has no result from the generation AI"),
                   "rebuild_not_generated")
    if d.get("status") not in ("ready", "failed"):
        raise _bad(tr(f"この下書きは処理の途中です（状態 {d.get('status')}）",
                      f"This draft is being processed (status {d.get('status')})"), "rebuild_draft_busy")
    busy = pipeline._setup_busy(svc, tr(" GLB を作り直せません", "The GLB cannot be rebuilt"))
    if busy:
        raise busy
    over = params.get("settings") or {}
    if not isinstance(over, dict):
        raise _bad(tr("settings はオブジェクトで指定してください", "Specify settings as an object"), "rebuild_settings")
    s = svc.settings.get()
    gen = validate_generation({**(d.get("settings") or s["generation"]), **{k: over[k] for k in OVERRIDABLE if k in over}})
    cur = d.get("seed")
    seed = params.get("seed", cur)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise _bad(tr(f"シードの指定が不正です: {seed!r}", f"Invalid seed: {seed!r}"), "candidate_unknown", seed=str(seed))
    sel = selection(draft)
    cand = next((c for c in _candidates(sel) if c["seed"] == seed), None)
    if seed != cur and (cand is None or cand.get("empty")):
        raise _bad(tr(f"シード {seed} はこの下書きの候補ではありません", f"Seed {seed} is not a candidate of this draft"),
                   "candidate_unknown", seed=seed)
    sample = sample_path(draft, seed)
    if sample is None and seed == cur:
        raise _bad(tr("この下書きには生成結果が保存されていないため、GLB だけを作り直すことはできません",
                      "This draft has no saved generation result, so the GLB alone cannot be rebuilt"), "no_saved_sample")
    if sample is None and params.get("require_sample") is True:
        # 画面では保存済みだった候補の .pt が、ジョブの開始までに消えた。確認なしに生成し直さない（点検 UA2-R2）
        raise _bad(tr(f"シード {seed} の候補の保存した生成結果が見つかりません",
                      f"The saved generation result of the candidate with seed {seed} was not found"),
                   "sample_missing", seed=seed)
    source ="saved_sample" if sample else "regenerated"
    prov = providers.get(d["provider"])
    secret = (d.get("input") or {}).get("text") if kind == "text" else None
    if secret:
        redact.add_secret(secret)
    try:
        return _rebuild(svc, ctx, draft, prov, s, gen, kind, seed, cur, cand, sel, sample, source)
    finally:
        if secret:
            redact.remove_secret(secret)


def _generation_images(draft) -> list[str]:
    """候補を生成し直すときに渡す画像: 元の生成で生成 AI に渡した画像（pipeline._generate が下書きに記録する
    generation_inputs）。記録が無い・欠けているときは下書きの入力画像の記録から（generation_file、無ければ file）。"""
    d = draft.data
    recorded = [draft.path(*str(r).split("/")) for r in d.get("generation_inputs") or []]
    if recorded and all(os.path.isfile(p) for p in recorded):
        return recorded
    # 背景除去の強制・アプリ側の背景除去（SF19・SF20）の画像は generation_file（pipeline._generate と同じ選び方）
    paths = [draft.path(*(r.get("generation_file") or r["file"]).split("/"))
             for r in (d.get("input") or {}).get("images") or [] if r.get("file")]
    missing = [p for p in paths if not os.path.isfile(p)]
    if not paths or missing:
        raise _bad(tr("生成に使った入力画像が下書きにありません", "The input image used for generation is not in the draft"),
                   "rebuild_input_missing")
    return paths


def _rebuild(svc, ctx, draft, prov, s, gen, kind, seed, cur, cand, sel, sample, source) -> dict:
    from . import pipeline

    d = draft.data
    gen0 = d.get("generation") or {}
    info0 = dict(gen0.get("info") or {})
    ctx.progress(0.0, "generate_check")
    # 保存した生成結果から作る（--from-dump）ときは 3D 生成のモデル・DINOv2・CLIP を読み込まないので、それらの有無と
    # CLIP の注意文への同意では止めない（実行環境・ソース・生成スクリプト・GPU は使うので確かめる。点検 R10A-05）
    gd, warnings = pipeline.preflight(svc, "dump" if source == "saved_sample" else kind, gen, ctx)
    for w in warnings:
        ctx.warn(w)
    prev_status = d.get("status")
    auto_seed = sel.get("chosen_seed") if sel.get("chosen_seed") is not None else (info0.get("picked") or {}).get("auto_seed", cur)
    how = (tr("保存した生成結果から。3D 生成はしません", "from the saved generation result, no 3D generation")
           if source == "saved_sample" else
           tr("生成結果が保存されていないため、このシードで生成し直します",
              "the generation result is not saved, so it is regenerated with this seed"))
    ctx.info(tr(f"下書き {draft.id}: シード {seed} の候補の体で GLB を作り直します（{how}"
                f"。自動の選別ではシード {auto_seed}、simplify {gen['simplify']:g}、テクスチャ {gen['texture_size']}）",
                f"Draft {draft.id}: rebuilding the GLB from the body of the candidate with seed {seed} ({how}; automatic "
                f"selection: seed {auto_seed}; simplify {gen['simplify']:g}, texture {gen['texture_size']})"))
    paths = _generation_images(draft) if kind == "image" and not sample else []
    tmp = os.path.join(draft.generate_dir, WORK_NEW)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    labels = [r.get("label") for r in (d.get("input") or {}).get("images") or []] if kind == "image" else []
    req = GenerationRequest(kind=kind, settings={**gen, "seed": seed, "select_pose": False, "keep_candidates": False},
                            text=(d.get("input") or {}).get("text") if kind == "text" else None,
                            image_path=paths[0] if paths else None, image_paths=paths or None,
                            image_labels=labels if labels and len(labels) == len(paths) else None, name=NAME)
    lo, hi = (0.0, 0.5) if sample else pipeline.GEN_RANGE
    d["status"] = "generating"
    svc.drafts.save(draft)
    try:
        run = prov.prepare_rebuild(s, req, sample, tmp) if sample else prov.prepare(s, req, tmp)
        ctx.progress(lo + 0.01, "generate_load")

        def on_line(line: str) -> None:
            u = prov.parse_line(line, run)
            if u is None:
                return
            if u.progress is not None or u.stage:
                ctx.progress(None if u.progress is None else lo + (hi - lo) * u.progress, u.stage, u.detail)
            if u.log:
                ctx.info(u.log)

        rc, out = ctx.run_process(run.cmd, on_line, env=run.env, cwd=run.cwd)
        if rc != 0:
            code = prov.classify_failure(out, run)
            ep = run.state.get("error_params") or {}
            msg = (tr(f"{prov.display_name} の GLB の作成のプロセスが終了コード {rc} で終わりました",
                      f"The {prov.display_name} GLB creation process ended with exit code {rc}") if sample
                   else tr(f"{prov.display_name} の生成のプロセスが終了コード {rc} で終わりました",
                           f"The {prov.display_name} generation process ended with exit code {rc}"))
            raise BackendError(code, msg, failure_text(out, 40), {"exit_code": rc, **ep})
        try:
            res = prov.collect(s, req, tmp, run)
        except (FileNotFoundError, ValueError, KeyError) as e:
            raise BackendError("INVALID_MODEL", tr(f"作り直した GLB がありません・読めません: {type(e).__name__}",
                                                   f"The rebuilt GLB is missing or cannot be read: {type(e).__name__}"),
                               failure_text(out, 20))
    except Exception as e:      # noqa: BLE001  失敗・取消: 下書きは元のまま（作業フォルダーを消し、状態を戻す）
        _restore(svc, draft, tmp, prev_status)
        if isinstance(e, (BackendError, JobCancelled)):
            if isinstance(e, BackendError):
                e.params = {**e.params, "draft_id": draft.id}
            raise
        raise _internal(e, draft) from e

    # --- 結果を下書きに入れる（作業フォルダー → rebuild_<k>。前の作り直しの結果は消す） ----------------------------
    # 想定外の例外（OSError など）でも、下書きを「処理の途中」のまま残さない（点検 UA2-R1。pipeline._run_generate の C104
    # と同じ形）。下書きの生成の記録を書き換える前なら元に戻し、書き換えた後なら failed にする。
    final, moved_ok = None, False
    try:
        k = int(gen0.get("rebuild_count") or 0) + 1
        final = os.path.join(draft.generate_dir, f"rebuild_{k}")
        shutil.rmtree(final, ignore_errors=True)
        while os.path.exists(final):               # 消せなかった残り（使用中など）は使わず、次の番号にする
            k += 1
            final = os.path.join(draft.generate_dir, f"rebuild_{k}")
        os.replace(tmp, final)
        moved_ok = True
        update = _result_update(draft, gen0, info0, res, tmp, final, source, cand, sel, seed, auto_seed, gen, k)
    except Exception as e:      # noqa: BLE001
        if moved_ok:
            shutil.rmtree(final, ignore_errors=True)
        _restore(svc, draft, tmp, prev_status)
        raise _internal(e, draft) from e
    picked, warns, old_w = update["picked"], update["warns"], update["old_w"]
    old_dir = gen0.get("rebuild_dir")
    try:
        d["generation"] = update["generation"]
        d["seed"] = seed
        d["settings"] = {**(d.get("settings") or {}), "simplify": gen["simplify"], "texture_size": gen["texture_size"]}
        d["status"] = "converting"
        svc.drafts.save(draft)
        if old_dir and old_dir != update["generation"]["rebuild_dir"]:
            shutil.rmtree(draft.path(*old_dir.split("/")), ignore_errors=True)
        for w in warns:
            if w not in old_w:
                ctx.warn(tr(f"生成の注意: {w}", f"Generation warning: {w}"))
        ctx.progress(hi, "generate_done")

        # --- 変換（同じ体なら今の向き、別の体なら回さない。生成し直したときと同じ） --------------------------------
        conv = d.get("conversion") or {}
        yaw = int(conv.get("yaw_deg") or 0) if seed == cur else 0
        pipeline._convert(svc, ctx, draft, draft.path(*d["generation"]["glb"].split("/")), gd, gen, (hi, 1.0), yaw_deg=yaw)
        d["status"] = "ready"
        svc.drafts.save(draft)
    except Exception as e:      # noqa: BLE001  変換の失敗・取消・想定外の例外: 変換し直し（run_reconvert）と同じく failed
        err = e if isinstance(e, (BackendError, JobCancelled)) else _internal(e, draft)
        d["status"] = "failed"
        if d.get("conversion"):
            d["conversion"]["status"] = "failed"
        code, msg = ((err.code, err.message) if isinstance(err, BackendError)
                     else ("CANCELLED", tr("GLB の作り直しを取り消しました", "The GLB rebuild was cancelled")))
        svc.drafts.add_error(draft, ctx.job.stage or "", code, msg)
        try:
            svc.drafts.save(draft)
        except OSError:
            pass
        if isinstance(err, BackendError):
            err.params = {**err.params, "draft_id": draft.id}
        if err is e:
            raise
        raise err from e
    ctx.info(tr(f"下書き {draft.id} をシード {seed} の体で作り直しました（保存した生成結果から）",
                f"Rebuilt draft {draft.id} with the body of seed {seed} (from the saved generation result)")
             if source == "saved_sample" else
             tr(f"下書き {draft.id} をシード {seed} の体で作り直しました（生成し直し）",
                f"Rebuilt draft {draft.id} with the body of seed {seed} (regenerated)"))
    return {"draft": draft.summary(), "picked": picked}


def _restore(svc, draft, tmp: str, prev_status) -> None:
    """3D 生成・GLB 化（とその結果の取り込みの前半）の失敗・取消: 作業フォルダーを消し、下書きの状態を元に戻す。"""
    shutil.rmtree(tmp, ignore_errors=True)
    draft.data["status"] = prev_status
    try:
        svc.drafts.save(draft)
    except OSError:
        pass


def _internal(e: Exception, draft) -> BackendError:
    """想定外の例外 → INTERNAL（下書きの ID つき。pipeline._run_generate と同じ形）。"""
    return BackendError("INTERNAL", f"{type(e).__name__}: {e}", failure_text(traceback.format_exc(), 30),
                        {"draft_id": draft.id})


def _result_update(draft, gen0, info0, res, tmp, final, source, cand, sel, seed, auto_seed, gen, k) -> dict:
    """作り直した GLB（rebuild_<k> に移した後）→ 下書きの新しい生成の記録など（下書きはまだ変えない）。"""
    rel = lambda p: os.path.relpath(p, draft.dir).replace("\\", "/")          # noqa: E731
    moved = lambda p: os.path.join(final, os.path.relpath(p, tmp))           # noqa: E731
    info = dict(info0)
    new = res.info or {}
    info["glb"] = new.get("glb")
    if source == "regenerated":
        info["pose"] = new.get("pose")             # 生成し直した体の判定（記録だけ）
        for key in ("sampler_overrides", "sampler_params", "low_vram", "strict_offline", "offline_blocked"):
            if key in new:
                info[key] = new[key]
    elif cand is not None:
        info["pose"] = _pose_of(cand)              # 候補の判定（generate_glb.py の選別の記録）
    picked = {"seed": seed, "auto_seed": auto_seed, "source": source,
              "simplify": gen["simplify"], "texture_size": gen["texture_size"]}
    if source == "saved_sample" and "offline_blocked" in new:
        picked["offline_blocked"] = new["offline_blocked"]
    if source == "regenerated":
        # このシードで候補の選別なしに生成し直した（ほかの生成の設定は generation.settings のまま。点検 UA2-R6）。
        # generation.settings の seed・select_pose は元の生成の要求の値なので、再現の手順をここに明記する
        picked["select_pose"] = False
    info["picked"] = picked
    files = dict(gen0.get("files") or {})
    files.pop("views_png", None)
    if res.files.get("views_png"):
        files["views_png"] = rel(moved(res.files["views_png"]))
    if res.files.get("metrics"):
        files["rebuild_metrics"] = rel(moved(res.files["metrics"]))
    # 生成の注意: 入力・文章の注意はそのまま、体の判定の注意（武器の疑い・向きで変わる判定）と自動の選別の注意は、
    # 今の体のものに付け直す
    old_w = list(gen0.get("warnings") or [])
    is_pose_w = lambda w: w in POSE_WARNINGS or str(w).startswith("selection:")    # noqa: E731
    base = [w for w in old_w if not is_pose_w(w)]
    if source == "regenerated":
        warns = base + [w for w in res.warnings if w in POSE_WARNINGS]
    elif cand is not None:
        warns = base + _pose_warnings(info.get("pose") or {}) + (
            ["selection: " + str(sel["warning"])] if seed == auto_seed and sel.get("warning") else [])
    else:                                          # 候補の記録の無い同じ体（候補の選別なし）: GLB だけを作り直した
        warns = old_w
    generation = {**gen0, "glb": rel(moved(res.glb)), "files": files, "warnings": warns, "info": info,
                  "rebuild_dir": rel(final), "rebuild_count": k}
    return {"generation": generation, "picked": picked, "warns": warns, "old_w": old_w}
