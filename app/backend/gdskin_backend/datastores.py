"""データフォルダーが、別のアプリ（Claude のデスクトップアプリなど、MSIX でパッケージ化されたアプリ）の中の仮想フォルダーと
分かれていないかを調べ、見えない側にある装備を取り込む。

背景（2026-10-16 の不具合）: パッケージ化されたアプリの中から動いたプロセス（Claude Code のセッションのコマンドなど）は、
``%LOCALAPPDATA%`` に新しく作ったファイルが ``%LOCALAPPDATA%\\Packages\\<パッケージ>\\LocalCache\\Local\\...`` に書かれ、
そのプロセスからは実フォルダーと重ねて見える。普通に起動したアプリからは実フォルダーしか見えない。そのため、セッションが
既定のライブラリに取り込んだ装備が、利用者が起動したアプリからは見えず、MOD に入らなかった（docs/app_architecture.md 12 章 #15）。

* :func:`store_paths` — データフォルダーに対応する仮想フォルダー（あれば）
* :func:`virtualized_into` — このプロセスが仮想フォルダーに書いているか（試しのファイルを 1 つ作って調べる）
* :func:`scan` — 仮想フォルダーにだけある装備
* :func:`run_import` — それを、内部ID・レコードのパス・タグ名・アセットを保って取り込む（modbuilder の import_from_library）
"""
from __future__ import annotations

import glob
import os
import secrets

from . import logs, paths
from .errors import BackendError
from .i18n import tr


def _local_appdata() -> str:
    return os.path.abspath(os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local"))


def store_paths(path: str) -> list[str]:
    """path（%LOCALAPPDATA% の下）に対応する、パッケージごとの仮想フォルダーのうち存在するもの。"""
    local = _local_appdata()
    path = os.path.abspath(path)
    if not paths.is_within(path, local):
        return []
    rel = os.path.relpath(path, local)
    if rel.split(os.sep)[0].lower() == "packages":
        return []
    out = []
    for pkg in sorted(glob.glob(os.path.join(local, "Packages", "*"))):
        cand = os.path.join(pkg, "LocalCache", "Local", rel)
        if os.path.isdir(cand):
            out.append(cand)
    return out


def virtualized_into(home: str, stores: list[str]) -> str | None:
    """このプロセスが新しく作るファイルが仮想フォルダーに入るなら、その仮想フォルダーを返す。"""
    if not stores or not os.path.isdir(home):
        return None
    name = f".gdskin-probe-{secrets.token_hex(4)}"
    probe = os.path.join(home, name)
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        return next((s for s in stores if os.path.isfile(os.path.join(s, name))), None)
    except OSError:
        return None
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def scan(library_dir: str, home: str | None = None) -> dict:
    """{"virtualized_into": 仮想フォルダー or None, "stores": [{"path", "library", "missing": [...], "total"}]}"""
    from modbuilder.library import Library
    home = os.path.abspath(home or paths.home())
    out = {"home": home, "virtualized_into": virtualized_into(home, store_paths(home)), "stores": []}
    if out["virtualized_into"]:
        return out          # 仮想フォルダーの中身は重ねて見えているので、取り込むものは無い
    try:
        mine = set(Library.open(library_dir).ids())
    except Exception:   # noqa: BLE001  まだライブラリが無い
        mine = set()
    for lib_dir in store_paths(library_dir):
        try:
            items = Library.open(lib_dir).list()
        except Exception:   # noqa: BLE001  ライブラリではない・読めない
            continue
        missing = [e for e in items if e.id not in mine and not e.dev_only]
        out["stores"].append({"path": lib_dir, "total": len(items),
                              "missing": [{"id": e.id, "name": e.name_en, "name_ja": e.name_ja} for e in missing]})
    return out


def missing_count(info: dict) -> int:
    return sum(len(s["missing"]) for s in info.get("stores", []))


def run_import(svc, ctx, params: dict) -> dict:
    """仮想フォルダーにだけある装備を、このアプリのライブラリに取り込む（取り込み先の既存の装備には触れない）。"""
    from modbuilder.errors import LibraryError
    from modbuilder.importers import import_from_library
    from modbuilder.library import Library
    with svc.mod_lock:
        info = scan(svc.library_dir(), svc.home())
        if info["virtualized_into"]:
            raise BackendError("INVALID_INPUT", tr("このアプリ自体が仮想フォルダーに書いているため取り込めません"
                                                   "（普通に起動したアプリで行ってください）",
                                                   "Cannot import because this app itself is writing to the virtualized "
                                                   "folder (do this in the app started normally)"),
                               params={"reason": "virtualized"})
        want = params.get("store")
        stores = [s for s in info["stores"] if s["missing"] and (not want or os.path.normcase(s["path"]) == os.path.normcase(want))]
        if not stores:
            raise BackendError("NOT_FOUND", tr("取り込む装備はありません", "There is no equipment to import"))
        lib = Library.open(svc.library_dir(), create=True)
        imported, skipped = [], []
        for s in stores:
            ids = [m["id"] for m in s["missing"]]
            ctx.info(tr(f"別の場所のライブラリから {len(ids)} 件を取り込みます: {', '.join(ids)}",
                        f"Importing {len(ids)} equipment from the library in another location: {', '.join(ids)}"))
            try:
                done, skip = import_from_library(lib, Library.open(s["path"]), ids)
            except LibraryError as e:
                raise BackendError("INVALID_INPUT", tr(f"取り込めません: {e}", f"Cannot import: {e}"))
            imported += [e.id for e in done]
            skipped += skip
        logs.info(tr(f"別の場所のライブラリから装備を取り込みました: {len(imported)} 件",
                     f"Imported {len(imported)} equipment from the library in another location"))
        return {"imported": imported, "skipped": skipped, "stores": [s["path"] for s in stores]}
