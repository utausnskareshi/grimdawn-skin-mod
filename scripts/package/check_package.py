"""配布物（展開したフォルダー、または ZIP）の検査（docs/packaging.md 5 章）。

    python scripts/package/check_package.py <フォルダー または .zip>

* 必要なファイルがあること（EXE、Python、バックエンド、生成スクリプト、セットアップの定義、ライセンス、README など）
* 含めてはいけないものが無いこと: 開発用のファイル（テスト・キャッシュ・開発用のスクリプト・文書・E2E）、サンプル、
  TRELLIS の見本画像、ゲームから取り出したファイル、試作のビルド、3D モデル・AI の重み、デバッグ情報
* 利用者・開発者を特定できる文字列（利用者のフォルダー名・メールアドレス・Claude の仮想フォルダー）・トークンが無いこと
  （メールアドレス・利用者名のパスの規則は scripts/publish/check_publish.py と共有。第三者のライセンスの文書は除く。
  非公開の一覧が無い・読めないときは警告する）。テキストかどうかは拡張子ではなく中身で決め（UTF-8・UTF-16）、
  表記を変えただけの形（\\uXXXX・%XX など）も見る。ファイル名・フォルダー名も見る。中身がバイナリーのもの（DLL・.pyd を含む）は
  UTF-8・UTF-16LE の両方で探す
  開発環境の既定のパス（C:\\ClaudeCode）が残っていないこと（D-027。2026-10-16 に警告から問題に変更）。
  単一ファイルの GDSkinStudio.exe は、中のアプリのアセンブリ（圧縮されている）を展開して見る
* 単一ファイルの GDSkinStudio.exe に、英語の画面の衛星アセンブリ（隣の en フォルダーでもよい）と、ヘルプの文（help_ja.md・
  help_en.md。GDSkinStudio.dll の埋め込みのリソース）が入っていること
* 鍵・ローカルの設定・非公開の一覧のファイル（公開前の検査と同じ名前の一覧）が無いこと
* セットアップの定義（setup_manifest.json）の license.files の全文が配布物にあること
* DINOv2 の固定コミット（D-025）: 取得の定義・models_manifest.json・fetch_models.py で一致し、Cell-DINO・X-Ray DINO の
  入った版でなく、そのコード・文書を取得の対象・ライセンスの表示に含まないこと
* ツール自体のライセンス（D-024: MIT）: LICENSE.txt が MIT の本文で、著作権表示が決定どおりの表記（COPYRIGHT）と一致すること、
  README.txt・THIRD_PARTY_NOTICES.txt と英語の README.en.txt・THIRD_PARTY_NOTICES.en.txt（2 言語対応 D-060）に同じ著作権表示が
  あること、仮の文字列（<COPYRIGHT HOLDER> など）が残っていないこと、LICENSE.txt・README(.en).txt・THIRD_PARTY_NOTICES(.en).txt・
  MANIFEST.txt が BOM なしの UTF-8 で、改行が CRLF であること
* ZIP のときは、エントリーのパスが安全なこと（絶対パス・.. が無い）と、全てが 1 つのフォルダーの下にあること

終了コード: 0 = 問題なし（警告は表示だけ）、1 = 問題あり。
"""
from __future__ import annotations

import codecs
import fnmatch
import json
import os
import posixpath
import re
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "publish"))
import check_publish  # noqa: E402  個人情報の規則（EMAIL・USERPATH）を公開前の検査と共有する

REQUIRED = [
    "GDSkinStudio.exe", "WebView2Loader.dll", "web/preview.html", "web/vendor/three/three.module.min.js",
    "web/vendor/three/LICENSE",
    # 3D プレビューが読むファイルと three の版の記録（app/frontend/GDSkinStudio/web の全部。KR_late-10）
    "web/preview.js", "web/preview.css", "web/vendor/three/README.txt",
    "web/vendor/three/addons/loaders/GLTFLoader.js", "web/vendor/three/addons/controls/OrbitControls.js",
    "web/vendor/three/addons/utils/BufferGeometryUtils.js",
    "python/python.exe", "python/python311.dll", "python/python311.zip", "python/python311._pth", "python/LICENSE.txt",
    "python/Lib/site-packages/numpy/__init__.py", "python/Lib/site-packages/scipy/__init__.py",
    "python/Lib/site-packages/PIL/__init__.py", "python/Lib/site-packages/lz4/__init__.py",
    "python/Lib/site-packages/fast_simplification/__init__.py", "python/Lib/site-packages/xatlas-0.0.11.dist-info/METADATA",
    "app/backend/run_backend.py", "app/backend/worker.py", "app/backend/bg_preview.py",
    "app/backend/gdskin_backend/server.py", "app/backend/gdskin_backend/setup/__init__.py",
    "app/backend/gdskin_backend/setup/download.py",
    "src/gdmesh/msh.py", "src/gdconvert/pipeline.py", "src/modbuilder/builder.py",
    "scripts/trellis/generate_glb.py", "scripts/trellis/postprocess_alt.py", "scripts/trellis/pose_check.py",
    "scripts/trellis/measure.py", "scripts/trellis/gen_helpers.py", "scripts/trellis/prompt_fit.py",
    "scripts/trellis/variant_base.py",
    "scripts/trellis/fetch_models.py", "scripts/trellis/setup_manifest.json",
    "scripts/trellis/setup_sources.json", "scripts/trellis/models_manifest.json", "scripts/trellis/models_manifest_text.json",
    "scripts/trellis/requirements-trellis-runtime.hashes.txt", "scripts/trellis/requirements-trellis-sdist.hashes.txt",
    "scripts/trellis/requirements-trellis-torch.hashes.txt", "scripts/trellis/notices/clip_model_card_notice.ja.txt",
    "scripts/trellis/notices/clip_model_card_notice.en.txt",
    "licenses/setup/trellis-runtime-packages.txt", "licenses/setup/trellis-runtime-packages.en.txt",
    "licenses/setup/trellis-runtime-packages.json",       # 非商用の印の検査（runtime_noncommercial_warnings）が読む
    "licenses/bundled/dotnet/LICENSE.txt",
    "README.txt", "README.en.txt", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.en.txt", "LICENSE.txt", "MANIFEST.txt",
    "gdskin_dist.json",
]

# 英語の画面の衛星アセンブリ（単一ファイルの GDSkinStudio.exe の中の一覧での名前。2026-10-17 点検 UG-R6）
SATELLITE_EN = b"en/GDSkinStudio.resources.dll"
# ヘルプの文の埋め込みのリソースの名前（GDSkinStudio.dll の中のリソースの一覧での名前。GDSkinStudio.csproj の EmbeddedResource の
# LogicalName・Help/HelpDocument.cs の ResourceName と同じにする。tests/test_publish_checks.py が 3 つの一致を確かめる）
HELP_RESOURCES = (b"GDSkinStudio.Help.help_ja.md", b"GDSkinStudio.Help.help_en.md")

# サードパーティのパッケージ（Python の site-packages）の中は、パッケージに付いているテストなどをそのまま認める
THIRD_PARTY = ("python/lib/site-packages/",)
FORBIDDEN_DIRS = {"tests", "test", "__pycache__", ".pytest_cache", ".git", ".vs", "obj", "docs", "samples", "prototype",
                  "research", "build", "tools", "e2erunner", "node_modules", ".idea", "work", "refs", "library", "builds"}
FORBIDDEN_FILES = ["test_*.py", "*_test.py", "conftest.py", "*.pdb", "*.user", "*.log", "*.tmp", "*.bak", ".ds_store",
                   "thumbs.db", "*.pyc", "*.pyo", "*.orig", "*.sln", "*.csproj", "*.cs", "*.xaml"]
DEV_SCRIPTS = ["scripts/trellis/analyze_glb.py", "scripts/trellis/compare_glb_render.py", "scripts/trellis/eval_*.py",
               "scripts/trellis/eval_pose.json", "scripts/trellis/build_cuda_ext.cmd", "scripts/trellis/fetch_cuda_redist.py",
               "scripts/trellis/cuda_redist_components.json", "scripts/trellis/requirements-trellis-dev-extra.lock.txt",
               "scripts/trellis/requirements-trellis-dev-extra.hashes.txt",
               "scripts/package/**", "scripts/*.py", "app/tools/**", "app/frontend/**", "tests/**", "src/*/tests/**"]
# ゲームのファイル（Grim Dawn から取り出したもの・MOD のビルド・セーブ）
GAME_EXT = {".msh", ".tex", ".arc", ".arz", ".dbr", ".anm", ".tpl", ".ssh", ".pfx", ".wrl", ".qst", ".fnt", ".map",
            ".gdc", ".gst", ".gsh"}
# 3D モデル・AI の重み・中間データ・画像（見本画像・生成結果）
MODEL_EXT = {".glb", ".gltf", ".obj", ".stl", ".mtl", ".usdz", ".ply", ".fbx", ".pt", ".pth", ".safetensors", ".onnx",
             ".ckpt", ".npz", ".npy", ".bin", ".gdskin", ".gdskinmod"}
ARCHIVE_EXT = {".whl", ".zip", ".7z", ".tar", ".gz", ".nupkg"}     # 取得物・作業の残り（python311.zip は標準ライブラリ）
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tga", ".dds", ".tif", ".tiff", ".exr", ".hdr", ".avif",
             ".heic", ".ico"}
IMAGE_OK = ("web/",)               # 画面の部品の画像だけ（現在は無し）
SAMPLE_NAMES = re.compile(r"typical_humanoid|yoimiya|orangeguy|dragonborn|example_multi_image|assets/example|"
                          r"mod0[1-9]|build_all|library_all|torso_on_head|candidates\.png", re.I)
# 実行ファイル（中身の文字列は embedded_problems で UTF-8・UTF-16LE の両方を探す）。テキストかどうかは拡張子ではなく
# 中身で判定する（_decode_text。UTF-16 の文書・.yaml・拡張子の無いファイルも検査する）
BINARY_EXT = {".exe", ".dll", ".pyd"}
# 利用者・開発者を特定できる文字列。開発者のメールアドレス・Steam の ID などの非公開の文字列はリポジトリに書かず、
# リポジトリの外の一覧から読む（scripts/publish/check_publish.py と同じファイル。docs/publish_checklist.md 5 章）
USERNAME = os.environ.get("USERNAME") or "user"
PRIVATE_LIST = os.environ.get("GDSKIN_PUBLISH_PRIVATE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), ".gdskin-publish-private.txt")


def private_patterns() -> tuple[list[str], str | None]:
    """非公開の一覧: 1 行に 1 つ。# は注釈、re: で始まる行は正規表現、ほかは文字どおり（文字コードは check_publish と同じく
    UTF-8 か BOM 付きの UTF-16）。戻り値は (パターン, 読めなかった理由)。無い・読めないときは ([], 理由)。
    正規表現が不正な行は除き、残りのパターンと理由を返す（モジュールの読み込みで落ちないように）。"""
    if not os.path.isfile(PRIVATE_LIST):
        return [], "ファイルがありません"
    try:
        lines = check_publish.private_lines(PRIVATE_LIST)
    except check_publish.PrivateListError as e:
        return [], f"読めません（{e}）"
    out, bad = [], []
    for no, s in lines:
        p = s[3:] if s.startswith("re:") else re.escape(s)
        try:
            re.compile(p)
        except re.error:
            bad.append(str(no))
            continue
        out.append(p)
    if bad:
        return out, f"{', '.join(bad)} 行目の正規表現が不正です（その行は検査していません）"
    return out, (None if out else "有効な行がありません")


# 末尾の境界は ASCII で判定する（\b は日本語の文字も単語の文字とみなし、「…\Users\<名前>の下」で外れる）。
# Git Bash の形（/c/Users/<名前>）も見る
PERSONAL_BUILTIN = (r"(?:[A-Za-z]:|(?<![A-Za-z0-9_.])/[A-Za-z])(\\\\|\\|/)Users(\\\\|\\|/)" + re.escape(USERNAME) +
                    r"(?![A-Za-z0-9_])", r"Claude_pzs8sxrjxfjjc", r"Packages(\\\\|\\|/)Claude")
# 表示用の名前（正規表現をそのまま出すと、中の利用者名が画面・ログに出る）
PERSONAL_LABELS = ("この PC の利用者のフォルダーのパス", "Claude のパッケージの名前", "Claude の仮想化フォルダー")
_PRIVATE, PRIVATE_ERROR = private_patterns()
PERSONAL = [re.compile(p, re.I) for p in (*PERSONAL_BUILTIN, *_PRIVATE)]
DEVPATH = re.compile(r"(?:C:|(?<![A-Za-z0-9_.])/c)(\\\\|\\|/)ClaudeCode", re.I)
# 第三者の文書（連絡先のメールアドレス・作者の環境のパスがあって当然の場所）。一般のメールアドレス・利用者名のパスの
# 検査から外す（非公開の一覧・PERSONAL_BUILTIN の検査は外さない）
THIRD_PARTY_TEXT = ("licenses/", "web/vendor/", "python/", "third_party_notices.txt", "third_party_notices.en.txt")

# ツール自体のライセンス（D-024: MIT）。著作権表示は利用者の指定どおり「Copyright (c) 2026 歌うＬＩＮＥ彼氏」
# （ＬＩＮＥ は全角）。表記の揺れ（半角・ローマ字）を防ぐため、コード上は文字コードで書く
COPYRIGHT = "Copyright (c) 2026 歌うＬＩＮＥ彼氏"
PLACEHOLDER = re.compile(r"<\s*COPYRIGHT[ _-]?HOLDERS?\s*>|<\s*名義\s*>", re.I)   # <COPYRIGHT HOLDER>・<名義>
NO_BOM = ("LICENSE.txt", "README.txt", "README.en.txt", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.en.txt", "MANIFEST.txt")
# 著作権表示が要る利用者向けの文書（日本語・英語）
COPYRIGHT_DOCS = ("README.txt", "README.en.txt", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.en.txt")


# D-025: DINOv2 のソースは Cell-DINO・X-Ray DINO が入る前のコミットに固定する。入った後の既知のコミット（旧の固定値）は使わない
DINOV2_FORBIDDEN_COMMITS = {"7764ea0f912e53c92e82eb78a2a1631e92725fc8"}
DINOV2_FORBIDDEN_FILES = re.compile(r"cell_dino|xray_dino|LICENSE_CELL_DINO|LICENSE_XRAY_DINO", re.I)


def dinov2_problems(low: dict[str, str], read) -> list[str]:
    """DINOv2 の固定コミットが、取得の定義（setup_sources.json・setup_manifest.json）・models_manifest.json・
    fetch_models.py で一致し、Cell-DINO・X-Ray DINO のコードや文書を取得の対象・ライセンスの表示に含まないこと（D-025）。"""
    import json as _json
    need = ["scripts/trellis/setup_sources.json", "scripts/trellis/setup_manifest.json",
            "scripts/trellis/models_manifest.json", "scripts/trellis/fetch_models.py"]
    if any(n not in low for n in need):
        return []                      # 必要なファイルの有無は REQUIRED で見る
    out = []
    try:
        srcs = _json.loads(read(low[need[0]]).decode("utf-8"))["sources"]["dinov2"]
        comp = next(c for c in _json.loads(read(low[need[1]]).decode("utf-8"))["components"] if c["id"] == "dinov2")
        models = _json.loads(read(low[need[2]]).decode("utf-8"))["dinov2"]
    except (ValueError, KeyError, StopIteration) as e:
        return [f"DINOv2 の固定値を読めません: {type(e).__name__}: {e}"]
    m = re.search(r'^DINOV2_COMMIT = "([0-9a-f]{40})"', read(low[need[3]]).decode("utf-8"), re.M)
    pinned = {
        "setup_sources.json": srcs.get("commit"),
        "setup_sources.json の url": (re.search(r"/archive/([0-9a-f]{40})\.zip$", srcs.get("url", "")) or [None, None])[1],
        "setup_manifest.json の version": comp.get("version"),
        "setup_manifest.json の PINNED_COMMIT": next(((s.get("write") or {}).get("PINNED_COMMIT", "").strip()
                                                    for s in comp["steps"] if s["type"] == "source_archive"), None),
        "setup_manifest.json の配布元": (re.search(r"/tree/([0-9a-f]{40})$", comp["source"].get("url", "")) or [None, None])[1],
        "models_manifest.json": models.get("commit"),
        "fetch_models.py": m.group(1) if m else None,
    }
    if len(set(pinned.values())) != 1:
        out.append("DINOv2 の固定コミットが一致しません: " + ", ".join(f"{k}={str(v)[:7]}" for k, v in pinned.items()))
    commit = srcs.get("commit")
    if commit in DINOV2_FORBIDDEN_COMMITS:
        out.append(f"DINOv2 の固定コミットが Cell-DINO・X-Ray DINO の入った版です（{commit[:7]}。D-025）")
    bad = [f for f in srcs.get("files", {}) if DINOV2_FORBIDDEN_FILES.search(f)]
    bad += [f for f in comp["license"].get("files", []) + ((comp.get("en") or {}).get("license") or {}).get("files", [])
            if DINOV2_FORBIDDEN_FILES.search(f)]
    bad += [f for f in low.values() if f.lower().startswith("licenses/") and DINOV2_FORBIDDEN_FILES.search(f)]
    if bad:
        out.append(f"DINOv2 の取得物・ライセンスの表示に Cell-DINO・X-Ray DINO のものがあります（D-025）: {bad[:3]}")
    if any(f.get("kind") == "noncommercial_files" for f in comp.get("flags", [])):
        out.append("DINOv2 に「非商用ライセンスのファイルを含む」の印が残っています（D-025）")
    return out


def license_file_problems(low: dict[str, str], read) -> list[str]:
    """セットアップの画面が全文を表示し同意の値を作る license.files（licenses\\ からの相対）が、全て配布物にあること。
    英語の画面で表示する en.license.files も同じ（2 言語対応 D-060）。無いと画面は「（ファイルが見つかりません）」のまま同意を求める。"""
    name = "scripts/trellis/setup_manifest.json"
    if name not in low:
        return []                      # 必要なファイルの有無は REQUIRED で見る
    try:
        comps = json.loads(read(low[name]).decode("utf-8"))["components"]
    except (ValueError, KeyError) as e:
        return [f"セットアップの定義を読めません: {type(e).__name__}: {e}"]
    out = []
    for c in comps:
        rels = list(c.get("license", {}).get("files", []))
        rels += [r for r in ((c.get("en") or {}).get("license") or {}).get("files", []) if r not in rels]
        for rel in rels:
            p = posixpath.normpath("licenses/" + rel)
            if p.startswith("../") or p.lower() not in low:
                out.append(f"セットアップのライセンスの全文がありません: {c.get('id')}: {p}")
    return out


def runtime_noncommercial_warnings(low: dict[str, str], read) -> list[str]:
    """TRELLIS 実行環境のパッケージの一覧（collect_runtime_licenses.py の出力）に非商用の条項のものがあるのに、
    セットアップの定義の packages に「非商用ライセンスのファイルを含む」の印が無いこと（D-004 設計メモ 2）。"""
    names = ("licenses/setup/trellis-runtime-packages.json", "scripts/trellis/setup_manifest.json")
    if any(n not in low for n in names):
        return []
    try:
        pkgs = json.loads(read(low[names[0]]).decode("utf-8"))
        comps = json.loads(read(low[names[1]]).decode("utf-8"))["components"]
    except (ValueError, KeyError):
        return []                      # 読めないことは license_file_problems・dinov2_problems で出る
    nc = sorted(set(pkgs.get("by_class", {}).get("noncommercial", [])) | set(pkgs.get("noncommercial_files", {})))
    comp = next((c for c in comps if c.get("id") == "packages"), {})
    if nc and not any(f.get("kind") == "noncommercial_files" for f in comp.get("flags", [])):
        return [f"TRELLIS 実行環境のパッケージに非商用の条項のものがあります（{', '.join(nc)}）が、セットアップの定義の "
                "packages に「非商用ライセンスのファイルを含む」の印（noncommercial_files）がありません"]
    return []


# .NET の単一ファイルのバンドル: apphost の中の署名（".net core bundle" の SHA-256）の直前 8 バイトが、
# バンドルの一覧（マニフェスト）の位置。EnableCompressionInSingleFile では中のアセンブリが raw Deflate で圧縮されていて、
# EXE のバイト列をそのまま探しても中の文字列は見えない
BUNDLE_SIGNATURE = bytes([0x8b, 0x12, 0x02, 0xb9, 0x6a, 0x61, 0x20, 0x38, 0x72, 0x7b, 0x93, 0x02, 0x14, 0xd7, 0xa0, 0x32,
                          0x13, 0xf5, 0xb9, 0xe6, 0xef, 0xae, 0x33, 0x18, 0xee, 0x3b, 0x2d, 0xce, 0x24, 0xb3, 0x6a, 0xae])


def bundle_entries(body: bytes) -> list[tuple[str, bytes]]:
    """単一ファイルの EXE の中のファイル [(パス, 中身（展開後）)]。読めなければ ValueError。"""
    def rstr(p):
        n = shift = 0
        while True:
            c = body[p]
            p += 1
            n |= (c & 0x7f) << shift
            shift += 7
            if not c & 0x80:
                return body[p:p + n].decode("utf-8"), p + n
    try:
        i = body.find(BUNDLE_SIGNATURE)
        if i < 8:
            raise ValueError("単一ファイルのバンドルの署名がありません")
        hdr = struct.unpack_from("<q", body, i - 8)[0]
        if not 0 < hdr < len(body):
            raise ValueError("バンドルの一覧の位置が範囲の外です")
        major, _minor, count = struct.unpack_from("<IIi", body, hdr)
        if major < 6 or not 0 < count < 100_000:
            raise ValueError(f"未対応のバンドルの版です（{major}、{count} 件）")
        _bundle_id, p = rstr(hdr + 12)
        p += 8 * 4 + 8                 # deps.json・runtimeconfig.json の位置と大きさ、flags
        out = []
        for _ in range(count):
            off, size, csize = struct.unpack_from("<qqq", body, p)
            p += 24 + 1                # 種類（1 バイト）
            path, p = rstr(p)
            data = body[off:off + (csize or size)]
            out.append((path, zlib.decompress(data, -15) if csize else data))
        return out
    except (struct.error, IndexError, UnicodeError, zlib.error) as e:
        raise ValueError(f"バンドルの一覧を読めません: {type(e).__name__}: {e}") from None


def dev_needles() -> list[tuple[bytes, str]]:
    """実行ファイルのバイト列で探す開発環境のパス [(バイト列, 表示用の名前)]（表示に利用者名を出さない）。"""
    home = f"\\Users\\{USERNAME}\\"
    return [(home.encode(), "この PC の利用者のフォルダーのパス"), (home.encode("utf-16-le"), "この PC の利用者のフォルダーのパス"),
            (b"ClaudeCode", "ClaudeCode"), ("ClaudeCode".encode("utf-16-le"), "ClaudeCode")]


def embedded_problems(name: str, body: bytes) -> list[str]:
    """実行ファイル・アセンブリ（と中身がバイナリーのファイル）のバイト列に、開発環境のパス・個人を特定できる文字列が
    埋め込まれていないか。"""
    for needle, what in dev_needles():
        if needle in body:
            return [f"実行ファイルに開発環境のパスが埋め込まれています（{what}）: {name}"]
    out = []
    texts = (body.decode("utf-8", errors="replace"), body[:len(body) // 2 * 2].decode("utf-16-le", errors="replace"),
             body[1:1 + (len(body) - 1) // 2 * 2].decode("utf-16-le", errors="replace"))
    for i, rx in enumerate(PERSONAL):
        if any(rx.search(t) for t in texts):
            what = PERSONAL_LABELS[i] if i < len(PERSONAL_BUILTIN) else "非公開の一覧の文字列"
            out.append(f"実行ファイルに個人を特定できる文字列が埋め込まれています（{what}）: {name}")
    return out


def _decode_text(body: bytes) -> str | None:
    """中身で判定した文字列。BOM で UTF-8・UTF-16LE・UTF-16BE を見分け、BOM が無く先頭 8 KB に NUL があるものは、NUL の位置の
    偏り（ASCII の UTF-16 は片方の位置だけが NUL）で UTF-16 を見分ける。どれでもなければバイナリー（None）。
    Windows PowerShell 5.1 の > ・メモ帳の「Unicode」は BOM 付きの UTF-16LE。"""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if body.startswith(bom):
            return body[len(bom):].decode(enc, errors="replace")
    head = body[:8192]
    if b"\0" not in head:
        return body.decode("utf-8", errors="replace")
    even, odd = head[0::2].count(0), head[1::2].count(0)
    if odd >= len(head) // 8 and even <= odd // 20:
        return body[:len(body) // 2 * 2].decode("utf-16-le", errors="replace")
    if even >= len(head) // 8 and odd <= even // 20:
        return body[:len(body) // 2 * 2].decode("utf-16-be", errors="replace")
    return None


def path_hits(rel: str) -> list[str]:
    """パス（ファイル名・フォルダー名）の中の個人を特定できる文字列（PERSONAL・メールアドレス）。"""
    hits = [m.group(0) for rx in PERSONAL for m in rx.finditer(rel) if m.group(0)]
    return hits + [m.group(0) for m in check_publish.EMAIL.finditer(rel) if not check_publish.EMAIL_OK.search(m.group(0))]


def license_problems(text: str) -> list[str]:
    """MIT の LICENSE の中身の問題（名義の表記・仮の文字列）。"""
    out = []
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    if not lines or lines[0].strip() != "MIT License":
        out.append("LICENSE の 1 行目が「MIT License」ではありません")
    holders = [ln for ln in lines if ln.startswith("Copyright")]
    if holders != [COPYRIGHT]:
        out.append(f"LICENSE の著作権表示が「{COPYRIGHT}」と一致しません（{holders}）")
    if PLACEHOLDER.search(text):
        out.append("LICENSE に仮の文字列（<COPYRIGHT HOLDER> など）が残っています")
    if "Permission is hereby granted, free of charge" not in text or 'THE SOFTWARE IS PROVIDED "AS IS"' not in text:
        out.append("LICENSE が MIT License の本文ではありません")
    return out


def _match(path: str, pat: str) -> bool:
    """パスの区切りを考えた照合（"a/**" は a の下全部、それ以外は段の数が同じで段ごとに fnmatch）。"""
    if pat.endswith("/**"):
        return path.startswith(pat[:-2])
    ps, qs = path.split("/"), pat.split("/")
    return len(ps) == len(qs) and all(fnmatch.fnmatch(a, b) for a, b in zip(ps, qs))


def _entries(target: str) -> tuple[dict[str, int], callable, list[str]]:
    """{相対パス: 大きさ}、中身を読む関数、ZIP 固有の問題。"""
    problems = []
    if target.lower().endswith(".zip"):
        z = zipfile.ZipFile(target)
        names = [i for i in z.infolist() if not i.is_dir()]
        tops = {i.filename.split("/", 1)[0] for i in names}
        for i in names:
            n = i.filename
            if n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/") or "\\" in n:
                problems.append(f"ZIP に安全でないパスがあります: {n}")
        if len(tops) != 1:
            problems.append(f"ZIP の中身が 1 つのフォルダーの下にありません: {sorted(tops)[:5]}")
        top = next(iter(tops)) + "/" if len(tops) == 1 else ""
        if path_hits(top):
            problems.append("個人を特定できる文字列（ZIP の最上位のフォルダー名）")
        files = {i.filename[len(top):]: i.file_size for i in names}
        return files, (lambda rel: z.read(top + rel)), problems
    files = {}
    for dp, dn, fn in os.walk(target):
        for f in fn:
            p = os.path.join(dp, f)
            files[os.path.relpath(p, target).replace(os.sep, "/")] = os.path.getsize(p)

    def read(rel):
        with open(os.path.join(target, rel), "rb") as fh:
            return fh.read()
    return files, read, problems


def check(target: str) -> tuple[list[str], list[str], dict]:
    files, read, errors = _entries(target)
    warnings: list[str] = []
    if not _PRIVATE:
        warnings.append(f"非公開の一覧（{PRIVATE_LIST}）: {PRIVATE_ERROR}。開発者のメールアドレス・Steam の ID などの"
                        "非公開の文字列は検査していません（docs/publish_checklist.md 5 章）")
    elif PRIVATE_ERROR:
        warnings.append(f"非公開の一覧（{PRIVATE_LIST}）: {PRIVATE_ERROR}")
    low = {f.lower(): f for f in files}
    for r in REQUIRED:
        if r.lower() not in low:
            errors.append(f"必要なファイルがありません: {r}")
    hidden: set[str] = set()           # パスに入った個人を特定できる文字列（表示では伏せる）
    for rel in sorted(files):
        lrel = rel.lower()
        third = lrel.startswith(THIRD_PARTY)
        parts = lrel.split("/")
        name = parts[-1]
        ext = os.path.splitext(name)[1]
        hits = path_hits(rel)
        if hits:
            hidden.update(hits)
            errors.append(f"個人を特定できる文字列（ファイル名・フォルダー名）: {rel}")
        if not third:
            bad_dir = next((p for p in parts[:-1] if p in FORBIDDEN_DIRS), None)
            if bad_dir:
                errors.append(f"開発用・作業用のフォルダー（{bad_dir}）: {rel}")
            if any(fnmatch.fnmatch(name, pat) for pat in FORBIDDEN_FILES):
                errors.append(f"開発用のファイル: {rel}")
            # 鍵・ローカルの設定・非公開の一覧（公開前の検査と同じ一覧）
            if ext in check_publish.SECRET_EXT or any(fnmatch.fnmatchcase(name, pat) for pat in check_publish.SECRET_NAMES):
                errors.append(f"鍵・ローカルの設定・非公開の一覧などのファイル: {rel}")
        elif "__pycache__" in parts or name.endswith(".pyc"):
            errors.append(f"キャッシュ（__pycache__）: {rel}")
        if any(_match(lrel, pat) for pat in DEV_SCRIPTS):
            errors.append(f"開発用のスクリプト・ソース: {rel}")
        if ext in GAME_EXT:
            errors.append(f"ゲームのファイル（{ext}）: {rel}")
        if ext in MODEL_EXT and not third:
            errors.append(f"3D モデル・AI の重み・共有ファイル（{ext}）: {rel}")
        if ext in ARCHIVE_EXT and lrel != "python/python311.zip" and not (third and ext in (".gz", ".zip", ".tar")):
            errors.append(f"アーカイブ（取得物・作業の残りの可能性）: {rel}")
        if ext in IMAGE_EXT and not third and not lrel.startswith(IMAGE_OK):
            errors.append(f"画像（見本画像・生成結果の可能性）: {rel}")
        if SAMPLE_NAMES.search(lrel):
            errors.append(f"サンプル・見本・試作の名前: {rel}")
        # 文字列の検査は拡張子ではなく中身で決める（UTF-16 の文書・.yaml・拡張子の無いファイルも見る）。中身がバイナリーの
        # ものは実行ファイルと同じく UTF-8・UTF-16LE の両方で探す（python/ はハッシュを照合した python.org の物なので除く）
        text = None
        if (not third and files[rel] < 8_000_000 and ext not in BINARY_EXT
                and ext not in GAME_EXT | MODEL_EXT | IMAGE_EXT | ARCHIVE_EXT):
            body = read(rel)
            text = _decode_text(body)
            if text is None and not lrel.startswith("python/"):
                errors += embedded_problems(rel, body)
        if text is not None:
            # 表記を変えただけの形（JSON の \uXXXX・HTML の文字参照・URL の %XX・全角）を戻した文字列も見る
            plain = check_publish.unescape_line(text)
            variants = (text, plain) if plain != text else (text,)
            for i, rx in enumerate(PERSONAL):
                if any(rx.search(t) for t in variants):
                    what = PERSONAL_LABELS[i] if i < len(PERSONAL_BUILTIN) else "非公開の一覧の文字列"
                    errors.append(f"個人を特定できる文字列（{what}）: {rel}")
            if not lrel.startswith(THIRD_PARTY_TEXT):
                m = next((m for t in variants for m in check_publish.EMAIL.finditer(t)
                          if not check_publish.EMAIL_OK.search(m.group(0))), None)
                if m:
                    errors.append(f"個人を特定できる文字列（メールアドレス {check_publish.mask(m.group(0))}）: {rel}")
                m = next((m for t in variants for rx in (check_publish.USERPATH, check_publish.MACPATH)
                          for m in rx.finditer(t) if m.group(1).lower() not in check_publish.USER_OK), None)
                if m:
                    errors.append(f"個人を特定できる文字列（利用者名を含むパス。名前 {check_publish.mask(m.group(1))}）: {rel}")
                m = check_publish.TOKENS.search(text)
                if m:
                    errors.append(f"トークン・鍵らしき文字列 {check_publish.mask(m.group(0))}: {rel}")
            if PLACEHOLDER.search(text) and not lrel.startswith("licenses/"):
                errors.append(f"仮の文字列（<COPYRIGHT HOLDER> など）が残っています: {rel}")
            if DEVPATH.search(text) and not lrel.startswith("licenses/"):
                # D-027: 配布物に入るスクリプトに開発環境の既定のパスを残さない（2026-10-16 に警告から問題に変更）
                errors.append(f"開発環境の既定のパス（C:\\ClaudeCode）が残っています: {rel}")
        if ext in BINARY_EXT and not lrel.startswith("python/") and not third:
            body = read(rel)
            if lrel == "gdskinstudio.exe":
                # 単一ファイルの EXE 全体は開発環境のパスだけを探す（中のアセンブリは下で展開して見る）
                what = next((w for needle, w in dev_needles() if needle in body), None)
                if what:
                    errors.append(f"実行ファイルに開発環境のパスが埋め込まれています（{what}）: {rel}")
            else:
                errors += embedded_problems(rel, body)
            # 単一ファイルの EXE の中のアプリのアセンブリ（GDSkinStudio.dll・<言語>/GDSkinStudio.resources.dll）は
            # 圧縮されていて上の検索では見えないので、展開して見る。読めなければ素通りさせない
            if lrel == "gdskinstudio.exe":
                try:
                    app = [(p, d) for p, d in bundle_entries(body) if p.rsplit("/", 1)[-1].lower().startswith("gdskinstudio.")]
                    if not any(p.lower() == "gdskinstudio.dll" for p, _ in app):
                        raise ValueError("GDSkinStudio.dll がバンドルの一覧にありません")
                except ValueError as e:
                    errors.append(f"EXE の中のアセンブリを検査できません（{e}）: {rel}")
                    app = []
                for p, d in app:
                    errors += embedded_problems(f"{rel} の中の {p}", d)
                # ヘルプの文は GDSkinStudio.dll の埋め込みのリソースにだけある。項目が消える・名前が変わる（LogicalName の
                # 打ち間違い、help.ja.md への改名で衛星アセンブリに移るなど）と、発行は通るがヘルプの画面は読めないという文だけに
                # なる（2026-11-18 TSG-02）。名前（UTF-8）は DLL の中のリソースの一覧にだけ現れる（コードは実行時に名前を組み立て、
                # C# の文字列は UTF-16 で入る）。文には「の中の GDSkinStudio.dll」「検査できません」を使わない（中身の検査・
                # 読めないバンドルの文を選ぶ tests/test_publish_checks.py の KEEP_EXE に当たり、リソースの無い偽の DLL の試験が落ちる）
                dll = next((d for p, d in app if p.lower() == "gdskinstudio.dll"), None)
                for res in HELP_RESOURCES:
                    if dll is not None and res not in dll:
                        errors.append(f"ヘルプの文（{res.decode()}）が EXE に埋め込まれていません: {rel}")
            # 英語の画面（Strings.en.resx）の衛星アセンブリ: 単一ファイルの発行では exe の中の一覧に入る（隣の en フォルダーでもよい）。
            # 無いと、設定で English を選んでも日本語のまま（2026-10-17 点検 UG-R6。作業フォルダーへの発行で exe の中にあることを確認）
            if lrel == "gdskinstudio.exe" and SATELLITE_EN not in body and SATELLITE_EN.decode().lower() not in low:
                errors.append(f"英語の画面の衛星アセンブリ（{SATELLITE_EN.decode()}）が入っていません: {rel}")
    errors += dinov2_problems(low, read)
    errors += license_file_problems(low, read)
    warnings += runtime_noncommercial_warnings(low, read)
    # ツール自体のライセンス（D-024）: LICENSE.txt の名義、README・THIRD_PARTY_NOTICES の表示、BOM なし、改行は CRLF
    for name in NO_BOM:
        if name.lower() not in low:
            continue
        body = read(low[name.lower()])
        if body.startswith(b"\xef\xbb\xbf"):
            errors.append(f"BOM があります（UTF-8、BOM なしにする）: {name}")
        rest = body.replace(b"\r\n", b"")
        if b"\r" in rest or b"\n" in rest:
            errors.append(f"改行が CRLF ではない箇所があります（\\r\\r\\n・単独の \\r や \\n）: {name}")
    if "license.txt" in low:
        errors += [p + "（LICENSE.txt）" for p in license_problems(read(low["license.txt"]).decode("utf-8", errors="replace"))]
    for name in COPYRIGHT_DOCS:
        if name.lower() in low and COPYRIGHT not in read(low[name.lower()]).decode("utf-8", errors="replace"):
            errors.append(f"著作権表示「{COPYRIGHT}」がありません: {name}")
    by_top: dict[str, int] = {}
    for rel, size in files.items():
        top = rel.split("/", 1)[0] if "/" in rel else "(直下)"
        by_top[top] = by_top.get(top, 0) + size
    info = {"files": len(files), "bytes": sum(files.values()), "by_top": dict(sorted(by_top.items(), key=lambda kv: -kv[1]))}
    # パスに入った個人を特定できる文字列は、どの文でも伏せて表示する
    for v in sorted(hidden, key=len, reverse=True):
        errors = [re.sub(re.escape(v), lambda _m, v=v: check_publish.mask(v), e, flags=re.I) for e in errors]
        info["by_top"] = {re.sub(re.escape(v), lambda _m, v=v: check_publish.mask(v), k, flags=re.I): n
                          for k, n in info["by_top"].items()}
    return errors, warnings, info


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    target = sys.argv[1]
    errors, warnings, info = check(target)
    print(f"検査: {target}")
    print(f"  ファイル {info['files']:,} 件、合計 {info['bytes'] / 1e6:,.1f} MB")
    for k, v in info["by_top"].items():
        print(f"    {k:28} {v / 1e6:10,.1f} MB")
    for w in warnings:
        print("  警告: " + w)
    for e in errors:
        print("  問題: " + e)
    print("  結果: " + ("問題なし" if not errors else f"問題 {len(errors)} 件"))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
