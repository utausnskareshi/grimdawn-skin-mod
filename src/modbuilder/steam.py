"""Steam ライブラリと Grim Dawn のインストール先の検出、起動オプションとゲーム設定の読み取り（すべて読み取りのみ）。

* Steam のルート: レジストリ（HKCU\\Software\\Valve\\Steam の SteamPath、HKLM の InstallPath）→ 既定の場所
* ライブラリ: ``<Steam>/steamapps/libraryfolders.vdf`` の各 ``path``
* Grim Dawn（AppID 219990）: 各ライブラリの ``steamapps/appmanifest_219990.acf`` の ``installdir``
* 起動オプション: ``<Steam>/userdata/<ID>/config/localconfig.vdf`` の apps/219990/LaunchOptions
  （本ツールは書き換えない。/basemods が入っているかを表示し、設定手順を案内する）
* 今の Steam ユーザー: レジストリ（HKCU\\Software\\Valve\\Steam\\ActiveProcess の ActiveUser）→
  ``<Steam>/config/loginusers.vdf``（:func:`current_user`。2026-10-17 機能 SF12）
* ゲームの言語: ``Documents/My Games/Grim Dawn/Settings/options.txt`` の ``language``
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from ._i18n import tr

__all__ = ["APP_ID", "parse_vdf", "steam_root", "library_folders", "find_grim_dawn", "launch_options",
           "game_language", "game_running", "documents_dir", "basemods_guide", "game_build_info", "current_user",
           "steam_running"]

APP_ID = "219990"
DEFAULT_STEAM = r"C:\Program Files (x86)\Steam"
# SteamID64 の組み立て（Valve の資料 https://developer.valvesoftware.com/wiki/SteamID）:
#   universe（8 bit）<< 56 | アカウントの種類（4 bit）<< 52 | instance（20 bit）<< 32 | アカウント ID（32 bit）
# 個人のアカウントは universe 1（Public）・種類 1（Individual）・instance 1（Desktop）。アカウント ID を 0 にした値を
# 基準にし、SteamID64 − STEAM_ID64_BASE = アカウント ID（userdata のフォルダー名）とする（2026-10-18 判断 #4: 値は定義で書く）。
STEAM_UNIVERSE_PUBLIC = 1
STEAM_ACCOUNT_TYPE_INDIVIDUAL = 1
STEAM_INSTANCE_DESKTOP = 1
STEAM_ID64_BASE = (STEAM_UNIVERSE_PUBLIC << 56) | (STEAM_ACCOUNT_TYPE_INDIVIDUAL << 52) | (STEAM_INSTANCE_DESKTOP << 32)
_ACTIVE_PROCESS = r"Software\Valve\Steam\ActiveProcess"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0     # tasklist のコンソールの窓を出さない


def _vdf_tokens(text: str):
    i, n = 0, len(text)
    esc = {"n": "\n", "t": "\t"}
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "{}":
            yield c, None
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(esc.get(text[j + 1], text[j + 1]))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            yield "s", "".join(buf)
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            yield "s", text[i:j]
            i = j


def parse_vdf(text: str) -> dict:
    """Valve の KeyValues テキスト（"key" "value" と "key" { ... }。// コメント、\\ のエスケープ）を辞書に。"""
    stack: list[dict] = [{}]
    key = None
    for kind, val in _vdf_tokens(text):
        if kind == "{":
            if key is None:
                raise ValueError(tr("VDF: キーの無いブロック", "VDF: block without a key"))
            d: dict = {}
            stack[-1][key] = d
            stack.append(d)
            key = None
        elif kind == "}":
            if len(stack) == 1:
                raise ValueError(tr("VDF: 対応しない }", "VDF: unmatched }"))
            stack.pop()
        elif key is None:
            key = val
        else:
            stack[-1][key] = val
            key = None
    return stack[0]


def _get(d: dict, *keys):
    """大文字小文字を区別せずに入れ子の値を取る。"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        low = {kk.lower(): v for kk, v in cur.items()}
        cur = low.get(k.lower())
    return cur


def _read(path: str) -> str:
    # 先頭の BOM（メモ帳で保存した VDF など）は取り除く（キーとして読むと全体の解析が失敗した。2026-10-27 R07A-04）
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def steam_root() -> str | None:
    cands = []
    try:
        import winreg
        for hive, sub, name in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    cands.append(winreg.QueryValueEx(k, name)[0])
            except OSError:
                pass
    except ImportError:   # Windows 以外（テスト用）
        pass
    cands.append(DEFAULT_STEAM)
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "steamapps")):
            return os.path.normpath(c)
    return None


def library_folders(root: str) -> list[str]:
    """Steam のライブラリの一覧（先頭はルート）。libraryfolders.vdf が読めない・形が違うときはルートだけ
    （1 つのファイルのために全てのライブラリの検出を失敗させない。2026-10-27 R07A-04・R07B-07）。"""
    out = [os.path.normpath(root)]
    p = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(p):
        try:
            data = parse_vdf(_read(p))
        except (OSError, ValueError):
            return out
        lf = _get(data, "libraryfolders")
        for k, v in (lf.items() if isinstance(lf, dict) else ()):
            path = v.get("path") if isinstance(v, dict) else (v if k.isdigit() else None)
            if isinstance(path, str) and path.strip():
                path = os.path.normpath(path)
                if os.path.normcase(path) not in {os.path.normcase(x) for x in out}:
                    out.append(path)
    return out


def find_grim_dawn(root: str | None = None) -> list[dict]:
    """[{path, library, source}]。見つかった順（appmanifest があるもの優先）。"""
    from .gamedata import looks_like_gd
    root = root or steam_root()
    if not root:
        return []
    found = []
    for lib in library_folders(root):
        acf = os.path.join(lib, "steamapps", f"appmanifest_{APP_ID}.acf")
        installdir, source = None, None
        if os.path.isfile(acf):
            # 読めない・形の違う appmanifest は installdir 無し（既定の Grim Dawn のフォルダーだけを探す）として続ける
            # （1 つのライブラリのために他のライブラリのゲームまで見つからなくしない。2026-10-27 R07A-04・R07B-07）
            try:
                installdir = _get(parse_vdf(_read(acf)), "AppState", "installdir")
            except (OSError, ValueError):
                installdir = None
            if not isinstance(installdir, str):
                installdir = None
            source = acf
        for name in ([installdir] if installdir else []) + ["Grim Dawn"]:
            gd = os.path.join(lib, "steamapps", "common", name)
            if looks_like_gd(gd) and os.path.normcase(gd) not in {os.path.normcase(f["path"]) for f in found}:
                found.append({"path": gd, "library": lib, "source": source or "steamapps/common"})
    return found


def launch_options(root: str | None = None) -> list[dict]:
    """[{user, path, options}]。Steam のユーザーごとの Grim Dawn の起動オプション。"""
    root = root or steam_root()
    out = []
    ud = os.path.join(root, "userdata") if root else ""
    if not ud or not os.path.isdir(ud):
        return out
    # 表示・案内のためだけの読み取りなので、読めないユーザーは飛ばす（導入の計画を OSError で止めない。2026-10-27 R07B-06）
    try:
        users = sorted(os.listdir(ud))
    except OSError:
        return out
    for user in users:
        p = os.path.join(ud, user, "config", "localconfig.vdf")
        if not os.path.isfile(p):
            continue
        try:
            data = parse_vdf(_read(p))
        except (OSError, ValueError):
            continue
        app = _get(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID)
        if isinstance(app, dict):
            opts = app.get("LaunchOptions") or _get(app, "LaunchOptions") or ""
            # 文字列だけを使う（ブロックの形の値で basemods_guide の .lower() が失敗した。R07B-07）
            out.append({"user": user, "path": p, "options": opts if isinstance(opts, str) else ""})
    return out


def _reg_value(sub: str, name: str):
    """HKCU\\<sub> の値 name（読み取りのみ）。無い・読めない・Windows 以外は None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            return winreg.QueryValueEx(k, name)[0]
    except (ImportError, OSError):
        return None


def _account_id(v) -> str | None:
    """アカウント ID（32 bit の数）か SteamID64 → userdata のフォルダー名。数でなければ None。"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    if n >= STEAM_ID64_BASE:
        n -= STEAM_ID64_BASE
    return str(n) if 0 < n < 2 ** 32 else None


def current_user(root: str | None = None, reg=None) -> tuple[str | None, str | None]:
    """今の Steam ユーザー（userdata のフォルダー名 = アカウント ID）と、判定の方法（読み取りのみ。2026-10-17 機能 SF12）。

    1. ``active_user``: HKCU\\Software\\Valve\\Steam\\ActiveProcess の ActiveUser（Steam にログインしている間のアカウント ID。
       0 はログインしていない）。Steam を終了しても 0 にならずに残ることがある（2026-10-20 確認）ので、Steam が
       起動していないと分かるとき（:func:`steam_running` が False）は、同じ ID を ``last_active_user``（最後に
       ログインしたユーザー）として返す（2026-10-20 R07B-05）
    2. ``login_users``: ``<Steam>/config/loginusers.vdf`` の MostRecent が 1 のユーザー、無ければ Timestamp が最も新しい
       ユーザー（SteamID64 をアカウント ID に直す）

    どちらも userdata にそのフォルダーがあるときだけ使う。判定できなければ (None, None)。reg はテスト用の
    レジストリの読み取り（``reg(サブキー, 値の名前)``）。"""
    reg = reg or _reg_value
    root = root or steam_root()
    if not root:
        return None, None
    ud = os.path.join(root, "userdata")

    def present(uid):
        return uid is not None and os.path.isdir(os.path.join(ud, uid))

    uid = _account_id(reg(_ACTIVE_PROCESS, "ActiveUser"))
    if present(uid):
        return uid, ("last_active_user" if steam_running(reg) is False else "active_user")
    p = os.path.join(root, "config", "loginusers.vdf")
    try:
        users = _get(parse_vdf(_read(p)), "users") if os.path.isfile(p) else None
    except (OSError, ValueError):
        users = None
    if not isinstance(users, dict):
        return None, None
    cands = []
    for sid, rec in users.items():
        uid = _account_id(sid)
        if not isinstance(rec, dict) or not present(uid):
            continue
        recent = str(_get(rec, "MostRecent") or "0").strip() == "1"
        try:
            ts = int(str(_get(rec, "Timestamp") or "0").strip())
        except ValueError:
            ts = 0
        cands.append((recent, ts, uid))
    if not cands:
        return None, None
    return max(cands)[2], "login_users"


def _pid_is_steam(pid: int) -> bool | None:
    """プロセス pid が動いている steam.exe か。判定できなければ None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False if ctypes.get_last_error() == 87 else None     # 87: そのプロセスは無い
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            if code.value != 259:                            # STILL_ACTIVE でない
                return False
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value).lower() == "steam.exe"
        finally:
            k32.CloseHandle(h)
    except (OSError, AttributeError, ValueError):
        return None


def steam_running(reg=None) -> bool | None:
    """Steam が起動しているか（HKCU の ActiveProcess の pid が動いている steam.exe か。読み取りのみ。機能 SF12）。
    判定できなければ None。起動中に起動オプションを変えた直後は、localconfig.vdf にまだ書かれていないことがある
    （Steam を終了すると書かれる [推定]）ので、画面の案内に使う。"""
    pid = (reg or _reg_value)(_ACTIVE_PROCESS, "pid")
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False
    return _pid_is_steam(pid)


def documents_dir() -> str:
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:   # CSIDL_PERSONAL
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def game_language(docs: str | None = None) -> str | None:
    p = os.path.join(docs or documents_dir(), "My Games", "Grim Dawn", "Settings", "options.txt")
    if not os.path.isfile(p):
        return None
    try:
        text = _read(p)
    except OSError:
        return None         # 読めない（他のプログラムが開いているなど）＝判定できない（2026-10-27 R07B-06）
    m = re.search(r'^\s*language\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else None


def game_running() -> bool | None:
    """Grim Dawn.exe が動いているか（tasklist）。True / False、判定できなければ None（2026-10-17 監査 C90。
    以前は判定できないと False＝「起動していない」扱いだった）。32 bit と 64 bit の実行ファイルはどちらも Grim Dawn.exe。"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq Grim Dawn.exe"],
                           capture_output=True, text=True, errors="replace", timeout=30, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return "grim dawn.exe" in (r.stdout or "").lower()


def game_build_info(gd: str | None) -> dict | None:
    """Steam のゲームのビルド番号（``appmanifest_219990.acf`` の buildid。読み取りのみ。2026-10-17 機能 SF09）。

    gd が ``<ライブラリ>/steamapps/common/<installdir>`` のときだけ、同じライブラリの appmanifest を読む（Steam 以外で
    入れたゲーム・テスト用の導入先は None）。→ {"buildid", "target_buildid"（Steam が更新を待っているときの新しい番号。
    無ければ None）, "update_pending"}。ゲームの更新の目安で、MOD の作り直しが要るかは補給官のレコードの中身で判定する
    （:func:`modbuilder.install.official_changes`）。"""
    if not gd:
        return None
    gd = os.path.abspath(gd)
    common = os.path.dirname(gd)
    steamapps = os.path.dirname(common)
    if os.path.basename(common).lower() != "common" or os.path.basename(steamapps).lower() != "steamapps":
        return None
    acf = os.path.join(steamapps, f"appmanifest_{APP_ID}.acf")
    if not os.path.isfile(acf):
        return None
    try:
        app = _get(parse_vdf(_read(acf)), "AppState")
    except (OSError, ValueError):
        return None
    if not isinstance(app, dict):
        return None
    installdir = _get(app, "installdir")
    if isinstance(installdir, str) and installdir.strip() \
            and os.path.normcase(installdir.strip()) != os.path.normcase(os.path.basename(gd)):
        return None          # 同じライブラリの別のフォルダー（記録と違う場所のゲーム）
    bid = _get(app, "buildid")
    bid = bid.strip() if isinstance(bid, str) else ""
    if not bid.isdigit() or bid == "0":
        return None
    target = _get(app, "TargetBuildID")
    target = target.strip() if isinstance(target, str) and target.strip().isdigit() and target.strip() != "0" else None
    flags = _get(app, "StateFlags")
    try:
        flags = int(flags) if isinstance(flags, str) else 0
    except ValueError:
        flags = 0
    # StateFlags の 2 は「更新が必要」（Steam の AppState の値）[推定]。TargetBuildID が今と違うときも更新を待っている
    pending = bool((target and target != bid) or (flags & 2))
    return {"buildid": bid, "target_buildid": target if target != bid else None, "update_pending": pending}


def basemods_guide(options: list[dict]) -> str:
    has = [o for o in options if "/basemods" in o["options"].lower()]
    if options and len(has) == len(options):
        users = ", ".join(tr(f"ユーザー {o['user']}: {o['options']!r}", f"user {o['user']}: {o['options']!r}") for o in has)
        return tr(f"Steam の起動オプションには既に /basemods が入っています（{users}）。Steam から起動すれば MOD が読まれます。\n"
                  "アンインストール後に他の /basemods の MOD を使わない場合は、Steam の Grim Dawn のプロパティ → 起動オプションから "
                  "/basemods を消してください（本ツールは起動オプションを書き換えません）。",
                  f"/basemods is already set in the Steam launch options ({users}). The MOD is loaded when you start "
                  "the game from Steam.\nIf you will not use any other /basemods MOD after uninstalling, remove "
                  "/basemods from Grim Dawn's Properties → Launch Options in Steam (this tool never changes the launch "
                  "options).")
    if has:
        users = ", ".join(f"{o['user']}: {o['options']!r}" for o in options)
        state = tr(f"一部の Steam ユーザーにだけ /basemods が入っています: {users}。",
                   f"/basemods is set for only some Steam users: {users}.")
    elif options:
        current = ", ".join(repr(o["options"]) for o in options)
        state = tr(f"Steam の起動オプションに /basemods が見つかりません（現在: {current}）。",
                   f"/basemods was not found in the Steam launch options (current: {current}).")
    else:
        state = tr("Steam の起動オプションに /basemods が見つかりません。", "/basemods was not found in the Steam launch options.")
    return state + tr(
        "\n/basemods 方式の MOD はこの起動オプションが無いと読み込まれません。本ツールは起動オプションを書き換えないので、"
        "必要なら次の手順で設定してください:\n"
        "  1. Steam のライブラリで Grim Dawn を右クリック → プロパティ\n"
        "  2. 一般 → 起動オプション に /basemods と入力（他のオプションがあれば半角スペースで区切って後ろに追加）\n"
        "  3. Steam から Grim Dawn を起動する（メインキャンペーンで MOD が有効になる）\n"
        "Steam の起動中に設定した直後は、Steam を終了するまで設定のファイル（localconfig.vdf）に書かれないことがあります"
        "（本ツールの確認はこのファイルを読みます）。\n"
        "アンインストール後に他の /basemods の MOD を使わない場合は、同じ場所から /basemods を消してください。",
        "\nMODs that use the /basemods method are not loaded without this launch option. This tool never changes the "
        "launch options, so set it with these steps if needed:\n"
        "  1. In the Steam library, right-click Grim Dawn → Properties\n"
        "  2. Under General → Launch Options, enter /basemods (if there are other options, add it at the end separated "
        "by a space)\n"
        "  3. Start Grim Dawn from Steam (the MOD is enabled in the main campaign)\n"
        "If you set it while Steam is running, it may not be written to the settings file (localconfig.vdf) until you "
        "exit Steam (this tool reads that file to check).\n"
        "If you will not use any other /basemods MOD after uninstalling, remove /basemods from the same place.")
