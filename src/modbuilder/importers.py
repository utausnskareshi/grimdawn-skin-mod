"""装備ライブラリへの取り込み。

* :func:`import_converted` — gdconvert の変換結果（``<名前>_m.msh``、``<名前>_f.msh``、``<名前>_dif.tex``、
  ``<名前>_report.json``、``<名前>_preview.png``）を新しい装備として取り込む。メッシュ内蔵マテリアルの
  テクスチャパス（gdconvert は ``Items/<namespace>/<名前>_dif.tex`` を書く）を、内部IDから決めた置き場所
  ``Items/aiskin/<ID>/<ID>_dif.tex`` に付け替える（ゲーム内で確認済みの大文字 "Items/" の書き方を保つ）。
* :func:`replace_converted` — 既存の装備の変換結果を差し替える（再生成。ID・レコードのパスは変わらない）。
* :func:`import_prototype` — 試作MOD（``prototype/mod03`` など、``database/records`` と ``resources`` を持つ
  手作業のソース）から装備を復元する。レコードのパス・タグ名・アセットのパスは試作のまま固定する
  （ゲーム内の収集に登録済みの試作のイリュージョンをそのまま使えるように）。
* :func:`import_from_library` — 別の装備ライブラリの装備を、内部ID・名前（レコードのパス・タグ）・作成日時・アセットの
  バイト列をそのまま保って取り込む（そのライブラリから作って導入済みの MOD と同じレコードになり、収集が保たれる。D-021）。
"""
from __future__ import annotations

import copy
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shutil

from . import assets as A
from . import paths
from ._i18n import tr
from .arz import norm_path, read_dbr, split_array
from .errors import LibraryError
from .gamedata import parse_tags
from .library import Equipment, Library, _move_dir, now_iso
from .naming import (HEAD_TEXTURE_FIELDS, ID_RE, INVISIBLE_RECORDS, INVISIBLE_SLOTS, MERCHANT_RECORDS, NAMESPACE,
                     OFFICIAL_INVISIBLE_MEDAL, OFFICIAL_INVISIBLE_SHOULDERS, SCHEME, derive_naming)

__all__ = ["find_converted", "generation_from_manifest", "import_converted", "replace_converted", "import_prototype",
           "import_from_library"]


def _read_record_json(path: str) -> dict:
    """変換結果の記録（manifest.json・<名前>_report.json）を読む。壊れていれば、どのファイルかの分かる LibraryError
    （以前は生の JSONDecodeError で、どのファイルが壊れているかが出なかった。文に「見つかりません」（英語の文は
    "not found"）を含めない: バックエンドが NOT_FOUND に振り分けるため。R08B-02）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LibraryError(tr(f"変換結果の記録を読めません（壊れています）: {path}: {e}",
                              f"Cannot read the conversion record (it is corrupted): {path}: {e}")) from None
    if not isinstance(data, dict):
        raise LibraryError(tr(f"変換結果の記録を読めません（壊れています）: {path}: JSON のオブジェクトではありません",
                              f"Cannot read the conversion record (it is corrupted): {path}: not a JSON object"))
    return data


def _check_shape(path: str, checks) -> None:
    """記録の中の項目の形を確かめる。checks = [(項目名, 値, 許す型)]（None は常に許す）。違えば _read_record_json と
    同じ形の、ファイルの分かる LibraryError（形を確かめずに .get などを呼び、AttributeError のトレースバックで落ちた。
    2026-10-27 R08B-01・R08A-03）。"""
    for key, value, types in checks:
        if value is not None and not isinstance(value, types):
            raise _shape_error(path, key)


def _shape_error(path: str, key: str) -> LibraryError:
    # 文に「見つかりません」（英語の文は "not found"）を含めない（_read_record_json と同じ）
    return LibraryError(tr(f"変換結果の記録を読めません（壊れています）: {path}: {key} の形が違います",
                           f"Cannot read the conversion record (it is corrupted): {path}: {key} has the wrong shape"))


def _check_manifest_shape(path: str, man: dict) -> None:
    """manifest.json の使う項目（files・quality_flags・load_flags）の形。"""
    files = man.get("files")
    _check_shape(path, [("files", files, dict)])
    for role in ("msh_m", "msh_f", "tex"):
        rec = (files or {}).get(role)
        _check_shape(path, [(f"files.{role}", rec, dict)])
        _check_shape(path, [(f"files.{role}.md5", (rec or {}).get("md5"), str)])
    flags = man.get("quality_flags")
    _check_shape(path, [("quality_flags", flags, dict), ("load_flags", man.get("load_flags"), list)])
    for k, v in (flags or {}).items():
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise _shape_error(path, f"quality_flags.{k}")


def _check_report_shape(path: str, report: dict) -> None:
    """<名前>_report.json の使う項目（input・steps・config.axes）の形。"""
    steps, config = report.get("steps"), report.get("config")
    _check_shape(path, [("input", report.get("input"), str), ("steps", steps, dict), ("config", config, dict)])
    _check_shape(path, [("config.axes", (config or {}).get("axes"), dict)])
    tex = (steps or {}).get("texture")
    _check_shape(path, [("steps.texture", tex, dict)])
    _check_shape(path, [("steps.texture.game_path", (tex or {}).get("game_path"), str)])
    for g in ("gender_m", "gender_f"):
        st = (steps or {}).get(g)
        _check_shape(path, [(f"steps.{g}", st, dict)])
        _check_shape(path, [(f"steps.{g}.{k}", (st or {}).get(k), dict) for k in ("msh", "fit")])


def find_converted(src: str, source_name: str | None = None) -> dict:
    """変換結果のファイルを探す → {name, male, female, texture, report, preview}。"""
    if os.path.isfile(src):
        raise LibraryError(tr(f"変換結果のフォルダーを指定してください: {src}",
                              f"Specify the folder of the conversion results: {src}"))
    if source_name is None:
        cands = []
        # フォルダー名の [ ] をワイルドカードとして扱わない（R08A-08）
        for p in glob.glob(os.path.join(glob.escape(src), "*_m.msh")):
            n = os.path.basename(p)[:-len("_m.msh")]
            if os.path.isfile(os.path.join(src, n + "_f.msh")) and os.path.isfile(os.path.join(src, n + "_dif.tex")):
                cands.append(n)
        if not cands:
            # 英語の文は "not found" を含める（日本語の文の「見つかりません」と対。アプリの NOT_FOUND の振り分け）
            raise LibraryError(tr(f"{src} に変換結果（<名前>_m.msh / _f.msh / _dif.tex）が 見つかりません（--source-name で指定）",
                                  f"Conversion results (<name>_m.msh / _f.msh / _dif.tex) not found in {src} "
                                  f"(specify with --source-name)"))
        if len(cands) != 1:
            raise LibraryError(tr(f"{src} に変換結果（<名前>_m.msh / _f.msh / _dif.tex）が {len(cands)} 組あります: "
                                  f"{', '.join(sorted(cands))}（--source-name で指定）",
                                  f"{src} has {len(cands)} sets of conversion results "
                                  f"(<name>_m.msh / _f.msh / _dif.tex): {', '.join(sorted(cands))} "
                                  f"(specify one with --source-name)"))
        source_name = cands[0]
    out = {"name": source_name}
    for key, suffix, required in (("male", "_m.msh", True), ("female", "_f.msh", True), ("texture", "_dif.tex", True),
                                  ("report", "_report.json", False), ("preview", "_preview.png", False)):
        p = os.path.join(src, source_name + suffix)
        if not os.path.isfile(p):
            if required:
                raise LibraryError(tr(f"変換結果がありません: {p}", f"Conversion result missing: {p}"))
            p = None
        out[key] = p
    # build/converted/<名前>/manifest.json（変換の一括実行が書く記録）があれば、md5 を照合して生成情報を読む
    mp = os.path.join(src, "manifest.json")
    out["manifest"] = None
    if os.path.isfile(mp):
        man = _read_record_json(mp)
        if man.get("name") == source_name:
            _check_manifest_shape(mp, man)
            for key, role in (("male", "msh_m"), ("female", "msh_f"), ("texture", "tex")):
                rec = (man.get("files") or {}).get(role)
                if rec and rec.get("md5"):
                    with open(out[key], "rb") as fh:
                        if hashlib.md5(fh.read()).hexdigest() != rec["md5"]:
                            raise LibraryError(tr(f"変換結果が manifest.json の記録と一致しません: {out[key]}",
                                                  f"The conversion result does not match the record in manifest.json: "
                                                  f"{out[key]}"))
            out["manifest"] = man
    return out


def generation_from_manifest(man: dict | None) -> dict:
    """manifest.json の生成情報（プロンプト・シード・入力 GLB）→ equipment の generation。"""
    if not man:
        return {}
    gen = {}
    if man.get("prompt"):
        gen["method"] = "text"
        gen["input_text"] = man["prompt"]
    if man.get("image"):
        gen["method"] = "image"
        gen["input_image"] = {"original_path": man["image"], "stored_path": None, "sha256": None}
    for k in ("seed", "model_id"):
        if man.get(k) is not None:
            gen[k] = man[k]
    if man.get("input_glb"):
        gen["source_glb"] = {"path": man["input_glb"]}
    return gen


def _conversion_summary(report: dict | None) -> dict:
    if not report:
        return {}
    # 形は _check_report_shape で確かめ済み（辞書か None）。None の項目は空の辞書として扱う
    s = report.get("steps") or {}
    out = {"input": report.get("input"), "name": report.get("name")}
    tex = s.get("texture") or {}
    out["texture"] = {"size": tex.get("size"), "verify": tex.get("verify")}
    for g in ("m", "f"):
        st = s.get(f"gender_{g}")
        if st:
            fit, ms = st.get("fit") or {}, st.get("msh") or {}
            out[g] = {"vertices": ms.get("vertices"), "triangles": ms.get("triangles"),
                      "bones": ms.get("bones"), "bbox_min": ms.get("bbox_min"),
                      "bbox_max": ms.get("bbox_max"),
                      "fit_rms": [fit.get("rms_body_to_model_before"), fit.get("rms_body_to_model_after")]}
    out["front"] = ((report.get("config") or {}).get("axes") or {}).get("target_front")
    return out


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# 試作（scheme prototype）の装備のアセットの置き場所・名前の頭の規則（試作の items/aiskin2・items/aiskin3 など）
PROTO_ASSET_DIR_RE = re.compile(r"^items/[a-z0-9_]+(?:/[a-z0-9_]+)*$")
PROTO_ASSET_PREFIX_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


def _asset_names(eq: Equipment) -> tuple[str, str]:
    """差し替えで書くアセットの名前の頭と置き場所（ゲーム内パスのフォルダー）。

    scheme id-v1 の装備は記録の naming を使わず、内部IDから決める（受け取った装備の記録の asset_prefix・asset_dir に
    ``..``・絶対パスを入れて、データフォルダーの外や別の装備のファイルに書かせないように。2026-10-17 監査 C13）。
    それ以外（試作）は記録の値を規則で検査する（区切り・``..``・``:`` を含む名前の頭や、items/ の外の置き場所は拒否）。"""
    naming = eq.naming
    if naming.get("scheme") == SCHEME:
        if not ID_RE.match(eq.id):
            raise LibraryError(tr(f"装備IDの形式が不正です: {eq.id}", f"Invalid equipment ID format: {eq.id}"))
        d = derive_naming(eq.id)
        return d["asset_prefix"], d["asset_dir"]
    prefix = naming.get("asset_prefix") or eq.id
    adir = naming.get("asset_dir") or f"items/{NAMESPACE}/{eq.id}"
    if not isinstance(prefix, str) or not PROTO_ASSET_PREFIX_RE.match(prefix) or ".." in prefix:
        raise LibraryError(tr(f"装備 {eq.id} のアセットの名前の頭が不正です: {prefix!r}",
                              f"Invalid asset name prefix for equipment {eq.id}: {prefix!r}"))
    if not isinstance(adir, str) or not PROTO_ASSET_DIR_RE.match(adir):
        raise LibraryError(tr(f"装備 {eq.id} のアセットの置き場所が不正です: {adir!r}",
                              f"Invalid asset folder for equipment {eq.id}: {adir!r}"))
    return prefix, adir


def _load_converted_assets(eq: Equipment, found: dict) -> tuple[list, str, str, dict | None]:
    """変換結果を読み、テクスチャパスを付け替えた assets のリストを作る。"""
    report = None
    if found.get("report"):
        report = _read_record_json(found["report"])
        _check_report_shape(found["report"], report)
    male, female, tex = _read(found["male"]), _read(found["female"]), _read(found["texture"])
    errors, info = [], []
    A.check_tex(os.path.basename(found["texture"]), tex, errors, info)
    for key, data in ((found["male"], male), (found["female"], female)):
        A.check_msh(os.path.basename(key), data, set(), {}, lambda p: True, errors, [], info)
    if errors:
        raise LibraryError(tr(f"変換結果に問題があります: {'; '.join(errors)}",
                              f"There are problems with the conversion results: {'; '.join(errors)}"))
    prefix, adir = _asset_names(eq)
    new_tex_gp = f"{adir}/{prefix}_dif.tex"
    material_path = "Items/" + new_tex_gp.split("/", 1)[1]   # gdconvert と同じ書き方（ゲーム内で確認済み）
    # 旧テクスチャパス: 報告の game_path、無ければマテリアルからテクスチャのファイル名で探す
    old = None
    if report:
        old = ((report.get("steps") or {}).get("texture") or {}).get("game_path")
    from gdmesh import msh
    texs = [p for _mi, _pn, p in A.material_textures(msh.read_bytes(male, strict=True))]
    if not old:
        base = os.path.basename(found["texture"]).lower()
        old = next((p for p in texs if os.path.basename(norm_path(p)) == base), None)
    if not old or norm_path(old) not in [norm_path(p) for p in texs]:
        raise LibraryError(tr(f"メッシュのマテリアルが変換したテクスチャを参照していません（マテリアルのテクスチャ: {texs}）",
                              f"The mesh material does not refer to the converted texture (material textures: {texs})"))
    mapping = {norm_path(old): material_path}
    out_files = []
    for g, data in (("m", male), ("f", female)):
        new, n = A.retarget_textures(data, mapping)
        if n == 0:
            raise LibraryError(tr(f"{g} のメッシュのテクスチャパスを付け替えられませんでした（{old}）",
                                  f"Could not retarget the texture path of the {g} mesh ({old})"))
        out_files.append((f"{prefix}_{g}.msh", new, f"{adir}/{prefix}_{g}.msh"))
    out_files.append((f"{prefix}_dif.tex", tex, new_tex_gp))
    return out_files, f"{adir}/{prefix}_m.msh", f"{adir}/{prefix}_f.msh", report


def _store_sources(eq: Equipment, found: dict) -> dict:
    sdir = os.path.join(eq.dir, "source")
    os.makedirs(sdir, exist_ok=True)
    stored = {}
    for key, name in (("report", "conversion_report.json"), ("preview", "preview.png")):
        dst = os.path.join(sdir, name)
        if found.get(key):
            shutil.copyfile(found[key], dst)
            stored[key] = "source/" + name
        elif os.path.isfile(dst):
            os.remove(dst)          # 差し替えで新しい結果に無いもの。前の変換の確認画像を今の結果として見せない（R08B-04）
    return stored


def _record_conversion(library: Library, eq: Equipment, src: str, found: dict, report: dict | None) -> None:
    stored = _store_sources(eq, found)
    man = found.get("manifest") or {}
    summary = _conversion_summary(report)
    flags = man.get("quality_flags") or {}
    if flags:
        summary["quality_flags"] = flags
    # GLB の読み込みの注意（gdconvert の load_*。テクスチャの UV が無い、切り抜き・半透明を不透明にした、など）も
    # 品質の注意と並べる（装備一覧と「変換し直す」の前後の比較に出る）
    load_flags = [f for f in (man.get("load_flags") or []) if isinstance(f, str)]
    if load_flags:
        summary["load_flags"] = load_flags
    library.set_conversion(eq, status="converted", tool="gdconvert", source_dir=os.path.abspath(src),
                           source_name=found["name"], report=stored.get("report"),
                           converted_at=man.get("converted") or now_iso(), summary=summary, error=None,
                           warnings=sorted({f for v in flags.values() for f in v} | set(load_flags)))
    if stored.get("preview"):
        eq.data["conversion"]["preview"] = stored["preview"]
    else:
        eq.data["conversion"].pop("preview", None)


def import_converted(library: Library, src: str, name_en: str, name_ja: str | None = None,
                     source_name: str | None = None, generation: dict | None = None) -> Equipment:
    """gdconvert の変換結果を新しい装備として取り込む。generation が無ければ manifest.json・報告から埋める。"""
    found = find_converted(src, source_name)
    gen = {"method": "external"}
    gen.update(generation_from_manifest(found.get("manifest")))
    gen.update(generation or {})
    eq = library.create(name_en, name_ja, gen)
    try:
        files, gp_m, gp_f, report = _load_converted_assets(eq, found)
        if report and not eq.data["generation"].get("source_glb"):
            eq.data["generation"]["source_glb"] = {"path": report.get("input")}
        library.set_assets(eq, [(f, d, gp) for f, d, gp in files], gp_m, gp_f)
        _record_conversion(library, eq, src, found, report)
        library.save(eq)
    except BaseException:
        # 作りかけの装備を残さない。Ctrl+C（KeyboardInterrupt）でも消す（メッシュの無い装備が残り、以後のビルドが全部
        # 止まった。replace_converted と同じ扱い。2026-10-27 R08B-02）
        shutil.rmtree(eq.dir, ignore_errors=True)
        raise
    return eq


def replace_converted(library: Library, key: str, src: str, source_name: str | None = None) -> Equipment:
    """既存の装備のメッシュ・テクスチャを新しい変換結果に差し替える（再生成）。"""
    eq = library.find(key)
    if eq.naming.get("scheme") != "id-v1" and not eq.naming.get("asset_dir"):
        raise LibraryError(tr(f"装備 {eq.id} はアセットの置き場所を持たないため差し替えできません",
                              f"Equipment {eq.id} cannot be replaced because it has no asset folder"))
    found = find_converted(src, source_name)
    files, gp_m, gp_f, report = _load_converted_assets(eq, found)
    # 途中（アセットの入れ替え・source への写し・保存）で失敗したら、装備を差し替える前の状態に戻す（元のアセットが
    # 消え、記録と食い違ったまま残った。R08A-02）
    data0 = copy.deepcopy(eq.data)
    sdir = os.path.join(eq.dir, "source")
    sources0 = {name: (_read(os.path.join(sdir, name)) if os.path.isfile(os.path.join(sdir, name)) else None)
                for name in ("conversion_report.json", "preview.png")}
    swapped, old_assets = False, None
    try:
        # DBR のテクスチャ指定（head_textures。試作の装備の hero02.tex など）は外す。変換結果のメッシュは自前のテクスチャを
        # マテリアルに持つので、残すと新しいメッシュに古いテクスチャが当たる見込み（R08B-06）
        old_assets = library.set_assets(eq, files, gp_m, gp_f, {}, keep_backup=True)
        swapped = True
        gen = generation_from_manifest(found.get("manifest"))
        if not gen.get("source_glb") and report and report.get("input"):
            # manifest に入力 GLB が無ければ変換の報告の入力にする（import_converted と同じ規則。ただし差し替えでは
            # 新しい変換の入力が正しいので、前の GLB を残さない。2026-10-27 R08A-08）
            gen["source_glb"] = {"path": report["input"]}
        if gen:
            eq.data["generation"].update(gen)
        _record_conversion(library, eq, src, found, report)
        library.save(eq)
    except BaseException:
        eq.data = data0
        adir = os.path.join(eq.dir, "assets")
        if swapped:
            # 新しい assets/ は消さずに assets.failed/ へ名前の付け替えで退けてから元に戻す（その場で消すと、書いたばかりの
            # ファイルをウイルス対策などが開いていれば途中で止まり、新しいファイルの一部が assets/ に残って元のアセットを
            # 戻せなかった）。退けられなければ assets/ を残す（記録と食い違うのでビルドが装備ID付きで止まる）
            failed = adir + ".failed"
            shutil.rmtree(failed, ignore_errors=True)
            what = tr(f"装備 {eq.id} のアセット", f"equipment {eq.id} assets")       # _move_dir の文の名詞句
            try:
                _move_dir(adir, failed, what)
                if old_assets:
                    _move_dir(old_assets, adir, what)
            except (LibraryError, OSError):
                pass            # 戻せなければ assets.old に残す（次の set_assets が assets が無ければ戻す）
            shutil.rmtree(failed, ignore_errors=True)
        for name, data in sources0.items():
            p = os.path.join(sdir, name)
            try:
                if data is None:
                    if os.path.isfile(p):
                        os.remove(p)
                else:
                    with open(p, "wb") as f:
                        f.write(data)
            except OSError:
                pass
        raise
    if old_assets:
        shutil.rmtree(old_assets, ignore_errors=True)
    return eq


# ---------------------------------------------------------------------------
# 試作MOD のソースから復元
# ---------------------------------------------------------------------------
def import_prototype(library: Library, mod_dir: str, allow_default_library: bool = False) -> list[Equipment]:
    """試作MOD のソース（database/records、resources/Text_EN、resources/Items）から装備を復元する。

    補給官のレコードの marketStaticItems の末尾（公式に無い要素）を消耗品の並びとみなし、各消耗品の transmuteDbrs
    から頭装備レコード（透明レコードと公式の肩を除く）を、頭装備レコードから男女のメッシュ・DBR のテクスチャ指定を、
    タグファイルから表示文を取り出す。レコードのパス・タグ名は試作のまま固定する。

    試作の装備は開発用（``dev_only``）で、製品版の既定ライブラリ（:func:`modbuilder.paths.library_dir`）には入れない
    （D-018）。開発用のライブラリを別に作って取り込み、``build(include_dev=True)``（CLI ``build --dev``）でビルドする。
    """
    if not allow_default_library and \
            os.path.normcase(os.path.abspath(library.root)) == os.path.normcase(os.path.abspath(paths.library_dir())):
        raise LibraryError(tr("試作の装備は製品版の既定ライブラリには取り込めません。開発用のライブラリを "
                              f"--library で指定してください（既定ライブラリ: {library.root}）",
                              "Prototype equipment cannot be imported into the default library of the release "
                              "version. Specify a development library with --library "
                              f"(default library: {library.root})"))
    db = os.path.join(mod_dir, "database")
    if not os.path.isdir(os.path.join(db, "records")):
        raise LibraryError(tr(f"試作MOD のソースではありません（database/records が無い）: {mod_dir}",
                              f"Not a prototype MOD source (no database/records): {mod_dir}"))

    def rec(rel):
        p = os.path.join(db, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            raise LibraryError(tr(f"{mod_dir} にレコードがありません: {rel}", f"Record missing in {mod_dir}: {rel}"))
        return dict(read_dbr(p))

    tags = {}
    tdir = os.path.join(mod_dir, "resources", "Text_EN")
    for f in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
        if f.lower().endswith(".txt"):
            tags.update(parse_tags(_read(os.path.join(tdir, f))))
    items_dir = os.path.join(mod_dir, "resources", "Items")

    # 消耗品の並び: 補給官（friendly）の在庫のうち aiskin の名前空間のもの
    merchant = rec(MERCHANT_RECORDS[0])
    sets = [x for x in split_array(merchant.get("marketStaticItems", "")) if x.startswith(f"records/items/{NAMESPACE}")]
    if not sets:
        raise LibraryError(tr("補給官のレコードに AI Skin の消耗品がありません",
                              "The Quartermaster records have no AI Skin consumables"))
    for rel in MERCHANT_RECORDS[1:]:
        tail = [x for x in split_array(rec(rel).get("marketStaticItems", "")) if x.startswith(f"records/items/{NAMESPACE}")]
        if tail != sets:
            raise LibraryError(tr(f"補給官のティアごとに消耗品の並びが違います: {rel}",
                                  f"The order of consumables differs between Quartermaster tiers: {rel}"))
    # 試作MOD（2026-10-16）の共通の透明レコードは、自前の胴・脚・手・足の 4 つと公式の透明の肩（メダルは無い）。
    # 自前の肩・メダル（2026-11-11 から。D-078）と公式の透明のメダル（C125）は試作には無いので、有無を問わない
    # （消耗品の中にあれば共通のものとして扱い、頭装備には数えない）
    proto_own = [s[3] for s in INVISIBLE_SLOTS if s[0] in ("chest", "legs", "hands", "feet")]
    proto_common = proto_own + [OFFICIAL_INVISIBLE_SHOULDERS]
    common = set(INVISIBLE_RECORDS) | {OFFICIAL_INVISIBLE_SHOULDERS, OFFICIAL_INVISIBLE_MEDAL}
    for inv in proto_own:
        rec(inv)     # 共通の透明レコードが試作と同じパスにあること

    existing_names = {e.naming["head_record"] for e in library.list()}
    created: list[Equipment] = []
    base_time = now_iso()
    order = 0
    try:
        for set_rel in sets:
            srec = rec(set_rel)
            dbrs = split_array(srec.get("transmuteDbrs", ""))
            heads = [d for d in dbrs if d not in common]
            include_invisible = all(x in dbrs for x in proto_common)
            if not heads:
                raise LibraryError(tr(f"{set_rel} に頭装備がありません", f"{set_rel} has no head equipment"))
            owner = None
            for head_rel in heads:
                if head_rel in existing_names:
                    raise LibraryError(tr(f"{head_rel} は既にライブラリにあります", f"{head_rel} is already in the library"))
                h = rec(head_rel)
                head_tag = h.get("itemNameTag")
                if head_tag not in tags:
                    raise LibraryError(tr(f"{head_rel} のタグ {head_tag} がタグファイルにありません",
                                          f"Tag {head_tag} of {head_rel} is not in the tag files"))
                is_owner = owner is None
                naming = {
                    "scheme": "prototype", "source": os.path.basename(os.path.normpath(mod_dir)),
                    "head_record": head_rel, "head_tag": head_tag,
                    "set_record": set_rel if is_owner else None,
                    # 消耗品の名前タグは description（試作は transmuteTag と同じ。ModBuilder の 2026-10-17 以降の形は
                    # transmuteTag に接頭辞の無い名前のタグを入れる。点検 C73）
                    "set_tag": (srec.get("description") or srec.get("transmuteTag")) if is_owner else None,
                    "set_desc_tag": srec.get("itemText") if is_owner else None,
                    "asset_dir": None, "asset_prefix": None,
                }
                male, female = h.get("armorMaleMesh"), h.get("armorFemaleMesh")
                if not isinstance(male, str) or not male or not isinstance(female, str) or not female:
                    # 試作の装備は男女の両方のメッシュを持つ前提（無いと None.startswith のトレースバックになった。R08B-01）
                    raise LibraryError(tr(f"{head_rel} に armorMaleMesh / armorFemaleMesh がありません",
                                          f"{head_rel} has no armorMaleMesh / armorFemaleMesh"))
                files = []
                for gp in (male, female):
                    p = os.path.join(items_dir, gp.split("/", 1)[1].replace("/", os.sep)) if gp.startswith("items/") else ""
                    if p and os.path.isfile(p):
                        data = _read(p)
                        files.append((gp, data))
                        from gdmesh import msh
                        for _mi, _pn, t in A.material_textures(msh.read_bytes(data, strict=True)):
                            tp = os.path.join(items_dir, norm_path(t).split("/", 1)[1].replace("/", os.sep))
                            if norm_path(t).startswith("items/") and os.path.isfile(tp) and \
                                    norm_path(t) not in [norm_path(g) for g, _ in files]:
                                files.append((norm_path(t), _read(tp)))
                if files:
                    naming["asset_dir"] = os.path.dirname(norm_path(files[0][0]))
                # 作成順（補給官の在庫の順）を保つため、同じ時刻にマイクロ秒ずつ足す
                created_at = _offset(base_time, order)
                order += 1
                eq = library.create(tags[head_tag], None, {"method": "prototype"}, naming=naming, created_at=created_at)
                created.append(eq)
                # 頭装備の名前は表示名（display_name.en = タグの値）から作るので texts.head.en は入れない（入れると改名しても
                # ゲーム内の名前が変わらない。2026-10-17 点検 C76）。消耗品の名前・説明は試作で付けた独自の文なので残す
                texts = {}
                if is_owner:
                    for role, key in (("set", naming["set_tag"]), ("set_desc", naming["set_desc_tag"])):
                        if key not in tags:
                            raise LibraryError(tr(f"{set_rel} のタグ {key} がタグファイルにありません",
                                                  f"Tag {key} of {set_rel} is not in the tag files"))
                        texts[role] = {"en": tags[key]}
                    owner = eq
                eq.data["texts"] = texts
                eq.data["illusion_set"] = {"owner": owner.id, "include_invisible": include_invisible}
                rel_files = [(norm_path(gp).split("/", 1)[1], data, norm_path(gp)) for gp, data in files]
                library.set_assets(eq, rel_files, male, female,
                                   {k: h[k] for k in HEAD_TEXTURE_FIELDS if h.get(k)})
                # 変換の記録（assets_src/<名前>_report.json があれば）
                rep_path = None
                if male and files:
                    stem = os.path.basename(male)[:-len("_m.msh")] if male.endswith("_m.msh") else None
                    cand = os.path.join(mod_dir, "assets_src", f"{stem}_report.json") if stem else None
                    if cand and os.path.isfile(cand):
                        rep_path = cand
                if rep_path:
                    report = _read_record_json(rep_path)
                    _check_report_shape(rep_path, report)
                    found = {"report": rep_path, "preview": rep_path.replace("_report.json", "_preview.png")}
                    if not os.path.isfile(found["preview"]):
                        found["preview"] = None
                    stored = _store_sources(eq, found)
                    library.set_conversion(eq, status="converted", tool="gdconvert", source_dir=os.path.dirname(rep_path),
                                           report=stored.get("report"), converted_at=None,
                                           summary=_conversion_summary(report), error=None)
                    eq.data["generation"]["source_glb"] = {"path": report.get("input")}
                else:
                    library.set_conversion(eq, status="not_required")
                eq.data["generation"]["settings"] = {"prototype_source": os.path.abspath(mod_dir)}
                eq.data["dev_only"] = True          # D-018: 開発用のビルドにだけ入れる
                library.save(eq)
    except BaseException:           # Ctrl+C でも作りかけの装備を残さない（R08B-02）
        for eq in created:
            shutil.rmtree(eq.dir, ignore_errors=True)
        raise
    return created


def _offset(iso: str, micro: int) -> str:
    t = _dt.datetime.fromisoformat(iso) + _dt.timedelta(microseconds=micro)
    return t.isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# 別の装備ライブラリから
# ---------------------------------------------------------------------------
def _is_default_library(library: Library) -> bool:
    return os.path.normcase(os.path.abspath(library.root)) == os.path.normcase(os.path.abspath(paths.library_dir()))


def import_from_library(library: Library, source: Library, keys: list[str] | None = None) -> tuple[list[Equipment], list[str]]:
    """source の装備を library に取り込む。戻り値は (取り込んだ装備, 飛ばした装備の説明)。

    内部ID・``naming``（レコードのパス・タグ名）・``created_at``（補給官の在庫の並び）・アセット（バイト一致）・生成と変換の
    情報をそのまま保つので、source から作ったビルドと同じレコード・タグ・アセットになる。``mod`` の状態は取り込み先では
    まだビルドしていないので not_built に戻し、``imported_from`` に取り込み元を記録する。

    keys を省くと source の全装備のうち開発用（``dev_only``）でないものを取り込み、開発用の装備は飛ばす。製品版の既定
    ライブラリには開発用の装備を取り込めない（keys で明示してもエラー。D-018）。取り込み先に同じ ID・レコードのパス・
    タグ名があればエラーで、途中で失敗したら取り込んだ分を消す（取り込み先の既存の装備には触れない）。
    """
    if os.path.normcase(os.path.abspath(library.root)) == os.path.normcase(os.path.abspath(source.root)):
        raise LibraryError(tr("取り込み元と取り込み先が同じライブラリです", "The source and destination are the same library"))
    skipped: list[str] = []
    if keys:
        items = [source.find(k) for k in keys]
        for eq in items:
            if eq.dev_only and _is_default_library(library):
                raise LibraryError(tr(f"{eq.id}（{eq.name_en}）は開発用の装備なので、製品版の既定ライブラリには取り込めません",
                                      f"{eq.id} ({eq.name_en}) is development equipment, so it cannot be imported "
                                      f"into the default library of the release version"))
    else:
        items = []
        for eq in source.list():
            if eq.dev_only:
                skipped.append(tr(f"{eq.id}（{eq.name_en}）: 開発用の装備",
                                  f"{eq.id} ({eq.name_en}): development equipment"))
            else:
                items.append(eq)
    existing = set(library.ids())
    incoming = {e.id for e in items}
    for eq in items:
        if eq.id in existing:
            raise LibraryError(tr(f"取り込み先に同じ装備ID {eq.id} があります",
                                  f"The destination already has equipment ID {eq.id}"))
        if os.path.lexists(os.path.join(library.equipment_root, eq.id)):
            # equipment.json の無い残り（消し切れなかったフォルダーなど）。上書き・後始末で消さないように止める（R12B-04）
            raise LibraryError(tr(f"取り込み先に装備ID {eq.id} のフォルダーが残っています"
                                  f"（{os.path.join(library.equipment_root, eq.id)}）",
                                  f"A folder for equipment ID {eq.id} remains in the destination "
                                  f"({os.path.join(library.equipment_root, eq.id)})"))
        # 消耗品をまとめている装備（illusion_set.owner）が取り込み先にも一緒に取り込む装備にも無いと、取り込み先のビルドが
        # 必ず止まる（trash からの復元・装備パッケージと同じ検査。R08A-09）
        try:
            owner = eq.set_owner
        except (KeyError, TypeError):
            raise LibraryError(tr(f"{eq.id}（{eq.name_en}）の装備の情報が不正です（illusion_set）",
                                  f"Invalid equipment data for {eq.id} ({eq.name_en}) (illusion_set)")) from None
        if owner != eq.id and owner not in incoming and owner not in existing:
            raise LibraryError(tr(f"{eq.id}（{eq.name_en}）の消耗品をまとめている装備 {owner} が取り込み先にも取り込む装備にも"
                                  f"ありません（{owner} も一緒に指定してください）",
                                  f"Equipment {owner}, which groups the consumables of {eq.id} ({eq.name_en}), is "
                                  f"neither in the destination nor among the equipment to import (specify {owner} "
                                  f"as well)"))
        problems = source.verify_assets(eq)
        if problems:
            raise LibraryError(tr(f"取り込み元のアセットが記録と一致しません: {'; '.join(problems)}",
                                  f"The source assets do not match the records: {'; '.join(problems)}"))
        library._check_naming_unique(eq.naming, eq.id)
    if len({e.id for e in items}) != len(items):
        raise LibraryError(tr("同じ装備が重複して指定されています", "The same equipment is specified more than once"))
    done: list[Equipment] = []
    created: list[str] = []
    try:
        for eq in items:
            dst = os.path.join(library.equipment_root, eq.id)
            if os.path.lexists(dst):     # 確認の後にできたフォルダー（自分の作っていないフォルダーは後始末で消さない）
                raise LibraryError(tr(f"取り込み先に装備ID {eq.id} のフォルダーが残っています（{dst}）",
                                      f"A folder for equipment ID {eq.id} remains in the destination ({dst})"))
            created.append(dst)
            # set_assets の入れ替え・replace_converted の巻き戻しの途中で止まったときの残り（assets.new・assets.old・
            # assets.failed）は写さない
            shutil.copytree(eq.dir, dst, ignore=shutil.ignore_patterns("assets.new", "assets.old", "assets.failed"))
            new = library.load(eq.id)
            if library.verify_assets(new):
                raise LibraryError(tr(f"{eq.id}: コピーしたアセットが記録と一致しません",
                                      f"{eq.id}: The copied assets do not match the records"))
            new.data["mod"] = {"status": "not_built", "last_build_id": None, "last_built_at": None,
                               "installed_build_id": None, "installed_at": None, "error": None}
            new.data["imported_from"] = {"library": source.root, "at": now_iso(),
                                         "mod_status": eq.data["mod"].get("status"),
                                         "installed_build_id": eq.data["mod"].get("installed_build_id")}
            if "dev_only" not in new.data:
                new.data["dev_only"] = eq.dev_only
            # ゲーム内パス（file_* の欄の値）は小文字・'/' 区切りにそろえる（手で書き換えた .gdskin など。点検 C78）
            a = new.data.get("assets") or {}
            for k in ("male_mesh", "female_mesh"):
                if isinstance(a.get(k), str):
                    a[k] = norm_path(a[k])
            if isinstance(a.get("head_textures"), dict):
                a["head_textures"] = {k: norm_path(v) if isinstance(v, str) else v for k, v in a["head_textures"].items()}
            library.save(new, touch=False)
            done.append(new)
    except BaseException:          # Ctrl+C でも（R08B-02）
        for d in created:          # 取り込みかけた装備を残さない（ID が重ならないことは上で確認済み）
            shutil.rmtree(d, ignore_errors=True)
        raise
    return done, skipped
