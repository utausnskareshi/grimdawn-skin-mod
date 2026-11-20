"""バックエンドの入口: ``python -I -X utf8 run_backend.py [--port 0] [--parent-pid <PID>]``。

環境変数:
  GDSKIN_BACKEND_TOKEN  合言葉（必須。16 文字以上。WPF が起動ごとに作る）
  GDSKIN_HOME           データフォルダー（既定 %LOCALAPPDATA%\\GrimDawnSkinMOD）

準備ができたら標準出力に ``GDSKIN_BACKEND_READY {"port": ..., "pid": ..., "version": ..., "api": 1}`` を 1 行書く。
``POST /api/v1/shutdown`` か、親プロセス（--parent-pid）の終了で止まる。

* 同じデータフォルダーで 2 つ目のバックエンドは動かさない（``<データ>\\app\\backend.lock`` を取る。再起動の直後は
  前のプロセスの終了を最大 15 秒待つ）。取れなければ終了コード 3（WPF は専用の文を出す）。
* 起動の直後（子プロセスを作る前）に自分用の Job Object（KILL_ON_JOB_CLOSE）に入る。どう終わっても、作業プロセス
  （TRELLIS の生成など）を残さない。
* 終了: 順番待ちと実行中の取り消せるジョブは取り消し、未完了のジョブを最大 10 秒、実行中の取り消せないジョブ（導入など）は
  最大 60 秒待つ（WPF はそれより長く待ってから強制終了する）。
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gdskin_backend import paths  # noqa: E402

paths.ensure_sys_path()

from gdskin_backend import API_VERSION, __version__, logs  # noqa: E402
from gdskin_backend.i18n import tr  # noqa: E402
from gdskin_backend.procutil import watch_parent  # noqa: E402
from gdskin_backend.server import BackendServer  # noqa: E402
from gdskin_backend.service import Service  # noqa: E402

# 終了の待ち時間（秒）。WPF（Services/BackendHost.cs）はこれより長く待ってから強制終了する（12 秒・65 秒）
SHUTDOWN_WAIT = 10.0
UNCANCELLABLE_WAIT = 60.0
# データフォルダーのロック（多重起動の防止。2026-10-17 監査 C105）。WPF の「バックエンドを再起動」は前のプロセスの
# 終了を待たないので、取れるまで少し待つ
LOCK_WAIT = 15.0
LOCK_RETRY = 0.5
EXIT_LOCKED = 3

_own_job = None         # 自分用の Job Object のハンドル（プロセスが終わるまで閉じない）


def acquire_data_lock(home: str, wait: float | None = None, retry: float = LOCK_RETRY):
    """``<データ>\\app\\backend.lock`` の先頭 1 バイトをロックする → 開いたファイル（プロセスが終わるまで持つ）。

    wait 秒（既定 LOCK_WAIT）たっても取れなければ None。ロックはプロセスが終われば（強制終了でも）OS が外す。"""
    if sys.platform != "win32":     # pragma: no cover  開発時の確認用（Windows 以外はロックしない）
        return open(os.devnull, "rb")
    import msvcrt
    d = os.path.join(home, "app")
    os.makedirs(d, exist_ok=True)
    f = open(os.path.join(d, "backend.lock"), "a+b")
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(retry)


def enter_own_job() -> bool:
    """自分用の Job Object（KILL_ON_JOB_CLOSE、子の抜け出しは許さない）を作り、自分自身を入れる（2026-10-17 監査 C107）。

    WPF はバックエンドを起動した後で自分の Job に入れるので、venv の python.exe（本物の Python を子として起動する
    転送用の実行ファイル）を指定したときなどに、本物のバックエンドが WPF の Job の外になりうる。そのうえ転送用の
    実行ファイルの Job は子の抜け出し（SILENT_BREAKAWAY_OK）を許すので、そのままでは作業プロセス（TRELLIS の生成など）が
    どの Job にも入らずに残りうる。ここで作る Job には子を作る前に入り、抜け出しを許さないので、その後の子孫は全てこの
    Job に入る（入れ子の Job では、直近の Job が抜け出しを許さなければ子は抜け出さない）。バックエンドがどう終わっても、
    ハンドルが閉じて子孫が終了する。入れなければ False（親の監視と WPF の Job だけで続ける）。"""
    global _own_job
    if sys.platform != "win32" or _own_job is not None:
        return _own_job is not None
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    h = k32.CreateJobObjectW(None, None)
    if not h:
        err = ctypes.get_last_error()
        logs.warning(tr(f"自分用の Job Object を作れません（WinError {err}）。親の監視だけで続けます",
                        f"Cannot create the backend's own Job Object (WinError {err}). "
                        f"Continuing with only the parent process monitoring"))
        return False
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
            and k32.AssignProcessToJobObject(h, k32.GetCurrentProcess())):
        err = ctypes.get_last_error()
        logs.warning(tr(f"自分用の Job Object に入れません（WinError {err}）。親の監視だけで続けます",
                        f"Cannot join the backend's own Job Object (WinError {err}). "
                        f"Continuing with only the parent process monitoring"))
        k32.CloseHandle(h)
        return False
    _own_job = h
    return True


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--parent-pid", type=int, default=0)
    a = ap.parse_args(argv)
    token = os.environ.get("GDSKIN_BACKEND_TOKEN", "")
    if len(token) < 16:
        print(tr("GDSKIN_BACKEND_TOKEN（16 文字以上）が必要です", "GDSKIN_BACKEND_TOKEN (16 or more characters) is required"),
              file=sys.stderr)
        return 2
    os.environ.pop("GDSKIN_BACKEND_TOKEN", None)       # 子プロセスに渡さない
    enter_own_job()                                      # 子プロセスを作る前に
    lock = acquire_data_lock(paths.home())
    if lock is None:
        # 行頭の GDSKIN_BACKEND_LOCKED は表示言語に依らない目印（文だけを表示言語にする）
        msg = tr(f"同じデータフォルダー（{paths.home()}）で別のバックエンドが動いています。"
                 f"{LOCK_WAIT:g} 秒待っても終わらないので、起動をやめます",
                 f"Another backend is running with the same data folder ({paths.home()}). "
                 f"It did not exit within {LOCK_WAIT:g} seconds, so this one will not start")
        print("GDSKIN_BACKEND_LOCKED " + msg, file=sys.stderr, flush=True)
        logs.warning(msg)
        return EXIT_LOCKED
    # ロックは、ここから先がどう終わっても（想定外の例外・Service/BackendServer の作成の失敗でも）最後に閉じる。
    # 例外で終わるときもジョブの片付けとサーバーの停止の後なので、正常の終了と同じ扱い（点検 C1_tests-06）
    try:
        # アクセス違反などのネイティブの異常でも Python のスタックを標準エラー（WPF のパイプ。WPF が末尾を app のログに
        # 書く）に残す（R17A-01）
        try:
            faulthandler.enable()
        except (AttributeError, ValueError, OSError, RuntimeError):   # RuntimeError は sys.stderr が None のとき
            pass
        try:
            svc = Service()             # 起動時の後始末（処理中のまま残った下書きの「中断」など）はロックを取った後で
            srv = BackendServer(svc, token, a.port)
        except Exception:
            # 起動の前の想定外の失敗も backend-*.log に残す（以前は標準エラーだけで、ログは 0 行だった。点検 R09B-03）
            logs.error(tr("想定外の例外でバックエンドを起動できません: ", "The backend cannot start because of an unexpected exception: ")
                       + traceback.format_exc()[-3000:], code="INTERNAL")
            raise
        if a.parent_pid:
            def parent_gone():
                logs.warning(tr(f"親プロセス（PID {a.parent_pid}）が終了したので、バックエンドを止めます",
                                f"The parent process (PID {a.parent_pid}) has exited, so the backend is stopping"))
                srv.stop()
            watch_parent(a.parent_pid, parent_gone)
        print("GDSKIN_BACKEND_READY " + json.dumps({"port": srv.port, "pid": os.getpid(), "version": __version__,
                                                     "api": API_VERSION}), flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            svc.shutdown()
        except Exception:
            # 想定外の例外で止まるときは backend-*.log にも残す（標準エラーだけでは、WPF が読めなかったときに原因が残らない）
            logs.error(tr("想定外の例外でバックエンドを終了します: ", "The backend is exiting because of an unexpected exception: ")
                       + traceback.format_exc()[-3000:], code="INTERNAL")
            raise
        finally:
            # 取り消したジョブの片付けを待つ。実行中の取り消せないジョブ（導入など）は途中で切らないよう長めに待つ
            if not svc.jobs.wait_idle(SHUTDOWN_WAIT, uncancellable_timeout=UNCANCELLABLE_WAIT):
                logs.warning(tr("終了を待ちきれなかったジョブがあります: ", "Some jobs did not finish before the shutdown: ")
                             + ", ".join(tr(f"{j.id}（{j.type}）", f"{j.id} ({j.type})") for j in svc.jobs.active()))
            srv.close()
        logs.info(tr("バックエンドを終了しました", "The backend has shut down"))
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
