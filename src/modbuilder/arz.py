"""Grim Dawn の .arz（データベース）・.dbr（テキストのレコード）・.tpl（テンプレート）。

試作MOD 1 の ``prototype/mod01/tools/arzlib.py``・``build_arz.py``・``arzdiff.py`` を、ファイルを経由せずに
使えるよう移植したもの。書き出しの規則（フィールド順・文字列表の順・型・Adler32 × 4 の末尾）は変えていない。
試作MOD 1 では公式 3 ファイルを展開 → 再コンパイルして意味的に一致し、ゲームが自前の .arz を読むことも
確認済み（prototype/mod01/BUILD.md）。

変更点:

* int 型の値を符号付き 32 ビットとして読む（公式 4 ファイル 93,863 レコードを ArchiveTool ``-database`` の
  展開結果とテキスト比較して全件一致を確認。2026-10-16。符号なしで読むと 3% のレコードで負の値が食い違う）。
* :func:`compile_records` はメモリ上のレコードとレコードの更新時刻（FILETIME）を受け取る。
* :func:`diff` は差分を文字列の一覧で返す（名前の対応表で読み替えて比較することもできる）。

形式（arzedit の arzfile.cs と公式ファイルの解析による）:

  ヘッダー 24 バイト: u16 2, u16 3, u32 レコード表の位置, 大きさ, 件数, u32 文字列表の位置, 大きさ
  レコードデータ（LZ4 ブロック圧縮）→ レコード表 → 文字列表 → 末尾 16 バイト（Adler32 × 4）
  レコード: [u16 型, u16 個数, u32 名前の番号, u32 値 × 個数] の並び。型 0=int 1=float 2=string 3=bool。
  先頭は templateName、残りはフィールド名の昇順。空の値は格納しない。file_* 型の値は小文字・'/' 区切り。
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass

import lz4.block

from ._i18n import tr

__all__ = [
    "T_INT", "T_FLOAT", "T_STRING", "T_BOOL", "ArzFile", "ArzRecord", "TemplateSet", "TemplateVar",
    "parse_dbr_text", "read_dbr", "dbr_text", "split_array", "norm_path", "compile_records", "record_text",
    "filetime_from_unix", "diff",
]

HEADER_FMT = "<HHIIIII"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 24
TRAILER_SIZE = 16
RECORD_ENTRY_MIN = 28    # レコード表の 1 件の最小の大きさ（u32 名前の番号, u32 種類の長さ, u32 位置, 大きさ × 2, u64 更新時刻）

T_INT, T_FLOAT, T_STRING, T_BOOL = 0, 1, 2, 3
TPL_TYPE_MAP = {"int": T_INT, "real": T_FLOAT, "bool": T_BOOL}   # それ以外は文字列


def norm_path(p: str) -> str:
    """ゲーム内パスの正規化（小文字・スラッシュ区切り）。"""
    return p.replace("\\", "/").lower()


def split_array(value: str) -> list[str]:
    """配列値を ';' で分割する。空要素は除く（arzedit と同じ扱い）。"""
    return [p for p in value.split(";") if p != ""]


def filetime_from_unix(ts: float) -> int:
    return int(ts * 10000000) + 116444736000000000


def _adler32(data: bytes, value: int = 1) -> int:
    return zlib.adler32(data, value) & 0xffffffff


def _trailer(header: bytes, rdata: bytes, rtable: bytes, stable: bytes) -> bytes:
    a = _adler32(header)
    a = _adler32(rdata, a)
    a = _adler32(rtable, a)
    a = _adler32(stable, a)
    return struct.pack("<IIII", a, _adler32(stable), _adler32(rdata), _adler32(rtable))


# ---------------------------------------------------------------------------
# .dbr テキスト
# ---------------------------------------------------------------------------
def parse_dbr_text(text: str) -> list[tuple[str, str]]:
    """'name,value,' の行を [(name, value)] に。空行は無視する。"""
    out = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if "," not in line:
            raise ValueError(tr(f"不正な行: {line!r}", f"Invalid line: {line!r}"))
        name, rest = line.split(",", 1)
        if rest.endswith(","):
            rest = rest[:-1]
        out.append((name.strip(), rest))
    return out


def read_dbr(path: str) -> list[tuple[str, str]]:
    with open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if text.startswith("﻿"):
        text = text[1:]
    return parse_dbr_text(text)


def dbr_text(fields: dict[str, str]) -> str:
    """templateName を先頭に、残りを名前順に並べた .dbr テキスト（改行 LF。試作MOD の Record.text() と同じ）。"""
    names = sorted(n for n in fields if n != "templateName")
    lines = ["templateName,%s," % fields["templateName"]]
    lines += ["%s,%s," % (n, fields[n]) for n in names]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# .arz 読み込み
# ---------------------------------------------------------------------------
@dataclass
class ArzRecord:
    name: str
    rtype: str
    offset: int
    csize: int
    dsize: int
    filetime: int
    fields: list | None = None   # [(fieldName, type, [values])]


class ArzFile:
    """.arz を読む（ファイルは読み取り専用で開くだけ）。

    形式の不正（表の範囲の外、文字列の番号の範囲外、LZ4 の展開の失敗、展開後の大きさの不一致、フィールドの並びの
    はみ出し）は ValueError にそろえる（2026-10-17 点検 C79。python-lz4 は短い出力をそのまま返すので大きさも確かめる）。

    max_bytes・max_records: ファイルの大きさ・レコード表の件数の上限。超えればファイルを読む前・レコードを作る前に ValueError
    （受け取った MOD の検査が、展開後の大きさ 0 B のレコードを大量に並べた細工したファイルで、レコード 1 件あたり約 230 B の
    オブジェクトを何百万件も作らないように使う。None は上限なしで、公式のファイルの読み取りは変えない。2026-10-27 検証 R05B-01）。"""

    def __init__(self, path: str, max_bytes: int | None = None, max_records: int | None = None):
        self.path = path
        if max_bytes is not None:
            size = os.path.getsize(path)
            if size > max_bytes:
                raise ValueError(tr(f".arz の形式が不正: ファイルが大きすぎる（{size:,} B > 上限 {max_bytes:,} B）: {path}",
                                    f"Invalid .arz format: the file is too large ({size:,} B > limit {max_bytes:,} B): "
                                    f"{path}"))
        with open(path, "rb") as f:
            self.data = f.read()
        d = self.data
        if len(d) < HEADER_SIZE + TRAILER_SIZE:
            raise ValueError(tr(f".arz として短すぎる: {path}", f"Too short to be an .arz file: {path}"))
        (self.unknown, self.version, self.rt_start, self.rt_size, self.rt_count,
         self.st_start, self.st_size) = struct.unpack_from(HEADER_FMT, d, 0)
        if self.version != 3 or self.st_start + self.st_size > len(d) or self.rt_start + self.rt_size > len(d) \
                or self.rt_start < HEADER_SIZE:
            raise ValueError(tr(f".arz のヘッダーが不正: {path}", f"Invalid .arz header: {path}"))
        # レコード表の 1 件は最低 28 バイト（名前の番号・種類の長さ・位置・大きさ × 2・更新時刻）なので、件数が表の大きさに
        # 収まらないものは壊れている（正しいファイルでは常に収まる）。レコードを作り始める前に止める（R05B-01）
        if self.rt_count * RECORD_ENTRY_MIN > self.rt_size:
            raise ValueError(tr(f".arz のヘッダーが不正: レコードの件数 {self.rt_count:,} がレコード表の大きさ "
                                f"{self.rt_size:,} B に収まらない: {path}",
                                f"Invalid .arz header: the record count {self.rt_count:,} does not fit in the record table of "
                                f"{self.rt_size:,} B: {path}"))
        if max_records is not None and self.rt_count > max_records:
            raise ValueError(tr(f".arz の形式が不正: レコードが多すぎる（{self.rt_count:,} 件 > 上限 {max_records:,} 件）: {path}",
                                f"Invalid .arz format: too many records ({self.rt_count:,} > limit {max_records:,}): "
                                f"{path}"))
        try:
            self.strings = self._read_strings()
            self.records = self._read_record_table()
        except (struct.error, IndexError, UnicodeError) as e:
            raise ValueError(tr(f".arz の表が壊れている: {path}: {e}", f"The .arz tables are corrupted: {path}: {e}")) from None
        self.trailer = d[self.st_start + self.st_size:]
        self._by_name = {r.name: r for r in self.records}

    def _read_strings(self) -> list[str]:
        d = self.data
        pos = self.st_start
        end = self.st_start + self.st_size
        n = struct.unpack_from("<I", d, pos)[0]
        pos += 4
        out = []
        for _ in range(n):
            ln = struct.unpack_from("<I", d, pos)[0]
            pos += 4
            if pos + ln > end:
                raise IndexError(tr("文字列表の範囲の外", "outside the string table"))
            out.append(d[pos:pos + ln].decode("utf-8", "replace"))
            pos += ln
        return out

    def _read_record_table(self) -> list[ArzRecord]:
        d = self.data
        pos = self.rt_start
        end = self.rt_start + self.rt_size
        recs = []
        for _ in range(self.rt_count):
            name_idx, tlen = struct.unpack_from("<II", d, pos)
            pos += 8
            rtype = d[pos:pos + tlen].decode("utf-8", "replace")
            pos += tlen
            off, csize, dsize, ft = struct.unpack_from("<IIIQ", d, pos)
            pos += 20
            if pos > end or HEADER_SIZE + off + csize > self.rt_start:
                raise IndexError(tr("レコード表の範囲の外", "outside the record table"))
            recs.append(ArzRecord(self.strings[name_idx], rtype, off, csize, dsize, ft))
        return recs

    def expected_trailer(self) -> bytes:
        d = self.data
        st_end = self.st_start + self.st_size
        return _trailer(d[:HEADER_SIZE], d[HEADER_SIZE:self.rt_start], d[self.rt_start:self.st_start],
                        d[self.st_start:st_end])

    def trailer_ok(self) -> bool:
        return self.trailer == self.expected_trailer()

    def names(self) -> list[str]:
        return [r.name for r in self.records]

    def get(self, name: str) -> ArzRecord | None:
        return self._by_name.get(norm_path(name))

    def decode(self, rec: ArzRecord, max_size: int | None = None, max_fields: int | None = None) -> list:
        """max_size: 展開後の大きさ（レコード表の dsize）の上限。超えれば展開する前に ValueError（受け取った MOD の検査が
        細工した小さなファイルで GB 単位の展開をしないように使う。None は上限なしで、公式のファイルの読み取りは変えない。
        2026-10-20 レビュー R05B-01）。

        max_fields: 項目の数の上限。超える項目に来たところで ValueError（展開後の大きさが上限内でも、8 バイトの空の項目の並びは
        Python のオブジェクトで約 17 倍に膨らむ。受け取った MOD の検査は全レコードの合計の残りを渡す。展開済みのレコードも
        同じ上限で確かめる。None は上限なし。2026-10-27 検証 R05B-02）。"""
        if rec.fields is not None:
            if max_fields is not None and len(rec.fields) > max_fields:
                raise ValueError(self._too_many_fields(rec, max_fields))
            return rec.fields
        if max_size is not None and rec.dsize > max_size:
            raise ValueError(tr(f".arz の形式が不正: {rec.name}: レコードが大きすぎる（展開後 {rec.dsize:,} B > 上限 {max_size:,} B）",
                                f"Invalid .arz format: {rec.name}: the record is too large ({rec.dsize:,} B decompressed "
                                f"> limit {max_size:,} B)"))
        start = HEADER_SIZE + rec.offset
        try:
            raw = lz4.block.decompress(self.data[start:start + rec.csize], uncompressed_size=rec.dsize)
        except Exception as e:   # noqa: BLE001  lz4.block.LZ4BlockError は ValueError ではない
            raise ValueError(tr(f".arz の形式が不正: {rec.name}: 展開できない: {e}",
                                f"Invalid .arz format: {rec.name}: cannot decompress: {e}")) from None
        if len(raw) != rec.dsize:
            raise ValueError(tr(f".arz の形式が不正: {rec.name}: 展開後の大きさが一致しない（{len(raw)} != {rec.dsize}）",
                                f"Invalid .arz format: {rec.name}: the decompressed size does not match "
                                f"({len(raw)} != {rec.dsize})"))
        pos = 0
        fields = []
        strings = self.strings
        try:
            while pos < len(raw):
                if max_fields is not None and len(fields) >= max_fields:
                    raise ValueError(self._too_many_fields(rec, max_fields))
                ftype, cnt, sidx = struct.unpack_from("<HHI", raw, pos)
                pos += 8
                if ftype == T_INT:
                    vals = list(struct.unpack_from("<%di" % cnt, raw, pos))
                elif ftype == T_FLOAT:
                    vals = list(struct.unpack_from("<%df" % cnt, raw, pos))
                else:
                    vals = list(struct.unpack_from("<%dI" % cnt, raw, pos))
                    if ftype == T_STRING:
                        vals = [strings[v] for v in vals]
                pos += 4 * cnt
                fields.append((strings[sidx], ftype, vals))
        except (struct.error, IndexError) as e:
            raise ValueError(tr(f".arz の形式が不正: {rec.name}: フィールドの並びが壊れている: {e}",
                                f"Invalid .arz format: {rec.name}: the field data is corrupted: {e}")) from None
        if pos != len(raw):
            raise ValueError(tr(f".arz の形式が不正: {rec.name}: フィールドの並びがレコードの終わりと合わない",
                                f"Invalid .arz format: {rec.name}: the field data does not end at the end of the "
                                "record"))
        rec.fields = fields
        return fields

    @staticmethod
    def _too_many_fields(rec: ArzRecord, max_fields: int) -> str:
        return tr(f".arz の形式が不正: {rec.name}: 項目が多すぎる（上限 {max_fields:,} 個）",
                  f"Invalid .arz format: {rec.name}: too many fields (limit {max_fields:,})")

    def record_fields(self, name: str) -> list[tuple[str, str]] | None:
        """レコードを .dbr と同じ (name, value) の並びで返す（無ければ None）。"""
        rec = self.get(name)
        if rec is None:
            return None
        return parse_dbr_text(record_text(self.decode(rec)))

    def record_pairs(self, name: str) -> list[tuple[str, str]] | None:
        """:meth:`record_fields` と同じ (name, value) の並びを、テキストを経由せずに展開の結果から作る（無ければ None）。

        record_fields は「name,value,」のテキストを行に分けて読み直すので、名前・文字列の値に改行を入れた細工したレコードは
        別の項目に見え、名前の前後の空白も落ちる。受け取った MOD の検査はこちらを使う（2026-10-20 レビュー R12A-01）。
        公式のデータでは record_fields と同じ結果になる（公式の 4 つの .arz の全 93,863 レコードで確認。2026-10-23）。"""
        rec = self.get(name)
        if rec is None:
            return None
        return [(fname, ";".join(_format_value(ftype, v) for v in vals)) for fname, ftype, vals in self.decode(rec)]


def _format_value(ftype: int, v) -> str:
    if ftype == T_FLOAT:
        return "%.6f" % v
    return str(v)


def record_text(fields: list) -> str:
    """ArchiveTool ``-database`` と同じ書式（name,value, 改行 LF）。"""
    lines = []
    for name, ftype, vals in fields:
        lines.append("%s,%s," % (name, ";".join(_format_value(ftype, v) for v in vals)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# .tpl（テンプレート）
# ---------------------------------------------------------------------------
_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*"(.*)"\s*$')


@dataclass
class TemplateVar:
    name: str
    cls: str
    vtype: str
    default: str
    source: str

    @property
    def arz_type(self) -> int:
        return TPL_TYPE_MAP.get(self.vtype, T_STRING)

    @property
    def is_array(self) -> bool:
        return self.cls == "array"

    @property
    def is_file(self) -> bool:
        return self.vtype.startswith("file_")


class TemplateSet:
    """``templates/*.tpl`` を含むルートからテンプレートを解決する（include は再帰展開）。"""

    def __init__(self, database_root: str):
        self.root = database_root
        self.index: dict[str, str] = {}
        for dirpath, _dirs, files in os.walk(database_root):
            for f in files:
                if f.lower().endswith(".tpl"):
                    full = os.path.join(dirpath, f)
                    self.index[norm_path(os.path.relpath(full, database_root))] = full
        if not self.index:
            raise FileNotFoundError(tr(f"テンプレート（.tpl）が見つかりません: {database_root}",
                                       f"Templates (.tpl) were not found: {database_root}"))
        self.cache: dict[str, dict[str, TemplateVar]] = {}

    def resolve(self, tpl_ref: str) -> str:
        rel = norm_path(tpl_ref)
        rel = re.sub(r"^%[^%]*%", "", rel)   # include 行の %TEMPLATE_DIR% などを除く
        if rel.startswith("database/"):
            rel = rel[len("database/"):]
        if rel in self.index:
            return rel
        raise KeyError(tr(f"テンプレートが見つかりません: {tpl_ref}", f"Template not found: {tpl_ref}"))

    def load(self, tpl_ref: str) -> dict[str, TemplateVar]:
        rel = self.resolve(tpl_ref)
        if rel not in self.cache:
            variables: dict[str, TemplateVar] = {}
            self._parse_into(rel, variables, [])
            self.cache[rel] = variables
        return self.cache[rel]

    def _parse_into(self, rel: str, variables: dict, stack: list) -> None:
        if rel in stack:
            return
        stack = stack + [rel]
        with open(self.index[rel], "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        for var in self._iter_variables(text):
            if var.get("type") == "include":
                inc = var.get("defaultValue") or var.get("value") or ""
                if inc:
                    try:
                        inc_rel = self.resolve(inc)
                    except KeyError:
                        raise KeyError(tr(f"{rel} が include する {inc} が見つかりません",
                                          f"{inc} included by {rel} was not found"))
                    self._parse_into(inc_rel, variables, stack)
                continue
            name = var.get("name", "")
            if name:
                variables.setdefault(name, TemplateVar(name, var.get("class", ""), var.get("type", ""),
                                                       var.get("defaultValue", ""), rel))

    @staticmethod
    def _iter_variables(text: str):
        lines = text.splitlines()
        i, n = 0, len(lines)
        while i < n:
            if lines[i].strip() == "Variable":
                j = i + 1
                while j < n and lines[j].strip() != "{":
                    j += 1
                j += 1
                var = {}
                while j < n and lines[j].strip() != "}":
                    m = _KV_RE.match(lines[j])
                    if m:
                        var[m.group(1)] = m.group(2)
                    j += 1
                yield var
                i = j + 1
            else:
                i += 1


# ---------------------------------------------------------------------------
# .arz 書き出し（build_arz.py の移植）
# ---------------------------------------------------------------------------
class _StringTable:
    def __init__(self):
        self.list: list[str] = []
        self.index: dict[str, int] = {}

    def add(self, s: str) -> int:
        i = self.index.get(s)
        if i is None:
            i = len(self.list)
            self.list.append(s)
            self.index[s] = i
        return i

    def pack(self) -> bytes:
        out = [struct.pack("<I", len(self.list))]
        for s in self.list:
            b = s.encode("utf-8")
            out.append(struct.pack("<I", len(b)))
            out.append(b)
        return b"".join(out)


def _encode_value(tv: TemplateVar, value: str, strings: _StringTable, where: str) -> int:
    t = tv.arz_type
    if t == T_INT:
        try:
            return int(value) & 0xffffffff
        except ValueError:
            try:
                return int(float(value)) & 0xffffffff
            except ValueError:
                raise ValueError(tr(f"{where}: int フィールド {tv.name} の値が不正: {value!r}",
                                    f"{where}: invalid value for int field {tv.name}: {value!r}"))
    if t == T_FLOAT:
        try:
            f = float(value)
        except ValueError:
            raise ValueError(tr(f"{where}: real フィールド {tv.name} の値が不正: {value!r}",
                                f"{where}: invalid value for real field {tv.name}: {value!r}"))
        return struct.unpack("<I", struct.pack("<f", f))[0]
    if t == T_BOOL:
        try:
            return 1 if int(float(value)) else 0
        except ValueError:
            raise ValueError(tr(f"{where}: bool フィールド {tv.name} の値が不正: {value!r}",
                                f"{where}: invalid value for bool field {tv.name}: {value!r}"))
    if tv.is_file:
        value = norm_path(value)
    return strings.add(value)


def _compile_record(rel_name: str, fields: list[tuple[str, str]], templates: TemplateSet, strings: _StringTable,
                    ignore_unknown: bool, warnings: list[str]) -> tuple[bytes, str]:
    fdict: dict[str, str] = {}
    order: list[str] = []
    for name, value in fields:
        if name in fdict:
            warnings.append(tr(f"{rel_name}: フィールド {name} が重複（後の値を採用）",
                               f"{rel_name}: duplicate field {name} (the later value is used)"))
        else:
            order.append(name)
        fdict[name] = value
    tpl_name = fdict.get("templateName", "")
    if not tpl_name:
        raise ValueError(tr(f"{rel_name}: templateName がありません", f"{rel_name}: templateName is missing"))
    tvars = templates.load(tpl_name)
    strings.add(rel_name)
    chunks = [struct.pack("<HHII", T_STRING, 1, strings.add("templateName"), strings.add(norm_path(tpl_name)))]
    class_value = ""
    for name in sorted(n for n in order if n != "templateName"):
        value = fdict[name]
        if value.strip() == "":
            continue
        tv = tvars.get(name)
        if tv is None:
            msg = tr(f"{rel_name}: テンプレート {tpl_name} に無いフィールド {name}",
                     f"{rel_name}: field {name} is not in template {tpl_name}")
            if ignore_unknown:
                warnings.append(msg + tr("（無視）", " (ignored)"))
                continue
            raise ValueError(msg)
        if name == "Class":
            class_value = value
        if tv.is_array:
            values = split_array(value)
        elif tv.arz_type == T_STRING:
            values = [value]
        else:
            values = [value.split(";")[0]]
        if not values:
            continue
        encoded = [_encode_value(tv, v, strings, rel_name) for v in values]
        chunks.append(struct.pack("<HHI", tv.arz_type, len(encoded), strings.add(name)))
        chunks.append(struct.pack("<%dI" % len(encoded), *encoded))
    return b"".join(chunks), class_value


def compile_records(records: dict[str, list[tuple[str, str]]], templates: TemplateSet, out_path: str,
                    filetime: int, ignore_unknown: bool = False) -> dict:
    """{レコード名: [(name, value)]} を .arz に書き出す。レコードは名前順。戻り値は件数・大きさ・警告。"""
    if not records:
        raise ValueError(tr("レコードがありません", "There are no records"))
    strings = _StringTable()
    warnings: list[str] = []
    data_parts = []
    entries = []
    offset = 0
    for rel in sorted(norm_path(r) for r in records):
        src = records.get(rel)
        if src is None:   # 呼び出し側が大文字を含む名前で渡した場合
            src = next(v for k, v in records.items() if norm_path(k) == rel)
        raw, class_value = _compile_record(rel, src, templates, strings, ignore_unknown, warnings)
        comp = lz4.block.compress(raw, store_size=False)
        entries.append((strings.index[rel], class_value, offset, len(comp), len(raw), filetime))
        data_parts.append(comp)
        offset += len(comp)
    data_blob = b"".join(data_parts)
    rt = []
    for name_idx, cls, off, csize, dsize, ft in entries:
        cb = cls.encode("utf-8")
        rt.append(struct.pack("<II", name_idx, len(cb)) + cb + struct.pack("<IIIQ", off, csize, dsize, ft))
    rt_blob = b"".join(rt)
    st_blob = strings.pack()
    rt_start = HEADER_SIZE + len(data_blob)
    st_start = rt_start + len(rt_blob)
    header = struct.pack(HEADER_FMT, 2, 3, rt_start, len(rt_blob), len(entries), st_start, len(st_blob))
    trailer = _trailer(header, data_blob, rt_blob, st_blob)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header + data_blob + rt_blob + st_blob + trailer)
    return {"records": len(entries), "strings": len(strings.list),
            "bytes": HEADER_SIZE + len(data_blob) + len(rt_blob) + len(st_blob) + TRAILER_SIZE, "warnings": warnings}


# ---------------------------------------------------------------------------
# 意味的な比較（arzdiff.py の移植）
# ---------------------------------------------------------------------------
def _canon(fields: list) -> list:
    out = []
    for name, ftype, vals in fields:
        if ftype == T_FLOAT:
            vals = [struct.unpack("<I", struct.pack("<f", v))[0] for v in vals]
        out.append((name, ftype, tuple(vals)))
    return out


def diff(a: ArzFile, b: ArzFile, ignore_time: bool = True, rename: dict[str, str] | None = None,
         value_map: dict[str, str] | None = None) -> dict:
    """2 つの .arz をレコード単位で比較する。

    ``rename``: A のレコード名 → B のレコード名（名前が違う同じ役割のレコードを対応づける）。
    ``value_map``: A の文字列値 → B の文字列値（レコードのパスやタグ名の読み替え。配列の要素ごとに適用）。
    戻り値: ``{"common", "same", "only_a", "only_b", "differences": [str], "a_trailer_ok", "b_trailer_ok"}``
    """
    rename = {norm_path(k): norm_path(v) for k, v in (rename or {}).items()}
    value_map = dict(value_map or {})
    lower_map = {k.lower(): v for k, v in value_map.items()}
    ra = {rename.get(r.name, r.name): r for r in a.records}
    rb = {r.name: r for r in b.records}
    out = {"common": 0, "same": 0, "only_a": sorted(set(ra) - set(rb)), "only_b": sorted(set(rb) - set(ra)),
           "differences": [], "a_trailer_ok": a.trailer_ok(), "b_trailer_ok": b.trailer_ok()}

    def mapv(ftype, vals):
        if ftype != T_STRING or not value_map:
            return vals
        return [value_map.get(v, lower_map.get(v.lower(), v)) for v in vals]

    for n in sorted(set(ra) & set(rb)):
        x, y = ra[n], rb[n]
        out["common"] += 1
        rec_diff = []
        if x.rtype != y.rtype:
            rec_diff.append(f"type {x.rtype!r} != {y.rtype!r}")
        if not ignore_time and x.filetime != y.filetime:
            rec_diff.append(f"filetime {x.filetime} != {y.filetime}")
        fa = _canon([(nm, t, mapv(t, v)) for nm, t, v in a.decode(x)])
        fb = _canon(b.decode(y))
        if fa != fb:
            da = {f[0]: f for f in fa}
            db = {f[0]: f for f in fb}
            for name in sorted(set(da) | set(db)):
                if name not in da:
                    rec_diff.append(tr(f"field {name}: B のみ {db[name][1:]!r}",
                                       f"field {name}: only in B {db[name][1:]!r}"))
                elif name not in db:
                    rec_diff.append(tr(f"field {name}: A のみ {da[name][1:]!r}",
                                       f"field {name}: only in A {da[name][1:]!r}"))
                elif da[name] != db[name]:
                    (ta, va), (tb, vb) = da[name][1:], db[name][1:]
                    if ta == tb == T_STRING and (len(va) > 1 or len(vb) > 1):
                        added = [v for v in vb if v not in va]
                        removed = [v for v in va if v not in vb]
                        rec_diff.append(tr(f"field {name}: 配列 A {len(va)} 要素 / B {len(vb)} 要素",
                                           f"field {name}: array A {len(va)} elements / B {len(vb)} elements")
                                        + (tr(f"、B で追加 {added}", f", added in B {added}") if added else "")
                                        + (tr(f"、B で削除 {removed}", f", removed in B {removed}") if removed else "")
                                        + (tr("、並びが違う", ", different order") if not added and not removed else ""))
                    else:
                        rec_diff.append(f"field {name}: A={da[name][1:]!r} B={db[name][1:]!r}")
            if not rec_diff:
                rec_diff.append(tr("フィールド順が異なる", "different field order"))
        if rec_diff:
            out["differences"].append(f"{n}: " + "; ".join(rec_diff))
        else:
            out["same"] += 1
    return out
