"""データフォルダーの容量の表示と整理（2026-10-17 機能 SF03。点検 A1_plan_gap-F04・G3_install-F04、監査 C88・C104 の残り）。

利用者が設定画面で押したときだけ消す（自動では消さない。保持の期間も設けない）。整理はどれも 2 段階:

1. :func:`plan`（何も変えない）→ 項目の一覧。消せるものは ``keep`` が None、残すものは ``keep`` に理由
2. :func:`cleanup` → 画面で確認した ID（``items``）のうち、その時点で計画し直しても消せるものだけを消す
   （確認の後に導入・ビルド・ジョブで状態が変わったものは消さず、``skipped`` で返す）

種類（kind）と、残すもの:

* ``builds``（builds\\<ID>）: 導入中のビルド（どの導入先でも。導入の記録の installed.build_dir または build_id。
  理由 installed）、「前の状態に戻す」の戻り先のビルド（previous）と、最新の成功したビルド（latest）。失敗したビルド・
  記録の無い作りかけも消せる。導入の記録が読めなければ断る
* ``backups``（install\\backups\\<ID>）: modbuilder.install.prune_backups の規則（protected_backups の baseline・
  previous・last_install と、新しい :data:`BACKUPS_KEEP` 件（recent））。使われなくなった共有の中身（_blobs）も消える
* ``trash``（library\\trash\\<名前>）: 全部消せる（装備一覧から削除した装備。完全に消すので元に戻せない）
* ``input_images``（装備の source\\input*.png など・記録の stored_path が指す source\\ の直下の input*）: 消したファイルを
  指す記録の stored_path も外す（MOD・変換結果は変わらない。その装備の再生成では画像を選び直す）。生成の画面の画像の欄が
  使っている装備（``exclude``。shown）は残す
* ``drafts``（work\\<下書きID>）: 失敗・中断した下書き（status failed。中断は起動時の後始末 C104 が付ける）だけが対象。
  順番待ち・実行中のジョブが使う下書き（in_use）と、画面に表示中の下書き（``exclude``。shown）は残す

builds・backups・trash・input_images は MOD の作成・導入・装備の変更と同じロック（svc.mod_lock）の中で行う（実行中なら
BUSY）。容量はハードリンク（退避の _blobs）を 1 回だけ数える。データフォルダーの中の TRELLIS 実行環境は数えない
（大きく時間がかかる。設定画面の「TRELLIS 実行環境の管理」で扱う）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat

from . import logs
from .drafts import is_link, remove_tree
from .errors import BackendError
from .i18n import tr

KINDS = ("builds", "backups", "trash", "input_images", "drafts")
LOCKED_KINDS = ("builds", "backups", "trash", "input_images")
BACKUPS_KEEP = 5                     # 新しい退避を残す件数（監査 C88 の modops.prune_backups の既定と同じ）
BUILD_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
INPUT_IMAGE_RE = re.compile(r"^input(_\d+)?\.(png|jpe?g|webp|bmp|gif|tiff?)$", re.IGNORECASE)   # CLI は元の拡張子（UD1-R3）
MAX_ITEMS = 10000
CATEGORIES = ("equipment", "input_images", "trash", "builds", "backups", "drafts", "received", "refs", "logs", "other")


def kind_label(kind: str) -> str:
    """整理の種類の表示名（ログに書く。表示言語の文）。"""
    return {"builds": tr("古いビルド", "old builds"), "backups": tr("導入の退避", "install backups"),
            "trash": tr("削除した装備（trash）", "deleted equipment (trash)"),
            "input_images": tr("保存した入力画像", "saved input images"),
            "drafts": tr("失敗・中断・保存済みの下書き", "failed, interrupted or saved drafts")}[kind]


# ---------------------------------------------------------------------------
# 大きさ
# ---------------------------------------------------------------------------
def _is_reparse(st) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _dir_size(path: str, seen: set | None = None) -> tuple[int, int]:
    """フォルダーの中のファイルの大きさの合計と数（リンク・ジャンクションはたどらない）。seen を渡すとハードリンクを
    1 回だけ数える（os.stat のボリュームとファイル番号）。"""
    total = files = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    if e.is_symlink() or _is_reparse(st):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if seen is not None:
                        full = os.stat(e.path, follow_symlinks=False)
                        key = (full.st_dev, full.st_ino) if full.st_ino else e.path
                        if key in seen:
                            continue
                        seen.add(key)
                    total += st.st_size
                    files += 1
                except OSError:
                    continue
    return total, files


def _file_size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _str_list(v, name: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > MAX_ITEMS or not all(isinstance(x, str) and x for x in v):
        raise BackendError("INVALID_INPUT", tr(f"{name} は文字列の配列で指定してください", f"{name} must be an array of strings"),
                           params={"reason": "storage_items"})
    return list(dict.fromkeys(v))


def _check_kind(kind) -> str:
    if kind not in KINDS:
        raise BackendError("INVALID_INPUT", tr(f"整理の種類が不正です: {kind!r}（{', '.join(KINDS)}）",
                                               f"Invalid cleanup kind: {kind!r} ({', '.join(KINDS)})"),
                           params={"reason": "storage_kind"})
    return kind


# ---------------------------------------------------------------------------
# 種類ごとの一覧（何も変えない）
# ---------------------------------------------------------------------------
def _protected_builds() -> tuple[dict[str, str], dict[str, str]]:
    """残すビルド → 理由（installed: 導入中 / previous: 「前の状態に戻す」の戻り先）を、導入の記録の build_dir（絶対パス。
    normcase）と build_id（ビルドのフォルダー名）の両方で返す → (by_dir, by_id)。

    build_id でも守るのは、データフォルダーを別の場所に移す（コピーして --data-dir で指定する）と、記録の build_dir が古い
    場所を指したままになるため（2026-10-17 U-D1 のレビュー UD1-R1）。受け取った MOD の build_id が偶然ビルドの名前と
    重なっても、残す側に倒れるだけ。戻り先は modbuilder.install.previous_install_backup の退避の installed_before
    （UD1-R2。消すと、戻した後のゲームの更新の判定などが働かない）。導入中を優先する。"""
    from modbuilder import install
    try:
        st = install.load_state()
    except (OSError, ValueError) as e:
        raise BackendError("INVALID_INPUT", tr("導入の記録（install\\state.json）が読めないため、どのビルドを導入中か分かりません。"
                                               "ビルドの整理を中止しました",
                                               "The install record (install\\state.json) could not be read, so it is not "
                                               "known which build is installed. Cleaning up old builds was cancelled"),
                           params={"reason": "storage_state", "error": type(e).__name__}) from None
    by_dir: dict[str, str] = {}
    by_id: dict[str, str] = {}

    def add(rec, why: str) -> None:
        if not isinstance(rec, dict):
            return
        bd, bid = rec.get("build_dir"), rec.get("build_id")
        if isinstance(bd, str) and bd:
            by_dir.setdefault(os.path.normcase(os.path.abspath(bd)), why)
        if isinstance(bid, str) and bid:
            by_id.setdefault(bid, why)

    targets = [t for t in (st.get("targets") or {}).values() if isinstance(t, dict)]
    for t in targets:
        add(t.get("installed"), "installed")
    for t in targets:
        if not t.get("installed"):
            continue
        try:
            pv = install.previous_install_backup(t)
        except Exception:   # noqa: BLE001 — 戻り先が分からなければ、導入中のビルドだけを守る（従来どおり）
            pv = None
        if pv:
            add((pv.get("meta") or {}).get("installed_before"), "previous")
    return by_dir, by_id


def _plan_builds(svc, exclude: set) -> tuple[list[dict], int]:
    from modbuilder.builder import MANIFEST
    base = svc.builds_dir()
    if not os.path.isdir(base):
        return [], 0
    by_dir, by_id = _protected_builds()
    items, latest_seen = [], False
    for name in sorted(os.listdir(base), reverse=True):        # 新しい順（ビルド ID は日時）
        d = os.path.join(base, name)
        if not BUILD_ID_RE.match(name) or not os.path.isdir(d):
            continue                                            # ビルドのフォルダーではないものは触らない
        m = {}
        try:
            with open(os.path.join(d, MANIFEST), encoding="utf-8") as f:
                m = json.load(f)
            if not isinstance(m, dict):
                m = {}
        except (OSError, ValueError):
            m = {}
        status = m.get("status") if m.get("status") in ("ok", "failed", "running") else "incomplete"
        keep = by_dir.get(os.path.normcase(os.path.abspath(d))) or by_id.get(name)
        if not keep and status == "ok" and not latest_seen:
            keep = "latest"
        if status == "ok":
            latest_seen = True
        size, nfiles = _dir_size(d)
        eq = ((m.get("library") or {}).get("equipment") or []) if isinstance(m.get("library"), dict) else []
        items.append({"id": name, "status": status, "created_at": m.get("created_at"), "equipment": len(eq),
                      "bytes": size, "files": nfiles, "keep": keep})
    return items, sum(i["bytes"] for i in items if not i["keep"])


def _backup_meta(base: str, bid: str) -> dict:
    try:
        with open(os.path.join(base, bid, "backup.json"), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _plan_backups(svc, exclude: set) -> tuple[list[dict], int]:
    from modbuilder import install
    try:
        p = install.prune_backups(keep=BACKUPS_KEEP, yes=False, log=lambda m: None)
    except install.InstallStateBroken as e:     # 導入の記録が壊れている（INTERNAL にしない。点検 R07B-03）
        raise BackendError("INVALID_INPUT", str(e), params={"reason": "storage_state", "path": e.path}) from e
    prot, remove = p.get("protected") or {}, set(p.get("remove") or [])
    base = install._backups_root()
    items, shas = [], {}
    for b in sorted(install.list_backups(), key=lambda b: (b.get("created_at") or "", b["id"]), reverse=True):
        bid = b["id"]
        files = _backup_meta(base, bid).get("files") or {}
        recs = {rel: r for rel, r in files.items() if isinstance(r, dict)}
        keep = prot.get(bid) or (None if bid in remove else "recent")
        for r in recs.values():
            s = shas.setdefault(r.get("sha256"), {"size": int(r.get("size") or 0), "kept": False})
            s["kept"] = s["kept"] or bool(keep)
        # broken: 記録（backup.json）が読めない退避（大きさ 0・ファイル無しで出る。保護されていなければ消せる。R07B-03）
        items.append({"id": bid, "created_at": b.get("created_at"), "reason": b.get("reason"), "files": sorted(recs),
                      "bytes": sum(int(r.get("size") or 0) for r in recs.values()), "keep": keep,
                      "broken": bool(b.get("broken"))})
    # 消して空く大きさの見込み: 残す退避が使わない中身（_blobs で共有するので、同じ中身は 1 回だけ）
    rm_shas = {r.get("sha256") for i in items if not i["keep"] for r in (_backup_meta(base, i["id"]).get("files") or {}).values()
               if isinstance(r, dict)}
    freed = sum(v["size"] for k, v in shas.items() if k in rm_shas and not v["kept"])
    return items, freed


def _library(svc):
    from modbuilder.library import Library
    root = svc.library_dir()
    if not os.path.isfile(os.path.join(root, "library.json")):
        return None
    return Library.open(root)


def _plan_trash(svc, exclude: set) -> tuple[list[dict], int]:
    lib = _library(svc)
    if lib is None:
        return [], 0
    items = []
    for t in lib.trash_items():
        size, nfiles = _dir_size(os.path.join(lib.trash_root, t["trash_name"]))
        items.append({"id": t["trash_name"], "equipment_id": t.get("id"), "name": t.get("name_ja") or t.get("name"),
                      "removed_at": t.get("removed_at"), "bytes": size, "files": nfiles, "keep": None})
    return items, sum(i["bytes"] for i in items)


def _input_records(eq) -> list[dict]:
    """記録の入力画像のうち stored_path のあるもの（generation.input_image と input_images。eq.data の中の dict そのもの）。"""
    g = eq.data.get("generation") if isinstance(eq.data.get("generation"), dict) else {}
    recs = [g.get("input_image")] + list(g.get("input_images") or [])
    return [r for r in recs if isinstance(r, dict) and isinstance(r.get("stored_path"), str) and r["stored_path"]]


def _stored_file(eq, sp: str) -> str | None:
    """stored_path（"source/…"。装備のフォルダーからの相対）が source\\ の直下の、名前が input で始まるファイルを指していれば
    その絶対パス。それ以外（source の外・別の名前）は消す対象にしないので None。"""
    src = os.path.normcase(os.path.abspath(os.path.join(eq.dir, "source")))
    p = os.path.abspath(os.path.join(eq.dir, sp.replace("/", os.sep)))
    if os.path.normcase(os.path.dirname(p)) != src or not os.path.basename(p).lower().startswith("input"):
        return None
    return p


def _input_files(eq) -> list[str]:
    """消す対象の入力画像（source\\ の直下のファイル名）: 名前が input*.png など（INPUT_IMAGE_RE）のものと、記録の
    stored_path が指すもの（CLI の library add-converted --keep-image は元の拡張子で保存する。U-D1 のレビュー UD1-R3）。"""
    src = os.path.join(eq.dir, "source")
    try:
        listing = {os.path.normcase(f): f for f in os.listdir(src) if os.path.isfile(os.path.join(src, f))}
    except OSError:
        return []
    names = {k for k, f in listing.items() if INPUT_IMAGE_RE.match(f)}
    for r in _input_records(eq):
        p = _stored_file(eq, r["stored_path"])
        if p and os.path.normcase(os.path.basename(p)) in listing:
            names.add(os.path.normcase(os.path.basename(p)))
    return sorted(listing[k] for k in names)


def _plan_input_images(svc, exclude: set) -> tuple[list[dict], int]:
    """exclude: 生成の画面の画像の欄が使っている装備の ID（再生成で保存した入力画像を欄に戻している。shown。UD1-R4）。"""
    lib = _library(svc)
    if lib is None:
        return [], 0
    items = []
    for eq in lib.list():
        files = _input_files(eq)
        if not files:
            continue                       # 消すファイルの無い装備は並べない（記録だけが残っていても画面には影響しない）
        size = sum(_file_size(os.path.join(eq.dir, "source", f)) for f in files)
        items.append({"id": eq.id, "name": eq.name_ja or eq.name_en, "files": len(files), "bytes": size,
                      "keep": "shown" if eq.id in exclude else None})
    return items, sum(i["bytes"] for i in items if not i["keep"])


def _plan_drafts(svc, exclude: set) -> tuple[list[dict], int]:
    active = svc.jobs.active()
    used = {str((j.params or {}).get("draft_id")) for j in active if (j.params or {}).get("draft_id")}
    job_ids = {j.id for j in active}
    items = []
    for dr in svc.drafts.list():
        name = os.path.basename(dr.dir)
        status = dr.data.get("status")
        # 保存済みの印のある ready の下書き（keep_drafts で残した、装備として保存したもの）も消せる。起動時に復元しない
        # ので、ここが消す手段になる（点検 R18A-02）
        saved = status == "ready" and isinstance(dr.data.get("saved"), dict)
        if status != "failed" and not saved:
            continue                       # 保存できる下書き（ready）・処理中の下書きは対象にしない
        errs = dr.data.get("errors")
        first = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else {}
        keep = None
        if name in exclude:
            keep = "shown"
        elif name in used or (dr.data.get("job_id") and dr.data.get("job_id") in job_ids):
            keep = "in_use"
        # 下書きのフォルダー自体がリンク・ジャンクションなら、消してもリンクを外すだけで空かない（リンク先は数えない。
        # 点検 R10B-01）
        size, nfiles = (0, 0) if is_link(dr.dir) else _dir_size(dr.dir)
        inp = dr.data.get("input") if isinstance(dr.data.get("input"), dict) else {}
        items.append({"id": name, "status": status, "interrupted": first.get("code") == "INTERRUPTED",
                      "error_code": first.get("code"), "kind": inp.get("kind"), "created_at": dr.data.get("created_at"),
                      "bytes": size, "files": nfiles, "keep": keep, **({"saved": True} if saved else {})})
    return items, sum(i["bytes"] for i in items if not i["keep"])


_PLANNERS = {"builds": _plan_builds, "backups": _plan_backups, "trash": _plan_trash,
             "input_images": _plan_input_images, "drafts": _plan_drafts}


def plan(svc, kind, exclude=None) -> dict:
    """整理の計画（何も変えない）→ {"kind", "items": [{"id", "bytes", "keep": None | 理由, ...}], "count"（消せる件数）,
    "bytes"（消して空く大きさの見込み）, "kept"（残す件数）}。exclude: 画面に表示中の下書きの ID（drafts）・生成の画面の
    画像の欄が使っている装備の ID（input_images）。ほかの種類では使わない。"""
    kind = _check_kind(kind)
    ex = set(_str_list(exclude, "exclude"))
    items, freed = _PLANNERS[kind](svc, ex)
    n = sum(1 for i in items if not i.get("keep"))
    return {"kind": kind, "items": items, "count": n, "bytes": int(freed), "kept": len(items) - n}


# ---------------------------------------------------------------------------
# 消す（利用者が確認した ID だけ）
# ---------------------------------------------------------------------------
def _remove_build(svc, bid: str) -> None:
    shutil.rmtree(os.path.join(svc.builds_dir(), bid))


def _remove_trash(svc, name: str) -> None:
    lib = _library(svc)
    d = lib._trash_dir(name) if lib is not None else None      # 一覧の名前と完全に一致するものだけ（U-C-R1）
    if d is None:
        raise FileNotFoundError(name)
    shutil.rmtree(d)


def _remove_inputs(svc, eq_id: str) -> None:
    lib = _library(svc)
    eq = lib.load(eq_id)
    removed, err = set(), None
    for f in _input_files(eq):
        p = os.path.abspath(os.path.join(eq.dir, "source", f))
        try:
            os.remove(p)
            removed.add(os.path.normcase(p))
        except OSError as e:
            err = e
    # 記録の stored_path は、消したファイル・もう無いファイルを指すものだけを外す（消せなかった・source の外の画像を
    # 指すものは残す。UD1-R3）
    for r in _input_records(eq):
        p = os.path.abspath(os.path.join(eq.dir, r["stored_path"].replace("/", os.sep)))
        if os.path.normcase(p) in removed or not os.path.isfile(p):
            r["stored_path"] = None
    lib.save(eq)                     # MOD の状態は変えない（MOD・変換結果は同じ。_mark_changed は呼ばない）
    if err is not None:
        raise err


def _remove_draft(svc, did: str) -> None:
    """中身を先に消し、draft.json は最後に消す（途中で消せなくても draft.json が残るので、一覧に出て、もう一度消せる。
    DraftStore.delete の rmtree(ignore_errors=True) では draft.json だけが消えて画面から消せなくなることがあった。UD1-R5）。"""
    d = svc.drafts._dir(did)
    if not os.path.isdir(d) and not is_link(d):       # リンク先の無いジャンクションも外す（点検 R10B-01）
        return
    remove_tree(d)                       # 消す順・リンクの扱いは DraftStore と同じ（R10B-02）


_REMOVERS = {"builds": _remove_build, "trash": _remove_trash, "input_images": _remove_inputs, "drafts": _remove_draft}


def cleanup(svc, kind, items, exclude=None) -> dict:
    """計画で確認した ID（items）のうち、今も消せるものを消す → {"kind", "removed", "failed": [{"id", "error"}],
    "skipped"（確認の後に残すものになった・見つからない ID）, "freed_bytes"}。"""
    kind = _check_kind(kind)
    want = _str_list(items, "items")
    if not want:
        raise BackendError("INVALID_INPUT", tr("消すものを指定してください", "Specify the items to delete"),
                           params={"reason": "storage_items"})
    lock = svc.mod_lock if kind in LOCKED_KINDS else None
    if lock is not None and not lock.acquire(timeout=2.0):
        raise BackendError("BUSY", tr("MOD の作成・導入・装備の変更の実行中は整理できません。終わってからもう一度操作してください",
                                      "Cleanup is not possible while a MOD build, installation or equipment change is "
                                      "running. Try again after it finishes"))
    try:
        p = plan(svc, kind, exclude)
        removable = {i["id"]: i for i in p["items"] if not i.get("keep")}
        targets = [i for i in want if i in removable]
        skipped = [i for i in want if i not in removable]
        removed, failed, freed = [], [], 0
        if kind == "backups" and targets:
            from modbuilder import install
            before = install.backups_usage()["bytes"]
            r = install.prune_backups(keep=BACKUPS_KEEP, yes=True, log=lambda m: logs.info(m), only=set(targets))
            removed = list(r.get("removed") or [])
            failed = [{"id": b, "error": "OSError"} for b in r.get("failed") or []]
            freed = max(0, before - int((r.get("usage") or {}).get("bytes") or 0))
        else:
            for i in targets:
                try:
                    _REMOVERS[kind](svc, i)
                    removed.append(i)
                    freed += int(removable[i].get("bytes") or 0)
                except Exception as e:   # noqa: BLE001 — 1 件の失敗で残りを止めない
                    failed.append({"id": i, "error": type(e).__name__})
    finally:
        if lock is not None:
            lock.release()
    label = kind_label(kind)
    logs.info(tr(f"データの整理（{label}）: {len(removed)} 件を消しました（{freed / 1024 / 1024:.1f} MB）",
                 f"Data cleanup ({label}): deleted {len(removed)} item(s) ({freed / 1024 / 1024:.1f} MB)")
              + (tr(f"、消せなかったもの {len(failed)} 件", f", {len(failed)} could not be deleted") if failed else "")
              + (tr(f"、確認の後に状態が変わったため残したもの {len(skipped)} 件",
                    f", {len(skipped)} kept because they changed after the check") if skipped else ""))
    return {"kind": kind, "removed": removed, "failed": failed, "skipped": skipped, "freed_bytes": int(freed)}


# ---------------------------------------------------------------------------
# 容量（何も変えない）
# ---------------------------------------------------------------------------
def _skip_dirs(svc, home: str) -> dict[str, str]:
    """数えないフォルダー（データフォルダーの直下にある TRELLIS 実行環境）→ 理由。"""
    out = {}
    cands = [os.path.join(home, "trellis-runtime")]
    try:
        r = svc.trellis_root()
        if r:
            cands.append(r)
    except Exception:   # noqa: BLE001
        pass
    for c in cands:
        c = os.path.normcase(os.path.abspath(c))
        if os.path.dirname(c) == os.path.normcase(home):
            out[c] = "trellis_runtime"
    return out


def _library_sizes(root: str, cats: dict) -> None:
    """library の中を、装備（equipment）・保存した入力画像（input_images）・trash に分けて数える。"""
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        p = os.path.join(root, name)
        if name == "trash" and os.path.isdir(p):
            b, n = _dir_size(p)
            cats["trash"]["bytes"] += b
            cats["trash"]["files"] += n
        elif name == "equipment" and os.path.isdir(p):
            b, n = _dir_size(p)
            ib = inn = 0
            for eid in os.listdir(p):
                src = os.path.join(p, eid, "source")
                try:
                    names = os.listdir(src)
                except OSError:
                    continue
                for f in names:
                    if INPUT_IMAGE_RE.match(f) and os.path.isfile(os.path.join(src, f)):
                        ib += _file_size(os.path.join(src, f))
                        inn += 1
            cats["input_images"]["bytes"] += ib
            cats["input_images"]["files"] += inn
            cats["equipment"]["bytes"] += b - ib
            cats["equipment"]["files"] += n - inn
        elif os.path.isdir(p):
            b, n = _dir_size(p)
            cats["equipment"]["bytes"] += b
            cats["equipment"]["files"] += n
        else:
            cats["equipment"]["bytes"] += _file_size(p)
            cats["equipment"]["files"] += 1


_TOP = {"builds": "builds", "install": "backups", "work": "drafts", "shared": "received", "refs": "refs", "logs": "logs"}


def usage(svc) -> dict:
    """データフォルダーの項目ごとの大きさと、整理で消せるものの件数・大きさ（何も変えない）→
    {"home", "total_bytes", "free_bytes", "categories": [{"id", "bytes", "files"}], "skipped": [{"id", "path"}],
    "cleanable": {kind: {"count", "bytes", "kept"} | {"error"}}}。"""
    home = os.path.abspath(svc.home())
    cats = {k: {"id": k, "bytes": 0, "files": 0} for k in CATEGORIES}
    skip = _skip_dirs(svc, home)
    skipped = []
    try:
        names = sorted(os.listdir(home))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(home, name)
        if os.path.normcase(p) in skip:
            skipped.append({"id": skip[os.path.normcase(p)], "path": p})
            continue
        if name == "library":
            _library_sizes(p, cats)
            continue
        cat = cats[_TOP.get(name, "other")]
        if os.path.isdir(p):
            # 導入の退避は中身をハードリンクで共有する（_blobs）ので 1 回だけ数える
            b, n = _dir_size(p, set() if name == "install" else None)
        else:
            b, n = _file_size(p), 1
        cat["bytes"] += b
        cat["files"] += n
    cleanable = {}
    for kind in KINDS:
        try:
            p = plan(svc, kind)
            cleanable[kind] = {"count": p["count"], "bytes": p["bytes"], "kept": p["kept"]}
        except BackendError as e:
            cleanable[kind] = {"error": e.code, "message": e.message}
        except Exception as e:   # noqa: BLE001 — 読めない記録があっても容量の表示は出す
            cleanable[kind] = {"error": "INTERNAL", "message": type(e).__name__}
    try:
        free = shutil.disk_usage(home).free
    except OSError:
        free = None
    return {"home": home, "total_bytes": sum(c["bytes"] for c in cats.values()), "free_bytes": free,
            "categories": [cats[k] for k in CATEGORIES], "skipped": skipped, "cleanable": cleanable,
            "backups_keep": BACKUPS_KEEP}
