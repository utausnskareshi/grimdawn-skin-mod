"""装備パッケージ（``.gdskin``）の書き出し・検査・取り込み（docs/app_architecture.md 15.1 節）。

中身（zip）::

  gdskin_package.json                         記録（形式・版・作成ツールの版・装備の一覧・全ファイルの SHA-256・package_id）
  equipment/<装備ID>/equipment.json           装備の記録（modbuilder の形式。利用者のフォルダー名などを除いたもの）
  equipment/<装備ID>/assets/<ID>_m.msh, _f.msh, _dif.tex    MOD に入るアセット（男女の .msh・.tex）
  equipment/<装備ID>/source/preview.png       変換の確認画像
  equipment/<装備ID>/source/preview_m.glb, preview_f.glb    3D プレビュー（男性用・女性用のバインド姿勢）
  equipment/<装備ID>/source/input*.png        入力画像（「入力画像とプロンプトを含める」を選んだときだけ）

入力画像とプロンプト（``generation.input_text``）は既定では含めない。元のファイル名・変換の作業フォルダー・利用者の
フォルダー名を含むパスは、含める場合でも書き出さない（:func:`sanitized_record`）。

取り込みは内部ID・レコードのパス・タグ名・作成日時・アセットのバイト列を保つ（modbuilder の ``import_from_library``。
同じ装備を持つ利用者どうしで同じ database.arz を作れるように）。同じ内部IDの装備が既にあれば、MOD に関わる内容
（:func:`mod_fingerprint`）が同じなら飛ばし、違えば利用者が選んだものだけを置き換える（今の装備は trash へ）。置き換えでは、
3D モデル・テクスチャが同じなら、手元にしか無い生成結果（generated.glb）・変換の報告・入力画像とプロンプトを引き継ぐ
（:func:`_carry_over_local`。違えば引き継がず、確認でそのことを示す。2026-10-17 監査 C95）。

受け取った装備の記録は :func:`validate_record` で検査する（naming は内部IDから決まるものと完全に一致、項目の型など。
2026-10-17 監査 C13・C96）。
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import shutil
import time

from . import __version__, logs, package_io
from .errors import BackendError
from .i18n import strip_end_period, tr

KIND = "equipment"
SOURCE_FILES = ("preview.png", "preview_m.glb", "preview_f.glb")
# 末尾は \Z（$ は末尾の改行の直前でも一致する。2026-10-27 検証 R12A-05）
INPUT_FILE_RE = re.compile(r"^input(?:_[1-4])?\.png\Z")
ASSET_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}\.(msh|tex)\Z")
ASSET_SUFFIXES = ("_m.msh", "_f.msh", "_dif.tex")     # アセットの名前は <asset_prefix> + これ（importers が必ずこの名前で作る）
# 受け取ったアセット 1 件の大きさの上限（.msh は 65,535 頂点の上限から数 MB、.tex は 8192² の DXT5 とミップでも約 90 MB。
# 記録どおりのハッシュなら大きなデータでも取り込めた。2026-10-27 検証 R12B-06）
ASSET_MAX = {".msh": 64 * 1024 * 1024, ".tex": 128 * 1024 * 1024}
# 受け取った装備の記録（equipment.json）の上限（正規の記録は 4 KB 前後。2026-10-27 検証 X5X-03・R12B-01）
RECORD_MAX = 256 * 1024
ITEM_TEXT_MAX = 200         # 確認の一覧に写す名前・作成日時・生成方法の長さの上限
MAX_ITEMS = 200
ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
# MOD の内容に関わる項目（同じ内部IDの装備が同じかの判定。表示名は Text_EN.arc に、作成日時は補給官の並び順と
# database.arz の時刻に、その他はレコード・Items.arc に入る）
FINGERPRINT_KEYS = ("display_name", "texts", "assets", "created_at", "illusion_set", "naming")


def _tool_info() -> dict:
    import modbuilder
    info = {"app": "GD Skin Studio", "backend": __version__, "modbuilder": modbuilder.__version__}
    try:
        import gdconvert
        info["gdconvert"] = gdconvert.__version__
    except Exception:   # noqa: BLE001
        pass
    return info


def _lib(svc, create: bool = True):
    from modbuilder.library import Library
    return Library.open(svc.library_dir(), create=create)


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------
def _scrub_paths(v, count: list[int]):
    """絶対パス（利用者のフォルダー名を含みうる）をファイル名だけにする。"""
    if isinstance(v, dict):
        return {k: _scrub_paths(x, count) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_paths(x, count) for x in v]
    if isinstance(v, str) and ABS_PATH_RE.match(v):
        count[0] += 1
        return re.split(r"[\\/]", v)[-1]
    return v


def sanitized_record(data: dict, include_inputs: bool) -> dict:
    """書き出す装備の記録。MOD の状態・エラーの履歴・取り込み元・作業フォルダー・元のファイル名は入れない。"""
    keep = ("format", "version", "id", "display_name", "texts", "generation", "created_at", "updated_at", "naming",
            "illusion_set", "assets", "conversion", "dev_only")
    d = {k: copy.deepcopy(data[k]) for k in keep if k in data}
    d["mod"] = {"status": "not_built", "last_build_id": None, "last_built_at": None, "installed_build_id": None,
                "installed_at": None, "error": None}
    d["errors"] = []
    gen = d.setdefault("generation", {})
    gen["source_glb"] = None                      # 生成結果の GLB（source\generated.glb）は入れない
    if include_inputs:
        if isinstance(gen.get("input_image"), dict):
            gen["input_image"] = {k: gen["input_image"].get(k) for k in ("stored_path", "sha256", "size")}
            gen["input_image"]["original_path"] = None
        # 背景除去の強制・アプリ側の背景除去（SF19・SF20。使ったときだけ記録にある）も再生成で同じにするため残す（U-B）
        gen["input_images"] = [{**{k: r.get(k) for k in ("label", "sha256", "size", "has_alpha", "stored_path")},
                                **{k: r[k] for k in ("force_bg_removal", "bg_removal") if r.get(k)}}
                               for r in gen.get("input_images") or [] if isinstance(r, dict)]
    else:
        gen["input_text"] = None
        gen["input_image"] = None
        gen["input_images"] = [{"label": r.get("label")} for r in gen.get("input_images") or [] if isinstance(r, dict)]
    conv = d.setdefault("conversion", {})
    conv["source_dir"] = None
    conv["report"] = None
    d["dev_only"] = bool(d.get("dev_only", gen.get("method") == "prototype"))
    return _scrub_paths(d, [0])


def run_export(svc, ctx, params: dict) -> dict:
    """ジョブ export_equipment: {"ids": [...], "dest": "...\\x.gdskin", "include_inputs": false, "overwrite": false}。"""
    ids = params.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise BackendError("INVALID_INPUT", tr("書き出す装備を 1 件以上選んでください", "Select at least one equipment to export"),
                           params={"reason": "package_no_items"})
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_ITEMS:
        raise BackendError("INVALID_INPUT", tr(f"一度に書き出せる装備は {MAX_ITEMS} 件までです",
                                               f"You can export up to {MAX_ITEMS} equipment at a time"))
    dest = str(params.get("dest") or "")
    if not dest:
        raise BackendError("INVALID_INPUT", tr("出力先のファイルを指定してください", "Specify the destination file"),
                           params={"reason": "package_path"})
    if not dest.lower().endswith(package_io.KINDS[KIND]["ext"]):
        dest += package_io.KINDS[KIND]["ext"]
    include_inputs = bool(params.get("include_inputs"))
    with svc.mod_lock:
        lib = _lib(svc)
        known = set(lib.ids())
        missing = [i for i in ids if i not in known]
        if missing:
            raise BackendError("NOT_FOUND", tr(f"装備がありません: {missing}", f"Equipment not found: {missing}"))
        items = sorted((lib.load(i) for i in ids), key=lambda e: (e.created_at, e.id))
        for eq in items:
            if (eq.data.get("conversion") or {}).get("status") != "converted" or not eq.data["assets"].get("files"):
                raise BackendError("INVALID_INPUT", tr(f"{eq.id} は変換済みではないので書き出せません",
                                                       f"{eq.id} cannot be exported because it has not been converted"),
                                   params={"reason": "package_not_converted", "id": eq.id})
            problems = lib.verify_assets(eq)
            if problems:
                raise BackendError("EXPORT_FAILED", tr("装備のアセットが記録と一致しません: ",
                                                       "The equipment assets do not match the record: ") + "; ".join(problems))
        ctx.progress(0.02, "package_export")
        with package_io.PackageWriter(dest, KIND, overwrite=bool(params.get("overwrite"))) as w:
            listed = []
            for k, eq in enumerate(items):
                ctx.check_cancel()
                base = f"equipment/{eq.id}/"
                for f in eq.data["assets"]["files"]:
                    w.add_file(base + "assets/" + f["file"], eq.asset_path(f["file"]))
                src = os.path.join(eq.dir, "source")
                n_source = 0
                for name in SOURCE_FILES:
                    p = os.path.join(src, name)
                    if os.path.isfile(p):
                        w.add_file(base + "source/" + name, p)
                        n_source += 1
                rec = sanitized_record(eq.data, include_inputs)
                n_inputs = 0
                if include_inputs and os.path.isdir(src):
                    for name in sorted(os.listdir(src)):
                        if INPUT_FILE_RE.match(name):
                            w.add_file(base + "source/" + name, os.path.join(src, name))
                            n_inputs += 1
                    _drop_missing_inputs(rec, {f"source/{n}" for n in os.listdir(src) if INPUT_FILE_RE.match(n)})
                w.add_bytes(base + "equipment.json",
                            (json.dumps(rec, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
                listed.append({"id": eq.id, "name": eq.name_en, "name_ja": eq.name_ja, "created_at": eq.created_at,
                               "method": (eq.data.get("generation") or {}).get("method"), "head_record": eq.naming["head_record"],
                               "input_images": n_inputs, "has_prompt": bool(include_inputs and rec["generation"].get("input_text"))})
                ctx.progress(0.05 + 0.9 * (k + 1) / len(items), "package_export", {"index": k + 1, "count": len(items)})
            manifest = w.finish({"created_at": package_io.now_iso(), "tool": _tool_info(), "include_inputs": include_inputs,
                                 "equipment": listed})
    size = os.path.getsize(w.dest)
    logs.info(tr(f"装備パッケージを書き出しました: {len(items)} 件、{size:,} B、package {manifest['package_id']}",
                 f"Exported the equipment package: {len(items)} equipment, {size:,} B, package {manifest['package_id']}")
              + (tr("（入力画像・プロンプトを含む）", " (input images and prompts included)") if include_inputs else ""))
    ctx.progress(1.0, "package_export_done")
    return {"dest": w.dest, "package_id": manifest["package_id"], "size": size, "include_inputs": include_inputs,
            "equipment": listed}


def _drop_missing_inputs(rec: dict, present: set[str]) -> None:
    """記録にある入力画像の stored_path のうち、パッケージに入れなかったものを消す。"""
    gen = rec.get("generation") or {}
    for r in ([gen.get("input_image")] if isinstance(gen.get("input_image"), dict) else []) + list(gen.get("input_images") or []):
        if r.get("stored_path") and r["stored_path"] not in present:
            r["stored_path"] = None


# ---------------------------------------------------------------------------
# 検査
# ---------------------------------------------------------------------------
def mod_fingerprint(data: dict) -> dict:
    """MOD の内容に関わる項目（同じ内部IDの装備の比較用）。"""
    a = data.get("assets") or {}
    files = sorted((f.get("file"), f.get("game_path"), f.get("sha256"), f.get("size")) for f in a.get("files") or [])
    return {"display_name": data.get("display_name") or {}, "texts": {k: v for k, v in (data.get("texts") or {}).items() if v},
            "assets": {"male_mesh": a.get("male_mesh"), "female_mesh": a.get("female_mesh"),
                       "head_textures": a.get("head_textures") or {}, "files": [list(x) for x in files]},
            "created_at": data.get("created_at"),
            "illusion_set": {"owner": (data.get("illusion_set") or {}).get("owner"),
                             "include_invisible": bool((data.get("illusion_set") or {}).get("include_invisible", True))},
            "naming": {k: v for k, v in (data.get("naming") or {}).items() if k != "scheme"}}


def differences(a: dict, b: dict) -> list[str]:
    fa, fb = mod_fingerprint(a), mod_fingerprint(b)
    return [k for k in FINGERPRINT_KEYS if fa[k] != fb[k]]


def local_only(eq) -> dict:
    """手元の装備にしか無いもの（パッケージに入れない生成結果の GLB・変換の報告と、既定では入れない入力）。
    置き換えの確認で、失われるもの・引き継ぐものを示す（2026-10-17 監査 C95）。"""
    src = os.path.join(eq.dir, "source")
    names = set(os.listdir(src)) if os.path.isdir(src) else set()
    numbered = sum(1 for n in names if INPUT_FILE_RE.match(n) and n != "input.png")    # input.png は 1 枚目の写し
    gen = eq.data.get("generation") if isinstance(eq.data.get("generation"), dict) else {}
    return {"generated_glb": "generated.glb" in names, "prompt": bool(gen.get("input_text")),
            "input_images": numbered or (1 if "input.png" in names else 0),
            "conversion_report": "conversion_report.json" in names}


def _carry_over_local(mine, new, new_lib) -> list[str]:
    """置き換える装備（mine）の手元にしか無いもの（source\\generated.glb・conversion_report.json・入力画像とプロンプト）を、
    取り込む装備（展開した一時ライブラリの new）に写す。3D モデル・テクスチャ（differences の "assets"）が同じときだけ
    呼ぶ（違うときに古い generated.glb を付けると「変換し直す」で黙って古いモデルに戻るため。2026-10-17 監査 C95）。
    入力は手元の組（プロンプト・入力画像・その記録）をそのまま使う。写したものの名前の一覧を返す。"""
    sdir, ddir = os.path.join(mine.dir, "source"), os.path.join(new.dir, "source")
    names = sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []
    os.makedirs(ddir, exist_ok=True)
    g0 = mine.data.get("generation") if isinstance(mine.data.get("generation"), dict) else {}
    g1 = new.data.setdefault("generation", {})
    done = []
    if "generated.glb" in names:
        shutil.copyfile(os.path.join(sdir, "generated.glb"), os.path.join(ddir, "generated.glb"))
        g1["source_glb"] = {"path": "source/generated.glb"}
        done.append("generated.glb")
    if "conversion_report.json" in names:
        shutil.copyfile(os.path.join(sdir, "conversion_report.json"), os.path.join(ddir, "conversion_report.json"))
        new.data.setdefault("conversion", {})["report"] = "source/conversion_report.json"
        done.append("conversion_report.json")
    inputs = [n for n in names if INPUT_FILE_RE.match(n)]
    if inputs or g0.get("input_text"):
        for n in os.listdir(ddir):              # パッケージに入っていた入力は使わない（手元の組と混ぜない）
            if INPUT_FILE_RE.match(n):
                os.remove(os.path.join(ddir, n))
        for n in inputs:
            shutil.copyfile(os.path.join(sdir, n), os.path.join(ddir, n))
        for k in ("input_text", "input_image", "input_images"):
            g1[k] = copy.deepcopy(g0.get(k))
        done += inputs + (["input_text"] if g0.get("input_text") else [])
    if done:
        new_lib.save(new, touch=False)
    return done


def validate_record(data: dict, eq_id: str, pkg_files: dict, package_ids: set[str]) -> list[str]:
    """受け取った装備の記録の検査（作った人のツールが壊れていた・書き換えられた記録で、ゲームのデータを
    上書きしたり、ほかの装備のファイルを指したりしないように）。問題の一覧（空なら問題なし）。"""
    from modbuilder.builder import CREATED_AT_MAX, CREATED_AT_MIN
    from modbuilder.library import (DESC_MAX, EQUIPMENT_FORMAT, FORMAT_VERSION, GENERATION_METHODS, NAME_MAX,
                                    validate_display_name)
    from modbuilder.naming import HEAD_TEXTURE_FIELDS, ID_RE, derive_naming
    p: list[str] = []
    if data.get("format") != EQUIPMENT_FORMAT or data.get("id") != eq_id or not ID_RE.match(str(eq_id)):
        return [tr(f"{eq_id}: 装備の記録の形式・ID が不正です", f"{eq_id}: the equipment record has an invalid format or ID")]
    try:
        if int(data.get("version", 0)) > FORMAT_VERSION:
            return [tr(f"{eq_id}: 新しい版の装備の記録です", f"{eq_id}: the equipment record is from a newer version")]
    except (TypeError, ValueError):
        return [tr(f"{eq_id}: 装備の記録の版が不正です", f"{eq_id}: the equipment record has an invalid version")]
    # 項目の型（違うと以下の .get で落ちて INTERNAL になっていた。2026-10-17 監査 C96）
    bad_types = [k for k in ("naming", "display_name", "generation", "illusion_set", "conversion", "assets", "texts")
                 if data.get(k) is not None and not isinstance(data[k], dict)]
    a = data.get("assets") or {}
    if isinstance(a, dict):
        if a.get("files") is not None and not (isinstance(a["files"], list) and all(isinstance(f, dict) for f in a["files"])):
            bad_types.append("assets.files")
        if a.get("head_textures") is not None and not isinstance(a["head_textures"], dict):
            bad_types.append("assets.head_textures")
    # 入れ子の項目・mod・errors・updated_at の型も確かめる（違うと取り込んだ後に装備一覧・MOD の作成・取り込みが例外で
    # 止まった。正規の書き出し（sanitized_record）はどれもこの形。2026-10-20 レビュー R12A-02・R12B-01）
    if not isinstance(data.get("mod"), dict):
        bad_types.append("mod")
    if data.get("errors") is not None and not isinstance(data["errors"], list):
        bad_types.append("errors")
    if data.get("updated_at") is not None and not isinstance(data["updated_at"], str):
        bad_types.append("updated_at")
    gen = data.get("generation") or {}
    if isinstance(gen, dict):
        if gen.get("input_image") is not None and not isinstance(gen["input_image"], dict):
            bad_types.append("generation.input_image")
        ims = gen.get("input_images")
        if ims is not None and not (isinstance(ims, list) and len(ims) <= 4 and all(isinstance(r, dict) for r in ims)):
            bad_types.append("generation.input_images")
        if gen.get("input_text") is not None and not isinstance(gen["input_text"], str):
            bad_types.append("generation.input_text")
        if gen.get("settings") is not None and not isinstance(gen["settings"], dict):
            bad_types.append("generation.settings")
    conv = data.get("conversion") or {}
    if isinstance(conv, dict):
        sm = conv.get("summary")
        if sm is not None and not (isinstance(sm, dict) and all(sm.get(k) is None or isinstance(sm[k], dict)
                                                                for k in ("m", "f", "quality_flags"))):
            bad_types.append("conversion.summary")
        w = conv.get("warnings")
        if w is not None and not (isinstance(w, list) and all(isinstance(x, str) for x in w)):
            bad_types.append("conversion.warnings")
        if conv.get("report") not in (None, "source/conversion_report.json"):
            bad_types.append("conversion.report")
    if bad_types:
        return [tr(f"{eq_id}: 装備の記録の項目の形式が不正です: {', '.join(bad_types)}",
                   f"{eq_id}: fields of the equipment record have an invalid format: {', '.join(bad_types)}")]
    # naming は内部IDから決まるもの（scheme id-v1）と完全に一致すること。asset_prefix・scheme・知らないキーも含めて
    # 比べる（違うと、取り込んだ装備の再生成でデータフォルダーの外や別の装備のファイルに書かれた。2026-10-17 監査 C13）
    naming = data.get("naming") or {}
    expected = derive_naming(eq_id)
    for k in sorted(set(naming) | set(expected)):
        if naming.get(k) != expected.get(k):
            p.append(tr(f"{eq_id}: レコードのパス・タグ名・アセットの名前（{k}）が内部ID から決まるものと違います",
                        f"{eq_id}: the record path, tag name or asset name ({k}) differs from the one the internal ID determines")
                     if k in expected else tr(f"{eq_id}: 装備の記録の naming に知らない項目があります: {k}",
                                              f"{eq_id}: unknown field in the naming of the equipment record: {k}"))
    dn = data.get("display_name") or {}
    p += [tr(f"{eq_id}: 英語名: {x}", f"{eq_id}: English name: {x}") for x in validate_display_name(dn.get("en") or "", "en")]
    if dn.get("ja"):
        p += [tr(f"{eq_id}: 日本語名: {x}", f"{eq_id}: Japanese name: {x}") for x in validate_display_name(dn["ja"], "ja")]
    # 表示する文（texts）の役割ごとの名前。head は頭装備の名前の上書き（画面では変えない。modbuilder の records.head_text）、
    # set・set_desc は消耗品の名前・説明文（2026-11-18 点検 backend_modpkg-02）
    roles = {"head": tr("頭装備の名前の上書き", "head item name override"),
             "set": tr("消耗品の名前", "consumable name"), "set_desc": tr("消耗品の説明文", "consumable description")}
    for role, langs in (data.get("texts") or {}).items():
        if role not in roles or not isinstance(langs, dict):
            p.append(tr(f"{eq_id}: 表示する文（texts）の項目が不正です: {role}", f"{eq_id}: invalid text field (texts): {role}"))
            continue
        for lang, v in langs.items():
            if v is not None:
                p += [tr(f"{eq_id}: {roles[role]}（texts.{role}.{lang}）: {x}",
                         f"{eq_id}: {roles[role]} (texts.{role}.{lang}): {x}")
                      for x in validate_display_name(v, lang, DESC_MAX if role == "set_desc" else NAME_MAX)]
    if gen.get("method") not in GENERATION_METHODS:
        p.append(tr(f"{eq_id}: 生成方法が不正です", f"{eq_id}: invalid generation method"))
    # 入力画像の stored_path は、パッケージに入っている source/input*.png だけ（正規の書き出しは入れなかったものを None に
    # する）。装備のフォルダーの外（絶対パス・「..」・UNC）を入力画像として表示・再生成に使わないように（レビュー R12A-03）
    for r in ([gen["input_image"]] if isinstance(gen.get("input_image"), dict) else []) + list(gen.get("input_images") or []):
        sp = r.get("stored_path")
        if sp is not None and not (isinstance(sp, str) and sp.startswith("source/")
                                   and INPUT_FILE_RE.match(sp[len("source/"):]) and f"equipment/{eq_id}/{sp}" in pkg_files):
            p.append(tr(f"{eq_id}: 入力画像の場所が不正です: {str(sp)[:80]}", f"{eq_id}: invalid input image location: {str(sp)[:80]}"))
    # 作成日時は本ツールが書く形（文字列、日付・時刻・タイムゾーン付きの ISO 8601）だけ。数値などは装備一覧の並べ替え
    # （created_at の比較）で TypeError になった（レビュー R12A-02）。MOD の作成と同じ固定の範囲（2000〜2999 年。3001 年
    # 以降はビルドの時刻の計算で止まった）の外も不正にする（2026-10-27 点検 R06B-01）
    ca = data.get("created_at")
    try:
        t = _dt.datetime.fromisoformat(ca) if isinstance(ca, str) else None
        if t is None or t.tzinfo is None or not CREATED_AT_MIN <= t < CREATED_AT_MAX:
            raise ValueError(ca)
    except (ValueError, OverflowError):
        p.append(tr(f"{eq_id}: 作成日時が不正です", f"{eq_id}: invalid creation time"))
    owner = (data.get("illusion_set") or {}).get("owner")
    if not isinstance(owner, str) or (owner != eq_id and owner not in package_ids):
        p.append(tr(f"{eq_id}: 消耗品をまとめている装備 {owner} がパッケージにありません",
                    f"{eq_id}: the equipment {owner} that groups the consumable is not in the package"))
    if (data.get("conversion") or {}).get("status") != "converted":
        p.append(tr(f"{eq_id}: 変換済みの装備ではありません", f"{eq_id}: the equipment has not been converted"))
    a = data.get("assets") or {}
    game_paths = set()
    # アセットの名前は内部IDから決まるもの（<ID>_m.msh・_f.msh・_dif.tex）、ゲーム内パスは <asset_dir>/<名前> だけ。
    # CON.msh などの予約デバイス名・同じゲーム内パスの重複（取り込めても MOD の作成が毎回失敗した）を通さない
    # （2026-10-27 検証 R12A-07・R12A-02）
    names_ok = {expected["asset_prefix"] + s for s in ASSET_SUFFIXES}
    seen_names = set()
    for f in a.get("files") or []:
        name, gp = str(f.get("file") or ""), str(f.get("game_path") or "").replace("\\", "/").lower()
        if not ASSET_FILE_RE.match(name) or name not in names_ok or gp != f"{expected['asset_dir']}/{name}".lower():
            p.append(tr(f"{eq_id}: アセットの名前・ゲーム内パスが不正です: {name} → {gp}",
                        f"{eq_id}: invalid asset name or in-game path: {name} → {gp}"))
            continue
        if name.lower() in seen_names or gp in game_paths:
            p.append(tr(f"{eq_id}: アセットのファイル名・ゲーム内パスが重複しています: {name} → {gp}",
                        f"{eq_id}: duplicate asset file name or in-game path: {name} → {gp}"))
            continue
        seen_names.add(name.lower())
        rec = pkg_files.get(f"equipment/{eq_id}/assets/{name}")
        if rec is None:
            p.append(tr(f"{eq_id}: アセット {name} がパッケージにありません", f"{eq_id}: asset {name} is not in the package"))
        elif rec["sha256"] != f.get("sha256") or rec["size"] != f.get("size"):
            p.append(tr(f"{eq_id}: アセット {name} が装備の記録（SHA-256）と一致しません",
                        f"{eq_id}: asset {name} does not match the equipment record (SHA-256)"))
        elif rec["size"] > ASSET_MAX[os.path.splitext(name)[1].lower()]:
            p.append(tr(f"{eq_id}: アセット {name} が大きすぎます（{rec['size']:,} B）",
                        f"{eq_id}: asset {name} is too large ({rec['size']:,} B)"))
        game_paths.add(gp)
    for key in ("male_mesh", "female_mesh"):
        if str(a.get(key) or "").replace("\\", "/").lower() not in game_paths:
            p.append(tr(f"{eq_id}: {key} がアセットにありません", f"{eq_id}: {key} is not among the assets"))
    for k, v in (a.get("head_textures") or {}).items():
        if k not in HEAD_TEXTURE_FIELDS or str(v).replace("\\", "/").lower() not in game_paths:
            p.append(tr(f"{eq_id}: head_textures の項目が不正です: {k}", f"{eq_id}: invalid head_textures entry: {k}"))
    allowed = {"equipment.json"} | {f"assets/{f.get('file')}" for f in a.get("files") or []} | {f"source/{s}" for s in SOURCE_FILES}
    prefix = f"equipment/{eq_id}/"
    extra = []
    for n in pkg_files:
        if not n.startswith(prefix):
            continue
        rel = n[len(prefix):]
        if rel not in allowed and not (rel.startswith("source/") and INPUT_FILE_RE.match(rel[len("source/"):])):
            extra.append(rel)
    if extra:
        p.append(tr(f"{eq_id}: 想定外のファイルがあります: {extra[:3]}", f"{eq_id}: unexpected files: {extra[:3]}"))
    return p


def _short(v):
    """確認の一覧に写す値: 文字列なら長さを切ったもの、それ以外は None（大きな値を一覧・応答に残さない。X5X-03）。"""
    return v[:ITEM_TEXT_MAX] if isinstance(v, str) else None


def _display_fields(dn: dict, gen: dict, data: dict) -> dict:
    """検査を通らない項目（dev_only・invalid）の表示の値を 1 行の短い文にする（名前は validate_record で確かめていない。
    2026-10-27 点検 R19B-01）。"""
    return {"name": package_io.display_text(dn.get("en"), 80), "name_ja": package_io.display_text(dn.get("ja"), 80),
            "created_at": package_io.display_text(data.get("created_at")), "method": package_io.display_text(gen.get("method"))}


def _classify(lib, pkg) -> list[dict]:
    """パッケージの装備ごとの状態: new / same / different / conflict / dev_only / invalid。
    装備の記録は 1 件ずつ読んで検査し、読んだ記録は残さない（2026-10-27 検証 X5X-03）。"""
    from modbuilder.errors import LibraryError
    listed = pkg.manifest.get("equipment")
    if not isinstance(listed, list) or not listed:
        raise package_io._err("broken", tr("装備の一覧がありません", "The equipment list is missing"))
    ids = [str((x or {}).get("id")) for x in listed if isinstance(x, dict)]
    dirs = sorted({n.split("/")[1] for n in pkg.files if n.startswith("equipment/") and n.count("/") >= 2})
    if sorted(ids) != dirs or len(set(ids)) != len(ids) or len(ids) > MAX_ITEMS:
        raise package_io._err("tampered", tr("装備の一覧と中身が合いません", "The equipment list does not match the contents"))
    stray = [n for n in pkg.files if not any(n.startswith(f"equipment/{i}/") for i in ids)]
    if stray:
        raise package_io._err("tampered", tr(f"想定外のファイルがあります: {stray[:3]}", f"Unexpected files: {stray[:3]}"))
    known = set(lib.ids())
    out = []
    for eq_id in ids:
        data = pkg.read_json(f"equipment/{eq_id}/equipment.json", RECORD_MAX)
        dn = data.get("display_name") if isinstance(data.get("display_name"), dict) else {}
        gen = data.get("generation") if isinstance(data.get("generation"), dict) else {}     # 型の違いは validate_record で invalid
        item = {"id": eq_id, "name": _short(dn.get("en")), "name_ja": _short(dn.get("ja")),
                "created_at": _short(data.get("created_at")),
                "method": _short(gen.get("method")), "has_prompt": bool(gen.get("input_text")),
                "input_images": sum(1 for n in pkg.files if n.startswith(f"equipment/{eq_id}/source/input")),
                "differences": [], "problems": []}
        if data.get("dev_only") or gen.get("method") == "prototype":
            item["status"] = "dev_only"
            item.update(_display_fields(dn, gen, data))
        else:
            problems = validate_record(data, eq_id, pkg.files, set(ids))
            if problems:
                item.update(status="invalid", problems=problems, **_display_fields(dn, gen, data))
            elif eq_id in known:
                mine = lib.load(eq_id)
                diff = differences(mine.data, data)
                item.update(status="different" if diff else "same", differences=diff,
                            local={"name": mine.name_en, "name_ja": mine.name_ja, "created_at": mine.created_at,
                                   "mod": (mine.data.get("mod") or {}).get("status")})
                if diff:
                    # 置き換えたときに手元の生成結果・入力を引き継ぐか（3D モデル・テクスチャが同じときだけ。C95）
                    item.update(local_only=local_only(mine), keeps_local="assets" not in diff)
            else:
                try:
                    lib._check_naming_unique(data["naming"], eq_id)
                    item["status"] = "new"
                except LibraryError as e:
                    item.update(status="conflict", problems=[str(e)])
        out.append(item)
    return out


def inspect(svc, path: str) -> dict:
    """読み込みの前の確認（ファイルの検査と、装備ライブラリとの比較）。何も変更しない。
    extracted_size は展開後の大きさの合計（取り込みで書かれる量の目安。2026-10-27 検証 R12B-06）。"""
    with package_io.open_package(path, KIND) as pkg:
        lib = _lib(svc)
        items = _classify(lib, pkg)
        m = pkg.manifest
        extracted = sum(r["size"] for r in pkg.files.values())
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("new", "same", "different", "conflict", "dev_only", "invalid")}
    blocking = [p for i in items for p in i["problems"]]
    out = {"kind": KIND, "path": os.path.abspath(path), "file_name": os.path.basename(path), "size": os.path.getsize(path),
           "extracted_size": extracted, "package_id": m["package_id"],
           "created_at": package_io.display_text(m.get("created_at")),      # 1 行の短い文（点検 R19B-01）
           "tool": package_io.display_tool(m.get("tool")),
           "include_inputs": bool(m.get("include_inputs")), "items": items, "counts": counts,
           "can_import": not blocking and (counts["new"] + counts["different"]) > 0, "blocking": blocking}
    logs.info(tr(f"装備パッケージを確認しました: package {m['package_id']}、",
                 f"Checked the equipment package: package {m['package_id']}, ")
              + tr("、", ", ").join(f"{k} {v}" for k, v in counts.items() if v))
    return out


# ---------------------------------------------------------------------------
# 取り込み
# ---------------------------------------------------------------------------
def run_import(svc, ctx, params: dict) -> dict:
    """ジョブ import_equipment: {"path", "package_id"（確認したときの値）, "replace": [置き換える装備ID]}。"""
    from modbuilder.errors import LibraryError
    try:
        return _run_import(svc, ctx, params)
    except LibraryError as e:
        raise BackendError("INVALID_INPUT", tr(f"装備一覧に取り込めません: {e}", f"Cannot import into the Library: {e}"),
                           params={"reason": "package_library"})


def _remove_dir_retry(d: str, tries: int = 5, wait: float = 0.2) -> bool:
    """フォルダーを消す（ウイルス対策ソフトなどが一時的に開いているファイルのため、少し待って数回やり直す）。
    消えた（初めから無い）なら True。"""
    for k in range(tries):
        if not os.path.lexists(d):
            return True
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.lexists(d):
            return True
        if k + 1 < tries:
            time.sleep(wait)
    return False


def _rollback_import(lib, trashed: list[tuple[str, str]], to_add: list[str]) -> list[dict]:
    """取り込みの失敗の後始末: 置き換えかけた装備を trash から元に戻し、新しく入れた分を消す（全部か、何もしないか）。
    装備ごとに最後まで続け、戻せなかったもの [{"id", "trash"（戻せなかった元の装備の trash のフォルダー名）, "left"（残った
    フォルダー）}] を返す。取り込みかけのフォルダーが消し切れないときは、元の装備をその中に入れ子で移さず trash に残す
    （以前は shutil.move が入れ子に移し、装備が一覧からも trash からも見えなくなった。レビュー R12B-04）。"""
    out = []
    for eq_id, t in trashed:
        d = os.path.join(lib.equipment_root, eq_id)
        try:
            if not _remove_dir_retry(d):
                out.append({"id": eq_id, "trash": os.path.basename(t), "left": d})
                continue
            os.rename(t, d)
        except OSError as e:
            logs.warning(tr(f"置き換えかけた装備 {eq_id} を元に戻せません: {type(e).__name__}: {e}",
                            f"Cannot put back the equipment {eq_id} that was being replaced: {type(e).__name__}: {e}"))
            out.append({"id": eq_id, "trash": os.path.basename(t), "left": d if os.path.lexists(d) else None})
    for eq_id in to_add:
        d = os.path.join(lib.equipment_root, eq_id)
        if not _remove_dir_retry(d):
            out.append({"id": eq_id, "trash": None, "left": d})
    return out


def _run_import(svc, ctx, params: dict) -> dict:
    from modbuilder.errors import LibraryError
    from modbuilder.importers import import_from_library
    from modbuilder.library import Library, now_iso
    path = str(params.get("path") or "")
    replace = set(params.get("replace") or [])
    with svc.mod_lock:
        ctx.progress(0.02, "package_verify")
        with package_io.open_package(path, KIND) as pkg:
            if params.get("package_id") and params["package_id"] != pkg.package_id:
                raise package_io._err("changed", tr("確認した後に共有ファイルが変わりました。もう一度読み込んでください",
                                                    "The share file changed after it was checked. Import it again"))
            lib = _lib(svc)
            items = _classify(lib, pkg)
            bad = [p for i in items for p in i["problems"]]
            if bad:
                raise package_io._err("invalid_items", tr("取り込めない装備があります: ", "Some equipment cannot be imported: ")
                                      + "; ".join(bad[:5]), problems=bad[:20])
            to_add = [i["id"] for i in items if i["status"] == "new"]
            to_replace = [i["id"] for i in items if i["status"] == "different" and i["id"] in replace]
            keeps_local = {i["id"] for i in items if i.get("keeps_local")}
            skipped = [{"id": i["id"], "status": i["status"]} for i in items if i["id"] not in to_add + to_replace]
            if not to_add and not to_replace:
                return {"imported": [], "replaced": [], "skipped": skipped, "package_id": pkg.package_id, "kept_local": []}
            ctx.progress(0.3, "package_extract")
            tmp = package_io.scratch_dir(svc.home(), "import")
            try:
                src_root = os.path.join(tmp, "library")
                src = Library.open(src_root, create=True)
                for eq_id in to_add + to_replace:
                    prefix = f"equipment/{eq_id}/"
                    for n in pkg.files:
                        if n.startswith(prefix):
                            dest = os.path.join(src.equipment_root, eq_id, *n[len(prefix):].split("/"))
                            pkg.extract(n, dest)
                    ctx.check_cancel()
                # 置き換える装備のうち 3D モデル・テクスチャが同じものは、手元にしか無い生成結果・入力を引き継ぐ
                # （展開した一時ライブラリの側に写すので、失敗しても手元の装備には触れない。C95）
                kept_local = {}
                for eq_id in to_replace:
                    if eq_id in keeps_local:
                        got = _carry_over_local(lib.load(eq_id), src.load(eq_id), src)
                        if got:
                            kept_local[eq_id] = got
                ctx.progress(0.7, "package_import")
                # 記録（equipment.json）の無い装備のフォルダーが残っていれば取り込まない（一覧に無いので new になるが、
                # 取り込みの失敗の後始末がそのフォルダーごと消すため。前の戻しで残ったものなど。レビュー R12B-04）
                left = [i for i in to_add if os.path.lexists(os.path.join(lib.equipment_root, i))]
                if left:
                    raise LibraryError(tr(f"装備のフォルダーが残っています（記録がありません）: {left}。"
                                          "フォルダーを確かめて片付けてから、もう一度取り込んでください",
                                          f"Equipment folders are left over (without records): {left}. "
                                          "Check and clean up the folders, then import again"))
                if to_add:
                    import_from_library(lib, src, to_add)
                trashed = []
                try:
                    for eq_id in to_replace:
                        t = lib.remove(eq_id)
                        trashed.append((eq_id, t))
                        import_from_library(lib, src, [eq_id])
                except Exception as e:
                    unrestored = _rollback_import(lib, trashed, to_add)
                    if not unrestored:
                        raise
                    names = ", ".join(tr(f"{u['id']}（trash\\{u['trash']}）", f"{u['id']} (trash\\{u['trash']})")
                                      if u.get("trash") else u["id"] for u in unrestored)
                    logs.error(tr(f"装備パッケージの取り込みの失敗の後、元に戻せなかった装備があります: {names}",
                                  f"Some equipment could not be put back after the equipment package import failed: {names}"))
                    p0 = e.params if isinstance(e, BackendError) else {}
                    code = e.code if isinstance(e, BackendError) else "INVALID_INPUT" if isinstance(e, LibraryError) else "INTERNAL"
                    why = strip_end_period(f"{type(e).__name__}: {e}")      # 後ろに句点が続くので重ねない（B-038 と同じ）
                    raise BackendError(code,
                                       tr(f"{why}。取り込みに失敗し、元に戻せなかった装備があります: {names}"
                                          "（残ったフォルダーを片付けてから trash から戻してください）",
                                          f"{why}. The import failed and some equipment could not be put back: "
                                          f"{names} (clean up the leftover folders, then restore the equipment from trash)"),
                                       params={**p0, "reason": "package_rollback", "unrestored": unrestored}) from e
                stamp = now_iso()
                for eq_id in to_add + to_replace:
                    eq = lib.load(eq_id)
                    eq.data["imported_from"] = {"package": os.path.basename(path), "package_id": pkg.package_id,
                                                "at": stamp, "tool": package_io.display_tool(pkg.manifest.get("tool")),
                                                **({"replaced": True} if eq_id in to_replace else {}),
                                                **({"kept_local": kept_local[eq_id]} if eq_id in kept_local else {})}
                    lib.save(eq, touch=False)
            finally:
                package_io.remove_tree(tmp)
    logs.info(tr(f"装備パッケージを取り込みました: 新規 {len(to_add)}、置き換え {len(to_replace)}、飛ばした {len(skipped)}",
                 f"Imported the equipment package: {len(to_add)} new, {len(to_replace)} replaced, {len(skipped)} skipped")
              + (tr(f"、手元の生成結果・入力を引き継いだ装備 {len(kept_local)}",
                    f", {len(kept_local)} kept their local generation results and inputs") if kept_local else "")
              + tr(f"（package {pkg.package_id}）", f" (package {pkg.package_id})"))
    ctx.progress(1.0, "package_import_done")
    return {"imported": to_add, "replaced": to_replace, "skipped": skipped, "package_id": pkg.package_id,
            "kept_local": sorted(kept_local)}
