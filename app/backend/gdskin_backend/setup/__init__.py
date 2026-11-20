"""初回セットアップ: TRELLIS 実行環境とモデルの取得・確認（計画書 8.2 節、docs/packaging.md 4 章）。

取得するものは ``scripts/trellis/setup_manifest.json``（部品ごとの名前・容量・ライセンス・配布元・手順）に固定してあり、
手順は ``setup_trellis_env.ps1 -Mode runtime`` と ``fetch_models.py`` と同じ結果になる（git の代わりに GitHub の
アーカイブ ZIP、uv はアプリが取得する。ps1 も同じハッシュつきの要件ファイルで入れるが、utils3d は GitHub のアーカイブ ZIP
自体の SHA-256 で照合する。アプリは展開後のファイルごとに照合してから入れる。点検 C30・C36）。

* 状態: 部品ごとに ``installed`` / ``partial`` / ``outdated`` / ``missing``（ファイルの有無と大きさだけを見る。速い）
* 取得（ジョブ ``setup``）: 入っていない部品を取得し、入っている部品のうちモデル・ソースはハッシュで照合して再利用する
  （照合済みのファイルは大きさと更新日時をデータフォルダーに記録し、次からは読み直さない）。PyTorch とパッケージは
  版（dist-info の名前）だけを確かめる。Python の仮想環境を作り直すときは、PyTorch とパッケージも入れ直す（点検 C29）
* 既存の TRELLIS 実行環境を使う: 何も書き込まずに状態を調べ、設定の ``trellis_root`` にする
* 記録: ``<データ>\\app\\setup_state.json``（同意・完了・部品ごとの結果）。TRELLIS 実行環境の中には CLIP の同意
  （consents.json、fetch_models.py と同じ形式）のほかは書かない
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
import zlib
from urllib.parse import quote

from .. import consent, i18n, logs, paths
from ..errors import BackendError
from ..i18n import tr
from .download import Downloader, sha256_file

MANIFEST_PATH = os.path.join(paths.TRELLIS_SCRIPTS, "setup_manifest.json")
SOURCES_PATH = os.path.join(paths.TRELLIS_SCRIPTS, "setup_sources.json")
LICENSES_DIR = os.path.join(paths.REPO_ROOT, "licenses")
STATE_VERSION = 1
MAX_ROOT_LEN = 80            # venv の中の最も長いパス（約 150 文字）と Windows の 260 文字の上限から
RUNTIME_COMPONENTS = ("python", "torch", "packages", "trellis_source")
SETUP_TOOL_COMPONENTS = ("uv",)   # セットアップ・修復だけに使う項目（3D 生成には使わない）
CHECK_WEIGHT = 300_000_000   # 動作確認（import）の進捗の重み（バイト換算）
HF_BASE = "https://huggingface.co"

_state_lock = threading.RLock()     # update_state が読み込みから保存まで持ち続ける（中で load_state・save_state も取る）
_manifest_cache: dict | None = None
_sources_cache: dict | None = None


# ---------------------------------------------------------------------------
# 定義と記録
# ---------------------------------------------------------------------------
def manifest() -> dict:
    global _manifest_cache
    if _manifest_cache is None:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            _manifest_cache = json.load(f)
    return _manifest_cache


def sources() -> dict:
    global _sources_cache
    if _sources_cache is None:
        with open(SOURCES_PATH, encoding="utf-8") as f:
            _sources_cache = json.load(f)["sources"]
    return _sources_cache


def component(cid: str) -> dict:
    for c in manifest()["components"]:
        if c["id"] == cid:
            return c
    raise BackendError("NOT_FOUND", tr(f"セットアップの項目がありません: {cid}", f"No such setup item: {cid}"))


def localized(comp: dict, lang: str | None = None) -> dict:
    """部品の表示の項目（name・description・version・license・flags・source）の、表示言語 lang（省略時は今の表示言語）の版。

    英語は部品の ``en``（setup_manifest.json。日本語を含む項目だけを書く）で置き換え、``en`` に無い項目は定義のまま。
    license は定義の license に en.license（名前・全文のファイル）を重ねたもの、flags は定義の注意（kind）に en.flags の
    同じ位置の文を重ねたもの（2 言語対応 D-060）。返す辞書は写し（変えても定義は変わらない）。"""
    out = copy.deepcopy({"name": comp["name"], "description": comp.get("description", ""), "version": comp.get("version"),
                         "license": comp.get("license", {}), "flags": comp.get("flags", []), "source": comp.get("source", {})})
    if (i18n.normalize(lang) if lang else i18n.lang()) != "en":
        return out
    e = copy.deepcopy(comp.get("en") or {})
    en_flags = e.get("flags") or []
    out.update({k: e[k] for k in ("name", "description", "version") if k in e})
    out["license"].update(e.get("license") or {})
    out["flags"] = [{**f, **(en_flags[i] if i < len(en_flags) else {})} for i, f in enumerate(out["flags"])]
    out["source"].update(e.get("source") or {})
    return out


def _files_from(ref: str) -> dict:
    """``models_manifest.json#trellis`` → {相対パス: {size, sha256}}。"""
    name, key = ref.split("#", 1)
    with open(os.path.join(paths.TRELLIS_SCRIPTS, name), encoding="utf-8") as f:
        return json.load(f)[key]["files"]


def state_path() -> str:
    return os.path.join(paths.app_dir(), "setup_state.json")


def load_state() -> dict:
    with _state_lock:
        try:
            with open(state_path(), encoding="utf-8") as f:
                st = json.load(f)
            if st.get("version") == STATE_VERSION:
                return st
        except (OSError, ValueError):
            pass
        return {"version": STATE_VERSION, "roots": {}}


def save_state(st: dict) -> None:
    with _state_lock:
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        tmp = state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, state_path())


def update_state(fn) -> dict:
    """読み込み → fn で変更 → 保存を 1 つのロックの中で行う（ジョブのスレッドの記録と HTTP のスレッドの mark・
    use_existing が重なっても、片方の変更を消さない。点検 R11A-05）。fn は辞書の操作だけにする（ほかのロックを待たない）。"""
    with _state_lock:
        st = load_state()
        fn(st)
        save_state(st)
        return st


def _root_key(root: str) -> str:
    return os.path.normcase(os.path.abspath(root))


def _writable_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: str) -> None:
    """フォルダーを消す。読み取り専用のファイル（setup_trellis_env.ps1 が git で取得した .git の中など）は、書き込みを
    許してから消し直す（点検 C35）。消せなければ OSError。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_writable_and_retry)


def _rmtree_quiet(path: str) -> None:
    try:
        if os.path.isdir(path):
            _rmtree(path)
    except OSError:
        pass


def default_root() -> str:
    """既定の取得先: データフォルダーの中。利用者のフォルダー名に全角文字がある・長すぎるなど使えないときは
    システムドライブ直下の GDSkinStudio\trellis-runtime（一般の利用者でも作れる）。"""
    root = os.path.join(paths.home(), "trellis-runtime")
    if not [p for p in check_root(root) if p["code"] in ("too_long", "non_ascii")]:
        return root
    return os.path.join((os.environ.get("SystemDrive") or "C:") + os.sep, "GDSkinStudio", "trellis-runtime")


# ---------------------------------------------------------------------------
# ライセンス
# ---------------------------------------------------------------------------
def _license_path(rel: str) -> str:
    p = os.path.normpath(os.path.join(LICENSES_DIR, rel))
    if not paths.is_within(p, paths.REPO_ROOT):
        raise BackendError("INVALID_INPUT", tr(f"ライセンスのファイルの場所が不正です: {rel}", f"Invalid license file location: {rel}"))
    return p


def license_digest(comp: dict) -> str:
    """部品のライセンス表示（名前・区分・注意・全文のファイル）の SHA-256。同意はこの値ごとに記録する。

    両方の言語の表示を含む（英語の表示 en.license・en.flags と、どちらかの言語で表示する全文のファイル。2 言語対応
    D-060）。どちらの言語の表示が変わっても同意し直しになる（CLIP の注意文の D-045 と同じ考え方）。表示言語で値は変わらない。"""
    en = comp.get("en") or {}
    h = hashlib.sha256()
    h.update(json.dumps({"id": comp["id"], "license": comp["license"], "flags": comp.get("flags", []),
                         "en": {"license": en.get("license") or {}, "flags": en.get("flags") or []}},
                        ensure_ascii=False, sort_keys=True).encode("utf-8"))
    files = list(comp["license"].get("files", []))
    for rel in (en.get("license") or {}).get("files", []):
        if rel not in files:
            files.append(rel)
    for rel in files:
        try:
            with open(_license_path(rel), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()


_NOTICE_LANG_FILE = re.compile(r"clip_model_card_notice\.([a-z]+)\.txt$")


def license_text(cid: str, lang: str | None = None) -> dict:
    """部品のライセンスの表示。名前・ライセンス名・注意・配布元と全文のファイルは表示言語 lang（省略時は今の表示言語）のもの
    （英語は en.license.files。パッケージの一覧は trellis-runtime-packages.en.txt。D-060）。lang を渡すと、言語ごとにある
    CLIP の注意文（clip_model_card_notice.<言語>.txt）はその言語のものだけを出す（同意の値 license_sha256 は両方の言語を
    含む。2026-10-18 未決定 #2）。"""
    comp = component(cid)
    loc = localized(comp, lang)
    shown = consent.normalize_lang(lang) if lang else None
    parts = []
    for rel in loc["license"].get("files", []):
        m = _NOTICE_LANG_FILE.search(rel)
        if shown and m and m.group(1) != shown:
            continue
        p = _license_path(rel)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                body = f.read()
        except OSError:
            body = tr("（ファイルが見つかりません）", "(file not found)")
        parts.append({"file": os.path.basename(p), "text": body})
    return {"id": cid, "name": loc["name"], "license": loc["license"]["name"], "class": comp["license"]["class"],
            "links": comp["license"].get("links", []), "flags": loc["flags"], "source": loc["source"],
            "files": parts, "license_sha256": license_digest(comp)}


# ---------------------------------------------------------------------------
# 状態（速い確認: ファイルの有無と大きさ）
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_entries(rel: str) -> list[tuple[str, str | None]]:
    """ハッシュつきの要件ファイル → [(名前, 版 or None（URL のもの）)]。"""
    with open(os.path.join(paths.TRELLIS_SCRIPTS, rel), encoding="utf-8") as f:
        text = f.read()
    out = []
    for e in re.split(r"\n(?=[A-Za-z])", text):
        first = e.splitlines()[0].strip() if e.strip() else ""
        if not first or first.startswith("#"):
            continue
        if " @ " in first:
            out.append((_norm(first.split(" @ ")[0]), None))
        else:
            name, ver = first.split()[0].split("==")
            out.append((_norm(name), ver))
    return out


def installed_dists(root: str) -> dict[str, str]:
    sp = os.path.join(root, "venv", "Lib", "site-packages")
    out = {}
    try:
        for n in os.listdir(sp):
            if n.endswith(".dist-info") and "-" in n:
                name, ver = n[:-len(".dist-info")].split("-", 1)
                out[_norm(name)] = ver
    except OSError:
        pass
    return out


def _venv_python(root: str) -> str:
    return os.path.join(root, "venv", "Scripts", "python.exe")


def _venv_version(root: str) -> str | None:
    try:
        with open(os.path.join(root, "venv", "pyvenv.cfg"), encoding="utf-8") as f:
            for ln in f:
                k, _, v = ln.partition("=")
                if k.strip() in ("version_info", "version"):
                    return v.strip()
    except OSError:
        return None
    return None


_GIT_REF_RE = re.compile(r"refs/[A-Za-z0-9._\-/]+\Z")


def _git_head(path: str) -> str | None:
    """git の取得（setup_trellis_env.ps1）の今のコミット。ブランチに居る（HEAD が「ref: refs/heads/<名前>」）ときは
    .git\\refs\\heads\\<名前>、無ければ .git\\packed-refs の該当行からコミットを解決する（解決できなければ HEAD の中身のまま）。
    以前は「ref: …」をコミットと比べ、固定のコミットに居る取得でも版違い（outdated）とみなしていた（点検 R20A-01）。"""
    git = os.path.join(path, ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="ascii") as f:
            head = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not head.startswith("ref:"):
        return head
    ref = head[4:].strip()
    if not _GIT_REF_RE.match(ref) or ".." in ref.split("/"):
        return head
    try:
        with open(os.path.join(git, *ref.split("/")), encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        pass
    try:
        with open(os.path.join(git, "packed-refs"), encoding="ascii") as f:
            for ln in f:
                parts = ln.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except (OSError, UnicodeDecodeError):
        pass
    return head


def _source_marker(dest: str) -> str | None:
    try:
        with open(os.path.join(dest, ".gdskin_source.json"), encoding="utf-8") as f:
            return json.load(f).get("commit")
    except (OSError, ValueError):
        return None


def _step_status(step: dict, root: str, dists: dict) -> tuple[str, str]:
    """1 手順の状態 → (installed / partial / outdated / missing, 説明)。"""
    t = step["type"]
    m = manifest()
    if t == "zip_extract":
        bad = [n for n, size in step["check"].items()
               if not (os.path.isfile(os.path.join(root, step["dest"], n)) and os.path.getsize(os.path.join(root, step["dest"], n)) == size)]
        return ("installed", "") if not bad else ("missing", ", ".join(bad))
    if t == "uv_python":
        if not os.path.isfile(_venv_python(root)):
            return "missing", tr("venv\\Scripts\\python.exe がありません", "venv\\Scripts\\python.exe is missing")
        v = _venv_version(root)
        return ("installed", v) if v == m["python"] else \
            ("outdated", tr(f"Python {v}（必要 {m['python']}）", f"Python {v} ({m['python']} required)"))
    if t == "wheel_files":
        return "installed", ""        # 一時的な取得物（状態は次の uv_pip で見る）
    if t in ("uv_pip", "source_wheel"):
        want = (requirement_entries(step["requirements"]) if t == "uv_pip" else [(_norm(step["package"]), None)])
        missing = [n for n, v in want if n not in dists]
        wrong = [tr(f"{n} {dists[n]}（必要 {v}）", f"{n} {dists[n]} ({v} required)")
                 for n, v in want if n in dists and v is not None and dists[n] != v]
        if len(missing) < len(want) and _step_status({"type": "uv_python"}, root, dists)[0] != "installed":
            # Python の版が違う・python.exe が無い仮想環境は Python の項目で作り直すので、入っているパッケージも
            # 入れ直しになる（点検 C29。状態・予定の取得量・進捗の重みをそろえる）
            return "outdated", tr("Python の仮想環境の作り直しで入れ直します",
                                  "Will be reinstalled when the Python virtual environment is recreated")
        if not missing and not wrong:
            return "installed", tr(f"{len(want)} 件", f"{len(want)} package(s)")
        if wrong:
            return "outdated", ", ".join(wrong[:5])
        return ("partial" if len(missing) < len(want) else "missing"), \
            tr(f"{len(missing)}/{len(want)} 件が未導入", f"{len(missing)}/{len(want)} not installed")
    if t == "source_archive":
        src = sources()[step["source"]]
        dest = os.path.join(root, src["dest"])
        commit = src["commit"]
        found = None
        for name, body in (step.get("write") or {}).items():
            try:
                with open(os.path.join(dest, name), encoding="utf-8") as f:
                    found = f.read().strip() == body.strip()
            except OSError:
                found = None             # 目印が無い（取得していない・途中）。違う版とは区別する
        if found is None and not step.get("write"):
            mark = _source_marker(dest)
            if mark is not None:
                found = mark == commit
            elif step["source"] == "trellis" and _git_head(dest):
                found = _git_head(dest) == commit        # setup_trellis_env.ps1 の git の取得
            elif step["source"] == "flexicubes":
                # サブモジュールとして取得されたもの。親（TRELLIS）の .git が無い（まだ取得していない・アプリの取得）なら
                # 目印が無いのと同じ扱い（以前は None を「版が違う」とみなし、空の取得先でも outdated になっていた。統合 D3）
                head = _git_head(os.path.join(root, sources()["trellis"]["dest"]))
                if head is not None:
                    found = head == sources()["trellis"]["commit"]
        missing = [r for r in src["files"] if not os.path.isfile(os.path.join(dest, r))]
        if found and not missing:
            return "installed", commit[:7]
        if found is False:
            return "outdated", tr("コミットが違います", "Different commit")   # 新しい版にしか無いファイルが足りなくても「版が違う」
        return ("partial" if len(missing) < len(src["files"]) else "missing"), \
            tr(f"{len(missing)} ファイルが未取得", f"{len(missing)} file(s) not downloaded")
    if t in ("hf_files", "hf_cache"):
        files = _files_from(step["files_from"])
        base = _hf_base(step, root)
        bad = [r for r, f in files.items()
               if not (os.path.isfile(os.path.join(base, r)) and os.path.getsize(os.path.join(base, r)) == f["size"])]
        if t == "hf_cache" and not bad:
            try:
                with open(os.path.join(_hf_cache_repo(step, root), "refs", "main"), encoding="ascii") as f:
                    if f.read().strip() != step["revision"]:
                        return "outdated", tr("リビジョンが違います", "Different revision")
            except OSError:
                return "partial", tr("refs/main がありません", "refs/main is missing")
        if not bad:
            return "installed", tr(f"{len(files)} ファイル", f"{len(files)} file(s)")
        return ("partial" if len(bad) < len(files) else "missing"), \
            tr(f"{len(bad)}/{len(files)} ファイルが未取得", f"{len(bad)}/{len(files)} files not downloaded")
    if t == "url_file":
        p = os.path.join(root, step["dest"])
        if os.path.isfile(p) and os.path.getsize(p) == step["size"]:
            return "installed", ""
        return ("partial" if os.path.isfile(p + ".part") else "missing"), ""
    return "missing", tr(f"不明な手順 {t}", f"Unknown step {t}")


def _archive_size(src: dict) -> int:
    """ソースのアーカイブの大きさ（進捗の重み用）。記録が無ければ展開後の合計。"""
    return int(src.get("archive_size") or src.get("total_bytes") or 1)


def _check_archive(z: zipfile.ZipFile, src: dict) -> dict:
    """アーカイブの中身を記録（setup_sources.json の files）と照合する → {相対パス: ZipInfo}。
    余分・不足のファイル、大きさ・SHA-256 の違いは DOWNLOAD_FAILED（reason = hash_mismatch）。"""
    prefix = src["archive_prefix"]
    got = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        if not info.filename.startswith(prefix):
            raise BackendError("DOWNLOAD_FAILED", tr(f"アーカイブの中身が記録と違います: {info.filename}",
                                                     f"The archive contents do not match the record: {info.filename}"),
                               params={"reason": "hash_mismatch", "file": info.filename})
        got[info.filename[len(prefix):]] = info
    extra = sorted(set(got) - set(src["files"]))
    missing = sorted(set(src["files"]) - set(got))
    if extra or missing:
        raise BackendError("DOWNLOAD_FAILED", tr(f"アーカイブの中身が記録と違います（余分 {extra[:3]}、不足 {missing[:3]}）",
                                                 f"The archive contents do not match the record (extra {extra[:3]}, "
                                                 f"missing {missing[:3]})"),
                           params={"reason": "hash_mismatch"})
    for rel, info in got.items():
        body = z.read(info)
        want = src["files"][rel]
        if len(body) != want["size"] or hashlib.sha256(body).hexdigest() != want["sha256"]:
            raise BackendError("DOWNLOAD_FAILED", tr(f"アーカイブのファイルが記録と合いません: {rel}",
                                                     f"A file in the archive does not match the record: {rel}"),
                               params={"reason": "hash_mismatch", "file": rel})
    return got


def _hf_cache_repo(step: dict, root: str) -> str:
    org, name = step["repo"].split("/")
    return os.path.join(root, step["cache"], f"models--{org}--{name}")


def _hf_base(step: dict, root: str) -> str:
    if step["type"] == "hf_cache":
        return os.path.join(_hf_cache_repo(step, root), "snapshots", step["revision"])
    return os.path.join(root, step["dest"])


def component_status(comp: dict, root: str | None, dists: dict | None = None) -> dict:
    if not root:
        return {"state": "missing", "detail": ""}
    dists = installed_dists(root) if dists is None else dists
    states = [_step_status(s, root, dists) for s in comp["steps"] if s["type"] != "wheel_files"]
    kinds = {k for k, _ in states}
    if kinds == {"installed"}:
        state = "installed"
    elif kinds == {"missing"}:
        state = "missing"
    elif "outdated" in kinds:
        state = "outdated"
    else:
        state = "partial"
    # 同じ説明は 1 回だけ（Python の作り直しのときのパッケージの 2 手順など。統合 D3）
    return {"state": state, "detail": "; ".join(dict.fromkeys(d for k, d in states if d and k != "installed"))}


def _free_bytes(path: str) -> int | None:
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def rebuild_reclaim_bytes(comps: list[dict], states: dict[str, str], root: str) -> int:
    """Python の項目で仮想環境を作り直すときに空きに戻る量（バイト）。

    作り直しで消す venv に入っている PyTorch・パッケージ（状態 outdated。_step_status は Python が対象のとき、venv に
    パッケージが入っている場合だけ outdated にする）の disk_bytes の合計。Python が導入済み・venv が無いときは 0。
    入っていない（missing・partial の）項目は差し引かない（点検 C29・FG02-R1。status が返し、画面の見積もりも同じ規則）。"""
    if states.get("python", "installed") == "installed" or not os.path.isdir(os.path.join(root, "venv")):
        return 0
    return sum(c["disk_bytes"] for c in comps if c["id"] in ("torch", "packages") and states.get(c["id"]) == "outdated")


def disk_need_bytes(comps: list[dict], states: dict[str, str], root: str) -> int:
    """取得に要る空き（バイト。余裕の 5 % と 1 GiB を含む）。未導入の項目が無ければ 0。

    未導入の項目の disk_bytes の合計 + PyTorch のホイール（入れ終わるまでホイールと展開後の両方を置く）
    − 作り直しで消える仮想環境の分（rebuild_reclaim_bytes）。画面（SetupViewModel.UpdateTotals）も同じ規則で見積もる。"""
    todo = [c for c in comps if states.get(c["id"]) != "installed"]
    if not todo:
        return 0
    need = sum(c["disk_bytes"] + (c["download_bytes"] if c["id"] == "torch" else 0) for c in todo)
    need -= rebuild_reclaim_bytes(comps, states, root)
    return int(max(need, 0) * 1.05) + (1 << 30)


_RESERVED_NAME_RE = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)", re.I)


def _clean_root(v) -> str:
    """取得先の入力の前後の空白と引用符（エクスプローラーの「パスとしてコピー」）を外す（画面の Root と同じ。点検 R20B-04）。"""
    return (v or "").strip().strip('"').strip() if isinstance(v, str) else ""


_SYSTEM_DIR_VARS = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")


def _system_dirs() -> list[str]:
    """一般の利用者が書き込めないシステムのフォルダー（環境変数の値。無い・相対・ドライブの直下の値は使わない）。"""
    out = []
    for name in _SYSTEM_DIR_VARS:
        v = (os.environ.get(name) or "").strip()
        if v and os.path.isabs(v):
            v = os.path.abspath(v)
            if os.path.dirname(v) != v and v not in out:
                out.append(v)
    return out


def root_game_dirs(s: dict) -> list[str]:
    """取得先の検査（check_root）が避ける Grim Dawn のフォルダー: 設定の game_dir と Steam の自動検出（設定の
    detect_game_dirs。レジストリと Steam のライブラリの一覧を読むだけで、通信しない）のうち、ゲームに見えるもの。"""
    from modbuilder.gamedata import looks_like_gd
    from .. import settings as _settings
    out = []
    for d in [s.get("game_dir") or ""] + list(_settings.detect_game_dirs()):
        if isinstance(d, str) and d and os.path.isabs(d) and looks_like_gd(d):
            d = os.path.abspath(d)
            if os.path.dirname(d) != d and d not in out:
                out.append(d)
    return out


def _loc_within(path: str, top: str) -> bool:
    """path が top の中か（同じも）。大文字小文字の違いと、実際の場所（ジャンクション・8.3 の短い名前・\\\\?\\ 形式）でも比べる
    （modops._within_real。調べられないときは中とみなさない）。"""
    from ..modops import _within_real
    try:
        return _within_real(path, top)
    except (OSError, ValueError):
        return paths.is_within(path, top)


def check_root(root: str, game_dirs: list[str] | None = None) -> list[dict]:
    """取得先の問題 → [{"code", "message"}]（空なら使える）。

    場所で止めるもの（未決定 #3 R20B-02 の決定 (b)。取得の開始の前に止める。どれか 1 つだけを出す）: ドライブの直下
    （drive_root）、Grim Dawn のフォルダー（同じ・中・それを含む。game_folder。game_dirs は root_game_dirs(設定)。None なら
    見ない）、アプリのデータフォルダーそのものと、それを含むフォルダー（data_folder。データフォルダーの中の既定の取得先
    trellis-runtime などは使える。maintenance.root_info の unsafe_path と同じ向き）、%SystemRoot%・%windir%・%ProgramFiles%・
    %ProgramFiles(x86)%・%ProgramW6432% とその中（system_folder。一般の利用者の権限で書き込めない）。それ以外の中身のある
    フォルダーは止めない（画面が開始のときに確認の警告を出す）。画面が取得先の欄を変えるたびに呼ぶので、通信せず、
    ファイルの属性を読むだけにする。"""
    problems = []
    if not root or not os.path.isabs(root):
        return [{"code": "not_absolute", "message": tr("取得先はドライブから始まるフォルダーを指定してください",
                                                       "Specify a destination folder that starts with a drive")}]
    root = os.path.abspath(root)
    try:
        from ..modops import _real_path
        real = _real_path(root)
    except (OSError, ValueError):
        real = root
    home = paths.home()
    game = next((d for d in game_dirs or () if _loc_within(root, d) or _loc_within(d, root)), None)
    sysdir = next((d for d in _system_dirs() if _loc_within(root, d)), None)
    if os.path.dirname(root) == root or os.path.dirname(real) == real:
        example = os.path.join(root, "trellis-runtime")
        problems.append({"code": "drive_root", "message": tr(f"ドライブの直下（{root}）は取得先にできません。{example} のように、"
                                                             "中のフォルダーを指定してください",
                                                             f"The root of a drive ({root}) cannot be the destination. "
                                                             f"Specify a folder inside it, such as {example}")})
    elif game:
        problems.append({"code": "game_folder", "message": tr(f"Grim Dawn のフォルダー（{game}）とその中・それを含むフォルダーは"
                                                              "取得先にできません（ゲームのファイルと混ざるため）",
                                                              f"The Grim Dawn folder ({game}), folders inside it and folders "
                                                              "that contain it cannot be the destination (the files would "
                                                              "mix with the game files)")})
    elif _loc_within(home, root):
        problems.append({"code": "data_folder", "message": tr(f"アプリのデータフォルダー（{home}）そのものと、それを含むフォルダーは"
                                                              "取得先にできません（「既定に戻す」で既定の取得先に戻すか、ほかのフォルダーを"
                                                              "選んでください）",
                                                              f"The app's data folder ({home}) and folders that contain it "
                                                              "cannot be the destination (click \"Reset to default\" to use the default "
                                                              "destination, or choose another folder)")})
    elif sysdir:
        problems.append({"code": "system_folder", "message": tr(f"Windows のフォルダー・Program Files（{sysdir}）とその中は、"
                                                                "一般の利用者の権限で書き込めないため取得先にできません",
                                                                f"The Windows folder or Program Files ({sysdir}) and folders "
                                                                "inside it cannot be the destination (a normal user cannot "
                                                                "write there)")})
    if len(root) > MAX_ROOT_LEN:
        problems.append({"code": "too_long", "message": tr(f"取得先のパスが長すぎます（{len(root)} 文字。{MAX_ROOT_LEN} 文字まで）",
                                                           f"The destination path is too long ({len(root)} characters; "
                                                           f"up to {MAX_ROOT_LEN} characters)")})
    if paths.IS_DIST and paths.is_within(root, paths.REPO_ROOT):
        problems.append({"code": "inside_app", "message": tr("アプリのフォルダーの中には置けません（アプリの更新で消えるため）",
                                                             "The destination cannot be inside the app folder (it would be "
                                                             "deleted when the app is updated)")})
    if re.search(r"[^\x20-\x7e]", root):
        problems.append({"code": "non_ascii", "message": tr("取得先のパスに半角の英数字・記号以外の文字（日本語や全角の文字など）が含まれています（一部の部品が読み込めないことがあります）",
                                                            "The destination path contains characters other than ASCII letters, "
                                                            "digits and symbols (such as Japanese or full-width characters; some components may fail to load)")})
    drive, rest = os.path.splitdrive(root)
    if drive and not os.path.exists(drive + os.sep):
        problems.append({"code": "no_drive", "message": tr(f"ドライブ {drive} がありません", f"Drive {drive} does not exist")})
    # Windows で使えない名前（<>:"|?*、末尾の空白・ピリオド、CON・NUL などの予約名）。作るときに分かりにくく失敗する（点検 R11A-02）
    parts = [p for p in re.split(r"[\\/]", rest) if p]
    if re.search(r'[<>:"|?*]', rest) or any(p != p.rstrip(" .") or _RESERVED_NAME_RE.match(p) for p in parts):
        problems.append({"code": "invalid_name", "message": tr("取得先のパスに Windows のフォルダー名に使えない文字・名前が含まれています",
                                                               "The destination path contains characters or names that "
                                                               "cannot be used in Windows folder names")})
    # 取得先か、その途中のフォルダーと同じ名前のファイルがある（フォルダーを作れない）。書き込めるかどうかは、ここでは
    # 調べない（画面が取得先の欄を変えるたびに呼ぶ。os.access は Windows のアクセス権を見ない）。取得の始めに確かめる
    p = root
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    if os.path.exists(p) and not os.path.isdir(p):
        problems.append({"code": "not_directory", "message": tr(f"取得先（または途中のフォルダー）と同じ名前のファイルがあります: {p}",
                                                                f"A file has the same name as the destination (or one of "
                                                                f"its parent folders): {p}")})
    return problems


def status(svc, root: str | None = None, lang: str | None = None) -> dict:
    """セットアップ画面の表示に使う全体の状態。

    clip_notice の text・sha256・lang は lang（表示言語。省略時は日本語）の CLIP の注意文。notices には両方の言語の注意文を
    入れる（取得・既存の環境の設定などの結果に入る status は lang を受け取らないので、画面は notices から表示言語のものを選ぶ）。
    部品の名前・説明・版・ライセンス名・注意・配布元は lang（省略時は今の表示言語）のもの（localized。D-060）。
    """
    from ..settings import resolve_trellis_root
    s = svc.settings.get()
    configured = s.get("trellis_root") or ""
    st = load_state()
    # 取消・失敗で設定に保存されなかった取得先（アプリが作ったもの）。使える実行環境が無ければ、既定の取得先ではなくそこを
    # 示す（次の起動で別の取得先に取り直して、途中の数 GB が残らないように。点検 R20B-03）
    pending = _pending_root(st)
    if pending is not None and not (os.path.isdir(pending)
                                    and (st.get("roots", {}).get(_root_key(pending)) or {}).get("created_by_app") is True):
        pending = None
    current = _clean_root(root) or configured or resolve_trellis_root(s) or pending or default_root()
    rec = st.get("roots", {}).get(_root_key(current), {})
    dists = installed_dists(current)
    comps = []
    for c in manifest()["components"]:
        cs = component_status(c, current, dists)
        loc = localized(c, lang)
        comps.append({
            "id": c["id"], "group": c["group"], "name": loc["name"], "description": loc["description"],
            "required": c["required"], "depends": c.get("depends", []), "consent": c.get("consent"),
            "version": loc["version"], "download_bytes": c["download_bytes"], "disk_bytes": c["disk_bytes"],
            "license": loc["license"]["name"], "license_class": c["license"]["class"], "license_links": c["license"].get("links", []),
            "license_sha256": license_digest(c), "flags": loc["flags"], "source": loc["source"],
            "state": cs["state"], "state_detail": cs["detail"], "record": rec.get("components", {}).get(c["id"]),
        })
    need = [c for c in comps if c["required"] and c["state"] != "installed"]
    # 生成の可否には、セットアップ・修復だけに使う道具（uv）を含めない。setup_trellis_env.ps1 で作った実行環境は PATH の uv を
    # 使い tools\uv を作らないので、既存の環境を選ぶと uv だけが未導入になる（取得・修復は uv が無ければ先に取得する。点検 R11A-01）
    need_gen = [c for c in need if c["id"] not in SETUP_TOOL_COMPONENTS]
    clip =consent.status(current if os.path.isdir(current) else None, lang)
    notices = {}
    for nl in consent._fetch_models().NOTICE_FILES:
        text, digest = consent.notice_text(nl)
        notices[nl] = {"text": text, "sha256": digest}
    return {
        "root": current, "configured_root": configured, "default_root": default_root(),
        "root_exists": os.path.isdir(current), "root_problems": check_root(current, root_game_dirs(s)),
        "free_bytes": _free_bytes(current),
        "components": comps,
        "required_missing": [c["id"] for c in need],
        "required_download_bytes": sum(c["download_bytes"] for c in need),
        "required_disk_bytes": sum(c["disk_bytes"] for c in need),
        # Python の作り直しで空きに戻る量。画面は選んだ項目の見積もりからこれを差し引き、run_install（disk_need_bytes）と
        # 同じ規則で空きを判定する（FG02-R1）
        "rebuild_reclaim_bytes": rebuild_reclaim_bytes(comps, {c["id"]: c["state"] for c in comps}, current),
        "ready_image": not need_gen,
        "ready_text": not need_gen and all(c["state"] == "installed" for c in comps if c["group"] == "text") and clip["accepted"],
        "clip_notice": {"text": clip["notice"], "sha256": clip["notice_sha256"], "lang": clip["notice_lang"],
                        "accepted": clip["accepted"], "accepted_at": clip["accepted_at"], "accepted_lang": clip["accepted_lang"],
                        "notices": notices},
        "completed_at": st.get("completed_at"), "skipped_at": st.get("skipped_at"),
        "needs_setup": not st.get("completed_at") and not st.get("skipped_at"),
        "acceptance": st.get("acceptance"), "last_install": rec.get("last_install"),
        "runtime_check": rec.get("runtime_check"),
        "app": {"dist": paths.IS_DIST, "version": paths.DIST_INFO.get("version") if paths.DIST_INFO else None},
        # 設定画面の削除・修復の対象か（アプリが作った取得先だけ。点検 SF04・SF16）
        "maintenance": _maintenance_info(current, st),
        # 途中で止まった取得先（アプリが作り、今もある。無ければ null。点検 R20B-03）
        "pending_root": pending,
    }


def _pending_root(st: dict) -> str | None:
    """記録の pending_root の path（形が違えば None）。"""
    p = st.get("pending_root")
    return p["path"] if isinstance(p, dict) and isinstance(p.get("path"), str) and p["path"] else None


def _maintenance_info(root: str, st: dict) -> dict:
    from .maintenance import root_info
    return root_info(root, st)


def mark(svc, what: str) -> dict:
    """完了・後で（skip）・やり直す（reset）の記録。"""
    if what not in ("completed", "skipped", "reset"):
        raise BackendError("INVALID_INPUT", tr("completed / skipped / reset のどれかです",
                                               "Must be one of completed / skipped / reset"))

    def fn(st):
        if what == "reset":
            st.pop("completed_at", None)
            st.pop("skipped_at", None)
        else:
            st[f"{what}_at"] = logs.now_iso()
    update_state(fn)
    logs.info(tr(f"初回セットアップの状態を記録しました: {what}", f"Recorded the first-run setup state: {what}"))
    return status(svc)


def use_existing(svc, root: str) -> dict:
    """既存の TRELLIS 実行環境を使う（何も書き込まない）。"""
    if not root or not os.path.isdir(root):
        raise BackendError("INVALID_INPUT", tr("フォルダーがありません", "The folder does not exist"), params={"reason": "not_found"})
    root = os.path.abspath(root)
    if not os.path.isfile(_venv_python(root)):
        raise BackendError("INVALID_INPUT", tr("TRELLIS 実行環境ではありません（venv\\Scripts\\python.exe がありません）",
                                               "Not a TRELLIS runtime (venv\\Scripts\\python.exe is missing)"),
                           params={"reason": "not_runtime"})
    svc.update_settings({"trellis_root": root})
    now = logs.now_iso()

    def fn(st):
        r = st.setdefault("roots", {}).setdefault(_root_key(root), {"path": root})
        r["found_at"] = now
    update_state(fn)
    logs.info(tr("既存の TRELLIS 実行環境を使う設定にしました", "Set to use the existing TRELLIS runtime"))
    # 取得先と同じ検査（全角文字・長すぎるパス・ゲームのフォルダーの中など）。使うのは止めないが、注意をログと画面
    # （status の root_problems）に出す（取得先として止める場所でも、既存の環境を使うことは止めない。R20B-02）
    for p in check_root(root, root_game_dirs(svc.settings.get())):
        logs.warning(tr(f"既存の TRELLIS 実行環境の場所の注意: {p['message']}",
                        f"Warning about the location of the existing TRELLIS runtime: {p['message']}"))
    return status(svc, root)


# ---------------------------------------------------------------------------
# 取得（ジョブ）
# ---------------------------------------------------------------------------
def summarize(p: dict) -> dict:
    return {"components": list(p.get("components") or []), "verify": bool(p.get("verify"))}


def resolve_selection(selected: list[str]) -> list[dict]:
    """選んだ部品 + 必須 + 依存 → 定義の順の部品の一覧。"""
    m = manifest()
    want = {c["id"] for c in m["components"] if c["required"]} | set(selected or [])
    changed = True
    while changed:
        changed = False
        for c in m["components"]:
            if c["id"] in want:
                for d in c.get("depends", []):
                    if d not in want:
                        want.add(d)
                        changed = True
    unknown = set(selected or []) - {c["id"] for c in m["components"]}
    if unknown:
        raise BackendError("INVALID_INPUT", tr(f"不明な項目: {sorted(unknown)}", f"Unknown items: {sorted(unknown)}"))
    return [c for c in m["components"] if c["id"] in want]


class _Progress:
    """部品ごとの重み（バイト）で全体の進捗を出す。"""

    def __init__(self, ctx, comps: list[dict], todo: set[str], with_check: bool):
        self.ctx = ctx
        self.weights = {c["id"]: (c["download_bytes"] if c["id"] in todo else max(1, c["disk_bytes"] // 20)) for c in comps}
        if with_check:
            self.weights["_check"] = CHECK_WEIGHT
        self.total = sum(self.weights.values()) or 1
        self.done = {k: 0.0 for k in self.weights}
        self.states = {c["id"]: "waiting" for c in comps}
        self.names = {c["id"]: localized(c)["name"] for c in comps}
        self._speed = (time.monotonic(), 0)
        self.bps = 0.0

    def set(self, cid: str, frac: float, stage: str, file: str | None = None, done_bytes: int | None = None,
            total_bytes: int | None = None, state: str | None = None) -> None:
        self.done[cid] = max(0.0, min(1.0, frac))
        if state:
            self.states[cid] = state
        if done_bytes is not None:
            now = time.monotonic()
            if self._speed is None or done_bytes < self._speed[1]:
                self._speed = (now, done_bytes)        # 次のファイル・手順に移った（照合だけで進んだ分は速さに入れない）
            elif now - self._speed[0] >= 1.0:
                t0, b0 = self._speed
                rate = (done_bytes - b0) / (now - t0)
                self.bps = 0.6 * self.bps + 0.4 * rate if self.bps else rate
                self._speed = (now, done_bytes)
        overall = sum(self.weights[k] * self.done[k] for k in self.weights) / self.total
        detail = {"component": cid, "name": self.names.get(cid, ""), "file": file, "done": done_bytes, "total": total_bytes,
                  "speed": int(self.bps) if done_bytes is not None else None, "components": dict(self.states)}
        self.ctx.progress(overall, stage, detail)

    def start_bytes(self) -> None:
        self._speed = None
        self.bps = 0.0


def run_install(svc, ctx, params: dict) -> dict:
    """ジョブ ``setup``: params = {root, components: [任意の項目], accepted: {id: license_sha256}, clip_notice_sha256,
    clip_notice_lang（表示した CLIP の注意文の言語 ja / en。省略時は SHA-256 が合う言語）, verify}。"""
    from ..settings import resolve_trellis_root
    # 検査は abspath の前の値で行う（引用符つき・相対のパスが作業フォルダー基準の絶対パスになって not_absolute を素通り
    # しないように。点検 R20B-04）
    raw = _clean_root(params.get("root")) or resolve_trellis_root(svc.settings.get()) or default_root()
    root = os.path.abspath(raw)
    # ドライブの直下・データフォルダー・システムのフォルダー・ゲームのフォルダーも、記録・同意・書き込みの前に止める（R20B-02）
    probs = check_root(raw, root_game_dirs(svc.settings.get()))
    if probs:
        raise BackendError("INVALID_INPUT", probs[0]["message"], params={"reason": "root_" + probs[0]["code"], "root": root})
    comps = resolve_selection(params.get("components") or [])
    accepted = params.get("accepted") or {}
    not_accepted = [c["id"] for c in comps if accepted.get(c["id"]) != license_digest(c)]
    if not_accepted:
        raise BackendError("CONSENT_REQUIRED", tr("ライセンスへの同意がない項目があります（表示した後で内容が変わった可能性があります）",
                                                  "The licenses of some items have not been accepted (their content may have "
                                                  "changed after they were shown)"),
                           params={"components": not_accepted, "reason": "license"})
    clip_lang = None
    if any(c.get("consent") == "clip" for c in comps):
        sha = params.get("clip_notice_sha256") or ""
        clip_lang = consent.normalize_lang(params["clip_notice_lang"]) if params.get("clip_notice_lang") \
            else consent.lang_of_digest(sha)
        if clip_lang is None or sha != consent.notice_text(clip_lang)[1]:
            raise BackendError("CONSENT_REQUIRED", tr("CLIP の注意事項への同意が必要です",
                                                      "Acceptance of the CLIP notice is required"), params={"reason": "clip"})
    active = [j for j in svc.jobs.active("gpu") if j.state == "running"]
    if active:
        # 列 gpu は 3D 生成・変換し直し（generate・convert・reconvert_equipment）。reason で画面の文を分ける（点検 FG02-R4）
        raise BackendError("BUSY", tr("3D 生成・変換の実行中はセットアップできません（終わってからもう一度実行してください）",
                                      "Setup cannot run while 3D generation or conversion is running (run it again after it "
                                      "finishes)"),
                           params={"reason": "generate_running"})
    # 背景除去の確認（列 preview）は TRELLIS 実行環境の Python で rembg を読み込む。パッケージの入れ替えや仮想環境の
    # 作り直しと重なると、どちらかが失敗しうる（点検 C42。逆向きは pipeline.run_bg_preview が止める）
    if [j for j in svc.jobs.active("preview") if j.state == "running"]:
        raise BackendError("BUSY", tr("背景除去の確認の実行中はセットアップできません（終わってからもう一度実行してください）",
                                      "Setup cannot run while the background removal check is running (run it again after "
                                      "it finishes)"),
                           params={"reason": "preview_running"})

    dists = installed_dists(root)
    pre = {c["id"]: component_status(c, root, dists)["state"] for c in comps}
    todo = {cid for cid, s in pre.items() if s != "installed"}
    verify_only = not todo
    # 作り直しで消す仮想環境に入っている分（入れ直す PyTorch・パッケージ）は空きに戻るので差し引く。差し引かないと約 8 GB
    # 多く見積もる（点検 C29）。画面も status の rebuild_reclaim_bytes（同じ関数の値）で同じ規則の見積もりをする（FG02-R1）
    need = disk_need_bytes(comps, pre, root)
    free = _free_bytes(root)
    if todo and free is not None and free < need:
        raise BackendError("DISK_SPACE_LOW", tr(f"取得先のドライブの空きが足りません（空き {free / 1e9:.1f} GB、必要 約 {need / 1e9:.1f} GB）",
                                                f"Not enough free space on the destination drive ({free / 1e9:.1f} GB free, "
                                                f"about {need / 1e9:.1f} GB needed)"),
                           params={"free_bytes": free, "need_bytes": need, "root": root})
    # 既存の仮想環境の作り直し（Python の版が違う・python.exe が無い）は venv を丸ごと消す。アプリが作った取得先・アプリが
    # 作った venv でなければ（既存の環境・ほかのアプリのフォルダー）、明示の許可（allow_venv_rebuild）が無い限り何も消さずに
    # 止める（点検 R11B-01）
    rec0 = load_state().get("roots", {}).get(_root_key(root), {})
    allow_rebuild = bool(params.get("allow_venv_rebuild")) or _app_venv(rec0)
    if "python" in todo and os.path.isdir(os.path.join(root, "venv")) and not allow_rebuild:
        raise _venv_rebuild_error(root, manifest()["python"])
    # 同じく、アプリの取得先でなければ、既存のソースのフォルダーの入れ替え（trellis_source・dinov2 の outdated・partial。
    # .git と変更が消える）と、venv を作り直さないときのパッケージの版の変更（torch・packages の outdated）も、明示の許可
    # （allow_replace。画面は確認で同意したときに送る）が無い限り何も書き換えずに止める（点検 R20A-01）
    if not _app_root(rec0) and not params.get("allow_replace"):
        rebuild_venv = "python" in todo
        replace = [c["id"] for c in comps
                   if (pre[c["id"]] in ("outdated", "partial") and any(s["type"] == "source_archive" for s in c["steps"]))
                   or (pre[c["id"]] == "outdated" and not rebuild_venv and any(s["type"] == "uv_pip" for s in c["steps"]))]
        if replace:
            raise BackendError("INVALID_INPUT", tr(f"取得先 {root} の既存の項目（{', '.join(replace)}）を固定の版に入れ替えるには"
                                                   "確認が必要です。アプリが作った取得先ではないので、確認なしでは変更しません",
                                                   f"Confirmation is needed to replace the existing items ({', '.join(replace)}) "
                                                   f"in the destination {root} with the pinned versions. It was not created by "
                                                   "this app, so they are not changed without confirmation"),
                               params={"reason": "existing_replace", "root": root, "items": replace})
    # 作業フォルダー（downloads）の空のフォルダーを最後に消すとき、アプリの取得先でなければ取得の前からあったものは残す
    keep_dirs = set() if _app_root(rec0) else _dirs_under(os.path.join(root, "downloads"))
    # 取得先をアプリが作ったか（初めての取得の前に、無い・空だった）を記録する。設定画面の削除・修復（点検 SF04・SF16）は、
    # アプリが作った取得先だけを対象にする（取得の前から中身があった既存の環境は書き換えない）
    try:
        fresh = not os.path.isdir(root) or not os.listdir(root)
        os.makedirs(root, exist_ok=True)
        # 書き込めるか（C:\Program Files の下など、一般の利用者が書けない場所）を、記録・同意・取得の前に確かめる（点検 R11A-02）
        probe = os.path.join(root, ".gdskin-write-test")
        with open(probe, "wb"):
            pass
        os.remove(probe)
    except OSError as e:
        raise BackendError("INVALID_INPUT", tr("取得先のフォルダーを作れないか、書き込めません。別のフォルダーを選んでください",
                                               "Cannot create or write to the destination folder. Choose another folder"),
                           f"{type(e).__name__}: {e}", {"reason": "root_not_writable", "root": root}) from e

    started = logs.now_iso()

    def mark_created(st):
        # 取得の途中で止まった（取消・失敗・強制終了）取得先は設定に保存されないので、次の起動で示せるように覚える。成功の後
        # の設定の保存の後で消す（点検 R20B-03。status の pending_root）
        st["pending_root"] = {"path": root, "at": started}
        r = st.setdefault("roots", {}).setdefault(_root_key(root), {"path": root})
        if "created_by_app" in r:
            return
        if fresh:
            r["created_by_app"] = True
        elif not (r.get("last_install") or r.get("components")):
            r["created_by_app"] = False
        # 以前の版のアプリが取得した取得先（印が無く、取得の記録はある）には印を付けない（maintenance.root_info が
        # アプリの既定の取得先のときだけ対象にする）
    update_state(mark_created)
    ctx.info(tr(f"初回セットアップ: 取得先 {root}、項目 {len(comps)}（うち取得するもの {len(todo)}）、必要な空き 約 {need / 1e9:.1f} GB",
                f"First-run setup: destination {root}, {len(comps)} item(s) ({len(todo)} to download), about "
                f"{need / 1e9:.1f} GB of free space needed"))
    runtime_changed = bool(todo & set(RUNTIME_COMPONENTS))
    st0 = load_state().get("roots", {}).get(_root_key(root), {})
    has_runtime = any(c["id"] in RUNTIME_COMPONENTS for c in comps)
    want_check = has_runtime and (runtime_changed or bool(params.get("verify")) or not (st0.get("runtime_check") or {}).get("ok"))
    prog = _Progress(ctx, comps, todo, want_check)
    inst = _Installer(svc, ctx, root, prog, allow_venv_rebuild=allow_rebuild)
    results = {}
    if clip_lang is not None:
        # 取得先に今の注意文への同意の記録があれば、そのまま使う（画面は同意済みなら注意文を出さない。表示していない言語で
        # 記録し直さない。2026-10-18 未決定 #2）
        if consent.status(root)["accepted"]:
            ctx.info(tr("CLIP の注意事項への同意の記録（取得先にある、今の注意文への同意）をそのまま使います",
                        "Using the record of acceptance of the CLIP notice that is already in the destination (acceptance "
                        "of the current notice)"))
        else:
            consent.accept(root, params["clip_notice_sha256"], method="app-setup", lang=clip_lang)
            ctx.info(tr(f"CLIP の注意事項への同意を記録しました（セットアップ画面、注意文 {clip_lang}）",
                        f"Recorded acceptance of the CLIP notice (setup screen, notice language {clip_lang})"))
    try:
        for i, c in enumerate(comps):
            ctx.check_cancel()
            name = localized(c)["name"]
            if c["id"] not in todo:
                # 念のため直前に状態を計算し直す（前の項目が仮想環境を作り直したなど、始める前の状態から変わった項目は取得する。
                # 点検 C29）
                now_state = component_status(c, root)["state"]
                if now_state != "installed":
                    pre[c["id"]] = now_state
                    todo.add(c["id"])
            if c["id"] in todo:
                prog.set(c["id"], 0.0, "setup_install", state="running")
                ctx.info(tr(f"[{i + 1}/{len(comps)}] {name}: 取得します（状態 {pre[c['id']]}）",
                            f"[{i + 1}/{len(comps)}] {name}: downloading (state {pre[c['id']]})"))
                inst.install(c)
                after = component_status(c, root)["state"]
                if after != "installed":
                    raise BackendError("SETUP_FAILED", tr(f"{name}: 取得の後も状態が {after} です",
                                                          f"{name}: the state is still {after} after the download"),
                                       params={"component": c["id"], "step": "status"})
                results[c["id"]] = "installed"
            else:
                prog.set(c["id"], 0.0, "setup_verify", state="verifying")
                inst.verify(c)
                results[c["id"]] = "reused"
            prog.set(c["id"], 1.0, "setup_install", state=results[c["id"]])
            inst.record(c, results[c["id"]])
        check = None
        if want_check or (has_runtime and todo & set(RUNTIME_COMPONENTS)):     # ループの中で取得に回った項目も含める
            prog.set("_check", 0.0, "setup_check")
            check = inst.runtime_check()
            prog.set("_check", 1.0, "setup_check")
    except BaseException:
        for cid, state in prog.states.items():
            if state in ("running", "verifying"):
                prog.states[cid] = "failed"
        # 部品の途中で失敗・取り消しても、照合済みのファイルの記録は残す（再試行で取得済みの数 GB を読み直さない。点検 R11A-06）
        try:
            inst.save_hashes()
        except Exception:  # noqa: BLE001  記録の失敗で元の例外を隠さない
            pass
        raise
    _remove_empty_dirs(os.path.join(root, "downloads"), keep_dirs)
    # 設定を空（自動）にするのは、自動の探索の最初の候補（<データ>\trellis-runtime）と同じときだけ。それ以外（利用者の
    # フォルダー名に全角文字があるときの既定 %SystemDrive%\GDSkinStudio\trellis-runtime を含む）は絶対パスを保存する。
    # 以前は既定の取得先なら空にしていたため、全角の利用者名の PC で配布物が実行環境を見つけられなかった（点検 N01）
    from ..settings import trellis_root_candidates
    auto = trellis_root_candidates({**svc.settings.get(), "trellis_root": ""})
    svc.update_settings({"trellis_root": "" if auto and _root_key(root) == _root_key(auto[0]) else root})
    now = logs.now_iso()

    def fn(st):
        r = st.setdefault("roots", {}).setdefault(_root_key(root), {"path": root})
        r["last_install"] = {"at": now, "components": results, "verify_only": verify_only}
        st["acceptance"] = {"accepted_at": now, "components": {c["id"]: license_digest(c) for c in comps}}
        # 取得先ごとの同意（前の取得で選んだ任意の項目も残す。修復で取り直すときに確かめる。点検 SF16）
        r["accepted"] = {**(r.get("accepted") or {}), **{c["id"]: license_digest(c) for c in comps}}
        if _pending_root(st) is not None and _root_key(_pending_root(st)) == _root_key(root):
            st.pop("pending_root", None)        # 設定に保存した（点検 R20B-03）
    update_state(fn)
    svc.invalidate_environment()
    ctx.info(tr("初回セットアップが終わりました: ", "First-run setup finished: ") + ", ".join(f"{k}={v}" for k, v in results.items()))
    return {"root": root, "components": results, "runtime_check": check, "status": status(svc, root)}


_UV_HASH_RE = re.compile(r"Hash mismatch", re.I)
_UV_TLS_RE = re.compile(r"invalid peer certificate|UnknownIssuer|certificate verify failed|CERTIFICATE_VERIFY_FAILED|"
                        r"self[- ]signed certificate in certificate chain", re.I)
_UV_NETWORK_RE = re.compile(r"error sending request|Failed to (download|fetch)|connection|timed out|dns error|"
                            r"tls handshake|HTTP status (5\d\d|429)|Request failed", re.I)
# OS のエラー番号（「(os error 112)」。文は利用者の言語になるので番号で見る。uv は長い行を折り返すので空白・改行を許す）
_UV_OS_ERROR_RE = re.compile(r"\(os\s+error\s+(\d+)\)", re.I)
_UV_DISK_FULL_ERRORS = {112, 39}          # ERROR_DISK_FULL・ERROR_HANDLE_DISK_FULL
_UV_LOCKED_ERRORS = {5, 32, 33}           # アクセス拒否・共有違反・ロック違反（ウイルス対策ソフトの検査など）


def uv_failure_reason(out: str) -> str:
    """uv の失敗の出力 → hash_mismatch / disk_full / write_failed / tls / network / uv（params.reason）。network と
    write_failed だけを自動でやり直す。

    ハッシュの不一致は「Failed to download」を含んでも通信エラーにしない（点検 C30）。取得先への書き込みの失敗（空き不足・
    アクセス拒否）も uv は「Failed to download」の原因として出すので、通信エラーより先に OS のエラー番号で判定する（点検
    R11A-03）。証明書の検証の失敗（HTTPS を検査するウイルス対策ソフト・社内プロキシなど）は、同じ出力に「error sending
    request」があるので network より先に判定する（C08）。"""
    out = out or ""
    if _UV_HASH_RE.search(out):
        return "hash_mismatch"
    codes = {int(n) for n in _UV_OS_ERROR_RE.findall(out)}
    if codes & _UV_DISK_FULL_ERRORS:
        return "disk_full"
    if codes & _UV_LOCKED_ERRORS:
        return "write_failed"
    if _UV_TLS_RE.search(out):
        return "tls"
    if _UV_NETWORK_RE.search(out):
        return "network"
    return "uv"


def _remove_empty_dirs(top: str, keep: set[str] = frozenset()) -> None:
    """取得の作業フォルダー（downloads）の空のフォルダーを消す（途中のファイルが残っていれば残す。keep（_dirs_under の
    値）にあるフォルダーは消さない）。"""
    for dp, dn, fn in os.walk(top, topdown=False):
        if os.path.normcase(os.path.abspath(dp)) in keep:
            continue
        try:
            if not os.listdir(dp):
                os.rmdir(dp)
        except OSError:
            pass


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _dirs_under(top: str) -> set[str]:
    """top とその下のフォルダー（os.path.normcase した絶対パス）。"""
    return {os.path.normcase(os.path.abspath(dp)) for dp, _, _ in os.walk(top)}


def _app_root(rec: dict) -> bool:
    """取得先の記録（setup_state.json の roots の 1 件）から、アプリが作った取得先か。印（created_by_app）の無い以前の版の
    取得先は、取得の記録があればアプリの取得先とみなす（mark_created と同じ扱い）。"""
    if "created_by_app" in rec:
        return bool(rec["created_by_app"])
    return bool(rec.get("last_install") or rec.get("components"))


def _app_venv(rec: dict) -> bool:
    """取得先の venv を、許可なしに消して作り直してよいか（アプリが作った取得先か、アプリが作った venv。点検 R11B-01）。"""
    return _app_root(rec) or bool(rec.get("venv_created_by_app"))


def _venv_rebuild_error(root: str, want: str) -> BackendError:
    have = _venv_version(root) or "?"
    venv = os.path.join(root, "venv")
    # 版は同じで venv\Scripts\python.exe だけが無いときは「Python 3.11.9 を Python 3.11.9 で作り直す」と言わず、python.exe が
    # 無いので作り直す、と言う（params.python_missing。2026-11-04 退行の確認 2 回目 B5）
    python_missing = not os.path.isfile(_venv_python(root))
    if python_missing and have == want:
        msg = tr(f"取得先の仮想環境 {venv}（Python {have}）には venv\\Scripts\\python.exe が無いので、作り直すために削除が"
                 f"必要です。アプリが作ったものではないので削除しません。ほかのアプリで使っている場合は、空のフォルダーを"
                 f"取得先にしてください",
                 f"The virtual environment {venv} (Python {have}) in the destination has no venv\\Scripts\\python.exe, so it "
                 f"must be deleted to recreate it. It was not created by this app, so it is not deleted. If another app uses "
                 f"it, choose an empty folder as the destination")
    else:
        msg = tr(f"取得先の仮想環境 {venv}（Python {have}）は、Python {want} で作り直すために削除が必要です。"
                 f"アプリが作ったものではないので削除しません。ほかのアプリで使っている場合は、空のフォルダーを"
                 f"取得先にしてください",
                 f"The virtual environment {venv} (Python {have}) in the destination must be "
                 f"deleted to recreate it with Python {want}. It was not created by this app, so "
                 f"it is not deleted. If another app uses it, choose an empty folder as the "
                 f"destination")
    return BackendError("INVALID_INPUT", msg,
                        params={"reason": "existing_venv_rebuild", "root": root, "venv_version": have, "required": want,
                                "python_missing": python_missing})


class _Installer:
    def __init__(self, svc, ctx, root: str, prog: _Progress, reinstall: bool = False, allow_venv_rebuild: bool = True):
        self.svc = svc
        self.ctx = ctx
        self.root = root
        self.prog = prog
        # 修復（点検 SF16）: uv に --reinstall を付ける（キャッシュの取り直しは maintenance が先に uv cache clean で行う。
        # --refresh だけではハッシュで固定したホイールのキャッシュの展開をそのまま使う。統合 D3）。通常の取得では False
        self.reinstall = reinstall
        # 既存の venv を消して作り直してよいか（run_install はアプリの取得先・アプリが作った venv か、明示の許可のときだけ
        # True にする。修復はアプリが作った取得先だけが対象。点検 R11B-01）
        self.allow_venv_rebuild = allow_venv_rebuild
        self.m = manifest()
        self.dl = Downloader(check_cancel=ctx.check_cancel, log=ctx.info)
        st = load_state().get("roots", {}).get(_root_key(root), {})
        self.hash_cache: dict = copy.deepcopy(st.get("file_hashes", {}))

    # --- 記録 ---------------------------------------------------------------------------------
    def record(self, comp: dict, result: str) -> None:
        now = logs.now_iso()

        def fn(st):
            r = st.setdefault("roots", {}).setdefault(_root_key(self.root), {"path": self.root})
            r.setdefault("components", {})[comp["id"]] = {"result": result, "at": now, "version": comp.get("version")}
            r["file_hashes"] = self.hash_cache
        update_state(fn)

    def save_hashes(self) -> None:
        """照合済みのファイルの記録だけを保存する（部品の結果は変えない。途中で失敗した取得の再試行用。点検 R11A-06）。"""
        def fn(st):
            st.setdefault("roots", {}).setdefault(_root_key(self.root), {"path": self.root})["file_hashes"] = self.hash_cache
        update_state(fn)

    # --- 照合 ---------------------------------------------------------------------------------
    def _cache_hit(self, path: str, sha256: str) -> bool:
        """ファイルを読まずに、記録（大きさ・更新日時・SHA-256）だけで照合済みと分かるか（点検 R11A-06）。"""
        cached = self.hash_cache.get(os.path.relpath(path, self.root))
        try:
            stt = os.stat(path)
        except OSError:
            return False
        return bool(cached) and cached[:2] == [stt.st_size, stt.st_mtime_ns] and cached[2] == sha256

    def _verified(self, path: str, sha256: str, on_bytes=None, crlf_ok: bool = False, force: bool = False) -> bool:
        """ファイルの SHA-256 が合うか（大きさと更新日時が前回の照合と同じなら読み直さない。force = True（照合し直し。
        点検 SF16）なら記録を使わずに読み直す）。合わない・無いときはそのファイルのハッシュの記録を消す（照合し直しで
        合わず、同意の記録が無いため取り直さなかったファイルを、次の通常の取得が古い記録を信じて再利用しないように。
        点検 U-D2-R2）。"""
        rel = os.path.relpath(path, self.root)
        try:
            stt = os.stat(path)
        except OSError:
            self.hash_cache.pop(rel, None)
            return False
        key = [stt.st_size, stt.st_mtime_ns]
        cached = self.hash_cache.get(rel)
        if cached and cached[:2] == key and cached[2] == sha256 and not force:
            if on_bytes:
                on_bytes(stt.st_size)
            return True
        digest = sha256_file(path, on_bytes, self.ctx.check_cancel)     # 大きなモデルの読み直しの途中でも取消を効かせる（R11B-04）
        if digest != sha256 and crlf_ok:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
        if digest == sha256:
            self.hash_cache[rel] = key + [sha256]
            return True
        self.hash_cache.pop(rel, None)
        return False

    def verify(self, comp: dict, full: bool = False, refetch: bool = True) -> list[str]:
        """入っている部品のファイルを照合する（モデル・ソース）。合わないファイルがあれば取り直す。

        full = True（照合し直し。点検 SF16）はハッシュの記録を使わずに全部のファイルを読み直す。refetch = False は
        照合だけ（合わないファイルを消さず、取り直さない）。戻り値は合わなかったファイル（取得先からの相対パス）。"""
        files = []
        for step in comp["steps"]:
            t = step["type"]
            if t in ("hf_files", "hf_cache"):
                base = _hf_base(step, self.root)
                files += [(os.path.join(base, r), f["size"], f["sha256"], False) for r, f in _files_from(step["files_from"]).items()]
            elif t == "url_file":
                files.append((os.path.join(self.root, step["dest"]), step["size"], step["sha256"], False))
            elif t == "source_archive":
                src = sources()[step["source"]]
                dest = os.path.join(self.root, src["dest"])
                files += [(os.path.join(dest, r), f["size"], f["sha256"], True) for r, f in src["files"].items()]
        total = sum(f[1] for f in files) or 1
        done = 0
        bad = []
        for path, size, sha, crlf in files:
            self.ctx.check_cancel()
            base_done = done
            ok = self._verified(path, sha, lambda d: self.prog.set(comp["id"], (base_done + d) / total, "setup_verify",
                                                                  os.path.basename(path), base_done + d, total), crlf, full)
            done += size
            if not ok:
                bad.append(os.path.relpath(path, self.root))
        name = localized(comp)["name"]
        if bad and not refetch:
            self.ctx.warn(tr(f"{name}: 照合が合わないファイルがあります（{len(bad)} 件: {', '.join(bad[:3])}）",
                             f"{name}: {len(bad)} file(s) do not match: {', '.join(bad[:3])}"))
            return bad
        if bad:
            self.ctx.warn(tr(f"{name}: 照合が合わないファイルがあります（{len(bad)} 件: {', '.join(bad[:3])}）。取り直します",
                             f"{name}: {len(bad)} file(s) do not match: {', '.join(bad[:3])}. Downloading them again"))
            for p in bad:
                full = os.path.join(self.root, p)
                if any(paths.is_within(full, os.path.join(self.root, d)) for d in ("models", "torch-home", "u2net", "hf-home")):
                    try:
                        os.remove(full)
                    except OSError:
                        pass
            self.install(comp)
        self.ctx.info(tr(f"{name}: 照合しました（{len(files)} ファイル）", f"{name}: verified ({len(files)} file(s))") if files
                      else tr(f"{name}: 入っています", f"{name}: installed"))
        return bad

    # --- 取得 ---------------------------------------------------------------------------------
    def install(self, comp: dict) -> None:
        steps = comp["steps"]
        weights = [self._step_weight(s) for s in steps]
        total = sum(weights) or 1
        base = 0
        for s, w in zip(steps, weights):
            lo, span = base / total, w / total
            getattr(self, "_" + s["type"])(comp, s, lambda f, file=None, d=None, t=None, lo=lo, span=span:
                                           self.prog.set(comp["id"], lo + span * f, "setup_install", file, d, t))
            base += w

    def _step_weight(self, s: dict) -> int:
        t = s["type"]
        if t == "wheel_files":
            return sum(f["size"] for f in s["files"])
        if t in ("url_file", "zip_extract"):
            return s["size"]
        if t in ("hf_files", "hf_cache"):
            return sum(f["size"] for f in _files_from(s["files_from"]).values())
        if t == "source_archive":
            return _archive_size(sources()[s["source"]])
        if t == "source_wheel":
            return _archive_size(sources()[s["source"]]) + 30_000_000
        if t == "uv_pip":
            return 50_000_000
        return 30_000_000

    def _fetch(self, comp, url, dest, size, sha256, report, md5=None, label=None):
        self.prog.start_bytes()
        return self.dl.fetch(url, dest, size=size, sha256=sha256, md5=md5, label=label,
                             progress=lambda d, t: report((d / t) if t else 0.0, label or os.path.basename(dest), d, t))

    def _zip_extract(self, comp, step, report):
        tmp = os.path.join(self.root, "downloads", os.path.basename(step["url"]))
        self._fetch(comp, step["url"], tmp, step["size"], step["sha256"], report)
        dest = os.path.join(self.root, step["dest"])
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                out = os.path.normpath(os.path.join(dest, info.filename))
                if not paths.is_within(out, dest):
                    raise BackendError("DOWNLOAD_FAILED", tr(f"アーカイブに不正なパスがあります: {info.filename}",
                                                             f"The archive contains an invalid path: {info.filename}"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        os.remove(tmp)

    def _wheel_files(self, comp, step, report):
        total = sum(f["size"] for f in step["files"])
        done = 0
        for f in step["files"]:
            d0 = done
            self._fetch(comp, f["url"], os.path.join(self.root, step["dir"], f["filename"]), f["size"], f["sha256"],
                        lambda fr, file, d, t, d0=d0: report((d0 + (d or 0)) / total, file, d0 + (d or 0), total), label=f["filename"])
            done += f["size"]

    def _url_file(self, comp, step, report):
        self._fetch(comp, step["url"], os.path.join(self.root, step["dest"]), step["size"], step["sha256"], report,
                    md5=step.get("md5"))
        self.hash_cache[os.path.relpath(os.path.join(self.root, step["dest"]), self.root)] = \
            [step["size"], os.stat(os.path.join(self.root, step["dest"])).st_mtime_ns, step["sha256"]]

    def _hf_files(self, comp, step, report, cache: bool = False):
        files = _files_from(step["files_from"])
        base = _hf_base(step, self.root)
        total = sum(f["size"] for f in files.values()) or 1
        done = 0
        links = step.get("link_prefix") or {}
        for rel, f in files.items():
            d0 = done
            dest = os.path.join(base, rel.replace("/", os.sep))
            linked = next((os.path.join(self.root, tgt, rel[len(pre):].replace("/", os.sep))
                           for pre, tgt in links.items() if rel.startswith(pre)), None)
            if linked and self._verified(linked, f["sha256"]):
                # 同じファイル（テキスト版のデコーダー = 画像版の重み）はハードリンクで共有する（fetch_models.py と同じ）。
                # 置いてあるファイルは、照合したリンク元と同じ実体か、照合して合うときだけ使う（大きさが同じでも中身の違う
                # コピーを照合済みとして記録しない。点検 R11A-04）
                if not (os.path.isfile(dest) and (_same_file(linked, dest) or
                                                  (os.path.getsize(dest) == f["size"] and self._verified(dest, f["sha256"])))):
                    self._link(linked, dest, rel)
                report((d0 + f["size"]) / total, rel, d0 + f["size"], total)
            elif os.path.isfile(dest) and self._cache_hit(dest, f["sha256"]):
                # 照合済みの記録と大きさ・更新日時が同じファイルは読み直さない（取得の再試行で、取得済みの数 GB を毎回
                # 読み直していた。点検 R11A-06）
                report((d0 + f["size"]) / total, rel, d0 + f["size"], total)
            else:
                url = f"{HF_BASE}/{step['repo']}/resolve/{step['revision']}/{quote(rel)}"
                self._fetch(comp, url, dest, f["size"], f["sha256"],
                            lambda fr, file, d, t, d0=d0, rel=rel: report((d0 + (d or 0)) / total, rel, d0 + (d or 0), total),
                            label=rel)
            st = os.stat(dest)
            self.hash_cache[os.path.relpath(dest, self.root)] = [st.st_size, st.st_mtime_ns, f["sha256"]]
            done += f["size"]
        if cache:
            refs = os.path.join(_hf_cache_repo(step, self.root), "refs")
            os.makedirs(refs, exist_ok=True)
            with open(os.path.join(refs, "main"), "w", encoding="ascii", newline="") as fh:
                fh.write(step["revision"])

    def _link(self, linked: str, dest: str, rel: str) -> None:
        """照合したリンク元 linked を dest にハードリンクする（作れないボリュームではコピー）。置いてある dest は消してから
        作る（大きさの違う dest が残ると os.link が失敗する）。コピーは <名前>.part に書いてから名前を変える
        （fetch_models.py と同じ）。dest を消せないときは DOWNLOAD_FAILED（reason = write_failed）。"""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            if os.path.lexists(dest):
                os.remove(dest)
        except OSError as e:
            raise BackendError("DOWNLOAD_FAILED", tr(f"{rel}: 置いてあるファイルを入れ替えられません（{type(e).__name__}: {e}）",
                                                     f"{rel}: cannot replace the existing file ({type(e).__name__}: {e})"),
                               params={"file": rel, "reason": "write_failed", "path": dest,
                                       "error": f"{type(e).__name__}: {e}"}) from e
        self.hash_cache.pop(os.path.relpath(dest, self.root), None)
        try:
            os.link(linked, dest)
        except OSError:
            shutil.copy2(linked, dest + ".part")
            os.replace(dest + ".part", dest)

    def _hf_cache(self, comp, step, report):
        self._hf_files(comp, step, report, cache=True)

    def _verified_archive(self, comp, src: dict, tmp: str, report, use, cleanup=None, span: float = 0.9) -> None:
        """ソースのアーカイブ ZIP（tmp）を取得し、中身を pin_sources.py の記録と照合してから ``use(z, got)`` に渡す
        （got = {相対パス: ZipInfo}。ZIP は開いたまま）。

        GitHub のアーカイブは圧縮がやり直されると ZIP の SHA-256 が変わりうるので、ZIP の照合は大きさも含めて行わず、
        展開する各ファイルの大きさと SHA-256 を照合する。途中で切れた・壊れた ZIP（BadZipFile・EOFError・zlib.error）や
        中身の違う ZIP は、ZIP を閉じてから消し（Windows では開いたままだと消せない）、1 回だけ取得からやり直す。
        前の実行で壊れた ZIP が downloads\\src に残っていても、「再試行」で直る（点検 C07）。2 回目も駄目なら
        DOWNLOAD_FAILED（reason = hash_mismatch か bad_archive）。"""
        size = _archive_size(src)
        label = os.path.basename(tmp)
        for attempt in (1, 2):
            self._fetch(comp, src["url"], tmp, None, None,
                        lambda f, file, d, t: report(span * min(1.0, (d or 0) / size), file, d, size), label=label)
            try:
                with zipfile.ZipFile(tmp) as z:
                    use(z, _check_archive(z, src))
                return
            except (zipfile.BadZipFile, EOFError, zlib.error, BackendError) as e:
                if isinstance(e, BackendError) and e.params.get("reason") != "hash_mismatch":
                    raise
                why = e.message if isinstance(e, BackendError) else f"{type(e).__name__}: {e}"
                for p in (tmp, tmp + ".part"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                if cleanup:
                    cleanup()
                if attempt == 2:
                    if isinstance(e, BackendError):
                        raise
                    raise BackendError("DOWNLOAD_FAILED", tr(f"{label}: 取得したアーカイブが壊れています（{why}）",
                                                             f"{label}: the downloaded archive is corrupted ({why})"),
                                       params={"reason": "bad_archive", "file": label, "url": src["url"]}) from e
                self.ctx.warn(tr(f"{label}: 取得したアーカイブが壊れているか記録と合わないので、取り直します（{why}）",
                                 f"{label}: the downloaded archive is corrupted or does not match the record, so it is "
                                 f"downloaded again ({why})"))

    def _source_archive(self, comp, step, report):
        src = sources()[step["source"]]
        tmp = os.path.join(self.root, "downloads", "src", f"{step['source']}-{src['commit'][:12]}.zip")
        dest = os.path.join(self.root, src["dest"])
        # 新しいフォルダーに展開してから入れ替える。古い版のフォルダー（D-025 の前の DINOv2 のコードなど）に上書きすると、
        # 新しい版に無いファイル（Cell-DINO・X-Ray DINO のコードと文書）が残るため（fetch_models.py と同じ方式）
        new_dir = dest + ".gdskin-new"

        def extract(z, got):
            if os.path.isdir(new_dir):
                _rmtree(new_dir)
            for rel, info in got.items():
                out = os.path.normpath(os.path.join(new_dir, rel.replace("/", os.sep)))
                if not paths.is_within(out, new_dir):
                    raise BackendError("DOWNLOAD_FAILED", tr(f"アーカイブに不正なパスがあります: {rel}",
                                                             f"The archive contains an invalid path: {rel}"))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(z.read(info))

        self._verified_archive(comp, src, tmp, report, extract, cleanup=lambda: _rmtree_quiet(new_dir))
        os.makedirs(new_dir, exist_ok=True)
        for name, body in (step.get("write") or {}).items():
            with open(os.path.join(new_dir, name), "w", encoding="ascii", newline="\n") as f:
                f.write(body)
        if not step.get("write"):
            with open(os.path.join(new_dir, ".gdskin_source.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump({"repo": src["repo"], "commit": src["commit"], "archive": src["url"]}, f)
        if os.path.isdir(dest):
            old_dir = dest + ".gdskin-old"
            self._clear_old_dir(old_dir)
            replaced = _step_status(step, self.root, {})[0]
            os.replace(dest, old_dir)
            self.ctx.info(tr(f"{os.path.relpath(dest, self.root)}: 前のフォルダー（状態 {replaced}）を新しい版に入れ替えます",
                             f"{os.path.relpath(dest, self.root)}: replacing the previous folder (state {replaced}) with the "
                             f"new version"))
            os.replace(new_dir, dest)
            try:
                _rmtree(old_dir)
            except OSError as e:
                self.ctx.warn(tr(f"{os.path.relpath(old_dir, self.root)}: 前の版のフォルダーを消せませんでした（次の取得で消します）: {e}",
                                 f"{os.path.relpath(old_dir, self.root)}: could not remove the folder of the previous version "
                                 f"(it will be removed at the next download): {e}"))
            stale = os.path.relpath(dest, self.root) + os.sep
            self.hash_cache = {k: v for k, v in self.hash_cache.items() if not k.startswith(stale)}
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(new_dir, dest)
        for rel in src["files"]:
            out = os.path.join(dest, rel.replace("/", os.sep))
            st = os.stat(out)
            self.hash_cache[os.path.relpath(out, self.root)] = [st.st_size, st.st_mtime_ns, src["files"][rel]["sha256"]]
        for rel in step.get("touch") or []:
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if not os.path.exists(p):
                open(p, "wb").close()
        os.remove(tmp)
        report(1.0, os.path.basename(dest))

    def _clear_old_dir(self, old_dir: str) -> None:
        """前の入れ替えの残り（<dest>.gdskin-old と、消せずに退避した <dest>.gdskin-old-N）を消す。

        読み取り専用のファイルは書き込みを許してから消す。それでも消せなければ（他のアプリが開いているなど）、ジョブは
        止めずに警告して別名へ退避し、入れ替えを続ける（点検 C35）。"""
        parent, base = os.path.split(old_dir)
        for name in sorted(os.listdir(parent)) if os.path.isdir(parent) else []:
            if name.startswith(base + "-") and name[len(base) + 1:].isdigit():
                _rmtree_quiet(os.path.join(parent, name))          # 以前に退避したもの（消せれば消す）
        if not os.path.isdir(old_dir):
            return
        try:
            _rmtree(old_dir)
            return
        except OSError as e:
            n = 1
            while os.path.exists(f"{old_dir}-{n}"):
                n += 1
            try:
                os.replace(old_dir, f"{old_dir}-{n}")
            except OSError as e2:
                raise BackendError("SETUP_FAILED", tr(f"前の版のフォルダーを消せず、別名にもできません: {os.path.relpath(old_dir, self.root)}",
                                                      f"Cannot remove or rename the folder of the previous version: "
                                                      f"{os.path.relpath(old_dir, self.root)}"),
                                   f"{e}\n{e2}", {"step": "replace_source", "path": old_dir}) from e2
            self.ctx.warn(tr(f"{os.path.relpath(old_dir, self.root)}: 前の版のフォルダーを消せないので、別名（{os.path.basename(old_dir)}-{n}）"
                             f"にして続けます: {e}",
                             f"{os.path.relpath(old_dir, self.root)}: cannot remove the folder of the previous version, so it "
                             f"was renamed ({os.path.basename(old_dir)}-{n}) and the setup continues: {e}"))

    def _source_wheel(self, comp, step, report):
        """GitHub のアーカイブのソースから 1 パッケージ（utils3d）をビルドして入れる（点検 C30）。

        アーカイブは他のソースと同じく、展開後のファイルごとの大きさと SHA-256（setup_sources.json）で照合する。照合した
        バイトから ZIP を作り直し（無圧縮・名前の順）、その場で計算した SHA-256 を付けて uv pip install --require-hashes に
        渡す。GitHub が圧縮をやり直して ZIP 自体の SHA-256 が変わっても止まらず、記録と違う中身は uv を呼ぶ前に止まる。"""
        src = sources()[step["source"]]
        work = os.path.join(self.root, "downloads", "src")
        stem = f"{step['source']}-{src['commit'][:12]}"
        tmp = os.path.join(work, stem + ".zip")
        built = os.path.join(work, stem + "-verified.zip")
        req = os.path.join(work, stem + "-requirements.txt")

        def rebuild(z, got):
            with zipfile.ZipFile(built, "w", zipfile.ZIP_STORED) as out:
                for rel in sorted(got):
                    zi = zipfile.ZipInfo(src["archive_prefix"] + rel, date_time=(1980, 1, 1, 0, 0, 0))
                    zi.external_attr = 0o644 << 16
                    out.writestr(zi, z.read(got[rel]))

        def remove_built():
            for p in (built, req):
                try:
                    os.remove(p)
                except OSError:
                    pass

        try:
            self._verified_archive(comp, src, tmp, report, rebuild, cleanup=remove_built, span=0.4)
            digest = sha256_file(built, check_cancel=self.ctx.check_cancel)
            with open(req, "w", encoding="utf-8", newline="\n") as f:
                f.write(f"{step['package']} @ {pathlib.Path(built).as_uri()} \\\n    --hash=sha256:{digest}\n")
            report(0.5, step["package"])
            args = ["pip", "install", "--python", _venv_python(self.root), "--no-deps", "--require-hashes", "-r", req]
            if step.get("no_build_isolation"):
                args.append("--no-build-isolation")
            if self.reinstall:
                args.append("--reinstall")
            self._run_uv(args, tr(f"{step['package']}（固定したソースからのビルド）", f"{step['package']} (build from the pinned source)"))
        finally:
            remove_built()
            try:
                os.remove(tmp)
            except OSError:
                pass
        report(1.0, step["package"])

    # --- uv -------------------------------------------------------------------------------------
    def _uv(self) -> str:
        exe = os.path.join(self.root, "tools", "uv", "uv.exe")
        if not os.path.isfile(exe):
            raise BackendError("SETUP_FAILED", tr("uv がありません（先に uv を取得してください）", "uv is missing (download uv first)"),
                               params={"step": "uv"})
        return exe

    def _uv_env(self) -> dict:
        env = {
            "UV_CACHE_DIR": os.path.join(self.root, "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": os.path.join(self.root, "python"),
            "UV_NO_CONFIG": "1", "UV_PYTHON_DOWNLOADS": "automatic", "UV_NO_PROGRESS": "1",
            "UV_HTTP_TIMEOUT": "180", "UV_HTTP_RETRIES": "5", "NO_COLOR": "1",
            # Windows の証明書ストアも使う（uv の既定は同梱の Mozilla のルート証明書だけ。HTTPS を検査するウイルス対策ソフト・
            # 社内プロキシの PC では uv の手順だけが失敗していた。中身は --require-hashes で固定のまま。点検 C08。uv 0.11 以降）
            "UV_SYSTEM_CERTS": "true",
        }
        # 利用者の環境の設定（別の索引・仮想環境・Python の場所）を引き継がない
        for k in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
                  "PIP_FIND_LINKS", "PIP_REQUIRE_VIRTUALENV", "UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL",
                  "UV_DEFAULT_INDEX", "UV_FIND_LINKS", "UV_PYTHON", "UV_SYSTEM_PYTHON", "UV_NO_BUILD_ISOLATION",
                  "UV_REQUIRE_HASHES", "UV_LINK_MODE", "UV_OFFLINE", "UV_NO_INDEX", "UV_CONSTRAINT", "UV_OVERRIDE",
                  "UV_PRERELEASE", "UV_INDEX_STRATEGY", "UV_KEYRING_PROVIDER", "UV_TOOL_DIR"):
            env[k] = None
        # 上に無い UV_* も、自分で設定するもの以外はすべて外す（UV_CONFIG_FILE は UV_NO_CONFIG があっても読まれ、
        # UV_NO_MANAGED_PYTHON は --python-preference と衝突して仮想環境の作成が失敗する。点検 R11B-01）。
        # プロキシ・証明書の変数（HTTPS_PROXY・SSL_CERT_FILE など）は UV_ で始まらないので引き継ぐ
        for k in os.environ:
            if k.upper().startswith("UV_") and k.upper() not in env:
                env[k] = None
        return env

    def _run_uv(self, args: list[str], step_name: str, on_line=None, attempts: int = 2) -> str:
        cmd = [self._uv(), *args]
        out = ""
        for n in range(1, attempts + 1):
            rc, out = self.ctx.run_process(cmd, on_line, env=self._uv_env(), cwd=self.root)
            if rc == 0:
                return out
            reason = uv_failure_reason(out)
            if reason == "disk_full":
                # 空き不足はやり直しても直らない。通信の確認ではなく空きを増やす案内にする（点検 R11A-03）
                from ..errors import failure_text
                raise BackendError("DISK_SPACE_LOW", tr(f"{step_name}: 取得先のドライブの空きが足りなくなりました（uv の終了コード {rc}）",
                                                        f"{step_name}: the destination drive ran out of free space (uv exit "
                                                        f"code {rc})"),
                                   failure_text(out, 30), {"step": step_name, "exit_code": rc, "reason": "uv_disk_full",
                                                           "root": self.root})
            if reason not in ("network", "write_failed") or n == attempts:
                from ..errors import failure_text
                raise BackendError("SETUP_FAILED" if reason == "uv" else "DOWNLOAD_FAILED",
                                   tr(f"{step_name}: 失敗しました（uv の終了コード {rc}）", f"{step_name} failed (uv exit code {rc})"),
                                   failure_text(out, 30), {"step": step_name, "exit_code": rc, "reason": reason})
            if reason == "write_failed":
                self.ctx.warn(tr(f"{step_name}: ファイルに書き込めませんでした。ほかのアプリやウイルス対策ソフトがファイルを使っている"
                                 f"可能性があるので、もう一度実行します（{n}/{attempts}）",
                                 f"{step_name}: a file could not be written. Another app or antivirus software may be "
                                 f"using it, so it is run again ({n}/{attempts})"))
            else:
                self.ctx.warn(tr(f"{step_name}: 通信エラーのようなので、もう一度実行します（{n}/{attempts}）",
                                 f"{step_name}: this looks like a network error, so it is run again ({n}/{attempts})"))
            time.sleep(5)
        return out

    def _uv_python(self, comp, step, report):
        py = self.m["python"]
        venv = os.path.join(self.root, "venv")
        rebuild = os.path.isdir(venv) and (_venv_version(self.root) != py or not os.path.isfile(_venv_python(self.root)))
        if rebuild and not self.allow_venv_rebuild:
            raise _venv_rebuild_error(self.root, py)      # アプリが作ったものでない venv は消さない（点検 R11B-01）
        report(0.05, f"Python {py}")
        self._run_uv(["python", "install", py, "--no-bin", "--no-registry"] + (["--reinstall"] if self.reinstall else []),
                     tr("Python のインストール", "Python installation"))
        report(0.6, "venv")
        if rebuild:
            _rmtree(venv)       # 作りかけ・別の版・python.exe の無い仮想環境（取得先の中だけ。点検 C29）
        if not os.path.isdir(venv):
            self._run_uv(["venv", venv, "--python", py, "--python-preference", "only-managed"],
                         tr("仮想環境の作成", "Virtual environment creation"))

            def fn(st):
                st.setdefault("roots", {}).setdefault(_root_key(self.root), {"path": self.root})["venv_created_by_app"] = True
            update_state(fn)    # 次に作り直すとき、許可を求めずに消してよい venv（点検 R11B-01）
        report(1.0, "venv")

    _DL_RE = re.compile(r"^\s*Downloading (\S+) \(([\d.]+)\s*(B|KiB|MiB|GiB)\)")
    _DONE_RE = re.compile(r"^\s*Downloaded (\S+)")
    _UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}

    def _uv_pip(self, comp, step, report):
        req = os.path.join(paths.TRELLIS_SCRIPTS, step["requirements"])
        args = ["pip", "install", "--python", _venv_python(self.root), "--no-deps", "--require-hashes", "-r", req]
        if step.get("local_dir"):
            args += ["--no-index", "--find-links", os.path.join(self.root, step["local_dir"])]
        if step.get("find_links"):
            args += ["--find-links", self.m["kaolin_links"]]
        if step.get("index_url"):
            args += ["--index-url", self.m["torch_index"]]
        if step.get("no_build_isolation"):
            args += ["--no-build-isolation"]
        if step.get("only_binary"):
            args += ["--only-binary", ":all:"]          # ソースからのビルドは source_wheel の手順だけ（点検 C37）
        if self.reinstall:
            args += ["--reinstall"]                     # 修復（点検 SF16）。キャッシュは先に uv cache clean で空にする
        total = comp["download_bytes"] if not step.get("local_dir") else 0
        sizes: dict[str, int] = {}
        done = [0]

        def on_line(line: str) -> None:
            m = self._DL_RE.match(line)
            if m:
                sizes[m.group(1)] = int(float(m.group(2)) * self._UNITS[m.group(3)])
                report(min(0.9, done[0] / total) if total else 0.3, m.group(1), done[0], total or None)
                return
            m = self._DONE_RE.match(line)
            if m and m.group(1) in sizes:
                done[0] += sizes[m.group(1)]
                report(min(0.9, done[0] / total) if total else 0.5, m.group(1), done[0], total or None)
            elif line.startswith("Installed ") or line.startswith("Prepared "):
                report(0.95, line.strip())

        self._run_uv(args, step["requirements"], on_line)
        if step.get("cleanup") and step.get("local_dir"):
            d = os.path.join(self.root, step["local_dir"])
            rec = load_state().get("roots", {}).get(_root_key(self.root), {})
            if rec.get("created_by_app") is True:
                # アプリが作った取得先（取得の前は無い・空だった）は、取得の作業フォルダーを丸ごと消す。今の定義に無い
                # ファイル（取消で残った別の版の .part、固定の版が変わる前に取得した wheel。torch は数 GB）も残さない
                # （点検 D2_backend-05。印の無い以前の版の取得先は、取得の記録だけでは利用者のフォルダーと区別できないので
                # 下の名前の一覧の消し方にする）
                _rmtree_quiet(d)
            else:
                # 同じ部品の wheel_files の手順で取得したファイル（と途中の .part）だけを消し、フォルダーが空になったときだけ
                # フォルダーも消す（取得先の downloads が利用者の Downloads と同じ場所のとき、Downloads\torch の中身を丸ごと
                # 消さない。点検 R20B-02）
                # 修復は wheel_files を先に済ませて、手順から外した部品を渡すので、定義の手順からも探す
                defined = [s for mc in manifest()["components"] if mc["id"] == comp["id"] for s in mc["steps"]]
                for s in comp["steps"] + defined:
                    if s["type"] == "wheel_files" and _root_key(os.path.join(self.root, s["dir"])) == _root_key(d):
                        for f in s["files"]:
                            for name in (f["filename"], f["filename"] + ".part"):
                                try:
                                    os.remove(os.path.join(d, name))
                                except OSError:
                                    pass
                try:
                    os.rmdir(d)
                except OSError:
                    pass                # 空でない（取得の前からあったファイル）・無い
        report(1.0, step["requirements"])

    # --- 動作確認 ---------------------------------------------------------------------------------
    # CHECK_CODE の q = … からは、生成が使う GPU のカーネルを 1 回ずつ動かす（この GPU に対応していない部品を、セットアップの
    # 最後に見つける）。この説明はコードの文字列の外に書く（文字列の中の日本語は 2 言語対応の網羅のテストが訳を求めるため）
    CHECK_CODE = r"""
import importlib.util, os, sys
sys.path[:0] = [os.environ['TRELLIS_SRC'], os.environ['TRELLIS_SCRIPTS']]
os.environ.setdefault('ATTN_BACKEND', 'xformers')
import torch, xformers.ops, spconv.pytorch, kaolin, utils3d, scipy, xatlas, pyvista, trimesh, cv2, open3d, rembg
assert torch.cuda.is_available(), 'CUDA is not available'
print('torch', torch.__version__, 'cuda', torch.version.cuda, torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0))
from trellis.pipelines import TrellisImageTo3DPipeline
import postprocess_alt
v = torch.tensor([[-0.5, -0.5, 0, 1], [0.5, -0.5, 0, 1], [0, 0.5, 0, 1]], device='cuda')
fid, _ = postprocess_alt.rasterize(v, torch.tensor([[0, 1, 2]], device='cuda'), 64, 64)
assert int((fid >= 0).sum()) > 0
q = torch.randn(1, 64, 2, 32, device='cuda', dtype=torch.float16)
assert tuple(xformers.ops.memory_efficient_attention(q, q, q).shape) == tuple(q.shape)
idx = torch.tensor([[0, 1, 1, 1], [0, 1, 1, 2], [0, 2, 1, 1]], dtype=torch.int32, device='cuda')
x = spconv.pytorch.SparseConvTensor(torch.randn(3, 8, device='cuda'), idx, [4, 4, 4], 1)
y = spconv.pytorch.SubMConv3d(8, 8, 3, indice_key='gdskin_check').cuda()(x)
assert tuple(y.features.shape) == (3, 8)
torch.cuda.synchronize()
print('gpu kernels ok: xformers memory_efficient_attention, spconv SubMConv3d')
optional = ['nvdiffrast', 'diff_gaussian_rasterization', 'pymeshfix', 'igraph']
present = [m for m in optional if importlib.util.find_spec(m) is not None]
print('runtime: dev-only packages present:', present)
print('imports ok')
"""

    def runtime_check(self) -> dict:
        """setup_trellis_env.ps1 の「Consistency and import check」と同じ確認（GPU で小さな描画と、xformers・spconv の
        GPU のカーネルの呼び出しまで。点検 C32）。"""
        t0 = time.monotonic()
        lines: list[str] = []

        def save(result: dict) -> None:
            def fn(st):
                r = st.setdefault("roots", {}).setdefault(_root_key(self.root), {"path": self.root})
                r["runtime_check"] = result
            update_state(fn)

        # 始める前に「未確認」を記録する。pip check の失敗・取消・想定外の例外で終わっても前回の ok が残らず、次の取得が
        # 動作確認を省かない（run_install の want_check。点検 R11A-03）
        save({"ok": False, "at": logs.now_iso(), "step": "running"})
        if os.path.isfile(os.path.join(self.root, "tools", "uv", "uv.exe")):
            try:
                self._run_uv(["pip", "check", "--python", _venv_python(self.root)],
                             tr("パッケージの整合性の確認", "Package consistency check"), attempts=1)
            except BackendError:
                save({"ok": False, "at": logs.now_iso(), "seconds": round(time.monotonic() - t0, 1), "torch": None,
                      "step": "pip_check"})
                raise
        from ..providers.trellis import runtime_env      # 生成と同じ環境変数（TORCH_HOME・HF_HOME・注意機構・使う GPU など）
        env = {**runtime_env(self.root),
               "TRELLIS_SRC": os.path.join(self.root, "src", "TRELLIS"), "TRELLIS_SCRIPTS": paths.TRELLIS_SCRIPTS,
               "PYTHONPATH": None, "PYTHONHOME": None, "VIRTUAL_ENV": None}
        # -I は PYTHONIOENCODING を無視するので、-X utf8 で日本語の OS のエラー文（DLL の読み込みの失敗など）も UTF-8 で
        # 受け取る（点検 C41）
        rc, out = self.ctx.run_process([_venv_python(self.root), "-I", "-X", "utf8", "-c", self.CHECK_CODE], lines.append,
                                       env=env, cwd=self.root)
        ok = rc == 0 and "imports ok" in out
        gpu = next((ln for ln in lines if ln.startswith("torch ")), None)
        result = {"ok": ok, "at": logs.now_iso(), "seconds": round(time.monotonic() - t0, 1), "torch": gpu}
        save(result)
        if not ok:
            from ..errors import DRIVER_OLD_RE, classify_output, failure_text
            code = classify_output(out, "SETUP_FAILED")
            if code == "LIBRARY_MISSING":
                code = "SETUP_FAILED"
            params = {"step": "runtime_check", "exit_code": rc}
            if code == "GPU_NOT_FOUND" and DRIVER_OLD_RE.search(out):
                params["reason"] = "driver_old"
            elif code == "SETUP_FAILED":
                # Visual C++ 再頒布可能パッケージが古い・無いときの失敗は、その案内にする（点検 C31）。それ以外は、
                # 仮想環境を消してからの再試行を案内する（再試行だけでは入れ直さないため。点検 C34）
                params["reason"] = "vc_runtime" if vc_runtime_suspected(out) else "runtime_check"
            raise BackendError(code, tr("TRELLIS 実行環境の動作確認（import と GPU の小さな計算）に失敗しました",
                                        "The TRELLIS runtime self-test (imports and a small GPU computation) failed"),
                               failure_text(out, 30), params)
        self.ctx.info(tr(f"動作確認: {gpu}（{result['seconds']} 秒）", f"Self-test: {gpu} ({result['seconds']} s)"))
        return result


# Visual C++ 再頒布可能パッケージ（msvcp140.dll など）が古い・無いときの、動作確認の出力（点検 C31）。WinError 126 は
# CUDA など他の DLL の欠けでも出るので、torch\lib の DLL の読み込みの失敗に限り、環境チェックの vc_runtime が warning の
# ときだけ専用の理由にする
_VC_RUNTIME_RE = re.compile(r"Microsoft Visual C\+\+ Redistributable is not installed|msvcp140|"
                            r"DLL load failed while importing onnxruntime_pybind11_state|"
                            r"\[WinError 12[67]\][^\n]*torch[\\/]+lib", re.I)


def vc_runtime_suspected(out: str, item=None) -> bool:
    """動作確認の出力（全体）が Visual C++ 再頒布可能パッケージの不足を示し、かつ環境チェックの vc_runtime が warning か。"""
    if not _VC_RUNTIME_RE.search(out or ""):
        return False
    if item is None:
        from ..envcheck import vc_runtime_item
        item = vc_runtime_item()
    return item is not None and item.status == "warning"
