"""ファイルの取得（初回セットアップ。計画書 8.2 節）。

* 途中のファイルは ``<名前>.part`` に書き、大きさと SHA-256（あれば MD5 も）が合ってから本来の名前に変える
  （壊れたファイル・途中のファイルが使われる場所に置かれることはない）。
* 通信エラー（接続の失敗・切断・タイムアウト・応答の途中の切断（chunked の IncompleteRead など）・HTTP 5xx・429）は、
  待ち時間を延ばしながら再試行する。``.part`` があれば ``Range`` で続きから取得する（サーバーが ``206`` を返さなければ
  最初から）。前の試行で 1 MiB 以上進んでいれば、再試行の回数を数え直す（少しずつでも進む回線で止めない。合計の試行は
  :data:`MAX_ATTEMPTS` まで）。
* 大きさの記録が無いファイル（ソースのアーカイブ）も、応答の Content-Length（206 では Content-Range の全体の長さ）より
  短く切れたら、通信エラーとして続きから取り直す（点検 C07）。
* 証明書の検証の失敗（ウイルス対策ソフトの HTTPS の検査・社内プロキシなど）は再試行せず、DOWNLOAD_FAILED
  （params.reason = tls）にする（点検 C08）。
* 再試行しても直らない HTTP の 4xx（404・403・410 など。407・408・425・429 を除く）は再試行せず、DOWNLOAD_FAILED
  （params.reason = http_status、params.status = 番号）にする（配布元のファイルの移動・削除を通信の問題として案内しない。
  点検 R11A-04）。
* 途中のファイルに書き込めない（ウイルス対策ソフト・バックアップソフトが一時的に開いている共有違反は、open では EACCES に
  なる）ときは、少し待って :data:`LOCK_WAITS` の回数だけやり直し、それでも駄目なら DOWNLOAD_FAILED（params.reason =
  write_failed）にする。照合の後の本来の名前への変更も同じ（点検 R11B-03）。
* 照合が合わなければ ``.part`` を消して最初から 1 回だけ取り直し、それでも合わなければ DOWNLOAD_FAILED にする。
  応答が伝える大きさが大きさの記録と違う、または受信した量が記録を超えたときは、取り直さずにその場で DOWNLOAD_FAILED
  （reason = hash_mismatch）にする（点検 R11B-05）。
* 取消は 64 KiB ごとに確かめる（``.part`` は残すので、次は続きから取得できる。点検 R11B-02）。
  照合の読み直しも 4 MiB ごとに確かめる。
* HTTPS の接続は、アドレスごとに TCP の接続と TLS の握手までを試し、握手で切られた（または時間切れになった）ら、
  もう一方の系（IPv6 ↔ IPv4）のアドレスで接続し直す。以後、その取得（:class:`Downloader`）の間は、そのホストに
  もう一方の系を先に使う（それがつながらずに避けた系で通ったときは、OS の順に戻す）。IPv6 の経路で TLS の握手だけが切られる回線（2026-11-05、利用者の PC で Amazon CloudFront の
  huggingface.co・download.pytorch.org への IPv6 の握手の 3〜5 割が WinError 10054 で切られ、IPv4 は全部通った）でも
  取得を続けるため（B-038）。証明書の検証の失敗は、別のアドレスでも同じなので切り替えない。プロキシ経由（CONNECT の
  トンネル）は今までどおり。
"""
from __future__ import annotations

import errno
import functools
import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

from .. import __version__
from ..errors import BackendError, JobCancelled
from ..i18n import strip_end_period, tr

USER_AGENT = f"GDSkinStudio/{__version__} (first-run setup)"
CHUNK = 1 << 20
# 応答の 1 回の読み取りの量。read(n) は n バイトそろうまで戻らないので、1 MiB では遅い回線（32 KiB/s など）で取消が 30 秒
# 以上効かなかった（点検 R11B-02）。read1 は chunked の応答が途中で切れたときの扱いが変わるので使わない
READ_CHUNK = 64 << 10
RETRY_WAITS = (2, 5, 10, 20, 30, 45)          # 再試行の待ち時間（秒）。進まない試行が続いたときの回数 = len + 1
PROGRESS_RESET = 1 << 20                      # 1 回の試行でこれだけ進めば、再試行の回数を数え直す（点検 C33）
MAX_ATTEMPTS = 60                             # 1 ファイルの試行の合計の上限（少しずつしか進まないサーバーで止まらないように）
TIMEOUT = 60
LOCK_WAITS = (1, 2, 4)                        # 途中のファイルが一時的に開けないときの待ち時間（秒。点検 R11B-03）
REPLACE_TRIES = 6                             # 照合の後の名前の変更のやり直しの回数（0.5 秒おき）
_HTTP_RETRY = (408, 425, 429)                 # 再試行する HTTP の 4xx
_HTTP_NETWORK = (407,)                        # 再試行しないが、通信（プロキシの認証）の問題として案内する 4xx


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTPS の接続: TLS の握手で切られたら、もう一方の系（IPv6 ↔ IPv4）で接続し直す（B-038）
# ---------------------------------------------------------------------------
def _family_name(fam: int) -> str:
    return "IPv6" if fam == socket.AF_INET6 else "IPv4" if fam == socket.AF_INET else str(fam)


class _FamilyFallback:
    """1 つの取得（Downloader）の間、TLS の握手で切られた系をホストごとに覚える（次の接続から、そのホストでは
    もう一方の系を先に試す）。log は切り替えたときの 1 行（ホストと系ごとに 1 回だけ）。"""

    def __init__(self, log=None):
        self.avoid: dict[str, int] = {}
        self.logged: set[tuple[str, int]] = set()
        self.log = log or (lambda m: None)

    def note(self, host: str, failed: int, other: int, error: Exception) -> None:
        self.avoid[host] = failed
        if (host, failed) in self.logged:
            return
        self.logged.add((host, failed))
        a, b = _family_name(failed), _family_name(other)
        # Windows のエラーの文の末尾の「。」・「.」は外す（後ろに「。」「;」が続くので。2026-11-05 の実際のログで「。。」になった）
        reason = strip_end_period(f"{type(error).__name__}: {error}")
        self.log(tr(f"{host}: {a} の接続が暗号化の開始（TLS の握手）で切られたので、{b} で接続し直します"
                    f"（{reason}。この取得の間は、このホストに {b} を先に使います）",
                    f"{host}: the {a} connection was cut while starting encryption (TLS handshake), so it reconnects "
                    f"over {b} ({reason}; {b} is tried first for this host for the rest of this download)"))


def _ordered_addresses(infos: list, avoid: int | None = None) -> list:
    """getaddrinfo の結果（同じアドレスは 1 つ）を、系を交互に並べる（RFC 8305 の考え方。先頭は OS の順の先頭の系）。
    avoid の系（前に TLS の握手で切られた系）は、もう一方の系があれば後ろに回す。"""
    by_family: dict[int, list] = {}
    for info in infos:
        fam, sa = info[0], info[4]
        lst = by_family.setdefault(fam, [])
        if all(x[4] != sa for x in lst):
            lst.append(info)
    fams = list(by_family)
    if avoid in by_family and len(fams) > 1:
        fams.remove(avoid)
        fams.append(avoid)
    out = []
    for i in range(max((len(v) for v in by_family.values()), default=0)):
        out += [by_family[f][i] for f in fams if i < len(by_family[f])]
    return out


class _FallbackHTTPSConnection(http.client.HTTPSConnection):
    """アドレスごとに TCP の接続と TLS の握手までを試す HTTPS の接続。

    TCP の接続の失敗は、今までどおり（``socket.create_connection`` と同じく）次のアドレスを試す。TLS の握手で切られた・
    時間切れになったときは、同じ系の残りのアドレスは飛ばして、もう一方の系のアドレスを試す（試すのは系ごとに 1 回なので、
    時間切れが続いても待ちは系の数 × timeout まで）。証明書の検証の失敗はそのまま上げる（別のアドレスでも同じ。
    Downloader が reason = tls にする）。全部だめなら最後の例外を上げる（Downloader の再試行は今までどおり）。"""

    def __init__(self, *args, fallback: _FamilyFallback | None = None, **kw):
        super().__init__(*args, **kw)
        self._fallback = fallback

    def connect(self):
        state = self._fallback
        if state is None or self._tunnel_host:
            return super().connect()            # プロキシ経由（CONNECT のトンネル）は今までどおり
        sys.audit("http.client.connect", self, self.host, self.port)
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        host = self.host.lower()
        families = {info[0] for info in infos}
        tls_failed: set[int] = set()
        last: Exception | None = None
        for fam, type_, proto, _canon, sa in _ordered_addresses(infos, state.avoid.get(host)):
            if fam in tls_failed:
                continue
            sock = None
            try:
                sock = socket.socket(fam, type_, proto)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sa)
            except OSError as e:
                if sock is not None:
                    sock.close()
                last = e
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as e:
                if e.errno != errno.ENOPROTOOPT:
                    sock.close()
                    raise
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                if state.avoid.get(host) == fam:
                    # 避けていた系で通った（もう一方の系がつながらなかった）。次からは OS の順に戻す（IPv4 の使えない回線で、
                    # 接続のたびに IPv4 の時間切れを待たないように）
                    del state.avoid[host]
                return
            except ssl.SSLCertVerificationError:
                sock.close()
                raise
            except OSError as e:                # 切断（WinError 10054 など）・時間切れ・握手の途中の EOF
                sock.close()
                last = e
                tls_failed.add(fam)
                others = families - tls_failed
                if others:
                    state.note(host, fam, min(others, key=lambda f: f != socket.AF_INET), e)
        if last is None:
            raise OSError(f"getaddrinfo returned no address for {self.host}")
        raise last


class _FallbackHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, fallback: _FamilyFallback, context=None):
        super().__init__(context=context)
        self._fallback = fallback

    def https_open(self, req):
        return self.do_open(functools.partial(_FallbackHTTPSConnection, fallback=self._fallback), req,
                            context=self._context)


def _default_opener(log) -> urllib.request.OpenerDirector:
    """取得に使う opener（HTTPS は系を切り替える接続。HTTP・プロキシ・転送の扱いは urllib の既定のまま）。"""
    return urllib.request.build_opener(_FallbackHTTPSHandler(_FamilyFallback(log)))


def sha256_file(path: str, progress=None, check_cancel=None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            if check_cancel:
                check_cancel()
            b = f.read(CHUNK * 4)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done)
    return h.hexdigest()


def _hash_existing(path: str, want_md5: bool, check_cancel=None) -> tuple[hashlib._Hash, hashlib._Hash | None, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5() if want_md5 else None
    n = 0
    with open(path, "rb") as f:
        while True:
            if check_cancel:
                check_cancel()
            b = f.read(CHUNK * 4)
            if not b:
                break
            sha.update(b)
            if md5:
                md5.update(b)
            n += len(b)
    return sha, md5, n


class Downloader:
    """1 ファイルずつ取得する。``progress(done_bytes, total_bytes)`` はファイルごとの進捗、
    ``check_cancel()`` は取り消されていれば例外を投げる関数、``log(msg)`` はジョブのログ。"""

    def __init__(self, check_cancel=None, log=None, waits=RETRY_WAITS, timeout=TIMEOUT, opener=None, sleep=time.sleep):
        self.check_cancel = check_cancel or (lambda: None)
        self.log = log or (lambda m: None)
        self.waits = tuple(waits)
        self.timeout = timeout
        # HTTPS は、TLS の握手で切られたらもう一方の系（IPv6 ↔ IPv4）で接続し直す（B-038）。log は後から差し替えても効くように包む
        self.opener = opener or _default_opener(lambda m: self.log(m))
        self.sleep = sleep

    def fetch(self, url: str, dest: str, size: int | None = None, sha256: str | None = None, md5: str | None = None,
              progress=None, label: str | None = None) -> dict:
        """dest に取得する → {"path", "bytes", "reused", "resumed_from", "attempts"}。

        dest がすでにあり、大きさと SHA-256 が合えば取得しない（reused）。"""
        label = label or os.path.basename(dest)
        progress = progress or (lambda d, t: None)
        if os.path.isfile(dest) and (size is None or os.path.getsize(dest) == size):
            if sha256 is None or sha256_file(dest, lambda d: progress(d, size or d), self.check_cancel) == sha256:
                progress(size or os.path.getsize(dest), size or os.path.getsize(dest))
                return {"path": dest, "bytes": os.path.getsize(dest), "reused": True, "resumed_from": 0, "attempts": 0}
            self.log(tr(f"{label}: 置いてあるファイルの照合値が違うので取り直します",
                        f"{label}: the existing file's hash does not match, so it is downloaded again"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        restarted = False
        while True:
            info = self._download(url, part, size, progress, label)
            ok, why = self._verify(part, size, sha256, md5, self.check_cancel)
            if ok:
                self._replace(part, dest, label, url)
                return {"path": dest, "bytes": os.path.getsize(dest), "reused": False, **info}
            os.remove(part)
            if restarted:
                raise BackendError("DOWNLOAD_FAILED", tr(f"{label}: 取得したファイルが記録と合いません（{why}）",
                                                         f"{label}: the downloaded file does not match the record ({why})"),
                                   params={"file": label, "reason": "hash_mismatch", "url": url})
            self.log(tr(f"{label}: 取得したファイルが記録と合わないので、最初から取り直します（{why}）",
                        f"{label}: the downloaded file does not match the record, so it is downloaded again from the "
                        f"beginning ({why})"))
            restarted = True

    def _replace(self, part: str, dest: str, label: str, url: str) -> None:
        """照合した ``.part`` を本来の名前に変える。ほかのプロセスが一時的に開いている（PermissionError）ときは 0.5 秒おきに
        やり直し、それでも駄目なら DOWNLOAD_FAILED（reason = write_failed）にする（以前は想定外のエラーになっていた。R11B-03）。"""
        for n in range(1, REPLACE_TRIES + 1):
            try:
                os.replace(part, dest)
                return
            except PermissionError as e:
                if n == REPLACE_TRIES:
                    raise BackendError("DOWNLOAD_FAILED", tr(f"{label}: 取得したファイルの名前を変えられません（{_describe(e)}）",
                                                             f"{label}: cannot rename the downloaded file ({_describe(e)})"),
                                       params={"file": label, "reason": "write_failed", "url": url, "path": dest,
                                               "error": _describe(e)}) from e
                self.check_cancel()
                self.sleep(0.5)

    @staticmethod
    def _verify(path: str, size: int | None, sha256: str | None, md5: str | None, check_cancel=None) -> tuple[bool, str]:
        n = os.path.getsize(path)
        if size is not None and n != size:
            return False, tr(f"大きさ {n:,} バイト（記録 {size:,}）", f"size {n:,} bytes (recorded {size:,})")
        if sha256 or md5:
            sha, m, _ = _hash_existing(path, bool(md5), check_cancel)
            if sha256 and sha.hexdigest() != sha256:
                return False, tr(f"SHA-256 {sha.hexdigest()[:16]}…（記録 {sha256[:16]}…）",
                                 f"SHA-256 {sha.hexdigest()[:16]}… (recorded {sha256[:16]}…)")
            if md5 and m.hexdigest() != md5:
                return False, tr(f"MD5 {m.hexdigest()}（記録 {md5}）", f"MD5 {m.hexdigest()} (recorded {md5})")
        return True, ""

    def _download(self, url: str, part: str, size: int | None, progress, label: str) -> dict:
        attempts = 0
        first_offset = os.path.getsize(part) if os.path.isfile(part) else 0
        if size is not None and first_offset > size:
            os.remove(part)
            first_offset = 0
        if first_offset:
            self.log(tr(f"{label}: 途中のファイル（{first_offset:,} バイト）の続きから取得します",
                        f"{label}: resuming the download from the partial file ({first_offset:,} bytes)"))
        fails = 0                       # 進まない（1 MiB 未満の）失敗が続いた回数。待ち時間と打ち切りに使う
        locked = 0                      # 途中のファイルに書き込めなかった回数（通信の失敗とは別に数える。R11B-03）
        while True:
            self.check_cancel()
            offset = os.path.getsize(part) if os.path.isfile(part) else 0
            if size is not None and offset == size:
                return {"resumed_from": first_offset, "attempts": attempts}
            attempts += 1
            try:
                self._once(url, part, offset, size, progress)
                return {"resumed_from": first_offset, "attempts": attempts}
            except (Cancelled, JobCancelled, BackendError):
                raise
            except Exception as e:  # noqa: BLE001  通信エラーは再試行する
                if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                    raise BackendError("DISK_SPACE_LOW", tr(f"{label}: ディスクの空きが足りません",
                                                            f"{label}: not enough free disk space"),
                                       params={"file": label, "path": part}) from e
                reason = _describe(e)
                if isinstance(e, OSError) and e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                    # 途中のファイルに書き込めない。共有違反（ウイルス対策ソフトの検査など）は open では winerror の無い
                    # EACCES になり、数秒で解けることが多いので待ってやり直す。通信の問題としては案内しない（R11B-03）
                    locked += 1
                    if e.errno == errno.EROFS or locked > len(LOCK_WAITS):
                        raise BackendError("DOWNLOAD_FAILED", tr(f"{label}: 途中のファイルに書き込めません（{reason}、{attempts} 回試行）",
                                                                 f"{label}: cannot write to the partial file ({reason}, "
                                                                 f"{attempts} attempt(s))"),
                                           params={"file": label, "reason": "write_failed", "url": url, "attempts": attempts,
                                                   "path": part, "error": reason}) from e
                    wait = LOCK_WAITS[locked - 1]
                    self.log(tr(f"{label}: 途中のファイルに書き込めません（{reason}）。ほかのアプリが開いている可能性があるので、"
                                f"{wait} 秒後にやり直します（{locked}/{len(LOCK_WAITS)}）",
                                f"{label}: cannot write to the partial file ({reason}). Another app may have it open, so "
                                f"it is retried in {wait} s ({locked}/{len(LOCK_WAITS)})"))
                    for _ in range(int(wait * 4)):
                        self.check_cancel()
                        self.sleep(0.25)
                    continue
                if _tls_error(e):
                    # 証明書の検証の失敗は一時的な失敗ではない（HTTPS を検査するウイルス対策ソフト・社内プロキシなど）
                    raise BackendError("DOWNLOAD_FAILED", tr(f"{label} を取得できません（証明書を検証できません: {reason}）",
                                                             f"Cannot download {label} (the certificate cannot be verified: "
                                                             f"{reason})"),
                                       params={"file": label, "reason": "tls", "url": url, "attempts": attempts,
                                               "error": reason}) from e
                done = os.path.getsize(part) if os.path.isfile(part) else 0
                # 前の試行で進んでいれば数え直す（Range で続きから取れるので、途中で何度切れても完了まで進める）
                fails = 1 if done - offset >= PROGRESS_RESET else fails + 1
                if not _retryable(e) or fails > len(self.waits) or attempts >= MAX_ATTEMPTS:
                    params = {"file": label, "reason": "network", "url": url, "attempts": attempts, "error": reason}
                    if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code not in _HTTP_RETRY + _HTTP_NETWORK:
                        # 配布元がファイルを返さない（移動・削除・ゲート付き、プロキシ・フィルターの遮断）。再試行しても
                        # 直らないので、通信の確認ではなく配布元の変更を案内する（R11A-04）
                        params.update(reason="http_status", status=e.code)
                    raise BackendError("DOWNLOAD_FAILED", tr(f"{label} を取得できません（{reason}、{attempts} 回試行）",
                                                             f"Cannot download {label} ({reason}, {attempts} attempt(s))"),
                                       params=params)
                wait = self.waits[fails - 1]
                self.log(tr(f"{label}: 通信エラー（{reason}）。{wait} 秒後に再試行します（{fails}/{len(self.waits)}、"
                            f"{done:,} バイトまで取得済み）",
                            f"{label}: network error ({reason}). Retrying in {wait} s ({fails}/{len(self.waits)}, "
                            f"{done:,} bytes downloaded so far)"))
                for _ in range(int(wait * 4)):
                    self.check_cancel()
                    self.sleep(0.25)

    def _once(self, url: str, part: str, offset: int, size: int | None, progress) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 416 and offset:          # 範囲が不正 → 最初から
                os.remove(part)
                raise ConnectionResetError("range not satisfiable; restarting") from e
            raise
        with resp:
            status = getattr(resp, "status", 200)
            if offset and status != 206:           # 続きからの取得に応じないサーバー → 最初から
                offset = 0
            # 応答が伝える全体の長さ（206 は Content-Range の全体、200 は Content-Length。無ければ None）。大きさの記録が
            # 無いファイルでも、これより短く切れたら通信エラーにする（切れた ZIP を完成品として残さない。点検 C07）
            announced = _announced_total(resp.headers, offset if status == 206 else 0)
            if size is not None and announced is not None and announced != size:
                # 配布元のファイルが記録と違う大きさ（差し替え・プロキシのエラーページなど）。読み始める前に止める（全体を
                # 2 回取得してから照合で失敗していた。点検 R11B-05）
                self._size_mismatch(part, announced, size, url)
            total = size if size is not None else announced
            mode = "ab" if offset else "wb"
            done = offset
            progress(done, total or 0)
            too_long = False
            with open(part, mode) as f:
                while True:
                    self.check_cancel()
                    b = resp.read(READ_CHUNK)
                    if not b:
                        break
                    if size is not None and done + len(b) > size:
                        too_long = True     # 記録より長い応答（Content-Length の無いもの）。書かずに止める（点検 R11B-05）
                        break
                    f.write(b)
                    done += len(b)
                    progress(done, total or done)
        if too_long:
            self._size_mismatch(part, None, size, url)
        if total is not None and done < total:
            raise ConnectionResetError(f"connection closed at {done:,} of {total:,} bytes")

    @staticmethod
    def _size_mismatch(part: str, announced: int | None, size: int, url: str) -> None:
        """配布元のファイルの大きさが記録と違う → 途中のファイルを消して DOWNLOAD_FAILED（reason = hash_mismatch）。
        取り直しても同じなので、照合の後の「最初から 1 回取り直す」を通さない（点検 R11B-05）。"""
        try:
            os.remove(part)
        except OSError:
            pass
        label = os.path.basename(part)[:-len(".part")] if part.endswith(".part") else os.path.basename(part)
        got = tr(f"{announced:,} バイト", f"{announced:,} bytes") if announced is not None else \
            tr(f"{size:,} バイトより大きい", f"larger than {size:,} bytes")
        raise BackendError("DOWNLOAD_FAILED", tr(f"{label}: 配布元のファイルの大きさ（{got}）が記録（{size:,} バイト）と違います",
                                                 f"{label}: the size of the file at the source ({got}) differs from the "
                                                 f"record ({size:,} bytes)"),
                           params={"file": label, "reason": "hash_mismatch", "url": url, "size": size, "announced": announced})


def _announced_total(headers, offset: int) -> int | None:
    """応答のヘッダーの全体の長さ: Content-Range（bytes a-b/全体）があればその全体、無ければ Content-Length + offset。"""
    m = re.match(r"\s*bytes\s+\d+-\d+/(\d+)\s*$", headers.get("Content-Range") or "")
    if m:
        return int(m.group(1))
    cl = (headers.get("Content-Length") or "").strip()
    return int(cl) + offset if cl.isdigit() else None


def _tls_error(e: Exception) -> bool:
    """証明書の検証の失敗か（urllib は URLError(reason=SSLCertVerificationError) で包む）。"""
    inner = e.reason if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError) else e
    return isinstance(inner, ssl.SSLCertVerificationError)


def _retryable(e: Exception) -> bool:
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code in _HTTP_RETRY
    if isinstance(e, OSError) and e.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOSPC):
        return False                      # 書き込みの失敗（権限・ディスク）は再試行しても直らない
    # http.client.IncompleteRead（chunked の応答が途中で切れた）・BadStatusLine など、応答の途中で起きる HTTP の
    # 例外も通信エラーとして再試行する（RemoteDisconnected は ConnectionResetError の子。点検 C33）
    return isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
                          http.client.HTTPException))


def _describe(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return tr(f"接続できません: {strip_end_period(str(e.reason))}", f"cannot connect: {strip_end_period(str(e.reason))}")
    return f"{type(e).__name__}: {strip_end_period(str(e))}"
