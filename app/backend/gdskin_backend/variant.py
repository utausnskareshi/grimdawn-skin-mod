"""装備の生成結果を元に、文章で見た目だけを作り直す（2026-10-17 追加機能 SF10。U-A3）。

TRELLIS のテキスト版の run_variant（固定したコミットの ``TrellisTextTo3DPipeline.run_variant``。example_variant.py）を使う。
装備に保存してある生成結果（``source\\generated.glb``）を TRELLIS の z-up の空間に戻して 64³ にボクセル化し、その形を
そのまま使って、文章から見た目（SLat）だけをサンプリングする（generate_glb.py --variant-base、scripts/trellis/variant_base.py）。
形と姿勢はほぼ元のまま、色・質感・装飾が変わる見込み [品質と姿勢の保たれ方は広くは評価していない]。

* 既定では使わない: 画面の生成の詳細設定で「装備一覧に『文章で見た目を作り直す』を出す」をオンにしたときだけ、装備一覧から
  始められる（既定の生成・変換・記録は何も変わらない）。テキストの任意部品（microsoft/TRELLIS-text-xlarge と CLIP）と、CLIP の
  注意文への同意（D-011）が要る（生成の前の確認 ``pipeline.preflight`` の "text" と同じ確認で止める）。
* ジョブ ``variant``（列 gpu）: params = {"equipment_id"（元の装備）, "text", "settings"（生成の設定。省いた項目は保存した設定）,
  "replace_equipment_id"（任意。保存するとその装備を差し替える。作り直した装備の「再生成」。元の装備そのものは指定できない:
  INVALID_INPUT・reason variant_self。前に作ったときの元の GLB と SHA-256 が違えば注意 variant_base_changed）}。
  新しい下書き（入力の種類 ``variant``）を作り、元の GLB を下書きの ``input\\variant_base.glb`` に写してから生成・変換する。
  元の装備は変えない。
* 候補の選別（D-012）・候補の保存（SF14）・背景の設定（SF19・SF20）は使わない（下書き・装備の生成の設定にはオフとして
  記録する）。サンプリングの設定（SF05）は SLat の分だけを渡す。「武器なし」の付け足し（D-013）は文章からの生成と同じ。
* 変換の向き（yaw_deg）は元の装備と同じ（作り直した GLB は元の GLB と同じ向きで出る）。
* 保存した装備の生成方法は ``text``（入力の文章・モデル microsoft/TRELLIS-text-xlarge・シード）で、``generation.variant_of``
  に元の装備の内部ID・元の GLB の SHA-256・元の生成方法・モデル・シード・向き・ボクセル数が残る（library_ops._generation_info）。
"""
from __future__ import annotations

import os
import shutil
import traceback

from . import providers, redact
from .errors import BackendError, JobCancelled, failure_text
from .i18n import tr
from .settings import BG_OPTION_KEYS, validate_generation

BASE_FILE = "input/variant_base.glb"
# 作り直しでは使わない生成の設定（オフとして下書き・装備の記録に残す）
UNUSED_SETTINGS = {"select_pose": False, "keep_candidates": False, "bg_app_removal": False,
                   **{k: False for k in BG_OPTION_KEYS}}
# 装備の生成の記録 variant_of に残す、元の装備の情報（下書きの input.variant の項目）
RECORD_KEYS = ("equipment_id", "base_glb_sha256", "base_method", "base_model_id", "base_seed", "yaw_deg")


def summarize(p: dict) -> dict:
    return {"equipment_id": p.get("equipment_id"), "replace_equipment_id": p.get("replace_equipment_id")}


def _bad(message: str, reason: str, **params) -> BackendError:
    return BackendError("INVALID_INPUT", message, params={"reason": reason, **params})


def record(draft_data: dict) -> dict | None:
    """下書きの入力（種類 variant）→ 装備の generation.variant_of（無ければ None）。"""
    inp = draft_data.get("input") or {}
    v = inp.get("variant")
    if inp.get("kind") != "variant" or not isinstance(v, dict):
        return None
    out = {k: v.get(k) for k in RECORD_KEYS}
    info = ((draft_data.get("generation") or {}).get("info") or {}).get("variant") or {}
    out["active_voxels"] = info.get("active_voxels")
    out["pipeline"] = "TrellisTextTo3DPipeline.run_variant"
    return out


def run_variant(svc, ctx, params: dict) -> dict:
    from . import library_ops, pipeline

    busy = pipeline._setup_busy(svc, tr("見た目を作り直せません", "The appearance cannot be remade"))
    if busy:
        raise busy
    eq_id = str(params.get("equipment_id") or "")
    over = params.get("settings") or {}
    if not isinstance(over, dict):
        raise _bad(tr("settings はオブジェクトで指定してください", "Specify settings as an object"), "variant_settings")
    s = svc.settings.get()
    gen = {**validate_generation({**s["generation"], **over}), **UNUSED_SETTINGS}
    prov = providers.get(gen["provider"])
    if not callable(getattr(prov, "variant_supported", None)) or not prov.variant_supported():
        raise _bad(tr(f"{prov.display_name} の生成スクリプトは見た目の作り直しに対応していません",
                      f"The {prov.display_name} generation script does not support appearance remakes"),
                   "variant_unsupported")
    text = pipeline.validate_text(params.get("text"))
    replace = params.get("replace_equipment_id")
    # 元の装備をそれ自身の作り直しで差し替えると variant_of が自分を指し、再生成のたびに形がずれていく（点検 UA3-R4）
    if replace is not None and replace == eq_id:
        raise _bad(tr("元の装備を、その装備自身から作り直した見た目で差し替えることはできません",
                      "Equipment cannot be replaced by an appearance remade from itself"), "variant_self",
                   equipment_id=eq_id)
    lib = library_ops._wrap(library_ops._lib)(svc)
    if eq_id not in lib.ids():
        raise BackendError("NOT_FOUND", tr(f"装備がありません: {eq_id}", f"Equipment not found: {eq_id}"))
    if replace is not None and (not isinstance(replace, str) or replace not in lib.ids()):
        raise BackendError("NOT_FOUND", tr(f"差し替える装備がありません: {replace}",
                                           f"The equipment to replace was not found: {replace}"))
    eq = lib.load(eq_id)
    base = os.path.join(eq.dir, "source", "generated.glb")
    if not os.path.isfile(base):
        raise _bad(tr("この装備には生成結果（source\\generated.glb）が保存されていないため、見た目を作り直せません",
                      "This equipment has no saved generation result (source\\generated.glb), so its appearance cannot "
                      "be remade"),
                   "variant_no_base", equipment_id=eq_id)
    # 作り直した装備の再生成（同じ元の装備で差し替える）: 前に作ったときの元の GLB の SHA-256（元の装備がその後に再生成・
    # 差し替えされていれば、いまの形と違うことを知らせる。_run）
    prev_sha = None
    if replace is not None:
        try:
            vo = ((lib.load(replace).data.get("generation") or {}).get("variant_of") or {})
        except Exception:       # noqa: BLE001  知らせるためだけの読み込み（読めない装備は保存のときに今までどおり止まる）
            vo = {}
        if isinstance(vo, dict) and vo.get("equipment_id") == eq_id and isinstance(vo.get("base_glb_sha256"), str):
            prev_sha = vo["base_glb_sha256"]
    redact.add_secret(text)        # 文章はログ・子プロセスの出力から置き換える（文章からの生成と同じ）
    try:
        return _run(svc, ctx, s, gen, prov, text, eq, base, replace, prev_sha)
    finally:
        redact.remove_secret(text)


def _run(svc, ctx, s, gen, prov, text, eq, base, replace, prev_sha=None) -> dict:
    from . import pipeline

    ctx.progress(0.0, "generate_check")
    gd, warnings = pipeline.preflight(svc, "text", gen, ctx)      # テキストのモデル・CLIP・同意（D-011）・GPU
    for w in warnings:
        ctx.warn(w)
    if "prompt_non_english" in pipeline.text_notes(text):
        ctx.warn(tr("文章に英語以外の文字が多く含まれています。生成 AI は英語の文章を前提にしているため、意図どおりの見た目に"
                    "ならないことがあります（日本語では約 30 文字で、生成 AI が読む長さの上限を超えます）",
                    "The text contains many non-English characters. The generation AI expects English text, so "
                    "the appearance may not come out as intended (in Japanese, about 30 characters already exceed "
                    "the length the generation AI reads)"), code="prompt_non_english")
    g0 = eq.data.get("generation") or {}
    y = (eq.data.get("conversion") or {}).get("yaw_deg") or 0
    yaw = y if y in pipeline.YAW_CHOICES and not isinstance(y, bool) else 0
    draft = svc.drafts.create("variant", prov.id, gen, replace, ctx.job.id)
    try:
        ctx.job.emit("draft", draft_id=draft.id)
        dst = draft.path(*BASE_FILE.split("/"))
        shutil.copyfile(base, dst)          # 生成の間に元の装備が変わっても、この下書きは写した GLB で作る
        draft.data["input"]["text"] = text
        sha = pipeline._sha256(dst)
        draft.data["input"]["variant"] = {"file": BASE_FILE, "equipment_id": eq.id, "base_glb_sha256": sha,
                                          "base_method": g0.get("method"), "base_model_id": g0.get("model_id"),
                                          "base_seed": g0.get("seed"), "yaw_deg": int(yaw)}
        if prev_sha and prev_sha != sha:
            # 下書きの注意（生成の記録の warnings の先頭。pipeline._generate）とジョブの注意で知らせる（止めない。UA3-R4）
            draft.data["input"]["warnings"] = ["variant_base_changed"]
            ctx.warn(tr(f"元の装備 {eq.id} の生成結果が、装備 {replace} を前に作ったときから変わっています（元の装備を再生成・"
                        "差し替えた、など）。今の元の装備の形で作り直します",
                        f"The generation result of the original equipment {eq.id} has changed since equipment "
                        f"{replace} was made (the original was regenerated or replaced, etc.). It is remade with the "
                        "current shape of the original equipment"), code="variant_base_changed")
        draft.data["status"] = "generating"
        svc.drafts.save(draft)
        rep = tr(f"、保存すると装備 {replace} を差し替え", f", replaces equipment {replace} when saved") if replace else ""
        ctx.info(tr(f"下書き {draft.id} を作成（見た目の作り直し: 装備 {eq.id} の生成結果の形、文章 {redact.fingerprint(text)}、"
                    f"変換の向き {yaw}°{rep}）",
                    f"Created draft {draft.id} (appearance remake: shape of the generation result of equipment "
                    f"{eq.id}, text {redact.fingerprint(text)}, conversion direction {yaw}°{rep})"))
        glb = pipeline._generate(svc, ctx, prov, s, "text", gen, text, draft)
        pipeline._convert(svc, ctx, draft, glb, gd, gen, pipeline.CONV_RANGE, yaw_deg=yaw)
        draft.data["status"] = "ready"
        svc.drafts.save(draft)
        ctx.info(tr(f"下書き {draft.id} の準備ができました（見た目の作り直し）",
                    f"Draft {draft.id} is ready (appearance remake)"))
        return {"draft": draft.summary()}
    except JobCancelled:
        svc.drafts.delete(draft.id)
        raise
    except BackendError as e:          # 失敗は下書きに記録して残す（生成の画面で原因を確かめられる。pipeline と同じ）
        draft.data["status"] = "failed"
        svc.drafts.add_error(draft, ctx.job.stage or "", e.code, e.message)
        e.params = {**e.params, "draft_id": draft.id}
        try:
            svc.drafts.save(draft)
        except (OSError, ValueError):    # 保存の失敗で分類済みのエラーと draft_id を INTERNAL に置き換えない（点検 R10B-01・R10B-03）
            pass
        raise
    except Exception as e:      # noqa: BLE001  想定外の例外も下書きを失敗として記録する（pipeline の C104 と同じ）
        err = BackendError("INTERNAL", f"{type(e).__name__}: {e}", failure_text(traceback.format_exc(), 30),
                           {"draft_id": draft.id})
        draft.data["status"] = "failed"
        svc.drafts.add_error(draft, ctx.job.stage or "", err.code, err.message)
        try:
            svc.drafts.save(draft)
        except (OSError, ValueError):
            pass
        raise err from e
