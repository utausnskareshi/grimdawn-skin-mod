"""子プロセスの起動・出力の読み取り・木ごとの終了と、親プロセス（WPF）の監視。

* 出力は標準出力と標準エラーを 1 本にまとめ、``\\n`` と ``\\r``（tqdm の進捗）で区切って 1 行ずつ渡す。
* 取消は ``taskkill /T /F`` で子孫ごと終了する（TRELLIS の生成プロセスは内部でさらにプロセスを作ることがある）。
* Windows ではコンソールの窓を出さない（CREATE_NO_WINDOW）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ManagedProcess:
    """子プロセス 1 つ。on_line(str) が出力の 1 行ごとに（読み取りのスレッドから）呼ばれる。"""

    def __init__(self, cmd: list[str], on_line, env: dict | None = None, cwd: str | None = None):
        self.cmd = cmd
        self.on_line = on_line
        self.killed = False
        full_env = dict(os.environ)
        for k, v in (env or {}).items():      # 値が None の変数は子プロセスに渡さない
            if v is None:
                full_env.pop(k, None)
            else:
                full_env[k] = v
        # -I（または -E）で起動する Python の子プロセスは PYTHON* の環境変数を無視するので、この 2 つは効かない。
        # 出力を UTF-8 にするには、コマンドに -X utf8 を付ける（作業プロセス・生成・セットアップの動作確認はそうしている。
        # 点検 C41）。-I を付けない子プロセスのために残す
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        full_env.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=full_env, cwd=cwd, creationflags=CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read, name=f"read-{self.proc.pid}", daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self) -> None:
        buf = b""
        stream = self.proc.stdout
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                idx = [i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0]
                if not idx:
                    break
                i = min(idx)
                line, buf = buf[:i], buf[i + 1:]
                if line.strip():
                    self._emit(line)
        if buf.strip():
            self._emit(buf)

    def _emit(self, raw: bytes) -> None:
        try:
            self.on_line(raw.decode("utf-8", errors="replace"))
        except Exception:   # noqa: BLE001  受け取り側の失敗で読み取りを止めない
            pass

    def wait(self, timeout: float | None = None) -> int:
        rc = self.proc.wait(timeout)
        self._reader.join(5)
        return rc

    def poll(self):
        return self.proc.poll()

    def kill_tree(self) -> None:
        if self.proc.poll() is not None:
            return
        self.killed = True
        kill_tree(self.proc.pid)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    else:   # pragma: no cover  Windows 以外は開発時の確認用
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass


def watch_parent(pid: int, on_exit, poll_seconds: float = 2.0) -> threading.Thread:
    """親プロセスの終了を監視し、終わったら on_exit() を呼ぶ（WPF が強制終了されたときにバックエンドも止まるように）。"""

    def run():
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if h:
                k32.WaitForSingleObject(h, 0xFFFFFFFF)
                k32.CloseHandle(h)
                on_exit()
                return
        while _pid_alive(pid):          # 開けなかったときは定期的に確かめる
            time.sleep(poll_seconds)
        on_exit()

    t = threading.Thread(target=run, name="parent-watch", daemon=True)
    t.start()
    return t


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259     # STILL_ACTIVE
    try:   # pragma: no cover
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_running(image_names: tuple[str, ...]) -> bool:
    """指定の実行ファイル名のプロセスがあるか（tasklist）。"""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace",
                             stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=15).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{n.lower()}"' in out for n in image_names)
