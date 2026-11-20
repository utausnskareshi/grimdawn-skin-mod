"""マルチプレイ用の MOD の共有ファイル（``.gdskinmod``）の書き出し・検査・受け取り（docs/app_architecture.md 15.2 節）。

ビルド済みの mods/ のファイル（database.arz・Text_XX.arc・Items.arc。Text_XX.arc は 2026-10-17 から 13 言語分、以前の版の
ビルドは Text_EN.arc[・Text_JA.arc]）を**そのまま**（作り直さず、同じ
バイト列で）受け渡す。マルチプレイでは全員の MOD が同一である必要があり、照合されるのはデータベースだけという
利用者の報告がある（docs/multiplayer_check.md。ゲーム内では未確認）。

中身（zip）::

  gdskin_mod.json        記録（形式・版・作成ツールの版・ビルドID・装備の一覧・ゲームの構成・照合コード（mod_code）・
                         データベースのコード（check_code）・全ファイルの SHA-256）
  mod/database.arz, mod/Text_XX.arc（13 言語。以前の版は Text_EN[・Text_JA]）, mod/Items.arc
  README.txt             使い方（アプリを使わない人向け）

受け取り（:func:`inspect`）: 共有ファイルの検査（package_io）→ ``<データ>\\shared\\received\\<package_id>\\`` に展開
（modbuilder のビルドと同じ形の build_manifest.json を書くので、導入・退避・ハッシュ照合は modbuilder の install を
そのまま使える）→ 中身の検査（:func:`check_content`）。他人の MOD はゲームのデータを書き換えうるので、本ツールが作る
範囲（records/items/aiskin/ の装備・消耗品・透明の部位、補給官 4 件、tagAISkin のタグ、Items.arc の aiskin/<ID>/ と
MOD のアイコン aiskin/icons/ と透明の肩・メダルの見た目 aiskin/invisible/）の外を
書き換えるものは導入させない。範囲の中の書き換え（templateName・Class・補給官の項目・消耗品の項目など）は、項目ごとの
違いを注意に出して、利用者が確かめてから導入する（2026-10-17 監査 C14 の利用者の判断）。

2 つのコード（2026-10-17 監査 C15 の利用者の判断）:

* 照合コード（:func:`mod_code`。記録・結果のキーは ``mod_code``）: MOD 全体（全ファイルの名前と SHA-256）から作る。
  相手と読み合わせるのはこれで、同じなら同じ MOD（見た目の Items.arc・表示名の Text_XX.arc も同じ）。
* データベースのコード（:func:`check_code`。キーは互換のため ``check_code`` のまま）: database.arz だけから作る。
  接続できるかの目安として詳細の欄にだけ出す（見た目・表示名だけを変えた作り直しでは同じになる）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter

from . import __version__, logs, package_io
from .errors import BackendError
from .i18n import tr

KIND = "mod"
RECEIVED = ("shared", "received")
# 末尾は \Z（$ は末尾の改行の直前でも一致する。2026-10-27 検証 R12A-05）
HEAD_RE = re.compile(r"^records/items/aiskin/(eq[0-9a-f]{10})/head\.dbr\Z")
SET_RE = re.compile(r"^records/items/aiskin/(eq[0-9a-f]{10})/illusion_set\.dbr\Z")
ITEMS_RE = re.compile(r"^aiskin/(eq[0-9a-f]{10})/[a-z0-9_.\-]+\.(msh|tex)\Z")
# MOD のアイテムのアイコン（2026-11-11 から。modbuilder.icons が作る 3 つだけ）
ICON_ENTRY_RE = re.compile(r"^aiskin/icons/(illusion|head|invisible|invisible_medal)\.tex\Z")
ICON_MAX = 64 * 1024        # アイコン 1 つの展開の上限（このアプリのアイコンは 16,524 バイト）
# 自前の透明の肩・メダルの見た目（2026-11-11 D-079。modbuilder.invisible_look が作る画像とメッシュの 2 つだけ）
LOOK_ENTRY_RE = re.compile(r"^aiskin/invisible/invisible\.(tex|msh)\Z")
LOOK_MAX = 64 * 1024        # 展開の上限（画像は 4,236 バイト、メッシュは約 770 バイト）
# このアプリの場所（Items.arc の aiskin/ = ゲームの items/aiskin/。公式のファイルは無い）の中のファイル。上の 3 つに当たらない
# ものの止める理由を分ける（新しい版のアプリの MOD か書き換え。2026-11-18 点検 backend_modpkg-03）。「.」「..」などの点で
# 始まる部分は含めない（aiskin/../ で外を指すものは、公式のファイルを置き換えるものとして止める）
NAMESPACE_ENTRY_RE = re.compile(r"^aiskin(/[a-z0-9_\-][a-z0-9_.\-]*)+\Z")
TAG_RE = re.compile(r"^tagAISkin[A-Za-z0-9_]*\Z")
TEXT_ARC_RE = re.compile(r"Text_([A-Z]{2})\.arc")
# 受け取った MOD の検査で展開する大きさの上限（2026-10-20 レビュー R05B-01）。LZ4 は同じバイトの並びを約 255 倍に縮めるので、
# 数 MB の細工したファイルで GB 単位の展開をさせられる。根拠: 公式の 4 つの .arz のレコードの最大は 35,988 B、本ツールの
# MOD（26 レコード）は最大 8,840 B・合計 182,928 B、タグファイルは約 3 KB
MAX_RECORD_SIZE = 1 << 20           # レコード 1 件（展開後）
MAX_DATABASE_SIZE = 64 << 20        # database.arz の全レコードの合計（展開後）
MAX_TAGS_FILE = 4 << 20             # Text_XX.arc のタグファイル 1 つ（展開後）
# database.arz のファイルの大きさ・レコード表の件数・全レコードの項目の数の上限（2026-10-27 点検 R05B-01・R05B-02）。
# 本ツールの出力はレコードが装備数×2＋8 件・装備 9 件で展開後 約 18 万 B。項目は装備 1 件あたり 1,395 個（head 660・
# illusion_set 735）と、装備数によらないその他の 8 レコードで 2,644 個（2026-11-04 実ビルド 4 つで実測）。
# 全レコードが同じ空のかたまりを指す細工（展開後の大きさの上限を 0 B で通る）や、8 バイトの空の項目の並び（Python の
# オブジェクトで約 15 倍に膨らむ）で、数 KB のファイルに GB 単位のメモリを使わせない。項目の数の予算は、レコード表の
# 名前（展開の前に分かる）から数えた装備数に比例させる（定数にすると、本ツールが作る大きな MOD を受け取れない。
# 点検 D2_backend-02）
MAX_DATABASE_FILE = 16 << 20
MAX_RECORDS = 20_000
# 受け取って検査できる装備の数。受け取る側の 1 ファイルの上限（package_io.MAX_FILE、1 GiB）で Items.arc は装備 約 340〜440 件
# までなので、それより低い上限は作らない。MOD の共有ファイルの書き出し（run_export）も同じ値で断る
MAX_EQUIPMENT = 1_000
BASE_FIELDS = 5_000                 # 装備によらないレコードの分（実測 2,644）
FIELDS_PER_EQUIPMENT = 2_000        # 装備 1 件あたり（実測 1,395）
MAX_RECORD_FIELDS = 5_000           # レコード 1 件（公式の最大のレコード 35,988 B でも 8 バイトの項目で 約 4,500 個）
MAX_TOTAL_FIELDS = BASE_FIELDS + FIELDS_PER_EQUIPMENT * MAX_EQUIPMENT     # 予算の最大（2,005,000 個）


def field_budget(equipment: int) -> int:
    """装備 equipment 件の MOD の、全レコードの項目の数の予算。"""
    return BASE_FIELDS + FIELDS_PER_EQUIPMENT * max(0, equipment)
MAX_BLOCKING = 100                  # 確認の結果・記録に入れる止める理由の件数（超えた分は「ほか N 件」）
# レコードの項目名として認める形（公式には 'color.a' や数字で始まる名前もある）と、文字列の値に認めない文字
# （改行・制御文字。本ツールのビルドも公式のデータも作らない。2026-10-20 レビュー R12A-01）
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+\Z")
BAD_VALUE_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def received_root(svc) -> str:
    return os.path.join(svc.home(), *RECEIVED)


def _code(hexdigest: str) -> str:
    s = hexdigest[:12].upper()
    return f"{s[0:4]}-{s[4:8]}-{s[8:12]}"


def check_code(sha256: str) -> str:
    """データベースのコード: database.arz の SHA-256 の先頭 12 桁（4 桁ずつ区切る）。接続できるかの目安（照合される
    のはデータベースだけという利用者の報告による）。見た目（Items.arc）・表示名（Text_XX.arc）だけを変えた作り直しでは
    同じになるので、相手と読み合わせる照合コードには使わない（:func:`mod_code`。2026-10-17 監査 C15）。"""
    return _code(sha256)


def mod_code(files: dict) -> str:
    """照合コード: MOD 全体のコード。全ファイルの「名前:SHA-256（小文字）」を名前順に 1 行ずつ並べた文字列（UTF-8）の
    SHA-256 の先頭 12 桁（4 桁ずつ区切る）。同じなら同じ MOD。files は {名前: {"sha256": …}} か {名前: SHA-256}
    （ビルド・導入の記録の files、共有ファイルの mod_files。2026-10-17 監査 C15）。"""
    lines = "".join(f"{n}:{str(v['sha256'] if isinstance(v, dict) else v).lower()}\n" for n, v in sorted(files.items()))
    return _code(hashlib.sha256(lines.encode("utf-8")).hexdigest())


# ---------------------------------------------------------------------------
# 照合の詳しい表示（2026-10-17 機能 SF13。表示だけで、照合コード・データベースのコードの定義は変えない）
# ---------------------------------------------------------------------------
def _sha(v) -> str:
    return str(v["sha256"] if isinstance(v, dict) else v).lower()


def file_codes(files: dict) -> dict[str, str]:
    """ファイルごとの短いコード（SHA-256 の先頭 12 桁。database.arz のものはデータベースのコードと同じ）。"""
    return {n: _code(_sha(v)) for n, v in sorted(files.items())}


def compare_files(mine: dict | None, theirs: dict | None) -> dict | None:
    """2 つの MOD のファイルの一覧（{名前: {"sha256"}} か {名前: SHA-256}）を比べる。どちらかが無ければ None。

    relation: same（全ファイルが同じ = 照合コードが同じ）/ look_only（database.arz は同じで、見た目の Items.arc・表示名の
    Text_XX.arc だけが違う。接続はできる見込み）/ database（database.arz が違う。接続できない見込み）。"""
    if not mine or not theirs:
        return None
    a = {n: _sha(v) for n, v in mine.items()}
    b = {n: _sha(v) for n, v in theirs.items()}
    names = sorted(set(a) | set(b))
    differ = [n for n in names if a.get(n) != b.get(n)]
    if not differ:
        relation = "same"
    elif a.get("database.arz") and a.get("database.arz") == b.get("database.arz"):
        relation = "look_only"
    else:
        relation = "database"
    return {"relation": relation, "differ": differ, "only_mine": [n for n in names if n not in b],
            "only_theirs": [n for n in names if n not in a]}


_CODE_SEP = re.compile(r"[\s\-‐-―ー－_:：]+")


def normalize_code(text) -> str | None:
    """相手が伝えたコードを XXXX-XXXX-XXXX の形にする（区切り・空白・大文字小文字・全角を問わない）。12 桁の 16 進で
    なければ None。"""
    if not isinstance(text, str):
        return None
    import unicodedata
    s = _CODE_SEP.sub("", unicodedata.normalize("NFKC", text)).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", s):
        return None
    return f"{s[0:4]}-{s[4:8]}-{s[8:12]}"


def _received_list(svc) -> list[dict]:
    """受け取った MOD（導入できるもの）の {"share_id", "file_name", "mod_code", "check_code"}。"""
    from modbuilder.builder import MANIFEST
    root = received_root(svc)
    out = []
    if not os.path.isdir(root):
        return out
    for sid in sorted(os.listdir(root)):
        try:
            with open(os.path.join(root, sid, MANIFEST), encoding="utf-8") as f:
                m = json.load(f)
            if m.get("status") != "ok":
                continue
            files = m["files"]["basemods"]
            sh = m.get("shared") or {}
            out.append({"share_id": sid, "file_name": sh.get("file_name"), "mod_code": mod_code(files),
                        "check_code": check_code(files["database.arz"]["sha256"])})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def match_code(svc, code) -> dict:
    """相手が伝えたコードを、導入中の MOD・最新のビルド・受け取った MOD の照合コードとデータベースのコードと比べる
    （機能 SF13）。→ {"code", "verdict", "matches": [{"target", "kind", "build_id", "file_name"}]}。

    verdict: installed / latest_build / received（照合コードが一致）、database_installed / database_latest_build /
    database_received（データベースのコードだけが一致）、none。"""
    from . import modops
    c = normalize_code(code)
    if c is None:
        raise BackendError("INVALID_INPUT", tr("コードは 12 桁の英数字（0-9・A-F。例 1A2B-3C4D-5E6F）で入力してください",
                                               "Enter the code as 12 alphanumeric characters (0-9, A-F; for example "
                                               "1A2B-3C4D-5E6F)"),
                           params={"reason": "code_format"})
    t = modops.compare_targets(svc)
    cands = []
    for what in ("installed", "latest_build"):
        x = t.get(what)
        if x:
            cands.append({"target": what, "build_id": x.get("build_id"), "file_name": x.get("file_name"),
                          "mod_code": mod_code(x["files"]), "check_code": check_code(_sha(x["files"]["database.arz"]))})
    for r in _received_list(svc):
        cands.append({"target": "received", "build_id": "shared-" + r["share_id"], "file_name": r["file_name"],
                      "mod_code": r["mod_code"], "check_code": r["check_code"]})
    matches = [{"target": x["target"], "kind": kind, "build_id": x["build_id"], "file_name": x["file_name"]}
               for kind, key in (("mod", "mod_code"), ("database", "check_code")) for x in cands if x[key] == c]
    verdict = "none"
    for m in matches:          # 照合コードの一致を先に、導入中 → 最新のビルド → 受け取った MOD の順
        verdict = m["target"] if m["kind"] == "mod" else "database_" + m["target"]
        break
    return {"code": c, "verdict": verdict, "matches": matches}


def _game_databases(gd_dir: str | None) -> dict:
    """ゲームの構成（データベースの有無と大きさ。パスや日時は入れない）。"""
    from modbuilder.gamedata import DATABASES
    out = {}
    if gd_dir:
        for key, rel in DATABASES.items():
            p = os.path.join(gd_dir, rel.replace("/", os.sep))
            if os.path.isfile(p):
                out[key] = os.path.getsize(p)
    return out


# ---------------------------------------------------------------------------
# 書き出し
# ---------------------------------------------------------------------------
# 共有ファイルの README.txt の文（{code}・{build}・{count}・{db_code} を埋める）。受け取る人の言語は分からないので、書き出したときの
# 表示言語に依らず日本語と英語の両方を書く（同じビルドなら README も package_id も表示言語に依らない。2 言語対応 D-060）
README = {"ja": """GD Skin Studio の MOD の共有ファイル（.gdskinmod）

このファイルは zip です。GD Skin Studio の「MOD」ページの「共有ファイルを読み込んで導入…」で読み込むと、
中身を確認してから、今の mods\\ のファイルを退避して導入します（元に戻せます）。

アプリを使わずに入れる場合: mod\\ の中のファイルを <Grim Dawn>\\mods\\ に置き、Steam の起動オプションに /basemods を
設定します。mods\\database.arz は 1 つしか読まれないため、他のメインキャンペーン用 MOD と同時には使えません。
今の mods\\ のファイルは先に別の場所へ退避してください。

照合コード: {code}
（MOD 全体のコード。相手のアプリに表示される照合コードと同じなら、同じ MOD です）
ビルド: {build}
装備: {count} 件

データベースのコード（database.arz だけのコード。接続できるかの目安）: {db_code}
""", "en": """GD Skin Studio MOD share file (.gdskinmod)

This file is a zip file. Import it with "Import and install a share file…" on the "MOD" page of GD Skin Studio:
the app shows its contents for you to check, then backs up the current files in mods\\ and installs it (you can undo this).

To install without the app: put the files inside mod\\ into <Grim Dawn>\\mods\\ and add /basemods to the Steam launch
options. Only one mods\\database.arz is read, so this cannot be used together with other main-campaign MODs.
Move the current files in mods\\ somewhere else first.

Verification code: {code}
(The code for the whole MOD. If it is the same as the verification code shown in the other person's app, it is the same MOD)
Build: {build}
Equipment: {count}

Database code (the code for database.arz alone; a guide to whether you can connect): {db_code}
"""}


def readme() -> str:
    """共有ファイルの README.txt の雛形（日本語の後に英語。{code}・{build}・{count}・{db_code} を埋める）。"""
    return README["ja"] + "\n" + "-" * 72 + "\n\n" + README["en"]


def run_export(svc, ctx, params: dict) -> dict:
    """ジョブ export_mod: {"build_id"（省くと最新の成功したビルド）, "dest": "...\\x.gdskinmod", "overwrite": false}。"""
    from modbuilder.builder import load_manifest
    from modbuilder.library import sha256_file
    from . import modops
    from .settings import resolve_game_dir
    dest = str(params.get("dest") or "")
    if not dest:
        raise BackendError("INVALID_INPUT", tr("出力先のファイルを指定してください", "Specify the destination file"),
                           params={"reason": "package_path"})
    if not dest.lower().endswith(package_io.KINDS[KIND]["ext"]):
        dest += package_io.KINDS[KIND]["ext"]
    with svc.mod_lock:
        b = modops.get_build(svc, params.get("build_id"))
        m = load_manifest(b["build_dir"])
        # 断るのは開発用の装備（dev_only。試作の装備）を実際に含むビルドだけ。設定「開発用の装備も MOD に入れる」で作った
        # ビルド（profile "dev"）でも、開発用の装備が無ければ中身は製品版と同じなので書き出す。dev_only の無い古い記録は
        # profile が dev なら開発用とみなす（2026-10-20 レビュー R12A-05）
        listed = (m.get("library") or {}).get("equipment") or []
        dev = [e for e in listed if e.get("dev_only", m.get("profile") == "dev")]
        if dev or (m.get("profile") == "dev" and not listed):
            raise BackendError("INVALID_INPUT", tr("開発用のビルド（試作の装備を含む）は共有できません",
                                                   "Development builds (with prototype equipment) cannot be shared")
                               + (": " + ", ".join(str(e.get("name_ja") or e.get("name") or e.get("id")) for e in dev[:5])
                                  if dev else ""),
                               params={"reason": "package_dev_build", "equipment": [e.get("id") for e in dev]})
        files = (m.get("files") or {}).get("basemods") or {}
        if "database.arz" not in files:
            raise BackendError("NOT_FOUND", tr("ビルドに database.arz がありません", "The build has no database.arz"))
        # 受け取る側の検査（check_content の装備の数・package_io.open_package の 1 ファイルの大きさ）で導入できない大きさは、
        # 書き出す前に断る（作れて送れるのに、受け取った人には壊れたファイルに見えていた。点検 D2_backend-02）
        if len(listed) > MAX_EQUIPMENT:
            raise BackendError("INVALID_INPUT", tr(
                f"装備が多すぎるため共有できません（{len(listed):,} 件。受け取る側で検査できるのは {MAX_EQUIPMENT:,} 件まで）。"
                f"装備を減らしたビルドを作ってから書き出してください",
                f"This build cannot be shared because it has too much equipment ({len(listed):,}; the receiver can check "
                f"up to {MAX_EQUIPMENT:,}). Build with less equipment and export again"),
                params={"reason": "package_too_many_equipment", "count": len(listed), "max": MAX_EQUIPMENT})
        big = sorted(n for n, r in files.items() if int(r.get("size") or 0) > package_io.MAX_FILE)
        if big:
            raise BackendError("INVALID_INPUT", tr(
                f"ファイルが大きすぎるため共有できません（{', '.join(big)}。受け取る側で読めるのは 1 ファイル "
                f"{package_io.MAX_FILE:,} B まで）。装備を減らしたビルドを作ってから書き出してください",
                f"This build cannot be shared because a file is too large ({', '.join(big)}; the receiver can read up to "
                f"{package_io.MAX_FILE:,} B per file). Build with less equipment and export again"),
                params={"reason": "package_file_too_large", "files": big, "max_bytes": package_io.MAX_FILE})
        ctx.progress(0.05, "package_export")
        with package_io.PackageWriter(dest, KIND, overwrite=bool(params.get("overwrite"))) as w:
            for k, (name, rec) in enumerate(sorted(files.items())):
                ctx.check_cancel()
                src = os.path.join(b["build_dir"], rec["path"].replace("/", os.sep))
                if sha256_file(src) != rec["sha256"]:
                    raise BackendError("EXPORT_FAILED", tr(f"ビルドのファイルが記録と一致しません（ビルドの後に変更された）: {name}",
                                                           f"A build file does not match the record (changed after the build): "
                                                           f"{name}"))
                got = w.add_file("mod/" + name, src)
                if got["sha256"] != rec["sha256"]:
                    raise BackendError("EXPORT_FAILED", tr(f"書き出したファイルのハッシュが一致しません: {name}",
                                                           f"The hash of the exported file does not match: {name}"))
                ctx.progress(0.05 + 0.85 * (k + 1) / len(files), "package_export", {"index": k + 1, "count": len(files)})
            equipment = [{k: e.get(k) for k in ("id", "name", "name_ja", "created_at", "head_record")}
                         for e in (m.get("library") or {}).get("equipment", [])]
            code = check_code(files["database.arz"]["sha256"])
            mcode = mod_code(files)
            w.add_bytes("README.txt", readme().format(code=mcode, db_code=code, build=m.get("build_id"), count=len(equipment))
                        .replace("\n", "\r\n").encode("utf-8"))
            manifest = w.finish({
                "created_at": package_io.now_iso(),
                "tool": {"app": "GD Skin Studio", "backend": __version__, "modbuilder": (m.get("tool") or {}).get("version")},
                "build": {"build_id": m.get("build_id"), "created_at": m.get("created_at"), "profile": m.get("profile"),
                          "languages": m.get("languages"), "source_time": m.get("source_time")},
                "mod_files": {n: {"sha256": r["sha256"], "size": r["size"]} for n, r in sorted(files.items())},
                "mod_code": mcode,           # 照合コード（MOD 全体）
                "check_code": code,          # データベースのコード（意味は以前の版と同じ。読む側は mod_files から計算し直す）
                "equipment": equipment,
                "game": {"databases": _game_databases(resolve_game_dir(svc.settings.get()))},
            })
    size = os.path.getsize(w.dest)
    logs.info(tr(f"MOD の共有ファイルを書き出しました: ビルド {m.get('build_id')}、{size:,} B、照合コード {mcode}"
                 f"（データベースのコード {code}）",
                 f"Exported the MOD share file: build {m.get('build_id')}, {size:,} B, verification code {mcode}"
                 f" (database code {code})"))
    ctx.progress(1.0, "package_export_done")
    return {"dest": w.dest, "package_id": manifest["package_id"], "mod_code": mcode, "check_code": code, "size": size,
            "build_id": m.get("build_id"), "equipment": equipment}


# ---------------------------------------------------------------------------
# 中身の検査
# ---------------------------------------------------------------------------
def check_content(mod_dir: str, gd=None) -> dict:
    """mod_dir の database.arz・Text_*.arc・Items.arc を読み、{"equipment", "blocking", "warnings", "records"} を返す。

    blocking: 本ツールが作る範囲の外を書き換えるもの・壊れたもの・同じ名前の重複（導入させない）。
    warnings: 本ツールが作る範囲の中で、本ツールが作る値（templateName・Class・レコード表の種類）や、この PC の公式の
    データから本ツールが作る内容と違う項目（ゲームの版の違いか、作った人による書き換え）。レコードごとに 1 件で、違う
    項目を 1 行ずつ並べる（2026-10-17 監査 C14。利用者の判断で、止めずに項目ごとの違いの一覧ではっきり注意し、利用者が
    確かめてから導入する）。gd（modbuilder の GameData）が無ければ公式のデータとの比較はせず、その旨を注意に出す。"""
    from modbuilder.arc import ArcFile
    from modbuilder.arz import T_STRING, ArzFile, norm_path, split_array
    from modbuilder.gamedata import parse_tags
    from modbuilder.install import BASEMODS_FILES
    from modbuilder.icons import icon_files
    from modbuilder.invisible_look import look_msh, look_tex
    from modbuilder.naming import (HEAD_INVISIBLE_F, HEAD_INVISIBLE_M, HEAD_TEXTURE_FIELDS, ICON_HEAD, ICON_SET,
                                   INVISIBLE_ICON, INVISIBLE_LOOK_FIELDS, INVISIBLE_LOOK_RECORDS, LEGACY_LOOK_FIELDS,
                                   INVISIBLE_MEDAL_RECORD, INVISIBLE_RECORDS, INVISIBLE_SLOTS, ITEM_COST,
                                   LEGACY_BITMAPS, MERCHANT_RECORDS, OFFICIAL_INVISIBLE_MEDAL, OFFICIAL_INVISIBLE_SHOULDERS)
    blocking: list[str] = []
    warnings: list[str] = []
    present = sorted(os.listdir(mod_dir))
    for n in present:
        if n not in BASEMODS_FILES:
            blocking.append(tr(f"想定外のファイル: {n}", f"Unexpected file: {n}"))
    for n in ("database.arz", "Text_EN.arc"):
        if n not in present:
            blocking.append(tr(f"{n} がありません", f"{n} is missing"))
    if blocking:
        return {"equipment": [], "blocking": blocking, "warnings": warnings, "records": 0}
    try:
        arz = ArzFile(os.path.join(mod_dir, "database.arz"), max_bytes=MAX_DATABASE_FILE, max_records=MAX_RECORDS)
        if not arz.trailer_ok():
            blocking.append(tr("database.arz の検査値（末尾の Adler32）が合いません（壊れています）",
                               "The checksum of database.arz (the Adler32 at the end) does not match (the file is corrupted)"))
        names = arz.names()
        # 展開する前に、レコード表の展開後の大きさで止める（細工した小さなファイルで GB 単位の展開をさせない。R05B-01）
        big = sorted(r.name for r in arz.records if r.dsize > MAX_RECORD_SIZE)
        total = sum(r.dsize for r in arz.records)
        if big or total > MAX_DATABASE_SIZE:
            return {"equipment": [], "blocking": [tr(
                f"database.arz のレコードが大きすぎます（展開後の合計 {total:,} B。上限は 1 件 {MAX_RECORD_SIZE:,} B・合計 "
                f"{MAX_DATABASE_SIZE:,} B で、このアプリはこれを超えるものを作りません）: {big[:5]}",
                f"The records in database.arz are too large ({total:,} B in total when extracted; the limits are "
                f"{MAX_RECORD_SIZE:,} B per record and {MAX_DATABASE_SIZE:,} B in total, and this app never creates "
                f"anything larger): {big[:5]}")], "warnings": warnings,
                "records": 0}
        # 装備の数（レコード表の名前から。展開する前に分かる）で、全レコードの項目の数の予算を決める（点検 D2_backend-02）
        n_eq = len({m.group(1) for m in (HEAD_RE.match(x) or SET_RE.match(x) for x in names) if m})
        if n_eq > MAX_EQUIPMENT:
            return {"equipment": [], "blocking": [tr(
                f"装備が多すぎます（{n_eq:,} 件。このアプリの検査の上限は {MAX_EQUIPMENT:,} 件で、このアプリはこれを超える MOD の"
                f"共有ファイルを作りません）",
                f"There is too much equipment ({n_eq:,}; this app can check up to {MAX_EQUIPMENT:,} and never creates "
                f"MOD share files with more)")], "warnings": warnings, "records": 0}
        budget = field_budget(n_eq)
        # 全レコードを先に展開する（壊れたレコードはここで ValueError。点検 C79）。項目の数はレコード 1 件と、全レコードの
        # 合計（予算）で上限を掛ける（点検 R05B-02）。予算を超えたら、どのレコードかではなく合計と予算を示す（ほかの正規の
        # レコードを「形式が不正」と名指ししない。点検 D2_backend-02）
        used = 0
        for r in arz.records:
            used += len(arz.decode(r, max_size=MAX_RECORD_SIZE, max_fields=MAX_RECORD_FIELDS))
            if used > budget:
                return {"equipment": [], "blocking": [tr(
                    f"database.arz の項目が多すぎます（{used:,} 個以上。このアプリの検査の上限は装備 {n_eq:,} 件で {budget:,} 個で、"
                    f"このアプリが作る MOD は装備 1 件あたり約 1,400 個です）",
                    f"database.arz has too many fields ({used:,} or more; this app can check up to {budget:,} for "
                    f"{n_eq:,} equipment, and the MODs it builds have about 1,400 per equipment)")],
                    "warnings": warnings, "records": 0}
    except Exception as e:   # noqa: BLE001
        return {"equipment": [], "blocking": [tr(f"database.arz を読めません: {e}", f"Cannot read database.arz: {e}")],
                "warnings": warnings, "records": 0}
    # 同じ名前のレコードが複数あると、検査で見るのは片方だけになる（本ツールのビルドは重複を作らない。2026-10-17 監査 C94）
    dups = sorted(k for k, c in Counter(norm_path(x) for x in names).items() if c > 1)
    if dups:
        blocking.append(tr(f"database.arz に同じ名前のレコードが複数あります（{len(dups)} 件）: {dups[:5]}",
                           f"database.arz has multiple records with the same name ({len(dups)}): {dups[:5]}"))
    # 項目の重複・名前に使えない文字・文字列の値の改行や制御文字。どれも本ツールのビルドは作らず、検査が読む値とゲームが
    # 読む値を食い違わせうる（重複はゲームがどちらを使うか分からない）ので導入させない（2026-10-20 レビュー R12A-01）
    odd = []
    for r in arz.records:
        fields = arz.decode(r)
        same = sorted(k for k, c in Counter(fname.lower() for fname, _t, _v in fields).items() if c > 1)
        if same:
            odd.append(tr(f"{r.name}: 同じ名前の項目 {same[:3]}", f"{r.name}: fields with the same name {same[:3]}"))
        badn = [fname for fname, _t, _v in fields if not FIELD_NAME_RE.match(fname)]
        if badn:
            odd.append(tr(f"{r.name}: 名前に使えない文字がある項目 {[_short(x, 40) for x in badn[:3]]}",
                          f"{r.name}: fields whose names contain characters that are not allowed "
                          f"{[_short(x, 40) for x in badn[:3]]}"))
        badv = [fname for fname, ftype, vals in fields if ftype == T_STRING and any(BAD_VALUE_RE.search(v) for v in vals)]
        if badv:
            odd.append(tr(f"{r.name}: 値に改行・制御文字がある項目 {[_short(x, 40) for x in badv[:3]]}",
                          f"{r.name}: fields whose values contain line breaks or control characters "
                          f"{[_short(x, 40) for x in badv[:3]]}"))
    if odd:
        blocking.append(tr(f"database.arz にこのアプリが作らない形の項目があります（検査を欺くおそれがあります。{len(odd)} 件）: "
                           f"{odd[:5]}",
                           f"database.arz has fields in a form this app never creates (they could deceive the check; "
                           f"{len(odd)}): {odd[:5]}"))
    tags: dict[str, str] = {}
    for n in present:
        if not n.endswith(".arc"):
            continue
        try:
            arc = ArcFile(os.path.join(mod_dir, n))
            entries = arc.names()
        except Exception as e:   # noqa: BLE001
            blocking.append(tr(f"{n} を読めません: {e}", f"Cannot read {n}: {e}"))
            continue
        # 同じ名前の項目・空き項目（本ツールの .arc は毎回新しく作るので、どちらも無い。C94）
        adups = sorted(k for k, c in Counter(norm_path(x) for x in arc.raw_names).items() if c > 1)
        if adups:
            blocking.append(tr(f"{n} に同じ名前の項目が複数あります（{len(adups)} 件）: {adups[:5]}",
                               f"{n} has multiple entries with the same name ({len(adups)}): {adups[:5]}"))
        if arc.empty_count:
            blocking.append(tr(f"{n} に空き項目（名前の無い項目）が {arc.empty_count} 件あります（このアプリは作りません）",
                               f"{n} has empty entries without a name ({arc.empty_count}; this app never "
                               "creates them)"))
        if n.startswith("Text_"):
            for e in entries:
                if not os.path.basename(e.lower()).startswith("tags_aiskin") or not e.lower().endswith(".txt"):
                    blocking.append(tr(f"{n}: このアプリのタグファイル以外が入っています: {e}",
                                       f"{n}: contains a file other than this app's tag files: {e}"))
                    continue
                # 読む前に展開後の大きさで止める。パートの展開後の大きさの合計がファイルの大きさと合わないもの（同じ圧縮の
                # かたまりを複数のパートから指して増やすもの）も読まない（R05B-01）
                ent = arc.entries[e]
                direct = ent.kind == 1 and ent.csize == ent.size        # ArcFile.read がパートを使わずに読む形（C77）
                parts = arc.parts[ent.first_part:ent.first_part + ent.part_count]
                if ent.size > MAX_TAGS_FILE:
                    blocking.append(tr(f"{n} を読めません: {e}: タグファイルが大きすぎます（{ent.size:,} B > 上限 {MAX_TAGS_FILE:,} B）",
                                       f"Cannot read {n}: {e}: the tag file is too large ({ent.size:,} B > limit "
                                       f"{MAX_TAGS_FILE:,} B)"))
                    continue
                if not direct and sum(u for _o, _c, u in parts) != ent.size:
                    blocking.append(tr(f"{n} を読めません: {e}: パートの大きさの合計がファイルの大きさと合いません（壊れています）",
                                       f"Cannot read {n}: {e}: the total size of the parts does not match the file size "
                                       "(the file is corrupted)"))
                    continue
                try:
                    t = parse_tags(arc.read(e))
                except (KeyError, ValueError, OSError) as ex:     # 壊れた・読めない形のエントリ（点検 C77・C79）
                    blocking.append(tr(f"{n} を読めません: {e}: {ex}", f"Cannot read {n}: {e}: {ex}"))
                    continue
                bad = sorted(k for k in t if not TAG_RE.match(k))
                if bad:
                    blocking.append(tr(f"{n}: このアプリのタグ以外（公式の表示文などを上書きするもの）があります: {bad[:5]}",
                                       f"{n}: contains tags other than this app's (ones that override official texts, etc.): "
                                       f"{bad[:5]}"))
                if n == "Text_EN.arc":
                    tags.update(t)
        else:        # Items.arc
            bad = [e for e in entries if not (ITEMS_RE.match(norm_path(e)) or ICON_ENTRY_RE.match(norm_path(e))
                                              or LOOK_ENTRY_RE.match(norm_path(e)))]
            # このアプリの場所（aiskin/）の中の知らないファイルは公式のファイルを置き換えない。新しい版のアプリが足した
            # ファイルか書き換えなので、理由を分けて止める（どちらも導入させない。点検 backend_modpkg-03）
            ours_unknown = [e for e in bad if NAMESPACE_ENTRY_RE.match(norm_path(e))]
            bad = [e for e in bad if not NAMESPACE_ENTRY_RE.match(norm_path(e))]
            if ours_unknown:
                blocking.append(tr(f"{n}: このアプリのこの版が作らないファイルがあります（新しい版のアプリで作った MOD か、"
                                   f"書き換えたものです。アプリを更新してから読み込み直してください）: {ours_unknown[:5]}",
                                   f"{n}: contains files that this version of the app does not create (a MOD made with a "
                                   f"newer version of the app, or a changed one; update the app and load it again): "
                                   f"{ours_unknown[:5]}"))
            if bad:
                blocking.append(tr(f"{n}: このアプリのファイル（aiskin/<ID>/・aiskin/icons/・aiskin/invisible/）以外のファイルが"
                                   f"あります（公式のモデル・テクスチャを置き換えるもの）: {bad[:5]}",
                                   f"{n}: contains files other than this app's files (aiskin/<ID>/, aiskin/icons/ and "
                                   f"aiskin/invisible/) (ones that replace official models or textures): {bad[:5]}"))
            # MOD のアイコン（2026-11-11 から）: このアプリが描くものとバイト単位で比べる。違っても見た目だけなので止めない
            ours = icon_files()
            for e in entries:
                if not ICON_ENTRY_RE.match(norm_path(e)):
                    continue
                try:
                    data = arc.read(e, max_size=ICON_MAX)
                except Exception as ex:   # noqa: BLE001
                    blocking.append(tr(f"{n}: MOD のアイコン {e} を読めません: {ex}", f"{n}: cannot read the MOD icon {e}: {ex}"))
                    continue
                if ours.get("items/" + norm_path(e)) != data:
                    warnings.append(tr(f"{n}: MOD のアイコン {e} が、このアプリの版のものと違います（作った人のアプリの版が違うか、"
                                       "書き換えたものです。見た目のアイコンだけの違いです）",
                                       f"{n}: the MOD icon {e} differs from this app version's icon (the creator used a "
                                       "different app version, or it was changed; only the icon looks different)"))
            # 自前の透明の肩・メダルの見た目（2026-11-11 D-079）: 画像はこのアプリが描くもの、メッシュはこの PC の公式の
            # 透明メッシュから作るものとバイト単位で比べる（公式のデータが無ければメッシュは比べない）。違えば注意（見た目が
            # 透明でなくなるおそれがあるので、利用者が確かめてから導入する）
            for e in entries:
                if not LOOK_ENTRY_RE.match(norm_path(e)):
                    continue
                try:
                    data = arc.read(e, max_size=LOOK_MAX)
                except Exception as ex:   # noqa: BLE001
                    blocking.append(tr(f"{n}: 透明の見た目のファイル {e} を読めません: {ex}",
                                       f"{n}: cannot read the invisible look file {e}: {ex}"))
                    continue
                try:
                    if norm_path(e).endswith(".tex"):
                        expected = look_tex()
                    else:
                        expected = look_msh(gd) if gd is not None else None
                except Exception:  # noqa: BLE001  この PC の公式の透明メッシュが読めない・形が違う
                    expected = None
                if expected is not None and expected != data:
                    warnings.append(tr(f"{n}: 透明の見た目のファイル {e} が、このアプリの作るものと違います（作った人のアプリ・ゲームの"
                                       "版が違うか、書き換えたものです。透明の肩・メダルが見えてしまうおそれがあります）",
                                       f"{n}: the invisible look file {e} differs from the one this app creates (a different app "
                                       "or game version, or it was changed; the invisible shoulders and medal may become "
                                       "visible)"))
    heads, sets = {}, {}
    allowed = set(INVISIBLE_RECORDS) | set(MERCHANT_RECORDS)
    others = []
    for n in names:
        mh, ms = HEAD_RE.match(n), SET_RE.match(n)
        if mh:
            heads[mh.group(1)] = n
        elif ms:
            sets[ms.group(1)] = n
        elif n not in allowed:
            others.append(n)
    if others:      # 1 件ずつ足さず、件数と先頭 5 件の 1 行にまとめる（dups・odd と同じ形。点検 R05B-01）
        blocking.append(tr(f"database.arz: このアプリが作るもの以外のレコードがあります（{len(others)} 件）: "
                           f"{[x[:80] for x in others[:5]]}",
                           f"database.arz: contains records that this app does not create ({len(others)}): "
                           f"{[x[:80] for x in others[:5]]}"))
    equipment = []
    for eq_id, rec in sorted(heads.items()):
        f = dict(arz.record_pairs(rec) or [])
        tag = f.get("itemNameTag", "")
        equipment.append({"id": eq_id, "record": rec, "name": tags.get(tag, tag), "has_set": eq_id in sets})
    set_records = set(sets.values())

    # --- 本ツールが作る値（templateName・Class・レコード表の種類。公式のデータが無くても確かめられる。C14） ---------------
    fixed = [(rec, HEAD_TEMPLATE) for rec in sorted(heads.values())] + [(rec, SET_TEMPLATE) for rec in sorted(sets.values())] \
        + [(s[3], ("database/templates/" + s[1], s[2])) for s in INVISIBLE_SLOTS] \
        + [(rec, (MERCHANT_TEMPLATE, None)) for rec in MERCHANT_RECORDS]
    for rel, (tpl, cls) in fixed:
        got = arz.record_pairs(rel)
        if got is None:
            continue
        f = dict(got)
        lines = []
        if norm_path(f.get("templateName", "")) != tpl:
            lines.append(tr(f"templateName: {_short(f.get('templateName'))}（このアプリの値 {tpl}）",
                            f"templateName: {_short(f.get('templateName'))} (this app's value {tpl})"))
        if cls is not None and f.get("Class") != cls:
            lines.append(tr(f"Class: {_short(f.get('Class'))}（このアプリの値 {cls}）",
                            f"Class: {_short(f.get('Class'))} (this app's value {cls})"))
        rtype = arz.get(rel).rtype
        if rtype != f.get("Class", ""):          # compile_records はレコードの Class をレコード表に書く（無ければ空）
            lines.append(tr(f"レコード表の種類（Class）: {_short(rtype)}（レコードの Class は {_short(f.get('Class', ''))}）",
                            f"Record table type (Class): {_short(rtype)} (the record's Class is {_short(f.get('Class', ''))})"))
        if lines:
            warnings.append(_diff_warning(tr(f"{rel}: このアプリが作る値と違います（作った人による書き換えか、壊れたファイルです。"
                                             "別の種類のアイテムとして扱われるおそれがあります）",
                                             f"{rel}: differs from the values this app creates (changed by the creator, or a "
                                             "corrupted file; the item may be treated as a different kind of item)"), lines))

    # --- 補給官: 公式の在庫に、このファイルの消耗品を足しただけか -------------------------------------------------------------
    # 公式の在庫に無い品を売らせるものは導入させない。公式の在庫の欠落・並べ替え・ほかの項目の違いは、項目ごとに注意する
    if gd is None:
        warnings.append(tr("この PC のゲームデータを読めないため、レコードの中身（補給官の在庫・消耗品の項目など）を公式の"
                           "データと比べていません。作った人による書き換えがあっても分かりません",
                           "The game data on this PC cannot be read, so the record contents (the Quartermaster's stock, the "
                           "consumable's fields, etc.) were not compared with the official data. Changes made by the creator "
                           "would not be detected"))
    for rel in MERCHANT_RECORDS:
        f = arz.record_pairs(rel)
        if f is None:
            continue
        added = split_array(dict(f).get("marketStaticItems", ""))
        off = gd.official_record(rel) if gd is not None else None
        if off is None:
            if gd is not None:
                warnings.append(tr(f"{rel}: この PC のゲームデータに補給官のレコードが無いため、在庫を公式と比べていません"
                                   "（拡張の構成の違い）",
                                   f"{rel}: the game data on this PC has no record for this Quartermaster, so the stock was not "
                                   "compared with the official one (different expansions)"))
            continue
        official = split_array(dict(off[1]).get("marketStaticItems", ""))
        extra = [x for x in added if x not in set(official) and x not in set_records]
        if extra:
            blocking.append(tr(f"{rel.rsplit('/', 1)[-1]}: 補給官の在庫にこのアプリの消耗品以外が足されています: {extra[:5]}",
                               f"{rel.rsplit('/', 1)[-1]}: items other than this app's consumables are added to the "
                               f"Quartermaster's stock: {extra[:5]}"))
        lines = _merchant_stock_lines(added, official, set_records)
        lines += _field_lines(dict(off[1]), dict(f), MERCHANT_CHANGED | {"templateName"})
        if lines:
            warnings.append(_diff_warning(tr(f"{rel}: 補給官のレコードが、この PC の公式のデータにこのアプリの消耗品を足したものと"
                                             "違います（ゲームの版の違いか、作った人による書き換えです）",
                                             f"{rel}: the Quartermaster record differs from the official data on this PC with "
                                             "this app's consumables added (a different game version, or changes made by the "
                                             "creator)"), lines))
    if gd is not None:
        warnings += _compare_with_official(arz, gd, heads, sets)
    for eq_id, rec in heads.items():
        f = dict(arz.record_pairs(rec) or [])
        for k in ("armorMaleMesh", "armorFemaleMesh", "mesh", *HEAD_TEXTURE_FIELDS):
            v = norm_path(f.get(k, "") or "")
            if v and not v.startswith(f"items/aiskin/{eq_id}/"):
                warnings.append(tr(f"{eq_id}: {k} がこのアプリの装備の外を指しています: {v}",
                                   f"{eq_id}: {k} points outside this app's equipment: {v}"))
        # アイコンは今の MOD のアイコンか、以前の版が使った公式のアイコン（2026-11-11 まで）
        if f.get("headMaleMesh", "").lower() != HEAD_INVISIBLE_M or f.get("headFemaleMesh", "").lower() != HEAD_INVISIBLE_F \
                or norm_path(f.get("bitmap", "")) not in (ICON_HEAD, LEGACY_BITMAPS["head"]):
            warnings.append(tr(f"{eq_id}: 頭装備レコードの項目がこのアプリの値と違います",
                               f"{eq_id}: fields of the head armor record differ from this app's values"))
    for eq_id, rec in sets.items():
        f = dict(arz.record_pairs(rec) or [])
        dbrs = split_array(f.get("transmuteDbrs", ""))
        # 自前の透明の部位と、以前の版が入れた公式の透明の肩・メダル（メダルは 2026-10-17 から。C125。2026-11-11 から
        # 肩・メダルも自前のレコード。D-078）は本ツールが入れるもの
        outside = [d for d in dbrs if d not in heads.values() and d not in INVISIBLE_RECORDS
                   and d not in (OFFICIAL_INVISIBLE_SHOULDERS, OFFICIAL_INVISIBLE_MEDAL)]
        if outside:
            warnings.append(tr(f"{eq_id}: イリュージョンの消耗品にこのアプリの装備以外が入っています（作った人による書き換えです）: "
                               f"{outside[:3]}",
                               f"{eq_id}: the illusion consumable contains items other than this app's equipment (changed by "
                               f"the creator): {outside[:3]}"))
        if f.get("itemCost") != ITEM_COST:
            warnings.append(tr(f"{eq_id}: 消耗品の値段がこのアプリの値（{ITEM_COST}）と違います: {f.get('itemCost')}",
                               f"{eq_id}: the consumable's price differs from this app's value ({ITEM_COST}): "
                               f"{f.get('itemCost')}"))
        if norm_path(f.get("fullBitmap", "") or "") not in (ICON_SET, LEGACY_BITMAPS["set"]):
            warnings.append(tr(f"{eq_id}: 消耗品のアイコンがこのアプリの値と違います: {_short(f.get('fullBitmap'))}",
                               f"{eq_id}: the consumable's icon differs from this app's value: {_short(f.get('fullBitmap'))}"))
    for rel in INVISIBLE_RECORDS:
        got = arz.record_pairs(rel)
        if got is None:         # 以前の版の MOD には肩・メダルの自前のレコードが無い（公式のものを使っていた）
            continue
        v = norm_path(dict(got).get("bitmap", "") or "")
        legacy = LEGACY_BITMAPS["medal" if rel == INVISIBLE_MEDAL_RECORD else "invisible"]
        if v not in (INVISIBLE_ICON[rel], legacy):
            warnings.append(tr(f"{rel}: 透明の部位のアイコンがこのアプリの値と違います: {_short(v)}",
                               f"{rel}: the icon of the invisible part differs from this app's value: {_short(v)}"))
        if rel in INVISIBLE_LOOK_RECORDS:
            # 肩・メダルの見た目のファイル: 今の自前のもの（D-079）か、以前の版の公式の透明のもの
            look = dict(got)
            odd = [k for k in INVISIBLE_LOOK_FIELDS
                   if norm_path(look.get(k, "") or "") not in (INVISIBLE_LOOK_FIELDS[k], LEGACY_LOOK_FIELDS[k])]
            if odd:
                warnings.append(tr(f"{rel}: 透明の部位の見た目のファイルがこのアプリの値と違います: "
                                   f"{', '.join(f'{k}={_short(look.get(k))}' for k in odd)}",
                                   f"{rel}: the invisible look files (mesh and texture) differ from this app's values: "
                                   f"{', '.join(f'{k}={_short(look.get(k))}' for k in odd)}"))
    return {"equipment": equipment, "blocking": blocking, "warnings": warnings[:50], "records": len(names)}


# 本ツールが作るレコードの templateName・Class（ゲームの版によらない。透明の部位は naming.INVISIBLE_SLOTS）。補給官の
# レコードは公式に Class が無い（2026-10-17 にこの PC のゲームデータで確認）ので、Class は公式のレコードと比べる
HEAD_TEMPLATE = ("database/templates/armor_head.tpl", "ArmorProtective_Head")
SET_TEMPLATE = ("database/templates/itemtransmuterset.tpl", "ItemTransmuterSet")
MERCHANT_TEMPLATE = "database/templates/factiontier.tpl"
# 本ツールが雛形（公式レコード）から変える項目
HEAD_CHANGED = {"armorMaleMesh", "mesh", "armorFemaleMesh", "headMaleMesh", "headFemaleMesh", "bitmap", "castsShadows",
                "itemNameTag", "itemLevel", "levelRequirement", "armorMaleBaseTexture", "armorMaleBumpTexture",
                "armorFemaleBaseTexture", "armorFemaleBumpTexture"}
# fullBitmap・透明の部位の bitmap は 2026-11-11 から MOD のアイコン（値は check_content が今と以前の版の値で確かめる）
SET_CHANGED = {"transmuteDbrs", "transmuteTag", "description", "itemText", "itemCost", "fullBitmap"}
INVISIBLE_CHANGED = {"templateName", "Class", "itemNameTag", "bitmap"}
MERCHANT_CHANGED = {"marketStaticItems"}
DIFF_LINES = 15             # 注意 1 件に並べる違いの行の数（残りは「ほか N 項目」）


def _cap_lines(lines: list[str], n: int = MAX_BLOCKING) -> list[str]:
    """確認の結果・記録に入れる行を先頭 n 件と「ほか M 件」にする（細工した MOD で数百万行を記録に書かない。R05B-01）。"""
    if len(lines) <= n:
        return lines
    return lines[:n] + [tr(f"ほか {len(lines) - n} 件", f"and {len(lines) - n} more")]


def _short(v, n: int = 70) -> str:
    if v is None:
        return tr("（なし）", "(none)")
    s = str(v)
    return repr(s) if len(s) <= n else repr(s[:n]) + "…"


def _diff_warning(head: str, lines: list[str]) -> str:
    """注意 1 件（1 行目が要約、2 行目からが項目ごとの違い）。"""
    shown = lines[:DIFF_LINES]
    more = len(lines) - len(shown)
    indent = tr("　", "  ")              # 項目ごとの行の字下げ（日本語は全角の空白）
    return "\n".join([head] + [indent + x for x in shown]
                     + ([tr(f"　…ほか {more} 項目", f"  …and {more} more")] if more > 0 else []))


def _field_lines(official: dict, got: dict, skip: set) -> list[str]:
    """項目ごとの違い（公式の値 → この MOD の値）。skip の項目は比べない。"""
    keys = sorted(k for k in set(official) | set(got) if k not in skip and official.get(k) != got.get(k))
    return [tr(f"{k}: 公式 {_short(official.get(k))} → この MOD {_short(got.get(k))}",
               f"{k}: official {_short(official.get(k))} → this MOD {_short(got.get(k))}") for k in keys]


def _merchant_stock_lines(added: list[str], official: list[str], set_records: set) -> list[str]:
    """補給官の marketStaticItems の違い。本ツールが作る形は「公式の在庫をそのままの順で先頭に、末尾にこの MOD の
    消耗品だけ（重複なし）」（modbuilder.records）。公式の在庫に無い品の追加は check_content が止める理由にする。"""
    lines = []
    off_set = set(official)
    missing = [x for x in dict.fromkeys(official) if x not in added]
    if missing:
        lines.append(tr(f"公式の在庫から消えている品 {len(missing)} 件: {missing[:5]}",
                        f"Items missing from the official stock ({len(missing)}): {missing[:5]}"))
    head_ok = added[:len(official)] == official
    if not head_ok and not missing:
        lines.append(tr("公式の在庫の並びが変わっています（このアプリは公式の在庫の後ろに消耗品を足します）",
                        "The order of the official stock has changed (this app adds the consumables after the official stock)"))
    if head_ok:
        again = [x for x in added[len(official):] if x in off_set]
        if again:
            lines.append(tr(f"公式の在庫の品が重ねて足されています: {again[:5]}",
                            f"Items of the official stock are added again: {again[:5]}"))
    dup = sorted(x for x, c in Counter(x for x in added if x in set_records).items() if c > 1)
    if dup:
        lines.append(tr(f"この MOD の消耗品が重複しています: {dup[:5]}", f"This MOD's consumables appear more than once: {dup[:5]}"))
    absent = sorted(x for x in set_records if x not in added)
    if absent:
        lines.append(tr(f"この MOD の消耗品のうち、この補給官の在庫に無いもの {len(absent)} 件: {absent[:5]}",
                        f"This MOD's consumables that are not in this Quartermaster's stock ({len(absent)}): {absent[:5]}"))
    return lines


def _compare_with_official(arz, gd, heads: dict, sets: dict) -> list[str]:
    """頭装備・消耗品・透明の部位を、この PC の公式の雛形から本ツールが作る内容と項目ごとに比べる（補給官は check_content）。
    消耗品は SET_CHANGED 以外の全ての項目（文字列の itemSkillName なども含む）を比べる。templateName・Class は本ツールが
    作る値と別に比べているので、ここでは除く。"""
    from modbuilder.naming import (INVISIBLE_LOOK_FIELDS, INVISIBLE_LOOK_RECORDS, INVISIBLE_RECORDS, INVISIBLE_SOURCE,
                                   TEMPLATE_HEAD, TEMPLATE_SET)
    out = []
    fixed = {"templateName", "Class"}
    set_recs = set(sets.values())
    # 透明の部位は、それぞれの雛形（肩の透明。メダルはメダルの透明。2026-11-11 から）と比べる。肩・メダルの見た目の
    # ファイルの項目（D-079）は、値を check_content が今と以前の版の値で確かめる
    groups = [(rec, TEMPLATE_HEAD, HEAD_CHANGED) for rec in sorted(heads.values())] \
        + [(rec, TEMPLATE_SET, SET_CHANGED) for rec in sorted(set_recs)] \
        + [(rec, INVISIBLE_SOURCE[rec],
            INVISIBLE_CHANGED | (set(INVISIBLE_LOOK_FIELDS) if rec in INVISIBLE_LOOK_RECORDS else set()))
           for rec in INVISIBLE_RECORDS]
    for rec, tpl, changed in groups:
        got = arz.record_pairs(rec)
        if got is None:
            continue
        off = gd.official_record(tpl)
        if off is None:
            out.append(tr(f"{rec}: この PC のゲームデータに雛形 {tpl} がありません（拡張の構成の違い）。中身を公式と比べていません",
                          f"{rec}: the game data on this PC has no template {tpl} (different expansions). The contents were "
                          "not compared with the official data"))
            continue
        lines = _field_lines(dict(off[1]), dict(got), changed | fixed)
        if lines:
            kind = tr("イリュージョンの消耗品（補給官で売られる品）", "the illusion consumable (the item sold by the Quartermaster)") \
                if rec in set_recs else tr("レコード", "the record")
            out.append(_diff_warning(tr(f"{rec}: {kind}に、この PC の公式の雛形 {tpl} からこのアプリが作る内容と違う項目が "
                                        f"{len(lines)} 個あります（ゲームの版の違いか、作った人による書き換えです）",
                                        f"{rec}: {len(lines)} field(s) of {kind} differ from what this app creates from the "
                                        f"official template {tpl} on this PC (a different game version, or changes made by the "
                                        "creator)"), lines))
    return out


# ---------------------------------------------------------------------------
# 受け取り（展開と検査。導入は modops）
# ---------------------------------------------------------------------------
def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _gamedata(svc):
    from modbuilder.gamedata import GameData
    from .settings import resolve_game_dir
    gd_dir = resolve_game_dir(svc.settings.get())
    if not gd_dir:
        return None, None
    try:
        return GameData(gd_dir), gd_dir
    except Exception:   # noqa: BLE001
        return None, gd_dir


def _as_dict(v) -> dict:
    """記録ファイルの項目が辞書でなければ空の辞書として扱う（files 以外の項目は package_id に含まれず、ハッシュを
    作り直さなくても書き換えられるため。型の違いで想定外のエラーにしない。2026-10-17 監査 C96 のレビュー FG07-R1）。"""
    return v if isinstance(v, dict) else {}


def _as_str(v) -> str | None:
    """記録ファイルの表示用の文字列の項目（build の build_id・created_at・profile など）。文字列でなければ None。"""
    return v if isinstance(v, str) else None


def _languages(v, files) -> list[str] | None:
    """表示名の言語（Text_XX の XX の一覧。無ければ None のまま）。記録の値が対応する言語の文字列の配列でなければ、MOD の
    ファイルの一覧の Text_XX.arc から決める（数値・真偽値を導入の計画・導入が反復して TypeError で止まり、文字列 "JA" は
    1 文字ずつに分かれた。2026-10-27 検証 R12B-02）。"""
    from modbuilder.records import LANGUAGES
    if v is None:
        return None
    if isinstance(v, list) and v and all(isinstance(x, str) and x.upper() in LANGUAGES for x in v):
        return [x.upper() for x in v]
    return sorted(m.group(1) for n in (files or {}) if (m := TEXT_ARC_RE.fullmatch(str(n))) and m.group(1) in LANGUAGES)


def valid_share_id(share_id) -> bool:
    """受け取った MOD の ID（package_id。16 進 16 桁）の形か。received\\ の下のフォルダー名に使うので、形の違う値は使わない。"""
    return isinstance(share_id, str) and bool(package_io.PACKAGE_ID_RE.fullmatch(share_id))


def inspect(svc, path: str) -> dict:
    """受け取った共有ファイルを検査し、``shared\\received\\<package_id>\\`` に展開する（mods\\ は変更しない）。

    記録ファイル（gdskin_mod.json）の mod_files の値が辞書でなければ PACKAGE_INVALID（broken）。build・tool・game
    （と game の databases）・equipment は表示用の情報なので、型が違えば無いものとして扱う（検査の結果は変えない）。"""
    from modbuilder.builder import MANIFEST
    from modbuilder.install import BASEMODS_FILES
    from modbuilder.library import now_iso, sha256_file
    with package_io.open_package(path, KIND) as pkg:
        m = pkg.manifest
        mod_files = m.get("mod_files")
        if not isinstance(mod_files, dict) or not mod_files:
            raise package_io._err("broken", tr("MOD のファイルの一覧がありません", "The list of MOD files is missing"))
        if not all(isinstance(rec, dict) for rec in mod_files.values()):
            raise package_io._err("broken", tr("MOD のファイルの一覧の形式が不正です", "The list of MOD files has an invalid format"))
        for n, rec in mod_files.items():
            if n not in BASEMODS_FILES or pkg.files.get("mod/" + n) != {"sha256": rec.get("sha256"), "size": rec.get("size")}:
                raise package_io._err("tampered", tr(f"MOD のファイルの一覧が中身と合いません: {n}",
                                                     f"The list of MOD files does not match the contents: {n}"))
        extra = [n for n in pkg.files if n != "README.txt" and not (n.startswith("mod/") and n[4:] in mod_files)]
        if extra:
            raise package_io._err("tampered", tr(f"想定外のファイルがあります: {extra[:3]}", f"Unexpected files: {extra[:3]}"))
        if "database.arz" not in mod_files:
            raise package_io._err("broken", tr("database.arz がありません", "database.arz is missing"))
        share_id = pkg.package_id
        root = received_root(svc)
        dest = os.path.join(root, share_id)
        # 消して置き換えるフォルダーは received\ の直下だけ（package_id の照合・形の確認と二重の守り。M3M-01）
        if not valid_share_id(share_id) or os.path.dirname(os.path.abspath(dest)) != os.path.abspath(root):
            raise package_io._err("tampered", tr("記録ファイルの package_id の形式が不正です",
                                                 "The package_id in the record file has an invalid format"))
        ok = False
        if os.path.isfile(os.path.join(dest, MANIFEST)):
            ok = all(os.path.isfile(os.path.join(dest, "mod", n)) and sha256_file(os.path.join(dest, "mod", n)) == r["sha256"]
                     for n, r in mod_files.items())
        if not ok:
            tmp = package_io.scratch_dir(svc.home(), "received")
            try:
                for n in mod_files:
                    pkg.extract("mod/" + n, os.path.join(tmp, "mod", n))
                os.makedirs(root, exist_ok=True)
                if os.path.isdir(dest):
                    shutil.rmtree(dest)
                shutil.move(tmp, dest)
            finally:
                package_io.remove_tree(tmp)
    gd, gd_dir = _gamedata(svc)
    content = check_content(os.path.join(dest, "mod"), gd)
    content["blocking"] = _cap_lines(content["blocking"])
    eq_listed = m.get("equipment")
    listed = [e for e in eq_listed if isinstance(e, dict)] if isinstance(eq_listed, list) else []
    ids_listed, ids_found = sorted(str(e.get("id")) for e in listed), sorted(e["id"] for e in content["equipment"])
    if ids_listed != ids_found:
        content["warnings"].insert(0, tr(f"記録の装備の一覧（{len(ids_listed)} 件）と database.arz の装備（{len(ids_found)} 件）が違います",
                                         f"The equipment list in the record ({len(ids_listed)}) differs from the equipment in "
                                         f"database.arz ({len(ids_found)})"))
    theirs, mine = _as_dict(_as_dict(m.get("game")).get("databases")), _game_databases(gd_dir)
    game = {"theirs": sorted(theirs), "mine": sorted(mine), "expansions_differ": sorted(theirs) != sorted(mine),
            "version_may_differ": any(theirs.get(k) != mine.get(k) for k in set(theirs) & set(mine)), "game_found": bool(gd_dir)}
    code = check_code(mod_files["database.arz"]["sha256"])
    mcode = mod_code(mod_files)          # 記録の値は使わず、ファイルの一覧から計算し直す
    # build・tool は表示用（package_id の対象外）。型・大きさを整えてから記録と応答に使う（2026-10-27 検証 R12B-01・R12B-02）
    b0 = _as_dict(m.get("build"))
    # build_id・created_at は確認ダイアログに出すので 1 行の短い文にする（相手が書いた改行・数 MB の文。点検 R19B-01）
    build = {**{k: (package_io.display_text(b0.get(k)) if k in ("build_id", "created_at") else _as_str(b0.get(k)))
                for k in ("build_id", "created_at", "profile", "source_time") if k in b0},
             **({"languages": _languages(b0.get("languages"), mod_files)} if "languages" in b0 else {})}
    tool = package_io.display_tool(m.get("tool"))
    shared = {"share_id": share_id, "package_id": share_id, "mod_code": mcode, "check_code": code,
              "file_name": os.path.basename(path),
              "received_at": now_iso(), "source_build_id": build.get("build_id"),
              "created_at": package_io.display_text(m.get("created_at")),
              "tool": tool, "equipment": content["equipment"], "game": game,
              "blocking": content["blocking"], "warnings": content["warnings"]}
    # modbuilder のビルドと同じ形の記録（install はこれを読んで、ファイルのハッシュを照合してから配置する）
    names = {str(e.get("id")): e for e in listed}
    _write_json(os.path.join(dest, MANIFEST), {
        "format": "gdskin-build", "version": 1, "build_id": "shared-" + share_id, "created_at": build.get("created_at"),
        "status": "ok" if not content["blocking"] else "rejected", "finished_at": now_iso(),
        "tool": {"name": "modbuilder", "version": tool.get("modbuilder")},
        "library": {"root": "", "equipment": [{"id": e["id"], "name": e["name"] or (names.get(e["id"]) or {}).get("name"),
                                                "name_ja": (names.get(e["id"]) or {}).get("name_ja")}
                                               for e in content["equipment"]]},
        "profile": build.get("profile"), "languages": build.get("languages"),
        "files": {"basemods": {n: {"size": r["size"], "sha256": r["sha256"], "path": "mod/" + n} for n, r in mod_files.items()}},
        "install": {"basemods": {n: "mod/" + n for n in mod_files}},
        "shared": shared,
    })
    if content["blocking"]:
        # 導入させない MOD のファイルは残さない（build_manifest.json は残すので、received_build は止める理由を返し続ける。
        # もう一度読み込めば展開し直して検査する。2026-10-17 監査 C98）
        package_io.remove_tree(os.path.join(dest, "mod"))
    logs.info(tr(f"MOD の共有ファイルを確認しました: package {share_id}、照合コード {mcode}（データベースのコード {code}）、"
                 f"装備 {len(content['equipment'])} 件、"
                 f"止める理由 {len(content['blocking'])} 件、注意 {len(content['warnings'])} 件",
                 f"Checked the MOD share file: package {share_id}, verification code {mcode} (database code {code}), "
                 f"{len(content['equipment'])} equipment, "
                 f"{len(content['blocking'])} reason(s) to stop, {len(content['warnings'])} warning(s)"))
    # 導入中の MOD・最新のビルドとの比較（同じなら導入は不要、見た目・表示名だけ違う、データベースが違う。機能 SF13）
    from . import modops
    try:
        mine = modops.compare_targets(svc)
    except Exception:   # noqa: BLE001  比較は表示だけ。読めなくても確認は続ける
        mine = {}
    compare = {}
    for what in ("installed", "latest_build"):
        x = mine.get(what)
        r = compare_files(x["files"], mod_files) if x else None
        compare[what] = (r | {"build_id": x.get("build_id"), "shared": bool(x.get("shared")),
                              "file_name": x.get("file_name")}) if r else None
    return {"kind": KIND, "path": os.path.abspath(path), "file_name": os.path.basename(path), "size": os.path.getsize(path),
            "share_id": share_id, "package_id": share_id, "mod_code": mcode, "check_code": code,
            "created_at": package_io.display_text(m.get("created_at")),
            "tool": tool, "build": build, "equipment": content["equipment"], "records": content["records"],
            "files": {n: {"size": r["size"], "sha256": r["sha256"]} for n, r in mod_files.items()},
            "file_codes": file_codes(mod_files), "compare": compare,
            "game": game, "blocking": content["blocking"], "warnings": content["warnings"],
            "can_install": not content["blocking"]}


def received_build(svc, share_id: str) -> dict:
    """受け取った MOD を modops のビルドの形で返す（導入の計画・導入に使う）。"""
    from modbuilder.builder import MANIFEST
    if not valid_share_id(share_id):
        raise BackendError("INVALID_INPUT", tr("受け取った MOD の ID が不正です", "The received MOD's ID is invalid"))
    d = os.path.join(received_root(svc), share_id)
    p = os.path.join(d, MANIFEST)
    if not os.path.isfile(p):
        raise BackendError("NOT_FOUND", tr("受け取った MOD がありません（もう一度読み込んでください）",
                                           "The received MOD was not found (import it again)"))
    with open(p, encoding="utf-8") as f:
        m = json.load(f)
    if m.get("status") != "ok":
        raise BackendError("INSTALL_BLOCKED", tr("この MOD は検査で導入できないと判定されました",
                                                 "The check found that this MOD cannot be installed"),
                           params={"blocking": (m.get("shared") or {}).get("blocking", [])})
    shared = dict(m.get("shared") or {})
    if "mod_code" not in shared:         # 2026-10-17 より前に受け取った MOD の記録には照合コード（MOD 全体）が無い
        shared["mod_code"] = mod_code(m["files"]["basemods"])
    return {"build_id": m["build_id"], "build_dir": d, "status": "ok", "created_at": m.get("created_at"),
            "finished_at": m.get("finished_at"), "equipment": [e.get("id") for e in m["library"]["equipment"]],
            "excluded": [], "files": m["files"]["basemods"],
            # 修正の前に受け取った MOD の記録には、型を確かめていない languages が残っている（R12B-02）
            "languages": _languages(m.get("languages"), m["files"]["basemods"]), "warnings": [], "checks": {},
            "error": None, "shared": shared}


def shared_info_for_build_dir(svc, build_dir: str | None) -> dict | None:
    """導入の記録の build_dir が受け取った MOD なら、その shared の情報。"""
    from modbuilder.builder import MANIFEST
    from .paths import is_within
    if not build_dir or not is_within(build_dir, received_root(svc)):
        return None
    try:
        with open(os.path.join(build_dir, MANIFEST), encoding="utf-8") as f:
            return json.load(f).get("shared")
    except (OSError, ValueError):
        return None
