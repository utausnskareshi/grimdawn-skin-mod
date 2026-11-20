"""配布用 ZIP を作る（計画書フェーズ6、docs/packaging.md）。

    python scripts/package/build_package.py [--out build/package] [--no-zip]

手順:
  1. WPF アプリを .NET 10 の自己完結型・単一ファイル（圧縮）で発行する（dotnet publish）
  2. Python 3.11.9 の embeddable 版（python.org、SHA-256 と MD5 を照合）を展開し、バックエンドの依存
     （numpy・scipy・Pillow・lz4・fast-simplification・xatlas）をハッシュを照合して入れる
  3. バックエンド・変換・MOD 作成・生成スクリプト・セットアップの定義を、許可した一覧のとおりにコピーする
  4. ライセンスの全文（licenses\\）、第三者の著作権表示（THIRD_PARTY_NOTICES.txt）、利用者向けの README.txt、
     LICENSE.txt（D-024: MIT。リポジトリ直下の LICENSE を CRLF にしたもの）、配布物の目印（gdskin_dist.json）、
     中身の一覧（MANIFEST.txt）を書く（どれも UTF-8、BOM なし）
  5. 配布物の Python で読み込みを確かめ（-B でキャッシュを作らない）、check_package.py で検査してから ZIP にする

作るもの: ``<out>\\GDSkinStudio-<版>-win-x64\\``（展開後と同じ中身）と ``<out>\\GDSkinStudio-<版>-win-x64.zip``。
ゲームのファイル・サンプル・TRELLIS の見本画像・試作のビルド・テスト・開発用のスクリプトは入れない（許可した一覧の
外はコピーしない。一覧のフォルダーの中に git で追跡していないファイルがあれば止める。check_package.py でも確かめる）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import check_package  # noqa: E402

PY_EMBED = {
    "version": "3.11.9",
    "url": "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip",
    "size": 11249023,
    "sha256": "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b",
    "md5": "6d9aa08531d48fcc261ba667e2df17c4",       # python.org の公開値（API の release_file）
}
# python311._pth: 標準ライブラリ（zip）・python フォルダー・site-packages だけを探す（利用者の環境変数・ユーザーの
# site-packages は見ない。バックエンドは -I でも起動する）
PTH = "python311.zip\n.\nLib\\site-packages\n\n# GD Skin Studio: run site (enables .pth files in site-packages)\nimport site\n"

FRONTEND = os.path.join(REPO, "app", "frontend", "GDSkinStudio", "GDSkinStudio.csproj")

# 許可した一覧（これ以外はコピーしない）
COPY_TREES = [
    ("app/backend/gdskin_backend", "app/backend/gdskin_backend"),
    ("src/gdmesh", "src/gdmesh"),
    ("src/gdconvert", "src/gdconvert"),
    ("src/modbuilder", "src/modbuilder"),
    ("licenses/setup", "licenses/setup"),
    ("licenses/bundled/dotnet", "licenses/bundled/dotnet"),
]
COPY_FILES = [
    "app/backend/run_backend.py", "app/backend/worker.py", "app/backend/bg_preview.py",
    # 3D 生成（TRELLIS 実行環境の Python で動く）: generate_glb.py が postprocess_alt・pose_check・measure・
    # gen_helpers・prompt_fit・fetch_models（固定値）を読み込む
    "scripts/trellis/generate_glb.py", "scripts/trellis/postprocess_alt.py", "scripts/trellis/pose_check.py",
    "scripts/trellis/measure.py", "scripts/trellis/gen_helpers.py", "scripts/trellis/prompt_fit.py",
    # 見た目の作り直し（SF10。--variant-base のときだけ generate_glb.py が読み込む）
    "scripts/trellis/variant_base.py",
    # セットアップ（アプリが読む定義と、手動で構築するときのスクリプト。開発用の -Mode dev の部品は入れない）
    "scripts/trellis/fetch_models.py", "scripts/trellis/setup_manifest.json", "scripts/trellis/setup_sources.json",
    "scripts/trellis/models_manifest.json", "scripts/trellis/models_manifest_text.json",
    "scripts/trellis/requirements-trellis-runtime.lock.txt", "scripts/trellis/requirements-trellis-runtime.hashes.txt",
    "scripts/trellis/requirements-trellis-sdist.hashes.txt", "scripts/trellis/requirements-trellis-torch.hashes.txt",
    "scripts/trellis/setup_trellis_env.ps1", "scripts/trellis/notices/clip_model_card_notice.ja.txt",
    # 英語の画面の CLIP の注意文（2026-10-18 未決定 #2。画面・fetch_models.py が表示言語で選ぶ）
    "scripts/trellis/notices/clip_model_card_notice.en.txt",
]
SKIP_NAMES = {"__pycache__", ".pytest_cache", "tests"}


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def version() -> str:
    init = open(os.path.join(REPO, "app", "backend", "gdskin_backend", "__init__.py"), encoding="utf-8").read()
    v = re.search(r'__version__ = "([^"]+)"', init).group(1)
    csproj = open(FRONTEND, encoding="utf-8").read()
    cv = re.search(r"<Version>([^<]+)</Version>", csproj).group(1)
    if v != cv:
        raise SystemExit(f"版が食い違っています: バックエンド {v}、アプリ {cv}")
    return v


def read_license() -> str:
    """リポジトリ直下の LICENSE（D-024: MIT）を読み、名義が決定どおりで仮の文字列が無いことを確かめる。
    アプリの EXE の著作権表示（csproj の <Copyright>）も同じ表記であることを確かめる。"""
    raw = open(os.path.join(REPO, "LICENSE"), "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("LICENSE に BOM があります（UTF-8、BOM なしにする）")
    # 改行は LF にそろえる（書き出しで CRLF にするので、CRLF のままだと \r\r\n になる。.gitattributes は改行を変えない）
    text = raw.decode("utf-8").replace("\r\n", "\n")
    if "\r" in text:
        raise SystemExit("LICENSE に単独の CR（\\r）の改行があります（LF か CRLF にする）")
    problems = check_package.license_problems(text)
    csproj = open(FRONTEND, encoding="utf-8-sig").read()
    m = re.search(r"<Copyright>([^<]*)</Copyright>", csproj)
    if not m or m.group(1) != check_package.COPYRIGHT:
        problems.append(f"アプリの csproj の <Copyright> が「{check_package.COPYRIGHT}」ではありません（{m.group(1) if m else 'なし'}）")
    if problems:
        raise SystemExit("ライセンスの表示に問題があります: " + "; ".join(problems))
    return text


def run(cmd: list[str], **kw) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


# ---------------------------------------------------------------------------
def publish(stage: str, work: str) -> dict:
    out = os.path.join(work, "publish")
    shutil.rmtree(out, ignore_errors=True)
    run(["dotnet", "publish", FRONTEND, "-c", "Release", "-r", "win-x64", "--self-contained", "true",
         "-p:PublishSingleFile=true", "-p:EnableCompressionInSingleFile=true", "-p:DebugType=none", "-p:DebugSymbols=false",
         "-p:GenerateDocumentationFile=false", "-o", out])
    for f in os.listdir(out):
        p = os.path.join(out, f)
        if f.lower().endswith((".pdb", ".xml")):
            continue                          # デバッグ情報・WebView2 の API 文書
        if f == "runtimes":
            continue                          # WebView2Loader.dll の重複（直下にある）
        dst = os.path.join(stage, f)
        (shutil.copytree if os.path.isdir(p) else shutil.copy2)(p, dst)
    # 使った .NET ランタイムの版（ライセンス・表示用）
    assets = json.load(open(os.path.join(os.path.dirname(FRONTEND), "obj", "project.assets.json"), encoding="utf-8"))
    deps = [d for fw in assets["project"]["frameworks"].values() for d in fw.get("downloadDependencies", [])]
    runtime = sorted(d["version"].strip("[]").split(",")[0].strip() for d in deps
                     if d["name"].lower() == "microsoft.netcore.app.runtime.win-x64")
    webview = sorted(k.split("/")[1] for k in assets["libraries"] if k.lower().startswith("microsoft.web.webview2/"))
    return {"dotnet_runtime": runtime[-1] if runtime else None, "webview2_sdk": webview[-1] if webview else None}


def embed_python(stage: str, cache: str) -> None:
    os.makedirs(cache, exist_ok=True)
    zpath = os.path.join(cache, os.path.basename(PY_EMBED["url"]))
    if not (os.path.isfile(zpath) and sha256(zpath) == PY_EMBED["sha256"]):
        log(f"取得: {PY_EMBED['url']}")
        with urllib.request.urlopen(PY_EMBED["url"], timeout=120) as r, open(zpath + ".part", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(zpath + ".part", zpath)
    data = open(zpath, "rb").read()
    if len(data) != PY_EMBED["size"] or hashlib.sha256(data).hexdigest() != PY_EMBED["sha256"] \
            or hashlib.md5(data).hexdigest() != PY_EMBED["md5"]:
        raise SystemExit("Python の embeddable 版が記録（大きさ・SHA-256・MD5）と合いません")
    pydir = os.path.join(stage, "python")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(pydir)
    with open(os.path.join(pydir, "python311._pth"), "w", encoding="ascii", newline="\r\n") as f:
        f.write(PTH)
    site = os.path.join(pydir, "Lib", "site-packages")
    os.makedirs(site)
    run([sys.executable, "-X", "utf8", "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--no-compile",
         "--require-hashes", "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.11",
         "--implementation", "cp", "--abi", "cp311", "--target", site, "-r", os.path.join(HERE, "backend-requirements.txt")])
    shutil.rmtree(os.path.join(site, "bin"), ignore_errors=True)     # コマンドの入口（使わない）
    for whl in glob.glob(os.path.join(site, "*.whl")):                 # pip が残すことがあるホイールのファイル
        os.remove(whl)


def untracked_copies() -> list[str]:
    """コピーする範囲（COPY_TREES・COPY_FILES）にある、git で追跡していないファイル（.gitignore で無視したもの・鍵・作業の残りを
    含む）。copytree は作業フォルダーを丸ごとコピーするので、許可した一覧の外のファイルが配布物に入らないように止める。
    SKIP_NAMES・.pyc/.pyo はコピーしないので除く。git を使えなければ SystemExit。"""
    try:
        r = subprocess.run(["git", "-C", REPO, "ls-files", "-z", "--cached", "--", *[s for s, _ in COPY_TREES], *COPY_FILES],
                           capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"git の一覧を読めません（コピーするファイルが追跡中か確かめられません）: {e}") from None
    tracked = set(r.stdout.decode("utf-8").split("\0"))
    out = []
    for src, _ in COPY_TREES:
        for dp, dn, fn in os.walk(os.path.join(REPO, src)):
            dn[:] = [d for d in dn if d not in SKIP_NAMES]
            for f in fn:
                rel = os.path.relpath(os.path.join(dp, f), REPO).replace(os.sep, "/")
                if not f.endswith((".pyc", ".pyo")) and rel not in tracked:
                    out.append(rel)
    return sorted(out + [rel for rel in COPY_FILES if rel not in tracked])


def require_tracked() -> None:
    bad = untracked_copies()
    if bad:
        raise SystemExit(f"配布物にコピーする範囲に、git で追跡していないファイルが {len(bad)} 件あります（.gitignore で無視したものを"
                         "含む。要らなければ消し、配布物に入れるなら git add する）: " + ", ".join(bad[:10]))


def copy_app(stage: str) -> None:
    require_tracked()

    def ignore(d, names):
        return [n for n in names if n in SKIP_NAMES or n.endswith((".pyc", ".pyo"))]
    for src, dst in COPY_TREES:
        shutil.copytree(os.path.join(REPO, src), os.path.join(stage, dst), ignore=ignore)
    for rel in COPY_FILES:
        dst = os.path.join(stage, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), dst)


# ---------------------------------------------------------------------------
# ライセンス
# ---------------------------------------------------------------------------
def _nuget_folders(assets_path: str | None = None) -> list[str]:
    """NuGet のパッケージを探すフォルダー。発行で使った project.assets.json の packageFolders の順（先頭がグローバル、
    続いてフォールバック）。NUGET_PACKAGES・nuget.config の globalPackagesFolder で ~/.nuget/packages 以外になる。
    gdskin_dist.json には書かない（利用者名を含むパスのため）。"""
    folders = []
    try:
        with open(assets_path or os.path.join(os.path.dirname(FRONTEND), "obj", "project.assets.json"), encoding="utf-8") as f:
            assets = json.load(f)
        folders += list(assets.get("packageFolders") or {})
        folders.append(((assets.get("project") or {}).get("restore") or {}).get("packagesPath") or "")
    except (OSError, ValueError):
        pass
    folders += [os.environ.get("NUGET_PACKAGES") or "", os.path.join(os.path.expanduser("~"), ".nuget", "packages")]
    return list(dict.fromkeys(os.path.normpath(f) for f in folders if f))


def _nuget(pkg: str, ver: str, folders: list[str] | None = None) -> str:
    folders = _nuget_folders() if folders is None else folders
    for d in folders:
        p = os.path.join(d, pkg.lower(), ver)
        if os.path.isdir(p):
            return p
    raise SystemExit(f"NuGet のパッケージ {pkg} {ver} が見つかりません（探した場所: {', '.join(folders)}）")


def bundled_licenses(stage: str, versions: dict) -> list[dict]:
    """同梱するもののライセンスを licenses\\bundled\\ に集め、THIRD_PARTY_NOTICES 用の一覧を返す。"""
    lic = os.path.join(stage, "licenses", "bundled")
    rows = []

    def put(sub: str, src: str, name: str | None = None) -> str:
        d = os.path.join(lic, sub)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, name or os.path.basename(src))
        shutil.copy2(src, dst)
        return os.path.relpath(dst, stage).replace(os.sep, "/")

    rt = versions["dotnet_runtime"]
    files = [put("dotnet", os.path.join(_nuget("microsoft.netcore.app.runtime.win-x64", rt), "LICENSE.TXT"), "LICENSE.txt"),
             put("dotnet", os.path.join(_nuget("microsoft.netcore.app.runtime.win-x64", rt), "THIRD-PARTY-NOTICES.TXT"),
                 "runtime-THIRD-PARTY-NOTICES.txt"),
             put("dotnet", os.path.join(_nuget("microsoft.windowsdesktop.app.runtime.win-x64", rt), "LICENSE"),
                 "windowsdesktop-LICENSE.txt")]
    files += sorted(os.path.relpath(p, stage).replace(os.sep, "/") for p in glob.glob(os.path.join(lic, "dotnet", "*-THIRD-PARTY-NOTICES.txt"))
                    if "runtime-" not in os.path.basename(p))
    # name・license は日本語、name_en・license_en は英語の THIRD_PARTY_NOTICES.en.txt 用（2 言語対応。D-060）
    rows.append({"name": f".NET {rt}（ランタイム・WPF・Windows Forms。自己完結型としてアプリに同梱）", "license": "MIT（同梱の第三者のコードは各ライセンス）",
                 "name_en": f".NET {rt} (runtime, WPF, Windows Forms; bundled with the app as self-contained)",
                 "license_en": "MIT (the bundled third-party code is under its own licenses)",
                 "source": "https://github.com/dotnet/runtime", "files": files})
    wv = versions["webview2_sdk"]
    files = [put("webview2", os.path.join(_nuget("microsoft.web.webview2", wv), "LICENSE.txt")),
             put("webview2", os.path.join(_nuget("microsoft.web.webview2", wv), "NOTICE.txt"))]
    rows.append({"name": f"Microsoft Edge WebView2 SDK {wv}（Microsoft.Web.WebView2）", "license": "BSD-3-Clause 形式（Microsoft）",
                 "name_en": f"Microsoft Edge WebView2 SDK {wv} (Microsoft.Web.WebView2)", "license_en": "BSD-3-Clause style (Microsoft)",
                 "source": "https://www.nuget.org/packages/Microsoft.Web.WebView2", "files": files})
    three_ver = re.search(r"r\d+", open(os.path.join(stage, "web", "vendor", "three", "README.txt"), encoding="utf-8").read())
    three = three_ver.group(0) if three_ver else ""
    rows.append({"name": f"three.js {three}（3D プレビュー。web\\vendor\\three）", "license": "MIT",
                 "name_en": f"three.js {three} (3D preview; web\\vendor\\three)", "license_en": "MIT",
                 "source": "https://github.com/mrdoob/three.js", "files": [put("three.js", os.path.join(stage, "web", "vendor", "three", "LICENSE"))]})
    rows.append({"name": f"Python {PY_EMBED['version']}（embeddable 版。python\\）", "license": "PSF-2.0（同梱の OpenSSL・libffi・SQLite・bzip2・xz・zlib などの各ライセンスを含む）",
                 "name_en": f"Python {PY_EMBED['version']} (embeddable package; python\\)",
                 "license_en": "PSF-2.0 (including the licenses of the bundled OpenSSL, libffi, SQLite, bzip2, xz, zlib and others)",
                 "source": "https://www.python.org/", "files": [put("python", os.path.join(stage, "python", "LICENSE.txt"))]})
    site = os.path.join(stage, "python", "Lib", "site-packages")
    for di in sorted(glob.glob(os.path.join(site, "*.dist-info"))):
        meta = open(os.path.join(di, "METADATA"), encoding="utf-8", errors="replace").read()
        name = re.search(r"^Name: (.+)$", meta, re.M).group(1).strip()
        ver = re.search(r"^Version: (.+)$", meta, re.M).group(1).strip()
        lexpr = re.search(r"^License-Expression: (.+)$", meta, re.M)
        lname = lexpr.group(1).strip() if lexpr else None
        if not lname:
            cls = re.findall(r"^Classifier: License :: (?:OSI Approved :: )?(.+)$", meta, re.M)
            lname = " / ".join(c.strip() for c in cls) or (re.search(r"^License: (.+)$", meta, re.M) or [None, None])[1]
        home = _home_url(meta)
        files = []
        for p in sorted(glob.glob(os.path.join(di, "**", "*"), recursive=True)):
            b = os.path.basename(p).upper()
            if os.path.isfile(p) and (any(k in b for k in ("LICEN", "COPYING", "NOTICE", "AUTHORS")) or os.sep + "licenses" + os.sep in p.lower()):
                rel = os.path.relpath(p, di)
                files.append(put(os.path.join("python-packages", f"{name}-{ver}"), p, rel.replace(os.sep, "_")))
        rows.append({"name": f"{name} {ver}（python\\Lib\\site-packages）", "license": lname or "不明",
                     "name_en": f"{name} {ver} (python\\Lib\\site-packages)", "license_en": lname or "unknown",
                     "source": home or f"https://pypi.org/project/{name}/", "files": files})
    return rows


def _home_url(meta: str) -> str | None:
    """METADATA の配布元の URL（Home-page: URL、または Project-URL: Source/Homepage/Repository, URL）。
    区切りは Home-page が「:」、Project-URL が「,」。改行をまたがず、http(s) の URL だけを採る（空・UNKNOWN は無し）。"""
    m = re.search(r"^(?:Home-page:[ \t]*|Project-URL:[^,\n]*(?:Source|Homepage|Repository)[^,\n]*,[ \t]*)(https?://\S+)[ \t]*$",
                  meta, re.M | re.I)
    return m.group(1) if m else None


def setup_display(c: dict, lang: str) -> tuple[str, dict, list[dict]]:
    """setup_manifest.json の部品の表示（名前・ライセンス・注意）。英語は部品の "en" の項目で置き換える（無い項目は日本語のまま）。"""
    if lang != "en":
        return c["name"], c["license"], c.get("flags", [])
    e = c.get("en") or {}
    lic = {**c["license"], **(e.get("license") or {})}
    en_flags = e.get("flags") or []
    flags = [{**f, **(en_flags[i] if i < len(en_flags) else {})} for i, f in enumerate(c.get("flags", []))]
    return e.get("name") or c["name"], lic, flags


def third_party_notices(stage: str, rows: list[dict], ver: str) -> None:
    """THIRD_PARTY_NOTICES.txt（日本語）と THIRD_PARTY_NOTICES.en.txt（英語。2 言語対応 D-060）を書く。"""
    setup = json.load(open(os.path.join(REPO, "scripts", "trellis", "setup_manifest.json"), encoding="utf-8"))
    # パッケージの件数は一覧（collect_runtime_licenses.py の出力）から取る。文に書くと一覧の更新で食い違う（点検 FG02-R3）
    pkg_count = json.load(open(os.path.join(REPO, "licenses", "setup", "trellis-runtime-packages.json"), encoding="utf-8"))["count"]
    for lang, name in (("ja", "THIRD_PARTY_NOTICES.txt"), ("en", "THIRD_PARTY_NOTICES.en.txt")):
        out = _notice_lines(lang, stage, rows, ver, setup, pkg_count)
        with open(os.path.join(stage, name), "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(out) + "\n")


def _notice_lines(lang: str, stage: str, rows: list[dict], ver: str, setup: dict, pkg_count: int) -> list[str]:
    en = lang == "en"

    def t(ja: str, en_text: str) -> str:
        return en_text if en else ja

    out = [t(f"GD Skin Studio {ver} — 第三者の著作権表示とライセンス（THIRD_PARTY_NOTICES）",
             f"GD Skin Studio {ver} — Third-party notices and licenses (THIRD_PARTY_NOTICES)"), "=" * 72, "",
           t("このファイルは、配布用 ZIP に同梱している第三者のソフトウェアの一覧と、そのライセンスの全文です。",
             "This file lists the third-party software bundled in the distribution ZIP, with the full text of their licenses."),
           t("English: THIRD_PARTY_NOTICES.en.txt", "Japanese: THIRD_PARTY_NOTICES.txt"), "",
           t("このツール自体（GD Skin Studio。app\\・src\\・scripts\\trellis\\ の本プロジェクトのコード）",
             "This tool itself (GD Skin Studio; this project's code in app\\, src\\ and scripts\\trellis\\)"),
           t("    ライセンス: MIT License（全文は LICENSE.txt）", "    License: MIT License (full text in LICENSE.txt)"),
           t(f"    著作権表示: {check_package.COPYRIGHT}", f"    Copyright notice: {check_package.COPYRIGHT}"),
           t("    下の第三者のソフトウェアには、MIT ではなく、それぞれのライセンスが適用されます。",
             "    The third-party software below is not covered by the MIT License; each is under its own license."), "",
           t("A. 同梱しているもの", "A. Bundled software"), "-" * 72]
    for r in rows:
        out += [f"* {r['name_en'] if en else r['name']}",
                t("    ライセンス: ", "    License: ") + (r["license_en"] if en else r["license"]),
                t("    配布元: ", "    Source: ") + r["source"],
                t("    全文: ", "    Full text: ") + (", ".join(r["files"]) if r["files"] else
                                                     t("（パッケージにライセンスのファイルが無い。上のライセンス名を参照）",
                                                       "(the package has no license file; see the license name above)"))]
    packages_list = t("licenses\\setup\\trellis-runtime-packages.txt", "licenses\\setup\\trellis-runtime-packages.en.txt")
    out += ["", t("B. 初回のセットアップで取得するもの（同梱しない。利用者の PC に配布元から直接取得する）",
                  "B. Downloaded during the first-run setup (not bundled; downloaded from the original sources directly to the user's PC)"),
            "-" * 72,
            t("アプリのセットアップの画面で、項目ごとにライセンス名と全文を表示し、同意を得てから取得します。",
              "The app's setup screen shows the license name and full text of each item and asks for consent before downloading."),
            t(f"全文は licenses\\setup\\ にもあります（パッケージ {pkg_count} 件の一覧と全文は {packages_list}）。",
              f"The full texts are also in licenses\\setup\\ (the list and full texts of the {pkg_count} packages: {packages_list}).")]
    for c in setup["components"]:
        name, lic, flags = setup_display(c, lang)
        notes = "".join(t("\n    注意: ", "\n    Note: ") + f["text"] for f in flags)
        out += [f"* {name}" + ("" if c["required"] else t("（任意）", " (optional)")),
                t("    ライセンス: ", "    License: ") + lic["name"],
                t("    配布元: ", "    Source: ") + c["source"]["url"],
                t("    全文: ", "    Full text: ") + ", ".join("licenses/" + f if not f.startswith("..") else f[3:] for f in lic["files"]) + notes]
    out += ["", t("C. 同梱も取得もしないもの", "C. Neither bundled nor downloaded"), "-" * 72,
            t("非商用ライセンスの nvdiffrast・diff-gaussian-rasterization、GPL の pymeshfix・igraph（GLB に変える後処理を"
              "寛容なライセンスの部品だけで作り直したので不要）、",
              "nvdiffrast and diff-gaussian-rasterization (non-commercial licenses), pymeshfix and igraph (GPL) — not needed, "
              "because the GLB post-processing was rebuilt with permissively licensed parts only —"),
            t("Grim Dawn のファイル（基準メッシュ・MOD の雛形は利用者のゲームから読み取る）。",
              "and Grim Dawn's files (the reference meshes and MOD templates are read from the user's game)."), "",
            t("D. ライセンスの全文（同梱しているもの）", "D. Full license texts (bundled software)"), "-" * 72]
    for r in rows:
        for rel in r["files"]:
            out += ["", "=" * 72, f"{r['name_en'] if en else r['name']} — {rel}", "=" * 72]
            out.append(open(os.path.join(stage, rel), encoding="utf-8", errors="replace").read().rstrip())
    return out


def tree_size(d: str) -> int:
    return sum(os.path.getsize(os.path.join(dp, f)) for dp, dn, fn in os.walk(d) for f in fn)


def write_docs(stage: str, ver: str, versions: dict) -> None:
    size_mb = tree_size(stage) / 1e6
    # 利用者向けの README（日本語の README.txt と英語の README.en.txt。2 言語対応 D-060）
    for name in ("README.txt", "README.en.txt"):
        tpl = open(os.path.join(HERE, "templates", name), encoding="utf-8").read()
        with open(os.path.join(stage, name), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(tpl.replace("{version}", ver).replace("{app_size}", f"{round(size_mb, -1):.0f} MB")
                    .replace("{copyright}", check_package.COPYRIGHT))
    # ツール自体のライセンス（D-024: MIT）。元はリポジトリ直下の LICENSE（UTF-8、BOM なし）。ZIP では改行を CRLF にする
    with open(os.path.join(stage, "LICENSE.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write(read_license())
    info = {"name": "GD Skin Studio", "version": ver, "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "python": PY_EMBED["version"], **versions, "layout": 1}
    with open(os.path.join(stage, "gdskin_dist.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)


def smoke(stage: str) -> None:
    """配布物の Python で、依存とバックエンドの読み込みを確かめる（-B: __pycache__ を作らない）。"""
    py = os.path.join(stage, "python", "python.exe")
    code = (
        "import sys, os, ssl, sqlite3, ctypes, zlib, numpy, scipy, scipy.optimize, scipy.spatial, PIL.Image, lz4.block, "
        "fast_simplification, xatlas\n"
        "sys.path.insert(0, os.path.join(sys.argv[1], 'app', 'backend'))\n"
        "os.environ['GDSKIN_HOME'] = sys.argv[2]\n"
        "from gdskin_backend import paths, setup, server, service\n"
        "import gdconvert, gdmesh, modbuilder\n"
        "assert paths.IS_DIST, 'gdskin_dist.json が読めません'\n"
        "print('python', sys.version.split()[0], 'numpy', numpy.__version__, 'scipy', scipy.__version__, "
        "'components', len(setup.manifest()['components']), 'dist', paths.DIST_INFO['version'])\n")
    tmp = os.path.join(os.path.dirname(stage), "_smoke_home")
    shutil.rmtree(tmp, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    run([py, "-I", "-B", "-c", code, stage, tmp], env=env)
    shutil.rmtree(tmp, ignore_errors=True)


def manifest(stage: str) -> None:
    rows = []
    for dp, dn, fn in os.walk(stage):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, stage).replace(os.sep, "/")
            if rel == "MANIFEST.txt":
                continue
            rows.append((rel, os.path.getsize(p), sha256(p)))
    with open(os.path.join(stage, "MANIFEST.txt"), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("# GD Skin Studio 配布物の中身の一覧（パス、大きさ（バイト）、SHA-256）\n# Contents of the GD Skin Studio distribution (path, size in bytes, SHA-256)\n")
        f.write(f"# ファイル {len(rows)} 件、合計 {sum(r[1] for r in rows):,} バイト（この MANIFEST.txt を除く）\n# Files: {len(rows)}, {sum(r[1] for r in rows):,} bytes in total (excluding this MANIFEST.txt)\n")
        for rel, size, h in rows:
            f.write(f"{rel}\t{size}\t{h}\n")


def make_zip(stage: str, zpath: str) -> None:
    top = os.path.basename(stage)
    tmp = zpath + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dp, dn, fn in os.walk(stage):
            dn.sort()
            for f in sorted(fn):
                p = os.path.join(dp, f)
                arc = top + "/" + os.path.relpath(p, stage).replace(os.sep, "/")
                z.write(p, arc)
    os.replace(tmp, zpath)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "build", "package"))
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()
    ver = version()
    read_license()                     # 名義・仮の文字列を先に確かめる（時間のかかる発行の前に止める）
    require_tracked()                  # 追跡していないファイルも先に確かめる（copy_app でも確かめる）
    name = f"GDSkinStudio-{ver}-win-x64"
    out = os.path.abspath(args.out)
    stage = os.path.join(out, name)
    work = os.path.join(out, "_work")
    log(f"== GD Skin Studio {ver} の配布物を作ります: {stage}")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    log("== 1. アプリの発行（.NET 10 自己完結型・単一ファイル）")
    versions = publish(stage, work)
    log("== 2. Python 3.11.9 embeddable とバックエンドの依存")
    embed_python(stage, os.path.join(out, "cache"))
    log("== 3. バックエンド・変換・MOD 作成・生成スクリプト・セットアップの定義")
    copy_app(stage)
    log("== 4. ライセンス・README・目印")
    rows = bundled_licenses(stage, versions)
    third_party_notices(stage, rows, ver)
    write_docs(stage, ver, versions)
    log("== 5. 読み込みの確認と検査")
    smoke(stage)
    manifest(stage)
    errors, warnings, info = check_package.check(stage)
    for w in warnings:
        log("  警告: " + w)
    if errors:
        for e in errors:
            log("  問題: " + e)
        raise SystemExit(f"検査で問題が {len(errors)} 件見つかりました")
    log(f"  検査: 問題なし（ファイル {info['files']:,} 件、{info['bytes'] / 1e6:,.1f} MB）")
    if not args.no_zip:
        zpath = os.path.join(out, name + ".zip")
        log("== 6. ZIP の作成")
        make_zip(stage, zpath)
        zerr, zwarn, zinfo = check_package.check(zpath)
        if zerr:
            raise SystemExit("ZIP の検査で問題: " + "; ".join(zerr[:5]))
        if zinfo["files"] != info["files"]:
            raise SystemExit(f"ZIP のファイル数（{zinfo['files']}）が展開前（{info['files']}）と違います")
        log(f"  ZIP: {zpath}（{os.path.getsize(zpath) / 1e6:,.1f} MB、ファイル {zinfo['files']:,} 件、展開後 {zinfo['bytes'] / 1e6:,.1f} MB、"
            f"SHA-256 {sha256(zpath)}）")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
