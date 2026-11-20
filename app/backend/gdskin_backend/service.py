"""部品をまとめたサービス（HTTP の窓口から呼ばれる）。"""
from __future__ import annotations

import os
import threading
import time

from . import (__version__, API_VERSION, datastores, envcheck, equipment_package, library_ops, logs, mod_package, modops,
               paths, pipeline, providers, setup)
from . import candidates
from . import variant
from .drafts import DraftStore
from .errors import BackendError
from .i18n import tr
from .jobs import JobManager
from .settings import Settings, resolve_trellis_root
from .setup import maintenance as setup_maintenance


class Service:
    def __init__(self, selftest: bool | None = None):
        self.started_at = logs.now_iso()
        self.settings = Settings()
        self.jobs = JobManager()
        self.drafts = DraftStore()
        self.mod_lock = threading.Lock()
        self._env_cache: tuple[float, dict | None] = (0.0, None)
        self._env_lock = threading.Lock()
        j = self.jobs
        j.register("generate", lambda ctx, p: pipeline.run_generate(self, ctx, p), "gpu", True, pipeline.summarize_generate)
        j.register("convert", lambda ctx, p: pipeline.run_reconvert(self, ctx, p), "gpu", True,
                   lambda p: {"draft_id": p.get("draft_id"), "yaw_deg": p.get("yaw_deg")})
        j.register("bg_preview", lambda ctx, p: pipeline.run_bg_preview(self, ctx, p), "preview", True,
                   pipeline.summarize_bg_preview)
        # 候補を選ぶ・保存した生成結果から GLB を作り直す（SF06・SF14。2026-10-17 追加機能 U-A2）
        j.register("rebuild_glb", lambda ctx, p: candidates.run_rebuild(self, ctx, p), "gpu", True, candidates.summarize)
        # 装備の生成結果の形のまま、文章で見た目を作り直す（SF10。2026-10-17 追加機能 U-A3。画面では既定で出さない）
        j.register("variant", lambda ctx, p: variant.run_variant(self, ctx, p), "gpu", True, variant.summarize)
        j.register("import_external", lambda ctx, p: datastores.run_import(self, ctx, p), "mod", False,
                   lambda p: {"store": p.get("store")})
        j.register("refs", lambda ctx, p: modops.run_refs(self, ctx, p), "mod", True)
        # 保存済みの装備を、保存してある生成結果から変換し直す（3D 生成はやり直さない）
        j.register("reconvert_equipment", lambda ctx, p: library_ops.run_reconvert_equipment(self, ctx, p), "gpu", True,
                   library_ops.summarize_reconvert)
        j.register("build", lambda ctx, p: modops.run_build(self, ctx, p), "mod", True,
                   lambda p: {k: p.get(k) for k in ("languages", "include_dev") if k in p})
        j.register("install", lambda ctx, p: modops.run_install(self, ctx, p), "mod", False,
                   lambda p: {k: p.get(k) for k in ("build_id", "shared_id", "replace_foreign", "force")})
        j.register("uninstall", lambda ctx, p: modops.run_uninstall(self, ctx, p), "mod", False,
                   lambda p: {k: p.get(k) for k in ("force",)})
        j.register("restore", lambda ctx, p: modops.run_restore(self, ctx, p), "mod", False,
                   lambda p: {k: p.get(k) for k in ("backup_id", "force")})
        # 共有ファイル（docs/app_architecture.md 15 章）。ファイルの場所は利用者のフォルダー名を含むので要約に入れない
        j.register("export_equipment", lambda ctx, p: equipment_package.run_export(self, ctx, p), "mod", True,
                   lambda p: {"count": len(p.get("ids") or []), "include_inputs": bool(p.get("include_inputs"))})
        j.register("import_equipment", lambda ctx, p: equipment_package.run_import(self, ctx, p), "mod", False,
                   lambda p: {"package_id": p.get("package_id"), "replace": len(p.get("replace") or [])})
        j.register("export_mod", lambda ctx, p: mod_package.run_export(self, ctx, p), "mod", True,
                   lambda p: {"build_id": p.get("build_id")})
        # 初回セットアップ（TRELLIS 実行環境とモデルの取得。docs/packaging.md 4 章）
        j.register("setup", lambda ctx, p: setup.run_install(self, ctx, p), "setup", True, setup.summarize)
        # 設定画面の TRELLIS 実行環境の削除・照合し直し・修復（点検 SF04・SF16。アプリが作った取得先だけ。列 setup で
        # 動くので、実行中は 3D 生成・背景除去の確認が始まらない）
        j.register("setup_remove", lambda ctx, p: setup_maintenance.run_remove(self, ctx, p), "setup", True,
                   setup_maintenance.summarize_remove)
        j.register("setup_repair", lambda ctx, p: setup_maintenance.run_repair(self, ctx, p), "setup", True,
                   setup_maintenance.summarize_repair)
        if selftest if selftest is not None else os.environ.get("GDSKIN_WORKER_SELFTEST") == "1":
            j.register("selftest", self._selftest, "test", True, lambda p: dict(p))
        n = logs.cleanup_old()
        logs.info(tr(f"バックエンドを起動しました（版 {__version__}、データ {paths.home()}、古いログ {n} 件を削除）",
                     f"Backend started (version {__version__}, data {paths.home()}, {n} old log file(s) deleted)"))
        self._startup_cleanup()

    def _startup_cleanup(self) -> None:
        """起動時の後始末（2026-10-17 監査 C104）。前のバックエンドが強制終了・異常終了したときの残りを片付ける。

        (1) 処理中のまま残った下書きを「中断」（failed・INTERRUPTED）にする。消さない（利用者の判断 2026-10-17）。
        (2) 作業プロセスの引数ファイル（``work\\_params``。変換の引数はプロンプトを含む）の残りを消す。
        (3) 共有ファイルの展開の途中で残った一時フォルダー（``tmp\\received-*``・``import-*``）のうち 1 日より古いものを
        消す（2026-10-17 監査 C98。package_io.cleanup_scratch）。
        (4) 装備のアセットの入れ替えの途中で残ったもの（``assets.new``・``assets.old``・``assets.failed``）を片付け、記録と
        一致するアセットに戻す（MOD を作らない操作（書き出し・共有ファイルの作成）の前にも直すため。2026-10-27 点検 X6X-04。
        Library.recover_interrupted_assets）。
        まだジョブを受け付けていない時点で呼ぶ。同じデータフォルダーのバックエンドは 1 つだけという前提
        （run_backend.py のロックと WPF の Mutex）。後始末に失敗しても起動は止めない（ログに残して続ける）。"""
        try:
            ids = self.drafts.recover_interrupted()
        except Exception as e:  # noqa: BLE001 — 後始末の失敗で起動を止めない（下書きの中身は出さない）
            ids = []
            detail = f"{type(e).__name__}: {e}" if isinstance(e, OSError) else type(e).__name__
            logs.warning(tr(f"処理中のまま残った下書きを確かめられません: {detail}",
                            f"Could not check for drafts left in progress: {detail}"))
        removed = 0
        pdir = os.path.join(paths.work_dir(), "_params")
        try:
            names = os.listdir(pdir) if os.path.isdir(pdir) else []
        except OSError as e:
            names = []
            logs.warning(tr(f"前回の作業プロセスの引数ファイルを確かめられません: {type(e).__name__}: {e}",
                            f"Could not check the argument files of the previous worker processes: {type(e).__name__}: {e}"))
        for name in names:
            try:
                os.remove(os.path.join(pdir, name))
                removed += 1
            except OSError:
                pass
        if ids:
            logs.warning(tr(f"処理の途中で終わった下書き {len(ids)} 件を「中断」にしました: {', '.join(ids)}",
                            f"Marked {len(ids)} draft(s) whose processing ended partway as interrupted: {', '.join(ids)}"),
                         code="INTERRUPTED")
        if removed:
            logs.info(tr(f"前回の作業プロセスの引数ファイル {removed} 件を消しました（work\\_params）",
                         f"Deleted {removed} argument file(s) left by the previous worker processes (work\\_params)"))
        try:
            from . import package_io
            scratch = package_io.cleanup_scratch(self.home())
        except Exception as e:  # noqa: BLE001 — 後始末の失敗で起動を止めない
            scratch = []
            logs.warning(tr(f"共有ファイルの展開の一時フォルダーを片付けられません: {type(e).__name__}: {e}",
                            f"Could not clean up the temporary folders for extracting share files: {type(e).__name__}: {e}"))
        if scratch:
            logs.info(tr(f"共有ファイルの展開の途中で残った一時フォルダー {len(scratch)} 件を消しました（tmp）",
                         f"Deleted {len(scratch)} temporary folder(s) left over from extracting share files (tmp)"))
        rec = {}
        if os.path.isfile(os.path.join(self.library_dir(), "library.json")):     # ライブラリが無いのは初回の起動など
            try:
                from modbuilder.library import Library
                with self.mod_lock:
                    rec = Library.open(self.library_dir()).recover_interrupted_assets()
            except Exception as e:  # noqa: BLE001 — 後始末の失敗で起動を止めない（LibraryError・OSError など）
                logs.warning(tr(f"装備のアセットの差し替えの残りを確かめられません: {type(e).__name__}: {e}",
                                f"Could not check for leftovers of replacing equipment assets: {type(e).__name__}: {e}"))
        if rec:
            items = ", ".join(f"{k} ({v})" for k, v in sorted(rec.items()))
            logs.warning(tr(f"アセットの差し替えの途中で止まった装備 {len(rec)} 件を片付けました: {items}",
                            f"Cleaned up {len(rec)} equipment item(s) whose asset replacement stopped partway: {items}"))

    # --- 場所 ---------------------------------------------------------------------------------
    def home(self) -> str:
        return paths.home()

    def library_dir(self) -> str:
        return paths.library_dir()

    def builds_dir(self) -> str:
        return paths.builds_dir()

    def refs_dir(self) -> str:
        return paths.refs_dir()

    # --- 情報 ---------------------------------------------------------------------------------
    def health(self) -> dict:
        return {"version": __version__, "api": API_VERSION, "pid": os.getpid(), "started_at": self.started_at,
                "home": self.home(), "library": self.library_dir(), "jobs_active": len(self.jobs.active()),
                "dist": paths.DIST_INFO}

    def environment(self, refresh: bool = False) -> dict:
        with self._env_lock:
            t, cached = self._env_cache
            if cached is None or refresh or time.monotonic() - t > 60:
                cached = envcheck.check_all(self)
                cached["checked_at"] = logs.now_iso()
                self._env_cache = (time.monotonic(), cached)
            return cached

    def invalidate_environment(self) -> None:
        with self._env_lock:
            self._env_cache = (0.0, None)

    def providers(self) -> list[dict]:
        s = self.settings.get()
        return [p.describe(s) for p in providers.REGISTRY.values()]

    def trellis_root(self) -> str | None:
        return resolve_trellis_root(self.settings.get())

    def paths_info(self) -> dict:
        return {"home": self.home(), "library": self.library_dir(), "work": paths.work_dir(), "builds": self.builds_dir(),
                "refs": self.refs_dir(), "logs": paths.logs_dir(), "repo": paths.REPO_ROOT}

    # --- 装備・下書き -------------------------------------------------------------------------
    def library(self) -> list[dict]:
        return library_ops.list_equipment(self)

    def drafts_list(self) -> list[dict]:
        # 1 件の要約の失敗で一覧の全体（GET /drafts）を失敗させない。失敗した下書きはフォルダー名と例外の型だけを
        # ログに残して飛ばす（中身は出さない。点検 R10B-05）
        # 候補の色（候補の図を読んで測る。1 枚 20 ms ほど）は、画面が復元しうる下書き（status が ready で、装備として保存済みの
        # 記録 saved が無いもの。GenerateViewModel.InitAsync）だけで測る。ほかの下書きの color・color_notice は None
        # （下書きを残す設定で下書きが多いと、起動のたびの一覧が遅くなっていた。2026-11-18 点検 BC-02）
        out = []
        for d in self.drafts.list():
            try:
                out.append(d.summary(colors=d.data.get("status") == "ready" and not isinstance(d.data.get("saved"), dict)))
            except Exception as e:  # noqa: BLE001
                name = os.path.basename(d.dir)
                logs.warning(tr(f"下書き {name} を一覧に出せません: {type(e).__name__}",
                                f"Could not list draft {name}: {type(e).__name__}"))
        return out

    def update_settings(self, partial: dict) -> dict:
        new = self.settings.update(partial)
        self.invalidate_environment()
        logs.info(tr("設定を変更しました: ", "Settings changed: ") + ", ".join(sorted(partial)))
        return new

    # --- テスト用 -------------------------------------------------------------------------------
    def _selftest(self, ctx, p: dict) -> dict:
        if p.get("inprocess"):
            for i in range(int(p.get("steps", 3))):
                ctx.check_cancel()
                ctx.progress((i + 1) / int(p.get("steps", 3)), "selftest")
                time.sleep(float(p.get("delay", 0.0)))
            if p.get("mode") == "raise":
                raise RuntimeError("selftest in-process failure")
            if p.get("mode") == "error":
                raise BackendError(p.get("code", "CONVERSION_FAILED"), "selftest classified error")
            return {"inprocess": True}
        return ctx.run_worker("selftest", p, (0.0, 1.0), default_error="CONVERSION_FAILED")

    def shutdown(self) -> list[str]:
        """終了（順番待ちは種類を問わず取り消し、実行中の取り消せるジョブも取り消す）→ 終わるのを待つ必要がある、
        実行中の取り消せないジョブの ID。"""
        logs.info(tr("バックエンドを終了します", "Shutting down the backend"))
        waiting = self.jobs.shutdown()
        if waiting:
            logs.info(tr(f"実行中の取り消せないジョブが終わるのを待ちます: {', '.join(waiting)}",
                         f"Waiting for running jobs that cannot be cancelled to finish: {', '.join(waiting)}"))
        return waiting
