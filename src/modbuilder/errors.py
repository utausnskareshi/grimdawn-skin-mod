"""ModBuilder の例外。CLI はこれらを捕まえて理由を表示し、0 以外で終了する。"""
from __future__ import annotations

__all__ = ["ModBuilderError", "LibraryError", "GameDataError", "GameToolMissing", "ValidationFailed", "BuildStopped",
           "InstallError", "InstallFailed"]


class ModBuilderError(Exception):
    """ModBuilder の処理が続けられないときの例外（利用者に見せる理由を message に入れる）。"""


class LibraryError(ModBuilderError):
    """装備ライブラリの読み書き・指定の誤り。"""


class GameDataError(ModBuilderError):
    """ゲームのインストール先やゲームデータが見つからない・読めない。"""


class GameToolMissing(GameDataError):
    """ゲームのフォルダーはあるが、MOD の作成に使う付属のツール（ArchiveTool.exe）が無い。``path``: 無いファイルの
    パス（2026-10-17 点検 C82・FG05-R2。アプリでは GAME_NOT_FOUND の reason "game_tool_missing"）。"""

    def __init__(self, message: str, path: str | None = None):
        super().__init__(message)
        self.path = path


class BuildStopped(ModBuilderError):
    """ビルドのフォルダーを作った後、検証以外の理由（ArchiveTool の失敗・打ち切り、ファイルの読み書き、ゲームデータの
    読み取りなど）で止まった。manifest（status "failed"）と装備の状態（build_failed）には記録済み（2026-10-17 点検 C83・
    FG05-R1。アプリでは MOD_BUILD_FAILED の reason "stopped"）。"""


class ValidationFailed(ModBuilderError):
    """検証で問題が見つかり、ビルドを止めた。``problems`` に理由の一覧。"""

    def __init__(self, message: str, problems: list[str]):
        super().__init__(message)
        self.problems = list(problems)

    def __str__(self) -> str:
        lines = [super().__str__()] + ["  - " + p for p in self.problems]
        return "\n".join(lines)


class InstallError(ModBuilderError):
    """導入・アンインストールを中止した（mods/ は変更していないか、変更前に戻した）。

    ``reason``: 止めた理由の種類（無ければ None）。build_files（ビルドのファイルが無い・記録と一致しない）・
    backup_missing（導入の前の状態の退避が無い・壊れている）・install_state（導入の記録 state.json が壊れている）・
    game_running（Grim Dawn が起動中。2026-11-04 点検 C5_english-01。文の目印「起動中」/"Grim Dawn is running" の
    代わりに使える）。アプリはこれで画面の文を選ぶ（2026-10-27 点検 R19B-03）。"""

    def __init__(self, *args, reason: str | None = None):
        super().__init__(*args)
        self.reason = reason


class InstallFailed(InstallError):
    """mods/ への書き込み（置き換え・削除）が途中で失敗した（2026-10-17 監査 C11）。

    ``rolled_back``: 変更したファイルを全て操作の前に戻せたか。``unrestored``: 戻せなかったファイル（対象ルートからの
    相対パス）。``backup_id``: この操作の前に作った退避（戻せなかったファイルはここにある）。``cause``: 原因
    （in_use 使用中 / read_only 読み取り専用 / permission 権限 / disk_full 空き容量 / other）。``action``: install /
    uninstall / restore。``stage``: 失敗した場所（mods: mods/ への書き込み / backup: データフォルダーの install\\backups
    への退避の作成。backup のときは mods/ を変更していない。2026-10-17 FG06-R5）。"""

    def __init__(self, message: str, *, action: str, cause: str = "other", rolled_back: bool = True,
                 unrestored: list[str] | None = None, backup_id: str | None = None, changed: bool = False,
                 stage: str = "mods"):
        super().__init__(message)
        self.action = action
        self.cause = cause
        self.rolled_back = rolled_back
        self.unrestored = list(unrestored or [])
        self.backup_id = backup_id
        self.changed = changed
        self.stage = stage
