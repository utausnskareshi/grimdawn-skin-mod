"""ユーザーデータ領域の既定の場所。

アプリ本体（このリポジトリ・配布物）とは別に、``%LOCALAPPDATA%\\GrimDawnSkinMOD\\`` 以下へ保存する（計画書 4.3、15 章）:

* ``library\\``   装備ライブラリ（``GDSKIN_LIBRARY`` で変更可）
* ``builds\\``    ビルドの出力（ビルドごとのフォルダー）
* ``install\\``   導入状態（``state.json``）と、mods/ の置き換え前の退避（``backups\\``）
* ``cache\\``     ゲームから取り出したテンプレートなど（消しても再作成される）
* ``refs\\``      gdconvert の基準メッシュ（gdconvert と共有。プレイヤー骨格のボーン名照合にも使う）

ルート全体は環境変数 ``GDSKIN_HOME`` で変えられる（テストでは一時フォルダーを渡す）。
"""
from __future__ import annotations

import os

__all__ = ["home", "library_dir", "builds_dir", "install_dir", "cache_dir", "refs_dir"]

APP_DIR_NAME = "GrimDawnSkinMOD"


def home() -> str:
    env = os.environ.get("GDSKIN_HOME")
    if env:
        return os.path.abspath(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, APP_DIR_NAME)


def library_dir() -> str:
    return os.path.abspath(os.environ.get("GDSKIN_LIBRARY") or os.path.join(home(), "library"))


def builds_dir() -> str:
    return os.path.join(home(), "builds")


def install_dir() -> str:
    return os.path.join(home(), "install")


def cache_dir() -> str:
    return os.path.join(home(), "cache")


def refs_dir() -> str:
    """gdconvert と同じ既定（``GDCONVERT_REFS`` → ``%LOCALAPPDATA%\\GrimDawnSkinMOD\\refs``）。"""
    env = os.environ.get("GDCONVERT_REFS")
    if env:
        return env
    return os.path.join(home(), "refs")
