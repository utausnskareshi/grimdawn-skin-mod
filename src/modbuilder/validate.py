"""生成物の検証（試作MOD 1〜3 の check_refs.py と build_mod.py の往復検証を組み込んだもの）。

:func:`validate_staging` はビルド前の作業フォルダー（``database/records/*.dbr``、``resources/<アーカイブ>/``）を検査する:

  R1 templateName が解決でき、全フィールドがテンプレート（include 展開後）にあり、値が型どおり、フィールドの重複なし
  R2 file_dbr の参照先が MOD 内か公式データベースにある
  R3 file_* の参照先（メッシュ・テクスチャ・アイコン）が公式 .arc か MOD の resources/ にある
  R4 'tag' で始まる文字列値が MOD のタグ（Text_EN）か公式タグに定義されている
  R5 transmuteDbrs の参照先が防具・武器テンプレートのレコード（例外は透明のメダル。2026-11-11 から自前の
     :data:`~modbuilder.naming.INVISIBLE_MEDAL_RECORD`、以前は公式の :data:`~modbuilder.naming.OFFICIAL_INVISIBLE_MEDAL`。2026-10-17 点検 C125）
  R6 公式レコードの差分コピーで、元のフィールドと配列の要素が失われていない
  R7 headMaleMesh / headFemaleMesh に静的ダミー（system/meshes/*）を入れていない
  T1 タグファイルの書式（key=value、重複なし、タグ名の形式）、Text_XX は Text_EN と同じキー
  A1 .msh（読み戻し一致・validate・頂点数 ≤ 65,535・マテリアルのテクスチャの実在・プレイヤー骨格のボーン名）。自前の透明の
     見た目のメッシュ（items/aiskin/invisible/。2026-11-11 D-079）は、公式の透明メッシュから作るものとバイト単位で同じこと
  A2 .tex（ヘッダー・2 の累乗・DXT1/3/5・ミップ列）。MOD のアイコン・自前の透明の見た目の画像は、このアプリが描くものと
     バイト単位で同じこと
  A3 MOD のファイルが公式 .arc の同名エントリを上書きしない。どこからも参照されないファイルは警告

:func:`verify_outputs` はビルド後の .arz / .arc を読み戻して元と比べる（自前の読み取り + ArchiveTool）。
:func:`verify_copies` はビルド後の .arz の差分コピーで、変えていない欄の値が雛形の公式レコードと型・値のビットまで
一致するかを確かめる（R8。.dbr のテキスト（'%.6f'）を経由しても値が変わっていないこと。点検 C80）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import assets as A
from ._i18n import tr
from .arc import ArcFile, WorkDirUnavailable
from .arz import (T_BOOL, T_FLOAT, T_INT, T_STRING, ArzFile, _canon, dbr_text, norm_path, parse_dbr_text, read_dbr,
                  record_text, split_array)
from .gamedata import parse_tags
from .icons import icon_files
from .invisible_look import look_msh, look_tex
from .naming import (ICON_PREFIX, INVISIBLE_LOOK_MSH, INVISIBLE_LOOK_PREFIX, INVISIBLE_LOOK_TEX, INVISIBLE_MEDAL_RECORD,
                     OFFICIAL_INVISIBLE_MEDAL, is_valid_tag)

__all__ = ["Report", "validate_staging", "verify_outputs", "verify_copies", "MALE_MESH_FIELDS", "FEMALE_MESH_FIELDS"]

MALE_MESH_FIELDS = ("armorMaleMesh", "mesh", "headMaleMesh")
FEMALE_MESH_FIELDS = ("armorFemaleMesh", "headFemaleMesh")


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    error_keys: set = field(default_factory=set)     # 問題のあったレコード名・アセットのゲーム内パス
    checks: dict = field(default_factory=dict)       # 検査名 → 対象数

    def error(self, key: str | None, msg: str) -> None:
        self.errors.append(msg)
        if key:
            self.error_keys.add(key)

    def to_dict(self) -> dict:
        return {"errors": self.errors, "warnings": self.warnings, "checks": self.checks, "info": self.info}


def _list_files(root: str) -> dict[str, str]:
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, _d, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            out[os.path.relpath(full, root).replace("\\", "/").lower()] = full
    return out


def load_mod_resources(staging: str) -> dict[str, str]:
    """resources/<アーカイブ名>/ 以下（Text_* を除く）→ {ゲーム内パス(小文字): 実ファイル}。"""
    out = {}
    rdir = os.path.join(staging, "resources")
    if not os.path.isdir(rdir):
        return out
    for arc in sorted(os.listdir(rdir)):
        adir = os.path.join(rdir, arc)
        if os.path.isdir(adir) and not arc.lower().startswith("text_"):
            for rel, full in _list_files(adir).items():
                out[arc.lower() + "/" + rel] = full
    return out


def load_mod_tags(staging: str, lang: str, rep: Report | None) -> dict[str, str]:
    tags: dict[str, str] = {}
    tdir = os.path.join(staging, "resources", "Text_" + lang)
    for rel, full in sorted(_list_files(tdir).items()):
        if rel.endswith(".txt"):
            with open(full, "rb") as f:
                data = f.read()
            problems = [] if rep is not None else None
            t = parse_tags(data, problems, f"Text_{lang}/{rel}")
            if rep is not None:
                for p in problems:
                    rep.error(None, p)
                for k in t:
                    if k in tags:
                        rep.error(None, tr(f"Text_{lang}: タグ {k} が複数のファイルにあります",
                                           f"Text_{lang}: tag {k} is in more than one file"))
            tags.update(t)
    return tags


def validate_staging(staging: str, gd, languages=("EN",)) -> Report:
    rep = Report()
    templates = gd.templates
    mod_res = load_mod_resources(staging)
    for key in sorted(mod_res):
        if gd.official_file_exists(key):
            rep.error(key, tr(f"A3 MOD のファイル {key} が公式 .arc（{gd.arc_index[key]}）の同名エントリを上書きします",
                              f"A3 MOD file {key} overwrites the entry with the same name in an official .arc "
                              f"({gd.arc_index[key]})"))
        if not key.startswith("items/"):
            rep.error(key, tr(f"A3 MOD のファイル {key} は Items.arc 以外のアーカイブです（未対応）",
                              f"A3 MOD file {key} is in an archive other than Items.arc (not supported)"))

    def file_exists(v: str) -> bool:
        k = norm_path(v)
        return k in mod_res or gd.official_file_exists(k)

    db_root = os.path.join(staging, "database")
    mod_records = {rel: full for rel, full in _list_files(db_root).items() if rel.endswith(".dbr")}
    if not mod_records:
        rep.error(None, tr("R1 レコードがありません", "R1 There are no records"))

    # --- タグ --------------------------------------------------------------------------
    mod_tags = load_mod_tags(staging, "EN", rep)
    for k, v in mod_tags.items():
        if not is_valid_tag(k):
            rep.error(None, tr(f"T1 タグ名の形式が不正: {k!r}", f"T1 Invalid tag name format: {k!r}"))
        if not v.strip():
            rep.error(None, tr(f"T1 タグ {k} の値が空です", f"T1 Tag {k} has an empty value"))
    for lang in languages:
        if lang.upper() == "EN":
            continue
        other = load_mod_tags(staging, lang.upper(), rep)
        if set(other) != set(mod_tags):
            rep.error(None, tr(f"T1 Text_{lang.upper()} のタグが Text_EN と一致しません: "
                               f"不足 {sorted(set(mod_tags) - set(other))} 余分 {sorted(set(other) - set(mod_tags))}",
                               f"T1 The tags in Text_{lang.upper()} do not match Text_EN: missing "
                               f"{sorted(set(mod_tags) - set(other))}, extra {sorted(set(other) - set(mod_tags))}"))
    official_tags = gd.official_tags
    rep.checks["tags"] = len(mod_tags)

    used_tags: set[str] = set()
    mesh_use: dict[str, set[str]] = {}
    referenced: set[str] = set()

    def template_of(rel: str) -> str | None:
        rel = norm_path(rel)
        if rel in mod_records:
            fields = read_dbr(mod_records[rel])
        else:
            got = gd.official_record(rel)
            if got is None:
                return None
            fields = got[1]
        return next((norm_path(v) for n, v in fields if n == "templateName"), None)

    # --- レコード ------------------------------------------------------------------------
    for rel in sorted(mod_records):
        fields = read_dbr(mod_records[rel])
        fdict = dict(fields)
        tpl = fdict.get("templateName")
        if not tpl:
            rep.error(rel, tr(f"R1 {rel}: templateName がありません", f"R1 {rel}: templateName is missing"))
            continue
        try:
            tvars = templates.load(tpl)
        except KeyError as e:
            rep.error(rel, f"R1 {rel}: {e}")
            continue
        names = [n for n, _ in fields]
        if len(names) != len(set(names)):
            rep.error(rel, tr(f"R1 {rel}: フィールド名が重複しています", f"R1 {rel}: duplicate field names"))
        for name, value in fields:
            if name == "templateName" or value == "":
                continue
            tv = tvars.get(name)
            if tv is None:
                rep.error(rel, tr(f"R1 {rel}: テンプレート {tpl} に無いフィールド {name}",
                                  f"R1 {rel}: field {name} is not in template {tpl}"))
                continue
            values = split_array(value) if tv.is_array else [value]
            for v in values:
                t = tv.arz_type
                try:
                    if t == T_INT:
                        int(v)
                    elif t == T_FLOAT:
                        float(v)
                    elif t == T_BOOL and int(v) not in (0, 1):
                        raise ValueError
                except ValueError:
                    rep.error(rel, tr(f"R1 {rel}: {name} の値 {v!r} が型 {tv.vtype} として不正",
                                      f"R1 {rel}: value {v!r} of {name} is invalid for type {tv.vtype}"))
                    continue
                if tv.vtype == "file_dbr":
                    if norm_path(v) not in mod_records and not gd.official_record_exists(v):
                        rep.error(rel, tr(f"R2 {rel}: {name} が参照するレコードがありません: {v}",
                                          f"R2 {rel}: the record referenced by {name} does not exist: {v}"))
                elif tv.is_file:
                    key = norm_path(v)
                    if not file_exists(v):
                        rep.error(rel, tr(f"R3 {rel}: {name} が参照するファイルが公式 .arc にも MOD にもありません: {v}",
                                          f"R3 {rel}: the file referenced by {name} is in neither the official .arc "
                                          f"files nor the MOD: {v}"))
                    if key in mod_res:
                        referenced.add(key)
                        if key.endswith(".msh"):
                            g = mesh_use.setdefault(key, set())
                            if name in MALE_MESH_FIELDS:
                                g.add("m")
                            elif name in FEMALE_MESH_FIELDS:
                                g.add("f")
                    if name in ("headMaleMesh", "headFemaleMesh") and key.startswith("system/meshes/"):
                        rep.error(rel, tr(f"R7 {rel}: {name} に静的ダミー {v} は使えません（武器が足元に出て移動方向が壊れる。"
                                          "creatures/pc/hero_00_invisible.msh 等を使う）",
                                          f"R7 {rel}: the static dummy {v} cannot be used for {name} (weapons "
                                          "appear at the feet and the movement direction breaks; use "
                                          "creatures/pc/hero_00_invisible.msh or similar)"))
                elif t == T_STRING and v.startswith("tag") and is_valid_tag(v):
                    used_tags.add(v)
                    if v not in mod_tags and v not in official_tags:
                        rep.error(rel, tr(f"R4 {rel}: {name} のタグ {v} が未定義です",
                                          f"R4 {rel}: tag {v} of {name} is not defined"))
            if name == "transmuteDbrs":
                for v in values:
                    # 透明のメダル（jewelry_medal.tpl）だけは許す。2026-11-11 から自前の透明のメダル（D-078）、以前は公式のもの
                    if norm_path(v) in (OFFICIAL_INVISIBLE_MEDAL, INVISIBLE_MEDAL_RECORD):
                        continue
                    t2 = template_of(v)
                    if t2 is None or not os.path.basename(t2).startswith(("armor_", "weapon")):
                        rep.error(rel, tr(f"R5 {rel}: transmuteDbrs の {v} は防具・武器のレコードではありません（{t2}）",
                                          f"R5 {rel}: {v} in transmuteDbrs is not an armor or weapon record ({t2})"))
        got = gd.official_record(rel)
        if got is not None:
            ofields = dict(got[1])
            for name, ov in ofields.items():
                nv = fdict.get(name)
                if nv is None:
                    rep.error(rel, tr(f"R6 {rel}: 上書きする公式レコードのフィールド {name} が失われています",
                                      f"R6 {rel}: field {name} of the overridden official record is missing"))
                elif nv != ov:
                    tv = tvars.get(name)
                    if tv is not None and tv.is_array:
                        missing = [x for x in split_array(ov) if x not in split_array(nv)]
                        if missing:
                            rep.error(rel, tr(f"R6 {rel}: {name} から元の要素が失われています: {missing}",
                                              f"R6 {rel}: original elements are missing from {name}: {missing}"))
                        else:
                            added = [x for x in split_array(nv) if x not in split_array(ov)]
                            rep.info.append(tr(f"{rel}: 公式レコードの差分コピー（{got[0]}）。{name} に追加 {added}",
                                               f"{rel}: modified copy of an official record ({got[0]}). "
                                               f"Added to {name}: {added}"))
                    else:
                        rep.info.append(tr(f"{rel}: 公式レコードの差分コピー（{got[0]}）。{name}: {ov!r} → {nv!r}",
                                           f"{rel}: modified copy of an official record ({got[0]}). "
                                           f"{name}: {ov!r} → {nv!r}"))
    rep.checks["records"] = len(mod_records)

    # --- アセット ---------------------------------------------------------------------------
    bones = {}
    for g in ("m", "f"):
        if any(g in s for s in mesh_use.values()):
            try:
                bones[g] = gd.player_bones(g)
            except Exception as e:  # noqa: BLE001
                rep.warnings.append(tr(f"プレイヤー骨格（{g}）を読めません: {e}", f"Cannot read the player skeleton ({g}): {e}"))
    nmsh = ntex = 0
    for key in sorted(mod_res):
        if key.endswith(".msh"):
            nmsh += 1
            with open(mod_res[key], "rb") as f:
                data = f.read()
            errs: list[str] = []
            r = A.check_msh(key, data, mesh_use.get(key, set()), bones, file_exists, errs, rep.warnings, rep.info)
            for e in errs:
                rep.error(key, "A1 " + e)
            if key.startswith(INVISIBLE_LOOK_PREFIX):
                # 自前の透明の見た目のメッシュ（D-079）は、この PC の公式の透明メッシュから作るものとバイト単位で同じこと
                try:
                    expected = look_msh(gd) if key == INVISIBLE_LOOK_MSH else None
                except Exception as e:  # noqa: BLE001  公式のメッシュが読めない・形が違う
                    rep.error(key, "A1 " + tr(f"{key}: 公式の透明メッシュから作り直せません: {e}",
                                              f"{key}: cannot be recreated from the official invisible mesh: {e}"))
                else:
                    if expected != data:
                        rep.error(key, "A1 " + tr(f"{key}: 透明の見た目のメッシュが、このアプリの作るものと違います",
                                                  f"{key}: the invisible look mesh differs from the one this app creates"))
            for t in (r or {}).get("textures", []):
                if t in mod_res:
                    referenced.add(t)
    icons = icon_files()
    for key in sorted(mod_res):
        if key.endswith(".tex"):
            ntex += 1
            with open(mod_res[key], "rb") as f:
                data = f.read()
            errs = []
            if key.startswith(ICON_PREFIX):
                # MOD のアイコン（公式のアイコンと同じ非圧縮の 32 ビット。DXT ではない）は、このアプリが描くものと
                # バイト単位で同じことを確かめる（2026-11-11）
                if icons.get(key) != data:
                    rep.error(key, "A2 " + tr(f"{key}: MOD のアイコンが、このアプリの作るものと違います",
                                              f"{key}: the MOD icon differs from the one this app creates"))
                else:
                    rep.info.append(f"{key}: MOD icon {len(data)} B")
                continue
            if key.startswith(INVISIBLE_LOOK_PREFIX):
                # 自前の透明の見た目の画像（全透明の 32x32。非圧縮。D-079）も、このアプリが描くものとバイト単位で比べる
                if key != INVISIBLE_LOOK_TEX or look_tex() != data:
                    rep.error(key, "A2 " + tr(f"{key}: 透明の見た目の画像が、このアプリの作るものと違います",
                                              f"{key}: the invisible look texture differs from the one this app creates"))
                else:
                    rep.info.append(f"{key}: invisible look texture {len(data)} B")
                continue
            A.check_tex(key, data, errs, rep.info)
            for e in errs:
                rep.error(key, "A2 " + e)
    rep.checks["meshes"] = nmsh
    rep.checks["textures"] = ntex
    for key in sorted(mod_res):
        if key not in referenced:
            rep.warnings.append(tr(f"A3 MOD のファイル {key} はどのレコード・メッシュからも参照されていません",
                                   f"A3 MOD file {key} is not referenced by any record or mesh"))
    for k in sorted(set(mod_tags) - used_tags):
        rep.warnings.append(tr(f"T1 タグ {k} はどのレコードからも参照されていません",
                               f"T1 Tag {k} is not referenced by any record"))
    for k in sorted(used_tags & set(mod_tags) & set(official_tags)):
        rep.warnings.append(tr(f"T1 タグ {k} は公式タグと同じ名前です（どちらが優先されるか未確認）",
                               f"T1 Tag {k} has the same name as an official tag (which one takes precedence "
                               "has not been confirmed)"))
    return rep


def verify_outputs(staging: str, outputs: dict[str, str], archivetool=None) -> Report:
    """ビルドした .arz / .arc の往復検証。outputs: {'database.arz': path, 'Text_EN.arc': path, 'Items.arc': path, ...}。

    * .arz: 自前の読み取りで Adler32 の末尾を検証し、各レコードを展開したテキストが元の .dbr と一致するか
      （ArchiveTool があれば ``-database`` の展開結果とも比べる）。``-database`` の出力先に使える作業フォルダーが
      無いとき（:class:`~modbuilder.arc.WorkDirUnavailable`）は V2 だけを省き、警告と ``checks`` に記録する
      （V1・V3・V4 は行う。2026-10-17 利用者の判断 C02）。
    * .arc: 自前の読み取りで全ファイルが元とバイト一致するか（ArchiveTool があれば ``-list`` で一覧が読めるか）。
    """
    rep = Report()
    src_records = {rel: full for rel, full in _list_files(os.path.join(staging, "database")).items()
                   if rel.endswith(".dbr")}
    expect = {}
    for rel, full in src_records.items():
        expect[rel] = dbr_text(dict(read_dbr(full)))
    arz_path = outputs.get("database.arz")
    if arz_path:
        # 読めない（壊れた・形の違う）database.arz は生の ValueError で止めず、.arc の V3 と同じくエラーにして続ける
        # （2026-10-27 R08B-03）
        try:
            a = ArzFile(arz_path)
            if not a.trailer_ok():
                rep.error("database.arz", tr("V1 database.arz の末尾（Adler32）が内容と一致しません",
                                             "V1 The database.arz trailer (Adler32) does not match its contents"))
            got = {r.name: record_text(a.decode(r)) for r in a.records}
        except ValueError as e:
            rep.error("database.arz", tr(f"V1 database.arz を読めません: {e}", f"V1 Cannot read database.arz: {e}"))
            got = None
        if got is not None:
            _compare_texts(rep, tr("V1 database.arz（自前の読み取り）", "V1 database.arz (built-in reader)"), expect, got)
        if archivetool is not None:
            try:
                extracted = archivetool.extract_database(arz_path)
            except WorkDirUnavailable as e:
                cands = tr("、", ", ").join(f"{c}: {why}" for c, why in e.candidates)
                rep.warnings.append(tr("V2 ArchiveTool による database.arz の照合（-database）を省きました: 一時フォルダーの"
                                       "パスに ArchiveTool が扱えない文字があり、ほかの作業フォルダーも使えません（"
                                       f"{cands}）。自前の読み取りでの照合（V1）は行っています",
                                       "V2 Skipped checking database.arz with ArchiveTool (-database): the path of "
                                       "the temporary folder contains characters ArchiveTool cannot handle, and no "
                                       f"other work folder can be used ({cands}). The check with the built-in reader "
                                       "(V1) was done"))
                rep.checks["arz_archivetool"] = "skipped: work_dir"
            else:
                _compare_texts(rep, tr("V2 database.arz（ArchiveTool -database）",
                                       "V2 database.arz (ArchiveTool -database)"), expect, extracted)
                rep.checks["arz_archivetool"] = len(extracted)
        if got is not None:
            rep.checks["arz_records"] = len(got)
    for name, path in outputs.items():
        if not name.lower().endswith(".arc"):
            continue
        arcname = name[:-4]
        src = _list_files(os.path.join(staging, "resources", arcname))
        try:
            arc = ArcFile(path)
            got = {k.replace("\\", "/").lower(): v for k, v in arc.read_all().items()}
        except Exception as e:  # noqa: BLE001
            rep.error(name, tr(f"V3 {name} を読めません: {e}", f"V3 Cannot read {name}: {e}"))
            continue
        for rel in sorted(set(src) - set(got)):
            rep.error(name, tr(f"V3 {name}: 元にあるファイルが入っていません: {rel}",
                               f"V3 {name}: a file in the source is missing: {rel}"))
        for rel in sorted(set(got) - set(src)):
            rep.error(name, tr(f"V3 {name}: 元に無いファイルが入っています: {rel}",
                               f"V3 {name}: contains a file that is not in the source: {rel}"))
        for rel in sorted(set(src) & set(got)):
            with open(src[rel], "rb") as f:
                if f.read() != got[rel]:
                    rep.error(name, tr(f"V3 {name}: 中身が元と一致しません: {rel}",
                                       f"V3 {name}: the contents do not match the source: {rel}"))
        if archivetool is not None:
            listed = sorted(e.replace("\\", "/").lower() for e in archivetool.list(path))
            if listed != sorted(src):
                rep.error(name, tr(f"V4 {name}: ArchiveTool -list の一覧が元と一致しません",
                                   f"V4 {name}: the ArchiveTool -list listing does not match the source"))
        rep.checks[name] = len(got)
    return rep


def _compare_texts(rep: Report, label: str, expect: dict[str, str], got: dict[str, str]) -> None:
    for rel in sorted(set(expect) - set(got)):
        rep.error(rel, tr(f"{label}: レコードが入っていません: {rel}", f"{label}: a record is missing: {rel}"))
    for rel in sorted(set(got) - set(expect)):
        rep.error(rel, tr(f"{label}: 元に無いレコードが入っています: {rel}",
                          f"{label}: contains a record that is not in the source: {rel}"))
    for rel in sorted(set(expect) & set(got)):
        if expect[rel] != got[rel]:
            fields = _differing_fields(expect[rel], got[rel])
            rep.error(rel, tr(f"{label}: 展開したテキストが元の .dbr と一致しません: {rel}{fields}",
                              f"{label}: the extracted text does not match the source .dbr: {rel}{fields}"))


def _differing_fields(a: str, b: str) -> str:
    """2 つの .dbr テキストで値が違う欄の名前（不一致の文に添える。点検 C78）。"""
    try:
        da, db = dict(parse_dbr_text(a)), dict(parse_dbr_text(b))
    except ValueError:
        return ""
    names = sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))
    if not names:
        return tr("（欄の並び）", " (field order)")
    return tr(f"（欄: {', '.join(names[:8])}）", f" (fields: {', '.join(names[:8])})")


def verify_copies(arz_path: str, gd, sources: dict[str, str], staging: str) -> Report:
    """R8: 差分コピー・雛形コピーのレコード（sources: レコード名 → '<db>:<公式レコード>'）で、作業フォルダーの .dbr の
    テキストが公式レコードのテキストと同じ欄（＝本ツールが変えていない欄）は、作った .arz の (型, 値のビット) が公式の
    .arz と一致するか（点検 C80）。.dbr のテキストは float を '%.6f' で書くので（ArchiveTool と同じ）、公式に往復しない
    値（1e-7 など）を持つ雛形を使うと黙って値が変わる。違えば止める。ゲームデータが型付きで読めない（テストの偽物）
    ときは検査しない（checks に記録）。"""
    rep = Report()
    built = ArzFile(arz_path)
    n = 0
    db_of = getattr(gd, "database", None)
    if db_of is None:
        rep.checks["copies"] = "skipped: no typed game data"
        return rep
    for rel, src in sorted(sources.items()):
        db, _, orel = src.partition(":")
        oarz = db_of(db)
        orec = oarz.get(orel) if oarz is not None else None
        brec = built.get(rel)
        if orec is None or brec is None:
            rep.error(rel, tr(f"R8 {rel}: 雛形の公式レコード（{src}）か作ったレコードが読めません",
                              f"R8 {rel}: cannot read the official base record ({src}) or the created record"))
            continue
        p = os.path.join(staging, "database", rel.replace("/", os.sep))
        staged = dict(read_dbr(p)) if os.path.isfile(p) else {}
        otext = dict(parse_dbr_text(record_text(oarz.decode(orec))))
        ofields = {f[0]: f for f in _canon(oarz.decode(orec))}
        bfields = {f[0]: f for f in _canon(built.decode(brec))}
        bad = sorted(name for name, of in ofields.items()
                     if staged.get(name) == otext.get(name) and bfields.get(name) != of)
        if bad:
            rep.error(rel, tr(f"R8 {rel}: 変えていない欄の値が雛形の公式レコード（{src}）と型・値のビットで一致しません"
                              f"（.dbr のテキストを経由して値が変わった）: {bad[:8]}",
                              f"R8 {rel}: the values of unchanged fields do not match the official base record "
                              f"({src}) in type and value bits (the values changed when passing through the .dbr "
                              f"text): {bad[:8]}"))
        n += 1
    rep.checks["copies"] = n
    return rep
