"""装備ライブラリ（計画書 4.3・10 章）。

保存先は既定で ``%LOCALAPPDATA%\\GrimDawnSkinMOD\\library\\``（:mod:`modbuilder.paths`）。構成::

  library.json                         {"format": "gdskin-library", "version": 1, ...}
  equipment/<装備ID>/equipment.json    装備 1 件の情報（下記）
  equipment/<装備ID>/assets/           MOD に入れるファイル（.msh / .tex。Items.arc の中身になる）
  equipment/<装備ID>/source/           変換の報告・確認画像・入力画像など（MOD には入れない）
  trash/<装備ID>-<日時>/               削除した装備（完全には消さない。Library.restore_from_trash で equipment/ へ戻す）

``equipment.json`` の項目（``version`` 1）:

  id               内部ID（``eq`` + 16進 10 桁）。表示名とは独立で、名前が重複しても衝突しない
  display_name     ゲーム内表示名 {"en": 必須, "ja": 任意}
  texts            表示文の上書き（任意）: {"head"|"set"|"set_desc": {"en", "ja"}}。無ければ display_name から作る
  generation       生成方法と入力: method（text / image / external / prototype）, input_text,
                   input_image {original_path, stored_path, sha256}, model_id, seed, settings, generated_at, source_glb
  created_at / updated_at   ISO 8601（UTC）。補給官の在庫に並べる順は created_at の順
  naming           作成時に固定したレコードのパス・タグ名・アセットのフォルダー（:mod:`modbuilder.naming`）
  illusion_set     {"owner": 消耗品を持つ装備のID（通常は自分）, "include_invisible": 透明な胴・脚・手・足・肩も登録するか}
  assets           male_mesh / female_mesh（ゲーム内パス）, head_textures（DBR で指定するテクスチャ。通常は空）,
                   files [{file（assets/ からの相対）, game_path, sha256, size}]
  conversion       変換状態: status（pending / converted / failed / not_required）, tool, source_dir, report,
                   converted_at, summary, error
  mod              MOD 生成状態: status（not_built / built / build_failed / installed）, last_build_id, last_built_at,
                   installed_build_id, installed_at, error
  errors           エラーの履歴 [{at, stage, message}]（新しい順に最大 20 件）
  dev_only         開発用の装備（試作の装備。D-018）。true なら製品版のビルド（build の既定）には入れない
"""
from __future__ import annotations

import copy
import datetime as _dt
import errno
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata

from . import __version__, paths
from ._i18n import tr
from .errors import LibraryError
from .naming import HEAD_TEXTURE_FIELDS, ID_RE, derive_naming, new_equipment_id

__all__ = ["Library", "Equipment", "now_iso", "sha256_file", "validate_display_name", "display_name_warnings",
           "FORMAT_VERSION"]

FORMAT_VERSION = 1
LIBRARY_FORMAT = "gdskin-library"
EQUIPMENT_FORMAT = "gdskin-equipment"
MAX_ERRORS = 20
NAME_MAX = 64
DESC_MAX = 400
FORBIDDEN_NAME_CHARS = set("=^{}\\")       # タグファイルの区切り・色コード・書式パラメーターに使われる文字
CONVERSION_STATUSES = ("pending", "converted", "failed", "not_required")
MOD_STATUSES = ("not_built", "built", "build_failed", "installed")
GENERATION_METHODS = ("text", "image", "external", "prototype")


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_display_name(name: str, lang: str = "en", max_len: int = NAME_MAX) -> list[str]:
    """表示名・説明文の問題の一覧（空なら問題なし）。タグファイルの 1 行に入れるので改行・制御文字（行・段落の区切り
    U+2028・U+2029 を含む。タグファイルの検証は str.splitlines で行を分けるので、そこで行が切れる）と、
    区切り・色コード・書式パラメーターに使われる = ^ { } \\ は使えない。"""
    problems = []
    # 英語の文は主語を省く（呼び出し側が「Name: 」「Description (English): 」などの見出しを付けて出す）
    if not isinstance(name, str) or not name.strip():
        return [tr("空です", "empty")]
    if name != name.strip():
        problems.append(tr("前後に空白があります", "has leading or trailing spaces"))
    if len(name) > max_len:
        problems.append(tr(f"長すぎます（{len(name)} 文字 > {max_len}）", f"too long ({len(name)} characters > {max_len})"))
    bad = sorted({c for c in name if c in FORBIDDEN_NAME_CHARS or unicodedata.category(c).startswith("C")
                  or unicodedata.category(c) in ("Zl", "Zp")})
    if bad:
        chars = " ".join(repr(c) for c in bad)
        problems.append(tr(f"使えない文字があります: {chars}", f"contains characters that cannot be used: {chars}"))
    return problems


def display_name_warnings(name: str | None, lang: str = "en") -> list[str]:
    """保存は止めない注意: 英語の表示名・英語の表示文に、印字可能な ASCII（U+0020〜U+007E）の外の文字があるか。

    英語の設定のゲームのフォント（Fonts.arc の直下）は U+0020〜U+007E の 95 字の字形だけを持つので、日本語・é・“ ” …
    などは表示されない見込み（ゲーム内では未確認。2026-10-17 点検 C75）。日本語（lang "ja"）は対象外。"""
    if lang != "en" or not isinstance(name, str):
        return []
    bad = sorted({c for c in name if not (0x20 <= ord(c) <= 0x7E)})
    if not bad:
        return []
    chars = " ".join(bad[:10]) + (" …" if len(bad) > 10 else "")
    # 英語の文は「日本語の欄」とする（消耗品の名前・説明文の注意にもそのまま使えるように）
    return [tr(f"英語の設定のゲームのフォントは半角英数字と記号（ASCII）しか持たないため、次の文字は表示されない見込みです: "
               f"{chars}。日本語は日本語名の欄に入れてください",
               f"The game's fonts in English only have ASCII letters, digits and symbols, so these characters will "
               f"probably not be shown: {chars}. Put Japanese text in the Japanese field")]


def _norm_game_path(p):
    return p.replace("\\", "/").lower() if isinstance(p, str) else p


def _retry_locked(fn, tries: int = 20, wait: float = 0.05):
    """fn() を、Windows の共有違反（他のスレッド・プロセスが同じファイルを開いている瞬間。PermissionError）のときだけ
    短い間隔で再試行する。最後まで失敗したら元の例外を投げる。"""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
            # 中身をディスクに書いてから置き換える（停電・強制終了の後に中身の無い equipment.json が残らないように）
            f.flush()
            os.fsync(f.fileno())
        # 置き換え先を他が読んでいる瞬間（ビルド中の装備一覧の読み取りなど）は WinError 5 になるので待って再試行する
        _retry_locked(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, what: str) -> dict:
    """JSON のファイルを読む。読めない（壊れた・途中で切れた・文字コードが違う・中身が辞書でない）ときは、場所の分かる
    LibraryError にする（生の JSONDecodeError では、どのファイルが原因か画面に出ない）。文に「見つかりません」は入れない
    （アプリはその語で NOT_FOUND に分ける。英語の文の "not found" も同じ）。what は表示言語の名詞句。
    BOM 付きの UTF-8（Windows PowerShell 5.1 の Set-Content -Encoding UTF8 や一部のエディターで手直しした記録）も読む
    （1 件で装備一覧・作成・削除・ビルドが止まった。書き込みは BOM なしのまま。2026-10-27 R06A-05）。"""
    def read():
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    try:
        data = _retry_locked(read)
    except (OSError, ValueError) as e:
        raise LibraryError(tr(f"{what}が読めません: {path}（{type(e).__name__}）",
                              f"Cannot read {what}: {path} ({type(e).__name__})")) from None
    if not isinstance(data, dict):
        raise LibraryError(tr(f"{what}が読めません: {path}（JSON の形が違います）",
                              f"Cannot read {what}: {path} (unexpected JSON structure)"))
    return data


def _version(data: dict, path: str) -> int:
    try:
        return int(data.get("version", 0))
    except (TypeError, ValueError):
        raise LibraryError(tr(f"記録の版が不正です: {path}（version {data.get('version')!r}）",
                              f"Invalid record version: {path} (version {data.get('version')!r})")) from None


def _missing_fields(data: dict) -> list[str]:
    """equipment.json の必要な項目のうち、無いか形が違うもの（Equipment の性質・summary と、ビルドの verify_assets・
    plan_records が前提にするもの）。入れ子の項目（naming のレコードのパス・タグ名、assets.files の各要素）も確かめる
    （欠けた記録は load を通った後で装備の分からない生の KeyError になり、装備一覧が止まる・全装備が作成失敗になった。
    2026-10-27 R06A-03・R06B-03）。"""
    bad = [k for k, t in (("display_name", dict), ("created_at", str), ("naming", dict), ("illusion_set", dict),
                          ("generation", dict), ("conversion", dict), ("mod", dict), ("assets", dict))
           if not isinstance(data.get(k), t)]
    if "display_name" not in bad and not isinstance(data["display_name"].get("en"), str):
        bad.append("display_name.en")
    if "illusion_set" not in bad and not isinstance(data["illusion_set"].get("owner"), str):
        bad.append("illusion_set.owner")
    if "naming" not in bad:
        # 消耗品のレコード名・タグ名は消耗品の持ち主だけが持つ（試作の取り込みは持ち主でない装備に None を書く）
        owner = "illusion_set" not in bad and data["illusion_set"].get("owner") == data.get("id")
        keys = ("head_record", "head_tag") + (("set_record", "set_tag", "set_desc_tag") if owner else ())
        bad += [f"naming.{k}" for k in keys if not isinstance(data["naming"].get(k), str)]
    if "assets" not in bad:
        a = data["assets"]
        if not isinstance(a.get("files"), list):
            bad.append("assets.files")
        else:
            for i, f in enumerate(a["files"]):
                if not isinstance(f, dict):
                    bad.append(f"assets.files[{i}]")
                    continue
                bad += [f"assets.files[{i}].{k}" for k in ("file", "game_path", "sha256") if not isinstance(f.get(k), str)]
                if not isinstance(f.get("size"), int) or isinstance(f.get("size"), bool):
                    bad.append(f"assets.files[{i}].size")
        if a.get("head_textures") is not None:
            if not isinstance(a["head_textures"], dict):
                bad.append("assets.head_textures")
            else:
                # 値はゲームのパス（数値などはビルドの norm_path で装備の分からない AttributeError になった）
                bad += [f"assets.head_textures.{k}" for k, v in a["head_textures"].items() if not isinstance(v, str)]
    return bad


def _move_dir(src: str, dst: str, what: str) -> None:
    """同じライブラリの中（同じドライブ）でフォルダーを移す。名前の付け替えだけで移し、コピー＋削除には切り替えない
    （中のファイルを他のプログラムが開いていると、shutil.move はコピーした後の削除で途中で止まり、移動元に一部だけが
    残って装備が半分消えた。R06A-02）。失敗したら何も変えずに LibraryError。別のドライブ（接合点など）のときだけ従来の
    shutil.move を使う。what は表示言語の名詞句（日本語は「装備 <ID> 」など、英語は "equipment <ID>" など）。"""
    try:
        _retry_locked(lambda: os.rename(src, dst), tries=5, wait=0.2)     # ウイルス対策などの一時的なロックは待つ
    except OSError as e:
        if e.errno == errno.EXDEV or getattr(e, "winerror", None) == 17:
            shutil.move(src, dst)
            return
        if isinstance(e, PermissionError):
            raise LibraryError(tr(f"{what}のフォルダーの中のファイルが他のプログラムで使用中のため移動できません。開いている画像・"
                                  f"3D ビューアーや、そのフォルダーを開いているウィンドウ・端末を閉じてから、もう一度試してください"
                                  f"（{src}: {type(e).__name__}）",
                                  f"Cannot move the {what} folder because a file in it is in use by another program. "
                                  f"Close any open image or 3D viewer and any window or terminal that has the folder "
                                  f"open, then try again ({src}: {type(e).__name__})")) from None
        # e の文（OS の言語の文）は入れない（日本語の Windows では「見つかりません」を含むことがあり、NOT_FOUND に化ける）
        raise LibraryError(tr(f"{what}のフォルダーを移動できません: {src} → {dst}（{type(e).__name__}、"
                              f"winerror {getattr(e, 'winerror', None)}）",
                              f"Cannot move the {what} folder: {src} → {dst} ({type(e).__name__}, "
                              f"winerror {getattr(e, 'winerror', None)})")) from None


class Equipment:
    """装備 1 件。``data`` が equipment.json の中身、``dir`` が装備のフォルダー。"""

    def __init__(self, data: dict, directory: str):
        self.data = data
        self.dir = directory

    # よく使う項目
    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def name_en(self) -> str:
        return self.data["display_name"]["en"]

    @property
    def name_ja(self) -> str | None:
        return self.data["display_name"].get("ja")

    @property
    def naming(self) -> dict:
        return self.data["naming"]

    @property
    def set_owner(self) -> str:
        return self.data["illusion_set"]["owner"]

    @property
    def owns_set(self) -> bool:
        return self.set_owner == self.id

    @property
    def created_at(self) -> str:
        return self.data["created_at"]

    @property
    def dev_only(self) -> bool:
        """開発用の装備（D-018）。製品版のビルドには入れない。キーが無い古い装備は生成方法 prototype なら開発用。"""
        v = self.data.get("dev_only")
        if v is None:
            return self.data["generation"].get("method") == "prototype"
        return bool(v)

    def asset_path(self, rel: str) -> str:
        """assets/ からの相対パス rel の場所。assets/ の外（``..``・絶対パス・ドライブ名・UNC）を指すものは LibraryError
        （受け取った装備の記録で、データフォルダーの外や別の装備のファイルに書かないように。2026-10-17 監査 C13）。
        試作の装備はサブフォルダー付きの rel（例 ``aiskin2/aiskin2_box_m.msh``）を持つので、区切りそのものは許す。"""
        if not isinstance(rel, str) or not rel or os.path.isabs(rel) or os.path.splitdrive(rel)[0] \
                or rel.startswith(("/", "\\")) or ":" in rel:
            raise LibraryError(tr(f"装備 {self.id} のアセットのパスが不正です（assets の外を指す）: {rel!r}",
                                  f"Invalid asset path for equipment {self.id} (points outside assets): {rel!r}"))
        base = os.path.abspath(os.path.join(self.dir, "assets"))
        p = os.path.abspath(os.path.join(base, rel.replace("/", os.sep)))
        try:
            inside = os.path.commonpath([base, p]) == base and p != base
        except ValueError:            # ドライブが違う
            inside = False
        if not inside:
            raise LibraryError(tr(f"装備 {self.id} のアセットのパスが不正です（assets の外を指す）: {rel!r}",
                                  f"Invalid asset path for equipment {self.id} (points outside assets): {rel!r}"))
        return p

    def text(self, role: str, lang: str) -> str | None:
        """texts の上書き（無ければ None）。"""
        return (self.data.get("texts") or {}).get(role, {}).get(lang)

    def add_error(self, stage: str, message: str) -> None:
        errs = self.data.setdefault("errors", [])
        errs.insert(0, {"at": now_iso(), "stage": stage, "message": message})
        del errs[MAX_ERRORS:]

    def summary(self) -> dict:
        d = self.data
        return {"id": d["id"], "name": d["display_name"].get("en"), "name_ja": d["display_name"].get("ja"),
                "method": d["generation"].get("method"), "conversion": d["conversion"].get("status"),
                "mod": d["mod"].get("status"), "created_at": d["created_at"], "head_record": d["naming"]["head_record"],
                "dev_only": self.dev_only}


def new_equipment_data(eq_id: str, name_en: str, name_ja: str | None, generation: dict, naming: dict | None = None,
                       created_at: str | None = None) -> dict:
    t = created_at or now_iso()
    gen = {"method": "external", "input_text": None, "input_image": None, "model_id": None, "seed": None,
           "settings": {}, "generated_at": None, "source_glb": None}
    gen.update(generation or {})
    if gen["method"] not in GENERATION_METHODS:
        raise LibraryError(tr(f"生成方法が不正: {gen['method']!r}（{', '.join(GENERATION_METHODS)}）",
                              f"Invalid generation method: {gen['method']!r} ({', '.join(GENERATION_METHODS)})"))
    return {
        "format": EQUIPMENT_FORMAT, "version": FORMAT_VERSION, "id": eq_id,
        "display_name": {"en": name_en, **({"ja": name_ja} if name_ja else {})},
        "texts": {},
        "generation": gen,
        "created_at": t, "updated_at": t,
        "naming": naming or derive_naming(eq_id),
        "illusion_set": {"owner": eq_id, "include_invisible": True},
        "assets": {"male_mesh": None, "female_mesh": None, "head_textures": {}, "files": []},
        "conversion": {"status": "pending", "tool": None, "source_dir": None, "report": None, "converted_at": None,
                       "summary": {}, "error": None},
        "mod": {"status": "not_built", "last_build_id": None, "last_built_at": None, "installed_build_id": None,
                "installed_at": None, "error": None},
        "errors": [],
        "dev_only": gen["method"] == "prototype",
    }


class Library:
    """装備ライブラリ。``Library.open(root, create=True)`` で開く。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    # --- 開く・作る ---------------------------------------------------------------------
    @classmethod
    def open(cls, root: str | None = None, create: bool = False) -> "Library":
        lib = cls(root or paths.library_dir())
        meta = os.path.join(lib.root, "library.json")
        if not os.path.isfile(meta):
            if not create:
                raise LibraryError(tr(f"装備ライブラリがありません: {lib.root}（library init で作成）",
                                      f"There is no equipment library: {lib.root} (create one with library init)"))
            _write_json(meta, {"format": LIBRARY_FORMAT, "version": FORMAT_VERSION, "created_at": now_iso(),
                               "tool": f"modbuilder {__version__}"})
            os.makedirs(lib.equipment_root, exist_ok=True)
        else:
            m = _read_json(meta, tr("装備ライブラリの情報（library.json）", "the equipment library data (library.json)"))
            if m.get("format") != LIBRARY_FORMAT:
                raise LibraryError(tr(f"装備ライブラリの形式ではありません: {meta}", f"Not in the equipment library format: {meta}"))
            if _version(m, meta) > FORMAT_VERSION:
                raise LibraryError(tr(f"新しい版の装備ライブラリです（{m.get('version')} > {FORMAT_VERSION}）: {meta}",
                                      f"The equipment library is from a newer version "
                                      f"({m.get('version')} > {FORMAT_VERSION}): {meta}"))
        return lib

    @property
    def equipment_root(self) -> str:
        return os.path.join(self.root, "equipment")

    # --- 読み書き -------------------------------------------------------------------------
    def ids(self) -> list[str]:
        """装備のフォルダーの名前（ID の形のものだけ）。エクスプローラーで作った写し（「<ID> - コピー」）などの ID の形で
        ない名前は装備として読めず、create・取り込みでも作られないので並べない（1 つで装備一覧・作成・削除・ビルドが
        止まった。2026-10-27 R06A-06）。"""
        if not os.path.isdir(self.equipment_root):
            return []
        return sorted(d for d in os.listdir(self.equipment_root)
                      if ID_RE.match(d) and os.path.isfile(os.path.join(self.equipment_root, d, "equipment.json")))

    def load(self, eq_id: str) -> Equipment:
        d = os.path.join(self.equipment_root, eq_id)
        p = os.path.join(d, "equipment.json")
        if not os.path.isfile(p):
            raise LibraryError(tr(f"装備がありません: {eq_id}", f"No such equipment: {eq_id}"))
        # 壊れた equipment.json は、どの装備かの分かる LibraryError にする（1 件でも生の JSONDecodeError・KeyError だと、
        # 装備一覧・作成・削除・ビルドがすべて原因の分からない想定外のエラーになった。R06A-04）
        data = _read_json(p, tr(f"装備 {eq_id} の情報（equipment.json）", f"the data of equipment {eq_id} (equipment.json)"))
        if data.get("format") != EQUIPMENT_FORMAT or data.get("id") != eq_id:
            raise LibraryError(tr(f"装備の情報が不正です: {p}", f"Invalid equipment data: {p}"))
        if _version(data, p) > FORMAT_VERSION:
            raise LibraryError(tr(f"新しい版の装備情報です: {p}", f"The equipment data is from a newer version: {p}"))
        missing = _missing_fields(data)
        if missing:
            raise LibraryError(tr(f"装備 {eq_id} の情報（equipment.json）に必要な項目が無いか形が違います: "
                                  f"{', '.join(missing)}（{p}）",
                                  f"The data of equipment {eq_id} (equipment.json) has missing fields or fields of the "
                                  f"wrong type: {', '.join(missing)} ({p})"))
        return Equipment(data, d)

    def list(self) -> list[Equipment]:
        """作成順（created_at、同時刻は ID 順）。補給官の在庫・ビルドの並びもこの順。"""
        items = [self.load(i) for i in self.ids()]
        return sorted(items, key=lambda e: (e.created_at, e.id))

    def save(self, eq: Equipment, touch: bool = True) -> None:
        if touch:
            eq.data["updated_at"] = now_iso()
        _write_json(os.path.join(eq.dir, "equipment.json"), eq.data)

    def find(self, key: str) -> Equipment:
        """ID または表示名（英語・日本語。大文字小文字は区別しない）で 1 件を探す。"""
        if key in self.ids():
            return self.load(key)
        k = key.strip().lower()
        hits = [e for e in self.list() if k in ((e.name_en or "").lower(), (e.name_ja or "").lower())]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            # 英語の文は "not found" を含める（日本語の文の「見つかりません」と対。アプリの NOT_FOUND の振り分け）
            raise LibraryError(tr(f"装備が見つかりません: {key}", f"Equipment not found: {key}"))
        names = ", ".join(f"{e.id} ({e.name_en})" for e in hits)
        raise LibraryError(tr(f"同じ名前の装備が {len(hits)} 件あります。ID で指定してください: {names}",
                              f"There are {len(hits)} equipment with the same name. Specify one by ID: {names}"))

    # --- 作成・編集・削除 ----------------------------------------------------------------
    def create(self, name_en: str, name_ja: str | None = None, generation: dict | None = None,
               eq_id: str | None = None, naming: dict | None = None, created_at: str | None = None) -> Equipment:
        problems = validate_display_name(name_en, "en") + (validate_display_name(name_ja, "ja") if name_ja else [])
        if problems:
            raise LibraryError(tr(f"表示名: {'; '.join(problems)}", f"Name: {'; '.join(problems)}"))
        existing = set(self.ids())
        if eq_id is None:
            eq_id = new_equipment_id(existing)
        elif not ID_RE.match(eq_id) or eq_id in existing:
            raise LibraryError(tr(f"装備IDが不正か既に使われています: {eq_id}",
                                  f"The equipment ID is invalid or already in use: {eq_id}"))
        self._check_naming_unique(naming or derive_naming(eq_id), eq_id)
        data = new_equipment_data(eq_id, name_en, name_ja, generation or {}, naming, created_at)
        eq = Equipment(data, os.path.join(self.equipment_root, eq_id))
        os.makedirs(os.path.join(eq.dir, "assets"), exist_ok=True)
        os.makedirs(os.path.join(eq.dir, "source"), exist_ok=True)
        self.save(eq, touch=False)
        return eq

    def _check_naming_unique(self, naming: dict, eq_id: str) -> None:
        keys = ("head_record", "set_record", "head_tag", "set_tag", "set_desc_tag")   # アセットの重なりはビルドで検査
        mine = {naming.get(k) for k in keys} - {None}
        for other in self.list():
            if other.id == eq_id:
                continue
            theirs = {other.naming.get(k) for k in keys} - {None}
            clash = mine & theirs
            if clash:
                raise LibraryError(tr(f"レコードのパス・タグ名が装備 {other.id} と重なります: {sorted(clash)}",
                                      f"Record paths or tag names overlap with equipment {other.id}: {sorted(clash)}"))

    def rename(self, key: str, name_en: str | None = None, name_ja: str | None = None) -> Equipment:
        eq = self.find(key)
        # 名前が変わらないなら記録も MOD の状態も変えない（同じ名前で built / installed が not_built に戻らないように。
        # CLI の library rename と PATCH /library/<id> も通る。R19A-04）
        before = copy.deepcopy((eq.data["display_name"], eq.data.get("texts")))
        if name_en is not None:
            p = validate_display_name(name_en, "en")
            if p:
                raise LibraryError(tr(f"表示名: {'; '.join(p)}", f"Name: {'; '.join(p)}"))
            old = eq.data["display_name"]["en"]
            head = (eq.data.get("texts") or {}).get("head") or {}
            if head.get("en") == old and name_en != old:
                # 試作から取り込んだ装備は texts.head.en に取り込みの時の名前が入っている（値は表示名と同じ）。残すと
                # 改名してもゲーム内の英語の頭装備の名前が変わらないので、表示名と同じ上書きは外す（2026-10-17 点検 C76）
                head.pop("en")
                if not head:
                    eq.data["texts"].pop("head")
            eq.data["display_name"]["en"] = name_en
        if name_ja is not None:
            if name_ja == "":
                eq.data["display_name"].pop("ja", None)
            else:
                p = validate_display_name(name_ja, "ja")
                if p:
                    raise LibraryError(tr(f"日本語名: {'; '.join(p)}", f"Japanese name: {'; '.join(p)}"))
                eq.data["display_name"]["ja"] = name_ja
        if (eq.data["display_name"], eq.data.get("texts")) == before:
            return eq
        self._mark_changed(eq)
        self.save(eq)
        return eq

    def set_texts(self, key: str, role: str, lang: str, value: str | None) -> Equipment:
        if role not in ("head", "set", "set_desc"):
            raise LibraryError(tr(f"texts の役割が不正: {role}", f"Invalid texts role: {role}"))
        eq = self.find(key)
        texts = eq.data.setdefault("texts", {})
        if value is None:
            texts.get(role, {}).pop(lang, None)
        else:
            p = validate_display_name(value, lang, DESC_MAX if role == "set_desc" else NAME_MAX)
            if p:
                raise LibraryError(f"texts.{role}.{lang}: " + "; ".join(p))
            texts.setdefault(role, {})[lang] = value
        self._mark_changed(eq)
        self.save(eq)
        return eq

    def remove(self, key: str) -> str:
        """装備をライブラリから外し、trash/ へ移す（完全には削除しない）。戻り値は移動先。
        他の装備がこの装備の消耗品を共有している場合は外せない。"""
        eq = self.find(key)
        members = [e.id for e in self.list() if e.set_owner == eq.id and e.id != eq.id]
        if members:
            raise LibraryError(tr(f"装備 {eq.id} の消耗品を他の装備が共有しています: {members}",
                                  f"Other equipment shares the consumable of equipment {eq.id}: {members}"))
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(self.root, "trash", f"{eq.id}-{stamp}")
        n = 2
        while os.path.exists(dst):          # 同じ秒に同じ装備を 2 回外したとき（戻して外すなど）。既存のフォルダーの中に入れない
            dst = os.path.join(self.root, "trash", f"{eq.id}-{stamp}-{n}")
            n += 1
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _move_dir(eq.dir, dst, tr(f"装備 {eq.id} ", f"equipment {eq.id}"))     # 移せなければ何も変えずに LibraryError（R06A-02）
        return dst

    # --- 削除した装備（trash）の一覧と復元（2026-10-17 機能 SF02） ----------------------------------
    @property
    def trash_root(self) -> str:
        return os.path.join(self.root, "trash")

    def _trash_path(self, name: str) -> str:
        """trash の直下のフォルダー name の場所。区切り・``..``・ドライブ名を含む名前や trash の外を指す名前は LibraryError
        （trash は利用者が触れる場所で、名前は画面から送られてくるので）。"""
        # 末尾の点も断る: Windows のパスの正規化は末尾の点を落とすので、"..." は trash そのもの、"a." は trash\a を指す（U-C-R1）
        if not isinstance(name, str) or not name or name.endswith(".") or name != name.strip() \
                or any(c in name for c in "/\\:") or any(unicodedata.category(c).startswith("C") for c in name):
            raise LibraryError(tr(f"trash のフォルダー名が不正です: {name!r}", f"Invalid trash folder name: {name!r}"))
        base = os.path.abspath(self.trash_root)
        p = os.path.abspath(os.path.join(base, name))
        if os.path.dirname(p) != base or os.path.basename(p) != name:
            raise LibraryError(tr(f"trash のフォルダー名が不正です: {name!r}", f"Invalid trash folder name: {name!r}"))
        return p

    def _trash_dir(self, name: str) -> str | None:
        """trash の直下のフォルダー name の場所（無ければ None。名前が不正なら LibraryError）。trash の一覧の名前と完全に
        一致するものだけを選ぶ（Windows は大文字・小文字の違いや 8.3 形式の短い名前でも同じフォルダーを開くので、別名では
        選べないようにする。U-C-R1）。"""
        p = self._trash_path(name)
        try:
            names = os.listdir(self.trash_root)
        except OSError:
            return None
        return p if name in names and os.path.isdir(p) else None

    @staticmethod
    def _load_trashed(d: str) -> Equipment:
        """trash のフォルダーの装備を読む（load と同じ検査。ID はフォルダー名ではなく equipment.json の id）。"""
        p = os.path.join(d, "equipment.json")
        if not os.path.isfile(p):
            raise LibraryError(tr(f"装備の情報（equipment.json）がありません: {os.path.basename(d)}",
                                  f"The equipment data (equipment.json) is missing: {os.path.basename(d)}"))
        try:
            with open(p, encoding="utf-8-sig") as f:           # BOM 付きの UTF-8 も読む（_read_json と同じ。R06A-05）
                data = json.load(f)
            version = int(data.get("version", 0))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            raise LibraryError(tr(f"装備の情報が読めません: {os.path.basename(d)}（{type(e).__name__}）",
                                  f"Cannot read the equipment data: {os.path.basename(d)} "
                                  f"({type(e).__name__})")) from None
        if data.get("format") != EQUIPMENT_FORMAT or not isinstance(data.get("id"), str) or not ID_RE.match(data["id"]):
            raise LibraryError(tr(f"装備の情報が不正です: {os.path.basename(d)}",
                                  f"Invalid equipment data: {os.path.basename(d)}"))
        if version > FORMAT_VERSION:
            raise LibraryError(tr(f"新しい版の装備情報です: {os.path.basename(d)}",
                                  f"The equipment data is from a newer version: {os.path.basename(d)}"))
        # load と同じく必要な項目も確かめる（確かめずに「戻せる」と表示し、戻した後の load で止まって、装備が trash から
        # 消えたまま装備一覧が使えなくなった。2026-10-27 R06A-02・R06B-02）
        missing = _missing_fields(data)
        if missing:
            raise LibraryError(tr(f"装備 {data['id']} の情報（equipment.json）に必要な項目が無いか形が違います: "
                                  f"{', '.join(missing)}（{os.path.basename(d)}）",
                                  f"The data of equipment {data['id']} (equipment.json) has missing fields or fields of "
                                  f"the wrong type: {', '.join(missing)} ({os.path.basename(d)})"))
        return Equipment(data, d)

    def trash_restore_problems(self, eq: Equipment, others: list[Equipment] | None = None) -> list[str]:
        """trash の装備を equipment/ に戻せない理由（空なら戻せる）。同じ内部ID・レコードのパス・タグ名・アセットの
        ゲーム内パスの重なりと、消耗品をまとめている装備が装備一覧に無いこと。others は今の装備一覧（省くと読む）。"""
        others = self.list() if others is None else others
        ids = {o.id for o in others}
        problems = []
        if eq.id in ids or os.path.exists(os.path.join(self.equipment_root, eq.id)):
            problems.append(tr(f"同じ内部ID（{eq.id}）の装備が装備一覧にあります（装備パッケージで置き換えた装備など）",
                               f"Equipment with the same internal ID ({eq.id}) is in the Library (for example, "
                               f"equipment replaced by an equipment package)"))
        try:
            keys = ("head_record", "set_record", "head_tag", "set_tag", "set_desc_tag")
            mine = {eq.naming.get(k) for k in keys} - {None}
            mine_assets = {_norm_game_path(f.get("game_path")) for f in eq.data["assets"].get("files") or []} - {None}
            owner = eq.data["illusion_set"]["owner"]
        except (KeyError, TypeError, AttributeError):
            return problems + [tr("装備の情報が不正です（naming・assets・illusion_set）",
                                  "Invalid equipment data (naming, assets, illusion_set)")]
        for other in others:
            if other.id == eq.id:
                continue
            clash = mine & ({other.naming.get(k) for k in keys} - {None})
            if clash:
                problems.append(tr(f"レコードのパス・タグ名が装備 {other.id} と重なります: {sorted(clash)}",
                                   f"Record paths or tag names overlap with equipment {other.id}: {sorted(clash)}"))
            theirs = {_norm_game_path(f.get("game_path")) for f in other.data["assets"].get("files") or []}
            if mine_assets & theirs:
                problems.append(tr(f"アセットのパスが装備 {other.id} と重なります: {sorted(mine_assets & theirs)[:3]}",
                                   f"Asset paths overlap with equipment {other.id}: "
                                   f"{sorted(mine_assets & theirs)[:3]}"))
        if owner != eq.id and owner not in ids:
            problems.append(tr(f"消耗品をまとめている装備 {owner} が装備一覧にありません（先にその装備を戻してください）",
                               f"Equipment {owner}, which groups the consumables, is not in the Library (restore that "
                               f"equipment first)"))
        return problems

    def trash_items(self) -> list[dict]:
        """trash の装備の一覧（外した日時の新しい順）。各項目は summary() の項目と ``trash_name``（フォルダー名）・
        ``removed_at``（ISO 8601。フォルダー名の日時、読めなければフォルダーの更新時刻）・``problems``（戻せない理由）・
        ``restorable``・``preview_png``（確認画像。無ければ None）。読めないフォルダーも problems 付きで並べる。"""
        if not os.path.isdir(self.trash_root):
            return []
        others = self.list()
        out = []
        for name in os.listdir(self.trash_root):
            d = os.path.join(self.trash_root, name)
            if not os.path.isdir(d):
                continue
            m = re.search(r"-(\d{8}-\d{6})(?:-\d+)?$", name)
            removed = None
            if m:
                try:
                    removed = _dt.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").astimezone().isoformat()
                except ValueError:
                    removed = None
            if removed is None:
                removed = _dt.datetime.fromtimestamp(os.path.getmtime(d)).astimezone().isoformat(timespec="seconds")
            item = {"trash_name": name, "removed_at": removed}
            try:
                eq = self._load_trashed(d)
                item.update(eq.summary())
                item["problems"] = self.trash_restore_problems(eq, others)
            except (LibraryError, KeyError, TypeError, AttributeError) as e:
                item.update(id=None, name=None, name_ja=None)
                item["problems"] = [str(e) if isinstance(e, LibraryError) else
                                    tr(f"装備の情報が不正です: {name}", f"Invalid equipment data: {name}")]
            prev = os.path.join(d, "source", "preview.png")
            item["preview_png"] = prev if os.path.isfile(prev) else None
            item["restorable"] = not item["problems"]
            out.append(item)
        out.sort(key=lambda x: (x["removed_at"], x["trash_name"]), reverse=True)
        return out

    def restore_from_trash(self, name: str) -> Equipment:
        """trash の装備（フォルダー名 name）を、同じ内部ID・レコードのまま equipment/ に戻す。戻せない理由があれば
        LibraryError（何も変えない）。MOD はビルド済み・導入済みでも「作成が必要」（not_built）にする（今の MOD に
        入っているかは分からないので）。"""
        src = self._trash_dir(name)
        if src is None:
            # 英語の文は "not found" を含める（日本語の文の「見つかりません」と対。アプリの NOT_FOUND の振り分け）
            raise LibraryError(tr(f"trash に装備が見つかりません: {name}", f"Equipment not found in trash: {name}"))
        eq = self._load_trashed(src)
        problems = self.trash_restore_problems(eq)
        if problems:
            raise LibraryError(tr(f"装備 {eq.id} を元に戻せません: {'; '.join(problems)}",
                                  f"Cannot restore equipment {eq.id}: {'; '.join(problems)}"))
        os.makedirs(self.equipment_root, exist_ok=True)
        dst = os.path.join(self.equipment_root, eq.id)
        what = tr(f"削除した装備 {eq.id} ", f"deleted equipment {eq.id}")
        _move_dir(src, dst, what)
        try:
            eq = self.load(eq.id)
            self._mark_changed(eq)
            self.save(eq)
        except BaseException:
            # 戻した後で読めない・保存できなければ trash の元の名前へ戻す（「何も変えない」を守る。戻せずに equipment/ に
            # 残すと装備一覧が使えなくなる。R06A-02・R06B-02）
            try:
                _move_dir(dst, src, what)
            except LibraryError:
                pass
            raise
        return eq

    # --- アセット -------------------------------------------------------------------------
    def set_assets(self, eq: Equipment, files: list[tuple[str, bytes, str]], male_mesh: str, female_mesh: str,
                   head_textures: dict | None = None, keep_backup: bool = False) -> str | None:
        """assets/ を作り直す。files = [(assets/ からの相対パス, 中身, ゲーム内パス)]。

        新しいファイルを assets.new/ に全部書いてから、assets/ → assets.old/、assets.new/ → assets/ の名前の付け替えで
        入れ替える（先に assets/ を消してから書くと、途中の失敗で元のアセットが消え、記録と食い違ったまま残った。
        R06B-03）。途中で失敗したら元の assets/ のまま例外を投げる。keep_backup なら元の assets/（assets.old/）を消さずに
        その場所を返す（呼び出し元が equipment.json の保存まで済ませてから消す・失敗したら戻す。元が無ければ None）。"""
        targets = [eq.asset_path(rel) for rel, _d, _gp in files]     # assets の外を指すものは書く前に LibraryError（C13）
        # ゲーム内パス（file_* の欄の値）は小文字・'/' 区切りで保存する（compile の後の値と作業フォルダーの .dbr が
        # 食い違わないように。2026-10-17 点検 C78）。項目の検査も書く前に行う
        ht = {k: _norm_game_path(v) for k, v in (head_textures or {}).items()}
        bad = set(ht) - set(HEAD_TEXTURE_FIELDS)
        if bad:
            raise LibraryError(tr(f"head_textures に指定できない項目: {sorted(bad)}",
                                  f"Fields not allowed in head_textures: {sorted(bad)}"))
        adir = os.path.join(eq.dir, "assets")
        new_dir, old_dir = adir + ".new", adir + ".old"
        what = tr(f"装備 {eq.id} のアセット", f"equipment {eq.id} assets")       # _move_dir の文の名詞句
        # 前の異常終了の残り: 入れ替えの途中（assets が無く assets.old だけある）なら元に戻し、それ以外は消す
        if not os.path.isdir(adir) and os.path.isdir(old_dir):
            _move_dir(old_dir, adir, what)
        for d in (new_dir, old_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
        recs = []
        try:
            os.makedirs(new_dir)
            for (rel, data, game_path), p in zip(files, targets):
                q = os.path.join(new_dir, os.path.relpath(p, adir))
                os.makedirs(os.path.dirname(q), exist_ok=True)
                with open(q, "wb") as f:
                    f.write(data)
                recs.append({"file": rel.replace("\\", "/"), "game_path": game_path,
                             "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            had_old = os.path.isdir(adir)
            if had_old:
                # 中のファイルを他のプログラムが開いていればここで失敗し、元の assets/ は何も変わらない
                _move_dir(adir, old_dir, what)
            try:
                _move_dir(new_dir, adir, what)
            except BaseException:
                if had_old:
                    _move_dir(old_dir, adir, what)
                raise
        except BaseException:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise
        eq.data["assets"] = {"male_mesh": _norm_game_path(male_mesh), "female_mesh": _norm_game_path(female_mesh),
                             "head_textures": ht, "files": recs}
        self._mark_changed(eq)
        if not had_old:
            return None
        if keep_backup:
            return old_dir
        shutil.rmtree(old_dir, ignore_errors=True)
        return None

    def verify_assets(self, eq: Equipment) -> list[str]:
        """assets/ のファイルが equipment.json の記録（sha256・大きさ）と一致するか。"""
        problems = []
        # アセットの入れ替えの途中で止まった残り（assets.new・assets.old・assets.failed）があれば、ライブラリの外での変更
        # ではなくそのことを示す（利用者が外で何かしたかのような案内になっていた。2026-10-27 X6X-04）
        interrupted = any(os.path.isdir(os.path.join(eq.dir, "assets" + s)) for s in (".new", ".old", ".failed"))
        for f in eq.data["assets"]["files"]:
            try:
                p = eq.asset_path(f["file"])
            except LibraryError as e:          # assets の外を指す記録（C13）
                problems.append(str(e))
                continue
            if not os.path.isfile(p):
                if interrupted:
                    problems.append(tr(f"{eq.id}: アセットがありません（前回のアセットの差し替えが途中で止まりました。MOD を"
                                       f"作成し直しても直らなければ、変換し直すか、装備を取り込み直してください）: {f['file']}",
                                       f"{eq.id}: Asset missing (the previous asset replacement stopped partway; if "
                                       f"building the MOD again does not fix it, reconvert or re-import the "
                                       f"equipment): {f['file']}"))
                else:
                    problems.append(tr(f"{eq.id}: アセットがありません: {f['file']}", f"{eq.id}: Asset missing: {f['file']}"))
            elif os.path.getsize(p) != f["size"] or sha256_file(p) != f["sha256"]:
                if interrupted:
                    problems.append(tr(f"{eq.id}: アセットが記録と一致しません（前回のアセットの差し替えが途中で止まりました。"
                                       f"MOD を作成し直しても直らなければ、変換し直すか、装備を取り込み直してください）: "
                                       f"{f['file']}",
                                       f"{eq.id}: Asset does not match the record (the previous asset replacement stopped "
                                       f"partway; if building the MOD again does not fix it, reconvert or re-import "
                                       f"the equipment): {f['file']}"))
                else:
                    problems.append(tr(f"{eq.id}: アセットが記録と一致しません（ライブラリの外で変更された）: {f['file']}",
                                       f"{eq.id}: Asset does not match the record (changed outside the library): "
                                       f"{f['file']}"))
        return problems

    def _assets_match(self, eq: Equipment, d: str) -> bool:
        """フォルダー d（assets/ と同じ並びのフォルダー）のファイルが equipment.json の記録と全て一致するか。"""
        if not os.path.isdir(d):
            return False
        adir = os.path.join(eq.dir, "assets")
        for f in eq.data["assets"]["files"]:
            try:
                p = os.path.join(d, os.path.relpath(eq.asset_path(f["file"]), adir))
            except LibraryError:
                return False
            if not os.path.isfile(p) or os.path.getsize(p) != f["size"] or sha256_file(p) != f["sha256"]:
                return False
        return True

    def recover_interrupted_assets(self) -> dict[str, str]:
        """アセットの入れ替え（set_assets・replace_converted の巻き戻し）の途中で異常終了した残り（assets.new・assets.old・
        assets.failed）を片付け、equipment.json の記録と一致するアセットを assets/ に戻す（次にその装備の set_assets が
        呼ばれるまで直らず、その間 MOD の作成全体が止まった。2026-10-27 X6X-04）。

        アセットの入れ替えと同時に動かない所で呼ぶ: MOD の作成の始め（builder.build。アプリのビルドは装備の変更と同じ
        mod_lock の中）と、アプリの起動時の後始末（ジョブを受け付ける前）。load・装備一覧の中では呼ばない（別のスレッドの
        入れ替えの途中を残りと誤認するため）。戻り値は残りのあった装備の {装備ID: "recovered"（記録と一致するアセットにした）|
        "unresolved"（どれも記録と一致しない。assets/ が無ければ assets.old を戻し、ほかは消さない）}。読めない装備は飛ばす。"""
        out = {}
        for i in self.ids():
            adir = os.path.join(self.equipment_root, i, "assets")
            rest = {s: adir + s for s in (".new", ".old", ".failed") if os.path.isdir(adir + s)}
            if not rest:
                continue
            try:
                eq = self.load(i)
            except LibraryError:
                continue
            what = tr(f"装備 {i} のアセット", f"equipment {i} assets")       # _move_dir の文の名詞句
            try:
                if not self._assets_match(eq, adir):
                    good = next((rest[s] for s in (".old", ".new") if s in rest and self._assets_match(eq, rest[s])), None)
                    if good is None:
                        if not os.path.isdir(adir) and ".old" in rest:
                            _move_dir(rest[".old"], adir, what)            # 入れ替えの途中（set_assets と同じく元に戻す）
                        out[i] = "unresolved"
                        continue
                    if os.path.isdir(adir):
                        # 記録と一致しない assets/ は消さずに assets.failed/ へ退けてから戻す（replace_converted と同じ）
                        shutil.rmtree(adir + ".failed", ignore_errors=True)
                        _move_dir(adir, adir + ".failed", what)
                    _move_dir(good, adir, what)
                for s in (".new", ".old", ".failed"):
                    shutil.rmtree(adir + s, ignore_errors=True)
                out[i] = "recovered"
            except (LibraryError, OSError):
                out[i] = "unresolved"
        return out

    # --- 状態 ------------------------------------------------------------------------------
    @staticmethod
    def _mark_changed(eq: Equipment) -> None:
        """内容が変わったら、以前のビルド・導入は古いことを示す（installed → built は導入状態の記録で分かる）。"""
        m = eq.data["mod"]
        if m.get("status") in ("built", "installed"):
            m["status"] = "not_built"

    def _update_each(self, ids, update) -> None:
        """装備 ids をそれぞれ読み、update(eq) してから保存する。読めない・保存できない装備があっても残りの装備の更新を
        続け、最後にその装備IDと理由を並べた LibraryError にする（途中で止まると、後ろの装備の状態が古いまま残った。
        2026-10-27 R06B-05）。"""
        failed = []
        for i in ids:
            try:
                eq = self.load(i)
                update(eq)
                self.save(eq, touch=False)
            except LibraryError as e:
                failed.append(f"{i}: {e}")
            except OSError as e:
                # e の文（OS の言語の文）は入れない（日本語の Windows では「見つかりません」を含み、NOT_FOUND に化ける）
                failed.append(f"{i}: {type(e).__name__} (winerror {getattr(e, 'winerror', None)})")
        if failed:
            raise LibraryError(tr(f"状態を更新できなかった装備があります（{len(failed)} 件）: {'; '.join(failed)}",
                                  f"The status of some equipment could not be updated ({len(failed)}): "
                                  f"{'; '.join(failed)}"))

    def mark_built(self, ids: list[str], build_id: str) -> None:
        t = now_iso()

        def update(eq):
            eq.data["mod"].update({"status": "built", "last_build_id": build_id, "last_built_at": t, "error": None})
        self._update_each(ids, update)

    def mark_build_failed(self, ids: list[str], message: str) -> None:
        def update(eq):
            eq.data["mod"].update({"status": "build_failed", "error": message})
            eq.add_error("build", message)
        self._update_each(ids, update)

    def mark_installed(self, ids: list[str], build_id: str) -> None:
        """導入したビルド build_id に入っている装備 ids を導入済みにする。ただし installed にするのは、そのビルドの後に
        内容を変えていない装備（状態が built / installed で last_build_id が build_id）だけ。ビルドの後に編集した装備
        （not_built）・作成に失敗した装備（build_failed）・別のビルドで作った装備は、状態を変えずに導入の記録
        （installed_build_id・installed_at）だけを残す（古いビルドの導入で「作り直しが必要」が消えないように。R06A-01）。
        ただし別のビルドで作って導入済み（installed）だった装備は built に戻す（新しいビルドを導入した後に古いビルドへ
        戻すと、ゲームには古い内容が入っているのに「導入済み」のまま残ったため）。
        ids に無い装備は導入の記録を消し、installed なら built（ビルドが無ければ not_built）に戻す。
        読めない装備があっても残りの装備は更新し、最後に LibraryError にする（R06B-05）。"""
        t = now_iso()

        def update(eq):
            m = eq.data["mod"]
            if eq.id in ids:
                if m.get("status") in ("built", "installed") and m.get("last_build_id") == build_id:
                    m["status"] = "installed"
                elif m.get("status") == "installed":
                    m["status"] = "built" if m.get("last_build_id") else "not_built"
                m.update({"installed_build_id": build_id, "installed_at": t})
            else:
                if m.get("status") == "installed":
                    m["status"] = "built" if m.get("last_build_id") else "not_built"
                m.update({"installed_build_id": None, "installed_at": None})
        self._update_each(self.ids(), update)

    def mark_uninstalled(self) -> None:
        self.mark_installed([], "")

    def set_conversion(self, eq: Equipment, **kw) -> None:
        c = eq.data["conversion"]
        for k, v in kw.items():
            if k == "status" and v not in CONVERSION_STATUSES:
                raise LibraryError(tr(f"変換状態が不正: {v}", f"Invalid conversion status: {v}"))
            c[k] = copy.deepcopy(v)
