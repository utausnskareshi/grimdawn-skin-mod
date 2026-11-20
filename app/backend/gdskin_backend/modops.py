"""MOD のビルド結果・導入・アンインストール・フォルダーへの出力（計画書 11 章、D-002）。

導入とアンインストールは modbuilder の ``install`` / ``uninstall`` をそのまま使う（退避 → コピー → SHA-256 照合、失敗時は戻す）。
画面は先に :func:`plan` で計画（``yes=False``。mods/ は変更しない）を取り、確認ダイアログで同意を得てからジョブを送る。
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import threading

from . import envcheck, logs
from .errors import BackendError
from .i18n import tr
from .settings import resolve_game_dir, resolve_install_target

KIND_ORDER = ("absent", "modbuilder", "aiskin-project", "modified", "foreign")


def _target(svc) -> tuple[str, bool]:
    from modbuilder.gamedata import looks_like_install_target
    target, is_test = resolve_install_target(svc.settings.get())
    if not target or not looks_like_install_target(target):
        raise BackendError("GAME_NOT_FOUND", tr("MOD の導入先が Grim Dawn のフォルダーではありません"
                                                "（database\\database.arz と ArchiveTool.exe が必要）",
                                                "The MOD install target is not a Grim Dawn folder "
                                                "(database\\database.arz and ArchiveTool.exe are required)"),
                           params={"path": target, "test": is_test})
    return os.path.abspath(target), is_test


# ---------------------------------------------------------------------------
# ビルドの一覧
# ---------------------------------------------------------------------------
def list_builds(svc, limit: int = 30) -> list[dict]:
    from modbuilder.builder import MANIFEST
    base = svc.builds_dir()
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base), reverse=True):
        p = os.path.join(base, name, MANIFEST)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        v = m.get("validation") or {}
        out.append({"build_id": m.get("build_id"), "build_dir": os.path.join(base, name), "status": m.get("status"),
                    "created_at": m.get("created_at"), "finished_at": m.get("finished_at"),
                    "equipment": [e.get("id") for e in (m.get("library") or {}).get("equipment", [])],
                    "excluded": [e.get("id") if isinstance(e, dict) else e for e in m.get("excluded", [])],
                    "files": (m.get("files") or {}).get("basemods") or {}, "languages": m.get("languages"),
                    "warnings": v.get("warnings", []), "checks": v.get("checks", {}),
                    "error": m.get("error")})
        if len(out) >= limit:
            break
    return out


def resolve_build(svc, build_id: str | None = None, shared_id: str | None = None) -> dict:
    """自分のビルド（builds\\）か、受け取った MOD（shared\\received\\。docs/app_architecture.md 15.2 節）。"""
    if shared_id:
        from .mod_package import received_build
        return received_build(svc, shared_id)
    return get_build(svc, build_id)


def get_build(svc, build_id: str | None) -> dict:
    builds = [b for b in list_builds(svc, 200) if b["status"] == "ok"]
    if build_id:
        builds = [b for b in builds if b["build_id"] == build_id]
    if not builds:
        raise BackendError("NOT_FOUND", tr("成功した MOD のビルドがありません（先に「MOD を作成」を実行）",
                                           "There is no successful MOD build (run \"Build MOD\" first)")
                           if not build_id else tr(f"ビルドがありません: {build_id}", f"Build not found: {build_id}"))
    return builds[0]


# ---------------------------------------------------------------------------
# 状態
# ---------------------------------------------------------------------------
def status(svc) -> dict:
    from modbuilder import install
    from modbuilder.gamedata import looks_like_install_target
    s = svc.settings.get()
    target, is_test = resolve_install_target(s)
    out = {"target": target, "is_test_target": is_test, "target_ok": bool(target and looks_like_install_target(target)),
           "game_dir": resolve_game_dir(s), "game_running": envcheck.game_running(),
           "launch_option": envcheck.launch_option_state(), "files": {}, "others": [], "installed": None,
           "history": [], "latest_build": None, "game_language": None}
    try:
        from modbuilder import steam
        out["game_language"] = steam.game_language()
    except Exception:   # noqa: BLE001
        pass
    builds = [b for b in list_builds(svc, 50) if b["status"] == "ok"]
    out["latest_build"] = builds[0] if builds else None
    # 導入先のゲームの Steam のビルド番号（ゲームの更新の目安。2026-10-17 機能 SF09）
    gb = _steam_build(target) if out["target_ok"] else None
    out["game_build"] = gb
    out["aiskin_unrecorded"] = []
    inst_files = None
    if out["target_ok"]:
        from .mod_package import file_codes
        try:
            st = install.load_state()
            info = install.inspect_target(target, "basemods", None, st)
        except install.InstallStateBroken as e:     # 壊れた state.json（INTERNAL にしない。点検 R07B-03）
            raise BackendError("INVALID_INPUT", str(e), params={"reason": "install_state", "path": e.path}) from e
        out["files"] = info["files"]
        out["others"] = info["others"]
        # mods\ 直下の、本ツールが管理しない .arc・.arz（/basemods で読み込まれ続ける。2026-10-17 監査 C85）
        out["basemods_others"] = info["basemods_others"]
        # mods\ にある、本ツールの記録の無い AI Skin の MOD（試作・記録を失う前の導入など。読み込まれる。2026-10-17 C126）
        out["aiskin_unrecorded"] = sorted(r for r, f in info["files"].items() if f.get("kind") == "aiskin-project")
        tgt = st["targets"].get(install._target_key(target, "basemods", None)) or {}
        inst = tgt.get("installed")
        if inst:
            out["installed"] = {k: inst.get(k) for k in ("build_id", "at", "equipment")} | {"files": sorted(inst.get("files", {}))}
            out["installed"].update(_installed_extra(svc, tgt))
            # 導入した後にゲームが更新され、補給官のレコードが変わったか（2026-10-17 監査 C84。判定できなければ []）
            rc = _records_changed(target, os.path.join(target, "mods", "database.arz"))
            out["installed"]["game_changed"] = rc or []
            # 作ったときのゲームとの違い（判定は補給官のレコード、Steam のビルド番号は目安。2026-10-17 機能 SF09）
            out["installed"]["game_update"] = _game_update(target, inst.get("build_dir"), rc, gb)
            # アンインストールで戻る、最初の導入の前のファイル（AI Skin の古い MOD なら残る。2026-10-17 C126）
            out["installed"]["after_uninstall"] = _after_uninstall(tgt)
            inst_files = inst.get("files") or {}
            if inst_files.get("database.arz"):
                out["installed"]["file_codes"] = file_codes(inst_files)       # ファイルごとの短いコード（機能 SF13）
        out["history"] = (tgt.get("history") or [])[-10:]
        out["baseline_backup"] = (tgt.get("baseline") or {}).get("backup_id")
    try:
        out["backups"] = install.backups_usage()           # 導入の退避の件数と大きさ（2026-10-17 監査 C88）
    except (OSError, ValueError):                          # 壊れた退避の記録でも状態の取得は止めない（R07B-03）
        out["backups"] = None
    lb = out["latest_build"]
    if lb and (lb.get("files") or {}).get("database.arz"):
        from .mod_package import check_code, compare_files, file_codes, mod_code
        # 照合コード（MOD 全体）とデータベースのコード（database.arz だけ。2026-10-17 監査 C15）
        lb["mod_code"] = mod_code(lb["files"])
        lb["check_code"] = check_code(lb["files"]["database.arz"]["sha256"])
        lb["file_codes"] = file_codes(lb["files"])                     # 機能 SF13
        if inst_files and inst_files.get("database.arz"):
            lb["compare_installed"] = compare_files(inst_files, lb["files"])
        if out["target_ok"]:
            db = lb["files"]["database.arz"]
            arz = os.path.join(lb["build_dir"], str(db.get("path") or "mod/database.arz").replace("/", os.sep))
            lb["game_update"] = _game_update(target, lb["build_dir"], _records_changed(target, arz), gb)
    gu = (out["installed"] or {}).get("game_update")
    if gu and gu["records_changed"]:
        if out["installed"].get("shared"):
            # 受け取った MOD: 作った人に今のゲームで作り直して共有し直してもらう。自分の MOD に置き換えると相手と照合コードが
            # 合わなくなるので、作り直し・最新のビルドの導入は案内しない（2026-10-17 UE-R1）
            gu["guidance"] = "shared_rebuild"
        else:
            # 作り直しの案内: 最新のビルドが今のゲームに合っていれば、それを導入し直すだけでよい（機能 SF09）。最新のビルドの
            # 補給官のレコードを判定できなかったとき（records_changed が None）は「合っている」とみなさない（UE-R3）
            lbu = (lb or {}).get("game_update")
            fixed = bool(lbu and lbu["records_changed"] == [] and lb.get("mod_code") != out["installed"].get("mod_code"))
            gu["guidance"] = "install_latest" if fixed else "rebuild"
    return out


def _installed_extra(svc, tgt: dict) -> dict:
    """導入中の MOD の照合コード（mod_code。MOD 全体）とデータベースのコード（check_code）、受け取った MOD か（shared）、
    「前の状態に戻す」の戻り先（previous）。"""
    from modbuilder import install
    from .mod_package import check_code, mod_code, shared_info_for_build_dir
    inst = tgt.get("installed") or {}
    out = {}
    db = (inst.get("files") or {}).get("database.arz")
    if db:
        out["mod_code"] = mod_code(inst["files"])         # 導入したファイルの記録（全ファイルの SHA-256）から（C15）
        out["check_code"] = check_code(db["sha256"])
    shared = shared_info_for_build_dir(svc, inst.get("build_dir"))
    if shared:
        out["shared"] = {k: shared.get(k) for k in ("share_id", "mod_code", "check_code", "file_name", "received_at",
                                                     "source_build_id")} \
            | {"equipment": [{"id": e.get("id"), "name": e.get("name")} for e in shared.get("equipment") or []]}
        if not out["shared"].get("mod_code") and out.get("mod_code"):     # 2026-10-17 より前に受け取った MOD
            out["shared"]["mod_code"] = out["mod_code"]
    # この導入の前の状態の退避（その時点の mods\ に戻せる）。同じビルドを続けて導入した場合は、前の導入が違うビルドだった
    # 退避まで遡る（2026-10-17 監査 C12。以前は最後の導入の退避で、受け取った MOD を 2 回入れると戻り先がその MOD 自身になった）
    pv = install.previous_install_backup(tgt)
    prev = None
    if pv is not None:
        before = pv["meta"].get("installed_before") or {}
        prev = {"backup_id": pv["backup_id"], "build_id": before.get("build_id"),
                "shared": bool(shared_info_for_build_dir(svc, before.get("build_dir"))),
                "files": sorted(pv["meta"].get("files") or {})}
    out["previous"] = prev
    return out


_GAME_CHANGED_CACHE: dict = {}
_GAME_CHANGED_LOCK = threading.Lock()
_GAME_CHANGED_MAX = 8          # 導入中の mods\database.arz と最新のビルドの database.arz など（2026-10-17 機能 SF09）


def _game_changed(target: str) -> list[str]:
    """導入中の mods\\database.arz の補給官のレコードが、今のゲームの最優先版と違うか（違うレコードの名前）。
    判定できなければ []（C84 のまま）。"""
    return _records_changed(target, os.path.join(target, "mods", "database.arz")) or []


_UNDECIDED = ("undecided",)     # キャッシュの「判定できない」（None と区別する印）


def _records_changed(target: str, arz: str) -> list[str] | None:
    """MOD の database.arz（arz）の補給官のレコードが、導入先のゲームの最優先版と違うか（違うレコードの名前）。
    読み出しに時間がかかるので、arz とゲームの .arz の大きさ・時刻が同じ間は前の結果を使う。
    判定できなければ None（arz が無い・読めない、テスト用の導入先などゲームデータが読めない。2026-10-17 UE-R3。
    以前は [] で「違いなし」と区別できず、最新のビルドを判定できないだけで「作り直しは不要」と案内し得た）。"""
    from modbuilder import install
    if not arz or not os.path.isfile(arz):
        return None
    try:
        game = install._open_game(target)
        s = os.stat(arz)
        key = (os.path.normcase(target), os.path.normcase(os.path.abspath(arz)), s.st_size, s.st_mtime_ns,
               json.dumps(game.fingerprint(), sort_keys=True))
    except Exception:   # noqa: BLE001  テスト用の導入先（偽のゲーム）など
        return None
    # ThreadingHTTPServer の要求が重なっても、他の要求の clear で自分の結果が消えないように、結果はローカルに持って返す
    # （2026-10-17 FG06-R4。以前は入れた後にキャッシュを読み直して KeyError になり得た）
    with _GAME_CHANGED_LOCK:
        res = _GAME_CHANGED_CACHE.get(key)
    if res is None:
        try:
            res = install.official_changes(arz, game)
        except Exception:   # noqa: BLE001  壊れた .arz・ゲームデータの読み出しの失敗など
            res = _UNDECIDED
        with _GAME_CHANGED_LOCK:
            if len(_GAME_CHANGED_CACHE) >= _GAME_CHANGED_MAX:
                _GAME_CHANGED_CACHE.clear()
            _GAME_CHANGED_CACHE[key] = res
    return None if res is _UNDECIDED else list(res)


# ---------------------------------------------------------------------------
# ゲームの更新の目安（Steam のビルド番号・データベースの大きさと時刻。2026-10-17 機能 SF09）
# ---------------------------------------------------------------------------
def _steam_build(target: str | None) -> dict | None:
    if not target:
        return None
    try:
        from modbuilder import steam
        return steam.game_build_info(target)
    except Exception:   # noqa: BLE001
        return None


def _db_fingerprint(gd: str) -> dict:
    """ゲームのデータベースの大きさと時刻（GameData.fingerprint と同じ形。gd は除く）。"""
    from modbuilder.gamedata import DATABASES
    out = {}
    for key, rel in DATABASES.items():
        p = os.path.join(gd, rel.replace("/", os.sep))
        if os.path.isfile(p):
            st = os.stat(p)
            out[key] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    return out


def _build_game_record(build_dir: str | None) -> tuple[dict | None, str | None]:
    """ビルドの記録の game（作ったときのゲームのフォルダーとデータベースの大きさ・時刻）と game_buildid。"""
    from modbuilder.builder import MANIFEST
    if not build_dir:
        return None, None
    try:
        with open(os.path.join(build_dir, MANIFEST), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None, None
    game, bid = m.get("game"), m.get("game_buildid")
    return (game if isinstance(game, dict) else None), (bid if isinstance(bid, str) and bid.isdigit() else None)


def _game_update(target: str, build_dir: str | None, records: list[str] | None, cur: dict | None) -> dict:
    """ビルド（build_dir）を作ったときと今の導入先のゲームの違い。

    判定は補給官のレコードの中身（records = :func:`_records_changed`）で行い、違えば state "rebuild"。Steam のビルド番号
    （作ったときの記録が無い古いビルドは、同じフォルダーのゲームのデータベースの大きさと時刻）は目安で、違ってもレコードが
    同じなら state "updated"（このまま使える）。比べられなければ "unknown"、同じなら "same"。
    レコードを判定できない（records が None）ときは records_changed も None で、版が同じなら "same"、他は "unknown"
    （「このまま使える」とは言わない。2026-10-17 UE-R3）。ビルド番号もデータベースの大きさ・時刻も、ビルドを作ったゲームの
    フォルダー（記録の game.gd）が導入先と同じときだけ比べる（別の Steam のコピーの番号と比べない。UE-R6）。"""
    game, then = _build_game_record(build_dir)
    now = (cur or {}).get("buildid")
    same_gd = bool(game and isinstance(game.get("gd"), str)
                   and os.path.normcase(os.path.abspath(game["gd"])) == os.path.normcase(os.path.abspath(target)))
    buildid_changed = (then != now) if then and now and same_gd else None
    files_changed = None
    if same_gd and not game.get("fake"):
        before = {k: v for k, v in game.items() if k != "gd"}
        if before and all(isinstance(v, dict) for v in before.values()):
            files_changed = before != _db_fingerprint(target)
    changed = buildid_changed if buildid_changed is not None else files_changed
    if records:
        state = "rebuild"
    elif changed is False:
        state = "same"
    elif changed and records is not None:
        state = "updated"
    else:
        state = "unknown"
    return {"state": state, "records_changed": None if records is None else list(records),
            "build_buildid": then, "current_buildid": now,
            "buildid_changed": buildid_changed, "files_changed": files_changed,
            "update_pending": bool((cur or {}).get("update_pending")), "pending_buildid": (cur or {}).get("target_buildid")}


_AFTER_UNINSTALL_CACHE: dict = {}      # baseline（kinds の記録の無い古い state.json）→ 退避から判定した種別（UE-R5）


def _after_uninstall(tgt: dict) -> dict:
    """アンインストールで戻す、最初の導入の前のファイルの持ち主（2026-10-17 利用者の判断 C126）。
    aiskin: 記録の無い AI Skin の MOD（アンインストールの後も残り、読み込まれる）/ foreign: 他の MOD / unknown。
    baseline に kinds の記録の無いファイル（古い state.json）は、アンインストールの計画と同じく退避のファイルを判定する
    （読み取りのみ。退避は変わらないので結果を覚える。読めなければ unknown。2026-10-17 UE-R5）。"""
    from modbuilder import install
    base = tgt.get("baseline") or {}
    recorded = base.get("kinds") or {}
    if not any(recorded.get(r) in (None, "absent") for r in base.get("files") or {}):
        kinds = install.baseline_kinds(base)
    else:
        try:
            key = json.dumps([install._backups_root(), base], sort_keys=True, default=str)
        except Exception:   # noqa: BLE001
            key = None
        with _GAME_CHANGED_LOCK:
            kinds = _AFTER_UNINSTALL_CACHE.get(key) if key else None
        if kinds is None:
            try:
                writes = install._baseline_writes(base)
            except Exception:   # noqa: BLE001  退避が無い・読めない
                writes = None
            kinds = install.baseline_kinds(base, writes)
            if writes is not None and key:
                with _GAME_CHANGED_LOCK:
                    if len(_AFTER_UNINSTALL_CACHE) >= 4:
                        _AFTER_UNINSTALL_CACHE.clear()
                    _AFTER_UNINSTALL_CACHE[key] = kinds
    return {"aiskin": sorted(r for r, k in kinds.items() if k == "aiskin-project"),
            "foreign": sorted(r for r, k in kinds.items() if k == "foreign"),
            "unknown": sorted(r for r, k in kinds.items() if k == "unknown")}


def compare_targets(svc) -> dict:
    """照合で比べる相手（機能 SF13）: {"installed": {"build_id", "files", "shared", "file_name"} | None,
    "latest_build": {"build_id", "files"} | None}。導入の記録とビルドの記録だけを読む（mods\\ とゲームデータは読まない）。"""
    from modbuilder import install
    from .mod_package import shared_info_for_build_dir
    from modbuilder.gamedata import looks_like_install_target
    out = {"installed": None, "latest_build": None}
    target, _ = resolve_install_target(svc.settings.get())
    if target and looks_like_install_target(target):        # 使えない導入先の記録とは比べない（点検 R19B-02）
        try:
            tgt = install.load_state()["targets"].get(install._target_key(target, "basemods", None)) or {}
        except (OSError, ValueError):
            tgt = {}
        inst = tgt.get("installed") or {}
        if (inst.get("files") or {}).get("database.arz"):
            sh = shared_info_for_build_dir(svc, inst.get("build_dir"))
            out["installed"] = {"build_id": inst.get("build_id"), "files": inst["files"], "shared": bool(sh),
                                "file_name": (sh or {}).get("file_name")}
    for b in list_builds(svc, 50):
        if b["status"] == "ok" and (b.get("files") or {}).get("database.arz"):
            out["latest_build"] = {"build_id": b["build_id"], "files": b["files"]}
            break
    return out


# ---------------------------------------------------------------------------
# 導入で入れ替わる装備（今ゲームにある装備のうち、新しいビルドに無いもの）
# ---------------------------------------------------------------------------
def game_equipment(mod_dir: str) -> dict[str, str] | None:
    """MOD のフォルダー（database.arz・Text_EN.arc）にある装備の頭装備レコード → 表示名。読めなければ None。

    装備 = ``records/items/aiskin*`` の下の、テンプレートが armor_head の レコード（透明の部位は armor_head 以外）。"""
    from modbuilder.arc import ArcFile
    from modbuilder.arz import ArzFile
    from modbuilder.gamedata import parse_tags
    arz = os.path.join(mod_dir, "database.arz")
    if not os.path.isfile(arz):
        return None
    try:
        a = ArzFile(arz)
        tags: dict[str, str] = {}
        text = os.path.join(mod_dir, "Text_EN.arc")
        if os.path.isfile(text):
            arc = ArcFile(text)
            for n in arc.names():
                if n.lower().endswith(".txt"):
                    tags.update(parse_tags(arc.read(n)))
        out = {}
        for n in a.names():
            if not n.lower().startswith("records/items/aiskin"):
                continue
            f = dict(a.record_fields(n) or [])
            if str(f.get("templateName", "")).replace("\\", "/").lower().endswith("armor_head.tpl"):
                tag = str(f.get("itemNameTag") or "")
                out[n] = tags.get(tag, tag or n)
        return out
    except Exception:   # noqa: BLE001  他の MOD の database.arz・壊れたファイル
        return None


def equipment_change(target_mods: str, build_mod_dir: str) -> dict:
    """{"removed": [{"record", "name"}], "added": [...], "kept": 件数}。今の mods/ が読めなければ空。"""
    before = game_equipment(target_mods)
    after = game_equipment(build_mod_dir) or {}
    if before is None:
        return {"removed": [], "added": [{"record": r, "name": n} for r, n in sorted(after.items())], "kept": 0, "known": False}
    return {"removed": [{"record": r, "name": n} for r, n in sorted(before.items()) if r not in after],
            "added": [{"record": r, "name": n} for r, n in sorted(after.items()) if r not in before],
            "kept": len(set(before) & set(after)), "known": True}


# ---------------------------------------------------------------------------
# 計画（確認ダイアログ用。mods/ は変更しない）
# ---------------------------------------------------------------------------
def _blocking_lines(msg: str) -> list[str]:
    return [ln.strip()[2:] for ln in msg.splitlines() if ln.strip().startswith("- ")]


# 中止の文（modbuilder.install の InstallError と _previous。どれも表示言語の文）の分け方。その言語の語で見る
# （英語の文は大文字小文字を問わない。modbuilder の英語の文にもこの語を入れる）
def _says_game_running(msg: str) -> bool:
    """Grim Dawn が起動中で止めた文か（英語の文は "Grim Dawn is running"）。reason の属性の無い例外（古い modbuilder）だけに
    使う（:func:`_game_running_error`）。"""
    return tr("起動中", "grim dawn is running") in msg.lower()


def _game_running_error(e) -> bool:
    """modbuilder の InstallError が Grim Dawn の起動中で止めたものか。今の modbuilder は reason（game_running）で分ける
    （英語の注意の文 "Could not check whether Grim Dawn is running" も文の目印に当たるので、文では分けない）。reason の
    属性が無い例外だけ文の目印で見る（2026-11-04 退行の確認 2 回目 B2・点検 C5_english-01）。"""
    if hasattr(e, "reason"):
        return e.reason == "game_running"
    return _says_game_running(str(e))


def _says_no_record(msg: str) -> bool:
    """導入・退避の記録が無くて止めた文か（英語の文は "no record"）。"""
    return tr("記録がありません", "no record") in msg.lower()


def plan(svc, action: str, build_id: str | None = None, replace_foreign: bool = False, force: bool = False,
         shared_id: str | None = None) -> dict:
    """action: install（shared_id があれば受け取った MOD）/ uninstall / restore（今の導入の前の状態に戻す）。"""
    from modbuilder import install
    from modbuilder.errors import InstallError
    target, is_test = _target(svc)
    base = {"action": action, "target": target, "is_test_target": is_test, "replace_foreign": bool(replace_foreign),
            "force": bool(force), "game_running": envcheck.game_running(), "launch_option": envcheck.launch_option_state(),
            "shared_id": shared_id}
    try:
        info = install.inspect_target(target, "basemods", None)
    except InstallError as e:       # 導入の記録（state.json）が壊れている（点検 R07B-03）
        return {**base, "existing": {}, "conflicts": [], "kept_foreign": [], "basemods_others": [], "executable": False,
                "blocking_code": "INSTALL_BLOCKED", "blocking": [str(e)], "message": str(e), "reason": e.reason,
                "can_replace_foreign": False, "can_force": False}
    base["existing"] = info["files"]
    # conflicts: 退避して置き換える（同意が要る）他の MOD・変更されたファイル。kept_foreign: 新しいビルドに無いので触らない
    # 他の MOD のファイル。basemods_others: mods\ 直下の他の .arc・.arz（/basemods で読み込まれ続ける）。
    # どれも modbuilder の計画から取る（2026-10-17 監査 C85・C89。以前は 4 つのファイルの foreign を全て conflicts にしていた）
    base["conflicts"] = []
    base["kept_foreign"] = []
    base["basemods_others"] = info["basemods_others"]
    if base["game_running"]:
        return {**base, "executable": False, "blocking_code": "GAME_RUNNING",
                "blocking": [tr("Grim Dawn が起動中です。ゲームを完全に終了してから実行してください",
                                "Grim Dawn is running. Exit the game completely, then try again")]}
    try:
        if action == "install":
            b = resolve_build(svc, build_id, shared_id)
            base["build"] = b
            p = install.install(target, b["build_dir"], "basemods", yes=False, force=force, replace_foreign=replace_foreign,
                                log=lambda m: None)
        elif action == "uninstall":
            p = install.uninstall(target, "basemods", yes=False, force=force, log=lambda m: None)
        elif action == "restore":
            prev = _previous(svc, target)
            base["previous"] = prev
            p = install.restore(target, prev["backup_id"], yes=False, force=force, log=lambda m: None)
        else:
            raise BackendError("INVALID_INPUT", tr(f"action は install / uninstall / restore: {action}",
                                                   f"action must be install, uninstall or restore: {action}"))
    except BackendError as e:
        if e.code != "INSTALL_BLOCKED":
            raise
        # 受け取った MOD が中身の検査で拒否されている（mod_package.check_content）
        return {**base, "executable": False, "blocking_code": "INSTALL_BLOCKED",
                "blocking": list(e.params.get("blocking") or [e.message]), "message": e.message,
                "can_replace_foreign": False, "can_force": False}
    except InstallError as e:
        msg = str(e)
        lines = _blocking_lines(msg) or [msg]
        code = "GAME_RUNNING" if _game_running_error(e) else "INSTALL_BLOCKED"
        if _says_no_record(msg) and e.reason is None:       # 退避の記録が無いのは backup_missing の文にする
            code = "NOT_FOUND"
        stopped = getattr(e, "plan", None) or {}
        conflicts = stopped.get("conflicts") or []
        if action == "install":
            can_replace = any(c["kind"] == "foreign" for c in conflicts) and not replace_foreign
            can_force = any(c["kind"] == "modified" for c in conflicts) and not force
        else:
            # uninstall・restore は replace_foreign を受け取らず、foreign も modified も force だけで続ける
            # （2026-10-17 FG06-R3。以前は restore の foreign に「置き換えに同意」を出し、同意しても続けられなかった）
            can_replace, can_force = False, bool(conflicts) and not force
        return {**base, "executable": False, "blocking_code": code, "blocking": lines, "message": msg,
                "reason": e.reason,             # build_files・backup_missing など。画面の文を選ぶ（点検 R19B-03）
                "conflicts": conflicts, "kept_foreign": stopped.get("kept_foreign") or [],
                "warnings": stopped.get("warnings") or [],
                "can_replace_foreign": can_replace, "can_force": can_force}
    keep = ("copy", "backup", "remove", "restore", "warnings", "baseline_backup", "game_language", "build_id", "backup_id",
            "conflicts", "kept_foreign", "game_changed",
            "restore_kinds", "aiskin_remains")      # アンインストールで戻す・残る AI Skin の MOD（2026-10-17 C126）
    out = {**base, "executable": True, "blocking": [], **{k: p.get(k) for k in keep if k in p}}
    if action == "install":
        # 導入でゲームから消える装備（2026-10-16: 1 件だけのビルドで 8 体が消えた。確認ダイアログで必ず見せる）
        out["equipment_change"] = equipment_change(os.path.join(target, "mods"), os.path.join(base["build"]["build_dir"], "mod"))
        if shared_id:
            out["shared"] = base["build"].get("shared")
    elif action == "restore":
        from modbuilder import paths as mb_paths
        out["equipment_change"] = equipment_change(
            os.path.join(target, "mods"), os.path.join(mb_paths.install_dir(), "backups", base["previous"]["backup_id"], "files"))
    return out


def _previous(svc, target: str) -> dict:
    """今の導入をしたときの退避（「前の状態に戻す」の戻り先）。無ければ InstallError。"""
    from modbuilder import install
    from modbuilder.errors import InstallError
    st = install.load_state()
    tgt = st["targets"].get(install._target_key(target, "basemods", None)) or {}
    if not tgt.get("installed"):
        raise InstallError(tr("このアプリで導入した記録がありません", "There is no record of an installation by this app"))
    prev = _installed_extra(svc, tgt).get("previous")
    if not prev:
        raise InstallError(tr("前の状態に戻す記録がありません（導入の前の退避が見つかりません）",
                              "There is no record to restore the previous state from (the backup from before the "
                              "installation was not found)"))
    return prev


# ---------------------------------------------------------------------------
# ジョブ（バックエンドの中で実行。取消は受け付けない）
# ---------------------------------------------------------------------------
def _library(svc):
    from modbuilder.library import Library
    try:
        return Library.open(svc.library_dir(), create=True)
    except Exception:   # noqa: BLE001
        return None


WRITE_CAUSES = ("in_use", "read_only", "permission", "disk_full")


def _install_error(e) -> BackendError:
    """modbuilder の InstallError → 画面に出すエラー。

    InstallFailed（mods\\ への書き込みの失敗。2026-10-17 監査 C11）は INSTALL_BLOCKED のまま params.reason で文を分ける:
    write_failed[_原因]（操作の前の状態のまま）/ backup_failed（データフォルダーに退避を作れなかった。mods\\ は未変更）/
    rollback_incomplete（元に戻せなかったファイルがある。unrestored と backup_id を文に入れる）/ state_failed（mods\\ の
    変更は完了したが導入の記録 state.json を保存できなかった。R07B-02）。以前は生の OSError が INTERNAL になり、画面は
    無条件に「元に戻しています」と出していた。"""
    from modbuilder.errors import InstallFailed
    msg = str(e)
    if isinstance(e, InstallFailed):
        if getattr(e, "stage", "mods") == "state":
            # 戻していない（rolled_back=False）が、元に戻せなかったファイルは無い。rollback_incomplete の文（unrestored が
            # 空の「元に戻せなかったファイルがあります: 」）にしない
            reason = "state_failed"
        elif not e.rolled_back:
            reason = "rollback_incomplete"
        elif getattr(e, "stage", "mods") == "backup":
            # データフォルダー（install\\backups）に退避を作れなかった。mods\\ は変更していない（2026-10-17 FG06-R5。
            # 以前は write_failed_<原因> になり、画面は「mods フォルダーに書き込めない」と違う場所を案内していた）
            reason = "backup_failed"
        else:
            reason = "write_failed" + (f"_{e.cause}" if e.cause in WRITE_CAUSES else "")
        return BackendError("INSTALL_BLOCKED", msg, params={
            "reason": reason, "cause": e.cause, "action": e.action, "rolled_back": e.rolled_back, "changed": e.changed,
            "stage": getattr(e, "stage", "mods"),
            "unrestored": tr("、", ", ").join("mods\\" + r.replace("/", "\\") for r in e.unrestored),
            "unrestored_files": list(e.unrestored), "backup_id": e.backup_id or "", "blocking": [msg]})
    reason = getattr(e, "reason", None)
    code = "GAME_RUNNING" if _game_running_error(e) else (
        "NOT_FOUND" if _says_no_record(msg) and reason is None else "INSTALL_BLOCKED")
    params = {"blocking": _blocking_lines(msg)}
    if reason:                                  # build_files・backup_missing・install_state（点検 R19B-03）
        params["reason"] = reason
    return BackendError(code, msg, params=params)


def run_install(svc, ctx, params: dict) -> dict:
    from modbuilder import install
    from modbuilder.errors import InstallError
    with svc.mod_lock:
        target, is_test = _target(svc)
        if envcheck.game_running():
            raise BackendError("GAME_RUNNING", tr("Grim Dawn が起動中です", "Grim Dawn is running"))
        b = resolve_build(svc, params.get("build_id"), params.get("shared_id"))
        ctx.progress(0.1, "install_backup")
        if params.get("shared_id"):
            what = tr(f"導入: 受け取った MOD ビルド {b['build_id']} → {target}",
                      f"Install: received MOD build {b['build_id']} → {target}")
        else:
            what = tr(f"導入: ビルド {b['build_id']} → {target}", f"Install: build {b['build_id']} → {target}")
        ctx.info(what + (tr("（テスト用の導入先）", " (test install target)") if is_test else ""))
        try:
            p = install.install(target, b["build_dir"], "basemods", yes=True, force=bool(params.get("force")),
                                replace_foreign=bool(params.get("replace_foreign")), library=_library(svc),
                                log=lambda m: ctx.info(m))
        except InstallError as e:
            raise _install_error(e) from e
        ctx.progress(1.0, "install_done")
        out = {"executed": p.get("executed"), "backup_id": p.get("backup_id"), "copy": p.get("copy"),
               "backup": p.get("backup"), "remove": p.get("remove"), "warnings": p.get("warnings"),
               "build_id": b["build_id"], "target": target, "is_test_target": is_test}
        db = (b.get("files") or {}).get("database.arz")
        if db:
            from .mod_package import check_code, mod_code
            out["mod_code"] = mod_code(b["files"])             # 照合コード（MOD 全体。2026-10-17 監査 C15）
            out["check_code"] = check_code(db["sha256"])       # データベースのコード
        if b.get("shared"):
            out["shared"] = {k: b["shared"].get(k) for k in ("share_id", "mod_code", "check_code", "file_name")}
        return out


def run_restore(svc, ctx, params: dict) -> dict:
    """今の導入の前の状態（その導入で作った退避）に戻す。受け取った MOD を外して自分の MOD に戻すときに使う。"""
    from modbuilder import install
    from modbuilder.errors import InstallError
    with svc.mod_lock:
        target, is_test = _target(svc)
        if envcheck.game_running():
            raise BackendError("GAME_RUNNING", tr("Grim Dawn が起動中です", "Grim Dawn is running"))
        ctx.progress(0.1, "restore_previous")
        try:
            prev = _previous(svc, target)
            if params.get("backup_id") and params["backup_id"] != prev["backup_id"]:
                raise BackendError("INSTALL_BLOCKED", tr("確認した後に導入の状態が変わりました。もう一度操作してください",
                                                         "The install state changed after it was checked. Try again"))
            ctx.info(tr(f"前の状態に戻す: 退避 {prev['backup_id']} → {target}",
                        f"Restore the previous state: backup {prev['backup_id']} → {target}")
                     + (tr("（テスト用の導入先）", " (test install target)") if is_test else ""))
            p = install.restore(target, prev["backup_id"], yes=True, force=bool(params.get("force")), library=_library(svc),
                                log=lambda m: ctx.info(m))
        except InstallError as e:
            raise _install_error(e) from e
        ctx.progress(1.0, "restore_done")
        # 戻した先が「本ツールの導入なし」なら、装備の状態は install.restore が導入なしにする（失敗は warnings の注意）
        st = status(svc)
        return {"executed": p.get("executed"), "backup_id": p.get("backup_id"), "restored": prev["backup_id"],
                "restore": p.get("restore"), "remove": p.get("remove"), "warnings": p.get("warnings"), "target": target,
                "is_test_target": is_test, "installed": st.get("installed")}


def run_uninstall(svc, ctx, params: dict) -> dict:
    from modbuilder import install
    from modbuilder.errors import InstallError
    with svc.mod_lock:
        target, is_test = _target(svc)
        if envcheck.game_running():
            raise BackendError("GAME_RUNNING", tr("Grim Dawn が起動中です", "Grim Dawn is running"))
        ctx.progress(0.1, "uninstall_restore")
        ctx.info(tr(f"アンインストール: {target}", f"Uninstall: {target}")
                 + (tr("（テスト用の導入先）", " (test install target)") if is_test else ""))
        try:
            p = install.uninstall(target, "basemods", yes=True, force=bool(params.get("force")), library=_library(svc),
                                  log=lambda m: ctx.info(m))
        except InstallError as e:
            raise _install_error(e) from e
        ctx.progress(1.0, "uninstall_done")
        return {"executed": p.get("executed"), "backup_id": p.get("backup_id"), "remove": p.get("remove"),
                "restore": p.get("restore"), "warnings": p.get("warnings"), "target": target, "is_test_target": is_test}


def prune_backups(svc, keep: int = 5, yes: bool = False) -> dict:
    """古い導入の退避を消す（利用者が操作したときだけ。アンインストールと「前の状態に戻す」が使う退避は残す。
    2026-10-17 監査 C88）。yes=False なら消すものの一覧だけ。設定画面の「データの容量と整理」は storage.plan・
    storage.cleanup（機能 SF03。確認した ID だけを消す）を使う。"""
    from modbuilder import install
    with svc.mod_lock:
        p = install.prune_backups(keep=max(0, int(keep)), yes=bool(yes), log=lambda m: logs.info(m))
    return {k: p.get(k) for k in ("keep", "count", "remove", "removed", "failed", "usage", "executed")} \
        | {"protected": sorted(p.get("protected") or {})}


def run_build(svc, ctx, params: dict) -> dict:
    s = svc.settings.get()
    gd = resolve_game_dir(s)
    if gd is None:
        raise BackendError("GAME_NOT_FOUND", tr("Grim Dawn のインストール先が見つかりません（MOD の作成にゲームデータが必要）",
                                                "Grim Dawn install folder not found (game data is needed to build the MOD)"))
    from .errors import JobCancelled
    base = svc.builds_dir()
    before = set(os.listdir(base)) if os.path.isdir(base) else set()
    with svc.mod_lock:
        try:
            res = ctx.run_worker("build", {"library_dir": svc.library_dir(), "gd": gd,
                                           "languages": params.get("languages") or s["build_languages"],
                                           "include_dev": bool(params.get("include_dev", s["include_dev_equipment"]))},
                                 (0.0, 1.0), default_error="MOD_BUILD_FAILED")
        except JobCancelled:
            _remove_new_builds(base, before)
            raise
        except Exception:
            # 取消以外の失敗: 記録（manifest）の無い作りかけだけを消す。失敗の記録（status failed）があるものは、ビルドの
            # 一覧に失敗として出すために残す（2026-10-17 点検 C83）
            _remove_new_builds(base, before, keep_failed=True)
            raise
    ctx.info(tr(f"MOD を作成しました: ビルド {res.get('build_id')}（装備 {len(res.get('equipment') or [])} 件）",
                f"MOD built: build {res.get('build_id')} ({len(res.get('equipment') or [])} equipment)"))
    return res


def _remove_new_builds(base: str, before: set, keep_failed: bool = False) -> None:
    """このジョブで増えたビルドのフォルダーのうち、成功の記録が無いもの（取り消した作りかけ）を消す。keep_failed なら
    失敗の記録（status failed の manifest）があるものは残し、記録の無いものだけを消す。"""
    from modbuilder.builder import MANIFEST
    if not os.path.isdir(base):
        return
    for name in set(os.listdir(base)) - before:
        d = os.path.join(base, name)
        status = None
        try:
            with open(os.path.join(d, MANIFEST), encoding="utf-8") as f:
                status = json.load(f).get("status")
        except (OSError, ValueError):
            pass
        if status == "ok" or (keep_failed and status == "failed"):
            continue
        shutil.rmtree(d, ignore_errors=True)


def run_refs(svc, ctx, params: dict) -> dict:
    gd = resolve_game_dir(svc.settings.get())
    if gd is None:
        raise BackendError("GAME_NOT_FOUND", tr("Grim Dawn のインストール先が見つかりません", "Grim Dawn install folder not found"))
    return ctx.run_worker("refs", {"refs_dir": svc.refs_dir(), "gd": gd}, (0.0, 1.0), default_error="EXPORT_FAILED")


# ---------------------------------------------------------------------------
# フォルダーへの出力（計画書 11 章「出力先の選択」）
# ---------------------------------------------------------------------------
README_NAME = "README_GDSkin.txt"
EXPORT_PART = ".gdskin-part"          # 出力の途中のファイルの一時名（全部そろってから正式な名前にする）


def _real_path(p: str) -> str:
    """実際の場所（ジャンクション・シンボリックリンク・8.3 の短い名前を解決し、\\\\?\\ の前置きを外す。まだ無い末尾はそのまま）。"""
    r = os.path.realpath(os.path.abspath(p))
    if r.startswith("\\\\?\\UNC\\"):
        r = "\\\\" + r[len("\\\\?\\UNC\\"):]
    elif r.startswith("\\\\?\\"):
        r = r[len("\\\\?\\"):]
    return r


def _within_real(path: str, root: str) -> bool:
    """path が root の中か。文字列の比較に加えて、実際の場所で比べる（8.3 の短い名前・ジャンクション・\\\\?\\ 形式・
    管理共有などの別名でも、同じフォルダーなら中とみなす。2026-10-27 検証 R12A-01・R12B-04）。"""
    from .paths import is_within
    if is_within(path, root) or is_within(_real_path(path), _real_path(root)):
        return True
    if not os.path.isdir(root):
        return False
    p = os.path.abspath(path)
    while True:                          # path の存在する祖先が root と同じフォルダーか（別名の共有など）
        if os.path.isdir(p):
            try:
                if os.path.samefile(p, root):
                    return True
            except OSError:
                pass
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent


def export(svc, build_id: str | None, dest: str) -> dict:
    from modbuilder.library import sha256_file
    if not dest or not isinstance(dest, str):
        raise BackendError("INVALID_INPUT", tr("出力先のフォルダーを指定してください", "Specify the destination folder"))
    dest = os.path.abspath(dest)
    s = svc.settings.get()
    games = sorted({d for d in (resolve_game_dir(s), resolve_install_target(s)[0]) if d})
    for d in games:
        if _within_real(dest, os.path.join(d, "mods")):
            raise BackendError("INVALID_INPUT", tr("Grim Dawn の mods フォルダーには「Grim Dawn に導入」を使ってください"
                                                   "（退避と競合の確認をするため）",
                                                   "Use \"Install to Grim Dawn\" for Grim Dawn's mods folder (it backs up "
                                                   "existing files and checks for conflicts)"),
                               params={"reason": "export_into_mods"})
    # ゲームのフォルダーの中（直下・gdx1\database など）にも書かない。フォルダーを作る前に断る（2026-10-17 監査 C93）
    for d in games:
        if _within_real(dest, d):
            raise BackendError("INVALID_INPUT", tr("Grim Dawn のフォルダーの中には出力できません（ゲームのファイルと混ざるため）。"
                                                   "ゲームのフォルダーの外を選んでください",
                                                   "You cannot export into the Grim Dawn folder (the files would mix with the "
                                                   "game files). Choose a folder outside the game folder"),
                               params={"reason": "export_into_game"})
    b = get_build(svc, build_id)
    os.makedirs(dest, exist_ok=True)
    clash = [n for n in list(b["files"]) + [README_NAME] if os.path.exists(os.path.join(dest, n))]
    if clash:
        raise BackendError("INVALID_INPUT", tr(f"出力先に同じ名前のファイルがあります: {clash}",
                                               f"Files with the same names already exist in the destination: {clash}"),
                           params={"reason": "export_exists", "files": clash})
    # 一時名（<名前>.gdskin-part）に書いてハッシュを照合し、全部そろってから正式な名前にする。途中で失敗したら、この呼び出しで
    # 書いたファイルを消す（以前は書きかけのファイルが正式な名前で残り、やり直すと export_exists で止まった。レビュー R12B-02）
    copied, parts, done = [], [], []
    try:
        for name, rec in b["files"].items():
            src = os.path.join(b["build_dir"], rec["path"].replace("/", os.sep))
            try:
                same = sha256_file(src) == rec["sha256"]
            except OSError as e:
                raise BackendError("EXPORT_FAILED", tr(f"ビルドのファイルを読めません: {name}: {e}",
                                                       f"Cannot read a build file: {name}: {e}"),
                                   params={"reason": "export_build_changed", "file": name}) from e
            if not same:
                raise BackendError("EXPORT_FAILED", tr(f"ビルドのファイルが記録と一致しません（ビルドの後に変更された）: {name}",
                                                       f"A build file does not match the record (changed after the build): "
                                                       f"{name}"),
                                   params={"reason": "export_build_changed", "file": name})
            part = os.path.join(dest, name + EXPORT_PART)
            parts.append(part)
            shutil.copyfile(src, part)
            if sha256_file(part) != rec["sha256"]:
                raise BackendError("EXPORT_FAILED", tr(f"コピーしたファイルのハッシュが一致しません: {name}",
                                                       f"The hash of the copied file does not match: {name}"),
                                   params={"reason": "export_verify", "file": name})
            copied.append({"file": name, "size": rec["size"], "sha256": rec["sha256"]})
        names = [c["file"] for c in copied]
        part = os.path.join(dest, README_NAME + EXPORT_PART)
        parts.append(part)
        # 出力したフォルダーは他の人に渡すこともあるので、表示言語に依らず日本語の後に英語を書く（2 言語対応 D-060）
        text = {"ja": ("GD Skin Studio で作成した Grim Dawn の MOD（/basemods 形式）\n\n"
                       f"ビルド: {b['build_id']}\n"
                       f"ファイル（{len(names)} 個）: {', '.join(names)}\n"
                       f"導入: 上の {len(names)} 個のファイルを <Grim Dawn>\\mods\\ に置き、Steam の起動オプションに /basemods を設定します。\n"
                       "mods\\ に同じ名前のファイル（他の MOD のものなど）があれば、置く前に別の場所へ退避してください。\n"
                       "mods\\database.arz は 1 つしか読まれないため、他のメインキャンペーン用 MOD とは同時に使えません。\n"
                       "アプリの「Grim Dawn に導入」を使うと、既存のファイルの退避と競合の確認を自動で行います。\n"),
                "en": ("Grim Dawn MOD made with GD Skin Studio (/basemods format)\n\n"
                       f"Build: {b['build_id']}\n"
                       f"Files ({len(names)}): {', '.join(names)}\n"
                       f"Install: put the {len(names)} files above into <Grim Dawn>\\mods\\ and add /basemods to the Steam "
                       "launch options.\n"
                       "If mods\\ has files with the same names (from other MODs, etc.), move them somewhere else before "
                       "placing these files.\n"
                       "Only one mods\\database.arz is read, so this cannot be used together with other main-campaign MODs.\n"
                       "\"Install to Grim Dawn\" in the app backs up existing files and checks for conflicts automatically.\n")}
        with open(part, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text["ja"] + "\n" + "-" * 72 + "\n\n" + text["en"])
        for part in parts:
            final = part[:-len(EXPORT_PART)]
            os.replace(part, final)
            done.append(final)
    except Exception as e:
        for p in parts + done:
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        if isinstance(e, OSError):
            full = e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)
            raise BackendError("EXPORT_FAILED", tr(f"出力先にファイルを書けません: {type(e).__name__}: {e}",
                                                   f"Cannot write files to the destination: {type(e).__name__}: {e}"),
                               params={"reason": "export_copy", "cause": "disk_full" if full else "os_error"}) from e
        raise
    logs.info(tr(f"MOD を出力しました: ビルド {b['build_id']}（{len(copied)} ファイル）",
                 f"Exported the MOD: build {b['build_id']} ({len(copied)} files)"))
    return {"build_id": b["build_id"], "dest": dest, "files": copied}
