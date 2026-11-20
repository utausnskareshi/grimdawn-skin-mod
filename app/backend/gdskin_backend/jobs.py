"""ジョブ・順番待ちの列・進捗のイベント（docs/app_architecture.md 3 章）。

* ジョブの種類ごとに処理（handler）と列（lane）を登録する。列ごとに 1 件ずつ順に実行する。
* 処理は ``handler(ctx: JobContext, params: dict) -> dict``。:class:`~gdskin_backend.errors.BackendError` で分類済みの
  エラーを、:class:`~gdskin_backend.errors.JobCancelled` で取消を伝える。それ以外の例外は INTERNAL にする。
* 子プロセスは :meth:`JobContext.run_process` で起動する。取消のときは子孫ごと終了する。
* イベント（進捗・ログ・状態）は通し番号つきでジョブに貯め、``GET /jobs/{id}/events?after=N&wait=S`` で返す。
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import time
import traceback
from collections import OrderedDict, deque

from . import logs
from .errors import BackendError, JobCancelled, classify_output, failure_text
from .i18n import tr
from .procutil import ManagedProcess

TERMINAL = ("succeeded", "failed", "cancelled")
MAX_EVENTS = 4000
MAX_JOBS = 200


def _quiet(fn, *args, **kw) -> None:
    """ログの書き込み（ファイルへの追記）。空き容量の不足・ほかのプロセスのロックなどで失敗しても、ジョブの状態の
    切り替えや列のスレッドを止めない（以前は失敗の記録の失敗で列のスレッドが終わり、ジョブが実行中のまま残った。点検 R09A-01）。"""
    try:
        fn(*args, **kw)
    except (OSError, ValueError):
        pass


class Job:
    def __init__(self, job_id: str, type_: str, params: dict, summary: dict, lane: str, cancellable: bool):
        self.id = job_id
        self.type = type_
        self.params = params
        self.public_params = summary
        self.lane = lane
        self.cancellable = cancellable
        self.state = "queued"
        self.progress = 0.0
        self.stage: str | None = None
        self.stage_detail: dict | None = None
        self.message: str | None = None
        self.result: dict | None = None
        self.error: dict | None = None
        self.created_at = logs.now_iso()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.cancel_requested = False
        self.warnings: list[str] = []
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._cond = threading.Condition()
        self._procs: set[ManagedProcess] = set()
        self._last_progress_emit = (0.0, None, 0.0)

    # --- イベント ------------------------------------------------------------------------
    def emit(self, kind: str, **data) -> dict:
        with self._cond:
            self._seq += 1
            ev = {"seq": self._seq, "time": logs.now_iso(), "kind": kind, **data}
            self._events.append(ev)
            self._cond.notify_all()
            return ev

    def events_after(self, after: int, wait: float = 0.0) -> tuple[list[dict], int, str]:
        deadline = time.monotonic() + max(0.0, min(wait, 30.0))
        with self._cond:
            while True:
                evs = [e for e in self._events if e["seq"] > after]
                if evs or self.state in TERMINAL:
                    return evs, self._seq, self.state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], self._seq, self.state
                self._cond.wait(remaining)

    def set_progress(self, progress: float | None = None, stage: str | None = None, detail: dict | None = None,
                     message: str | None = None, force: bool = False) -> None:
        if progress is not None:
            self.progress = max(self.progress, min(1.0, float(progress)))   # 進捗は戻さない
        if stage is not None and stage != self.stage:
            self.stage = stage
            force = True
        if detail is not None:
            if detail != self.stage_detail:
                force = True
            self.stage_detail = detail
        if message is not None:
            self.message = message
        last_p, last_stage, last_t = self._last_progress_emit
        now = time.monotonic()
        if force or self.progress - last_p >= 0.005 or now - last_t >= 1.0:
            self._last_progress_emit = (self.progress, self.stage, now)
            self.emit("progress", progress=round(self.progress, 4), stage=self.stage, detail=self.stage_detail,
                      message=self.message)

    def summary(self) -> dict:
        return {"id": self.id, "type": self.type, "state": self.state, "progress": round(self.progress, 4),
                "stage": self.stage, "stage_detail": self.stage_detail, "message": self.message,
                "cancellable": self.cancellable, "lane": self.lane, "params": self.public_params,
                "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
                "warnings": list(self.warnings), "error": self.error, "last_seq": self._seq}

    def detail(self) -> dict:
        return {**self.summary(), "result": self.result}


class JobContext:
    """処理に渡す道具。"""

    def __init__(self, job: Job, manager: "JobManager"):
        self.job = job
        self.manager = manager
        self.log = logs.JobLog(job.id)

    # --- 進捗とログ ------------------------------------------------------------------------
    def progress(self, progress: float | None = None, stage: str | None = None, detail: dict | None = None,
                 message: str | None = None) -> None:
        self.job.set_progress(progress, stage, detail, message)

    def info(self, message: str, stage: str | None = None) -> None:
        rec = logs.info(message, job=self.job.id, stage=stage or self.job.stage)
        self.log.write(message)
        self.job.emit("log", level="info", message=rec["message"])

    def warn(self, message: str, stage: str | None = None, code: str | None = None) -> None:
        rec = logs.warning(message, job=self.job.id, stage=stage or self.job.stage, code=code)
        self.log.write("WARNING: " + message)
        self.job.warnings.append(rec["message"])
        self.job.emit("log", level="warning", message=rec["message"], code=code)

    def check_cancel(self) -> None:
        if self.job.cancel_requested:
            raise JobCancelled()

    # --- 子プロセス --------------------------------------------------------------------------
    def run_process(self, cmd: list[str], on_line=None, env: dict | None = None, cwd: str | None = None,
                    keep_lines: int = 600) -> tuple[int, str]:
        """子プロセスを最後まで実行する → (終了コード, 出力の末尾)。取り消されたら JobCancelled。"""
        self.check_cancel()
        tail: deque = deque(maxlen=keep_lines)

        def handle(line: str) -> None:
            tail.append(line)
            self.log.write(line)
            if on_line is not None:
                on_line(line)

        self.log.write("$ " + " ".join(cmd))
        proc = ManagedProcess(cmd, handle, env=env, cwd=cwd)
        with self.manager._lock:
            self.job._procs.add(proc)
        try:
            if self.job.cancel_requested:        # 起動の直後に取消が来た
                proc.kill_tree()
            rc = proc.wait()
        finally:
            with self.manager._lock:
                self.job._procs.discard(proc)
        if proc.killed or self.job.cancel_requested:
            raise JobCancelled()
        return rc, "\n".join(tail)

    def run_worker(self, task: str, params: dict, progress_range: tuple[float, float] = (0.0, 1.0),
                   default_error: str = "INTERNAL", stage_prefix: str | None = None, python: str | None = None,
                   script: str | None = None, env: dict | None = None) -> dict:
        """作業プロセス（既定は worker.py をバックエンドの Python で）で task を実行し、結果の data を返す。

        python / script を渡すと、同じ取り決め（@@GDSKIN の行）の別のスクリプトを別の Python で動かす（背景除去の確認は
        TRELLIS 実行環境の Python で bg_preview.py）。"""
        import os
        import sys
        from . import paths
        pdir = os.path.join(paths.work_dir(), "_params")
        os.makedirs(pdir, exist_ok=True)
        ppath = os.path.join(pdir, f"{self.job.id}-{task}.json")
        with open(ppath, "w", encoding="utf-8", newline="\n") as f:
            json.dump(params, f, ensure_ascii=False)
        lo, hi = progress_range
        state = {"result": None, "error": None}

        def on_line(line: str) -> None:
            if not line.startswith("@@GDSKIN "):
                return
            try:
                msg = json.loads(line[len("@@GDSKIN "):])
            except ValueError:
                return
            t = msg.get("type")
            if t == "progress":
                p = msg.get("progress")
                self.progress(None if p is None else lo + (hi - lo) * float(p), msg.get("stage"), msg.get("detail"))
            elif t == "log":
                (self.warn if msg.get("level") == "warning" else self.info)(str(msg.get("message", "")))
            elif t == "result":
                state["result"] = msg.get("data") or {}
            elif t == "error":
                state["error"] = msg

        cmd = [python or sys.executable, "-I", "-X", "utf8", script or paths.WORKER_SCRIPT, task, ppath]
        try:
            rc, out = self.run_process(cmd, on_line, env=env)
        finally:
            try:
                os.remove(ppath)
            except OSError:
                pass
        if state["error"]:
            e = state["error"]
            raise BackendError(e.get("code") or default_error,
                               e.get("message") or tr("作業プロセスでエラー", "Error in the worker process"),
                               e.get("detail"), e.get("params"))
        if rc != 0 or state["result"] is None:
            code = classify_output(out, default_error)
            raise BackendError(code, tr(f"作業プロセス（{task}）が終了コード {rc} で終わりました",
                                        f"The worker process ({task}) exited with code {rc}"),
                               failure_text(out, 30), {"exit_code": rc})
        return state["result"]


class _Handler:
    def __init__(self, fn, lane: str, cancellable: bool, summarize=None):
        self.fn = fn
        self.lane = lane
        self.cancellable = cancellable
        self.summarize = summarize or (lambda p: {})


class JobManager:
    def __init__(self):
        self._handlers: dict[str, _Handler] = {}
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lanes: dict[str, queue.Queue] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._stopping = False
        self._before_start = None       # テスト用: 列から取り出した直後（実行中に切り替える前）に job を渡して呼ぶ

    def register(self, type_: str, fn, lane: str, cancellable: bool = True, summarize=None) -> None:
        self._handlers[type_] = _Handler(fn, lane, cancellable, summarize)
        if lane not in self._lanes:
            q: queue.Queue = queue.Queue()
            self._lanes[lane] = q
            t = threading.Thread(target=self._lane_loop, args=(lane, q), name=f"lane-{lane}", daemon=True)
            t.start()
            self._threads.append(t)

    @property
    def types(self) -> list[str]:
        return sorted(self._handlers)

    def submit(self, type_: str, params: dict | None = None) -> Job:
        if self._stopping:
            raise BackendError("BUSY", tr("バックエンドを終了しています", "The backend is shutting down"))
        h = self._handlers.get(type_)
        if h is None:
            raise BackendError("INVALID_INPUT", tr(f"ジョブの種類が不明です: {type_}（{', '.join(self.types)}）",
                                                   f"Unknown job type: {type_} ({', '.join(self.types)})"))
        if params is None:
            params = {}
        elif not isinstance(params, dict):        # dict() に任せると ["ab"] などが黙って通る（2026-10-17 監査 C102）
            raise BackendError("INVALID_INPUT", tr("ジョブの引数（params）はオブジェクトで指定してください",
                                                   "The job parameters (params) must be an object"))
        params = dict(params)
        try:
            summary = h.summarize(params)
        except (TypeError, AttributeError, ValueError) as e:
            # 要約は params の中身の型を前提にする（input に .get、ids に len など）。JSON として正しいが型の違う値は
            # INVALID_INPUT（400）にする。以前は想定外のエラー（INTERNAL、ログに ERROR）になっていた（点検 R09A-01・X1X-01）
            raise BackendError("INVALID_INPUT", tr(f"ジョブの引数の型が正しくありません（{type_}）",
                                                   f"The job parameters have the wrong type ({type_})"),
                               f"{type(e).__name__}: {e}", {"reason": "job_params", "type": type_}) from e
        job = Job("j" + time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(3), type_, params,
                  summary, h.lane, h.cancellable)
        with self._lock:
            # 終了の要求（shutdown）と同じロックで確かめる。終了の後に順番待ちのジョブが増えないように
            if self._stopping:
                raise BackendError("BUSY", tr("バックエンドを終了しています", "The backend is shutting down"))
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS:
                old_id, old = next(iter(self._jobs.items()))
                if old.state not in TERMINAL:
                    break
                del self._jobs[old_id]
            job.emit("state", state="queued")
        _quiet(logs.info, tr(f"ジョブを受け付けました: {type_}", f"Job accepted: {type_}"),
               job=job.id)     # 失敗しても列には必ず入れる（R09A-01）
        self._lanes[h.lane].put(job)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise BackendError("NOT_FOUND", tr(f"ジョブがありません: {job_id}", f"Job not found: {job_id}"))
        return job

    def list(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    def active(self, lane: str | None = None) -> list[Job]:
        return [j for j in self.list() if j.state not in TERMINAL and (lane is None or j.lane == lane)]

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        # 状態の判定から取消の記録までを、列のスレッドの「順番待ち → 実行中」の切り替え（_lane_loop）と同じロックで行う。
        # 以前は判定がロックの外にあり、「取り消した」と返したジョブが実行されることがあった（2026-10-17 監査 C100）
        with self._lock:
            if job.state in TERMINAL:
                return job
            if not job.cancellable and job.state == "running":
                raise BackendError("NOT_CANCELLABLE", tr("このジョブは途中で取り消せません（mods/ を中途半端な状態にしないため）",
                                                         "This job cannot be cancelled partway (to avoid leaving mods/ "
                                                         "in an incomplete state)"))
            job.cancel_requested = True
            procs = list(job._procs)
            if job.state == "queued":
                job.error = {"code": "CANCELLED", "message": tr("取り消されました", "Cancelled")}
                self._finish(job, "cancelled")
        for p in procs:
            p.kill_tree()
        _quiet(logs.info, tr("取り消しを要求しました", "Cancellation requested"), job=job.id)
        return job

    def stop_accepting(self) -> list[str]:
        """終了の第 1 段（すぐに終わる）: 新しいジョブを断り、順番待ちのジョブを種類を問わず取り消す。

        戻り値は、実行中の取り消せないジョブ（導入・アンインストール・復元・取り込み）の ID。これらは途中で止めると
        mods/ や装備一覧が中途半端になるので、終わるまで待つ（:meth:`wait_idle`）。以前は順番待ちの取り消せないジョブも
        終了の要求の後に始まり、WPF の強制終了で途中で切られることがあった（2026-10-17 監査 C99）。"""
        with self._lock:
            self._stopping = True
            for job in list(self._jobs.values()):
                if job.state == "queued":
                    job.cancel_requested = True
                    job.error = {"code": "CANCELLED", "message": tr("バックエンドの終了のため取り消されました",
                                                                    "Cancelled because the backend is shutting down")}
                    self._finish(job, "cancelled")
            return [j.id for j in self._jobs.values() if j.state == "running" and not j.cancellable]

    def shutdown(self) -> list[str]:
        """終了: :meth:`stop_accepting` の後、実行中の取り消せるジョブを取り消し、列のスレッドを止める。

        戻り値は :meth:`stop_accepting` と同じ（終わるのを待つ必要がある、実行中の取り消せないジョブの ID）。"""
        waiting = self.stop_accepting()
        for job in self.active():
            if job.cancellable:
                try:
                    self.cancel(job.id)
                except BackendError:
                    pass
        for q in self._lanes.values():
            q.put(None)
        return waiting

    def wait_idle(self, timeout: float = 10.0, uncancellable_timeout: float | None = None) -> bool:
        """未完了のジョブが無くなるまで待つ（最大 timeout 秒）。uncancellable_timeout を渡すと、実行中の取り消せない
        ジョブが残っている間は、その秒数まで待ち続ける（終了の途中で導入などを切らないため。WPF はこれより長く待つ）。"""
        start = time.monotonic()
        limit = max(timeout, uncancellable_timeout or 0.0)
        while True:
            act = self.active()
            if not act:
                return True
            elapsed = time.monotonic() - start
            if elapsed >= limit:
                return False
            if elapsed >= timeout and not any(j.state == "running" and not j.cancellable for j in act):
                return False
            time.sleep(0.05)

    def join(self, timeout: float = 10.0) -> list[str]:
        """:meth:`shutdown` の後、列のスレッドが終わるまで待つ（スレッドごとに最大 timeout 秒）→ 終わらなかったスレッドの
        名前（空なら全て終わった）。_run は状態を終わりにした後にもログを書くので、書き込みの終わりまで待つときに使う
        （テストの後始末など。R23T-01）。"""
        for t in self._threads:
            t.join(timeout=timeout)
        return [t.name for t in self._threads if t.is_alive()]

    # --- 実行 -------------------------------------------------------------------------------
    def _finish(self, job: Job, state: str) -> None:
        job.state = state
        job.finished_at = logs.now_iso()
        if state == "succeeded":
            job.set_progress(1.0, force=True)
        job.emit("state", state=state, error=job.error)

    def _lane_loop(self, lane: str, q: queue.Queue) -> None:
        while True:
            job = q.get()
            if job is None:
                return
            if self._before_start is not None:      # テスト用のフック（取消と開始の重なりを再現する）
                self._before_start(job)
            # 「まだ順番待ちか」の判定と実行中への切り替えを、cancel() と同じロックの中で行う（2026-10-17 監査 C100）
            with self._lock:
                if job.state != "queued":       # 順番待ちの間に取り消された
                    continue
                if self._stopping:              # 終了の要求の後は始めない（stop_accepting が取り消し済みのはず。念のため）
                    job.cancel_requested = True
                    job.error = {"code": "CANCELLED", "message": tr("バックエンドの終了のため取り消されました",
                                                                    "Cancelled because the backend is shutting down")}
                    self._finish(job, "cancelled")
                    continue
                job.state = "running"
                job.started_at = logs.now_iso()
            try:
                self._run(job)
            except BaseException as e:  # noqa: BLE001  最後の守り: _run から抜けた例外でも列のスレッドを止めない（R09A-01）
                with self._lock:
                    if job.state not in TERMINAL:
                        job.error = {"code": "INTERNAL", "message": f"{type(e).__name__}: {e}",
                                     "detail": failure_text(traceback.format_exc(), 30), "params": {}}
                        self._finish(job, "failed")
                _quiet(logs.error, tr(f"ジョブの後始末に失敗しました: {type(e).__name__}: {e}",
                                      f"Failed to clean up after the job: {type(e).__name__}: {e}"), job=job.id, code="INTERNAL")

    def _run(self, job: Job) -> None:
        """実行中（_lane_loop が切り替え済み）のジョブの処理を最後まで行う。

        ログの書き込みは :func:`_quiet` を通す。書き込みの失敗で状態の切り替え（_finish）を飛ばさない（R09A-01）。"""
        h = self._handlers[job.type]
        ctx = None
        job.emit("state", state="running")
        _quiet(logs.info, tr(f"ジョブを開始しました: {job.type}", f"Job started: {job.type}"), job=job.id)
        try:
            ctx = JobContext(job, self)     # ジョブのログのフォルダーを作れない場合もジョブの失敗にする
            ctx.check_cancel()          # 実行中に切り替えた直後に取消が来た（取り消せるジョブだけ。処理を始めずに取り消す）
            result = h.fn(ctx, job.params)
            job.result = result if isinstance(result, dict) else {"value": result}
            self._finish(job, "succeeded")
        except JobCancelled:
            job.error = {"code": "CANCELLED", "message": tr("取り消されました", "Cancelled")}
            self._finish(job, "cancelled")
            _quiet(logs.info, tr(f"ジョブを取り消しました: {job.type}", f"Job cancelled: {job.type}"), job=job.id)
        except BackendError as e:
            job.error = e.to_dict()
            if ctx is not None:
                _quiet(ctx.log.write, f"ERROR {e.code}: {e.message}\n{e.detail or ''}")
            _quiet(logs.error, e.message, job=job.id, stage=job.stage, code=e.code)
            self._finish(job, "failed")
        except Exception as e:   # noqa: BLE001  想定外の例外もジョブの失敗にとどめ、サービスは動かし続ける
            tb = traceback.format_exc()
            job.error = {"code": "INTERNAL", "message": f"{type(e).__name__}: {e}", "detail": failure_text(tb, 30),
                         "params": {}}
            if ctx is not None:
                _quiet(ctx.log.write, tb)
            _quiet(logs.error, tr(f"想定外のエラー: {type(e).__name__}: {e}", f"Unexpected error: {type(e).__name__}: {e}"),
                   job=job.id, stage=job.stage, code="INTERNAL")
            self._finish(job, "failed")
        else:
            # 完了の記録は try の外で行う（記録の失敗で、成功を送ったジョブが failed に変わらないように。R09A-01）
            _quiet(logs.info, tr(f"ジョブが完了しました: {job.type}", f"Job completed: {job.type}"), job=job.id)
