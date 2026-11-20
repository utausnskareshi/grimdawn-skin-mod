"""GitHub に公開する前の検査（docs/publish_checklist.md 5 章）。

    python scripts/publish/check_publish.py [--list <TSV>] [--private <ファイル>] [--strict]

対象は、git に入る予定のファイル（`git ls-files --cached --others --exclude-standard`。追跡中のものと、
.gitignore で除いていない未追跡のもの）。git の外のファイル（build/・samples/ など）は見ない。
追跡中のものは索引（git add した内容。コミットに入るもの）の中身を検査し、作業フォルダーと違えば作業フォルダーの
中身も検査する（index.stale の警告。直した後は git add -A をやり直す）。未追跡のものは作業フォルダーの中身を検査する。
コミットがあれば、過去のコミットにだけある版（索引に無い中身・消したファイル・名前を変える前のパス）も検査する（push で
公開されるため）。過去のコミットは push される範囲（ブランチ・タグ・HEAD）だけを見る（stash は push されないので警告だけ）。

* 大きすぎるファイル（1 ファイル 1 MB 超は警告、10 MB 超は問題）と合計の大きさ
* 入れてはいけないファイル: ゲームのファイル（.arz・.arc・.msh・.tex・.dbr など）、3D モデル・AI の重み、
  ZIP・EXE・DLL、キャッシュ、ログ、鍵・証明書・.env、ビルド・サンプルのフォルダー
* ライセンスの分からないファイル: 画像などのバイナリー（allowlist.json に出どころを書いたものだけ認める）、
  vendor/ の下で LICENSE の無いもの、第三者の著作権表示・SPDX を含むファイル（警告。出どころの確認用）
* 個人情報・環境固有の情報: メールアドレス、PC の利用者名を含むパス、Steam の ID（SteamID64・userdata の番号）、
  この PC の名前、git の user.name（LICENSE の著作権表示の名義と同じなら対象外）・user.email、トークン・鍵らしき文字列、Claude Code の内部のセッション ID・
  一時フォルダーのパス、LAN の IP アドレス、開発環境の既定のパス（DEVPATH。警告）。表記を変えただけの形（JSON の \\uXXXX・
  HTML の文字参照・URL の %XX・全角の＠）、WSL・UNC・macOS のパスも見る。ファイル名・フォルダー名（メールアドレス・Steam の ID・
  この PC・git の設定の名前・非公開の一覧）と、画像のメタデータ（PNG の tEXt など・JPEG の Exif・XMP。テキストに埋め込んだ
  data URI の画像も）も見る
* 非公開の文字列の一覧（--private。既定は リポジトリの隣の .gdskin-publish-private.txt。リポジトリの外に置く。UTF-8 か
  BOM 付きの UTF-16）に書いた文字列（本名・メールアドレス・Steam の ID など）。この一覧は公開しない
* ツール自体のライセンス: LICENSE（MIT）の著作権表示と同じ行が README.md にあること、公開用の文書が揃っていること
* コミットと注釈つきタグがあれば、作者・コミッター・タグを付けた人のメールアドレスが GitHub の noreply のアドレスであること
  （仮の .invalid・example.com なども問題。push の前に書き換える）。名前とメッセージにはファイルと同じ規則を当てる
  （Co-Authored-By の行のアドレスなどは文書と同じ基準）

見つかったものは、行番号と、伏せ字にした一致箇所を表示する（個人情報をそのまま画面やログに出さない）。
認めるもの（出どころの確認済みなど）は scripts/publish/allowlist.json に理由とともに書く。
どの指摘にも当たらない allowlist.json の項目は警告（allow.unused）にする（コードが変わって要らなくなった許可を残さない）。

終了コード: 0 = 問題なし（警告は表示だけ。--strict では警告も問題にする）、1 = 問題あり、2 = 実行できない。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import fnmatch
import html
import json
import os
import re
import struct
import subprocess
import sys
import unicodedata
import urllib.parse
import zlib
from collections import defaultdict
from dataclasses import dataclass

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ALLOWLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowlist.json")
PRIVATE_DEFAULT = os.path.join(os.path.dirname(REPO), ".gdskin-publish-private.txt")

WARN_FILE = 1 * 1024 * 1024
MAX_FILE = 10 * 1024 * 1024
WARN_TOTAL = 50 * 1024 * 1024

# 公開用の文書（無ければ問題）
REQUIRED = ["LICENSE", "README.md", "CONTRIBUTING.md", "CHANGELOG.md", ".gitignore", ".gitattributes"]

# 入れてはいけないファイル
FORBIDDEN_EXT = {
    # Grim Dawn のファイル・MOD のビルド・セーブ・本ツールの受け渡し用ファイル
    ".arz", ".arc", ".msh", ".tex", ".dbr", ".tpl", ".anm", ".ssh", ".gst", ".gsh", ".gdc", ".gdskin", ".gdskinmod",
    # 3D モデル・AI の重み（テキストの .obj・.stl・.mtl もバイナリーの判定に当たらないので拡張子で止める）
    ".glb", ".gltf", ".ply", ".fbx", ".obj", ".stl", ".mtl", ".usdz", ".bin", ".onnx", ".pt", ".pth", ".safetensors",
    ".ckpt", ".npz", ".npy",
    # アーカイブ・実行ファイル・デバッグ情報
    ".zip", ".7z", ".rar", ".tar", ".gz", ".whl", ".nupkg", ".exe", ".dll", ".pdb", ".msi",
    # キャッシュ・ログ・作業の残り
    ".pyc", ".pyo", ".log", ".tmp", ".bak", ".orig",
    # 鍵・証明書
    ".pem", ".key", ".pfx", ".p12", ".snk",
}
# 鍵・ローカルの設定・非公開の一覧（既定の名前 .gdskin-publish-private.txt は「-publish-private」なので * から始める）。
# 鍵の名前と拡張子は配布物の検査（scripts/package/check_package.py）でも使う
SECRET_NAMES = [".env", ".env.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "credentials*.json", "*publish-private*",
                "settings.local.json"]
SECRET_EXT = {".pem", ".key", ".pfx", ".p12", ".snk"}
FORBIDDEN_NAMES = SECRET_NAMES + ["thumbs.db", "desktop.ini", ".ds_store"]
FORBIDDEN_DIRS = {"build", "samples", "bin", "obj", "__pycache__", ".pytest_cache", ".vs", ".vscode", ".idea", ".claude",
                  "node_modules", ".venv", "venv"}
# 第三者のライセンスの全文・同梱の第三者のコード（メールアドレスや著作権表示があって当然の場所）
THIRD_PARTY_DIRS = ("licenses/",)
VENDOR = re.compile(r"(^|/)vendor/([^/]+)/")

# 個人情報・環境固有の情報。境界は ASCII の英数字で判定する（\w・\b は日本語の文字も単語の文字とみなすので、
# 「連絡先はxxx@…」「既定は<ドライブ>:\Users\…」のように空白なしで日本語に続くと一致しない）
EMAIL = re.compile(r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9_])")
EMAIL_OK = re.compile(r"(@(example\.(com|org|net)|[\w.-]+\.(example|invalid|test|localhost))$)|"
                      r"(^(git@github\.com|noreply@github\.com|noreply@anthropic\.com)$)|(@users\.noreply\.github\.com$)", re.I)
# コミットの作者・コミッターに認めるアドレス（GitHub の noreply だけ。EMAIL_OK の例示用のアドレスは、文書の中では良いが、
# コミットの作者としては仮のアドレスなので push の前に書き換える。docs/publish_checklist.md 6.3 節）
COMMIT_EMAIL_OK = re.compile(r"(@users\.noreply\.github\.com$)|(^noreply@github\.com$)", re.I)
# ドライブ名・Git Bash の形（/c/）のほか、WSL の /mnt/c/ と UNC（\\<PC>\c$\Users\<名前>・\\<PC>\Users\<名前>）も見る
USERPATH = re.compile(r"(?:(?<![A-Za-z0-9_])[A-Za-z]:|(?<![A-Za-z0-9_.])(?:/mnt)?/[a-z]|\\\\[^\\/\s]+(?:\\[a-z]\$)?)"
                      r"[\\/]+Users[\\/]+([^\\/\s\"'<>%{}|*?:;,()\[\]`$]+)", re.I)
# Linux の /home/<名前> と、WSL の UNC（\\wsl.localhost\<ディストリビューション>\home\<名前>）
HOMEPATH = re.compile(r"(?:(?<![A-Za-z0-9_.])/home/|\\\\wsl(?:\.localhost|\$)?\\[^\\/\s]+\\home\\)"
                      r"([^\\/\s\"'<>%{}|*?:;,()\[\]`$]+)", re.I)
# macOS の /Users/<名前>（file:///Users/<名前> も。直前が英数字・: のものは URL のパスや USERPATH の形なので外す）
MACPATH = re.compile(r"(?<![A-Za-z0-9_.:$~\\-])/Users/([^\\/\s\"'<>%{}|*?:;,()\[\]`$]+)")
USER_OK = {"public", "default", "default user", "all users", "example", "tester", "testuser", "username", "you", "name",
           "runner", "yamada", "山田", "ユーザー", "ユーザー名", "利用者", "利用者名", "...", "shared"}
STEAM = re.compile(r"(?<![A-Za-z0-9_])7656119[0-9]{10}(?![A-Za-z0-9_])|userdata[\\/]+([0-9]{3,})")
LANIP = re.compile(r"(?<![A-Za-z0-9_])192\.168\.[0-9]{1,3}\.[0-9]{1,3}(?![A-Za-z0-9_])")
# ドライブ名の形のほか、Git Bash の形（/c/ の下）も見る
DEVPATH = re.compile(r"(?:(?<![A-Za-z0-9_])[A-Za-z]:|(?<![A-Za-z0-9_.])/[a-z])[\\/]+ClaudeCode(?![A-Za-z0-9_])", re.I)
SESSION = re.compile(r"(?<![A-Za-z0-9_])local_[0-9a-f]{8}-[0-9a-f]{4}-|side[-_]session[-_]notes|[\\/]\.claude[\\/]|"
                     r"Temp[\\/]+claude[\\/]|Packages[\\/]+Claude_|[\\/]scratchpad[\\/]", re.I)
TOKENS = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{30,}|"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})|"
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|(?i:\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*)")
ASSIGN = re.compile(r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|access[_-]?key|token)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']")

# ライセンスの出どころの確認（警告）
NOTICE = re.compile(r"(?i)\bcopyright\b\s*(?:\(c\)|©)?\s*(?:19|20)\d\d|SPDX-License-Identifie[r]|\bLicensed under the\b")


@dataclass
class Finding:
    level: str     # "error" | "warn"
    rule: str
    path: str
    line: int
    detail: str
    raw: str = ""  # 伏せる前の一致文字列（表示しない。allowlist.json の value との照合と、同じ行の別の値の区別に使う）


def mask(s: str) -> str:
    """一致した文字列を伏せる（先頭 2 文字と長さだけ）。"""
    s = s.strip()
    return (s[:2] + "*" * max(3, len(s) - 2)) if len(s) > 2 else "***"


def run_git(*args: str) -> str:
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} が失敗しました")
    return r.stdout.decode("utf-8", "replace")


SUBMODULE = "submodule"


def candidates() -> dict[str, str | None]:
    """{パス: 索引の blob の ID}。未追跡のものは None、サブモジュールは SUBMODULE。
    追跡中のものは作業フォルダーから消えていても対象にする（索引の内容がコミットに入るため）。"""
    out: dict[str, str | None] = {}
    for rec in run_git("ls-files", "-z", "--stage").split("\0"):
        if not rec:
            continue
        meta, p = rec.split("\t", 1)
        mode, oid = meta.split()[:2]
        out.setdefault(p, SUBMODULE if mode == "160000" else oid)
    for p in run_git("ls-files", "-z", "--others", "--exclude-standard").split("\0"):
        if p and os.path.isfile(os.path.join(REPO, p)):
            out.setdefault(p, None)
    return dict(sorted(out.items()))


def read_objects(oids) -> dict[str, tuple[str, bytes]]:
    """git のオブジェクトの {ID: (種類, 中身)}（git cat-file --batch でまとめて読む）。"""
    oids = sorted(set(oids))
    if not oids:
        return {}
    r = subprocess.run(["git", "-C", REPO, "cat-file", "--batch"], input=("\n".join(oids) + "\n").encode("ascii"),
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "git cat-file が失敗しました")
    out, data, pos = {}, r.stdout, 0
    for oid in oids:
        nl = data.index(b"\n", pos)
        head = data[pos:nl].decode("ascii", "replace").split()
        if len(head) != 3:
            raise RuntimeError(f"git のオブジェクトを読めません: {' '.join(head)}")
        size = int(head[2])
        out[oid] = (head[1], data[nl + 1:nl + 1 + size])
        pos = nl + 1 + size + 1
    return out


# push で公開される範囲（ブランチ・タグと今の HEAD）。--all は refs/stash（push されない。書き換えの前の古いコミットを
# 親に持つことがある）まで含むので使わない
PUSH_REFS = ("HEAD", "--branches", "--tags")


def history_objects(known: set[str]) -> dict[str, str]:
    """過去のコミットにある blob のうち、索引に無いもの（古い版・消したファイル）の {ID: パス}。コミットが無ければ空。"""
    try:
        run_git("rev-parse", "--verify", "-q", "HEAD")
    except RuntimeError:
        return {}
    out = {}
    for line in run_git("rev-list", *PUSH_REFS, "--objects").splitlines():
        oid, _, p = line.partition(" ")
        if p and oid not in known:
            out.setdefault(oid, p)      # 木（フォルダー）も混じる。種類は読んでから見る
    return out


def history_paths() -> set[str]:
    """過去のコミットに出てくる全てのパス（名前だけ変えたファイルの古い名前を含む。rev-list --objects は同じ中身を 1 回しか
    出さないので、コミットごとの変更の一覧から集める）。コミットが無ければ空。"""
    try:
        run_git("rev-parse", "--verify", "-q", "HEAD")
    except RuntimeError:
        return set()
    out = run_git("log", *PUSH_REFS, "-m", "--no-renames", "--name-only", "-z", "--format=")
    return {p.strip("\n") for p in out.split("\0") if p.strip("\n")}


class PrivateListError(ValueError):
    """非公開の文字列の一覧を読めない（文字コード・正規表現の誤り）。"""


def private_lines(path: str) -> list[tuple[int, str]]:
    """非公開の文字列の一覧の有効な行 [(行番号, 文字列)]。BOM で UTF-8・UTF-16 を見分け、BOM が無ければ UTF-8 として読む
    （メモ帳の「Unicode」・Windows PowerShell 5.1 の > は BOM 付きの UTF-16）。読めなければ PrivateListError。
    配布物の検査（scripts/package/check_package.py）も使う。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise PrivateListError(f"{path}: {type(e).__name__}") from None
    try:
        text = data.decode("utf-16") if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else data.decode("utf-8-sig")
    except UnicodeError:
        raise PrivateListError(f"{path}: UTF-8・UTF-16 として読めません。UTF-8 で保存し直してください") from None
    out = []
    for no, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s and not s.startswith("#"):
            out.append((no, s))
    return out


def load_private(path: str) -> list[re.Pattern]:
    """非公開の文字列の一覧。1 行に 1 つ。# で始まる行は注釈、re: で始まる行は正規表現、ほかは文字どおり（大文字小文字を区別しない）。
    読めない・正規表現が不正なときは PrivateListError。"""
    pats = []
    if not os.path.isfile(path):
        return pats
    for no, s in private_lines(path):
        try:
            pats.append(re.compile(s[3:] if s.startswith("re:") else re.escape(s), re.I))
        except re.error as e:
            raise PrivateListError(f"{path}（{no} 行目）: 正規表現が不正です（{e}）") from None
    return pats


def runtime_identity(public_name: str | None = None) -> list[tuple[str, re.Pattern]]:
    """この PC・この git の設定から分かる名前（PC の名前、git の user.name・user.email）。

    public_name（LICENSE の著作権表示の名義）と同じ user.name は対象にしない。公開する名前で、LICENSE・README.md の
    著作権表示に書くもの（docs/publish_checklist.md 6.3 節）。"""
    out = []
    host = os.environ.get("COMPUTERNAME") or ""
    if len(host) >= 4:
        out.append(("PC の名前", re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(host) + r"(?![A-Za-z0-9_-])", re.I)))
    for key, label in (("user.email", "git の user.email"), ("user.name", "git の user.name")):
        try:
            v = run_git("config", "--get", key).strip()
        except RuntimeError:
            v = ""
        if key == "user.name" and public_name and v.casefold() == public_name.strip().casefold():
            continue
        if len(v) >= 4 and not EMAIL_OK.search(v):
            out.append((label, re.compile(re.escape(v), re.I)))
    return out


def load_allowlist() -> list[dict]:
    if not os.path.isfile(ALLOWLIST):
        return []
    with open(ALLOWLIST, encoding="utf-8") as f:
        data = json.load(f)
    for e in data.get("allow", []):
        if not e.get("reason"):
            raise ValueError(f"allowlist.json: 理由（reason）の無い項目があります: {e}")
        # 個人情報・秘密の指摘の文は伏せ字なので、match（文への部分一致）では先頭 2 文字が同じ別の値まで許可してしまう
        if str(e.get("rule", "")).startswith(("pii.", "secret.")) and e.get("match"):
            raise ValueError("allowlist.json: pii.*・secret.* の項目は match ではなく value（伏せる前の値）で書いてください: "
                             f"{e.get('rule')} {e.get('path')}")
    return data.get("allow", [])


def entry_matches(e: dict, f: Finding) -> bool:
    """allowlist.json の 1 項目が指摘 f に当たるか。"""
    if e.get("rule") not in (f.rule, "*"):
        return False
    if not fnmatch.fnmatchcase(f.path, e.get("path", "*")):
        return False
    if e.get("match") and e["match"] not in f.detail:
        return False
    # value: 伏せる前の一致文字列との完全一致（大文字小文字は区別しない）
    if e.get("value") is not None and f.raw.lower() != str(e["value"]).lower():
        return False
    return True


def allowed(f: Finding, allow: list[dict]) -> bool:
    return any(entry_matches(e, f) for e in allow)


def unused_entries(findings: list[Finding], allow: list[dict]) -> list[int]:
    """どの指摘にも当たらない allowlist.json の項目の番号（0 から）。コードが変わって要らなくなった許可は、
    後で同じ文を含む別の行を確認なしに許可してしまうので、警告（allow.unused）にして外してもらう。"""
    return [i for i, e in enumerate(allow) if not any(entry_matches(e, f) for f in findings)]


def is_text(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _strings(b: bytes) -> str:
    """メタデータの塊の中の文字列（ASCII の 4 文字以上の並びと、UTF-16LE として読める並び）。"""
    out = [m.group(0).decode("ascii") for m in re.finditer(rb"[\x20-\x7e]{4,}", b)]
    for start in (0, 1):
        t = b[start:start + (len(b) - start) // 2 * 2].decode("utf-16-le", "replace")
        out += [s for s in re.split(r"[\x00-\x1f�]+", t) if len(s) >= (4 if s.isascii() else 2) and s.isprintable()]
    return "\n".join(out)


def binary_text(data: bytes) -> str:
    """画像のメタデータの文字列（PNG の tEXt・zTXt・iTXt・eXIf、JPEG の APP1（Exif・XMP）・COM）。画素のデータは見ない
    （全体から文字を拾うと、圧縮されたデータの偶然の並びがメールアドレスなどに当たる）。知らない形式は空。"""
    out = []
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            pos = 8
            while pos + 8 <= len(data):
                n, kind = struct.unpack_from(">I4s", data, pos)
                body = data[pos + 8:pos + 8 + n]
                pos += 12 + n
                if kind == b"tEXt":
                    out.append(body.replace(b"\0", b"\n").decode("latin-1"))
                elif kind == b"zTXt":
                    key, _, rest = body.partition(b"\0")
                    out.append(key.decode("latin-1") + "\n" + zlib.decompress(rest[1:]).decode("latin-1"))
                elif kind == b"iTXt":
                    key, _, rest = body.partition(b"\0")
                    compressed, rest = rest[:1] == b"\1", rest[2:]
                    lang, _, rest = rest.partition(b"\0")
                    tkey, _, text = rest.partition(b"\0")
                    if compressed:
                        text = zlib.decompress(text)
                    out.append("\n".join(x.decode("utf-8", "replace") for x in (key, lang, tkey, text)))
                elif kind == b"eXIf":
                    out.append(_strings(body))
                elif kind == b"IEND":
                    break
        elif data.startswith(b"\xff\xd8"):
            pos = 2
            while pos + 4 <= len(data) and data[pos] == 0xFF:
                marker = data[pos + 1]
                if marker in (0xD9, 0xDA):                  # 終わり・画素のデータの始まり
                    break
                if 0xD0 <= marker <= 0xD7 or marker in (0x01, 0xFF):
                    pos += 1 if marker == 0xFF else 2
                    continue
                n = struct.unpack_from(">H", data, pos + 2)[0]
                seg = data[pos + 4:pos + 2 + n]
                pos += 2 + n
                if marker == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/\0"):
                    out.append(seg[29:].decode("utf-8", "replace"))      # XMP
                elif marker == 0xE1:
                    out.append(_strings(seg))                           # Exif
                elif marker == 0xFE:
                    out.append(seg.decode("utf-8", "replace"))          # コメント
    except (struct.error, zlib.error, IndexError, ValueError):
        pass                               # 壊れた残りは読まない（読めた分は検査する）
    return "\n".join(out)


# テキストに埋め込んだ base64 のデータ（data:image/png;base64,...）
DATA_URI = re.compile(r"data:([\w.+/-]+);base64,([A-Za-z0-9+/=]{64,})")
_U_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def unescape_line(line: str) -> str:
    """表記を変えただけの形（JSON の \\uXXXX・HTML の文字参照・URL の %XX・全角の英数字と＠）を戻した行。"""
    s = line
    if "\\u" in s:
        s = _U_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), s)
        s = s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")    # サロゲートの対を 1 文字に
    if "&" in s:
        s = html.unescape(s)
    if "%" in s:
        s = urllib.parse.unquote(s)
    return unicodedata.normalize("NFKC", s)


def redact(line: str, private: list[re.Pattern], ident: list[tuple[str, re.Pattern]]) -> str:
    """警告の文に入れる行から、個人情報・秘密らしき部分（メールアドレス・利用者名・Steam の ID・トークン・代入の値・
    非公開の一覧・この PC・git の設定の名前）を伏せ字にした行。一致した部分だけを置き換える。"""
    spans = [m.span() for m in EMAIL.finditer(line) if not EMAIL_OK.search(m.group(0))]
    for rx in (USERPATH, HOMEPATH, MACPATH):
        spans += [m.span(1) for m in rx.finditer(line) if m.group(1).lower() not in USER_OK]
    spans += [m.span() for m in STEAM.finditer(line)] + [m.span() for m in TOKENS.finditer(line)]
    spans += [m.span(1) for m in ASSIGN.finditer(line)]
    for rx in private + [rx for _, rx in ident]:
        spans += [m.span() for m in rx.finditer(line) if m.group(0)]
    out, pos = [], 0
    for a, b in sorted(spans):
        if b <= pos:
            continue
        a = max(a, pos)
        out += [line[pos:a], mask(line[a:b])]
        pos = b
    return "".join(out) + line[pos:]


def check_path_pii(p: str, private: list[re.Pattern], ident: list[tuple[str, re.Pattern]], add,
                   what: str = "ファイル名・フォルダー名") -> None:
    """パス（ファイル名・フォルダー名。ブランチ・タグの名前にも使う）の個人情報: メールアドレス・Steam の ID・この PC・git の
    設定の名前・非公開の一覧。パスは push で公開されるので中身と同じに扱う（表示は main で一致した部分を伏せる）。"""
    for m in EMAIL.finditer(p):
        if not EMAIL_OK.search(m.group(0)):
            add("error", "pii.email", p, 0, f"{what}にメールアドレス {mask(m.group(0))}", raw=m.group(0))
    for m in STEAM.finditer(p):
        add("error", "pii.steam", p, 0, f"{what}に Steam の ID らしき数字 {mask(m.group(0))}", raw=m.group(0))
    for label, rx in ident:
        for m in rx.finditer(p):
            add("error", "pii.identity", p, 0, f"{what}に {label} {mask(m.group(0))}", raw=m.group(0))
    for rx in private:
        for m in rx.finditer(p):
            if m.group(0):
                add("error", "pii.private", p, 0, f"{what}に非公開の一覧の文字列 {mask(m.group(0))}", raw=m.group(0))


def check_path(p: str, size: int, add) -> None:
    low = p.lower()
    parts = low.split("/")
    ext = os.path.splitext(low)[1]
    if ext in FORBIDDEN_EXT:
        add("error", "path.forbidden", p, 0, f"入れてはいけない種類のファイル（{ext}）")
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pat) for pat in FORBIDDEN_NAMES):
        add("error", "path.forbidden", p, 0, "鍵・ローカルの設定・非公開の一覧などのファイル名")
    bad_dirs = FORBIDDEN_DIRS.intersection(parts[:-1])
    if bad_dirs:
        add("error", "path.forbidden", p, 0, "入れてはいけないフォルダーの中: " + ", ".join(sorted(bad_dirs)))
    if size > MAX_FILE:
        add("error", "size.file", p, 0, f"{size / 1048576:.1f} MB（上限 {MAX_FILE // 1048576} MB）")
    elif size > WARN_FILE:
        add("warn", "size.file", p, 0, f"{size / 1048576:.1f} MB（{WARN_FILE // 1048576} MB を超える）")


def own_copyright(read) -> str | None:
    """LICENSE の著作権表示の行（このツール自体の名義。license.notice の対象から外す）。read: パス → 中身（無ければ None）。"""
    text = read("LICENSE")
    if text is None:
        return None
    m = re.search(r"^Copyright \(c\) .+$", text, re.M)
    return m.group(0) if m else None


def copyright_holder(own: str | None) -> str | None:
    """著作権表示の行（Copyright (c) の後に年と名義）の名義の部分。年が無ければ (c) の後ろ全体、行が無ければ None。"""
    if not own:
        return None
    m = re.match(r"Copyright \(c\)\s+(?:\d{4}(?:\s*[-–,]\s*\d{4})*\s+)?(.+?)\s*$", own)
    return m.group(1) if m else None


def check_text(p: str, text: str, private: list[re.Pattern], ident: list[tuple[str, re.Pattern]], own: str | None,
               add) -> None:
    third_party = p.startswith(THIRD_PARTY_DIRS) or VENDOR.search(p) is not None
    for no, line in enumerate(text.splitlines(), 1):
        if not third_party and NOTICE.search(line) and not (own and own in line):
            add("warn", "license.notice", p, no,
                "著作権表示・ライセンスの記載（出どころの確認用）: " + redact(line, private, ident).strip()[:100])
        # 個人情報は、文字どおりの行と、表記を変えただけの形（\uXXXX・&#..;・%XX・全角）を戻した行の両方で見る
        # （同じ値は add で 1 件にまとまる）
        plain = unescape_line(line)
        for s in (line, plain) if plain != line else (line,):
            if not third_party:
                for m in EMAIL.finditer(s):
                    if not EMAIL_OK.search(m.group(0)):
                        add("error", "pii.email", p, no, f"メールアドレス {mask(m.group(0))}", raw=m.group(0))
            for rx in (USERPATH, HOMEPATH, MACPATH):
                for m in rx.finditer(s):
                    if m.group(1).lower() not in USER_OK:
                        add("error", "pii.userpath", p, no, f"利用者名を含むパス（名前 {mask(m.group(1))}）", raw=m.group(1))
            for m in STEAM.finditer(s):
                add("error", "pii.steam", p, no, f"Steam の ID らしき数字 {mask(m.group(0))}", raw=m.group(0))
            for label, rx in ident:
                for m in rx.finditer(s):
                    add("error", "pii.identity", p, no, f"{label} {mask(m.group(0))}", raw=m.group(0))
            for rx in private:
                for m in rx.finditer(s):
                    add("error", "pii.private", p, no, f"非公開の一覧の文字列 {mask(m.group(0))}", raw=m.group(0))
        for m in TOKENS.finditer(line):
            add("error", "secret.token", p, no, f"トークン・鍵らしき文字列 {mask(m.group(0))}", raw=m.group(0))
        for m in ASSIGN.finditer(line):
            add("warn", "secret.assign", p, no, f"パスワード・トークンらしき代入 {mask(m.group(1))}", raw=m.group(1))
        for m in SESSION.finditer(line):
            add("error", "env.session", p, no, f"Claude Code の内部の ID・一時フォルダー（{m.group(0)}）")
        for m in LANIP.finditer(line):
            add("warn", "env.lanip", p, no, f"LAN の IP アドレス {m.group(0)}")
        for m in DEVPATH.finditer(line):
            add("warn", "env.devpath", p, no, "開発環境の既定のパス: " + redact(line, private, ident).strip()[:100])


def check_metadata(p: str, data: bytes, line: int, private: list[re.Pattern], ident: list[tuple[str, re.Pattern]],
                   own: str | None, add) -> None:
    """画像などのメタデータ（binary_text）に個人情報の規則を当てる。license.unknown を allowlist.json で認めても外さない。
    line: 指摘の行番号（ファイルそのものは 0、テキストに埋め込んだものはその行）。"""
    meta = binary_text(data)
    if not meta:
        return

    def add_meta(level, rule, path, _line, detail, raw=""):
        add(level, rule, path, line, detail + "（画像などのメタデータ。消してから保存し直す）", raw)
    check_text(p, meta, private, ident, own, add_meta)


def check_data_uris(p: str, text: str, private: list[re.Pattern], ident: list[tuple[str, re.Pattern]], own: str | None,
                    add) -> None:
    """テキストに埋め込んだ base64 のデータ（data:...;base64,...）。画像は出どころの確認（license.unknown）、中のメタデータには
    個人情報の規則を当てる。第三者のコード（vendor/・licenses/）の中は見ない。"""
    if p.startswith(THIRD_PARTY_DIRS) or VENDOR.search(p) is not None:
        return
    for no, line in enumerate(text.splitlines(), 1):
        for m in DATA_URI.finditer(line):
            try:
                data = base64.b64decode(m.group(2))
            except (binascii.Error, ValueError):
                continue
            if m.group(1).lower().startswith("image/"):
                add("error", "license.unknown", p, no,
                    "埋め込みの画像（data URI）。出どころとライセンスを allowlist.json に書いたものだけ入れる")
            check_metadata(p, data, no, private, ident, own, add)


def check_licenses(files: list[str], add, read) -> None:
    names = set(files)
    vendors = {}
    for p in files:
        m = VENDOR.search(p)
        if m:
            vendors.setdefault(p[:m.end()], []).append(p)
    for root in vendors:
        if not any(re.match(re.escape(root) + r"(LICEN[CS]E|COPYING)", f, re.I) for f in names):
            add("error", "license.vendor", root, 0, "第三者のコードのフォルダーに LICENSE がありません")
    if "LICENSE" not in names:
        return
    text = read("LICENSE") or ""
    m = re.search(r"^Copyright \(c\) .+$", text, re.M)
    if "MIT License" not in text or not m:
        add("error", "license.root", "LICENSE", 0, "MIT License の本文・著作権表示が見つかりません")
        return
    if "README.md" in names:
        readme = read("README.md") or ""
        if m.group(0) not in readme:
            add("error", "license.root", "README.md", 0, "LICENSE と同じ著作権表示の行がありません")


def check_commits(private: list[re.Pattern], ident: list[tuple[str, re.Pattern]], own: str | None, add) -> int:
    """push される範囲（PUSH_REFS）のコミットと注釈つきタグ。作者・コミッター・タグを付けた人のメールアドレスは GitHub の
    noreply だけを認め、名前とメッセージにはファイルと同じ規則（check_text）を当てる（行番号は 1 = 作者名・タグを付けた人の名前、
    2 = コミッター名、続いてメッセージ）。警告（開発環境のパスなど）は出さない。コミットが無ければ 0。"""
    try:
        run_git("rev-parse", "--verify", "-q", "HEAD")
    except RuntimeError:
        return 0

    def add_msg(level, rule, path, line, detail, raw=""):
        if level == "error":
            add(level, rule, path, line, detail + "（コミット・タグの名前・メッセージ。修正のコミットを足しても消えない。push の前に "
                "git commit --amend・git rebase -i の reword などで直す）", raw)

    def check_email(where, field):
        if not COMMIT_EMAIL_OK.search(field):
            why = "（仮のアドレス。push の前に noreply へ書き換える。docs/publish_checklist.md 6.3 節）" if EMAIL_OK.search(field) else ""
            add("error", "git.author", where, 0, f"GitHub の noreply ではないメールアドレス {mask(field)}{why}")
        if any(rx.search(field) for rx in private):
            add("error", "git.author", where, 0, "コミットに非公開の一覧の文字列があります")

    log = run_git("log", *PUSH_REFS, "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01")
    n = 0
    for rec in log.split("\x01"):
        f = rec.strip("\n").split("\0")
        if len(f) < 6:
            continue
        n += 1
        where = f"commit {f[0][:10]}"
        for field in (f[2], f[4]):         # 作者・コミッター
            check_email(where, field)
        check_text(where, "\n".join((f[1], f[3], f[5])), private, ident, own, add_msg)
    # 注釈つきタグ（タグのオブジェクトの作者とメッセージ。git push --tags・git push origin <タグ> で公開される）
    tags = run_git("for-each-ref", "refs/tags",
                   "--format=%(objecttype)%00%(refname:short)%00%(taggername)%00%(taggeremail)%00%(contents)%01")
    for rec in tags.split("\x01"):
        f = rec.strip("\n").split("\0")
        if len(f) < 5 or f[0] != "tag":
            continue
        where = f"tag {f[1]}"
        check_email(where, f[3].strip().strip("<>"))
        check_text(where, "\n".join((f[2], "", f[4])), private, ident, own, add_msg)
    # ブランチ・タグの名前（push で公開される）
    for ref in run_git("for-each-ref", "refs/heads", "refs/tags", "--format=%(refname:short)").splitlines():
        if ref.strip():
            check_path_pii(f"ref {ref.strip()}", private, ident, add_msg, what="ブランチ・タグの名前")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub に公開する前の検査")
    ap.add_argument("--private", default=os.environ.get("GDSKIN_PUBLISH_PRIVATE", PRIVATE_DEFAULT),
                    help="非公開の文字列の一覧（リポジトリの外に置く）")
    ap.add_argument("--list", help="公開予定のファイルの一覧（パスと大きさ）を TSV で書き出す")
    ap.add_argument("--strict", action="store_true", help="警告も問題として扱う")
    ap.add_argument("--quiet", action="store_true", help="許可済みの件数の内訳を出さない")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    try:
        index = candidates()
        objects = read_objects(o for o in index.values() if o and o != SUBMODULE)
        history = history_objects({o for o in index.values() if o})
        objects.update(read_objects(history))
        old_paths = history_paths() - set(index)
        stash = subprocess.run(["git", "-C", REPO, "rev-parse", "-q", "--verify", "refs/stash"],
                               capture_output=True).returncode == 0
    except (RuntimeError, FileNotFoundError) as e:
        print(f"git のファイル一覧を読めません（git init と .gitignore が必要）: {e}")
        return 2
    files = list(index)
    private_path = os.path.abspath(args.private)
    # 大文字小文字・区切りの違い（c:\…・C:/…）でもリポジトリの中と分かるように比べる
    if os.path.normcase(private_path).startswith(os.path.normcase(REPO + os.sep)):
        print(f"非公開の文字列の一覧はリポジトリの外に置いてください: {private_path}")
        return 2

    # コミットに入る中身: 追跡中のものは索引の blob、未追跡のものは作業フォルダー（git add -A でそのまま入る）
    contents: dict[str, bytes] = {}
    worktree: dict[str, bytes | None] = {}
    for p, oid in index.items():
        full = os.path.join(REPO, p)
        wt = None
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                wt = fh.read()
        worktree[p] = wt
        if oid is None:
            contents[p] = wt
        elif oid != SUBMODULE:
            contents[p] = objects[oid][1]

    def read(p):
        data = contents.get(p)
        return None if data is None else data.decode("utf-8", "replace").replace("\r\n", "\n")

    try:
        private = load_private(private_path)
    except PrivateListError as e:
        print(f"非公開の文字列の一覧を読めません: {e}")
        return 2
    own = own_copyright(read)
    ident = runtime_identity(copyright_holder(own))
    allow = load_allowlist()

    findings: list[Finding] = []
    seen = set()

    def add(level, rule, path, line, detail, raw=""):
        # 同じ行の同じ規則は 1 件にまとめる（1 行に同じパスが何度も出るとき）。個人情報などは値ごとに分ける
        # （同じ行の別の名前が、allowlist で許可した 1 件にまとめられて消えないように）
        key = (rule, path, line, raw.lower()) if raw else (rule, path, line) if line else (rule, path, line, detail)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(level, rule, path, line, detail, raw))

    def add_history(level, rule, path, line, detail, raw=""):
        # 過去のコミットにだけある版: push で公開されるので問題として出す（警告は今の版で見る）
        if level == "error":
            add(level, rule, path, line, detail + "（過去のコミットの版。修正のコミットを足しても消えない。"
                "commit --amend か履歴の作り直しが必要）", raw)

    total = 0
    by_top: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    rows = []
    for p, oid in index.items():
        if oid == SUBMODULE:
            add("error", "path.forbidden", p, 0, "サブモジュール（中身を検査できない）")
            continue
        data = contents[p]
        size = len(data)
        total += size
        top = p.split("/")[0] if "/" in p else "(直下)"
        by_top[top][0] += 1
        by_top[top][1] += size
        rows.append((p, size))
        check_path(p, size, add)
        check_path_pii(p, private, ident, add)
        wt = worktree[p]
        if oid is not None and (wt is None or wt.replace(b"\r\n", b"\n") != data.replace(b"\r\n", b"\n")):
            add("warn", "index.stale", p, 0,
                "索引（git add した内容）と作業フォルダーが違います（" + ("作業フォルダーに無い" if wt is None else "両方を検査") +
                "）。直した後は git add -A をやり直してから検査する")
        text = is_text(data)
        if text is None:
            add("error", "license.unknown", p, 0, "バイナリー（画像など）。出どころとライセンスを allowlist.json に書いたものだけ入れる")
            check_metadata(p, data, 0, private, ident, own, add)
            continue
        check_text(p, text, private, ident, own, add)
        check_data_uris(p, text, private, ident, own, add)
        if oid is not None and wt is not None and wt != data:
            wt_text = is_text(wt)
            if wt_text is not None:
                check_text(p, wt_text, private, ident, own, add)
                check_data_uris(p, wt_text, private, ident, own, add)
    for oid, p in history.items():
        kind, data = objects[oid]
        if kind != "blob":
            continue
        check_path(p, len(data), add_history)
        text = is_text(data)
        if text is None:
            add_history("error", "license.unknown", p, 0, "バイナリー（画像など）。出どころとライセンスを allowlist.json に書いたものだけ入れる")
            check_metadata(p, data, 0, private, ident, own, add_history)
            continue
        check_text(p, text, private, ident, own, add_history)
        check_data_uris(p, text, private, ident, own, add_history)
    # 過去のコミットにだけあるパス（消したファイル・名前を変える前の名前・フォルダー名）
    for p in sorted(old_paths):
        check_path_pii(p, private, ident, add_history)
    for req in REQUIRED:
        if req not in contents:
            add("error", "docs.missing", req, 0, "公開用のファイルがありません")
    check_licenses(files, add, read)
    n_commits = check_commits(private, ident, own, add)
    if stash:
        add("warn", "git.stash", "refs/stash", 0, "stash があります。push されないので検査の対象外（書き換えの前の古いコミットを"
            "指すことがある。要らなければ git stash drop、戻すなら git stash pop）")
    if total > WARN_TOTAL:
        add("warn", "size.total", "(合計)", 0, f"{total / 1048576:.1f} MB")
    # 使われない許可（ほかの指摘をすべて出した後に数える）
    for i in unused_entries(findings, allow):
        e = allow[i]
        what = " ".join(f"{k}={e[k]!r}" for k in ("rule", "path", "match") if e.get(k))
        if e.get("value") is not None:
            what += " value=（伏せ字）"
        add("warn", "allow.unused", "scripts/publish/allowlist.json", 0,
            f"どの指摘にも当たらない許可（{i + 1} 番目の項目: {what}）。要らなければ外す")

    shown = [f for f in findings if not allowed(f, allow)]
    # ファイル名・フォルダー名に当たった個人情報は、表示するパスでも伏せる（Finding.path は allowlist.json の照合のため生のまま）
    hidden = sorted({f.raw for f in findings if f.rule.startswith("pii.") and not f.line and f.raw
                     and f.raw.lower() in f.path.lower()}, key=len, reverse=True)

    def show_path(s: str) -> str:
        for v in hidden:
            s = re.sub(re.escape(v), lambda _m, v=v: mask(v), s, flags=re.I)
        return s
    ok_count = defaultdict(int)
    for f in findings:
        if allowed(f, allow):
            ok_count[f.rule] += 1

    if args.list:
        with open(args.list, "w", encoding="utf-8", newline="\n") as out:
            out.write("path\tbytes\n")
            for p, s in rows:
                out.write(f"{p}\t{s}\n")

    print(f"公開予定のファイル: {len(files)} 件、合計 {total / 1048576:.2f} MB（{total:,} バイト）")
    for top, (n, s) in sorted(by_top.items(), key=lambda kv: -kv[1][1]):
        print(f"  {show_path(top):<14} {n:>4} 件 {s / 1048576:>8.2f} MB")
    print(f"非公開の文字列の一覧: {'読み込み ' + str(len(private)) + ' 件' if private else '無し（' + private_path + '）'}"
          f"、この PC・git の設定から {len(ident)} 件、コミット {n_commits} 件")
    errors = [f for f in shown if f.level == "error" or args.strict]
    warns = [f for f in shown if f.level == "warn" and not args.strict]
    for title, group in (("問題", errors), ("警告", warns)):
        if not group:
            continue
        print(f"\n== {title}: {len(group)} 件")
        by_rule = defaultdict(list)
        for f in group:
            by_rule[f.rule].append(f)
        for rule in sorted(by_rule):
            print(f"[{rule}] {len(by_rule[rule])} 件")
            for f in by_rule[rule]:
                loc = f"{show_path(f.path)}:{f.line}" if f.line else show_path(f.path)
                print(f"  {loc}  {f.detail}")
    if ok_count and not args.quiet:
        print("\n許可済み（allowlist.json）: " + "、".join(f"{r} {n} 件" for r, n in sorted(ok_count.items())))
    if not private:
        print("\n注意: 非公開の文字列の一覧がありません。本名・メールアドレス・Steam の ID などを書いた一覧を "
              f"{private_path} に置くと、それも検査します（docs/publish_checklist.md 5 章）。")
    print("\n結果: " + ("問題あり" if errors else "問題なし" + (f"（警告 {len(warns)} 件）" if warns else "")))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
