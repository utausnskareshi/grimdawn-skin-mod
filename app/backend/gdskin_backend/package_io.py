"""共有ファイル（装備パッケージ ``.gdskin`` / MOD の共有ファイル ``.gdskinmod``。中身は zip）の読み書きの共通部分。
docs/app_architecture.md 15 章。

* 書き出し（:class:`PackageWriter`）: 一時ファイルに書いてから置き換える。中身のファイルごとに SHA-256・大きさを
  記録ファイル（``gdskin_package.json`` / ``gdskin_mod.json``）の ``files`` に書き、``files`` から ``package_id``
  （中身が同じなら同じ値）を作る。
* 読み込み（:func:`open_package`）: 他の利用者から受け取ったファイルは信頼しない。次を確かめてから中身を使う。

  - zip として読めるか、暗号化・未対応の圧縮が無いか、項目数・大きさの上限（展開の爆弾を防ぐ）
  - 項目の名前（``..``・絶対パス・ドライブ名・``\\``・制御文字を拒否。同じ名前の重複も拒否）
  - 記録ファイルの形式・版（新しい版のアプリで作ったものは読まない）
  - 全ての項目が ``files`` に記録され、SHA-256・大きさが一致すること（壊れたファイル・一部の書き換えの検出）。
    大きさは中央ディレクトリの値に加えて、展開して読んだバイト数でも照合する（2026-10-17 監査 C97）。
    zip 自体の CRC-32 も展開のときに照合される。``package_id`` も計算し直して照合する。

  壊れた zip（deflate の破損・途中で切れたデータ・未対応の形式）は、どこで読んでも PACKAGE_INVALID（reason broken）に
  そろえる（:data:`ZIP_READ_ERRORS`。2026-10-17 監査 C96。以前は zlib.error などが INTERNAL になった）。

  ハッシュは署名ではない: 中身と記録ファイルを一緒に作り直したもの（作った人自身の改変）は区別できない。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import unicodedata
import zipfile
import zlib

from .errors import BackendError
from .i18n import tr

FORMAT_VERSION = 1
KINDS = {
    "equipment": {"manifest": "gdskin_package.json", "format": "gdskin-equipment-package", "ext": ".gdskin"},
    "mod": {"manifest": "gdskin_mod.json", "format": "gdskin-mod-package", "ext": ".gdskinmod"},
}
MAX_ENTRIES = 4000
MAX_FILE = 1024 * 1024 * 1024          # 1 GiB（Items.arc でも十分に大きい）
MAX_TOTAL = 4 * 1024 * 1024 * 1024     # 4 GiB
# 記録ファイルの上限（正規の記録は装備 200 件分でも数百 KB。以前の 8 MiB は、記録の tool などを装備ごとに写す取り込みで
# ライブラリを大きく膨らませられた。2026-10-27 検証 R12B-01）
MAX_MANIFEST = 2 * 1024 * 1024
MAX_RATIO = 400                        # 展開後 / 圧縮後（極端な比は展開の爆弾）
# 共有ファイル全体の展開後の大きさの比の上限（項目ごとの比は 1 MiB 以下の項目を並べると効かない。本ツールの .tex・.msh の
# 比は 1.2〜1.5、.arz・.arc・画像は無圧縮でほぼ 1。展開後が AMPLIFY_MIN を超えるときだけ見る。2026-10-27 検証 R12B-06）
MAX_TOTAL_RATIO = 20
AMPLIFY_MIN = 64 * 1024 * 1024
NAME_PART = r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}"
# 末尾は \Z（$ は末尾の改行の直前でも一致し、"…/input.png\n" を正しい名前とみなした。2026-10-27 検証 R12A-05）
NAME_RE = re.compile(rf"^{NAME_PART}(?:/{NAME_PART}){{0,7}}\Z")
PACKAGE_ID_RE = re.compile(r"[0-9a-f]{16}")      # package_id（package_id_of の値。受け取った MOD のフォルダー名にも使う）
STORED_EXT = (".png", ".jpg", ".arz", ".arc")    # 既に圧縮されている形式は zip で圧縮しない
# 共有ファイル（zip）を読むときに、ファイルの破損・形式の違いで出る例外（CRC-32 の不一致・deflate の破損・途中で
# 切れたデータ・未対応の形式・名前の文字コード・読み込みの失敗）。いずれも PACKAGE_INVALID の broken にする。
# 暗号化された項目は open_package の項目の検査で unsafe にする（zipfile が出す RuntimeError はここに含めない）
ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, UnicodeDecodeError, OSError)
# 信頼しない JSON を読むときの例外（深い入れ子は RecursionError になる。2026-10-27 検証 R12B-03）
JSON_ERRORS = (UnicodeDecodeError, ValueError, RecursionError)
TOOL_KEYS = ("app", "backend", "modbuilder", "gdconvert")
TOOL_VALUE_MAX = 64
_DISPLAY_SCAN = 4096                   # 表示用の文字列で、上限の長さより後ろを調べる文字数（display_text）


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def package_id_of(files: dict) -> str:
    """中身のファイルの一覧（名前・SHA-256・大きさ）から作る ID（16 進 16 桁）。中身が同じなら同じ値。"""
    canon = json.dumps({k: [v["sha256"], v["size"]] for k, v in sorted(files.items())}, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _err(reason: str, message: str, **params) -> BackendError:
    return BackendError("PACKAGE_INVALID", message, params={"reason": reason, **params})


def _broken(name: str, e: BaseException) -> BackendError:
    return _err("broken", tr(f"{name} を展開できません（ファイルが壊れています: {type(e).__name__}: {e}）",
                             f"Cannot extract {name} (the file is corrupted: {type(e).__name__}: {e})"), file=name)


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name)) and ".." not in name.split("/")


def _flat(s: str) -> str:
    """制御文字・書式の文字（改行・タブ・U+202E などの向きの指定）・行と段落の区切りを空白にし、前後の空白を除く。"""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in s).strip()


def display_text(v, n: int = 64) -> str | None:
    """受け取った記録ファイルの表示用の文字列（作成日時・ビルドID・装備の名前など）を 1 行の短い文にする。文字列で
    なければ None。:func:`_flat` の文字を空白にし、n 文字を超えたら切って「…」を付ける（相手が書いた改行でアプリの文に
    見える行を確認ダイアログに混ぜる、数 MB の文で画面が止まる、を防ぐ。2026-10-27 点検 R19B-01）。"""
    if not isinstance(v, str):
        return None
    scan = n + _DISPLAY_SCAN                 # 先頭の一部だけを調べる（数 MB の値を 1 文字ずつ見ない）
    s = _flat(v[:scan])
    return s if len(s) <= n and len(v) <= scan else s[:n] + "…"


def display_tool(t) -> dict:
    """記録ファイルの tool（作成ツールの版。表示用）を、既知のキー・短い文字列だけにする（tool は package_id の対象外で、
    書き換えた大きな値が確認の応答や、取り込んだ装備ごとの記録に写された。2026-10-27 検証 R12B-01）。書式の文字・行の
    区切りも :func:`_flat` で除く（2026-10-27 点検 R19B-01）。"""
    if not isinstance(t, dict):
        return {}
    return {k: _flat(t[k][:TOOL_VALUE_MAX + _DISPLAY_SCAN])[:TOOL_VALUE_MAX] for k in TOOL_KEYS if isinstance(t.get(k), str)}


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------
class PackageWriter:
    """``with PackageWriter(dest, kind) as w: w.add_file(...); w.finish(manifest)``。finish しなければ一時ファイルを消す。"""

    def __init__(self, dest: str, kind: str, overwrite: bool = False):
        if kind not in KINDS:
            raise ValueError(kind)
        self.kind = kind
        self.dest = os.path.abspath(dest)
        if os.path.isdir(self.dest):
            raise BackendError("INVALID_INPUT", tr(f"出力先がフォルダーです: {self.dest}",
                                                   f"The destination is a folder: {self.dest}"),
                               params={"reason": "package_dest"})
        if os.path.exists(self.dest) and not overwrite:
            raise BackendError("INVALID_INPUT", tr(f"出力先に同じ名前のファイルがあります: {self.dest}",
                                                   f"A file with the same name already exists at the destination: {self.dest}"),
                               params={"reason": "package_exists"})
        os.makedirs(os.path.dirname(self.dest), exist_ok=True)
        self.tmp = f"{self.dest}.{secrets.token_hex(4)}.tmp"
        self.zf = zipfile.ZipFile(self.tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6)
        self.files: dict[str, dict] = {}
        self.done = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if not self.done:
            self.abort()
        return False

    def _check(self, arcname: str) -> None:
        if not valid_name(arcname) or arcname in self.files or arcname == KINDS[self.kind]["manifest"]:
            raise ValueError(tr(f"共有ファイルの項目名が不正か重複: {arcname}",
                                f"Invalid or duplicate entry name in the share file: {arcname}"))

    def add_file(self, arcname: str, src: str) -> dict:
        self._check(arcname)
        h = hashlib.sha256()
        n = 0
        ctype = zipfile.ZIP_STORED if arcname.lower().endswith(STORED_EXT) else zipfile.ZIP_DEFLATED
        with open(src, "rb") as f, self.zf.open(self._info(arcname, ctype), "w", force_zip64=True) as out:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
                n += len(block)
                out.write(block)
        rec = {"sha256": h.hexdigest(), "size": n}
        self.files[arcname] = rec
        return rec

    def add_bytes(self, arcname: str, data: bytes) -> dict:
        self._check(arcname)
        ctype = zipfile.ZIP_STORED if arcname.lower().endswith(STORED_EXT) else zipfile.ZIP_DEFLATED
        self.zf.writestr(self._info(arcname, ctype), data)
        rec = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        self.files[arcname] = rec
        return rec

    @staticmethod
    def _info(arcname: str, ctype: int) -> zipfile.ZipInfo:
        zi = zipfile.ZipInfo(arcname, _fixed_time())
        zi.compress_type = ctype
        zi.external_attr = 0o644 << 16
        return zi

    def finish(self, manifest: dict) -> dict:
        """記録ファイルを書いて閉じ、出力先に置き換える。戻り値は manifest（files・package_id 入り）。"""
        manifest = {"format": KINDS[self.kind]["format"], "version": FORMAT_VERSION, "kind": self.kind, **manifest}
        manifest["files"] = dict(sorted(self.files.items()))
        manifest["package_id"] = package_id_of(self.files)
        data = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
        self.zf.writestr(self._info(KINDS[self.kind]["manifest"], zipfile.ZIP_DEFLATED), data)
        self.zf.close()
        os.replace(self.tmp, self.dest)
        self.done = True
        return manifest

    def abort(self) -> None:
        try:
            self.zf.close()
        except Exception:   # noqa: BLE001
            pass
        try:
            os.remove(self.tmp)
        except OSError:
            pass


def _fixed_time() -> tuple:
    """zip の項目の日時（中身には関係しないので固定。同じ中身なら同じ zip になるように）。"""
    return (2026, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------
class Package:
    """検査済みの共有ファイル。``manifest`` は記録ファイルの中身、``files`` は {項目名: {sha256, size}}。"""

    def __init__(self, path: str, kind: str, zf: zipfile.ZipFile, manifest: dict):
        self.path = path
        self.kind = kind
        self.zf = zf
        self.manifest = manifest
        self.files: dict[str, dict] = manifest["files"]
        # package_id は受け取った MOD のフォルダー名にも使うので、形も確かめる（open_package の照合と二重の守り。M3M-01）
        if not isinstance(manifest.get("package_id"), str) or not PACKAGE_ID_RE.fullmatch(manifest["package_id"]):
            raise _err("tampered", tr("記録ファイルの package_id の形式が不正です",
                                      "The package_id in the record file has an invalid format"))
        self.package_id: str = manifest["package_id"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        self.zf.close()

    def read(self, name: str, limit: int = MAX_MANIFEST) -> bytes:
        rec = self.files.get(name)
        if rec is None:
            raise _err("broken", tr(f"共有ファイルに {name} がありません", f"{name} is not in the share file"))
        if rec["size"] > limit:
            raise _err("too_large", tr(f"{name} が大きすぎます（{rec['size']} B）", f"{name} is too large ({rec['size']} B)"))
        try:
            data = self.zf.read(name)
        except ZIP_READ_ERRORS as e:
            raise _broken(name, e) from None
        if len(data) != rec["size"] or hashlib.sha256(data).hexdigest() != rec["sha256"]:
            raise _err("tampered", tr(f"{name} が記録（SHA-256）と一致しません", f"{name} does not match the record (SHA-256)"))
        return data

    def read_json(self, name: str, limit: int = MAX_MANIFEST) -> dict:
        data = self.read(name, limit)
        try:
            d = json.loads(data.decode("utf-8"))
        except JSON_ERRORS:
            raise _err("broken", tr(f"{name} を JSON として読めません", f"Cannot read {name} as JSON"))
        if not isinstance(d, dict):
            raise _err("broken", tr(f"{name} の形式が不正です", f"{name} has an invalid format"))
        return d

    def extract(self, name: str, dest: str) -> None:
        """name を dest に書き出す（書きながら SHA-256・大きさを照合し、違えば消してエラー）。"""
        rec = self.files[name]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        h = hashlib.sha256()
        n = 0
        try:
            src = self.zf.open(name)
        except ZIP_READ_ERRORS as e:
            raise _broken(name, e) from None
        try:
            # 共有ファイルを読む側の例外だけを broken にする（書き出し先の OSError（容量不足など）はそのまま）
            with src, open(dest, "wb") as out:
                while True:
                    try:
                        block = src.read(1 << 20)
                    except ZIP_READ_ERRORS as e:
                        raise _broken(name, e) from None
                    if not block:
                        break
                    n += len(block)
                    if n > rec["size"]:
                        raise _err("tampered", tr(f"{name} が記録より大きいです", f"{name} is larger than recorded"))
                    h.update(block)
                    out.write(block)
            if n != rec["size"] or h.hexdigest() != rec["sha256"]:
                raise _err("tampered", tr(f"{name} が記録（SHA-256）と一致しません", f"{name} does not match the record (SHA-256)"))
        except BaseException:
            _remove(dest)
            raise


def _remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def kind_of(path: str) -> str | None:
    """拡張子から種類を推定する（中身の確認は open_package）。"""
    low = path.lower()
    for k, v in KINDS.items():
        if low.endswith(v["ext"]):
            return k
    return None


def open_package(path: str, expect: str | None = None, verify: bool = True) -> Package:
    """共有ファイルを開いて検査する（モジュールの説明の項目）。expect（"equipment" / "mod"）と違う種類ならエラー。

    verify=True なら全ての項目の SHA-256 をここで照合する（数十 MB で数秒）。"""
    if not path or not isinstance(path, str):
        raise BackendError("INVALID_INPUT", tr("共有ファイルを指定してください", "Specify a share file"),
                           params={"reason": "package_path"})
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise _err("not_found", tr(f"ファイルがありません: {path}", f"File not found: {path}"))
    try:
        zf = zipfile.ZipFile(path)
    except ZIP_READ_ERRORS + (ValueError,) as e:
        raise _err("broken", tr(f"zip として読めません（壊れているか、共有ファイルではありません）: {e}",
                                f"Cannot read it as a zip file (it is corrupted or is not a share file): {e}"))
    try:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise _err("unsafe", tr(f"項目が多すぎます（{len(infos)} > {MAX_ENTRIES}）",
                                    f"Too many entries ({len(infos)} > {MAX_ENTRIES})"))
        names: dict[str, zipfile.ZipInfo] = {}
        seen_lower: set[str] = set()
        total = 0
        for zi in infos:
            n = zi.filename
            if zi.is_dir():
                raise _err("unsafe", tr(f"想定外のフォルダーの項目: {n!r}", f"Unexpected folder entry: {n!r}"))
            if not valid_name(n):
                raise _err("unsafe", tr(f"項目の名前が不正です（危険な名前を含む）: {n!r}",
                                        f"Invalid entry name (contains a dangerous name): {n!r}"))
            if n.lower() in seen_lower:
                raise _err("unsafe", tr(f"同じ名前の項目が重複しています: {n!r}", f"Duplicate entry name: {n!r}"))
            seen_lower.add(n.lower())
            if zi.flag_bits & 0x1:
                raise _err("unsafe", tr(f"暗号化された項目は読めません: {n!r}", f"Encrypted entries cannot be read: {n!r}"))
            if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise _err("unsafe", tr(f"未対応の圧縮形式: {n!r}", f"Unsupported compression method: {n!r}"))
            if zi.file_size > MAX_FILE:
                raise _err("too_large", tr(f"項目が大きすぎます: {n!r}（{zi.file_size} B）",
                                           f"Entry too large: {n!r} ({zi.file_size} B)"))
            if zi.compress_size and zi.file_size / max(zi.compress_size, 1) > MAX_RATIO and zi.file_size > 1024 * 1024:
                raise _err("unsafe", tr(f"圧縮率が極端な項目: {n!r}", f"Entry with an extreme compression ratio: {n!r}"))
            total += zi.file_size
            names[n] = zi
        if total > MAX_TOTAL:
            raise _err("too_large", tr(f"展開後の大きさが上限を超えます（{total} B）",
                                       f"The extracted size exceeds the limit ({total} B)"))
        zsize = os.path.getsize(path)
        if total > AMPLIFY_MIN and total > MAX_TOTAL_RATIO * max(zsize, 1):
            raise _err("unsafe", tr(f"展開後の大きさ（{total} B）が共有ファイル（{zsize} B）に比べて極端に大きいです",
                                    f"The extracted size ({total} B) is extremely large compared with the share file ({zsize} B)"))
        found = [k for k, v in KINDS.items() if v["manifest"] in names]
        if len(found) != 1:
            raise _err("broken", tr("GD Skin Studio の共有ファイルではありません（記録ファイルがありません）",
                                    "This is not a GD Skin Studio share file (it has no record file)"))
        kind = found[0]
        if expect and kind != expect:
            raise _err("kind_" + kind, tr(f"種類が違う共有ファイルです（{kind}。必要なのは {expect}）",
                                          f"This share file is of a different type ({kind}; {expect} is required)"), kind=kind)
        mname = KINDS[kind]["manifest"]
        if names[mname].file_size > MAX_MANIFEST:
            raise _err("too_large", tr("記録ファイルが大きすぎます", "The record file is too large"))
        try:
            manifest = json.loads(zf.read(mname).decode("utf-8"))
        except ZIP_READ_ERRORS + JSON_ERRORS as e:
            raise _err("broken", tr(f"記録ファイルを読めません: {type(e).__name__}: {e}",
                                    f"Cannot read the record file: {type(e).__name__}: {e}"))
        if not isinstance(manifest, dict) or manifest.get("format") != KINDS[kind]["format"] or manifest.get("kind") != kind:
            raise _err("broken", tr("記録ファイルの形式が違います", "The record file has a different format"))
        try:
            ver = int(manifest.get("version", 0))
        except (TypeError, ValueError):
            ver = 0
        if ver < 1:
            raise _err("broken", tr("記録ファイルの版が不正です", "The record file has an invalid version"))
        if ver > FORMAT_VERSION:
            raise _err("version", tr(f"新しい版のアプリで作られた共有ファイルです（共有ファイルの形式の版 {ver} > {FORMAT_VERSION}）。アプリを更新してください",
                                     f"This share file was made with a newer version of the app (file format version {ver} > "
                                     f"{FORMAT_VERSION}). Update the app"),
                       version=ver)
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise _err("broken", tr("記録ファイルにファイルの一覧がありません", "The record file has no file list"))
        listed = set(files)
        present = set(names) - {mname}
        if listed != present:
            extra, missing = sorted(present - listed), sorted(listed - present)
            raise _err("tampered", tr("中身と記録が合いません", "The contents do not match the record")
                       + (tr(f"（記録に無いファイル: {extra[:5]}）", f" (files not in the record: {extra[:5]})") if extra else "")
                       + (tr(f"（無いファイル: {missing[:5]}）", f" (missing files: {missing[:5]})") if missing else ""))
        for n, rec in files.items():
            if not (isinstance(rec, dict) and isinstance(rec.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", rec["sha256"])
                    and isinstance(rec.get("size"), int) and rec["size"] >= 0):
                raise _err("broken", tr(f"記録ファイルの {n} の項目が不正です", f"The entry for {n} in the record file is invalid"))
            if names[n].file_size != rec["size"]:
                raise _err("tampered", tr(f"{n} の大きさが記録と違います（{names[n].file_size} != {rec['size']}）",
                                          f"The size of {n} differs from the record ({names[n].file_size} != {rec['size']})"))
        if manifest.get("package_id") != package_id_of(files):
            raise _err("tampered", tr("記録ファイルの package_id が中身の一覧と合いません",
                                      "The package_id in the record file does not match the list of contents"))
        pkg = Package(path, kind, zf, manifest)
        if verify:
            for n, rec in sorted(files.items()):
                h = hashlib.sha256()
                got = 0
                try:
                    with zf.open(n) as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            got += len(block)
                            if got > rec["size"]:
                                break
                            h.update(block)
                except ZIP_READ_ERRORS as e:      # CRC-32 の不一致・deflate の破損・途中で切れたデータなど（C96）
                    raise _broken(n, e) from None
                # 展開して読んだバイト数も記録と照合する（中央ディレクトリの大きさと記録を同じ嘘の値にした項目は、
                # 無圧縮だと実データの終わりで止まってエラーにならない。2026-10-17 監査 C97）
                if got != rec["size"]:
                    raise _err("tampered", tr(f"{n} の展開後の大きさが記録と違います（{got}{'+' if got > rec['size'] else ''}"
                                              f" != {rec['size']} B。壊れているか、書き換えられています）",
                                              f"The extracted size of {n} differs from the record "
                                              f"({got}{'+' if got > rec['size'] else ''} != {rec['size']} B; it is corrupted "
                                              "or has been modified)"), file=n)
                if h.hexdigest() != rec["sha256"]:
                    raise _err("tampered", tr(f"{n} が記録（SHA-256）と一致しません（壊れているか、書き換えられています）",
                                              f"{n} does not match the record (SHA-256); it is corrupted or has been modified"),
                               file=n)
        return pkg
    except BaseException:
        zf.close()
        raise


SCRATCH_PREFIXES = ("received", "import")        # scratch_dir の prefix（mod_package・equipment_package）
SCRATCH_MAX_AGE = 24 * 3600                      # 起動時に消す、残った一時フォルダーの古さ（秒）


def scratch_dir(home: str, prefix: str) -> str:
    """データフォルダーの中の作業用の一時フォルダー（展開先。終わったら消す。強制終了で残ったものは、次の起動で
    :func:`cleanup_scratch` が消す）。"""
    d = os.path.join(home, "tmp", f"{prefix}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}")
    os.makedirs(d)
    return d


def remove_tree(d: str) -> None:
    shutil.rmtree(d, ignore_errors=True)


def cleanup_scratch(home: str, max_age: float = SCRATCH_MAX_AGE, now: float | None = None) -> list[str]:
    """``<データ>\\tmp\\`` に残った展開の一時フォルダー（received-*・import-*）のうち、max_age 秒より古いものを消す
    （2026-10-17 監査 C98。展開の途中でアプリが強制終了すると残る）。消したフォルダー名の一覧を返す。

    バックエンドの起動時（ジョブを受け付ける前）に呼ぶ。同じデータフォルダーのバックエンドは 1 つだけという前提だが、
    念のため新しいもの（動いているジョブが使っているかもしれないもの）は残す。"""
    root = os.path.join(home, "tmp")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    now = time.time() if now is None else now
    removed = []
    for name in names:
        if not any(name.startswith(p + "-") for p in SCRATCH_PREFIXES):
            continue
        d = os.path.join(root, name)
        try:
            if not os.path.isdir(d) or os.path.islink(d) or now - os.path.getmtime(d) < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not os.path.exists(d):
            removed.append(name)
    return removed
