"""ログファイル（docs/app_architecture.md 8.2 節）。

* ``logs\\backend-YYYYMMDD.log`` — 1 行 1 件: 日時、レベル、ジョブID、処理、エラーコード、内容（原因・対処方法）
* ``logs\\jobs\\<ジョブID>.log`` — 子プロセスの出力
* どちらも :func:`gdskin_backend.redact.redact` を通してから書く。14 日より古いファイルは起動時に消す。
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from collections import deque

from . import paths
from .i18n import tr
from .redact import redact

RETENTION_DAYS = 14
FIELD_MAX = 16384           # 1 件の内容・対処の長さの上限（トレースバックの末尾 3000 文字・出力の 30 行より十分大きい）
_lock = threading.Lock()
_listeners: list = []       # (dict) -> None。画面のログ表示用にメモリにも残す
_recent: deque = deque(maxlen=2000)


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _backend_log_path(day: _dt.date | None = None) -> str:
    day = day or _dt.date.today()
    return os.path.join(paths.logs_dir(), f"backend-{day:%Y%m%d}.log")


def _field(v) -> str:
    s = str(v).replace("\r", " ").replace("\n", " | ")
    if len(s) > FIELD_MAX:
        # 1 件が際限なく長くならないように（相手の指定した文字列を含む行への守り。点検 R09B-01・X5X-01）
        s = s[:FIELD_MAX] + tr(f"…（全 {len(s)} 文字）", f"... ({len(s)} characters in total)")
    return s


def log(level: str, message: str, job: str | None = None, stage: str | None = None, code: str | None = None,
        hint: str | None = None) -> dict:
    """ログを 1 件書く。戻り値は画面に送る形（個人情報を除いた後）。"""
    rec = {"time": now_iso(), "level": level.upper(), "job": job, "stage": stage, "code": code,
           "message": redact(_field(message)), "hint": redact(_field(hint)) if hint else None}
    parts = [rec["time"], f"{rec['level']:<5}"]
    if job:
        parts.append(f"job={job}")
    if stage:
        parts.append(f"stage={stage}")
    if code:
        parts.append(f"code={code}")
    parts.append(rec["message"])
    if rec["hint"]:
        parts.append(tr(f"対処: {rec['hint']}", f"Hint: {rec['hint']}"))
    line = " ".join(parts)
    with _lock:
        # ログのファイルに書けなくても（ディスクの空き・ウイルス対策のロックなど）、呼び出し元の処理（ジョブ・エラーの
        # 応答）を失敗させない。画面のログ（_recent）には残す（R09A-01）
        try:
            os.makedirs(paths.logs_dir(), exist_ok=True)
            with open(_backend_log_path(), "a", encoding="utf-8", errors="backslashreplace", newline="\n") as f:
                f.write(line + "\n")
        except OSError:
            pass
        _recent.append(rec)
    return rec


def info(message: str, **kw) -> dict:
    return log("INFO", message, **kw)


def warning(message: str, **kw) -> dict:
    return log("WARN", message, **kw)


def error(message: str, **kw) -> dict:
    return log("ERROR", message, **kw)


def recent(limit: int = 500, level: str | None = None) -> list[dict]:
    with _lock:
        items = list(_recent)
    if level:
        items = [r for r in items if r["level"] == level.upper()]
    return items[-limit:]


def tail_file(lines: int = 500) -> list[str]:
    """今日（無ければ最新）のバックエンドのログの末尾。"""
    d = paths.logs_dir()
    if not os.path.isdir(d):
        return []
    files = sorted(f for f in os.listdir(d) if f.startswith("backend-") and f.endswith(".log"))
    out: deque = deque(maxlen=lines)
    for name in files[-2:]:
        with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
            for ln in f:
                out.append(ln.rstrip("\n"))
    return list(out)


def cleanup_old(days: int = RETENTION_DAYS) -> int:
    """古いログを消す。消した数を返す。"""
    limit = time.time() - days * 86400
    n = 0
    for d in (paths.logs_dir(), paths.job_logs_dir()):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.endswith(".log") and os.path.getmtime(p) < limit:
                try:
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
    return n


class JobLog:
    """ジョブごとの詳細ログ（子プロセスの出力）。"""

    def __init__(self, job_id: str):
        self.path = os.path.join(paths.job_logs_dir(), f"{job_id}.log")
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError:
            pass                        # 書けなければ write が何もしない（ジョブは止めない。R09A-01）
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8", errors="backslashreplace", newline="\n") as f:
                    f.write(redact(line.rstrip("\r\n")) + "\n")
            except OSError:
                pass
