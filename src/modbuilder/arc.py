"""Grim Dawn の .arc（リソースアーカイブ）: 自前の読み取りと、ArchiveTool.exe による作成・検査。

読み取り（:class:`ArcFile`）はファイルを読み取り専用で開くだけなので、ゲームのフォルダーの .arc にも直接使える
（ArchiveTool は .arc を読むだけでも更新日時を変えるため、ゲームの .arc には使わない。2026-10-16 確認）。
形式（ARC v3。2026-10-16 にこのPCで確認）:

  ヘッダー 28 バイト: 'ARC\\0', u32 版(3), u32 ファイル数, u32 パート数, u32 パート表の大きさ,
                      u32 名前表の大きさ, u32 パート表の位置
  パート表: (u32 位置, u32 圧縮後, u32 展開後) × パート数。圧縮後 = 展開後なら無圧縮、違えば LZ4 ブロック
  名前表: NUL 終端の名前の並び（パート表の直後）
  ファイル表（名前表の直後、44 バイト × ファイル数）:
      u32 種別(3=圧縮, 1=無圧縮, 0=空き), u32 位置, u32 圧縮後, u32 展開後, u32 Adler32(展開後),
      u64 FILETIME, u32 パート数, u32 最初のパート, u32 名前の長さ, u32 名前表での位置

ArchiveTool の書き方（2026-10-17 にこのPCで確認。点検 C77・C81）: ヘッダーの後は 2048 バイト目までゼロで、データは
2048 から。名前は小文字、FILETIME はファイルの更新日時。圧縮レベル 1〜9 は種別 3 で、256 KiB ごとのパートを LZ4 で
圧縮する（縮まないパートは無圧縮のまま。圧縮後 = 展開後）。空のファイルはパート 0。圧縮レベル 0 は種別 1 で、パートを
使わず（パート数 0）、データをファイル表の「位置」に直接置く（圧縮後 = 展開後）。:meth:`ArcFile.read` は両方を読む
（種別 1 かつ圧縮後 = 展開後なら位置から直接。arzedit・gd-edit と同じ条件）。

検証: ゲームの resources/*.arc（本編・gdx1〜3 の 65 個）のファイル一覧が ArchiveTool ``-list`` の出力と一致
（ArchiveTool が開けない menu.arc を除く 64 個）、database/templates.arc の 818 ファイルが ArchiveTool ``-extract``
の結果とバイト一致、全ファイルの Adler32 が一致。
"""
from __future__ import annotations

import locale
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass

import lz4.block

from . import paths
from ._i18n import tr
from .errors import ModBuilderError

__all__ = ["ArcEntry", "ArcFile", "ArchiveTool", "WorkDirUnavailable", "path_problem", "safe_work_dir",
           "work_dir_candidates"]

_HEADER = "<7I"
_ENTRY = "<5IQ4I"
_ENTRY_SIZE = struct.calcsize(_ENTRY)   # 44
ARC_MAGIC = 0x00435241                  # 'ARC\0'


@dataclass
class ArcEntry:
    name: str
    kind: int
    size: int
    csize: int
    adler32: int
    filetime: int
    first_part: int
    part_count: int
    offset: int = 0          # ファイル表の「位置」（種別 1 はデータの位置。種別 3 は最初のパートの位置）


class ArcFile:
    """.arc を読み取り専用で開く。``entries`` はファイル名（アーカイブ内の相対パス、大文字小文字はそのまま）→ ArcEntry。

    形式の不正（表・名前・パートの範囲の外、LZ4 の展開の失敗、大きさ・Adler32 の不一致）は ValueError にそろえる
    （2026-10-17 点検 C79。以前は struct.error・IndexError・LZ4BlockError などにばらついた）。"""

    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(28)
            if len(head) < 28:
                raise ValueError(tr(f".arc として短すぎる: {path}", f"Too short to be an .arc file: {path}"))
            magic, ver, nfiles, nparts, pt_size, names_size, pt_off = struct.unpack(_HEADER, head)
            if magic != ARC_MAGIC or ver != 3:
                raise ValueError(tr(f".arc（v3）ではない: {path}", f"Not an .arc (v3) file: {path}"))
            self.file_size = os.fstat(f.fileno()).st_size
            if pt_size < 12 * nparts or pt_off + pt_size + names_size + _ENTRY_SIZE * nfiles > self.file_size:
                raise ValueError(tr(f".arc の表がファイルの外を指している（壊れている）: {path}",
                                    f"The .arc tables point outside the file (corrupted): {path}"))
            f.seek(pt_off)
            pt = f.read(pt_size)
            names = f.read(names_size)
            table = f.read(_ENTRY_SIZE * nfiles)
        if len(table) != _ENTRY_SIZE * nfiles:
            raise ValueError(tr(f".arc のファイル表が欠けている: {path}", f"The .arc file table is incomplete: {path}"))
        self.parts = [struct.unpack_from("<3I", pt, 12 * i) for i in range(nparts)]
        for i, (off, c, _u) in enumerate(self.parts):
            if off + c > self.file_size:
                raise ValueError(tr(f".arc のパート {i} がファイルの外を指している（壊れている）: {path}",
                                    f"Part {i} of the .arc file points outside the file (corrupted): {path}"))
        self.entries: dict[str, ArcEntry] = {}
        # ファイル表のそのままの名前の並び（同じ名前が重複していても全部）と、空きエントリの数。entries は名前の辞書なので
        # 重複は後のものだけが残る。受け取った MOD の検査（gdskin_backend.mod_package）が重複・空きを見るのに使う
        # （2026-10-17 監査 C94。公式の .arc も同じクラスで読むので、ここでは例外にしない）
        self.raw_names: list[str] = []
        self.empty_count = 0
        for i in range(nfiles):
            kind, off, csize, size, adler, ft, pcount, pfirst, nlen, noff = struct.unpack_from(_ENTRY, table, _ENTRY_SIZE * i)
            if kind == 0 or nlen == 0:      # 空きエントリ（公式の database/templates.arc にもある）
                self.empty_count += 1
                continue
            if noff + nlen > names_size or pfirst + pcount > nparts:
                raise ValueError(tr(f".arc のファイル表の項目 {i} が名前表・パート表の外を指している（壊れている）: {path}",
                                    f"Entry {i} of the .arc file table points outside the name table or the part table "
                                    f"(corrupted): {path}"))
            name = names[noff:noff + nlen].decode("utf-8", "replace")
            self.raw_names.append(name)
            self.entries[name] = ArcEntry(name, kind, size, csize, adler, ft, pfirst, pcount, off)

    def names(self) -> list[str]:
        return list(self.entries)

    def read(self, name: str, max_size: int | None = None) -> bytes:
        """``name`` の中身。``max_size`` を渡すと、記録の大きさがそれを超えるエントリーは読む前に ValueError
        （受け取ったファイルなど、大きさを信用できない .arc で展開後の巨大なデータをメモリに作らない。点検 R05B-01）。"""
        e = self.entries.get(name)
        if e is None:
            low = {k.lower(): v for k, v in self.entries.items()}
            e = low.get(name.replace("\\", "/").lower())
        if e is None:
            raise KeyError(tr(f"{os.path.basename(self.path)} に {name} がありません",
                              f"{name} is not in {os.path.basename(self.path)}"))
        if max_size is not None and e.size > max_size:
            raise ValueError(tr(f"{name}: 大きさ {e.size} が上限 {max_size} を超えている",
                                f"{name}: the size {e.size} exceeds the limit {max_size}"))
        buf = bytearray()
        with open(self.path, "rb") as f:
            if e.kind == 1 and e.csize == e.size:
                # 種別 1（ArchiveTool の圧縮レベル 0）: パートを使わず、データはファイル表の位置に直接ある（C77）
                if e.offset + e.size > self.file_size:
                    raise ValueError(tr(f"{name}: データがファイルの外を指している（壊れている）",
                                        f"{name}: the data points outside the file (corrupted)"))
                f.seek(e.offset)
                buf += f.read(e.size)
            else:
                # パートの展開後の大きさの合計が記録の大きさと違えば、展開する前に止める（壊れた・細工した表で
                # 記録より大きなデータを展開しない。正しいファイルでは常に一致する）
                total = sum(u for _o, _c, u in self.parts[e.first_part:e.first_part + e.part_count])
                if total != e.size:
                    raise ValueError(tr(f"{name}: パートの展開後の大きさの合計が一致しない（{total} != {e.size}。壊れている）",
                                        f"{name}: the total decompressed size of the parts does not match "
                                        f"({total} != {e.size}) (corrupted)"))
                for k in range(e.first_part, e.first_part + e.part_count):
                    off, c, u = self.parts[k]
                    f.seek(off)
                    blk = f.read(c)
                    if c == u:
                        buf += blk
                        continue
                    try:
                        buf += lz4.block.decompress(blk, uncompressed_size=u)
                    except Exception as ex:   # noqa: BLE001  lz4.block.LZ4BlockError は ValueError ではない
                        raise ValueError(tr(f"{name}: パート {k} を展開できない（壊れている）: {ex}",
                                            f"{name}: cannot decompress part {k} (corrupted): {ex}")) from None
        data = bytes(buf)
        if len(data) != e.size:
            raise ValueError(tr(f"{name}: 展開後の大きさが一致しない（{len(data)} != {e.size}）",
                                f"{name}: the decompressed size does not match ({len(data)} != {e.size})"))
        if (zlib.adler32(data) & 0xffffffff) != e.adler32:
            raise ValueError(tr(f"{name}: Adler32 が一致しない", f"{name}: Adler32 does not match"))
        return data

    def read_all(self) -> dict[str, bytes]:
        return {n: self.read(n) for n in self.entries}


# ゲーム付属のツール（ArchiveTool・TextureCompiler）はパスを ANSI（A 版の API・narrow の argv）で扱い、出力も ANSI で書く
_ANSI = "mbcs" if os.name == "nt" else locale.getencoding()


def path_problem(path: str) -> str | None:
    """ArchiveTool にそのまま渡すと問題になるパスなら理由、無ければ None（ASCII だけのパスは常に None）。

    * ``not_ansi``: ANSI コードページで厳密に符号化できない（日本語版 Windows の é・한・絵文字など）。ツールが開けない
    * ``dbcs_trail``: 2 バイト文字の 2 バイト目が A〜Z（0x41〜0x5A）か ``\\``（0x5C）。``-extract``・``-database`` は
      出力先のパスをバイト単位で A〜Z だけ小文字化するので別のフォルダーに書き（山 → 屍、ス → ベ）、0x5C の字
      （ソ・表・能）では止まる。``C:\\Users`` の U のような ASCII の大文字は問題にしない
    """
    try:
        path.encode(_ANSI, "strict")
    except UnicodeEncodeError:
        return "not_ansi"
    for ch in path:
        if ord(ch) < 0x80:
            continue
        b = ch.encode(_ANSI, "strict")
        if len(b) >= 2 and (0x41 <= b[-1] <= 0x5A or b[-1] == 0x5C):
            return "dbcs_trail"
    return None


def _path_note(*paths: str) -> str:
    """失敗の文に添える、パスの文字が原因の可能性の説明（問題が無ければ空）。"""
    for p in paths:
        if p and path_problem(os.path.abspath(p)) == "not_ansi":
            return tr("（パスに Windows の文字コード（ANSI）で表せない文字があります。ASCII だけのフォルダーで試してください）",
                      " (the path contains characters that cannot be represented in the Windows code page (ANSI); "
                      "try a folder whose path is ASCII only)")
    return ""


# Windows のパスの長さの上限（MAX_PATH 260 から終端の NUL を除く）。ゲーム付属のツールは長いパスを扱えず、上限を超える
# 出力は終了コード 0 のまま書かれない（点検 T6_backend-F01: 深いフォルダーへの -database で 15 件中 4 件が欠けた）
MAX_PATH_CHARS = 259
# 作業フォルダーの下に ArchiveTool の -database が書く、最も深いファイルの相対パスの長さの見込み（2026-10-17 機能 SF22）:
# modbuilder_arz_<8 文字>\records\creatures\npcs\merchants\factiontables\devilscrossing_respected_01.dbr が 102 文字
WORK_DEPTH = 110


def path_too_long(path: str, depth: int) -> bool:
    """path の下に、path からの相対で depth 文字のファイルを作ると Windows のパスの上限を超えるか。"""
    return len(os.path.abspath(path)) + 1 + depth > MAX_PATH_CHARS


class WorkDirUnavailable(ModBuilderError):
    """ArchiveTool の ``-database``・``-extract`` の出力先に使える作業フォルダーが無い（候補のパスがどれも
    :func:`path_problem` に当たるか、書き込めない）。``candidates`` に [(候補, 理由)]。"""

    def __init__(self, message: str, candidates: list[tuple[str, str]]):
        super().__init__(message)
        self.candidates = list(candidates)


def work_dir_candidates(home: str | None = None) -> list[str]:
    """ArchiveTool の出力先の候補（この順に試す。2026-10-17 利用者の判断 C02）: 一時フォルダー（%TEMP%）→
    データフォルダーの work\\ → %SystemDrive%\\GDSkinStudio\\tmp。"""
    drive = os.environ.get("SystemDrive") or "C:"
    return [os.path.abspath(tempfile.gettempdir()), os.path.join(home or paths.home(), "work"),
            os.path.join(drive + os.sep, "GDSkinStudio", "tmp")]


def safe_work_dir(home: str | None = None, create: bool = True) -> tuple[str | None, list[tuple[str, str]]]:
    """ArchiveTool の ``-database``・``-extract`` の出力先に使う作業フォルダー → (フォルダー, 使わなかった候補と理由)。

    ``-database`` は出力先の絶対パスを ANSI のバイト単位で A〜Z だけ小文字にするので（:func:`path_problem`）、
    %TEMP% に 山・ス などを含む PC では別のフォルダーに書いて全レコードが欠落した（点検 C02）。候補
    （:func:`work_dir_candidates`）のうち、パスが :func:`path_problem` に当たらず、作成と書き込みができる最初のものを
    返す。無ければ (None, 理由)。create=False は作成・書き込みを試さず、パスの判定だけを行う（環境チェック用）。
    パスが長く、出力が Windows のパスの上限を超える候補（:func:`path_too_long`）も理由 ``too_long`` で飛ばす
    （2026-10-17 機能 SF22。普通の %TEMP% は 40 文字ほどなので、選ばれるフォルダーは変わらない）。"""
    skipped = []
    for c in work_dir_candidates(home):
        prob = path_problem(c)
        if prob is not None:
            skipped.append((c, prob))
            continue
        if path_too_long(c, WORK_DEPTH):
            skipped.append((c, "too_long"))
            continue
        if not create:
            return c, skipped
        try:
            os.makedirs(c, exist_ok=True)
            probe = tempfile.mkdtemp(prefix="gdskin_probe_", dir=c)
            os.rmdir(probe)
        except OSError as e:
            skipped.append((c, f"not_writable: {e.strerror or e}"))
            continue
        return c, skipped
    return None, skipped


class ArchiveTool:
    """``<GD>/ArchiveTool.exe`` の呼び出し。ModBuilder が作った .arc / .arz にだけ使う（ゲームの .arc には使わない）。

    入力のパスは作業フォルダー（cwd）と相対の名前で渡す（ツールは A 版の API で開くので、ANSI に無い字を含む絶対パスは
    開けず、-database は異常終了する。2026-10-17 点検 C16）。出力は ANSI コードページで読む（作業プロセスは
    ``-X utf8`` で動くので、指定しないと日本語を含むエラーの文が失われる。C71）。

    ``-database``・``-extract`` の出力先（絶対パスでしか渡せない）は work_dir の下に作る。省くと最初に使うときに
    :func:`safe_work_dir` で選び、使えるものが無ければ :class:`WorkDirUnavailable`（点検 C02）。"""

    def __init__(self, gd: str, timeout: float = 300.0, work_dir: str | None = None, home: str | None = None):
        self.exe = os.path.join(gd, "ArchiveTool.exe")
        if not os.path.isfile(self.exe):
            raise FileNotFoundError(tr(f"ArchiveTool.exe が見つかりません: {self.exe}",
                                       f"ArchiveTool.exe was not found: {self.exe}"))
        self.timeout = timeout
        self.work_dir = work_dir
        self._home = home

    def _work_dir(self) -> str:
        if self.work_dir is None:
            d, skipped = safe_work_dir(self._home)
            if d is None:
                listed = tr("、", ", ").join(tr(f"{c}（{why}）", f"{c} ({why})") for c, why in skipped)
                raise WorkDirUnavailable(tr("ArchiveTool の出力先に使える作業フォルダーがありません（候補のパスに ArchiveTool が"
                                            f"扱えない文字があるか、パスが長すぎるか、書き込めません）: {listed}",
                                            "There is no work folder that ArchiveTool can use for its output (the "
                                            "candidate paths contain characters ArchiveTool cannot handle, are too long, or are not "
                                            f"writable): {listed}"), skipped)
            self.work_dir = d
        return self.work_dir

    def _run(self, args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.exe] + args, cwd=cwd, capture_output=True, text=True, encoding=_ANSI,
                                  errors="replace", timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            # 止まったツールは subprocess.run が終わらせる。ModBuilderError なので task_build では MOD_BUILD_FAILED（C70）
            what = args[1] if len(args) > 1 else ""
            target = f"{os.path.join(cwd or '', args[0])}{_path_note(cwd or '', args[0])}"
            raise ModBuilderError(tr(f"ゲーム付属のツール（ArchiveTool {what}）が {self.timeout:.0f} 秒応答しません: {target}",
                                     f"The game's bundled tool (ArchiveTool {what}) did not respond for "
                                     f"{self.timeout:.0f} seconds: {target}")) from None

    def build(self, src_dir: str, out_arc: str) -> None:
        """src_dir の中身を out_arc にする（圧縮レベル 9）。ソースフォルダーをカレントにして相対パスで渡す
        （絶対パスだとフルパスがエントリ名になり、展開できなくなる。試作MOD 1 で確認）。出力の .arc も src_dir からの
        相対パスで渡す（別のドライブで相対にできないときだけ絶対パス）。"""
        out_arc = os.path.abspath(out_arc)
        if os.path.exists(out_arc):
            os.remove(out_arc)
        os.makedirs(os.path.dirname(out_arc), exist_ok=True)
        try:
            out_arg = os.path.relpath(out_arc, os.path.abspath(src_dir))
        except ValueError:
            out_arg = out_arc
        r = self._run([out_arg, "-add", ".", ".", "9"], cwd=src_dir)
        if r.returncode != 0 or not os.path.isfile(out_arc):
            note = _path_note(src_dir, out_arc)
            raise RuntimeError(tr(f"ArchiveTool -add に失敗（終了コード {r.returncode}）: {out_arc}{note}"
                                  f"\n{r.stdout}{r.stderr}",
                                  f"ArchiveTool -add failed (exit {r.returncode}): {out_arc}{note}"
                                  f"\n{r.stdout}{r.stderr}"))

    def list(self, arc: str) -> list[str]:
        arc = os.path.abspath(arc)
        r = self._run([os.path.basename(arc), "-list"], cwd=os.path.dirname(arc))
        if r.returncode != 0:
            note = _path_note(arc)
            raise RuntimeError(tr(f"ArchiveTool -list に失敗（終了コード {r.returncode}）: {arc}{note}\n{r.stdout}{r.stderr}",
                                  f"ArchiveTool -list failed (exit {r.returncode}): {arc}{note}\n{r.stdout}{r.stderr}"))
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def extract_database(self, arz: str) -> dict[str, str]:
        """``-database`` で展開し、{レコード名: .dbr テキスト} を返す（一時フォルダーは消す）。

        .arz は cwd とファイル名で渡す。出力先は相対にできない（ASCII のフォルダーでも 0 件になる）ので、作業フォルダー
        （:meth:`_work_dir`）の下の絶対パス。終了コード 0 で 1 件も出なければ、出力先のパスが原因の可能性を示す
        RuntimeError（空の結果で照合に進むと「全レコードが入っていない」になり原因が分からない。点検 C02）。"""
        tmp = tempfile.mkdtemp(prefix="modbuilder_arz_", dir=self._work_dir())
        try:
            arz = os.path.abspath(arz)
            r = self._run([os.path.basename(arz), "-database", tmp], cwd=os.path.dirname(arz))
            if r.returncode != 0:
                note = _path_note(arz, tmp)
                raise RuntimeError(tr(f"ArchiveTool -database に失敗（終了コード {r.returncode}）: {arz}{note}"
                                      f"\n{r.stdout}{r.stderr}",
                                      f"ArchiveTool -database failed (exit {r.returncode}): {arz}{note}"
                                      f"\n{r.stdout}{r.stderr}"))
            out = {}
            for dirpath, _d, files in os.walk(tmp):
                for f in files:
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, tmp).replace("\\", "/").lower()
                    with open(full, "rb") as fh:
                        out[rel] = fh.read().decode("utf-8", "replace")
            if not out:
                why = path_problem(tmp) or tr("判定では問題なし", "none detected")
                raise RuntimeError(tr(f"ArchiveTool -database の出力先 {tmp} に何も出ませんでした（出力先のパスに ArchiveTool が"
                                      f"扱えない文字がある可能性: {why}）\n{r.stdout}{r.stderr}",
                                      f"ArchiveTool -database wrote nothing to the output folder {tmp} (the output path may "
                                      f"contain characters ArchiveTool cannot handle: {why})\n{r.stdout}{r.stderr}"))
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def extract(self, arc: str) -> dict[str, bytes]:
        """``-extract`` で展開し、{アーカイブ内の相対パス(小文字): 中身} を返す（出力先は作業フォルダーの下）。"""
        tmp = tempfile.mkdtemp(prefix="modbuilder_arc_", dir=self._work_dir())
        try:
            r = self._run([os.path.abspath(arc), "-extract", tmp])
            if r.returncode != 0:
                raise RuntimeError(tr(f"ArchiveTool -extract に失敗（終了コード {r.returncode}）: {arc}\n{r.stdout}{r.stderr}",
                                      f"ArchiveTool -extract failed (exit {r.returncode}): {arc}\n{r.stdout}{r.stderr}"))
            subs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            if len(subs) != 1:      # 全体展開は <出力先>/<アーカイブ名>/ の下に出る
                raise RuntimeError(tr(f"ArchiveTool -extract の結果の構成が想定外: {subs}",
                                      f"Unexpected layout of the ArchiveTool -extract output: {subs}")
                                   + (tr(f"（出力先 {tmp} のパスが原因の可能性）",
                                         f" (the path of the output folder {tmp} may be the cause)") if not subs else ""))
            root = os.path.join(tmp, subs[0])
            out = {}
            for dirpath, _d, files in os.walk(root):
                for f in files:
                    full = os.path.join(dirpath, f)
                    with open(full, "rb") as fh:
                        out[os.path.relpath(full, root).replace("\\", "/").lower()] = fh.read()
            return out
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
