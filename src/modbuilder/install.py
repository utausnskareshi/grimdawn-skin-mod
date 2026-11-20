"""mods/ への導入・アンインストール・再導入・退避からの復元（計画書 11 章、D-002）。

* 形式: ``basemods``（既定。``<GD>/mods/`` 直下に database.arz・13 言語の Text_XX.arc・Items.arc。Steam の
  起動オプション /basemods で読まれる。mods/database.arz は 1 つしか読まれないので、他のメインキャンペーン用 MOD と排他）
  と ``custom``（任意。``<GD>/mods/<名前>/database/<名前>.arz`` と ``resources/*.arc``。Custom Game から選ぶ）。
* 置き換える前に、mods/ の既存ファイルの持ち主を判定する:
    modbuilder       本ツールが導入したもの（導入状態の記録とハッシュが一致）
    modified         本ツールが導入したが、その後に変更されている → 中止（--force で続行）
    aiskin-project   試作MOD など本プロジェクトの手作業ビルド（中身が全て AI Skin のレコード・タグ・アセット）→ 退避して置き換え
    foreign          他の MOD・不明なファイル → 警告して中止（他 MOD との競合。--replace-foreign で退避して置き換え）
* 置き換え・削除するファイルは必ず先に ``%LOCALAPPDATA%\\GrimDawnSkinMOD\\install\\backups\\<ID>\\`` へ退避し、
  コピー後にハッシュ（SHA-256）を照合する。同じ内容のファイルは ``backups\\_blobs\\<sha256>`` へのハードリンクで共有する
  （同じビルドの再導入で退避が増えないように。作れないドライブではコピー。2026-10-17 監査 C88）。
* mods/ の変更は 2 段階（2026-10-17 監査 C11）: 全ファイルを mods/ の中の一時名（``*.modbuilder-tmp``）にコピーして
  ハッシュを照合してから（ここまでは既存のファイルを変えない）、置き換え・削除を行う。途中で失敗したら、実際に変更した
  ファイルだけを退避から戻し（1 件の失敗で止めずに全件を試す）、:class:`~modbuilder.errors.InstallFailed` にする。
  読み取り専用のファイルは計画の段階で知らせて止める。
* ``yes=False`` のときは計画を返すだけで何も変更しない（CLI は --yes が無ければ確認用の表示だけ）。
* ゲームの実行中は中止する（起動中は .arc がロックされ、database.arz は次回起動まで反映されない [推定]）。起動しているか
  判定できないときは計画に注意を出す。置き換えの直前にもう一度確かめる（2026-10-17 監査 C90）。
* Steam の起動オプションは書き換えない（:func:`modbuilder.steam.basemods_guide` の案内を返す）。
* 退避は自動では消さない。:func:`prune_backups`（CLI の ``backups --prune``）で、アンインストールと「前の状態に戻す」が
  使う退避を残して古いものを消す。
"""
from __future__ import annotations

import datetime as _dt
import errno
import json
import os
import shutil
import stat
import time

from . import paths, steam
from ._i18n import tr
from .arc import ArcFile
from .arz import ArzFile, norm_path, split_array
from .builder import CUSTOM_NAME_RE, RESERVED_CUSTOM_NAMES, WINDOWS_RESERVED_NAMES, load_manifest
from .errors import InstallError, InstallFailed
from .gamedata import looks_like_install_target
from .library import Library, now_iso, sha256_file
from .naming import MERCHANT_RECORDS, NAMESPACE
from .records import LANGUAGES

__all__ = ["BASEMODS_FILES", "classify_file", "inspect_target", "install", "uninstall", "restore", "reinstall",
           "status", "list_backups", "load_state", "previous_install_backup", "protected_backups", "prune_backups",
           "backups_usage", "official_changes", "GAME_CHANGED_WARNING", "baseline_kinds", "AISKIN_REMAINS_WARNING",
           "InstallStateBroken"]

# basemods 形式で管理するファイル。Text_XX.arc は v1.3.1.1 の 13 言語（2026-10-17 利用者の判断 C124。以前の版のビルドは
# Text_EN.arc（と Text_JA.arc）だけで、新しい版のビルドを導入すると増える）
BASEMODS_FILES = ("database.arz",) + tuple(f"Text_{lang}.arc" for lang in LANGUAGES) + ("Items.arc",)
STATE_FORMAT = "gdskin-install-state"
TMP_SUFFIX = ".modbuilder-tmp"          # mods/ の中に作る一時ファイル（本ツールだけが使う名前）
BLOBS = "_blobs"                        # 退避の中身を内容のハッシュで共有する場所（backups\_blobs\<sha256>）
# 試作MOD の検証用 variant（prototype/mod01 の testfriendly / friendlyatzero）が上書きする公式レコード
PROJECT_EXTRA_RECORDS = {"records/creatures/pc/faction_player.dbr", "records/game/gamefactions.dbr"}
# 利用者に見せる文は、使うときに表示言語（tr）で選ぶ。以前のモジュールの定数の名前（CAUSE_TEXT・GAME_CHANGED_WARNING・
# AISKIN_REMAINS_WARNING・LIBRARY_UPDATE_WARNING。CLI・テストが使う）は __getattr__ が読んだときの表示言語で返す（2026-10-24 D-060）
def _cause_texts() -> dict[str, str]:
    """原因 → 利用者に見せる説明（InstallFailed.cause）。"""
    return {
        "in_use": tr("ファイルが他のプログラム（ゲーム・ウイルス対策ソフト・他のツールなど）に使われています",
                     "The file is in use by another program (the game, antivirus software, another tool, etc.)"),
        "read_only": tr("ファイルが読み取り専用です", "The file is read-only"),
        "permission": tr("書き込みの権限がありません", "No permission to write"),
        "disk_full": tr("ディスクの空き容量が足りません", "Not enough free disk space"),
        "other": tr("ファイルを書き込めませんでした", "Could not write the file"),
    }


def _cause_text(cause: str) -> str:
    return _cause_texts()[cause]


def _game_changed_warning() -> str:
    return tr("ゲームが更新され、補給官の品ぞろえ（公式のレコード）が MOD を作ったときと変わっています。"
              "このまま使うと、ゲームの新しい品ぞろえを古い内容で上書きします。"
              "MOD を作成し直して、導入し直してください",
              "Grim Dawn has been updated, and the Quartermaster's stock (official records) has changed since the MOD "
              "was built. As it is, the game's new stock would be overwritten with the old contents. Rebuild the MOD "
              "and install it again")


# アンインストールは最初の導入の前の状態に戻す（2026-10-17 利用者の判断 C126: 意味は変えず、AI Skin の古い MOD が残ることを示す）
def _aiskin_remains_warning(files: str) -> str:
    return tr(f"最初の導入の前に mods/ にあった AI Skin の古い MOD（本ツールの記録の無いもの: {files}）に戻します。"
              "アンインストールの後も、この MOD は /basemods で読み込まれ続けます",
              f"Uninstalling returns to the old AI Skin MOD that was in mods/ before the first install ({files}; not "
              "recorded by this tool). That MOD is still loaded with /basemods after uninstalling")


# mods/ の変更の後に装備ライブラリの導入状態を更新できなかったとき（2026-10-20 R07B-02）。画面（ModViewModel の
# LibraryUpdateWarningHead）は書き出しで見分けてアプリの文に置き換える（英語の文の書き出しは
# "Could not update the install status in the equipment library"）
def _library_update_warning() -> str:
    return tr("装備ライブラリの導入状態を更新できませんでした（mods/ の変更は完了しています。"
              "装備一覧の「導入済み」の表示が古いことがあります）",
              "Could not update the install status in the equipment library (the changes to mods/ are complete. "
              "\"Installed\" in the Library may be out of date)")


def __getattr__(name: str):
    """以前の定数の名前 → 読んだときの表示言語の文（AISKIN_REMAINS_WARNING は {files} を含む書式）。"""
    if name == "CAUSE_TEXT":
        return _cause_texts()
    if name == "GAME_CHANGED_WARNING":
        return _game_changed_warning()
    if name == "AISKIN_REMAINS_WARNING":
        return _aiskin_remains_warning("{files}")
    if name == "LIBRARY_UPDATE_WARNING":
        return _library_update_warning()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# 状態ファイル
# ---------------------------------------------------------------------------
def _state_path() -> str:
    return os.path.join(paths.install_dir(), "state.json")


class InstallStateBroken(InstallError, ValueError):
    """導入の記録（state.json）が壊れている・形が違う（2026-10-27 R07B-03。以前は生の JSONDecodeError・KeyError・
    AttributeError で、アプリでは INTERNAL になった）。ValueError も継ぐので、(OSError, ValueError) を捕まえている
    呼び出し側はそのまま動く。``path``: state.json のパス。自動で空にはしない（baseline を失うため）。"""

    def __init__(self, message: str, path: str):
        super().__init__(message, reason="install_state")
        self.path = path


def _state_broken(p: str, detail: str) -> InstallStateBroken:
    # 文に「記録がありません」（英語 "no record"）を入れない（アプリはそれを NOT_FOUND に振り分ける）
    return InstallStateBroken(tr(f"導入の記録（{p}）が壊れています（{detail}）。メモ帳などで直すか、ファイルの名前を変えて"
                                 "どけてください（どけると記録なしで始められますが、アンインストールで最初の導入の前の状態に"
                                 "戻せなくなります）",
                                 f"The install record ({p}) is corrupted ({detail}). Fix it with a text editor such as "
                                 "Notepad, or rename the file to move it aside (then the app starts without the record, "
                                 "but uninstalling can no longer return to the state before the first install)"), p)


def load_state() -> dict:
    p = _state_path()
    if os.path.isfile(p):
        with open(p, encoding="utf-8-sig") as f:
            try:
                st = json.load(f)
            except ValueError as e:         # JSONDecodeError・UnicodeDecodeError
                raise _state_broken(p, str(e)) from e
        if not isinstance(st, dict):
            raise _state_broken(p, tr("JSON のオブジェクトではありません", "not a JSON object"))
        if st.get("format") == STATE_FORMAT:
            targets = st.get("targets")
            if not isinstance(targets, dict) or not all(
                    isinstance(t, dict) and isinstance(t.get("gd"), str) and isinstance(t.get("format"), str)
                    and isinstance(t.get("history", []), list) for t in targets.values()):
                raise _state_broken(p, tr("導入先（targets）の形が違います", "the targets have an unexpected form"))
            return st
    return {"format": STATE_FORMAT, "version": 1, "targets": {}}


def _save_state(st: dict) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    _retry_os(os.replace, tmp, p)       # 状態の取得が state.json を読んでいる間は置き換えられない（2026-10-20 R07B-01）


def _commit_state(st: dict, action: str, backup_id: str | None) -> None:
    """mods/ を変更した後に導入の記録を保存する。保存できなければ、mods/ は変更済みと分かる InstallFailed
    （stage state。以前は生の OSError が「導入に失敗しました」（INTERNAL）になった。2026-10-20 R07B-02）。"""
    try:
        _save_state(st)
    except OSError as e:
        cause = _os_cause(e, staged=getattr(e, "filename2", None) is not None)     # 一時名に書けた後の置き換えの失敗
        raise InstallFailed(tr(f"mods/ の変更は完了しましたが、導入の記録（{_state_path()}）を保存できませんでした: "
                               f"{_cause_text(cause)}（{e}）。ファイルを使っているプログラムを閉じてから、もう一度同じ操作を"
                               "行ってください（「変更されています」と出たら、続行してください）",
                               f"The changes to mods/ are complete, but the install record ({_state_path()}) could not "
                               f"be saved: {_cause_text(cause)} ({e}). Close any program that is using the file, then "
                               "do the same operation again (if files are reported as changed, continue)"),
                            action=action, cause=cause, rolled_back=False, backup_id=backup_id, changed=True,
                            stage="state") from e


def _target_key(gd: str, fmt: str, name: str | None) -> str:
    return os.path.normcase(os.path.abspath(gd)) + "|" + fmt + ("|" + name if name else "")


def _target(st: dict, gd: str, fmt: str, name: str | None) -> dict:
    return st["targets"].setdefault(_target_key(gd, fmt, name), {
        "gd": os.path.abspath(gd), "format": fmt, "name": name, "installed": None, "baseline": None, "history": []})


def _pick_custom_name(st: dict, gd: str) -> str:
    """Custom Game の名前を省いたとき: 導入済みの custom が 1 つならその名前。0 件・2 件以上なら InstallError。"""
    cands = sorted(t["name"] for t in st["targets"].values() if t["format"] == "custom" and t.get("installed")
                   and os.path.normcase(t["gd"]) == os.path.normcase(gd))
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise InstallError(tr("Custom Game 形式で導入した記録がありません（mods/ は変更していません）",
                              "There is no record of an install in Custom Game format (mods/ was not changed)"))
    raise InstallError(tr(f"Custom Game の名前を --name で指定してください（導入済み: {', '.join(cands)}）",
                          f"Specify the Custom Game name with --name (installed: {', '.join(cands)})"))


# ---------------------------------------------------------------------------
# 既存ファイルの判定
# ---------------------------------------------------------------------------
def _is_project_record(name: str) -> bool:
    return name.startswith(f"records/items/{NAMESPACE}") or name in MERCHANT_RECORDS or name in PROJECT_EXTRA_RECORDS


def classify_file(path: str, rel: str, installed: dict | None) -> tuple[str, str]:
    """mods/ の既存ファイルの持ち主 → (種別, 説明)。rel は対象ルートからの相対パス。"""
    if not os.path.isfile(path):
        return "absent", tr("なし", "None")
    rec = (installed or {}).get("files", {}).get(rel)
    sha = sha256_file(path)
    if rec is not None:
        if rec["sha256"] == sha:
            return "modbuilder", tr(f"このアプリが導入（ビルド {installed.get('build_id')}）",
                                    f"Installed by this app (build {installed.get('build_id')})")
        return "modified", tr("このアプリが導入した後に変更されています", "Changed after this app installed it")
    low = rel.lower()
    try:
        if low.endswith(".arz"):
            a = ArzFile(path)
            names = a.names()
            others = [n for n in names if not _is_project_record(n)]
            if names and not others and any(n.startswith(f"records/items/{NAMESPACE}") for n in names):
                return "aiskin-project", tr(f"このアプリの記録の無い AI Skin の MOD のデータベース（{len(names)} レコード、全て AI Skin 関係）",
                                            f"Database of an AI Skin MOD with no record in this app ({len(names)} record(s), all AI Skin)")
            return "foreign", tr(f"他の MOD のデータベース（{len(names)} レコード。例: {others[:3]}）",
                                 f"Database of another MOD ({len(names)} record(s), e.g. {others[:3]})")
        if low.endswith(".arc"):
            entries = list(ArcFile(path).entries)
            base = os.path.basename(low)
            if base.startswith("text_"):
                ok = entries and all(os.path.basename(e.lower()).startswith("tags_aiskin") for e in entries)
            else:
                ok = entries and all(e.lower().startswith(NAMESPACE) for e in entries)
            if ok:
                return "aiskin-project", tr(f"このアプリの記録の無い AI Skin の MOD のアーカイブ（{len(entries)} ファイル、全て AI Skin 関係）",
                                            f"Archive of an AI Skin MOD with no record in this app ({len(entries)} file(s), all AI Skin)")
            return "foreign", tr(f"他の MOD のアーカイブ（{len(entries)} ファイル。例: {entries[:3]}）",
                                 f"Archive of another MOD ({len(entries)} file(s), e.g. {entries[:3]})")
    except Exception as e:  # noqa: BLE001
        return "foreign", tr(f"読めないファイル（{e}）", f"Unreadable file ({e})")
    return "foreign", tr("このアプリの管理外のファイル", "File not managed by this app")


def _root(gd: str, fmt: str, name: str | None) -> str:
    return os.path.join(gd, "mods") if fmt == "basemods" else os.path.join(gd, "mods", name)


def _p(root: str, rel: str) -> str:
    return os.path.join(root, rel.replace("/", os.sep))


def _is_link_dir(path: str) -> bool:
    """リンク・ジャンクション（リパースポイント）か。Python 3.11 の os.walk はジャンクションをたどるので、自分で見分ける
    （storage.py の _is_reparse と同じ判定）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0)
                                            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _walk(root: str, links: list[str] | None = None):
    """os.walk(root)。ただしリンク・ジャンクションのサブフォルダーには入らない（mods/ の外のファイルを管理対象にしない・
    外の空のフォルダーを消さない。2026-10-27 R07B-02）。links を渡すと、入らなかったフォルダー（root からの相対パス）を足す。"""
    for dirpath, dirs, files in os.walk(root):
        keep = []
        for d in dirs:
            if _is_link_dir(os.path.join(dirpath, d)):
                if links is not None:
                    links.append(os.path.relpath(os.path.join(dirpath, d), root).replace("\\", "/"))
            else:
                keep.append(d)
        dirs[:] = keep
        yield dirpath, dirs, files


def _links_warning(prefix: str, links: list[str]) -> list[str]:
    return [tr(f"mods/{prefix}{rel}: リンク・ジャンクションなので、中は見ずに触りません",
               f"mods/{prefix}{rel}: This is a link or junction, so its contents are not checked or touched")
            for rel in links]


def _managed(gd: str, fmt: str, name: str | None, extra: list[str]) -> list[str]:
    """管理対象のファイル（対象ルートからの相対パス）。custom は mods/<名前>/ の全ファイル（本ツールの一時ファイルと、
    リンク・ジャンクションの中は除く。2026-10-27 R07A-03・R07B-02）。

    大文字・小文字だけが違う既存のファイルは extra の綴りにそろえる（Windows では同じファイル。別のファイルとして
    削除の対象にすると、置いたばかりのファイルを消してしまう。2026-10-17 監査 C87）。"""
    root = _root(gd, fmt, name)
    if fmt == "basemods":
        rels = list(BASEMODS_FILES)
    else:
        rels = []
        if os.path.isdir(root):
            for dirpath, _d, files in _walk(root):
                for f in files:
                    if f.lower().endswith(TMP_SUFFIX):      # 強制終了などで残った本ツールの一時ファイル（_apply が消す）
                        continue
                    rels.append(os.path.relpath(os.path.join(dirpath, f), root).replace("\\", "/"))
    for r in extra:
        same = [x for x in rels if os.path.normcase(x) == os.path.normcase(r)]
        if r in rels:
            continue
        if same:
            rels[rels.index(same[0])] = r
        else:
            rels.append(r)
    return rels


def _basemods_others(root: str) -> list[dict]:
    """mods/ 直下の、本ツールが管理しない *.arc・*.arz（他の /basemods の MOD の残りなど。/basemods で読み込まれ続ける）。
    名前と大きさだけ（中身は読まない）。フォルダー（survivalmode など）と一時ファイルは含めない（2026-10-17 監査 C85）。"""
    if not os.path.isdir(root):
        return []
    managed = {f.lower() for f in BASEMODS_FILES}
    out = []
    for e in sorted(os.listdir(root)):
        p = os.path.join(root, e)
        if e.lower() in managed or not e.lower().endswith((".arc", ".arz")) or not os.path.isfile(p):
            continue
        try:
            out.append({"name": e, "size": os.path.getsize(p)})
        except OSError:
            out.append({"name": e, "size": None})
    return out


def inspect_target(gd: str, fmt: str = "basemods", name: str | None = None, state: dict | None = None,
                   extra: list[str] | None = None) -> dict:
    st = state or load_state()
    tgt = st["targets"].get(_target_key(gd, fmt, name), {})
    root = _root(gd, fmt, name)
    out = {}
    for rel in _managed(gd, fmt, name, extra or []):
        p = _p(root, rel)
        kind, detail = classify_file(p, rel, tgt.get("installed"))
        out[rel] = {"kind": kind, "detail": detail, **({"size": os.path.getsize(p)} if os.path.isfile(p) else {})}
    others = []
    if fmt == "basemods" and os.path.isdir(root):
        managed = {f.lower() for f in BASEMODS_FILES}
        others = sorted(e for e in os.listdir(root) if e.lower() not in managed)
    links: list[str] = []           # custom の mods/<名前>/ の中のリンク・ジャンクション（たどらない。R07B-02）
    if fmt == "custom" and os.path.isdir(root):
        for _w in _walk(root, links):
            pass
    return {"root": root, "files": out, "others": others,
            "basemods_others": _basemods_others(root) if fmt == "basemods" else [], "links": sorted(links)}


def _readonly_blocking(root: str, rels, prefix: str) -> list[str]:
    """置き換え・削除するファイルのうち読み取り専用のもの（Windows では置き換えも削除もできない）。"""
    out = []
    for rel in sorted(set(rels)):
        if _is_readonly(_p(root, rel)):
            out.append(tr(f"mods/{prefix}{rel}: 読み取り専用のファイルです（エクスプローラーでファイルのプロパティを開き、"
                          "「読み取り専用」を外してから、もう一度実行してください）",
                          f"mods/{prefix}{rel}: The file is read-only (open the file's Properties in File Explorer, "
                          "clear \"Read-only\", then run again)"))
    return out


# ---------------------------------------------------------------------------
# ファイル操作の下回り
# ---------------------------------------------------------------------------
def _is_readonly(path: str) -> bool:
    try:
        return os.path.isfile(path) and not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        return False


RETRY_DELAYS = (0.05, 0.1, 0.2, 0.5, 1.0)     # 一時的に使用中のファイルの置き換え・削除をやり直す間隔（合計 約 2 秒）


def _retry_os(fn, *args):
    """fn(*args)（os.replace / os.remove）。Windows で他のプログラムが一時的に開いている（アプリの状態の取得・ウイルス
    対策ソフトの検査など）ときの PermissionError（WinError 5・32・33）だけ、少し待ってやり直す。読み取り専用の
    ファイルはやり直さない。やり直しても失敗したら最後の例外を投げる（2026-10-20 R07B-01）。"""
    for delay in RETRY_DELAYS + (None,):
        try:
            return fn(*args)
        except PermissionError as e:
            if (delay is None or getattr(e, "winerror", None) not in (5, 32, 33)
                    or any(isinstance(a, str) and _is_readonly(a) for a in args)):
                raise
            time.sleep(delay)


def _force_remove(path: str) -> bool:
    """本ツールが作ったファイル（一時ファイル）を消す。読み取り専用なら属性を外してから。消せたか（無ければ True）。"""
    try:
        if not os.path.lexists(path):
            return True
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        os.remove(path)
        return True
    except OSError:
        return False


def _os_cause(e: BaseException, *, staged: bool = False) -> str:
    """OSError → 原因（CAUSE_TEXT のキー）。

    staged: 同じフォルダーへの一時ファイルの書き込みが成功した後の置き換え・削除の失敗か。Windows で他のプログラムが
    開いているファイルへの os.replace は ERROR_ACCESS_DENIED（WinError 5）になるので、そのフォルダーに書けると
    分かっている段階では、読み取り専用でない限り「使用中」とみなす（2026-10-17 FG06-R1）。"""
    if not isinstance(e, OSError):
        return "other"
    win = getattr(e, "winerror", None)
    if win in (32, 33):                         # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        return "in_use"
    if win in (39, 112) or e.errno == errno.ENOSPC:     # ERROR_HANDLE_DISK_FULL / ERROR_DISK_FULL
        return "disk_full"
    if win == 5 or e.errno in (errno.EACCES, errno.EPERM):
        for f in (getattr(e, "filename", None), getattr(e, "filename2", None)):
            if isinstance(f, str) and _is_readonly(f):
                return "read_only"
        return "in_use" if staged else "permission"
    return "other"


def _restore_meta(dst: str, item: dict) -> None:
    """戻したファイルに元の更新日時・読み取り専用の属性を写す（退避の記録から。古い退避は退避のファイルから）。"""
    try:
        if item.get("mtime_ns") is not None:
            os.utime(dst, ns=(item["mtime_ns"], item["mtime_ns"]))
            if item.get("readonly"):
                os.chmod(dst, stat.S_IREAD)
        elif item.get("src"):
            shutil.copystat(item["src"], dst)
    except OSError:
        pass        # 中身は戻っている（属性・時刻は付随の情報）


def _put(src: str, dst: str, sha: str, item: dict | None = None) -> None:
    """src を dst に置く（一時名にコピー → ハッシュ照合 → 置き換え → 属性）。戻しに使う。一時ファイルは必ず消す。"""
    tmp = dst + TMP_SUFFIX
    try:
        _force_remove(tmp)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, tmp)
        if sha256_file(tmp) != sha:
            raise InstallError(tr(f"戻すファイルのハッシュが一致しません: {src}",
                                  f"The hash of the file to restore does not match: {src}"))
        _retry_os(os.replace, tmp, dst)
        if item:
            _restore_meta(dst, item)
    finally:
        _force_remove(tmp)


def _rollback(root: str, changed: list[str], rollback: dict, log) -> list[str]:
    """実際に変更したファイル（changed）だけを操作の前に戻す。1 件の失敗で止めずに全件を試し、戻せなかったものを返す。

    rollback[rel] は操作の前の中身（{"src", "sha256", ...}）か、None（操作の前は無かった → 消す）。"""
    failed = []
    for rel in reversed(changed):
        dst = _p(root, rel)
        how = rollback.get(rel)
        try:
            if how is None:
                if os.path.lexists(dst):
                    _retry_os(os.remove, dst)
            else:
                _put(how["src"], dst, how["sha256"], how)
            log(tr(f"戻しました: {dst}", f"Restored: {dst}"))
        except Exception as e:  # noqa: BLE001  全件を試す
            failed.append(rel)
            log(tr(f"戻せませんでした: {dst}（{e}）", f"Could not restore: {dst} ({e})"))
    return sorted(failed)


def _apply(root: str, writes: dict, removes: list[str], rollback: dict, *, action: str, backup_id: str | None,
           prefix: str, log, sweep: bool = False) -> None:
    """mods/ を変更する（2026-10-17 監査 C11）。

    writes: {rel: {"src", "sha256", ...}} 置くファイル（"mtime_ns"・"readonly"・"restore_meta" があれば置いた後に属性を写す）
    removes: [rel] 消すファイル（無ければ何もしない）
    rollback: {rel: {"src", "sha256", ...} | None} 失敗したときの戻し方（writes と removes の全てを含む）

    1. 全ての writes を mods/ の中の一時名にコピーしてハッシュを照合する（ここまでは既存のファイルを変えない）
    2. ゲームが起動していないことをもう一度確かめる
    3. 置き換え・削除（成功したものだけ changed に記録）。失敗したら changed だけを戻して InstallFailed
       （Ctrl+C などの KeyboardInterrupt でも changed を戻してから、そのまま投げ直す。2026-10-27 R07A-01）
    一時ファイルは、この実行が作ったもの（と同じ名前の古い残り）だけを、読み取り専用を外してから消す。sweep（custom）は
    対象ルートの下の本ツールの一時ファイルの残りも全て消す（2026-10-27 R07A-03）。"""
    tmps = [_p(root, rel) + TMP_SUFFIX for rel in sorted(set(writes) | set(removes) | set(rollback))]
    if sweep and os.path.isdir(root):
        seen = {os.path.normcase(t) for t in tmps}
        for dirpath, _d, files in _walk(root):
            for f in files:
                t = os.path.join(dirpath, f)
                if f.lower().endswith(TMP_SUFFIX) and os.path.normcase(t) not in seen:
                    tmps.append(t)
    for t in tmps:                      # 前回の失敗の残り（本ツールの一時ファイルの名前だけ）
        _force_remove(t)
    changed: list[str] = []
    try:
        try:
            for rel, w in writes.items():
                dst = _p(root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copyfile(w["src"], dst + TMP_SUFFIX)
                except OSError as e:
                    if os.path.normcase(str(getattr(e, "filename", "") or "")) != os.path.normcase(w["src"]):
                        raise
                    # コピー元（退避・ビルド）を読めない。mods/ への書き込みの失敗にしない（2026-10-27 R07B-05）
                    raise InstallError(tr(f"コピー元のファイルを読めませんでした（mods/ は変更していません）: {w['src']}（{e}）",
                                          f"Could not read the source file (mods/ was not changed): {w['src']} ({e})")) from e
                if sha256_file(dst + TMP_SUFFIX) != w["sha256"]:
                    raise InstallError(tr(f"コピーしたファイルのハッシュが一致しません: {dst}（コピー元: {w['src']}。"
                                          "mods/ は変更していません）",
                                          f"The hash of the copied file does not match: {dst} (source: {w['src']}; "
                                          "mods/ was not changed)"))
        except OSError as e:
            cause = _os_cause(e)
            raise InstallFailed(tr(f"mods/ にファイルを書き込めませんでした（mods/ は変更していません）: {_cause_text(cause)}（{e}）",
                                   f"Could not write files to mods/ (mods/ was not changed): "
                                   f"{_cause_text(cause)} ({e})"),
                                action=action, cause=cause, rolled_back=True, backup_id=backup_id) from e
        if steam.game_running():
            # 英語の文は "Grim Dawn is running" を含める（日本語の文の「起動中」と対。アプリの GAME_RUNNING の振り分け）。
            # 文の目印に頼らずに分けられるよう reason も付ける（起動を確かめられない注意の英語の文も目印に当たるため。
            # 2026-11-04 点検 C5_english-01・B-025）
            raise InstallError(tr("Grim Dawn が起動中です（操作の途中で起動されました）。mods/ は変更していません。"
                                  "ゲームを完全に終了してから、もう一度実行してください",
                                  "Grim Dawn is running (it was started during the operation). mods/ was not changed. "
                                  "Exit the game completely, then run again"), reason="game_running")
        try:
            for rel, w in writes.items():
                dst = _p(root, rel)
                _retry_os(os.replace, dst + TMP_SUFFIX, dst)      # 一時的な使用中はやり直す（R07B-01）
                changed.append(rel)
                if w.get("restore_meta"):
                    _restore_meta(dst, w)
                log(tr(f"配置: {dst}（sha256 一致）", f"Placed: {dst} (sha256 matches)"))
            for rel in removes:
                p = _p(root, rel)
                if os.path.lexists(p):
                    _retry_os(os.remove, p)
                    changed.append(rel)
                    log(tr(f"削除（退避済み）: {p}", f"Deleted (backed up): {p}"))
        except Exception as e:  # noqa: BLE001
            # 一時名への書き込みが成功していれば、そのフォルダーには書ける（WinError 5 は使用中とみなす）
            cause = _os_cause(e, staged=bool(writes))
            unrestored = _rollback(root, changed, rollback, log)
            what = tr(f"{_cause_text(cause)}（{e}）", f"{_cause_text(cause)} ({e})")
            if unrestored:
                files = ", ".join(f"mods/{prefix}{r}" for r in unrestored)
                msg = (tr(f"mods/ の変更に失敗し、元に戻せなかったファイルがあります: {files}。原因: {what}。",
                          f"Changing mods/ failed, and some files could not be restored: {files}. Cause: {what}.")
                       + (tr(f"操作の前のファイルは退避 {backup_id} にあります（ゲームを終了し、ファイルを使っているプログラムを"
                             f"閉じてから restore {backup_id} で戻せます）",
                             f" The files from before the operation are in backup {backup_id} (exit the game, close "
                             f"any program that is using the files, then restore them with restore {backup_id})")
                          if backup_id else ""))
            else:
                msg = tr(f"mods/ の変更に失敗したため、変更したファイルを元に戻しました。原因: {what}",
                         f"Changing mods/ failed, so the changed files were restored. Cause: {what}")
            raise InstallFailed(msg, action=action, cause=cause, rolled_back=not unrestored, unrestored=unrestored,
                                backup_id=backup_id, changed=True) from e
        except BaseException:
            # KeyboardInterrupt（CLI の Ctrl+C）・SystemExit: 途中まで置き換えた mods/ を残さない（2026-10-27 R07A-01）
            _rollback(root, changed, rollback, log)
            raise
    finally:
        for t in tmps:
            _force_remove(t)


# ---------------------------------------------------------------------------
# 退避
# ---------------------------------------------------------------------------
def _backups_root() -> str:
    return os.path.join(paths.install_dir(), "backups")


def _backup_copy(src: str, dst: str, sha: str) -> None:
    """src を退避の dst に置く。中身は backups\\_blobs\\<sha256> にハードリンクで共有し、作れなければコピーする。"""
    blob = os.path.join(_backups_root(), BLOBS, sha)
    linked = False
    try:
        if not (os.path.isfile(blob) and sha256_file(blob) == sha):
            os.makedirs(os.path.dirname(blob), exist_ok=True)
            tmp = blob + ".tmp"
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, blob)
            finally:
                _force_remove(tmp)
        os.link(blob, dst)
        linked = True
    except OSError:
        linked = False          # ハードリンクを作れないドライブ・リンク数の上限
    if not linked:
        shutil.copyfile(src, dst)
    if sha256_file(dst) != sha:
        raise InstallError(tr(f"退避したファイルのハッシュが一致しません: {dst}",
                              f"The hash of the backed-up file does not match: {dst}"))


def _new_backup(root: str, rels: list[str], reason: str, tgt: dict, action: str, extra: dict | None = None) -> dict:
    """mods/ の rels を退避する。extra は backup.json に足す項目（導入の installing。:func:`_interrupted_installs`）。"""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = _backups_root()
    bid, n = stamp, 2
    while os.path.exists(os.path.join(base, bid)):
        bid, n = f"{stamp}-{n}", n + 1
    bdir = os.path.join(base, bid)
    files, absent = {}, []
    try:
        for rel in rels:
            src = _p(root, rel)
            if not os.path.isfile(src):
                absent.append(rel)
                continue
            dst = os.path.join(bdir, "files", rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            h = sha256_file(src)
            _backup_copy(src, dst, h)
            st = os.stat(src)
            files[rel] = {"sha256": h, "size": os.path.getsize(dst), "mtime_ns": st.st_mtime_ns,
                          "readonly": not (st.st_mode & stat.S_IWRITE)}
        meta = {"id": bid, "created_at": now_iso(), "reason": reason, "gd": tgt["gd"], "format": tgt["format"],
                "name": tgt.get("name"), "root": root, "files": files, "absent": absent,
                "installed_before": tgt.get("installed"), **(extra or {})}
        os.makedirs(bdir, exist_ok=True)
        # 一時名に書いてから置く（途中で強制終了しても、途中までの backup.json を残さない。2026-10-20 R07B-03）
        tmp = os.path.join(bdir, "backup.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(bdir, "backup.json"))
    except OSError as e:
        shutil.rmtree(bdir, ignore_errors=True)        # 作りかけの退避（backup.json の無いもの）を残さない
        cause = _os_cause(e)
        raise InstallFailed(tr(f"退避を作れませんでした（mods/ は変更していません）: {_cause_text(cause)}（{e}）",
                               f"Could not create the backup (mods/ was not changed): {_cause_text(cause)} ({e})"),
                            action=action, cause=cause, rolled_back=True, stage="backup") from e
    except InstallError:
        shutil.rmtree(bdir, ignore_errors=True)
        raise
    return meta


def _load_backup(bid: str) -> tuple[dict, str]:
    bdir = os.path.join(_backups_root(), bid)
    p = os.path.join(bdir, "backup.json")
    if not os.path.isfile(p):
        removed = any(h.get("backup_id") == bid and h.get("backup_removed")
                      for t in load_state()["targets"].values() for h in t.get("history") or [])
        raise InstallError(tr(f"退避がありません: {bid}", f"No such backup: {bid}")
                           + (tr("（古い退避の整理で消去済みです）", " (it was deleted in a cleanup of old backups)")
                              if removed else ""), reason="backup_missing")
    with open(p, encoding="utf-8") as f:
        try:
            meta = json.load(f)
        except ValueError:
            meta = None
    if not isinstance(meta, dict):      # 生の JSONDecodeError（画面では INTERNAL）にしない（2026-10-20 R07B-03）
        raise InstallError(tr(f"退避の記録が壊れています: {bid}（{p}）", f"The backup record is corrupted: {bid} ({p})"),
                           reason="backup_missing")
    return meta, bdir


def _backup_item(meta: dict, bdir: str, rel: str) -> dict:
    """退避の 1 ファイル → 置き方（_put・_apply の writes / rollback の形）。"""
    rec = meta["files"][rel]
    return {"src": os.path.join(bdir, "files", rel.replace("/", os.sep)), "sha256": rec["sha256"],
            "mtime_ns": rec.get("mtime_ns"), "readonly": rec.get("readonly"), "restore_meta": True,
            "size": rec.get("size"), "backup_id": meta.get("id")}


def _check_backup_src(rel: str, item: dict, deep: bool = False) -> None:
    """mods/ に書き戻す退避のファイル（item["src"]）があり、記録と合うかを mods/ を変える前に確かめる（以前は実行の
    一時名へのコピーで初めて失敗し、mods/ への書き込みの失敗のように見えた。2026-10-27 R07B-05）。deep なら sha256 も
    比べる（無ければ大きさだけ）。合わなくても、同じ中身の共有のファイル（backups\\_blobs\\<sha256>）が残っていれば、
    item["src"] をそれに差し替える。どちらも無ければ InstallError。"""
    src = item["src"]
    try:
        ok = os.path.isfile(src) and (item.get("size") is None or os.path.getsize(src) == item["size"]) \
            and (not deep or sha256_file(src) == item["sha256"])
    except OSError:
        ok = False
    if ok:
        return
    blob = os.path.join(_backups_root(), BLOBS, item["sha256"])
    try:
        if os.path.isfile(blob) and sha256_file(blob) == item["sha256"]:
            item["src"] = blob
            return
    except OSError:
        pass
    bid = item.get("backup_id") or "?"
    raise InstallError(tr(f"退避 {bid} の {rel} が見つからないか壊れています（{src}）。mods/ は変更していません",
                          f"{rel} in backup {bid} is missing or damaged ({src}). mods/ was not changed"),
                       reason="backup_missing")


def _rollback_map(meta: dict, bdir: str, rels) -> dict:
    return {rel: (_backup_item(meta, bdir, rel) if rel in meta["files"] else None) for rel in rels}


def list_backups() -> list[dict]:
    """退避の一覧。名前が _ で始まるフォルダー（_blobs・消している途中の _del-*）は含めない。記録（backup.json）が
    読めない退避は broken: True で返す（1 件のために一覧・容量・整理の全てが失敗しないように。保護されていなければ
    整理で消せる。2026-10-20 R07B-03）。"""
    base = _backups_root()
    out = []
    if os.path.isdir(base):
        for b in sorted(os.listdir(base)):
            p = os.path.join(base, b, "backup.json")
            if b.startswith("_") or not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                m = None
            if not isinstance(m, dict):
                out.append({"id": b, "created_at": None, "reason": None, "gd": None, "format": None, "name": None,
                            "files": [], "absent": [], "broken": True})
                continue
            out.append({k: m.get(k) for k in ("id", "created_at", "reason", "gd", "format", "name")}
                       | {"files": sorted(m.get("files", {})), "absent": m.get("absent", [])})
    return out


def previous_install_backup(tgt: dict) -> dict | None:
    """今の導入の「前の状態」の退避（アプリの「前の状態に戻す」の戻り先）→ {"backup_id", "meta", "chain"}。無ければ None。

    今の build_id の install の履歴を新しい順に見て、その退避の installed_before の build_id が今と違う最初のもの（None も
    「違う」に含める）。同じなら遡る（同じビルド・同じ受け取った MOD を 2 回導入した場合。2026-10-17 監査 C12）。途中の
    退避が読めなければ、古い時代の退避に飛ばないようにそこで止めて None。chain は見た退避（整理で消さないもの）。"""
    inst = tgt.get("installed") or {}
    bid = inst.get("build_id")
    if not bid:
        return None
    chain = []
    for h in reversed(tgt.get("history") or []):
        if h.get("action") != "install" or h.get("build_id") != bid or not h.get("backup_id"):
            continue
        try:
            meta, _ = _load_backup(h["backup_id"])
        except Exception:  # noqa: BLE001  退避が消されている
            return None
        chain.append(h["backup_id"])
        if (meta.get("installed_before") or {}).get("build_id") != bid:
            return {"backup_id": h["backup_id"], "meta": meta, "chain": chain}
    return None


def _interrupted_installs(tgt: dict) -> list[tuple[dict, str]]:
    """中断した導入の退避（新しい順の [(meta, bdir)]。2026-10-27 R07A-01・R07B-01・X6X-03）。

    導入は mods/ を変える前に退避の記録（backup.json。installing に置くファイルの sha256 を持つ）を書き、成功の後に
    state.json の履歴に加える。強制終了・電源断・記録の保存の失敗で履歴に加わらなかった導入の退避を、同じ導入先・
    この導入先の最後の履歴より後・今の導入の記録（installed）の上で作られたもの、として見つける（以前の版の退避は
    installing が無いので対象外）。"""
    hist = tgt.get("history") or []
    last = max((str(h.get("at") or "") for h in hist), default="")
    used = {h.get("backup_id") for h in hist}
    before = (tgt.get("installed") or {}).get("build_id")
    out = []
    for b in list_backups():
        if (b.get("broken") or b.get("reason") != "install" or b["id"] in used or str(b.get("created_at") or "") <= last
                or b.get("format") != tgt.get("format") or b.get("name") != tgt.get("name")
                or os.path.normcase(str(b.get("gd") or "")) != os.path.normcase(str(tgt.get("gd") or ""))):
            continue
        try:
            meta, bdir = _load_backup(b["id"])
        except InstallError:
            continue
        ins = meta.get("installing")
        if (not isinstance(ins, dict) or not isinstance(ins.get("files"), dict)
                or not isinstance(meta.get("files"), dict) or not isinstance(meta.get("absent"), list)
                or (meta.get("installed_before") or {}).get("build_id") != before):
            continue
        out.append((meta, bdir))
    out.sort(key=lambda mb: (str(mb[0].get("created_at") or ""), mb[0]["id"]), reverse=True)
    return out


def _interrupted_residue(root: str, tgt: dict, rels) -> dict[str, tuple[dict, str]]:
    """mods/ のファイルのうち中断した導入が置いたもの（sha256 が installing と一致）→ その導入の前の中身を持つ退避
    (meta, bdir)。新しい退避から順に、その時点の中身が中断した導入の残りでないものを選ぶ（何度も中断した場合）。
    導入の前の中身が分からないもの（どの退避の中身も本ツールの残り）は含めない（今までどおり判定する）。"""
    cands = _interrupted_installs(tgt)
    if not cands:
        return {}
    out = {}
    for rel in rels:
        p = _p(root, rel)
        if not os.path.isfile(p):
            continue
        ours = {m["installing"]["files"].get(rel) for m, _b in cands} - {None}
        if not ours or sha256_file(p) not in ours:
            continue
        for meta, bdir in cands:
            if rel in meta["files"]:
                if (meta["files"][rel] or {}).get("sha256") in ours:
                    continue
                out[rel] = (meta, bdir)
                break
            if rel in meta["absent"]:
                out[rel] = (meta, bdir)
                break
    return out


def _interrupted_warning(prefix: str, residue: dict) -> list[str]:
    if not residue:
        return []
    ids = ", ".join(sorted({m["id"] for m, _b in residue.values()}))
    files = ", ".join(f"mods/{prefix}{r}" for r in sorted(residue))
    return [tr(f"前回の導入が中断されていました（退避 {ids}）。そのときに置いたファイル（{files}）はこのアプリのものとして"
               "扱い、中断の前の状態はその退避から取ります",
               f"The previous install was interrupted (backup {ids}). The files it placed ({files}) are treated as this "
               "app's files, and the state from before the interruption is taken from that backup")]


def protected_backups(st: dict | None = None) -> dict[str, str]:
    """消してはいけない退避 → 理由（baseline: アンインストールが使う / previous: 「前の状態に戻す」が使う /
    last_install: 今の導入をしたときの退避。中断した導入の退避（:func:`_interrupted_installs`。やり直しの導入が
    導入の前の状態に使う）も last_install として残す）。"""
    st = st or load_state()
    out: dict[str, str] = {}
    for t in st["targets"].values():
        base = t.get("baseline") or {}
        if base.get("backup_id"):
            out[base["backup_id"]] = "baseline"
        for rec in (base.get("files") or {}).values():
            if isinstance(rec, dict) and rec.get("backup_id"):
                out.setdefault(rec["backup_id"], "baseline")
        inst = t.get("installed")
        if not inst:
            continue
        prev = previous_install_backup(t)
        for b in (prev or {}).get("chain", []):
            out.setdefault(b, "previous")
        for h in reversed(t.get("history") or []):
            if h.get("action") == "install" and h.get("build_id") == inst.get("build_id") and h.get("backup_id"):
                out.setdefault(h["backup_id"], "last_install")
                break
    # 中断した導入の退避。記録の無い導入先（最初の導入が中断した）も退避の記録から見る
    targets = dict(st["targets"])
    for b in list_backups():
        if b.get("reason") == "install" and isinstance(b.get("gd"), str) and isinstance(b.get("format"), str):
            targets.setdefault(_target_key(b["gd"], b["format"], b.get("name")),
                               {"gd": b["gd"], "format": b["format"], "name": b.get("name"), "installed": None,
                                "history": []})
    for t in targets.values():
        for meta, _b in _interrupted_installs(t):
            out.setdefault(meta["id"], "last_install")
    return out


def backups_usage() -> dict:
    """退避の件数と、ディスク上の大きさ（ハードリンクで共有している中身は 1 回だけ数える）。"""
    base = _backups_root()
    seen, total = set(), 0
    if os.path.isdir(base):
        for dirpath, _d, files in os.walk(base):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    s = os.stat(p)
                except OSError:
                    continue
                key = (s.st_dev, s.st_ino) if s.st_ino else p
                if key not in seen:
                    seen.add(key)
                    total += s.st_size
    return {"count": len(list_backups()), "bytes": total}


def prune_backups(keep: int = 5, yes: bool = False, log=print, only=None) -> dict:
    """古い退避を消す（利用者が操作したときだけ。自動では消さない）。:func:`protected_backups` と、新しい keep 件は残す。

    消した退避を指す履歴には ``backup_removed`` を付ける（restore でその ID を指定すると、消去済みと分かるエラーにする）。
    どの退避からも使われなくなった共有の中身（backups\\_blobs）も消す。only（ID の集まり）を渡すと、消せるもののうち
    その中にあるものだけを消す（画面で確認した一覧の外は、確認の後に状態が変わっても消さない。2026-10-17 機能 SF03）。"""
    keep = max(0, int(keep))            # 負の値は 0 と同じ（アプリの modops.prune_backups と揃える。点検 X3X-03）
    st = load_state()
    items = sorted(list_backups(), key=lambda b: (b.get("created_at") or "", b["id"]))
    prot = protected_backups(st)
    recent = {b["id"] for b in items[-keep:]} if keep > 0 else set()
    remove = [b["id"] for b in items if b["id"] not in prot and b["id"] not in recent
              and (only is None or b["id"] in only)]
    plan = {"action": "prune", "keep": keep, "count": len(items), "remove": remove, "protected": prot,
            "usage": backups_usage(), "executed": False}
    if not yes:
        return plan
    base = _backups_root()
    _remove_trash(base, log)            # 前回消しきれなかった _del-*
    removed, failed = [], []
    for bid in remove:
        # 先に _del-<ID> へ名前を変えてから消す（中のファイルが使用中なら名前の変更が失敗し、何も消えない。以前は
        # backup.json だけが先に消え、一覧にも整理の対象にも出ない files\ が残った。2026-10-20 R07B-04）
        trash, n = os.path.join(base, f"_del-{bid}"), 2
        while os.path.exists(trash):
            trash, n = os.path.join(base, f"_del-{bid}-{n}"), n + 1
        try:
            os.rename(os.path.join(base, bid), trash)
        except OSError as e:
            failed.append(bid)
            log(tr(f"退避を消せませんでした: {bid}（{e}）", f"Could not delete backup: {bid} ({e})"))
            continue
        removed.append(bid)
        log(tr(f"退避を消しました: {bid}", f"Deleted backup: {bid}"))
        _remove_trash(base, log, [trash])
    if removed:
        at = now_iso()
        for t in st["targets"].values():
            for h in t.get("history") or []:
                if h.get("backup_id") in removed:
                    h["backup_removed"] = at
        _save_state(st)
    _gc_blobs(base)
    plan.update({"executed": True, "removed": removed, "failed": failed, "usage": backups_usage()})
    return plan


def _remove_trash(base: str, log, dirs: list[str] | None = None) -> None:
    """消す退避（_del-*。dirs を省くと backups\\ の中の全て）を消す。消しきれなければログだけ（次の整理でやり直す）。"""
    if dirs is None:
        dirs = [os.path.join(base, d) for d in (os.listdir(base) if os.path.isdir(base) else []) if d.startswith("_del-")]
    for d in dirs:
        try:
            shutil.rmtree(d)
        except OSError as e:
            log(tr(f"消している途中の退避を消しきれませんでした（次の整理でやり直します）: {os.path.basename(d)}（{e}）",
                   f"Could not finish deleting a backup (will retry at the next cleanup): {os.path.basename(d)} ({e})"))


def _gc_blobs(base: str) -> None:
    """どの退避の記録（backup.json）からも使われなくなった共有の中身（backups\\_blobs\\<sha256>）を消す。
    読めない記録が 1 つでもあれば何も消さない（どの中身を使っているか分からないため）。"""
    bl = os.path.join(base, BLOBS)
    if not os.path.isdir(bl):
        return
    used = set()
    for b in os.listdir(base):
        p = os.path.join(base, b, "backup.json")
        if b.startswith("_") or not os.path.isfile(p):      # _del-*（消している途中の退避）は使っていない
            continue
        try:
            with open(p, encoding="utf-8") as f:
                used |= {r.get("sha256") for r in json.load(f).get("files", {}).values()}
        except (OSError, ValueError, AttributeError):
            return
    for h in os.listdir(bl):
        if h not in used:
            _force_remove(os.path.join(bl, h))


# ---------------------------------------------------------------------------
# ゲームの更新の検出（2026-10-17 監査 C84）
# ---------------------------------------------------------------------------
def _open_game(gd: str):
    from .gamedata import GameData
    return GameData(gd)


def official_changes(arz_path: str, game) -> list[str]:
    """MOD の database.arz のうち、公式レコードを同じ名前で上書きするもの（補給官の 4 ティア）を、今のゲームの最優先版と
    比べる。違うレコードの名前の一覧（MOD に無いものは比べない）。

    marketStaticItems は、その .arz 自身に入っているレコード（MOD の消耗品）を除いて比べ、他の全てのフィールドは
    そのまま比べる。manifest の記録ではなく MOD の中身で比べるので、古いビルドと受け取った MOD にも使える。"""
    a = ArzFile(arz_path)
    own = {norm_path(n) for n in a.names()}
    out = []
    for rel in MERCHANT_RECORDS:
        got = a.record_fields(rel)
        if got is None:
            continue
        off = game.official_record(rel)
        if off is None:
            out.append(rel)
            continue
        mod, cur = dict(got), dict(off[1])
        mod_items = [norm_path(x) for x in split_array(mod.pop("marketStaticItems", "")) if norm_path(x) not in own]
        cur_items = [norm_path(x) for x in split_array(cur.pop("marketStaticItems", ""))]
        if mod_items != cur_items or mod != cur:
            out.append(rel)
    return out


def _game_changes(gd: str, arz_path: str | None, game=None) -> list[str]:
    """official_changes。ゲームデータや .arz が読めなければ空（判定できない。導入は止めない）。"""
    if not arz_path or not os.path.isfile(arz_path):
        return []
    try:
        return official_changes(arz_path, game if game is not None else _open_game(gd))
    except Exception:  # noqa: BLE001  テスト用の導入先（偽のゲーム）など
        return []


# ---------------------------------------------------------------------------
# 導入
# ---------------------------------------------------------------------------
def _check_common(gd: str) -> list[str]:
    """導入先とゲームの起動を確かめる。起動中なら InstallError。判定できなければ注意の文を返す。"""
    if not looks_like_install_target(gd):
        raise InstallError(tr(f"Grim Dawn のインストール先ではありません（database\\database.arz と ArchiveTool.exe が必要）: {gd}",
                              f"Not a Grim Dawn install folder (database\\database.arz and ArchiveTool.exe are "
                              f"required): {gd}"))
    running = steam.game_running()
    if running:
        # 英語の文は "Grim Dawn is running" を含める（日本語の文の「起動中」と対。アプリの GAME_RUNNING の振り分け）。
        # reason game_running でも分けられる（下の「確かめられない」注意の英語の文も "Grim Dawn is running" を含むので、
        # 文の目印より reason を使う。2026-11-04 点検 C5_english-01・B-025）
        raise InstallError(tr("Grim Dawn が起動中です。ゲームを完全に終了してから実行してください"
                              "（起動中は mods/ の .arc がロックされ、database.arz は次回起動まで反映されない）",
                              "Grim Dawn is running. Exit the game completely before running this (while the game "
                              "is running, the .arc files in mods/ are locked, and database.arz does not take effect "
                              "until the next start)"), reason="game_running")
    if running is None:
        # 画面（ModViewModel の ShownWarnings）はこの注意を「起動しているか確かめられません」（英語の文は
        # "Could not check whether Grim Dawn is running"）で見分けて除き、アプリの文を出す
        return [tr("Grim Dawn が起動しているか確かめられませんでした。ゲームを完全に終了していることを確認してから実行してください",
                   "Could not check whether Grim Dawn is running. Make sure the game has exited completely before "
                   "running this")]
    return []


def _build_files(build_dir: str, fmt: str) -> tuple[dict, dict]:
    m = load_manifest(build_dir)
    if m.get("status") != "ok":
        raise InstallError(tr(f"ビルドが成功していません（status {m.get('status')}）: {build_dir}",
                              f"The build did not succeed (status {m.get('status')}): {build_dir}"))
    files = m.get("files", {}).get(fmt)
    if not files:
        raise InstallError(tr(f"ビルドに {fmt} 形式の出力がありません（custom は build --custom-game <名前> で作る）",
                              f"The build has no output in {fmt} format (custom is made with "
                              f"build --custom-game <name>)"))
    out = {}
    for rel, rec in files.items():
        src = os.path.join(build_dir, rec["path"].replace("/", os.sep))
        if not os.path.isfile(src):
            raise InstallError(tr(f"ビルドのファイルがありません: {src}", f"A build file is missing: {src}"),
                               reason="build_files")
        if sha256_file(src) != rec["sha256"]:
            raise InstallError(tr(f"ビルドのファイルが記録と一致しません（ビルド後に変更された）: {src}",
                                  f"A build file does not match the record (changed after the build): {src}"),
                               reason="build_files")
        out[rel] = {"src": src, "sha256": rec["sha256"], "size": rec["size"]}
    return m, out


def _has_files(root: str) -> bool:
    """root の下にファイルがあるか（本ツールの一時ファイルの残りだけなら無い扱い。中断した最初の導入の残り。
    リンク・ジャンクションの中は見ない。2026-10-27 R07A-03・R07B-02）。"""
    return os.path.isdir(root) and any(not f.lower().endswith(TMP_SUFFIX)
                                       for _dp, _d, files in _walk(root) for f in files)


def _blocked(msg: str, plan: dict) -> InstallError:
    e = InstallError(msg)
    e.plan = plan           # 止まった計画（競合の一覧など。modops が画面に出す）
    return e


def install(gd: str, build_dir: str, fmt: str = "basemods", yes: bool = False, force: bool = False,
            replace_foreign: bool = False, library: Library | None = None, log=print, game=None) -> dict:
    """ビルドを mods/ に導入する。yes=False なら計画だけを返す。game は今のゲームデータ（省くと gd から読む。ゲームの
    更新の検出に使う）。"""
    gd = os.path.abspath(gd)
    build_dir = os.path.abspath(build_dir)
    manifest, new = _build_files(build_dir, fmt)
    name = manifest.get("custom_game") if fmt == "custom" else None
    if fmt == "custom" and not name:
        raise InstallError(tr("Custom Game の名前がビルドの記録にありません", "The build record has no Custom Game name"))
    # 手で書き換えた記録の名前（Windows の予約名 nul・末尾の改行など）で、予約名・不正な名前のフォルダーに書かない
    # （ビルドと同じ規則。2026-10-27 点検 R06A-04）
    if fmt == "custom" and (not isinstance(name, str) or not CUSTOM_NAME_RE.match(name)
                            or name.lower() in WINDOWS_RESERVED_NAMES):
        raise InstallError(tr(f"ビルドの記録の Custom Game の名前が不正です: {name!r}（mods/ は変更していません。"
                              "build --custom-game で作り直してください）",
                              f"The Custom Game name in the build record is invalid: {name!r} (mods/ was not changed. "
                              "Build again with build --custom-game)"))
    if fmt == "custom" and name.lower() in RESERVED_CUSTOM_NAMES:
        raise InstallError(tr(f"Custom Game の名前 {name} は公式のフォルダー（Crucible）の名前なので使えません"
                              "（mods/ は変更していません。build --custom-game で別の名前を使ってください）",
                              f"The Custom Game name {name} cannot be used because it is the name of an official "
                              "folder (Crucible) (mods/ was not changed. Use another name with build --custom-game)"))
    pre_warnings = _check_common(gd)
    st = load_state()
    tgt = _target(st, gd, fmt, name)
    root = _root(gd, fmt, name)
    prefix = "" if fmt == "basemods" else name + "/"
    # 中断した導入（強制終了・電源断・記録の保存の失敗）の残り → その導入の前の中身を持つ退避。本ツールのファイルとして
    # 扱い、導入前の状態（baseline）はその退避から取る（記録の無い AI Skin の MOD として baseline に入れない。
    # 2026-10-27 R07A-01・R07B-01・X6X-03）
    residue = _interrupted_residue(root, tgt, _managed(gd, fmt, name, list(new)))
    if fmt == "custom" and tgt.get("installed") is None and tgt.get("baseline") is None and _has_files(root) \
            and not set(_managed(gd, fmt, name, [])) <= set(residue):
        # 公式の mods/survivalmode や他の Custom Game の MOD を消さない（--replace-foreign でも。2026-10-17 監査 C87）
        raise InstallError(tr(f"mods/{name}/ は本ツールで導入した記録の無いフォルダーです（公式のフォルダーか他の MOD）。"
                              "--replace-foreign でも置き換えません（mods/ は変更していません）。"
                              "build --custom-game で別の名前を使ってください",
                              f"mods/{name}/ is a folder that this tool did not install (an official folder or "
                              "another MOD). It is not replaced even with --replace-foreign (mods/ was not changed). "
                              "Use another name with build --custom-game"))
    info = inspect_target(gd, fmt, name, st, list(new))
    for rel, (meta, _b) in residue.items():
        if rel in info["files"]:
            info["files"][rel].update(kind="modbuilder",
                                      detail=tr(f"本ツールの中断した導入が置いたファイル（退避 {meta['id']}）",
                                                f"Placed by an interrupted install of this tool (backup {meta['id']})"))
    # 中断した導入だけが置いたファイル（今回のビルドに無い）は、消さずに中断の前の中身に戻す
    restore_pre = {rel: _backup_item(meta, bdir, rel) for rel, (meta, bdir) in residue.items()
                   if rel not in new and rel in meta["files"]}
    blocking, warnings, backup_rels, remove = [], list(pre_warnings), [], []
    warnings += _interrupted_warning(prefix, residue) + _links_warning(prefix, info["links"])
    conflicts, kept = [], []
    for rel, f in info["files"].items():
        kind = f["kind"]
        if kind == "absent":
            continue
        if kind == "foreign":
            if rel in new or fmt == "custom":
                conflicts.append({"file": rel, "kind": kind, "detail": f["detail"]})
                # 画面（ModViewModel）は --replace-foreign・--force・「（触らない）」（英語は "(not touched)"）を含む注意を除く
                (warnings if replace_foreign else blocking).append(
                    f"mods/{prefix}{rel}: {f['detail']}"
                    + (tr("（--replace-foreign により退避して置き換える）", " (backed up and replaced because of --replace-foreign)")
                       if replace_foreign else ""))
                if replace_foreign:
                    backup_rels.append(rel)
                    if rel not in new:
                        remove.append(rel)
            else:
                kept.append({"file": rel, "kind": kind, "detail": f["detail"]})
                warnings.append(tr(f"mods/{rel}: {f['detail']}（触らない）", f"mods/{rel}: {f['detail']} (not touched)"))
            continue
        if kind == "modified":
            conflicts.append({"file": rel, "kind": kind, "detail": f["detail"]})
            if not force:
                blocking.append(tr(f"mods/{prefix}{rel}: {f['detail']}（--force で退避して置き換える）",
                                   f"mods/{prefix}{rel}: {f['detail']} (back up and replace with --force)"))
                continue
        backup_rels.append(rel)
        if rel not in new and rel not in restore_pre:
            remove.append(rel)      # 前回の導入にあって今回のビルドに無いファイル（例: 以前の版のビルドを導入し直したときの Text_XX.arc）
    blocking += _readonly_blocking(root, [r for r in set(new) | set(remove) | set(restore_pre)
                                          if os.path.isfile(_p(root, r))], prefix)
    for rel, item in restore_pre.items():
        try:
            _check_backup_src(rel, item)
        except InstallError as e:
            blocking.append(str(e))
    plan = {"action": "install", "gd": gd, "format": fmt, "name": name, "root": root, "build_id": manifest["build_id"],
            "build_dir": build_dir, "copy": {rel: v["sha256"] for rel, v in new.items()}, "backup": backup_rels,
            "remove": remove, "existing": info["files"], "others": info["others"],
            "basemods_others": info["basemods_others"], "conflicts": conflicts, "kept_foreign": kept,
            "blocking": blocking, "warnings": warnings, "executed": False,
            "interrupted": {rel: meta["id"] for rel, (meta, _b) in residue.items()}, "links": info["links"]}
    db = new.get("database.arz") if fmt == "basemods" else new.get(f"database/{name}.arz")
    plan["game_changed"] = _game_changes(gd, db["src"] if db else None, game)
    if plan["game_changed"]:
        plan["warnings"].append(_game_changed_warning())
    if fmt == "basemods":
        plan["guide"] = steam.basemods_guide(steam.launch_options())
        lang = steam.game_language()
        plan["game_language"] = lang
        if lang and lang.lower() != "english":
            built = [str(x).upper() for x in manifest.get("languages") or ["EN"]]
            old = ("" if set(LANGUAGES) <= set(built) else
                   tr(f"（このビルドは以前の版で作ったもので、Text_XX.arc は {', '.join(built)} だけです。作り直すと 13 言語分が入ります）",
                      f" (this build was made with an earlier version and has Text_XX.arc only for {', '.join(built)}; "
                      f"rebuilding adds all 13 languages)"))
            # 日本語の設定のゲームは mods/Text_JA.arc を読む（2026-11-18 ゲーム内で確認。IG-03）。英語と日本語以外は未確認
            if lang.lower() != "japanese":
                plan["warnings"].append(
                    tr(f"ゲームの言語が {lang} です。英語と日本語以外の設定で、mods/Text_XX.arc（装備名・説明）が読まれるかは"
                       f"未確認です",
                       f"The game language is {lang}. Whether mods/Text_XX.arc (equipment names and descriptions) is read "
                       f"with languages other than English and Japanese has not been confirmed") + old)
            elif "JA" not in built:
                # Text_JA.arc の無い以前の版のビルドは、日本語の設定のゲームが読むファイルが無い（作り直すよう添える）
                plan["warnings"].append(tr(f"ゲームの言語が {lang} です", f"The game language is {lang}") + old)
    if blocking:
        listed = "\n  - ".join(blocking)
        msg = tr(f"導入を中止しました（mods/ は変更していません）:\n  - {listed}",
                 f"Install stopped (mods/ was not changed):\n  - {listed}")
        if any("database.arz" in b for b in blocking):
            msg += tr("\nmods/database.arz は 1 つしか読まれないため、他のメインキャンペーン用 MOD とは同時に使えません。",
                      "\nOnly one mods/database.arz is read, so this cannot be used together with other main-campaign "
                      "MODs.")
        raise _blocked(msg, plan)
    if not yes:
        return plan

    # --- 実行 ---
    first = tgt.get("installed") is None and tgt.get("baseline") is None
    # 導入で作るフォルダー（アンインストールで空なら消す。もともとあったものは消さない。2026-10-17 監査 C91）
    created = [d for d in (["mods"] + ([f"mods/{name}"] if fmt == "custom" else []))
               if not os.path.isdir(os.path.join(gd, d.replace("/", os.sep)))]
    try:
        # 配置するファイルは毎回退避の対象に入れる（無かったファイルは absent に残り、restore でこの退避に戻すと消える。
        # 例: Text_JA.arc を含む MOD を、含まない MOD の上に導入して戻す。2026-10-16 の MOD の共有で追加）
        # installing: 置くファイルの sha256 と、導入の前の種別。mods/ を変える前に書くので、中断した導入をやり直しで
        # 見分けられる（:func:`_interrupted_installs`。2026-10-27 R07A-01・R07B-01・X6X-03）
        installing = {"build_id": manifest["build_id"], "files": {rel: v["sha256"] for rel, v in new.items()},
                      "kinds": {rel: f["kind"] for rel, f in info["files"].items()}, "created_dirs": created}
        backup = _new_backup(root, sorted(set(backup_rels) | set(new) | set(restore_pre)), "install", tgt, "install",
                             {"installing": installing})
        log(tr(f"退避: {len(backup['files'])} ファイル → backups/{backup['id']}",
               f"Backup: {len(backup['files'])} file(s) → backups/{backup['id']}"))
        bdir = os.path.join(_backups_root(), backup["id"])
        _apply(root, {**{rel: {"src": v["src"], "sha256": v["sha256"]} for rel, v in new.items()}, **restore_pre}, remove,
               _rollback_map(backup, bdir, set(new) | set(remove) | set(restore_pre)), action="install",
               backup_id=backup["id"], prefix=prefix, log=log, sweep=fmt == "custom")
    except InstallError:
        if first:
            _remove_created_dirs(gd, created)       # 失敗した最初の導入で作ったフォルダー（空のときだけ）
        raise
    prev_files = set((tgt.get("installed") or {}).get("files") or {})
    prev_lib = (tgt.get("installed") or {}).get("library")     # 置き換える前の導入のライブラリ（R08A-04）
    if first or tgt.get("baseline") is None:
        # 導入の記録があるのに baseline が無い（古い state.json など）ときは、前の導入のファイル（本ツールのもの）を
        # 導入前の状態に入れない（アンインストールで本ツールの古い MOD を書き戻さない。2026-10-20 R07A-01）
        own = set() if first else {r for r in backup["files"] if r in prev_files
                                   and info["files"].get(r, {}).get("kind") in ("modbuilder", "modified")}
        tgt["baseline"] = {"backup_id": backup["id"],
                           "files": {r: dict(v, backup_id=backup["id"]) for r, v in backup["files"].items() if r not in own},
                           "absent": backup["absent"] + sorted(own),
                           "kinds": {r: ("absent" if r in own else info["files"].get(r, {}).get("kind", "absent"))
                                     for r in set(backup["files"]) | set(backup["absent"])},
                           "created_dirs": created, "at": now_iso()}
        base = tgt["baseline"]
        for rel, (meta, _b) in residue.items():       # 中断した導入の残りは、中断の前の中身を導入前の状態にする
            if rel in own:
                continue
            ins = meta["installing"]
            if rel in meta["files"]:
                base["files"][rel] = dict(meta["files"][rel], backup_id=meta["id"])
                if rel in base["absent"]:
                    base["absent"].remove(rel)
                kind = (ins.get("kinds") or {}).get(rel)
                if kind in ("foreign", "aiskin-project"):
                    base["kinds"][rel] = kind
                else:
                    base["kinds"].pop(rel, None)        # baseline_kinds が退避のファイルから判定する
            else:
                base["files"].pop(rel, None)
                if rel not in base["absent"]:
                    base["absent"].append(rel)
                base["kinds"][rel] = "absent"
            for d in ins.get("created_dirs") or []:
                if d not in base["created_dirs"]:
                    base["created_dirs"].append(d)
    else:
        # 最初の導入では触らなかった他の MOD のファイルを今回初めて退避して置き換えたら、それも導入前の状態に加える
        # （アンインストールで戻す。退避はファイルごとに backup_id で指す。2026-10-17 監査 C86）
        base = tgt["baseline"]
        for rel, rec in backup["files"].items():
            kind = info["files"].get(rel, {}).get("kind")
            if rel in (base.get("files") or {}) or rel in prev_files or kind not in ("foreign", "aiskin-project"):
                continue
            base.setdefault("files", {})[rel] = dict(rec, backup_id=backup["id"])
            if rel in (base.get("absent") or []):
                base["absent"].remove(rel)
            base.setdefault("kinds", {})[rel] = kind
        # 中断した導入が初めて置き換えた他の MOD のファイルも同じ（中断の前の中身は、中断した導入の退避にある）
        for rel, (meta, _b) in residue.items():
            kind = (meta["installing"].get("kinds") or {}).get(rel)
            if rel in (base.get("files") or {}) or rel in prev_files or rel not in meta["files"] \
                    or kind not in ("foreign", "aiskin-project"):
                continue
            base.setdefault("files", {})[rel] = dict(meta["files"][rel], backup_id=meta["id"])
            if rel in (base.get("absent") or []):
                base["absent"].remove(rel)
            base.setdefault("kinds", {})[rel] = kind
    tgt["installed"] = {"build_id": manifest["build_id"], "build_dir": build_dir, "at": now_iso(),
                        "files": {rel: {"sha256": v["sha256"], "size": v["size"]} for rel, v in new.items()},
                        "equipment": [e["id"] for e in manifest["library"]["equipment"]],
                        "library": manifest["library"]["root"]}
    tgt["history"].append({"at": now_iso(), "action": "install", "build_id": manifest["build_id"], "backup_id": backup["id"]})
    _commit_state(st, "install", backup["id"])
    _update_library(library, manifest["library"]["root"], tgt["installed"]["equipment"], manifest["build_id"], plan, log,
                    also=(prev_lib,))
    plan.update({"executed": True, "backup_id": backup["id"]})
    return plan


def _update_library(library: Library | None, root: str, ids: list[str], build_id: str | None,
                    plan: dict | None = None, log=print, also=()) -> None:
    """装備ライブラリの導入状態を更新する。mods/ の変更の後なので、失敗しても例外にせず注意にする（以前は
    「導入に失敗しました」（INTERNAL）になり、mods/ と表示が食い違った。2026-10-20 R07B-02）。

    渡されたライブラリ（library）と、ビルド・導入の記録のライブラリ（root。空文字は既定のライブラリ）の両方を更新する
    （CLI は --library が無くても既定のライブラリを渡すので、開発用のライブラリのビルドを導入・アンインストールしても
    開発用のライブラリの状態が変わらなかった。2026-10-20 R08A-04）。also は前の導入の記録のライブラリなど、導入なしに
    するライブラリ（library・root と同じなら、そちらの更新だけ）。同じライブラリは 1 回だけ。開けないライブラリは飛ばす。"""
    libs, seen = [], set()
    if library is not None:
        libs.append((library, False))
        seen.add(os.path.normcase(os.path.abspath(library.root)))
    for r, off in ((root, False), *((a, True) for a in also)):
        if r is None:
            continue
        key = os.path.normcase(os.path.abspath(r or paths.library_dir()))
        if key in seen:
            continue
        seen.add(key)
        try:
            libs.append((Library.open(r), off))
        except Exception:  # noqa: BLE001  ライブラリが無くても導入自体は成立している
            continue
    for lib, off in libs:
        try:
            known = set(lib.ids())
            if build_id and not off:
                lib.mark_installed([i for i in ids if i in known], build_id)
            else:
                lib.mark_uninstalled()
        except Exception as e:  # noqa: BLE001  装備の情報が壊れている・equipment.json が使用中など
            warning = _library_update_warning()
            log(f"{warning}: {e}")
            if plan is not None and warning not in plan["warnings"]:
                plan["warnings"].append(warning)


def reinstall(gd: str, build_dir: str | None = None, fmt: str = "basemods", name: str | None = None, yes: bool = False,
              force: bool = False, library: Library | None = None, log=print) -> dict:
    """導入済みのビルド（または指定のビルド）をもう一度導入する（ファイルの修復・更新）。"""
    st = load_state()
    if build_dir is None:
        if fmt == "custom" and not name:
            name = _pick_custom_name(st, os.path.abspath(gd))
        tgt = st["targets"].get(_target_key(os.path.abspath(gd), fmt, name))
        if not tgt or not tgt.get("installed"):
            # 英語の文は "no record" を含める（日本語の文の「記録がありません」と対。アプリの NOT_FOUND の振り分け）
            raise InstallError(tr("導入の記録がありません（install でビルドを指定する）",
                                  "There is no record of an install (specify a build with install)"))
        build_dir = tgt["installed"]["build_dir"]
    plan = install(gd, build_dir, fmt, yes=yes, force=force, library=library, log=log)
    plan["action"] = "reinstall"
    return plan


def _baseline_writes(base: dict) -> dict:
    """導入前の状態（baseline）の各ファイル → 置き方。ファイルごとの退避（backup_id）から。古い state.json は
    baseline の backup_id を既定にする。退避が読めなければ InstallError（mods/ を変える前に止める）。"""
    out, cache = {}, {}
    for rel, rec in (base.get("files") or {}).items():
        bid = (rec or {}).get("backup_id") or base.get("backup_id")
        if not bid:
            raise InstallError(tr(f"導入前のファイルの退避の記録がありません: {rel}",
                                  f"There is no record of a backup of the file from before the install: {rel}"),
                               reason="backup_missing")
        if bid not in cache:
            cache[bid] = _load_backup(bid)
        meta, bdir = cache[bid]
        if rel not in meta.get("files", {}):
            raise InstallError(tr(f"退避 {bid} に導入前のファイルがありません: {rel}",
                                  f"Backup {bid} does not have the file from before the install: {rel}"),
                               reason="backup_missing")
        out[rel] = _backup_item(meta, bdir, rel)
    return out


def baseline_kinds(base: dict | None, writes: dict | None = None) -> dict[str, str]:
    """導入前の状態（baseline）のファイル（アンインストールで戻すもの）→ 最初の導入の前の持ち主の種別
    （aiskin-project: 記録の無い AI Skin の MOD / foreign: 他の MOD・不明）。baseline の kinds の記録から。記録が無ければ、
    writes（:func:`_baseline_writes`）があれば退避のファイルを判定し、できなければ "unknown"（2026-10-17 C126）。"""
    base = base or {}
    kinds = base.get("kinds") or {}
    out = {}
    for rel in sorted(base.get("files") or {}):
        kind = kinds.get(rel)
        if kind in (None, "absent") and writes and rel in writes:
            try:
                kind = classify_file(writes[rel]["src"], rel, None)[0]
            except Exception:  # noqa: BLE001
                kind = None
        out[rel] = kind if kind not in (None, "absent") else "unknown"
    return out


def uninstall(gd: str, fmt: str = "basemods", name: str | None = None, yes: bool = False, force: bool = False,
              library: Library | None = None, log=print) -> dict:
    """本ツールが導入したファイルを取り除き、最初の導入前の状態（baseline）に戻す。

    最初の導入の前にあったのが記録の無い AI Skin の MOD（試作・手作業で置いたもの・記録を失う前の導入）なら、それに
    戻るので AI Skin の MOD は残る。計画の restore_kinds（戻すファイル → 種別）・aiskin_remains（残る AI Skin のファイル）と
    注意の文で示す（2026-10-17 利用者の判断 C126。「AI Skin を全部外す」操作は作らない）。"""
    gd = os.path.abspath(gd)
    st = load_state()
    if fmt == "custom" and not name:
        name = _pick_custom_name(st, gd)
    tgt = st["targets"].get(_target_key(gd, fmt, name))
    if not tgt or not tgt.get("installed"):
        raise InstallError(tr("このアプリで導入した記録がありません（mods/ は変更していません）",
                              "There is no record of an install by this app (mods/ was not changed)"))
    pre_warnings = _check_common(gd)
    root = _root(gd, fmt, name)
    prefix = "" if fmt == "basemods" else name + "/"
    installed = tgt["installed"]
    base = tgt.get("baseline") or {"backup_id": None, "files": {}, "absent": list(installed["files"])}
    base_files = base.get("files") or {}
    rels = sorted(set(installed["files"]) | set(base_files))
    info = inspect_target(gd, fmt, name, st, rels)
    # 導入が中断した後のアンインストール: 中断した導入が置いたファイルは「変更されています」にせず、今の記録に無い
    # ものは中断の前の中身に戻す（2026-10-27 R07A-01）
    residue = _interrupted_residue(root, tgt, _managed(gd, fmt, name, rels))
    pre = {rel: (meta, bdir) for rel, (meta, bdir) in residue.items() if rel not in rels}
    pre_writes = {rel: _backup_item(meta, bdir, rel) for rel, (meta, bdir) in pre.items() if rel in meta["files"]}
    pre_removes = sorted(rel for rel in pre if rel not in pre_writes)
    # 導入で消した baseline のファイルの位置に、後から他の MOD のファイルが置かれていれば、それも競合にする
    # （baseline と同じ中身なら止めない。restore と同じく --force で続行。2026-10-20 R07A-02）
    conflicts = [{"file": rel, "kind": f["kind"], "detail": f["detail"]} for rel, f in info["files"].items()
                 if rel not in residue and ((rel in installed["files"] and f["kind"] == "modified")
                 or (rel not in installed["files"] and rel in base_files and f["kind"] == "foreign"
                     and sha256_file(_p(root, rel)) != (base_files[rel] or {}).get("sha256")))]
    rels = sorted(set(rels) | set(pre))
    blocking = [tr(f"mods/{prefix}{c['file']}: {c['detail']}（--force で続行）",
                   f"mods/{prefix}{c['file']}: {c['detail']} (continue with --force)") for c in conflicts if not force]
    blocking += _readonly_blocking(root, [r for r in rels if os.path.isfile(_p(root, r))], prefix)
    try:
        writes = _baseline_writes(base)         # 導入前のファイルの退避が読めるかを、変更の前に確かめる
    except InstallError as e:
        writes = {}
        blocking.append(str(e))
    kinds = baseline_kinds(base, writes)
    writes.update(pre_writes)
    for rel, item in writes.items():            # 退避のファイルそのものがあるか（R07B-05）
        try:
            _check_backup_src(rel, item)
        except InstallError as e:
            blocking.append(str(e))
    aiskin = [r for r, k in kinds.items() if k == "aiskin-project"]
    plan = {"action": "uninstall", "gd": gd, "format": fmt, "name": name, "root": root,
            "remove": [r for r in installed["files"] if r not in base_files] + pre_removes,
            "restore": sorted(set(base_files) | set(pre_writes)), "baseline_backup": base.get("backup_id"),
            "restore_kinds": kinds, "aiskin_remains": aiskin,
            "existing": info["files"], "conflicts": conflicts, "kept_foreign": [], "blocking": blocking,
            "warnings": pre_warnings + ([_aiskin_remains_warning(", ".join(f"mods/{prefix}{r}" for r in aiskin))]
                                        if aiskin else [])
            + _interrupted_warning(prefix, residue) + _links_warning(prefix, info["links"]),
            "executed": False, "interrupted": {rel: meta["id"] for rel, (meta, _b) in residue.items()}}
    if fmt == "basemods":
        plan["guide"] = tr("mods/ を元に戻しました。他に /basemods を使う MOD が無ければ、Steam の Grim Dawn のプロパティ → "
                           "起動オプションから /basemods を消してください（本ツールは起動オプションを書き換えません）。"
                           "収集に登録済みの AI Skin のイリュージョンが、レコードを消した後にどう表示されるかは未確認です。",
                           "mods/ was restored. If no other MOD uses /basemods, remove /basemods from Grim Dawn's "
                           "Properties → Launch Options in Steam (this tool never changes the launch options). How "
                           "AI Skin illusions already in your collection are shown after their records are removed has "
                           "not been confirmed.")
    if blocking:
        listed = "\n  - ".join(blocking)
        raise _blocked(tr(f"アンインストールを中止しました（mods/ は変更していません）:\n  - {listed}",
                          f"Uninstall stopped (mods/ was not changed):\n  - {listed}"), plan)
    if not yes:
        return plan
    for rel, item in writes.items():            # 中身も照合してから退避を作る（壊れていれば退避を増やさずに止める）
        _check_backup_src(rel, item, deep=True)
    backup = _new_backup(root, rels, "uninstall", tgt, "uninstall")
    log(tr(f"退避: {len(backup['files'])} ファイル → backups/{backup['id']}",
           f"Backup: {len(backup['files'])} file(s) → backups/{backup['id']}"))
    bdir = os.path.join(_backups_root(), backup["id"])
    _apply(root, writes, [r for r in rels if r not in base_files and r not in pre_writes],
           _rollback_map(backup, bdir, rels), action="uninstall", backup_id=backup["id"], prefix=prefix, log=log,
           sweep=fmt == "custom")
    _cleanup_dirs(gd, fmt, root, base)
    tgt["history"].append({"at": now_iso(), "action": "uninstall", "build_id": installed["build_id"],
                           "backup_id": backup["id"], "restored": base.get("backup_id")})
    tgt["installed"] = None
    tgt["baseline"] = None
    _commit_state(st, "uninstall", backup["id"])
    _update_library(library, installed.get("library", ""), [], None, plan, log)
    plan.update({"executed": True, "backup_id": backup["id"]})
    return plan


def restore(gd: str, backup_id: str, yes: bool = False, force: bool = False, library: Library | None = None,
            log=print) -> dict:
    """指定した退避の時点の状態に、管理対象のファイルを戻す（ロールバック）。"""
    gd = os.path.abspath(gd)
    meta, bdir = _load_backup(backup_id)
    if os.path.normcase(meta["gd"]) != os.path.normcase(gd):
        raise InstallError(tr(f"この退避は別のインストール先のものです: {meta['gd']}",
                              f"This backup belongs to another install folder: {meta['gd']}"))
    fmt, name = meta["format"], meta.get("name")
    st = load_state()
    tgt = _target(st, gd, fmt, name)
    pre_warnings = _check_common(gd)
    root = _root(gd, fmt, name)
    prefix = "" if fmt == "basemods" else name + "/"
    rels = sorted(set(meta["files"]) | set(meta["absent"]))
    info = inspect_target(gd, fmt, name, st, rels)
    conflicts = [{"file": rel, "kind": f["kind"], "detail": f["detail"]} for rel, f in info["files"].items()
                 if f["kind"] in ("foreign", "modified") and rel in rels]
    blocking = [tr(f"mods/{prefix}{c['file']}: {c['detail']}（--force で続行）",
                   f"mods/{prefix}{c['file']}: {c['detail']} (continue with --force)") for c in conflicts if not force]
    blocking += _readonly_blocking(root, [r for r in rels if os.path.isfile(_p(root, r))], prefix)
    # 今の導入の記録のファイルのうち、この退避が覆わないもの（退避の後の導入で増えたファイル）。戻すと記録の無い
    # ファイルが mods/ に残り、導入前の状態の記録も失うので、--force でも止める（2026-10-27 R07A-02）
    covered = {os.path.normcase(r) for r in rels}
    uncovered = sorted(r for r in ((tgt.get("installed") or {}).get("files") or {})
                       if os.path.normcase(r) not in covered)
    blocking += [tr(f"mods/{prefix}{r}: 退避 {backup_id} の時点には無かった、後の導入のファイルです。新しい退避から順に "
                    "restore するか、先に uninstall してから restore してください",
                    f"mods/{prefix}{r}: This file comes from a later install and is not covered by backup {backup_id}. "
                    "Restore the newer backups first, one at a time, or uninstall first and then restore")
                 for r in uncovered]
    writes = {rel: _backup_item(meta, bdir, rel) for rel in meta["files"]}
    for rel, item in writes.items():            # 退避のファイルそのものがあるか（R07B-05）
        try:
            _check_backup_src(rel, item)
        except InstallError as e:
            blocking.append(str(e))
    plan = {"action": "restore", "gd": gd, "format": fmt, "name": name, "root": root, "backup_id": backup_id,
            "restore": sorted(meta["files"]), "remove": sorted(meta["absent"]), "existing": info["files"],
            "conflicts": conflicts, "kept_foreign": [], "blocking": blocking, "warnings": pre_warnings, "executed": False,
            "uncovered": uncovered}
    if blocking:
        listed = "\n  - ".join(blocking)
        raise _blocked(tr(f"復元を中止しました（mods/ は変更していません）:\n  - {listed}",
                          f"Restore stopped (mods/ was not changed):\n  - {listed}"), plan)
    if not yes:
        return plan
    for rel, item in writes.items():            # 中身も照合してから退避を作る（壊れていれば退避を増やさずに止める）
        _check_backup_src(rel, item, deep=True)
    old = tgt.get("installed")
    before = meta.get("installed_before")
    # 導入なし（アンインストールの後など）から導入ありの状態に戻すときは、今の状態を導入前の状態（baseline）として
    # 記録する（以前は baseline が無いままになり、次の導入で本ツールの MOD のファイルが導入前の状態に入った。
    # 2026-10-20 R07A-01）。戻す先の導入のファイルも退避に入れ、install の最初の導入と同じく種別を記録する
    first = before is not None and old is None and tgt.get("baseline") is None
    base_rels = sorted(set(rels) | set(before.get("files") or {})) if first else rels
    binfo = inspect_target(gd, fmt, name, st, base_rels) if first else None
    created = [d for d in (["mods"] + ([f"mods/{name}"] if fmt == "custom" else []))
               if not os.path.isdir(os.path.join(gd, d.replace("/", os.sep)))] if first else []
    cur = _new_backup(root, base_rels, f"restore {backup_id}", tgt, "restore")
    cdir = os.path.join(_backups_root(), cur["id"])
    _apply(root, writes, sorted(meta["absent"]), _rollback_map(cur, cdir, rels), action="restore", backup_id=cur["id"],
           prefix=prefix, log=log, sweep=fmt == "custom")
    base = tgt.get("baseline") or {}
    tgt["installed"] = before
    if tgt["installed"] is None:
        _cleanup_dirs(gd, fmt, root, base)
        tgt["baseline"] = None
    elif first:
        tgt["baseline"] = {"backup_id": cur["id"],
                           "files": {r: dict(v, backup_id=cur["id"]) for r, v in cur["files"].items()},
                           "absent": cur["absent"],
                           "kinds": {r: binfo["files"].get(r, {}).get("kind", "absent") for r in set(cur["files"]) | set(cur["absent"])},
                           "created_dirs": created, "at": now_iso()}
    tgt["history"].append({"at": now_iso(), "action": "restore", "backup_id": cur["id"], "restored": backup_id})
    _commit_state(st, "restore", cur["id"])
    inst = tgt["installed"]
    if inst:
        _update_library(library, inst.get("library", ""), inst.get("equipment", []), inst.get("build_id"), plan, log,
                        also=((old or {}).get("library"),))
    elif old or library is not None:
        # 本ツールの導入なしに戻したら、装備も導入なしにする（uninstall と同じ。2026-10-20 R07A-03）
        _update_library(library, (old or {}).get("library", ""), [], None, plan, log)
    plan.update({"executed": True, "backup_id": cur["id"]})
    return plan


def _remove_empty_dirs(root: str, keep_root: bool = False) -> None:
    if not os.path.isdir(root):
        return
    # リンク・ジャンクションの中（mods/ の外）には入らず、それ自体も消さない（2026-10-27 R07B-02）
    dirs = [dirpath for dirpath, _dirs, _files in _walk(root)]
    for dirpath in reversed(dirs):          # 子から先に
        if keep_root and os.path.normcase(dirpath) == os.path.normcase(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def _remove_created_dirs(gd: str, created: list[str]) -> None:
    """導入で作ったフォルダー（gd からの相対パス）のうち、空のものだけを子から順に消す。もともとあったものは消さない。"""
    for d in sorted(created, key=len, reverse=True):
        p = os.path.join(gd, d.replace("/", os.sep))
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass


def _cleanup_dirs(gd: str, fmt: str, root: str, base: dict) -> None:
    """アンインストール（と、導入前の状態に戻す restore）の後、空になったフォルダーを消す（2026-10-17 監査 C91）。

    custom は mods/<名前>/ の中の空のフォルダー。導入で作った mods/（custom なら mods/<名前>/ も）は空なら消す。
    記録（created_dirs）の無い古い state.json の custom は、これまでどおり mods/<名前>/ も空なら消す。"""
    created = base.get("created_dirs")
    if fmt == "custom":
        _remove_empty_dirs(root, keep_root=created is not None)
    if created:
        _remove_created_dirs(gd, created)


def status(gd: str) -> dict:
    """mods/ の現状と導入の記録。"""
    gd = os.path.abspath(gd)
    st = load_state()
    out = {"gd": gd, "basemods": inspect_target(gd, "basemods", None, st), "targets": [],
           "launch_options": steam.launch_options(), "game_language": steam.game_language(),
           "game_running": steam.game_running(), "backups": backups_usage()}
    for t in st["targets"].values():
        if os.path.normcase(t["gd"]) == os.path.normcase(gd):
            out["targets"].append({k: t.get(k) for k in ("format", "name", "installed", "baseline")}
                                  | {"history": t.get("history", [])[-5:]})
    return out
