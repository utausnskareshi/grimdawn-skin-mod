"""リポジトリ（アプリ本体）とデータフォルダーの場所。

アプリ本体のファイル（このリポジトリ、配布時は展開したフォルダー）は読むだけにし、利用者のデータは
データフォルダー（``%LOCALAPPDATA%\\GrimDawnSkinMOD``、環境変数 ``GDSKIN_HOME``）に置く。データフォルダーの既定は
modbuilder（``modbuilder.paths``）と同じで、装備ライブラリ・ビルド・導入状態・基準メッシュを共有する。
"""
from __future__ import annotations

import os
import sys

# app/backend/gdskin_backend/paths.py → リポジトリのルート
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(BACKEND_DIR))
SRC_DIR = os.path.join(REPO_ROOT, "src")
TRELLIS_SCRIPTS = os.path.join(REPO_ROOT, "scripts", "trellis")
WORKER_SCRIPT = os.path.join(BACKEND_DIR, "worker.py")


def _dist_info() -> dict | None:
    """配布物（ZIP を展開したフォルダー）なら gdskin_dist.json の中身（版・作成日時）。開発中のリポジトリでは None。"""
    import json
    try:
        with open(os.path.join(REPO_ROOT, "gdskin_dist.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


DIST_INFO = _dist_info()
IS_DIST = DIST_INFO is not None


def ensure_sys_path() -> None:
    """``src``（gdmesh・gdconvert・modbuilder）とバックエンドを import できるようにする（``python -I`` でも動くように）。"""
    for p in (SRC_DIR, BACKEND_DIR):
        if p not in sys.path:
            sys.path.insert(0, p)


ensure_sys_path()

from modbuilder import paths as _mb_paths  # noqa: E402


def home() -> str:
    return _mb_paths.home()


def app_dir() -> str:
    return os.path.join(home(), "app")


def work_dir() -> str:
    """生成結果の下書き。"""
    return os.path.join(home(), "work")


def logs_dir() -> str:
    return os.path.join(home(), "logs")


def job_logs_dir() -> str:
    return os.path.join(logs_dir(), "jobs")


def library_dir() -> str:
    return _mb_paths.library_dir()


def builds_dir() -> str:
    return _mb_paths.builds_dir()


def install_dir() -> str:
    return _mb_paths.install_dir()


def refs_dir() -> str:
    return _mb_paths.refs_dir()


def is_within(path: str, root: str) -> bool:
    """path が root の中か（大文字小文字・区切りの違いを無視）。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([p, r]) == r
    except ValueError:      # 別のドライブ
        return False
