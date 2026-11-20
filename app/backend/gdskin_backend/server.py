"""HTTP の窓口（127.0.0.1 のみ、合言葉つき。docs/app_architecture.md 2 章）。

* ``Authorization: Bearer <合言葉>`` が無い・違う → 401
* ``Host`` が ``127.0.0.1:<ポート>`` / ``localhost:<ポート>`` 以外、または ``Origin`` ヘッダーがある → 403
  （外部のページ・DNS リバインディングからの呼び出しを断る）
* 応答は ``{"ok": true, "data": ...}`` / ``{"ok": false, "error": {...}}``
"""
from __future__ import annotations

import hmac
import json
import math
import re
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import consent, library_ops, logs, modops
from .errors import BackendError
from .i18n import tr

PREFIX = "/api/v1"
MAX_BODY = 1024 * 1024
LOG_PATH_MAX = 200              # ログに書く要求のパスの長さの上限（要求行は約 64 KB まで送れる。点検 R09B-01・X5X-01）
REJECT_LOG_INTERVAL = 60.0      # 認証の前の失敗（FORBIDDEN・UNAUTHORIZED）を同じ code でログに書く間隔（秒）

ROUTES: list[tuple[str, re.Pattern, str]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + PREFIX + pattern + "$"), fn.__name__))
        return fn
    return deco


# 問い合わせのパラメータ（?limit= など）の数値。誤りは INVALID_INPUT（400）にする。以前は int()/float() の例外が
# 「想定外のエラー」（INTERNAL、ログに ERROR）になっていた（2026-10-17 監査 C102）
def int_param(q: dict, name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = q.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise BackendError("INVALID_INPUT", tr(f"{name} は整数で指定してください", f"{name} must be an integer")) from None
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise BackendError("INVALID_INPUT", tr(
            f"{name} の範囲が不正です（{lo if lo is not None else ''}〜{hi if hi is not None else ''}）",
            f"{name} is out of range ({lo if lo is not None else ''} to {hi if hi is not None else ''})"))
    return v


def float_param(q: dict, name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = q.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise BackendError("INVALID_INPUT", tr(f"{name} は数値で指定してください", f"{name} must be a number")) from None
    if not math.isfinite(v) or (lo is not None and v < lo) or (hi is not None and v > hi):
        raise BackendError("INVALID_INPUT", tr(
            f"{name} の範囲が不正です（{lo if lo is not None else ''}〜{hi if hi is not None else ''}）",
            f"{name} is out of range ({lo if lo is not None else ''} to {hi if hi is not None else ''})"))
    return v


class Api:
    """各エンドポイント。戻り値が data になる。"""

    def __init__(self, service, server: "BackendServer"):
        self.svc = service
        self.server = server

    @route("GET", "/health")
    def health(self, q, body):
        return self.svc.health()

    @route("POST", "/shutdown")
    def shutdown(self, q, body):
        # すぐに終わる第 1 段（新しいジョブを断り、順番待ちを種類を問わず取り消す）はここで行い、終わるのを待つ必要が
        # ある実行中の取り消せないジョブの ID を返す（WPF はその間、終了を長めに待つ）。取り消せるジョブの取消
        # （子プロセスの終了）と HTTP の停止は別のスレッドで行う（2026-10-17 監査 C99）
        waiting = self.svc.jobs.stop_accepting()
        threading.Thread(target=self.server.stop, name="shutdown", daemon=True).start()
        return {"stopping": True, "waiting_jobs": waiting}

    @route("GET", "/environment")
    def environment(self, q, body):
        return self.svc.environment(refresh=q.get("refresh") in ("1", "true"))

    @route("GET", "/paths")
    def paths(self, q, body):
        return self.svc.paths_info()

    @route("GET", "/settings")
    def get_settings(self, q, body):
        return self.svc.settings.get()

    @route("PUT", "/settings")
    def put_settings(self, q, body):
        return self.svc.update_settings(body or {})

    @route("GET", "/settings/detect-game")
    def detect_game(self, q, body):
        from .settings import detect_game_dirs
        return {"found": detect_game_dirs()}

    @route("GET", "/providers")
    def providers(self, q, body):
        return self.svc.providers()

    # ?lang=en: 英語の画面（注意文は表示言語のもの。2026-10-18 未決定 #2）。省略・ほかの値は日本語
    @route("GET", "/consent/clip")
    def consent_status(self, q, body):
        return consent.status(self.svc.trellis_root(), q.get("lang"))

    @route("POST", "/consent/clip")
    def consent_accept(self, q, body):
        b = body or {}
        if not b.get("accept"):
            raise BackendError("INVALID_INPUT", tr("accept: true が必要です", "accept: true is required"))
        # lang: 表示した注意文の言語（省略時は SHA-256 が合う言語）
        out = consent.accept(self.svc.trellis_root(), b.get("notice_sha256", ""), lang=b.get("lang") or None)
        self.svc.invalidate_environment()
        logs.info(tr(f"CLIP の注意事項への同意を記録しました（画面の同意ボタン、注意文 {out['accepted_lang']}）",
                     f"Recorded acceptance of the CLIP notice (Agree button in the app, notice {out['accepted_lang']})"))
        return out

    # --- 初回セットアップ（docs/packaging.md 4 章） ------------------------------------------------
    @route("GET", "/setup/status")
    def setup_status(self, q, body):
        from . import setup
        return setup.status(self.svc, q.get("root") or None, q.get("lang"))

    @route("GET", r"/setup/license/(?P<cid>[a-z0-9_]+)")
    def setup_license(self, q, body, cid):
        from . import setup
        return setup.license_text(cid, q.get("lang"))

    @route("POST", "/setup/use-existing")
    def setup_use_existing(self, q, body):
        from . import setup
        return setup.use_existing(self.svc, str((body or {}).get("root") or ""))

    @route("POST", "/setup/mark")
    def setup_mark(self, q, body):
        from . import setup
        return setup.mark(self.svc, str((body or {}).get("state") or ""))

    # 削除・修復の確認用（何も変えない。実行はジョブ setup_remove・setup_repair。点検 SF04・SF16）
    @route("GET", "/setup/remove-plan")
    def setup_remove_plan(self, q, body):
        from .setup import maintenance
        scope = q.get("scope") or "all"
        if scope not in maintenance.SCOPES:
            raise BackendError("INVALID_INPUT", tr("scope は text / all のどちらかです", "scope must be text or all"),
                               params={"reason": "maint_scope"})
        return maintenance.plan(self.svc, q.get("root") or None, scope)

    @route("GET", "/setup/repair-plan")
    def setup_repair_plan(self, q, body):
        from .setup import maintenance
        return maintenance.repair_plan(self.svc, q.get("root") or None)

    # --- ジョブ -------------------------------------------------------------------------------
    @route("POST", "/jobs")
    def submit(self, q, body):
        body = body or {}
        params = body.get("params")
        job = self.svc.jobs.submit(str(body.get("type", "")), {} if params is None else params)   # 型は submit が確かめる
        return job.summary()

    @route("GET", "/jobs")
    def jobs(self, q, body):
        return [j.summary() for j in self.svc.jobs.list()[: int_param(q, "limit", 50, 0, 1000)]]

    @route("GET", r"/jobs/(?P<job_id>[A-Za-z0-9]+)")
    def job(self, q, body, job_id):
        return self.svc.jobs.get(job_id).detail()

    @route("GET", r"/jobs/(?P<job_id>[A-Za-z0-9]+)/events")
    def job_events(self, q, body, job_id):
        job = self.svc.jobs.get(job_id)
        evs, last, state = job.events_after(int_param(q, "after", 0, 0, None), float_param(q, "wait", 0.0, 0.0, 30.0))
        return {"events": evs, "last_seq": last, "state": state, "job": job.summary()}

    @route("POST", r"/jobs/(?P<job_id>[A-Za-z0-9]+)/cancel")
    def job_cancel(self, q, body, job_id):
        return self.svc.jobs.cancel(job_id).summary()

    # --- 入力画像 -----------------------------------------------------------------------------
    @route("POST", "/images/inspect")
    def image_inspect(self, q, body):
        """入力画像の確認（pipeline.inspect_image: 形式・大きさ・透明部分の有無・不透明な割合・注意）。ジョブにしない軽い
        問い合わせ。画面は WebP の透明部分の判定に使う（WIC は透明な WebP も透明なしで返す。点検 C44）。"""
        from . import pipeline
        return pipeline.inspect_image((body or {}).get("path"))

    # --- 文章の入力の補助（SF07。2026-10-17 U-A4） ---------------------------------------------------
    @route("POST", "/prompt/estimate")
    def prompt_estimate(self, q, body):
        """生成 AI が読む長さ（CLIP のトークン数、77 まで）と、読まれない後ろの部分・英語以外の語の見積もり（prompt_tokens）。
        ジョブにしない軽い問い合わせ。生成の処理は変えない。body = {text, allow_weapons}。"""
        from . import prompt_tokens
        b = body or {}
        return prompt_tokens.estimate(b.get("text"), self.svc.trellis_root(), bool(b.get("allow_weapons")))

    # --- 下書き -------------------------------------------------------------------------------
    @route("GET", "/drafts")
    def drafts(self, q, body):
        return self.svc.drafts_list()

    @route("GET", r"/drafts/(?P<draft_id>d[0-9a-f]+)")
    def draft(self, q, body, draft_id):
        return self.svc.drafts.get(draft_id).summary()

    @route("DELETE", r"/drafts/(?P<draft_id>d[0-9a-f]+)")
    def draft_delete(self, q, body, draft_id):
        self.svc.drafts.get(draft_id)
        self.svc.drafts.delete(draft_id)
        return {"deleted": draft_id}

    @route("POST", r"/drafts/(?P<draft_id>d[0-9a-f]+)/save")
    def draft_save(self, q, body, draft_id):
        b = body or {}
        # as_new: 新しい装備として保存する（下書きに記録した再生成の差し替え先を使わない。点検 R18A-01）
        return library_ops.save_draft(self.svc, draft_id, b.get("name_en", ""), b.get("name_ja"), b.get("keep_image"),
                                      b.get("replace_equipment_id"), as_new=bool(b.get("as_new")))

    # --- 装備ライブラリ -----------------------------------------------------------------------
    @route("GET", "/library")
    def library(self, q, body):
        return self.svc.library()

    @route("GET", r"/library/(?P<eq_id>eq[0-9a-f]+)")
    def equipment(self, q, body, eq_id):
        return library_ops.get_equipment(self.svc, eq_id)

    @route("PATCH", r"/library/(?P<eq_id>eq[0-9a-f]+)")
    def equipment_patch(self, q, body, eq_id):
        b = body or {}
        return library_ops.rename(self.svc, eq_id, b.get("name_en"), b.get("name_ja"))

    @route("DELETE", r"/library/(?P<eq_id>eq[0-9a-f]+)")
    def equipment_delete(self, q, body, eq_id):
        return library_ops.remove(self.svc, eq_id)

    @route("PUT", r"/library/(?P<eq_id>eq[0-9a-f]+)/texts")
    def equipment_texts(self, q, body, eq_id):
        """消耗品の名前・説明文（texts の set・set_desc。空欄なら既定の文。機能 SF11）。"""
        return library_ops.set_item_texts(self.svc, eq_id, (body or {}).get("texts"))

    @route("GET", "/library/trash")
    def library_trash(self, q, body):
        """削除した装備（trash）の一覧と、戻せるか（機能 SF02）。"""
        return library_ops.list_trash(self.svc)

    @route("POST", "/library/trash/restore")
    def library_trash_restore(self, q, body):
        return library_ops.restore_trash(self.svc, str((body or {}).get("name") or ""))

    # --- MOD ----------------------------------------------------------------------------------
    @route("GET", "/mod/status")
    def mod_status(self, q, body):
        return modops.status(self.svc)

    @route("GET", "/mod/builds")
    def mod_builds(self, q, body):
        return modops.list_builds(self.svc)

    @route("POST", "/mod/plan")
    def mod_plan(self, q, body):
        b = body or {}
        return modops.plan(self.svc, str(b.get("action", "")), b.get("build_id"), bool(b.get("replace_foreign")),
                           bool(b.get("force")), b.get("shared_id"))

    @route("POST", "/packages/inspect")
    def package_inspect(self, q, body):
        """共有ファイルの確認（装備パッケージ: 装備一覧との比較。MOD: 受け取りの展開と中身の検査）。何も導入しない。"""
        from . import equipment_package, mod_package, package_io
        b = body or {}
        path, expect = str(b.get("path") or ""), b.get("expect")
        if expect not in ("equipment", "mod"):
            with package_io.open_package(path, verify=False) as pkg:
                expect = pkg.kind
        if expect == "equipment":
            return equipment_package.inspect(self.svc, path)
        if not self.svc.mod_lock.acquire(timeout=2.0):
            raise BackendError("BUSY", tr("MOD の作成・導入の実行中です。終わってからもう一度操作してください",
                                          "A MOD build or installation is running. Try again after it finishes"))
        try:
            return mod_package.inspect(self.svc, path)
        finally:
            self.svc.mod_lock.release()

    @route("POST", "/mod/compare-code")
    def mod_compare_code(self, q, body):
        """相手が伝えた照合コードを、導入中の MOD・最新のビルド・受け取った MOD と比べる（表示だけ。2026-10-17 機能 SF13）。"""
        from . import mod_package
        return mod_package.match_code(self.svc, (body or {}).get("code"))

    @route("POST", "/mod/export")
    def mod_export(self, q, body):
        b = body or {}
        return modops.export(self.svc, b.get("build_id"), b.get("dest", ""))

    # --- データの容量と整理（2026-10-17 機能 SF03。消すのは /storage/cleanup だけで、画面で確認した ID のうち、
    # その時点でも消せるものだけ。docs: storage.py の冒頭） -----------------------------------------------------------
    @route("GET", "/storage/usage")
    def storage_usage(self, q, body):
        from . import storage
        return storage.usage(self.svc)

    @route("POST", "/storage/plan")
    def storage_plan(self, q, body):
        from . import storage
        b = body or {}
        return storage.plan(self.svc, b.get("kind"), b.get("exclude"))

    @route("POST", "/storage/cleanup")
    def storage_cleanup(self, q, body):
        from . import storage
        b = body or {}
        return storage.cleanup(self.svc, b.get("kind"), b.get("items"), b.get("exclude"))

    @route("GET", "/datastores")
    def datastores(self, q, body):
        from . import datastores as ds
        return ds.scan(self.svc.library_dir(), self.svc.home())

    @route("GET", "/logs")
    def logs_tail(self, q, body):
        return {"lines": logs.tail_file(int_param(q, "lines", 500, 0, 100000)), "dir": self.svc.paths_info()["logs"]}


def _content_length(raw: str | None) -> int:
    """Content-Length → 本文の大きさ。数字でない・負の値は INVALID_INPUT（以前は 500 になり、負の値では
    rfile.read(-1) が相手の切断まで待ち続けた。2026-10-17 監査 C101）。"""
    if raw is None or not raw.strip():
        return 0
    try:
        n = int(raw)
    except ValueError:
        raise BackendError("INVALID_INPUT", tr("Content-Length が不正です", "Invalid Content-Length")) from None
    if n < 0:
        raise BackendError("INVALID_INPUT", tr("Content-Length が不正です", "Invalid Content-Length"))
    return n


def _path_for_log(raw: str) -> str:
    """ログに書く要求のパス。先頭 LOG_PATH_MAX 文字に切り、表示できない文字（ESC・NUL など）は「?」に置き換える
    （相手の指定したパスをそのまま書くと、ログを際限なく大きくでき、偽の行も見せられた。点検 R09B-01・X5X-01）。"""
    try:
        p = urlparse(raw).path
    except ValueError:              # 「//[」で始まるなど、urlparse が読めない
        p = raw.split("?", 1)[0]
    n = len(p)
    p = "".join(c if c.isprintable() else "?" for c in p[:LOG_PATH_MAX])
    if n > LOG_PATH_MAX:
        p += tr(f"…（全 {n} 文字）", f"... ({n} characters in total)")
    return p


def _check_text(body) -> None:
    """本文の文字列に対になっていないサロゲート（JSON の "\\ud800" など）があれば INVALID_INPUT。UTF-8 にできないので、
    以前は応答・設定ファイルの書き込みの途中で失敗し、ジョブだけが登録される・応答なしで切れることがあった（点検 R09B-02）。"""
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise BackendError("INVALID_INPUT", tr("文字として不正な値（対になっていないサロゲート）が含まれています",
                                               "The body contains invalid characters (unpaired surrogates)")) from None
    except RecursionError:          # 読めた限界に近い深さの入れ子（R09A-02）
        raise BackendError("INVALID_INPUT", tr("JSON として読めません", "The body cannot be read as JSON")) from None


class _Handler(BaseHTTPRequestHandler):
    server_version = "GDSkinBackend"
    protocol_version = "HTTP/1.1"
    # 接続ごとの読み書きの時間切れ（秒）。要求を途中まで送って止まった接続がスレッドを持ち続けないように（合言葉の検査は
    # ヘッダーを読み終えた後なので、合言葉を知らない相手にもできた。点検 R09B-04・X5X-02）。時間切れは handle_one_request が
    # 接続を閉じる。WPF の保持接続の遊休の寿命（既定 1 分）と events の長い待ち（最大 30 秒。その間はソケットを読まない）より
    # 長くする。少しずつ送り続ける接続は防げない部分的な対策（同時接続数の上限は正規の画面を締め出しうるので設けない）
    timeout = 120

    def log_message(self, fmt, *args):     # 標準エラーへのアクセスログは出さない（ログファイルにはエラーだけ）
        pass

    def handle(self):
        # 保持している接続を相手が切った（次の要求の読み取りで ConnectionResetError など）のは不具合ではないので、
        # 標準エラーにトレースバックを出さずに終える（点検 R09B-02）
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True

    def _send(self, status: int, payload: dict, close: bool = False) -> None:
        # 直列化の失敗（JSON にできない返り値）は今までどおり呼び出し元（_dispatch）で INTERNAL にする
        text = json.dumps(payload, ensure_ascii=False)
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            # 対になっていないサロゲート（受け取ったファイルの名前などに由来）は「?」に置き換えて送る。backslashreplace の
            # \ud800 は WPF の System.Text.Json が読めない（以前は 500・応答なしの切断になった。点検 R09B-02）
            data = text.encode("utf-8", "replace")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if close:
                # 本文を読み終えていない要求へのエラー。読まなかった本文が次の要求として解釈されないよう、接続を閉じる
                # （send_header が close_connection も立てる。2026-10-17 監査 C101）
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, TimeoutError) as e:
            # 応答を送る前に相手が接続を切った（events の長い待ちの途中で閉じた など）・書き込みが時間切れになった。想定外の
            # エラー（INTERNAL）として記録せず、その接続にエラーの応答を書き直すこともしない（点検 R09B-02・R09B-04）
            self.close_connection = True
            try:
                logs.info(tr(f"{self.command} {_path_for_log(self.path)}: 応答を送る前に相手が接続を切りました"
                             f"（{type(e).__name__}）",
                             f"{self.command} {_path_for_log(self.path)}: the client closed the connection before the "
                             f"response was sent ({type(e).__name__})"))
            except (OSError, ValueError):
                pass

    def _error(self, e: BackendError, close: bool = False) -> None:
        try:
            self._send(e.http_status, {"ok": False, "error": e.to_dict()}, close)
        except (TypeError, ValueError):
            # エラーの中身を JSON にできない（params に JSON にできない値など）。応答なしで切らず、最小限の INTERNAL を返す
            fallback = BackendError("INTERNAL", f"{e.code}: {e.message}")
            self._send(fallback.http_status, {"ok": False, "error": fallback.to_dict()}, True)

    def _dispatch(self, method: str) -> None:
        srv: BackendServer = self.server.owner      # type: ignore[attr-defined]
        body_read = False           # 本文を読み終えたか（読み終える前のエラーでは接続を閉じる）
        authed = False              # Origin・Host・合言葉の検査を通ったか（通る前の失敗はログを間引く）
        try:
            if self.headers.get("Origin"):
                raise BackendError("FORBIDDEN", tr("ブラウザからの呼び出しは受け付けません",
                                                   "Requests from a browser are not accepted"))
            host = (self.headers.get("Host") or "").lower()
            if host not in srv.allowed_hosts:
                raise BackendError("FORBIDDEN", tr("Host が不正です", "Invalid Host"))
            auth = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(auth.encode(), f"Bearer {srv.token}".encode()):
                raise BackendError("UNAUTHORIZED", tr("合言葉（トークン）がありません・違います", "The token is missing or wrong"))
            authed = True
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            body = None
            if self.headers.get("Transfer-Encoding"):
                raise BackendError("INVALID_INPUT", tr(
                    "Transfer-Encoding の要求は受け付けません（Content-Length で送ってください）",
                    "Requests with Transfer-Encoding are not accepted (send them with Content-Length)"))
            n = _content_length(self.headers.get("Content-Length"))
            if n > MAX_BODY:
                raise BackendError("INVALID_INPUT", tr("要求が大きすぎます", "The request is too large"))
            try:
                raw = self.rfile.read(n) if n else b""
            except TimeoutError:
                # 本文を途中で止めた要求（R09B-04）。想定外のエラーにせず、接続を閉じる（body_read は False のまま）
                raise BackendError("INVALID_INPUT", tr("本文を受け取り終える前に時間切れになりました",
                                                       "Timed out before the whole body was received")) from None
            body_read = True
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):
                    # RecursionError: 深く入れ子にした配列・オブジェクト（ValueError の子ではない。点検 R09A-02）
                    raise BackendError("INVALID_INPUT", tr("JSON として読めません", "The body cannot be read as JSON")) from None
                _check_text(body)
            if body is not None and not isinstance(body, dict):
                # 各エンドポイントは本文をオブジェクトとして読む（配列・文字列は body.get の例外で INTERNAL になっていた。C102）
                raise BackendError("INVALID_INPUT", tr("本文は JSON のオブジェクトで送ってください", "Send the body as a JSON object"))
            for m, rx, name in ROUTES:
                if m != method:
                    continue
                mt = rx.match(u.path)
                if mt:
                    data = getattr(srv.api, name)(q, body, **mt.groupdict())
                    self._send(200, {"ok": True, "data": data})
                    return
            raise BackendError("NOT_FOUND", tr(f"{method} {u.path} はありません", f"{method} {u.path} does not exist"))
        except BackendError as e:
            if not authed:
                # 認証の前の失敗は合言葉を知らない相手でも起こせるので、同じ code は REJECT_LOG_INTERVAL 秒に 1 行に間引く
                # （最初の 1 件は必ず書く。画面とバックエンドの合言葉の食い違いの調査用。点検 R09B-01・X5X-01）
                omitted = srv.note_rejected(e.code)
                if omitted is not None:
                    logs.warning(f"{method} {_path_for_log(self.path)}: {e.message}"
                                 + (tr(f"（ほか {omitted} 件を省略）", f" ({omitted} more omitted)") if omitted else ""),
                                 code=e.code)
            elif e.code not in ("NOT_FOUND",):
                logs.warning(f"{method} {_path_for_log(self.path)}: {e.message}", code=e.code)
            self._error(e, close=not body_read)
        except Exception as e:  # noqa: BLE001
            logs.error(tr(f"{method} {_path_for_log(self.path)}: 想定外のエラー {type(e).__name__}: {e}",
                          f"{method} {_path_for_log(self.path)}: unexpected error {type(e).__name__}: {e}"), code="INTERNAL")
            self._error(BackendError("INTERNAL", f"{type(e).__name__}: {e}", traceback.format_exc()[-1500:]),
                        close=not body_read)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """待ち受けのポートを他のソケットに横取りさせない（2026-10-17 監査 C103）。

    http.server.HTTPServer は既定で SO_REUSEADDR を付けるので、Windows では同じポートに SO_REUSEADDR 付きの別の
    ソケットが bind でき、どちらが接続を受けるかが不定になる（合言葉と要求の中身が漏れうる）。Microsoft の推奨どおり
    SO_EXCLUSIVEADDRUSE を付ける。ポートは 0 番（OS が選ぶ）なので、再起動の時に衝突する心配は無い。"""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class BackendServer:
    def __init__(self, service, token: str, port: int = 0):
        if not token or len(token) < 16:
            raise ValueError(tr("合言葉（GDSKIN_BACKEND_TOKEN）は 16 文字以上",
                                "The token (GDSKIN_BACKEND_TOKEN) must be at least 16 characters"))
        self.service = service
        self.token = token
        self.httpd = _ExclusiveHTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.owner = self       # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.api = Api(service, self)
        self._stopped = threading.Event()
        self._rejected_lock = threading.Lock()
        self._rejected: dict[str, tuple[float, int]] = {}     # code → (最後にログに書いた時刻, その後に省いた件数)

    def note_rejected(self, code: str) -> int | None:
        """認証の前の失敗を 1 件数える → ログに書くなら、前の行の後に省いた件数。書かない（間引く）なら None。"""
        now = time.monotonic()
        with self._rejected_lock:
            last = self._rejected.get(code)
            if last is not None and now - last[0] < REJECT_LOG_INTERVAL:
                self._rejected[code] = (last[0], last[1] + 1)
                return None
            self._rejected[code] = (now, 0)
            return last[1] if last is not None else 0

    def serve_forever(self) -> None:
        try:
            self.httpd.serve_forever(poll_interval=0.25)
        finally:
            self._stopped.set()

    def stop(self) -> None:
        try:
            self.service.shutdown()
        finally:
            self.httpd.shutdown()

    def close(self) -> None:
        self.httpd.server_close()
