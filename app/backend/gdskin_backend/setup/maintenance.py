"""TRELLIS 実行環境の管理（点検 SF04・SF16。D-004 の設計メモ 5「確認・削除できるようにする」）。

* 削除（ジョブ ``setup_remove``）: テキスト→3D の任意のモデル（TRELLIS-text-xlarge と CLIP）だけ、または実行環境
  全体を消す。全体では、アプリが作る既知のもの（tools・python・venv・uv-cache・src・models・torch-home・hf-home・u2net・
  downloads・warp-cache・consents.json）だけを消し、利用者が置いたほかのファイルは残して知らせる。
* 照合し直し・修復（ジョブ ``setup_repair``）: モデルとソースの全ファイルの SHA-256 をハッシュの記録を使わずに読み直し、
  合わないファイルだけを取り直す（models）。uv のキャッシュを空にしてから、Python・PyTorch・パッケージを uv の
  ``--reinstall`` で取り直して入れ直し、動作確認をする（venv）。どちらも利用者が押したときだけ動く。

対象はアプリのセットアップが作った取得先だけ（:func:`root_info`）。既存の環境として登録したもの（use_existing）、
取得の前から中身があったもの、開発用のリポジトリの隣の trellis-runtime は書き換えない。3D 生成・変換し直し（列 gpu）・
背景除去の確認（列 preview）・ほかのセットアップの実行中は断る（逆向きは pipeline の run_generate・run_bg_preview が
列 setup の実行中を断る。このジョブも列 setup で動く）。

削除で空く容量は、ハードリンク（テキスト版のデコーダー = 画像版の重み、uv のキャッシュ = venv のファイル）を
ファイルの実体（ボリュームとファイル番号）ごとに 1 回だけ数え、消すものの外にもリンクが残るファイルは数えない。
"""
from __future__ import annotations

import json
import os
import stat

from .. import consent, logs, paths
from .. import setup as S          # setup のパッケージ（テストが差し替える manifest などを、呼ぶときに引く）
from ..errors import BackendError, JobCancelled
from ..i18n import tr

SCOPES = ("text", "all")
# 実行環境全体の削除で消すもの（アプリのセットアップ・生成が取得先の直下に作るものだけ）
ALL_ENTRIES = ("tools", "python", "venv", "uv-cache", "src", "models", "torch-home", "hf-home", "u2net", "downloads",
               "warp-cache", "consents.json", "consents.json.tmp")
VENV_COMPONENTS = ("python", "torch", "packages")
_VERIFY_STEPS = ("hf_files", "hf_cache", "url_file", "source_archive")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PROGRESS_EVERY = 400


# ---------------------------------------------------------------------------
# 対象の判定
# ---------------------------------------------------------------------------
def dev_runtime() -> str:
    """開発用の TRELLIS 実行環境（リポジトリの隣の trellis-runtime）。削除・修復の対象にしない。"""
    return os.path.join(os.path.dirname(paths.REPO_ROOT), "trellis-runtime")


def _app_default_roots() -> set[str]:
    """アプリのセットアップの既定の取得先（setup.default_root の 2 つの候補）。"""
    drive = (os.environ.get("SystemDrive") or "C:") + os.sep
    return {S._root_key(os.path.join(paths.home(), "trellis-runtime")),
            S._root_key(os.path.join(drive, "GDSkinStudio", "trellis-runtime"))}


def root_info(root: str | None, st: dict | None = None) -> dict:
    """取得先が削除・修復の対象（アプリのセットアップが作った取得先）か → {root, exists, app_created, reason}。

    reason（対象でないとき）: no_root・dev_runtime（開発時の、開発用の実行環境）・unsafe_path（ドライブの直下、データ
    フォルダー・アプリのフォルダーを含む（同じ場所も）。開発時はリポジトリの中も）・existing_runtime（既存の環境として
    登録した、または取得の前から中身があった）・not_app_root（アプリの取得の記録が無い。記録に作成の印が無い以前の版の
    取得先は、既定の取得先にあるときだけ対象）・not_found（記録もフォルダーも無い。削除した後・まだ取得していない）。

    配布物（paths.IS_DIST）では、展開したフォルダーの隣の trellis-runtime は開発用の実行環境ではない（設定の
    trellis_root_candidates と同じ規則）。全角の利用者名の PC の既定の取得先 %SystemDrive%\\GDSkinStudio\\trellis-runtime
    の隣や上にアプリを展開しても、記録に従って対象にする（点検 U-D2-R1）。展開したフォルダーそのもの・それを含む場所は
    断る（展開したフォルダーの python はアプリ自身の Python で、全体の削除で消す名前と同じ）。"""
    out = {"root": root or "", "exists": bool(root) and os.path.isdir(root), "app_created": False, "reason": None,
           "has_runtime": False}
    if not root or not os.path.isabs(root):
        out["reason"] = "no_root"
        return out
    root = os.path.abspath(root)
    key = S._root_key(root)
    if not paths.IS_DIST and key == S._root_key(dev_runtime()):
        out["reason"] = "dev_runtime"
    elif (os.path.dirname(root) == root or paths.is_within(paths.home(), root) or paths.is_within(paths.REPO_ROOT, root)
          or (not paths.IS_DIST and paths.is_within(root, paths.REPO_ROOT))):
        out["reason"] = "unsafe_path"
    else:
        rec = ((st if st is not None else S.load_state()).get("roots") or {}).get(key) or {}
        created = rec.get("created_by_app")
        if rec.get("found_at") or created is False:
            out["reason"] = "existing_runtime"
        elif not (rec.get("last_install") or rec.get("components") or created):
            out["reason"] = "not_app_root" if out["exists"] else "not_found"      # 無い（削除した・まだ取得していない）
        elif created is None and key not in _app_default_roots():
            out["reason"] = "not_app_root"
        else:
            out["app_created"] = True
    # アプリが作るもの（consents.json 以外）が 1 つでもあるか。全体の削除で利用者のファイルだけが残った取得先は、印が
    # 残って対象のままなので、画面はこれが無ければ照合し直し・入れ直しを押せなくする（点検 U-D2-R3）
    out["has_runtime"] = bool(out["exists"]) and any(os.path.lexists(os.path.join(root, n))
                                                     for n in ALL_ENTRIES if not n.startswith("consents"))
    return out


def _require_app_root(root: str) -> dict:
    info = root_info(root)
    if not info["app_created"]:
        msg = {"dev_runtime": tr("開発用の TRELLIS 実行環境は、アプリから削除・修復しません",
                                 "The app does not remove or repair a development TRELLIS runtime"),
               "existing_runtime": tr("既存の TRELLIS 実行環境（アプリが作ったものではない取得先）は、アプリから削除・修復しません",
                                      "The app does not remove or repair an existing TRELLIS runtime (a destination not "
                                      "created by the app)"),
               "unsafe_path": tr("この場所はアプリから削除・修復できません", "The app cannot remove or repair this location")}.get(
            info["reason"], tr("アプリのセットアップが作った取得先ではないため、アプリから削除・修復しません",
                               "The app does not remove or repair this destination because it was not created by the app's "
                               "setup"))
        raise BackendError("INVALID_INPUT", msg, params={"reason": "maint_" + str(info["reason"]), "root": root})
    return info


def _resolve_root(svc, params: dict) -> str:
    from ..settings import resolve_trellis_root
    from . import default_root
    s = svc.settings.get()
    return os.path.abspath(params.get("root") or s.get("trellis_root") or resolve_trellis_root(s) or default_root())


def check_idle(svc, own_job=None) -> None:
    """3D 生成・変換し直し（列 gpu）・背景除去の確認（列 preview）の順番待ち・実行中と、ほかのセットアップの実行中は断る。"""
    def others(lane, running_only=False):
        return [j for j in svc.jobs.active(lane) if j is not own_job and (not running_only or j.state == "running")]
    if others("gpu"):
        raise BackendError("BUSY", tr("3D 生成・変換の実行中・順番待ちの間は、TRELLIS 実行環境の削除・修復はできません",
                                      "The TRELLIS runtime cannot be removed or repaired while 3D generation or conversion is "
                                      "running or queued"),
                           params={"reason": "maint_generate_running"})
    if others("preview"):
        raise BackendError("BUSY", tr("背景除去の確認の実行中は、TRELLIS 実行環境の削除・修復はできません",
                                      "The TRELLIS runtime cannot be removed or repaired while the background removal check is "
                                      "running"),
                           params={"reason": "maint_preview_running"})
    if others("setup", running_only=True):
        raise BackendError("BUSY", tr("セットアップの実行中は、TRELLIS 実行環境の削除・修復はできません",
                                      "The TRELLIS runtime cannot be removed or repaired while the setup is running"),
                           params={"reason": "maint_setup_running"})


# ---------------------------------------------------------------------------
# 削除
# ---------------------------------------------------------------------------
def _text_components() -> list[dict]:
    return [c for c in S.manifest()["components"] if c["group"] == "text"]


def targets(root: str, scope: str) -> list[str]:
    """消すもの（取得先からの相対パス）。"""
    if scope == "all":
        return list(ALL_ENTRIES)
    if scope != "text":
        raise BackendError("INVALID_INPUT", tr(f"削除の範囲は {' / '.join(SCOPES)} のどちらかです",
                                               f"The removal scope must be one of {' / '.join(SCOPES)}"),
                           params={"reason": "maint_scope"})
    out = []
    for c in _text_components():
        for step in c["steps"]:
            if step["type"] == "hf_files":
                out.append(step["dest"])
            elif step["type"] == "hf_cache":
                repo = S._hf_cache_repo(step, root)
                out.append(os.path.relpath(repo, root))
                out.append(os.path.join(step["cache"], ".locks", os.path.basename(repo)))
    return [os.path.normpath(p) for p in out]


def _is_link(st: os.stat_result) -> bool:
    """シンボリックリンク・ジャンクションなど（中は辿らない）。"""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _iter_files(path: str, st: os.stat_result, check_cancel=None):
    """path の中の通常のファイルの lstat（リンクは辿らない。Windows の lstat はファイル番号と リンクの数も返す）。"""
    if _is_link(st):
        return
    if stat.S_ISDIR(st.st_mode):
        try:
            with os.scandir(path) as it:
                names = [e.path for e in it]
        except OSError:
            return
        for p in names:
            if check_cancel:
                check_cancel()
            try:
                est = os.lstat(p)
            except OSError:
                continue
            yield from _iter_files(p, est, check_cancel)
    elif stat.S_ISREG(st.st_mode):
        yield st


def removal_plan(root: str, scope: str, check_cancel=None) -> dict:
    """消すものと、空く容量（ハードリンクを数え直した値）。何も変えない。"""
    root = os.path.abspath(root)
    rels = targets(root, scope)
    entries = []
    inodes: dict[tuple, list] = {}          # (ボリューム, ファイル番号) → [大きさ, リンクの数, 消すものの中の数]
    single = 0
    for rel in rels:
        p = os.path.join(root, rel)
        e = {"path": rel, "files": 0, "bytes": 0, "exists": False}
        try:
            st = os.lstat(p)
        except OSError:
            entries.append(e)
            continue
        e["exists"] = True
        for fst in _iter_files(p, st, check_cancel):
            e["files"] += 1
            e["bytes"] += fst.st_size
            if fst.st_ino and fst.st_nlink > 1:
                rec = inodes.setdefault((fst.st_dev, fst.st_ino), [fst.st_size, fst.st_nlink, 0])
                rec[2] += 1
            else:
                single += fst.st_size
        entries.append(e)
    freed = single + sum(size for size, nlink, n in inodes.values() if n >= nlink)
    shared = sum(size for size, nlink, n in inodes.values() if n < nlink)
    kept = []
    if scope == "all" and os.path.isdir(root):
        known = {os.path.normcase(n) for n in ALL_ENTRIES}
        kept = sorted(n for n in os.listdir(root) if os.path.normcase(n) not in known)
    comps = [c["id"] for c in _text_components()] if scope == "text" else [c["id"] for c in S.manifest()["components"]]
    clip = consent.status(root if os.path.isdir(root) else None)["accepted"]
    return {"root": root, "scope": scope, "entries": entries,
            "files": sum(e["files"] for e in entries), "apparent_bytes": sum(e["bytes"] for e in entries),
            "freed_bytes": freed, "shared_bytes": shared, "kept": kept, "components": comps,
            "clip_consent_removed": clip, "nothing": not any(e["exists"] for e in entries)}


def plan(svc, root: str | None, scope: str) -> dict:
    """画面の確認用（GET /setup/remove-plan）: 対象かどうかと、消すもの・空く容量。"""
    root = _resolve_root(svc, {"root": root})
    info = root_info(root)
    out = {"root": root, "scope": scope, **info}
    if info["app_created"] and os.path.isdir(root):
        out.update(removal_plan(root, scope))
    return out


def _remove_path(path: str, st: os.stat_result, on_file, failed: list, check_cancel) -> None:
    """path を消す（リンク・ジャンクションはリンクだけを消し、先は辿らない。読み取り専用は書き込みを許して消し直す）。"""
    if _is_link(st):
        try:
            os.unlink(path)          # Windows ではディレクトリのリンク・ジャンクションも消せる（先は消さない）
        except OSError:
            try:
                os.rmdir(path)
            except OSError as e:
                failed.append((path, str(e)))
        return
    if stat.S_ISDIR(st.st_mode):
        try:
            with os.scandir(path) as it:
                names = [e.path for e in it]
        except OSError as e:
            failed.append((path, str(e)))
            return
        for p in names:
            check_cancel()
            try:
                est = os.lstat(p)
            except OSError:
                continue
            _remove_path(p, est, on_file, failed, check_cancel)
        try:
            os.rmdir(path)
        except OSError as e:
            if not any(f[0].startswith(path + os.sep) for f in failed[-50:]):
                failed.append((path, str(e)))
        return
    try:
        os.unlink(path)
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.unlink(path)
        except OSError as e:
            failed.append((path, str(e)))
            return
    except FileNotFoundError:
        return
    except OSError as e:
        failed.append((path, str(e)))
        return
    on_file(st)


def _drop_clip_consent(root: str) -> bool:
    """consents.json から CLIP の同意の記録を消す（テキスト→3D のモデルを消したとき。取り直すときは同意し直す）。"""
    p = os.path.join(root, "consents.json")
    try:
        with open(p, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, ValueError):
        return False
    key = consent._fetch_models().CONSENT_KEY
    if not isinstance(records, dict) or key not in records:
        return False
    del records[key]
    if records:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    else:
        os.remove(p)
    return True


def summarize_remove(p: dict) -> dict:
    return {"scope": p.get("scope")}


def run_remove(svc, ctx, params: dict) -> dict:
    """ジョブ ``setup_remove``: params = {root（省略時は使っている取得先）, scope: text / all}。"""
    scope = params.get("scope")
    if scope not in SCOPES:
        raise BackendError("INVALID_INPUT", tr(f"削除の範囲は {' / '.join(SCOPES)} のどちらかです",
                                               f"The removal scope must be one of {' / '.join(SCOPES)}"),
                           params={"reason": "maint_scope"})
    root = _resolve_root(svc, params)
    _require_app_root(root)
    check_idle(svc, getattr(ctx, "job", None))
    if not os.path.isdir(root):
        raise BackendError("INVALID_INPUT", tr("取得先のフォルダーがありません", "The destination folder does not exist"),
                           params={"reason": "maint_not_found", "root": root})
    ctx.progress(0.0, "setup_remove")
    pl = removal_plan(root, scope, ctx.check_cancel)
    what = tr("テキスト→3D のモデル", "text → 3D models") if scope == "text" else tr("全体", "everything")
    ctx.info(tr(f"TRELLIS 実行環境の削除（{what}）: {pl['files']} ファイル、"
                f"空く容量 約 {pl['freed_bytes'] / 1e9:.2f} GB（ハードリンクで共有していて空かない分 {pl['shared_bytes'] / 1e9:.2f} GB）",
                f"Removing the TRELLIS runtime ({what}): {pl['files']} file(s), about {pl['freed_bytes'] / 1e9:.2f} GB to be "
                f"freed ({pl['shared_bytes'] / 1e9:.2f} GB shared through hard links will not be freed)"))
    free0 = S._free_bytes(root)
    total = max(pl["files"], 1)
    done = [0]
    failed: list = []
    # 実際に消したファイルで数え直す空き（計画と同じ規則。ハードリンクは実体ごとに 1 回、消したものの外にリンクが残る
    # ものは数えない）。消せなかったファイルがある・中止したときも、表示と記録が見積もりにならないように（点検 U-D2-R4）
    single = [0]
    linked: dict[tuple, list] = {}

    def on_file(fst):
        done[0] += 1
        if fst.st_ino and fst.st_nlink > 1:
            linked.setdefault((fst.st_dev, fst.st_ino), [fst.st_size, fst.st_nlink, 0])[2] += 1
        else:
            single[0] += fst.st_size
        if done[0] % _PROGRESS_EVERY == 0:
            ctx.progress(min(0.99, done[0] / total), "setup_remove", {"done": done[0], "total": total})

    def freed_now() -> int:
        return single[0] + sum(size for size, nlink, n in linked.values() if n >= nlink)

    removed = []
    clip_dropped = False
    root_removed = False
    interrupted: BaseException | None = None
    try:
        for e in pl["entries"]:
            if not e["exists"]:
                continue
            p = os.path.join(root, e["path"])
            n0 = len(failed)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            _remove_path(p, st, on_file, failed, ctx.check_cancel)
            if len(failed) == n0:
                removed.append(e["path"])
        if scope == "text":
            try:
                clip_dropped = _drop_clip_consent(root)
            except OSError as e:
                failed.append((os.path.join(root, "consents.json"), str(e)))
        if scope == "all" and not failed and not pl["kept"]:
            try:
                os.rmdir(root)
                root_removed = True
            except OSError:
                pass
    except BaseException as exc:        # 中止など: 消した分を記録してから伝える（点検 U-D2-R4）
        interrupted = exc
    key = S._root_key(root)
    now = logs.now_iso()
    rels = [os.path.normcase(r) + os.sep for r in removed]
    freed = freed_now()
    last = {"at": now, "scope": scope, "freed_bytes": freed, "estimated_freed_bytes": pl["freed_bytes"], "files": done[0],
            "failed": len(failed)}
    if interrupted is not None:
        last["cancelled" if isinstance(interrupted, JobCancelled) else "interrupted"] = True
    complete = interrupted is None and not failed

    def fn(st):
        r = (st.get("roots") or {}).get(key)
        if r is None:
            return
        if scope == "all" and complete:
            pend = st.get("pending_root")
            if isinstance(pend, dict) and isinstance(pend.get("path"), str) and S._root_key(pend["path"]) == key:
                st.pop("pending_root", None)     # 途中で止まった取得先の記録も消す（点検 R20B-03）
            if root_removed:
                st["roots"].pop(key, None)       # 次に同じ場所へ取得すれば、新しく作った取得先として記録し直す
            else:
                # 利用者のファイルを残した（取得先のフォルダーが残る）: アプリが作った取得先の印だけを残す。消すと、同じ場所へ
                # 取得し直したときに「取得の前から中身があった」とみなされ、削除・修復の対象から外れる（点検 U-D2-R3）
                st["roots"][key] = {"path": r.get("path") or root, "created_by_app": True, "last_remove": last}
            return
        r["file_hashes"] = {k: v for k, v in (r.get("file_hashes") or {}).items()
                            if not os.path.normcase(k).startswith(tuple(rels))}
        if interrupted is None:
            for cid in pl["components"]:
                (r.get("components") or {}).pop(cid, None)
        r["last_remove"] = last
    S.update_state(fn)
    if interrupted is not None:
        svc.invalidate_environment()
        ctx.info(tr(f"TRELLIS 実行環境の削除を中止しました: {done[0]} ファイルを削除、空いた容量 約 {freed / 1e9:.2f} GB",
                    f"Removal of the TRELLIS runtime was stopped: {done[0]} file(s) removed, about {freed / 1e9:.2f} GB freed"))
        raise interrupted
    if scope == "all" and not failed:
        s = svc.settings.get()
        if s.get("trellis_root") and S._root_key(s["trellis_root"]) == key:
            svc.update_settings({"trellis_root": ""})       # 自動の探索に戻す
    svc.invalidate_environment()
    free1 = S._free_bytes(root)
    if failed:
        ctx.warn(tr(f"消せなかったファイル・フォルダーがあります（{len(failed)} 件。ほかのアプリが開いている可能性があります）: ",
                    f"{len(failed)} file(s) or folder(s) could not be removed (another app may have them open): ")
                 + ", ".join(os.path.relpath(f[0], root) for f in failed[:3]))
    if pl["kept"]:
        ctx.warn(tr("アプリが作ったものではないファイル・フォルダーは残しました: ", "Kept files and folders not created by the app: ")
                 + ", ".join(pl["kept"][:10]))
    if clip_dropped:
        ctx.info(tr("CLIP の注意事項への同意の記録を消しました（テキスト→3D のモデルを取得し直すときは、もう一度同意します）",
                    "Removed the record of acceptance of the CLIP notice (you accept it again when downloading the text → 3D "
                    "models again)"))
    ctx.info(tr(f"TRELLIS 実行環境の削除が終わりました: {len(removed)} 項目、空いた容量 約 {freed / 1e9:.2f} GB",
                f"Removal of the TRELLIS runtime finished: {len(removed)} item(s), about {freed / 1e9:.2f} GB freed"))
    # freed_bytes は実際に消したファイルの値（消せなかったファイルがあれば見積もり estimated_freed_bytes より小さい）
    return {"root": root, "scope": scope, "removed": removed, "kept": pl["kept"], "freed_bytes": freed,
            "estimated_freed_bytes": pl["freed_bytes"], "shared_bytes": pl["shared_bytes"], "files": done[0],
            "disk_freed_bytes": (free1 - free0) if free0 is not None and free1 is not None else None,
            "failed": [os.path.relpath(f[0], root) for f in failed[:20]], "failed_count": len(failed),
            "clip_consent_removed": clip_dropped, "root_removed": root_removed,
            "status": S.status(svc, root)}


# ---------------------------------------------------------------------------
# 照合し直し・修復
# ---------------------------------------------------------------------------
def _verify_files(comp: dict, root: str) -> list[tuple[str, int]]:
    out = []
    for step in comp["steps"]:
        t = step["type"]
        if t in ("hf_files", "hf_cache"):
            base = S._hf_base(step, root)
            out += [(os.path.join(base, r), f["size"]) for r, f in S._files_from(step["files_from"]).items()]
        elif t == "url_file":
            out.append((os.path.join(root, step["dest"]), step["size"]))
        elif t == "source_archive":
            src = S.sources()[step["source"]]
            out += [(os.path.join(root, src["dest"], r), f["size"]) for r, f in src["files"].items()]
    return out


def _venv_need_bytes() -> int:
    """入れ直しに要る空き: PyTorch のホイール（取り直す）+ uv のキャッシュに展開し直す PyTorch とパッケージ（入れ直しの前に
    uv cache clean で空にする。古い展開は venv から外れた時点で空きに戻る）。余裕の 5 % と 1 GiB を含む。"""
    by = {c["id"]: c for c in S.manifest()["components"]}
    torch, pkgs = by.get("torch") or {}, by.get("packages") or {}
    need = torch.get("download_bytes", 0) + torch.get("disk_bytes", 0) + pkgs.get("disk_bytes", 0)
    return int(need * 1.05) + (1 << 30)


def repair_plan(svc, root: str | None) -> dict:
    """画面の確認用（GET /setup/repair-plan）: 対象かどうか、照合する量、入れ直しで取得する量と要る空き。"""
    root = _resolve_root(svc, {"root": root})
    info = root_info(root)
    out = {"root": root, **info, "models": [], "verify_bytes": 0, "venv_download_bytes": 0, "venv_need_bytes": 0,
           "free_bytes": S._free_bytes(root)}
    if not (info["app_created"] and os.path.isdir(root)):
        return out
    dists = S.installed_dists(root)
    present = [c for c in S.manifest()["components"] if S.component_status(c, root, dists)["state"] != "missing"]
    models = [c for c in present if any(s["type"] in _VERIFY_STEPS for s in c["steps"])]
    out["models"] = [c["id"] for c in models]
    out["verify_bytes"] = sum(size for c in models for _, size in _verify_files(c, root))
    out["venv_download_bytes"] = sum(c["download_bytes"] for c in S.manifest()["components"] if c["id"] in VENV_COMPONENTS)
    out["venv_need_bytes"] = _venv_need_bytes()
    return out


def summarize_repair(p: dict) -> dict:
    return {"models": bool(p.get("models")), "venv": bool(p.get("venv"))}


def run_repair(svc, ctx, params: dict) -> dict:
    """ジョブ ``setup_repair``: params = {root（省略時は使っている取得先）, models: bool, venv: bool}。"""
    do_models, do_venv = bool(params.get("models")), bool(params.get("venv"))
    if not (do_models or do_venv):
        raise BackendError("INVALID_INPUT", tr("照合し直す（models）か入れ直す（venv）かを選んでください",
                                               "Choose re-verify (models) or reinstall (venv)"),
                           params={"reason": "maint_nothing"})
    root = _resolve_root(svc, params)
    _require_app_root(root)
    check_idle(svc, getattr(ctx, "job", None))
    if not os.path.isdir(root):
        raise BackendError("INVALID_INPUT", tr("取得先のフォルダーがありません", "The destination folder does not exist"),
                           params={"reason": "maint_not_found", "root": root})
    st = S.load_state()
    rec = (st.get("roots") or {}).get(S._root_key(root)) or {}
    # 取り直すのは、前の取得で同意したライセンスのままの項目だけ（同意は取得先ごとの記録と、最後の取得の記録）
    accepted = {**((st.get("acceptance") or {}).get("components") or {}), **(rec.get("accepted") or {})}
    clip_ok = consent.status(root)["accepted"]

    def may_fetch(c: dict) -> bool:
        return accepted.get(c["id"]) == S.license_digest(c) and (c.get("consent") != "clip" or clip_ok)

    dists = S.installed_dists(root)
    comps = S.manifest()["components"]
    states = {c["id"]: S.component_status(c, root, dists)["state"] for c in comps}
    models = [c for c in comps if states[c["id"]] != "missing" and any(s["type"] in _VERIFY_STEPS for s in c["steps"])] \
        if do_models else []
    venv = []
    if do_venv:
        venv = [c for c in comps if c["id"] in VENV_COMPONENTS or (c["id"] == "uv" and states["uv"] != "installed")]
        refused = [c["id"] for c in venv if not may_fetch(c)]
        if refused:
            raise BackendError("CONSENT_REQUIRED", tr("入れ直す項目のライセンスへの同意の記録がありません（セットアップの画面で同意してください）",
                                                      "There is no record of license consent for the items to reinstall "
                                                      "(accept them on the Setup page)"),
                               params={"reason": "license", "components": refused})
        need, free = _venv_need_bytes(), S._free_bytes(root)
        if free is not None and free < need:
            # reason で画面の案内を入れ直しの場面に合わせる（取得先の選び直しではなく、空きを増やす。点検 U-D2-R5）
            raise BackendError("DISK_SPACE_LOW", tr(f"取得先のドライブの空きが足りません（空き {free / 1e9:.1f} GB、必要 約 {need / 1e9:.1f} GB）",
                                                    f"Not enough free space on the destination drive ({free / 1e9:.1f} GB free, "
                                                    f"about {need / 1e9:.1f} GB needed)"),
                               params={"free_bytes": free, "need_bytes": need, "root": root, "reason": "maint_reinstall"})
    todo = {c["id"] for c in venv}
    prog = S._Progress(ctx, [c for c in comps if c in models or c in venv], todo, bool(venv))
    inst = S._Installer(svc, ctx, root, prog, reinstall=bool(venv))
    ctx.info(tr(f"TRELLIS 実行環境の修復: 取得先 {root}、照合し直す項目 {len(models)}、入れ直す項目 {len(venv)}",
                f"Repairing the TRELLIS runtime: destination {root}, {len(models)} item(s) to re-verify, {len(venv)} "
                f"item(s) to reinstall"))
    results: dict[str, str] = {}
    refetched: list[str] = []
    needs_setup: dict[str, list[str]] = {}
    check = None
    try:
        for c in models:
            ctx.check_cancel()
            prog.set(c["id"], 0.0, "setup_verify", state="verifying")
            ok = may_fetch(c)
            bad = inst.verify(c, full=True, refetch=ok)
            if bad and not ok:
                needs_setup[c["id"]] = bad[:20]
                results[c["id"]] = "mismatch"
            else:
                refetched += bad
                results[c["id"]] = "repaired" if bad else "verified"
            prog.set(c["id"], 1.0, "setup_verify", state="installed" if results[c["id"]] != "mismatch" else "failed")
            inst.record(c, results[c["id"]])
        if venv:
            check = _reinstall_venv(svc, ctx, root, inst, prog, venv, results)
    except BaseException:
        for cid, s in prog.states.items():
            if s in ("running", "verifying"):
                prog.states[cid] = "failed"
        raise
    now = logs.now_iso()

    def fn(st):
        r = st.setdefault("roots", {}).setdefault(S._root_key(root), {"path": root})
        r["last_repair"] = {"at": now, "models": do_models, "venv": do_venv, "components": results,
                            "refetched": refetched[:50], "needs_setup": sorted(needs_setup)}
    S.update_state(fn)
    svc.invalidate_environment()
    if needs_setup:
        ctx.warn(tr("照合が合わないファイルがあり、ライセンス・注意事項への同意の記録が無いため取り直していません。セットアップの画面から"
                    "取得し直してください: ",
                    "Some files do not match, but they were not downloaded again because there is no record of acceptance "
                    "of the licenses and notices. Download them again from the Setup page: ") + ", ".join(sorted(needs_setup)))
    ctx.info(tr("TRELLIS 実行環境の修復が終わりました: ", "Repair of the TRELLIS runtime finished: ")
             + ", ".join(f"{k}={v}" for k, v in results.items()))
    return {"root": root, "components": results, "refetched": refetched[:50], "refetched_count": len(refetched),
            "needs_setup": needs_setup, "runtime_check": check, "status": S.status(svc, root)}


def _reinstall_venv(svc, ctx, root: str, inst: "S._Installer", prog: "S._Progress", venv: list[dict], results: dict) -> dict:
    """uv のキャッシュを空にしてから、Python・PyTorch・パッケージを uv の --reinstall で入れ直し、動作確認をする。

    uv は取得・展開を終えてから入れ替えるので、通信の失敗で今の仮想環境が壊れることはない。仮想環境の Python 自体が
    起動しない（python.exe・pyvenv.cfg が壊れた）ときだけ、仮想環境を作り直す。PyTorch のホイール（約 3.4 GB）は
    入れ終わると消しているので、先に取り直す（通信の失敗で止まっても仮想環境は変えていない）。"""
    by = {c["id"]: c for c in venv}
    if "uv" in by:
        prog.set("uv", 0.0, "setup_install", state="running")
        inst.install(by["uv"])
        prog.set("uv", 1.0, "setup_install", state="installed")
        results["uv"] = "installed"
        inst.record(by["uv"], "installed")
    torch = by["torch"]
    prog.set("torch", 0.0, "setup_install", state="running")
    for s in torch["steps"]:
        if s["type"] == "wheel_files":
            inst._wheel_files(torch, s, lambda f, file=None, d=None, t=None: prog.set("torch", 0.8 * f, "setup_install", file, d, t))
    venv_dir = os.path.join(root, "venv")
    if os.path.isdir(venv_dir):
        rc = 1
        if os.path.isfile(S._venv_python(root)):
            try:
                rc, _ = ctx.run_process([S._venv_python(root), "-I", "-c", "import encodings, sys"],
                                        env={"PYTHONPATH": None, "PYTHONHOME": None, "VIRTUAL_ENV": None}, cwd=root)
            except OSError:
                rc = 1
        if rc != 0:
            ctx.warn(tr("仮想環境の Python が起動しないので、仮想環境を作り直します",
                        "The Python of the virtual environment does not start, so the virtual environment is recreated"))
            S._rmtree(venv_dir)
    # uv のキャッシュを空にしてから入れ直す。venv のファイルは uv のキャッシュの展開とハードリンクで同じ実体なので、
    # --reinstall（--refresh）だけでは、ハッシュで固定した PyPI のホイールはキャッシュの展開をそのまま使い、中身が壊れた
    # ファイルも同じ実体のまま入れ直していた（統合 D3 で実測: 取得 0 件・壊したファイルが残る）。消しても venv のファイルは
    # 残るので、取得が途中で失敗しても今の仮想環境は動く。消せなければ（ほかのプロセスが開いているなど）警告して続ける
    try:
        inst._run_uv(["cache", "clean"], tr("uv のキャッシュの消去", "Clearing the uv cache"), attempts=1)
    except BackendError as e:
        ctx.warn(tr(f"uv のキャッシュを消せませんでした（修復は続けます。壊れたパッケージのファイルは直らないことがあります）: {e.message}",
                    f"Could not clear the uv cache (the repair continues; damaged package files may not be fixed): "
                    f"{e.message}"))
    for cid in VENV_COMPONENTS:
        c = by[cid]
        name = S.localized(c)["name"]
        prog.set(cid, 0.0, "setup_install", state="running")
        ctx.info(tr(f"{name}: 入れ直します（uv --reinstall）", f"{name}: reinstalling (uv --reinstall)"))
        # PyTorch のホイールは上で取得・照合したので、取得の手順（wheel_files）は繰り返さない（置いてあるホイール約 3.4 GB の
        # SHA-256 をもう一度読み直していた。uv は --require-hashes で照合する。点検 R11B-05）
        inst.install({**c, "steps": [s for s in c["steps"] if s["type"] != "wheel_files"]} if cid == "torch" else c)
        after = S.component_status(c, root)["state"]
        if after != "installed":
            raise BackendError("SETUP_FAILED", tr(f"{name}: 入れ直しの後も状態が {after} です",
                                                  f"{name}: the state is still {after} after the reinstall"),
                               params={"component": cid, "step": "status"})
        results[cid] = "reinstalled"
        prog.set(cid, 1.0, "setup_install", state="installed")
        inst.record(c, "reinstalled")
    try:
        # どこからも使われない古い展開が残っていれば消す（空きを戻す。失敗しても修復は続ける）
        inst._run_uv(["cache", "prune"], tr("uv のキャッシュの整理", "Pruning the uv cache"), attempts=1)
    except BackendError as e:
        ctx.warn(tr(f"uv のキャッシュを整理できませんでした（修復は続けます）: {e.message}",
                    f"Could not prune the uv cache (the repair continues): {e.message}"))
    prog.set("_check", 0.0, "setup_check")
    check = inst.runtime_check()
    prog.set("_check", 1.0, "setup_check")
    return check
